// F1 plainScale (UX 02:35 / SO-11): `node --test web/src/features/region/plainscale.test.ts` (Node >= 22.18 strips the types).
// Run by tests/test_feat_region.py. A picture's scale '1 m = 145 px' is jargon in customer text; say the ratio or its source.
import assert from "node:assert/strict";
import { test } from "node:test";

const { plainScale } = await import(String(new URL("./scalewords.ts", import.meta.url)));

test("a ratio label is kept", () => {
  assert.equal(plainScale({ label: "1:200", source: "printed on the drawing" }), "1:200");
  assert.equal(plainScale(null), "");
});

test("a picture's px label becomes the printed ratio it came from", () => {
  assert.equal(plainScale({ label: "1 m = 51 px", source: "printed 1:200 on the sheet; the picture is 257 dpi" }), "1:200");
  assert.equal(plainScale({ label: "1 m = 29 px", source: "printed 1\" = 40'-0\" on a Tabloid sheet (its dimension strings fit)" }), "1\" = 40'-0\"");
});

test("no ratio: where the scale came from, never px", () => {
  assert.equal(plainScale({ label: "1 m = 145 px", source: "from the scale bar on the drawing" }), "from the scale bar");
  assert.equal(plainScale({ label: "1 m = 39 px", source: "set by you", user: true }), "set by you");
  assert.equal(plainScale({ label: "1 m = 40 px", source: "read from the drawing" }), "from the drawing");
  for (const s of ["from the scale bar", "set by you", "from the drawing"]) assert.ok(!/px/.test(s));
});
