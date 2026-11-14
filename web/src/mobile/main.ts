/**
 * The on-site phone page (web/mobile.html, W6): take a photo of a printed plan, ask, see the answer drawn on the photo.
 * Same /api as the customer UI (upload → ask → shapes), same Apple-like tokens (../style.css), same answer wording
 * helpers (../customer, ../card, ../measure). One screen: photo → question (chips / typed / voice) → big number, one
 * sentence, status pills, evidence on the photo, and a two-tap scale tool when the scale is unknown.
 */
import "@fontsource-variable/inter";
import "../style.css";
import "./mobile.css";
import { doorScale, fileUrl, listFiles, privacy, roomShapes, upload } from "../api";
import { dataLineHtml, tradeoffLine } from "../governance";
import { fmtValue, unitLabel } from "../card";
import { approxEvidence, calm, confidenceWords, costTier, isGuess, needsMeasuring, printedReading, targetNoun } from "../customer";
import { doorScaleOf, fmtM, scaleFromAnswer, scaleGuessed, scaleLabel, type Scale } from "../measure";
import type { Answer, Evidence, FileInfo } from "../types";
import { floorName, PROJECTS, type Project } from "../projects";
import { prepImage } from "./prep";
import { PhotoView, polyArea, polyLength, type Mark } from "./view";

type Pt = [number, number];
type Space = "pdf_pt" | "image_px";
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 'Try a case' (captain 20:53 / 21:02): W5's catalogue (web/fixtures/projects.json via ../projects: the desktop's
 *  projects, one list), each project its own case: its first 2D sheet, its chips, its credit; a project's IFC model is a
 *  link to the full view. The server helper (phoneapi.ts) says which sheets it has and opens a non-dataset sheet through
 *  W5's upload; experiments/w6/warm_cases.py asks every chip once, so the booth answers from memory. Answers are live. */
const SHEET_EXT = /\.(pdf|png|jpe?g)$/i;
const sheetsOf = (p: Project) => p.sheets.filter((s) => SHEET_EXT.test(s.path));
const sheetOf = (p: Project) => sheetsOf(p)[0];
/** A case's chips for one of its sheets: the sheet's own (an optional 'chips' on a catalogue sheet), else the case's on
 *  its first sheet; on another sheet only the case's chips about any sheet ('this floor', 'each floor') plus the photo
 *  flow's, since the others name rooms of the first one (DfE Level 01 offered Level 00's Multi-Purpose Hall). */
const GENERIC_Q = /\b(this|each|every|all|the whole) (floor|drawing|sheet|plan|level|storey|page)s?\b|\bon each floor\b/i;
function chipsFor(p: Project, i: number): string[] {
  const own = (sheetsOf(p)[i] as { chips?: string[] } | undefined)?.chips;
  if (own?.length) return own;
  if (i === 0) return p.chips;
  const keep = p.chips.filter((q) => GENERIC_Q.test(q));
  return [...keep, ...CHIPS.filter((q) => !keep.includes(q))].slice(0, 4);
}
// a scanned print: the catalogue's 'scan' kind or badge, and the historic sheets (scans of old prints: HABS / HAER)
const isScan = (p: Project) => ["scan", "historic"].includes(p.kind as string) || (p as { badge?: string }).badge === "scan";
// AREAS > LENGTHS > COUNTS (Stoa, Sat 12:40). 'How many doors?' was the third chip until cycle 7: on a fresh DfE phone
// photo it took 190 s (load ~70) to 478 s (load ~290) and said '≈ 67 Rough guess', nothing marked (the sheet has ~104);
// a typed door question still works. Both tap chips need no model: CPU only, seconds, the line / outline is the proof.
const CHIPS = ["How big is this room?", "How long is this wall?", "Total floor area?"];

const state = {
  /** the case opened from 'Try a case' (null = the visitor's own photo or PDF) */
  case: null as Project | null,
  /** which of the case's 2D sheets is shown (Maleva: floor 1-5) */
  caseSheet: 0,
  file: null as FileInfo | null,
  pdf: false,
  page: 0,
  /** pages in the PDF (1 for photos) */
  pages: 1,
  /** image px per evidence unit (1 for images; the render scale for a PDF page) */
  k: 1,
  /** a rotated PDF page (its /Rotate applied when drawn): evidence pt ↔ image px through pdf.js; null = px = pt × k */
  map: null as null | { toPx: (p: Pt) => Pt; toPt: (p: Pt) => Pt },
  answer: null as Answer | null,
  question: "",
  /** the scale in evidence units (px or pt) per metre: the answer's, or the visitor's two taps */
  scale: null as Scale | null,
  /** a scale an earlier answer READ from this photo / sheet (its scale bar, a printed 1:N): the tap-the-room flow uses
   *  it before the door-swing guess (Stoa's DfE school sheet: scale bar 31.5 px/m, door swings 56 px/m) */
  fileScale: null as Scale | null,
  tool: null as null | "scale" | "room" | "length",
  taps: [] as Pt[],
  /** outlines the visitor asked for (tap the room), in evidence units */
  rooms: [] as { poly: Pt[]; method?: string }[],
  /** lines the visitor measured (tap both ends: 'How long is this wall?'), in evidence units */
  lines: [] as Pt[][],
  /** a photo of a printed sheet with perspective: the homography photo px → the paper's flat plane (row-major 3x3) and
   *  k = flat length / photo length on average over the sheet; tap-the-room measures there (null = on the photo) */
  flat: null as null | { H: number[]; k: number; how: string; quad: Pt[]; w: number; h: number },
  busy: null as AbortController | null,
  /** after the printed scale bar replaced the door-swing guess: what the guess had said (the number changed under the
   *  visitor's eyes, so the page says why) */
  barNote: "",
  t: { upload: 0, ask: 0, room: 0 },
};
(window as unknown as { __w6: typeof state }).__w6 = state; // playwright reads timings from here

const view = new PhotoView($("photo"));
/** test hook (playwright): the screen point of an image point */
(window as unknown as { __w6at: (x: number, y: number) => number[] }).__w6at = (x, y) => view.toClient(...toPx([x, y]));
const space = (): Space => (state.pdf ? "pdf_pt" : "image_px");
// a case's sheet is a drawing even when it is an image (Stoa's PNGs): 'your photo' is only the visitor's own picture
const sheet = () => (state.pdf || state.case ? "drawing" : "photo");

// ---------- the photo's scale from its door swings (best estimate; GET /api/m/doorscale, src/mobile/doorscale.py) ----------
interface DoorEst { ok: boolean; px_per_m?: number; arcs?: number; leaf_m?: number; why?: string }
const doorMemo = new Map<string, Promise<DoorEst>>();
function doorEstimate(path: string): Promise<DoorEst> {
  if (!doorMemo.has(path)) {
    doorMemo.set(path, fetch(`/api/m/doorscale?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false }))
      .catch(() => ({ ok: false, why: "unavailable" })));
  }
  return doorMemo.get(path)!;
}
// ---------- the scale bar printed on the photo (GET /api/m/barscale, src/mobile/barscale.py) ----------
// A printed fact, not a guess: tap-the-room measures at it before the door swings (DfE photo: bar 31.50 px/m, by hand
// 31.4, door swings 29.8). It reads the photo's text layer (local OCR, ~20-80 s cold, cached on disk for the later
// questions on the photo too; not the warm-up's: that one reads O1's deskewed copy), so it starts when the visitor
// picks 'tap the room' (no cost for other questions) and a room measured at the door swings meanwhile is measured
// again when it arrives.
// Its two ends are kept like the visitor's two scale taps, so a tilted photo maps them into the flat plane exactly.
interface BarEst { ok: boolean; px_per_m?: number; pts?: number[][]; m?: number; values?: number[]; unit?: string; why?: string }
type BarScale = Scale & { pts: [Pt, Pt]; m: number; bar: string };
const barMemo = new Map<string, Promise<BarScale | null>>();
/** settled bar readings by file (absent = still reading) */
const barDone = new Map<string, BarScale | null>();
function barScale(path: string): Promise<BarScale | null> {
  if (!barMemo.has(path)) {
    barMemo.set(path, fetch(`/api/m/barscale?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false }))
      .catch(() => ({ ok: false })).then((d: BarEst) => {
        const ok = d.ok && !!d.px_per_m && d.px_per_m > 0 && d.pts?.length === 2 && !!d.m && d.m > 0;
        const v = d.values ?? [], u = d.unit ?? "m";
        const span = v.length > 1 ? `${+v[0].toFixed(2)}–${+v[v.length - 1].toFixed(2)} ${u}` : `${d.m} m`;
        const s: BarScale | null = ok ? { unitsPerM: d.px_per_m!, label: scaleLabel(d.px_per_m!, "image_px"), source: `from the scale bar printed on the drawing, ${span}`,
          pts: [d.pts![0] as Pt, d.pts![1] as Pt], m: d.m!, bar: `scale bar ${span}` } : null;
        barDone.set(path, s);
        onBar(path, s);
        return s;
      }));
  }
  return barMemo.get(path)!;
}
/** The bar arrived: a tapped room measured at the door-swing guess, or waiting for two scale taps, is measured at it.
 *  A scale the visitor set, or one an answer read, stays; a server answer's own number is not touched. */
function onBar(path: string, s: BarScale | null) {
  if (state.file?.path !== path || state.pdf) return;
  if (!s) { // no bar on this photo: drop the 'Reading the scale bar…' line (the door-swing estimate stays)
    if (state.answer && (state.rooms.length || state.lines.length)) renderResult();
    if (state.tool === "scale" && !state.scale && !state.taps.length && (state.rooms.length || state.lines.length))
      toolHint("We could not read a scale bar on this photo. Set the scale: tap both ends of a length you know (a printed dimension; a door is about 0.9 m).");
    return;
  }
  if (!state.fileScale || scaleGuessed(state.fileScale)) state.fileScale = s;
  if ((!state.rooms.length && !state.lines.length) || state.scale?.user || (state.scale && !scaleGuessed(state.scale))) return;
  if (state.tool === "scale" && (state.scale || state.taps.length)) return; // the visitor is setting it by hand
  const last = state.rooms[state.rooms.length - 1], ln = state.lines[state.lines.length - 1];
  const was = !state.scale ? "" : ln && state.answer?.solver === "tap" ? `${fmtM(lengthMetres(ln, state.scale))} m` : last ? `${fmtM(areaM2(last.poly, state.scale))} m²` : "";
  state.barNote = was ? `The door-swing estimate had said ≈ ${was}; the printed scale bar is the better scale.` : "";
  state.scale = s;
  setTool(state.tool === "room" || state.tool === "length" ? state.tool : null);
  if (state.answer) renderResult();
  if (!state.lines.length) zoomToRoom();
}
// ---------- a tilted photo: measure in the paper's flat plane (GET /api/m/sheetquad, src/mobile/sheetquad.py) ----------
// Stoa's DfE sheet through a phone camera tilted 10 / 20 / 30°, scale bar at the bottom, the room at the top of the
// sheet vs a flat photo: +27 / +53 / +75 % on the photo, +3 / +6 / +16 % in the flat plane (KNOWLEDGE 19:2x).
interface SheetQuad { ok: boolean; H?: number[]; quad?: number[][]; w?: number; h?: number; how?: string }
async function sheetFlat(path: string) {
  const j: SheetQuad = await fetch(`/api/m/sheetquad?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false })).catch(() => ({ ok: false }));
  if (state.file?.path !== path || !j.ok || !j.H || j.H.length !== 9 || !j.quad || !j.w || !j.h || !/^perspective/.test(j.how ?? "")) return;
  const qa = polyArea(j.quad as Pt[]);
  if (!(qa > 0)) return;
  state.flat = { H: j.H, k: Math.sqrt((j.w * j.h) / qa), how: j.how!, quad: j.quad as Pt[], w: j.w, h: j.h };
  if (state.rooms.length && state.scale && !state.tool) { // the room tags and the number at the corrected m²
    setTool(null);
    if (state.answer) renderResult();
  }
}
/** photo px → flat px */
function fpt(p: Pt): Pt {
  const H = state.flat!.H, w = H[6] * p[0] + H[7] * p[1] + H[8];
  return [(H[0] * p[0] + H[1] * p[1] + H[2]) / w, (H[3] * p[0] + H[4] * p[1] + H[5]) / w];
}
/** m² of an outline (evidence units) at scale s; on a tilted photo of a sheet, in its flat plane: a scale the visitor
 *  tapped is mapped there exactly, one without a place (door swings, a scale bar an answer read) by the sheet's average */
function areaM2(poly: Pt[], s: Scale): number {
  const f = !state.pdf ? state.flat : null;
  if (!f || !onSheet(poly, f.quad)) return polyArea(poly) / (s.unitsPerM * s.unitsPerM);
  const u = flatUnits(s, f);
  return polyArea(poly.map(fpt)) / (u * u);
}
/** metres along a line (evidence units) at scale s; on a tilted photo of a sheet, in its flat plane like areaM2 */
function lengthMetres(line: Pt[], s: Scale): number {
  const f = !state.pdf ? state.flat : null;
  if (!f || !onSheet(line, f.quad)) return polyLength(line) / s.unitsPerM;
  return polyLength(line.map(fpt)) / flatUnits(s, f);
}
/** flat px per metre: the visitor's two scale taps mapped exactly, a deskewed-copy scale by the long sides, else × k */
function flatUnits(s: Scale, f: NonNullable<typeof state.flat>): number {
  const t = (s as Scale & { pts?: [Pt, Pt]; m?: number }).pts, m = (s as Scale & { m?: number }).m;
  if (t && m) {
    const [a, b] = [fpt(t[0]), fpt(t[1])];
    return Math.hypot(b[0] - a[0], b[1] - a[1]) / m;
  }
  return (s as FlatScale).deskewed ? s.unitsPerM * deskewToFlat(f) : s.unitsPerM * f.k;
}
/** A scale an answer read on O1's DESKEWED copy of the photo (stoa/solvers/raster/photo.py, in prepare() since gate
 *  d25: 'its corners were found and the perspective undone'): its px are flat-sheet px, the sheet's long side = the
 *  quad's mean long side in photo px (photo.deskew: L = max(mean width, mean height)), not photo px at the scale bar.
 *  So it maps to our flat plane by the long sides (no tilt factor k): at a 10 % foreshortened height, ×k would read
 *  lengths ~5 % (areas ~11 %) off. Our quad stands in for O1's (both find the same paper corners). */
type FlatScale = Scale & { deskewed?: boolean };
function deskewToFlat(f: { quad: Pt[]; w: number; h: number; k: number }): number {
  const [tl, tr, br, bl] = f.quad, d = (p: Pt, q: Pt) => Math.hypot(q[0] - p[0], q[1] - p[1]);
  const L = Math.max((d(tl, tr) + d(bl, br)) / 2, (d(tl, bl) + d(tr, br)) / 2);
  return L > 0 ? Math.max(f.w, f.h) / L : f.k;
}
const deskewedAnswer = (a: Answer) => [...(a.trace ?? []), ...(a.assumptions ?? [])].some((l) => /perspective undone/i.test(String(l)));
/** The scale an answer MEASURED with on O1's deskewed copy when it states no 'px per m': the copy traced as vectors at
 *  the paper it names ('an A2 sheet at 150 dpi') and the printed 1:N ('scale: 1:200 printed on the sheet'), so
 *  px/m on the copy = dpi / 0.0254 / N. DfE photo tilted 10°: 150 dpi, 1:200 → 29.53 px/m on the copy → 31.71 flat px/m
 *  vs 31.75 from the paper's true size (−0.1 %); the page said 'Scale unknown — set it' under a measured 36.06 m². */
function deskewedScale(a: Answer): Scale | null {
  // also an image traced WITHOUT a perspective step (a scan, Stoa's PNG of the DfE sheet: 'the image's metadata gives an
  // A2 sheet at 257 dpi' + 'scale: 1:200 printed'): the trace is on the image's own pixel grid, so the same px/m holds
  // there (MPH outline 632,692 px² at 50.59 px/m = 247.2 m² vs the answer's 247.73); the caller marks it deskewed only
  // when the answer says 'perspective undone'. Was: 'Scale unknown — set it' under a measured 247.73 m².
  const t = [...(a.trace ?? []), ...(a.assumptions ?? [])].map(String).join("\n");
  const d = t.match(/\b(A[0-6]|ANSI [A-E]|ARCH [A-E]|Tabloid|Letter|Legal)\s+(?:sheet\s+)?at\s+(\d{2,4}(?:\.\d+)?)\s*dpi\b/i);
  const n = t.match(/\bscale:?\s*1\s*:\s*(\d{1,5})\s+printed\b/i) ?? t.match(/\bat printed 1\s*:\s*(\d{1,5})\b/i);
  const dpi = d ? Number(d[2]) : 0, N = n ? Number(n[1]) : 0;
  if (!(dpi > 0 && N >= 1 && N <= 50000)) return null;
  const u = dpi / 0.0254 / N;
  return { unitsPerM: u, label: `1:${N}`, source: `printed 1:${N} on the ${d![1]} sheet it names` };
}
/** the outline's middle lies on the paper found (else the paper found is something else: measure on the photo) */
function onSheet(poly: Pt[], quad: Pt[]): boolean {
  const cx = poly.reduce((a, p) => a + p[0], 0) / poly.length, cy = poly.reduce((a, p) => a + p[1], 0) / poly.length;
  let inside = false;
  for (let i = 0, j = quad.length - 1; i < quad.length; j = i++) {
    const [xi, yi] = quad[i], [xj, yj] = quad[j];
    if (yi > cy !== yj > cy && cx < ((xj - xi) * (cy - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
const tiltNote = (poly: Pt[]) => (!state.pdf && state.flat && onSheet(poly, state.flat.quad) ? " Measured on the sheet made flat (the photo is tilted)." : "");
const PT_PER_M_AT_1 = (72 / 25.4) * 1000; // PDF points per real metre at 1:1
/** The scale printed on a vector PDF page (GET /api/m/pdfscale, src/mobile/pdfscale.py, ~0.1 s), or null. */
const printedMemo = new Map<string, Promise<Scale | null>>();
function pdfPrinted(path: string, page: number): Promise<Scale | null> {
  const k = `${path}|${page}`;
  if (!printedMemo.has(k)) {
    printedMemo.set(k, fetch(`/api/m/pdfscale?path=${encodeURIComponent(path)}&page=${page}`).then((r) => (r.ok ? r.json() : { ok: false }))
      .then((d: { ok: boolean; n?: number; all?: number[]; text?: string }) => {
        if (!d.ok || !d.n || !(d.n > 0)) return null;
        const more = (d.all ?? []).length > 1 ? `; the sheet also prints ${d.all!.slice(1, 3).map((n) => `1:${n}`).join(", ")}` : "";
        const as = d.text && d.text.replace(/\s+/g, "") !== `1:${d.n}` ? ` as ${d.text}` : "";
        return { unitsPerM: PT_PER_M_AT_1 / d.n, label: `1:${d.n}`, source: `printed on the drawing${as}${more}` } as Scale;
      })
      .catch(() => null));
  }
  return printedMemo.get(k)!;
}
/** A scale for measuring a tapped room when nothing better is known: the door swings on the photo (GET /api/m/doorscale)
 *  or, on a vector PDF, W5's door-swing reader (GET /api/doorscale, quarter arcs in the vector paths, rounded box corners
 *  left out; ~0.1-0.5 s). No estimate → null (the visitor sets the scale with two taps). */
async function estimatedScale(): Promise<Scale | null> {
  if (!state.file) return null;
  if (state.pdf) {
    // the scale printed on the sheet first (title block; rounded box corners fooled the door swings: USACE A-101F,
    // printed 1:50, read '1:250 from 20 door swings'); a 404 where the phone plugin is not loaded → door swings
    const printed = await pdfPrinted(state.file.path, state.page);
    if (printed) return printed;
    const p = await doorScale(state.file.path, state.page);
    return p.ok && p.ratio ? doorScaleOf(p) : null;
  }
  const bar = barDone.get(state.file.path);
  if (bar) return bar;
  const d = await doorEstimate(state.file.path);
  if (!d.ok || !d.px_per_m) return null;
  return { unitsPerM: d.px_per_m, label: scaleLabel(d.px_per_m, "image_px"), source: `estimated from ${d.arcs} door swings, a door being about ${d.leaf_m ?? 0.85} m wide` };
}

/** The drawing's 1:N on a vector PDF at scale s (pdf_pt per m), for the wall-line tracer; 0 on photos / no scale. */
const scaleN = (s: Scale | null | undefined): number => (state.pdf && s && s.unitsPerM > 0 ? Math.round((PT_PER_M_AT_1 / s.unitsPerM) * 100) / 100 : 0);

// ---------- 1. pick / take a photo ----------
for (const id of ["cam", "pickf"]) {
  $<HTMLInputElement>(id).addEventListener("change", (e) => {
    const f = (e.target as HTMLInputElement).files?.[0];
    (e.target as HTMLInputElement).value = "";
    if (f) void takeFile(f);
  });
}
$("retake").addEventListener("click", () => reset());
$("fit").addEventListener("click", () => zoomFocus(null));

function reset() {
  state.busy?.abort();
  focus = null;
  Object.assign(state, { case: null, caseSheet: 0, file: null, pdf: false, page: 0, pages: 1, k: 1, map: null, answer: null, question: "", scale: null, fileScale: null, tool: null, taps: [], rooms: [], lines: [], flat: null, barNote: "" });
  caseUi();
  view.clearMarks();
  document.body.dataset.step = "pick";
  $("result").innerHTML = "";
  $("result").hidden = true;
  $("stageNote").textContent = "";
  setTool(null);
  pager();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------- pages of a drawing set (PDF): ‹ 3 / 121 › ----------
function pager() {
  const many = state.pdf && state.pages > 1;
  $("pager").hidden = !many;
  if (!many) return;
  $("pgN").textContent = `${state.page + 1} / ${state.pages}`;
  $<HTMLButtonElement>("pgPrev").disabled = state.page <= 0;
  $<HTMLButtonElement>("pgNext").disabled = state.page >= state.pages - 1;
}
/** Another page: its own scale and rooms (an answer on the page before is put away; asking again goes on from here). */
async function goPage(p: number) {
  if (!state.file || !state.pdf || p < 0 || p >= state.pages || p === state.page) return;
  state.busy?.abort();
  Object.assign(state, { answer: null, scale: null, fileScale: null, taps: [], rooms: [], lines: [] });
  setTool(null);
  view.clearMarks();
  try {
    await showDrawing(p);
  } catch (e) {
    return fail(`We could not show page ${p + 1}: ${(e as Error).message}`, () => void goPage(p));
  }
  ready();
}
$("pgPrev").addEventListener("click", () => void goPage(state.page - 1));
$("pgNext").addEventListener("click", () => void goPage(state.page + 1));
$("pgN").addEventListener("click", () => {
  const v = window.prompt(`Go to page (1–${state.pages})`, String(state.page + 1));
  const n = v ? parseInt(v, 10) : NaN;
  if (Number.isFinite(n)) void goPage(Math.max(1, Math.min(state.pages, n)) - 1);
});

async function takeFile(f: File) {
  reset();
  document.body.dataset.step = "photo";
  const pdf = /\.pdf$/i.test(f.name) || f.type === "application/pdf";
  // Android's file browser can hand over any file: CAD models and other formats belong on the full page
  // (by name first: browsers call a .dxf 'image/vnd.dxf' and a .dwg 'image/vnd.dwg')
  const photoExt = /\.(jpe?g|png|heic|heif|webp|gif|bmp|tiff?)$/i.test(f.name);
  const otherExt = /\.[a-z0-9]{1,5}$/i.test(f.name) && !photoExt;
  if (!pdf && (otherExt || (!photoExt && (!f.type.startsWith("image/") || /vnd\.(dxf|dwg)/i.test(f.type))))) {
    const ext = (f.name.match(/\.([a-z0-9]{1,5})$/i)?.[1] ?? "").toUpperCase();
    return fail(`This page reads photos and PDFs${ext ? `, not ${ext} files` : ""}. Open the full view (top right) for IFC models and DXF drawings, or print the plan to PDF.`, () => reset(), "Choose another file");
  }
  // the photo at once (a local preview), so the screen is never blank while it uploads
  if (!pdf) {
    try {
      await view.show(URL.createObjectURL(f));
    } catch {
      /* HEIC on a browser that cannot show it: the server's JPG comes after the upload */
    }
  }
  progress(["Getting the photo ready"]);
  const prep = pdf ? { file: f, note: "" } : await prepImage(f);
  let info: FileInfo | null = null;
  const t0 = performance.now();
  for (let attempt = 1; attempt <= 3 && !info; attempt++) {
    try {
      info = await upload(prep.file, "auto", { onProgress: (d) => progress([`Uploading ${Math.round(d * 100)} %`], d) });
    } catch (e) {
      const permanent = /limit is|cannot read|could not be|is empty|\b401\b/.test(String((e as Error).message));
      if (permanent) return fail(`The upload did not work: ${(e as Error).message}`, () => reset(), "Choose another file");
      if (attempt === 3) return fail(`The upload did not work: ${(e as Error).message}`, () => void takeFile(f));
      progress([`Connection lost — trying again (${attempt + 1} of 3)…`]);
      await sleep(1200 * attempt);
    }
  }
  state.t.upload = performance.now() - t0;
  state.file = info!;
  state.pdf = pdf;
  if (!pdf) {
    void doorEstimate(info!.path); // ready by the time a room is tapped
    void sheetFlat(info!.path);
  }
  try {
    await showDrawing();
  } catch (e) {
    return fail(`We could not show this file: ${(e as Error).message}`, () => void takeFile(f));
  }
  const notes = [prep.note, info!.upload_note].filter(Boolean);
  $("stageNote").textContent = notes.length ? notes.join(" · ") : "";
  ready();
}

// ---------- 'Try a case' ----------
/** The cards: projects with a 2D sheet this server has (GET /api/m/cases); dataset sheets only when an older server has
 *  no helper; the whole card hides when none is left. */
async function renderCases() {
  const have: Record<string, boolean> | null = await fetch("/api/m/cases").then((r) => (r.ok ? r.json() : null)).then((j) => j?.sheets ?? null).catch(() => null);
  const shown = PROJECTS.filter((p) => {
    const sh = sheetOf(p);
    return !!sh && (have ? !!have[sh.path] : sh.path.startsWith("data/"));
  });
  $("casesCard").hidden = !shown.length;
  $("cases").innerHTML = shown.map((p) => {
    const sh = sheetOf(p)!;
    const k = isScan(p) ? "scan" : /\.pdf$/i.test(sh.path) ? "PDF" : "image";
    return `<button type="button" class="m-case" data-id="${esc(p.id)}">
      <img src="/api/m/case/thumb?path=${encodeURIComponent(sh.path)}" alt="" width="64" height="64" loading="lazy" onerror="this.style.visibility='hidden'">
      <span><b>${esc(p.name)}</b><small>${esc(p.contents || p.facts.join(" · "))}</small></span>
      <span class="m-case-k${k === "scan" ? " scan" : ""}">${esc(k)}</span></button>`;
  }).join("");
}
$("cases").addEventListener("click", (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>(".m-case")?.dataset.id;
  const p = PROJECTS.find((x) => x.id === id);
  if (p) void openCase(p);
});
void renderCases();

/** Chips, the back button and the credit line for the case on screen (or the photo flow's when none is). */
function caseUi() {
  const c = state.case;
  $("chips").innerHTML = (c ? chipsFor(c, state.caseSheet) : CHIPS).map((q) => `<button type="button" class="m-chip" data-q="${esc(q)}">${esc(q)}</button>`).join("");
  $("retake").textContent = c ? "All cases" : "New photo";
  $<HTMLInputElement>("q").placeholder = c ? "Ask your own question…" : "Ask about this drawing…";
  const ifc = c?.sheets.find((s) => /\.ifc$/i.test(s.path));
  const many = c ? sheetsOf(c) : [];
  const ss = $("caseSheets");
  ss.hidden = many.length < 2;
  ss.innerHTML = many.length < 2 ? "" : `<span>Sheet</span>${many.map((s, i) => `<button type="button" class="m-chip" data-sheet="${i}" aria-pressed="${i === state.caseSheet}">${esc(s.label || `Sheet ${i + 1}`)}</button>`).join("")}`;
  const n = $("caseNote");
  n.hidden = !c;
  n.innerHTML = c ? `${esc(c.credit)}${ifc ? ` <a href="./?file=${encodeURIComponent(ifc.path)}">Open the 3D model ›</a>` : ""}` : "";
}

$("caseSheets").addEventListener("click", (e) => {
  const i = Number((e.target as HTMLElement).closest<HTMLElement>("[data-sheet]")?.dataset.sheet);
  if (state.case && Number.isInteger(i) && i !== state.caseSheet) void openCase(state.case, i);
});

async function openCase(c: Project, sheet = 0) {
  const sh = sheetsOf(c)[sheet];
  if (!sh) return;
  reset();
  state.case = c;
  state.caseSheet = sheet;
  caseUi();
  document.body.dataset.step = "photo";
  progress([`Opening “${c.name}”`]);
  const t0 = performance.now();
  try {
    let info: FileInfo;
    const ext = sh.path.slice(sh.path.lastIndexOf(".")).toLowerCase();
    if (sh.path.startsWith("data/")) {
      // a dataset sheet: the API reads it (its licence and credit from the listing)
      const f = (await listFiles("", sh.path.split("/").pop()!.replace(SHEET_EXT, ""), 5, false).catch(() => [])).find((x) => x.path === sh.path);
      info = { ...(f ?? { source: "dataset", siblings: {}, n_questions: 0, license: "", credit: c.credit }), path: sh.path, ext } as FileInfo;
    } else {
      // the server opens it: an upload already there, or an example file handed to its own upload step (same store and
      // answer cache as a visitor's upload)
      const r = await fetch(`/api/m/case?path=${encodeURIComponent(sh.path)}${privacy.on ? `&private=1${privacy.eu ? "&eu=1" : ""}` : ""}`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ?? `the server said ${r.status}`);
      info = (j.how ? { path: j.path, ext, source: "upload", siblings: { [ext]: j.path }, n_questions: 0, license: "", credit: c.credit } : j) as FileInfo;
    }
    if (state.case !== c || state.caseSheet !== sheet) return; // another case, sheet or photo was picked meanwhile
    state.t.upload = 0;
    state.file = info;
    state.pdf = /\.pdf$/i.test(info.path);
    await showDrawing();
    $("stageNote").textContent = `${sh.label || "Sheet"} · opened in ${((performance.now() - t0) / 1000).toFixed(1)} s`;
    ready();
  } catch (e) {
    if (state.case === c) fail(`This case did not open: ${(e as Error).message}`, () => void openCase(c, sheet));
  }
}

/** The server's copy (upright, maybe converted) is what the evidence coordinates refer to: show that one. */
async function showDrawing(page = 0) {
  const f = state.file!;
  focus = null;
  if (state.pdf) {
    progress([page ? `Drawing page ${page + 1}` : "Drawing the sheet"]);
    const { renderPage } = await import("./pdfpage");
    const r = await renderPage(fileUrl(f.path), page);
    state.k = r.scale;
    state.map = r.rotation % 360 ? { toPx: r.toPx, toPt: r.toPt } : null;
    state.page = page;
    state.pages = r.pages;
    await view.show(r.url);
    pager();
  } else {
    await view.show(fileUrl(f.path));
    state.k = 1;
    state.map = null;
    state.pages = 1;
    pager();
  }
}

/** The photo box follows the photo's shape (a landscape photo needs no 58 % of the screen), and is a bit lower while an
 *  answer is shown, so the number stays in view under it. */
/** the phone turned sideways (mobile.css: photo left and sticky, question + answer right) */
const sideways = () => matchMedia("(orientation: landscape) and (max-height: 520px)").matches;
function fitPhotoBox() {
  const el = $("photo");
  if (!view.w) return;
  const vh = window.innerHeight;
  // sideways (mobile.css: photo left, answer right): the photo box takes the screen's height under the top bar
  const side = sideways();
  const max = side ? vh - 124 : document.body.dataset.step === "answer" ? vh * 0.46 : vh * 0.58;
  const want = el.clientWidth * (view.h / view.w) + 8;
  const h = Math.round(Math.max(side ? 160 : 240, Math.min(max, want)));
  if (Math.abs(el.clientHeight - h) < 2) return;
  el.style.height = `${h}px`;
  requestAnimationFrame(() => view.fit(false));
}
/** The box the page zoomed to for the answer (the traced room, the evidence); null = the whole sheet. Turning the
 *  phone refits the photo box, and the answer must stay in view (was: back to the whole sheet, the room a speck). */
let focus: number[] | null = null;
function zoomFocus(b: number[] | null, animate = true) {
  focus = b;
  if (b) view.zoomTo(b, animate);
  else view.fit(animate);
}
// only a real size change (rotation): the URL bar sliding in and out on scroll changes the height, not the width, and
// must not reset a zoomed-in answer
let lastW = window.innerWidth;
window.addEventListener("resize", () => {
  if (Math.abs(window.innerWidth - lastW) < 2) return;
  lastW = window.innerWidth;
  fitPhotoBox();
  const b = focus;
  if (b) requestAnimationFrame(() => requestAnimationFrame(() => focus === b && view.zoomTo(b, false)));
});

function ready() {
  fitPhotoBox();
  document.body.dataset.step = "ask";
  $("result").hidden = true;
  hideProgress();
  $("askCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- 2. ask: chips, typed, voice ----------
caseUi();
$("chips").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest("button");
  if (!b) return;
  const q = b.dataset.q ?? b.textContent ?? "";
  $<HTMLInputElement>("q").value = q;
  if (/this room/i.test(q)) thisRoom();
  else if (/this wall/i.test(q)) thisLength();
  else void askNow();
});
$("askForm").addEventListener("submit", (e) => {
  e.preventDefault();
  void askNow();
});

type SR = { lang: string; interimResults: boolean; continuous: boolean; start(): void; stop(): void; onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null; onend: (() => void) | null; onerror: ((e: { error: string }) => void) | null };
const SRClass = (window as unknown as { SpeechRecognition?: new () => SR; webkitSpeechRecognition?: new () => SR }).SpeechRecognition
  ?? (window as unknown as { webkitSpeechRecognition?: new () => SR }).webkitSpeechRecognition;
let rec: SR | null = null;
if (!SRClass) $("mic").hidden = true;
$("mic").addEventListener("click", () => {
  if (!SRClass) return;
  if (rec) {
    rec.stop();
    return;
  }
  const r = new SRClass();
  rec = r;
  r.lang = navigator.language || "en-US";
  r.interimResults = true;
  r.continuous = false;
  let final = "";
  $("mic").classList.add("on");
  $<HTMLInputElement>("q").placeholder = "Listening… ask your question";
  r.onresult = (e) => {
    let t = "";
    for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
    $<HTMLInputElement>("q").value = t;
    if (e.results[e.results.length - 1].isFinal) final = t;
  };
  r.onerror = (e) => {
    $<HTMLInputElement>("q").placeholder = e.error === "not-allowed" ? "Microphone not allowed: type your question" : "Did not catch that: type or try again";
  };
  r.onend = () => {
    rec = null;
    $("mic").classList.remove("on");
    if (!/Microphone|catch/.test($<HTMLInputElement>("q").placeholder)) $<HTMLInputElement>("q").placeholder = "Ask about this drawing…";
    if (final.trim()) void askNow();
  };
  r.start();
});

/** 'How big is this room?': the visitor points at the room (one tap), we outline it and measure it at the drawing's
 *  scale: the visitor's, else the door-swing estimate (Best estimate · Scale unknown — set it), else we ask for two taps.
 *  No AI model: CPU only, a few seconds. */
function thisRoom() {
  if (!state.file) return;
  state.busy?.abort();
  const q = "How big is this room?";
  state.question = q;
  state.answer = { value: null, unit: "m2", confidence: 0.5, evidence: [], trace: ["tap-the-room: outlined on the photo (flood fill) or, on a vector PDF at a known scale, along its wall lines"], solver: "tap",
    intent: { task: "area", target: "room", scope: "room" }, cost: {} };
  state.rooms = [];
  state.lines = [];
  state.barNote = "";
  if (!state.scale?.user) state.scale = state.fileScale;
  state.t.ask = 0;
  view.clearMarks();
  $("result").hidden = true;
  document.body.dataset.step = "answer";
  setTool("room");
  toolHint("Tap inside the room you mean. We outline it and measure it.");
}

/** 'How long is this wall?': the visitor taps both ends, we measure the line at the drawing's scale (the visitor's, one
 *  read from the drawing, the printed scale bar, else the door swings), in the paper's flat plane on a tilted photo.
 *  No model: the desktop's Measure → Length, with the scale found for them. */
function thisLength() {
  if (!state.file) return;
  state.busy?.abort();
  const q = "How long is this wall?";
  state.question = q;
  state.answer = { value: null, unit: "m", confidence: 0.5, evidence: [], trace: ["tap-the-wall: the line between the visitor's two taps, at the drawing's scale"], solver: "tap",
    intent: { task: "length", target: "wall", scope: "room" }, cost: {} };
  state.rooms = [];
  state.lines = [];
  state.barNote = "";
  if (!state.scale?.user) state.scale = state.fileScale;
  state.t.ask = 0;
  view.clearMarks();
  $("result").hidden = true;
  document.body.dataset.step = "answer";
  setTool("length");
}

/** The visitor's second tap of a length: draw it, find a scale if none yet, show the metres. */
async function measureLine(p: Pt, q: Pt) {
  const file = state.file;
  if (!file) return;
  const t0 = performance.now();
  state.lines.push([p, q]);
  if (!state.scale) {
    view.onTap = null;
    view.addMark({ kind: "line", pts: [p, q].map(toPx), tone: "user" });
    toolHint("Line drawn. Working out the drawing's scale…");
    const est = state.fileScale ?? (await estimatedScale());
    if (state.file !== file) return;
    state.scale = state.scale ?? est;
  }
  state.t.room = performance.now() - t0;
  if (state.answer?.solver === "tap") state.t.ask = state.t.room;
  if (!state.scale) {
    renderResult();
    setTool("scale");
    toolHint(!state.pdf && barMemo.has(file.path) && !barDone.has(file.path)
      ? "Line drawn. Reading the scale bar printed on the drawing… or set the scale now: tap both ends of a length you know."
      : "Line drawn. Now set the scale: tap both ends of a length you know (the scale bar, a printed dimension; a door is about 0.9 m).");
    return;
  }
  setTool(null);
  renderResult();
  if (sideways()) window.scrollTo({ top: 0, behavior: "smooth" });
}

async function askNow() {
  const q = $<HTMLInputElement>("q").value.trim();
  if (!q || !state.file) return;
  $<HTMLInputElement>("q").blur();
  state.busy?.abort();
  const ctl = new AbortController();
  state.busy = ctl;
  state.question = q;
  state.answer = null;
  state.rooms = [];
  state.lines = [];
  state.scale = null;
  state.barNote = "";
  setTool(null);
  view.clearMarks();
  $("result").hidden = true;
  document.body.dataset.step = "asking";
  const t0 = performance.now();
  const tick = setInterval(() => askProgress(q, performance.now() - t0), 500);
  askProgress(q, 0);
  try {
    const a = await askWithRetry(state.file.path, q, ctl.signal);
    state.t.ask = performance.now() - t0;
    if (ctl.signal.aborted) return;
    state.answer = a;
    state.scale = scaleFromAnswer(a, space()) ?? (!state.pdf ? deskewedScale(a) : null);
    if (state.scale && !state.pdf && deskewedAnswer(a)) (state.scale as FlatScale).deskewed = true;
    if (state.scale && !scaleGuessed(state.scale)) state.fileScale = state.scale;
    await showAnswer();
  } catch (e) {
    if (ctl.signal.aborted) return;
    fail(`No answer this time: ${(e as Error).message}`, () => void askNow());
  } finally {
    clearInterval(tick);
    if (state.busy === ctl) state.busy = null;
  }
}

/** POST /api/ask; a dropped connection (phone locked, Wi-Fi switch) is retried twice: the server keeps the answer it
 *  is working on (cached per file + question), so a retry picks it up instead of starting again. */
async function askWithRetry(path: string, question: string, signal: AbortSignal): Promise<Answer> {
  for (let attempt = 1; ; attempt++) {
    try {
      // private mode: the same request as the desktop's (api.ts ask): uploads say private (the middleware switches every
      // hosted step off); the sample (a dataset file) asks the API's pipeline with its hosted steps off
      const priv = privacy.on ? { private: 1, ...(path.startsWith("upload/") ? {} : { solver: "pipeline", solver_args: { gemini: 0, verify: 0 } }) } : {};
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, question, ...priv }), signal });
      if (!r.ok) {
        let d = `${r.status} ${r.statusText}`;
        try {
          d = (await r.json()).detail ?? d;
        } catch {
          /* not json */
        }
        throw Object.assign(new Error(d), { server: true });
      }
      return (await r.json()) as Answer;
    } catch (e) {
      if (signal.aborted || (e as { server?: boolean }).server || attempt >= 3) throw e;
      if (document.hidden) await new Promise<void>((res) => document.addEventListener("visibilitychange", () => res(), { once: true }));
      await sleep(800 * attempt);
    }
  }
}

$("cancel").addEventListener("click", () => {
  state.busy?.abort();
  hideProgress();
  document.body.dataset.step = "ask";
});

// ---------- progress (never a blank screen) ----------
function progress(lines: string[], frac?: number) {
  const p = $("progress");
  p.hidden = false;
  p.innerHTML = `<div class="m-spin"></div><div class="m-ptext">${lines.map((l, i) => `<div class="${i ? "sub" : "main"}">${esc(l)}</div>`).join("")}${frac !== undefined
    ? `<div class="m-bar"><i style="width:${Math.round(frac * 100)}%"></i></div>` : ""}</div>`;
  $("cancel").hidden = document.body.dataset.step !== "asking";
}
function hideProgress() {
  $("progress").hidden = true;
  $("cancel").hidden = true;
}
function askProgress(q: string, ms: number) {
  const s = ms / 1000;
  const step = s < 4 ? "Reading the drawing" : s < 15 ? "Finding rooms, labels and the scale" : s < 45 ? "Looking closer with a vision model" : "Still working: a second model is checking";
  // honest past the usual time: a fresh DfE photo 'How many doors?' took 478 s at load ~290 (the door detector waited
  // 240 s and gave up) while this still said '1–4 minutes'; tapping a room needs no model and answers in seconds
  const hint = s < 15 ? "" : s < 45 ? (state.pdf ? "" : "photos take longer than PDFs")
    : s < 240 ? (state.pdf ? "a big sheet can take a minute or two" : "a photo can take 1–4 minutes")
    : "longer than usual: the server is busy · you can Cancel and tap a room to measure it in seconds";
  progress([step, `“${q}” · ${s.toFixed(0)} s${hint ? ` · ${hint}` : ""}`]);
  $("cancel").hidden = false;
}
function fail(msg: string, retry: () => void, label = "Try again") {
  // behind the booth link's password gate (deploy/gate.py) a request without its cookie gets 401 '{"error": "password
  // required …"}': the gate was restarted with a new password, or the cookie was cleared. Say so and go to its sign-in
  // page (it comes back here), instead of '401 Unauthorized' and a retry that cannot work
  if (/\b401\b|password required/i.test(msg)) {
    msg = "This page needs its password again (the booth link signs you in once a week, or the password was changed).";
    label = "Sign in again";
    retry = () => location.assign(`/__gate/login?next=${encodeURIComponent(location.pathname + location.search)}`);
  } else if (/dynamically imported module|Importing a module script failed|error loading dynamically/i.test(msg)) {
    // a lazy part of the page (pdf.js) is gone: the server was swapped to a new build while this page was open (its
    // file names changed), or the gate's sign-in ran out; a reload fetches the new page (through the sign-in if needed)
    msg = "The page was updated on the server while it was open. Reload it to go on.";
    label = "Reload";
    retry = () => location.reload();
  }
  hideProgress();
  document.body.dataset.step = state.file ? "ask" : "pick";
  const r = $("result");
  r.hidden = false;
  r.innerHTML = `<div class="m-err"><b>Something went wrong</b><p>${esc(msg)}</p><button type="button" class="m-btn primary" id="retry">${esc(label)}</button></div>`;
  $("retry").addEventListener("click", retry);
}

// ---------- 3. the answer ----------
/** a size printed as a label (M-101A 'What size is the suction header?' -> 16 inches): read, never measured at a scale
 *  (it said 'Area, marked on the drawing' + 'Scale unknown — set it') */
const nominal = (a: Answer, q: string) => /\b(what|which) size\b|\bpipe size\b|\bdiameter\b|\bnominal\b/i.test(q) && /^(in|inch|inches|mm|"|dn)$/i.test(String(a.unit ?? "").trim());
const isArea = (a: Answer, q: string) => !nominal(a, q) && (a.intent?.task === "area" || /\b(area|how (big|large)|size|m²|m2|floor space|fläche|pinta-ala|superficie|surface)\b/i.test(q));
const isLength = (a: Answer, q: string) => !nominal(a, q) && (a.intent?.task === "length" || (!isArea(a, q) && /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|breite|länge|leveys|pituus)\b/i.test(q)));
/** what a count counts, in the question's own words ('How many check valves (CV) are shown?' -> 'check valves'); the
 *  parser's target was 'unknown' there ('5 unknowns') and 'kitchen' for 'How many doors does the Kitchen have?' */
function countNoun(a: Answer, q: string, n: number): string | null {
  const m = q.match(/\bhow many\s+(?:of\s+the\s+)?([a-z][a-z\- ]{1,40}?)(?=\s*\(|\s+(?:are|is|does|do|did|on|in|at|shown|there|have|has|per|of|for|can|were|was|drawn|marked)\b|\s*\?|$)/i);
  let w = m?.[1].trim().toLowerCase() ?? "";
  if (!w) {
    // another language: the parser's target only when the question names it ('Wie viele Türen und Fenster' is not 'windows')
    const t = (a.intent?.target ?? "").toLowerCase().replace(/^ifc/, "").replace(/_/g, " ").trim();
    if (!t || /^(unknown|element|drawing)$/.test(t) || !q.toLowerCase().includes(t)) return null;
    return targetNoun(t, n);
  }
  if (n === 1) w = w.replace(/ies$/, "y").replace(/(ss|sh|ch|x)es$/, "$1").replace(/([^s])s$/, "$1");
  return w;
}
const onThis = (e: Evidence) => !!state.file && (e.file === state.file.path || e.file?.split("/").pop() === state.file.path.split("/").pop())
  && (e.page ?? 0) === state.page && (!e.bbox_space || e.bbox_space === space());
const toPx = (p: number[]): Pt => (state.map ? state.map.toPx([p[0], p[1]]) : [p[0] * state.k, p[1] * state.k]);
/** a point on the shown image → evidence units (px on a photo, unrotated pdf_pt on a PDF) */
const toEv = (x: number, y: number): Pt => (state.map ? state.map.toPt([x, y]) : [x / state.k, y / state.k]);

async function showAnswer() {
  const a = state.answer!;
  hideProgress();
  document.body.dataset.step = "answer";
  // a PDF answer on another page: show that page
  const evPage = (a.evidence ?? []).find((e) => e.bbox || e.polygon)?.page ?? 0;
  if (state.pdf && evPage !== state.page) await showDrawing(evPage).catch(() => undefined);
  renderResult();
  fitPhotoBox();
  await new Promise((r) => requestAnimationFrame(r));
  drawEvidence(a);
  // an area answer with room boxes but no outline: trace the outlines on the photo (flood fill, CPU; approximate)
  if (isArea(a, state.question)) void outlineBoxes(a);
  // the photo with the marks AND the number on one screen: the photo shrinks a little, the page scrolls to its top
  requestAnimationFrame(() => (sideways() ? window.scrollTo({ top: 0, behavior: "smooth" }) : $("stage").scrollIntoView({ behavior: "smooth", block: "start" })));
}

function drawEvidence(a: Answer) {
  const marks: Mark[] = [];
  const approx = approxEvidence(a);
  const ev = a.evidence ?? [];
  let n = 0;
  const boxes: number[][] = [];
  ev.forEach((e, i) => {
    if (!onThis(e)) return;
    const ap = approx.has(i) || e.exact === false;
    if (e.polygon && e.polygon.length > 2) marks.push({ kind: "poly", pts: (e.polygon as number[][]).map(toPx), approx: ap });
    else if (e.polyline && e.polyline.length > 1) marks.push({ kind: "line", pts: (e.polyline as number[][]).map(toPx), approx: ap });
    else if (e.bbox && e.bbox.length === 4) {
      const b = e.bbox as number[];
      marks.push({ kind: "box", pts: [toPx([b[0], b[1]]), toPx([b[2], b[3]])], label: n < 30 ? String(++n) : undefined, approx: ap });
    }
    // where it is, for the zoom: the box, else the outline's extent (an answer whose evidence is only a polygon or a
    // measured line was never zoomed to)
    const b = e.bbox && e.bbox.length === 4 ? (e.bbox as number[]) : null;
    const pts = b ? [toPx([b[0], b[1]]), toPx([b[2], b[3]])] : ((e.polygon ?? e.polyline ?? []) as number[][]).map(toPx);
    // the scale bar the answer was measured with stays drawn and listed, but the first zoom is the answer's own place:
    // DfE laundry skirting zoomed out to the room + the bar and showed a 10 m room as a speck
    if (pts.length > 1 && !/^(graphic )?scale ?bar\b|^scale\b/i.test(String(e.note ?? ""))) {
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      boxes.push([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
    }
  });
  view.setMarks(marks);
  // zoom to the evidence when it is one place (a room); the whole sheet when the marks are spread out
  if (boxes.length) {
    const u = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
    zoomFocus((u[2] - u[0]) * (u[3] - u[1]) < view.w * view.h * 0.25 ? u : null);
  } else zoomFocus(null);
}

/** Where the answer came from on the shown page, one entry per mark (numbered like the boxes drawEvidence numbers):
 *  the answer card lists them, tap = zoom there (captain 20:53: 'tap the evidence to zoom there'). Boxes in image px. */
function evPlaces(a: Answer): { box: number[]; label: string; note: string }[] {
  const out: { box: number[]; label: string; note: string }[] = [];
  let n = 0;
  for (const e of a.evidence ?? []) {
    if (!onThis(e)) continue;
    const b = e.bbox && e.bbox.length === 4 ? (e.bbox as number[]) : null;
    const pts = b ? [toPx([b[0], b[1]]), toPx([b[2], b[3]])] : ((e.polygon && e.polygon.length > 2 ? e.polygon : e.polyline ?? []) as number[][]).map(toPx);
    if (pts.length < 2) continue;
    const label = b && !(e.polygon && e.polygon.length > 2) && !(e.polyline && e.polyline.length > 1) && n < 30 ? String(++n) : "";
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    // a reader's aside in brackets is for the trace, not the button: 'dimension 9950 mm (printed; measures 9952 …)'
    const note = calm(String(e.note ?? "")).replace(/\s*\([^)]*\b(measures?|px|pt|conf|confidence|ocr|bbox|score|iou)\b[^)]*\)?/gi, "")
      .replace(/\s+/g, " ").replace(/[\s;,:]+$/, "").trim();
    const what = note.length > 44 ? `${note.slice(0, 42)}…` : note || (label ? "marked here" : "");
    out.push({ box: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], label, note: label ? what : what ? `Outline · ${what}` : "Outline" });
  }
  return out;
}
/** Zoom to one place, with room around it (a 20 × 9 pt label alone would fill the screen). */
function zoomPlace(b: number[]) {
  const padW = Math.max(0, view.w / 10 - (b[2] - b[0])) / 2, padH = Math.max(0, view.h / 10 - (b[3] - b[1])) / 2;
  zoomFocus([b[0] - padW, b[1] - padH, b[2] + padW, b[3] + padH]);
  $("stage").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function outlineBoxes(a: Answer) {
  const ev = a.evidence ?? [];
  const n = scaleN(state.scale);
  const items = ev.map((e, i) => ({ e, i })).filter(({ e }) => onThis(e) && e.bbox && !e.polygon).slice(0, 4)
    .map(({ e, i }) => ({ i, bbox: e.bbox as number[], ...(n > 0 ? { scale_n: n } : {}) }));
  if (!items.length || !state.file) return;
  try {
    const r = await roomShapes(state.file.path, state.page, space(), items);
    if (state.answer !== a) return;
    for (const s of r.shapes) if (s.poly && s.poly.length > 2 && trusted(r, s)) view.addMark({ kind: "poly", pts: s.poly.map(toPx), approx: true });
  } catch {
    /* outlines are a bonus: the boxes stay */
  }
}

/** A text answer that is a list (lines, or 3+ comma-separated items) or too long for the number font: its head line and
 *  items, storey names as people say them ('1.korrus: 50' -> 'Floor 1: 50'); null for a short value. */
function listParts(v: string): { head: string; items: string[] } | null {
  const t = v.trim();
  if (t.length <= 28 && !t.includes("\n")) return null;
  const say = (x: string) => x.replace(/^[-•*]\s*/, "").replace(/^([^:]{1,24}):/, (_m, k: string) => {
    const f = floorName(k);
    return `${f === k.trim() ? k : f[0].toUpperCase() + f.slice(1)}:`;
  }).trim();
  let lines = t.split(/\n+/).map((x) => x.trim()).filter(Boolean);
  if (lines.length === 1) {
    const parts = t.split(/,\s+|;\s+/);
    lines = parts.length >= 3 ? ["", ...parts] : [t];
  }
  const head = /^[-•*]\s/.test(lines[0]) ? "" : lines.shift() ?? "";
  return { head: say(head), items: lines.filter((x) => !/^by (storey|floor|level|type|room)s?:?$/i.test(x)).map(say).slice(0, 12) };
}
function listHtml(l: { head: string; items: string[] }): string {
  return `<div class="m-value m-listval">${l.head ? `<div class="m-listhead">${esc(l.head)}</div>` : ""}${l.items.length
    ? `<ul class="m-list">${l.items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>`;
}

/** The one-sentence explanation under the number. */
function sentence(a: Answer, q: string): string {
  const t = a.intent?.target && !/^(unknown|drawing|floor|area|room)$/i.test(a.intent.target) ? a.intent.target.replace(/_/g, " ") : "";
  const ev = (a.evidence ?? []).filter(onThis);
  const on = state.pdf || state.case ? "the drawing" : "your photo";
  const polys = ev.filter((e) => e.polygon && e.polygon.length > 2).length; // the scale bar's box is not an outline
  const how = ev.length
    ? polys ? `measured from the outline${polys > 1 ? "s" : ""} drawn on ${on}` : ev.length > 1 ? `marked in ${ev.length} places on ${on}` : `marked on ${on}`
    : `nothing marked on ${on}`;
  if (a.intent?.task === "count" || a.unit === "count") {
    const n = typeof a.value === "number" ? a.value : NaN;
    const noun = Number.isFinite(n) ? countNoun(a, q, n) : null;
    return !Number.isFinite(n) ? `${how[0].toUpperCase()}${how.slice(1)}.` : noun ? `${n} ${noun}${/\b(?:in|of|does|do|inside|around) the \w|\bhave\b/i.test(q) ? "" : " on this drawing"}, ${how}.` : `Counted ${n}, ${how}.`;
  }
  if (nominal(a, q)) return `Read off the ${on === "your photo" ? "photo" : "drawing"} (a printed size, not measured), ${how}.`;
  if (isArea(a, q)) return `${t ? `Area of the ${t}` : "Area"}, ${how}.`;
  if (isLength(a, q)) return `${t ? `Length of the ${t}` : "Length"}, ${how}.`;
  return `${how[0].toUpperCase()}${how.slice(1)}.`;
}

function renderResult() {
  const a = state.answer!;
  const q = state.question;
  const r = $("result");
  r.hidden = false;
  const size = isArea(a, q) || isLength(a, q);
  const measure = needsMeasuring(a, q, state.file?.path ?? "");
  const unit = unitLabel(a.unit) || (isArea(a, q) ? "m²" : isLength(a, q) ? "m" : "");
  let big = a.value === null || a.value === undefined ? "" : fmtValue(a);
  const words = typeof a.value === "string" && !/\d/.test(a.value);
  // the desktop's words (Very sure / Fairly sure / Best estimate / Our guess / Rough guess, same 0.4 as the expert
  // workbench): a 0.05 answer must not read like a measurement
  const c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  const scaleOk = !!state.scale && !scaleGuessed(state.scale);
  // a figure READ off the print (AFCEC 'TOTAL COMMON SPACE = 4,479 SF', W5 printedReading, QA 18:56): nothing to
  // measure, no scale needed; the sentence quotes the print
  const printed = printedReading(a) || printedNote(a);
  const tier = costTier(a);
  // the visitor's own measurement (tap the room) at the scale: replaces a missing / unmeasured number; a room the
  // visitor TAPPED is always the number shown (it is what they just asked for by tapping): DfE photo 'Total floor area?'
  // ≈ 3,770 then a kitchen tap showed 3,766.88 m² over the sentence 'The room you tapped …' (34.6 m² on the photo)
  const mine = myMeasure();
  const serverBig = big;
  // a low-confidence number is shown as one: '≈ 3,770' (3 significant digits), not '3,766.88' (DfE photo, @0.05)
  const guessy = !measure && !words && isGuess(a);
  let shownBig = serverBig;
  if (size && typeof a.value === "number" && Math.abs(a.value) >= 100) {
    const v = guessy ? Number(a.value.toPrecision(3)) : a.value;
    shownBig = Math.abs(v) >= 1000 ? v.toLocaleString("en", { maximumFractionDigits: 2 }) : fmtValue({ ...a, value: v }); // areas: 2,500 m²
  }
  // one switch for the number, its sentence, its pill and its unit: the visitor's measurement or the server's answer
  const useMine = !!mine && (!!mine.room || measure || words || !big);
  big = useMine ? mine!.value : shownBig;
  const lowConf = !useMine && guessy;
  const guessNum = ((measure || words) && !useMine) || lowConf;
  // 'Total floor area?' is about the whole drawing: tapping one room does not answer it. From the question's words:
  // the parser's intent.scope is 'drawing' for EVERY question ('How big is the laundry?' too), so it cannot tell
  const whole = wholeQ(q);
  // outlines of this answer on the shown page: a whole-sheet answer with none (or many) cannot be re-measured at a scale
  const outlines = (a.evidence ?? []).filter((e) => onThis(e) && e.polygon && e.polygon.length > 2).length;
  // the server's own number, kept in small type when the visitor's tapped room took the big number's place
  const before = mine?.room && serverBig && !words && typeof a.value === "number" && a.solver !== "tap"
    ? `You asked “${q}”: ${guessy || measure ? "≈ " : ""}${shownBig}${unit ? ` ${unit}` : ""} (${measure ? "not measured" : confidenceWords(a.confidence ?? 0).word.toLowerCase()}).`
    : "";
  const tappedLabel = !!mine?.room && a.solver !== "tap";
  // 'How long is this wall?': the visitor's line, its own button to measure another one
  const tapLine = a.solver === "tap" && isLength(a, q);
  const noNumber = !big || (guessNum && words);
  // a list or a long text answer ('Total spaces (whole model): 285\nBy storey:\n- 1.korrus: 50 …', 'the rooms off the
  // corridor: 2.10 'Zimmer', 2.11 …') in the 64 px number font filled two screens (Maleva 'rooms on each floor')
  const listed = typeof a.value === "string" ? listParts(a.value) : null;
  // what a site engineer can act on: not our plumbing ('recognised as …', 'LLM budget reached (20000 tokens …)')
  const assume = (a.assumptions ?? []).filter((x) => !/^recognised as|LLM budget|tokens or \d+ calls|\bsolver\b|cascade/i.test(x))[0];
  // the marks, one button each: tap = zoom there; 'All' = back to all of them (the visitor's own outline needs none)
  const places = mine?.room || a.solver === "tap" ? [] : evPlaces(a);
  const whereHtml = places.length ? `<div class="m-where"><span class="m-wlabel">Where it is on the ${sheet()} (tap to zoom):</span>${places.slice(0, 8)
    .map((p, i) => `<button type="button" class="m-chip" data-w="${i}">${p.label ? `${p.label} · ` : ""}${esc(p.note)}</button>`).join("")}${places.length > 1
    ? `<button type="button" class="m-chip" data-w="all">${places.length > 8 ? `All ${places.length}` : "All"}</button>` : ""}</div>` : "";
  r.innerHTML = `
    <div class="m-hero">
      <div class="m-q">${esc(tappedLabel ? "The room you tapped" : q)}</div>
      ${listed && !useMine ? listHtml(listed) : big && !(guessNum && words) ? `<div class="m-value"><span class="m-big${big.length > 9 ? " long" : ""}">${guessNum ? "≈ " : ""}${esc(big)}</span>${unit && (!words || useMine) ? `<span class="m-unit">${esc(unit)}</span>` : ""}</div>`
        : `<div class="m-value"><span class="m-big name">${size ? "Not measured yet" : "No answer"}</span></div>`}
      <div class="m-sentence">${esc(useMine ? mine!.how : printed ? `As printed on the ${sheet()}: “${printed}”.` : a.solver === "tap" && state.lines.length ? `The line you tapped is drawn on the ${sheet()}. Set the scale to measure it: tap both ends of the scale bar or of a printed dimension.` : a.solver === "tap" && state.rooms.length ? `The room you tapped is outlined on the ${sheet()}. Set the scale to measure it: tap both ends of the scale bar or of a printed dimension.` : guessNum && size && measure ? (whole ? `An estimate for the whole ${sheet()}, not measured on it. To measure one room, tap it.` : `Not measured on ${sheet() === "drawing" ? "the drawing" : "your photo"} yet: tap the ${tapNoun(q)} to outline it.`) : sentence(a, q))}</div>
      <div class="hero-badges m-badges">
        <span class="conf ${c.cls}" title="calibrated confidence ${Math.round((a.confidence ?? 0) * 100)} %">${esc(useMine ? (scaleGuessed(state.scale) ? "Best estimate" : state.scale?.user ? "Measured by you" : state.pdf ? "Measured on the drawing" : "Measured on the photo") : noNumber ? "No number yet" : c.word)}</span>
        ${size && !scaleOk && !(printed && !useMine) && !(whole && !useMine && outlines !== 1) ? `<button type="button" class="scale-unknown" id="scalePill">Scale unknown — set it</button>` : ""}
        ${size && scaleOk ? `<span class="m-scale">Scale ${esc(state.scale!.label)} · ${esc(state.scale!.source)}</span>` : ""}
        <span class="m-tier t-${tier.tier}" title="${esc(tier.why)}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= (tier.tier === "cheap" ? 1 : tier.tier === "mid" ? 2 : 3) ? "on" : ""}"></i>`).join("")}</span>${esc(tier.word)}</span>
        ${dataLineHtml(a, esc)}
      </div>
      ${useMine ? "" : scopeLines(a).map((l) => `<div class="m-assume m-scope">${esc(calm(l)).slice(0, 200)}</div>`).join("")}
      ${mine?.room && barNote() ? `<div class="m-assume">${esc(barNote())}</div>` : ""}
      ${before || tappedLabel ? `<div class="m-assume m-before">${esc(before || `You asked “${q}”.`)}</div>` : ""}
      ${assume && c.cls.startsWith("est") && !mine?.room ? `<div class="m-assume">Assumed: ${esc(calm(assume)).slice(0, 220)}</div>` : ""}
      ${(a.missing ?? []).length && c.cls.startsWith("est") && !mine?.room ? `<div class="m-assume">To be sure: ${esc(calm((a.missing ?? [])[0])).slice(0, 160)}</div>` : ""}
    </div>
    ${whereHtml}
    <div class="m-actions">
      ${size && tapLine ? `<button type="button" class="m-btn" id="lineBtn">Measure another length</button>` : size ? `<button type="button" class="m-btn" id="roomBtn">${state.rooms.length ? `Tap another ${tapNoun(q)}` : `Tap the ${tapNoun(q)} to outline it`}</button>` : ""}
      ${size ? `<button type="button" class="m-btn" id="scaleBtn">${state.scale?.user ? "Set the scale again" : "Set the scale"}</button>` : ""}
      <button type="button" class="m-btn ghost" id="again">Ask something else</button>
    </div>
    <div class="m-time">${useMine && mine!.room && a.solver !== "tap" && state.t.room ? `Outlined in <b>${(state.t.room / 1000).toFixed(1)} s</b>`
      : `Answered in <b>${(state.t.ask / 1000).toFixed(1)} s</b>`}${state.t.upload ? ` · upload ${(state.t.upload / 1000).toFixed(1)} s` : ""}${a.cached ? " · from memory" : ""}</div>
    ${!state.pdf || state.case ? "" : state.file?.credit && state.file.source !== "upload" ? `<div class="m-credit">${esc(state.file.credit)}</div>` : ""}`;
  r.querySelectorAll<HTMLElement>("[data-w]").forEach((b) => b.addEventListener("click", () => {
    const w = b.dataset.w;
    if (w === "all") {
      const u = [Math.min(...places.map((p) => p.box[0])), Math.min(...places.map((p) => p.box[1])), Math.max(...places.map((p) => p.box[2])), Math.max(...places.map((p) => p.box[3]))];
      zoomPlace(u);
    } else if (places[Number(w)]) zoomPlace(places[Number(w)].box);
  }));
  $("scalePill")?.addEventListener("click", () => setTool("scale"));
  $("scaleBtn")?.addEventListener("click", () => setTool("scale"));
  $("roomBtn")?.addEventListener("click", () => setTool("room"));
  $("lineBtn")?.addEventListener("click", () => setTool("length"));
  $("again").addEventListener("click", () => {
    document.body.dataset.step = "ask";
    $<HTMLInputElement>("q").value = "";
    $<HTMLInputElement>("q").focus();
  });
}

/** What the number includes and leaves out (captain 21:13, engineer research: quantities are NET with the rule shown,
 *  'skirting = perimeter − openings'): Answer.includes / .excludes when the server sets them, else an assumption or
 *  trace line that starts with 'Includes:' / 'Excludes:' (the shape told to W5 / G1 at 21:20). At most two lines. */
function scopeLines(a: Answer): string[] {
  const x = a as Answer & { includes?: unknown; excludes?: unknown };
  const out: string[] = [];
  for (const [k, v] of [["Includes", x.includes], ["Excludes", x.excludes]] as const) {
    const t = (Array.isArray(v) ? v.join(", ") : typeof v === "string" ? v : "").trim();
    if (t) out.push(`${k}: ${t}`);
  }
  if (!out.length)
    for (const l of [...(a.assumptions ?? []), ...(a.trace ?? [])].map(String)) {
      const m = l.match(/^\s*(includes|excludes)\s*:\s*(.+)$/i);
      if (m && !out.some((o) => o.toLowerCase().startsWith(m[1].toLowerCase()))) out.push(`${m[1][0].toUpperCase()}${m[1].slice(1).toLowerCase()}: ${m[2].trim()}`);
    }
  return out.slice(0, 2);
}

/** A tapped room at the door-swing guess while the printed scale bar is still being read, or after it replaced it. */
function barNote(): string {
  if (state.pdf || !state.file) return "";
  if ((state.scale as Partial<BarScale> | null)?.bar) return state.barNote;
  return scaleGuessed(state.scale) && barMemo.has(state.file.path) && !barDone.has(state.file.path)
    ? "Reading the scale bar printed on the drawing to measure it exactly…" : "";
}

/** An evidence note that QUOTES a printed figure equal to the answer: E1's area_table writes 'TOTAL COMMON SPACE =
 *  4,479 SF (printed area table)' (AFCEC PDF, 2.4 s), which W5's printedReading (it looks for "read: '…'") misses, so the
 *  page said 'Scale unknown — set it' under a printed total. The quoted part, else ''. */
function printedNote(a: Answer): string {
  if (typeof a.value !== "number" || !(a.value > 0)) return "";
  const v = a.value;
  for (const e of a.evidence ?? []) {
    const m = (e.note ?? "").match(/^(.{0,80}?(\d[\d,]*(?:\.\d+)?)\s*(SF|sq\.?\s*ft|ft²|ft2|m²|m2|sqm))(?!\w)[^\n]*\bprinted\b/i);
    if (!m) continue;
    const n = Number(m[2].replace(/,/g, ""));
    const asM2 = /^(sf|sq|ft)/i.test(m[3]) ? n * 0.09290304 : n;
    if (n > 0 && (Math.abs(v - n) <= 0.005 * n || Math.abs(v - asM2) <= 0.01 * asM2)) return m[1].trim();
  }
  return "";
}
/** A small, thin, rectangular outline is the box drawn around a label, not a room: USACE S-100F, a tap on the
 *  '150 SLAB-ON-GRADE …' callout outlined its frame (1.43 m² at 1:50, 4.6 : 1) instead of the 61.69 m² slab. A hint,
 *  not a rejection: a real small room (a WC, a duct) can look like it, and the visitor sees which one it is. */
function labelFrame(poly: Pt[], m2: number): boolean {
  if (!(m2 > 0 && m2 < 2.5) || poly.length > 10) return false;
  const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys);
  return w > 0 && h > 0 && Math.max(w / h, h / w) >= 2.5 && polyArea(poly) / (w * h) >= 0.9;
}
/** 'Total floor area?' is about the whole drawing: tapping one room does not answer it, nor does one outline at the
 *  visitor's scale. From the question's words: the parser's intent.scope is 'drawing' for EVERY question. */
const wholeQ = (q: string) => /\b(total|whole|entire|overall|gross|all|every|sum|altogether|in total|yhteensä|gesamt)\b/i.test(q) && !/\b(this|that|the) room\b/i.test(q);
/** The visitor's tapped room (area) at the current scale, or the answer's own outline / line at the visitor's scale. */
function myMeasure(): { value: string; how: string; room?: boolean } | null {
  const a = state.answer;
  const s = state.scale;
  if (!a || !s) return null;
  const length = isLength(a, state.question);
  const ln = a.solver === "tap" ? state.lines[state.lines.length - 1] : undefined;
  if (ln) {
    const m = lengthMetres(ln, s);
    return { room: true, value: fmtM(m), how: `The line you tapped on the ${sheet()}, at ${s.user ? "your scale" : `${s.label} (${s.source})`}.${imperial(s) ? ` That is ${feet(m)} ft.` : ""}${tiltNote(ln)} Check that its ends sit on the wall's ends.` };
  }
  const last = state.rooms[state.rooms.length - 1];
  if (last) {
    const m2 = areaM2(last.poly, s);
    const traced = last.method === "wall lines" ? "traced along its walls (doorways closed)" : "outlined";
    return length ? null : { room: true, value: fmtM(m2), how: `The room you tapped, ${traced} on the ${sheet()}, at ${s.user ? "your scale" : `${s.label} (${s.source})`}.${imperial(s) ? ` That is ${sqft(m2)} sq ft.` : ""}${tiltNote(last.poly)} The outline is traced: check it.${labelFrame(last.poly, m2) ? " It looks like the frame around a label: to measure the room around it, tap an empty spot of the room." : ""}` };
  }
  if (!s.user) return null;
  const ev = (a.evidence ?? []).filter(onThis);
  // a total over several outlines is not the first outline's area at your scale (one would replace the total)
  const polys = ev.filter((e) => e.polygon && e.polygon.length > 2);
  if (!length && polys.length > 1 && wholeQ(state.question)) return null;
  const poly = polys[0]?.polygon as Pt[] | undefined;
  const line = ev.find((e) => e.polyline && e.polyline.length > 1)?.polyline as Pt[] | undefined;
  const m2 = poly ? areaM2(poly, s) : 0;
  const m = line ? lengthMetres(line, s) : 0;
  if (!length && poly) return { value: fmtM(m2), how: `The outline on the ${sheet()} at your scale (${s.label}).${imperial(s) ? ` That is ${sqft(m2)} sq ft.` : ""}${tiltNote(poly)}` };
  if (length && line) return { value: fmtM(m), how: `The line on the ${sheet()} at your scale (${s.label}).${imperial(s) ? ` That is ${feet(m)} ft.` : ""}${tiltNote(line)}` };
  return null;
}

// ---------- 4. tools: two-tap scale, tap-the-room ----------
function setTool(t: null | "scale" | "room" | "length") {
  state.tool = t;
  state.taps = [];
  view.clearMarks("user");
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r.poly) });
  // the scale the rooms were measured at, on the photo: the printed bar's two ends
  const sb = state.scale as Partial<BarScale> | null;
  for (const l of state.lines) view.addMark({ kind: "line", pts: l.map(toPx), tone: "user", label: lineTag(l) });
  if (sb?.bar && sb.pts && (state.rooms.length || state.lines.length) && !state.pdf) view.addMark({ kind: "line", pts: sb.pts.map(toPx), tone: "user", label: sb.bar });
  view.onTap = t ? onTap : null;
  document.body.dataset.tool = t ?? "";
  const bar = $("toolBar");
  bar.hidden = !t;
  $("scaleSheet").hidden = true;
  if (t === "scale") toolHint("Tap one end of a length you know, then the other end (pinch to zoom in first for precision). A printed dimension or the scale bar is best; a door is about 0.9 m.");
  if (t === "room") toolHint(`Tap inside the room you mean. We outline it on the ${sheet()}.`);
  if (t === "length") toolHint(`Tap one end of the wall, then the other end (pinch to zoom in first). We measure the line on the ${sheet()}.`);
  if ((t === "room" || t === "length") && !state.pdf && state.file) void barScale(state.file.path); // the printed scale bar, read meanwhile
  if (t) $("photo").scrollIntoView({ behavior: "smooth", block: "center" });
}
/** what the tap tool outlines, in the question's words: a slab / zone / roof is an 'area', anything else a 'room' */
const tapNoun = (q: string) => (/\b(slab|zone|region|footprint|deck|roof|site|plot|pad|paving|area of the \d)/i.test(q) ? "area" : "room");
function toolHint(t: string) {
  $("toolHint").textContent = t;
}
$("toolDone").addEventListener("click", () => setTool(null));

function roomTag(poly: Pt[]): string | undefined {
  const s = state.scale;
  if (!s) return undefined;
  const m2 = areaM2(poly, s);
  return `${fmtM(m2)} m²${imperial(s) ? ` · ${sqft(m2)} sq ft` : ""}`;
}
function lineTag(l: Pt[]): string | undefined {
  const s = state.scale;
  if (!s) return undefined;
  const m = lengthMetres(l, s);
  return `${fmtM(m)} m${imperial(s) ? ` · ${feet(m)} ft` : ""}`;
}
/** A scale in feet: typed by the visitor in feet (80', 12'-6", 80 ft) or printed in feet-inches (1/4" = 1'-0", 1" = 40').
 *  US sheets answer in square feet (Stoa's AFCEC dorm tables), so the measured value gets its sq ft / ft too. */
const imperial = (s: Scale | null): boolean => !!s && /\d\s*(?:'|’|′|ft\b|feet|foot)|\d\s*(?:"|″|”)/.test(`${s.label} ${s.source}`);
const sqft = (m2: number): string => Math.round(m2 / 0.09290304).toLocaleString("en-US");
const feet = (m: number): string => (m / 0.3048).toLocaleString("en-US", { maximumFractionDigits: 1 });

function onTap(x: number, y: number) {
  const ev: Pt = toEv(x, y);
  if (state.tool === "scale") {
    state.taps.push(ev);
    view.addMark({ kind: "dot", pts: [[x, y]], tone: "user" });
    if (state.taps.length === 1) toolHint("Now tap the other end.");
    if (state.taps.length === 2) {
      view.addMark({ kind: "line", pts: state.taps.map(toPx), tone: "user" });
      openScaleSheet();
    }
    if (state.taps.length > 2) {
      state.taps = [ev];
      view.clearMarks("user");
      view.addMark({ kind: "dot", pts: [[x, y]], tone: "user" });
      toolHint("Now tap the other end.");
    }
  } else if (state.tool === "room") void traceRoom(ev);
  else if (state.tool === "length") {
    state.taps.push(ev);
    view.addMark({ kind: "dot", pts: [[x, y]], tone: "user" });
    if (state.taps.length === 1) toolHint("Now tap the other end.");
    else {
      const [p, q] = state.taps;
      const [a, b] = [toPx(p), toPx(q)];
      if (Math.hypot(b[0] - a[0], b[1] - a[1]) < 3) {
        state.taps = [p];
        return;
      }
      void measureLine(p, q);
    }
  }
}

function openScaleSheet() {
  const sh = $("scaleSheet");
  sh.hidden = false;
  const inp = $<HTMLInputElement>("scaleM");
  inp.value = "";
  toolHint("How long is that in reality?");
  setTimeout(() => inp.focus(), 50);
}
$("scaleForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const typed = $<HTMLInputElement>("scaleM").value;
  const m = lengthM(typed, $("scaleUnit").textContent === "ft" ? "ft" : "m");
  if (m) setScaleFrom(m, /ft|'|’|′|feet|foot/i.test(typed) || $("scaleUnit").textContent === "ft" ? typed.trim() : "");
});
// US sheets print feet (Stoa's AFCEC / USACE examples: a 0-80' scale bar): the unit next to the box switches m <-> ft
$("scaleUnit").addEventListener("click", () => {
  const u = $("scaleUnit");
  u.textContent = u.textContent === "ft" ? "m" : "ft";
  $<HTMLInputElement>("scaleM").placeholder = u.textContent === "ft" ? "e.g. 80 or 12'-6\"" : "e.g. 3.40";
});

/** A typed real length in metres: '3.40', '3,40' (in `unit`), or with its own unit: 80', 80 ft, 12'-6", 12' 6", 150",
 *  2400 mm, 340 cm, 3.4 m. null when it cannot be read. */
export function lengthM(text: string, unit: "m" | "ft"): number | null {
  const t = text.trim().toLowerCase().replace(/(\d),(\d)/g, "$1.$2").replace(/[’′]/g, "'").replace(/[”″]|''/g, '"');
  const fi = t.match(/^(\d+(?:\.\d+)?)\s*(?:'|ft|feet|foot)\s*-?\s*(?:(\d+(?:\.\d+)?)\s*(?:"|in|inch|inches)?)?$/);
  if (fi) return (Number(fi[1]) + Number(fi[2] ?? 0) / 12) * 0.3048;
  const m = t.match(/^(\d+(?:\.\d+)?)\s*(mm|cm|m|"|in|inch|inches)?$/);
  if (!m) return null;
  const v = Number(m[1]);
  const k = m[2] === "mm" ? 0.001 : m[2] === "cm" ? 0.01 : m[2] === "m" ? 1 : m[2] ? 0.0254 : unit === "ft" ? 0.3048 : 1;
  return v > 0 ? v * k : null;
}
$("scaleSheet").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-m]");
  if (b) setScaleFrom(Number(b.dataset.m));
});

function setScaleFrom(m: number, asTyped = "") {
  if (!(m > 0) || state.taps.length < 2) return;
  const [p, q] = state.taps;
  const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
  if (d < 3) return;
  const u = d / m;
  state.scale = { unitsPerM: u, label: scaleLabel(u, space()), source: `set by you: the line you tapped is ${asTyped ? `${/^[\d.,]+$/.test(asTyped) ? `${asTyped} ft` : asTyped} = ` : ""}${+m.toFixed(asTyped ? 2 : 3)} m`, user: true, pts: [p, q], m } as Scale;
  $("scaleSheet").hidden = true;
  setTool(null);
  // redraw the visitor's rooms with their m², and the answer with its numbers at the new scale
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r.poly) });
  if (state.answer) renderResult();
  zoomToRoom();
}
/** Zoom to the room the visitor outlined last (the marks are the proof: show them). */
function zoomToRoom() {
  const r = state.rooms[state.rooms.length - 1];
  if (!r) return;
  const px = r.poly.map(toPx);
  const xs = px.map((p) => p[0]), ys = px.map((p) => p[1]);
  zoomFocus([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
}

/** W5's flood fill (web/server/room_shape.py: shapes without a 'method') renders a ROTATED PDF page with its /Rotate
 *  but reads and returns pdf_pt as if it were unrotated (KNOWLEDGE 18:15: USACE S-100F tap → a ragged region across
 *  the slab lines that still holds the tap, 18.2 m²). On a rotated page only E1's wall-line tracer ('method' set) is
 *  trusted, until the reply says it was fixed ('derotated: true', asked of W5 18:45). */
const trusted = (r: object, s?: { method?: string }) => !state.map || !!s?.method || !!(r as { derotated?: boolean }).derotated;

/** Why an outline is NOT the room the visitor tapped (null = it is plausible): it must hold the tap (or pass within a
 *  wall's width of it) and must not be most of the sheet (half its area, or most of both its sides). W5's flood fill
 *  (web/server/room_shape.py) reads a ROTATED PDF page in the rotated space (KNOWLEDGE 18:15): USACE M-101A tap → the
 *  whole page, S-100F → a region elsewhere; a leak through an open doorway on a photo fills the paper around the plan. */
function notThisRoom(poly: Pt[], tap: Pt): string | null {
  const px = poly.map(toPx), [tx, ty] = toPx(tap);
  let inside = false;
  for (let i = 0, j = px.length - 1; i < px.length; j = i++) {
    const [xi, yi] = px[i], [xj, yj] = px[j];
    if (yi > ty !== yj > ty && tx < ((xj - xi) * (ty - yi)) / (yj - yi) + xi) inside = !inside;
  }
  if (!inside) {
    const tol = 0.01 * Math.max(view.w, view.h);
    const near = px.some(([x1, y1], i) => {
      const [x2, y2] = px[(i + 1) % px.length], dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy;
      const u = L ? Math.max(0, Math.min(1, ((tx - x1) * dx + (ty - y1) * dy) / L)) : 0;
      return Math.hypot(x1 + u * dx - tx, y1 + u * dy - ty) <= tol;
    });
    if (!near) return "does not hold the tap";
  }
  // a leak around the building fills the paper: on a keystoned DfE photo 66 % of the image, 89 % × 88 % of its sides
  // (8,652 m²); a room on a photo of a whole sheet is a few %
  const share = polyArea(px) / (view.w * view.h);
  if (share > 0.5) return `${Math.round(share * 100)} % of the ${sheet()}`;
  const xs = px.map((p) => p[0]), ys = px.map((p) => p[1]);
  if (Math.max(...xs) - Math.min(...xs) > 0.85 * view.w && Math.max(...ys) - Math.min(...ys) > 0.85 * view.h) return `spans the whole ${sheet()}`;
  return null;
}

async function traceRoom(ev: Pt) {
  if (!state.file) return;
  toolHint("Outlining the room…");
  const t0 = performance.now();
  view.addMark({ kind: "dot", pts: [toPx(ev)], tone: "user" });
  const r = Math.max(2, 4 / state.k);
  try {
    const file = state.file;
    // a vector PDF: its scale first (the visitor's, one read from the sheet, else its door swings, ~0.3 s), so E1's
    // wall-line tracer closes door swings at the doorway (DfE laundry 7.03 m² vs 5.68 by flood fill, gold 7.0)
    if (state.pdf && !state.scale) {
      const est = state.fileScale ?? (await estimatedScale());
      if (state.file !== file) return;
      state.scale = state.scale ?? est;
    }
    const n = scaleN(state.scale);
    const res = await roomShapes(file.path, state.page, space(), [{ i: 0, bbox: [ev[0] - r, ev[1] - r, ev[0] + r, ev[1] + r], ...(n > 0 ? { scale_n: n } : {}) }]);
    if (state.file !== file) return; // a new photo was picked meanwhile
    const poly = res.shapes[0]?.poly as Pt[] | null | undefined;
    if (!poly || poly.length < 3) {
      toolHint("No closed room there (an open door or a faint wall?). Tap closer to the middle of the room.");
      return;
    }
    const off = trusted(res, res.shapes[0]) ? notThisRoom(poly, ev) : "flood fill on a rotated page";
    if (off) {
      console.info("w6: outline rejected:", off, res.shapes[0]?.method);
      toolHint("Could not find this room's walls there (the outline ran off the room). Tap closer to the middle of the room, or measure a length with the scale tool.");
      return;
    }
    state.rooms.push({ poly, method: res.shapes[0]?.method });
    if (!state.scale && !state.fileScale) {
      // the door-swing estimate started at upload; on a big sheet it takes a second, sharper look (~10 s)
      view.onTap = null;
      view.addMark({ kind: "poly", pts: poly.map(toPx), tone: "user" });
      toolHint("Room outlined. Working out the scale from the door swings…");
    }
    if (!state.scale) {
      const est = state.fileScale ?? (await estimatedScale());
      if (state.file !== file) return;
      state.scale = state.scale ?? est; // the visitor may have set one meanwhile
    }
    // smaller than any room at the known scale: a gap between lines or a symbol (USACE M-101A piping plan: a tap between
    // pipes outlined 0.05 m², '0 sq ft')
    const tiny = state.scale && !scaleGuessed(state.scale) ? areaM2(poly, state.scale) : Infinity;
    if (tiny < 0.3) {
      state.rooms.pop();
      setTool("room");
      toolHint(`That spot is not inside a room (a gap between lines or a symbol: ${tiny < 0.1 ? tiny.toFixed(2) : tiny.toFixed(1)} m²). Tap the middle of a room.`);
      return;
    }
    setTool(null); // the outline, with its m² tag once the scale is known
    state.t.room = performance.now() - t0; // tap → outline (+ scale): the time a tapped room's number took
    if (state.answer?.solver === "tap") state.t.ask = state.t.room;
    if (!state.scale) {
      renderResult();
      setTool("scale");
      toolHint(!state.pdf && barMemo.has(file.path) && !barDone.has(file.path)
        ? "Room outlined. Reading the scale bar printed on the drawing… or set the scale now: tap both ends of a length you know."
        : "Room outlined. Now set the scale: tap both ends of a length you know (the scale bar, a printed dimension; a door is about 0.9 m).");
      return;
    }
    renderResult();
    zoomToRoom();
    if (sideways()) window.scrollTo({ top: 0, behavior: "smooth" }); // the number heads the right column
  } catch (e) {
    toolHint(`Could not outline it: ${(e as Error).message}`);
  }
}

// ---------- private mode (captain 18:53): the photo never leaves our server; the desktop's switch, key and URL flag ----------
// (localStorage 'be.private', ?private=1), so a phone opened from the desktop's link keeps the visitor's choice. Uploads
// and questions carry private=1 (W5's middleware turns every hosted step off; its answer cache keys on it).
let privTimer = 0;
function privNote(on: boolean) {
  const n = $("privNote");
  n.innerHTML = (on
    ? `<b>Private mode is on.</b> Your photo and questions stay on our server: no Gemini or NVIDIA steps, only our own readers and local models.<small>${esc(tradeoffLine())}</small>`
    : `<b>Private mode is off.</b> When we are unsure, a step may ask Gemini (Google) or NVIDIA Nemotron for a second reading. Each answer says where its data went.`)
    + `<button type="button" class="pn-x" aria-label="Close">✕</button>`;
  n.hidden = false;
  n.querySelector(".pn-x")?.addEventListener("click", () => (n.hidden = true));
  window.clearTimeout(privTimer);
  privTimer = window.setTimeout(() => (n.hidden = true), 9000);
}
function setPrivate(on: boolean, tell = true) {
  privacy.on = on;
  const b = $("privBtn");
  b.setAttribute("aria-checked", String(on));
  b.title = on ? "Private mode is on: your photo never leaves our server. Tap to allow Gemini / NVIDIA second readings."
    : "Private mode: your photo never leaves our server (no Gemini or NVIDIA steps)";
  try {
    localStorage.setItem("be.private", on ? "1" : "0");
  } catch {
    /* storage off: the URL still says it */
  }
  const url = new URL(location.href);
  if (on) url.searchParams.set("private", "1");
  else url.searchParams.delete("private");
  history.replaceState(null, "", url);
  if (tell) privNote(on);
}
$("privBtn").addEventListener("click", () => setPrivate(!privacy.on));
setPrivate((() => {
  const u = new URL(location.href).searchParams.get("private");
  if (u !== null) return u === "1";
  try {
    return localStorage.getItem("be.private") === "1";
  } catch {
    return false;
  }
})(), false);

document.body.dataset.step = "pick";
