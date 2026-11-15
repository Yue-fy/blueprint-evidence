/**
 * F2 Auto takeoff (captain 19:54, prompts/F2.md): 'Generate takeoff' on a sheet -> one table with every labelled room's
 * floor area, baseboard length (perimeter minus door openings), doors and windows, slab areas by type and the
 * sheet's door / window totals. Each number keeps its confidence and a 'Show' link that outlines its evidence on the
 * drawing. The estimator's view (captain 21:13, engineer research): rooms coloured on the plan by ROOM TYPE with a
 * legend, NET totals with their rule, low-confidence rows in a Review list OUTSIDE the totals, doors as sets AND
 * leaves, wet rooms flagged (skirting usually not fitted) with a switch to leave them out of the skirting total, and
 * sheet ID + page on every exported row. Export CSV / Excel (XLSX) also carries this visit's answers (W5's
 * takeoffRows(), section 'visit'), so there is one export, not two.
 *
 * Backend: stoa/features/takeoff.py (router /feat/takeoff, through the web middleware's /api proxy). It runs the
 * existing readers one question per room quantity in a niced background job (local only, no model calls), caches by
 * file content, and reports progress; this panel polls it. Feature flag: ?features=takeoff (registry.ts). Fails closed:
 * when the API has no takeoff router the button says so and only the visitor's own answers can be exported (CSV).
 */
import { registerFeature, type FeatureApi, type Overlay2D, type DrawingPt, type Space } from "../registry";
import type { Answer, Evidence, FileInfo } from "../../types";
import { takeoffRows } from "../../takeoff";
import { PROJECTS, sameSheet } from "../../projects";
import { openCase } from "../../api";

type Row = {
  section: string; // room | printed | slab | sheet | total | visit
  item: string;
  ocr_name?: string; // a scan: what the OCR read before the name was snapped to room words
  copy?: number;
  copies?: number;
  quantity: string; // area | baseboard | doors | windows | slab_area | rooms | area_sum | <intent task>
  value: number | string | null;
  unit: string | null;
  confidence: number;
  method?: string;
  status?: string;
  note?: string;
  question?: string;
  evidence?: Evidence[];
  assumptions?: string[];
  id?: string;
  sheet?: string | null;
  page?: number | null;
  room_type?: string;
  wet?: boolean;
  leaves?: number | null;
  review?: string | null;
  flags?: string[];
  check?: { value: number | null; diff_pct: number | null; agree: boolean; how: string };
  label_box?: number[] | null;
  fixed?: string | null; // measured by a person (HH:MM)
  derived?: boolean; // a skirting from the outline a person measured for the room's area (perimeter minus its openings)
  reading?: { value: number | string | null; confidence: number; method?: string } | null;
};
type TypeSum = { type: string; label: string; color: string; rooms: number; measured: number; review: number; area: number | null; unit: string;
  focus: number[] | null; outlines: { id: string; item: string; review: boolean; bbox: number[] | null; polygon: DrawingPt[] | null; page: number | null }[] };
type PrintedTypes = {
  types: { type: string; unit: string | null; value: number; rows: string[]; items: string[] }[];
  sum: number;
  check: { printed: string; printed_value: number; sum: number; diff_pct: number | null; agree: boolean; id?: string } | null;
};
type NameSum = { item: string; copies: number; names: string[]; area?: { value: number; unit: string | null; used: number; rows: string[]; confidence: number };
  baseboard?: { value: number; unit: string | null; used: number; rows: string[]; confidence: number } };
type StoreySum = { storey: string; elevation_m: number | null; rooms: number; area: number; unit: string; doors: number; windows: number };
type Summary = { status: string; by_type?: TypeSum[]; sheet?: string | null; unit?: string; printed_by_type?: PrintedTypes | null; by_name?: NameSum[]; by_storey?: StoreySum[];
  area?: { value: number | null; unit?: string | null } | null; doors?: { value?: number | null; sets?: number | null } | null;
  rooms?: { labelled?: number; measured?: number; review?: number } | null };
/** T3's window.stoaProject.copyOf(file, page): the sheet on screen is a print / scan of another sheet of the project. */
type Orig = { file: string; page: number; label: string; level?: string | null; how?: string; catalogue?: boolean };
type Status = {
  job?: string;
  status: "queued" | "running" | "done" | "failed" | "unknown" | "off";
  done?: number;
  total?: number;
  current?: string;
  rows?: Row[];
  error?: string;
  kind?: string;
  source?: string;
  scale_n?: number | null;
  scale_calib?: { n?: number | null; agree?: number; how?: string } | null; // no printed 1:N: the scale from printed room areas
  seconds?: number;
  file?: string;
  page?: number;
  rooms?: number;
  traced?: string; // a picture: read on its traced vector copy (how the paper size was found)
  untraced?: string; // a picture we could not trace (why; rooms are not measured)
  ifc_note?: string | null; // a building model whose IfcSpaces are placeholders (no room areas)
  rooms_cut?: { measured: number; not_measured: number; names: string[]; limit: number } | null; // over the room limit
  sheet?: string | null;
  review_count?: number;
  cost?: { api_calls?: number; llm_calls_replayed?: number; tier?: string };
  ifc?: { spaces: number; linked: number; agree: number; differ: number; added: number };
};

const API = "/api/feat/takeoff";
/** This browser's id ('stoa.visitor', shared with F7 / T3 / F6): a person's Measure fixes are kept per visitor on the
 *  server, so one judge's measurement never shows to the next one (content-keyed jobs are shared by every visitor). */
const VISITOR = (() => {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  const w = window as unknown as { stoaVisitor?: string };
  const fresh = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    let v = localStorage.getItem("stoa.visitor");
    if (!ok(v)) {
      v = ok(w.stoaVisitor) ? w.stoaVisitor : fresh();
      localStorage.setItem("stoa.visitor", v);
    }
    return (w.stoaVisitor = v);
  } catch {
    return (w.stoaVisitor = ok(w.stoaVisitor) ? w.stoaVisitor : fresh());
  }
})();
const V = `&visitor=${VISITOR}`;
/** The room types' tints are the glance card's / 3D view's muted ones (one palette, UX M5): on white paper an outline
 *  in the same tint is too faint, so outlines take a darker shade of it. */
const darker = (hex: string, k = 0.45) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return hex;
  return "#" + m.slice(1).map((h) => Math.round(parseInt(h, 16) * (1 - k)).toString(16).padStart(2, "0")).join("");
};
const ROOM_COLS: [string, string][] = [["area", "Area"], ["baseboard", "Skirting"], ["doors", "Doors"], ["windows", "Windows"]];
const LABEL: Record<string, string> = { slab_area: "Slab area", doors: "Doors", windows: "Windows", area: "Floor area",
  rooms: "Rooms labelled", area_sum: "Net floor area", baseboard: "Skirting", length: "Length", count: "Count",
  baseboard_sum: "Skirting total", baseboard_dry: "Skirting total without wet rooms", door_leaves: "Door leaves", answer: "Answer",
  wall_length: "Wall length", wall_area: "Wall area (one face)" };

const CSS = `
.tk{font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;min-width:0}
.tk-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.tk-go{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer}
.tk-go[disabled]{opacity:.55;cursor:default}
.tk-go.again{background:#fff;color:#1f6feb;border:1px solid #1f6feb;font-weight:500}
.tk-key{margin:-2px 0 0}
.tk-meta{color:#57606a;font-size:12px}
.tk-prog{position:relative;height:18px;border-radius:9px;background:#eaeef2;overflow:hidden}
.tk-prog>i{position:absolute;inset:0 auto 0 0;background:#9ecbff;transition:width .3s}
.tk-prog>span{position:relative;display:block;padding:0 8px;font-size:11px;line-height:18px;color:#24292f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tk-wrap{max-height:52vh;overflow:auto;border:1px solid #d0d7de;border-radius:6px}
.tk-t{border-collapse:collapse;width:100%;font-size:11.5px}
.tk-t th,.tk-t td{padding:2px 3px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap}
.tk-t th:first-child,.tk-t td:first-child{text-align:left;white-space:normal;min-width:84px;max-width:120px}
.tk-t td.tk-why{white-space:normal;text-align:left;min-width:120px}
.tk-t thead th{position:sticky;top:0;background:#f6f8fa;font-weight:600;z-index:1}
.tk-t tr.tk-sec td{background:#f6f8fa;font-weight:600;text-align:left;color:#57606a}
.tk-v{border:0;background:none;padding:1px 3px;border-radius:4px;cursor:pointer;font:inherit;color:#0b5cad;text-decoration:underline dotted;text-underline-offset:2px}
.tk-v:not([disabled])::after{content:"⌖";font-size:9px;color:#57606a;margin-left:1px}
.tk-v:hover,.tk-v.on{background:#ddf4ff}
.tk-v[disabled]{cursor:default;color:#8c959f;text-decoration:none}
.tk-d{display:inline-block;width:6px;height:6px;border-radius:50%;margin-left:2px;vertical-align:middle}
.tk-d.s{background:#1a7f37}.tk-d.f{background:#bf8700}.tk-d.r{background:#cf222e}.tk-d.n{background:#d0d7de}
.tk-foot{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tk-foot button{border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
.tk-foot button[disabled]{opacity:.5;cursor:default}
.tk-note{color:#57606a;font-size:11px}
.tk-why{color:#57606a;font-size:11px;white-space:normal}
.tk-x{border:0;background:none;color:#8c959f;cursor:pointer;padding:0 2px}
.tk-sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;border:1px solid rgba(0,0,0,.15)}
.tk-leg{display:flex;flex-wrap:wrap;gap:4px}
.tk-leg button{display:inline-flex;align-items:center;border:1px solid #d0d7de;background:#fff;border-radius:12px;padding:2px 8px;font:inherit;font-size:11px;cursor:pointer;color:#24292f}
.tk-leg button.off{opacity:.45;text-decoration:line-through}
.tk-opt{display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#57606a}
.tk-opt label{display:inline-flex;gap:4px;align-items:center;cursor:pointer}
.tk-t tr.tk-rev td{color:#8c959f}
.tk-t tr.tk-rev td:first-child::before{content:"⚑ ";color:#bf8700}
.tk-t tr.tk-tot td{font-weight:600}
.tk-flag{color:#0b5cad;font-size:10px;margin-left:4px}
.tk-ck{font-size:10px;color:#bf8700;margin-left:1px}.tk-ck.ok{color:#1a7f37}
.tk-m{border:1px solid #1f6feb;color:#1f6feb;background:#fff;border-radius:10px;padding:0 7px;font:inherit;font-size:11px;cursor:pointer;margin-left:4px}
.tk-off{border:0;background:#1f6feb;color:#fff;border-radius:50%;width:15px;height:15px;line-height:13px;padding:0;margin-left:2px;font-size:11px;cursor:pointer;vertical-align:middle}
.tk-copy{border:1px solid #d4a72c;background:#fff8c5;border-radius:6px;padding:6px 8px;font-size:12px;color:#24292f}
.tk-copy button{border:1px solid #1f6feb;color:#1f6feb;background:#fff;border-radius:10px;padding:0 8px;font:inherit;font-size:11px;cursor:pointer;margin-left:4px}
@media (prefers-color-scheme: dark){
:root:not([data-theme="light"]) body[data-view="customer"] .tk-t thead th{background:var(--panel,#1c1c1e);color:var(--ink,#f5f5f7)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-t tr.tk-sec td{background:#2c2c2e;color:var(--muted,#98989d)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-t th,:root:not([data-theme="light"]) body[data-view="customer"] .tk-t td{border-bottom-color:var(--line,rgba(255,255,255,.12))}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-wrap{border-color:var(--line-strong,rgba(255,255,255,.2))}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-meta,:root:not([data-theme="light"]) body[data-view="customer"] .tk-note,:root:not([data-theme="light"]) body[data-view="customer"] .tk-why,:root:not([data-theme="light"]) body[data-view="customer"] .tk-opt{color:var(--muted,#98989d)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-v,:root:not([data-theme="light"]) body[data-view="customer"] .tk-flag{color:var(--accent-ink,#409cff)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-v:hover,:root:not([data-theme="light"]) body[data-view="customer"] .tk-v.on{background:var(--accent-soft,rgba(10,132,255,.24))}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-v[disabled]{color:var(--muted,#98989d)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-leg button,:root:not([data-theme="light"]) body[data-view="customer"] .tk-foot button{background:var(--fill,rgba(118,118,128,.32));color:var(--ink,#f5f5f7);border-color:var(--line-strong,rgba(255,255,255,.2))}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-go.again,:root:not([data-theme="light"]) body[data-view="customer"] .tk-m,:root:not([data-theme="light"]) body[data-view="customer"] .tk-copy button{background:transparent;color:var(--accent-ink,#409cff);border-color:var(--accent-ink,#409cff)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-prog{background:var(--fill,rgba(118,118,128,.32))}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-prog>i{background:rgba(10,132,255,.45)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-prog>span{color:var(--ink,#f5f5f7)}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-t tr.tk-rev td{color:#8e8e93}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-d.n{background:#48484a}
:root:not([data-theme="light"]) body[data-view="customer"] .tk-copy{background:rgba(255,159,10,.16);border-color:rgba(255,159,10,.5);color:var(--ink,#f5f5f7)}
}
`;

function fmtUnit(u: string | null | undefined): string {
  const s = String(u ?? "").toLowerCase();
  if (!s || s === "count") return "";
  if (s === "m2" || s === "m²" || s === "sqm") return " m²";
  if (s === "ft2" || s === "sf" || s === "sqft") return " ft²";
  return " " + String(u);
}
function fmtVal(r: Row): string {
  if (r.value === null || r.value === undefined || r.value === "") return "—";
  if (typeof r.value === "number") {
    // a count stays whole; an area / length keeps its decimals even when printed whole ('6.0 m²' read as 6 showed '6 m²'
    // next to '3.50 m²' on a Finnish plan)
    const count = r.unit === "count" || /^(doors|windows|rooms)/.test(String(r.quantity ?? ""));
    const v = count && Number.isInteger(r.value) ? String(r.value) : r.value.toFixed(r.value >= 100 ? 1 : 2);
    return (r.status === "estimate" ? "≈ " : "") + v + fmtUnit(r.unit);
  }
  return String(r.value).slice(0, 40);
}
/** The answer card's words (customer.ts confidenceWords; under 0.4 = a guess = the takeoff's Review list). */
function sure(c: number): [string, string] {
  if (c >= 0.9) return ["s", "very sure"];
  if (c >= 0.7) return ["s", "fairly sure"];
  if (c >= 0.4) return ["f", "best estimate"];
  if (c > 0) return ["r", c < 0.25 ? "rough guess" : "our guess"];
  return ["n", "no reading"];
}
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

function store<T>(key: string, v?: T): T | null {
  try {
    if (v !== undefined) {
      localStorage.setItem(key, JSON.stringify(v));
      return v;
    }
    const s = localStorage.getItem(key);
    return s ? (JSON.parse(s) as T) : null;
  } catch {
    return null;
  }
}

function csvOf(rows: Row[], file: string): string {
  const cols = ["section", "item", "quantity", "value", "unit", "confidence", "method", "evidence_box", "question", "file"];
  const q = (s: unknown) => {
    const t = String(s ?? "");
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const lines = [cols.join(",")];
  for (const r of rows) {
    const e = r.evidence?.[0];
    lines.push([r.section, r.item, LABEL[r.quantity] ?? r.quantity, r.value ?? "", r.unit ?? "", r.confidence, r.method ?? "",
      e?.bbox ? (e.bbox as number[]).map((v) => Math.round(v)).join(" ") + " " + (e.bbox_space ?? "") : "", r.question ?? "",
      e?.file ?? file].map(q).join(","));
  }
  return lines.join("\n") + "\n";
}

function download(blob: Blob, name: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

registerFeature("takeoff", (api: FeatureApi) => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  let st: Status = { status: "unknown" };
  let poll = 0;
  let shown: string | null = null; // the row key whose evidence is outlined
  let overlay: Overlay2D | null = null;
  let typesOv: Overlay2D | null = null;
  let summ: Summary | null = null; // GET /summary of the finished takeoff (room types, colours, outlines)
  let summJob = "";
  let showTypes = store<boolean>("be.takeoff.types") ?? true; // colour the plan by room type while the panel is open
  let wetIn = store<boolean>("be.takeoff.wet") ?? true; // wet rooms in the skirting total (default: in, flagged)
  const hiddenTypes = new Set<string>();
  const fileKey = () => `be.takeoff.${api.file()?.path ?? ""}#${api.page()}`;
  /** This visit's answers on this drawing (W5's takeoffRows()) as rows of section 'visit': one export for both. */
  const visit = (): Row[] => {
    const f = api.file();
    let list: ReturnType<typeof takeoffRows> = [];
    try {
      list = takeoffRows();
    } catch {
      list = [];
    }
    return list.filter((r) => !f || r.path === f.path).map((r) => {
      const n = Number(String(r.shown).replace(/,/g, ""));
      return { section: "visit", item: r.question, quantity: "answer", value: r.shown !== "" && Number.isFinite(n) ? n : r.shown || null,
        unit: r.unit || null, confidence: (r.confidence ?? 0) / 100, method: r.method, question: r.question,
        note: `${r.evidence}${r.data ? ` · data went to: ${r.data}` : ""} · asked ${r.at}`, page: r.page, evidence: [] } as Row;
    });
  };
  const readable = (f: FileInfo | null) => !!f && /^(pdf|png|jpe?g|ifc)$/i.test(String(f.ext || f.path.split(".").pop() || "").replace(/^\./, ""));

  const panel = api.addPanel({ id: "takeoff", title: "Takeoff", render: (el) => render(el), onClose: () => clearShow() });
  // a plain click on the plan (not a drag, not on a control) clears the highlighted outline (UX S-M1 00:10)
  let down: [number, number] | null = null;
  window.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; }, true);
  window.addEventListener("click", (e) => {
    const t = e.target as HTMLElement | null;
    const pane = document.getElementById("pane2d");
    if (!shown || !pane || !t || !pane.contains(t) || t.closest("button, a, input, .feat-panel, .m-panel, #featBar")) return;
    if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    clearShow();
    drawTypes();
    if (panel.isOpen()) render(panel.el);
  }, true);
  // one selection on the plan at a time (UX N-5, W5 00:35): W5's right-click selection drops our row outline, and a
  // row we light drops theirs ('stoa:deselect'); our own deselect event is ignored while we fire it
  let firing = false;
  window.addEventListener("stoa:selection", (e) => {
    if (firing || !shown || !(e as CustomEvent).detail) return;
    clearShow();
    drawTypes();
    if (panel.isOpen()) render(panel.el);
  });
  // UX M9 (pass 2a, p2a/36): Takeoff + Measure + More tools left a 250 px strip of plan. Another tool on the sheet
  // (Measure 'measure', Ask about an area 'region', the floor tool 'floor') takes over: this panel steps aside and the
  // Takeoff button brings it back (the job and its rows stay)
  window.addEventListener("stoa:tool", (e) => {
    const name = (e as CustomEvent).detail?.name;
    if (name && name !== "takeoff" && panel.isOpen()) panel.close();
  });
  window.addEventListener("keydown", (e) => {
    const t = e.target as HTMLElement | null;
    if (e.key !== "Escape" || !panel.isOpen() || (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (shown) { clearShow(); drawTypes(); render(panel.el); } // first Esc clears the highlighted outline, the next closes the panel
    else panel.close();
  });

  function ov(): Overlay2D {
    overlay ??= api.addOverlay2D("takeoff");
    return overlay;
  }
  function clearShow() {
    shown = null;
    overlay?.clear();
    typesOv?.clear();
  }
  /** Every room's traced outline filled with its room type's colour (the estimator's classic view) + a legend. */
  function drawTypes() {
    if (typesOv && !typesOv.g.isConnected) typesOv = null;
    typesOv ??= api.addOverlay2D("takeoff-types");
    const o = typesOv;
    o.clear();
    if (!showTypes || !panel.isOpen() || !summ?.by_type?.length || st.status !== "done") return;
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    for (const t of summ.by_type) {
      if (hiddenTypes.has(t.type)) continue;
      for (const s of t.outlines) {
        if (s.page !== undefined && s.page !== null && Number(s.page) !== api.page()) continue;
        let el: SVGElement | null = null;
        if (s.polygon && s.polygon.length > 2) {
          el = document.createElementNS(NS, "polygon");
          el.setAttribute("points", s.polygon.map((q) => o.toContent(q).join(",")).join(" "));
        } else if (s.bbox && s.bbox.length === 4) {
          const [x0, y0] = o.toContent([s.bbox[0], s.bbox[1]]);
          const [x1, y1] = o.toContent([s.bbox[2], s.bbox[3]]);
          el = document.createElementNS(NS, "rect");
          el.setAttribute("x", String(Math.min(x0, x1)));
          el.setAttribute("y", String(Math.min(y0, y1)));
          el.setAttribute("width", String(Math.abs(x1 - x0)));
          el.setAttribute("height", String(Math.abs(y1 - y0)));
        }
        if (!el) continue;
        el.setAttribute("fill", t.color);
        el.setAttribute("fill-opacity", s.review ? "0.25" : "0.6");
        el.setAttribute("stroke", darker(t.color));
        el.setAttribute("stroke-width", String(1.2 / z));
        if (s.review) el.setAttribute("stroke-dasharray", `${4 / z} ${3 / z}`);
        el.style.pointerEvents = "none";
        const ti = document.createElementNS(NS, "title");
        ti.textContent = `${s.item} · ${t.label}${s.review ? " · in Review (not in the totals)" : ""}`;
        el.append(ti);
        o.g.append(el);
      }
    }
  }
  let ran = false; // this page load ran (or regenerated / fixed) the open sheet's takeoff: its summary is news to the glance
  async function loadSummary() {
    const f = api.file();
    if (!f || st.status !== "done" || !st.job || summJob === st.job) return drawTypes();
    summJob = st.job;
    const fresh = ran;
    ran = false;
    try {
      const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}${V}`);
      summ = r.ok ? ((await r.json()) as Summary) : null;
    } catch {
      summ = null;
    }
    if (panel.isOpen()) render(panel.el);
    if (summ && fresh) updated();
    void loadOrig();
  }
  /** UX C-10 (01:17, MAJOR): a scanned print of a sheet the project also has (T3's copyOf: same drawing number / same
   *  look) gets its own, rougher takeoff (DfE Level 00's scan: 1,366.5 m² vs the original's 1,717.2). The panel says
   *  so, compares the two finished takeoffs and opens the original's; nothing shows without T3's project feature. */
  let orig: Orig | null = null;
  let origSum: Summary | null = null;
  let origKey = "";
  function copyOf(): Orig | null {
    const f = api.file();
    if (!f) return null;
    try {
      const p = (window as unknown as { stoaProject?: { copyOf?: (file: string, page?: number) => Orig | null } }).stoaProject;
      const o = p?.copyOf?.(f.path, api.page()) ?? null;
      if (o && o.file && !(o.file === f.path && Number(o.page ?? 0) === api.page())) return o;
    } catch {
      /* no project feature: the sample's own sheet list below */
    }
    return catalogueCopy(f.path);
  }
  /** A sample case lists its scanned print next to the sheet it was printed from (fixtures/projects.json: badge 'scan',
   *  label 'Level 00 · scanned print' beside 'Level 00'): T3's project covers uploaded sets only. */
  function catalogueCopy(path: string): Orig | null {
    try {
      for (const pr of PROJECTS) {
        const sh = pr.sheets.find((x) => sameSheet(x.path, path));
        if (!sh) continue;
        // W5's declared link first (projects.json copy_of: the glance card reads the same field), then the label rule
        const decl = (sh as { copy_of?: string }).copy_of;
        const d = decl ? pr.sheets.find((x) => x !== sh && x.path === decl) : undefined;
        if (d) return { file: d.path, page: 0, label: d.label, how: `${pr.name}'s sheet list`, catalogue: true };
        if (sh.badge !== "scan") continue;
        const base = sh.label.split(" · ")[0].trim().toLowerCase();
        const o = pr.sheets.find((x) => x !== sh && x.badge !== "scan" && x.label.trim().toLowerCase() === base);
        if (o) return { file: o.path, page: 0, label: o.label, how: `${pr.name}'s sheet list`, catalogue: true };
      }
    } catch {
      /* no catalogue */
    }
    return null;
  }
  async function loadOrig(force = false) {
    const o = copyOf();
    const k = o ? `${o.file}#${o.page ?? 0}` : "";
    if (k === origKey && !force) return;
    origKey = k;
    orig = o;
    origSum = null;
    if (o) {
      try {
        // a sample sheet opens as an upload copy on this server (W5's memoised /api/case): compare with THAT copy's
        // takeoff, the one the visitor sees after 'Open Level 00'
        const at = o.catalogue ? (await openCase(o.file)).path : o.file;
        const r = await fetch(`${API}/summary?path=${encodeURIComponent(at)}&page=${Number(o.page ?? 0)}${V}`);
        const j = r.ok ? ((await r.json()) as Summary) : null;
        if (origKey === k) origSum = j && j.status === "done" ? j : null;
      } catch {
        if (origKey === k) origSum = null;
      }
    }
    if (panel.isOpen()) render(panel.el);
  }
  window.addEventListener("stoa:project", () => void loadOrig(true));
  let reopen = false; // 'Open Level 00' from the banner: the panel opens again on the original
  async function openOrig() {
    const o = orig;
    if (!o) return;
    reopen = true;
    // a sample's sheet: its tab under the case title (W5) opens it the way a visitor would
    const tab = document.querySelector<HTMLButtonElement>(`button[role="tab"][data-path="${window.CSS.escape(o.file)}"]`);
    if (tab) return tab.click();
    try {
      await api.openFile(o.file, Number(o.page ?? 0));
    } catch {
      reopen = false;
      return flash(`Could not open ${o.label}.`);
    }
  }
  function origLine(): string {
    if (!orig) return "";
    const name = orig.label || orig.file.split("/").pop() || "the original";
    const n = (v: number) => v.toLocaleString("en", { maximumFractionDigits: 1 });
    const u = (x: Summary | null) => fmtUnit(x?.area?.unit ?? x?.unit ?? "m2");
    const mine = st.status === "done" ? summ : null;
    const a0 = origSum?.area?.value, a1 = mine?.area?.value;
    const d0 = origSum?.doors?.sets ?? origSum?.doors?.value, d1 = mine?.doors?.sets ?? mine?.doors?.value;
    let cmp = "";
    if (typeof a0 === "number" && typeof a1 === "number") {
      // the room type whose area differs most between the two readings (UX: circulation 280 vs 512)
      const t0 = new Map((origSum?.by_type ?? []).map((t) => [t.label, t.area ?? 0]));
      let worst: [string, number, number] | null = null;
      for (const t of mine?.by_type ?? []) {
        const b = t0.get(t.label);
        if (b === undefined) continue;
        const dd = Math.abs((t.area ?? 0) - b);
        if (!worst || dd > Math.abs(worst[1] - worst[2])) worst = [t.label, t.area ?? 0, b];
      }
      const big = worst && Math.abs(worst[1] - worst[2]) >= Math.max(5, 0.05 * Math.max(worst[1], worst[2]));
      // a print whose total holds only some of its rooms (a scan: single-reading outlines wait in Review) is no
      // like-for-like total: say its coverage instead of a difference (532.9 m² 'for 30 of 86 rooms', not '−1,184')
      const lab = mine?.rooms?.labelled ?? 0, got = mine?.rooms?.measured ?? 0, rv = mine?.rooms?.review ?? 0;
      const part = lab > 0 && got < 0.6 * lab; // the demo's scan: 65 of 80 keeps its difference (UX C-10)
      const cov = lab > 0 && got < lab ? ` for ${got} of ${lab} rooms${rv ? ` (${rv} in Review)` : ""}` : "";
      cmp = ` ${name}: ${n(a0)}${u(origSum)}${typeof d0 === "number" ? ` · ${d0} doors` : ""}; this print: ${n(a1)}${u(mine)}${cov}${typeof d1 === "number" ? ` · ${d1} doors` : ""}${part ? "." : ` (${a1 - a0 >= 0 ? "+" : "−"}${n(Math.abs(a1 - a0))}${u(mine)}${big ? `; ${worst![0].toLowerCase()} ${n(worst![1])} vs ${n(worst![2])}` : ""}).`}`;
    } else if (typeof a0 === "number") cmp = ` ${name}: ${n(a0)}${u(origSum)}${typeof d0 === "number" ? ` · ${d0} doors` : ""}.`;
    else cmp = ` ${name} has no finished takeoff yet: open it and generate one.`;
    return `<div class="tk-copy">A print of <b>${esc(name)}</b>${orig.how && !orig.catalogue ? ` (${esc(orig.how)})` : ""}. Use the original's takeoff: this one is read from the scanned print and is rougher.${esc(cmp)}<button data-act="orig">Open ${esc(name)}</button></div>`;
  }
  /** The glance card (W5) and others re-read a sheet's summary when its takeoff finished again or a fix changed it
   *  (UX C-13: the panel said Staff 13 · 167 m² after a regeneration while the glance kept 14 · 180). */
  function updated() {
    const f = api.file();
    try {
      window.dispatchEvent(new CustomEvent("stoa:takeoff", { detail: { file: f?.path ?? "", page: api.page(), job: st.job ?? "", status: st.status } }));
    } catch {
      /* nobody listening */
    }
  }
  function showEvidence(r: Row, key: string) {
    const o = ov();
    o.clear();
    if (shown === key) {
      shown = null;
      render(panel.el);
      return;
    }
    shown = key;
    firing = true;
    try {
      window.dispatchEvent(new CustomEvent("stoa:deselect"));
    } finally {
      firing = false;
    }
    const ev = r.evidence ?? [];
    if (ev.length && ev.some((e) => e.ifc_guid) && !ev.some((e) => e.bbox)) {
      // a building model's row (IFC takeoff): the answer card highlights the elements in 3D
      void api.showAnswer({ value: r.value as number | string | null, unit: r.unit ?? null, confidence: r.confidence ?? 0,
        evidence: ev as Answer["evidence"], trace: [r.method ?? "takeoff"], solver: "takeoff/ifc",
        assumptions: r.assumptions ?? [] } as Answer, `${LABEL[r.quantity] ?? r.quantity} of ${r.item}`).catch(() => undefined);
      render(panel.el);
      return;
    }
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    const f = api.file();
    let n = 0;
    for (const e of r.evidence ?? []) {
      if (f && e.file && e.file !== f.path && !f.path.endsWith(String(e.file).split("/").pop() ?? "")) continue;
      if (e.page !== undefined && e.page !== null && Number(e.page) !== api.page()) continue;
      const pts = (e.polygon as DrawingPt[] | null | undefined) ?? null;
      const line = (e.polyline as DrawingPt[] | null | undefined) ?? null;
      const b = e.bbox as number[] | null | undefined;
      let el: SVGElement | null = null;
      if (pts && pts.length > 2) {
        el = document.createElementNS(NS, "polygon");
        el.setAttribute("points", pts.map((p) => o.toContent(p).join(",")).join(" "));
      } else if (line && line.length > 1) {
        el = document.createElementNS(NS, "polyline");
        el.setAttribute("points", line.map((p) => o.toContent(p).join(",")).join(" "));
        el.setAttribute("fill", "none");
      } else if (b && b.length === 4) {
        const [x0, y0] = o.toContent([b[0], b[1]]);
        const [x1, y1] = o.toContent([b[2], b[3]]);
        el = document.createElementNS(NS, "rect");
        el.setAttribute("x", String(Math.min(x0, x1)));
        el.setAttribute("y", String(Math.min(y0, y1)));
        el.setAttribute("width", String(Math.abs(x1 - x0)));
        el.setAttribute("height", String(Math.abs(y1 - y0)));
      }
      if (!el) continue;
      if (el.tagName !== "polyline") el.setAttribute("fill", "rgba(31,111,235,0.18)");
      el.style.pointerEvents = "none";
      el.setAttribute("stroke", "#1f6feb");
      el.setAttribute("stroke-width", String(2.5 / z));
      const t = document.createElementNS(NS, "title");
      t.textContent = `${r.item} · ${LABEL[r.quantity] ?? r.quantity}: ${fmtVal(r)}${e.note ? " — " + e.note : ""}`;
      el.append(t);
      o.g.append(el);
      n++;
      if (n >= (r.unit === "count" ? 600 : 12)) break; // a count: one marker per door / window it counted
    }
    render(panel.el);
    if (!n) return flash(`No mark on this page for ${r.item} (${r.method || "no evidence"})`);
    const boxes = (r.evidence ?? []).map((e) => e.bbox as number[] | null | undefined).filter((b): b is number[] => !!b && b.length === 4);
    if (boxes.length && typeof (api as { focus?: unknown }).focus === "function") {
      const u = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
      // the panel covers the plan's right side (UX X-9: 45 % at 1280 px): widen the frame to the right by the covered
      // share, so the room lands in the part of the plan that stays visible
      const pane = document.getElementById("pane2d")?.getBoundingClientRect();
      const pr = (panel.el.closest(".feat-panel") as HTMLElement | null ?? panel.el).getBoundingClientRect();
      if (pane && pane.width > 0 && panel.isOpen() && pr.left < pane.right && pr.right > pane.left) {
        const f = (pr.left - pane.left) / pane.width;
        if (f > 0.3 && f < 0.97) u[2] = u[0] + (u[2] - u[0]) / f;
      }
      try {
        (api as unknown as { focus(b: number[]): void }).focus(u);
      } catch {
        /* an older registry: the outline is drawn, the view does not move */
      }
    }
  }
  let flashMsg = "";
  function flash(m: string) {
    flashMsg = m;
    render(panel.el);
    setTimeout(() => {
      if (flashMsg === m) {
        flashMsg = "";
        render(panel.el);
      }
    }, 4000);
  }

  async function start(refresh = false) {
    const f = api.file();
    if (!f) return;
    st = { status: "queued", done: 0, total: 0 };
    // the job id is content-keyed: 'Generate again' keeps it, so the old summary (legend, same-name and storey lines)
    // must be dropped here or it outlives the new reading
    summJob = "";
    summ = null;
    ran = true;
    render(panel.el);
    try {
      const r = await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), refresh, visitor: VISITOR }) });
      if (r.status === 404) {
        st = { status: "off", error: "This server has no takeoff service yet (STOA_FEATURES=takeoff on the API)." };
        return render(panel.el);
      }
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
      st = (await r.json()) as Status;
      store(fileKey() + ".job", st.job);
      render(panel.el);
      schedule();
    } catch (e) {
      st = { status: "failed", error: String((e as Error).message ?? e) };
      render(panel.el);
    }
  }
  function schedule() {
    clearTimeout(poll);
    if (st.status === "running" || st.status === "queued") poll = window.setTimeout(refreshStatus, 1500);
  }
  async function refreshStatus() {
    const job = st.job ?? store<string>(fileKey() + ".job");
    if (!job) return;
    try {
      const r = await fetch(`${API}/status?job=${encodeURIComponent(job)}&path=${encodeURIComponent(api.file()?.path ?? "")}${V}`);
      if (r.ok) st = { ...(await r.json()), job } as Status;
    } catch {
      /* keep the last status; try again */
    }
    if (panel.isOpen()) render(panel.el);
    schedule();
    if (st.status === "done") void loadSummary();
  }

  let pendingFix: Row | null = null;
  async function sendFix(id: string, m: { value: number; poly?: DrawingPt[]; space: string } | null, item?: string) {
    if (!st.job) return;
    try {
      const r = await fetch(`${API}/fix`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ job: st.job, id, value: m ? m.value : null, unit: "m2", polygon: m?.poly ?? null, space: m?.space ?? null,
          item: item ?? null, path: api.file()?.path ?? "", visitor: VISITOR }) });
      if (!r.ok) throw new Error(String(r.status));
      st = { ...(await r.json()), job: st.job } as Status;
      const sk = m && item ? (st.rows ?? []).find((x) => x.derived && x.item === item && x.quantity === "baseboard") : undefined;
      if (sk && typeof sk.value === "number")
        flash(`${item}: ${m!.value.toFixed(2)} m² measured by you; its skirting from your outline: ${sk.value.toFixed(2)} m (perimeter minus the door openings). Both are in the totals.`);
      summJob = ""; // the totals and the card changed
      ran = true;
      void loadSummary();
      if (panel.isOpen()) render(panel.el);
    } catch (e) {
      flash(`Could not save the measurement (${String((e as Error).message ?? e)}).`);
    }
  }
  api.onMeasure((m) => {
    const r = pendingFix;
    if (!r?.id || m.kind !== "area" || !(m.value > 0)) return;
    if (api.file()?.path !== m.file) return;
    pendingFix = null;
    void sendFix(r.id, { value: m.value, poly: m.poly, space: m.space }, r.item);
    if (!panel.isOpen()) panel.open();
    flash(`${r.item}: ${m.value.toFixed(2)} m² measured by you, now in the totals.`);
  });

  /** The summed lines (shown and exported, never in a total): rooms with one name ('all Secondary Classrooms', X1), and
   *  Stoa's 'areas by type' from the sheet's own printed area table, checked against its printed total. */
  function sumRows(): { names: Row[]; types: Row[]; storeys: Row[] } {
    if (st.status !== "done" || !summ) return { names: [], types: [], storeys: [] };
    const rows = st.rows ?? [];
    const byId = new Map(rows.filter((r) => r.id).map((r) => [r.id!, r]));
    const mem = (ids: string[]) => ids.map((id) => byId.get(id)).filter((r): r is Row => !!r);
    const base = { sheet: rows[0]?.sheet ?? null, page: rows[0]?.page ?? null, quantity: "area", status: "ok" };
    const names: Row[] = (summ.by_name ?? []).filter((n) => n.area).map((n) => {
      const also = n.names.filter((x) => x !== n.item);
      return { ...base, section: "same_name", item: `${n.item} ×${n.copies}`, value: n.area!.value, unit: n.area!.unit,
        confidence: n.area!.confidence, method: `${n.area!.used} of ${n.copies} rooms summed${n.area!.used < n.copies ? " (the rest are in Review)" : ""}${also.length ? ` · incl. ${also.join(", ")}` : ""}`,
        evidence: mem(n.area!.rows).flatMap((r) => r.evidence ?? []) };
    });
    const types: Row[] = (summ.printed_by_type?.types ?? []).map((t) => {
      const m = mem(t.rows);
      return { ...base, section: "printed_type", item: t.type.replace(/^./, (c) => c.toUpperCase()), value: t.value, unit: t.unit,
        confidence: m.length ? Math.min(...m.map((r) => r.confidence)) : 0.9, method: `sum of ${t.items.join(" + ")}`,
        evidence: m.flatMap((r) => r.evidence ?? []) };
    });
    // a building model, floor by floor: one line per storey (rooms' area; doors + windows by the model's storeys)
    const storeys: Row[] = (summ.by_storey ?? []).map((x) => ({ ...base, section: "storey", item: x.storey, value: x.area, unit: x.unit,
      confidence: 0.95, method: `${x.rooms} rooms · ${x.doors} doors · ${x.windows} windows (IFC model)`,
      evidence: rows.filter((r) => r.quantity === "area" && (r as { storey?: string }).storey === x.storey).flatMap((r) => r.evidence ?? []) }));
    return { names, types, storeys };
  }

  async function exportAs(fmt: "csv" | "xlsx") {
    const f = api.file();
    const sums = sumRows();
    const rows = [...(st.rows ?? []), ...sums.names, ...sums.types, ...sums.storeys.flatMap((r) => {
      const x = (summ?.by_storey ?? []).find((y) => y.storey === r.item);
      return x ? [r, { ...r, quantity: "doors", value: x.doors, unit: "count", evidence: [] }, { ...r, quantity: "windows", value: x.windows, unit: "count", evidence: [] }] : [r];
    }), ...visit()];
    const name = `${(f?.path.split("/").pop() ?? "drawing").replace(/\.[^.]+$/, "")}-takeoff.${fmt}`;
    try {
      const r = await fetch(`${API}/export`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ rows, fmt, file: f?.path ?? "" }) });
      if (!r.ok) throw new Error(String(r.status));
      download(await r.blob(), name);
    } catch {
      if (fmt === "xlsx") return flash("Excel export needs the takeoff service; CSV works here.");
      download(new Blob([csvOf(rows, f?.path ?? "")], { type: "text/csv" }), name);
    }
  }

  function cell(r: Row | undefined, key: string): string {
    if (!r) return `<td></td>`;
    const [cls, word] = sure(r.confidence);
    const tip = [`${LABEL[r.quantity] ?? r.quantity}: ${fmtVal(r)} (${word})`, r.review ? `⚑ Review, not in the totals: ${r.review}` : "",
      r.quantity === "doors" && typeof r.leaves === "number" && r.leaves !== r.value ? `${r.value} door sets = ${r.leaves} leaves` : "",
      ...(r.flags ?? []), r.check ? `model room vs our trace: ${r.check.value === null ? "no trace" : `traced ${r.check.value} (${(r.check.diff_pct ?? 0) > 0 ? "+" : ""}${r.check.diff_pct} %)`}${r.check.agree ? " ✓ agree within 2 %" : ""}` : "",
      r.sheet ? `sheet ${r.sheet}` : "", r.method ? `read by ${r.method}` : "",
      ...(r.assumptions ?? []).slice(0, 3), r.note ?? "", r.status && r.status !== "ok" ? `status: ${r.status}` : ""].filter(Boolean).join("\n");
    const can = (r.evidence ?? []).length > 0;
    const ck = r.check ? (r.check.agree ? `<span class="tk-ck ok" title="the IFC model's room and our trace agree within 2 %">✓</span>` : `<span class="tk-ck" title="the model's room and our trace differ: the model's value is shown">≠</span>`) : "";
    const off = shown === key ? `<button class="tk-off" data-off="1" title="Clear this outline from the plan (Esc, or click the number again)" aria-label="Clear the outline">×</button>` : "";
    return `<td><button class="tk-v${shown === key ? " on" : ""}" data-k="${esc(key)}" title="${esc(tip)}"${can ? "" : " disabled"}>${esc(fmtVal(r))}</button>${off}${ck}<span class="tk-d ${cls}" title="${word}"></span></td>`;
  }

  let rowIndex = new Map<string, Row>();
  function render(el: HTMLElement) {
    const f = api.file();
    rowIndex = new Map();
    if (!readable(f)) {
      el.innerHTML = `<div class="tk"><p class="tk-note">Open a drawing (PDF or image) to generate its takeoff.</p></div>`;
      return;
    }
    const keep = el.querySelector<HTMLElement>(".tk-wrap")?.scrollTop ?? 0; // a click re-renders: stay where you were
    const rows = st.rows ?? [];
    const busy = st.status === "running" || st.status === "queued";
    const pct = st.total ? Math.round((100 * (st.done ?? 0)) / st.total) : busy ? 3 : 0;
    const meta = [st.sheet ? `Sheet ${st.sheet}` : "", st.source,
      st.ifc ? `linked to the IFC model: ${st.ifc.linked} rooms (${st.ifc.agree} agree with our trace within 2 %)` : "", st.traced ? "picture read as traced lines (rough)" : "",
      st.untraced ? "picture not traced: paper size unknown, so rooms are not measured (calibrate with Measure)" : "",
      st.ifc_note ?? "",
      st.rooms_cut ? `${st.rooms_cut.not_measured} more labelled rooms NOT measured (limit ${st.rooms_cut.limit} per sheet): ${st.rooms_cut.names.slice(0, 6).join(", ")}${st.rooms_cut.not_measured > 6 ? " …" : ""}` : "",
      st.scale_calib?.n ? `scale ≈ 1:${st.scale_calib.n} derived ${st.scale_calib.how ?? ""}`
        : st.scale_n ? `scale 1:${st.scale_n}` : /^ifc/.test(String(st.kind ?? "")) ? "measured in the model: no drawing scale needed"
        : st.status === "done" ? "no scale found" : "",
      st.status === "done" ? `${st.seconds ?? "?"} s` : "",
      `Cost: cheap · on our server, no new AI model calls${st.cost?.llm_calls_replayed ? ` (${st.cost.llm_calls_replayed} earlier local-model answers reused)` : ""}`].filter(Boolean).join(" · ");
    const types = new Map((summ?.by_type ?? []).map((t) => [t.type, t]));
    const sw = (r: Row) => {
      const t = r.room_type ? types.get(r.room_type) : undefined;
      return t ? `<span class="tk-sw" style="background:${esc(t.color)}" title="${esc(t.label)}"></span>` : "";
    };
    // rooms: one line per (item, copy)
    const lines = new Map<string, Record<string, Row>>();
    for (const r of rows) {
      if (r.section !== "room") continue;
      const k = `${r.item}#${r.copy ?? 1}`;
      if (!lines.has(k)) lines.set(k, {});
      lines.get(k)![r.quantity] = r;
    }
    // a column no room has a value in (windows on a sheet with no window symbols) is left out (UX 23:30)
    const cols = ROOM_COLS.filter(([q]) => q !== "windows" || [...lines.values()].some((d) => d[q] && d[q].value !== null && d[q].value !== undefined));
    const span = cols.length + 1;
    let body = "";
    if (lines.size) {
      body += `<tr class="tk-sec"><td colspan="${span}">Rooms (${lines.size})</td></tr>`;
      for (const [k, d] of lines) {
        const any = Object.values(d)[0];
        const rev = Object.values(d).some((r) => r.review);
        const wet = any.wet ? `<span class="tk-flag" title="${esc((d.baseboard?.flags ?? [])[0] ?? "wet room: usually tiled, skirting usually not fitted")}">wet</span>` : "";
        const ocr = Object.values(d).find((r) => r.ocr_name)?.ocr_name;
        const name = sw(any) + (ocr ? `<span title="${esc(`the scan's text reads '${ocr}': spelled with the nearest room words`)}">${esc(any.item)}</span>` : esc(any.item)) + ((any.copies ?? 1) > 1 ? ` <span class="tk-note">${any.copy}/${any.copies}</span>` : "") + wet;
        body += `<tr${rev ? ` class="tk-rev" title="Something in this room is in Review: not in the totals"` : ""}><td>${name}</td>${cols.map(([q]) => {
          const key = `${k}|${q}`;
          if (d[q]) rowIndex.set(key, d[q]);
          return cell(d[q], key);
        }).join("")}</tr>`;
      }
    }
    const other = (sec: string, title: string, list: Row[], why: (r: Row) => string = (r) => r.method ?? "", label = true) => {
      if (!list.length) return;
      body += `<tr class="tk-sec"><td colspan="${span}">${title}</td></tr>`;
      list.forEach((r, i) => {
        const key = `${sec}#${i}`;
        rowIndex.set(key, r);
        const what = label && sec !== "visit" ? ` <span class="tk-note">${esc(LABEL[r.quantity] ?? r.quantity)}</span>` : "";
        const meas = sec === "review" && r.quantity === "area" && r.id && st.job
          ? ` <button class="tk-m" data-measure="${esc(key)}" title="Trace this room on the plan with the Measure tool: your number replaces ours and counts in the totals">Measure</button>` : "";
        const undo = r.fixed && r.id ? ` <button class="tk-x" data-undo="${esc(r.id)}" title="Undo your measurement (back to our reading${r.reading?.value !== null && r.reading?.value !== undefined ? `: ${esc(String(r.reading.value))}` : ""})">↺</button>` : "";
        body += `<tr${sec === "total" ? ` class="tk-tot"` : ""}><td>${sw(r)}${esc(r.item)}${what}${undo}</td>${cell(r, key)}<td colspan="${span - 2}" class="tk-why">${esc(why(r))}${meas}</td></tr>`;
      });
    };
    const review = rows.filter((r) => r.review);
    other("review", `Review — not in the totals (${review.length})`, review, (r) => r.review ?? "");
    other("fixed", "Measured on the drawing by a person (in the totals)", rows.filter((r) => r.fixed), (r) => `${r.derived ? `${r.method ?? "from your outline"}, ${r.fixed}` : `measured ${r.fixed}`}; our reading was ${r.reading?.value ?? "none"}`);
    other("printed", "Printed on the sheet", rows.filter((r) => r.section === "printed"));
    const sums = sumRows();
    if (sums.storeys.length) other("storey", "By storey (net floor area of its rooms · doors · windows)", sums.storeys, (r) => r.method ?? "", false);
    if (sums.names.length) other("byname", "Same name, several rooms (floor area summed)", sums.names, (r) => r.method ?? "", false);
    if (sums.types.length) {
      const c = summ?.printed_by_type?.check;
      other("ptype", `Printed areas by type${c ? (c.agree ? ` · add up to ${c.printed} ✓` : ` · ${c.diff_pct} % off ${c.printed}`) : ""}`, sums.types, (r) => r.method ?? "", false);
    }
    other("slab", "Slabs by type", rows.filter((r) => r.section === "slab"));
    // a piping plan (Stoa's M-101A): its tagged equipment by family, each tag marked on the plan (⌖)
    other("equipment", "Tagged equipment (piping plan)", rows.filter((r) => r.section === "equipment"), (r) => (r.assumptions ?? [])[0] ?? r.method ?? "", false);
    other("wall", "Walls by type (building model)", rows.filter((r) => r.section === "wall"));
    const sheetRows = rows.filter((r) => r.section === "sheet");
    other("sheet", "Whole sheet", sheetRows, (r) => r.quantity === "doors" && typeof r.leaves === "number" && typeof r.value === "number"
      ? `${r.value} door sets = ${r.leaves} leaves (${r.leaves - r.value} double doors) · ${r.method ?? ""}` : r.method ?? "");
    const tot = rows.filter((r) => r.section === "total" && (wetIn ? r.quantity !== "baseboard_dry" : r.quantity !== "baseboard_sum"));
    other("total", "Totals (net, Review rows left out)", tot, (r) => (r.assumptions ?? []).join(" · "), false);
    other("visit", "Your questions on this drawing (in the export too)", visit(), (r) => r.note ?? "");
    const legend = summ?.by_type?.length && st.status === "done"
      ? `<div class="tk-leg">${summ.by_type.map((t) => `<button data-type="${esc(t.type)}" class="${hiddenTypes.has(t.type) ? "off" : ""}" title="${esc(`${t.label}: ${t.rooms} rooms${t.review ? `, ${t.review} in Review` : ""}. Click to show / hide on the plan; double-click to zoom to them`)}"><span class="tk-sw" style="background:${esc(t.color)}"></span>${esc(t.label)} · ${t.rooms}${t.area !== null ? ` · ${t.area >= 100 ? t.area.toFixed(0) : t.area.toFixed(1)}${fmtUnit(t.unit)}` : ""}</button>`).join("")}</div>
        <div class="tk-opt"><label><input type="checkbox" data-opt="types"${showTypes ? " checked" : ""}> Colour the plan by room type</label>
        <label title="Wet rooms (toilets, showers, changing, laundry) are usually tiled: skirting is usually not fitted there"><input type="checkbox" data-opt="wet"${wetIn ? " checked" : ""}> Wet rooms in the skirting total</label></div>` : "";
    const btn = busy ? `<button class="tk-go" disabled>Working…</button>` : st.status === "done"
      ? `<button class="tk-go again" data-act="refresh" title="Read the sheet again (the table below is the finished takeoff)">Generate again</button>`
      : `<button class="tk-go" data-act="go">Generate takeoff</button>`;
    el.innerHTML = `<div class="tk">
      <div class="tk-head">${btn}<span class="tk-meta">${esc(meta)}</span></div>
      ${busy ? `<div class="tk-prog"><i style="width:${pct}%"></i><span>${st.status === "queued" ? "Waiting for the previous takeoff to finish…" : `${st.done ?? 0} / ${st.total ?? "?"} · ${esc(st.current ?? "")}`}</span></div>` : ""}
      ${st.status === "failed" || st.status === "off" ? `<p class="tk-note">${esc(st.error ?? "The takeoff failed.")}</p>` : ""}
      ${origLine()}
      ${legend}
      ${body ? `<p class="tk-note tk-key"><span class="tk-d s"></span> sure <span class="tk-d f"></span> best estimate <span class="tk-d r"></span> guess · ⚑ Review: not in the totals · click a number to see it on the plan (× or Esc clears it)</p>
      <div class="tk-wrap"><table class="tk-t"><thead><tr><th>Item</th>${lines.size ? cols.map(([, h]) => `<th>${h}</th>`).join("") : `<th>Value</th><th colspan="${span - 2}">Where it comes from</th>`}</tr></thead><tbody>${body}</tbody></table></div>`
        : st.status === "done" ? `<p class="tk-note">No rooms, slab types or tagged equipment were found on this page.</p>` : !busy ? `<p class="tk-note">One click reads every room's area, baseboard (perimeter minus door openings), doors and windows, and slab areas by type. Each number links to where it comes from.</p>` : ""}
      ${flashMsg ? `<p class="tk-note">${esc(flashMsg)}</p>` : ""}
      <div class="tk-foot"><button data-act="csv"${busy ? " disabled title=\"Wait until the takeoff is complete\"" : ""}>Export CSV</button><button data-act="xlsx"${busy ? " disabled title=\"Wait until the takeoff is complete\"" : ""}>Export Excel</button>
        <span class="tk-note">${busy ? "Export when the takeoff is complete. " : ""}Click a number (⌖) to see where it comes from. <span class="tk-d s"></span> sure <span class="tk-d f"></span> best estimate <span class="tk-d r"></span> guess · ⚑ in Review: not in the totals. Each export row carries its sheet and page.</span></div>
    </div>`;
    const wrap = el.querySelector<HTMLElement>(".tk-wrap");
    if (wrap && keep) wrap.scrollTop = keep;
    el.querySelectorAll<HTMLButtonElement>("button[data-k]").forEach((b) =>
      b.addEventListener("click", () => {
        const r = rowIndex.get(b.dataset.k!);
        if (r) showEvidence(r, b.dataset.k!);
      }));
    el.querySelector<HTMLButtonElement>("button[data-off]")?.addEventListener("click", () => {
      clearShow();
      drawTypes();
      render(el);
    });
    el.querySelector<HTMLButtonElement>("button[data-act=orig]")?.addEventListener("click", () => void openOrig());
    el.querySelectorAll<HTMLButtonElement>("button[data-measure]").forEach((b) =>
      b.addEventListener("click", () => {
        const r = rowIndex.get(b.dataset.measure!);
        if (!r?.id) return;
        pendingFix = r;
        const lb = r.label_box ?? (r.evidence?.[0]?.bbox as number[] | undefined) ?? null;
        // F1 390d4955 / W5 39623539: openMeasure('area', at) traces the room at `at` and OFFERS the outline ('Use it' /
        // 'Edit points' / 'Trace yourself'); the plan zooms to the room first
        const at: DrawingPt | undefined = lb && lb.length === 4 ? [(lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2] : undefined;
        if (lb && lb.length === 4) {
          const w = Math.max(lb[2] - lb[0], lb[3] - lb[1]) * 6;
          api.focus([at![0] - w, at![1] - w, at![0] + w, at![1] + w], (r.evidence?.[0]?.bbox_space as Space | undefined) ?? undefined);
        }
        api.openMeasure("area", at);
        flash(`Measure ${r.item}: check the suggested outline and press 'Use it' (or edit its points / trace it yourself): your area replaces ours and goes into the totals.`);
        panel.close(); // the Measure tool's own panel (Suggest, Done) sits where this one is: it comes back with the area
      }));
    el.querySelectorAll<HTMLButtonElement>("button[data-undo]").forEach((b) =>
      b.addEventListener("click", () => void sendFix(b.dataset.undo!, null)));
    el.querySelectorAll<HTMLButtonElement>("button[data-type]").forEach((b) => {
      b.addEventListener("click", () => {
        const t = b.dataset.type!;
        if (hiddenTypes.has(t)) hiddenTypes.delete(t);
        else hiddenTypes.add(t);
        render(el);
      });
      b.addEventListener("dblclick", () => {
        const t = types.get(b.dataset.type!);
        hiddenTypes.delete(b.dataset.type!);
        if (t?.focus) api.focus(t.focus);
        render(el);
      });
    });
    el.querySelector<HTMLInputElement>("input[data-opt=types]")?.addEventListener("change", (e) => {
      showTypes = (e.target as HTMLInputElement).checked;
      store("be.takeoff.types", showTypes);
      render(el);
    });
    el.querySelector<HTMLInputElement>("input[data-opt=wet]")?.addEventListener("change", (e) => {
      wetIn = (e.target as HTMLInputElement).checked;
      store("be.takeoff.wet", wetIn);
      render(el);
    });
    el.querySelector<HTMLButtonElement>("[data-act=go]")?.addEventListener("click", () => void start(false));
    el.querySelector<HTMLButtonElement>("[data-act=refresh]")?.addEventListener("click", () => void start(true));
    el.querySelector<HTMLButtonElement>("[data-act=csv]")?.addEventListener("click", () => void exportAs("csv"));
    el.querySelector<HTMLButtonElement>("[data-act=xlsx]")?.addEventListener("click", () => void exportAs("xlsx"));
    drawTypes();
  }

  /** A takeoff already on the server for this sheet (the catalogue warm-up, another visitor, an earlier visit): its
   *  job id from GET /summary (content-keyed, no work started), so the table shows at once. */
  async function probe() {
    const f = api.file();
    if (!f) return;
    try {
      const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}${V}`);
      if (!r.ok) return;
      const s = (await r.json()) as { status: string; job?: string };
      if (s.job && ["done", "running", "queued"].includes(s.status) && st.status === "unknown" && api.file()?.path === f.path) {
        store(fileKey() + ".job", s.job);
        st = { status: "running", job: s.job };
        void refreshStatus();
      }
    } catch {
      /* no takeoff service: the panel offers 'Generate takeoff' */
    }
  }
  function resume() {
    const job = store<string>(fileKey() + ".job");
    if (job) {
      st = { status: "running", job };
      void refreshStatus();
    } else void probe();
  }

  // UX M9: opening the takeoff closes the Measure tool's panel / a drawn 'Ask about an area' (their 'stoa:tool' listeners)
  function toolEvent() {
    try {
      window.dispatchEvent(new CustomEvent("stoa:tool", { detail: { name: "takeoff" } }));
    } catch {
      /* nobody listening */
    }
  }
  api.addToolbarButton({ id: "takeoff", label: "Takeoff", icon: "▦", title: "Generate a quantity takeoff of this sheet",
    when: () => readable(api.file()), onClick: () => {
      panel.open();
      toolEvent();
      if (st.status === "unknown") resume();
      void loadOrig();
    } });

  // a building model alone (.ifc upload) opens in 3D, where the drawing's toolbar (inside the 2D pane) is not shown:
  // the model's takeoff is offered under its answer cards instead
  const isModel = () => /\.ifc$/i.test(api.file()?.path ?? "");
  api.addCardAction({ id: "takeoff-model", label: "Takeoff of this model", icon: "▦", order: 40,
    title: "Every room of the model with its area, skirting, doors and windows, and the model's totals (Excel export)",
    when: () => isModel() && !panel.isOpen(), run: () => {
      panel.open();
      if (st.status === "unknown") resume();
    } });

  // W5's answer card records every answer of the visit (web/src/takeoff.ts); the panel lists this drawing's and the
  // export carries them (section 'visit'), so the old 'Add to takeoff' pill is gone (captain 20:12: one export).
  api.onAnswer(() => {
    if (panel.isOpen() && st.status === "done") render(panel.el);
  });

  // W5 12737064: Esc / the 'Clear marks' pill ('stoa:clear-marks' with no scope) clear every plug-in layer: our lit row's
  // outline goes (the room-type tints belong to the open panel and stay with it); Measure's scoped clears are not ours
  window.addEventListener("stoa:clear-marks", (e) => {
    if (((e as CustomEvent).detail as { scope?: string } | null | undefined)?.scope || !shown) return;
    shown = null;
    overlay?.clear();
    if (panel.isOpen()) render(panel.el);
  });
  // captain 01:18 'Start over' (W5's window 'stoa:reset' {file, catalogue, forgetCorrections}): back to the first state:
  // no outline, no tints, panel closed, legend filters and messages cleared, no Measure waiting for a room. The jobs are
  // content-keyed and stay (the sample sheets' takeoffs are warm); THIS visitor's Measure fixes are forgotten on the
  // sample sheets, and on their own uploads only when they said so (the rule of F7's corrections/forget).
  window.addEventListener("stoa:reset", (e) => {
    const d = ((e as CustomEvent).detail ?? {}) as { forgetCorrections?: boolean; forget?: boolean; forgetUploads?: boolean; catalogue?: boolean };
    clearTimeout(poll);
    clearShow();
    pendingFix = null;
    flashMsg = "";
    hiddenTypes.clear();
    if (panel.isOpen()) panel.close();
    st = { status: "unknown" };
    summ = null;
    summJob = "";
    orig = null;
    origSum = null;
    origKey = "";
    // F7's rule (night-F7 75f1f108): everything goes only when the visitor agreed to forget their OWN upload's
    // corrections (W5: forget = own && agreed; older shells: own upload && forgetCorrections); otherwise their own
    // uploads (this tab's 'stoa.ownUploads', + the open file when it is theirs) keep their fixes, sample sheets never do
    const all = d.forget === true || d.forgetUploads === true
      || (d.forget === undefined && d.forgetUploads === undefined && d.catalogue === false && d.forgetCorrections === true);
    let keep: string[] = [];
    if (!all) {
      try {
        const own = JSON.parse(sessionStorage.getItem("stoa.ownUploads") ?? "[]") as unknown;
        if (Array.isArray(own)) keep = own.filter((x): x is string => typeof x === "string" && x.startsWith("upload/"));
      } catch {
        keep = [];
      }
      if (d.catalogue === false) {
        try {
          keep = [...new Set([...keep, ...api.uploads()])];
        } catch {
          /* no uploads() */
        }
      }
    }
    void fetch(`${API}/forget`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ visitor: VISITOR, keep }) }).catch(() => undefined);
  });

  api.onDrawing(() => {
    clearTimeout(poll);
    st = { status: "unknown" };
    overlay?.clear();
    typesOv?.clear();
    summ = null;
    summJob = "";
    shown = null;
    orig = null;
    origSum = null;
    origKey = "";
    if (reopen && !panel.isOpen()) panel.open();
    reopen = false;
    if (panel.isOpen()) {
      render(panel.el);
      resume();
      void loadOrig();
    }
  });
});
