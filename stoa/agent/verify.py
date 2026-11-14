"""Nemotron as the visible verifier of unsure answers (G1; NVIDIA Nemotron prize).

Hosted `nvidia/nemotron-3.5-super-vl-120b-a12b` gets what a human checker would get: the question, our answer, the
first lines of the reasoning, the drawing with the evidence boxes drawn on it (numbered, red) and a zoomed crop of
them. It replies agree / disagree / unsure with a one-line reason. The verdict moves the CONFIDENCE only, never the
value (router rule 3: a new reader checks, it does not overrule); the shifts are measured, see experiments/g1/.

    from stoa.agent.verify import verify, apply_verdict
    v = verify(path, question, ans)      # {"verdict", "reason", "value_seen", "cost": Cost, "images": n}
    ans = apply_verdict(ans, v)
"""

from __future__ import annotations

import json
import re
import os
import time

from stoa.contract import Answer, Cost
from stoa.data import abspath

MODEL = os.environ.get("STOA_VERIFIER_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
LABEL = "Nemotron 3.5 Super VL"
# confidence shifts, measured with experiments/g1/verify_eval.py (12:40, 40 balanced rows with conf < 0.8 from the
# final-night dev/hard/red-team runs, artifacts/G1/verify_v40.jsonl): WITH evidence boxes Nemotron mostly agrees with
# the boxes (agree 12 right / 8 wrong; disagree 4 right / 1 wrong) -> no signal, no shift. WITHOUT boxes its
# disagreement means something (7 of 8 wrong) -> x0.5. Agreement never raises confidence (P(right | agree) 0.57 vs
# base 0.50). AUROC 0.651 -> 0.670 (in-sample, small: treat as 'does no harm', not as a gain)
AGREE_UP = 0.0  # conf += AGREE_UP * (AGREE_MAX - conf)
AGREE_MAX = 0.8
DISAGREE_MULT = 0.5  # answers with no located evidence
DISAGREE_MULT_BOXED = 1.0  # answers with boxes: shown, not applied
PAGE_PIXELS = 1_600_000
CROP_PIXELS = 1_000_000
MAX_BOXES = 40

PROMPT = """You check an answer about a construction drawing, as a careful human reviewer would.
QUESTION: {question}
PROPOSED ANSWER: {answer}
HOW IT WAS FOUND: {how}
Image 1 is the drawing{boxes1}.{img2}
Does the drawing support the proposed answer? Judge from what you can see; if the answer is a count, count the marked
items and check whether unmarked ones were missed. If you cannot tell from the image, say "unsure".
Reply with JSON only: {{"verdict": "agree" | "disagree" | "unsure", "value_seen": "<your own reading, or empty>",
"reason": "<at most 25 words>"}}"""


def _page_image(file: str, page: int | None):
    """(PIL image, scale from evidence units to image px, evidence bbox space it expects)."""
    from PIL import Image

    from stoa import llm

    a = abspath(file)
    if a.suffix.lower() == ".pdf":
        img, zoom = llm.render_pdf_page(a, page or 0, dpi=150, max_pixels=PAGE_PIXELS)
        return img, zoom, "pdf_pt"
    img = Image.open(a).convert("RGB")
    s = min(1.0, (PAGE_PIXELS / max(1, img.width * img.height)) ** 0.5)
    if s < 1:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
    return img, s, "image_px"


def evidence_images(path: str, ans: Answer):
    """[page with numbered boxes, zoomed crop of the boxes] (crop only when the boxes cover < 40% of the page)."""
    from PIL import ImageDraw

    boxed = [e for e in ans.evidence if e.bbox and e.file and not str(e.file).lower().endswith(".ifc")]
    if boxed:
        f0, p0 = boxed[0].file, boxed[0].page
        boxed = [e for e in boxed if e.file == f0 and e.page == p0][:MAX_BOXES]
        img, s, space = _page_image(f0, p0)
    else:
        from stoa.agent.gemini import thumbnail

        img = thumbnail(path)
        return ([img] if img is not None else []), 0
    draw = ImageDraw.Draw(img)
    rects = []
    for i, e in enumerate(boxed, 1):
        x0, y0, x1, y1 = (v * s for v in e.bbox)
        if x1 - x0 < 6:
            x0, x1 = x0 - 6, x1 + 6
        if y1 - y0 < 6:
            y0, y1 = y0 - 6, y1 + 6
        rects.append((x0, y0, x1, y1))
        draw.rectangle([x0, y0, x1, y1], outline=(230, 0, 0), width=3)
        if len(boxed) <= 25:
            draw.text((x0 + 2, max(0, y0 - 12)), str(i), fill=(230, 0, 0))
    out = [img]
    X0, Y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    X1, Y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    if (X1 - X0) * (Y1 - Y0) < 0.4 * img.width * img.height:
        pad = 0.15 * max(X1 - X0, Y1 - Y0) + 30
        crop = img.crop((max(0, X0 - pad), max(0, Y0 - pad), min(img.width, X1 + pad), min(img.height, Y1 + pad)))
        k = (CROP_PIXELS / max(1, crop.width * crop.height)) ** 0.5
        if k > 1:
            crop = crop.resize((int(crop.width * min(k, 4)), int(crop.height * min(k, 4))))
        out.append(crop)
    return out, len(boxed)


def verify(path: str, question: str, ans: Answer, model: str = MODEL) -> dict:
    from stoa import llm

    t0 = time.perf_counter()
    try:
        imgs, nbox = evidence_images(path, ans)
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unsure", "reason": f"no image ({type(e).__name__})", "value_seen": "", "cost": Cost()}
    if not imgs:
        return {"verdict": "unsure", "reason": "no drawing image to check", "value_seen": "", "cost": Cost()}
    from stoa.agent.private import hosted_allowed

    if not hosted_allowed("nvidia"):
        return {"verdict": "unsure", "reason": "private mode: no Nemotron check (the drawing stays on this server)",
                "value_seen": "", "cost": Cost()}
    how = " | ".join(str(t)[:160] for t in ans.trace[:4])
    prompt = PROMPT.format(
        question=question, answer=f"{ans.value} {ans.unit or ''}".strip(), how=how[:700],
        boxes1=(f" with the answer's {nbox} evidence item(s) marked by numbered red boxes" if nbox else
                " (the answer has no marked evidence)"),
        img2=(" Image 2 is a zoomed crop of the marked region." if len(imgs) > 1 else ""))
    try:
        r = llm.chat(prompt, images=imgs, backend="nvidia", model=model, max_tokens=900, temperature=0.0,
                     thinking=False, cache=True, max_pixels=PAGE_PIXELS, max_side=4096)
    except Exception as e:  # noqa: BLE001
        if "STOA_LLM_REPLAY" in str(e):  # E1 20:58: gate runs replay the disk cache; a miss is not an outage
            return {"verdict": "unsure", "reason": f"{LABEL} not called (replay run: no cached response, no new "
                                                   f"hosted call)", "value_seen": "", "cost": Cost()}
        return {"verdict": "unsure", "reason": f"{LABEL} unavailable ({type(e).__name__}: {str(e)[:80]})",
                "value_seen": "", "cost": Cost()}
    tin, tout = r.prompt_tokens or 0, r.completion_tokens or 0
    cost = Cost(llm_calls=1, api_calls=1, llm_tokens_in=tin, llm_tokens_out=tout, api_tokens_in=tin,
                api_tokens_out=tout, wall_seconds=round(time.perf_counter() - t0, 3))
    try:
        d = json.loads(r.text)
    except Exception:  # noqa: BLE001
        d = llm.extract_json(r.text) or {}
    d = d if isinstance(d, dict) else {}
    verdict = str(d.get("verdict", "unsure")).strip().lower()
    if verdict not in ("agree", "disagree", "unsure"):
        verdict = "unsure"
    return {"verdict": verdict, "reason": str(d.get("reason", ""))[:200], "value_seen": str(d.get("value_seen", ""))[:60],
            "cost": cost, "images": len(imgs), "boxes": nbox}


_NOT_THE_QUANTITY = re.compile(r"\b(?:is an? (?:dimension|grid|level|elevation|height|label|tag|room number|sheet number|"
                               r"scale)|(?:dimension|grid|elevation|level) (?:label|line|string|value|tag)|not an? "
                               r"(?:area|length|count|room area|perimeter))\b", re.I)

def apply_verdict(ans: Answer, v: dict) -> Answer:
    """Move the confidence by the verdict (never the value); add the verdict to the trace (and missing if it disagrees)."""
    c0 = ans.confidence
    if v["verdict"] == "agree":
        c = c0 + AGREE_UP * max(0.0, AGREE_MAX - c0)
    elif v["verdict"] == "disagree":
        c = c0 * (DISAGREE_MULT_BOXED if v.get("boxes") else DISAGREE_MULT)
    else:
        c = c0
    seen = f" (it reads {v['value_seen']})" if v.get("value_seen") and v["verdict"] == "disagree" else ""
    word = {"agree": "agrees", "disagree": "disagrees", "unsure": "is unsure"}[v["verdict"]]
    reason = " ".join(str(v.get("reason") or "").split()) or "it gave no reason"  # QA 20:00 (2): 'because  →'
    ans.trace = [*ans.trace, f"Second opinion (NVIDIA {LABEL}): {word}{seen} because {reason} → confidence "
                             f"{c0:.2f} → {c:.2f}"]
    if v["verdict"] == "disagree":
        ans.missing = [*ans.missing, f"{LABEL} looked at the evidence and disagrees{seen}: {reason}"]
        if c0 < 0.5 and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool) and \
                _NOT_THE_QUANTITY.search(reason):
            # QA 20:00 (1): '≈ 7200 m²' stayed on the card although Nemotron said '7200 is a dimension label, not an
            # area': a weak reading the checker identifies as another kind of number is withheld, not displayed
            ans.missing = [f"withheld: {ans.value} {ans.unit or ''} is, per {LABEL}, not the asked quantity "
                           f"({reason})".replace("  ", " "), *ans.missing]
            ans.trace = [*ans.trace, f"{ans.value} {ans.unit or ''} withheld (a weak reading the checker identifies "
                                     f"as a dimension / level / label)"]
            ans.value = None
    ans.confidence = round(c, 3)
    ans.cost = (ans.cost or Cost()).add(v.get("cost") or Cost())
    return ans


__all__ = ["verify", "apply_verdict", "evidence_images"]
