/**
 * T3 — the uploaded project loop (audit REPORT-1951 fix #5). Switched on with ?features=project (or all).
 *
 * Sheets uploaded together (one upload folder: /api/upload/set) are ONE project (backend stoa/features/project.py,
 * /api/feat/project/*): every sheet gets a role and a level read from its title block or file name (the user can rename
 * both), a switcher lists the sheets by level, and two scope chips choose what a question is about:
 *   - This sheet: the normal ask on the sheet on screen;
 *   - This building: the question is asked on every floor plan the backend picks (one per level; never ceiling plans,
 *     sections, elevations, or two plans of one level), each through the normal /ask (private mode, caches and F7's
 *     corrections apply), and the readings are added up there — or, when that is not safe, the per-level values are
 *     shown with the reason there is no total.
 * Every answered quantity lands in the project's review list (source sheet, value, evidence link, a 'checked' tick, the
 * user's corrected value — kept on the server, so it survives a reload and goes to F7's memory); questions without a
 * usable answer are listed as open. Export: CSV.
 *
 * Fail closed: any error removes the feature's UI (registry); nothing here changes the normal flow when it is off.
 */
import { registerFeature, type FeatureApi } from "../registry";
import type { Answer, FileInfo } from "../../types";
import { upload, uploadSet } from "../../api";
import { confidenceWords } from "../../customer";

type Sheet = { id: string; file: string; page: number; number: string; title: string; kind: string; role: string; kind_source: string;
  storey: string | null; level: string; level_source: string; building: string; partial: boolean; include?: boolean | null; label: string;
  copy_of?: string; copy_how?: string; medium?: string };
type Level = { storey: string | null; level: string; building: string; sheets: string[] };
type PerSheet = { sheet: string; label: string; level: string; file: string; page: number; value: unknown; unit: string | null; confidence: number;
  counted: boolean; note: string; n_evidence: number };
type Item = { id: string; question: string; scope: "sheet" | "building"; file: string; page: number; sheet: string; level: string; value: unknown;
  unit: string | null; confidence: number; solver: string; n_evidence: number; per_sheet?: PerSheet[] | null; why_open: string; status: string;
  checked: boolean; corrected: { value: unknown; unit: string | null; note: string; at: string } | null;
  ambiguous?: { label: string; n: number; why: string; choices: { n: number; label: string; value: number; unit: string; page?: number }[] } | null };
type Project = { id: string; name: string; files: string[]; sheets: Sheet[]; levels: Level[]; items: Item[]; open: Item[]; rev: number; corrections: boolean };
type PlanOut = { sheets: { id: string; file: string; page: number; label: string; level: string; role: string }[]; skipped: { id: string; label: string; why: string }[];
  reason: string; confidence: number; sheet_question: string; floor_area?: boolean };
type BuildingAnswer = Answer & { per_sheet: PerSheet[]; no_total_why: string; scope: "building" };

const DRAWING = /\.(pdf|png|jpe?g)$/i;
/** a question about the whole building / every level (stoa/features/project.py _SCOPE_WORDS says the same) */
const BUILDING_Q = /\b(?:whole|entire|complete)\s+(?:building|school|house|home|project|facility|dorm\w*)\b|\b(?:all|every|each)\s+(?:the\s+)?(?:floors|levels|storeys|stories)\b|\b(?:does|do)\s+the\s+(?:building|school|house)\s+(?:have|contain)\b|\b(?:in|of|across)\s+the\s+(?:building|school)\b/i;
/** a question naming a level ('on the first floor', 'Level 02', 'floor 3', 'basement') */
// a whole-floor area ('total floor area of the school / on this plan', 'gross internal area', 'area of all rooms'),
// not a room type's ('total floor area of all Group Rooms'): the server sums the sheet's traced rooms (stoa/features/project.py)
const FLOOR_AREA_Q = /\b(?:gross|net)\s+(?:internal\s+)?(?:floor\s+)?area\b|\b(?:GIA|GIFA|NIA)\b|\b(?:total|overall)\s+(?:floor\s+)?area\b(?:\s*[?.!]?\s*$|\s+(?:of|on|in)\s+(?:the\s+|this\s+)?(?:whole\s+|entire\s+)?(?:building|school|house|project|plan|floor|level|storey|sheet|drawing)\b)|\barea\s+of\s+all\s+(?:the\s+)?(?:rooms|spaces)\b|\bfloor\s+area\s+(?:of|on|in)\s+(?:the\s+|this\s+)?(?:whole\s+|entire\s+)?(?:building|school|house|project|plan|floor|level|storey|sheet|drawing)\b/i;
const LEVEL_Q = /\b(?:ground|first|second|third|fourth|fifth|sixth|1st|2nd|3rd|\d{1,2}th)\s+(?:floor|level|storey|story)\b|\blevel\s*-?\d{1,2}\b|\b(?:floor|storey)\s*\d{1,2}\b|\bbasement\b/i;
/** a question about one sheet / one level */
const THIS_SHEET_Q = /\b(?:this|the)\s+(?:sheet|plan|drawing|page)\b|\bthis\s+(?:floor|level|storey)\b|\b(?:on|of)\s+(?:the\s+)?(?:ground|first|second|third|fourth|top|1st|2nd|3rd)\s+(?:floor|level|storey)\b|\blevel\s*-?\d/i;
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const fmt = (v: unknown, unit?: string | null) => {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  const s = Number.isFinite(n) ? (Number.isInteger(n) ? n.toLocaleString("en") : n.toLocaleString("en", { maximumFractionDigits: 2 })) : String(v);
  const u = unit && unit !== "count" ? ` ${unit.replace(/^m2$/, "m²").replace(/^ft2$/, "ft²").replace(/^m3$/, "m³")}` : "";
  return s + u;
};
const fileStem = (n: string) => n.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "project";
/** the card's own words for a confidence (customer.ts), lower-case for the list */
const sure = (c: number) => (c > 0 ? confidenceWords(c).word.toLowerCase() : "no reading");

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let d = r.statusText;
    try {
      d = (await r.json()).detail ?? d;
    } catch {
      /* not json */
    }
    throw new Error(`${r.status} ${d}`);
  }
  return (await r.json()) as T;
}
const P = (pid: string, tail = "") => `/api/feat/project/${encodeURIComponent(pid)}${tail}`;

/** The project's files for the file on screen: the drawings of its upload folder (FileInfo.set), or the file alone. */
function filesOf(f: FileInfo | null): string[] {
  if (!f || f.source !== "upload") return [];
  const set = ((f as FileInfo & { set?: string[] }).set ?? []).filter((p) => DRAWING.test(p));
  return set.length >= 2 ? set : DRAWING.test(f.path) ? [f.path] : [];
}

/** What a host without openFile() can still do: open the file through the address bar (?file=), which every view
 *  supports. W5 asked for openFile(path, page) (inbox 20:20); used as soon as the registry has it. */
async function openSheet(api: FeatureApi, s: { file: string; page: number }) {
  try {
    if (api.file()?.path === s.file) {
      if (s.page !== api.page()) await api.openPage(s.page);
      return;
    }
    return await api.openFile(s.file, s.page); // W5 48f4418
  } catch (e) {
    console.warn("[project] openFile failed, opening through the address bar:", e);
  }
  const u = new URL(location.href);
  u.searchParams.set("file", s.file);
  u.searchParams.delete("q");
  location.href = u.toString();
}

const STYLE = `
.prj { display: flex; flex-direction: column; gap: 14px; font-size: 14.5px; }
.prj h4 { margin: 4px 0 6px; font-size: 12px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
.prj-name { font-size: 15px; font-weight: 600; }
.prj-sub { color: var(--muted); font-size: 13px; }
.prj-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.prj-chip { min-height: 36px; padding: 0 14px; border: 1px solid var(--line); border-radius: 999px; background: var(--fill); color: var(--ink); font: inherit; font-size: 14px; cursor: pointer; }
.prj-chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
.prj-lv { display: flex; flex-direction: column; gap: 4px; }
.prj-lvname { font-size: 12.5px; font-weight: 600; color: var(--muted); margin-top: 6px; }
.prj-sheet { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 40px; padding: 6px 10px; border: 0; border-radius: 10px; background: transparent; color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
.prj-sheet:hover { background: var(--fill); }
.prj-sheet.on { background: var(--fill-2); font-weight: 600; }
.prj-sheet .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prj-role { flex: none; font-size: 11.5px; padding: 2px 8px; border-radius: 999px; background: var(--fill); color: var(--muted); }
.prj-role.off { text-decoration: line-through; }
.prj-edit { flex: none; border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 12.5px; cursor: pointer; padding: 4px 6px; }
.prj-form { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; padding: 6px 10px 10px; }
.prj-form input[type=text], .prj-form input:not([type]), .prj-form select { min-height: 32px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); font: inherit; font-size: 13.5px; padding: 0 8px; }
.prj-btn { min-height: 36px; padding: 0 14px; border: 0; border-radius: 999px; background: var(--accent); color: #fff; font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; }
.prj-btn.ghost { background: var(--fill); color: var(--ink); font-weight: 500; }
.prj-btn:disabled { opacity: .5; cursor: default; }
.prj-ask { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-radius: 12px; background: var(--fill); }
.prj-ask textarea { min-height: 52px; resize: vertical; border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; background: var(--panel); color: var(--ink); font: inherit; font-size: 14.5px; }
.prj-total { font-size: 26px; font-weight: 700; letter-spacing: -.01em; }
.prj-why { color: var(--muted); font-size: 13px; }
.prj-warn { color: #b45309; font-size: 13.5px; }
.prj-tbl { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.prj-tbl td, .prj-tbl th { padding: 6px 4px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.prj-tbl th { font-size: 12px; color: var(--muted); font-weight: 600; }
.prj-tbl .n { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.prj-tbl .skip { color: var(--muted); }
.prj-item { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 10px; align-items: start; padding: 8px 0; border-bottom: 1px solid var(--line); }
.prj-item input[type=checkbox] { width: 18px; height: 18px; margin-top: 2px; accent-color: var(--accent); }
.prj-q { font-size: 14px; }
.prj-src { grid-column: 2 / 4; color: var(--muted); font-size: 12.5px; }
.prj-val { text-align: right; font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; }
.prj-val s { color: var(--muted); font-weight: 400; margin-right: 6px; }
.prj-acts { grid-column: 2 / 4; display: flex; gap: 6px; flex-wrap: wrap; }
.prj-link { border: 0; background: transparent; color: var(--accent); font: inherit; font-size: 13px; cursor: pointer; padding: 2px 0; }
.prj-open { padding: 8px 10px; border-radius: 10px; background: var(--fill); margin-bottom: 6px; }
.prj-progress li { color: var(--muted); font-size: 13px; }
.prj-progress li.done { color: var(--ink); }
.prj-progress { margin: 0; padding-left: 18px; }
`;

registerFeature("project", (api) => {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.append(style);

  let proj: Project | null = null;
  let scope: "sheet" | "building" = "sheet";
  let editing = "";
  let correcting = "";
  let busy = "";
  let progress: { label: string; state: "todo" | "now" | "done"; value?: string }[] = [];
  let last: BuildingAnswer | null = null;
  let lastQ = "";
  let loading = "";
  // the backend router is mounted only when the API's STOA_FEATURES lists 'project' (or 'all')
  let served = false;
  const mounted: Promise<boolean> = fetch("/api/feat").then((r) => (r.ok ? r.json() : null))
    .then((j: { mounted?: string[] } | null) => (served = !!j?.mounted?.includes("project"))).catch(() => false)
    .then((ok) => {
      rerender();
      return ok;
    });
  const rereads = new Set<string>();

  const panel = api.addPanel({ id: "project", title: "Project", render: (el) => render(el) });
  const btn = api.addToolbarButton({ id: "project", label: "Project", title: "This upload as one project: sheets by level, building totals, review list",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21h18M5 21V7l7-4 7 4v14M9 21v-6h6v6"/></svg>`,
    onClick: () => panel.toggle(), when: () => !!proj && proj.sheets.length >= 2 });
  void btn;
  // Until main.ts links several dropped drawings itself (asked of W5 20:20 / 20:36), this button makes a project from
  // several files: upload each (plain, no style tag), link them (/api/upload/set: one folder), open the first sheet.
  const pick = document.createElement("input");
  pick.type = "file";
  pick.multiple = true;
  pick.accept = ".pdf,.png,.jpg,.jpeg";
  pick.hidden = true;
  document.body.append(pick);
  pick.addEventListener("change", async () => {
    const fs = [...(pick.files ?? [])].filter((f) => DRAWING.test(f.name));
    pick.value = "";
    if (fs.length < 2) return;
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="project"][data-id="newproject"] span:last-child');
    try {
      const got: FileInfo[] = [];
      for (const [i, f] of fs.entries()) {
        if (b) b.textContent = `Uploading ${i + 1} of ${fs.length}…`;
        got.push(await upload(f, ""));
      }
      if (b) b.textContent = "Linking the sheets…";
      const linked = await uploadSet(got.map((g) => g.path));
      await openSheet(api, { file: linked[0].path, page: 0 });
    } catch (e) {
      if (b) b.textContent = `Could not make the project: ${String((e as Error).message ?? e).slice(0, 60)}`;
      return;
    }
  });
  const newBtn = api.addToolbarButton({ id: "newproject", label: "Make a project…", title: "Upload several sheets of one building as one project",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>`,
    onClick: () => pick.click(), when: () => served && (!proj || proj.sheets.length < 2) });
  void newBtn;

  const rerender = () => {
    if (panel.isOpen()) render(panel.el);
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="project"][data-id="project"] span:last-child');
    if (b && proj) b.textContent = `Project · ${proj.sheets.length} sheets`;
    const bb = document.querySelector<HTMLElement>('.feat-btn[data-feature="project"][data-id="project"]');
    if (bb) bb.hidden = !proj || proj.sheets.length < 2;
    const nb = document.querySelector<HTMLElement>('.feat-btn[data-feature="project"][data-id="newproject"]');
    if (nb) nb.hidden = !served || (!!proj && proj.sheets.length >= 2);
  };

  async function load(f: FileInfo | null) {
    if (!(await mounted)) return; // the API does not serve /feat/project: show nothing (fail closed)
    const files = filesOf(f);
    if (!files.length) {
      proj = null;
      rerender();
      return;
    }
    const key = [...files].sort().join("|");
    if (proj && [...proj.files].sort().join("|") === key) return rerender();
    if (loading === key) return;
    loading = key;
    try {
      const first = !proj;
      proj = await j<Project>("/api/feat/project/open", { method: "POST", body: JSON.stringify({ files }) });
      rerender();
      if (first && proj.sheets.length >= 2 && !panel.isOpen()) panel.open();
      // pictures: their text layer (title block, level) is read by the upload's warm-up in the background; read the
      // sheets again once it has had time to land (the server re-reads only sheets it could not read before)
      if (proj.sheets.some((s) => s.kind_source === "not read" || !s.level) && !rereads.has(key)) {
        rereads.add(key);
        for (const ms of [45_000, 120_000, 300_000])
          window.setTimeout(() => {
            if (!proj || [...proj.files].sort().join("|") !== key) return;
            void j<Project>("/api/feat/project/open", { method: "POST", body: JSON.stringify({ files }) }).then((p) => {
              if (proj && proj.id === p.id) {
                proj = { ...p };
                rerender();
              }
            }).catch(() => undefined);
          }, ms);
      }
    } catch (e) {
      console.warn("[project] could not open the project:", e);
      proj = null;
      rerender();
    } finally {
      loading = "";
    }
  }

  api.onDrawing((f) => void load(f));
  api.onUpload(() => void load(api.file())); // several drawings dropped together = one linked folder (W5 48f4418)
  void load(api.file());
  // 'This building' answers through the normal question box too (W5's beforeAsk): the per-level table fills in the
  // panel, the total shows on the card like any answer
  // The question's own words win over the chip: 'in the whole building' / 'on all floors' asks every level even with
  // 'This sheet' picked (one sheet cannot answer it: 'the sheet prints no total'); 'on this plan' stays on the sheet.
  api.beforeAsk((q, f) => {
    if (!proj || !q.trim() || !proj.files.includes(f.path) || proj.levels.filter((l) => l.storey !== null).length < 2) return null;
    if (LEVEL_Q.test(q) && !BUILDING_Q.test(q)) return onLevel(q, f); // 'on the first floor' while another level is open
    const sheetQ = THIS_SHEET_Q.test(q);
    const buildingQ = BUILDING_Q.test(q) && !sheetQ;
    if (!buildingQ && (scope !== "building" || sheetQ)) return FLOOR_AREA_Q.test(q) ? onFloorArea(q, f) : null;
    if (scope !== "building") {
      scope = "building";
      rerender();
    }
    return askBuilding(q);
  });

  // every answer on a sheet of the project -> its review list (numbers; a refusal = an open question)
  api.onAnswer((a, q) => {
    const f = api.file();
    if (!proj || !f || !q || !proj.files.includes(f.path)) return;
    if ((a as Partial<BuildingAnswer>).scope === "building" || a.solver === "project/sheets") return; // stored by /combine
    if (((a as unknown as { learned?: { changed?: boolean }[] }).learned ?? []).some((l) => l.changed)) return; // F7 applied the user's value
    if (a.value !== null && typeof a.value !== "number" && !/^-?[\d.,]+$/.test(String(a.value))) return;
    const slim = { value: a.value, unit: a.unit, confidence: a.confidence, solver: a.solver, evidence: (a.evidence ?? []).slice(0, 20), missing: a.missing ?? [],
      assumptions: (a.assumptions ?? []).slice(0, 8) };  // 'N rooms are labelled X … their mean' makes it a 'Which one?' question
    void j<{ project: Project }>(P(proj.id, "/items"), { method: "POST", body: JSON.stringify({ question: q, file: f.path, page: api.page(), answer: slim }) })
      .then((r) => {
        proj = r.project;
        rerender();
      }).catch((e) => console.warn("[project] review list:", e));
  });

  /** 'This building': ask every floor plan the backend picks, then add up there. */
  /** A question naming a level: open that level's plan and ask there (the card follows the sheet); else the sheet on
   *  screen answers as usual. Never rejects. */
  async function onLevel(q: string, f: FileInfo): Promise<Answer> {
    const here = { page: api.page(), show: false as const };
    try {
      const r = await j<{ sheet: { id: string; file: string; page: number; label: string; level: string } | null }>(P(proj!.id, "/route"),
        { method: "POST", body: JSON.stringify({ question: q, current: `${f.path}#${api.page()}` }) });
      if (r.sheet) {
        if (scope !== "sheet") scope = "sheet";
        await openSheet(api, r.sheet);
        rerender();
        return await api.askWith(q, { page: r.sheet.page }); // shown on the level's own sheet
      }
    } catch (e) {
      console.warn("[project] level route:", e);
    }
    return api.askWith(q, here).catch((e) => ({ value: null, unit: null, confidence: 0, evidence: [], trace: [String(e)], solver: "project",
      missing: [`the question failed: ${String((e as Error).message ?? e)}`] }) as Answer);
  }

  /** One sheet's net floor area = the sum of its traced rooms (server); null when it cannot (a picture, no scale). */
  async function floorArea(sheet: string): Promise<Answer | null> {
    if (!proj) return null;
    try {
      const a = await j<Answer & { fallback?: boolean }>(P(proj.id, "/floor_area"), { method: "POST", body: JSON.stringify({ sheet }) });
      return a && a.value !== null && !a.fallback ? a : null;
    } catch (e) {
      console.warn("[project] floor area:", e);
      return null;
    }
  }
  /** 'What is the total floor area on this plan?' on a project sheet: the traced rooms' sum, else the usual reader. Never rejects. */
  async function onFloorArea(q: string, f: FileInfo): Promise<Answer> {
    const a = await floorArea(`${f.path}#${api.page()}`);
    if (a) return a;
    return api.askWith(q, { page: api.page(), show: false }).catch((e) => ({ value: null, unit: null, confidence: 0, evidence: [], trace: [String(e)], solver: "project",
      missing: [`the question failed: ${String((e as Error).message ?? e)}`] }) as Answer);
  }

  /** Never rejects (a rejected beforeAsk promise switches the feature off): errors come back as an answer that says so. */
  async function askBuilding(q: string): Promise<Answer> {
    if (!proj || !q.trim()) return { value: null, unit: null, confidence: 0, evidence: [], trace: ["project: no project open"], solver: "project/sheets" };
    if (!panel.isOpen()) panel.open();
    busy = "building";
    last = null;
    lastQ = q;
    progress = [];
    rerender();
    try {
      const pl = await j<PlanOut>(P(proj.id, "/plan"), { method: "POST", body: JSON.stringify({ question: q }) });
      progress = pl.sheets.map((s) => ({ label: `${s.level || "No level"} · ${s.label}`, state: "todo" as const }));
      rerender();
      const parts: { sheet: string; answer: unknown }[] = [];
      for (let i = 0; i < pl.sheets.length; i++) {
        const s = pl.sheets[i];
        progress[i].state = "now";
        rerender();
        let a: Answer | null = null;
        try {
          // a whole-floor area: the level's traced rooms summed (the general readers only guess it), else the usual ask
          if (pl.floor_area) a = await floorArea(s.id);
          if (!a) a = await api.askWith(pl.sheet_question, { file: s.file, page: s.page, show: false });
        } catch (e) {
          console.warn("[project] sheet ask failed:", s.label, e);
        }
        progress[i].state = "done";
        progress[i].value = a ? fmt(a.value, a.unit) : "no answer";
        parts.push({ sheet: s.id, answer: a ? { value: a.value, unit: a.unit, confidence: a.confidence, solver: a.solver, evidence: (a.evidence ?? []).slice(0, 500),
          cost: a.cost ?? {}, missing: a.missing ?? [], assumptions: (a.assumptions ?? []).slice(0, 4) } : { value: null } });
        rerender();
      }
      const r = await j<{ answer: BuildingAnswer; project: Project }>(P(proj.id, "/combine"), { method: "POST", body: JSON.stringify({ question: q, parts }) });
      proj = r.project;
      last = r.answer;
      if (pl.skipped.length) last.assumptions = [...(last.assumptions ?? []), ...pl.skipped.map((s) => `${s.label}: left out (${s.why})`)];
    } catch (e) {
      last = { value: null, unit: null, confidence: 0, evidence: [], trace: [String(e)], solver: "project/sheets", per_sheet: [], missing: [`the building answer failed: ${String((e as Error).message ?? e)}`],
        no_total_why: String((e as Error).message ?? e), scope: "building" } as BuildingAnswer;
    } finally {
      busy = "";
      rerender();
    }
    return last!;
  }

  async function patchItem(id: string, body: Record<string, unknown>) {
    if (!proj) return;
    const r = await j<{ project: Project; item: Item & { f7?: string } }>(P(proj.id, `/items/${encodeURIComponent(id)}`), { method: "POST", body: JSON.stringify(body) });
    proj = r.project;
    rerender();
    // a corrected value on the sheet on screen went to F7's memory: ask again with F7's NEW revision (its client still
    // sends the old one until the next answer, and that one is cached), so the card shows the user's value right away
    const it = r.item;
    if (it?.f7 && it.scope === "sheet" && api.file()?.path === it.file && body.value !== undefined) {
      try {
        const c = await j<{ rev?: number }>(`/api/feat/corrections?path=${encodeURIComponent(it.file)}`);
        if (c.rev) await api.askWith(it.question, { page: it.page, solver_args: { learn: c.rev } });
      } catch {
        /* F7 not served: the review list keeps the user's value anyway */
      }
    }
  }
  async function patchSheet(sheet: string, body: Record<string, unknown>) {
    if (!proj) return;
    proj = await j<Project>(P(proj.id, "/sheet"), { method: "POST", body: JSON.stringify({ sheet, ...body }) });
    rerender();
  }
  async function showItem(it: Item) {
    if (it.scope === "building") {
      if (it.per_sheet?.length) {
        last = { value: it.value, unit: it.unit, confidence: it.confidence, evidence: [], trace: [], solver: it.solver, per_sheet: it.per_sheet,
          no_total_why: it.why_open, scope: "building" } as BuildingAnswer;
        lastQ = it.question;
        scope = "building";
        rerender();
      }
      return;
    }
    if (api.file()?.path !== it.file) return openSheet(api, { file: it.file, page: it.page });
    await api.askWith(it.question, { page: it.page }).catch(() => undefined); // cached: the same answer, shown with its marks
  }

  function render(el: HTMLElement) {
    if (!proj) {
      el.innerHTML = `<p class="prj-sub">Upload two or more sheets of one building together to make a project.</p>`;
      return;
    }
    const cur = api.file()?.path ?? "";
    const curId = `${cur}#${api.page()}`;
    const byId = new Map(proj.sheets.map((s) => [s.id, s]));
    // what 'This building' will ask: one whole plan per level (the server's /plan decides; this is the button's count)
    const plans = proj.sheets.filter((s) => (s.kind === "floor_plan" || s.kind === "drawing" || s.kind === "unknown") && s.include !== false
      && (!s.copy_of || s.include === true));
    const perLevel = new Set(plans.filter((s) => !s.partial && s.storey !== null).map((s) => `${s.building}|${s.storey}`)).size;
    const nFloor = perLevel >= 2 ? perLevel : plans.length;
    const lv = proj.levels.map((l) => `<div class="prj-lvname">${esc(l.building ? `${l.building} · ` : "")}${esc(l.level)}</div>` + l.sheets.map((id) => {
      const s = byId.get(id)!;
      const on = s.id === curId || (s.file === cur && !proj!.sheets.some((x) => x.id === curId));
      const copyOf = s.copy_of && s.include !== true ? byId.get(s.copy_of) : undefined;
      const off = s.include === false || !!copyOf;
      const why = copyOf ? ` · a copy of ${copyOf.label}: ${s.copy_how ?? "the same drawing"}; its quantities are counted once, from that sheet` : "";
      return `<button type="button" class="prj-sheet${on ? " on" : ""}" data-open="${esc(s.id)}" title="${esc(s.file.split("/").pop())}">
          <span class="t">${esc(s.label)}</span><span class="prj-role${off ? " off" : ""}" title="${esc(`${s.role} (from the ${s.kind_source})${s.level ? ` · level from the ${s.level_source}` : ""}${why}`)}">${esc(copyOf ? `${s.role} · copy` : s.role)}</span>
          <span class="prj-edit" data-edit="${esc(s.id)}" role="button" tabindex="0">Change</span></button>`
        + (editing === s.id ? `<form class="prj-form" data-form="${esc(s.id)}">
          <input name="level" placeholder="Level, e.g. Level 01" value="${esc(s.level)}" aria-label="Level name">
          <select name="role" aria-label="Kind of sheet">${["Floor plan", "Ceiling plan", "Section", "Elevation", "Site plan", "Schedule", "Detail", "Drawing"]
            .map((r) => `<option${r === s.role ? " selected" : ""}>${r}</option>`).join("")}</select>
          <label class="prj-sub"><input type="checkbox" name="include"${off ? "" : " checked"}> count in building totals</label>
          <button class="prj-btn" type="submit">Save</button><button class="prj-btn ghost" type="button" data-cancel>Cancel</button></form>` : "");
    }).join("")).join("");

    const ask = scope === "building" ? `<div class="prj-ask">
        <textarea id="prjQ" aria-label="Question about the whole building" placeholder="e.g. How many doors are there in the whole building?">${esc(lastQ || api.question())}</textarea>
        <div><button class="prj-btn" type="button" id="prjAsk"${busy ? " disabled" : ""}>${busy ? "Asking each level…" : `Ask all ${nFloor} levels`}</button></div>
        ${progress.length ? `<ol class="prj-progress">${progress.map((p) => `<li class="${p.state}">${esc(p.label)}${p.state === "now" ? " …" : p.value ? ` — <b>${esc(p.value)}</b>` : ""}</li>`).join("")}</ol>` : ""}
        ${last ? building(last) : ""}</div>`
      : `<p class="prj-sub">Questions in the box below are about the sheet on screen. Pick <b>This building</b> to ask every level at once.</p>`;

    const items = proj.items.slice().reverse();
    const review = items.length ? items.map((it) => {
      const c = it.corrected;
      const src = it.scope === "building" ? `whole building · ${it.level}` : `${it.level ? it.level + " · " : ""}${it.sheet || it.file.split("/").pop()}`;
      return `<div class="prj-item"><input type="checkbox" data-check="${esc(it.id)}"${it.checked ? " checked" : ""} aria-label="Checked">
        <div class="prj-q">${esc(it.question)}</div>
        <div class="prj-val">${c ? `<s>${esc(fmt(it.value, it.unit))}</s>${esc(fmt(c.value, c.unit))}` : esc(fmt(it.value, it.unit))}</div>
        <div class="prj-src">${esc(src)} · ${c ? `corrected by you${c.note ? `: ${esc(c.note)}` : ""}` : `${esc(sure(it.confidence))}${it.n_evidence ? ` · ${it.n_evidence} marked on the drawing` : " · not located"}`}</div>
        <div class="prj-acts"><button class="prj-link" type="button" data-show="${esc(it.id)}">Show</button>
          <button class="prj-link" type="button" data-correct="${esc(it.id)}">${c ? "Change correction" : "Correct"}</button></div>
        ${correcting === it.id ? `<form class="prj-form" data-cform="${esc(it.id)}" style="grid-column:1/4">
          <input name="value" inputmode="decimal" placeholder="Right value" value="${esc(c?.value ?? "")}" aria-label="Right value" size="8">
          <input name="unit" placeholder="unit" value="${esc(c?.unit ?? it.unit ?? "")}" aria-label="Unit" size="5">
          <input name="note" placeholder="How you know (optional)" value="${esc(c?.note ?? "")}" aria-label="Note">
          <button class="prj-btn" type="submit">Save</button>${c ? `<button class="prj-btn ghost" type="button" data-uncorrect="${esc(it.id)}">Remove</button>` : ""}
          <button class="prj-btn ghost" type="button" data-cancel>Cancel</button></form>` : ""}</div>`;
    }).join("") : `<p class="prj-sub">Answers you get on these sheets are listed here to check, correct and export.</p>`;

    // an answer that averaged several rooms of one name: one pick per numbered mark (the card's numbers), or keep the mean
    const picks = (it: Item) => it.ambiguous?.choices?.length ? `<div class="prj-chips" role="group" aria-label="Which one did you mean?">${it.ambiguous.choices.map((c) =>
      `<button class="prj-chip" type="button" data-pick="${esc(it.id)}" data-n="${c.n}" data-v="${c.value}" data-u="${esc(c.unit)}" title="${esc(`${c.label} — mark ${c.n} on the drawing`)}">${c.n} · ${esc(fmt(c.value, c.unit))}</button>`).join("")}</div>` : "";
    const open = proj.open.length ? proj.open.map((it) => `<div class="prj-open"><div class="prj-q">${esc(it.question)}</div>
        <div class="prj-sub">${esc(it.scope === "building" ? "whole building" : it.level || it.sheet)} · ${esc(it.why_open || "no answer yet")}</div>
        ${picks(it)}
        <div class="prj-acts">${it.ambiguous ? `<button class="prj-link" type="button" data-show="${esc(it.id)}">Show the ${it.ambiguous.choices.length} on the drawing</button>` : ""}
        <button class="prj-link" type="button" data-correct="${esc(it.id)}">Give the value</button>
        <button class="prj-link" type="button" data-resolve="${esc(it.id)}">${it.ambiguous ? "Keep the mean" : "Mark resolved"}</button></div>
        ${correcting === it.id ? `<form class="prj-form" data-cform="${esc(it.id)}"><input name="value" placeholder="Value" size="8" aria-label="Value"><input name="unit" placeholder="unit" size="5" value="${esc(it.unit ?? "")}" aria-label="Unit">
          <input name="note" placeholder="How you know" aria-label="Note"><button class="prj-btn" type="submit">Save</button><button class="prj-btn ghost" type="button" data-cancel>Cancel</button></form>` : ""}</div>`).join("")
      : `<p class="prj-sub">No open questions.</p>`;

    el.innerHTML = `<div class="prj">
      <div><div class="prj-name">${esc(proj.name)}</div><div class="prj-sub">${proj.sheets.length} sheets · ${proj.levels.filter((l) => l.storey !== null).length} levels read from the sheets · kept on this server</div></div>
      <div class="prj-chips" role="group" aria-label="What questions are about">
        <button type="button" class="prj-chip" data-scope="sheet" aria-pressed="${scope === "sheet"}">This sheet</button>
        <button type="button" class="prj-chip" data-scope="building" aria-pressed="${scope === "building"}">This building</button></div>
      ${ask}
      <div><h4>Sheets by level</h4><div class="prj-lv">${lv}</div></div>
      <div><h4>Review list (${proj.items.length})</h4>${review}
        ${proj.items.length ? `<p style="display:flex;gap:8px"><a class="prj-btn ghost" style="display:inline-flex;align-items:center;text-decoration:none" href="${P(proj.id, "/export?fmt=xlsx")}" download="${esc(fileStem(proj.name))}-review.xlsx">Export XLSX</a>
          <a class="prj-btn ghost" style="display:inline-flex;align-items:center;text-decoration:none" href="${P(proj.id, "/export?fmt=csv")}" download="${esc(fileStem(proj.name))}-review.csv">CSV</a></p>` : ""}</div>
      <div><h4>Open questions (${proj.open.length})</h4>${open}</div></div>`;
    wire(el);
  }

  function building(a: BuildingAnswer) {
    const rows = a.per_sheet.map((r) => `<tr class="${r.counted ? "" : "skip"}"><td>${esc(r.level || "—")}</td><td>${esc(r.label)}</td>
        <td class="n">${esc(fmt(r.value, r.unit))}</td><td>${r.counted ? "added" : esc(r.note || "not added")}</td>
        <td><button class="prj-link" type="button" data-openfile="${esc(r.file)}" data-page="${r.page}" data-q="1">Show</button></td></tr>`).join("");
    const head = a.value !== null && a.value !== undefined
      ? `<div class="prj-total">${esc(fmt(a.value, a.unit))}</div><div class="prj-why">${esc(sure(a.confidence))} · ${a.per_sheet.some((r) => /^not added: the same/.test(r.note)) ? "the most on one level: lifts / stairs pass through every level, so levels are not added" : `the sum of ${a.per_sheet.filter((r) => r.counted).length} levels, one plan each`}${a.cost?.wall_seconds ? ` · ${Math.round(a.cost.wall_seconds)} s` : ""}</div>`
      : `<div class="prj-warn"><b>No building total.</b> ${esc(a.no_total_why || "no sheet gave a number")}</div><div class="prj-why">The value of each sheet is below; it is listed under open questions.</div>`;
    const left = (a.assumptions ?? []).filter((x) => /left out/.test(x));
    return `${head}<table class="prj-tbl"><thead><tr><th>Level</th><th>Sheet</th><th class="n">Value</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${left.length ? `<div class="prj-why">${left.map(esc).join("<br>")}</div>` : ""}`;
  }

  function wire(el: HTMLElement) {
    el.querySelectorAll<HTMLElement>("[data-scope]").forEach((b) => b.addEventListener("click", () => {
      scope = b.dataset.scope as "sheet" | "building";
      rerender();
    }));
    el.querySelector("#prjAsk")?.addEventListener("click", () => {
      const q = (el.querySelector<HTMLTextAreaElement>("#prjQ")?.value ?? "").trim();
      // (askWith would pass a context, which skips beforeAsk: ask the levels here, then show the total on the card)
      if (q) void askBuilding(q).then((a) => api.showAnswer(a, q)).catch(() => undefined);
    });
    el.querySelectorAll<HTMLElement>("[data-open]").forEach((b) => b.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).closest("[data-edit]")) return;
      const s = proj?.sheets.find((x) => x.id === b.dataset.open);
      if (s) void openSheet(api, s);
    }));
    el.querySelectorAll<HTMLElement>("[data-edit]").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      editing = editing === b.dataset.edit ? "" : b.dataset.edit!;
      rerender();
    }));
    el.querySelectorAll<HTMLFormElement>("[data-form]").forEach((f) => f.addEventListener("submit", (e) => {
      e.preventDefault();
      const d = new FormData(f);
      const id = f.dataset.form!;
      editing = "";
      // a copy of another sheet counts only when the user ticks it in (true); null = 'as read' (a copy is then left out)
      const isCopy = !!proj?.sheets.find((x) => x.id === id)?.copy_of;
      void patchSheet(id, { level: String(d.get("level") ?? ""), role: String(d.get("role") ?? ""), include: d.get("include") ? (isCopy ? true : null) : false });
    }));
    el.querySelectorAll<HTMLElement>("[data-cancel]").forEach((b) => b.addEventListener("click", () => {
      editing = "";
      correcting = "";
      rerender();
    }));
    el.querySelectorAll<HTMLInputElement>("[data-check]").forEach((c) => c.addEventListener("change", () => void patchItem(c.dataset.check!, { checked: c.checked })));
    el.querySelectorAll<HTMLElement>("[data-correct]").forEach((b) => b.addEventListener("click", () => {
      correcting = correcting === b.dataset.correct ? "" : b.dataset.correct!;
      rerender();
      el.querySelector<HTMLInputElement>(`[data-cform="${CSS.escape(correcting)}"] input[name=value]`)?.focus();
    }));
    el.querySelectorAll<HTMLFormElement>("[data-cform]").forEach((f) => f.addEventListener("submit", (e) => {
      e.preventDefault();
      const d = new FormData(f);
      const v = String(d.get("value") ?? "").trim();
      if (!v) return;
      correcting = "";
      void patchItem(f.dataset.cform!, { value: v, unit: String(d.get("unit") ?? "") || null, note: String(d.get("note") ?? "") });
    }));
    el.querySelectorAll<HTMLElement>("[data-uncorrect]").forEach((b) => b.addEventListener("click", () => {
      correcting = "";
      void patchItem(b.dataset.uncorrect!, { value: null });
    }));
    el.querySelectorAll<HTMLElement>("[data-resolve]").forEach((b) => b.addEventListener("click", () => void patchItem(b.dataset.resolve!, { resolved: true })));
    el.querySelectorAll<HTMLElement>("[data-pick]").forEach((b) => b.addEventListener("click", () => {
      const it = proj?.open.find((x) => x.id === b.dataset.pick);
      const amb = it?.ambiguous;
      void patchItem(b.dataset.pick!, { value: Number(b.dataset.v), unit: b.dataset.u || null,
        note: amb ? `you picked mark ${b.dataset.n} of the ${amb.choices.length} rooms labelled '${amb.label}'` : `you picked mark ${b.dataset.n}` });
    }));
    el.querySelectorAll<HTMLElement>("[data-show]").forEach((b) => b.addEventListener("click", () => {
      const it = [...(proj?.items ?? []), ...(proj?.open ?? [])].find((x) => x.id === b.dataset.show);
      if (it) void showItem(it);
    }));
    el.querySelectorAll<HTMLElement>("[data-openfile]").forEach((b) => b.addEventListener("click", async () => {
      const file = b.dataset.openfile!;
      const page = Number(b.dataset.page ?? 0);
      if (api.file()?.path === file) {
        const q = proj && lastQ ? (await j<PlanOut>(P(proj.id, "/plan"), { method: "POST", body: JSON.stringify({ question: lastQ }) }).catch(() => null))?.sheet_question : "";
        if (q) await api.askWith(q, { page }).catch(() => undefined);
      } else await openSheet(api, { file, page });
    }));
  }
});
