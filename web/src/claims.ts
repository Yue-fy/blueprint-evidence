/**
 * The four headline claims (captain 05:00 Sun, prompts/LANDING.md), in ONE place: the landing's proof strip and the
 * customer "How accurate is it?" page both read them, so the two can never disagree.
 * Source: V1's run of the final demo code on ALL 127,044 dataset questions (KNOWLEDGE 04:21, artifacts/V1/runs/all-d34b:
 * Verda A100, pipeline, Gemini off; 80,653 answers at confidence >= 0.8, 98.3 % right). Change them only with a new
 * full-dataset run.
 */
/** line: the landing's one-line form (captain SPRINT-0550: big number + one line); text + small: the full claim on the measured page. */
export type Claim = { big: string; text: string; small: string; line?: string };

export const CLAIMS: Claim[] = [
  { big: "98 %", text: "right when it says it’s sure", small: "80,653 sure answers out of 127,044 dataset questions" },
  { big: "99.3 %", text: "on CAD floor plans · 100 % on IFC models", line: "on CAD plans · IFC 100 %", small: "all 37,584 + 3,062 questions of those dataset parts" },
  { big: "≈ €0.005", text: "per 1,000 questions", small: "measured on Verda GPUs" },
  { big: "Private", text: "nothing leaves the server", small: "on by default" },
];

/** The same run, all of it (UX 05:12 (c): show the whole-dataset number next to the parts, so nobody asks about cherry-picking).
 *  Re-counted by W5 from artifacts/V1/runs/all-d34b/preds_all.jsonl.gz: 104,316 of 126,208 scored right; confidence >= 0.9
 *  ('Very sure' on the card) 77,828 of 78,885 = 98.66 %; CubiCasa 63,879 of 85,514. */
export const DATASET_OVERALL = { pct: "82.65 %", scored: "126,208", veryPct: "98.7 %", veryN: "78,885", scanPct: "74.7 %" };
