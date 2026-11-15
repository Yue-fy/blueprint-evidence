// F4 convention switch: GROSS floor area (to the outer face of the external walls) from the takeoff's traced room
// outlines (the inner wall faces), on a raster, pure JS (no canvas: runs in Node tests too). Every room grows by the
// external wall thickness T (Euclidean distance ≤ T); the union closes every internal wall thinner than 2T and its outer
// edge lands on the outer face of the external walls. Enclosed gaps left inside (shafts, voids, rooms without a label,
// courtyards) are counted when small (≤ 10 m²: shafts, wall junctions) and reported, not counted, when larger.
import type { Pt } from "./calc";

export interface Gross {
  area: number; holesIn: number; holesOut: number; nHolesOut: number; cell: number; rooms: number;
  /** the raster, for drawing it on the plan: cell (x, y) covers metres [x0 + x·cell, …) ; mask 1 = counted, 2 = an
   *  enclosed space NOT counted (large, no label) */
  grid: { x0: number; y0: number; W: number; H: number; mask: Uint8Array };
}

const BIG = 1e20; // 'no room cell in this line' (finite: Infinity - Infinity would be NaN in the envelope)

/** Exact 1-D squared distance transform (Felzenszwalb & Huttenlocher) of f (0 at a room cell, BIG elsewhere). */
function edt1(f: Float64Array, n: number, d: Float64Array, v: Int32Array, z: Float64Array) {
  let k = 0;
  v[0] = 0;
  z[0] = -Infinity;
  z[1] = Infinity;
  for (let q = 1; q < n; q++) {
    let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    while (s <= z[k]) {
      k--;
      s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    }
    k++;
    v[k] = q;
    z[k] = s;
    z[k + 1] = Infinity;
  }
  k = 0;
  for (let q = 0; q < n; q++) {
    while (z[k + 1] < q) k++;
    d[q] = (q - v[k]) * (q - v[k]) + f[v[k]];
  }
}

/** Fill polygon p (grid coordinates) into m (scanline, even-odd, cell centres). */
function fillPoly(m: Uint8Array, W: number, H: number, p: Pt[]) {
  const ys = p.map((q) => q[1]);
  const y0 = Math.max(0, Math.floor(Math.min(...ys))), y1 = Math.min(H - 1, Math.ceil(Math.max(...ys)));
  for (let y = y0; y <= y1; y++) {
    const cy = y + 0.5;
    const xs: number[] = [];
    for (let i = 0; i < p.length; i++) {
      const [ax, ay] = p[i], [bx, by] = p[(i + 1) % p.length];
      if ((ay <= cy && by > cy) || (by <= cy && ay > cy)) xs.push(ax + ((cy - ay) / (by - ay)) * (bx - ax));
    }
    xs.sort((a, b) => a - b);
    for (let i = 0; i + 1 < xs.length; i += 2) {
      const xa = Math.max(0, Math.ceil(xs[i] - 0.5)), xb = Math.min(W - 1, Math.floor(xs[i + 1] - 0.5));
      for (let x = xa; x <= xb; x++) m[y * W + x] = 1;
    }
  }
}

/** Squared distance (cells²) from every cell to the nearest cell where mask === want: columns, then rows. */
function sqDist(mask: Uint8Array, W: number, H: number, want: number): Float64Array {
  const N = Math.max(W, H);
  const f = new Float64Array(N), d = new Float64Array(N), v = new Int32Array(N), z = new Float64Array(N + 1);
  const D = new Float64Array(W * H);
  for (let x = 0; x < W; x++) {
    for (let y = 0; y < H; y++) f[y] = mask[y * W + x] === want ? 0 : BIG;
    edt1(f, H, d, v, z);
    for (let y = 0; y < H; y++) D[y * W + x] = d[y];
  }
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) f[x] = D[y * W + x];
    edt1(f, W, d, v, z);
    for (let x = 0; x < W; x++) D[y * W + x] = d[x];
  }
  return D;
}

/** Gross area (m²) of rooms `polys` (drawing units; mPerUnit = metres per unit). mode 'external' (GEA, default): every
 *  room grown by t metres (the external walls' reach) and joined. mode 'internal' (GIA): a closing of radius t (grow by
 *  t, shrink back by t): internal walls thinner than 2t are filled, the outline returns to the external walls' INNER
 *  face. */
export function grossArea(polys: Pt[][], mPerUnit: number, t: number, opts: { maxCells?: number; smallHoleM2?: number; mode?: "external" | "internal" } = {}): Gross | null {
  const ps = polys.filter((p) => p && p.length > 2);
  if (!ps.length || !(mPerUnit > 0) || !(t > 0)) return null;
  const xs = ps.flatMap((p) => p.map((q) => q[0] * mPerUnit)), ys = ps.flatMap((p) => p.map((q) => q[1] * mPerUnit));
  const pad = t + 0.5;
  const x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad, x1 = Math.max(...xs) + pad, y1 = Math.max(...ys) + pad;
  const maxCells = opts.maxCells ?? 6_000_000;
  let cell = Math.max(0.02, Math.min(t / 4, 0.05)); // ≥ 4 cells across the wall
  while (((x1 - x0) / cell) * ((y1 - y0) / cell) > maxCells) cell *= 1.25;
  const W = Math.ceil((x1 - x0) / cell), H = Math.ceil((y1 - y0) / cell);
  const m = new Uint8Array(W * H);
  for (const p of ps) fillPoly(m, W, H, p.map((q) => [(q[0] * mPerUnit - x0) / cell, (q[1] * mPerUnit - y0) / cell] as Pt));
  const r2 = (t / cell) * (t / cell) * (1 + 1e-9); // 0.3 / 0.05 = 5.9999…: keep the cells at exactly t
  const D = sqDist(m, W, H, 1); // to the nearest room cell
  let g = new Uint8Array(W * H);
  for (let i = 0; i < W * H; i++) if (D[i] <= r2) g[i] = 1;
  if (opts.mode === "internal") {
    const E = sqDist(g, W, H, 0); // to the nearest cell outside the grown rooms
    const back = new Uint8Array(W * H);
    for (let i = 0; i < W * H; i++) if (E[i] > r2 || m[i]) back[i] = 1;
    g = back;
  }
  let n = 0;
  for (let i = 0; i < W * H; i++) n += g[i];
  // enclosed gaps: empty cells not reachable from the border
  const seen = new Uint8Array(W * H);
  const stack: number[] = [];
  const push = (i: number) => {
    if (!g[i] && !seen[i]) {
      seen[i] = 1;
      stack.push(i);
    }
  };
  for (let x = 0; x < W; x++) {
    push(x);
    push((H - 1) * W + x);
  }
  for (let y = 0; y < H; y++) {
    push(y * W);
    push(y * W + W - 1);
  }
  while (stack.length) {
    const i = stack.pop()!;
    const x = i % W, y = (i / W) | 0;
    if (x > 0) push(i - 1);
    if (x < W - 1) push(i + 1);
    if (y > 0) push(i - W);
    if (y < H - 1) push(i + W);
  }
  const small = (opts.smallHoleM2 ?? 10) / (cell * cell);
  let holesIn = 0, holesOut = 0, nHolesOut = 0;
  for (let i = 0; i < W * H; i++) {
    if (g[i] || seen[i]) continue; // a room cell, a filled gap, or outside
    // one enclosed gap: flood it
    let size = 0;
    const cells: number[] = [];
    seen[i] = 1;
    stack.push(i);
    while (stack.length) {
      const j = stack.pop()!;
      size++;
      cells.push(j);
      const x = j % W, y = (j / W) | 0;
      for (const k of [x > 0 ? j - 1 : -1, x < W - 1 ? j + 1 : -1, y > 0 ? j - W : -1, y < H - 1 ? j + W : -1]) {
        if (k >= 0 && !g[k] && !seen[k]) {
          seen[k] = 1;
          stack.push(k);
        }
      }
    }
    if (size <= small) holesIn += size;
    else {
      holesOut += size;
      nHolesOut++;
    }
    for (const j of cells) g[j] = size <= small ? 1 : 2;
  }
  const a = cell * cell;
  return { area: Math.round((n + holesIn) * a * 100) / 100, holesIn: Math.round(holesIn * a * 100) / 100, holesOut: Math.round(holesOut * a * 100) / 100, nHolesOut, cell, rooms: ps.length,
    grid: { x0, y0, W, H, mask: g } };
}
