"""Erin's piping reader (stoa/solvers/piping.py, PR #14) on IMAGES of piping sheets (G1 c7; Stoa's own M-101A example is
a PNG: 13/25 of Erin's questions there vs 24/25 on the PDF copy).

Builds her page model (_Page / _Word / _Block) from the image's text layer that the other readers already read
(label_count._image_lines: an OCR layer cached by raster / W5's warm pre-read, or the PDF's own lines for our renders) and
reuses her tag-family COUNTS unchanged (sizes / notes: raster reads them right on images; her block ranking did not). Evidence is in image pixels. Not on images: tagged
spans at the printed scale (her points-per-foot is PDF-point based) and the Gemini connection step. Abstains when the
layer is not read yet or nothing matches, so the other image readers answer as before."""

from __future__ import annotations

import re

from stoa.contract import Answer
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

IMG = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")


def _page(img_abs: str):
    from stoa.solvers import piping as P
    from stoa.solvers.label_count import _image_lines

    lines = _image_lines(img_abs) or ()
    if len(lines) < 5:
        return None
    words, blocks = [], []
    order = 0
    for bi, (t, bb) in enumerate(lines):
        t = re.sub(r"\s+", " ", str(t)).strip()
        if not t:
            continue
        x0, y0, x1, y1 = (float(c) for c in bb)
        blocks.append(P._Block(text=t, bbox=(x0, y0, x1, y1)))
        toks = t.split(" ")
        total = max(1, sum(len(w) for w in toks) + len(toks) - 1)
        cx = x0
        for w in toks:  # word boxes interpolated along the line box by character count
            wx1 = cx + (x1 - x0) * len(w) / total
            words.append(P._Word(text=w, bbox=(round(cx, 1), y0, round(wx1, 1), y1), block=bi, line=0, order=order))
            order += 1
            cx = wx1 + (x1 - x0) / total
    text = "\n".join(b.text for b in blocks)
    return P._Page(number=0, text=text, words=tuple(words), blocks=P._augment_adjacent_blocks(tuple(blocks)))


def _image_space(ans: Answer) -> Answer:
    for e in ans.evidence:
        e.bbox_space = "image_px"
        e.page = 0
        e.exact = False
    ans.trace = [t.replace("selected PDF page 0", "the image's text layer") for t in ans.trace]
    ans.confidence = round(min(float(ans.confidence or 0), 0.8), 3)  # OCR'd labels: one misread changes a count
    return ans


@register("piping_img")
class PipingImage(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        from stoa.solvers.piping import PipingSolver, _SHEET_RE

        return str(path).lower().endswith(IMG) and bool(PipingSolver._CUES.search(question) or
                                                        _SHEET_RE.search(question))

    def solve(self, path: str, question: str) -> Answer:
        from stoa.solvers import piping as P

        if not self.can_handle(path, question):
            return self.abstain("not a piping question on an image")
        if re.search(r"\b(?:HOW\s+LONG|LENGTH|SPACING|SPAN|CONNECT|COME\s+FROM|GO\s+TO|WHERE\s+DOES|WHAT\s+DOES)\b",
                     question, re.I):
            return self.abstain("spans / connections are read on the PDF (printed-scale points / Gemini step)")
        try:
            page = _page(str(self.abspath(path)))
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"no text layer ({type(e).__name__})")
        if page is None:
            return self.abstain("the image's text layer is not read yet")
        if "NOTE" in question.upper() or re.search(r"\b(?:SIZE|DIAMETER)\b", question, re.I):
            # measured on X1's 25 PNG rows: OCR lines are not PDF text blocks, so her block ranking picked the wrong
            # callout / half a note (2 L: '12"' -> 2 inches, note 2) while raster reads sizes / notes right there
            return self.abstain("sizes / notes on images: raster's text reader answers them")
        target = P._count_target(question)
        if target is None or not re.search(r"\b(?:HOW\s+MANY|COUNT|NUMBER)\b", question, re.I):
            return self.abstain("unsupported piping question kind")
        family, distinct = target
        if family == "PS":
            matches = [("PS", w) for w in P._bare_ps_words(page)]
        else:
            matches = [(tag, w) for tag, w in P._word_tags(page) if tag == family or tag.startswith(family)]
        if distinct:
            uniq: dict = {}
            for tag, w in matches:
                uniq.setdefault(tag, w)
            matches = list(uniq.items())
        if not matches:
            return self.abstain(f"no {family} labels in the image's text layer")
        a = Answer(value=len(matches), unit="count", confidence=0.8,
                   evidence=[P._evidence(path, page, tag, w) for tag, w in matches],
                   trace=["the image's text layer (Erin's piping reader)", f"parsed {family} tags",
                          f"counted {'distinct IDs' if distinct else 'printed occurrences'}: {len(matches)}"],
                   solver="piping_img", assumptions=["counted only explicit printed labels read from the image"])
        return _image_space(a)
