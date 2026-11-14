"""The sheets of an uploaded drawing SET (a multi-page PDF), each labelled for the page picker (captain 18:50: a project
of many sheets, auto-classified by floor and kind). Called by the Vite middleware (GET /api/sheets?path=):

    $JUNCTION_VENV/bin/python web/server/sheet_index.py /tmp/stoa-uploads/ab12/set.pdf

prints {"pages": [{"page", "number", "kind", "storey", "title"}], "n", "source"}. VP's stoa.sheet.sets.sheet_index when
this tree has it (kind, storey, sheet number, title per page), else P1's stoa.formats.pages (sheet number from the title
block, 'plan' / 'elev' from short title lines, the storey a plan title names). Never raises: {"pages": [], "error"}.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

MAX_PAGES = 120


_INDEX = re.compile(r"\b(index of drawings|sheet index|drawing index|list of drawings|drawing list|cover sheet|title sheet)\b", re.I)


def _title(text: str) -> str:
    """The sheet's own title: a short line that names a plan / elevation / section / schedule (not a notes line). A cover
    or index sheet lists every other sheet's title ('FIRST FLOOR PLAN T-SHAPE' on the DAF dorm cover): 'Cover sheet'."""
    if _INDEX.search(text):
        return "Cover sheet"
    for ln in text.splitlines():
        ln = ln.strip()
        if 6 <= len(ln) <= 60 and re.search(r"\b(plan|elevation|section|schedule|details?|layout|grundriss|ansicht|schnitt)\b", ln, re.I) \
                and not re.search(r"\b(see|refer|note|typ\.?|similar)\b", ln, re.I):
            return re.sub(r"\s+", " ", ln)
    return ""


_STOREY = re.compile(r"\b((?:lower |upper )?ground|first|second|third|fourth|fifth|sixth|basement|mezzanine|roof|"
                     r"\d{1,2}(?:st|nd|rd|th))\s+floor\b|\b(level|storey|story)\s+(\d{1,2}|[A-Z]\d?)\b", re.I)


def _storey_any(text: str) -> str:
    """A storey the page names anywhere ('Secondary Type 3 600 Second Floor' in a title block): the most frequent
    phrase, as printed in words ('Second floor', 'Level 2'); '' when none or a tie between different storeys."""
    seen: dict[str, int] = {}
    for m in _STOREY.finditer(text):
        k = (f"{m.group(1)} floor" if m.group(1) else f"{m.group(2)} {m.group(3)}").strip().lower()
        seen[k] = seen.get(k, 0) + 1
    if not seen:
        return ""
    best = sorted(seen.items(), key=lambda kv: -kv[1])
    if len(best) > 1 and best[0][1] == best[1][1]:
        return ""
    return best[0][0][:1].upper() + best[0][0][1:]


def index(path: str) -> dict:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.chdir(root)
    with contextlib.redirect_stdout(sys.stderr):
        try:  # VP (night-VP fb50c3d+): kind, storey, number, title per page
            from stoa.sheet.sets import sheet_index

            out = []
            for s in sheet_index([path], max_pages=MAX_PAGES):
                d = s.as_dict() if hasattr(s, "as_dict") else dict(vars(s))
                out.append({"page": int(d.get("page", len(out))), "number": d.get("number") or "", "kind": d.get("kind") or "",
                            "storey": d.get("storey_name") or d.get("storey") or "", "title": d.get("title") or "",
                            "building": d.get("building") or "", "partial": bool(d.get("partial"))})
            if out:
                return {"pages": out, "n": len(out), "source": "stoa.sheet.sets"}
        except Exception:  # noqa: BLE001 — absent on this tree or failed: P1's page reader below
            pass
        from stoa.formats.pages import _sheet_kind, page_scores, sheet_id, sheet_storey

        pages = page_scores(path)[:MAX_PAGES]
        out = []
        for pg in pages:
            k = int(pg["page"])
            text = pg.get("text") or ""
            kind = _sheet_kind(text) or ""
            try:
                num = sheet_id(path, k) or "" if len(pages) <= 60 else ""
            except Exception:  # noqa: BLE001
                num = ""
            storey = (sheet_storey(text) or "") if kind == "plan" else ""
            storey = storey or (_storey_any(text) if kind != "elev" and not _INDEX.search(text) else "")
            out.append({"page": k, "number": num, "kind": {"plan": "floor plan", "elev": "elevation / section"}.get(kind, ""),
                        "storey": storey, "title": _title(text)})
        return {"pages": out, "n": len(out), "source": "stoa.formats.pages"}


def main() -> None:
    try:
        print(json.dumps(index(sys.argv[1])))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"pages": [], "n": 0, "error": f"{type(e).__name__}: {e}"[:300]}))


if __name__ == "__main__":
    main()
