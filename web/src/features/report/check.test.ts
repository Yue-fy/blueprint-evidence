// F8 report geometry check: `node --test web/src/features/report/check.test.ts` (Node >= 22.18 strips the types).
// Run by tests/test_feat_report.py. Cases = the shapes of real upload answers that the replay over 749 upload runs found
// wrongly flagged (artifacts/F8/scripts/check_replay.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";

const { geometryCheck, fileScaleN } = await import(String(new URL("./check.ts", import.meta.url)));

const MM = 72 / 25.4;                                   // pdf_pt per mm on paper
const rect = (x: number, y: number, wmm: number, hmm: number) =>
  [[x, y], [x + wmm * MM, y], [x + wmm * MM, y + hmm * MM], [x, y + hmm * MM]];

test("an A1 sheet on an A3 page is checked at 1:400, not the printed 1:200", () => {
  // 20 x 10 mm on the A3 page at 1:400 = 8 x 4 m = 32 m²
  const a = { value: 32.1, unit: "m2", evidence: [{ page: 0, bbox_space: "pdf_pt", polygon: rect(100, 100, 20, 10) }],
    assumptions: ["the sheet prints its scale for A1 ('1:200 @ A1') but this PDF page is A3: the drawing is at 1:400 on this file; read on the page enlarged 2.00x to A1, where 1:200 holds",
      "scale: 1:200 printed on the sheet; traced from the wall lines (approximate)"] };
  assert.equal(fileScaleN(a), 400);
  const g = geometryCheck(a, "plan.pdf");
  assert.equal(g.verdict, "agrees", g.text);
  assert.match(g.text, /× 400²/);
});

test("a scanned PDF read at 300 dpi with a 29.8 px-per-m bar is at 1:396 on its page", () => {
  const a = { value: 1, unit: "m2", assumptions: ["scanned PDF (the page is one image, no text layer): read as an image at 300 dpi",
    "the image is an A1 sheet at 150 dpi (from its scale bar (29.80 px per m) and its printed 1:200, = A1 at its pixel size): …",
    "scale: 1:200 printed on the sheet"] };
  assert.equal(fileScaleN(a), 396);
});

test("the printed 1:N is used when nothing says otherwise", () => {
  assert.equal(fileScaleN({ value: 1, unit: "m2", assumptions: ["scale: 1:100 printed on the sheet"] }), 100);
});

test("skirting: the run is the outline's line + every separate 'skirting run' piece; openings only by name", () => {
  // outline 20 x 10 mm at 1:200 = 4 x 2 m (perimeter 12 m); run in two pieces 30 + 25 mm = 11 m; one 1 m opening (5 mm)
  const poly = rect(100, 100, 20, 10);
  const a = { value: 11.02, unit: "m", evidence: [
    { page: 0, bbox_space: "pdf_pt", note: "Store: skirting 11.02 m = outline 12.0 m minus 1 opening at 1:200 (approximate)", polygon: poly, polyline: [[100, 100], [100 + 30 * MM, 100]] },
    { page: 0, bbox_space: "pdf_pt", note: "Store: skirting run (approximate)", polyline: [[100, 120], [100 + 25 * MM, 120]] },
    { page: 0, bbox_space: "pdf_pt", note: "Store: opening 1.00 m (no skirting)", polyline: [[100, 140], [100 + 5 * MM, 140]] }],
    assumptions: ["scale: 1:200 printed on the sheet", "Store 12.0 - (1.0) = 11.02 m"] };
  const g = geometryCheck(a, "plan.pdf");
  assert.equal(g.verdict, "agrees", g.text);
  assert.match(g.text, /2 pieces/);
  assert.match(g.text, /deducted opening 5 mm/);
});

test("skirting with the openings NOT deducted (as asked) = the run + the openings it skips", () => {
  const poly = rect(100, 100, 20, 10);
  const a = { value: 12.02, unit: "m", evidence: [
    { page: 0, bbox_space: "pdf_pt", note: "Store: skirting 11.0 m = outline 12.0 m minus 1 opening at 1:200 (approximate)", polygon: poly, polyline: [[100, 100], [100 + 55 * MM, 100]] },
    { page: 0, bbox_space: "pdf_pt", note: "Store: opening 1.00 m (no skirting)", polyline: [[100, 140], [100 + 5 * MM, 140]] }],
    assumptions: ["scale: 1:200 printed on the sheet", "skirting = the room's whole traced outline (wall faces), door openings NOT deducted as asked; with the openings deducted it would be 11.0 m",
      "Store 12.0 - (1.0) = 11.0 m"] };
  const g = geometryCheck(a, "plan.pdf");
  assert.equal(g.verdict, "agrees", g.text);
  assert.match(g.text, /not deducted, as asked/);
});

test("a skirting line the check cannot name is not checked (no guessing what it is)", () => {
  const poly = rect(100, 100, 20, 10);
  const a = { value: 11, unit: "m", evidence: [
    { page: 0, bbox_space: "pdf_pt", note: "Store: skirting", polygon: poly, polyline: [[100, 100], [100 + 55 * MM, 100]] },
    { page: 0, bbox_space: "pdf_pt", note: "Store: something else", polyline: [[100, 140], [100 + 5 * MM, 140]] }],
    assumptions: ["scale: 1:200 printed on the sheet", "Store 12.0 - (1.0) = 11.0 m"] };
  assert.equal(geometryCheck(a, "plan.pdf"), null);
});

test("line thickness: a small room widened by half a pen per side is explained, a 0.5 mm widening is not", () => {
  // 17 x 10 mm at 1:200 = 3.4 x 2.0 m = 6.8 m²; perimeter 10.8 m; widened by 0.15 mm on paper (0.03 m) per side ≈ +0.33 m² (4.8 %)
  const base = { unit: "m2", evidence: [{ page: 0, bbox_space: "pdf_pt", note: "Laundry: traced at 1:200 (approximate)", polygon: rect(100, 100, 17, 10) }],
    assumptions: ["scale: 1:200 printed on the sheet", "its outline traced from the drawn wall lines around that point (door openings closed at 0.00 m, line thickness corrected; 24 of 24 readings agree)"] };
  const ok = geometryCheck({ ...base, value: 6.8 + 10.8 * 0.03 }, "plan.pdf");
  assert.equal(ok.verdict, "thick", ok.text);
  assert.match(ok.text, /0\.15 mm on paper per side/);
  const far = geometryCheck({ ...base, value: 6.8 + 10.8 * 0.1 }, "plan.pdf");   // 0.5 mm per side: not a line width
  assert.equal(far.verdict, "check", far.text);
});

test("a big picture read at a reduced size: its dpi is the reduced copy's, the outline is in full-size pixels", () => {
  // full 9922 px wide, read at 6000 px at 181 dpi -> 299.3 dpi at full size; 1:200; outline 1000 x 500 px at 299.3 dpi
  const dpi = 181 * 9922 / 6000, mm = 25.4 / dpi;
  const m2 = (1000 * mm * 200 / 1000) * (500 * mm * 200 / 1000);
  const a = { value: Number(m2.toFixed(2)), unit: "m2", evidence: [{ page: 0, bbox_space: "image_px", polygon: [[0, 0], [1000, 0], [1000, 500], [0, 500]] }],
    assumptions: ["large image (9922×7016 px) read at 6000×4243 px (a clean drawing: kept to flat grey levels)",
      "the image is an A1 sheet at 181 dpi (from its scale bar (36.00 px per m) and its printed 1:200, = A1 at its pixel size): …",
      "scale: 1:200 printed on the sheet; traced from the wall lines (approximate)"] };
  const g = geometryCheck(a, "plan.png");
  assert.equal(g.verdict, "agrees", g.text);
  assert.match(g.text, /full size/);
});

test("the reader's own 'scale: 1:400' line wins over the file-level recognition (A1 sheet on an A3 page, web upload)", () => {
  const a = { value: 32.1, unit: "m2", recognition: { scale: "1:200", scale_source: "printed" },
    evidence: [{ page: 0, bbox_space: "pdf_pt", polygon: rect(100, 100, 20, 10) }],
    assumptions: ["scale: 1:400 printed on the sheet; traced from the wall lines (approximate)",
      "recognised as: architectural drawing sheet (CAD/Revit) · 1:200 (confidence 0.70)"] };
  assert.equal(fileScaleN(a), 400);
  assert.equal(geometryCheck(a, "plan.pdf").verdict, "agrees");
});

test("a scanned PDF page read at 300 dpi whose image is 'an A1 sheet at 150 dpi' is at 1:400 (no bar reading needed)", () => {
  const a = { value: 1, unit: "m2", assumptions: ["scanned PDF (the page is one image, no text layer): read as an image at 300 dpi",
    "the image is an A1 sheet at 150 dpi (from its pixel size and the sheet size it prints): its lines were traced … at the printed scale",
    "scale: 1:200 printed on the sheet; traced from the wall lines (approximate)"] };
  assert.equal(fileScaleN(a), 400);
});

test("other wordings of the page's own scale: 'so the page scale is 1:400'; 'printed scale 1:200 at 150 dpi' on a 300 dpi scan", () => {
  assert.equal(fileScaleN({ value: 1, unit: "m2", assumptions: ["the scale 1:200 is printed for A1 paper; this page is A3, so the page scale is 1:400"] }), 400);
  assert.equal(fileScaleN({ value: 1, unit: "m2", assumptions: ["scanned PDF (the page is one image, no text layer): read as an image at 300 dpi",
    "the question's rooms = the 5 labels Maths 1, …; their areas are added; outlines measured with the printed scale 1:200 at 150 dpi (the image …)"] }), 400);
});
