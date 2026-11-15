/**
 * Measure on the drawing (customer view): ONE tool (captain 21:42, F1): Area = click the corners (or click inside a room
 * to trace it: web/server/room_shape.py), Length = a polyline, Count = numbered markers, Calibrate = two clicks on a
 * known length + its value typed ('5400 mm'). Clicks snap to the drawing through F1's engine (stoa/features/region.py). Stoa's site engineers ask about areas, then lengths: when the drawing's scale is unknown
 * or doubtful, the visitor fixes it here in ten seconds instead of getting no answer.
 *
 * Scale = drawing units per metre (PDF points or image pixels). Sources, best first: set by the visitor (kept per file
 * in this browser), read from an answer (title block "1:200", a scale bar "33.2 px per m", the CAD file's mapping).
 */
import type { Answer, Evidence } from "./types";
import { recordAnswer } from "./takeoff";
import type { Viewer2D } from "./viewer2d";
import type { RoomShape } from "./api";
import { imperialScale, plainScale } from "./features/region/scalewords";

export { imperialScale, plainScale };

export interface Scale {
  unitsPerM: number;
  /** "1:200" / "1 m = 33 px" */
  label: string;
  /** "from the title block", "set by you" */
  source: string;
  user?: boolean;
  /** a scale set with two clicks: the clicked reference length in drawing units (its clicking error, T1 xr-19) */
  refUnits?: number;
}

const PT_PER_M_AT_1 = (72 / 25.4) * 1000; // PDF points per real metre at 1:1
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
export const fmtM = (m: number) => (m >= 100 ? m.toFixed(0) : m >= 10 ? m.toFixed(1) : m.toFixed(2));

export function scaleLabel(unitsPerM: number, space: "pdf_pt" | "image_px"): string {
  if (space === "pdf_pt") {
    const n = PT_PER_M_AT_1 / unitsPerM;
    const r = n > 20 ? Math.round(n / 5) * 5 : Math.round(n);
    return `1:${r}`;
  }
  return `1 m = ${unitsPerM >= 10 ? unitsPerM.toFixed(0) : unitsPerM.toFixed(1)} px`;
}

/** The scale an answer used, from its trace / notes / value ("scale 1:200 from the title block", "scale bar …: 33.20 px
 *  per m", a scale question's value "1:200"). PDF scales need the paper scale; images need pixels per metre. */
/** The recognise step's result: Answer.recognition (D31) or the first trace line 'recognised: {json}'. */
export function recognised(a: Answer): Record<string, unknown> | null {
  if (a.recognition && typeof a.recognition === "object") return a.recognition;
  const t = (a.trace ?? []).find((x) => x.startsWith("recognised: {"));
  if (!t) return null;
  try {
    return JSON.parse(t.slice("recognised: ".length)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** A scale that is only an estimate (door swings, the usual 1:100, a reader's guess), not printed or set by the visitor. */
export const scaleGuessed = (s: Scale | null | undefined) => !!s && !s.user && /estimated|assumed|unsure/.test(s.source);

/** The drawing's scale from its door swings (web/server/door_scale.py result), else the usual 1:100 of floor plans:
 *  a best estimate for a plan that prints none (captain 14:39). */
export function doorScaleOf(d: { ok: boolean; ratio?: number; doors?: number; leaf_m?: number } | null): Scale {
  if (d?.ok && d.ratio && d.ratio > 0) {
    return { unitsPerM: PT_PER_M_AT_1 / d.ratio, label: `1:${d.ratio}`, source: `estimated from ${d.doors} door swings, a door being about ${d.leaf_m ?? 0.85} m wide` };
  }
  return { unitsPerM: PT_PER_M_AT_1 / 100, label: "1:100", source: "assumed, the usual scale of floor plans" };
}

/** A scale PRINTED on a vector PDF page (W6's GET /api/m/pdfscale: E1's linework.sheet_scales, else recognition):
 *  {n: 200, text: '1:200'} or {n: 48, text: '1/4" = 1'-0"'} → a Scale; null when nothing is printed. */
export function printedScaleOf(p: { ok?: boolean; n?: number; text?: string } | null): Scale | null {
  const n = Number(p?.n ?? 0);
  if (!p?.ok || !(n >= 1 && n <= 50000)) return null;
  const t = String(p.text ?? "").trim();
  return { unitsPerM: PT_PER_M_AT_1 / n, label: `1:${Math.round(n * 100) / 100}`, source: t && !/^1\s*:\s*[\d.]+$/.test(t) ? `printed on the drawing (${t})` : "printed on the drawing" };
}

/** source of a scale the visitor set earlier and F7's corrections remembered (it goes when an answer no longer uses it) */
export const LEARNED = "set by you (remembered for this project)";

export function scaleFromAnswer(a: Answer, space: "pdf_pt" | "image_px"): Scale | null {
  const text = [...(a.trace ?? []), ...(a.assumptions ?? []), ...(a.evidence ?? []).map((e) => e.note ?? "")].join("\n");
  const px = text.match(/([\d.]+)\s*px per m\b/i);
  if (space === "image_px" && px && Number(px[1]) > 0) {
    const u = Number(px[1]);
    return { unitsPerM: u, label: scaleLabel(u, space), source: /scale bar/i.test(text) ? "from the scale bar on the drawing" : "read from the drawing" };
  }
  if (space !== "pdf_pt") return null;
  // P1's paper copy (stoa.formats.normalize.paper_copy): '1:200 @ A1' printed on an A3 page = 1:400 on this file. The
  // readers measured on the page enlarged to A1 (where 1:200 holds); the viewer and this tool show the uploaded page, so
  // its own 1:400 is the scale here (taking 1:200 made 'Measure yourself' 4x too small and a false door-swing warning).
  const onFile = text.match(/\bthe drawing is at 1\s*:\s*(\d{1,6}) on this file\b/i);
  if (onFile && Number(onFile[1]) >= 1) {
    const n = Number(onFile[1]);
    const why = text.match(/\('1:(\d{1,6}) @ (A\d)'\) but this PDF page is (A\d)/);
    return { unitsPerM: PT_PER_M_AT_1 / n, label: `1:${n}`, source: why ? `printed as 1:${why[1]} for ${why[2]} paper; this page is ${why[3]}` : "printed on the drawing (for another paper size)" };
  }
  // US / UK architectural scales: 1/4" = 1'-0" is 1:48, 1" = 40'-0" is 1:480 (Stoa's M-101A and AFCEC sheets). The 1:N
  // patterns below must not read '1/4"' as 1:4 (that made every measurement on a US sheet 12× too small, 16:55).
  const imp = imperialScale(typeof a.value === "string" && /^\s*[\d\s/.]+\s*(?:"|''|″)\s*=/.test(a.value) ? a.value : text);
  if (imp) return { unitsPerM: PT_PER_M_AT_1 / imp.n, label: `1:${imp.n}`, source: `printed on the drawing (${imp.text})` };
  const v = typeof a.value === "string" ? a.value.match(/^\s*1\s*[:/]\s*(\d{1,5})\s*$/) : null;
  const m = v ?? text.match(/\bscale:?\s*(?:of\s*|is\s*)?1\s*[:/]\s*(\d{1,5})\b(?!\s*(?:"|''|″|\/))/i) ?? text.match(/\b(?:Massstab|Maßstab|mittakaava|échelle|scala)\s*1\s*[:/]\s*(\d{1,5})\b/i);
  // plan_rooms / E1 when nothing is printed: 'no scale printed: 1:100 makes … (estimate)', 'scale: NOT printed … — 1:100
  // is an estimate', '… assumed 1:100'
  const est = m ? null : text.match(/\b(?:no scale printed|scale:?\s*NOT printed)[^\n|]*?\b1\s*:\s*(\d{1,5})\b/i) ?? text.match(/\bassumed 1\s*:\s*(\d{1,5})\b/i);
  // G1's recognise step (D31 Answer.recognition, else its 'recognised: {json}' trace line): a scale PRINTED on the sheet
  // that no reader used (line-only CAD sheets: every reader declined, the prior answered; X1 14:31)
  const rec = m || est ? null : recognised(a);
  const rs = rec && rec.scale_source === "printed" ? String(rec.scale ?? "").match(/^\s*1\s*:\s*(\d{1,5})\s*$/) : null;
  const k = m ?? est ?? rs;
  if (!k) return null;
  const n = Number(k[1]);
  if (!(n >= 1 && n <= 50000)) return null;
  // F7's corrections: 'scale: 1:200 set by you (the drawing prints no scale)' = the visitor's earlier correction
  const src = !est && /\bset by you\b/i.test(text) ? LEARNED : est ? "estimated (not printed on the drawing; unsure)" : rs ? "printed on the drawing" : /title block/i.test(text) ? "from the title block"
    : /printed (?:scale|on the sheet)|scale[^\n]{0,12}printed/i.test(text) ? "printed on the drawing"
    : v ? `read from the drawing${(a.confidence ?? 0) < 0.5 ? " (unsure)" : ""}` : "read from the drawing";
  return { unitsPerM: PT_PER_M_AT_1 / n, label: `1:${n}`, source: src };
}


/** A real length typed for 'Calibrate': '5400 mm', '5.4 m', '540 cm', '5,4', '18\' 6"', '72 in'. A bare number above 100
 *  is millimetres (dimension strings on metric drawings are in mm: nobody calibrates on a 5400 m line). null = unreadable. */
export function parseLength(t: string): { m: number; read: string } | null {
  let s = String(t ?? "").trim().toLowerCase().replace(/[′’]/g, "'").replace(/[″”]/g, '"');
  if (!s) return null;
  s = /^\d{1,3}(,\d{3})+(\.\d+)?\b/.test(s) ? s.replace(/,/g, "") : s.replace(",", ".");
  const ft = s.match(/^(\d+(?:\.\d+)?)\s*(?:'|ft|feet)\s*-?\s*(?:(\d+(?:\.\d+)?)\s*(?:"|in|inch(?:es)?)?)?$/);
  if (ft) {
    const inches = Number(ft[1]) * 12 + Number(ft[2] ?? 0);
    return inches > 0 ? { m: inches * 0.0254, read: `${ft[1]}' ${ft[2] ?? 0}"` } : null;
  }
  const inch = s.match(/^(\d+(?:\.\d+)?)\s*(?:"|in|inch(?:es)?)$/);
  if (inch) return Number(inch[1]) > 0 ? { m: Number(inch[1]) * 0.0254, read: `${inch[1]}"` } : null;
  const mt = s.match(/^(\d+(?:\.\d+)?)\s*(mm|cm|m|metres?|meters?)?$/);
  if (!mt) return null;
  const n = Number(mt[1]);
  if (!(n > 0)) return null;
  const unit = mt[2] ? (mt[2].startsWith("met") ? "m" : mt[2]) : n > 100 ? "mm" : "m";
  const m = unit === "mm" ? n / 1000 : unit === "cm" ? n / 100 : n;
  return { m, read: `${mt[1]} ${unit}` };
}

type Tool = "scale" | "length" | "area" | "count" | null;
type Shapes = (path: string, page: number, space: string, items: { i: number; bbox: number[]; others?: [number, number][]; scale_n?: number }[]) => Promise<{ shapes: RoomShape[]; space: string }>;
type P = [number, number];
/** the drawing's own scale as F1's engine read it (stoa/features/region.py scale_info) */
type EngScale = { label: string; source: string; agrees: boolean | null; check: string; units_per_m: number | null };
type Res = {
  mode: string; error?: string; text?: string; area_m2?: number; perimeter_m?: number; length_m?: number; skirting_m?: number | null;
  openings?: { a: P; b: P; m: number }[]; polygon?: P[]; polyline?: P[]; snapped?: boolean; scale?: EngScale; local?: boolean;
};

/** What a feature plug-in hands the measure tool (F1's region feature, on by default): the answer card, so a measurement
 *  can be shown as an answer (which also puts it in the takeoff). Without it, 'Save to takeoff' records the row only. */
let host: { showAnswer?: (a: Answer, q?: string) => Promise<void> } = {};
let current: Measure | null = null;
/** captain 23:47: the right-click selection's 'Edit outline' / 'Trace it yourself' on a room (F1's region plug-in calls
 *  this): see Measure.outlineRoom. */
type Kept = { tool: Tool; pts: P[]; segs: (P[] | null)[]; res: Res | null; done: boolean; name: string; fromSugg: boolean; what: string; savedQ: string[]; loose: boolean; free: boolean };

/** UX S-M8 — main.ts / a plug-in's onDrawing: a sheet (or another page of it) is on screen: keep the shape drawn on the
 *  sheet before, bring back the one kept for this sheet. See Measure.sheetShown. */
export function measureSheetShown(file: string, page: number) {
  current?.sheetShown(file, page);
}

export function measureRoomOutline(mode: "edit" | "trace", poly: [number, number][] | null, at: [number, number] | null, name = "") {
  return current ? current.outlineRoom(mode, poly, at, name) : Promise.resolve(false);
}
/** tell the viewer's right-click selection (W5) to let go: one selection at a time, Esc / Clear selection clear it too */
const deselect = () => {
  try {
    window.dispatchEvent(new CustomEvent("stoa:deselect"));
  } catch {
    /* nobody listening */
  }
};
export function setMeasureHost(h: { showAnswer?: (a: Answer, q?: string) => Promise<void> }) {
  host = h ?? {};
}

const STYLE = `
.measure.on { top: 64px; bottom: auto; left: auto; right: 12px; z-index: 8; }
body[data-view="expert"] .measure.on { bottom: auto; }
.measure .m-panel { width: min(400px, calc(100vw - 48px)); }
.measure .m-tools button { min-width: 0; padding: 0 6px; }
.measure .m-head .m-scale { cursor: help; }
.measure .m-scale .ok { color: #1b7f3b; font-weight: 600; }
.measure .m-scale .warn { color: #b26a00; font-weight: 600; }
.measure .m-acts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.measure .m-acts:empty { display: none; }
.measure .m-acts button { min-height: 36px; padding: 0 14px; border-radius: 999px; border: 0; background: var(--fill-2, #eef1f5); color: var(--ink); font-weight: 600; cursor: pointer; font-size: 13.5px; }
.measure .m-acts button.primary { background: var(--accent); color: var(--cfff-fg, #fff); }
.measure .m-out .m-sheet { display: block; color: var(--muted); font-size: 12px; }
.measure .m-ask input { width: 130px; }
@media (max-width: 600px) {
  /* QA 23:13: on a phone the panel covered the middle of the drawing: dock it compact at the bottom of the screen */
  .measure.on { position: fixed; left: 8px; right: 8px; bottom: 8px; top: auto !important; z-index: 60; }
  .measure.on .m-panel { width: auto; padding: 8px 10px; max-height: 42vh; overflow: auto; }
  .measure.on .m-hint { display: none; }
  .measure.on .m-tools { margin-top: 4px; }
  .measure.on .m-tools button { min-height: 36px; font-size: 13px; }
  .measure.on .m-ctl { margin-top: 6px; }
  .measure.on .m-out small, .measure.on .m-out .m-sheet { display: none; }
}
.v2d-overlay .m-line.m1-seg .m-dot-s, .v2d-overlay .m-line.m1-gap .m-dot-s { display: none; }
.v2d-overlay .m-line.m1-gap line { stroke: #ff9f0a; stroke-width: 4px; stroke-dasharray: 7 5; }
.v2d-overlay .m-line.m1-hover line { stroke-dasharray: 5 5; opacity: .8; }
.measure .m-ctl { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.measure .m-ctl:empty { display: none; }
.measure .m-head .m-all { font-size: 12.5px; margin-right: 2px; white-space: nowrap; }
.measure .m-ctl button { min-height: 32px; padding: 0 9px; border-radius: 999px; border: 1px solid var(--line, #d0d5dd); background: transparent; color: var(--ink); font-weight: 600; cursor: pointer; font-size: 12.5px; }
.measure .m-ctl button:disabled { opacity: .4; cursor: default; }
.measure .m-ctl button.armed { background: var(--accent); color: var(--cfff-fg, #fff); border-color: var(--accent); }
.v2d-overlay .m-poly.m1-sugg polygon { fill: rgba(255, 159, 10, .12); stroke: #ff9f0a; stroke-dasharray: 8 6; }
.v2d-overlay g.m1-mid .m-dot-s { fill: #fff; stroke: #5e5ce6; stroke-dasharray: 2 2; opacity: .85; }
.v2d-overlay g.m1-mid { pointer-events: all; cursor: copy; }
.v2d-overlay g.m1-h { pointer-events: all; cursor: move; }
.v2d-overlay g.m1-h.first .m-dot-s { fill: #5e5ce6; }
.v2d-overlay g.m1-n .m-dot-s { fill: #5e5ce6; stroke: #fff; }
.v2d-overlay g.m1-n text { font: 700 11px system-ui, sans-serif; fill: #fff; pointer-events: none; transform-box: fill-box; transform-origin: center; transform: scale(calc(1 / var(--z, 1))); }
`;

async function post(url: string, body: unknown): Promise<any> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
const shoelace = (ps: P[]) => Math.abs(ps.reduce((s, p, i) => { const q = ps[(i + 1) % ps.length]; return s + p[0] * q[1] - q[0] * p[1]; }, 0)) / 2;
const pathLen = (ps: P[], closed: boolean) => ps.reduce((s, p, i) => (i + 1 < ps.length || closed ? s + Math.hypot(ps[(i + 1) % ps.length][0] - p[0], ps[(i + 1) % ps.length][1] - p[1]) : s), 0);
/** a measured value: 2 decimals below 100 (the captain's '7.03 m² · perimeter 10.96 m') */
const fmt2 = (m: number) => (m >= 100 ? m.toFixed(1) : m.toFixed(2));
/** imperial twins of a metric readout: 450 ft², 18'-0" */
const ft2 = (m2: number) => { const v = m2 * 10.76391; return v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1); };
const ftIn = (m: number) => {
  const tot = m / 0.0254;
  let ft = Math.floor(tot / 12);
  let inch = Math.round(tot - ft * 12);
  if (inch === 12) { ft += 1; inch = 0; }
  return `${ft}'-${inch}"`;
};
/** Ramer–Douglas–Peucker: a magnetic path's many points -> the few that keep its shape within eps (drawing units) */
const simplify = (ps: P[], eps: number): P[] => {
  if (ps.length <= 2) return ps;
  const [a, b] = [ps[0], ps[ps.length - 1]];
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1e-9;
  let k = 0, dmax = 0;
  for (let i = 1; i < ps.length - 1; i++) {
    const d = Math.abs((b[0] - a[0]) * (a[1] - ps[i][1]) - (a[0] - ps[i][0]) * (b[1] - a[1])) / L;
    if (d > dmax) { dmax = d; k = i; }
  }
  return dmax <= eps ? [a, b] : [...simplify(ps.slice(0, k + 1), eps).slice(0, -1), ...simplify(ps.slice(k), eps)];
};
/** a traced outline as corners: no repeated closing point (two handles on one spot), no zero-length sides */
const ring = (poly: [number, number][]): P[] => {
  const out: P[] = [];
  for (const q of poly) {
    const p: P = [q[0], q[1]];
    const prev = out[out.length - 1];
    if (!prev || Math.hypot(p[0] - prev[0], p[1] - prev[1]) > 1e-6) out.push(p);
  }
  while (out.length > 3 && Math.hypot(out[0][0] - out[out.length - 1][0], out[0][1] - out[out.length - 1][1]) <= 1e-6) out.pop();
  return out;
};
const bboxOf = (ps: P[]) => {
  const xs = ps.map((p) => p[0]), ys = ps.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};

/**
 * THE measure tool (captain 21:42; F1 cycle 3, W5 handed the file over 22:40–00:30). Three modes and a calibration:
 *  - AREA: click the room's corners; click the first corner again (or double-click) to close. Drag a corner to edit,
 *    Backspace removes the last point, Alt = no snap. A click inside a room, away from every line, traces that room
 *    (the answer card's 'click inside the room'). A fresh outline's sides move onto the wall faces.
 *  - LENGTH: a polyline; double-click or Enter finishes.
 *  - COUNT: numbered markers.
 *  - Calibrate: two clicks on a length you know, then type it ('5400 mm', '5.4 m', '18\' 6"'); F7 remembers it.
 * Every click snaps to the drawing (vector PDF line ends / corners / crossings, ink corners on images and scans) through
 * F1's engine (POST /api/feat/region/snap, /measure: area, perimeter, skirting = perimeter minus the door openings found
 * along it, the scale chip '1:200 · from the drawing ✓ bar agrees'). Without the engine (the API has no 'region'
 * feature) the tool still works: no snapping, the numbers computed here at the scale in force.
 */
export class Measure {
  private el: HTMLElement;
  private file = "";
  private auto: Scale | null = null;
  private user: Scale | null = null;
  private tool: Tool = null;
  private pts: P[] = [];
  private n = 0;
  private done = false;
  private hover: P | null = null;
  private res: Res | null = null;
  /** the engine's reading of the drawing's own scale (file + page it was read for) */
  private eng: EngScale | null = null;
  private engKey = "";
  private probeP: Promise<void> = Promise.resolve();
  /** the engine is reading the drawing's scale (a scan's first read takes 10-20 s: OCR of the title block, scale bar) */
  private probing = false;
  private probeAt = 0;
  private reprobe = 0;
  private stillReading = false;
  /** F1's engine on the API for this file: null = not tried yet, false = not there / cannot read it (the tool measures
   *  here, no snapping) */
  private engine: boolean | null = null;
  private seq = 0;
  private alt = false;
  private queue: Promise<void> = Promise.resolve();
  private skipClick = 0;
  private pending: Tool = null;
  private raf = 0;
  private emitted = "";
  /** a point of this shape was placed without snapping (Alt, nothing to snap to, no engine): show the clicking accuracy */
  private loose = false;
  /** a point of this shape was placed with Alt: the visitor wants it exactly there, so no side snap either (QA 23:13:
   *  on a scan an Alt outline's side was pulled onto the room's label, -20 %) */
  private free = false;
  /** magnetic lasso (captain 22:56): segs[i] = the path from pts[i] to pts[i+1] along the drawing's lines (null =
   *  straight: Alt-click, a vector PDF, no engine); hoverPath = the live path from the last point to the pointer */
  private magnetic = false;
  private segs: (P[] | null)[] = [];
  private hoverPath: P[] | null = null;
  private hoverSeq = 0;
  private hoverBusy = false;
  /** a traced outline offered, not used yet (captain 22:56: 'We traced it — use it / edit points / trace yourself') */
  private sugg: P[] | null = null;
  /** 'Suggest outline' pressed: the next click inside a room traces it as a suggestion */
  private armTrace = false;
  /** the questions of the answers this tool showed (Save to takeoff): this shape's, and all on this sheet */
  private savedQ: string[] = [];
  private allSavedQ: string[] = [];
  /** the room's printed name when the answer card traced a named room (the takeoff row's question) */
  private name = "";
  /** Count: what the visitor is counting ('doors'), for the takeoff row */
  private what = "";
  /** told when the visitor sets a scale (the hero's scale line follows) */
  onScale: (s: Scale | null) => void = () => {};
  /** A measurement finished (F7 20:38: 'use it as the answer?'): a traced room / a clicked outline (area, outline, its
   *  name when the card traced a named room) or a length; manual = the visitor clicked (false: the answer card traced it). */
  onMeasure: (m: { kind: "area" | "length"; value: number; unit: "m²" | "m"; poly?: [number, number][]; line?: [[number, number], [number, number]]; name?: string; manual: boolean; scale: string }) => void = () => {};

  /** a selected room's outline shown dashed while the visitor traces it themselves (not clickable, not measured) */
  private ref: P[] | null = null;
  /** the shape on screen came from a suggestion ('Use it' / 'Edit points' / a selected room's outline), not the clicks */
  private fromSugg = false;
  /** UX S-M8: a measurement is work. The shape on screen is kept per sheet (file#page) when the visitor opens another
   *  sheet or page, and comes back when that sheet is shown again (this page load only; Clear all / ✕ drop it). */
  private kept = new Map<string, Kept>();
  /** the sheet (file#page) the shape on screen was drawn on ('' = no shape) */
  private shapeKey = "";

  constructor(private v2d: Viewer2D, host: HTMLElement, private shapes: Shapes) {
    current = this;
    if (!document.getElementById("measure-f1-style")) {
      const st = document.createElement("style");
      st.id = "measure-f1-style";
      st.textContent = STYLE;
      document.head.append(st);
    }
    this.el = document.createElement("div");
    this.el.className = "measure";
    this.el.hidden = true;
    this.el.innerHTML = `
      <button type="button" class="m-open" title="Measure areas, lengths and counts on the drawing; check or set its scale">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17 17 3l4 4L7 21z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m7 13 2 2m1-5 2 2m1-5 2 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        <span>Measure</span></button>
      <div class="m-panel" hidden>
        <div class="m-head"><span class="m-scale"></span><button type="button" class="link m-all" aria-label="Clear all marks on this sheet" title="Remove every mark you made on this sheet (outlines, lines, markers, areas you drew) and the answers they made">Clear my marks</button><button type="button" class="m-x" aria-label="Close the measure tool" title="Close the measure tool (Esc twice). Results you saved to the takeoff stay.">✕</button></div>
        <div class="m-tools">
          <button type="button" data-tool="area" title="Click the corners of a room (or click inside it to trace it)">Area</button>
          <button type="button" data-tool="length" title="Click along a line; double-click or Enter to finish">Length</button>
          <button type="button" data-tool="count" title="Click each item to drop a numbered marker">Count</button>
          <button type="button" data-tool="scale" title="Click both ends of a length you know and type it (e.g. 5400 mm)">Calibrate</button>
        </div>
        <div class="m-ctl"></div>
        <div class="m-hint"></div>
        <form class="m-ask" hidden>
          <label>Real length <input type="text" inputmode="decimal" autocomplete="off" required placeholder="5400 mm or 5.4 m" /></label>
          <button type="submit">Set</button>
        </form>
        <div class="m-out"></div>
        <div class="m-acts"></div>
      </div>`;
    host.append(this.el);
    (window as unknown as { __measure: Measure }).__measure = this; // dev hook (QA scripts), like __v2d
    this.q(".m-open").addEventListener("click", () => this.open(true));
    this.q(".m-x").addEventListener("click", () => this.closeAsk());
    this.q(".m-all").addEventListener("click", () => this.clearAll());
    // UX 01:37 (b): a feature panel (Takeoff, trust map …) opened AFTER Measure sat under it: place again on every change
    try {
      const fp = document.getElementById("featPanels");
      if (fp) new MutationObserver(() => { if (!this.q(".m-panel").hidden) this.place(); }).observe(fp, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
    } catch {
      /* placed when the tool opens */
    }
    // another tool on the sheet takes over ('Ask about an area'): the measure panel closes
    window.addEventListener("stoa:tool", (e) => {
      if ((e as CustomEvent).detail?.name !== "measure" && !this.q(".m-panel").hidden) this.open(false);
    });
    // captain 01:18: W5's global 'Start over' (window 'stoa:reset'): the tool back to its first state — closed, no shape,
    // nothing kept per sheet, no answers of its own, the visitor's scales on this browser forgotten (the drawing's own
    // reading stays: it is the drawing's, not the visitor's)
    window.addEventListener("stoa:reset", () => {
      this.kept.clear();
      this.allSavedQ = [];
      this.user = null;
      this.auto = null;
      this.name = "";
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (k && k.startsWith("stoa.scale.")) localStorage.removeItem(k);
        }
      } catch {
        /* no storage */
      }
      this.open(false);
      this.render();
    });
    // the viewer's global 'Clear marks' (W5; no detail — our own events carry one): the shape on the sheet goes; the
    // answers already saved to the takeoff stay (that is 'Clear selection' / 'Clear all' here)
    window.addEventListener("stoa:clear-marks", (e) => {
      if ((e as CustomEvent).detail || !this.tool) return;
      this.use(this.tool);
      this.out("");
    });
    this.el.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) => b.addEventListener("click", () => {
      b.blur(); // keys (Enter, Backspace) go to the drawing's shape, not to this button
      this.use(b.dataset.tool as Tool);
    }));
    this.q<HTMLFormElement>(".m-ask").addEventListener("submit", (e) => {
      e.preventDefault();
      const t = this.q<HTMLInputElement>(".m-ask input").value;
      const L = parseLength(t);
      if (!L) {
        this.out(`Could not read “${esc(t)}”: type a length like <b>5400 mm</b>, <b>5.4 m</b> or <b>18' 6"</b>.`);
        return;
      }
      this.setUserScale(L.m, L.read);
    });
    // keep pan / zoom gestures on the panel from reaching the drawing
    for (const ev of ["pointerdown", "wheel", "dblclick"]) this.el.addEventListener(ev, (e) => e.stopPropagation());
    const inDrawing = (t: EventTarget | null) => t instanceof Node && this.v2d.el.contains(t) && !this.el.contains(t);
    document.addEventListener("pointerdown", (e) => {
      if (!inDrawing(e.target)) return;
      this.alt = e.altKey;
      if (e.pointerType === "touch" && this.tool) this.loupe(e.clientX, e.clientY);
    }, true);
    // touch: a small loupe above the finger shows what the point lands on (captain 21:42)
    document.addEventListener("pointermove", (e) => {
      if (this.lp && e.pointerType === "touch") this.loupe(e.clientX, e.clientY);
    }, true);
    for (const ev of ["pointerup", "pointercancel"]) document.addEventListener(ev, (e) => {
      if ((e as PointerEvent).pointerType === "touch") this.loupe(null);
    }, true);
    // UX S-M3 / 00:28: a right-click while a shape is being drawn (or count markers are down) belongs to the tool:
    // Close shape / Finish line · Undo point · Cancel — the viewer's selection menu (and its Esc) never sees it
    document.addEventListener("contextmenu", (e) => {
      if (!inDrawing(e.target) || this.el.hidden || this.q(".m-panel").hidden) return;
      // an open OR finished shape: this tool's own menu (the registry's copy of it reads the control row by its words)
      const busy = (this.tool === "area" || this.tool === "length" || this.tool === "count") && this.pts.length > 0;
      if (!busy) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      this.ctxMenu(e.clientX, e.clientY);
    }, true);
    // double-click closes an area / finishes a length (and must not reach the viewer's 'fit the page')
    document.addEventListener("dblclick", (e) => {
      if (!this.tool || this.tool === "scale" || this.tool === "count" || !inDrawing(e.target)) return;
      e.stopPropagation();
      e.preventDefault();
      const h = e.target instanceof Element ? e.target.closest<SVGGElement>("g.m1-h") : null;
      if (h && this.done && h.dataset.i) {
        this.dropPoint(Number(h.dataset.i)); // edit: delete a corner
        return;
      }
      this.queue = this.queue.then(() => this.finish());
    }, true);
    document.addEventListener("keydown", (e) => {
      if (!this.tool || this.el.hidden || (e.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))) return;
      if (e.key === "Escape") {
        if (this.pts.length || this.sugg || this.res || this.ref) this.clearSel();
        else {
          deselect();
          this.open(false);
        }
      }
      else if (e.key === "Enter" && (this.tool === "area" || this.tool === "length") && this.pts.length) {
        e.preventDefault(); // a still-focused tool button must not be pressed again (it would clear the shape)
        this.queue = this.queue.then(() => this.finish());
      }
      else if (e.key === "Backspace" && this.pts.length && this.tool !== "scale") {
        e.preventDefault();
        this.undoPoint();
      }
    });
    this.v2d.el.addEventListener("pointermove", (e) => {
      if (!(this.tool === "area" || this.tool === "length") || this.done || !this.pts.length) return;
      const p = this.v2d.clientToDrawing(e.clientX, e.clientY);
      if (!p) return;
      this.hover = p;
      if (this.magnetic && !(e as PointerEvent).altKey) void this.hoverLasso();
      else this.hoverPath = null;
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.draw(); });
    });
  }

  private q<T extends HTMLElement = HTMLElement>(sel: string) {
    return this.el.querySelector(sel) as T;
  }

  private lp: HTMLCanvasElement | null = null;
  /** The loupe: the drawing around the finger at 3x the screen scale, with a cross at the point, 100 px above it. */
  private loupe(cx: number | null, cy = 0) {
    if (cx === null) {
      this.lp?.remove();
      this.lp = null;
      return;
    }
    const node = this.v2d.el.querySelector<HTMLCanvasElement | HTMLImageElement>(".v2d-content");
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const W = node instanceof HTMLCanvasElement ? node.width : node.naturalWidth;
    if (!(rect.width > 0 && W > 0)) return;
    const S = 120, mag = 3;
    if (!this.lp) {
      this.lp = document.createElement("canvas");
      this.lp.width = S;
      this.lp.height = S;
      this.lp.className = "m-loupe";
      this.lp.style.cssText = `position:fixed;width:${S}px;height:${S}px;border-radius:50%;border:3px solid #5e5ce6;box-shadow:0 6px 20px rgba(0,0,0,.3);background:#fff;pointer-events:none;z-index:80;`;
      document.body.append(this.lp);
    }
    this.lp.style.left = `${Math.round(cx - S / 2)}px`;
    this.lp.style.top = `${Math.round(Math.max(4, cy - S - 40))}px`;
    const k = W / rect.width; // content px per screen px
    const x = (cx - rect.left) * k, y = (cy - rect.top) * k;
    const half = (S / 2 / mag) * k;
    const g = this.lp.getContext("2d")!;
    g.fillStyle = "#fff";
    g.fillRect(0, 0, S, S);
    try {
      g.drawImage(node, x - half, y - half, 2 * half, 2 * half, 0, 0, S, S);
    } catch {
      /* not drawable yet */
    }
    g.strokeStyle = "#5e5ce6";
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(S / 2, S / 2 - 12);
    g.lineTo(S / 2, S / 2 + 12);
    g.moveTo(S / 2 - 12, S / 2);
    g.lineTo(S / 2 + 12, S / 2);
    g.stroke();
  }

  private key() {
    return `${this.file}#${this.v2d.page}`;
  }

  private page() {
    return this.v2d.page || 0;
  }

  /** A new drawing: tools closed, the visitor's scale for this file (if they set one before) restored. */
  setFile(path: string) {
    this.keep();
    this.open(false);
    this.file = path;
    this.auto = null;
    this.user = null;
    this.eng = null;
    this.engKey = "";
    this.engine = null; // per file: one file the engine cannot read does not switch snapping off for the next
    this.reprobe = 0;
    this.stillReading = false;
    // magnetic is an option: plain corner clicks + side snap are already within 1 % on scans (36/36), and across a
    // doorway the magnetic path follows the door swing unless that piece is Alt-clicked straight
    this.magnetic = false;
    this.n = 0;
    this.el.hidden = !/\.(pdf|png|jpe?g)$/i.test(path);
    try {
      const s = JSON.parse(localStorage.getItem(`stoa.scale.${path}`) ?? "null");
      if (s && s.unitsPerM > 0) this.user = { ...s, user: true };
    } catch {
      /* no storage */
    }
    this.render();
  }

  setAutoScale(s: Scale | null) {
    if (!s) return;
    // a title block / scale bar / CAD scale beats a vision model's guess; never replace a better one with a worse one
    if (this.auto && !/unsure|assumed/.test(this.auto.source) && /unsure/.test(s.source)) return;
    // the door swings beat a reader's guess from typical room sizes (both are estimates; doors are drawn to scale)
    if (this.auto && /door swings/.test(this.auto.source) && /estimated|assumed|unsure/.test(s.source)) return;
    const same = this.auto?.label === s.label && this.auto?.source === s.source;
    this.auto = s;
    this.render();
    if (!same && !this.user) this.onScale(s);
    this.measureWaiting("measured once the drawing's scale was read");
  }

  /** QA 00:56 (c): a shape finished while no scale was known (a scan still being read) has no number: measure it as
   *  soon as a scale arrives (the engine's reading, the upload's pre-read, an answer's scale). */
  private measureWaiting(how: string) {
    if (this.done && this.pts.length && (this.tool === "area" || this.tool === "length") && (!this.res || this.res.error) && this.units() > 0)
      this.queue = this.queue.then(() => this.measure(!this.free, how));
  }

  /** An answer on this file that uses no scale: a remembered correction's scale is not the one in force any more (F7
   *  20:31: after Undo the card still said 'Scale 1:200 printed' for an answer measured at no scale). */
  dropLearned() {
    if (!this.auto || this.auto.source !== LEARNED) return;
    this.auto = null;
    this.render();
    if (!this.user) this.onScale(null);
  }

  get scale(): Scale | null {
    return this.user ?? this.auto;
  }

  /** the scale read from the drawing or an answer (not the visitor's) */
  get autoScale(): Scale | null {
    return this.auto;
  }

  /** A paper scale picked by the visitor ("Change" → 1:200) on a PDF drawing. */
  setRatio(n: number) {
    if (this.v2d.space !== "pdf_pt" || !(n > 0)) return;
    const u = PT_PER_M_AT_1 / n;
    this.user = { unitsPerM: u, label: `1:${n}`, source: "chosen by you", user: true };
    this.save(this.user);
    this.render();
    this.onScale(this.user);
    if (this.done) void this.measure(false);
  }

  /** Back to the drawing's own scale. */
  clearUser() {
    if (!this.user) return;
    this.user = null;
    this.save(null);
    this.render();
    this.onScale(this.auto);
    if (this.done) void this.measure(false);
  }

  /** Drawing units per metre in force for measuring: the visitor's, else the drawing's own (engine), else an answer's. */
  private units(): number {
    if (this.user) return this.user.unitsPerM;
    if (this.eng?.units_per_m && this.engKey === this.key()) return this.eng.units_per_m;
    return this.auto?.unitsPerM ?? 0;
  }

  /** the chip: 'Scale 1:200 · from the drawing ✓ bar agrees' */
  /** UX 01:37 (c): a picture's scale without a printed 1:N reads '1 m = 50.55 px': jargon outside Expert view, where the
   *  scale is named by where it came from (the px value stays in the tooltip) */
  private pxJargon(label: string | undefined): boolean {
    return !!label && /^1 m = [\d.,]+ px$/.test(label) && document.body.dataset.view !== "expert";
  }

  private chip(): string {
    if (this.user) {
      if (this.pxJargon(this.user.label)) {
        const rest = this.user.source.replace(/^set by you\s*/, "").trim();
        return `<span title="${esc(this.user.label)}">Scale <b>set by you</b>${rest ? ` <small>${esc(rest)}</small>` : ""}</span>`;
      }
      return `Scale <b>${esc(this.user.label)}</b> <small>${esc(this.user.source)}</small>`;
    }
    const e = this.engKey === this.key() ? this.eng : null;
    if (e?.units_per_m) {
      const chk = e.agrees ? ` <span class="ok">✓ bar agrees</span>` : e.agrees === false ? ` <span class="warn">⚠ bar disagrees</span>` : "";
      if (this.pxJargon(e.label)) return `<span title="${esc(e.label)}">Scale <b>from the drawing</b> <small>${/bar/i.test(e.source ?? "") ? "its scale bar" : "its printed scale"}</small>${chk}</span>`;
      return `Scale <b>${esc(e.label)}</b> <small>from the drawing</small>${chk}`;
    }
    if ((this.probing || this.stillReading) && this.engine !== false && (!this.auto || scaleGuessed(this.auto))) return `Scale <small>reading it on the drawing…</small>`;
    if (this.auto && this.pxJargon(this.auto.label)) {
      // a printed ratio behind a picture's px ('printed 1:200 on the sheet; the picture is 257 dpi') reads '1:200 printed'
      const w = plainScale(this.auto);
      if (/^1:|=/.test(w)) return `<span title="${esc(`${this.auto.label} · ${this.auto.source}`)}">Scale <b>${esc(w)}</b> <small>printed on the drawing</small></span>`;
      return `<span title="${esc(this.auto.label)}">Scale <b>${esc(this.auto.source)}</b></span>`;
    }
    if (this.auto) return `Scale <b>${esc(this.auto.label)}</b> <small>${esc(this.auto.source)}</small>`;
    return `Scale <b>not known</b> <small>calibrate: two clicks on a length you know</small>`;
  }

  /** the scale in use is an imperial one (a printed 1/8" = 1'-0", a length typed in feet): the readout adds ft² / ft-in */
  private imperial(): boolean {
    const e = this.engKey === this.key() ? this.eng : null;
    const t = [this.user?.label, this.user?.source, e?.label, e?.source, this.auto?.label, this.auto?.source].filter(Boolean).join(" ");
    return /\d\s*(?:"|″|'')\s*=\s*\d+\s*(?:'|′)|\b(?:feet|ft)\b|\d\s*'\s*-?\s*\d+\s*"/i.test(t);
  }

  private chipLabel(): string {
    if (this.user) return this.pxJargon(this.user.label) ? `the scale ${this.user.source}` : `${this.user.label} (${this.user.source})`;
    const e = this.engKey === this.key() ? this.eng : null;
    const chk = e?.agrees ? ", scale bar agrees" : e?.agrees === false ? ", scale bar disagrees" : "";
    if (e?.units_per_m) return this.pxJargon(e.label) ? `the drawing's own scale${chk ? ` (${chk.slice(2)})` : ""}` : `${e.label} (from the drawing${chk})`;
    if (this.auto) return this.pxJargon(this.auto.label) ? `the scale ${this.auto.source}` : `${this.auto.label} (${this.auto.source})`;
    return "no scale";
  }

  /** Ask the engine for the drawing's own scale (printed ratio + paper size, checked against a scale bar) once per sheet. */
  private probe(): Promise<void> {
    const k = this.key();
    // read again when nothing was found 20 s ago: an upload's text layer (title block, scale bar) may be ready by now
    if ((this.engKey === k && (this.eng || this.probing || Date.now() - this.probeAt < 20000)) || this.engine === false || !this.file) return this.probeP;
    this.probeAt = Date.now();
    this.engKey = k;
    this.eng = null;
    this.probing = true;
    this.render();
    const r = this.v2d.el.getBoundingClientRect();
    const c = this.v2d.clientToDrawing(r.left + r.width / 2, r.top + r.height / 2) ?? [100, 100];
    this.probeP = post("/api/feat/region/measure", { path: this.file, page: this.page(), space: this.v2d.space, points: [c, [c[0] + 10, c[1]]], mode: "length" })
      .then((res: Res) => {
        this.engine = true;
        if (this.engKey === k) this.eng = res?.scale?.units_per_m ? res.scale : null;
        // QA 23:13: a just-uploaded scan is still being read (title block, scale bar: 10-60 s): ask again, keep 'reading…'
        if (this.engKey === k && !this.eng && /not read yet|no text layer/i.test(res?.scale?.source ?? "") && this.reprobe < 12) {
          this.reprobe++;
          this.probeAt = 0;
          setTimeout(() => {
            if (this.engKey !== k || this.q(".m-panel").hidden) return;
            this.engKey = "";
            void this.probe().then(() => { if (this.tool === "scale" || !this.pts.length) this.firstTool(); });
          }, 5000);
          this.stillReading = true;
        } else if (this.engKey === k) this.stillReading = false;
        // QA 00:56 (c): a shape finished while the scale was still being read had no number: measure it now
        if (this.engKey === k && this.eng) {
          // a shape measured meanwhile at another scale (an answer's / the pre-read's): again at the drawing's own, so the
          // number and the chip agree
          const used = this.res?.scale?.units_per_m;
          if (!this.user && this.done && this.pts.length && used && this.eng.units_per_m && Math.abs(used / this.eng.units_per_m - 1) > 0.003)
            this.queue = this.queue.then(() => this.measure(false, "measured again at the drawing's own scale")); // its corners as they are
          else this.measureWaiting("measured once the drawing's scale was read");
        }
      })
      .catch(() => {
        this.engine = false;
      })
      .finally(() => {
        if (this.engKey === k) this.probing = false;
        this.render();
      });
    return this.probeP;
  }

  open(on: boolean, tool: Tool = null) {
    this.q(".m-panel").hidden = !on;
    this.q(".m-open").hidden = on;
    this.el.classList.toggle("on", on);
    if (!on) {
      this.use(null);
      this.v2d.measureClear();
      this.loupe(null);
      return;
    }
    this.place();
    try {
      window.dispatchEvent(new CustomEvent("stoa:tool", { detail: { name: "measure" } }));
    } catch {
      /* nobody listening */
    }
    // the answer card / right-click ask for a tool: theirs. The Measure button: Area when a usable scale is known, else
    // Calibrate first (captain 21:42), decided again once the engine has read the drawing's own scale.
    const p = this.probe();
    // while the engine reads the scale, start on Area (the decision follows the reading)
    this.use(tool ?? (this.usable() || this.probing ? "area" : "scale"));
    if (!tool) void p.then(() => this.firstTool());
  }

  private menu: HTMLDivElement | null = null;
  /** the tool's right-click menu (UX S-M3): CAD habits — right-click to finish */
  private ctxMenu(x: number, y: number) {
    this.menu?.remove();
    const m = document.createElement("div");
    m.className = "m1-menu";
    m.setAttribute("role", "menu");
    m.style.cssText = `position:fixed;left:${x + 4}px;top:${y + 4}px;z-index:90;background:var(--surface,#fff);color:var(--ink,#111);border:1px solid var(--line,#d0d5dd);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.18);padding:4px;min-width:170px;font:14px/1.3 system-ui,sans-serif;`;
    const n = this.pts.length;
    const items: [string, boolean, () => void][] = this.tool === "count"
      ? [["Undo last marker", n > 0, () => this.undoPoint()], ["Clear the markers", n > 0, () => this.clearSel()]]
      : this.done
        ? [[this.tool === "area" ? "New shape" : "New line", true, () => this.use(this.tool)],
          ["Undo last point", n > 0, () => this.undoPoint()],
          [this.tool === "area" ? "Discard this shape" : "Discard this line", true, () => this.clearSel()]]
        : [[this.tool === "area" ? "Close shape" : "Finish line", this.tool === "area" ? n >= 3 : n >= 2, () => { this.queue = this.queue.then(() => this.finish()); }],
          ["Undo point", n > 0, () => this.undoPoint()],
          [this.tool === "area" ? "Cancel shape" : "Cancel line", true, () => this.clearSel()]];
    for (const [label, on, fn] of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.textContent = label;
      b.disabled = !on;
      b.style.cssText = "display:block;width:100%;text-align:left;border:0;background:none;padding:8px 12px;border-radius:7px;font:inherit;color:inherit;cursor:pointer;";
      b.addEventListener("click", () => { close(); fn(); });
      m.append(b);
    }
    const close = () => {
      m.remove();
      if (this.menu === m) this.menu = null;
      document.removeEventListener("pointerdown", away, true);
      document.removeEventListener("keydown", esc, true);
    };
    const away = (ev: Event) => { if (!m.contains(ev.target as Node)) close(); };
    // Esc closes this menu only (the shape stays)
    const esc = (ev: KeyboardEvent) => { if (ev.key === "Escape") { ev.stopImmediatePropagation(); ev.preventDefault(); close(); } };
    document.addEventListener("pointerdown", away, true);
    document.addEventListener("keydown", esc, true);
    document.body.append(m);
    const r = m.getBoundingClientRect();
    if (r.right > window.innerWidth - 8) m.style.left = `${Math.max(8, x - r.width - 4)}px`;
    if (r.bottom > window.innerHeight - 8) m.style.top = `${Math.max(8, y - r.height - 4)}px`;
    this.menu = m;
  }

  /** ✕ (UX M3): a finished measurement that is not saved yet is not dropped silently: one line asks first. */
  private closeAsk() {
    const r = this.res;
    const counted = this.tool === "count" && this.pts.length > 0;
    const unsaved = ((this.done && !!r && (r.area_m2 != null || r.length_m != null)) || counted) && !this.savedQ.length;
    if (!unsaved || this.q(".m-out").querySelector(".m-drop")) {
      this.open(false);
      return;
    }
    const v = counted ? `count of ${this.pts.length}` : r!.area_m2 != null ? `${fmt2(r!.area_m2)} m²` : `${fmt2(r!.length_m!)} m`;
    const acts = this.q(".m-acts");
    acts.innerHTML = "";
    this.out(`<span class="m-drop">Your ${v} is not saved. Save it to the takeoff before closing?</span>`);
    const btn = (label: string, fn: () => void, primary = false) => {
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = label;
      if (primary) x.className = "primary";
      x.addEventListener("click", fn);
      acts.append(x);
    };
    btn("Save and close", () => void this.save2takeoff().then(() => this.open(false)), true);
    btn("Close without saving", () => this.open(false));
    btn("Keep measuring", () => this.readout());
  }

  /** Keep the open panel clear of the view switcher (#toolbar: one row on a desktop, two on a phone): below it when they
   *  would overlap. */
  private place() {
    this.el.style.top = "";
    this.el.style.right = "";
    if (window.matchMedia?.("(max-width: 600px)").matches) return; // docked at the bottom (CSS)
    // F6 23:13: an open feature panel (takeoff, trust map …) sits top-right too: Measure goes left of it (and above it,
    // z-index 8, if there is no room), so Calibrate's 'Set' is never under a panel
    try {
      const hostR = this.el.offsetParent?.getBoundingClientRect();
      const lefts = [...document.querySelectorAll<HTMLElement>("#featPanels .feat-panel")]
        .filter((p) => !p.hidden && p.offsetWidth > 0 && !p.closest("[hidden]"))
        .map((p) => p.getBoundingClientRect().left);
      const w = this.el.getBoundingClientRect().width;
      if (hostR && lefts.length) {
        const right = hostR.right - Math.min(...lefts) + 8;
        if (hostR.right - right - w >= hostR.left + 8) this.el.style.right = `${Math.round(right)}px`;
      }
    } catch {
      /* stays top-right */
    }
    const bar = document.getElementById("toolbar")?.getBoundingClientRect();
    const me = this.el.getBoundingClientRect();
    const host = this.el.offsetParent?.getBoundingClientRect();
    if (!bar || !host || !(bar.height > 0)) return;
    const overlap = me.left < bar.right && me.right > bar.left && me.top < bar.bottom && me.bottom > bar.top;
    if (overlap) this.el.style.top = `${Math.round(bar.bottom - host.top + 8)}px`;
  }

  /** a scale to measure with that is not a guess: the visitor's, the drawing's own (engine), or a printed / read one */
  private usable(): boolean {
    return !!this.user || !!(this.eng?.units_per_m && this.engKey === this.key()) || (!!this.auto && !scaleGuessed(this.auto));
  }

  private firstTool() {
    if (this.q(".m-panel").hidden || this.pts.length) return;
    if (this.stillReading && !this.usable()) {
      if (this.tool === "scale" && this.q(".m-ask").hidden) this.use("area");
      this.q(".m-hint").textContent = "Still reading the drawing's scale (a new scan takes up to a minute). You can start clicking; or Calibrate.";
      return;
    }
    if (this.usable()) {
      if (this.tool === "scale" && this.q(".m-ask").hidden) this.use("area");
      return;
    }
    if (this.tool !== "scale") {
      this.pending = this.tool;
      this.use("scale");
    }
    const s = this.scale;
    this.q(".m-hint").innerHTML = `${this.units() ? `This drawing prints no usable scale (we would use about <b>${esc(s?.label ?? "")}</b>, an estimate).` : "This drawing has no scale we can read."} <b>Calibrate first:</b> click both ends of a length you know (a dimension, a door ≈ 0.9 m), then type it.${this.units() ? ` <button type="button" class="link m-skip">Measure anyway</button>` : ""}`;
    this.q(".m-skip")?.addEventListener("click", () => this.use(this.pending ?? "area"));
  }

  use(tool: Tool) {
    this.tool = tool;
    this.shapeKey = "";
    this.pts = [];
    this.done = false;
    this.res = null;
    this.hover = null;
    this.emitted = "";
    this.loose = false;
    this.free = false;
    this.name = "";
    this.sugg = null;
    this.armTrace = false;
    this.savedQ = [];
    this.segs = [];
    this.hoverPath = null;
    this.ref = null;
    this.fromSugg = false;
    this.el.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
    this.q(".m-ask").hidden = true;
    this.q(".m-acts").innerHTML = "";
    this.out("");
    this.v2d.onDrawingClick = tool ? (p) => { this.queue = this.queue.then(() => this.click(p)).catch(() => {}); } : null;
    this.v2d.el.classList.toggle("measuring", !!tool);
    if (tool) this.v2d.measureClear();
    this.hint();
    this.ctl();
  }

  /** The always-visible controls (captain 22:56): Undo point · Cancel shape · Done · Magnetic · Suggest (+ 'Clear all my marks' in the head). */
  private ctl() {
    const box = this.q(".m-ctl");
    box.innerHTML = "";
    if (!(this.tool === "area" || this.tool === "length" || this.tool === "count")) return;
    const b = (label: string, title: string, on: boolean, fn: () => void, cls = "", act = "") => {
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = label;
      x.title = title;
      x.disabled = !on;
      if (cls) x.className = cls;
      if (act) x.dataset.act = act;
      x.addEventListener("click", () => { x.blur(); fn(); });
      box.append(x);
    };
    const n = this.pts.length;
    const open = (this.tool === "area" && !this.done && n >= 3) || (this.tool === "length" && !this.done && n >= 2);
    b("↶ Undo point", "Remove the last point (Backspace)", n > 0, () => this.undoPoint());
    // UX S-m3: say what each clear does ('Cancel shape' / 'Cancel line' / 'Clear markers'; data-act for menus that copy it)
    b(this.tool === "count" ? "Clear markers" : this.tool === "area" ? "Cancel shape" : "Cancel line", this.tool === "count" ? "Remove these markers and their count (Esc)" : "Remove this shape and its result, also from the takeoff (Esc)", n > 0 || !!this.sugg || !!this.res || this.savedQ.length > 0, () => this.clearSel(), "", "cancel");
    if (this.tool !== "count") b("✓ Done", this.tool === "area" ? "Close the outline (or click the first corner, or double-click)" : "Finish the line (or double-click, Enter)", open, () => { this.queue = this.queue.then(() => this.finish()); });
    if ((this.tool === "area" || this.tool === "length") && this.engine !== false) b(this.magnetic ? "🧲 Magnetic: on" : "🧲 Magnetic: off", this.tool === "area" ? "Magnetic lasso: the outline between your clicks follows the drawing's lines (pictures, scans). Alt-click = a straight piece (a doorway)" : "Magnetic: the line between your clicks follows the drawing's lines (a curved or kinked wall). Alt-click = a straight piece", !this.done, () => {
      this.magnetic = !this.magnetic;
      this.hoverPath = null;
      this.ctl();
    }, this.magnetic ? "armed" : "");
    if (this.tool === "area") b("✨ Suggest", "Click inside a room: we trace its outline for you to use, edit or ignore", !n && !this.sugg, () => {
      this.armTrace = !this.armTrace;
      this.q(".m-hint").textContent = this.armTrace ? "Click inside the room: we trace its outline as a suggestion." : "";
      if (!this.armTrace) this.hint();
      this.ctl();
    }, this.armTrace ? "armed" : "");
  }

  /** Backspace / 'Undo point': the last point goes (a finished shape opens again). */
  private undoPoint() {
    if (!this.pts.length) return;
    this.pts.pop();
    this.segs.length = Math.max(0, this.pts.length - 1);
    this.hoverPath = null;
    this.done = false;
    this.res = null;
    this.draw();
    this.readout();
  }

  /** A double-click on a corner of a finished shape deletes it (an area keeps 3, a line 2). */
  private dropPoint(i: number) {
    if (!(i >= 0 && i < this.pts.length) || this.pts.length <= (this.tool === "area" ? 3 : 2)) return;
    this.pts.splice(i, 1);
    this.queue = this.queue.then(() => this.measure(false, "as you edited it"));
  }

  /** a shape worth keeping: an outline / line with >= 2 points, or count markers */
  private hasWork() {
    return ((this.tool === "area" || this.tool === "length") && this.pts.length >= 2) || (this.tool === "count" && this.pts.length > 0);
  }

  /** the shape on screen -> kept for its sheet (UX S-M8) */
  private keep() {
    if (!this.hasWork() || !this.shapeKey) return;
    this.kept.set(this.shapeKey, { tool: this.tool, pts: [...this.pts], segs: [...this.segs], res: this.res, done: this.done, name: this.name, fromSugg: this.fromSugg, what: this.what, savedQ: [...this.savedQ], loose: this.loose, free: this.free });
  }

  /** A sheet is on screen (file, page): a shape drawn on another page goes into keeping; the one kept for this sheet comes
   *  back (the panel opens on its tool); the same sheet drawn again (a re-render) gets its shape redrawn. */
  sheetShown(file: string, page: number) {
    if (!file) return;
    // main.ts opens a file WITHOUT setFile when an answer's evidence continues on another sheet (openFile keep): the
    // tool follows the sheet on screen (the shape of the sheet left is kept for it, the scale is the new sheet's)
    if (file !== this.file) this.setFile(file);
    const k = `${file}#${page || 0}`;
    if (this.shapeKey && this.shapeKey !== k) {
      this.keep();
      if (this.tool) this.use(this.tool);
    } else if (this.shapeKey === k && this.pts.length) {
      this.draw();
      return;
    }
    const s = this.kept.get(k);
    if (!s || this.hasWork()) return;
    this.kept.delete(k);
    if (this.q(".m-panel").hidden) this.open(true, s.tool);
    else this.use(s.tool);
    this.pts = s.pts;
    this.segs = s.segs;
    this.res = s.res;
    this.done = s.done;
    this.name = s.name;
    this.fromSugg = s.fromSugg;
    this.what = s.what;
    this.savedQ = s.savedQ;
    this.loose = s.loose;
    this.free = s.free;
    this.shapeKey = k;
    this.draw();
    const show = () => {
      if (this.shapeKey !== k || this.pts !== s.pts) return; // changed since
      if (s.tool === "count" || s.done) this.readout(false, s.savedQ.length ? "" : "kept while you looked at another sheet (not saved yet)");
      else this.readout(true);
      this.ctl();
    };
    show();
    void this.probe().then(() => { this.render(); show(); }); // the sheet's scale is read again after a sheet change
  }

  /** 'Cancel shape' / Esc: this shape, its result and the answer it made (card, takeoff, a remembered correction) go. */
  clearSel() {
    deselect();
    const qs = this.savedQ;
    const tool = this.tool;
    this.use(tool);
    this.out("");
    if (qs.length) this.forget("selection", qs);
  }

  /** 'Clear all': every mark on this sheet — this tool's shapes, the calibration line, the areas drawn with 'Ask about an
   *  area' (F1 region listens) — and every answer this tool made here. */
  clearAll() {
    deselect();
    this.kept.delete(this.key());
    const qs = [...this.allSavedQ];
    this.allSavedQ = [];
    this.use(this.tool);
    this.v2d.measureClear();
    this.out("");
    this.forget("all", qs);
  }

  /** Tell the page what was cleared: window event 'stoa:clear-marks' {scope, file, page, questions}. The card and the
   *  takeoff (W5) drop those answers, F7 forgets a measured value it was told to remember, F1's region drops its box. */
  private forget(scope: "selection" | "all", questions: string[]) {
    this.allSavedQ = this.allSavedQ.filter((q) => !questions.includes(q));
    try {
      window.dispatchEvent(new CustomEvent("stoa:clear-marks", { detail: { scope, file: this.file, page: this.page(), questions } }));
    } catch {
      /* nobody listening */
    }
  }

  private hint() {
    const snap = this.engine === false ? " Snapping unavailable here: measured exactly as clicked." : " Snaps to the drawing; Alt = free.";
    const h = this.done && (this.tool === "area" || this.tool === "length") ? `Drag a ${this.tool === "area" ? "corner" : "point"} to adjust; ${this.tool === "area" ? "New shape" : "New line"} to start again.`
      : this.tool === "scale" ? "Calibrate: click both ends of a length you know (a dimension line, a door ≈ 0.9 m, the scale bar), then type its real length."
      : this.tool === "length" ? `Click along the line; double-click or Enter to finish.${snap}`
      : this.tool === "area" ? `Click the room's corners; click the first one again (or double-click) to close.${snap}`
      : this.tool === "count" ? "Click each item to drop a numbered marker. Click a marker to remove it; drag it to move it."
      : "";
    this.q(".m-hint").textContent = h;
  }

  private render() {
    this.q(".m-scale").innerHTML = this.chip();
    const e = this.engKey === this.key() ? this.eng : null;
    this.q(".m-scale").title = e?.units_per_m && !this.user ? `${e.source}${e.check ? ` · ${e.check}` : ""}` : "";
    if (this.tool !== "scale" || this.q(".m-ask").hidden) this.hint();
  }

  private save(s: Scale | null) {
    try {
      if (s) localStorage.setItem(`stoa.scale.${this.file}`, JSON.stringify(s));
      else localStorage.removeItem(`stoa.scale.${this.file}`);
    } catch {
      /* no storage */
    }
  }

  private setUserScale(meters: number, read = "") {
    if (!(meters > 0) || this.pts.length < 2) return;
    const [p, q] = this.pts;
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (d <= 0) return;
    let u = d / meters;
    // QA 23:13: two clicks are ~1 % precise; when they agree with the drawing's own scale within 1.5 %, the visitor has
    // confirmed it: keep the drawing's exact value (still 'set by you', so F7 remembers it for the set)
    const e0 = this.engKey === this.key() ? this.eng : null;
    const confirmed = !!e0?.units_per_m && Math.abs(u / e0.units_per_m - 1) <= 0.015;
    if (confirmed) u = e0!.units_per_m!;
    this.user = { unitsPerM: u, label: scaleLabel(u, this.v2d.space), source: confirmed ? "set by you (confirms the drawing's)" : "set by you", user: true, refUnits: d };
    this.save(this.user);
    this.v2d.measureClear();
    this.q(".m-ask").hidden = true;
    const next = this.pending ?? "area";
    this.pending = null;
    this.render();
    this.onScale(this.user);
    const e = this.engKey === this.key() ? this.eng : null;
    const vs = e?.units_per_m ? Math.abs(d / meters / e.units_per_m - 1) : NaN;
    this.use(next);
    this.v2d.measureLine(p, q, `${fmtM(meters)} m`, "ref");
    this.out(`Scale set: <b>${esc(this.user.label)}</b>${read ? ` <small>from ${esc(read)} between your two clicks</small>` : ""}${Number.isFinite(vs) ? `<small>${vs <= 0.015 ? `✓ agrees with the drawing's ${esc(e!.label)} (${(vs * 100).toFixed(1)} % apart): its exact value is used` : `the drawing says ${esc(e!.label)} (${Math.round(vs * 100)} % apart): yours is used`}</small>` : ""}`);
  }

  private out(html: string) {
    this.q(".m-out").innerHTML = html;
  }

  /** T1 expert rule xr-19: the measuring uncertainty from clicking accuracy, as one muted line. ~2 screen px per click
   *  (in drawing units at the current zoom): a two-click length ±2r/L, plus the same again for a scale set with two
   *  clicks; a traced room at such a scale ±2× the scale's error (area); a hand-clicked area ≈ ±3 %. '' when < 0.5 %. */
  private clickErr(kind: "length" | "area", lenUnits = 0): string {
    const s = this.scale;
    const a = this.v2d.clientToDrawing(0, 0), b = this.v2d.clientToDrawing(2, 0);
    const r = a && b ? Math.hypot(b[0] - a[0], b[1] - a[1]) : 0;
    if (!(r > 0)) return "";
    const scaleErr = s?.user && s.refUnits ? (2 * r) / s.refUnits : 0;
    const e = kind === "length" ? (lenUnits > 0 ? (2 * r) / lenUnits : 0) + scaleErr : 2 * scaleErr;
    if (!(e >= 0.005)) return "";
    const pct = e >= 0.1 ? Math.round(e * 100) : Math.round(e * 1000) / 10;
    return `<div class="m-err">≈ ±${pct} % (clicking accuracy${scaleErr ? ", the scale's two clicks included" : ""})</div>`;
  }

  /** As if the visitor clicked at p with the open tool (right-click 'Measure from here': the length's first end). */
  clickAt(p: [number, number]) {
    // a caller with a room point means 'trace this room' (F2's takeoff 'Measure', the card): offered as a suggestion
    if (this.tool === "area" && !this.pts.length && !this.done && !this.sugg) this.armTrace = true;
    this.queue = this.queue.then(() => this.click(p)).catch(() => {});
  }

  /** drawing units per screen px at the current zoom */
  private unitsPerPx(): number {
    const a = this.v2d.clientToDrawing(10, 10), b = this.v2d.clientToDrawing(20, 10);
    return a && b ? Math.hypot(b[0] - a[0], b[1] - a[1]) / 10 : 1;
  }

  /** the nearest line end / corner / crossing (vector PDF) or ink corner (image, scan) within 10 screen px */
  private async snap(p: P): Promise<{ point: P; kind: string }> {
    if (this.engine === false || !this.file) return { point: p, kind: "none" };
    try {
      // pictures / scans: at most 8 cm on the drawing (at low zoom 10 screen px reach across a wall to its outer corner)
      const u = this.units();
      const r = await post("/api/feat/region/snap", { path: this.file, page: this.page(), space: this.v2d.space, point: p, radius: 10 * this.unitsPerPx(), ...(u > 0 ? { raster_radius: 0.08 * u } : {}) });
      this.engine = true;
      return r?.point ? { point: [Number(r.point[0]), Number(r.point[1])], kind: String(r.kind ?? "none") } : { point: p, kind: "none" };
    } catch {
      this.engine = false;
      this.hint();
      return { point: p, kind: "none" };
    }
  }

  private async click(p: [number, number]) {
    if (!this.tool || performance.now() < this.skipClick) return;
    const alt = this.alt;
    if (this.tool === "scale") {
      if (this.pts.length >= 2) {
        this.pts = [];
        this.v2d.measureClear();
        this.q(".m-ask").hidden = true;
      }
      const s = alt ? p : (await this.snap(p)).point;
      if (this.tool !== "scale") return;
      this.pts.push(s);
      this.v2d.measureDot(s);
      if (this.pts.length === 2) {
        this.v2d.measureClear();
        this.v2d.measureLine(this.pts[0], this.pts[1], "?", "ref");
        const f = this.q<HTMLFormElement>(".m-ask");
        f.hidden = false;
        f.querySelector("input")!.value = "";
        f.querySelector("input")!.focus();
        this.q(".m-hint").textContent = "Type the real length between your two clicks: 5400 mm, 5.4 m, 18' 6\"…";
      }
      return;
    }
    if (this.tool === "count") {
      this.pts.push(p);
      this.draw();
      this.readout();
      return;
    }
    if (this.done) return; // finished: drag a corner to adjust, or Clear to start again
    const tool = this.tool;
    // 'Suggest outline' armed: this click (inside a room) asks for a traced outline — a suggestion, never forced
    if (tool === "area" && this.armTrace && !this.pts.length && !this.sugg) {
      this.armTrace = false;
      this.out("Tracing the room…");
      const sh = await this.trace(p);
      if (this.tool !== tool || this.pts.length) return;
      if (sh?.poly && sh.poly.length >= 3) this.suggest(ring(sh.poly), `traced ${sh.method === "wall lines" ? "from the wall lines (door swings closed at the doorway)" : "outline"}`);
      else {
        this.out("No closed room around that point (an opening without a door line). Click its corners instead.");
        this.ctl();
      }
      return;
    }
    // QA 00:05: a click a few px beside the first corner's dot (it is 10 px wide) closed nothing and added a 5th
    // corner: within 12 screen px of the first corner = close the outline
    if (tool === "area" && !this.done && !this.sugg && this.pts.length >= 3
        && Math.hypot(p[0] - this.pts[0][0], p[1] - this.pts[0][1]) <= 12 * this.unitsPerPx()) {
      await this.finish();
      return;
    }
    const sn = alt ? { point: p, kind: "none" } : await this.snap(p);
    if (this.tool !== tool || this.done) return;
    if (this.sugg) return; // a suggestion is on screen: use it, edit it or trace yourself first
    if (sn.kind === "none") this.loose = true;
    if (alt && !this.magnetic) this.free = true; // magnetic: Alt = a straight piece, still snapped
    const prev = this.pts[this.pts.length - 1];
    this.pts.push(sn.point);
    if (prev && (tool === "area" || tool === "length")) {
      const n = this.pts.length;
      this.segs.length = n - 2;
      this.segs.push(this.magnetic && !alt ? await this.lasso(prev, sn.point) : null);
      this.hoverPath = null;
    }
    this.draw();
    if (tool === "length" && this.pts.length >= 2) this.readout(true);
    else this.readout();
  }

  /** Dark ink on the rendered drawing within r screen px of p (drawing coordinates): the click is on a line, not inside
   *  a room. Reads the viewer's own canvas (PDF) or the image's pixels around the point; false when it cannot tell. */
  private nearInk(p: P, r = 6): boolean {
    try {
      const node = this.v2d.el.querySelector<HTMLCanvasElement | HTMLImageElement>(".v2d-content");
      if (!node) return false;
      const c = this.v2d.drawingToClient(p);
      const rect = node.getBoundingClientRect();
      const W = node instanceof HTMLCanvasElement ? node.width : node.naturalWidth;
      const H = node instanceof HTMLCanvasElement ? node.height : node.naturalHeight;
      if (!(rect.width > 0 && W > 0)) return false;
      const k = W / rect.width;
      const x = (c.x - rect.left) * k, y = (c.y - rect.top) * k;
      const rr = Math.max(1, Math.round(r * k));
      const x0 = Math.max(0, Math.round(x - rr)), y0 = Math.max(0, Math.round(y - rr));
      const w = Math.min(W, Math.round(x + rr) + 1) - x0, h = Math.min(H, Math.round(y + rr) + 1) - y0;
      if (!(w > 0 && h > 0)) return false;
      let data: Uint8ClampedArray;
      if (node instanceof HTMLCanvasElement) data = node.getContext("2d")!.getImageData(x0, y0, w, h).data;
      else {
        const cv = document.createElement("canvas");
        cv.width = w;
        cv.height = h;
        const g = cv.getContext("2d")!;
        g.drawImage(node, x0, y0, w, h, 0, 0, w, h);
        data = g.getImageData(0, 0, w, h).data;
      }
      let dark = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] > 128 && 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2] < 140) dark++;
      }
      return dark >= Math.max(2, Math.round(k * k));
    } catch {
      return false; // a tainted / missing canvas: cannot tell
    }
  }

  /** A traced outline as a suggestion: dashed on the plan, with 'Use it' / 'Edit points' / 'Trace yourself'. */
  private suggest(poly: P[], how: string, lead = "") {
    this.sugg = poly;
    this.pts = [];
    this.done = false;
    this.res = null;
    this.draw();
    const u = this.units();
    const a = u > 0 ? `<b>${fmt2(shoelace(poly) / (u * u))} m²</b> · perimeter ${fmt2(pathLen(poly, true) / u)} m` : "";
    this.out(`${lead || "We traced it"}${a ? `: ${a}` : ""} <small>${esc(how)}${u > 0 ? ` · at ${esc(this.chipLabel())}` : ""}. Use it, edit its points, or trace the room yourself.</small>`);
    const acts = this.q(".m-acts");
    acts.innerHTML = "";
    const take = (edit: boolean) => {
      if (!this.sugg) return;
      this.pts = this.sugg;
      this.sugg = null;
      this.done = true;
      this.fromSugg = true;
      // its sides move onto the wall faces like a clicked outline (DfE laundry: traced 6.66 -> 6.99 m², gold 7.0)
      this.queue = this.queue.then(() => this.measure(true, edit ? "our traced outline, edit its corners" : "our traced outline"));
    };
    for (const [label, fn, primary] of [["Use it", () => take(false), true], ["Edit points", () => take(true), false], ["Trace yourself", () => { this.sugg = null; this.use("area"); }, false]] as [string, () => void, boolean][]) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (primary) b.className = "primary";
      b.addEventListener("click", fn);
      acts.append(b);
    }
    this.q(".m-hint").textContent = "";
    this.ctl();
  }

  /** the magnetic path a .. b (engine), null = straight */
  private async lasso(a: P, b: P): Promise<P[] | null> {
    if (this.engine === false || !this.file) return null;
    try {
      const r = await post("/api/feat/region/scissors", { path: this.file, page: this.page(), space: this.v2d.space, points: [a, b] });
      return r?.kind === "magnetic" && Array.isArray(r.path) && r.path.length >= 2 ? r.path.map((q: number[]) => [q[0], q[1]] as P) : null;
    } catch {
      return null;
    }
  }

  /** a magnetic line's pieces joined into one polyline (straight where a piece is null), simplified to 2 cm (a pixel
   *  staircase would add length) */
  private magLine(): P[] {
    const line: P[] = [];
    for (let i = 0; i + 1 < this.pts.length; i++) {
      const piece = this.segs[i] ?? [this.pts[i], this.pts[i + 1]];
      line.push(...(i ? piece.slice(1) : piece));
    }
    if (line.length < 2) return [...this.pts];
    const u = this.units();
    const out = simplify(line, u > 0 ? 0.02 * u : 2 * this.unitsPerPx());
    return out.filter((p, i) => i === 0 || p[0] !== out[i - 1][0] || p[1] !== out[i - 1][1]);
  }

  /** live magnetic preview: one request in flight, the newest pointer position wins */
  private async hoverLasso() {
    if (this.hoverBusy || !this.magnetic || !(this.tool === "area" || this.tool === "length") || this.done || !this.pts.length || !this.hover) return;
    this.hoverBusy = true;
    const my = ++this.hoverSeq;
    const a = this.pts[this.pts.length - 1], b = this.hover;
    const path = await this.lasso(a, b);
    this.hoverBusy = false;
    if (my !== this.hoverSeq || this.done || this.pts[this.pts.length - 1] !== a) return;
    this.hoverPath = path;
    this.draw();
    if (this.hover && (this.hover[0] !== b[0] || this.hover[1] !== b[1])) void this.hoverLasso();
  }

  /** The room around p traced on the server (web/server/room_shape.py), at the scale in force (vector PDFs: the wall-line
   *  tracer closes door swings at the doorway). */
  private async trace(p: P, others: P[] = []): Promise<RoomShape | null> {
    const u = this.units();
    const r = 1;
    const scaleN = this.v2d.space === "pdf_pt" && /\.pdf$/i.test(this.file) && u > 0 ? Math.round((72 / 0.0254 / u) * 100) / 100 : 0;
    try {
      const res = await this.shapes(this.file, this.page(), this.v2d.space, [{ i: ++this.n, bbox: [p[0] - r, p[1] - r, p[0] + r, p[1] + r], ...(others.length ? { others } : {}), ...(scaleN > 0 ? { scale_n: scaleN } : {}) }]);
      const sh = res.shapes[0];
      return sh?.poly && sh.area ? sh : null;
    } catch {
      return null;
    }
  }

  /** close the area / finish the length: drop double clicks' twin points, measure (a fresh area snaps its sides) */
  private async finish() {
    if (!(this.tool === "area" || this.tool === "length") || this.done) return;
    if (this.tool === "length" && this.segs.some((s) => s)) {
      // magnetic line: drop a double-click's twin point, join the pieces along the lines, keep its kinks (2 cm)
      const tol0 = 3 * this.unitsPerPx();
      while (this.pts.length > 2 && Math.hypot(this.pts[this.pts.length - 1][0] - this.pts[this.pts.length - 2][0], this.pts[this.pts.length - 1][1] - this.pts[this.pts.length - 2][1]) <= tol0) {
        this.pts.pop();
        this.segs.length = this.pts.length - 1;
      }
      if (this.pts.length < 2) return;
      this.pts = this.magLine();
      this.segs = [];
      this.hoverPath = null;
      this.done = true;
      this.hover = null;
      this.draw();
      this.out("Measuring…");
      await this.measure(!this.free, "magnetic");
      return;
    }
    if (this.tool === "area" && this.segs.some((s) => s)) {
      // magnetic: drop a double-click's twin point, close along the lines, join the pieces, keep the shape's corners
      const tol0 = 3 * this.unitsPerPx();
      while (this.pts.length > 3 && Math.hypot(this.pts[this.pts.length - 1][0] - this.pts[this.pts.length - 2][0], this.pts[this.pts.length - 1][1] - this.pts[this.pts.length - 2][1]) <= tol0) {
        this.pts.pop();
        this.segs.length = this.pts.length - 1;
      }
      if (this.pts.length < 3) return;
      const n = this.pts.length;
      const close = this.magnetic && !this.alt ? await this.lasso(this.pts[n - 1], this.pts[0]) : null;
      const pieces = [...this.segs.slice(0, n - 1), close];
      const ring2: P[] = [];
      for (let i = 0; i < n; i++) {
        const piece = pieces[i] ?? [this.pts[i], this.pts[(i + 1) % n]];
        ring2.push(...piece.slice(0, -1));
      }
      const u = this.units();
      // simplify a closed ring in two halves (split at the point farthest from the first): a chord of length 0 would
      // collapse it
      const eps = u > 0 ? 0.03 * u : 2 * this.unitsPerPx();
      let far = 0;
      ring2.forEach((q, i) => { if (Math.hypot(q[0] - ring2[0][0], q[1] - ring2[0][1]) > Math.hypot(ring2[far][0] - ring2[0][0], ring2[far][1] - ring2[0][1])) far = i; });
      this.pts = ring([...simplify(ring2.slice(0, far + 1), eps).slice(0, -1), ...simplify([...ring2.slice(far), ring2[0]], eps).slice(0, -1)]);
      this.segs = [];
      this.hoverPath = null;
      this.done = true;
      this.hover = null;
      this.draw();
      this.out("Measuring…");
      await this.measure(!this.free, "magnetic lasso");
      return;
    }
    const tol = 3 * this.unitsPerPx();
    const pts = this.pts.filter((p, i) => i === 0 || Math.hypot(p[0] - this.pts[i - 1][0], p[1] - this.pts[i - 1][1]) > tol);
    if (this.tool === "area" && pts.length > 3 && Math.hypot(pts[0][0] - pts[pts.length - 1][0], pts[0][1] - pts[pts.length - 1][1]) <= tol) pts.pop();
    if (pts.length < (this.tool === "area" ? 3 : 2)) return;
    this.pts = pts;
    this.done = true;
    this.hover = null;
    this.draw();
    this.out("Measuring…");
    await this.measure(!this.free, this.free ? "as you clicked (Alt: no snapping)" : "");
  }

  /** the numbers of the finished shape: the engine (snapped sides, door openings, the drawing's scale) or, without it,
   *  the plain geometry at the scale in force */
  private async measure(snapSides: boolean, how = "") {
    const tool = this.tool;
    if (!(tool === "area" || tool === "length")) return;
    const my = ++this.seq;
    // the drawing's own scale first (up to 4 s): a shape measured right after the tool opened ('Edit outline' on a
    // selected room) went out at an answer's auto scale (DfE scan 39.07 vs the drawing's 39.37 px/m: +1.5 % area)
    // while the chip already said 1:200 from the drawing
    if (!this.user && this.engine !== false && this.file && (this.probing || this.engKey !== this.key()))
      await Promise.race([this.probe(), new Promise((r) => setTimeout(r, 4000))]);
    if (my !== this.seq || this.tool !== tool) return;
    const own = this.user ? this.user.unitsPerM : this.eng?.units_per_m && this.engKey === this.key() ? 0 : this.auto?.unitsPerM ?? 0;
    let r: Res | null = null;
    if (this.engine !== false && this.file) {
      try {
        r = await post("/api/feat/region/measure", {
          path: this.file, page: this.page(), space: this.v2d.space, points: this.pts, mode: tool, snap: snapSides,
          ...(own > 0 ? { units_per_m: own } : {}),
        });
        this.engine = true;
        if (!(own > 0) && r?.scale?.units_per_m) {
          this.eng = r.scale;
          this.engKey = this.key();
        }
      } catch {
        this.engine = false;
        r = null;
      }
    }
    if (my !== this.seq || this.tool !== tool) return;
    if (r && tool === "area" && r.snapped && r.polygon && r.polygon.length >= 3) this.pts = r.polygon.map((q) => [q[0], q[1]] as P);
    // pictures / scans: the line's segments on the wall faces, its ends on the walls it runs into (engine snap_polyline)
    if (r && tool === "length" && r.snapped && r.polyline && r.polyline.length >= 2) this.pts = r.polyline.map((q) => [q[0], q[1]] as P);
    this.res = r && (r.area_m2 != null || r.length_m != null) ? r : this.local(tool);
    this.render();
    this.draw();
    this.readout(false, how);
    this.emit();
  }

  /** the shape's numbers computed here (no engine, or the engine found no scale but an answer gave one) */
  private local(tool: "area" | "length"): Res {
    const u = this.units();
    if (!(u > 0)) return { mode: tool, error: this.stillReading || this.probing
      ? "Still reading the drawing's scale (a scan's first read takes up to a minute): your shape stays and is measured as soon as the scale is read. Or Calibrate now (two clicks on a length you know)."
      : "No scale yet: calibrate first (two clicks on a length you know, then type it)." };
    if (tool === "length") return { mode: tool, length_m: Math.round((pathLen(this.pts, false) / u) * 1000) / 1000, local: true };
    return { mode: tool, area_m2: Math.round((shoelace(this.pts) / (u * u)) * 1000) / 1000, perimeter_m: Math.round((pathLen(this.pts, true) / u) * 1000) / 1000, skirting_m: null, openings: [], local: true };
  }

  /** the shape on the sheet itself: mm on the paper (PDF) or px (image) */
  private onSheet(units: number, what: string): string {
    if (!(units > 0)) return "";
    if (this.v2d.space !== "pdf_pt" && document.body.dataset.view !== "expert") return ""; // UX 01:37 (c): px = Expert view only
    return this.v2d.space === "pdf_pt" ? `${what} ${(units * 25.4 / 72).toFixed(1)} mm on the sheet` : `${what} ${units.toFixed(0)} px on the image`;
  }

  /** the live readout under the tools */
  private readout(live = false, how = "") {
    this.ctl();
    const acts = this.q(".m-acts");
    acts.innerHTML = "";
    const btn = (label: string, fn: () => void, primary = false, title = "") => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (title) b.title = title;
      if (primary) b.className = "primary";
      b.addEventListener("click", fn);
      acts.append(b);
    };
    const clear = () => btn(this.tool === "count" ? "New count" : this.tool === "length" ? "New line" : "New shape", () => this.use(this.tool), false, "Clear the drawing and start again (what you saved to the takeoff stays)");
    const at = `at ${esc(this.chipLabel())}`;
    if (this.tool === "count") {
      this.out(this.pts.length ? `<b>${this.pts.length}</b> ${esc(this.what || "marked")} <small>by you, numbered on the drawing</small>` : "");
      if (this.pts.length) {
        const inp = document.createElement("input");
        inp.type = "text";
        inp.placeholder = "what? e.g. doors";
        inp.value = this.what;
        inp.title = "What you are counting (it names the takeoff row)";
        inp.style.cssText = "min-height:36px;width:130px;border:1px solid var(--line);border-radius:999px;padding:0 12px;font:inherit;font-size:13.5px;";
        inp.addEventListener("input", () => {
          this.what = inp.value.trim().slice(0, 40);
          const b = this.q(".m-out b");
          if (b?.nextSibling) b.nextSibling.textContent = ` ${this.what || "marked"} `;
        });
        acts.append(inp);
        btn("Save to takeoff", () => void this.save2takeoff(), true, "Add this count to this visit's takeoff (Export)");
        clear();
      }
      return;
    }
    if (this.tool === "length" && !this.done) {
      const u = this.units();
      const L = pathLen(this.segs.some((s) => s) ? this.magLine() : this.pts, false);
      this.out(this.pts.length >= 2 && u > 0 ? `Length so far: <b>${fmt2(L / u)} m</b> <small>double-click or Enter to finish · ${at}</small>` : "");
      if (this.pts.length) clear();
      return;
    }
    if (this.tool === "area" && !this.done) {
      if (this.pts.length) {
        const u = this.units();
        const live = this.pts.length >= 3 && u > 0 ? ` · <b>${fmt2(shoelace(this.pts) / (u * u))} m²</b> so far, perimeter ${fmt2(pathLen(this.pts, true) / u)} m <small>(closing back to the first corner; ${at})</small>` : "";
        this.out(`${this.pts.length} corner${this.pts.length === 1 ? "" : "s"}${live} <small>click the first corner again (or double-click) to close</small>`);
        clear();
      }
      return;
    }
    const r = this.res;
    if (!r || live) return;
    if (r.error || (r.area_m2 == null && r.length_m == null)) {
      this.out(esc(r.error ?? r.text ?? "Could not measure this shape."));
      if (!this.units()) btn("Calibrate", () => { this.pending = this.tool; this.use("scale"); }, true);
      clear();
      return;
    }
    const sides = r.snapped ? " · sides on the wall faces" : "";
    const imp = this.imperial();
    if (this.tool === "length") {
      const Lu = pathLen(this.pts, false);
      this.out(`Length: <b>${fmt2(r.length_m!)} m</b>${imp ? ` <small>(${ftIn(r.length_m!)})</small>` : ""}${this.pts.length > 2 ? ` <small>${this.pts.length - 1} segments</small>` : ""} <small>${at}${how ? ` · ${esc(how)}` : ""}${r.snapped ? " · on the wall faces" : ""}</small><span class="m-sheet">${esc(this.onSheet(Lu, "drawn"))}</span>${this.loose ? this.clickErr("length", Lu) : ""}`);
    } else {
      const sk = r.skirting_m != null && r.openings ? ` · skirting <b>${fmt2(r.skirting_m)} m</b>${imp ? ` <small>${ftIn(r.skirting_m)}</small>` : ""} <small>(perimeter minus ${r.openings.length} opening${r.openings.length === 1 ? "" : "s"}, dashed)</small>` : "";
      this.out(`Area of your outline: <b>${fmt2(r.area_m2!)} m²</b>${imp ? ` <small>(${ft2(r.area_m2!)} ft²)</small>` : ""} · perimeter <b>${fmt2(r.perimeter_m!)} m</b>${imp ? ` <small>(${ftIn(r.perimeter_m!)})</small>` : ""}${sk} <small>${at}${how ? ` · ${esc(how)}` : ""}${sides}${r.local && !how ? " · measured as clicked" : ""}</small><span class="m-sheet">${esc(this.onSheet(pathLen(this.pts, true), "perimeter"))}</span>${this.loose ? this.clickErr("area") : ""}`);
    }
    btn("Save to takeoff", () => void this.save2takeoff(), true, "Show it as an answer and add it to this visit's takeoff (Export)");
    clear();
    this.hint();
  }

  /** onMeasure once per finished (or re-measured) shape: F7 offers 'use it as the answer', F2's takeoff hears it */
  private emit() {
    const r = this.res;
    if (!r || r.error) return;
    const scale = this.user?.label ?? (this.eng?.units_per_m ? this.eng.label : this.auto?.label ?? "");
    const k = `${this.tool}|${r.area_m2 ?? r.length_m}|${this.pts.length}`;
    if (k === this.emitted) return;
    this.emitted = k;
    try {
      if (this.tool === "area" && r.area_m2 != null) this.onMeasure({ kind: "area", value: Math.round(r.area_m2 * 1000) / 1000, unit: "m²", poly: this.pts.map((p) => [p[0], p[1]] as P), manual: true, scale });
      else if (this.tool === "length" && r.length_m != null) this.onMeasure({ kind: "length", value: Math.round(r.length_m * 1000) / 1000, unit: "m", ...(this.pts.length === 2 ? { line: [this.pts[0], this.pts[1]] as [P, P] } : { line: [this.pts[0], this.pts[this.pts.length - 1]] as [P, P] }), manual: true, scale });
    } catch {
      /* a listener's problem never breaks measuring */
    }
  }

  /** The measurement as an answer card (it joins this visit's takeoff; F2's export and F7 see it). */
  private async save2takeoff() {
    const r = this.res;
    const sp = this.v2d.space;
    const page = sp === "pdf_pt" ? this.page() : null;
    const f = this.file;
    let a: Answer;
    let q: string;
    if (this.tool === "count") {
      const what = this.what || "items";
      q = this.what ? `How many ${this.what} did I count on this drawing?` : "How many did I mark on this drawing?";
      a = { value: this.pts.length, unit: null, confidence: 0.9, solver: "measure/count", trace: [`counted by hand: ${this.pts.length} ${what}, each marked`],
        evidence: this.pts.map((p, i) => ({ file: f, page, bbox: [p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2], bbox_space: sp, note: `${this.what ? `${this.what} ` : "marker "}${i + 1}`, exact: true })) } as Answer;
    } else if (this.tool === "length" && r?.length_m != null) {
      q = "Length of the line I measured";
      a = { value: r.length_m, unit: "m", confidence: 0.6, solver: "measure/length", trace: [`measured by hand: ${this.pts.length} points at ${this.chipLabel()} (your line, not checked)`],
        assumptions: [`scale ${this.chipLabel()}`],
        evidence: [{ file: f, page, bbox: bboxOf(this.pts), bbox_space: sp, polyline: this.pts, note: `your line: ${r.length_m.toFixed(2)} m`, exact: true }] } as Answer;
    } else if (this.tool === "area" && r?.area_m2 != null) {
      q = this.name ? `What is the area of the ${this.name} (measured)?` : "What is the area of this outline (measured)?";
      const ev: Evidence[] = [{ file: f, page, bbox: bboxOf(this.pts), bbox_space: sp, polygon: this.pts, exact: true,
        note: `${this.fromSugg ? "our traced outline, used by you" : "your outline"}${r.snapped ? ", on the wall faces" : ""}: ${r.area_m2.toFixed(2)} m², perimeter ${r.perimeter_m?.toFixed(2)} m` } as Evidence];
      for (const o of r.openings ?? []) ev.push({ file: f, page, bbox: bboxOf([o.a, o.b]), bbox_space: sp, polyline: [o.a, o.b], exact: false, note: `opening ${o.m.toFixed(2)} m (no skirting)` } as Evidence);
      a = { value: r.area_m2, unit: "m2", confidence: 0.6, solver: "measure/area", trace: [this.fromSugg ? `suggested outline (traced by us${this.res?.snapped ? ", sides on the wall faces" : ""}), used by you: ${this.pts.length} corners at ${this.chipLabel()} (not checked)` : `measured by hand: ${this.pts.length} corners at ${this.chipLabel()} (your outline, not checked)`],
        assumptions: [`scale ${this.chipLabel()}`, ...(r.skirting_m != null ? [`skirting ${r.skirting_m.toFixed(2)} m = perimeter ${r.perimeter_m?.toFixed(2)} m minus ${r.openings?.length ?? 0} door opening(s)`] : [])],
        evidence: ev } as Answer;
    } else return;
    if (!this.savedQ.includes(q)) this.savedQ.push(q);
    if (!this.allSavedQ.includes(q)) this.allSavedQ.push(q);
    this.ctl();
    try {
      if (host.showAnswer) {
        await host.showAnswer(a, q);
        this.out(`${this.q(".m-out").innerHTML}<small>✓ Shown as the answer and added to this visit's takeoff (Export under the question).</small>`);
      } else {
        recordAnswer(a, { path: f, page, question: q, link: location.href });
        this.out(`${this.q(".m-out").innerHTML}<small>✓ Added to this visit's takeoff.</small>`);
      }
    } catch (e) {
      this.out(`${this.q(".m-out").innerHTML}<small>Could not save it: ${esc((e as Error).message)}</small>`);
    }
  }

  /** the shape on the measure layer: segments (+ the rubber band to the pointer), the closed outline, the door openings
   *  (dashed), draggable corners; count = numbered markers */
  private draw() {
    if (this.tool === "scale" || !this.tool) return;
    if (this.pts.length && !this.shapeKey) this.shapeKey = this.key();
    this.v2d.measureClear();
    if (this.ref && this.tool === "area" && !this.done) this.v2d.measurePoly(this.ref, "", "m1-sugg");
    if (this.tool === "count") {
      this.pts.forEach((p, i) => {
        const g = this.v2d.measureDot(p);
        g.classList.add("m1-n");
        g.querySelector("circle")?.setAttribute("r", "10");
        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("dy", "0.36em");
        t.textContent = String(i + 1);
        g.append(t);
        this.handle(g, i);
      });
      this.ctl();
      return;
    }
    const r = this.res;
    if (this.sugg) {
      this.v2d.measurePoly(this.sugg, "", "m1-sugg");
      this.ctl();
      return;
    }
    if (this.tool === "area" && this.done && this.pts.length >= 3) {
      this.v2d.measurePoly(this.pts, r?.area_m2 != null ? `📐 ${fmt2(r.area_m2)} m²` : "");
    } else {
      if ((this.tool === "area" || this.tool === "length") && !this.done && (this.segs.some((s) => s) || this.hoverPath)) {
        // magnetic: each piece along the lines, then the live path to the pointer
        for (let i = 0; i + 1 < this.pts.length; i++) {
          const piece = this.segs[i] ?? [this.pts[i], this.pts[i + 1]];
          for (let k = 0; k + 1 < piece.length; k++) this.v2d.measureLine(piece[k], piece[k + 1], "", "m1-seg");
        }
        const hp = this.hover ? this.hoverPath ?? [this.pts[this.pts.length - 1], this.hover] : [];
        for (let k = 0; k + 1 < hp.length; k++) this.v2d.measureLine(hp[k], hp[k + 1], "", "m1-seg m1-hover");
      } else {
      const line = [...this.pts, ...(!this.done && this.hover ? [this.hover] : [])];
      for (let i = 0; i + 1 < line.length; i++) {
        const last = this.done && i + 2 === line.length && r?.length_m != null;
        this.v2d.measureLine(line[i], line[i + 1], last ? `📐 ${fmt2(r!.length_m!)} m` : "", `m1-seg${!this.done && i + 2 === line.length && this.hover ? " m1-hover" : ""}`);
      }
      }
    }
    for (const o of r?.openings ?? []) this.v2d.measureLine(o.a, o.b, "", "m1-gap");
    // a finished shape: a '+' on every side adds a corner there (drag it)
    if (this.done && this.pts.length <= 40) {
      const n = this.pts.length;
      for (let i = 0; i < (this.tool === "area" ? n : n - 1); i++) {
        const a = this.pts[i], b = this.pts[(i + 1) % n];
        const g = this.v2d.measureDot([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
        g.classList.add("m1-mid");
        g.querySelector("circle")?.setAttribute("r", "4.5");
        const tt = document.createElementNS("http://www.w3.org/2000/svg", "title");
        tt.textContent = "Drag to add a corner here";
        g.append(tt);
        g.addEventListener("pointerdown", (e) => {
          if (e.button !== 0) return;
          e.stopPropagation();
          e.preventDefault();
          this.pts.splice(i + 1, 0, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
          this.drag(i + 1, e);
        });
      }
    }
    if (this.pts.length <= 40) this.pts.forEach((p, i) => {
      const g = this.v2d.measureDot(p);
      if (i === 0 && this.tool === "area" && !this.done && this.pts.length >= 3) g.classList.add("first");
      if (this.done) {
        const tt = document.createElementNS("http://www.w3.org/2000/svg", "title");
        tt.textContent = "Drag to move · double-click to delete this corner";
        g.append(tt);
      }
      this.handle(g, i);
    });
    this.ctl();
  }

  /** a corner / marker: drag it to edit (snapped on release unless Alt; a finished shape is measured again as placed);
   *  the first corner of an open area closes it */
  private handle(g: SVGGElement, i: number) {
    g.classList.add("m1-h");
    g.dataset.i = String(i);
    g.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      e.preventDefault();
      if (i === 0 && this.tool === "area" && !this.done && this.pts.length >= 3) {
        this.skipClick = performance.now() + 400;
        this.queue = this.queue.then(() => this.finish());
        return;
      }
      this.drag(i, e);
    });
  }

  /** Move point i with the pointer (document listeners: the layer is redrawn while dragging); on release it snaps
   *  (unless Alt) and a finished shape is measured again as placed. */
  private drag(i: number, e: PointerEvent) {
    let moved = false;
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      ev.stopPropagation();
      ev.preventDefault();
      const q = this.v2d.clientToDrawing(ev.clientX, ev.clientY);
      if (!q || !this.pts[i]) return;
      moved = true;
      this.pts[i] = q;
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.draw(); });
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      ev.stopPropagation();
      document.removeEventListener("pointermove", move, true);
      document.removeEventListener("pointerup", up, true);
      document.removeEventListener("pointercancel", up, true);
      this.skipClick = performance.now() + 400;
      if (!moved) {
        // UX S-M7: a click on a count marker (no drag) removes THAT marker; the others are numbered again
        if (this.tool === "count" && this.pts[i]) {
          this.pts.splice(i, 1);
          this.readout();
        }
        this.draw();
        return;
      }
      const alt = ev.altKey;
      this.queue = this.queue.then(async () => {
        if (!alt && this.tool !== "count" && this.pts[i]) this.pts[i] = (await this.snap(this.pts[i])).point;
        if (this.done) await this.measure(false, "as you placed it");
        else {
          this.res = null;
          this.draw();
          this.readout();
        }
      });
    };
    document.addEventListener("pointermove", move, true);
    document.addEventListener("pointerup", up, true);
    document.addEventListener("pointercancel", up, true);
  }

  /** Trace the room around point p (drawing coordinates) and measure it at the current scale: area (m²) and the
   *  outline's extent along the drawing's axes (length ≥ width, m). Draws the outline; null when there is no scale or
   *  no closed room there. Used by the answer card (a room found by its printed label). */
  /** captain 23:47 — a room selected by right-click: 'edit' = the Area tool holding that room's outline as an editable
   *  polygon (drag / add / delete corners; its sides snapped onto the wall faces, then measured); 'trace' = the Area tool
   *  empty, the outline dashed for reference, magnetic on for pictures / scans. No outline given: the room around `at` is
   *  traced on the server first. -> false when no outline could be had (the visitor clicks the corners). */
  async outlineRoom(mode: "edit" | "trace", poly: P[] | null, at: P | null, name = ""): Promise<boolean> {
    this.open(true, "area");
    deselect(); // the selection's highlight gives way to the outline being edited
    this.name = name;
    let outline = poly && poly.length >= 3 ? ring(poly) : null;
    if (!outline && at) {
      this.out(`Tracing ${name ? esc(name) : "the room"}…`);
      const sh = await this.trace(at);
      outline = sh?.poly && sh.poly.length >= 3 && sh.poly.length <= 60 ? ring(sh.poly) : null;
    }
    if (this.tool !== "area") return false;
    if (!outline) {
      this.out(`No closed outline found for ${name ? esc(name) : "that room"} (an opening without a door line): click its corners.`);
      return false;
    }
    if (mode === "edit") {
      this.pts = outline;
      this.done = true;
      this.fromSugg = true;
      this.queue = this.queue.then(() => this.measure(true, `${name ? `${name}: ` : ""}the room's outline, drag its corners to edit`));
    } else {
      this.ref = outline;
      this.out("");
      this.magnetic = this.engine !== false && (this.v2d.space === "image_px" || !!this.eng?.source?.match(/scan|image|px/i));
      this.draw();
      this.ctl();
      this.q(".m-hint").textContent = `Click ${name ? `${name}'s` : "the room's"} corners (its outline is dashed for reference)${this.magnetic ? "; magnetic: the line follows the walls between clicks" : ""}. Click the first corner again to close.`;
    }
    return true;
  }

  async roomAt(p: [number, number], name = "", others: [number, number][] = [], manual = false): Promise<{ m2: number; length: number; width: number; perimeter: number; poly: [number, number][] } | null> {
    const s = this.scale;
    if (!s) return null;
    this.out(`Tracing ${name ? esc(name) : "the room"}…`);
    try {
      // a vector PDF: its 1:N lets the wall-line tracer close door swings at the doorway (1 m = unitsPerM pt)
      const scaleN = this.v2d.space === "pdf_pt" && /\.pdf$/i.test(this.file) ? Math.round(72 / 0.0254 / s.unitsPerM * 100) / 100 : 0;
      const res = await this.shapes(this.file, 0, this.v2d.space, [{ i: ++this.n, bbox: [p[0] - 1, p[1] - 1, p[0] + 1, p[1] + 1], ...(others.length ? { others } : {}), ...(scaleN > 0 ? { scale_n: scaleN } : {}) }]);
      const sh = res.shapes[0];
      if (!sh?.poly || !sh.area) {
        this.out(others.length ? `${esc(name || "The room")} is open to the next rooms (doorways without a door line), so its outline does not close. Click its corners with Area, or measure its length and width with Length.`
          : `No closed room found there (the outline leaks through an opening). Click its corners with Area instead.`);
        return null;
      }
      const m2 = sh.area / (s.unitsPerM * s.unitsPerM);
      const xs = sh.poly.map((q) => q[0]);
      const ys = sh.poly.map((q) => q[1]);
      const dx = (Math.max(...xs) - Math.min(...xs)) / s.unitsPerM;
      const dy = (Math.max(...ys) - Math.min(...ys)) / s.unitsPerM;
      // the outline's length (baseboards / skirting = this minus the door openings, captain 16:59 convention)
      const poly = sh.poly;
      const perimeter = poly.reduce((n, q, i) => { const r = poly[(i + 1) % poly.length]; return n + Math.hypot(r[0] - q[0], r[1] - q[1]); }, 0) / s.unitsPerM;
      // the card's trace is a SUGGESTION in the Area tool (captain 22:56): use it, edit its points, or trace it yourself
      const offer = this.tool === "area" && !this.pts.length && !this.q(".m-panel").hidden && sh.poly.length <= 40;
      const how = `traced ${sh.method === "wall lines" ? "from the wall lines (door swings closed at the doorway)" : "outline"} at ${s.label} (${s.source}); inner faces of the walls${sh.method === "wall lines" ? "" : ", furniture lines may cut it"}; door openings not deducted from the perimeter`;
      if (offer) {
        this.name = name;
        this.suggest(ring(sh.poly), how, `${name ? `${esc(name)}: we` : "We"} traced it: <b>${fmtM(m2)} m²</b> · perimeter ${fmtM(perimeter)} m. `.replace(/: <b>[^<]*<\/b> · perimeter [^.]* m\. $/, ""));
        this.out(`${name ? `${esc(name)}: ` : ""}we traced it: <b>${fmtM(m2)} m²</b> · perimeter <b>${fmtM(perimeter)} m</b> <small>${esc(how)}. Use it, edit its points, or trace the room yourself.</small>${this.clickErr("area")}`);
      } else {
        this.v2d.measurePoly(sh.poly, `${fmtM(m2)} m²`);
        this.out(`${name ? `${esc(name)}: ` : "Room area: "}<b>${fmtM(m2)} m²</b> · perimeter <b>${fmtM(perimeter)} m</b> <small>${esc(how)}. Not right? Click the corners with Area.</small>${this.clickErr("area")}`);
      }
      try {
        this.onMeasure({ kind: "area", value: Math.round(m2 * 1000) / 1000, unit: "m²", poly: sh.poly, ...(name ? { name } : {}), manual, scale: s.label });
      } catch {
        /* a listener's problem never breaks measuring */
      }
      return { m2, length: Math.max(dx, dy), width: Math.min(dx, dy), perimeter, poly: sh.poly };
    } catch (e) {
      this.out(`Could not trace the room: ${esc((e as Error).message)}`);
      return null;
    }
  }
}
