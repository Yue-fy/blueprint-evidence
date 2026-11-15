// F4 convention switch: wall thickness read from the drawing's INK (pure: works on any grey raster; Node-tested). From
// every traced room edge (the inner wall face) walk outwards along the edge's normal: the first ink within 12 cm is the
// wall's inner face, the wall ends at the last ink before 40 cm of white (so a wall drawn as two lines, a window's
// glazing lines or a solid poché all measure their full thickness). Then keep walking (up to 30 m) on a room mask: a
// ray that reaches another room crossed an INTERNAL wall; one that never does crossed an EXTERNAL wall (the facade, or a
// courtyard's wall). The most common external thickness is the gross convention's wall; the internal one is a check
// against the gap between room outlines (calc.ts measureWalls).
import type { Pt } from "./calc";

export interface InkStat { m: number | null; n: number; share: number; reach?: number | null }
export interface InkWalls { internal: InkStat; external: InkStat; rays: { a: Pt; b: Pt; ext: boolean; m: number }[]; why: string }
// external.reach: from the TRACED outline to the wall's outer face (the gross raster grows the traced outlines by it)

/** Most common value (bin = 2 px), then the median of the values within ±1.5 bins of it; share = that fraction. */
function modeOf(xs: number[], bin: number): { v: number | null; share: number } {
  if (!xs.length) return { v: null, share: 0 };
  const h = new Map<number, number>();
  for (const x of xs) h.set(Math.round(x / bin), (h.get(Math.round(x / bin)) ?? 0) + 1);
  const score = (b: number) => (h.get(b) ?? 0) * 2 + (h.get(b - 1) ?? 0) + (h.get(b + 1) ?? 0);
  const best = [...h.keys()].sort((a, b) => score(b) - score(a) || a - b)[0] * bin;
  const near = xs.filter((x) => Math.abs(x - best) <= 1.5 * bin).sort((a, b) => a - b);
  return { v: near[(near.length - 1) >> 1], share: near.length / xs.length };
}

/** Room ids on a coarse grid (raster px / cell): 0 = no room, i + 1 = room i. */
function roomMask(polys: Pt[][], W: number, H: number, cell: number): { id: Int32Array; w: number; h: number } {
  const w = Math.ceil(W / cell), h = Math.ceil(H / cell);
  const id = new Int32Array(w * h);
  polys.forEach((p, k) => {
    const q = p.map(([x, y]) => [x / cell, y / cell] as Pt);
    const ys = q.map((v) => v[1]);
    for (let y = Math.max(0, Math.floor(Math.min(...ys))); y <= Math.min(h - 1, Math.ceil(Math.max(...ys))); y++) {
      const cy = y + 0.5;
      const xs: number[] = [];
      for (let i = 0; i < q.length; i++) {
        const [ax, ay] = q[i], [bx, by] = q[(i + 1) % q.length];
        if ((ay <= cy && by > cy) || (by <= cy && ay > cy)) xs.push(ax + ((cy - ay) / (by - ay)) * (bx - ax));
      }
      xs.sort((a, b) => a - b);
      for (let i = 0; i + 1 < xs.length; i += 2) for (let x = Math.max(0, Math.ceil(xs[i] - 0.5)); x <= Math.min(w - 1, Math.floor(xs[i + 1] - 0.5)); x++) id[y * w + x] = k + 1;
    }
  });
  return { id, w, h };
}

/**
 * @param ink   true where the raster pixel (x, y) is ink (dark)
 * @param W, H  raster size (px)
 * @param polys the rooms' traced outlines in raster px
 * @param pxPerM raster px per metre
 */
export function inkWalls(ink: (x: number, y: number) => boolean, W: number, H: number, polys: Pt[][], pxPerM: number): InkWalls {
  const out: InkWalls = { internal: { m: null, n: 0, share: 0 }, external: { m: null, n: 0, share: 0 }, rays: [], why: "" };
  const ps = polys.filter((p) => p.length > 2);
  if (ps.length < 2 || !(pxPerM > 5)) return { ...out, why: "too few traced rooms or too coarse a picture" };
  const cell = Math.max(1, pxPerM * 0.1);
  const mask = roomMask(ps, W, H, cell);
  const roomAt = (x: number, y: number) => {
    const cx = Math.floor(x / cell), cy = Math.floor(y / cell);
    return cx < 0 || cy < 0 || cx >= mask.w || cy >= mask.h ? 0 : mask.id[cy * mask.w + cx];
  };
  const isInk = (x: number, y: number) => x >= 0 && y >= 0 && x < W && y < H && ink(Math.round(x), Math.round(y));
  const step = 0.25 * pxPerM, findIn = 0.12 * pxPerM, white = 0.4 * pxPerM, maxWall = 1.0 * pxPerM, far = 30 * pxPerM;
  const ints: number[] = [], exts: number[] = [], reach: number[] = [];
  ps.forEach((p, k) => {
    let s = 0;
    for (let i = 0; i < p.length; i++) s += p[i][0] * p[(i + 1) % p.length][1] - p[(i + 1) % p.length][0] * p[i][1];
    const sg = s > 0 ? 1 : -1; // raster y down: positive shoelace = clockwise on screen; outward normal (sg·dy, -sg·dx)
    let budget = 400;
    for (let e = 0; e < p.length && budget > 0; e++) {
      const a = p[e], b = p[(e + 1) % p.length];
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (L < 0.5 * pxPerM) continue; // short jogs: corners, door reveals
      const nx = (sg * (b[1] - a[1])) / L, ny = (-sg * (b[0] - a[0])) / L;
      const n = Math.floor(L / step);
      for (let j = 1; j < n && budget > 0; j++, budget--) {
        const t = j / n;
        const ox = a[0] + t * (b[0] - a[0]), oy = a[1] + t * (b[1] - a[1]);
        // the inner face: first ink from 3 cm inside the room to 12 cm out
        let d = -0.03 * pxPerM, first = NaN;
        for (; d <= findIn; d += 0.5) if (isInk(ox + nx * d, oy + ny * d)) {
          first = d;
          break;
        }
        if (Number.isNaN(first)) continue; // an opening: no wall here
        let last = first, gap = 0;
        for (d = first + 0.5; d - first <= maxWall && gap < white; d += 0.5) {
          if (isInk(ox + nx * d, oy + ny * d)) {
            last = d;
            gap = 0;
          } else gap += 0.5;
        }
        if (gap < white) continue; // ink all the way: hatching / text / a dense area, not a wall end
        const th = last - first + 0.5; // ink edge to edge (the ray steps 0.5 px)
        // what is behind it: another room within 30 m -> internal
        let hit = 0;
        for (d = last + 0.05 * pxPerM; d <= far; d += cell * 0.9) {
          const r = roomAt(ox + nx * d, oy + ny * d);
          if (r && r !== k + 1) {
            hit = r;
            break;
          }
          if (r === k + 1) break; // came back into the same room (a concave outline): unclear
        }
        const ext = !hit && d > far;
        if (hit) ints.push(th / pxPerM);
        else if (ext) {
          exts.push(th / pxPerM);
          reach.push((last + 0.25) / pxPerM);
        }
        else continue;
        if (out.rays.length < 400) out.rays.push({ a: [ox + nx * first, oy + ny * first], b: [ox + nx * last, oy + ny * last], ext, m: th / pxPerM });
      }
    }
  });
  const bin = 2 / pxPerM;
  const mi = modeOf(ints, bin), me = modeOf(exts, bin);
  out.internal = { m: mi.v, n: ints.length, share: mi.share };
  out.external = { m: me.v, n: exts.length, share: me.share, reach: modeOf(reach, bin).v };
  if (exts.length < 12) out.why = "too few external wall crossings with clear ink";
  else if (me.share < 0.25) out.why = `the external walls' ink varies too much (${Math.round(me.share * 100)} % agree)`;
  else if (me.v !== null && (me.v < 0.08 || me.v > 1.0)) out.why = `the external walls' ink reads ${me.v.toFixed(2)} m: not a wall`;
  return out;
}

/** The external wall thickness to use, or null with `why` (never a guess). */
export function externalWall(w: InkWalls): number | null {
  return !w.why && w.external.m !== null ? Math.round(w.external.m * 100) / 100 : null;
}
/** How far the gross raster grows the traced outlines to reach the external walls' outer face (null: not measured). */
export function externalReach(w: InkWalls): number | null {
  const r = w.external.reach ?? null;
  return externalWall(w) !== null && r !== null && r > 0.05 ? Math.round(r * 100) / 100 : null;
}
