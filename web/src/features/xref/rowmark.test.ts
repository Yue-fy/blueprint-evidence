// F8 xref row mark: `node --test web/src/features/xref/rowmark.test.ts` (Node >= 22.18 strips the types).
// Run by tests/test_feat_xref.py. Cases = X1's SFO 2-page set, 'How tall is door 7.04A?' on d31 / night 83545b7f: the
// schedule reader answered 7'-0" (right) but marked the FIRST 7'-0" on the sheet (row 1.01A).
import assert from "node:assert/strict";
import { test } from "node:test";

const { markRow } = await import(String(new URL("./rowmark.ts", import.meta.url)));

const hop = {
  to_page: 1,
  to_bbox: [363.08, 986.59, 384.92, 1229.92],
  to_row: { key: "7.04A", table: "DOOR SCHEDULE", cells: [
    { text: "7.04A", bbox: [367.08, 1193.77, 380.92, 1225.92] },
    { text: "3' - 0\"", bbox: [367.08, 1130.38, 380.92, 1161.66] },
    { text: "7' - 0\"", bbox: [367.08, 1057.54, 380.92, 1088.82] },
    { text: "0' - 2\"", bbox: [367.08, 990.59, 380.92, 1021.87] },
  ] },
};
const elsewhere = { file: "upload/x/sfo-set.pdf", page: 1, bbox: [60, 1057, 74, 1088], bbox_space: "pdf_pt", note: "'7' - 0\"' printed here, near 'height' (text search)" };

test("the mark moves from the first match on the sheet to the named row's cell", () => {
  const a = markRow({ value: "7'-0\"", unit: null, confidence: 0.5, evidence: [elsewhere, { file: "upload/x/sfo-set.pdf", page: 0, bbox: [1, 2, 3, 4] }], trace: [] }, hop, "upload/x/sfo-set.pdf");
  assert.equal(a.evidence.length, 2);                                 // page-0 item kept, the page-1 stray one replaced
  assert.deepEqual(a.evidence[0].bbox, [367.08, 1057.54, 380.92, 1088.82]);
  assert.equal(a.evidence[0].exact, true);
  assert.match(a.evidence[0].note, /row 7\.04A of the DOOR SCHEDULE/);
  assert.equal(a.evidence[1].page, 0);
  assert.match(a.trace[0], /^xref: marked the value in row 7\.04A/);
  assert.equal(a.support.state, "supported");
  assert.match(a.support.why, /row 7\.04A/);
});

test("a size answer marks both cells; an answer already in the row is left alone", () => {
  const a = markRow({ value: "3'-0\" wide x 7'-0\" high", unit: null, confidence: 0.3, evidence: [elsewhere], trace: [] }, hop, null);
  assert.deepEqual(a.evidence.map((e: { bbox: number[] }) => e.bbox[1]), [1130.38, 1057.54]);
  const inRow = { ...elsewhere, bbox: [367.08, 1057.54, 380.92, 1088.82] };
  const b = markRow({ value: "7'-0\"", unit: null, confidence: 0.85, evidence: [inRow], trace: [] }, hop, null);
  assert.equal(b.evidence[0], inRow);
  assert.equal(b.trace.length, 0);
  assert.equal(b.support, undefined);
});

test("no change when the value is not printed in the row, or is a converted number", () => {
  for (const v of ["6'-8\"", 2.13, "3.0", null]) {
    const a = markRow({ value: v, unit: null, confidence: 0.3, evidence: [elsewhere], trace: [] }, hop, "f.pdf");
    assert.equal(a.evidence[0], elsewhere, String(v));
  }
  const noRow = markRow({ value: "7'-0\"", unit: null, confidence: 0.3, evidence: [elsewhere], trace: [] }, { ...hop, to_row: null }, "f.pdf");
  assert.equal(noRow.evidence[0], elsewhere);
});

// AW 078-24-28 A-601 (156-sheet set, d31): 'What is the height of door 104S?' -> pdf_text 1 3/4" @0.85 = the THICKNESS
// cell; the HEIGHT heading ('WIDTH HEIGHT THICKNESS' line) sits over 7' - 0".
const aw = {
  to_page: 93,
  to_bbox: [252, 1124, 700, 1142],
  to_row: { key: "104S", table: "DOOR SCHEDULE", cells: [
    { text: "104S", bbox: [256, 1128, 278, 1138] }, { text: "3' - 0\"", bbox: [315, 1128, 339, 1138] },
    { text: "7' - 0\"", bbox: [367, 1128, 391, 1138] }, { text: "1 3/4\"", bbox: [434, 1128, 458, 1138] },
  ], asked: { column: "height", text: "7' - 0\"", bbox: [367, 1128, 391, 1138], header: "WIDTH HEIGHT THICKNESS" } },
};

test("the printed cell under the asked heading wins over another cell of the row", () => {
  const a = markRow({ value: "1 3/4\"", unit: null, confidence: 0.85, evidence: [{ file: "f.pdf", page: 93, bbox: [434, 1128, 458, 1138] }], trace: [], assumptions: [] }, aw, null);
  assert.equal(a.value, "7'-0\"");
  assert.deepEqual(a.evidence[0].bbox, [367, 1128, 391, 1138]);
  assert.equal(a.evidence.length, 1);
  assert.equal(a.assumptions[0], "convention: the height printed in the DOOR SCHEDULE, row 104S (the scheduled size)");
  assert.match(a.assumptions[1], /HEIGHT column: the WIDTH HEIGHT THICKNESS heading's column prints 7' - 0" in row 104S/);
  assert.match(a.assumptions[1], /the cell '1 3\/4"' of another column/);
  assert.equal(a.support.state, "supported");
});

test("a reader that agrees with the asked cell keeps its value; a metric answer of the same size agrees", () => {
  const ok = markRow({ value: "7'-0\"", unit: null, confidence: 0.85, evidence: [{ file: "f.pdf", page: 93, bbox: [367, 1128, 391, 1138] }], trace: [] }, aw, null);
  assert.equal(ok.value, "7'-0\"");
  assert.equal(ok.trace.length, 0);                                   // already on the asked cell: untouched
  const metric = markRow({ value: 2.134, unit: "m", confidence: 0.6, evidence: [], trace: [] }, aw, "f.pdf");
  assert.equal(metric.value, 2.134);
  assert.deepEqual(metric.evidence[0].bbox, [367, 1128, 391, 1138]);
});

// F8 c8: 'What size is door X?' after the hop (d32, SFO A-303 / AW A-601): the reader answered one cell ('3' - 0"', 2 of
// 10), 'W wide x nan high' or HEIGHT x THICKNESS ('7'-0" x 1 3/4"' for 105S); the row prints both cells.
const size = { ...aw, to_row: { ...aw.to_row, asked: { column: "size", text: "3' - 0\" x 7' - 0\"", bbox: [315, 1128, 339, 1138],
  header: "WIDTH HEIGHT THICKNESS", parts: [
    { column: "width", text: "3' - 0\"", bbox: [315, 1128, 339, 1138], header: "WIDTH HEIGHT THICKNESS" },
    { column: "height", text: "7' - 0\"", bbox: [367, 1128, 391, 1138], header: "WIDTH HEIGHT THICKNESS" }] } } };
const rowBox = { file: "f.pdf", page: 93, bbox: [252, 1124, 700, 1142], note: "t2 row" };

test("a size answer that is one cell, NaN or another pair becomes width x height, both cells marked", () => {
  for (const v of ["3' - 0\"", "3'-0\" wide x nan high", "7'-0\" x 1 3/4\"", "7'-0\" x 3'-0\""]) {
    const a = markRow({ value: v, unit: null, confidence: 0.65, evidence: [rowBox], trace: [], assumptions: [] }, size, null);
    assert.equal(a.value, "3'-0\" x 7'-0\"", v);
    assert.ok(a.confidence >= 0.8, v);
    assert.deepEqual(a.evidence.map((e: { bbox: number[] }) => e.bbox[0]), [315, 367], v);
    assert.match(a.assumptions[0], /^convention: the width x height printed in the DOOR SCHEDULE, row 104S/, v);
    assert.match(a.assumptions[1], /WIDTH and HEIGHT columns \(width x height\): row 104S of the DOOR SCHEDULE prints width 3' - 0", height 7' - 0"/, v);
    assert.match(a.assumptions[1], new RegExp(`the reader said ${v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`), v);
  }
});

test("a size answer that prints width then height is kept, raised to 0.8, marked on both cells", () => {
  const a = markRow({ value: "3'-0\" wide x 7'-0\" high", unit: null, confidence: 0.3, evidence: [rowBox], trace: [] }, size, null);
  assert.equal(a.value, "3'-0\" wide x 7'-0\" high");
  assert.equal(a.confidence, 0.8);
  assert.match(a.trace.join("\n"), /WIDTH \/ HEIGHT columns print 3' - 0" x 7' - 0" in row 104S, as the reader said: confidence 0.3 -> 0.8/);
  assert.deepEqual(a.evidence.map((e: { bbox: number[] }) => e.bbox[0]), [315, 367]);
  assert.equal(a.support.state, "supported");
  assert.match(a.support.why, /^the printed values '3' - 0"' and '7' - 0"' are marked in row 104S/);
  assert.equal(a.assumptions[0], "convention: the width x height printed in the DOOR SCHEDULE, row 104S (the scheduled size)");
  const sure = markRow({ value: "3'-0\" x 7'-0\"", unit: null, confidence: 0.9, evidence: [rowBox], trace: [] }, size, null);
  assert.equal(sure.confidence, 0.9);
});

test("a reader that gave no value gets the printed cell(s) of the named row (G1: door sizes are text questions)", () => {
  const a = markRow({ value: null, unit: null, confidence: 0.1, evidence: [], trace: [], assumptions: [], missing: ["the door's size"] }, size, "f.pdf");
  assert.equal(a.value, "3'-0\" x 7'-0\"");
  assert.equal(a.confidence, 0.8);
  assert.deepEqual(a.missing, []);
  assert.match(a.assumptions[1], /row 104S of the DOOR SCHEDULE prints width 3' - 0", height 7' - 0" .*; the reader gave no value$/);
  assert.deepEqual(a.evidence.map((e: { bbox: number[] }) => e.bbox[0]), [315, 367]);
  assert.equal(a.evidence[0].file, "f.pdf");
  assert.equal(a.support.state, "supported");
  const one = markRow({ value: "", unit: null, confidence: 0, evidence: [], trace: [] }, { ...size, to_row: { ...size.to_row, asked: { ...size.to_row.asked.parts[1] } } }, "f.pdf");
  assert.equal(one.value, "7'-0\"");
  assert.match(one.assumptions[1], /HEIGHT column: .*the reader gave no value$/);
  // no asked column: nothing to read, the abstention stays
  const none = markRow({ value: null, unit: null, confidence: 0, evidence: [], trace: [] }, hop, "f.pdf");
  assert.equal(none.value, null);
});
