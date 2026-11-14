"""`pipe_runs`: 'What does the 16" line connect to?' / 'Where does the 6" pipe go?' on a piping sheet = the printed RUN
CALLOUTS of that pipe size (G1 cycle 5; captain 16:15, Stoa's M-101A).

Piping plans label each run with its size and where it goes or comes from, often over two or three stacked lines:
'16" SS FROM' / 'OPERATING TANKS', '12" TO HYDRANT LOOP AND PIG' / 'LAUNCH TRAP (SEE DETAIL C1' / 'ON SHEET MS513)',
'16" SUCTION HEADER'. The reader joins each callout with the lines stacked under it, splits it into TO / FROM / the
run's name, and answers with every callout of the asked size (narrowed by other words of the question: 'the 12"
discharge header'), each boxed. Deterministic, $0. It reads text only: the drawn line itself is not traced, so the
components along it are not verified (said in `missing`).
"""

from __future__ import annotations

import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

_CONNECT_Q = re.compile(r"\b(connect(?:s|ed|ing)?|go(?:es)?|going|run(?:s)?|lead(?:s)?|feed(?:s)?|suppl(?:y|ies)|"
                        r"come(?:s)? from|coming from|originat\w*|serve(?:s)?|end(?:s)?|terminat\w*|where\b|attached|"
                        r"joins?|tie(?:s)? in)", re.I)
_PIPE_Q = re.compile(r"\b(line|pipe|piping|main|header|run|branch)s?\b", re.I)
_SIZE_Q = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:\"|”|''|-?\s?inch(?:es)?\b|in\.)", re.I)
_CALLOUT = re.compile(r'^\s*(\d{1,2}(?:\.\d)?)\s*(?:"|”|\'\')\s*(?:SS\b|CS\b|STL\b|DIA\.?\b)?\s*(.*)$', re.I)
_SEE = re.compile(r"\(?\s*,?\s*\bSEE\b.*$", re.I)
_STOP = {"the", "a", "an", "what", "does", "do", "is", "line", "pipe", "piping", "connect", "connects", "connected",
         "to", "go", "goes", "where", "from", "come", "comes", "run", "runs", "lead", "leads", "feed", "feeds", "on",
         "this", "sheet", "plan", "inch", "in", "of", "which", "with"}


def _assemble(lines) -> list[dict]:
    """Size callouts with the lines stacked under them: [{'size', 'text', 'box', 'parts'}]."""
    out = []
    for t, bb, d in lines:
        m = _CALLOUT.match(t)
        if not m or not re.search(r"[A-Za-z]{2,}", m.group(2) or ""):
            continue
        parts, box, cur = [t], list(bb), d
        for _ in range(3):
            if re.search(r"\bSHEET\b|\)\s*$", parts[-1], re.I):
                break  # 'DETAIL E1, SHEET M-504' / '… MS513)' ends the callout
            h = max(1.0, cur[3] - cur[1])
            nxt = [(t2, b2, d2) for t2, b2, d2 in lines if 0 <= d2[1] - cur[3] <= 0.9 * h and abs(d2[0] - cur[0]) < 3 * h
                   and not _CALLOUT.match(t2)]
            if not nxt:
                break
            t2, b2, d2 = min(nxt, key=lambda x: x[2][1])
            parts.append(t2)
            box = [min(box[0], b2[0]), min(box[1], b2[1]), max(box[2], b2[2]), max(box[3], b2[3])]
            cur = d2
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        out.append({"size": float(m.group(1)), "text": text, "box": box, "body": _CALLOUT.match(text).group(2)})
    return out


def _describe(c: dict) -> tuple[str, str]:
    """-> (kind 'to' | 'from' | 'name', phrase) of one callout; references ('SEE DETAIL C1 …') go to the phrase end."""
    body = c["body"].strip()
    see = _SEE.search(body)
    ref = ""
    if see:
        ref = see.group(0).strip(" ,()")
        body = body[:see.start()].strip(" ,(")
    m = re.match(r"^(TO|FROM)\b\s*(.+)$", body, re.I)
    if m:
        kind, phrase = m.group(1).lower(), m.group(2).strip(" ,")
    else:
        kind, phrase = "name", body
    return kind, phrase + (f" ({ref.lower()})" if ref else "")


@register("pipe_runs")
class PipeRuns(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"))

    def solve(self, path: str, question: str) -> Answer:
        from stoa.data import abspath
        from stoa.solvers.label_count import _IMG, _display_lines

        ms = _SIZE_Q.search(question)
        if not ms or not _CONNECT_Q.search(question) or not _PIPE_Q.search(question) or \
                re.search(r"\b(how many|how long|length|size|diameter)\b", question, re.I):
            return self.abstain("not a 'what does the N\" line connect to' question")
        size = float(ms.group(1))
        p = str(abspath(path))
        img = p.lower().endswith(_IMG)
        try:
            lines = _display_lines(p, img)
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the text ({type(e).__name__})")
        if not lines:
            return self.abstain("no text layer read yet")
        calls = [c for c in _assemble(lines) if abs(c["size"] - size) < 1e-6]
        if not calls:
            return self.abstain(f'no printed {size:g}" run callout on the sheet')
        qw = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in _STOP}
        named = [c for c in calls if qw and qw & set(re.findall(r"[a-z]{3,}", c["text"].lower()))]
        if named:
            calls = named
        seen, items = set(), []
        for c in calls:
            kind, phrase = _describe(c)
            key = (kind, phrase.lower())
            if key in seen or not phrase:
                continue
            seen.add(key)
            items.append((kind, phrase, c))
        order = {"from": 0, "to": 1, "name": 2}
        items.sort(key=lambda x: order[x[0]])
        words = {"from": "from", "to": "to", "name": "run named"}
        value = "; ".join(f'{words[k]} {ph}' for k, ph, _ in items)
        ends = [x for x in items if x[0] in ("to", "from")]
        conf = 0.65 if ends and len(items) <= 3 else 0.5 if ends else 0.4
        if img:
            conf -= 0.1
        space = "image_px" if img else "pdf_pt"
        ev = [Evidence(file=path, page=0, bbox=c["box"], bbox_space=space, exact=True,
                       note=f'callout "{c["text"][:70]}"') for _, _, c in items[:12]]
        return Answer(value=f'{size:g}" line: {value}', unit=None, confidence=round(conf, 2), evidence=ev,
                      solver=self.name,
                      trace=[f'pipe_runs: {len(items)} printed {size:g}" run callout(s): '
                             + " | ".join(c["text"] for _, _, c in items[:6])],
                      assumptions=[f'Convention: the run callouts printed with the size {size:g}" name where the line '
                                   "goes / comes from (TO / FROM) and what the run is"],
                      missing=["the drawn line was not traced: components along it (valves, pumps) are not verified"])
