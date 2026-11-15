// T3 regression (2) (M1 03:14, captain 03:58): after 'Which one did you mean?' -> mark 2 the card showed the MEAN of the
// same-named rooms, because web/src/api.ts ask() sends solver_args only when a solver is named (or a plug-in adds ask
// args), and the project's re-ask named none -> its {learn, visitor} were dropped and the plain cached mean came back.
// `node --test web/src/features/project/pick_ask.test.ts` (Node >= 22.18 strips the types). Run by tests/test_feat_project.py.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";

// the web's own imports have no extension ('./features/registry'): resolve them to the .ts file, as Vite does
registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (e) {
      if (/^\.\.?\//.test(spec) && !/\.[a-z]+$/i.test(spec)) return next(`${spec}.ts`, ctx);
      throw e;
    }
  },
});

// registry.ts hangs a debug handle on window and a key listener on document at import time: minimal stand-ins
const g = globalThis as unknown as Record<string, unknown>;
const quiet = { addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true };
g.window = Object.assign(globalThis, quiet);
g.document = { ...quiet, querySelector: () => null, getElementById: () => null };
const bodies: Record<string, unknown>[] = [];
globalThis.fetch = (async (_url: unknown, init?: { body?: unknown }) => {
  bodies.push(JSON.parse(String(init?.body ?? "{}")));
  return new Response(JSON.stringify({ value: 51.7, unit: "m2", confidence: 0.5, evidence: [] }),
    { status: 200, headers: { "content-type": "application/json" } });
}) as typeof fetch;
const { ask } = await import(String(new URL("../../api.ts", import.meta.url)));

const UP = "upload/0123456789ab/SEN-L00.pdf";
const Q = "How big is the classroom?";

test("api.ask keeps solver_args only when a solver is named (why the pick's re-ask names 'pipeline')", async () => {
  await ask(UP, Q, "pipeline", { learn: 3, visitor: "visitorAAAA1111" });
  assert.equal(bodies.at(-1)?.solver, "pipeline");
  assert.deepEqual(bodies.at(-1)?.solver_args, { learn: 3, visitor: "visitorAAAA1111" });
  await ask(UP, Q, undefined, { learn: 3, visitor: "visitorAAAA1111" });
  assert.equal(bodies.at(-1)?.solver_args, undefined); // the trap the re-ask fell into
});

test("the review list's pick / correction re-ask names solver 'pipeline' with learn + visitor", () => {
  const src = readFileSync(new URL("./index.ts", import.meta.url), "utf8");
  const calls = src.match(/api\.askWith\([^\n]*solver_args: \{ learn: c\.rev, visitor: VISITOR \}[^\n]*\)/g) ?? [];
  assert.ok(calls.length >= 1, "the pick's re-ask with learn + visitor is gone from project/index.ts");
  for (const c of calls) assert.match(c, /solver: "pipeline"/, `re-ask without a solver loses its solver_args: ${c}`);
});
