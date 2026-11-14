"""Gemini plan-and-execute agent for question kinds no template covers (G1; "Best Use of Gemini API").

Why plan-and-execute and not a step-by-step loop: cost is scored and Google allows few calls. The router's local agent
calls its LLM once per step (5-7 calls); here Gemini sees the question, the drawing's thumbnail, the extracted table
and our pipeline's own unsure answer ONCE, writes every tool call up front (later inputs refer to earlier results as
{s1}), our local tools run the plan without any LLM in the loop, and a second Gemini call reads the results and
answers (optionally asking for a few more steps once). So 2 Gemini calls per question, 3 at most.

Numbers never come from Gemini: an answer must be a step reference (or a value a tool returned); confidence comes from
the tools via the router Agent's grounding rules (`Agent._finish`). The only thing Gemini may read itself is a short
TEXT off the image (a title, a name, a date) when no tool can; that answer is capped at READ_CAP unless the PDF text
layer finds the same words (then it gets their box as evidence).

    python -m stoa.eval --solver gemini_agent --split <small.jsonl>        (key needed; <= ~60 questions)
    python experiments/g1/ask_gemini.py <path> "<question>"                (one question, readable trace)
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from stoa.contract import Answer, Cost, Evidence
from stoa.data import abspath, siblings, source_of
from stoa.router.agent import KNOWN, SCOPE_PENALTY, Agent, _fmt, _num

PLANNER = os.environ.get("STOA_GEMINI_PLANNER", "gemini-3.1-pro-preview")
LABEL = {"gemini-3.1-pro-preview": "Gemini 3.1 Pro", "gemini-3-flash-preview": "Gemini 3 Flash",
         "gemini-3.1-flash-preview": "Gemini 3.1 Flash"}
MAX_PLAN = 8  # tool steps per plan
MAX_MORE = 4  # extra steps Gemini may ask for after seeing the results (once)
READ_CAP = 0.45  # a text Gemini read off the image itself, no tool confirms it
READ_FOUND = 0.6  # ... and the PDF text layer has the same words (their box = evidence)
# assumed list prices (USD per 1M tokens) for the cost line; preview pricing, written down so runs compare
PRICE = {"gemini-3.1-pro-preview": (2.0, 12.0), "gemini-3-flash-preview": (0.5, 3.0)}
THUMB_PIXELS = 1_400_000

PLAN_SYS = """You plan how to answer one question about a construction drawing or building model (floor plans, IFC
building models, schedules, historical scanned drawings). You do NOT compute or read numbers yourself: you write a
plan of tool calls; the tools run locally and return values with a confidence and where in the drawing they are.

Tools:
- ask: ONE simple question in English for the team's solvers (they are reliable on the KNOWN QUESTIONS forms; other
  simple wordings also work: a count of one object class, the area of one room / apartment / storey, a length, a
  volume, a label's text, the scale, a sheet number). Split complex questions into several asks.
- table: input "". The structured data extracted from this drawing (rooms, areas, apartments; or the IFC model's
  storeys, spaces and element counts). It is shown below when it exists.
- rows: (swiss drawings only) a condition on the room table, e.g. "area_m2 > 10 and numbered and not outdoor" or
  "apartment == 'B' and name == 'Bad'". Columns: number, name, area_m2, apartment, outdoor, numbered, n_doors,
  n_windows, n_toilets, n_washbasins, n_bathtubs, n_showers. Result: the count of matching rooms ({sN}) and their
  area sum ({sN.area}).
- find_text: a word or phrase searched in the PDF text layer; hits come back with page + box.
- look: a short question for the local vision model about the drawing image (last resort; weak at counting).
- calc: arithmetic over earlier results by reference, e.g. "{s1} * 450", "({s2} - {s3}) / {s3} * 100",
  "({s1} > 31) + ({s2} > 31)". min, max, round, abs, sum, sqrt work.
Rules:
1. Every number in a later input must be a {sN} reference or stated in the question. Never type a number you guessed.
2. Ask about names you can see in the table or the image (storey names, room numbers, apartment letters); for a
   whole-building question ask about the model, for one floor name the storey as the table writes it.
3. Everyday words map to drawing words: "ground floor" = the lowest above-ground storey (EG, 0, Pohjakerros,
   Erdgeschoss, '00 ...'); "bedroom" = Zimmer / Schlafzimmer / MH / Chambre / Pièce; "bathroom" = Bad / KH / WC.
   Say the mapping in "assumptions".
4. A question with several parts gets one step per part.
4b. Every ask must carry the question's scope words verbatim: the sheet ('On sheet A700 of the ... drawings'), the
   storey, the model, the apartment. An ask without them is answered for another scope.
5. Prefer the fewest steps; ask before look. If the question is fully answered by one KNOWN form, one ask is enough.
Reply with JSON only."""

PLAN_SCHEMA = {"type": "object", "properties": {
    "understanding": {"type": "string", "description": "what is asked, in <= 25 words"},
    "steps": {"type": "array", "maxItems": MAX_PLAN, "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "tool": {"type": "string",
                                           "enum": ["ask", "table", "rows", "find_text", "look", "calc"]},
        "input": {"type": "string"}}, "required": ["id", "tool", "input"]}},
    "assumptions": {"type": "array", "items": {"type": "string"}}},
    "required": ["understanding", "steps"]}

ANSWER_SYS = """You planned tool calls to answer a question about a construction drawing; their results are below.
Now give the final answer.
- "answer": a step reference such as "s3" whose value IS the answer (preferred for any number), several references
  joined by " ; " for a question with several parts, or a short text (a name, a storey, "yes"/"no").
- "source": "tools" when the answer comes from the results; "image" only for a short text you read off the drawing
  image yourself because no tool could (a title, a name, a date). Never read numbers off the image.
- "unit": count, m2, m3, m, ft2, ft, %, or the unit the question asks for (EUR, CHF, h, ...); "" for text.
- "reasoning": <= 40 words a person can check (which result answers what).
- "assumptions": what you assumed (word mappings, scope); "missing": what would make the answer sure.
- "more_steps": ONLY if a needed result is missing or failed: up to 4 more tool calls (ids continue the numbering),
  otherwise []. You get one more round at most.
Reply with JSON only."""

ANSWER_SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string"}, "source": {"type": "string", "enum": ["tools", "image"]},
    "unit": {"type": "string"}, "reasoning": {"type": "string"},
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "missing": {"type": "array", "items": {"type": "string"}},
    "more_steps": {"type": "array", "maxItems": MAX_MORE, "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "tool": {"type": "string",
                                           "enum": ["ask", "table", "rows", "find_text", "look", "calc"]},
        "input": {"type": "string"}}, "required": ["id", "tool", "input"]}}},
    "required": ["answer", "source", "reasoning"]}

_REF = re.compile(r"\{\s*(s\d+)(?:\.(area|count))?\s*\}")
_BARE_REF = re.compile(r"\s*\{?\s*(s\d+)(?:\.(area|count))?\s*\}?\s*")


def thumbnail(path: str):
    """The drawing as one image for Gemini (PDF page 0, a raster, or an IFC/DXF file's sibling sheet); None if none."""
    from PIL import Image

    from stoa import llm

    p = path
    if p.lower().endswith((".ifc", ".dxf", ".svg", ".json")):
        sib = siblings(path)
        p = sib.get(".pdf") or sib.get(".png") or sib.get(".jpg") or ""
    if not p:
        return None
    a = abspath(p)
    if not a.exists():
        return None
    if a.suffix.lower() == ".pdf":
        img, _ = llm.render_pdf_page(a, 0, dpi=150, max_pixels=THUMB_PIXELS)
        return img
    img = Image.open(a).convert("RGB")
    s = (THUMB_PIXELS / max(1, img.width * img.height)) ** 0.5
    if s < 1:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
    return img


class GeminiAgent(Agent):
    """`ask_fn(path, question) -> Answer` = the pipeline's atomic cascade (no agent inside, no escalation)."""

    def __init__(self, ask_fn=None, model: str = PLANNER, image: bool = True, **kw):
        super().__init__(ask_fn=ask_fn, backend="gemini", **kw)
        self.model = model
        self.image = image
        self.label = LABEL.get(model, model)
        self._names: set[str] = set()

    # -- Gemini calls ----------------------------------------------------------------------------------
    def _call(self, msgs, schema, images, purpose: str, cost_box: list):
        from stoa import llm
        from stoa.agent.private import check

        check("gemini")  # private mode: behaves like an unreachable API
        r = llm.chat(msgs, images=images or None, backend="gemini", model=self.model, max_tokens=6000,
                     temperature=0.0, json_schema=schema, purpose=purpose, cache=True, max_pixels=THUMB_PIXELS,
                     max_side=4096)
        pin, pout = PRICE.get(self.model, (2.0, 12.0))
        tin, tout = r.prompt_tokens or 0, r.completion_tokens or 0
        c = Cost(llm_calls=1, api_calls=1, llm_tokens_in=tin, llm_tokens_out=tout, api_tokens_in=tin,
                 api_tokens_out=tout)
        c.gemini_usd = round((tin * pin + tout * pout) / 1e6, 6)  # extra field (Cost allows extras)
        cost_box[0] = cost_box[0].add(c)
        try:
            d = json.loads(r.text)
        except Exception:  # noqa: BLE001
            d = llm.extract_json(r.text) or {}
        return d if isinstance(d, dict) else {}

    # -- plan execution (local, no LLM) -----------------------------------------------------------------
    def _subst(self, inp: str, vals: dict) -> tuple[str, list[str]]:
        bad: list[str] = []

        def rep(m):
            v = vals.get(m.group(1), {}).get(m.group(2) or "value")
            if _num(v) is None:
                bad.append(m.group(0))
                return m.group(0)
            return _fmt(_num(v))

        return _REF.sub(rep, inp), bad

    def _execute(self, steps, path, vals, evidence, results, trace, cost_box, obs_lines) -> None:
        for st in steps[:MAX_PLAN]:
            sid = str(st.get("id") or f"s{len(vals) + 1}").strip().strip("{}")
            tool, raw = str(st.get("tool", "")), str(st.get("input", "")).strip()
            inp, bad = self._subst(raw, vals)
            if bad:
                obs = f"error: {', '.join(bad)} has no numeric value"
            elif tool not in ("ask", "table", "rows", "find_text", "look", "calc"):
                obs = f"error: unknown tool {tool}"
            else:
                n0 = len(results)
                box = [Cost()]
                obs = self._run(tool, inp, path, evidence, results, box)
                cost_box[0] = cost_box[0].add(box[0])
                new = results[n0:]
                if tool == "ask" and self._names and new and not any(
                        re.search(r"(?<![\w.\-])" + re.escape(n) + r"(?![\w.\-])", inp.lower()) for n in self._names):
                    # esc16 12:33: the sub-asks of 'On sheet A700 of …, how many types SS, TT and UU' dropped the sheet
                    # -> another table's counts (123 vs 126). Same rule as the router agent's scope drift
                    v, c, e, w = results[-1]
                    results[-1] = (v, round(c * SCOPE_PENALTY, 3), e, w)
                    obs += (" -- WARNING: this ask names none of " + ", ".join(f"'{n}'" for n in sorted(self._names))
                            + " from the question, so it may be about another scope; re-ask naming it")
                if tool == "rows" and len(new) >= 2:
                    vals[sid] = {"value": new[0][0], "count": new[0][0], "area": new[1][0]}
                elif new and tool != "table":
                    vals[sid] = {"value": new[-1][0]}
                else:
                    vals[sid] = {}
            line = f"{sid} {tool}({inp[:110]}) → {obs[:220]}"
            trace.append(line)
            obs_lines.append(f"{sid} {tool}({inp[:200]}) -> {obs[:1200]}")

    # -- main --------------------------------------------------------------------------------------------
    def solve(self, path: str, question: str, intent=None, base: Answer | None = None) -> Answer:
        t0 = time.perf_counter()
        src = source_of(path)
        cost_box = [Cost()]
        trace: list[str] = []
        evidence: list[Evidence] = []
        results: list = []
        vals: dict[str, dict] = {}
        obs_lines: list[str] = []
        assumptions: list[str] = []
        missing: list[str] = []
        from stoa.router.intent import parse

        _f = parse(question).filters
        self._names = {str(_f[k]).strip().lower() for k in ("storey", "model", "sheet", "apartment") if _f.get(k)}
        try:
            table = self._table(path)
        except Exception as e:  # noqa: BLE001
            table = f"no structured table ({type(e).__name__})"
        known = KNOWN.get(src, [])
        ctx = (f"FILE: {Path(path).name} (source {src}; sibling files: {', '.join(sorted(siblings(path))) or 'none'})\n"
               + ("KNOWN QUESTIONS (fill in <...>):\n- " + "\n- ".join(known) + "\n" if known else "")
               + f"TABLE:\n{table[:3500]}\n")
        if base is not None and base.value is not None:
            ctx += (f"OUR PIPELINE'S UNSURE ANSWER: {_fmt(base.value)} {base.unit or ''} (confidence "
                    f"{base.confidence:.2f}, by {base.solver}); its first steps: "
                    + " | ".join(str(t)[:140] for t in base.trace[:3])
                    + ("; missing: " + "; ".join(str(m)[:120] for m in base.missing[:2]) if base.missing else "")
                    + "\nCheck it or compute the answer another way.\n")
        ctx += f"QUESTION: {question}"
        img = None
        if self.image:
            try:
                img = thumbnail(path)
            except Exception:  # noqa: BLE001
                img = None
        msgs = [{"role": "system", "content": PLAN_SYS}, {"role": "user", "content": ctx}]
        try:
            plan = self._call(msgs, PLAN_SCHEMA, [img] if img is not None else None,
                              f"G1 general agent: plan for an unsure/unseen question ({src})", cost_box)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"{self.label} planner unavailable ({type(e).__name__}: {str(e)[:80]})", cost_box, t0)
        steps = plan.get("steps") or []
        assumptions += [str(a)[:160] for a in plan.get("assumptions") or [] if str(a).strip()]
        trace.append(f"{self.label} (planner) understood: {str(plan.get('understanding', ''))[:160]}; "
                     f"plan = {len(steps)} step(s): " + ", ".join(f"{s.get('id')} {s.get('tool')}" for s in steps[:MAX_PLAN]))
        self._execute(steps, path, vals, evidence, results, trace, cost_box, obs_lines)
        final = None
        for rnd in range(2):
            msgs = [{"role": "system", "content": ANSWER_SYS},
                    {"role": "user", "content": f"QUESTION: {question}\nFILE: {Path(path).name} (source {src})\n"
                                                f"PLAN UNDERSTANDING: {plan.get('understanding', '')}\nRESULTS:\n"
                                                + "\n".join(obs_lines)[:9000]
                                                + ("\nThis is the last round: more_steps must be []." if rnd else "")}]
            try:
                final = self._call(msgs, ANSWER_SCHEMA, None,
                                   f"G1 general agent: answer from tool results ({src})", cost_box)
            except Exception as e:  # noqa: BLE001
                trace.append(f"{self.label}: answer call failed ({type(e).__name__})")
                final = None
                break
            more = final.get("more_steps") or []
            if not more or rnd == 1:
                break
            trace.append(f"{self.label} asked for {len(more)} more step(s)")
            self._execute(more[:MAX_MORE], path, vals, evidence, results, trace, cost_box, obs_lines)
        if not final:
            return self._fail(f"{self.label} gave no answer", cost_box, t0, trace)
        return self._conclude(path, question, final, vals, results, evidence, trace, assumptions, missing,
                              cost_box, t0)

    def _fail(self, why: str, cost_box, t0, trace=None) -> Answer:
        c = cost_box[0]
        c.wall_seconds = round(time.perf_counter() - t0, 3)
        return Answer(value=None, confidence=0.0, trace=[*(trace or []), why], solver="gemini_agent",
                      missing=[why], cost=c)

    def _conclude(self, path, question, final, vals, results, evidence, trace, assumptions, missing, cost_box, t0):
        raw = str(final.get("answer", "")).strip()
        unit = str(final.get("unit") or "").strip() or None
        reasoning = str(final.get("reasoning", ""))[:300]
        assumptions += [str(a)[:160] for a in final.get("assumptions") or [] if str(a).strip()]
        missing += [str(m)[:160] for m in final.get("missing") or [] if str(m).strip()]
        parts = [p.strip() for p in raw.split(";")] if ";" in raw else [raw]
        refs = [_BARE_REF.fullmatch(p) for p in parts]
        trace.append(f"{self.label} (answer): {raw[:80]} {unit or ''} — {reasoning}".strip())
        if len(parts) > 1 and all(refs):  # several parts: each part is a referenced tool value
            vs = [vals.get(m.group(1), {}).get(m.group(2) or "value") for m in refs]
            if all(_num(v) is not None for v in vs):
                confs = []
                for v in vs:
                    cs = [c for vv, c, _, _ in results if c >= 0 and _num(vv) is not None
                          and abs(_num(vv) - _num(v)) <= 0.005 * max(abs(_num(v)), 1)]
                    confs.append(max(cs) if cs else 0.6)  # a calc of tool values: grounded, not separately rated
                for _, c, ev, w in results:
                    if c >= 0:
                        evidence += [e.model_copy(update={"note": f"{w[5:60]} — {e.note}" if e.note else w[5:60]})
                                     for e in ev[:40]]
                cost = cost_box[0]
                cost.wall_seconds = round(time.perf_counter() - t0, 3)
                value = " ; ".join(_fmt(_num(v)) for v in vs)
                conf = round(min(0.9, min(confs) * 0.95), 3)
                trace.insert(0, f"agent: {self.label} plan-and-execute ({len(vals)} tool steps, {cost.api_calls} "
                                f"Gemini calls)")
                return Answer(value=value, unit=unit, confidence=conf, evidence=evidence[:300], trace=trace,
                              solver="gemini_agent", assumptions=assumptions, missing=missing, cost=cost)
        val = raw
        if refs[0] and len(parts) == 1:
            m = refs[0]
            v = vals.get(m.group(1), {}).get(m.group(2) or "value")
            val = _fmt(_num(v)) if _num(v) is not None else (str(v) if v is not None else None)
        image_text = final.get("source") == "image" and val and _num(val) is None
        ans = self._finish(val, unit, results, evidence, trace, assumptions, missing, cost_box[0], t0, question)
        ans.solver = "gemini_agent"
        ans.trace[0] = (f"agent: {self.label} plan-and-execute ({len(vals)} tool steps, {ans.cost.api_calls} Gemini "
                        "calls)")
        if image_text:  # read off the thumbnail by Gemini itself: locate it in the text layer if we can
            hits: list[Evidence] = []
            try:
                obs = self._find_text(path, str(val)[:60], hits)
            except Exception:  # noqa: BLE001
                obs = "no text layer"
            ans.missing = [m for m in ans.missing if "not backed by a tool result" not in m]
            if hits:
                ans.confidence = READ_FOUND
                ans.evidence = hits + ans.evidence
                ans.trace.append(f"{self.label} read '{str(val)[:40]}' off the image; the text layer has it ({obs[:80]})")
                ans.assumptions.append(f"read by {self.label} from the drawing image; the PDF text layer confirms the words")
            else:
                ans.confidence = min(ans.confidence, READ_CAP) if ans.confidence > 0.25 else READ_CAP
                ans.assumptions.append(f"read by {self.label} from the drawing image (no tool reading confirms it)")
                ans.missing.append("no text layer / OCR hit confirms the text Gemini read; check it on the drawing")
        return ans


__all__ = ["GeminiAgent", "PLANNER", "thumbnail"]
