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
import { PROJECTS, sameSheet } from "../../projects"; // read-only: the catalogue's sheet labels ('Special school · Level 00')
import type { Answer, FileInfo } from "../../types";
import { registerFeature, type DrawingPt, type FeatureApi, type Overlay2D, type Space } from "../registry";

type Box = [number, number, number, number];
type Region = { id: number; kind: "added" | "removed" | "changed"; description: string; near: string | null; box_a: Box; box_b: Box };
type Demo = { id: string; title: string; credit: string; available: boolean; a: { name: string; url: string }; b: { name: string; url: string } };
type TextCh = { kind: "added" | "removed" | "changed" | "moved"; old: string | null; new: string | null; box_a: Box | null; box_b: Box | null; box_b_in_a?: Box | null; shift?: [number, number]; source?: string | null };
type ByName = { name: string; unit: string; old: number[]; new: number[]; count_old: number; count_new: number; sum_old: number; sum_new: number; delta: number; changed?: [number, number][] };
/** '12.09 → 10.5 m² (1 of 13 rooms)': some of the rooms sharing a name changed (Swiss sheets: 13 rooms 'Zimmer'). */
function partial(d: ByName, u: string): string | null {
  const ch = d.changed ?? [];
  if (d.count_old !== d.count_new || !ch.length || ch.length >= d.count_old) return null;
  return `${ch.slice(0, 3).map(([o, n]) => `${fmt(o)} → ${fmt(n)}`).join(", ")}${ch.length > 3 ? ", …" : ""} ${u} (${ch.length} of ${d.count_old} rooms)`;
}
type Result = {
  page_b_note?: string | null;
  doors?: { count_old: number; count_new: number; unchanged: number; moved: { box_a: Box; box_b: Box; moved_m: number }[]; removed: { box_a: Box }[]; added: { box_b: Box; box_b_in_a: Box }[]; scale: string | null; error?: string } | null;
  scale?: { ratio: string; source: string } | null;
  a: { file: string; page: number | null; space: string; kind: string; size: [number, number]; name?: SheetName };
  b: { file: string; page: number | null; space: string; kind: string; name?: SheetName };
  alignment: { method: string; ok: boolean; overlap: number; residual: number; residual_unit: string; scale: number; rotation_deg: number; perspective: boolean; note: string; how?: string };
  regions: Region[];
  text: TextCh[];
  text_check?: { candidates: number; checked: number; ocr: { crops: number; cpu_seconds: number } | null; note: string | null } | null;
  quantities: { what: string; old: number; new: number; unit: string; delta: number; box_a: Box; box_b: Box }[];
  areas: { by_name: ByName[]; rooms?: { name: string; kind: string }[]; total: { unit: string; old: number; new: number; delta: number } | null; source?: string; ocr_overruled?: number };
  /** a scan whose text the upload warm-up is still reading: ask again for its room areas */
  areas_pending?: boolean;
  key?: string;
  suggested_questions: { question: string; kind: string; region: number | null }[];
  summary: string;
  same_sheet?: boolean;
  /** only part of the linework lines up (< 0.85): two different drawings (another floor / building), never priced */
  other_sheet?: boolean;
  drawing_kind?: { a: string | null; b: string | null };
  approximate?: boolean;
  images?: Record<string, string>;
  wall_seconds?: number;
  cost?: { tier?: string; ocr_crops?: number; cpu_seconds?: number };
  /** captain 21:51: the quantity changes priced (example prices; skirting arrives from POST /cost) */
  revision_cost?: Cost | null;
  /** two versions of a MODEL (IFC): elements matched by GlobalId; nothing is drawn on a sheet, changes light up in 3D */
  model?: boolean;
  model_changes?: { by_class: ModelClass[]; elements: ModelEl[]; n_elements: number; count_a: number; count_b: number; matched: number; by_id: number };
  /** the compare's own arguments (the /cost call must hit the same cache entry) and its follow-up state */
  _args?: { a: string; b: string; page_a: number; page_b: number | null };
  _costBusy?: boolean;
  _costErr?: string;
  /** the skirting in revision_cost was traced with tiled wet rooms included (F4's Conventions tab) */
  _costWet?: boolean;
};
type Mark = { box: Box; kind: "added" | "removed" | "changed" };
type ModelClass = { kind: string; old: number; new: number; added?: number; removed?: number; moved?: number; changed?: number };
type ModelEl = { change: "added" | "removed" | "moved" | "changed"; cls: string; kind: string; name: string; storey: string;
  guid_a: string | null; guid_b: string | null; moved_m?: number | null; what?: { what: string; old: number | string; new: number | string; unit: string }[] };
/** What a person calls a sheet (server: sheet_name): the printed title, sheet number, revision code; 'file' = nothing
 *  printed could be read (a photo before its text layer): the title is the file name. version = the examples' issue. */
type SheetName = { title: string; number: string | null; revision: string | null; source: "sheet" | "file"; file: string; version?: string };
type CostLine = {
  id: string; group: "floor" | "doors" | "walls" | "skirting"; item: string; detail?: string; old?: number | null; new?: number | null;
  delta: number; unit: string; price: string | null; per_height?: boolean; eur: number; how?: string; approx?: boolean;
  marks: Mark[]; outlines?: { polygon: [number, number][]; kind: "added" | "removed" }[];
  /** skirting: openings deducted (default) / the whole outline (F4's Conventions 'door openings not deducted') */
  delta_net?: number; delta_whole?: number; wet?: boolean;
  /** a model's cost line: the elements it counts (lit up in 3D on a click) */
  guids?: string[];
};
type Cost = {
  prices: { key: string; label: string; unit: string; eur: number }[]; wall_height_m: number; lines: CostLine[];
  review: { id: string; item: string; why: string; marks: Mark[] }[]; total_eur: number; skirting: "pending" | "done" | "n/a";
  rule: string; prices_note: string; summary: string;
  /** why nothing is priced (two different drawings, a section / elevation) */
  withheld?: string;
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
/** '1 label', '3 labels' (UX NEW-11: no 'label(s)') */
const nn = (k: number, word: string, many?: string) => `${k} ${k === 1 ? word : many ?? `${word}s`}`;
/** UX NEW-11: the two versions in a person's words, not file names. The common title when both sheets print the same
 *  one, and what tells them apart: the example's issue, the revision code, the sheet number, else which is on screen. */
function versions(r: Result): { title: string | null; a: string; b: string } {
  const A = r.a.name, B = r.b.name;
  if (!A || !B) return { title: null, a: baseName(r.a.file), b: baseName(r.b.file) }; // an older server
  const tag = (n: SheetName, o: SheetName, dflt: string) =>
    n.version ?? (n.revision && n.revision !== o.revision ? `rev ${n.revision}` : n.number && n.number !== o.number ? `sheet ${n.number}` : dflt);
  if (A.title.toLowerCase() === B.title.toLowerCase()) {
    // same printed title, no revision to tell them apart (a visitor's own two exports, two DXF / IFC versions): their
    // file names do ('Grundriss EG · flat-rev-A → flat-rev-B'), else 'on screen' / 'the other version'
    const stem = (f: string) => baseName(f).replace(/\.[a-z0-9]{2,4}$/i, "");
    const fa = stem(A.file), fb = stem(B.file);
    const byFile = !!fa && !!fb && fa.toLowerCase() !== fb.toLowerCase();
    return { title: A.title, a: tag(A, B, byFile ? fa : "on screen"), b: tag(B, A, byFile ? fb : "the other version") };
  }
  // 'Primary school, first floor · issue A (62 m² classrooms)' (no brackets in brackets)
  const full = (n: SheetName) => (n.source === "file" ? n.file : n.title) + (n.version ? ` · ${n.version}` : n.revision ? ` rev ${n.revision}` : "");
  return { title: null, a: full(A), b: full(B) };
}
/** One sheet for the CSV (estimators want sheet + revision on every row): 'First Floor Plan · 2.01.002 · rev C01 · file.pdf' */
const sheetCell = (n: SheetName | undefined, file: string) =>
  n ? [n.source === "sheet" ? n.title : "", n.version ?? "", n.number ?? "", n.revision ? `rev ${n.revision}` : "", n.file].filter(Boolean).join(" · ") : baseName(file);
const money = (v: number) => `€${Math.abs(v).toLocaleString("en-GB", { maximumFractionDigits: 0 })}`;
const signedMoney = (v: number) => (Math.round(v) > 0 ? "+" : Math.round(v) < 0 ? "−" : "±") + money(v);
const GROUPS: { key: CostLine["group"]; label: string; note: string }[] = [
  { key: "floor", label: "Floor finishes", note: "printed room areas on both sheets" },
  { key: "doors", label: "Doors", note: "door sets: a double door is one set" },
  { key: "walls", label: "Partition walls", note: "walls that appear, vanish or are redrawn × wall height" },
  { key: "skirting", label: "Skirting", note: "traced room outline − openings; wet rooms, stairs, lifts: none" },
];
/** the cost groups of a MODEL compare: quantities from the model, not read from a sheet */
const MODEL_NOTE: Partial<Record<CostLine["group"], string>> = {
  floor: "room areas in the two models",
  doors: "doors in the two models: one door = one set",
  walls: "inner walls added, removed or resized × their own height",
};
const PRICE_KEY = "f5.prices"; // the visitor's own unit prices (this browser only)
function loadPrices(): Record<string, number> {
  try {
    const v = JSON.parse(localStorage.getItem(PRICE_KEY) ?? "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}
function savePrices(p: Record<string, number>) {
  try {
    localStorage.setItem(PRICE_KEY, JSON.stringify(p));
  } catch {
    /* private window: prices last for this visit only */
  }
}
const OFF_MSG = "Revision compare is not switched on on this server (STOA_FEATURES=revcompare or all).";
const NET_MSG = "Couldn't reach the server (it may be restarting for a moment).";
/** An error in words: a dropped connection anywhere (our calls, the app's upload) reads as NET_MSG. */
const msgOf = (e: unknown) => {
  const m = String((e as Error)?.message ?? e);
  return /Failed to fetch|NetworkError|Load failed|network error/i.test(m) ? NET_MSG : m;
};
/** fetch that says what went wrong in words: a refused / dropped connection is not 'Failed to fetch' (UX M11). */
async function call(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch {
    throw new Error(NET_MSG);
  }
}
/** 'feature is off' (our guard) or a route that is not mounted at all ('Not Found'); any other 404 ('no such file')
 *  is shown as the server said it (QA 21:19). */
const isOff = (status: number, detail: unknown) => status === 404 && (/is off \(STOA_FEATURES\)/.test(String(detail ?? "")) || String(detail ?? "") === "Not Found");

/** The panel's first sentence in plain words: what changed in quantities, then doors, renamed labels, other marks.
 *  Rooms with the same change are grouped ('Yr 3, Yr 4, Yr 5, Yr 6: 62 → 55 m² (12 rooms)'). */
/** Nothing at all differs (no linework, no label, no printed area, no door): the two are the same drawing. */
const nothingChanged = (r: Result) => r.alignment.ok && !r.regions.length && !r.text.length && !r.areas?.by_name?.length && !r.model_changes?.n_elements &&
  !(r.doors && (r.doors.added.length || r.doors.removed.length || r.doors.moved.length)) && !r.areas_pending;
/** Something is drawn on the sheet for this result (boxes): the 'Hide marks' toggle only then. */
const hasMarks = (r: Result) => r.regions.length > 0 || r.text.some((t) => t.kind !== "moved" && !!(t.box_a ?? t.box_b_in_a));

function headline(res: Result): string {
  const out: string[] = [];
  const by = res.areas?.by_name ?? [];
  const groups = new Map<string, { names: string[]; rooms: number }>();
  const other: string[] = [];
  const fresh: string[] = [];
  const some: { t: string; n: number }[] = []; // some of the rooms of one name changed
  const gone: { t: string; a: number }[] = []; // rooms only on the old version (a CAD pair: really gone)
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
    } else if (d.count_new && partial(d, u)) some.push({ t: `${d.name}: ${partial(d, u)}`, n: d.changed?.length ?? 0 });
    else if (d.count_new) other.push(`${d.name} ${fmt(d.sum_old)} → ${fmt(d.sum_new)} ${u}`);
    else if (!res.areas?.source) gone.push({ t: `${d.name} ${d.old.map(fmt).join(" + ")} ${u}`, a: d.sum_old });
    else other.push(d.name); // a scan: 'not found' may be unreadable, not gone (listed below the headline)
  }
  const big = [...groups.entries()].sort((x, y) => y[1].rooms - x[1].rooms);
  for (const [k, g] of big.slice(0, 2))
    out.push(`${g.names.sort().join(", ")}: ${k}${g.rooms > 1 ? ` (${g.rooms} rooms)` : ""}`);
  const someShown = some.sort((x, y) => y.n - x.n).slice(0, Math.max(0, 2 - big.length));
  for (const x of someShown) out.push(x.t);
  if (fresh.length) out.push(`new: ${fresh.slice(0, 3).join(", ")}`);
  if (gone.length) out.push(`gone: ${gone.sort((x, y) => y.a - x.a).slice(0, 2).map((g) => g.t).join(", ")}`); // UX: the 69 m² room that vanished was missing
  const rest = Math.max(0, big.length - 2) + (some.length - someShown.length) + other.length + Math.max(0, gone.length - 2);
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
.rc-drop{position:relative;cursor:pointer}
.rc-drop input[type=file]{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden}
.rc-drop:focus-within{outline:2px solid #3b6fd8;outline-offset:2px}
.rc-pick{display:inline-flex;align-items:center;min-height:36px;padding:0 14px;border-radius:999px;background:#1d1d1f;color:#fff;font-size:14px;font-weight:500}
.rc-retry{margin-left:8px;min-height:30px;padding:0 12px;border-radius:999px;border:0;background:#1d1d1f;color:#fff;font:inherit;font-size:13px;cursor:pointer}
.rc-sum{margin:0 0 10px;line-height:1.45}
.rc-same b{color:#1e7d32}
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
.rc-thumb{display:block;width:100%;padding:0;margin:4px 0 10px;border:1px solid var(--line,#e5e5ea);border-radius:12px;overflow:hidden;background:#fff;cursor:pointer;font:inherit;color:inherit;text-align:left}
.rc-thumb img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}
.rc-thumb span{display:block;font-size:12.5px;color:#667;padding:6px 10px}
.rc-thumb:hover{border-color:#999}
.rc-tools{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:2px 0 8px}
.rc-tools button{min-height:32px;padding:0 12px;border-radius:999px;border:0;background:var(--fill-2,rgba(0,0,0,.06));font:inherit;font-size:13px;cursor:pointer;color:inherit}
.rc-tools button[aria-pressed="true"]{background:#1d1d1f;color:#fff}
.rc-tools small{color:var(--ink-2,#667);font-size:12px}
.rc-btns button,.rc-btns a{min-height:40px;padding:0 14px;border-radius:999px;border:0;background:var(--fill-2,rgba(0,0,0,.06));font:inherit;cursor:pointer;color:inherit;text-decoration:none;display:inline-flex;align-items:center}
.rc-btns button.primary{background:#1d1d1f;color:#fff}
.rc-imp td,.rc-imp th{padding:5px 6px;text-align:left;font-size:13.5px;vertical-align:top}
.rc-imp{border-collapse:collapse;width:100%}.rc-imp tr+tr td{border-top:1px solid var(--line,#e5e5ea)}
.rc-imp small{color:var(--ink-2,#667)}
.rc-busy{color:var(--ink-2,#667);font-size:14px}
.rc-fade{display:flex;align-items:center;gap:10px;font-size:13.5px;margin:0 0 12px}
.rc-fade input{flex:1;min-height:32px}
.rc-cost{border:1px solid var(--line,#e5e5ea);border-radius:14px;padding:12px 12px 6px;margin:0 0 14px}
.rc-total{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:0 0 4px}
.rc-total b{font-size:26px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.rc-total small{color:var(--ink-2,#667)}
.rc-ct{border-collapse:collapse;width:100%;margin:6px 0 4px}
.rc-ct td{padding:5px 6px;font-size:13.5px;vertical-align:baseline;line-height:1.3}
.rc-ct td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.rc-ct tr.g td{font-weight:600;padding-top:10px;border-bottom:1px solid var(--line,#e5e5ea)}
.rc-ct tr.g small{font-weight:400}
.rc-ct tr.l{cursor:pointer}.rc-ct tr.l:hover td,.rc-ct tr.l.on td{background:var(--fill-2,rgba(0,0,0,.05))}
.rc-ct small{color:var(--ink-2,#667)}
.rc-ct .neg{color:${COL.removed}}.rc-ct .pos{color:${COL.added}}
.rc-ct tr.more td{color:#3b6fd8;cursor:pointer;font-size:13px}
.rc-prices{margin:6px 0 8px;font-size:13.5px}
.rc-prices summary{cursor:pointer;font-weight:600;padding:4px 0}
.rc-prices .ex{display:inline-block;background:rgba(240,140,0,.14);color:#8a5200;border-radius:6px;padding:1px 7px;font-size:12px;font-weight:600;margin-left:6px}
.rc-pgrid{display:grid;grid-template-columns:1fr auto;gap:6px 10px;align-items:center;margin:6px 0}
.rc-pgrid input{width:88px;min-height:34px;padding:0 8px;border-radius:8px;border:1px solid var(--line,#ccd);font:inherit;text-align:right;background:transparent;color:inherit}
.rc-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:9999;display:flex;align-items:center;gap:10px;max-width:min(560px,calc(100vw - 32px));padding:10px 12px 10px 16px;border-radius:14px;background:#1d1d1f;color:#fff;font-size:14px;box-shadow:0 8px 30px rgba(0,0,0,.25)}
.rc-toast button{min-height:32px;padding:0 12px;border-radius:999px;border:0;background:rgba(255,255,255,.16);color:#fff;font:inherit;cursor:pointer}
.rc-pgrid label{display:flex;align-items:center;gap:6px;justify-content:flex-end;white-space:nowrap}
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
  let marksOff = false;
  /** F4's Conventions tab (window.stoaConventions + event 'stoa:conventions', as F3's order list and F8's report
   *  follow it): skirting with or without tiled wet rooms, door openings deducted or not. null = that tab is off. */
  let conv: { wetSkirting?: boolean; deductOpenings?: boolean } | null = null;
  const wetOn = () => conv?.wetSkirting === true;
  let retry: (() => void) | null = null; // the last action that failed on the network: 'Try again' runs it once more
  /** A notice at the bottom of the screen for the one-click example while no drawing is open: the panel sits behind
   *  the landing page then, so its progress and errors would be invisible (UX M11: a failed first click did nothing). */
  let toastEl: HTMLDivElement | null = null;
  function toast(msg: string | null, again?: () => void) {
    if (!msg) {
      toastEl?.remove();
      toastEl = null;
      return;
    }
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "rc-toast";
      toastEl.setAttribute("role", "status");
      document.body.append(toastEl);
    }
    toastEl.innerHTML = `<span>${esc(msg)}</span>${again ? `<button type="button" data-again>Try again</button>` : ""}${again ? `<button type="button" data-x aria-label="Close">×</button>` : ""}`;
    toastEl.querySelector("[data-again]")?.addEventListener("click", () => { toast(null); again?.(); });
    toastEl.querySelector("[data-x]")?.addEventListener("click", () => toast(null));
  } // the visitor hid the marks ('Hide marks', Esc, the panel closed); a pick or a new compare shows them
  /** The app's start-up lists the files and THEN shows the landing (main.ts boot → window.__ready). The example link
   *  opened its sheet before that (cached uploads: 0.7 s, the boot's landing at 1.1 s), so the example sat BEHIND the
   *  landing and every click hit it (QA 02:46). Wait for the start-up (at most 20 s), open, then leave the landing. */
  async function bootDone(ms = 20000) {
    const t0 = Date.now();
    while (!(window as unknown as { __ready?: boolean }).__ready && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 100));
  }
  function leaveLanding() {
    const l = document.getElementById("landing");
    if (l && !l.hidden) l.hidden = true; // as a sample card does (no history entry: Back leaves the deep link)
  }
  let impact: Impact[] = [];
  let impactBusy = false;
  let fade = 0; // 0..100: opacity of the other version laid over this one (lined up)
  let demoCredit = ""; // licence credit of the one-click example on screen
  let sets: SetsRes | null = null; // both issues' sheets paired (only when either side has more than one sheet)
  let rows: SetRow[] = [];
  let setRun = 0; // bumps on every new drop: an older run stops filling rows
  let cmpRun = 0; // bumps on every compare and on 'Start over': a late answer of an older one is dropped
  const infos = new Map<string, FileInfo>(); // the dropped files' upload records by path
  let myPrices: Record<string, number> = loadPrices(); // overrides of the example prices ('__h' = wall height, m)
  const costMore = new Set<string>(); // cost groups the visitor expanded past their first lines
  const ov: Overlay2D = api.addOverlay2D("revcompare");

  const onA = () => !!res && api.file()?.path === res.a.file && api.page() === (res.a.page ?? 0);

  function rect(box: Box, color: string, opts: { dashed?: boolean; label?: string; bold?: boolean; fill?: boolean; key?: string } = {}) {
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
    // a white halo under the box: readable over another tool's colour fill (UX M9: marks on the takeoff colours)
    const halo = r.cloneNode() as SVGRectElement;
    halo.setAttribute("fill", "none");
    halo.setAttribute("stroke", "#fff");
    halo.setAttribute("stroke-opacity", "0.9");
    halo.setAttribute("stroke-width", String(((opts.bold ? 3.5 : 2) + 3) * z));
    halo.removeAttribute("stroke-dasharray");
    ov.g.append(halo, r);
    const hit: Element[] = [halo];
    if (opts.label) {
      const c = document.createElementNS(SVG, "circle");
      const cx = x0, cy = y0, rad = 10 * z;
      c.setAttribute("cx", String(cx));
      c.setAttribute("cy", String(cy));
      c.setAttribute("r", String(rad));
      c.setAttribute("fill", color);
      c.setAttribute("stroke", "#fff");
      c.setAttribute("stroke-width", String(1.5 * z));
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
      hit.push(c, t);
    }
    // UX X-5: a mark answers a click (its number or its outline): picked, its line scrolled into view in the panel
    if (opts.key) {
      const key = opts.key;
      for (const e of hit as SVGElement[]) {
        e.style.pointerEvents = e === halo ? "stroke" : "auto";
        e.style.cursor = "pointer";
        e.addEventListener("click", (ev) => {
          ev.stopPropagation();
          pickMark(key);
        });
      }
    }
  }

  /** Pick a change from the drawing (click on its mark, or right-click → 'What changed here'). */
  function pickMark(key: string) {
    picked = key;
    marksOff = false;
    if (!panel.isOpen()) panel.open();
    draw();
    refresh();
    const sel = key.startsWith("r") ? `[data-r="${key.slice(1)}"]` : key.startsWith("t") ? `[data-t="${key.slice(1)}"]` : "";
    const li = sel ? panel.el?.querySelector<HTMLElement>(sel) : null;
    li?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  /** The change mark under the pointer (sheet A's frame; the smallest box wins), as a picked key. Hit-tested on the
   *  screen: the box plus 14 px, so its number badge (drawn on the box corner) counts too. */
  function markAt(ctx: { point: DrawingPt | null; space: string; client: { x: number; y: number } }): string | null {
    if (!ctx.point || !res || !onA() || !res.alignment.ok || marksOff || ctx.space !== res.a.space) return null;
    const pad = 14;
    let best: { key: string; area: number } | null = null;
    const consider = (box: Box | null | undefined, key: string) => {
      if (!box) return;
      const p = ov.toClient([box[0], box[1]]), q = ov.toClient([box[2], box[3]]);
      if (!p || !q) return;
      const x0 = Math.min(p.x, q.x) - pad, x1 = Math.max(p.x, q.x) + pad, y0 = Math.min(p.y, q.y) - pad, y1 = Math.max(p.y, q.y) + pad;
      const { x, y } = ctx.client;
      if (x < x0 || x > x1 || y < y0 || y > y1) return;
      const area = (x1 - x0) * (y1 - y0);
      if (!best || area < best.area) best = { key, area };
    };
    for (const r of res.regions) { // a numbered badge (on the box's top-left corner) wins outright
      const c = ov.toClient([r.box_a[0], r.box_a[1]]);
      if (c && Math.hypot(ctx.client.x - c.x, ctx.client.y - c.y) <= 14) return `r${r.id}`;
    }
    for (const r of res.regions) consider(r.box_a, `r${r.id}`);
    res.text.forEach((t, i) => t.kind !== "moved" && consider(t.box_a ?? t.box_b_in_a, `t${i}`));
    return (best as { key: string } | null)?.key ?? null;
  }

  /** A traced room outline (skirting lines): drawing coordinates, sheet A's frame. */
  function poly(pts: [number, number][], color: string) {
    const z = 1 / Math.max(1e-6, ov.zoom());
    const p = document.createElementNS(SVG, "polygon");
    p.setAttribute("points", pts.map((q) => ov.toContent(q as DrawingPt).join(",")).join(" "));
    p.setAttribute("fill", color);
    p.setAttribute("fill-opacity", "0.16");
    p.setAttribute("stroke", color);
    p.setAttribute("stroke-width", String(3.5 * z));
    p.setAttribute("stroke-linejoin", "round");
    const halo = p.cloneNode() as SVGPolygonElement;
    halo.setAttribute("fill", "none");
    halo.setAttribute("stroke", "#fff");
    halo.setAttribute("stroke-opacity", "0.9");
    halo.setAttribute("stroke-width", String(6.5 * z));
    ov.g.append(halo, p);
  }

  /** The one-click example as a picture (its changes in red / green, cut where most changed): a click runs it. */
  const demoThumb = () => `<button type="button" class="rc-thumb" data-demo title="Open both issues and compare them"><img src="/api/feat/revcompare/demo/dfe630/thumb.png" alt="The example: a school's first floor, walls only in the old issue red, only in the new one green" loading="lazy" onerror="this.parentElement.remove()"><span>A real revision: red only in the old issue, green only in the new one</span></button>`;

  /** Re-render only when the panel is open (background steps must not pop a closed panel back up). */
  const refresh = () => { if (panel.isOpen()) panel.open(); };
  /** Forget the comparison on screen (marks, cost, sheet rows): back to 'drop the other version'. */
  function clearAll() {
    if (res?.model) void light([]);
    setRun++;
    cmpRun++;
    busy = "";
    res = null;
    other = null;
    sets = null;
    rows = [];
    impact = [];
    picked = null;
    fade = 0;
    demoCredit = "";
    error = "";
    marksOff = false;
    draw();
    refresh();
  }

  function draw() {
    ov.clear();
    if (!res || res.model || !onA() || !res.alignment.ok || marksOff) return;
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
    const costPick = !!picked && (picked.startsWith("c:") || picked.startsWith("v:")); // only that line's marks
    if (!costPick)
      for (const r of res.regions)
        rect(r.box_a, COL[r.kind], { dashed, label: String(r.id), bold: picked === `r${r.id}`, fill: picked === `r${r.id}`, key: `r${r.id}` });
    if (picked === "doors" && res.doors && res.doors.count_old != null) {
      for (const d of res.doors.removed) rect(d.box_a, COL.removed, { bold: true });
      for (const d of res.doors.added) rect(d.box_b_in_a, COL.added, { bold: true });
      for (const d of res.doors.moved) rect(d.box_a, COL.changed, { bold: true });
    }
    const rc = res.revision_cost;
    const cl = picked?.startsWith("c:") ? rc?.lines.find((l) => `c:${l.id}` === picked) : picked?.startsWith("v:") ? rc?.review.find((l) => `v:${l.id}` === picked) : null;
    if (cl) {
      for (const o of ("outlines" in cl ? cl.outlines : undefined) ?? []) poly(o.polygon, COL[o.kind]);
      for (const m of cl.marks) rect(m.box, COL[m.kind], { bold: true, fill: true, dashed: dashed || ("approx" in cl && !!cl.approx && cl.group !== "skirting") });
    }
    res.text.forEach((t, i) => {
      if (t.kind === "moved" || costPick) return;
      const box = t.box_a ?? t.box_b_in_a ?? null;
      if (box) rect(box, COL[t.kind], { dashed: !!t.source, bold: picked === `t${i}`, fill: picked === `t${i}`, key: `t${i}` });
    });
  }

  /** UX X-5: every change in view after a compare, left of the panel (the viewer's zoomTo leaves the panel's width
   *  out), instead of the marks on the right running under it. Once per compare / opened row, not on re-renders. */
  function frameAll() {
    if (!res || !onA() || !res.alignment.ok || marksOff) return;
    // the linework changes (the building); a label changed in the title block or the legend would stretch the frame to
    // the whole sheet, so labels count only when there is no linework change
    const texts = res.text.filter((t) => t.kind !== "moved" && (t.box_a ?? t.box_b_in_a)).map((t) => (t.box_a ?? t.box_b_in_a)!);
    const boxes: Box[] = res.regions.length ? res.regions.map((r) => r.box_a) : texts;
    if (!boxes.length) return;
    const u = [0, 1, 2, 3].map((k) => (k < 2 ? Math.min : Math.max)(...boxes.map((b) => b[k])));
    // api.focus pads the box by 0.8 of its size on each side; half the box around its centre = the changes + 15 %
    // at least a fifth of the sheet: one new wall alone is shown with the rooms around it (like the viewer's own
    // evidence framing), not blown up to the whole screen
    const [W, H] = res.a.size ?? [0, 0];
    const cx = (u[0] + u[2]) / 2, cy = (u[1] + u[3]) / 2;
    const hw = Math.max(u[2] - u[0], 0.2 * W) / 4, hh = Math.max(u[3] - u[1], 0.2 * H) / 4;
    try {
      api.focus([cx - hw, cy - hh, cx + hw, cy + hh], res.a.space as Space);
    } catch {
      /* an older shell without focus: the view stays */
    }
  }

  /** frameAll once the drawing is on screen: a cached compare answers before the viewer has drawn a just-opened sheet,
   *  and its first fit() (which ignores the panel) would undo the frame. The viewer marks itself data-state="ready"
   *  right before that fit; an observer callback runs after it. */
  function frameWhenShown() {
    const v = document.getElementById("v2d");
    if (!v || v.dataset.state === "ready") return frameAll();
    const mo = new MutationObserver(() => {
      if (v.dataset.state !== "ready") return;
      mo.disconnect();
      frameAll();
    });
    mo.observe(v, { attributes: true, attributeFilter: ["data-state"] });
    setTimeout(() => mo.disconnect(), 60000);
  }

  async function post(path: string, body: unknown): Promise<unknown> {
    const r = await call(`/api/feat/revcompare/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (isOff(r.status, j.detail)) throw new Error(OFF_MSG);
    if (!r.ok) throw new Error(j.detail ? String(j.detail) : `the server said ${r.status}`);
    return j;
  }
  const fetchCompare = async (a: string, pageA: number, b: string, pageB?: number) => {
    const args = { a, b, page_a: pageA, page_b: pageB ?? null }; // page_b null: the server picks B's page
    const r = (await post("compare", args)) as Result;
    r._args = args;
    return r;
  };

  /** The cost lines arrive at once (floor, doors, walls); skirting needs every changed room traced on both sheets
   *  (~0.5 s a room): fetched here once per result, then the panel re-renders with it. */
  async function followCost(r: Result | null) {
    const c = r?.revision_cost;
    // skirting traced without wet rooms while the Conventions tab includes them (or the other way round): trace again
    if (r && c && !c.withheld && c.skirting === "done" && (r._costWet ?? false) !== wetOn() && !r._costBusy) c.skirting = "pending";
    if (!r || !c || c.skirting !== "pending" || !r._args || r._costBusy || r._costErr) return;
    r._costBusy = true;
    if (res === r) refresh();
    try {
      const wet = wetOn();
      const got = (await post("cost", { ...r._args, wet_skirting: wet })) as { revision_cost: Cost | null };
      if (got.revision_cost) {
        r.revision_cost = got.revision_cost;
        r._costWet = wet;
      }
    } catch (e) {
      r._costErr = String((e as Error).message ?? e).slice(0, 120);
    }
    r._costBusy = false;
    if (res === r) {
      draw();
      refresh();
    }
  }

  /** A just-dropped scan: the upload warm-up reads its text in the background (20-60 s); ask again until the server
   *  has its room tags, then swap the result in (same key = same pair), unless the visitor moved on. */
  async function followAreas(first: Result, ask: () => Promise<Result>, row?: SetRow) {
    for (let i = 0; i < 30 && first.areas_pending; i++) {
      await new Promise((ok) => setTimeout(ok, 6000));
      let r: Result;
      try {
        r = await ask();
      } catch {
        return;
      }
      if (r.areas_pending) continue;
      if (row) row.res = r;
      if (res && res.key === r.key) {
        res = r;
        draw();
      }
      refresh();
      return;
    }
  }

  async function compare(b: FileInfo, pageB?: number) {
    const a = api.file();
    if (!a) return;
    retry = () => void compare(b, pageB);
    other = b;
    res = null;
    error = "";
    impact = [];
    picked = null;
    marksOff = false;
    busy = `Lining up ${baseName(b.path)} with the sheet on screen…`;
    panel.open();
    const run = ++cmpRun;
    try {
      const pageA = api.page();
      const got = await fetchCompare(a.path, pageA, b.path, pageB);
      if (run !== cmpRun) return; // 'Start over' or a newer drop meanwhile
      res = got;
      marksOff = !!res.other_sheet; // two different drawings: 100+ label boxes would bury the plan ('Show marks' brings them)
      const row = rows.find((x) => x.a.file === a.path && x.a.page === pageA && x.b.file === b.path);
      if (row) row.res = res;
      if (res.areas_pending) void followAreas(res, () => fetchCompare(a.path, pageA, b.path, res?.b.page ?? pageB), row);
    } catch (e) {
      if (run !== cmpRun) return;
      error = msgOf(e);
    }
    busy = "";
    draw();
    panel.open();
    if (res?.model) void light(res.alignment.ok ? modelGuids(res) : []);
    else frameWhenShown();
    void followCost(res);
  }

  /** Several files dropped, or a multi-page PDF on either side: pair both issues' sheets, compare the pair on screen
   *  first, then every other pair in turn (cached on the server: clicking a row later is instant). */
  async function compareSets(bs: FileInfo[]) {
    const a = api.file();
    if (!a) return;
    if (/\.ifc$/i.test(a.path) || bs.some((x) => /\.ifc$/i.test(x.path))) return compare(bs[0]); // a model: no sheets to pair
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
    if (sr.sheets_b.length === 1 && sr.pairs.length === 1) {
      // one sheet dropped against a multi-page set: it is THAT sheet's new version, not an issue that dropped the rest
      const p = sr.pairs[0], x = sr.sheets_a[p.a];
      if (api.file()?.path !== x.file || api.page() !== x.page) await api.openFile(x.file, x.page);
      return compare(bs[0], sr.sheets_b[p.b].page);
    }
    sets = sr;
    rows = sr.pairs.map((p) => ({ a: sr!.sheets_a[p.a], b: sr!.sheets_b[p.b], weak: p.weak }));
    if (!infos.has(bs[0].path)) bs.forEach((x) => infos.set(x.path, x));
    const here = rows.find((r) => r.a.file === a.path && r.a.page === api.page());
    if (here) await compare(infos.get(here.b.file) ?? { ...bs[0], path: here.b.file }, here.b.page);
    else if (rows.length) await openRow(rows[0]); // the sheet on screen has no partner: open the first one that has
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
    refresh();
    try {
      row.res = await fetchCompare(row.a.file, row.a.page, row.b.file, row.b.page);
      if (row.res.areas_pending) void followAreas(row.res, () => fetchCompare(row.a.file, row.a.page, row.b.file, row.b.page), row);
    } catch (e) {
      row.err = msgOf(e).slice(0, 160);
    }
    row.busy = false;
    refresh(); // rows fill in the background: never re-open a panel the visitor closed
  }

  /** A row click: open that sheet of the old issue and show its compare (drawn on it). */
  async function openRow(row: SetRow) {
    if (api.file()?.path !== row.a.file || api.page() !== row.a.page) await api.openFile(row.a.file, row.a.page);
    other = infos.get(row.b.file) ?? other;
    impact = [];
    picked = null;
    marksOff = false;
    if (!row.res && !row.err) await fillRow(row);
    res = row.res ?? null;
    marksOff = !!res?.other_sheet;
    error = row.err ?? "";
    draw();
    panel.open();
    frameWhenShown();
    void followCost(res);
  }

  const sheetName = (x: SheetRef) => `${x.n_pages > 1 ? `page ${x.page + 1} · ` : ""}${x.sheet_no ? `${x.sheet_no} ` : ""}${x.title || baseName(x.file)}`;
  function rowLine(r: SetRow): string {
    if (r.busy) return "comparing…";
    if (r.err) return esc(r.err);
    if (!r.res) return "click to compare";
    if (!r.res.alignment.ok) return "could not be lined up: probably another sheet";
    if (nothingChanged(r.res)) return `<span class="rc-same"><b>✓ Nothing changed</b></span>`;
    const h = headline(r.res);
    const bits = (h.startsWith("What changed: ") ? h.slice(14) : h).replace(/\.$/, "").split(" · ");
    const rc = r.res.revision_cost;
    if (r.res.other_sheet) return "looks like a different sheet (another floor?): not priced";
    const cost = rc && rc.lines.some((l) => l.price) ? ` · <b>${signedMoney(totalOf(rc))}</b>` : "";
    const line = esc(bits.length > 3 ? `${bits.slice(0, 2).join(" · ")} · +${bits.length - 2} more` : bits.join(" · ")) + cost; // the full line is shown when opened
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
    const p: string[] = [`<div class="rc-sec">The whole issue: ${sets.sheets_a.length} → ${nn(sets.sheets_b.length, "sheet")}</div>`];
    const priced = rows.filter((r) => r.res?.revision_cost && r.res.same_sheet !== false);
    const issueEur = priced.reduce((s, r) => s + totalOf(r.res!.revision_cost!), 0);
    const skPending = priced.some((r) => r.res!.revision_cost!.skirting === "pending");
    p.push(`<p class="rc-meta">${esc(sets.summary)}${changed || same ? ` · compared so far: ${changed} changed, ${same} unchanged` : ""}${priced.some((r) => r.res!.revision_cost!.lines.some((l) => l.price)) ? ` · <b>cost of the issue so far ${signedMoney(issueEur)}</b> (example prices${skPending ? "; skirting only for the sheets you opened" : ""})` : ""} · sheets paired by ${sets.pairs.some((x) => x.how === "features") ? "their printed words or, on scans, their shapes" : "their printed words"} (no AI model)</p><ul class="rc-list rc-set">`);
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
    retry = () => void runDemo(id);
    error = "";
    sets = null;
    rows = [];
    setRun++;
    busy = "Fetching the example (two issues of the same sheet)…";
    panel.open();
    const landing = !api.file(); // the panel is not visible yet: say it at the bottom of the screen too
    if (landing) toast("Opening the example: two issues of a school's first floor…");
    try {
      const lr = await call("/api/feat/revcompare/demo");
      const lj = await lr.json().catch(() => ({}));
      if (isOff(lr.status, lj.detail)) throw new Error(OFF_MSG);
      if (!lr.ok) throw new Error(lj.detail ? String(lj.detail) : `the server said ${lr.status}`);
      const d = ((lj.demos ?? []) as Demo[]).find((x) => x.id === id);
      if (!d || !d.available) throw new Error("This example's drawings are not on this server.");
      const get = async (f: { name: string; url: string }) => {
        const r = await call(`/api${f.url}`);
        if (!r.ok) throw new Error(`could not fetch ${f.name} (${r.status})`);
        return new File([await r.blob()], f.name, { type: "application/pdf" });
      };
      const [fa, fb] = await Promise.all([get(d.a), get(d.b)]);
      busy = "Uploading both versions…";
      render(panel.el);
      const [ia, ib] = await Promise.all([upload(fa), upload(fb)]);
      await bootDone();
      await api.openFile(ia.path, 0);
      leaveLanding();
      toast(null);
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
      error = msgOf(e);
      panel.open();
      render(panel.el);
      if (!api.file()) toast(`Couldn't open the example. ${error}`, () => void runDemo(id));
      else toast(null);
    }
  }

  async function checkImpact() {
    if (!res || !other || impactBusy) return;
    impactBusy = true;
    const r0 = res, b0 = other;
    impact = r0.suggested_questions.slice(0, 6).map((q) => ({ question: q.question }));
    panel.open();
    // a room printed only on one version is asked only there ('new: 69 m²' needs no answer from the old sheet); both
    // versions and up to 3 asks at once (QA 21:25: 12 asks one after another took 196 s on a loaded server)
    const roomOf = (q: string) => r0.areas?.by_name?.find((d) => q.toLowerCase().includes(d.name.toLowerCase()));
    const jobs: (() => Promise<void>)[] = [];
    for (const row of impact) {
      const d = roomOf(row.question);
      const ask = (side: "a" | "b") => async () => {
        try {
          const ans = await api.askWith(row.question, side === "a" ? { show: false, file: r0.a.file, page: r0.a.page ?? 0 } : { show: false, file: b0.path, page: r0.b.page ?? 0 });
          if (res === r0) row[side] = ans;
        } catch (e) {
          row.err = String((e as Error).message ?? e).slice(0, 120);
        }
        refresh();
      };
      if (!d || d.count_old > 0) jobs.push(ask("a"));
      if (!d || d.count_new > 0) jobs.push(ask("b"));
    }
    let next = 0;
    await Promise.all([0, 1, 2].map(async () => {
      while (next < jobs.length && res === r0) await jobs[next++]();
    }));
    impactBusy = false;
    refresh();
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
    // a room printed on one version only was asked only there: 'new' / 'gone', checked against its own printed tag
    const only = room && !room.count_old ? "b" : room && !room.count_new ? "a" : null;
    if (only === "b" && typeof vb === "number") d = `<span class="rc-d pos">new</span>`;
    if (only === "a" && typeof va === "number") d = `<span class="rc-d neg">gone</span>`;
    const ok = (m: unknown, p: number | undefined) => typeof m === "number" && p != null && Math.abs(m - p) <= 0.03 * p;
    if (room && (u === "m2" || u === room.unit) && (only ? typeof (only === "a" ? va : vb) === "number" : typeof va === "number" && typeof vb === "number")) {
      const po = room.old[0], pn = room.new[0];
      agreed = only === "a" ? ok(va, po) : only === "b" ? ok(vb, pn) : ok(va, po) && ok(vb, pn);
      check = agreed ? `<br><small>✓ printed ${po != null ? fmt(po) : "—"} → ${pn != null ? fmt(pn) : "—"} ${unitWord(room.unit)}: measured and printed agree</small>`
        : `<br><small>printed ${po != null ? fmt(po) : "—"} → ${pn != null ? fmt(pn) : "—"} ${unitWord(room.unit)}</small>`;
    }
    // QA 21:25: 'a guess' next to '✓ measured and printed agree' read as a contradiction: when the printed tag confirms
    // the measurement, say that instead of the measuring step's own doubt
    const how = (x: Answer) => (agreed ? "matches the printed tag" : sure(x.confidence));
    const val = (x: Answer | undefined, side: "a" | "b") => (only && only !== side ? `<small>not on this version</small>`
      : x ? (x.value == null ? "—" : `${typeof x.value === "number" ? fmt(x.value) : esc(x.value)} ${esc(shownUnit(x))}<br><small>${how(x)}</small>`) : impactBusy ? "…" : "—");
    if (!check && /\bdoors?\b/i.test(r.question) && res?.doors?.count_old != null)
      check = `<br><small>doors, not leaves: the Doors row above counts each leaf of a double door</small>`;
    return `<tr data-q="${i}"><td>${esc(r.question)}${r.err ? `<br><small>${esc(r.err)}</small>` : ""}</td><td>${val(r.a, "a")}</td><td>${val(r.b, "b")}</td><td>${d}${check}</td></tr>`;
  }

  // ---------------------------------------------------------------- the cost of a revision (captain 21:51)
  const priceOf = (c: Cost, key: string) => myPrices[key] ?? c.prices.find((p) => p.key === key)?.eur ?? 0;
  const heightOf = (c: Cost) => myPrices.__h ?? c.wall_height_m;
  const eurOf = (c: Cost, l: CostLine) => (l.price ? l.delta * (l.per_height ? heightOf(c) : 1) * priceOf(c, l.price) : 0);
  const convLines = (c: Cost) => {
    for (const l of c.lines)
      if (l.group === "skirting" && l.delta_net != null) l.delta = conv?.deductOpenings === false ? l.delta_whole ?? l.delta_net : l.delta_net;
    return c;
  };
  const totalOf = (c: Cost) => convLines(c).lines.reduce((s, l) => s + eurOf(c, l), 0);
  const qtyTxt = (c: Cost, l: CostLine) => {
    if (l.group === "doors") return l.delta ? `${l.delta > 0 ? "+" : "−"}${Math.abs(l.delta)} door set${Math.abs(l.delta) === 1 ? "" : "s"}` : "no change";
    const m1 = (v: number) => (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(1); // metres: 0.1 m is plenty
    if (l.per_height) return `${m1(l.delta)} m × ${fmt(heightOf(c))} m = ${m1(l.delta * heightOf(c))} m²`;
    if (l.unit === "m") return l.delta ? `${m1(l.delta)} m` : "no change";
    return l.delta ? `${signed(l.delta)} ${l.unit}` : "no change";
  };
  const priceTxt = (c: Cost, l: CostLine) => {
    if (!l.price) return "not priced";
    const p = c.prices.find((x) => x.key === l.price);
    return `× ${money(priceOf(c, l.price))}/${p?.unit === "door" ? "set" : p?.unit ?? ""}`;
  };
  /** Revision B's cost: one big number, one row per group (subtotal), its lines (click = marks on the drawing),
   *  editable example prices, and what we could not measure (kept out of the total). */
  function costSection(r: Result): string {
    const c = r.revision_cost;
    if (!c) return "";
    if (c.withheld)
      return `<div class="rc-sec">Cost of this revision</div><p class="rc-meta"><b>Not priced:</b> ${esc(c.withheld)}.</p>`;
    if (!c.lines.length && !c.review.length && c.skirting !== "pending")
      return `<div class="rc-sec">Cost of this revision</div><p class="rc-meta"><b>€0</b>: no quantity changed (floor, doors, walls, skirting).</p>`;
    const p: string[] = [`<div class="rc-sec">Cost of this revision</div><div class="rc-cost">`];
    const tot = totalOf(c); // (applies the Conventions tab's skirting rule to the lines first)
    const v = versions(r);
    p.push(`<div class="rc-total"><b class="${tot < 0 ? "neg" : ""}">${signedMoney(tot)}</b><small>${v.title ? `${esc(v.title)}: ` : ""}${esc(v.a)} → ${esc(v.b)} · <span class="ex" style="background:rgba(240,140,0,.14);color:#8a5200;border-radius:6px;padding:1px 7px;font-weight:600">example prices: use your own</span></small></div>`);
    const bits: string[] = [];
    for (const g of GROUPS) {
      const ls = c.lines.filter((l) => l.group === g.key && l.price);
      if (!ls.length) continue;
      const e = ls.reduce((s, l) => s + eurOf(c, l), 0);
      const q = g.key === "doors"
        ? [ls.filter((l) => l.delta > 0).reduce((s, l) => s + l.delta, 0), ls.filter((l) => l.delta < 0).reduce((s, l) => s - l.delta, 0)].map((v, i) => (v ? `${i ? "−" : "+"}${v}` : "")).filter(Boolean).join(" ") + " door sets"
        : `${g.key === "floor" ? "floor" : g.key === "walls" ? "walls" : "skirting"} ${signed(+ls.reduce((s, l) => s + l.delta, 0).toFixed(1))} ${g.key === "floor" ? "m²" : "m"}`;
      bits.push(`${q} (${signedMoney(e)})`);
    }
    if (bits.length) p.push(`<p class="rc-meta" style="margin:0">${esc(bits.join(" · "))}</p>`);
    if (r.same_sheet === false)
      p.push(`<p class="rc-warn" style="margin:8px 0 0">These two may not be versions of one sheet: much of the drawing differs. Check that before you use this cost.</p>`);
    p.push(`<table class="rc-ct">`);
    for (const g of GROUPS) {
      const ls = c.lines.filter((l) => l.group === g.key).sort((x, y) => Math.abs(eurOf(c, y)) - Math.abs(eurOf(c, x)) || (x.price ? 0 : 1) - (y.price ? 0 : 1));
      const pending = g.key === "skirting" && c.skirting === "pending";
      if (!ls.length && !pending) continue;
      const e = ls.reduce((s, l) => s + eurOf(c, l), 0);
      // two columns (the panel is ~400 px wide): what + how much on the left, euros on the right
      const note = g.key === "skirting" && conv
        ? `traced room outline${conv.deductOpenings === false ? " (door openings not deducted)" : " − openings"}; stairs, lifts: none; tiled wet rooms: ${r._costWet ? "included" : "none"} (as set in Conventions)`
        : r.model ? (MODEL_NOTE[g.key] ?? g.note) : g.note;
      p.push(`<tr class="g"><td>${esc(g.label)}<br><small>${esc(note)}</small></td><td class="n">${ls.length ? signedMoney(e) : ""}</td></tr>`);
      if (pending) {
        const n = r.areas?.rooms?.length ?? 0;
        p.push(`<tr><td colspan="2"><small>${r._costErr ? `Skirting could not be measured: ${esc(r._costErr)}` : `Measuring the skirting of ${n ? `${nn(n, "changed room")}` : "the changed rooms"} on both sheets (≈ 0.5 s a room)…`}</small></td></tr>`);
      }
      const show = costMore.has(g.key) ? ls : ls.slice(0, g.key === "floor" ? 6 : 4);
      for (const l of show) {
        const e1 = eurOf(c, l);
        const q = `<span class="${l.delta < 0 ? "neg" : l.delta > 0 ? "pos" : ""}">${esc(qtyTxt(c, l))}</span> <small>${esc(priceTxt(c, l))}</small>`;
        p.push(`<tr class="l${picked === `c:${l.id}` ? " on" : ""}" data-cl="${esc(l.id)}"><td><b>${esc(l.item)}</b> · ${q}${l.detail ? `<br><small>${esc(l.detail)}${l.group === "skirting" && conv?.deductOpenings === false ? " net · whole outline used" : ""}${l.group === "skirting" && l.wet ? " · wet room" : ""}${l.approx && l.group === "floor" ? " · read by OCR: check" : ""}</small>` : ""}</td><td class="n">${l.price ? signedMoney(e1) : "<small>€0</small>"}</td></tr>`);
      }
      if (ls.length > show.length) p.push(`<tr class="more" data-cmore="${g.key}"><td colspan="2">Show all ${ls.length} lines</td></tr>`);
    }
    p.push(`</table>`);
    if (c.review.length) {
      p.push(`<p class="rc-meta" style="margin:10px 0 4px"><b>Check by hand (not in the total)</b></p><ul class="rc-list">`);
      for (const v of c.review)
        p.push(`<li data-cv="${esc(v.id)}" class="${picked === `v:${v.id}` ? "on" : ""}"><span class="rc-n" style="background:#8e8e93">?</span><span><b>${esc(v.item)}</b><br><small>${esc(v.why)}</small></span></li>`);
      p.push(`</ul>`);
    }
    const custom = Object.keys(myPrices).length > 0;
    p.push(`<details class="rc-prices"${custom ? " open" : ""}><summary>Unit prices <span class="ex">example prices: use your own</span></summary><div class="rc-pgrid">`);
    for (const x of c.prices)
      p.push(`<span>${esc(x.label)}</span><label>€ <input type="number" min="0" step="1" inputmode="decimal" data-price="${esc(x.key)}" value="${priceOf(c, x.key)}"> / ${esc(x.unit === "door" ? "set" : x.unit)}</label>`);
    p.push(`<span>Wall height (partitions)</span><label><input type="number" min="0" step="0.1" inputmode="decimal" data-price="__h" value="${heightOf(c)}"> m</label>`);
    p.push(`</div>${custom ? `<button type="button" data-preset style="min-height:34px;border-radius:999px;border:0;padding:0 12px;background:var(--fill-2,rgba(0,0,0,.06));font:inherit;cursor:pointer;color:inherit">Back to the example prices</button>` : ""}</details>`);
    p.push(`<p class="rc-meta" style="margin:4px 0 6px">How: ${esc(c.rule)}. Your prices stay in this browser. No AI model calls.</p></div>`);
    return p.join("");
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
    const A = sheetCell(res.a.name, res.a.file), B = sheetCell(res.b.name, other.path);
    for (const d of res.areas?.by_name ?? [])
      rows.push([A, B, res.model ? "room area (model)" : "room area (printed)", "", d.name, d.old.join(" + "), d.new.join(" + "), d.delta, unitWord(d.unit), `${d.count_old} → ${nn(d.count_new, "room")}`, "", "", res.model ? "room areas in the two models" : "printed area tags on both sheets"]);
    // a model: every changed element, by its ids in both files (box columns = the GlobalIds)
    for (const e of res.model_changes?.elements ?? [])
      rows.push([A, B, `element ${e.change}`, e.guid_a ?? e.guid_b ?? "", `${e.kind}${e.name ? `: ${e.name}` : ""}`,
        (e.what ?? []).map((w) => `${w.what} ${w.old}`).join("; "), (e.what ?? []).map((w) => `${w.what} ${w.new}`).join("; "),
        e.moved_m ?? "", e.moved_m ? "m moved" : "", e.storey, e.guid_a ?? "", e.guid_b ?? "", "IFC elements matched by GlobalId"]);
    for (const r of impact)
      if (r.a || r.b)
        rows.push([A, B, "quantity (measured)", "", r.question, r.a?.value ?? "", r.b?.value ?? "",
          typeof r.a?.value === "number" && typeof r.b?.value === "number" ? +(r.b.value - r.a.value).toFixed(3) : "", r.a?.unit ?? r.b?.unit ?? "",
          `confidence ${r.a?.confidence ?? ""} / ${r.b?.confidence ?? ""}`, "", "", "pipeline on each version"]);
    const rc = res.revision_cost;
    if (rc) {
      convLines(rc);
      for (const l of rc.lines)
        rows.push([A, B, `cost: ${l.group}`, l.id, l.item, l.old ?? "", l.new ?? "", l.delta, l.unit, `${qtyTxt(rc, l)} ${priceTxt(rc, l)} = ${l.price ? signedMoney(eurOf(rc, l)) : "€0"}`, "", "", `${l.how ?? ""} (example prices unless you set your own)`]);
      rows.push([A, B, "cost: total", "", "Cost of this revision", "", "", Math.round(totalOf(rc)), "EUR", `${nn(rc.review.length, "item")} to check by hand, not in the total`, "", "", rc.rule]);
      for (const v of rc.review) rows.push([A, B, "cost: check by hand", v.id, v.item, "", "", "", "", v.why, "", "", "not priced"]);
    }
    res.text.forEach((t, i) => { if (t.kind !== "moved") rows.push([A, B, `text ${t.kind}`, `T${i}`, "", t.old ?? "", t.new ?? "", "", "", "", box(t.box_a), box(t.box_b), t.source ?? "printed text"]); });
    for (const r of res.regions)
      rows.push([A, B, `linework ${r.kind}`, r.id, r.description, "", "", "", "", r.near ?? "", box(r.box_a), box(r.box_b), res.approximate ? "pixel difference (approximate)" : "pixel difference"]);
    const csv = rows.map((r) => r.map(q).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const slug = (n: string) => n.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
    a.download = `changes_${slug(baseName(res.a.file))}_vs_${slug(baseName(other.path))}.csv`; // no spaces (QA 21:25: easier to mail / script)
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  /** The open sheet's printed name (POST /name), asked once per sheet; null until it answers (or on an older server). */
  const sheetNames = new Map<string, SheetName | null>();
  const nameTries = new Map<string, number>();
  function nameOf(path: string, page: number): SheetName | null {
    const k = `${path}#${page}`;
    if (!sheetNames.has(k)) {
      sheetNames.set(k, null);
      post("name", { path, page }).then((n) => {
        sheetNames.set(k, n as SheetName);
        // a picture is named from the text layer the upload warm-up reads (never a cold read): ask again while it is
        // still being read (up to 4 × 15 s)
        const t = nameTries.get(k) ?? 0;
        if ((n as SheetName).source === "file" && /\.(png|jpe?g|tiff?|webp)$/i.test(path) && t < 4) {
          nameTries.set(k, t + 1);
          setTimeout(() => { sheetNames.delete(k); refresh(); }, 15000);
        }
        refresh();
      }).catch(() => undefined);
    }
    return sheetNames.get(k) ?? null;
  }
  /** UX 01:29: a catalogue example sheet is called what its project card calls it ('Special school · Level 00'). */
  function catalogueName(path: string): string | null {
    try {
      for (const p of PROJECTS) for (const s of p.sheets) if (sameSheet(s.path, path)) return `${p.name} · ${s.label}`;
    } catch {
      /* no catalogue */
    }
    return null;
  }

  /** 'On screen: Special School Plan Level 00 · SEN-…-0201 · rev C01 · compared with rev C02' (UX NEW-11); the file
   *  names stay in the tooltip. Before the server has named them (or an older server): the file names. */
  function onScreenLine(path: string): string {
    const pg = api.page() ? ` · page ${api.page() + 1}` : "";
    const named = res && res.a.name && res.b.name && res.a.file === path ? res : null;
    if (!named) {
      // nothing compared yet (UX C-7): the sheet's own printed name from the server, the file name until it answers
      const cat = catalogueName(path);
      const n = cat ? null : nameOf(path, api.page());
      const tag = n ? [n.number, n.revision ? `rev ${n.revision}` : ""].filter(Boolean).join(" · ") : "";
      const what = cat ? `<b title="${esc(baseName(path))}">${esc(cat)}</b>` : n && n.source === "sheet" ? `<b title="${esc(n.file)}">${esc(n.title)}</b>${n.version ? ` · ${esc(n.version)}` : tag ? ` <small>(${esc(tag)})</small>` : ""}` : `<b>${esc(baseName(path))}</b>`;
      return `On screen: ${what}${pg}${other ? ` · compared with <b>${esc(baseName(other.path))}</b>` : ""}`;
    }
    const A = named.a.name!, B = named.b.name!, v = versions(named);
    const tip = (n: SheetName) => ` title="${esc(n.file)}"`;
    // the sheet number / revision as a small note when the tag does not already say it (an example's issue: no note)
    const extra = (n: SheetName, tag: string) => n.version ? "" :
      [n.number && !tag.includes(n.number) ? n.number : "", n.revision && !tag.includes(n.revision) ? `rev ${n.revision}` : ""].filter(Boolean).join(" · ");
    const note = (n: SheetName, tag: string) => (extra(n, tag) ? ` <small>(${esc(extra(n, tag))})</small>` : "");
    if (v.title)
      return `On screen: <b${tip(A)}>${esc(v.title)}</b>${pg} · <b>${esc(v.a)}</b>${note(A, v.a)} · compared with <b${tip(B)}>${esc(v.b)}</b>`;
    return `On screen: <b${tip(A)}>${esc(v.a)}</b>${pg}${note(A, v.a)} · compared with <b${tip(B)}>${esc(v.b)}</b>`;
  }

  /** Light model elements up in the 3D view (the model on screen = the old version; added elements are only in the
   *  other one). [] = lights off. */
  async function light(guids: string[]) {
    try {
      const v = (await api.viewer3d()) as { highlight?: (g: string[], frame?: boolean) => number } | null;
      v?.highlight?.(guids, guids.length > 0);
    } catch {
      /* no 3D viewer: the list still says what changed */
    }
  }
  const modelGuids = (r: Result, kind?: string) =>
    (r.model_changes?.elements ?? []).filter((e) => e.guid_a && (!kind || e.kind === kind)).map((e) => e.guid_a!) ;
  function modelSection(r: Result): string {
    const mc = r.model_changes!;
    const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
    const out: string[] = [`<div class="rc-sec">Elements that changed</div><ul class="rc-list">`];
    for (const d of mc.by_class.slice(0, 10)) {
      const bits = (["added", "removed", "moved", "changed"] as const).filter((k) => d[k]).map((k) => `${d[k]} ${k}`);
      const dd = d.new - d.old;
      out.push(`<li data-mcls="${esc(d.kind)}" title="Light them up in the 3D view"><b>${esc(cap(d.kind === "furniture" ? d.kind : `${d.kind}s`))}</b><span>${d.old} → ${d.new}${bits.length ? ` · ${bits.join(" · ")}` : ""}</span>${dd ? `<span class="rc-d ${dd < 0 ? "neg" : "pos"}">${dd > 0 ? "+" : "−"}${Math.abs(dd)}</span>` : ""}</li>`);
    }
    out.push(`</ul><ul class="rc-list">`);
    const say = (e: ModelEl) => (e.what ?? []).map((w) => w.what === "name" || w.what === "type" ? `${w.what} ${w.old} → ${w.new}` : `${w.what} ${fmt(Number(w.old))} → ${fmt(Number(w.new))} ${w.unit}`)
      .concat(e.moved_m ? [`moved ${fmt(e.moved_m)} m`] : []).join(" · ");
    for (const e of mc.elements.slice(0, 40)) {
      const where = [e.name, e.storey].filter(Boolean).join(" · ");
      out.push(`<li ${e.guid_a ? `data-guid="${esc(e.guid_a)}" title="Show it in the 3D view"` : `title="Only in the other model"`}><b>${esc(cap(e.kind))} ${esc(e.change)}</b><span>${esc(where)}${say(e) ? ` · ${esc(say(e))}` : ""}</span></li>`);
    }
    out.push(`</ul>`);
    if (mc.n_elements > 40) out.push(`<p class="rc-meta">${mc.n_elements - 40} more in the exported list.</p>`);
    out.push(`<p class="rc-meta">${mc.matched} of ${Math.min(mc.count_a, mc.count_b)} elements found in both models. The model on screen is the one you opened: its changed and removed elements light up in 3D; added ones exist only in the other file.</p>`);
    return out.join("");
  }

  function render(el: HTMLElement) {
    const focusKey = (document.activeElement as HTMLElement | null)?.dataset?.price ?? null; // a price being typed
    const a = api.file();
    const parts: string[] = [];
    if (!a) {
      el.innerHTML = `<p class="rc-busy">${busy ? esc(busy) : "Open a drawing first, then compare it with another version of the same sheet."}</p>${error ? `<p class="rc-warn">${esc(error)}${error === NET_MSG && retry ? ` <button type="button" data-retry class="rc-retry">Try again</button>` : ""}</p>` : ""}${busy ? "" : `${demoThumb()}<div class="rc-btns"><button data-demo>Try a real revision (school first floor)</button><button data-demoset>A whole issue (2 sheets)</button></div>`}`;
      el.querySelectorAll("[data-demo]").forEach((x) => x.addEventListener("click", () => void runDemo("dfe630")));
      el.querySelector("[data-retry]")?.addEventListener("click", () => { error = ""; retry?.(); });
      el.querySelector("[data-demoset]")?.addEventListener("click", () => void runDemo("dfe630set"));
      return;
    }
    parts.push(`<p class="rc-meta">${onScreenLine(a.path)}</p>`);
    const isModel = /\.ifc$/i.test(a.path);
    parts.push(`<label class="rc-drop" data-drop><span>${other ? "Compare with another file" : isModel ? "Drop the other version of this model here (IFC: an older or newer export of the same building)" : "Drop the other version of this sheet here (PDF, PNG, JPG or a CAD file, DXF: an older issue, a revised print, a scan or a photo)"}</span><span class="rc-pick" aria-hidden="true">Choose a file…</span><input type="file" accept="${isModel ? ".ifc" : ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.dxf"}" ${isModel ? "" : "multiple "}data-file></label>`);
    if (res && !busy) {
      // Undo / Clear / Esc for every mark tool (captain 22:57): back to all marks, hide them, or forget the comparison
      const esc1 = picked ? "Esc: all marks" : hasMarks(res) ? "Esc: close (marks hidden)" : "Esc: close";
      parts.push(`<div class="rc-tools">${picked ? `<button type="button" data-unpick>Show all marks</button>` : ""}${res.alignment.ok && hasMarks(res) ? `<button type="button" data-marks aria-pressed="${marksOff}">${marksOff ? "Show marks" : "Hide marks"}</button>` : ""}<button type="button" data-clear title="Forget this comparison; drop another version">Clear</button><small>${esc1}</small></div>`);
    }
    if (!other && !busy && !res && !sets) parts.push(`<p class="rc-meta">A whole new issue? Drop all its sheets (several files or one multi-page PDF): each is paired with its sheet here and compared.</p>`);
    if (!other && !busy && !res) parts.push(`<p class="rc-meta">No other version at hand? <a href="#" data-demo>Try a real revision</a> (opens an example in place of this drawing): a primary school's first floor, issued twice (classrooms re-planned from 62 to 55 m²). Or <a href="#" data-demoset>a whole issue</a>: ground + first floor, which sheet changed?</p>`+ demoThumb());
    if (busy) parts.push(`<p class="rc-busy">${esc(busy)}</p>`);
    if (error) parts.push(`<p class="rc-warn">${esc(error)}${error === NET_MSG && retry ? ` <button type="button" data-retry class="rc-retry">Try again</button>` : ""}</p>`);
    if (demoCredit && res && other) parts.push(`<p class="rc-meta">Example: ${esc(demoCredit)}.</p>`);
    parts.push(setSection());
    if (res && sets) parts.push(`<div class="rc-sec">This sheet: ${esc(sheetName(rows.find((r) => r.res === res)?.a ?? { file: res.a.file, page: res.a.page ?? 0, n_pages: 1, title: "", sheet_no: null, kind: "" }))}</div>`);
    if (res) {
      const al = res.alignment;
      if (!al.ok) parts.push(`<p class="rc-warn">${esc(res.summary)}</p>`);
      else {
        if (res.other_sheet)
          parts.push(`<p class="rc-warn"><b>These look like two different drawings</b>, not two versions of one sheet: only ${Math.round(al.overlap * 100)} % of the lines match (another floor, another building?). Nothing is priced${hasMarks(res) ? `; ${marksOff ? "'Show marks' draws" : "the marks only show"} where the two drawings differ` : ""}.</p>`);
        else if (res.same_sheet === false) parts.push(`<p class="rc-warn">These two do not look like versions of the same sheet (another floor?). The marks below may be differences between two drawings, not edits.</p>`);
        if (res.other_sheet) {
          const h = headline(res);
          if (h.startsWith("What changed: ")) parts.push(`<p class="rc-sum">Where the two drawings differ: ${esc(h.slice(14))}</p>`);
        } else if (nothingChanged(res))
          parts.push(`<p class="rc-sum rc-same"><b>✓ Nothing changed:</b> the two are the same drawing${res.approximate ? " (as far as a scan or photo shows)" : ""}.</p>`);
        else parts.push(`<p class="rc-sum">${esc(res.model ? res.summary : headline(res))}</p>`);
        if (!res.model) parts.push(`<p class="rc-meta">${esc(res.summary.split(" Printed room areas:")[0].split(" Only ")[0])}</p>`);
        else parts.push(`<p class="rc-meta">Matched ${esc(res.alignment.how ?? "")}. Exact: the models' own elements and quantities, no drawing is read.</p>`);
        parts.push(costSection(res));
        const ar = res.areas;
        if (ar?.by_name?.length) {
          parts.push(`<div class="rc-sec">Room areas that changed (${res.model ? "from the two models" : "printed on both sheets"})</div>${ar.source ? `<p class="rc-meta">${esc(ar.source)}${ar.ocr_overruled ? ` · ${nn(ar.ocr_overruled, "reading")} set back: the printed number looks the same on both sheets` : ""}.</p>` : ""}<ul class="rc-list">`);
          for (const d of ar.by_name.slice(0, 12)) {
            const u = unitWord(d.unit);
            const same = d.count_old === d.count_new && new Set(d.old).size === 1 && new Set(d.new).size === 1;
            const txt = same ? `${fmt(d.old[0])} → ${fmt(d.new[0])} ${u}${d.count_old > 1 ? ` (×${d.count_old})` : ""}`
              : !d.count_old ? `new: ${d.new.map(fmt).join(" + ")} ${u}`
              : !d.count_new ? (ar.source ? `not found on the scan (removed, or not readable): ${d.old.map(fmt).join(" + ")} ${u}` : `gone: ${d.old.map(fmt).join(" + ")} ${u}`)
              : partial(d, u) ?? `${nn(d.count_old, "room")} ${fmt(d.sum_old)} → ${nn(d.count_new, "room")} ${fmt(d.sum_new)} ${u}`;
            parts.push(`<li data-room="${esc(d.name)}"><b>${esc(d.name)}</b><span>${esc(txt)}</span><span class="rc-d ${d.delta < 0 ? "neg" : "pos"}">${signed(d.delta)}</span></li>`);
          }
          parts.push(`</ul>`);
          if (ar.total?.delta) parts.push(`<p class="rc-meta">${res.model ? "All room areas" : "All printed room areas"}: ${fmt(ar.total.old)} → ${fmt(ar.total.new)} ${unitWord(ar.total.unit)} (${signed(ar.total.delta)})</p>`);
        }
        if (res.model && res.model_changes) parts.push(modelSection(res));
        const dr = res.doors;
        if (dr && dr.count_old != null) {
          const ch = dr.added.length || dr.removed.length || dr.moved.length;
          parts.push(`<div class="rc-sec">Doors</div><ul class="rc-list"><li data-doors><b>Door leaves (swing arcs)</b><span>${dr.count_old} → ${dr.count_new}${ch ? ` · ${dr.added.length} added · ${dr.removed.length} removed · ${dr.moved.length} moved` : " · none moved"}</span>${dr.count_new !== dr.count_old ? `<span class="rc-d ${dr.count_new < dr.count_old ? "neg" : "pos"}">${signed(dr.count_new - dr.count_old)}</span>` : ""}</li></ul>`);
          parts.push(`<p class="rc-meta">Counted from the swing arcs drawn on both sheets (no model). A double door has 2 leaves, so this is more than the number of doors; 'moved' = the same door drawn at a new place.</p>`);
        }
        if (res.areas_pending && !ar?.by_name?.length) parts.push(`<p class="rc-busy">Reading the room labels on the scan… the room areas follow here (usually within a minute).</p>`);
        if (res.page_b_note) parts.push(`<p class="rc-meta">${esc(res.page_b_note)}</p>`);
        if (res.scale?.ratio) parts.push(`<p class="rc-meta">Sizes in metres at ${esc(res.scale.ratio)} (${esc(res.scale.source)}).</p>`);
        // plain words (QA 21:25: 'features+ecc', '×1.0577' was jargon); the server says how (older servers: built here)
        const how = al.how ?? (al.method.startsWith("text") ? `by the words printed on both sheets`
          : `by matching the drawing's shapes${Math.abs(al.rotation_deg) >= 0.1 ? `: the other version was turned ${Math.abs(al.rotation_deg).toFixed(1)}°` : ""}`);
        if (res.model) parts.push(`<p class="rc-meta">Cost: ${esc(res.cost?.tier ?? "cheap")} (no AI model calls, ${fmt(res.wall_seconds ?? 0)} s): the two models' own elements.</p>`);
        else parts.push(`<p class="rc-meta">Lined up ${esc(how)} · ${Math.round(al.overlap * 100)}% of the lines match · Cost: ${esc(res.cost?.tier ?? "cheap")} (no AI model calls${res.cost?.ocr_crops ? `; one OCR read of ${nn(res.cost.ocr_crops, "label crop")}` : ""}${res.wall_seconds ? `, ${res.wall_seconds} s` : ""})${res.approximate && hasMarks(res) ? " · scan / photo: marks are approximate (dashed)" : ""}</p>`);
        if (res.images?.b_aligned)
          parts.push(`<label class="rc-fade">Fade to the other version <input type="range" min="0" max="100" step="5" value="${fade}" data-fade> <span data-fadev>${fade}%</span></label>`);
        if (hasMarks(res)) parts.push(`<div class="rc-legend"><span><i style="background:${COL.removed}"></i>only on this sheet (removed)</span><span><i style="background:${COL.added}"></i>only on the other (added)</span><span><i style="background:${COL.changed}"></i>changed</span></div>`);
        const txt = res.text.map((t, i) => ({ t, i })).filter(({ t }) => t.kind !== "moved");
        const moved = res.text.length - txt.length;
        if (txt.length) {
          parts.push(`<div class="rc-sec">Text that changed</div><ul class="rc-list">`);
          const order = { changed: 0, added: 1, removed: 2, moved: 3 };
          for (const { t, i } of txt.sort((x, y) => order[x.t.kind] - order[y.t.kind]).slice(0, 40)) {
            const s = t.kind === "changed" ? `‘${esc(t.old)}’ → ‘${esc(t.new)}’` : t.kind === "added" ? `‘${esc(t.new)}’ added` : `‘${esc(t.old)}’ removed`;
            parts.push(`<li data-t="${i}"${picked === `t${i}` ? ` class="on"` : ""}><span class="rc-n" style="background:${COL[t.kind]}">T</span><span>${s}${t.source ? ` <em class="rc-meta">(${esc(t.source)})</em>` : ""}</span></li>`);
          }
          parts.push(`</ul>`);
        }
        if (moved) parts.push(`<p class="rc-meta">${nn(moved, "printed label")} moved (same text, new place).</p>`);
        const tc = res.text_check;
        if (tc && tc.checked) parts.push(`<p class="rc-meta">Labels compared on the scan: ${tc.checked}${tc.ocr ? ` · ${tc.ocr.crops} that looked different were read by OCR (no AI model)` : " · all look the same"}${tc.note ? ` · ${esc(tc.note)}` : ""}</p>`);
        else if (tc?.note) parts.push(`<p class="rc-meta">${esc(tc.note)}</p>`);
        if (res.regions.length) {
          parts.push(`<div class="rc-sec">Linework</div><ul class="rc-list">`);
          for (const r of res.regions.slice(0, 40))
            parts.push(`<li data-r="${r.id}"${picked === `r${r.id}` ? ` class="on"` : ""}><span class="rc-n" style="background:${COL[r.kind]}">${r.id}</span><span>${esc(r.description)}</span></li>`);
          parts.push(`</ul>`);
        }
        parts.push(`<div class="rc-btns">`);
        if (res.suggested_questions.length) parts.push(`<button class="primary" data-impact ${impactBusy ? "disabled" : ""}>${impactBusy ? "Asking both versions…" : "Check the quantities on both"}</button>`);
        if (res.images?.b_aligned) parts.push(`<a href="/api${esc(res.images.b_aligned)}" target="_blank" rel="noopener">Other version, lined up</a>`);
        if (res.images?.overlay) parts.push(`<a href="/api${esc(res.images.overlay)}" target="_blank" rel="noopener">Overlay image</a>`);
        parts.push(`<button data-csv>Export change list</button>`);
        parts.push(`</div>`);
        if (impact.length) {
          // the versions' short tags ('issue A', 'rev C01') when the two print the same title, else this / other
          const v = versions(res), short = (t: string) => t.replace(/\s*\(.*\)$/, "");
          const fits = v.title && v.a !== "on screen" && short(v.a).length <= 16 && short(v.b).length <= 16; // a long file name: no
          const [ha, hb] = fits ? [short(v.a), short(v.b)] : ["This sheet", "Other"];
          parts.push(`<table class="rc-imp"><tr><th>Question</th><th>${esc(ha)}</th><th>${esc(hb)}</th><th>Change</th></tr>${impact.map(impactRow).join("")}</table>`);
          parts.push(`<p class="rc-meta">Each number is the normal pipeline's answer on that version; click a row to see this sheet's answer with its evidence.</p>`);
        }
      }
    }
    // keep the reading position: a re-render (a pick, the skirting arriving) must not jump the panel to the top
    let scroller: HTMLElement | null = null;
    for (let n: HTMLElement | null = el; n && !scroller; n = n.parentElement) {
      const o = getComputedStyle(n).overflowY;
      if ((o === "auto" || o === "scroll") && n.scrollHeight > n.clientHeight) scroller = n;
    }
    const top = scroller?.scrollTop ?? 0;
    el.innerHTML = parts.join("");
    if (scroller) scroller.scrollTop = top;
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
        error = msgOf(e);
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
    el.querySelectorAll("[data-demo]").forEach((x) => x.addEventListener("click", (e) => {
      e.preventDefault();
      void runDemo("dfe630");
    }));
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
    el.querySelector("[data-retry]")?.addEventListener("click", () => { error = ""; retry?.(); });
    el.querySelector("[data-unpick]")?.addEventListener("click", () => { picked = null; draw(); render(el); });
    el.querySelector("[data-marks]")?.addEventListener("click", () => { marksOff = !marksOff; draw(); render(el); });
    el.querySelector("[data-clear]")?.addEventListener("click", () => clearAll());
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
        const key = li.dataset.r ? `r${li.dataset.r}` : li.dataset.t ? `t${li.dataset.t}` : "doors";
        picked = picked === key && !marksOff ? null : key; // a second click on the same row = back to all marks
        marksOff = false;
        draw();
        render(el);
      }),
    );
    el.querySelectorAll<HTMLElement>("[data-cl],[data-cv]").forEach((li) =>
      li.addEventListener("click", () => {
        const key = li.dataset.cl ? `c:${li.dataset.cl}` : `v:${li.dataset.cv}`;
        const again = picked === key && !marksOff;
        picked = again ? null : key; // a second click on the same line = back to all marks
        marksOff = false;
        draw();
        render(el);
        if (again) {
          if (res?.model) void light(modelGuids(res));
          return;
        }
        // bring the line's marks into view: their union, sheet A's frame
        const rc = res?.revision_cost;
        const cl = rc?.lines.find((l) => `c:${l.id}` === picked) ?? rc?.review.find((l) => `v:${l.id}` === picked);
        if (res?.model) { // a model's line: its elements light up in 3D (a room line: that room)
          const g = (cl as CostLine | undefined)?.guids ?? [];
          const room = cl && cl.id.startsWith("floor:") ? (res.model_changes?.elements ?? []).filter((e) => e.cls === "IfcSpace" && e.guid_a && cl.id.slice(6).startsWith(e.name)).map((e) => e.guid_a!) : [];
          void light([...g, ...room].filter(Boolean));
          return;
        }
        const bs: number[][] = (cl?.marks ?? []).map((m) => m.box);
        for (const o of (cl && "outlines" in cl ? cl.outlines : undefined) ?? [])
          bs.push([Math.min(...o.polygon.map((q) => q[0])), Math.min(...o.polygon.map((q) => q[1])), Math.max(...o.polygon.map((q) => q[0])), Math.max(...o.polygon.map((q) => q[1]))]);
        if (bs.length && res?.a.space) {
          // the marks' union, padded and never smaller than ~1/8 of the sheet (a room tag alone zooms in far too much)
          let [x0, y0, x1, y1] = [Math.min(...bs.map((b) => b[0])), Math.min(...bs.map((b) => b[1])), Math.max(...bs.map((b) => b[2])), Math.max(...bs.map((b) => b[3]))];
          const want = 0.12 * Math.max(...res.a.size);
          const gx = Math.max(0.25 * (x1 - x0), (want - (x1 - x0)) / 2, 0), gy = Math.max(0.25 * (y1 - y0), (want - (y1 - y0)) / 2, 0);
          [x0, y0, x1, y1] = [x0 - gx, y0 - gy, x1 + gx, y1 + gy];
          api.focus([x0, y0, x1, y1], res.a.space as Space);
        }
      }),
    );
    // a model: a class row lights up its changed elements, an element row that one (the model on screen = the old one)
    el.querySelectorAll<HTMLElement>("li[data-mcls]").forEach((li) => li.addEventListener("click", () => {
      if (res?.model) void light(modelGuids(res, li.dataset.mcls));
    }));
    el.querySelectorAll<HTMLElement>("li[data-guid]").forEach((li) => li.addEventListener("click", () => void light([li.dataset.guid!])));
    el.querySelectorAll<HTMLElement>("[data-cmore]").forEach((tr) => tr.addEventListener("click", () => {
      costMore.add(tr.dataset.cmore!);
      render(el);
    }));
    el.querySelectorAll<HTMLInputElement>("input[data-price]").forEach((inp) => {
      inp.addEventListener("change", () => {
        const v = Number(inp.value);
        if (Number.isFinite(v) && v >= 0) myPrices[inp.dataset.price!] = v;
        savePrices(myPrices);
        render(el);
      });
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
    });
    el.querySelector("[data-preset]")?.addEventListener("click", () => {
      myPrices = {};
      savePrices(myPrices);
      render(el);
    });
    if (focusKey) el.querySelector<HTMLInputElement>(`input[data-price="${focusKey}"]`)?.focus();
    el.querySelectorAll<HTMLElement>("tr[data-q]").forEach((tr) =>
      tr.addEventListener("click", () => {
        const row = impact[Number(tr.dataset.q)];
        if (row?.a) void api.askWith(row.question, { page: res?.a.page ?? 0 }).catch(() => undefined);
      }),
    );
  }

  // closing the panel (its ×, Esc, another tool's panel) takes the marks off the drawing; the toolbar brings both back
  const panel = api.addPanel({ id: "revcompare", title: "Compare revisions", render, onClose: () => { marksOff = true; draw(); if (res?.model) void light([]); } });
  api.addToolbarButton({
    id: "revcompare", label: "Compare revision", icon: "⇄", title: "Compare this sheet with another version of it",
    onClick: () => {
      if (panel.isOpen()) return panel.close();
      marksOff = false;
      panel.open();
      draw();
      if (res?.model && res.alignment.ok) void light(modelGuids(res));
    },
    when: () => !!api.file() && /\.(pdf|png|jpe?g|tiff?|webp|ifc)$/i.test(api.file()!.path),
  });
  api.onDrawing(() => draw());
  api.addContextMenuItem({
    id: "revcompare-here", order: 5,
    label: (ctx) => {
      const k = markAt(ctx);
      return k?.startsWith("r") ? `What changed here (#${k.slice(1)})` : "What changed here (label)";
    },
    when: (ctx) => markAt(ctx) != null,
    run: (ctx) => {
      const k = markAt(ctx);
      if (k) pickMark(k);
    },
  });
  // F4's Conventions tab: its skirting rule applies here too (wet rooms: traced again on the server; openings: here)
  const followConv = (d: unknown) => {
    const c = (d as { conventions?: { wetSkirting?: boolean; deductOpenings?: boolean } } | null)?.conventions;
    if (!c) return;
    conv = { wetSkirting: c.wetSkirting, deductOpenings: c.deductOpenings };
    if (res?.revision_cost) void followCost(res);
    refresh();
  };
  followConv((window as unknown as { stoaConventions?: unknown }).stoaConventions);
  window.addEventListener("stoa:conventions", (ev) => followConv((ev as CustomEvent).detail));
  // Esc: a picked line → all marks again; else close the panel (marks off). One step per Esc: window capture runs
  // before the shell's document-level Esc (which hides the answer's marks), and a handled Esc is marked done, so the
  // answer's marks go on the NEXT Esc. An open menu / overlay / a right-click selection keeps its own Esc.
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || !panel.isOpen()) return;
    const t = e.target as HTMLElement | null;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (document.querySelector('.feat-menu, .feat-report, .feat-region-capture, .feat-region-pop, dialog[open], [role="dialog"]:not([hidden]), .measure.on, #v2d.has-selection')) return;
    e.preventDefault();
    if (picked) {
      picked = null;
      draw();
      refresh();
    } else panel.close();
  }, true);
  // captain 01:18 'Start over' (window 'stoa:reset', W5): back to how the drawing first opened: no comparison, no
  // marks, no notice; the shell closes the open panel itself. The uploads and the server's cache stay (a re-drop of the
  // same file is instant).
  window.addEventListener("stoa:reset", () => {
    toast(null);
    clearAll();
    if (panel.isOpen()) panel.close();
    marksOff = true;
    draw();
  });
  // the viewer's eraser / the marks chip's 'Hide' / Measure's 'Clear all' (window 'stoa:clear-marks', no detail or
  // scope 'all'): every mark on the plan goes, ours too. A view action: the comparison stays, and the panel's
  // 'Show marks' (or a click on a line) brings them back. Measure's 'Clear selection' is about its own shape only.
  window.addEventListener("stoa:clear-marks", (e) => {
    const scope = (e as CustomEvent<{ scope?: string } | null>).detail?.scope;
    if ((scope !== undefined && scope !== "all") || !res || marksOff) return;
    if (res.model) void light([]);
    marksOff = true;
    picked = null;
    draw();
    refresh();
  });
  const want = new URLSearchParams(location.search).get("revdemo");
  if (want) setTimeout(() => void runDemo(want), 300); // after the app's own start-up
});
