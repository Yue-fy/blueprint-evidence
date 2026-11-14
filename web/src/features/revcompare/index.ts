/**
 * F5 revision compare (captain 19:54, prompts/F5.md). "Compare revision" in the viewer toolbar → the visitor drops the
 * OTHER version of the sheet on screen → the API lines the two up (shared printed words on vector PDFs: exact; image
 * features + ECC on scans / photos) and returns what was added / removed / changed, printed text that changed
 * ('A: 62 m2' → 'A: 55 m2'), and printed room areas compared by name — all marked on the drawing (red = only in this
 * version, green = only in the other one, orange = changed). "Check the quantities" asks the normal pipeline the
 * suggested questions (rooms next to a change, doors, windows) on BOTH versions (askWith, show:false) and shows
 * old → new with each answer's confidence; a row click shows that answer with its own evidence.
 * Whole issues: drop several files (or a multi-page PDF) when the open upload has several sheets too → POST /sets pairs
 * every sheet of the old issue with its sheet in the new one (printed words; image features on scans), each pair is
 * compared in turn and gets one plain line ('Yr 4: 62 → 55 m²', 'no changes', 'new in this issue'); a click opens it.
 * Backend: stoa/features/revcompare.py (POST /api/feat/revcompare/compare, /sets). Switched on with ?features=revcompare.
 */
import { upload } from "../../api";
import type { Answer, FileInfo } from "../../types";
import { registerFeature, type DrawingPt, type FeatureApi, type Overlay2D } from "../registry";

type Box = [number, number, number, number];
type Region = { id: number; kind: "added" | "removed" | "changed"; description: string; near: string | null; box_a: Box; box_b: Box };
type Demo = { id: string; title: string; credit: string; available: boolean; a: { name: string; url: string }; b: { name: string; url: string } };
type TextCh = { kind: "added" | "removed" | "changed" | "moved"; old: string | null; new: string | null; box_a: Box | null; box_b: Box | null; box_b_in_a?: Box | null; shift?: [number, number]; source?: string | null };
type ByName = { name: string; unit: string; old: number[]; new: number[]; count_old: number; count_new: number; sum_old: number; sum_new: number; delta: number };
type Result = {
  page_b_note?: string | null;
  doors?: { count_old: number; count_new: number; unchanged: number; moved: { box_a: Box; box_b: Box; moved_m: number }[]; removed: { box_a: Box }[]; added: { box_b: Box; box_b_in_a: Box }[]; scale: string | null; error?: string } | null;
  scale?: { ratio: string; source: string } | null;
  a: { file: string; page: number | null; space: string; kind: string; size: [number, number] };
  b: { file: string; page: number | null; space: string; kind: string };
  alignment: { method: string; ok: boolean; overlap: number; residual: number; residual_unit: string; scale: number; rotation_deg: number; perspective: boolean; note: string; how?: string };
  regions: Region[];
  text: TextCh[];
  text_check?: { candidates: number; checked: number; ocr: { crops: number; cpu_seconds: number } | null; note: string | null } | null;
  quantities: { what: string; old: number; new: number; unit: string; delta: number; box_a: Box; box_b: Box }[];
  areas: { by_name: ByName[]; total: { unit: string; old: number; new: number; delta: number } | null };
  suggested_questions: { question: string; kind: string; region: number | null }[];
  summary: string;
  same_sheet?: boolean;
  approximate?: boolean;
  images?: Record<string, string>;
  wall_seconds?: number;
  cost?: { tier?: string; ocr_crops?: number; cpu_seconds?: number };
};
type Impact = { question: string; a?: Answer; b?: Answer; err?: string };
type SheetRef = { file: string; page: number; n_pages: number; title: string; sheet_no: string | null; kind: string };
type SetsRes = { sheets_a: SheetRef[]; sheets_b: SheetRef[]; pairs: { a: number; b: number; score: number; how: string; weak: boolean }[]; only_a: number[]; only_b: number[]; summary: string };
/** One row of 'the whole issue': a pair of sheets and its compare (filled one after the other). */
type SetRow = { a: SheetRef; b: SheetRef; weak: boolean; res?: Result; err?: string; busy?: boolean };
const SET_AUTO = 12; // pairs compared automatically; the rest on click

const SVG = "http://www.w3.org/2000/svg";
const COL = { removed: "#d93a3a", added: "#2f9e44", changed: "#f08c00", moved: "#3b6fd8" } as const;
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const unitWord = (u: string) => (u === "m2" ? "m²" : u === "ft2" ? "ft²" : u);
const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toLocaleString("en-GB", { maximumFractionDigits: 0 }) : String(+v.toFixed(2)));
const signed = (v: number) => (v > 0 ? "+" : v < 0 ? "−" : "±") + fmt(Math.abs(v));
const baseName = (p: string) => p.split("/").pop() ?? p;
const OFF_MSG = "Revision compare is not switched on on this server (STOA_FEATURES=revcompare or all).";
/** 'feature is off' (our guard) or a route that is not mounted at all ('Not Found'); any other 404 ('no such file')
 *  is shown as the server said it (QA 21:19). */
const isOff = (status: number, detail: unknown) => status === 404 && (/is off \(STOA_FEATURES\)/.test(String(detail ?? "")) || String(detail ?? "") === "Not Found");

/** The panel's first sentence in plain words: what changed in quantities, then doors, renamed labels, other marks.
 *  Rooms with the same change are grouped ('Yr 3, Yr 4, Yr 5, Yr 6: 62 → 55 m² (12 rooms)'). */
function headline(res: Result): string {
  const out: string[] = [];
  const by = res.areas?.by_name ?? [];
  const groups = new Map<string, { names: string[]; rooms: number }>();
  const other: string[] = [];
  const fresh: string[] = [];
  for (const d of by) {
    const u = unitWord(d.unit);
    const uniform = d.count_old === d.count_new && new Set(d.old).size === 1 && new Set(d.new).size === 1;
    if (!d.count_old) fresh.push(`${d.name} ${d.new.map(fmt).join(" + ")} ${u}`);
    else if (uniform) {
      const k = `${fmt(d.old[0])} → ${fmt(d.new[0])} ${u}`;
      const g = groups.get(k) ?? { names: [], rooms: 0 };
      g.names.push(d.name);
      g.rooms += d.count_old;
      groups.set(k, g);
    } else if (d.count_new) other.push(`${d.name} ${fmt(d.sum_old)} → ${fmt(d.sum_new)} ${u}`);
  }
  const big = [...groups.entries()].sort((x, y) => y[1].rooms - x[1].rooms);
  for (const [k, g] of big.slice(0, 2))
    out.push(`${g.names.sort().join(", ")}: ${k}${g.rooms > 1 ? ` (${g.rooms} rooms)` : ""}`);
  if (fresh.length) out.push(`new: ${fresh.slice(0, 3).join(", ")}`);
  const rest = big.length - 2 + other.length;
  if (rest > 0) out.push(`${rest} more room area${rest > 1 ? "s" : ""} changed`);
  const dr = res.doors;
  if (dr && dr.count_old != null) {
    const bits = [dr.added.length ? `${dr.added.length} added` : "", dr.removed.length ? `${dr.removed.length} removed` : "", dr.moved.length ? `${dr.moved.length} moved` : ""].filter(Boolean);
    if (bits.length) out.push(`door leaves ${dr.count_old} → ${dr.count_new}: ${bits.join(", ")}`);
  }
  const ren = res.text.filter((t) => t.kind === "changed" && t.source).slice(0, 2).map((t) => `‘${t.old}’ → ‘${t.new}’`);
  if (ren.length) out.push(`renamed: ${ren.join(", ")}`);
  if (res.regions.length) out.push(`${res.regions.length} drawing change${res.regions.length > 1 ? "s" : ""} marked on the plan`);
  return out.length ? `What changed: ${out.join(" · ")}.` : "No changes found between the two versions.";
}
const sure = (c: number | undefined) => (c == null ? "" : c >= 0.7 ? "sure" : c >= 0.4 ? "fairly sure" : "a guess");

function injectCss() {
  if (document.getElementById("feat-revcompare-css")) return;
  const s = document.createElement("style");
  s.id = "feat-revcompare-css";
  s.textContent = `
.rc-drop{display:flex;flex-direction:column;gap:8px;align-items:flex-start;padding:16px;border:1.5px dashed var(--line,#ccd);border-radius:14px;margin:4px 0 12px}
.rc-drop.over{border-color:#3b6fd8;background:rgba(59,111,216,.06)}
.rc-drop input{max-width:100%}
.rc-sum{margin:0 0 10px;line-height:1.45}
.rc-meta{font-size:12.5px;color:var(--ink-2,#667);margin:0 0 12px}
.rc-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12.5px;margin:0 0 12px}
.rc-legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.rc-sec{margin:14px 0 6px;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--ink-2,#667)}
.rc-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.rc-list li{display:flex;gap:8px;align-items:baseline;padding:7px 8px;border-radius:10px;cursor:pointer;line-height:1.35}
.rc-list li:hover,.rc-list li.on{background:var(--fill-2,rgba(0,0,0,.05))}
.rc-n{flex:none;min-width:22px;height:22px;border-radius:11px;color:#fff;font-size:12px;font-weight:600;display:inline-flex;align-items:center;justify-content:center}
.rc-d{font-variant-numeric:tabular-nums;font-weight:600}
.rc-d.neg{color:${COL.removed}}.rc-d.pos{color:${COL.added}}
.rc-warn{background:rgba(240,140,0,.1);border-radius:10px;padding:8px 10px;margin:0 0 10px;font-size:13.5px}
.rc-btns{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.rc-btns button,.rc-btns a{min-height:40px;padding:0 14px;border-radius:999px;border:0;background:var(--fill-2,rgba(0,0,0,.06));font:inherit;cursor:pointer;color:inherit;text-decoration:none;display:inline-flex;align-items:center}
.rc-btns button.primary{background:#1d1d1f;color:#fff}
.rc-imp td,.rc-imp th{padding:5px 6px;text-align:left;font-size:13.5px;vertical-align:top}
.rc-imp{border-collapse:collapse;width:100%}.rc-imp tr+tr td{border-top:1px solid var(--line,#e5e5ea)}
.rc-imp small{color:var(--ink-2,#667)}
.rc-busy{color:var(--ink-2,#667);font-size:14px}
.rc-fade{display:flex;align-items:center;gap:10px;font-size:13.5px;margin:0 0 12px}
.rc-fade input{flex:1;min-height:32px}
`;
  document.head.append(s);
}

registerFeature("revcompare", (api: FeatureApi) => {
  injectCss();
  let res: Result | null = null;
  let other: FileInfo | null = null;
  let busy = "";
  let error = "";
  let picked: string | null = null; // "r3" | "t5" | "q1": the row the visitor clicked (bold on the drawing)
  let impact: Impact[] = [];
  let impactBusy = false;
  let fade = 0; // 0..100: opacity of the other version laid over this one (lined up)
  let demoCredit = ""; // licence credit of the one-click example on screen
  let sets: SetsRes | null = null; // both issues' sheets paired (only when either side has more than one sheet)
  let rows: SetRow[] = [];
  let setRun = 0; // bumps on every new drop: an older run stops filling rows
  const infos = new Map<string, FileInfo>(); // the dropped files' upload records by path
  const ov: Overlay2D = api.addOverlay2D("revcompare");

  const onA = () => !!res && api.file()?.path === res.a.file && api.page() === (res.a.page ?? 0);

  function rect(box: Box, color: string, opts: { dashed?: boolean; label?: string; bold?: boolean; fill?: boolean } = {}) {
    const pts = ([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]] as DrawingPt[]).map((p) => ov.toContent(p));
    const z = 1 / Math.max(1e-6, ov.zoom()); // content px per screen px: sizes below are in screen px
    const pad = 4 * z;
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad, x1 = Math.max(...xs) + pad, y1 = Math.max(...ys) + pad;
    const r = document.createElementNS(SVG, "rect");
    r.setAttribute("x", String(x0));
    r.setAttribute("y", String(y0));
    r.setAttribute("width", String(x1 - x0));
    r.setAttribute("height", String(y1 - y0));
    r.setAttribute("rx", String(4 * z));
    r.setAttribute("fill", opts.fill ? color : "none");
    r.setAttribute("fill-opacity", opts.fill ? "0.12" : "0");
    r.setAttribute("stroke", color);
    r.setAttribute("stroke-width", String((opts.bold ? 3.5 : 2) * z));
    if (opts.dashed) r.setAttribute("stroke-dasharray", `${6 * z} ${4 * z}`);
    ov.g.append(r);
    if (opts.label) {
      const c = document.createElementNS(SVG, "circle");
      const cx = x0, cy = y0, rad = 10 * z;
      c.setAttribute("cx", String(cx));
      c.setAttribute("cy", String(cy));
      c.setAttribute("r", String(rad));
      c.setAttribute("fill", color);
      ov.g.append(c);
      const t = document.createElementNS(SVG, "text");
      t.setAttribute("x", String(cx));
      t.setAttribute("y", String(cy + 4 * z));
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", String(11 * z));
      t.setAttribute("font-weight", "600");
      t.setAttribute("fill", "#fff");
      t.textContent = opts.label;
      ov.g.append(t);
    }
  }

  function draw() {
    ov.clear();
    if (!res || !onA() || !res.alignment.ok) return;
    if (fade > 0 && res.images?.b_aligned) {
      // b_aligned.png covers sheet A's page exactly (A's render frame): map its unit square onto the page corners
      const [W, H] = res.a.size;
      const O = ov.toContent([0, 0]), X = ov.toContent([W, 0]), Y = ov.toContent([0, H]);
      const im = document.createElementNS(SVG, "image");
      im.setAttribute("href", `/api${res.images.b_aligned}`);
      im.setAttribute("width", "1");
      im.setAttribute("height", "1");
      im.setAttribute("preserveAspectRatio", "none");
      im.setAttribute("transform", `matrix(${X[0] - O[0]} ${X[1] - O[1]} ${Y[0] - O[0]} ${Y[1] - O[1]} ${O[0]} ${O[1]})`);
      im.setAttribute("opacity", String(fade / 100));
      im.style.mixBlendMode = "multiply";
      ov.g.append(im);
    }
    const dashed = !!res.approximate;
    for (const r of res.regions)
      rect(r.box_a, COL[r.kind], { dashed, label: String(r.id), bold: picked === `r${r.id}`, fill: picked === `r${r.id}` });
    if (picked === "doors" && res.doors && res.doors.count_old != null) {
      for (const d of res.doors.removed) rect(d.box_a, COL.removed, { bold: true });
      for (const d of res.doors.added) rect(d.box_b_in_a, COL.added, { bold: true });
      for (const d of res.doors.moved) rect(d.box_a, COL.changed, { bold: true });
    }
    res.text.forEach((t, i) => {
      if (t.kind === "moved") return;
      const box = t.box_a ?? t.box_b_in_a ?? null;
      if (box) rect(box, COL[t.kind], { dashed: !!t.source, bold: picked === `t${i}`, fill: picked === `t${i}` });
    });
  }

  async function post(path: string, body: unknown): Promise<unknown> {
    const r = await fetch(`/api/feat/revcompare/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (isOff(r.status, j.detail)) throw new Error(OFF_MSG);
    if (!r.ok) throw new Error(j.detail ? String(j.detail) : `the server said ${r.status}`);
    return j;
  }
  const fetchCompare = (a: string, pageA: number, b: string, pageB?: number) =>
    post("compare", { a, b, page_a: pageA, page_b: pageB ?? null }) as Promise<Result>; // page_b null: the server picks B's page

  async function compare(b: FileInfo, pageB?: number) {
    const a = api.file();
    if (!a) return;
    other = b;
    res = null;
    error = "";
    impact = [];
    busy = `Lining up ${baseName(b.path)} with the sheet on screen…`;
    panel.open();
    try {
      res = await fetchCompare(a.path, api.page(), b.path, pageB);
      const row = rows.find((x) => x.a.file === a.path && x.a.page === api.page() && x.b.file === b.path);
      if (row) row.res = res;
    } catch (e) {
      error = String((e as Error).message ?? e);
    }
    busy = "";
    draw();
    panel.open();
  }

  /** Several files dropped, or a multi-page PDF on either side: pair both issues' sheets, compare the pair on screen
   *  first, then every other pair in turn (cached on the server: clicking a row later is instant). */
  async function compareSets(bs: FileInfo[]) {
    const a = api.file();
    if (!a) return;
    const run = ++setRun;
    sets = null;
    rows = [];
    const mine = api.uploads().filter((p) => p !== a.path && !bs.some((x) => x.path === p) && /\.(pdf|png|jpe?g|tiff?|webp)$/i.test(p));
    let sr: SetsRes | null = null;
    try {
      sr = (await post("sets", { a: [a.path, ...mine], b: bs.map((x) => x.path) })) as SetsRes;
    } catch {
      sr = null; // an older server without /sets: compare the first file as before
    }
    if (run !== setRun) return;
    if (!sr || (sr.sheets_a.length <= 1 && sr.sheets_b.length <= 1)) return compare(bs[0]);
    sets = sr;
    rows = sr.pairs.map((p) => ({ a: sr!.sheets_a[p.a], b: sr!.sheets_b[p.b], weak: p.weak }));
    const here = rows.find((r) => r.a.file === a.path && r.a.page === api.page());
    if (here) await compare(infos.get(here.b.file) ?? { ...bs[0], path: here.b.file }, here.b.page);
    else {
      other = null;
      res = null;
      panel.open();
    }
    for (const row of rows.slice(0, SET_AUTO)) {
      if (run !== setRun) return;
      if (row.res || row.err) continue;
      await fillRow(row);
    }
  }

  async function fillRow(row: SetRow) {
    row.busy = true;
    panel.open();
    try {
      row.res = await fetchCompare(row.a.file, row.a.page, row.b.file, row.b.page);
    } catch (e) {
      row.err = String((e as Error).message ?? e).slice(0, 160);
    }
    row.busy = false;
    panel.open();
  }

  /** A row click: open that sheet of the old issue and show its compare (drawn on it). */
  async function openRow(row: SetRow) {
    if (api.file()?.path !== row.a.file || api.page() !== row.a.page) await api.openFile(row.a.file, row.a.page);
    other = infos.get(row.b.file) ?? other;
    impact = [];
    picked = null;
    if (!row.res && !row.err) await fillRow(row);
    res = row.res ?? null;
    error = row.err ?? "";
    draw();
    panel.open();
  }

  const sheetName = (x: SheetRef) => `${x.n_pages > 1 ? `p${x.page + 1} · ` : ""}${x.sheet_no ? `${x.sheet_no} ` : ""}${x.title || baseName(x.file)}`;
  function rowLine(r: SetRow): string {
    if (r.busy) return "comparing…";
    if (r.err) return esc(r.err);
    if (!r.res) return "click to compare";
    if (!r.res.alignment.ok) return "could not be lined up: probably another sheet";
    const h = headline(r.res);
    const bits = (h.startsWith("What changed: ") ? h.slice(14) : h).replace(/\.$/, "").split(" · ");
    const line = esc(bits.length > 3 ? `${bits.slice(0, 2).join(" · ")} · +${bits.length - 2} more` : bits.join(" · ")); // the full line is shown when opened
    if (r.res.same_sheet === false)
      // lines up well but much changed (a heavy revision: the DfE first floor on a scan, 25 % of the linework) vs lines
      // up badly (another floor of the same building: 0.76)
      return r.res.alignment.overlap >= 0.8 ? `<b>a heavy revision, or another sheet: check</b> · ${line}` : "looks like a different sheet (another floor?): not compared further";
    return line;
  }
  function setSection(): string {
    if (!sets) return "";
    const changed = rows.filter((r) => r.res && r.res.alignment.ok && (r.res.regions.length || r.res.text.some((t) => t.kind !== "moved"))).length;
    const same = rows.filter((r) => r.res && r.res.alignment.ok && !r.res.regions.length && !r.res.text.some((t) => t.kind !== "moved")).length;
    const a = api.file();
    const p: string[] = [`<div class="rc-sec">The whole issue: ${sets.sheets_a.length} → ${sets.sheets_b.length} sheet(s)</div>`];
    p.push(`<p class="rc-meta">${esc(sets.summary)}${changed || same ? ` · compared so far: ${changed} changed, ${same} unchanged` : ""} · sheets paired by ${sets.pairs.some((x) => x.how === "features") ? "their printed words or, on scans, their shapes" : "their printed words"} (no AI model)</p><ul class="rc-list rc-set">`);
    rows.forEach((r, i) => {
      const on = a && r.a.file === a.path && r.a.page === api.page();
      p.push(`<li data-row="${i}" class="${on ? "on" : ""}"><span class="rc-n" style="background:${r.res && !r.res.regions.length && !r.res.text.some((t) => t.kind !== "moved") ? "#8e8e93" : COL.changed}">${i + 1}</span><span><b>${esc(sheetName(r.a))}</b>${r.weak ? " <small>(paired loosely)</small>" : ""}<br><small>${rowLine(r)}</small></span></li>`);
    });
    for (const j of sets.only_b) p.push(`<li data-newb="${j}"><span class="rc-n" style="background:${COL.added}">+</span><span><b>${esc(sheetName(sets.sheets_b[j]))}</b><br><small>new in this issue: no sheet like it in the old one</small></span></li>`);
    for (const i of sets.only_a) p.push(`<li data-gonea="${i}"><span class="rc-n" style="background:${COL.removed}">−</span><span><b>${esc(sheetName(sets.sheets_a[i]))}</b><br><small>not in the new issue</small></span></li>`);
    p.push(`</ul>`);
    if (a && !rows.some((r) => r.a.file === a.path && r.a.page === api.page()))
      p.push(`<p class="rc-warn">The sheet on screen has no partner in the other issue. Click a row above to see one that has.</p>`);
    return p.join("");
  }

  /** One-click example (captain 21:13; W5's catalogue card links `?features=revcompare&revdemo=dfe630`): the server
   *  hands out both versions, they are uploaded like a visitor's files, the first opens and the second is compared. */
  async function runDemo(id: string) {
    error = "";
    sets = null;
    rows = [];
    setRun++;
    busy = "Fetching the example (two issues of the same sheet)…";
    panel.open();
    try {
      const lr = await fetch("/api/feat/revcompare/demo");
      const lj = await lr.json().catch(() => ({}));
      if (isOff(lr.status, lj.detail)) throw new Error(OFF_MSG);
      if (!lr.ok) throw new Error(lj.detail ? String(lj.detail) : `the server said ${lr.status}`);
      const d = ((lj.demos ?? []) as Demo[]).find((x) => x.id === id);
      if (!d || !d.available) throw new Error("This example's drawings are not on this server.");
      const get = async (f: { name: string; url: string }) => {
        const r = await fetch(`/api${f.url}`);
        if (!r.ok) throw new Error(`could not fetch ${f.name} (${r.status})`);
        return new File([await r.blob()], f.name, { type: "application/pdf" });
      };
      const [fa, fb] = await Promise.all([get(d.a), get(d.b)]);
      busy = "Uploading both versions…";
      render(panel.el);
      const [ia, ib] = await Promise.all([upload(fa), upload(fb)]);
      await api.openFile(ia.path, 0);
      demoCredit = d.credit;
      infos.set(ib.path, ib);
      await compareSets([ib]); // one sheet each: the plain compare; a set: every sheet paired and compared
      // the example's point is the sheet that changed: open it when the one on screen did not (a visitor's own drop
      // stays on the sheet they were looking at)
      const changed = (r: SetRow) => !!r.res && r.res.alignment.ok && (r.res.regions.length > 0 || r.res.text.some((t) => t.kind !== "moved"));
      const first = rows.find(changed);
      if (first && res && !rows.some((r) => r.res === res && changed(r))) await openRow(first);
    } catch (e) {
      busy = "";
      error = String((e as Error).message ?? e);
      panel.open();
      render(panel.el);
    }
  }

  async function checkImpact() {
    if (!res || !other || impactBusy) return;
    impactBusy = true;
    impact = res.suggested_questions.slice(0, 6).map((q) => ({ question: q.question }));
    panel.open();
    for (const row of impact) {
      try {
        row.a = await api.askWith(row.question, { show: false, file: res.a.file, page: res.a.page ?? 0 });
        row.b = await api.askWith(row.question, { show: false, file: other.path, page: res.b.page ?? 0 });
      } catch (e) {
        row.err = String((e as Error).message ?? e).slice(0, 120);
      }
      panel.open();
    }
    impactBusy = false;
    panel.open();
  }

  function impactRow(r: Impact, i: number): string {
    const va = r.a?.value, vb = r.b?.value;
    const u = r.a?.unit ?? r.b?.unit ?? "";
    let d = "";
    if (typeof va === "number" && typeof vb === "number") {
      const delta = vb - va;
      const tol = !u || u === "count" ? 0.5 : 0.01 * Math.max(Math.abs(va), 1e-9);
      d = Math.abs(delta) > tol ? `<span class="rc-d ${delta < 0 ? "neg" : "pos"}">${signed(delta)} ${esc(u === "count" ? "" : unitWord(u))}</span>` : `<small>same</small>`;
    }
    const shownUnit = (x: Answer) => (x.unit && x.unit !== "count" ? unitWord(x.unit) : "");
    // cross-check: the room's printed area tag on each version (when the room has one value per version)
    let check = "", agreed = false;
    const room = res?.areas?.by_name?.find((d) => r.question.toLowerCase().includes(d.name.toLowerCase()) && new Set(d.old).size <= 1 && new Set(d.new).size <= 1);
    if (room && typeof va === "number" && typeof vb === "number" && (u === "m2" || u === room.unit)) {
      const ok = (m: number, p: number | undefined) => p != null && Math.abs(m - p) <= 0.03 * p;
      const po = room.old[0], pn = room.new[0];
      agreed = ok(va, po) && ok(vb, pn);
      check = agreed ? `<br><small>✓ printed ${fmt(po)} → ${fmt(pn)} ${unitWord(room.unit)}: measured and printed agree</small>`
        : `<br><small>printed ${po != null ? fmt(po) : "—"} → ${pn != null ? fmt(pn) : "—"} ${unitWord(room.unit)}</small>`;
    }
    // QA 21:25: 'a guess' next to '✓ measured and printed agree' read as a contradiction: when the printed tag confirms
    // the measurement, say that instead of the measuring step's own doubt
    const how = (x: Answer) => (agreed ? "matches the printed tag" : sure(x.confidence));
    const val = (x: Answer | undefined) => (x ? (x.value == null ? "—" : `${typeof x.value === "number" ? fmt(x.value) : esc(x.value)} ${esc(shownUnit(x))}<br><small>${how(x)}</small>`) : impactBusy ? "…" : "—");
    if (!check && /\bdoors?\b/i.test(r.question) && res?.doors?.count_old != null)
      check = `<br><small>doors, not leaves: the Doors row above counts each leaf of a double door</small>`;
    return `<tr data-q="${i}"><td>${esc(r.question)}${r.err ? `<br><small>${esc(r.err)}</small>` : ""}</td><td>${val(r.a)}</td><td>${val(r.b)}</td><td>${d}${check}</td></tr>`;
  }

  /** One row per change (rooms' printed areas, measured quantities, text, linework) with both sheets' boxes. */
  function exportCsv() {
    if (!res || !other) return;
    const q = (v: unknown) => {
      const t = v == null ? "" : String(v);
      return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
    };
    const box = (b: Box | null | undefined) => (b ? b.map((v) => Math.round(v * 10) / 10).join(" ") : "");
    const rows: unknown[][] = [["sheet_old", "sheet_new", "type", "id", "what", "old", "new", "change", "unit", "where / note", "box_old", "box_new", "source"]];
    const A = baseName(res.a.file), B = baseName(other.path);
    for (const d of res.areas?.by_name ?? [])
      rows.push([A, B, "room area (printed)", "", d.name, d.old.join(" + "), d.new.join(" + "), d.delta, unitWord(d.unit), `${d.count_old} → ${d.count_new} room(s)`, "", "", "printed area tags on both sheets"]);
    for (const r of impact)
      if (r.a || r.b)
        rows.push([A, B, "quantity (measured)", "", r.question, r.a?.value ?? "", r.b?.value ?? "",
          typeof r.a?.value === "number" && typeof r.b?.value === "number" ? +(r.b.value - r.a.value).toFixed(3) : "", r.a?.unit ?? r.b?.unit ?? "",
          `confidence ${r.a?.confidence ?? ""} / ${r.b?.confidence ?? ""}`, "", "", "pipeline on each version"]);
    res.text.forEach((t, i) => { if (t.kind !== "moved") rows.push([A, B, `text ${t.kind}`, `T${i}`, "", t.old ?? "", t.new ?? "", "", "", "", box(t.box_a), box(t.box_b), t.source ?? "printed text"]); });
    for (const r of res.regions)
      rows.push([A, B, `linework ${r.kind}`, r.id, r.description, "", "", "", "", r.near ?? "", box(r.box_a), box(r.box_b), res.approximate ? "pixel difference (approximate)" : "pixel difference"]);
    const csv = rows.map((r) => r.map(q).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const slug = (n: string) => n.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
    a.download = `changes_${slug(A)}_vs_${slug(B)}.csv`; // no spaces (QA 21:25: easier to mail / script)
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  function render(el: HTMLElement) {
    const a = api.file();
    const parts: string[] = [];
    if (!a) {
      el.innerHTML = `<p class="rc-busy">${busy ? esc(busy) : "Open a drawing first, then compare it with another version of the same sheet."}</p>${error ? `<p class="rc-warn">${esc(error)}</p>` : ""}${busy ? "" : `<div class="rc-btns"><button data-demo>Try a real revision (school first floor)</button><button data-demoset>A whole issue (2 sheets)</button></div>`}`;
      el.querySelector("[data-demo]")?.addEventListener("click", () => void runDemo("dfe630"));
      el.querySelector("[data-demoset]")?.addEventListener("click", () => void runDemo("dfe630set"));
      return;
    }
    parts.push(`<p class="rc-meta">On screen: <b>${esc(baseName(a.path))}</b>${api.page() ? ` · page ${api.page() + 1}` : ""}${other ? ` · compared with <b>${esc(baseName(other.path))}</b>` : ""}</p>`);
    parts.push(`<label class="rc-drop" data-drop><span>${other ? "Compare with another file" : "Drop the other version of this sheet here (PDF, PNG, JPG: an older issue, a revised print, a scan or a photo)"}</span><input type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp" multiple data-file></label>`);
    if (!other && !busy && !res && !sets) parts.push(`<p class="rc-meta">A whole new issue? Drop all its sheets (several files or one multi-page PDF): each is paired with its sheet here and compared.</p>`);
    if (!other && !busy && !res) parts.push(`<p class="rc-meta">No other version at hand? <a href="#" data-demo>Try a real revision</a>: a primary school's first floor, issued twice (classrooms re-planned from 62 to 55 m²). Or <a href="#" data-demoset>a whole issue</a>: ground + first floor, which sheet changed?</p>`);
    if (busy) parts.push(`<p class="rc-busy">${esc(busy)}</p>`);
    if (error) parts.push(`<p class="rc-warn">${esc(error)}</p>`);
    if (demoCredit && res && other) parts.push(`<p class="rc-meta">Example: ${esc(demoCredit)}.</p>`);
    parts.push(setSection());
    if (res && sets) parts.push(`<div class="rc-sec">This sheet: ${esc(sheetName(rows.find((r) => r.res === res)?.a ?? { file: res.a.file, page: res.a.page ?? 0, n_pages: 1, title: "", sheet_no: null, kind: "" }))}</div>`);
    if (res) {
      const al = res.alignment;
      if (!al.ok) parts.push(`<p class="rc-warn">${esc(res.summary)}</p>`);
      else {
        if (res.same_sheet === false) parts.push(`<p class="rc-warn">These two do not look like versions of the same sheet (another floor?). The marks below may be differences between two drawings, not edits.</p>`);
        parts.push(`<p class="rc-sum">${esc(headline(res))}</p>`);
        parts.push(`<p class="rc-meta">${esc(res.summary.split(" Printed room areas:")[0])}</p>`);
        const ar = res.areas;
        if (ar?.by_name?.length) {
          parts.push(`<div class="rc-sec">Room areas that changed (printed on both sheets)</div><ul class="rc-list">`);
          for (const d of ar.by_name.slice(0, 12)) {
            const u = unitWord(d.unit);
            const same = d.count_old === d.count_new && new Set(d.old).size === 1 && new Set(d.new).size === 1;
            const txt = same ? `${fmt(d.old[0])} → ${fmt(d.new[0])} ${u}${d.count_old > 1 ? ` (×${d.count_old})` : ""}`
              : !d.count_old ? `new: ${d.new.map(fmt).join(" + ")} ${u}` : !d.count_new ? `gone: ${d.old.map(fmt).join(" + ")} ${u}`
              : `${d.count_old} room(s) ${fmt(d.sum_old)} → ${d.count_new} room(s) ${fmt(d.sum_new)} ${u}`;
            parts.push(`<li data-room="${esc(d.name)}"><b>${esc(d.name)}</b><span>${esc(txt)}</span><span class="rc-d ${d.delta < 0 ? "neg" : "pos"}">${signed(d.delta)}</span></li>`);
          }
          parts.push(`</ul>`);
          if (ar.total?.delta) parts.push(`<p class="rc-meta">All printed room areas: ${fmt(ar.total.old)} → ${fmt(ar.total.new)} ${unitWord(ar.total.unit)} (${signed(ar.total.delta)})</p>`);
        }
        const dr = res.doors;
        if (dr && dr.count_old != null) {
          const ch = dr.added.length || dr.removed.length || dr.moved.length;
          parts.push(`<div class="rc-sec">Doors</div><ul class="rc-list"><li data-doors><b>Door leaves (swing arcs)</b><span>${dr.count_old} → ${dr.count_new}${ch ? ` · ${dr.added.length} added · ${dr.removed.length} removed · ${dr.moved.length} moved` : " · none moved"}</span>${dr.count_new !== dr.count_old ? `<span class="rc-d ${dr.count_new < dr.count_old ? "neg" : "pos"}">${signed(dr.count_new - dr.count_old)}</span>` : ""}</li></ul>`);
          parts.push(`<p class="rc-meta">Counted from the swing arcs drawn on both sheets (no model). A double door has 2 leaves, so this is more than the number of doors; 'moved' = the same door drawn at a new place.</p>`);
        }
        if (res.page_b_note) parts.push(`<p class="rc-meta">${esc(res.page_b_note)}</p>`);
        if (res.scale?.ratio) parts.push(`<p class="rc-meta">Sizes in metres at ${esc(res.scale.ratio)} (${esc(res.scale.source)}).</p>`);
        // plain words (QA 21:25: 'features+ecc', '×1.0577' was jargon); the server says how (older servers: built here)
        const how = al.how ?? (al.method.startsWith("text") ? `by the words printed on both sheets (exact to ${al.residual} ${al.residual_unit})`
          : `by matching the drawing's shapes${Math.abs(al.rotation_deg) >= 0.1 ? `: the other version was turned ${Math.abs(al.rotation_deg).toFixed(1)}°` : ""}`);
        parts.push(`<p class="rc-meta">Lined up ${esc(how)} · ${Math.round(al.overlap * 100)}% of the lines match · Cost: ${esc(res.cost?.tier ?? "cheap")} (no AI model calls${res.cost?.ocr_crops ? `; one OCR read of ${res.cost.ocr_crops} label crop(s)` : ""}${res.wall_seconds ? `, ${res.wall_seconds} s` : ""})${res.approximate ? " · scan / photo: marks are approximate (dashed)" : ""}</p>`);
        if (res.images?.b_aligned)
          parts.push(`<label class="rc-fade">Fade to the other version <input type="range" min="0" max="100" step="5" value="${fade}" data-fade> <span data-fadev>${fade}%</span></label>`);
        parts.push(`<div class="rc-legend"><span><i style="background:${COL.removed}"></i>only on this sheet (removed)</span><span><i style="background:${COL.added}"></i>only on the other (added)</span><span><i style="background:${COL.changed}"></i>changed</span></div>`);
        const txt = res.text.map((t, i) => ({ t, i })).filter(({ t }) => t.kind !== "moved");
        const moved = res.text.length - txt.length;
        if (txt.length) {
          parts.push(`<div class="rc-sec">Text that changed</div><ul class="rc-list">`);
          const order = { changed: 0, added: 1, removed: 2, moved: 3 };
          for (const { t, i } of txt.sort((x, y) => order[x.t.kind] - order[y.t.kind]).slice(0, 40)) {
            const s = t.kind === "changed" ? `‘${esc(t.old)}’ → ‘${esc(t.new)}’` : t.kind === "added" ? `‘${esc(t.new)}’ added` : `‘${esc(t.old)}’ removed`;
            parts.push(`<li data-t="${i}"><span class="rc-n" style="background:${COL[t.kind]}">T</span><span>${s}${t.source ? ` <em class="rc-meta">(${esc(t.source)})</em>` : ""}</span></li>`);
          }
          parts.push(`</ul>`);
        }
        if (moved) parts.push(`<p class="rc-meta">${moved} printed label(s) moved (same text, new place).</p>`);
        const tc = res.text_check;
        if (tc && tc.checked) parts.push(`<p class="rc-meta">Labels compared on the scan: ${tc.checked}${tc.ocr ? ` · ${tc.ocr.crops} that looked different were read by OCR (no AI model)` : " · all look the same"}${tc.note ? ` · ${esc(tc.note)}` : ""}</p>`);
        else if (tc?.note) parts.push(`<p class="rc-meta">${esc(tc.note)}</p>`);
        if (res.regions.length) {
          parts.push(`<div class="rc-sec">Linework</div><ul class="rc-list">`);
          for (const r of res.regions.slice(0, 40))
            parts.push(`<li data-r="${r.id}"><span class="rc-n" style="background:${COL[r.kind]}">${r.id}</span><span>${esc(r.description)}</span></li>`);
          parts.push(`</ul>`);
        }
        parts.push(`<div class="rc-btns">`);
        if (res.suggested_questions.length) parts.push(`<button class="primary" data-impact ${impactBusy ? "disabled" : ""}>${impactBusy ? "Asking both versions…" : "Check the quantities on both"}</button>`);
        if (res.images?.b_aligned) parts.push(`<a href="/api${esc(res.images.b_aligned)}" target="_blank" rel="noopener">Other version, lined up</a>`);
        if (res.images?.overlay) parts.push(`<a href="/api${esc(res.images.overlay)}" target="_blank" rel="noopener">Overlay image</a>`);
        parts.push(`<button data-csv>Export change list</button>`);
        parts.push(`</div>`);
        if (impact.length) {
          parts.push(`<table class="rc-imp"><tr><th>Question</th><th>This sheet</th><th>Other</th><th>Change</th></tr>${impact.map(impactRow).join("")}</table>`);
          parts.push(`<p class="rc-meta">Each number is the normal pipeline's answer on that version; click a row to see this sheet's answer with its evidence.</p>`);
        }
      }
    }
    el.innerHTML = parts.join("");
    const input = el.querySelector<HTMLInputElement>("[data-file]");
    const drop = el.querySelector<HTMLElement>("[data-drop]");
    const take = async (list: FileList | File[] | null | undefined) => {
      const fs = [...(list ?? [])].slice(0, 40);
      if (!fs.length) return;
      busy = fs.length > 1 ? `Uploading ${fs.length} files…` : `Uploading ${fs[0].name}…`;
      error = "";
      sets = null;
      rows = [];
      render(el);
      try {
        const got: FileInfo[] = [];
        for (const f of fs) got.push(await upload(f));
        got.forEach((x) => infos.set(x.path, x));
        await compareSets(got);
      } catch (e) {
        busy = "";
        error = String((e as Error).message ?? e);
        render(el);
      }
    };
    input?.addEventListener("change", () => void take(input.files));
    drop?.addEventListener("dragover", (e) => {
      e.preventDefault();
      drop.classList.add("over");
    });
    drop?.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop?.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      drop.classList.remove("over");
      void take(e.dataTransfer?.files);
    });
    el.querySelector("[data-demo]")?.addEventListener("click", (e) => {
      e.preventDefault();
      void runDemo("dfe630");
    });
    el.querySelector("[data-demoset]")?.addEventListener("click", (e) => {
      e.preventDefault();
      void runDemo("dfe630set");
    });
    el.querySelectorAll<HTMLElement>("[data-row]").forEach((li) => li.addEventListener("click", () => void openRow(rows[Number(li.dataset.row)])));
    el.querySelectorAll<HTMLElement>("[data-gonea]").forEach((li) => li.addEventListener("click", () => {
      const x = sets?.sheets_a[Number(li.dataset.gonea)];
      if (x) void api.openFile(x.file, x.page).then(() => { res = null; draw(); panel.open(); });
    }));
    el.querySelectorAll<HTMLElement>("[data-newb]").forEach((li) => li.addEventListener("click", () => {
      const x = sets?.sheets_b[Number(li.dataset.newb)];
      if (x) void api.openFile(x.file, x.page).then(() => { res = null; draw(); panel.open(); }); // a row above brings the old issue back
    }));
    el.querySelector("[data-impact]")?.addEventListener("click", () => void checkImpact());
    el.querySelector("[data-csv]")?.addEventListener("click", () => exportCsv());
    const fr = el.querySelector<HTMLInputElement>("[data-fade]");
    fr?.addEventListener("input", () => {
      fade = Number(fr.value) || 0;
      const v = el.querySelector("[data-fadev]");
      if (v) v.textContent = `${fade}%`;
      draw();
    });
    el.querySelectorAll<HTMLElement>("[data-r],[data-t],[data-doors]").forEach((li) =>
      li.addEventListener("click", () => {
        picked = li.dataset.r ? `r${li.dataset.r}` : li.dataset.t ? `t${li.dataset.t}` : "doors";
        el.querySelectorAll(".on").forEach((x) => x.classList.remove("on"));
        li.classList.add("on");
        draw();
      }),
    );
    el.querySelectorAll<HTMLElement>("tr[data-q]").forEach((tr) =>
      tr.addEventListener("click", () => {
        const row = impact[Number(tr.dataset.q)];
        if (row?.a) void api.askWith(row.question, { page: res?.a.page ?? 0 }).catch(() => undefined);
      }),
    );
  }

  const panel = api.addPanel({ id: "revcompare", title: "Compare revisions", render });
  api.addToolbarButton({
    id: "revcompare", label: "Compare revision", icon: "⇄", title: "Compare this sheet with another version of it",
    onClick: () => panel.toggle(), when: () => !!api.file() && /\.(pdf|png|jpe?g|tiff?|webp)$/i.test(api.file()!.path),
  });
  api.onDrawing(() => draw());
  const want = new URLSearchParams(location.search).get("revdemo");
  if (want) setTimeout(() => void runDemo(want), 300); // after the app's own start-up
});
