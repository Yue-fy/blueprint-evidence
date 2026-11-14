/**
 * Plan geometry of a Swiss floor (GET /api/plan3d, stoa/geometry/plan3d.py) and the mapping of answer evidence onto it,
 * shared by the 2D viewer (exact room outlines instead of boxes) and the generated 3D view (src/plan3d.ts). No three.js.
 */
import type { Evidence } from "./types";

export type Pt = [number, number];
export type BBox = [number, number, number, number];
export interface PlanRoom { id: string; number: string | null; name: string | null; area: number; unit: string | null; kind: string; bbox: BBox; poly: Pt[]; anchor: Pt }
export interface PlanGeo {
  ok: boolean;
  error?: string;
  bbox: BBox;
  heights: { wall: number; door: number; sill: number; head: number };
  assumptions: string[];
  walls: Pt[][];
  rooms: PlanRoom[];
  /** gap: the wall opening the door sits in (plan3d.py, newer payloads only): a lintel goes over it */
  doors: { id: string; bbox: BBox; leaf: Pt[] | null; gap?: Pt[] | null }[];
  windows: { id: string; bbox: BBox; foot: Pt[] }[];
  units: { label: string | null; bbox: BBox; poly: Pt[] }[];
  /** the DXF's FIXTURE layer (counters, basins, WCs; newer payloads only): low blocks, height estimated by room kind */
  fixtures?: { poly: Pt[]; h: number; room_kind: string }[];
  pdf: { file: string; s: number; a: number; b: number; page_w?: number; page_h?: number } | null;
  png: { file: string; k: number } | null;
  dxf: string;
}
export type PlanKind = "room" | "unit" | "door" | "window";
export interface PlanHit { kind: PlanKind; i: number; bbox: BBox }

const cache = new Map<string, Promise<PlanGeo>>();
/** The floor's geometry (cached per path); rejects with the reason when there is none. */
export function planGeo(path: string): Promise<PlanGeo> {
  if (!cache.has(path)) {
    cache.set(path, (async () => {
      const r = await fetch(`/api/plan3d?path=${encodeURIComponent(path)}`);
      const g = await r.json();
      if (!r.ok || !g.ok) throw new Error(g.error ?? g.detail ?? "no plan geometry");
      return g as PlanGeo;
    })());
    cache.get(path)!.catch(() => cache.delete(path));
  }
  return cache.get(path)!;
}

/** An evidence box in DXF metres (from the floor's PDF / PNG, or a DXF note), or null. */
export function toDxf(g: PlanGeo, ev: Evidence): BBox | null {
  const m = (ev.note ?? "").match(/\[DXF m: ([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\]/);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
  if (!ev.bbox || ev.bbox.length < 4 || !g.pdf?.s) return null;
  let b = ev.bbox as number[];
  if (ev.bbox_space === "image_px") {
    if (!g.png?.k || ev.file !== g.png.file) return null;
    b = b.map((v) => v / g.png!.k);
  } else if (ev.file !== g.pdf.file) return null;
  const { s, a } = g.pdf;
  const xs = [(b[0] - a) / s, (b[2] - a) / s];
  const ys = [(g.pdf.b - b[1]) / s, (g.pdf.b - b[3]) / s];
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

/** The room / apartment / door / window an evidence item boxes: by its note's kind first, then the best overlap (≥ 0.3). */
export function matchEvidence(g: PlanGeo, ev: Evidence): PlanHit | null {
  const b = toDxf(g, ev);
  if (!b) return null;
  const note = ev.note ?? "";
  const want: PlanKind | null = /^\s*door\b/i.test(note) ? "door" : /^\s*window\b/i.test(note) ? "window"
    : /^\s*room\b/i.test(note) ? "room" : /^\s*(apartment|whg\.?|unit)\b|\boutline\b/i.test(note) ? "unit" : null;
  let best: PlanHit | null = null;
  let bestS = 0;
  const consider = (kind: PlanKind, list: { bbox: BBox }[]) => {
    if (want && kind !== want) return;
    list.forEach((x, i) => {
      const s = iou(b, x.bbox);
      if (s > bestS) [best, bestS] = [{ kind, i, bbox: x.bbox }, s];
    });
  };
  consider("room", g.rooms);
  consider("unit", g.units);
  consider("door", g.doors);
  consider("window", g.windows);
  return best && bestS >= 0.3 ? best : null;
}

/** DXF metres → PDF points of the floor's sheet. */
export const dxfToPdf = (g: PlanGeo, [x, y]: Pt): Pt => [g.pdf!.s * x + g.pdf!.a, -g.pdf!.s * y + g.pdf!.b];

export function iou(a: BBox, b: BBox) {
  const ix = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
  const iy = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
  const i = ix * iy;
  const u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i;
  return u > 0 ? i / u : 0;
}
