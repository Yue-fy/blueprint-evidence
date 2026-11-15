// W5 governance unit tests: `node --test web/src/governance.test.ts` (Node >= 22.18 strips the types). Pure module, no DOM.
// Captain 03:58: the answer card may name Gemini only when the answer's data_used / cost / network line show Gemini ran
// (QA 03:13: in Private mode a printed-figure card said 'our reading and Gemini's agree' next to 'Stayed on our server').
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

// governance.ts imports "./card" without an extension (Vite / tsc bundler style): resolve it to card.ts here
registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (e) {
      if (spec.startsWith(".") && !/\.[a-z]+$/i.test(spec)) return next(`${spec}.ts`, ctx);
      throw e;
    }
  },
});
const G = await import(String(new URL("./governance.ts", import.meta.url)));

const PRIVATE_LINE = "data: private mode: no hosted model was called (Gemini, NVIDIA off)";
// QA 03:13 / UX 03:16 (demo 75f9ee4e, Private default): AFCEC 'How much common space…' and Dormitory west wing
const afcecPrivate = {
  value: 4479, unit: "SF", confidence: 0.9, solver: "pipeline/raster", evidence: [],
  trace: ["raster (question as asked): 4,479 SF (TOTAL COMMON SPACE)", "Read 'TOTAL COMMON SPACE = 4,479 SF'", PRIVATE_LINE],
  cost: { api_calls: 0, llm_tokens_in: 812 }, data_used: ["local"],
};

test("Private mode's own trace line is not a Gemini reading", () => {
  assert.equal(G.geminiRead(PRIVATE_LINE), false);
  assert.equal(G.geminiRead("escalation (confidence 0.30 < 0.5): Gemini 3.1 Pro planned 0 step(s) (…) and reached no answer (Gemini 3.1 Pro planner unavailable (RuntimeError: backend 'gemini' needs $GEMINI…); answer kept"), false);
  assert.equal(G.geminiRead("two independent readings agree (ours 4,479 SF, Gemini 4,479 SF) ✓"), true);
});

test("a Private answer: Gemini did not run, the data stayed on our server", () => {
  assert.equal(G.geminiRan(afcecPrivate), false);
  assert.deepEqual(G.dataWent(afcecPrivate).map((p: { label: string }) => p.label), ["our server"]);
  // the same answer without a declared data_used (older ask_upload): still not Gemini
  assert.equal(G.geminiRan({ ...afcecPrivate, data_used: undefined }), false);
});

test("Gemini ran only when the data says so", () => {
  const ran = { ...afcecPrivate, trace: ["two independent readings agree (ours 4,479 SF, Gemini 4,479 SF) ✓"], cost: { api_calls: 1, gemini_usd: 0.004 }, data_used: ["local", "gemini"] };
  assert.equal(G.geminiRan(ran), true);
  // its cost alone (no declared data_used)
  assert.equal(G.geminiRan({ ...afcecPrivate, data_used: undefined, trace: [], cost: { api_calls: 1, gemini_usd: 0.002 } }), true);
  // the network line (netguard) names Google's API
  assert.equal(G.geminiRan({ ...afcecPrivate, trace: ["network: this answer reached generativelanguage.googleapis.com:443"] }), true);
  // a hosted call whose provider is not named is not 'Gemini'
  assert.equal(G.geminiRan({ ...afcecPrivate, cost: { api_calls: 1 }, data_used: ["local", "hosted"] }), false);
  // a trace that only MENTIONS Gemini (a Private answer quoting a Gemini line from an older cache) is not enough
  assert.equal(G.geminiRan({ ...afcecPrivate, trace: [...afcecPrivate.trace, "two independent readings agree (ours 4,479 SF, Gemini 4,479 SF) ✓"] }), false);
});
