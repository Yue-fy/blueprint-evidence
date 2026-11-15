// F4 convention switch (captain 21:51): the maths, no DOM. A takeoff (F2's /feat/takeoff rows) is recounted under the
// estimator's conventions: floor area NET (to the inner wall faces: the default) · to wall CENTRE lines · GROSS (to the
// outer face of the external walls, a floor total); skirting with or without wet rooms and with or without the door
// openings deducted; doors as SETS (a double door = 1: the default) or LEAVES. Defaults stay the captain's conventions.
// A basis that cannot be computed is reported with the reason (never a guessed number); a wall thickness is either
// MEASURED here (the gap between neighbouring rooms' traced outlines) or typed by the visitor (then named 'your value').

export type Pt = [number, number];
export interface Ev { file?: string; page?: number | null; bbox?: number[] | null; bbox_space?: string; polygon?: Pt[] | null; polyline?: Pt[] | null; note?: string }
/** F2's per-room components (KNOWLEDGE 22:10, night-F2 2a5567cb): the same dict on every row of a room. */
export interface Comp {
  room_type?: string | null; wet?: boolean; area_m2?: number | null; perimeter_m?: number | null; skirting_net_m?: number | null;
  openings_m?: number[] | null; doors?: number | null; door_leaves?: number | null; leaves_per_door?: number[] | null;
  windows?: number | null; wall_thickness_m?: number | null; confidence?: number | null; agree?: boolean | null; methods?: string[];
}
export interface TkRow {
  section: string; item: string; copy?: number; copies?: number; quantity: string; value: number | string | null; unit: string | null;
  confidence: number; method?: string; status?: string; evidence?: Ev[]; assumptions?: string[]; id?: string; sheet?: string | null;
  page?: number | null; room_type?: string; wet?: boolean; leaves?: number | null; review?: string | null; flags?: string[];
  components?: Comp; fixed?: string | null;
}

export type AreaBasis = "net" | "centre" | "gia" | "gross"; // gia = gross internal (inner face of the external walls)
export interface Conventions {
  area: AreaBasis;
  wetSkirting: boolean; // wet rooms' skirting in the totals (default true, flagged)
  deductOpenings: boolean; // door openings deducted from the skirting (default true)
  doors: "sets" | "leaves"; // default sets: a double door = 1
  wallM: number | null; // the visitor's internal wall thickness (m); null = use the measured one
  extWallM: number | null; // the visitor's external wall thickness (m), for GROSS
}
export const DEFAULTS: Conventions = { area: "net", wetSkirting: true, deductOpenings: true, doors: "sets", wallM: null, extWallM: null };
export const isDefault = (c: Conventions) => c.area === "net" && c.wetSkirting && c.deductOpenings && c.doors === "sets";

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const FT2 = 10.7639104; // ft² per m²

export interface Room {
  key: string; item: string; copy: number; copies: number; type: string; wet: boolean;
  area: number | null; unit: "m2" | "ft2" | string; areaM2: number | null; perimeter: number | null; perimeterFrom: string;
  skirtNet: number | null; doors: number | null; leaves: number | null; windows: number | null; conf: number;
  reviewArea: string | null; reviewSkirt: string | null; reviewDoors: string | null;
  poly: Pt[] | null; space: string | null; page: number | null; sheet: string | null;
  rows: Record<string, TkRow>;
}

/** Shoelace area (absolute) and outline length of a polygon in its own units. */
export function polyArea(p: Pt[]): number {
  let s = 0;
  for (let i = 0; i < p.length; i++) {
    const [x0, y0] = p[i];
    const [x1, y1] = p[(i + 1) % p.length];
    s += x0 * y1 - x1 * y0;
  }
  return Math.abs(s) / 2;
}
export function polyLength(p: Pt[]): number {
  let s = 0;
  for (let i = 0; i < p.length; i++) s += Math.hypot(p[(i + 1) % p.length][0] - p[i][0], p[(i + 1) % p.length][1] - p[i][1]);
  return s;
}
const PER_RX = /(?:perimeter|outline) (\d+(?:\.\d+)?) m\b/;

/** The takeoff's room rows grouped into rooms (one per printed label copy), with what each convention needs. Uses F2's
 *  `components` when the rows carry them, else the rows themselves (an older takeoff). */
export function roomsOf(rows: TkRow[]): Room[] {
  const by = new Map<string, Record<string, TkRow>>();
  for (const r of rows) {
    if (r.section !== "room") continue;
    const k = `${r.item}#${r.copy ?? 1}`;
    if (!by.has(k)) by.set(k, {});
    by.get(k)![r.quantity] = r;
  }
  const out: Room[] = [];
  for (const [key, d] of by) {
    const any = Object.values(d)[0];
    const c: Comp = Object.values(d).find((r) => r.components)?.components ?? {};
    const a = d.area, b = d.baseboard, dr = d.doors, w = d.windows;
    const unit = String(a?.unit ?? "m2");
    const area = num(a?.value);
    const areaM2 = num(c.area_m2) ?? (area !== null ? (unit === "ft2" ? area / FT2 : unit === "m2" ? area : null) : null);
    const ev = (a?.evidence ?? []).find((e) => Array.isArray(e.polygon) && e.polygon.length > 2) ?? null;
    let perimeter = num(c.perimeter_m), perimeterFrom = perimeter !== null ? "the traced outline" : "";
    if (perimeter === null && b) {
      const texts = [...(b.assumptions ?? []), ...(b.evidence ?? []).map((e) => e.note ?? "")];
      const m = texts.map((t) => PER_RX.exec(t)).find(Boolean);
      if (m) {
        perimeter = Number(m[1]);
        perimeterFrom = "the skirting reading";
      }
    }
    if (perimeter === null && ev?.polygon && areaM2) {
      const pa = polyArea(ev.polygon);
      if (pa > 0) {
        perimeter = Math.round(polyLength(ev.polygon) * Math.sqrt(areaM2 / pa) * 1000) / 1000;
        perimeterFrom = "the traced outline";
      }
    }
    out.push({
      key, item: any.item, copy: any.copy ?? 1, copies: any.copies ?? 1, type: String(c.room_type ?? any.room_type ?? "other"),
      wet: !!(c.wet ?? any.wet), area, unit, areaM2, perimeter, perimeterFrom,
      skirtNet: num(c.skirting_net_m) ?? num(b?.value), doors: num(c.doors) ?? num(dr?.value),
      leaves: num(c.door_leaves) ?? num(dr?.leaves), windows: num(c.windows) ?? num(w?.value),
      conf: Math.min(...Object.values(d).map((r) => Number(r.confidence) || 0)),
      reviewArea: a?.review ?? null, reviewSkirt: b?.review ?? null, reviewDoors: dr?.review ?? null,
      poly: ev?.polygon ?? null, space: ev?.bbox_space ?? null, page: ev?.page ?? any.page ?? null, sheet: any.sheet ?? null, rows: d,
    });
  }
  return out;
}

// ---------------------------------------------------------------- wall thickness from the gaps between room outlines

export interface WallGap { a: Pt; b: Pt; m: number; rooms: [string, string] }
export interface WallMeasure {
  thickness: number | null; // metres, null = not measured (with `why`)
  why: string;
  samples: number; // boundary samples that found a neighbour across a wall
  pairs: number; // neighbouring room pairs
  share: number; // share of those samples within ±3 cm of the thickness
  mPerUnit: number | null; // drawing units -> metres (median over the rooms)
  space: string | null;
  page: number | null;
  shown: WallGap[]; // a few measured gaps to draw on the plan (evidence)
}

function nearestOnSeg(p: Pt, a: Pt, b: Pt): Pt {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const L = dx * dx + dy * dy;
  const t = L > 0 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L)) : 0;
  return [a[0] + t * dx, a[1] + t * dy];
}
function median(xs: number[]): number {
  const s = [...xs].sort((x, y) => x - y);
  const n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}

/** The internal wall thickness, measured: walk every room outline (a sample every 25 cm), find the nearest OTHER room's
 *  outline across a gap of 4–60 cm, and take the most common gap (2 cm bins, then the median of the samples within ±3 cm
 *  of it). Outlines are the inner wall faces, so the gap between two neighbouring rooms IS the wall between them. Needs
 *  ≥ 3 neighbouring pairs and ≥ 25 % of the samples agreeing; else not measured, with the reason. */
export function measureWalls(rooms: Room[]): WallMeasure {
  const none = (why: string, extra: Partial<WallMeasure> = {}): WallMeasure =>
    ({ thickness: null, why, samples: 0, pairs: 0, share: 0, mPerUnit: null, space: null, page: null, shown: [], ...extra });
  const withPoly = rooms.filter((r) => r.poly && r.poly.length > 2 && r.areaM2 && r.areaM2 > 0);
  if (withPoly.length < 3) return none(withPoly.length ? "only a few rooms have a traced outline" : "no traced room outlines on this sheet (e.g. a printed area table)");
  // one space / page: the most common
  const sp = new Map<string, number>();
  for (const r of withPoly) sp.set(`${r.space}|${r.page}`, (sp.get(`${r.space}|${r.page}`) ?? 0) + 1);
  const best = [...sp.entries()].sort((x, y) => y[1] - x[1])[0][0];
  const list = withPoly.filter((r) => `${r.space}|${r.page}` === best);
  const ks = list.map((r) => Math.sqrt(r.areaM2! / Math.max(1e-9, polyArea(r.poly!)))).filter((k) => Number.isFinite(k) && k > 0);
  if (ks.length < 3) return none("the room outlines have no usable scale");
  const k = median(ks); // metres per drawing unit
  const step = 0.25 / k, lo = 0.04 / k, hi = 0.6 / k;
  const boxes = list.map((r) => {
    const xs = r.poly!.map((p) => p[0]), ys = r.poly!.map((p) => p[1]);
    return [Math.min(...xs) - hi, Math.min(...ys) - hi, Math.max(...xs) + hi, Math.max(...ys) + hi];
  });
  const gaps: WallGap[] = [];
  const pairSet = new Set<string>();
  for (let i = 0; i < list.length; i++) {
    const P = list[i].poly!;
    const near = list.map((_, j) => j).filter((j) => j !== i && !(boxes[j][0] > boxes[i][2] || boxes[j][2] < boxes[i][0] || boxes[j][1] > boxes[i][3] || boxes[j][3] < boxes[i][1]));
    if (!near.length) continue;
    let budget = 600;
    for (let e = 0; e < P.length && budget > 0; e++) {
      const a = P[e], b = P[(e + 1) % P.length];
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const n = Math.max(1, Math.floor(L / step));
      for (let s = 0; s < n && budget > 0; s++, budget--) {
        const t = (s + 0.5) / n;
        const p: Pt = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
        let bd = Infinity, bq: Pt | null = null, bj = -1;
        for (const j of near) {
          const Q = list[j].poly!;
          for (let f = 0; f < Q.length; f++) {
            const q = nearestOnSeg(p, Q[f], Q[(f + 1) % Q.length]);
            const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
            if (d < bd) {
              bd = d;
              bq = q;
              bj = j;
            }
          }
        }
        if (bq && bd >= lo && bd <= hi) {
          gaps.push({ a: p, b: bq, m: bd * k, rooms: [list[i].key, list[bj].key] });
          pairSet.add([list[i].key, list[bj].key].sort().join("|"));
        }
      }
    }
  }
  const [space, page] = best.split("|");
  const base = { mPerUnit: k, space: space === "null" ? null : space, page: page === "null" ? null : Number(page), samples: gaps.length, pairs: pairSet.size };
  if (pairSet.size < 3 || gaps.length < 12) return none("too few neighbouring rooms to measure a wall between them", base);
  const bins = new Map<number, number>();
  for (const g of gaps) bins.set(Math.round(g.m / 0.02), (bins.get(Math.round(g.m / 0.02)) ?? 0) + 1);
  const score = (b: number) => (bins.get(b) ?? 0) * 2 + (bins.get(b - 1) ?? 0) + (bins.get(b + 1) ?? 0);
  const mode = [...bins.keys()].sort((x, y) => score(y) - score(x) || x - y)[0] * 0.02;
  const close = gaps.filter((g) => Math.abs(g.m - mode) <= 0.03);
  const share = close.length / gaps.length;
  if (share < 0.25) return none(`the gaps between rooms vary too much (no common wall thickness; ${Math.round(share * 100)} % agree)`, base);
  const thickness = Math.round(median(close.map((g) => g.m)) * 1000) / 1000;
  // evidence: up to 12 measured gaps spread over different room pairs
  const shown: WallGap[] = [];
  const used = new Set<string>();
  for (const g of close) {
    const pk = [...g.rooms].sort().join("|");
    if (used.has(pk)) continue;
    used.add(pk);
    shown.push(g);
    if (shown.length >= 12) break;
  }
  return { ...base, thickness, why: "", share, shown };
}

// ---------------------------------------------------------------- applying the conventions

export interface Cell { value: number | null; def: number | null; unit: string; note: string; left?: string | null }
export interface RoomOut { room: Room; area: Cell; skirt: Cell; doors: Cell }
export interface Total { key: string; label: string; value: number | null; def: number | null; unit: string; rule: string; note: string; disabled?: string }
export interface Applied { rooms: RoomOut[]; totals: Total[]; wall: number | null; wallFrom: string; labels: Record<"area" | "skirt" | "doors", string> }
export interface GrossIn { value: number | null; why: string; note: string; ext?: number | null; extFrom?: string }

const r2 = (v: number) => Math.round(v * 100) / 100;
export const fmtM = (m: number) => `${(Math.round(m * 1000) / 1000).toFixed(m < 1 ? 2 : 1)} m`;

/** The floor area to the wall centre lines of a room whose outline is the inner wall faces: grow it by half the wall
 *  thickness. For a room drawn with right-angled corners (convex minus re-entrant corners = 4) this is exactly
 *  A + P·t/2 + t² (mitred corners); other shapes are close. */
export const centreArea = (a: number, p: number, t: number) => a + (p * t) / 2 + t * t;

/** The outline grown outwards by d (its own units) with mitred corners: each edge moved out by d, neighbours
 *  intersected (the centre-line outline drawn on Show). Parallel neighbours keep the moved point; a very sharp corner
 *  (mitre > 4d) is clipped to 4d. */
export function offsetPoly(p: Pt[], d: number): Pt[] {
  const n = p.length;
  if (n < 3 || !(d > 0)) return p;
  let s = 0;
  for (let i = 0; i < n; i++) s += p[i][0] * p[(i + 1) % n][1] - p[(i + 1) % n][0] * p[i][1];
  const sg = s > 0 ? 1 : -1; // counter-clockwise (y up) -> the outward normal is (dy, -dx)
  const lines = p.map((a, i) => {
    const b = p[(i + 1) % n];
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const nx = (sg * (b[1] - a[1])) / L, ny = (-sg * (b[0] - a[0])) / L;
    return { a: [a[0] + nx * d, a[1] + ny * d] as Pt, b: [b[0] + nx * d, b[1] + ny * d] as Pt };
  });
  return p.map((v, i) => {
    const l1 = lines[(i - 1 + n) % n], l2 = lines[i];
    const d1x = l1.b[0] - l1.a[0], d1y = l1.b[1] - l1.a[1], d2x = l2.b[0] - l2.a[0], d2y = l2.b[1] - l2.a[1];
    const den = d1x * d2y - d1y * d2x;
    if (Math.abs(den) < 1e-12) return l2.a;
    const t = ((l2.a[0] - l1.a[0]) * d2y - (l2.a[1] - l1.a[1]) * d2x) / den;
    const q: Pt = [l1.a[0] + t * d1x, l1.a[1] + t * d1y];
    const m = Math.hypot(q[0] - v[0], q[1] - v[1]);
    return m > 4 * d ? [v[0] + ((q[0] - v[0]) * 4 * d) / m, v[1] + ((q[1] - v[1]) * 4 * d) / m] : q;
  });
}

/** Every room and total under the conventions `c`, each with its value under the default conventions beside it. */
export function applyConventions(rows: TkRow[], rooms: Room[], c: Conventions, wall: WallMeasure, gross: GrossIn | null): Applied {
  const t = c.wallM ?? wall.thickness;
  const wallFrom = c.wallM !== null ? "your value" : wall.thickness !== null ? `measured between ${wall.pairs} pairs of neighbouring rooms` : "";
  const labels = {
    area: c.area === "net" ? "Net: floor inside the wall faces"
      : c.area === "centre" ? `To wall centre lines (walls ${t !== null ? fmtM(t) : "?"}, ${wallFrom || "not measured"})`
      : c.area === "gia" ? `Gross internal (GIA): to the inner face of the external walls, internal walls included (walls ${t !== null ? fmtM(t) : "?"}, ${wallFrom || "not measured"})`
      : `Gross: to the outer face of the external walls${gross?.ext ? ` (external walls ${fmtM(gross.ext)}, ${gross.extFrom || "your value"})` : c.extWallM !== null ? ` (external walls ${fmtM(c.extWallM)}, your value)` : ""}`,
    skirt: `${c.deductOpenings ? "door openings deducted" : "door openings not deducted"}; ${c.wetSkirting ? "wet rooms included (flagged)" : "wet rooms left out"}`,
    doors: c.doors === "sets" ? "door sets (a double door = 1)" : "door leaves (a double door = 2)",
  };
  const out: RoomOut[] = rooms.map((r) => {
    // area
    const unit = r.unit;
    let av = r.area, an = "";
    if (c.area === "centre") {
      if (r.area === null) an = "no reading";
      else if (t === null) an = "wall thickness unknown: net kept";
      else if (r.perimeter === null || r.areaM2 === null) an = "no outline length: net kept";
      else {
        const m2 = centreArea(r.areaM2, r.perimeter, t);
        av = r2(unit === "ft2" ? m2 * FT2 : m2);
        an = `${r2(r.areaM2)} m² + outline ${fmtM(r.perimeter)} × ${fmtM(t / 2)} + corners`;
      }
    } else if (c.area === "gross" || c.area === "gia") an = "gross is a floor total: rooms stay net";
    // skirting
    let sv = r.skirtNet, sn = "", left: string | null = null;
    if (r.skirtNet === null) sn = r.perimeter !== null ? "no skirting here (stairs, lifts, plant)" : "no reading";
    else if (!c.deductOpenings) {
      if (r.perimeter !== null) {
        sv = r2(r.perimeter);
        sn = r.perimeter > r.skirtNet ? `openings ${fmtM(r.perimeter - r.skirtNet)} not deducted` : "no openings to deduct";
      } else sn = "outline length unknown: openings stay deducted";
    }
    if (r.wet && r.skirtNet !== null) {
      if (!c.wetSkirting) left = "wet room left out";
      else sn = sn ? `${sn}; wet room` : "wet room (usually tiled): in the total";
    }
    // doors
    let dv = r.doors, dn = "";
    if (c.doors === "leaves" && r.doors !== null) {
      if (r.leaves !== null) {
        dv = r.leaves;
        dn = r.leaves > r.doors ? `${r.leaves - r.doors} double door${r.leaves - r.doors > 1 ? "s" : ""}: 2 leaves each` : "";
      } else dn = "leaves unknown: counted as sets";
    }
    return {
      room: r,
      area: { value: av, def: r.area, unit, note: an },
      skirt: { value: left ? null : sv, def: r.skirtNet, unit: "m", note: sn, left },
      doors: { value: dv, def: r.doors, unit: "count", note: dn },
    };
  });
  // totals (Review rows left out, like F2's)
  const totals: Total[] = [];
  // gross / GIA are one floor total (computed in m²): it goes to the sheet's main area unit, shown in that unit
  const mainUnit = out.filter((o) => o.room.unit === "ft2").length > out.filter((o) => o.room.unit === "m2").length ? "ft2" : "m2";
  for (const u of ["m2", "ft2"]) {
    const ok = out.filter((o) => o.room.unit === u && o.area.def !== null && !o.room.reviewArea);
    if (!ok.length) continue;
    const def = r2(ok.reduce((s, o) => s + (o.area.def ?? 0), 0));
    const nRev = out.filter((o) => o.room.unit === u && o.room.reviewArea).length;
    const rest = `${ok.length} rooms${nRev ? `, ${nRev} in Review left out` : ""}`;
    if ((c.area === "gross" || c.area === "gia") && u === mainUnit) {
      const gv = gross?.value ?? null;
      totals.push({ key: "area", label: c.area === "gia" ? "Gross internal floor area (GIA)" : "Gross floor area", value: gv === null ? null : u === "ft2" ? r2(gv * FT2) : gv, def, unit: u, rule: labels.area,
        note: gross?.value !== null && gross?.value !== undefined ? gross.note : "", disabled: gross?.value !== null && gross?.value !== undefined ? undefined : (gross?.why || "cannot be computed on this sheet") });
    } else if (c.area === "centre") {
      const kept = ok.filter((o) => o.area.value === o.area.def).length;
      totals.push({ key: "area", label: "Floor area to wall centre lines", value: t === null ? null : r2(ok.reduce((s, o) => s + (o.area.value ?? 0), 0)), def, unit: u,
        rule: labels.area, note: `${rest}${kept && t !== null ? `; ${kept} kept net (no outline length)` : ""}`,
        disabled: t === null ? `the wall thickness is not known: ${wall.why || "not measured"}` : undefined });
    } else totals.push({ key: "area", label: "Net floor area", value: def, def, unit: u, rule: labels.area, note: rest });
  }
  const sk = out.filter((o) => o.skirt.def !== null && !o.room.reviewSkirt);
  if (sk.length) {
    const def = r2(sk.reduce((s, o) => s + (o.skirt.def ?? 0), 0));
    const val = r2(sk.reduce((s, o) => s + (o.skirt.value ?? 0), 0));
    const wet = sk.filter((o) => o.room.wet);
    totals.push({ key: "skirt", label: "Skirting total", value: val, def, unit: "m", rule: labels.skirt,
      note: `${sk.length} rooms${wet.length ? `; ${wet.length} wet room${wet.length > 1 ? "s" : ""} ${c.wetSkirting ? "included" : "left out"} (${r2(wet.reduce((s, o) => s + (o.skirt.def ?? 0), 0))} m net)` : ""}` });
  }
  const sd = rows.find((r) => r.section === "sheet" && r.quantity === "doors" && num(r.value) !== null);
  if (sd) {
    const sets = num(sd.value)!, leaves = num(sd.leaves);
    const val = c.doors === "leaves" ? leaves : sets;
    totals.push({ key: "doors", label: c.doors === "leaves" ? "Door leaves (whole sheet)" : "Doors (whole sheet)", value: val, def: sets, unit: "count", rule: labels.doors,
      note: leaves !== null ? `${sets} door sets = ${leaves} leaves (${leaves - sets} double)` : "", disabled: val === null ? "the door leaves were not counted on this sheet" : undefined });
  }
  return { rooms: out, totals, wall: t, wallFrom, labels };
}

/** '+41.2 m² vs net' (the change a toggle makes), '' when none. */
export function delta(t: Pick<Total, "value" | "def" | "unit">, vs: string): string {
  if (t.value === null || t.def === null) return "";
  const d = t.value - t.def;
  if (Math.abs(d) < 0.005) return "";
  const u = t.unit === "m2" ? " m²" : t.unit === "ft2" ? " ft²" : t.unit === "m" ? " m" : "";
  const pct = t.def ? ` (${d > 0 ? "+" : ""}${((100 * d) / t.def).toFixed(1)} %)` : "";
  return `${d > 0 ? "+" : "−"}${Math.abs(d) >= 100 || Number.isInteger(d) ? Math.round(Math.abs(d) * 10) / 10 : Math.abs(d).toFixed(2)}${u} vs ${vs}${pct}`;
}

// ---------------------------------------------------------------- export (CSV / Excel): the convention on every row

export const EXPORT_HEAD = ["section", "item", "quantity", "value", "unit", "default value", "change", "convention", "in totals", "how sure", "confidence", "sheet", "page", "file", "how measured", "evidence box"];
type XCell = string | number | null;
const how = (c: number) => (c >= 0.9 ? "very sure" : c >= 0.7 ? "fairly sure" : c >= 0.4 ? "best estimate" : c > 0 ? (c < 0.25 ? "rough guess" : "our guess") : "no reading");
const unitW = (u: string) => (u === "m2" ? "m²" : u === "ft2" ? "ft²" : u === "count" ? "" : u);

export function exportRows(ap: Applied, rows: TkRow[], file: string): XCell[][] {
  const out: XCell[][] = [];
  const box = (r?: TkRow) => {
    const b = r?.evidence?.find((e) => e.bbox)?.bbox;
    return b ? `${b.map((v) => Math.round(v)).join(" ")} ${r?.evidence?.find((e) => e.bbox)?.bbox_space ?? ""}`.trim() : "";
  };
  const chg = (c: Cell) => (c.value !== null && c.def !== null && Math.abs(c.value - c.def) > 0.004 ? r2(c.value - c.def) : c.left ? "left out" : "");
  for (const o of ap.rooms) {
    const r = o.room;
    const nm = r.copies > 1 ? `${r.item} (${r.copy}/${r.copies})` : r.item;
    const line = (q: string, c: Cell, conv: string, row: TkRow | undefined, rev: string | null) =>
      out.push(["room", nm, q, c.value, unitW(c.unit), c.def, chg(c), [conv, c.note, c.left].filter(Boolean).join("; "), rev ? `no: ${rev}` : c.left ? "no: " + c.left : "yes",
        how(Number(row?.confidence) || 0), row ? Number(row.confidence) : null, r.sheet ?? "", r.page !== null ? r.page + 1 : "", file, row?.method ?? "", box(row)]);
    if (r.rows.area) line("floor area", o.area, ap.labels.area, r.rows.area, r.reviewArea);
    if (r.rows.baseboard) line("skirting", o.skirt, ap.labels.skirt, r.rows.baseboard, r.reviewSkirt);
    if (r.rows.doors) line("doors", o.doors, ap.labels.doors, r.rows.doors, r.reviewDoors);
  }
  for (const r of rows.filter((x) => x.section === "printed" || x.section === "slab")) {
    out.push([r.section, r.item, r.quantity, num(r.value) ?? (r.value as string | null), unitW(String(r.unit ?? "")), num(r.value), "",
      r.section === "printed" ? "printed on the sheet: conventions not applied" : "slab by type: conventions not applied", r.review ? `no: ${r.review}` : "yes",
      how(Number(r.confidence) || 0), Number(r.confidence), r.sheet ?? "", r.page !== null && r.page !== undefined ? r.page + 1 : "", file, r.method ?? "", box(r)]);
  }
  for (const t of ap.totals) {
    out.push(["total", t.label, t.key === "area" ? "floor area" : t.key === "skirt" ? "skirting" : "doors", t.value, unitW(t.unit), t.def,
      t.value !== null && t.def !== null && Math.abs(t.value - t.def) > 0.004 ? r2(t.value - t.def) : "", [t.rule, t.note, t.disabled ? `not computed: ${t.disabled}` : ""].filter(Boolean).join("; "),
      "", "", null, rows.find((r) => r.sheet)?.sheet ?? "", "", file, "sum of the rows above (Review rows left out)", ""]);
  }
  return out;
}

const csvCell = (v: XCell) => {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
export const toCsv = (head: string[], rows: XCell[][]) => `﻿${[head, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n")}\r\n`;
