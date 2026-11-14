"""The pipeline's last step for unsure answers: Gemini plans another way to the answer, Nemotron checks the evidence.

Both are OFF by default (Google's no-abuse rule, NVIDIA credits): solver args `gemini=1` / `verify=1`, or the env
defaults STOA_GEMINI_ESCALATE=1 / STOA_VERIFY=1 for a demo server. Never in bulk evals.

Combining (router rule 3, a new reader checks, it does not overrule a sure answer):
- Gemini reaches the same value      -> keep it; confidence up to max(own, min(Gemini's, AGREE_MAX)); ✓ in the trace
- Gemini reaches another value with clearly more tool support (conf >= own + TAKE_MARGIN) -> Gemini's answer, the old
  one named in `missing`; otherwise keep ours and name Gemini's in `missing`
- Nemotron's verdict on the final answer moves only its confidence (stoa.agent.verify.apply_verdict)
"""

from __future__ import annotations

import os
import re
import time

from stoa.contract import Answer

GEMINI_BELOW = float(os.environ.get("STOA_GEMINI_BELOW", 0.5))  # escalate answers below this confidence
VERIFY_BELOW = float(os.environ.get("STOA_VERIFY_BELOW", 0.8))
AGREE_MAX = 0.8
TAKE_MARGIN = 0.1


def _same(a, b, unit) -> bool:
    from stoa.pipeline import _agree

    return _agree(a, b, "count" if (unit or "") == "count" else "area")


def gemini_step(pipe, path: str, question: str, ans: Answer) -> Answer:
    from stoa.agent.gemini import GeminiAgent

    t0 = time.perf_counter()
    ag = GeminiAgent(ask_fn=lambda p, q: pipe._solve_atomic(p, q, allow_agent=False))
    try:
        g = ag.solve(path, question, base=ans)
    except Exception as e:  # noqa: BLE001
        ans.trace = [*ans.trace, f"escalation: {ag.label} agent failed ({type(e).__name__}); answer kept"]
        return ans
    cost = (ans.cost.add(g.cost) if ans.cost else g.cost)
    n_steps = sum(1 for t in g.trace if re.match(r"\s*s\d+ \w+\(", str(t)))
    head = (f"{ag.label} planned {n_steps} step(s) (our answer was unsure, {ans.confidence:.2f} < {GEMINI_BELOW})")
    if g.value is None:
        ans.trace = [*ans.trace, head + f" and reached no answer ({(g.missing or ['?'])[0][:80]}); answer kept"]
        ans.cost = cost
        return ans
    steps = [f"  {t}" for t in g.trace[1:]]
    if str(ans.solver or "").endswith("prior") and _same(ans.value, g.value, ans.unit or g.unit):
        # its tools only reached the same typical value: no reading of this drawing, so no agreement to claim
        ans.trace = [*ans.trace, head + f": its tools reached only the same typical value ({g.value}), not a reading of "
                     "this drawing; answer kept", *steps]
        ans.cost = cost
        return ans
    if _same(ans.value, g.value, ans.unit or g.unit):
        c = max(ans.confidence, min(g.confidence, AGREE_MAX))
        ans.trace = [*ans.trace, head + f": same value {g.value} ✓ → confidence {ans.confidence:.2f} → {c:.2f}", *steps]
        if not ans.evidence:
            ans.evidence = g.evidence
        ans.assumptions = [*ans.assumptions, *[a for a in g.assumptions if a not in ans.assumptions][:4]]
        ans.confidence = round(c, 3)
        ans.cost = cost
        return ans
    if g.confidence >= ans.confidence + TAKE_MARGIN:
        g.trace = [*ans.trace[:3], head + f": {g.value} {g.unit or ''} @{g.confidence:.2f} replaces {ans.value} "
                   f"@{ans.confidence:.2f} (better tool support)", *steps]
        g.missing = [*g.missing, f"the cascade answered {ans.value} {ans.unit or ''} @{ans.confidence:.2f} "
                                 f"({ans.solver}) before {ag.label}'s plan"]
        g.solver = "pipeline/gemini_agent"
        g.cost = cost
        g.cost.wall_seconds = round((ans.cost.wall_seconds if ans.cost else 0) + time.perf_counter() - t0, 3)
        return g
    ans.trace = [*ans.trace, head + f": {g.value} {g.unit or ''} @{g.confidence:.2f}, not better supported; "
                 "answer kept", *steps]
    ans.missing = [*ans.missing, f"{ag.label}'s plan reached {g.value} {g.unit or ''} @{g.confidence:.2f}"]
    ans.cost = cost
    return ans


# team-wide Gemini calls per day before escalation stops asking (Google's no-abuse rule; logs/gemini_calls.jsonl)
TEAM_DAY_CAP = int(os.environ.get("STOA_GEMINI_TEAM_CAP", 800))


def _team_calls_today() -> int:
    from stoa.llm import GEMINI_LOG

    if os.environ.get("STOA_LLM_REPLAY") == "1":
        return 0  # S0 arbitration replay: answers only from the disk cache, no new calls -> the day cap does not apply

    day, n = time.strftime("%Y-%m-%d"), 0
    try:
        with open(GEMINI_LOG, encoding="utf-8") as f:
            n = sum(1 for line in f if f'"day": "{day}"' in line)
    except OSError:
        pass
    return n


def escalate(pipe, path: str, question: str, ans: Answer, gemini: bool, verify: bool) -> Answer:
    """Gemini: unsure (< GEMINI_BELOW) answers to non-template wordings, <= 3 calls (plan, answer, one more answer),
    responses cached by request content (drawing image + question). Nemotron: unsure (< VERIFY_BELOW) answers with NO
    located evidence only (with boxes it just agrees with them: experiments/g1 verify_v40)."""
    from stoa.pipeline import _located
    from stoa.router.prior import known_template

    if gemini and os.environ.get("STOA_GEMINI_VISION_STEP", "1") != "0" and \
            str(path).lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")):
        # G1 cycle 3 (captain 14:39): the cascade fell to the prior / found nothing located -> Gemini looks at the page
        from stoa.agent.vision import vision_step, wanted

        why = wanted(ans, question, path)
        if why:
            if _team_calls_today() >= TEAM_DAY_CAP:
                ans.trace = [*ans.trace, f"vision reading skipped: the team's Gemini calls today reached {TEAM_DAY_CAP}"]
            else:
                ans = vision_step(path, question, ans, why=why)
                if str(ans.solver or "").startswith("pipeline/gemini_") or any(
                        str(t).startswith("hybrid routing (") or "cross-check: its estimate" in str(t)
                        for t in ans.trace[-3:]):
                    # one Gemini call per question here (vision answer, located-and-measured area, area cross-check):
                    # the planner would re-ask the same readers and could replace a measured area by a relayed one
                    gemini = False
    if ans.value is None:
        return ans
    if gemini and ans.confidence < GEMINI_BELOW and not known_template(question):
        if _team_calls_today() >= TEAM_DAY_CAP:
            ans.trace = [*ans.trace, f"escalation skipped: the team's Gemini calls today reached {TEAM_DAY_CAP}"]
        else:
            ans = gemini_step(pipe, path, question, ans)
    if verify and ans.confidence < VERIFY_BELOW and not _located(ans):
        from stoa.agent.verify import apply_verdict, verify as nemotron

        ans = apply_verdict(ans, nemotron(path, question, ans))
    return ans


__all__ = ["escalate", "gemini_step", "GEMINI_BELOW", "VERIFY_BELOW"]
