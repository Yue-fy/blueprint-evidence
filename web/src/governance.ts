/** Data governance in the customer view (captain 18:53): where a question's drawing went, and the private mode's
 *  measured tradeoff. One place for the numbers; update them when G1 / S0 measure private vs hybrid again. */
import { modelsUsed } from "./card";
import type { Answer } from "./types";

/** Measured, not estimated. S0 16:09 (gate d16 code, solver upload): the same 105 held-out questions (wild-sealed) with
 *  the hosted steps on (gemini = 1, verify = 1, as the demo runs) and off. QA 18:56: Stoa's own four example sheets
 *  through the UI, 17 questions: Gemini on 11, our local reading alone 12. */
export const PRIVACY_TRADEOFF = {
  // P1 20:15 (experiments/p1/private on night-P1): newest stoa-like dev pairs, same code, private vs hybrid
  heldOut: { questions: 153, withHosted: 75.2, private: 77.1, medianSecondsHosted: 8.6, medianSecondsPrivate: 7.7, source: "P1, 14 Nov 20:15" },
  // RL 20:15: gate d26 ed95f26, Stoa's 90 example questions, hosted answers replayed from cache
  stoaSheets: { questions: 90, withHosted: 78, private: 78, costPer1000Hosted: 4.42, costPer1000Private: 0.94, source: "RL, gate d26, 14 Nov 20:15" },
};

export function tradeoffLine(): string {
  const h = PRIVACY_TRADEOFF.heldOut, s = PRIVACY_TRADEOFF.stoaSheets;
  return `Measured on ${h.questions} unseen questions like Stoa's: ${Math.round(h.private)} % right in private mode vs ${Math.round(h.withHosted)} % with Gemini, `
    + `about ${Math.round(h.medianSecondsPrivate)} s instead of ${Math.round(h.medianSecondsHosted)} s per answer (median). `
    + `On Stoa's own ${s.questions} example questions: ${s.private} vs ${s.withHosted} right, at $${s.costPer1000Private.toFixed(2)} instead of $${s.costPer1000Hosted.toFixed(2)} per 1,000 questions.`;
}

export type Place = { label: string; cls: "local" | "gm" | "nv" | "eu"; who: string };
const PLACES: Record<string, Place> = {
  local: { label: "our server", cls: "local", who: "our own readers and local models" },
  gemini: { label: "Gemini (Google)", cls: "gm", who: "a Gemini 3.1 Pro step" },
  nvidia: { label: "NVIDIA Nemotron", cls: "nv", who: "an NVIDIA Nemotron step" },
  verda: { label: "Verda (EU)", cls: "eu", who: "an EU-hosted text model on Verda" },
};

/** Where this answer's drawing / question went: our server always; a hosted model only when one of its steps made a
 *  contribution (G1's per-answer data_used field or 'data_used: …' trace line when present, else the model credits
 *  in the trace; a step that failed or was skipped sent nothing that counts). */
export function dataWent(a: Answer): Place[] {
  const rec = a as unknown as Record<string, unknown>;
  const lines = (a.trace ?? []).map(String);
  const declared = Array.isArray(rec.data_used) ? (rec.data_used as unknown[]).map(String)
    : lines.find((l) => /^\s*data_used\s*:/i.test(l))?.replace(/^\s*data_used\s*:\s*/i, "").split(/[\s,;+]+/);
  const keys = new Set<string>(["local"]);
  if (declared?.length) {
    for (const d of declared.map((x) => x.toLowerCase())) if (PLACES[d]) keys.add(d);
  } else {
    for (const m of modelsUsed(a)) {
      if (m.cls === "gm") keys.add("gemini");
      if (m.cls === "nv") keys.add("nvidia");
    }
    if (lines.some((l) => /\bverda\b/i.test(l) && !/^\s*data:/i.test(l))) keys.add("verda");
  }
  return [...keys].map((k) => PLACES[k]);
}

/** The value strip's line: '🔒 Stayed on our server' or 'Data: our server · Gemini (Google)'. */
export function dataLineHtml(a: Answer, esc: (s: string) => string): string {
  const ps = dataWent(a);
  const priv = (a.trace ?? []).some((l) => /^\s*data:\s*private mode/i.test(String(l)));
  if (ps.length === 1)
    return `<span class="data-went local" title="${esc(`Every step ran on our server (${ps[0].who}); the drawing was not sent to any outside service${priv ? " (private mode)" : ""}.`)}">`
      + `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 11V8a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`
      + `Stayed on our server</span>`;
  return `<span class="data-went hosted" title="${esc(`Parts of this question went to: ${ps.slice(1).map((p) => `${p.label} (${p.who})`).join(", ")}. Switch on Private in the top bar to keep drawings on our server.`)}">`
    + `Data: ${ps.map((p) => esc(p.label)).join(" · ")}</span>`;
}
