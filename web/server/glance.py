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
# names are matched lower case with accents removed (KÖÖK -> kook, SÕLM -> solm); English, German, Estonian (Maleva),
# Finnish words follow W7's 3D legend (look3d.ts ROOM_WORDS), so a room has the same type on the card and in 3D
TYPES: list[tuple[str, re.Pattern]] = [
    ("Wet rooms", re.compile(r"\b(toilets?|wc|w\.c\.|changing|shower\w*|hygiene|accessible|bath(room)?|washroom|restroom|lav|"
                                        r"bad|badezimmer|dusche\w*|sanitar\w*|tualett\w*|pesukoda|pesuruum|vannituba|saun\w*|"
                                        r"leiliruum|kylpyhuone\w*|pesuhuone\w*|laundry)\b", re.I)),
    ("Circulation", re.compile(r"\b(corridor|lobby|stair\w*|circulation|entrance|foyer|vestibule|lift|hallway|passage|landing|walkway|"
                               r"korr?idor\w*|flur|diele|vorplatz|eingang\w*|treppenhaus|aufzug|esik|eeskoda|fuajee\w*|trepp\w*|"
                               r"trepikoda|porras\w*|kaytava\w*|eteinen|tuulikaappi)\b", re.I)),
    ("Halls and sport", re.compile(r"\b(hall|gym\w*|sports?|fitness|studio|dance|drama|activity)\b", re.I)),
    ("Teaching", re.compile(r"\b(class\w*|teaching|music|art|science|lab\w*|lrc|library|mfl|english|maths?|ict|d\s*&?\s*t|food room|"
                            r"practical|sen|therapy|sensory|group room|resource|year|yr)\b", re.I)),
    ("Kitchen and dining", re.compile(r"\b(kitchen|servery|dining|cafe|canteen|kuche|kueche|pantry|kook|teekook|keittio\w*)\b", re.I)),
    ("Staff and offices", re.compile(r"\b(office|staff|head|meeting|admin\w*|interview|medical|reception|conference|work ?room|"
                                      r"store ?keeper|deputy|bursar|sick|repro\w*|parent|buro|personali?\w*|kabinet\w*|"
                                      r"valveruum|hooldustootaja\w*|tootaja\w*)\b", re.I)),
    # one group like W7's 3D legend 'Storage · technical' (same colour on the plan, the card and the 3D floors)
    ("Storage and plant", re.compile(r"\b(stores?|storage|cupboard|cleaner\w*|janitor|closet|reduit|abstell\w*|plant|boiler|comms|"
                                     r"server|electric\w*|riser|mechanical|switch|meter|tank|duct|keller\w*|technik\w*|"
                                     r"abiruum|panipaik\w*|sahver|tehno\w*|tehniline\w*|soojussolm|veem\.?\s?solm|el\.?\s?kilp|"
                                     r"elektri\w*|ventilatsioon\w*|koristus\w*|liikumisvahendid|varasto\w*)\b", re.I)),
    ("Bedrooms and living", re.compile(r"\b(dorm\w*|bed(room)?s?|living|lounge|zimmer|wohn\w*|schlaf\w*|sleep\w*|elutuba|magamis\w*|"
                                       r"olohuone\w*|makuuhuone\w*|uhistegevuse)\b", re.I)),
]


def _fold(name: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", name or "") if not unicodedata.combining(c)).lower()


def room_type(name: str) -> str:
    """The first part of a combined name that we know decides ('ELUTUBA+KÖÖK' -> living, like W7's 3D legend)."""
    for part in re.split(r"[+&,]|\s/\s", _fold(name)):
        for t, rx in TYPES:
            if rx.search(part):
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


REVIEW_BELOW = 0.4  # a room reading under this confidence goes to the review list, outside the totals (estimators, 21:13 (8))


# labels that are not rooms (a wall, a canopy, the site): never in a room total (DfE L00 'Enclosed Wall' traced as 119 m²)
# F2 22:45: DfE L00's 'Enclosed Wall' is the OCR of 'Enclosed / Walkway' (a real enclosed corridor, 119 m²): only a wall's
# OWN label is not a room (bare 'Wall', 'retaining / party / curtain … wall'), never an enclosed walkway
_NOT_ROOM_WORDS = re.compile(r"\b(canopy|external|outline|boundary|site|void|fence|paving|"
                             r"planting|landscap\w*|car ?park|parking|court(yard)?|play ?area)\b", re.I)
_WALL_ONLY = re.compile(r"^\s*(?:(?:retaining|existing|party|curtain|boundary|external|internal|new|partition|fire|cavity|"
                        r"screen|garden|low|glass|glazed)\s+)?walls?\s*$", re.I)


class _NotRoom:
    """NOT_ROOM.search(label): a label that is not a room (a wall's own label, a canopy, the site …)."""

    @staticmethod
    def search(label: str):
        t = str(label or "")
        if re.search(r"enclosed|walkway", t, re.I):
            return None
        return _WALL_ONLY.search(t) or _NOT_ROOM_WORDS.search(t)


NOT_ROOM = _NotRoom()


def _weak(x: dict) -> bool:
    """A typical-value guess (prior) or a near-zero confidence is not a reading of the drawing: never in a total."""
    return "prior" in str(x.get("method") or "") or (_num(x.get("confidence")) or 0.0) < 0.15


def _doors(x: dict) -> dict:
    """Door SETS vs leaves (estimators: 'unclear scope (sets / leaves)'): the reader's convention line, and the leaves
    when every door has its evidence box ('double door (2 leaves) 3')."""
    a = " ".join(str(t) for t in x.get("assumptions") or [])
    ev = x.get("evidence") or []
    out = {"sets": bool(re.search(r"double door .{0,60}counts? as ONE door", a, re.I))}
    if ev and len(ev) == int(_num(x.get("value")) or -1):
        out["leaves"] = sum(2 if re.search(r"double|2 leaves|pair", str(e.get("note") or ""), re.I) else 1 for e in ev)
    m = str(x.get("method") or "")
    out["source"] = "the linked 3D model" if "ifc" in m else "the drawing"
    return out


def sheet_label(abs_path: str, page: int) -> str:
    """'A-101 GROUND FLOOR PLAN' from the title block (VP's stoa.sheet.sets; PDFs only, ~0.1 s); '' when unknown."""
    if not abs_path.lower().endswith(".pdf"):
        return ""
    try:
        from stoa.sheet.sets import classify
        s = classify(abs_path, page)
        num = s.number if s.number and not re.fullmatch(r"A[0-4]|B[0-4]", s.number) else None  # a paper size, not a sheet
        return " ".join(x for x in (num, s.title) if x)
    except Exception:  # noqa: BLE001
        return ""


# F2's room types (stoa.features.takeoff ROOM_TYPES, t2) -> this card's groups (W7's 3D tints; plant joins storage like
# W7's 'Storage · technical'), so the card, the plan and the 3D floors share one palette
F2_GROUP = {"wet": "Wet rooms", "circulation": "Circulation", "kitchen": "Kitchen and dining", "teaching": "Teaching",
            "hall": "Halls and sport", "staff": "Staff and offices", "storage": "Storage and plant", "plant": "Storage and plant",
            "sleep": "Bedrooms and living", "living": "Bedrooms and living", "other": "Other rooms"}


def _f2():
    """F2's takeoff module when it has sheet_summary (night-F2 96a2ae3+, engine t2), else None."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from stoa.features import takeoff as T
    except Exception:  # noqa: BLE001
        return None
    return T if hasattr(T, "sheet_summary") and hasattr(T, "status") else None


def _visitor(fn) -> dict:
    """F2 23:52: takeoff fixes are per visitor (T.status / T.sheet_summary take visitor=<browser id>); the middleware passes
    the id as STOA_GLANCE_VISITOR. Only when F2's function takes it (older takeoff modules: no argument, shared fixes)."""
    v = os.environ.get("STOA_GLANCE_VISITOR", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v):
        return {}
    try:
        import inspect

        return {"visitor": v} if "visitor" in inspect.signature(fn).parameters else {}
    except (TypeError, ValueError):
        return {}


def glance_f2(T, abs_path: str, page: int, virtual: str) -> dict | None:
    """The card from F2's own summary (F2 21:51: totals exclude the review rows, door sets / leaves, sheet + revision,
    skirting with / without wet rooms); rows = F2's finished rows (status()), so a line still shows its outlines.
    None when F2 has no finished t2 takeoff of this sheet (the caller then tries the older result files)."""
    s = T.sheet_summary(virtual or abs_path, page, **_visitor(T.sheet_summary))
    if s.get("status") != "done":
        return None
    st = T.status(s["job"], s.get("file") or virtual, **_visitor(T.status))
    rows = [dict(r) for r in st.get("rows") or []]
    idx = {}
    for i, x in enumerate(rows):
        x["i"] = i
        if x.get("id"):
            idx[x["id"]] = i
        for e in x.get("evidence") or []:
            if virtual and e.get("file") and Path(str(e["file"])).name == Path(virtual).name:
                e["file"] = virtual
        if x.get("review") and x.get("status") == "ok":
            x["status"] = "review"
    unit = s.get("unit") or "m2"
    groups: dict[str, dict] = {}
    # F2 00:11 / UX M5: the card says what the takeoff panel says — F2's own rooms and area per type (review rooms
    # counted as the panel counts them), the outlines of the type for 'show on the plan'. Older summaries (no per-type
    # 'rooms' / 'area'): recount from the outlines as before.
    own = all(isinstance(t.get("rooms"), int) and _num(t.get("area")) is not None for t in s.get("by_type") or []) and bool(s.get("by_type"))
    for t in (s.get("by_type") or []) if own else []:
        g = F2_GROUP.get(str(t.get("type")), str(t.get("label") or "Other rooms"))
        G = groups.setdefault(g, {"n": 0, "m2": 0.0, "rows": []})
        G["n"] += int(t["rooms"])
        G["m2"] += _num(t["area"]) or 0.0
        G["rows"] += [idx[o["id"]] for o in t.get("outlines") or [] if o.get("id") in idx]
    for t in [] if own else s.get("by_type") or []:
        g = F2_GROUP.get(str(t.get("type")), "Other rooms")
        for o in t.get("outlines") or []:
            i = idx.get(o.get("id"))
            v = _num(o.get("value"))
            if i is None or v is None or o.get("review") or (o.get("unit") or unit) != unit or NOT_ROOM.search(str(o.get("item") or "")):
                continue
            G = groups.setdefault(g, {"n": 0, "m2": 0.0, "rows": []})
            G["n"] += 1
            G["m2"] += v
            G["rows"].append(i)
    sheet_row = lambda q: next((x["i"] for x in rows if x.get("section") == "sheet" and x.get("quantity") == q), None)  # noqa: E731

    def count(q: str):
        n = s.get(q)
        if not n or n.get("estimate") or _num(n.get("value")) is None or sheet_row(q) is None:
            return None
        out = {"value": int(_num(n["value"])), "conf": n.get("confidence"), "row": sheet_row(q),
               "method": str(rows[sheet_row(q)].get("method") or "")}
        if q == "doors":
            out.update({"sets": True, "leaves": n.get("leaves") if isinstance(n.get("leaves"), int) else None,
                        "source": "the linked 3D model" if "ifc" in out["method"] else "the drawing"})
        return out
    sk = s.get("skirting") or {}
    rooms_m = sum(G["n"] for G in groups.values())
    area = round(sum(G["m2"] for G in groups.values()), 2)
    printed_total = None
    if not groups and s.get("area") and _num((s["area"] or {}).get("value")) is not None and not s["area"].get("estimate"):
        printed_total = {"value": _num(s["area"]["value"]), "unit": s["area"].get("unit") or unit, "rule": s["area"].get("rule")}
    # F2 22:29: a printed area table grouped by space type (AFCEC A-2: dormitory 18,472 / circulation 4,470 / … ft²) and
    # whether the types add up to the sheet's printed TOTAL — Stoa's 'areas by type' (shown when no rooms were measured)
    printed_types, printed_check = [], None
    pbt = s.get("printed_by_type") if isinstance(s.get("printed_by_type"), dict) else None
    for t in (pbt or {}).get("types") or []:
        v = _num(t.get("value"))
        if v is None:
            continue
        name = str(t.get("type") or "other").strip()
        printed_types.append({"type": name[:1].upper() + name[1:], "value": v, "unit": t.get("unit") or unit,
                              "rows": [idx[r] for r in t.get("rows") or [] if r in idx]})
    ck = (pbt or {}).get("check") or None
    if isinstance(ck, dict) and _num(ck.get("printed_value")) is not None:
        printed_check = {"label": str(ck.get("printed") or "the printed total"), "value": _num(ck.get("printed_value")),
                         "sum": _num(ck.get("sum")), "agree": bool(ck.get("agree")), "diff_pct": _num(ck.get("diff_pct"))}
    sc = s.get("scale") or {}
    review = []
    for r in s.get("review") or []:
        i = idx.get(r.get("id"))
        if i is None:
            continue
        q = "area" if "area" in str(r.get("quantity") or "").lower() else "baseboard" if re.search(r"skirt|base", str(r.get("quantity") or ""), re.I) else str(r.get("quantity") or "")
        if q in ("area", "baseboard") and _num(r.get("value")) is not None:  # a typical-value count is not a reading to review
            review.append({"row": i, "item": r.get("item"), "quantity": q, "value": r.get("value"), "unit": r.get("unit"), "conf": r.get("confidence"), "reason": r.get("reason")})
    return {
        "ok": True, "engine": "f2-summary", "key": s.get("job"), "kind": s.get("kind"), "seconds": s.get("seconds"), "unit": unit,
        "scale": sc.get("text") or "", "scale_source": sc.get("source") or "",
        "rooms": rooms_m, "rooms_labelled": (s.get("rooms") or {}).get("labelled", rooms_m), "area_m2": area,
        "printed_total": printed_total, "printed_types": printed_types[:8], "printed_check": printed_check,
        "types": sorted(({"type": k, "n": v["n"], "m2": round(v["m2"], 2), "rows": v["rows"]} for k, v in groups.items()), key=lambda t: -t["m2"]),
        "doors": count("doors"), "windows": count("windows"),
        "skirting_m": round(_num(sk.get("value")) or 0.0, 2) if not sk.get("estimate") else 0.0,
        "skirting_rooms": max(0, int(sk.get("rooms") or 0) - int(sk.get("review") or 0)),
        "skirting_dry_m": _num(sk.get("without_wet")), "wet_rooms": sk.get("wet_rooms"),
        "unread": max(0, int((s.get("rooms") or {}).get("labelled", 0)) - int((s.get("rooms") or {}).get("measured", 0))
                      - int((s.get("rooms") or {}).get("review", 0))),
        "review": review, "sheet": " ".join(x for x in (s.get("sheet"), f"rev {s['revision']}" if s.get("revision") else "") if x),
        "rows": rows,
    }


def glance(abs_path: str, page: int = 0, virtual: str = "") -> dict:
    T = _f2()
    if T is not None:
        try:
            g = glance_f2(T, abs_path, page, virtual)
            if g is not None:
                return g
        except Exception as e:  # noqa: BLE001  (F2's summary changed shape: the older reading below)
            print(f"glance: F2 summary failed: {type(e).__name__}: {e}", file=sys.stderr)
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
    ok = [x for x in rows if x.get("status") == "ok" and _num(x.get("value")) is not None and not _weak(x)
          and not (x.get("section") == "room" and NOT_ROOM.search(str(x.get("item") or "")))]
    review = [x for x in ok if x.get("section") == "room" and (_num(x.get("confidence")) or 0.0) < REVIEW_BELOW]
    rv = {x["i"] for x in review}
    ok = [x for x in ok if x["i"] not in rv]
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
                        "method": sheet[q].get("method"), **(_doors(sheet[q]) if q == "doors" else {})} if q in sheet else None)
    if not area and not sheet:
        return {"ok": False, "why": "the takeoff read no rooms or counts on this sheet", "key": key}
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
        "unread": len({str(x.get("item")) + str(x.get("copy") or "") for x in rows if x.get("section") == "room"
                       and x.get("quantity") == "area" and (x.get("status") != "ok" or _weak(x))
                       and not NOT_ROOM.search(str(x.get("item") or ""))}),
        "review": [{"row": x["i"], "item": x.get("item"), "quantity": x.get("quantity"), "value": x.get("value"),
                    "unit": x.get("unit"), "conf": x.get("confidence")} for x in review],
        "sheet": sheet_label(abs_path, page),
        "rows": rows,
    }


def xlsx(abs_path: str, page: int, virtual: str, out: str) -> dict:
    """The takeoff as an Excel file (F2's stoa.features.takeoff.to_xlsx: one line per room + every reading with its
    evidence), each row carrying the sheet (title-block number + title, else the file name) and page."""
    g = glance(abs_path, page, virtual)
    if not g.get("ok"):
        return g
    from stoa.features.takeoff import to_xlsx
    name = Path(virtual or abs_path).name
    sheet = f"{g['sheet']} · {name}" if g.get("sheet") else name
    review = {x["row"] for x in g.get("review") or []}
    rows = []
    for r in g["rows"]:  # a copy for the file: the 'file' column names the sheet; review / guessed rows say so
        r = dict(r, evidence=[dict(e, file=sheet) for e in r.get("evidence") or []])
        if r["i"] in review:
            r["status"] = "review (not in totals)"
        elif r.get("status") == "ok" and _weak(r):
            r["status"] = "not read (typical value, not in totals)"
        rows.append(r)
    Path(out).write_bytes(to_xlsx(rows, sheet, "Takeoff"))
    return {"ok": True, "out": out, "rows": len(rows)}


def main() -> None:
    try:
        if len(sys.argv) > 4 and sys.argv[4].endswith(".xlsx"):
            out = xlsx(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
        else:
            out = glance(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0, sys.argv[3] if len(sys.argv) > 3 else "")
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
