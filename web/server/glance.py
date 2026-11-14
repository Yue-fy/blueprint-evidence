"""'This floor at a glance' (captain 21:14): the open sheet's quantities from F2's takeoff, summarised for the card that
replaces the old drawing-vs-model differences in the customer view. Called by the Vite middleware (GET /api/glance):

    $JUNCTION_VENV/bin/python web/server/glance.py /tmp/stoa-uploads/ab12/plan.png 0 upload/ab12/plan.png

prints {"ok": true, "scale", "scale_source", "rooms", "area_m2", "area_conf", "types": [{type, n, m2, rows}], "doors",
"windows", "skirting_m", "skirting_rooms", "rows": [...]} or {"ok": false, "why"}. It never runs a takeoff (F2's panel
does, ~2-4 min a sheet): it only reads a finished result.json, found by F2's own key (content hash + page + engine) in
$STOA_TAKEOFF_CACHE, the system temp folder and $JUNCTION_SHARED/artifacts/F2/cache. Never raises.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# room types an estimator groups by (UK school sheets, US dorms, flats); first match wins, in this order
TYPES: list[tuple[str, re.Pattern]] = [
    ("Toilets and changing", re.compile(r"\b(toilets?|wc|w\.c\.|changing|shower|hygiene|accessible|bath(room)?|washroom|restroom|lav)\b", re.I)),
    ("Circulation", re.compile(r"\b(corridor|lobby|stair\w*|circulation|entrance|foyer|vestibule|lift|hallway|passage|landing|walkway|korridor)\b", re.I)),
    ("Halls and sport", re.compile(r"\b(hall|gym\w*|sports?|fitness|studio|dance|drama|activity)\b", re.I)),
    ("Teaching", re.compile(r"\b(class\w*|teaching|music|art|science|lab\w*|lrc|library|mfl|english|maths?|ict|d\s*&?\s*t|food room|"
                            r"practical|sen|therapy|sensory|group room|resource|year|yr)\b", re.I)),
    ("Kitchen and dining", re.compile(r"\b(kitchen|servery|dining|laundry|cafe|canteen|k[üu]che|pantry)\b", re.I)),
    ("Staff and offices", re.compile(r"\b(office|staff|head|meeting|admin\w*|interview|medical|reception|conference|work ?room|"
                                      r"store ?keeper|deputy|bursar|sick|repro\w*|parent)\b", re.I)),
    ("Storage", re.compile(r"\b(stores?|storage|cupboard|cleaner\w*|janitor|closet|reduit|abstell\w*)\b", re.I)),
    ("Plant and services", re.compile(r"\b(plant|boiler|comms|server|electric\w*|riser|mechanical|switch|meter|tank|duct)\b", re.I)),
    ("Bedrooms and living", re.compile(r"\b(dorm\w*|bed(room)?s?|living|lounge|zimmer|wohn\w*|sleep\w*)\b", re.I)),
]


def room_type(name: str) -> str:
    for t, rx in TYPES:
        if rx.search(name or ""):
            return t
    return "Other rooms"


def _key(abs_path: str, page: int) -> str | None:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from stoa.features.takeoff import job_key
    except Exception:  # noqa: BLE001  (no takeoff module on this tree)
        return None
    return job_key(abs_path, page)


def _find(key: str) -> Path | None:
    dirs = [os.environ.get("STOA_TAKEOFF_CACHE", ""), str(Path(tempfile.gettempdir()) / "stoa-takeoff"),
            str(Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts/F2/cache")]
    for d in filter(None, dirs):
        p = Path(d) / key / "result.json"
        if p.is_file():
            return p
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def glance(abs_path: str, page: int = 0, virtual: str = "") -> dict:
    key = _key(abs_path, page)
    if not key:
        return {"ok": False, "why": "the takeoff module is not on this server"}
    p = _find(key)
    if not p:
        return {"ok": False, "why": "no takeoff of this sheet yet", "key": key}
    r = json.loads(p.read_text())
    if r.get("status") != "done":
        return {"ok": False, "why": f"takeoff {r.get('status', 'not finished')}", "key": key}
    rows = r.get("rows") or []
    for i, x in enumerate(rows):
        x["i"] = i
        for e in x.get("evidence") or []:  # the takeoff ran on the same bytes, maybe under another upload folder
            if virtual and e.get("file") and Path(str(e["file"])).name == Path(virtual).name:
                e["file"] = virtual
    ok = [x for x in rows if x.get("status") == "ok" and _num(x.get("value")) is not None]
    area = [x for x in ok if x.get("section") == "room" and x.get("quantity") == "area"]
    types: dict[str, dict] = {}
    for x in area:
        t = types.setdefault(room_type(str(x.get("item", ""))), {"n": 0, "m2": 0.0, "rows": []})
        t["n"] += 1
        t["m2"] += float(x["value"])
        t["rows"].append(x["i"])
    sheet = {x.get("quantity"): x for x in ok if x.get("section") == "sheet"}
    skirt = [x for x in ok if x.get("section") == "room" and x.get("quantity") == "baseboard"]
    scale_src = next((a for x in area[:5] for a in (x.get("assumptions") or []) if str(a).lower().startswith("scale:")), "")
    count = lambda q: ({"value": int(_num(sheet[q]["value"])), "conf": sheet[q].get("confidence"), "row": sheet[q]["i"],  # noqa: E731
                        "method": sheet[q].get("method")} if q in sheet else None)
    return {
        "ok": True, "key": key, "kind": r.get("kind"), "seconds": r.get("seconds"),
        "scale": f"1:{int(r['scale_n']) if float(r['scale_n']).is_integer() else r['scale_n']}" if _num(r.get("scale_n")) else "",
        "scale_source": re.sub(r"^scale:\s*", "", str(scale_src)),
        "rooms": len(area), "rooms_labelled": next((int(_num(x["value"])) for x in ok if x.get("quantity") == "rooms"), len(area)),
        "area_m2": round(sum(float(x["value"]) for x in area), 2),
        "types": sorted(({"type": k, "n": v["n"], "m2": round(v["m2"], 2), "rows": v["rows"]} for k, v in types.items()),
                        key=lambda t: -t["m2"]),
        "doors": count("doors"), "windows": count("windows"),
        "skirting_m": round(sum(float(x["value"]) for x in skirt), 2), "skirting_rooms": len(skirt),
        "unread": sum(1 for x in rows if x.get("section") == "room" and x.get("status") != "ok"),
        "rows": rows,
    }


def main() -> None:
    try:
        out = glance(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0, sys.argv[3] if len(sys.argv) > 3 else "")
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
