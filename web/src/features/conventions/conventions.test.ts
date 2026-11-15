// F4 convention switch unit tests: `node --test web/src/features/conventions/conventions.test.ts` (Node >= 22.18 strips
// the types). Run by tests/test_feat_conventions.py in the team's pytest. Pure modules only (no DOM, no network).
import assert from "node:assert/strict";
import { test } from "node:test";

const here = (f: string) => String(new URL(f, import.meta.url));
const C = await import(here("./calc.ts"));
const G = await import(here("./gross.ts"));

type P = [number, number];
const rect = (x: number, y: number, w: number, h: number): P[] => [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
/** A takeoff like F2's: rooms as 1 unit = 0.05 m (k), area / skirting / doors rows with `components`. */
function room(item: string, poly: P[], o: { wet?: boolean; doors?: number; leaves?: number; openings?: number; review?: string | null; comp?: boolean } = {}) {
  const k = 0.05;
  const a = Math.round(C.polyArea(poly) * k * k * 100) / 100;
  const per = Math.round(C.polyLength(poly) * k * 1000) / 1000;
  const net = o.openings !== undefined ? Math.round((per - o.openings) * 1000) / 1000 : per;
  const comp = { room_type: o.wet ? "wet" : "office", wet: !!o.wet, area_m2: a, perimeter_m: per, skirting_net_m: net, openings_m: null,
    doors: o.doors ?? 1, door_leaves: o.leaves ?? o.doors ?? 1, leaves_per_door: null, windows: 0, wall_thickness_m: null, confidence: 0.8, agree: null, methods: [] };
  const base = { section: "room", item, copy: 1, copies: 1, confidence: 0.8, wet: !!o.wet, ...(o.comp === false ? {} : { components: comp }) };
  return [
    { ...base, quantity: "area", value: a, unit: "m2", review: o.review ?? null, evidence: [{ polygon: poly, bbox_space: "pdf_pt", page: 0 }] },
    { ...base, quantity: "baseboard", value: net, unit: "m", review: null, assumptions: [`perimeter ${per} m minus openings`] },
    { ...base, quantity: "doors", value: o.doors ?? 1, unit: "count", leaves: o.leaves ?? o.doors ?? 1, review: null },
  ];
}
// three offices in a row, 4 x 5 m each (80 x 100 units), walls 0.2 m (4 units) between them; one wet room below
const ROWS = [
  ...room("Office A", rect(0, 0, 80, 100), { openings: 0.9 }),
  ...room("Office B", rect(84, 0, 80, 100), { openings: 0.9, doors: 1, leaves: 2 }),
  ...room("Office C", rect(168, 0, 80, 100), { openings: 0.9 }),
  ...room("WC", rect(0, 104, 40, 40), { wet: true, openings: 0.8 }),
  { section: "sheet", item: "Whole sheet", quantity: "doors", value: 4, unit: "count", leaves: 5, confidence: 0.7 },
  { section: "printed", item: "TOTAL", quantity: "area", value: 2650, unit: "ft2", confidence: 0.9 },
];

test("rooms: grouped per label copy, components read, review kept", () => {
  const rs = C.roomsOf(ROWS);
  assert.equal(rs.length, 4);
  const b = rs.find((r: { item: string }) => r.item === "Office B");
  assert.equal(b.area, 20);
  assert.equal(b.perimeter, 18);
  assert.equal(b.skirtNet, 17.1);
  assert.equal(b.leaves, 2);
  assert.equal(rs.find((r: { item: string }) => r.item === "WC").wet, true);
});

test("rooms without components: perimeter from the skirting row's assumption", () => {
  const rs = C.roomsOf(room("Office", rect(0, 0, 80, 100), { openings: 0.9, comp: false }));
  assert.equal(rs[0].perimeter, 18);
  assert.equal(rs[0].skirtNet, 17.1);
  assert.equal(rs[0].doors, 1);
});

test("wall thickness: measured from the gaps between neighbouring outlines", () => {
  const w = C.measureWalls(C.roomsOf(ROWS));
  assert.ok(w.thickness !== null, w.why);
  assert.ok(Math.abs(w.thickness - 0.2) < 0.011, String(w.thickness));
  assert.ok(w.pairs >= 3);
  assert.ok(Math.abs(w.mPerUnit - 0.05) < 1e-6);
  assert.ok(w.shown.length > 0 && w.shown.length <= 12);
  // a printed table only: not measured, with the reason
  const none = C.measureWalls([]);
  assert.equal(none.thickness, null);
  assert.match(none.why, /no traced room outlines/);
});

test("defaults reproduce the takeoff's own totals", () => {
  const rs = C.roomsOf(ROWS);
  const ap = C.applyConventions(ROWS, rs, C.DEFAULTS, C.measureWalls(rs), null);
  const t = Object.fromEntries(ap.totals.map((x: { key: string }) => [x.key, x]));
  assert.equal(t.area.value, 64); // 3 x 20 + 4
  assert.equal(t.area.def, 64);
  assert.equal(t.skirt.value, Math.round((3 * 17.1 + 7.2) * 100) / 100);
  assert.equal(t.doors.value, 4);
  assert.equal(C.delta(t.area, "net"), "");
  assert.ok(C.isDefault(C.DEFAULTS));
});

test("each switch changes only its own quantity, with the delta", () => {
  const rs = C.roomsOf(ROWS);
  const w = C.measureWalls(rs);
  const run = (p: object) => Object.fromEntries(C.applyConventions(ROWS, rs, { ...C.DEFAULTS, ...p }, w, null).totals.map((x: { key: string }) => [x.key, x]));
  // wet rooms out: the WC's 7.2 m
  const wet = run({ wetSkirting: false });
  assert.equal(Math.round((wet.skirt.def - wet.skirt.value) * 100) / 100, 7.2);
  assert.equal(wet.area.value, 64);
  // openings not deducted: + 3 x 0.9 + 0.8
  const op = run({ deductOpenings: false });
  assert.equal(Math.round((op.skirt.value - op.skirt.def) * 100) / 100, 3.5);
  // leaves: the sheet's 5
  const lv = run({ doors: "leaves" });
  assert.equal(lv.doors.value, 5);
  assert.equal(C.delta(lv.doors, "door sets"), "+1 vs door sets (+25.0 %)");
  // centre lines with your 0.2 m walls: each room A + P t/2 + t²
  const cl = run({ area: "centre", wallM: 0.2 });
  const want = 3 * C.centreArea(20, 18, 0.2) + C.centreArea(4, 8, 0.2);
  assert.ok(Math.abs(cl.area.value - want) < 0.02, `${cl.area.value} vs ${want}`);
  assert.match(C.delta(cl.area, "net"), /^\+\d/);
});

test("centre lines: exact for a rectangle grown by half the wall", () => {
  // 4 x 5 m room, 0.2 m walls: (4.2 x 5.2) = 21.84 = 20 + 18 x 0.1 + 0.04
  assert.ok(Math.abs(C.centreArea(20, 18, 0.2) - 21.84) < 1e-9);
});

test("offset outline: a rectangle grown by d either way round, an L-shape matches A + P·d + 4d²", () => {
  for (const r of [rect(0, 0, 4, 5), [...rect(0, 0, 4, 5)].reverse() as P[]]) {
    const o = C.offsetPoly(r, 0.1);
    assert.ok(Math.abs(C.polyArea(o) - 4.2 * 5.2) < 1e-9, String(C.polyArea(o)));
  }
  const L: P[] = [[0, 0], [6, 0], [6, 2], [2, 2], [2, 5], [0, 5]];
  const o = C.offsetPoly(L, 0.1);
  assert.ok(Math.abs(C.polyArea(o) - (C.polyArea(L) + C.polyLength(L) * 0.1 + 4 * 0.01)) < 1e-9);
});

test("a basis that cannot be computed is disabled with the reason (never guessed)", () => {
  const rows = [...room("Hall", rect(0, 0, 100, 100))]; // one room: no neighbour, no wall
  const rs = C.roomsOf(rows);
  const w = C.measureWalls(rs);
  assert.equal(w.thickness, null);
  const ap = C.applyConventions(rows, rs, { ...C.DEFAULTS, area: "centre" }, w, null);
  const a = ap.totals.find((x: { key: string }) => x.key === "area");
  assert.equal(a.value, null);
  assert.match(a.disabled, /wall thickness is not known/);
  const g = C.applyConventions(rows, rs, { ...C.DEFAULTS, area: "gross" }, w, { value: null, why: "the external wall thickness is not measured on this sheet: type it below", note: "" });
  assert.match(g.totals.find((x: { key: string }) => x.key === "area").disabled, /external wall thickness/);
});

test("an imperial sheet (rooms in ft²): gross shown in ft², computed in m²", () => {
  const FT2 = 10.7639104;
  const rows = room("Office", rect(0, 0, 80, 100)).map((r) => (r.quantity === "area" ? { ...r, value: Math.round(20 * FT2 * 100) / 100, unit: "ft2", components: { ...r.components, area_m2: null } } : r));
  const rs = C.roomsOf(rows);
  assert.ok(Math.abs(rs[0].areaM2 - 20) < 0.01);
  const ap = C.applyConventions(rows, rs, { ...C.DEFAULTS, area: "gross" }, C.measureWalls(rs), { value: 25, why: "", note: "test" });
  const a = ap.totals.find((x: { key: string }) => x.key === "area");
  assert.equal(a.unit, "ft2");
  assert.ok(Math.abs(a.value - 25 * FT2) < 0.01, String(a.value));
});

test("review rows stay out of every total", () => {
  const rows = [...ROWS.filter((r) => r.item !== "WC"), ...room("WC", rect(0, 104, 40, 40), { wet: true, review: "rough reading (confidence 0.30)" })];
  const rs = C.roomsOf(rows);
  const ap = C.applyConventions(rows, rs, { ...C.DEFAULTS, area: "centre", wallM: 0.2 }, C.measureWalls(rs), null);
  const a = ap.totals.find((x: { key: string }) => x.key === "area");
  assert.equal(a.def, 60);
  assert.match(a.note, /1 in Review left out/);
});

test("gross: rooms grown by the external walls, joined (raster within 0.5 %)", () => {
  const k = 0.05;
  const polys = [rect(0, 0, 80, 100), rect(84, 0, 80, 100)]; // 4 x 5 m + 0.2 m wall + 4 x 5 m
  const g = G.grossArea(polys, k, 0.3);
  const want = (8.2 + 0.6) * (5 + 0.6) - (4 - Math.PI) * 0.09; // rounded outer corners
  assert.ok(Math.abs(g.area - want) / want < 0.005, `${g.area} vs ${want}`);
  assert.equal(g.nHolesOut, 0);
  // a ring of rooms around a 6 x 6 m courtyard: the courtyard is reported, not counted
  const ring = [rect(0, 0, 200, 40), rect(0, 160, 200, 40), rect(0, 44, 40, 112), rect(160, 44, 40, 112)];
  const r = G.grossArea(ring, k, 0.3);
  assert.equal(r.nHolesOut, 1);
  assert.ok(r.holesOut > 20 && r.holesOut < 36, String(r.holesOut));
  assert.equal(G.grossArea([], k, 0.3), null);
  // GIA: close the 0.2 m internal wall (radius 0.125 m), back to the inner faces: 8.2 x 5 = 41 m²; a lone room stays 20
  const gi = G.grossArea(polys, k, 0.125, { mode: "internal" });
  assert.ok(Math.abs(gi.area - 41) / 41 < 0.01, `GIA ${gi.area}`);
  const one = G.grossArea([rect(0, 0, 80, 100)], k, 0.125, { mode: "internal" });
  assert.ok(Math.abs(one.area - 20) / 20 < 0.01, `GIA one room ${one.area}`);
});

test("ink walls: internal 0.20 m and external 0.35 m read from a drawn plan; no ink = not measured", async () => {
  const W = await import(here("./walls.ts"));
  const ppm = 30; // px per metre
  const m = (v: number) => v * ppm;
  // two rooms 4 x 5 m (inner faces) with a 0.20 m wall between them, external walls 0.35 m, drawn solid; origin 3 m in
  const o = 3;
  const rooms: P[][] = [rect(m(o), m(o), m(4), m(5)), rect(m(o + 4.2), m(o), m(4), m(5))];
  const inRect = (x: number, y: number, r: P[]) => x >= r[0][0] && x < r[1][0] && y >= r[0][1] && y < r[2][1];
  const outer = rect(m(o - 0.35), m(o - 0.35), m(8.2 + 0.7), m(5 + 0.7));
  const ink = (x: number, y: number) => inRect(x, y, outer) && !rooms.some((r) => inRect(x, y, r));
  const w = W.inkWalls(ink, m(16), m(12), rooms, ppm);
  assert.equal(w.why, "", w.why);
  assert.ok(Math.abs(w.internal.m - 0.2) <= 0.04, `internal ${w.internal.m}`);
  assert.ok(Math.abs(w.external.m - 0.35) <= 0.04, `external ${w.external.m}`);
  assert.equal(W.externalWall(w), Math.round(w.external.m * 100) / 100);
  assert.ok(Math.abs(W.externalReach(w) - 0.35) <= 0.04, `reach ${w.external.reach}`);
  const none = W.inkWalls(() => false, m(16), m(12), rooms, ppm);
  assert.equal(W.externalWall(none), null);
  assert.match(none.why, /too few/);
});

test("export: every row names its convention; totals carry default + change", () => {
  const rs = C.roomsOf(ROWS);
  const ap = C.applyConventions(ROWS, rs, { ...C.DEFAULTS, wetSkirting: false, doors: "leaves" }, C.measureWalls(rs), null);
  const out = C.exportRows(ap, ROWS, "upload/x/plan.pdf");
  assert.equal(out[0].length, C.EXPORT_HEAD.length);
  const conv = C.EXPORT_HEAD.indexOf("convention");
  for (const r of out) assert.ok(String(r[conv]).length > 0, JSON.stringify(r));
  const wc = out.find((r: unknown[]) => r[1] === "WC" && r[2] === "skirting");
  assert.equal(wc[3], null);
  assert.equal(wc[6], "left out");
  assert.match(String(wc[conv]), /wet rooms left out/);
  const printed = out.find((r: unknown[]) => r[0] === "printed");
  assert.match(String(printed[conv]), /conventions not applied/);
  const doors = out.find((r: unknown[]) => r[0] === "total" && r[2] === "doors");
  assert.deepEqual([doors[3], doors[5], doors[6]], [5, 4, 1]);
  const csv = C.toCsv(C.EXPORT_HEAD, out);
  assert.ok(csv.startsWith("﻿section,item,"));
});
