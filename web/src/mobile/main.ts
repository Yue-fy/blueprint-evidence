/**
 * The on-site phone page (web/mobile.html, W6): take a photo of a printed plan, ask, see the answer drawn on the photo.
 * Same /api as the customer UI (upload → ask → shapes), same Apple-like tokens (../style.css), same answer wording
 * helpers (../customer, ../card, ../measure). One screen: photo → question (chips / typed / voice) → big number, one
 * sentence, status pills, evidence on the photo, and a two-tap scale tool when the scale is unknown.
 */
import "@fontsource-variable/inter";
import "../style.css";
import "./mobile.css";
import { doorScale, fileUrl, glance, listFiles, privacy, roomShapes, upload, type AskCtx, type Glance, type RoomShape } from "../api";
import { dataLineHtml, tradeoffLine } from "../governance";
import { fmtValue, unitLabel } from "../card";
import { approxEvidence, calm, confidenceWords, conventionOf, costTier, implausible, isGuess, levelRead, needsMeasuring, printedReading, rivalReading, scopeOf, sizeCheck, targetNoun } from "../customer";
import { doorScaleOf, fmtM, scaleFromAnswer, scaleGuessed, scaleLabel, type Scale } from "../measure";
import type { Answer, Evidence, FileInfo } from "../types";
import { floorName, PROJECTS, type Project } from "../projects";
import { prepImage } from "./prep";
import { centroid, PhotoView, polyArea, polyLength, type Mark } from "./view";
import type { Word } from "./pdfpage";

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
const isMore = (p: Project) => (p as { more?: boolean }).more === true || (p as { group?: string }).group === "more";
/** a scanned print shown as one SHEET of a project (the school's 'Level 00 (scan)'): its button says so */
const scanSheet = (s: { label?: string; badge?: string }) => s.badge === "scan" && !/\bscan/i.test(s.label ?? "");
// AREAS > LENGTHS > COUNTS (Stoa, Sat 12:40). 'How many doors?' was the third chip until cycle 7: on a fresh DfE phone
// photo it took 190 s (load ~70) to 478 s (load ~290) and said '≈ 67 Rough guess', nothing marked (the sheet has ~104);
// a typed door question still works. Both tap chips need no model: CPU only, seconds, the line / outline is the proof.
const CHIPS = ["How big is this room?", "How long is this wall?", "Total floor area?"];
/** chips for a sheet that is not a floor plan (an elevation, a section: heights and windows, not rooms) */
const SIDE_CHIPS = ["What does this drawing show?", "How tall is the building?", "How many windows are shown?"];

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
  rooms: [] as Room[],
  /** the visitor turned the pages of this upload (a multi-page PDF): its questions are about the page on screen, as the
   *  desktop's (main.ts onPage); '' = the solver picks the page */
  paged: "",
  /** lines the visitor measured (tap both ends: 'How long is this wall?'), in evidence units */
  lines: [] as Pt[][],
  /** a photo of a printed sheet with perspective: the homography photo px → the paper's flat plane (row-major 3x3) and
   *  k = flat length / photo length on average over the sheet; tap-the-room measures there (null = on the photo) */
  flat: null as null | { H: number[]; k: number; how: string; quad: Pt[]; w: number; h: number },
  /** a photo of a sheet we have on file (GET /api/m/match): its stored copy, the homography photo px → that sheet's units
   *  and back, the sheet's space and a title for the card ('Special school · Level 00') */
  match: null as null | { path: string; H: number[]; Hi: number[]; space: "image_px" | "pdf_pt"; title: string; close?: boolean },
  /** the answer on screen was asked on the matched drawing on file (its title), its marks mapped onto the photo */
  answeredOn: "",
  busy: null as AbortController | null,
  /** after the printed scale bar replaced the door-swing guess: what the guess had said (the number changed under the
   *  visitor's eyes, so the page says why) */
  barNote: "",
  /** the visitor's sheet is not a floor plan (an answer's sheet check said 'elevation', 'section' …; UX M22): its chips */
  notPlan: "",
  t: { upload: 0, ask: 0, room: 0 },
};
(window as unknown as { __w6: typeof state }).__w6 = state; // playwright reads timings from here

const view = new PhotoView($("photo"));
/** where the long-press menu was opened (null = closed) */
let pressAt: Pt | null = null;
/** test hook (playwright): the screen point of an image point */
(window as unknown as { __w6at: (x: number, y: number) => number[] }).__w6at = (x, y) => view.toClient(...toPx([x, y]));
/** test hook: zoom the sheet to a box (image px / pdf_pt), as a visitor's pinch would */
(window as unknown as { __w6zoom: (b: number[]) => void }).__w6zoom = (b) => {
  const [x0, y0] = toPx([b[0], b[1]]), [x1, y1] = toPx([b[2], b[3]]);
  view.zoomTo([Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)], false);
};
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
// ---------- the printed words of a picture, in its px (GET /api/m/words, src/mobile/words.py) ----------
// for the furniture check of tap-the-room (a store has its name inside, a table none): from the upload's traced copy
// (P1's <stem>.vec6.pdf: Stoa's DfE Level 00 PNG 249 words, ~0.1 s), else its OCR reading if already on disk, else null.
// A miss is not remembered (the reading may land later: the scale bar reads the photo's text layer).
const wordsMemo = new Map<string, Promise<Word[] | null>>();
function imageWords(path: string): Promise<Word[] | null> {
  if (!wordsMemo.has(path)) {
    const p = fetch(`/api/m/words?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false })).catch(() => ({ ok: false }))
      .then((d: { ok: boolean; words?: [number, number, string, number?, number?][] }) => (d.ok && d.words
        ? d.words.map(([x, y, t, hw, hh]) => ({ at: [x, y] as Pt, t: String(t ?? ""), hw: Number(hw) || 0, hh: Number(hh) || 0 })) : null));
    wordsMemo.set(path, p);
    void p.then((w) => w || wordsMemo.delete(path));
  }
  return wordsMemo.get(path)!;
}
// ---------- the scale bar printed on the photo (GET /api/m/barscale, src/mobile/barscale.py) ----------
// A printed fact, not a guess: tap-the-room measures at it before the door swings (DfE photo: bar 31.50 px/m, by hand
// 31.4, door swings 29.8). It reads the photo's text layer (local OCR, ~20-80 s cold, cached on disk for the later
// questions on the photo too; not the warm-up's: that one reads O1's deskewed copy), so it starts when the visitor
// picks 'tap the room' (no cost for other questions) and a room measured at the door swings meanwhile is measured
// again when it arrives.
// Its two ends are kept like the visitor's two scale taps, so a tilted photo maps them into the flat plane exactly.
/** A room the visitor tapped. m2 / scaleText: the sheet's finished takeoff measured it (GET /api/glance, F2: the same
 *  outline, name and m² as the desktop's right-click and takeoff), so its number is the takeoff's, not re-measured. */
interface Room { poly: Pt[]; method?: string; around?: boolean; checked?: boolean; name?: string; nameBox?: number[]; m2?: number; scaleText?: string; zone?: boolean; matched?: string }
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
  const was = !state.scale ? "" : ln && state.answer?.solver === "tap" ? `${fmtM(lengthMetres(ln, state.scale))} m` : last && last.m2 === undefined ? `${fmtM(areaM2(last.poly, state.scale))} m²` : "";
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
/** a 3x3 homography (row-major) applied to a point */
function hmap(H: number[], p: Pt): Pt {
  const w = H[6] * p[0] + H[7] * p[1] + H[8];
  return [(H[0] * p[0] + H[1] * p[1] + H[2]) / w, (H[3] * p[0] + H[4] * p[1] + H[5]) / w];
}
function hinv(m: number[]): number[] {
  const [a, b, c, d, e, f, g, h, i] = m;
  const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
  const det = a * A + b * B + c * C;
  return [A, -(b * i - c * h), b * f - c * e, B, a * i - c * g, -(a * f - c * d), C, -(a * h - b * g), a * e - b * d].map((v) => v / det);
}
/** Booth (W6 01:45): is this photo a printed sheet of one of the cases (GET /api/m/match, src/mobile/photomatch.py: SIFT +
 *  RANSAC homography, ~1-4 s)? Then a tap takes that sheet's takeoff room, mapped onto the photo (DfE Level 00 printed:
 *  Kitchen 36.36 m² as on the desktop, instead of 38.5 by flood fill at the door-swing scale). */
async function photoMatch(path: string) {
  type M = { ok: boolean; sheet?: string; path?: string; H?: number[]; inliers?: number; close?: boolean };
  const j: M = await fetch(`/api/m/match?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false })).catch(() => ({ ok: false }));
  if (state.file?.path !== path || state.pdf || !j.ok || !j.path || !j.H || j.H.length !== 9) return;
  const p = PROJECTS.find((x) => x.sheets.some((sh) => sh.path === j.sheet));
  const sh = p?.sheets.find((x) => x.path === j.sheet);
  const title = [p?.name, sh?.label].filter(Boolean).join(" · ") || "a drawing on file";
  state.match = { path: j.path, H: j.H, Hi: hinv(j.H), space: /\.pdf$/i.test(j.path) ? "pdf_pt" : "image_px", title, ...(j.close ? { close: true } : {}) };
  void sheetTakeoff(j.path, 0);
  matchNote();
}
/** the photo match still running for this upload: a chip tapped right after the upload waits for it (up to 6 s; ~1-4 s
 *  warm) so the question goes to the drawing on file, not into a slow first read of the photo */
let matching: { path: string; p: Promise<void> } | null = null;
/** the stage line under the photo says which drawing on file the photo was matched to */
function matchNote() {
  const n = $("stageNote"), t = state.match ? `Recognised: ${state.match.close ? "part of " : ""}${state.match.title} (on file) — tap a room` : "";
  if (t && !n.textContent?.includes(t)) n.textContent = [n.textContent, t].filter(Boolean).join(" · ");
}
/** An answer asked on the matched sheet, its marks (that sheet's units) mapped onto the photo (its px). */
function onPhoto(a: Answer, m: NonNullable<typeof state.match>): Answer {
  const file = state.file!.path, base = m.path.split("/").pop();
  const map = (ps: number[][]) => ps.map((q) => hmap(m.Hi, [q[0], q[1]]));
  const evidence = (a.evidence ?? []).map((e) => {
    if ((e.file !== m.path && e.file?.split("/").pop() !== base) || (e.page ?? 0) !== 0) return e;
    const out = { ...e, file, page: null, bbox_space: "image_px" } as Evidence;
    if (e.polygon?.length) out.polygon = map(e.polygon as number[][]) as Evidence["polygon"];
    if (e.polyline?.length) out.polyline = map(e.polyline as number[][]) as Evidence["polyline"];
    const b = e.bbox as number[] | null | undefined;
    if (b && b.length >= 4) {
      const cs = map([[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]);
      out.bbox = [Math.min(...cs.map((c) => c[0])), Math.min(...cs.map((c) => c[1])), Math.max(...cs.map((c) => c[0])), Math.max(...cs.map((c) => c[1]))] as Evidence["bbox"];
    }
    return out;
  });
  return { ...a, evidence };
}
/** The answer's scale (on the sheet) carried onto the photo near its marks: units per m x the photo's px per sheet unit
 *  there (the ratio of an outline's area on the photo to its area on the sheet; else the homography at the sheet middle). */
function photoScale(a0: Answer, a: Answer, m: NonNullable<typeof state.match>): Scale | null {
  // the sheet's own scale as the answer states it: a px / pt per m, else (a picture sheet) 'A2 at 257 dpi' + 'printed 1:200'
  const s0 = scaleFromAnswer(a0, m.space) ?? (m.space === "image_px" ? deskewedScale(a0) : null);
  if (!s0) return null;
  const i = (a0.evidence ?? []).findIndex((e) => (e.polygon?.length ?? 0) > 2);
  let k = 0;
  if (i >= 0) k = Math.sqrt(polyArea(a.evidence[i].polygon as Pt[]) / Math.max(1e-9, polyArea(a0.evidence[i].polygon as Pt[])));
  if (!(k > 0)) {
    const c = hmap(m.Hi, [0, 0]), dx = hmap(m.Hi, [100, 0]), dy = hmap(m.Hi, [0, 100]);
    k = Math.sqrt(Math.abs((dx[0] - c[0]) * (dy[1] - c[1]) - (dx[1] - c[1]) * (dy[0] - c[0]))) / 100;
  }
  if (!(k > 0)) return null;
  const u = s0.unitsPerM * k;
  return { unitsPerM: u, label: /^1:\d+/.test(s0.label) ? s0.label : scaleLabel(u, "image_px"), source: `${s0.source} (on the drawing on file, carried onto your photo)` };
}
/** The matched sheet's takeoff room under a tap on the photo, its outline mapped onto the photo, or null. */
async function matchedRoom(ev: Pt): Promise<Room | null> {
  const m = state.match;
  if (!m || state.pdf) return null;
  const r = await takeoffRoom(m.path, hmap(m.H, ev), 0, m.space);
  if (!r || state.match !== m) return null;
  const poly = r.poly.map((q) => hmap(m.Hi, q));
  const lb = r.nameBox;
  const nameBox = lb && lb.length >= 4 ? (() => { const cs = [hmap(m.Hi, [lb[0], lb[1]]), hmap(m.Hi, [lb[2], lb[3]])]; return [Math.min(cs[0][0], cs[1][0]), Math.min(cs[0][1], cs[1][1]), Math.max(cs[0][0], cs[1][0]), Math.max(cs[0][1], cs[1][1])]; })() : undefined;
  return { ...r, poly, nameBox, matched: m.title };
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
// the sticky photo bar (cam2 / pickf2, UX M13) hides while the full photo card is on screen
new IntersectionObserver((es) => $("photoBar").classList.toggle("away", es.some((e) => e.isIntersecting)), { threshold: 0.35 })
  .observe(document.querySelector(".m-pick")!);
for (const id of ["cam", "pickf", "cam2", "pickf2"]) {
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
  closePress();
  focus = null;
  Object.assign(state, { case: null, caseSheet: 0, file: null, pdf: false, page: 0, pages: 1, k: 1, map: null, answer: null, question: "", scale: null, fileScale: null, tool: null, taps: [], rooms: [], lines: [], flat: null, match: null, answeredOn: "", barNote: "", notPlan: "", paged: "" });
  caseUi();
  view.clearMarks();
  document.body.dataset.step = "pick";
  marksChip();
  $("result").innerHTML = "";
  $("result").hidden = true;
  $("stageNote").textContent = "";
  setTool(null);
  pager();
  delUi();
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
  Object.assign(state, { answer: null, scale: null, fileScale: null, taps: [], rooms: [], lines: [], notPlan: "", paged: state.file.path });
  closePress();
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
    matching = { path: info!.path, p: photoMatch(info!.path) };
  }
  try {
    await showDrawing();
  } catch (e) {
    return fail(`We could not show this file: ${(e as Error).message}`, () => void takeFile(f));
  }
  const notes = [prep.note, info!.upload_note].filter(Boolean);
  $("stageNote").textContent = notes.length ? notes.join(" · ") : "";
  matchNote();
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
  const card = (p: Project) => {
    const sh = sheetOf(p)!;
    // only a scan gets a badge (captain 20:53); 'PDF' / 'IMAGE' were noise for a site engineer (UX polish 4, 23:30)
    const k = isScan(p) ? "scan" : "";
    // captain 22:56: six cases simple -> complex, each with its difficulty (catalogue 'level', W5 23:18)
    const lv = String((p as { level?: string }).level ?? "").toLowerCase();
    const lvl = ["simple", "medium", "complex"].includes(lv) ? lv : "";
    return `<button type="button" class="m-case" data-id="${esc(p.id)}">
      <img src="/api/m/case/thumb?path=${encodeURIComponent(sh.path)}" alt="" width="64" height="64" loading="lazy" onerror="this.style.visibility='hidden'">
      <span><b>${esc(p.name)}</b><small>${esc(p.contents || p.facts.join(" · "))}</small></span>
      <span class="m-case-tags">${lvl ? `<span class="m-lvl lv-${lvl}">${lvl[0].toUpperCase()}${lvl.slice(1)}</span>` : ""}${k ? `<span class="m-case-k scan">${esc(k)}</span>` : ""}</span></button>`;
  };
  // captain 22:17: the main cases first, then the catalogue's 'More examples' (a project with more: true or group 'more')
  const main = shown.filter((p) => !isMore(p)), more = shown.filter(isMore);
  $("cases").innerHTML = main.map(card).join("") + (more.length && main.length ? `<h2 class="m-more-h">More examples</h2>` : "") + more.map(card).join("");
}
/** A case by its id or one of its catalogue 'aliases' (W5 22:40: the 'dfe-scan' project became the school's scanned
 *  sheet; same rule as projects.ts projectById), and the sheet an alias names: the scan sheet for a '…scan' alias. */
function caseById(id: string): { p: Project; sheet: number } | null {
  const p = PROJECTS.find((x) => x.id === id) ?? PROJECTS.find((x) => ((x as { aliases?: string[] }).aliases ?? []).includes(id));
  if (!p) return null;
  const scan = p.id !== id && /scan/i.test(id) ? sheetsOf(p).findIndex((s) => (s as { badge?: string }).badge === "scan") : -1;
  return { p, sheet: Math.max(0, scan) };
}
$("cases").addEventListener("click", (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>(".m-case")?.dataset.id;
  const c = id ? caseById(id) : null;
  if (c) void openCase(c.p);
});
void renderCases();

/** Chips, the back button and the credit line for the case on screen (or the photo flow's when none is). */
function caseUi() {
  const c = state.case;
  $("chips").innerHTML = (c ? chipsFor(c, state.caseSheet) : state.notPlan ? SIDE_CHIPS : CHIPS).map((q) => `<button type="button" class="m-chip" data-q="${esc(q)}">${esc(q)}</button>`).join("");
  $("retake").textContent = c ? "All cases" : "New photo";
  // short enough for the input beside the mic and Ask buttons at 390 px ('Ask your own questio' was cut, UX 23:30)
  $<HTMLInputElement>("q").placeholder = c ? "Your own question…" : "Your question…";
  const ifc = c?.sheets.find((s) => /\.ifc$/i.test(s.path));
  const many = c ? sheetsOf(c) : [];
  const ss = $("caseSheets");
  ss.hidden = many.length < 2;
  ss.innerHTML = many.length < 2 ? "" : `<span>Sheet</span>${many.map((s, i) => `<button type="button" class="m-chip" data-sheet="${i}" aria-pressed="${i === state.caseSheet}">${esc(s.label || `Sheet ${i + 1}`)}${scanSheet(s) ? " · scan" : ""}</button>`).join("")}`;
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
  void sheetTakeoff(f.path, state.page); // the sheet's takeoff rooms, ready for the first tap
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
  delUi();
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
  state.answeredOn = "";
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
  state.answeredOn = "";
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

// ---------- touching the plan without a tool (UX 00:10 S-M9: a tap and a long-press on a case's plan did nothing) ----------
/** A tap on the plan = tap-the-room (the cheap, model-free measure). After an area answer (or a tapped room) it adds the
 *  room as 'Tap the room to outline it' does, the answer kept in small type; otherwise it starts 'How big is this room?'
 *  at that point. Not while a question is being answered, nor on a sheet that is not a floor plan. */
function tapPlan(ev: Pt) {
  if (!$("pressMenu").hidden) return closePress(); // a tap beside the open menu closes it (the desktop's empty click)
  if (!state.file || state.tool || state.busy || document.body.dataset.step === "asking" || state.notPlan) return;
  const a = state.answer;
  if (a && notPlan(a)) return;
  if (a && !isLength(a, state.question) && (a.solver === "tap" ? !state.lines.length : isArea(a, state.question))) setTool("room");
  else thisRoom();
  void traceRoom(ev);
}
view.onIdleTap = (x, y) => tapPlan(toEv(x, y));

/** Long-press (or right-click) on the plan: the desktop's right-click items (features/rightclick: the same questions, the
 *  point sent as context) for the room under the finger, named from its printed label; 'Area of …' is tap-the-room. */
function closePress() {
  $("pressMenu").hidden = true;
  pressAt = null;
  view.clearMarks("muted");
  drawMine(); // hidden while the menu was about another room (UX 01:12: the hall's outline stayed under 'Kitchen')
}
function askPointed(q: string, ev: Pt) {
  closePress();
  $<HTMLInputElement>("q").value = q;
  void askNow({ point: ev, space: space(), page: state.page });
}
/** The printed text line under / next to the finger (the desktop's c.label: within 36 pt, 3x that in picture px), joined
 *  along its baseline ('CV-1', '16"', a note), or ''. */
function labelAt(words: Word[] | null, p: Pt): string {
  if (!words?.length) return "";
  const gap = (w: Word) => Math.hypot(Math.max(0, Math.abs(w.at[0] - p[0]) - w.hw), Math.max(0, Math.abs(w.at[1] - p[1]) - w.hh));
  const best = words.reduce((a, b) => (gap(b) < gap(a) ? b : a));
  if (gap(best) > (state.pdf ? 36 : 108)) return "";
  const h = Math.max(best.hh, 1);
  const row = words.filter((w) => Math.abs(w.at[1] - best.at[1]) < h * 0.7 && Math.abs(w.hh - best.hh) < h * 0.5).sort((a, b) => a.at[0] - b.at[0]);
  let i = row.indexOf(best), j = i;
  while (i > 0 && row[i].at[0] - row[i].hw - (row[i - 1].at[0] + row[i - 1].hw) < h * 2.6) i--;
  while (j < row.length - 1 && row[j + 1].at[0] - row[j + 1].hw - (row[j].at[0] + row[j].hw) < h * 2.6) j++;
  return row.slice(i, j + 1).map((w) => w.t.trim()).join(" ").replace(/\s+/g, " ").trim();
}
// the desktop's right-click rules (features/rightclick): a pipe size, an element tag
const PIPE = /(\d+(?:\s*\d\/\d)?\s*(?:"|″|in\b)|\bDN\s?\d+|\bØ\s?\d+)/i;
const SYMBOL = /^(?:[A-Z]{1,4}-?\d{1,3}[A-Z]?|[A-Z]{1,3}\d{1,3})$/;
function drawPress(ev: Pt, name: string | null, label = "") {
  const h = $("pressH");
  h.textContent = name === null ? "Finding the room…" : name || (label ? `“${label.slice(0, 40)}”` : "This spot");
  // a printed label at the finger: the desktop's label items, the same questions (max 2 here; the room's come first)
  const tag = label.replace(/-?\d+[A-Z]?$/, "");
  const labelItems: [string, () => void][] = !label ? [] : [
    ...(PIPE.test(label) ? [["Size of this pipe", () => askPointed(`What size is the pipe labelled "${label}"?`, ev)]] as [string, () => void][] : []),
    ...(SYMBOL.test(label) && /[A-Z]/.test(label) ? [[`Count all ${tag} tags`, () => askPointed(`How many ${tag} tags are on this drawing?`, ev)]] as [string, () => void][] : []),
    ...(!name && /\p{L}{3}/u.test(label) && !PIPE.test(label) && !SYMBOL.test(label) ? [[`Find all “${label.slice(0, 24)}”`, () => askPointed(`How many rooms are named "${label}" on this drawing, and what is their total area?`, ev)]] as [string, () => void][] : []),
    ...(!name && label.length >= 12 ? [["Explain this note", () => askPointed(`What does the note "${label.slice(0, 160)}" on this drawing mean?`, ev)]] as [string, () => void][] : []),
  ].slice(0, 2);
  const items: [string, () => void][] = [
    [name ? `Area of ${name}` : "Area of this room", () => { closePress(); tapPlan(ev); }],
    ...(name ? [
      [`Doors of ${name}`, () => askPointed(`How many doors does the ${name} have?`, ev)],
      [`Skirting of ${name}`, () => askPointed(`How long is the skirting (baseboard) of the ${name}, door openings excluded?`, ev)],
    ] as [string, () => void][] : []),
    ...labelItems,
    ["Measure from here", () => {
      closePress();
      thisLength();
      const [x, y] = toPx(ev);
      onTap(x, y);
    }],
    ["What is here?", () => askPointed(name ? `What is the ${name} used for, and how big is it?` : label ? `What is "${label.slice(0, 80)}" on this drawing?` : "What is at the point I clicked on the drawing?", ev)],
  ];
  const box = $("pressItems");
  box.replaceChildren(...items.map(([label, run]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "m-btn ghost m-press-i";
    b.textContent = label;
    b.addEventListener("click", run);
    return b;
  }));
}
async function pressMenu(ev: Pt) {
  if (!state.file || state.tool || state.busy || document.body.dataset.step === "asking") return;
  const file = state.file;
  pressAt = ev;
  view.clearMarks("muted");
  view.clearMarks("user"); // the menu is about the pressed spot: earlier outlines come back when it closes
  view.addMark({ kind: "dot", pts: [toPx(ev)], tone: "muted" });
  drawPress(ev, null);
  $("pressMenu").hidden = false;
  marksChip();
  $("pressMenu").scrollIntoView({ block: "nearest", behavior: "smooth" });
  // the printed text at the finger (a tag, a pipe size, a note): its items at once
  const label = labelAt(await sheetWords(file).catch(() => null), ev);
  if (pressAt !== ev || state.file !== file) return;
  if (label) drawPress(ev, null, label);
  // the room under the finger and its printed name: one trace on the CPU (~0.3 s), no scale needed for a name
  let name = "";
  try {
    // a vector PDF: its printed scale first (~0.1 s), so the trace runs along the wall lines as a tap's does (the flood
    // fill without it leaked from the Staff Social Room into a corridor loop)
    // (a picture: its wall lines too, at the bar / door-swing scale, as a tap's)
    // the sheet's takeoff first: its room under the finger, named as on the desktop (UX C-1)
    const tk = (await matchedRoom(ev)) ?? (await takeoffRoom(file.path, ev));
    if (pressAt !== ev || state.file !== file) return;
    if (tk) {
      view.addMark({ kind: "poly", pts: tk.poly.map(toPx), tone: "muted" });
      return drawPress(ev, tk.name ?? "", label);
    }
    const s0 = state.scale ?? state.fileScale ?? (await estimatedScale());
    if (pressAt !== ev || state.file !== file) return;
    const r = Math.max(2, 4 / state.k), n = scaleN(s0);
    let poly = !state.pdf && s0 ? await lineRoom(file.path, ev, s0) : null;
    if (!poly) {
      const res = await roomShapes(file.path, state.page, space(), [{ i: 0, bbox: [ev[0] - r, ev[1] - r, ev[0] + r, ev[1] + r], ...(n > 0 ? { scale_n: n } : {}) }]);
      poly = trusted(res, res.shapes[0]) ? (res.shapes[0]?.poly as Pt[] | null | undefined) ?? null : null;
    }
    if (poly && poly.length > 2 && !notThisRoom(poly, ev)) {
      name = (await checkedName(file, poly, ev, s0))?.name ?? "";
      if (pressAt === ev && state.file === file) view.addMark({ kind: "poly", pts: poly.map(toPx), tone: "muted" });
    }
  } catch {
    /* no name: the spot's own items */
  }
  if (pressAt !== ev || state.file !== file) return;
  drawPress(ev, name, label);
}
view.onLongPress = (x, y) => void pressMenu(toEv(x, y));
$("pressX").addEventListener("click", () => closePress());

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

async function askNow(ctx?: AskCtx) {
  const q = $<HTMLInputElement>("q").value.trim();
  if (!q || !state.file) return;
  $<HTMLInputElement>("q").blur();
  state.busy?.abort();
  closePress();
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
    // a photo recognised as a sheet on file (GET /api/m/match): the question goes to that sheet (the desktop's answer,
    // often from memory), and its marks come back onto the photo through the homography. A CLOSE-UP (a part of the
    // sheet) only for what the visitor pointed at or named ('How big is the kitchen?'): 'How many doors?' on it would
    // count the whole sheet's, mostly off the photo
    if (!state.pdf && !state.match && matching?.path === state.file.path) await Promise.race([matching.p, sleep(6000)]);
    if (ctl.signal.aborted) return;
    const m = !state.pdf && (!state.match?.close || ctx?.point || askedThing(q)) ? state.match : null;
    const mctx = m && ctx ? { ...ctx, ...(ctx.point ? { point: hmap(m.H, ctx.point as Pt) as [number, number] } : {}), space: m.space, page: 0 } : ctx;
    const a0 = await askWithRetry(m ? m.path : state.file.path, q, ctl.signal, mctx);
    state.t.ask = performance.now() - t0;
    if (ctl.signal.aborted) return;
    const a = m ? onPhoto(a0, m) : a0;
    state.answeredOn = m ? m.title : "";
    state.answer = a;
    state.scale = (m ? photoScale(a0, a, m) : scaleFromAnswer(a, space())) ?? (!state.pdf && !m ? deskewedScale(a) : null);
    if (state.scale && !state.pdf && deskewedAnswer(a)) (state.scale as FlatScale).deskewed = true;
    if (state.scale && !scaleGuessed(state.scale)) state.fileScale = state.scale;
    await showAnswer();
  } catch (e) {
    if (ctl.signal.aborted) return;
    fail(`No answer this time: ${(e as Error).message}`, () => void askNow(ctx));
  } finally {
    clearInterval(tick);
    if (state.busy === ctl) state.busy = null;
  }
}

/** POST /api/ask; a dropped connection (phone locked, Wi-Fi switch) is retried twice: the server keeps the answer it
 *  is working on (cached per file + question), so a retry picks it up instead of starting again. */
/** A question that names its own sheet or page, or asks about the whole set, keeps the solver's own page pick (the
 *  desktop's OWN_PAGE_Q, main.ts) */
const OWN_PAGE_Q = /\b(sheets?|pages?|drawings?|blatt|seite|arkki|sivu)\s+[\w.-]*\d|\b[A-Z]{1,2}-\d{2,4}[A-Z]?\b|\b(whole|entire|all)\s+(building|house|set|floors|storeys|levels|sheets|pages)\b|\bevery\s+(floor|storey|level|sheet|page)\b/i;
/** the page of a multi-page PDF upload the visitor turned to, as the desktop sends it (api.ts ask 'page'; W5's
 *  ask_upload.page_on_screen): the phone's pager showed page 69 while the question was answered from the page the
 *  solver picked */
const onPage = (path: string, q: string): number | undefined =>
  path.startsWith("upload/") && state.pdf && state.pages > 1 && state.paged === path && !OWN_PAGE_Q.test(q) ? state.page : undefined;
async function askWithRetry(path: string, question: string, signal: AbortSignal, ctx?: AskCtx): Promise<Answer> {
  const page = onPage(path, question);
  // what the visitor pointed at (a long-press): the desktop's askWith context (W5's ask_upload.ask_context)
  const pointed = ctx ? { context: ctx, ...(ctx.point ? { point: ctx.point } : {}) } : {};
  for (let attempt = 1; ; attempt++) {
    try {
      // private mode: the same request as the desktop's (api.ts ask): uploads say private (the middleware switches every
      // hosted step off); the sample (a dataset file) asks the API's pipeline with its hosted steps off
      const priv = privacy.on ? { private: 1, ...(path.startsWith("upload/") ? {} : { solver: "pipeline", solver_args: { gemini: 0, verify: 0 } }) } : {};
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, question, ...priv, ...(page !== undefined ? { page } : {}), ...pointed }), signal });
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
  marksHidden = false;
  renderResult();
  fitPhotoBox();
  await new Promise((r) => requestAnimationFrame(r));
  drawEvidence(a);
  // an area answer with room boxes but no outline: trace the outlines on the photo (flood fill, CPU; approximate)
  // (not around the rows of a printed area table: AFCEC circulation painted blobs over the table)
  if (isArea(a, state.question) && !fromTable(a)) void outlineBoxes(a);
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
    // '; ' separates the items when there are any (QA 22:26, S-100F 'slab area of each type?': '300 SLAB-ON-GRADE W/ D16
    // @ 300 TOP FACE, BOTTOM FACE …: 17.100 x 12.000 = 205.2 m2; 300 SLAB-ON-GRADE …' was cut at every comma)
    const semi = t.split(/;\s+/);
    const parts = semi.length >= 2 ? semi : t.split(/,\s+/);
    lines = parts.length >= (semi.length >= 2 ? 2 : 3) ? ["", ...parts] : [t];
  }
  const head = /^[-•*]\s/.test(lines[0]) ? "" : lines.shift() ?? "";
  return { head: say(head), items: lines.filter((x) => !/^by (storey|floor|level|type|room)s?:?$/i.test(x)).map(say).slice(0, 12) };
}
function listHtml(l: { head: string; items: string[] }): string {
  return `<div class="m-value m-listval">${l.head ? `<div class="m-listhead">${esc(l.head)}</div>` : ""}${l.items.length
    ? `<ul class="m-list${l.items.some((x) => x.length > 48) ? " long" : ""}">${l.items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>`;
}

/** What the question asks the size of, in its own words, when it says 'the X' ('How long is the corridor of apartment
 *  B?' → 'corridor of apartment B': the parser's target there is 'apartment'; 'What is the floor area of the Multi-Purpose
 *  Hall?' → 'Multi-Purpose Hall'), or ''. Short phrases only (a sentence-long question keeps the parser's target). */
function askedThing(q: string): string {
  const m = q.trim().replace(/\?+$/, "").match(/^(?:how (?:long|big|large|wide|deep|tall|high) is|what(?: is|'s) the (?:(?:total |overall |floor |net |gross )?(?:length|area|size|width|floor area)) of) the ([\p{L}\d][\p{L}\d .&/'-]{1,44})$/iu);
  return m && !/\b(drawing|plan|sheet|this|that)\b/i.test(m[1]) ? m[1].trim() : "";
}

/** The one-sentence explanation under the number. */
function sentence(a: Answer, q: string): string {
  const t = askedThing(q) || (a.intent?.target && !/^(unknown|drawing|floor|area|room)$/i.test(a.intent.target) ? a.intent.target.replace(/_/g, " ") : "");
  const ev = (a.evidence ?? []).filter(onThis);
  // a photo recognised as a sheet on file was answered on that sheet: its outline was measured there, not on the photo
  const on = state.pdf || state.case ? "the drawing" : state.answeredOn ? "the matching drawing on file" : "your photo";
  const polys = ev.filter((e) => e.polygon && e.polygon.length > 2).length; // the scale bar's box is not an outline
  const lines = ev.filter((e) => e.polyline && e.polyline.length > 1).length;
  // an area / length with no outline or measured line was not measured: 'marked' said it was (UX M22: the only mark was
  // the sheet title); a printed number it read is 'read off' the sheet, anything else says what the mark is
  const unmeasured = !polys && !lines && (isArea(a, q) || isLength(a, q));
  const how = ev.length
    ? polys ? `measured from the outline${polys > 1 ? "s" : ""} drawn on ${on}`
      : unmeasured ? ev.some((e) => /'[^']*\d[^']*'/.test(e.note ?? "")) ? `read off ${on}, not measured (marked where it is printed)` : `not measured: the mark shows what the reading used on ${on}`
      : ev.length > 1 ? `marked in ${ev.length} places on ${on}` : `marked on ${on}`
    : `nothing marked on ${on}`;
  // a count or a list is shown by its outlines, not measured from them (Maleva 'rooms on each floor', Swiss corridor rooms)
  const shown = polys ? `${polys > 1 ? `${polys} outlined` : "outlined"} on ${on}` : how;
  if (a.intent?.task === "count" || a.unit === "count") {
    const n = typeof a.value === "number" ? a.value : NaN;
    const noun = Number.isFinite(n) ? countNoun(a, q, n) : null;
    // a list answer ('rooms on each floor': Total 285 / Floor 1: 50 …) with this sheet's items outlined: say what they are
    // (was '50 outlined on the drawing.')
    const listNoun = !Number.isFinite(n) && polys > 1 ? countNoun(a, q, polys) : null;
    if (listNoun) return `${polys} ${listNoun} on this ${sheet() === "drawing" ? "sheet" : "photo"}, outlined on ${on}.`;
    return !Number.isFinite(n) ? `${shown[0].toUpperCase()}${shown.slice(1)}.` : noun ? `${n} ${noun}${/\b(?:in|of|does|do|inside|around) the \w|\bhave\b/i.test(q) ? "" : " on this drawing"}, ${shown}.` : `Counted ${n}, ${shown}.`;
  }
  if (nominal(a, q)) return `Read off the ${on === "your photo" ? "photo" : "drawing"} (a printed size, not measured), ${how}.`;
  if (isArea(a, q)) return `${t ? `Area of the ${t}` : "Area"}, ${how}.`;
  if (isLength(a, q)) return `${t ? `Length of the ${t}` : "Length"}, ${how}.`;
  return `${shown[0].toUpperCase()}${shown.slice(1)}.`;
}

/** The sheet is NOT a floor plan, by the pipeline's own sheet check ("this sheet is an elevation ('EAST ELEVATION'): it
 *  shows no floor plan, so rooms cannot be counted or measured on it"; missing "a floor-plan sheet … (this one shows:
 *  elevation)"): {kind, title}, or null. UX M22 (23:43): a HABS east elevation photo + 'Total floor area?' showed
 *  '≈ 120 m² · Area, marked on your photo.' with the sheet title as the only mark. */
function notPlan(a: Answer): { kind: string; title: string } | null {
  for (const t of [...(a.assumptions ?? []), ...(a.trace ?? []).map(String)]) {
    const m = t.match(/this sheet is an? ([a-z][a-z /-]{2,30}?) \('([^']{1,60})'\)[^:]{0,40}: it shows no floor plan/i);
    if (m) return { kind: m[1].toLowerCase(), title: m[2] };
  }
  for (const t of a.missing ?? []) {
    const m = t.match(/floor[- ]plan\b.*\(this one shows: ([a-z][a-z /-]{2,30})\)/i);
    if (m) return { kind: m[1].toLowerCase(), title: "" };
  }
  return null;
}

/** Why the reading's number is not shown (W5's implausible() said 'checked' / 'level' / 'echo'), and what to do. */
function rejectedWhy(a: Answer, q: string, bad: "checked" | "level" | "echo" | "ratio"): string {
  const len = isLength(a, q) && !isArea(a, q);
  const n = typeof a.value === "number" ? a.value.toLocaleString("en", { maximumFractionDigits: 2 }) : String(a.value).trim();
  const u = unitLabel(a.unit) || "";
  const why = bad === "checked" ? `The reading took ${n}${u ? ` ${u}` : ""}: ${sizeCheck(a)}.`
    : bad === "ratio" ? `The reading found the ${sheet()}'s scale (${n}), not the ${len ? "length" : "area"}.`
    : bad === "level" ? `The reading took ‘${levelRead(a)}’: a level printed on the ${sheet()}, not ${len ? "a length" : "an area"}.`
    : `The reading found only ‘${n}’, the number in your question, not the ${len ? "length" : "area"}.`;
  return `${why} Tap the ${tapNoun(q)} to ${len ? "measure it" : "outline it"}.`;
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
  // a words-only answer is no answer to a size / measuring / 'how many' question (a photo's 'kitchen' for 'How big is
  // this room?': tap it to measure), but it IS the answer to 'What is the biggest room?' ('Multi-Purpose Hall', the DfE
  // sheet showed 'No answer' with both words marked) or any free-text question: shown as the desktop shows it
  const hideWords = words && (size || measure || /^\s*how (many|much)\b/i.test(q));
  // the desktop's words (Very sure / Fairly sure / Best estimate / Our guess / Rough guess, same 0.4 as the expert
  // workbench): a 0.05 answer must not read like a measurement
  const c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  const scaleOk = !!state.scale && !scaleGuessed(state.scale);
  // a figure READ off the print (AFCEC 'TOTAL COMMON SPACE = 4,479 SF', W5 printedReading, QA 18:56): nothing to
  // measure, no scale needed; the sentence quotes the print
  // the number is NOT the asked thing by the pipeline's own check (W5 implausible: a sheet's printed GIFA total for one
  // room, a mm dimension, a printed LEVEL 'TOP OF SLAB EL. = 10000', the question's own number): the desktop shows
  // 'Not found' and why (QA r17, EFA scans '≈ 1,274 m² · Rough guess' for a room); so does the phone, never '≈ 1,274'
  const bad = measure ? implausible(a, q) : "";
  // a scale ratio given as the size ('How big is this room?' on the DfE photo -> '1:200' @0.3, pipeline/raster): shown as '1:200 m²'
  const ratio = size && typeof a.value === "string" && /^\s*1\s*[:/]\s*[\d.,]+\s*$/.test(a.value);
  const rejected = ratio ? "ratio" : bad === "checked" || bad === "level" || bad === "echo" ? bad : "";
  if (rejected) big = "";
  // not a floor plan (an elevation, a section): floor areas and rooms / doors cannot be measured on it (lengths can:
  // heights on an elevation), so no number, no tap-the-room, no scale pill; the guess is mentioned, smaller (UX M22)
  const np = notPlan(a);
  const blocked = !!np && (isArea(a, q) || ((a.intent?.task === "count" || a.unit === "count") && /\b(rooms?|spaces?|doors?|apartments?|flats?)\b/i.test(q)));
  const npGuess = blocked && typeof a.value === "number" ? `${a.value.toLocaleString("en", { maximumFractionDigits: 1 })}${unitLabel(a.unit) && a.unit !== "count" ? ` ${unitLabel(a.unit)}` : ""}` : "";
  if (blocked) big = "";
  if (np && !state.case && state.notPlan !== np.kind) { // the visitor's photo is an elevation: its own chips from now on
    state.notPlan = np.kind;
    caseUi();
  }
  const sum = rejected ? "" : tableSum(a);
  const dims = rejected ? "" : dimSum(a);
  const printed = rejected ? "" : printedReading(a) || printedNote(a) || sum || dims;
  let tier = costTier(a);
  // the visitor's own measurement (tap the room) at the scale: replaces a missing / unmeasured number; a room the
  // visitor TAPPED is always the number shown (it is what they just asked for by tapping): DfE photo 'Total floor area?'
  // ≈ 3,770 then a kitchen tap showed 3,766.88 m² over the sentence 'The room you tapped …' (34.6 m² on the photo)
  const mine = myMeasure();
  const serverBig = big;
  // a low-confidence number is shown as one: '≈ 3,770' (3 significant digits), not '3,766.88' (DfE photo, @0.05)
  const guessy = !measure && !words && isGuess(a);
  let shownBig = serverBig;
  if (!rejected && !blocked && size && typeof a.value === "number" && Math.abs(a.value) >= 100) {
    const v = guessy ? Number(a.value.toPrecision(3)) : a.value;
    shownBig = Math.abs(v) >= 1000 ? v.toLocaleString("en", { maximumFractionDigits: 2 }) : fmtValue({ ...a, value: v }); // areas: 2,500 m²
  }
  // one switch for the number, its sentence, its pill and its unit: the visitor's measurement or the server's answer
  const useMine = !!mine && (!!mine.room || measure || words || !big);
  big = useMine ? mine!.value : shownBig;
  const lowConf = !useMine && guessy;
  const guessNum = ((measure || hideWords) && !useMine) || lowConf;
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
  // the visitor's own outline / line is the number: traced on the CPU, no model; the cost and data lines are ITS, not the
  // earlier answer's (UX 00:10 S-M9: 'Expensive · Data: our server · Gemini' under a 0.3 s outline)
  const tapped = !!mine?.room || a.solver === "tap";
  if (tapped) tier = { tier: "cheap", word: "Cheap · no AI", why: "outlined on our server's CPU from the drawing's own lines and text: no AI model was asked" };
  // a replay from the answer cache did no new work: the tier is what the first answer cost, said as such (the desktop's)
  const again = !tapped && !!a.cached && tier.tier !== "cheap";
  const tapName = mine?.room && !(a.solver === "tap" && state.lines.length) ? state.rooms[state.rooms.length - 1]?.name ?? "" : "";
  // 'How long is this wall?': the visitor's line, its own button to measure another one
  const tapLine = a.solver === "tap" && isLength(a, q);
  const noNumber = !big || (guessNum && hideWords);
  // a list or a long text answer ('Total spaces (whole model): 285\nBy storey:\n- 1.korrus: 50 …', 'the rooms off the
  // corridor: 2.10 'Zimmer', 2.11 …') in the 64 px number font filled two screens (Maleva 'rooms on each floor')
  const listed = typeof a.value === "string" ? listParts(a.value) : null;
  // what a site engineer can act on: not our plumbing ('recognised as …', 'LLM budget reached (20000 tokens …)')
  // (the convention and Includes / Excludes lines are the Scope line already: AFCEC dorms said 'Assumed: Convention: …')
  // ('no vector reader answered this question … read as an image at 136 dpi': S-100F 'overall length of the foundation')
  const assume = (a.assumptions ?? []).filter((x) => !/^recognised as|LLM budget|tokens or \d+ calls|\bsolver\b|cascade|^\s*(convention|includes?|excludes?)\s*:|^no (?:\w+ )?reader answered|read as an image at \d+ dpi/i.test(x))[0];
  // the marks, one button each: tap = zoom there; 'All' = back to all of them (the visitor's own outline needs none)
  const places = mine?.room || a.solver === "tap" ? [] : evPlaces(a);
  // the same 'Scope:' line as the desktop card (W5 customer.ts scopeOf: includes / excludes + the convention in words +
  // what the reader did, '1 door opening deducted (0.83 m)'); none on the visitor's own outline or a bare tap
  // (and none under 'Not measured yet' / 'No answer': S-100F 'slab area of each type?' showed the slab rule under no number)
  const scope = useMine || a.solver === "tap" || (noNumber && !listed) ? null : scopeOf(a, q, conventionOf(a, q));
  // two readings that disagree, as the desktop's card has them (W5 rivalReading, which already keeps a picture guess
  // beside a measured outline quiet: UX 23:30 B1/B2 / M15): one calm line, not a pill
  const rv = !useMine && !noNumber && !rejected && !blocked && a.solver !== "tap" ? rivalReading(a) : null;
  const rvText = rv ? `${rv.value.toLocaleString("en", { maximumFractionDigits: 2 })}${unitLabel(rv.unit) && rv.unit !== "count" ? ` ${unitLabel(rv.unit)}` : ""}` : "";
  const rivalHtml = rv ? `<div class="m-assume m-rival">Two readings differ: ${esc(rv.kind === "gemini" ? `${rv.who} estimated ${rvText} by eye (not measured).`
    : rv.kind === "ours" ? `our own ${rv.who} read ${rvText}.` : `a second reading (${rv.who}) gave ${rvText}${rv.quote ? `, quoting ‘${rv.quote}’` : ""}.`)}</div>` : "";
  const whereHtml = places.length ? `<div class="m-where"><span class="m-wlabel">Where it is on the ${sheet()} (tap to zoom):</span>${places.slice(0, 8)
    .map((p, i) => `<button type="button" class="m-chip" data-w="${i}">${p.label ? `${p.label} · ` : ""}${esc(p.note)}</button>`).join("")}${places.length > 1
    ? `<button type="button" class="m-chip" data-w="all">${places.length > 8 ? `All ${places.length}` : "All"}</button>` : ""}</div>` : "";
  r.innerHTML = `
    <div class="m-hero">
      <div class="m-q">${tapName ? `<b>${esc(tapName)}</b> · the ${state.rooms[state.rooms.length - 1]?.zone ? "area" : "room"} you tapped` : esc(tappedLabel ? "The room you tapped" : q)}</div>
      ${listed && !useMine ? listHtml(listed) : big && !(guessNum && hideWords) ? `<div class="m-value"><span class="m-big${big.length > 9 ? " long" : ""}">${guessNum ? "≈ " : ""}${esc(big)}</span>${unit && (!words || useMine) ? `<span class="m-unit">${esc(unit)}</span>` : ""}</div>`
        : `<div class="m-value"><span class="m-big name">${blocked ? "Not a floor plan" : size ? "Not measured yet" : "No answer"}</span></div>`}
      <div class="m-sentence">${esc(useMine ? mine!.how : blocked ? `This ${sheet()} shows ${/^[aeiou]/.test(np!.kind) ? "an" : "a"} ${np!.kind}${np!.title ? ` (‘${np!.title}’)` : ""}, not a floor plan: ${isArea(a, q) ? "floor area" : "rooms and doors"} cannot be measured on it.${npGuess ? ` A rough guess said ≈ ${npGuess} (not measured).` : ""}` : rejected ? rejectedWhy(a, q, rejected) : printed && printed === dims ? `Added up from the dimensions printed on the ${sheet()}: ${dims}.` : printed && printed === sum ? `Added up from the area table printed on the ${sheet()}: ${sum}.` : printed ? `As printed on the ${sheet()}: “${printed}”.` : a.solver === "tap" && state.lines.length ? `The line you tapped is drawn on the ${sheet()}. Set the scale to measure it: tap both ends of the scale bar or of a printed dimension.` : a.solver === "tap" && state.rooms.length ? `The room you tapped is outlined on the ${sheet()}. Set the scale to measure it: tap both ends of the scale bar or of a printed dimension.` : guessNum && size && measure && listed ? `Each line as read from the ${sheet()}, not measured on it: tap the ${tapNoun(q)} to measure one.` : guessNum && size && measure ? (whole ? `An estimate for the whole ${sheet()}, not measured on it. To measure one room, tap it.` : `Not measured on ${sheet() === "drawing" ? "the drawing" : "your photo"} yet: tap the ${tapNoun(q)} to outline it.`) : sentence(a, q))}</div>
      <div class="hero-badges m-badges">
        <span class="conf ${c.cls}" title="calibrated confidence ${Math.round((a.confidence ?? 0) * 100)} %">${esc(useMine ? (scaleGuessed(state.scale) ? "Best estimate" : state.scale?.user ? "Measured by you" : sheet() === "drawing" ? "Measured on the drawing" : mine?.room && state.rooms[state.rooms.length - 1]?.matched ? "Measured on the drawing on file" : "Measured on the photo") : noNumber ? "No number yet" : c.word)}</span>
        ${size && !blocked && !scaleOk && !(printed && !useMine) && !(whole && !useMine && outlines !== 1) ? `<button type="button" class="scale-unknown" id="scalePill">Scale unknown — set it</button>` : ""}
        ${size && scaleOk ? `<span class="m-scale" title="${esc(state.scale!.source)}">${esc(scalePill(state.scale!))}</span>` : ""}
        <span class="m-tier t-${tier.tier}" title="Cost tier ${esc(tier.word.toLowerCase())}: ${esc(tier.why)}${again ? ". This time it came from our answer cache: no new model calls" : ""}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= (tier.tier === "cheap" ? 1 : tier.tier === "mid" ? 2 : 3) ? "on" : ""}"></i>`).join("")}</span>${esc(tapped ? "Cheap, no AI" : costWords(a, tier.tier))}${again ? " <small>(cached)</small>" : ""}</span>
        ${dataLineHtml(tapped ? ({ value: null, evidence: [], trace: privacy.on ? ["data: private mode"] : [], cost: {} } as unknown as Answer) : a, esc)}
      </div>
      ${scope ? `<div class="m-assume m-scope"><b>Scope:</b> ${esc(calm(scope.line))}${scope.alt ? ` · other way: ${esc(scope.alt)}` : ""}</div>` : ""}
      ${rivalHtml}
      ${mine?.room && barNote() ? `<div class="m-assume">${esc(barNote())}</div>` : ""}
      ${state.answeredOn && !mine?.room ? `<div class="m-assume">Answered on the drawing on file that matches your photo (${esc(state.answeredOn)}); its marks are drawn onto your photo.</div>` : ""}
      ${before || tappedLabel ? `<div class="m-assume m-before">${esc(before || `You asked “${q}”.`)}</div>` : ""}
      ${assume && c.cls.startsWith("est") && !mine?.room ? `<div class="m-assume">Assumed: ${esc(calm(assume)).slice(0, 220)}</div>` : ""}
      ${(a.missing ?? []).length && c.cls.startsWith("est") && !mine?.room ? `<div class="m-assume">To be sure: ${esc(calm((a.missing ?? [])[0])).slice(0, 160)}</div>` : ""}
    </div>
    ${whereHtml}
    <div class="m-actions">
      ${size && tapLine ? `<button type="button" class="m-btn" id="lineBtn">Measure another length</button>` : size && !blocked ? `<button type="button" class="m-btn" id="roomBtn">${state.rooms.length ? `Tap another ${tapNoun(q)}` : `Tap the ${tapNoun(q)} to outline it`}</button>` : ""}
      ${size && !blocked ? `<button type="button" class="m-btn" id="scaleBtn">${state.scale?.user ? "Set the scale again" : "Set the scale"}</button>` : ""}
      <button type="button" class="m-btn ghost" id="again">Ask something else</button>
      <button type="button" class="m-btn ghost" id="startOver">Start over</button>
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
  $("startOver").addEventListener("click", () => startOver());
  marksChip();
}

/** captain 01:18 'Start over' (the desktop's): the sheet as first opened — no answer, no marks, no tapped room or line,
 *  no scale tool, the whole sheet in view. The file, its recognised drawing on file and what was read about its scale
 *  stay (the phone keeps no corrections to forget). */
function startOver() {
  state.busy?.abort();
  closePress();
  Object.assign(state, { answer: null, question: "", scale: null, tool: null, taps: [], rooms: [], lines: [], answeredOn: "", barNote: "" });
  marksHidden = false;
  setTool(null);
  view.clearMarks();
  $("result").hidden = true;
  $<HTMLInputElement>("q").value = "";
  document.body.dataset.step = "ask";
  zoomFocus(null);
  marksChip();
  $("stage").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- the marks pill over the plan (captain 01:16 (6), the desktop's W5 marksChip: '46 doors marked · Hide ×',
// 'Laundry selected ×'; W5's .marks-chip look from style.css) ----------
let marksHidden = false;
/** what the answer's marks are, in words: '36 doors', 'The answer's outline', 'The answer's 4 places' */
function marksWhat(a: Answer, k: number): string {
  const noun = /^(count|n|items?|pcs)?$/i.test(String(a.unit ?? "")) && typeof a.value === "number" ? countNoun(a, state.question, k) : null;
  if (noun) return `${k} ${noun}`;
  // the desktop's words (W5 marksWhat): 'The 77 areas marked' for a whole-floor area (was 'The answer's 77 outlines')
  if (a.intent?.task === "area") return k <= 3 ? "The measured area" : `The ${k} areas`;
  if (a.intent?.task === "length") return "The measured length";
  const poly = (a.evidence ?? []).filter(onThis).some((e) => (e.polygon?.length ?? 0) > 2);
  return k === 1 ? (poly ? "The answer's outline" : "The answer's place") : `The answer's ${k} ${poly ? "outlines" : "places"}`;
}
function marksChip() {
  const el = $("marksChip");
  const a = state.answer;
  const room = state.rooms[state.rooms.length - 1];
  const k = a && state.file ? (a.evidence ?? []).filter(onThis).length : 0;
  let html = "";
  let act: (() => void) | null = null;
  if (!$("pressMenu").hidden || state.tool || document.body.dataset.step === "asking") html = "";
  else if (room) {
    html = `<span class="mc-what">${esc(room.name ?? (room.zone ? "Area" : "Room"))} selected</span><button type="button" class="mc-btn" aria-label="Clear the room you tapped">×</button>`;
    act = () => {
      state.rooms = [];
      drawMine();
      if (state.answer?.solver === "tap") {
        state.answer = null;
        $("result").hidden = true;
        document.body.dataset.step = "ask";
      } else if (state.answer) renderResult();
    };
  } else if (a && a.solver !== "tap" && k && !marksHidden) {
    html = `<span class="mc-what">${esc(marksWhat(a, k))} marked</span><button type="button" class="mc-btn" aria-label="Hide the answer's marks">Hide ×</button>`;
    act = () => { marksHidden = true; view.clearMarks("answer"); };
  } else if (a && a.solver !== "tap" && k && marksHidden) {
    html = `<button type="button" class="mc-btn mc-show">Show ${esc(marksWhat(a, k).replace(/^The /, "the "))}</button>`;
    act = () => { marksHidden = false; drawEvidence(a); };
  }
  el.hidden = !html;
  el.innerHTML = html;
  // the answer is framed below the pill (UX 02:15: it covered the dorm's mark 1 and S-100F's mark 3); set before the
  // answer's zoom (renderResult runs before drawEvidence), not re-framed later (a cleared room must not make it jump)
  const ph = $("photo");
  view.topInset = html ? Math.max(0, el.offsetTop + el.offsetHeight + 8 - ph.offsetTop) : 0;
  el.querySelector("button")?.addEventListener("click", (e) => {
    e.stopPropagation();
    act?.();
    marksChip();
  });
}

/** The desktop card's cost words (W5 customer.ts tierHtml; captain 21:39: concrete and neutral, not a red 'Expensive';
 *  UX 00:53 NEW-4): '≈ $0.01 · 2 Gemini calls', 'Our GPU · 3 calls', 'Cheap, no AI'. Same tiers (costTier). */
function costWords(a: Answer, tier: "cheap" | "mid" | "high"): string {
  const c = (a.cost ?? {}) as Record<string, number>;
  const n = (k: string) => Math.max(0, (Number(c[k] ?? 0) || 0) - (Number(c[`prep_${k}`] ?? 0) || 0));
  const calls = n("api_calls");
  const usd = n("gemini_usd") || n("usd") || (n("api_tokens_in") * 2 + n("api_tokens_out") * 12) / 1e6; // Gemini 3.1 Pro list price per M tokens
  const who = (a.trace ?? []).some((l) => /gemini/i.test(String(l))) || n("gemini_usd") > 0 ? "Gemini" : "hosted model";
  const w = tier === "high" ? `${usd > 0 ? `≈ $${usd < 0.01 ? usd.toFixed(3) : usd.toFixed(2)} · ` : ""}${calls || 1} ${who} call${calls > 1 ? "s" : ""}`
    : tier === "mid" ? `our GPU${n("llm_calls") ? ` · ${n("llm_calls")} call${n("llm_calls") > 1 ? "s" : ""}` : ""}` : "cheap, no AI";
  return w[0].toUpperCase() + w.slice(1);
}
/** Where the scale came from, short, for the pill (UX 00:53: 'printed 1:200 on the A2 sheet it names' said 1:200 twice);
 *  the sentence and the pill's tooltip keep the full source. */
/** the scale pill's words: 'Scale 1:200 · printed on the sheet', 'Scale bar 0–20 m', 'Scale · estimated from door swings' */
function scalePill(s: Scale): string {
  const bar = (s as Partial<BarScale>).bar;
  if (bar && !s.user) return `Scale bar ${bar.replace(/^scale bar\s*/i, "")}`;
  const room = state.rooms[state.rooms.length - 1];
  if (room?.scaleText && !s.user && state.answer?.solver === "tap") return `Scale ${room.scaleText.replace(/ printed on the sheet$/, "")} · ${room.matched ? "of the drawing on file" : "printed on the sheet"}`;
  return /\b(px|pt)\b/.test(s.label) && !s.user ? `Scale ${scaleWhere(s.source)}` : `Scale ${s.label} · ${scaleWhere(s.source)}`;
}
function scaleWhere(src: string): string {
  if (/^set by you/i.test(src)) return "set by you";
  if (/scale bar/i.test(src)) return "from the scale bar";
  if (/door[- ]?swing/i.test(src)) return "estimated from door swings";
  if (/^printed\b|printed on/i.test(src)) return "printed on the sheet";
  return src.length > 40 ? `${src.slice(0, 40).replace(/\s+\S*$/, "")}…` : src;
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
/** A total ADDED UP from a printed area table (E1's area_table: 'sum of 4 printed table rows naming circulation: 914 +
 *  913 + 1,403 + 1,240 SF = 4,470 SF'): the sum itself, shown as the sentence (estimators trust a total with its rows). */
function tableSum(a: Answer): string {
  const t = (a.assumptions ?? []).find((x) => /^sum of \d+ printed (?:table )?rows?\b/i.test(x) && /=\s*[\d,.]+/.test(x));
  return t ? t.slice(t.lastIndexOf(":") + 1).trim() : "";
}
/** A length READ off printed dimension strings: the overall dimension that equals the sum of its chain (AFCEC 'How long
 *  is the building?': "the printed total '323'-4"' = 133'-10" + 55'-8" + 133'-10" = 323.333 ft"), as
 *  '323'-4" = 133'-10" + 55'-8" + 133'-10"', or ''. Printed, so no scale is needed (the card showed 'Scale unknown'). */
function dimSum(a: Answer): string {
  if (!/^(m|mm|cm|ft|feet|in|inches)$/i.test(String(a.unit ?? "").trim())) return "";
  for (const t of a.assumptions ?? []) {
    const m = t.match(/printed total '(.+?)' = (.+?) = [\d.,]+ ?(?:m|mm|cm|ft|feet)\b/i);
    if (m) return `${m[1]} = ${m[2]}`;
  }
  return "";
}
/** read off a printed area table (rows' boxes): no room to trace around them */
const fromTable = (a: Answer) => !!tableSum(a) || (a.assumptions ?? []).some((x) => /printed in the sheet's own area table|printed area table/i.test(x))
  || (a.evidence ?? []).some((e) => /\(printed area table\b/i.test(e.note ?? ""));
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
    return { room: true, value: fmtM(m), how: `The line you tapped on the ${sheet()}, at ${scaleWords(s)}.${imperial(s) ? ` That is ${feet(m)} ft.` : ""}${tiltNote(ln)} Check that its ends sit on the wall's ends.` };
  }
  const last = state.rooms[state.rooms.length - 1];
  if (last) {
    const m2 = roomM2(last, s);
    if (last.m2 !== undefined && !s.user) { // the takeoff's room: its number, not measured again here
      const nm = last.name ? `The ${last.name}` : `The ${last.zone ? "area" : "room"} you tapped`;
      const how = last.zone ? "measured it (its outline and printed dimensions)" : "traced it along its walls";
      return length ? null : { room: true, value: roomFmt(last, s), how: last.matched
        ? `${nm}, from the drawing on file that matches your photo (${last.matched}): its takeoff ${how} at ${last.scaleText ?? "its own scale"}. The outline is drawn onto your photo: check it.`
        : `${nm}, as this sheet's takeoff ${how}, at ${last.scaleText ?? "its own scale"}. The same outline and number as the desktop's takeoff: check it.` };
    }
    const traced = last.method === "takeoff" ? "as this sheet's takeoff traced it along its walls" : last.method === "wall lines" ? "traced along its walls (doorways closed)"
      : last.method === "solid walls" ? "outlined along its solid walls (the floor pattern ignored)" : "outlined";
    return length ? null : { room: true, value: roomFmt(last, s), how: `${last.name ? `The ${last.name}` : "The room you tapped"}, ${last.zone ? "as this sheet's takeoff measured it (its outline and printed dimensions)" : traced} on the ${sheet()}, at ${scaleWords(s, last)}.${imperial(s) ? ` That is ${sqft(m2)} sq ft.` : ""}${tiltNote(last.poly)}${last.around ? " Your tap was on furniture, so this is the room around it." : ""} The outline is traced: check it.${!state.pdf && !last.around && !last.checked && m2 < ISLAND_M2 && !labelFrame(last.poly, m2) ? " If that is furniture (a table, a bed), tap an empty spot of the room." : ""}${labelFrame(last.poly, m2) ? " It looks like the frame around a label: to measure the room around it, tap an empty spot of the room." : ""}` };
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
  if (t && pressAt) closePress();
  state.tool = t;
  state.taps = [];
  drawMine();
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
/** the visitor's rooms, lines and the scale bar they were measured at, on the plan */
function drawMine() {
  view.clearMarks("user");
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r), tagBelow: tagBelow(r.nameBox) });
  // the scale the rooms were measured at, on the photo: the printed bar's two ends
  const sb = state.scale as Partial<BarScale> | null;
  for (const l of state.lines) view.addMark({ kind: "line", pts: l.map(toPx), tone: "user", label: lineTag(l) });
  if (sb?.bar && sb.pts && (state.rooms.length || state.lines.length) && !state.pdf) view.addMark({ kind: "line", pts: sb.pts.map(toPx), tone: "user", label: sb.bar });
  marksChip();
}
/** what the tap tool outlines, in the question's words: a slab / zone / roof is an 'area', anything else a 'room' */
const tapNoun = (q: string) => (/\b(slab|zone|region|footprint|deck|roof|site|plot|pad|paving|area of the \d)/i.test(q) ? "area" : "room");
function toolHint(t: string) {
  $("toolHint").textContent = t;
}
$("toolDone").addEventListener("click", () => setTool(null));

/** a tapped room's m²: the takeoff's own number (unless the visitor set a scale), else its outline at the scale */
const roomM2 = (r: Room, s: Scale): number => (r.m2 !== undefined && !s.user ? r.m2 : areaM2(r.poly, s));
/** a tapped room's m² in words: the takeoff's number as the desktop prints it (247.73, not ≈ 248), else fmtM */
const roomFmt = (r: Room, s: Scale): string => (r.m2 !== undefined && !s.user ? r.m2.toLocaleString("en-US", { maximumFractionDigits: 2 }) : fmtM(roomM2(r, s)));
/** the scale in words for a sentence (UX 01:12 C-1: never '1 m = 51 px'): the visitor's, the scale bar's, a printed 1:N */
function scaleWords(s: Scale, r?: Room): string {
  if (s.user) return "your scale";
  if (r?.scaleText) return r.scaleText;
  const bar = (s as Partial<BarScale>).bar;
  if (bar) return `the ${bar} printed on the drawing`;
  return /\b(px|pt)\b/.test(s.label) ? `a scale ${s.source}` : `${s.label} (${s.source})`;
}
function roomTag(r: Room): string | undefined {
  const s = state.scale;
  if (!s) return undefined;
  const m2 = roomM2(r, s);
  return `${roomFmt(r, s)} m²${imperial(s) ? ` · ${sqft(m2)} sq ft` : ""}`;
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
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r), tagBelow: tagBelow(r.nameBox) });
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

/** A tap on FURNITURE (DfE Level 01 Music Room, a tap in its middle: the table traced as a 1.07 m² 'room'; the room is
 *  57.93): an outline under ISLAND_M2 at the scale is traced again just outside it on its 4 sides (one /api/shapes call
 *  with 4 items); an outline from there that holds the tap AND the small outline is the room around it (the small one
 *  was an island: a table, a bed, a kitchen unit), the smallest such one wins (a leak into the corridor is bigger). A
 *  small ROOM (WC, store) has walls all round: the traces outside it are other rooms that do not hold it, so it stays. */
const ISLAND_M2 = 8; // DfE Level 01, 77 taps scattered in 11 big rooms: right room (±15 %) 64 → 70 (4 m²: 66); 37 label taps: 0 escapes
const inPoly = (poly: Pt[], [x, y]: Pt) => {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
};
/** The outline surrounds point c: rays from c in 8 directions all meet its boundary. A flood fill's contour wraps a
 *  table + chairs as an INLET (the Music Room's: the tap was outside its polygon), so a point-in-polygon test is too strict;
 *  a WC beside an L-shaped room fails this on the rays that point away from the room. */
function surrounds(poly: Pt[], [cx, cy]: Pt): boolean {
  if (inPoly(poly, [cx, cy])) return true;
  let hit = 0;
  for (let k = 0; k < 8; k++) {
    const dx = Math.cos((k * Math.PI) / 4), dy = Math.sin((k * Math.PI) / 4);
    const meets = poly.some(([x1, y1], i) => {
      const [x2, y2] = poly[(i + 1) % poly.length], ex = x2 - x1, ey = y2 - y1, den = dx * ey - dy * ex;
      if (Math.abs(den) < 1e-12) return false;
      const t = ((x1 - cx) * ey - (y1 - cy) * ex) / den, u = ((x1 - cx) * dy - (y1 - cy) * dx) / den;
      return t > 0 && u >= 0 && u <= 1;
    });
    if (meets) hit++;
  }
  return hit === 8;
}
// ---------- the tapped room's printed name (UX 00:10 S-M9: 'The room you tapped · 16.3 m²' did not say it was the Store) ----------
const AREA_TOKEN = /^(?:[\d.,]+\s*)?(?:m²|m2|sq\.?|ft²|ft2|sf|sqft|sq\.?\s*ft)$/i;
const TAG_TOKEN = /^[A-Z]{1,4}-?\d{1,4}[A-Z]?$/; // a door / window / equipment tag inside the room: D12, FD30, W-3
const DECIMAL = /^\d+[.,]\d+$/; // an area or a dimension printed beside the name ('Store 16.30'), not part of it
const letterCount = (t: string) => (t.match(/\p{L}/gu) ?? []).length;
/** drafting notes are not room names: '(TYP.)' beside a check valve on USACE M-101A was offered as 'Area of (TYP.)' */
const NOTE_WORD = /^(typ|typical|sim|similar|nts|ref|see|note|notes|detail|section|elev|dn|up|eq|cl|min|max|approx|existing|new|from|to)\.?$/i;
// a name of 3 letters or fewer is a known room abbreviation ('WC', 'D&T'); 'MAV' beside a valve on M-101A is not
const SHORT_ROOM = /^(wc|w\.c\.?|it|ict|pe|dt|d&t|av|lab|gym|bar|den|spa|mdf|idf|ups|kök|bad|wic|sh|sr|ws)\b/i;
const isNameLine = (t: string): boolean => /^[\p{L}\d]/u.test(t) && !NOTE_WORD.test(t.replace(/[()[\]]/g, "").trim())
  && letterCount(t) >= 0.5 * t.replace(/\s/g, "").length && (letterCount(t) > 3 || SHORT_ROOM.test(t.trim()));
/** 'STAFF SOCIAL ROOM' -> 'Staff Social Room' (a card heading in capitals shouts); 'WC', 'D&T' stay */
function tidyName(t: string): string {
  const s = t.replace(/\s+/g, " ").trim();
  if (s !== s.toUpperCase() || letterCount(s) <= 3) return s;
  return s.split(" ").map((w) => (/^(AND|OF|THE|FOR|TO)$/.test(w) ? w.toLowerCase() : letterCount(w) <= 3 ? w : w[0] + w.slice(1).toLowerCase())).join(" ");
}
/** The name printed inside an outline: the text line nearest its middle (by text height: a name is printed bigger than a
 *  note), plus the lines just above / below it in the same column ('Staff Social' / 'Room'); door tags, areas and
 *  dimensions are left out. Returns the name and its box (evidence units) so the m² tag can sit below it. */
function roomNames(poly: Pt[], words: Word[] | null, tap?: Pt): { name: string; box: number[] }[] {
  if (!words?.length) return [];
  const onTap = (w: Word) => !!tap && Math.abs(w.at[0] - tap[0]) <= w.hw * 1.1 && Math.abs(w.at[1] - tap[1]) <= Math.max(w.hh, 1) * 1.6;
  const toks = words.filter((w) => (inPoly(poly, w.at) || onTap(w)) && /[\p{L}\d]/u.test(w.t) && !TAG_TOKEN.test(w.t.trim()) && !AREA_TOKEN.test(w.t.trim()))
    .map((w) => ({ ...w, t: w.t.trim(), hh: w.hh > 0 ? w.hh : Math.max(1, w.hw / Math.max(2, w.t.length)) }))
    .sort((a, b) => a.at[0] - a.hw - (b.at[0] - b.hw));
  const rows: Word[][] = [];
  for (const w of toks) {
    const r = rows.find((r) => {
      const l = r[r.length - 1], h = Math.max(l.hh, w.hh), gap = w.at[0] - w.hw - (l.at[0] + l.hw);
      return Math.abs(l.at[1] - w.at[1]) < h * 0.7 && gap < h * 2.6 && gap > -h * 2;
    });
    if (r) r.push(w);
    else rows.push([w]);
  }
  const boxOf = (ws: Word[]) => [Math.min(...ws.map((w) => w.at[0] - w.hw)), Math.min(...ws.map((w) => w.at[1] - w.hh)), Math.max(...ws.map((w) => w.at[0] + w.hw)), Math.max(...ws.map((w) => w.at[1] + w.hh))];
  const lines = rows.map((r) => {
    // a decimal at either end is an area / a dimension beside the name, not part of it
    while (r.length && DECIMAL.test(r[r.length - 1].t)) r = r.slice(0, -1);
    while (r.length && DECIMAL.test(r[0].t)) r = r.slice(1);
    const b = r.length ? boxOf(r) : [0, 0, 0, 0];
    return { t: r.map((w) => w.t).join(" "), b, hh: Math.max(0, ...r.map((w) => w.hh)), c: [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2] as Pt };
  }).filter((l) => letterCount(l.t) >= 3 && /\p{L}{2}/u.test(l.t) && isNameLine(l.t));
  // nearest the outline's middle or the finger (a leaked outline's middle can be another room's name: a press under
  // 'Staff Social Room' on DfE Level 01 was named 'Toilet'), in text heights
  const c = centroid(poly);
  const score = (l: (typeof lines)[number]) => Math.min(Math.hypot(l.c[0] - c[0], l.c[1] - c[1]), tap ? Math.hypot(l.c[0] - tap[0], l.c[1] - tap[1]) : Infinity) / l.hh;
  const out: { name: string; box: number[] }[] = [];
  const used = new Set<(typeof lines)[number]>();
  for (const best of [...lines].sort((a, b) => score(a) - score(b))) {
    if (used.has(best) || out.length >= 2) continue;
    const named = nameBlock(best, lines);
    named.block.forEach((l) => used.add(l));
    out.push({ name: named.name, box: named.box });
  }
  return out;
}
function nameBlock<L extends { t: string; b: number[]; hh: number; c: Pt }>(best: L, lines: L[]): { name: string; box: number[]; block: L[] } {
  const block = [best];
  for (let k = 0; k < 2; k++) {
    const bb = [Math.min(...block.map((l) => l.b[0])), Math.min(...block.map((l) => l.b[1])), Math.max(...block.map((l) => l.b[2])), Math.max(...block.map((l) => l.b[3]))];
    const next = lines.find((l) => !block.includes(l) && l.hh / best.hh > 0.7 && l.hh / best.hh < 1.43 && l.b[0] < bb[2] && l.b[2] > bb[0]
      && Math.max(l.b[1] - bb[3], bb[1] - l.b[3]) < best.hh * 1.6);
    if (!next) break;
    block.push(next);
  }
  block.sort((a, b) => a.c[1] - b.c[1]);
  let name = tidyName(block.map((l) => l.t).join(" "));
  if (name.length > 40) name = `${name.slice(0, 40).replace(/\s+\S*$/, "")}…`;
  return { name, block, box: [Math.min(...block.map((l) => l.b[0])), Math.min(...block.map((l) => l.b[1])), Math.max(...block.map((l) => l.b[2])), Math.max(...block.map((l) => l.b[3]))] };
}
/** The printed name of the room outlined at the tap, checked: traced again from the name itself, it must give the same
 *  room (area within ±30 %, holding the tap). An outline has no holes, so a corridor looping round a block holds the
 *  block's room names (DfE Level 01: a corridor tap was named 'Independant Life Skills Room', 239 m²): no name beats the
 *  wrong one. One /api/shapes call per candidate (≤ 2), CPU only. */
async function checkedName(file: FileInfo, poly: Pt[], tap: Pt, scale: Scale | null): Promise<{ name: string; box: number[] } | null> {
  const cands = roomNames(poly, await sheetWords(file), tap);
  const n = scaleN(scale);
  for (const c of cands) {
    const at: Pt = [(c.box[0] + c.box[2]) / 2, (c.box[1] + c.box[3]) / 2];
    // a picture: E1's tracer from the label (its own use: 'around its label') on the traced copy, as the tap was; the
    // flood fill found no room on X1's scan of the DfE sheet, so the hall stayed unnamed
    let q = !state.pdf && scale ? await lineRoom(file.path, at, scale) : null;
    if (!q) {
      const res = await roomShapes(file.path, state.page, space(), [{ i: 0, bbox: [at[0] - 1, at[1] - 1, at[0] + 1, at[1] + 1], ...(n > 0 ? { scale_n: n } : {}) }]).catch(() => null);
      q = res && trusted(res, res.shapes[0]) ? (res.shapes[0]?.poly as Pt[] | null | undefined) ?? null : null;
    }
    if (!q || q.length < 3) continue;
    const k = polyArea(q) / Math.max(1e-9, polyArea(poly));
    if (k > 0.7 && k < 1.43 && surrounds(q, tap)) return c;
  }
  return null;
}
/** A picture's room around the tap along its wall lines (GET /api/m/lineroom, src/mobile/lineroom.py), in its px, or null.
 *  The scale in px per m of the picture: a deskewed answer's scale is not in these px, so only a plain one is sent. */
async function lineRoom(path: string, ev: Pt, s: Scale): Promise<Pt[] | null> {
  if ((s as Partial<FlatScale>).deskewed || !(s.unitsPerM > 0)) return null;
  const u = `/api/m/lineroom?path=${encodeURIComponent(path)}&x=${ev[0].toFixed(0)}&y=${ev[1].toFixed(0)}&ppm=${s.unitsPerM.toFixed(3)}`;
  const d = await fetch(u).then((r) => (r.ok ? r.json() : null)).catch(() => null) as { ok?: boolean; poly?: number[][] } | null;
  return d?.ok && d.poly && d.poly.length > 2 ? d.poly.map(([x, y]) => [x, y] as Pt) : null;
}
/** The sheet's finished takeoff (F2, GET /api/glance: every labelled room's outline, name and m², as the desktop's
 *  right-click and takeoff use them). Kept per sheet; a sheet with no takeoff yet is asked again at the next tap. */
const takeoffMemo = new Map<string, Promise<Glance | null>>();
function sheetTakeoff(path: string, page: number): Promise<Glance | null> {
  const k = `${path}#${page}`;
  if (!takeoffMemo.has(k)) {
    const p = glance(path, page).then((g) => (g.ok && g.rows?.length ? g : null)).catch(() => null);
    void p.then((g) => { if (!g) setTimeout(() => takeoffMemo.delete(k), 30_000); }); // none yet: asked again after 30 s
    takeoffMemo.set(k, p);
  }
  return takeoffMemo.get(k)!;
}
const SQFT = /^(ft2|ft²|sf|sq ?ft)$/i;
/** UX 01:12 C-1: the takeoff's room under the tap (the smallest outline holding it), its name, m² and scale words, or null.
 *  DfE Level 00 PNG hall: 247.73 m² as on the desktop (the flood fill leaked through the hall's doors into the Store and
 *  the Servery strip: 271). */
async function takeoffRoom(path: string, ev: Pt, page = state.page, sp: string = space()): Promise<Room | null> {
  const g = await sheetTakeoff(path, page);
  if (!g) return null;
  let best: { poly: Pt[]; ar: number; m2: number; name: string; lb?: number[]; zone: boolean } | null = null;
  for (const r of g.rows ?? []) {
    // rooms, and the slab zones of a structural sheet (USACE S-100F 150 slab: 61.69 m² as the desktop's chip; taps gave
    // 7.9 / 11.9 m² sub-panels between the slab's dimension lines)
    const zone = r.section === "slab" && /area/.test(r.quantity);
    if (!(zone || (r.section === "room" && r.quantity === "area")) || typeof r.value !== "number" || !(r.value > 0)) continue;
    const m2 = /^(m2|m²|sq ?m)$/i.test(r.unit) ? r.value : SQFT.test(r.unit) ? r.value * 0.09290304 : 0;
    if (!m2) continue;
    for (const e of r.evidence ?? []) {
      const poly = e.polygon as Pt[] | null | undefined;
      if (!poly || poly.length < 3 || e.file !== path || (e.page ?? 0) !== page || (e.bbox_space && e.bbox_space !== sp)) continue;
      if (!inPoly(poly, ev)) continue;
      const ar = polyArea(poly);
      if (!best || ar < best.ar) best = { poly, ar, m2, name: String(r.item ?? "").replace(/\s+/g, " ").trim(), lb: (r as { label_box?: number[] }).label_box, zone };
    }
  }
  if (!best) return null;
  const n = String(g.scale ?? "").match(/^1\s*:\s*\d+/)?.[0]?.replace(/\s/g, "");
  return { poly: best.poly, method: "takeoff", m2: best.m2, name: best.name ? (best.zone ? best.name : calm(best.name)) : undefined, nameBox: best.lb, checked: true, zone: best.zone,
    scaleText: n ? `${n} printed on the sheet` : "the sheet's own scale" };
}
/** The takeoff room's scale for a sheet with none yet: its outline px per m (so later taps and lines measure alike). */
function takeoffScale(r: Room): Scale | null {
  if (r.m2 === undefined || !(r.m2 > 0)) return null;
  const u = Math.sqrt(polyArea(r.poly) / r.m2);
  if (!(u > 0)) return null;
  const n = r.scaleText?.match(/^1:\d+/)?.[0];
  // a matched photo: px per m on the photo for this room (the sheet's 1:N does not hold on the photo's pixels)
  if (r.matched) return { unitsPerM: u, label: scaleLabel(u, "image_px"), source: `from the matching drawing on file (${n ?? "its own scale"})` };
  return { unitsPerM: u, label: n ?? scaleLabel(u, space()), source: n ? `printed ${n} on the sheet (as its takeoff read it)` : "from this sheet's takeoff" };
}
/** A picture with a hatched floor (parquet, tiles: UX 01:05 NEW-R1): the room on its SOLID walls only (src/mobile/wallfill.py
 *  over W5's room_shape.outline), in its px, or null. Asked only when the normal outline is a strip between pattern lines. */
async function solidRoom(path: string, ev: Pt): Promise<Pt[] | null> {
  const u = `/api/m/wallfill?path=${encodeURIComponent(path)}&x=${ev[0].toFixed(0)}&y=${ev[1].toFixed(0)}`;
  const d = await fetch(u).then((r) => (r.ok ? r.json() : null)).catch(() => null) as { ok?: boolean; poly?: number[][] } | null;
  return d?.ok && d.poly && d.poly.length > 2 ? d.poly.map(([x, y]) => [x, y] as Pt) : null;
}
/** an outline thinner than a doorway: a strip between floor-pattern lines or a gap between two strokes, not a room.
 *  Its mean width 2A/P below 0.45 m at a known scale, else below 1 % of the picture's long side. */
function strip(poly: Pt[], s: Scale | null, frac = 0.01, minM = 0.45): boolean {
  const w = (2 * polyArea(poly)) / Math.max(1e-9, polyLength([...poly, poly[0]]));
  if (s && !scaleGuessed(s)) return w / s.unitsPerM < minM;
  const long = state.pdf ? 0 : Math.max(view.w, view.h) / Math.max(1e-9, state.k);
  return long > 0 && w < frac * long;
}
/** the printed words of the shown sheet: a PDF page's from pdf.js, a picture's from its traced copy / text layer */
const sheetWords = (file: FileInfo): Promise<Word[] | null> => (state.pdf
  ? import("./pdfpage").then((m) => m.pageRuns(fileUrl(file.path), state.page)).catch(() => null)
  : imageWords(file.path));
/** a tapped room's m² tag goes just below its printed name (it covered it: UX S-M9), in image px */
function tagBelow(b?: number[]): Pt | undefined {
  if (!b) return undefined;
  const cs = [toPx([b[0], b[1]]), toPx([b[2], b[1]]), toPx([b[0], b[3]]), toPx([b[2], b[3]])];
  return [(Math.min(...cs.map((q) => q[0])) + Math.max(...cs.map((q) => q[0]))) / 2, Math.max(...cs.map((q) => q[1]))];
}

async function aroundIsland(path: string, ev: Pt, poly: Pt[], s: Scale): Promise<{ poly: Pt[]; method?: string } | null> {
  const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  // 0.3 m past the island's edge (a chair is ~0.45 m deep: the 4 probes rarely all land on chairs)
  const m = Math.max(0.3 * s.unitsPerM, 0.12 * Math.max(x1 - x0, y1 - y0));
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, r = Math.max(2, 4 / state.k), n = scaleN(s);
  const probes: Pt[] = [[x0 - m, cy], [x1 + m, cy], [cx, y0 - m], [cx, y1 + m]];
  const items = probes.map((p, i) => ({ i, bbox: [p[0] - r, p[1] - r, p[0] + r, p[1] + r], ...(n > 0 ? { scale_n: n } : {}) }));
  // a picture: one call PER probe. room_shape.py takes 2+ items of one call that land in one region for a fill that
  // leaked through a door and thickens the walls until they fail, and around a table all 4 probes are in the same room
  // (DfE Level 00 PNG: no flood fill to step out to). A PDF keeps the one call (E1's wall lines answer it first; measured so)
  const res: { shapes: (RoomShape | null)[] } = state.pdf ? await roomShapes(path, state.page, space(), items)
    : { shapes: await Promise.all(items.map((it) => roomShapes(path, state.page, space(), [it]).then((x) => x.shapes[0] ?? null).catch(() => null))) };
  const small = polyArea(poly);
  let best: { poly: Pt[]; method?: string } | null = null;
  for (const sh of (res.shapes ?? []).filter((x): x is RoomShape => !!x)) {
    const q = sh?.poly as Pt[] | null | undefined;
    if (!q || q.length < 3 || !trusted(res, sh)) continue;
    const off = notThisRoom(q, ev); // the sheet-share checks; 'holds the tap' is surrounds() below (the tap may sit in an inlet)
    if (off && off !== "does not hold the tap") continue;
    const a = polyArea(q);
    if (a < 2 * small || !surrounds(q, [cx, cy])) continue;
    // E1's wall-line trace first (doorways closed; Science: 56.1 m² vs a 45.6 m² flood fill around the benches), then the smallest
    const better = !best || (!!sh.method !== !!best.method ? !!sh.method : a < polyArea(best.poly));
    if (better) best = { poly: q, method: sh.method };
  }
  return best;
}

async function traceRoom(ev: Pt) {
  if (!state.file) return;
  toolHint("Outlining the room…");
  const t0 = performance.now();
  view.addMark({ kind: "dot", pts: [toPx(ev)], tone: "user" });
  const r = Math.max(2, 4 / state.k);
  try {
    const file = state.file;
    // the sheet's finished takeoff first (UX 01:12 C-1): the room it traced under the tap, with the desktop's name and m²
    const tk = (await matchedRoom(ev)) ?? (await takeoffRoom(file.path, ev));
    if (state.file !== file) return;
    if (tk) {
      state.rooms.push(tk);
      state.scale = state.scale ?? takeoffScale(tk);
      toolHint("");
      setTool(null);
      state.t.room = performance.now() - t0;
      if (state.answer?.solver === "tap") state.t.ask = state.t.room;
      renderResult();
      zoomToRoom();
      if (sideways()) window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
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
    let poly = res.shapes[0]?.poly as Pt[] | null | undefined;
    let method = res.shapes[0]?.method;
    let off = !poly || poly.length < 3 ? "no closed room" : trusted(res, res.shapes[0]) ? notThisRoom(poly, ev) : "flood fill on a rotated page";
    if (off && !state.pdf) {
      // a picture: the wall lines of its traced copy may close a room the flood fill could not (X1's scan of the DfE
      // sheet: broken walls, 'No closed room there' in the Multi-Purpose Hall); they need a scale (door radii)
      toolHint("Looking for the room's walls along the drawing's lines…");
      const s = state.scale ?? state.fileScale ?? (await estimatedScale());
      if (state.file !== file) return;
      const lr = s ? await lineRoom(file.path, ev, s) : null;
      if (state.file !== file) return;
      if (lr && !notThisRoom(lr, ev)) {
        [poly, method, off] = [lr, "wall lines", null];
        state.scale = state.scale ?? s;
      }
    }
    // a picture whose outline is a strip between floor-pattern lines (parquet, tiles: UX NEW-R1), or none: the room on
    // its solid walls only (UX's Nordic flat: living room / bedroom / hall whole instead of one floorboard, ~0.2 s)
    if (!state.pdf && (off || !poly || poly.length < 3 || strip(poly, state.scale ?? state.fileScale))) {
      toolHint("Looking past the floor pattern for the room's walls…");
      const sw = await solidRoom(file.path, ev);
      if (state.file !== file) return;
      if (sw && !notThisRoom(sw, ev) && (!poly || poly.length < 3 || off || polyArea(sw) > 3 * polyArea(poly))) [poly, method, off] = [sw, "solid walls", null];
      toolHint("");
    }
    if (!poly || poly.length < 3) {
      toolHint("No closed room there (an open door or a faint wall?). Tap closer to the middle of the room.");
      return;
    }
    if (off) {
      console.info("w6: outline rejected:", off, res.shapes[0]?.method);
      toolHint("Could not find this room's walls there (the outline ran off the room). Tap closer to the middle of the room, or measure a length with the scale tool.");
      return;
    }
    state.rooms.push({ poly, method });
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
    // a picture at a known scale: the room along its wall lines (E1's measure_room on the upload's traced copy: doorways
    // closed, other rooms' words kept out) instead of the flood fill (DfE Level 00 PNG hall 246.97 m² vs 271, printed
    // 247.73; Laundry 6.95 vs 5.42). The flood fill stays when there is no traced copy or no closed room.
    if (!state.pdf && state.scale && method !== "wall lines" && method !== "solid walls") {
      const lr = await lineRoom(file.path, ev, state.scale);
      if (state.file !== file) return;
      if (lr && !notThisRoom(lr, ev) && !strip(lr, state.scale)) state.rooms[state.rooms.length - 1] = { poly: lr, method: "wall lines" };
    }
    // the outline now kept (the flood fill's, or the wall lines' that replaced it): the checks below are on it
    poly = state.rooms[state.rooms.length - 1].poly;
    // smaller than any room at the known scale: a gap between lines or a symbol (USACE M-101A piping plan: a tap between
    // pipes outlined 0.05 m², '0 sq ft')
    const tiny = state.scale && !scaleGuessed(state.scale) ? areaM2(poly, state.scale) : Infinity;
    if (tiny < 0.3) {
      state.rooms.pop();
      setTool("room");
      toolHint(`That spot is not inside a room (a gap between lines or a symbol: ${tiny < 0.1 ? tiny.toFixed(2) : tiny.toFixed(1)} m²). Tap the middle of a room.`);
      return;
    }
    if (strip(poly, state.scale, 0.005, 0.3)) { // a 0.7 m cupboard stays (2A/P 0.35 m); board / tile strips are 0.1-0.2 m // without a sure scale only a very thin one (a 1.2 m corridor on a big sheet stays)
      state.rooms.pop();
      setTool("room");
      toolHint("That outline is a thin strip between two lines (a floor pattern or a wall's two faces), not a room. Tap the middle of a room, or set the scale and measure a length.");
      return;
    }
    // a small outline with no printed word inside it (a store or a WC has its name inside; a table has none): furniture,
    // so trace the room around it. Needs the sheet's words (the label check: on DfE Level 01 the ray test alone took a
    // Secure Store for the Staff Social Room around it and a Store for a corridor looping round its block): a PDF's from
    // pdf.js, a picture's from its traced copy / text layer (words.py); a picture without them gets the sentence's hint.
    if (state.scale && areaM2(poly, state.scale) < ISLAND_M2) {
      toolHint("Small outline: checking whether that was furniture inside a bigger room…");
      const words = await sheetWords(file);
      if (state.file !== file) return;
      // a word inside the outline, or the tap ON a word (DfE L00 PNG 'Reprographics' is printed wall to wall across its
      // narrow room: the tap on it outlined the half below the text, 1.48 m², which holds no word, and stepped out to 33 m²)
      const labelled = !words || words.some((w) => inPoly(poly, w.at) || lengthMetres([w.at, ev], state.scale!) < 0.5);
      const around = labelled ? null : await aroundIsland(file.path, ev, poly, state.scale).catch(() => null);
      if (state.file !== file) return;
      const last = state.rooms[state.rooms.length - 1];
      state.rooms[state.rooms.length - 1] = around ? { ...around, around: true, checked: true } : { ...last, checked: !!words };
      toolHint("");
    }
    // its printed name ('Store'), for the card's heading and to keep the m² tag off it: checked by a second trace, so it
    // lands a moment after the number (the outline is not kept waiting)
    const room = state.rooms[state.rooms.length - 1];
    void checkedName(file, room.poly, ev, state.scale).then((named) => {
      if (!named || state.file !== file || !state.rooms.includes(room)) return;
      Object.assign(room, { name: named.name, nameBox: named.box });
      if (state.tool) return; // the visitor is tapping again: the next render shows it
      setTool(null);
      if (state.answer) renderResult();
    }).catch(() => undefined);
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
// (localStorage 'be.private.v2', set by the button only; a ?private=1 / 0 link sets the mode on open), so a phone keeps the visitor's choice. Uploads
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
/** UX 03:19: builds before 03:09 wrote 'be.private' = '0' on EVERY page load, so the new Private default never reached a
 *  returning visitor (Stoa's testers, booth phones). The choice lives in 'be.private.v2', written ONLY when the visitor
 *  presses the Private button (`chosen`): the same key as the desktop's (W5 cc447b8b), so both pages share one choice. */
const PRIV_KEY = "be.private.v2";
function setPrivate(on: boolean, tell = true, chosen = false) {
  privacy.on = on;
  const b = $("privBtn");
  b.setAttribute("aria-checked", String(on));
  b.title = on ? "Private mode is on: your photo never leaves our server. Tap to allow Gemini / NVIDIA second readings."
    : "Private mode: your photo never leaves our server (no Gemini or NVIDIA steps)";
  if (chosen) {
    try {
      localStorage.setItem(PRIV_KEY, on ? "1" : "0");
    } catch {
      /* storage off: a reload falls back to the default (Private on), the safe side */
    }
    // UX 03:28 / T2 03:38 (W5 959dec2b on the desktop): the URL is never written on load (a link copied from a phone
    // with Private off turned it off for whoever opened it). A '?private=' the visitor came in with gives way to their
    // own tap (PRIV_KEY keeps the choice).
    const url = new URL(location.href);
    if (url.searchParams.has("private")) {
      url.searchParams.delete("private");
      history.replaceState(history.state, "", url);
    }
  }
  if (tell) privNote(on);
}
$("privBtn").addEventListener("click", () => setPrivate(!privacy.on, true, true));
setPrivate((() => {
  const u = new URL(location.href).searchParams.get("private");
  if (u !== null) return u === "1";
  // S0 03:1x freeze (D62 5, captain's 30 % rule: QA 03:02 default mode = 50 % Expensive on Stoa's PNGs with 0 values changed by
  // Gemini; P1: local = 99.7 % of the hybrid's accuracy): Private is the DEFAULT, Gemini / NVIDIA is the visitor's opt-in
  // (the button; '?private=0' links too). A visitor who switched it off WITH THE BUTTON keeps that (PRIV_KEY = '0').
  try {
    return localStorage.getItem(PRIV_KEY) !== "0";
  } catch {
    return true;
  }
})(), false);

// ---------- T2: 'Delete' = the desktop's 'Delete my files' (privacy feature) for the visitor's own photo / PDF ----------
// Shown only for an upload the visitor sent (never a 'Try a case' sheet) and only when this server has the privacy
// feature (GET /api/feat/privacy/note answers). POST /api/feat/privacy/delete removes the upload folder and the readings
// made from it; the web server also forgets its in-memory answers about it (W5 1d8be27b).
const delState = { can: false };
function delUi() {
  const f = state.file;
  $("delMine").hidden = !(delState.can && f && !state.case && /^upload\//.test(f.path));
}
function delSays(html: string) {
  const n = $("privNote");
  n.innerHTML = `${html}<button type="button" class="pn-x" aria-label="Close">✕</button>`;
  n.hidden = false;
  n.querySelector(".pn-x")?.addEventListener("click", () => (n.hidden = true));
  window.clearTimeout(privTimer);
  privTimer = window.setTimeout(() => (n.hidden = true), 12000);
}
$("delMine").addEventListener("click", async () => {
  const f = state.file;
  if (!f || !/^upload\//.test(f.path)) return;
  const name = f.path.split("/").pop() ?? f.path;
  if (!confirm(`Delete ${name} and the readings made from it from our server? This cannot be undone.`)) return;
  try {
    const r = await fetch("/api/feat/privacy/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ upload: f.path }) });
    if (!r.ok) throw new Error(r.status === 404 ? "not available on this server" : `HTTP ${r.status}`);
    const j = (await r.json()) as { ok: boolean; deleted: string[]; sample?: string; forgotten_in_memory?: number; forgotten_in_api?: number };
    if (j.sample) return delSays(`<b>Nothing to delete.</b> ${esc(name)} is one of our example sheets, shared by every visitor, so it stays (nothing you uploaded is stored with it).`);
    if (!j.ok) return delSays("<b>Some files could not be removed.</b> Please tell the team at the stand.");
    const mem = (j.forgotten_in_memory ?? 0) + (j.forgotten_in_api ?? 0);
    reset();
    const n = j.deleted.length;
    delSays(`<b>Deleted.</b> ${esc(name)} is gone from our server (${n} item${n === 1 ? "" : "s"} incl. readings and page copies)${mem ? `; the server also forgot ${mem} answer${mem === 1 ? "" : "s"} held in memory` : ""}.`);
  } catch (e) {
    delSays(`<b>Could not delete:</b> ${esc((e as Error).message)}`);
  }
});
// the feature list (always 200; the registry's own probe) rather than /api/feat/privacy/note, whose 404 on a server
// without the feature was a console error on every page load (c9walk: 'Errors: 1' on :8131, and the demo :5180 had it off)
void fetch("/api/feat").then((r) => (r.ok ? r.json() : null)).then((j: { features?: Record<string, string> } | null) => {
  delState.can = j?.features?.privacy === "mounted";
  delUi();
}).catch(() => undefined);

document.body.dataset.step = "pick";

// a link straight to a case: mobile.html?case=<id or alias>[&sheet=<1-based number or the sheet's label>]
(() => {
  const u = new URL(location.href).searchParams;
  const c = caseById(u.get("case") ?? "");
  if (!c) return;
  const want = (u.get("sheet") ?? "").trim().toLowerCase();
  const sheets = sheetsOf(c.p);
  const byLabel = sheets.findIndex((s) => (s.label ?? "").toLowerCase() === want);
  const i = /^\d+$/.test(want) && Number(want) >= 1 && Number(want) <= sheets.length ? Number(want) - 1 : byLabel >= 0 ? byLabel : c.sheet;
  void openCase(c.p, i);
})();
