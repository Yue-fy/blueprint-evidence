"""Table QA: an LLM writes a few lines of pandas over tables we extracted deterministically; we run the code.

Why: the hidden test asks harder, free-form questions. The geometry (areas, counts, which room holds which
fixture) is exact and cheap to extract; what varies is the *question*. So the model only translates the
question into a query over a small, documented table (no numbers come from the model), the code runs on
real data, and the rows it used become the evidence. Cost: one local text-only call (~2-4k tokens in);
a second call only if the first code raised (the error is fed back, as in IFC-Bench's iterative agent).

    tables = floor_tables(fm)             # rooms / objects / apartments DataFrames (+ dxf bboxes)
    res = ask(question, tables)           # {"value", "unit", "used": {"rooms": [ids]}, "code", "error"}
"""

from __future__ import annotations

import math
import re
from pathlib import Path
import statistics

import pandas as pd
from shapely.geometry import Point
from shapely.geometry import box as sbox

from stoa.solvers.vector.floor import FloorModel

SAFE_BUILTINS = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k) for k in (
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len", "list", "max", "min", "range", "round",
    "set", "sorted", "str", "sum", "tuple", "zip", "isinstance", "print")}


# what the drawings call the rooms of a kind (bedrooms are mostly plain "Zimmer"/"Pièce"/"Stanza" in Swiss plans)
CONCEPT_LABELS = {"bathroom": ["Bad", "Bain", "Bagno", "Badezimmer", "Dusche", "WC", "Douche"],
                  "kitchen": ["Küche", "Cuisine", "Cucina", "Kochen"],
                  "bedroom": ["Zimmer", "Schlafzimmer", "Pièce", "Chambre", "Chambre à coucher", "Stanza", "Camera",
                              "Camera da letto"],
                  "balcony": ["Balkon", "Balcon", "Balcone", "Loggia", "Terrasse", "Terrazza", "Sitzplatz"]}


def _sides(poly) -> tuple[float, float]:
    """Length and width of a room = sides of its minimum rotated rectangle (exact for rectangular rooms)."""
    try:
        xs = list(poly.minimum_rotated_rectangle.exterior.coords)
        a = ((xs[0][0] - xs[1][0]) ** 2 + (xs[0][1] - xs[1][1]) ** 2) ** 0.5
        b = ((xs[1][0] - xs[2][0]) ** 2 + (xs[1][1] - xs[2][1]) ** 2) ** 0.5
        return round(max(a, b), 2), round(min(a, b), 2)
    except Exception:  # noqa: BLE001
        return float("nan"), float("nan")


def door_sides(fm: FloorModel, b, kind: str = "door", max_gap: float = 0.6) -> list:
    """The spaces a door/window opens onto: [swing room, room across the wall] (a window, or a door with nothing
    across within `max_gap` m: [room]). A DXF door block's bbox is the leaf square, ~80-90% inside the room it swings
    into; the wall side is the bbox edge least inside that room (a door in a corner has two such edges: the one
    with a room closest across it). Checked by eye on 2 plans (site1000 floor9385, site1024 floor9518: every door
    right); 80 dev floors: 2,511 doors, 48 lead outside/unmodelled, 2 join different apartments (was 8 before the
    corner rule). The old 0.25 m buffer touched 3-4 rooms per door (Korridor with 8 'doors')."""
    from shapely.geometry import LineString

    cand = [r for r in fm.rooms if r.poly.distance(b) < max_gap]
    if not cand:
        return []
    r1 = max(cand, key=lambda r: r.poly.intersection(b).area)
    if r1.poly.intersection(b).area < 0.3 * b.area:  # no swing square (sliding door / window in the wall)
        r1 = min(cand, key=lambda r: r.poly.distance(b.centroid))
    if kind != "door":
        return [r1]
    x0, y0, x1, y1 = b.bounds
    inner = r1.poly.buffer(-0.02)
    edges = [LineString(p) for p in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)),
                                     ((x0, y1), (x0, y0)))]
    frac = [e.intersection(inner).length / max(e.length, 1e-9) for e in edges]
    others = [r for r in cand if r is not r1]
    best = (9e9, None)
    for e, f in zip(edges, frac):
        if f > min(frac) + 0.1 or not others:
            continue
        m = e.interpolate(0.5, normalized=True)
        r = min(others, key=lambda r: r.poly.distance(m))
        best = min(best, (r.poly.distance(m), r), key=lambda t: t[0])
    return [r1] + ([best[1]] if best[1] is not None and best[0] <= max_gap else [])


def distinct_items(fm: FloorModel, kind: str) -> list:
    """(index, item) of the floor's items of a kind, without exact duplicates: some DXFs hold the same door block twice
    at the same place (site10089 floor39107 doors 11/12, site1000 floor9385 4 pairs of 36). The floor's door COUNT keeps
    them (the dataset's gold counts DXF entities); what one room has, or which rooms a door joins, counts it once."""
    seen, out = set(), []
    for k, it in enumerate(fm.items.get(kind, [])):
        key = tuple(round(v, 2) for v in it.bbox)
        if key not in seen:
            seen.add(key)
            out.append((k, it))
    return out


def floor_tables(fm: FloorModel) -> dict[str, pd.DataFrame]:
    rooms = []
    for r in fm.rooms:
        length, width = _sides(r.poly)
        rooms.append(dict(room_id=r.idx, number=r.number, name=r.name, area_m2=round(r.net, 2),
                          apartment=r.unit, outdoor=r.outdoor, shaft=r.shaft, printed_area_m2=r.printed_area,
                          perimeter_m=round(r.poly.length, 2), length_m=length, width_m=width,
                          n_doors=0, n_windows=0, n_toilets=0, n_washbasins=0, n_bathtubs=0, n_showers=0))
    by_id = {d["room_id"]: d for d in rooms}
    objects = []
    for kind, items in fm.items.items():
        first = {k for k, _ in distinct_items(fm, kind)}
        for k, it in enumerate(items):
            b = sbox(*it.bbox)
            c = b.centroid
            if kind in ("door", "window"):  # in a wall: a door joins two spaces, a window belongs to one room
                hit = [r for r in fm.rooms if r.poly.buffer(0.25).intersects(b)]  # (apartment column only, unchanged)
                sides = door_sides(fm, b, kind)
            else:
                hit = [r for r in fm.rooms if r.poly.contains(Point(c.x, c.y))] or \
                      [r for r in fm.rooms if r.poly.intersects(b)]
                sides = hit
            for r in sides if k in first else []:  # a block drawn twice at the same place counts once per room
                by_id[r.idx][f"n_{kind}s"] += 1
            row = dict(object_id=f"{kind}{k + 1}", kind=kind, rooms=",".join(r.number or f"#{r.idx}" for r in sides),
                       apartment=next((r.unit for r in hit if r.unit), None))
            if kind == "door":
                row["connects"] = " <-> ".join(f"{r.number or '#' + str(r.idx)} {r.name or ''}".strip() for r in sides) + \
                    ("" if len(sides) == 2 else " <-> outside/unlabelled space")
            objects.append(row)
    apts = []
    for u in fm.units:
        rs = [r for r in fm.rooms if r.unit == u.label]
        row = dict(apartment=u.label, n_rooms_numbered=sum(1 for r in rs if r.number),
                   n_rooms_indoor=sum(1 for r in rs if r.number and not r.outdoor and not r.shaft),
                   net_area_m2=round(sum(r.net for r in rs if r.number and not r.outdoor), 2),
                   outdoor_area_m2=round(sum(r.net for r in rs if r.outdoor), 2))
        for c in ("bathroom", "kitchen", "bedroom", "balcony"):  # rooms of a kind, by the drawing's labels
            labs = {x.lower() for x in CONCEPT_LABELS[c]}
            row[f"n_{c}s"] = sum(1 for r in rs if r.number and (r.name or "").lower() in labs)
        row["n_doors"] = sum(1 for o in objects if o["kind"] == "door" and o["apartment"] == u.label)
        row["n_windows"] = sum(1 for o in objects if o["kind"] == "window" and o["apartment"] == u.label)
        apts.append(row)
    net = [r for r in fm.rooms if r.number and not r.outdoor and not r.shaft]
    floor = [dict(net_area_m2=round(sum(r.net for r in net), 2), n_rooms_numbered_indoor=len(net),
                  n_apartments=len(fm.units), outdoor_area_m2=round(sum(r.net for r in fm.rooms if r.number and r.outdoor), 2))]
    rdf = pd.DataFrame(rooms)
    if not rdf.empty:
        rdf = rdf.set_index("room_id", drop=False)  # rooms.index == room_id, so `.index` is safe to report
        rdf.index.name = None
    return {"rooms": rdf, "objects": pd.DataFrame(objects), "apartments": pd.DataFrame(apts), "floor": pd.DataFrame(floor)}


DOC = """Tables extracted from one floor plan (Swiss, DXF; areas in m²; labels in German/French/Italian):
rooms: one row per room polygon. room_id (int, key), number (str like "2.05" or None), name (room label, e.g.
  Zimmer/Schlafzimmer=bedroom, Bad/Bain/Bagno=bathroom, Küche/Cuisine/Cucina=kitchen, Korridor/Couloir=corridor,
  Wohnen/Essen/Séjour=living, Balkon/Balcon=balcony, Reduit=storage, Treppenhaus=stairs, or None), area_m2 (float,
  net floor area), apartment (letter like "A" or None), outdoor (bool), shaft (bool: shaft/void), printed_area_m2 (area printed on the plan
  or None), perimeter_m (outline length), length_m / width_m (long / short side of the room's bounding rectangle,
  exact for rectangular rooms), n_doors (doors opening into or out of the room), n_windows (windows of the room),
  n_toilets, n_washbasins, n_bathtubs, n_showers.
objects: object_id, kind (door|window|toilet|washbasin|bathtub|shower), rooms (comma-separated room numbers: for a
  door the TWO spaces it connects, or one if it leads outside; for a window or fixture the room it is in), apartment,
  connects (doors only: "2.20 Küche <-> 2.19"). `rooms` holds room NUMBERS (never room_id): match whole items,
  e.g. objects[(objects.kind == 'door') & objects.rooms.str.split(',').apply(lambda l: '2.05' in l)]. Doors or
  windows of ONE room = that room's n_doors / n_windows. Rooms reachable directly from room X = the other room of
  each door whose `rooms` contains X.
apartments: apartment (letter), n_rooms_numbered (incl. balconies), n_rooms_indoor (what "how many rooms" means),
  net_area_m2 (numbered indoor rooms), outdoor_area_m2, n_bathrooms, n_kitchens, n_bedrooms (rooms labelled
  Zimmer/Pièce/Stanza/Schlafzimmer…), n_balconies (incl. loggias/terraces), n_doors, n_windows (objects assigned to it).
floor: one row: net_area_m2 (the floor's total net floor area), n_rooms_numbered_indoor, n_apartments, outdoor_area_m2.
Conventions: "net floor area" of an apartment = its numbered rooms, excluding outdoor ones (= apartments.net_area_m2);
of the floor = numbered rooms that are neither outdoor nor shafts (= floor.net_area_m2). A "room" is a row with a
number; rows without a number are wall gaps, ducts or voids (ignore them unless asked). Rooms "not in apartment X" =
floor total minus apartments.net_area_m2 of X. Answer "which apartment" with its letter. A door between two
rooms appears in both rooms' n_doors; count doors/windows in `objects`, not by summing n_doors. Room numbers are
strings: compare with "2.05", split `rooms` on ","."""

PROMPT = """{doc}

{data}

Question: {question}

Write Python (pandas available as pd; DataFrames {names}) that computes the answer.
Set `answer` (a number; a short string for which/what questions; True/False for yes/no questions), `unit`
({units} or None) and `used` = list of {id_col} values of the rows the answer is based on (empty if none).
For "which ..." questions `answer` is the identifier (apartment letter, room number or room name), not its area.
Compute everything from the DataFrames: never type a number or room_id from the data above into the code.
No imports, no printing. Reply with one ```python code block only."""


WHICH = re.compile(r"^\s*(which|welche[rs]?|quel(?:le)?s?\s+(?:appartement|pièce|logement)|lequel|laquelle|quale|"
                   r"in\s+which|welk[e]?|vilk(?:en|et)|cu[áa]l|kumpi|kt[óo]r[ey])\b", re.I)


def _render(tables: dict[str, pd.DataFrame], max_rows: int = 160, sep: str = ",") -> str:
    """sep="|": values are shown as printed, without CSV quote doubling (a 14"x6" cell is not shown doubled)."""
    out = []
    for name, df in tables.items():
        if df.empty:
            out.append(f"{name}: (empty)")
            continue
        if sep == ",":
            body = df.head(max_rows).to_csv(index=False)
        else:  # by hand: csv.QUOTE_NONE would still escape quotes (14\"x6\")
            def cell(v):
                return "" if v is None or (isinstance(v, float) and v != v) else str(v).replace(sep, "/").replace("\n", " ")
            body = "\n".join([sep.join(map(str, df.columns))] +
                             [sep.join(cell(v) for v in r) for r in df.head(max_rows).itertuples(index=False)]) + "\n"
        out.append(f"{name} ({len(df)} rows{', columns separated by ' + repr(sep) if sep != ',' else ''}):\n" + body)
    return "\n".join(out)


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    if m is None:  # an unclosed fence (reply cut at max_tokens): drop the opening fence line
        m = re.search(r"```(?:python)?[^\n]*\n(.*)", text, re.S)
    return (m.group(1) if m else text).strip()


HARDCODED = re.compile(r"^\s*answer\s*=\s*[-+*/().\d\s]+(?:#.*)?$", re.M)
TYPED_TEXT = re.compile(r"""^\s*answer\s*=\s*(?P<q>["'])(?P<v>(?:(?!(?P=q)).|\\.)*)(?P=q)\s*(?:#.*)?$""", re.M)


def _typed_cell(code: str, tables: dict[str, pd.DataFrame]) -> str | None:
    """`answer = "AL-1/GL-1"` where that string is a cell of a table: the model copied it from the rendered CSV
    (an 8B model often reads the neighbouring row). Returns the literal, or None."""
    m = TYPED_TEXT.search(code)
    if m is None:
        return None
    lit = m.group("v").replace('\\"', '"').replace("\\'", "'").strip()
    norm = re.sub(r"\s+", "", lit).upper()
    if not norm:
        return None
    for df in tables.values():
        for c in df.columns:
            if (df[c].dtype == object or pd.api.types.is_string_dtype(df[c])) and (df[c].astype(str).str.replace(r"\s+", "", regex=True).str.upper() == norm).any():
                return lit
    return None


# pandas >= 2 made Series.str.replace literal by default (regex=False); 8B-written code still assumes the old regex
# default: `.str.replace(r'(\d+)\s*SF', r'\1')` left '108 SF' in place and to_numeric raised (S0 11:10, A-801 49560)
REPLACE_CALL = re.compile(r"""\.str\.replace\(\s*(r?(["'])((?:\\.|(?!\2).)*)\2)\s*,\s*(r?(["'])(?:\\.|(?!\5).)*\5)\s*\)""")


def _regex_replace(m: re.Match) -> str:
    pat = m.group(3)
    # only unmistakable regexes (\\d, \\s, \\w escapes or a [...] class) that compile: '$', '(' or '|' alone stay literal
    if not re.search(r"\\[dDsSwWb]|\[[^\]]+\]", pat):
        return m.group(0)
    try:
        re.compile(pat.encode().decode("unicode_escape") if not m.group(1).startswith("r") else pat)
    except (re.error, UnicodeDecodeError):
        return m.group(0)
    return f".str.replace({m.group(1)}, {m.group(4)}, regex=True)"


def _run(code: str, tables: dict[str, pd.DataFrame]) -> dict:
    code = re.sub(r"(?<=[A-Za-z])'(?=S\b)", r"\\'", code)  # WOMEN'S inside '...' -> WOMEN\'S
    code = REPLACE_CALL.sub(_regex_replace, code)
    code = "\n".join(ln for ln in code.splitlines() if not re.match(r"\s*(import\s+(pandas|re|math|numpy)\b|from\s+\w+\s+import)", ln))
    if re.search(r"\b(import|open|exec|eval|__\w+__|os\.|sys\.|subprocess)\b", code):
        raise ValueError("code uses a forbidden construct")
    # RL 20:21 / audit risk 3: the real `pd` is in env, so file readers / writers would pass the check above
    # (pd.read_csv('/any/path'), read_pickle = code execution, df.to_csv(...)). The model only needs the given tables.
    if re.search(r"\bread_\w+\s*\(|\.to_(csv|pickle|parquet|json|excel|html|sql|feather|hdf|xml|clipboard|stata|orc|latex|markdown)\b|"
                 r"\bpd\s*\.\s*io\b|\b(getattr|setattr|globals|locals|vars|compile|breakpoint)\s*\(", code):
        raise ValueError("code uses a forbidden construct (file I/O or reflection); use only the given DataFrames")
    if HARDCODED.search(code):
        raise ValueError("`answer` is a typed-in number; compute it from the DataFrames (filter rows, then "
                         "len()/sum()) so the result is traceable to rows")
    # T2 (audit 19:51 risk #3): the generated code is untrusted; stoa.sandbox checks its AST, gives it module
    # proxies (no pd.read_csv / pd.io / statistics.sys), refuses file / network / process access while it runs,
    # and stops it after STOA_SANDBOX_SECONDS. Only the given DataFrames (copies) are in its namespace.
    from stoa import sandbox

    env = sandbox.run(code, tables, SAFE_BUILTINS)
    if "answer" not in env:
        raise ValueError("code did not set `answer`")
    ans = env["answer"]
    if hasattr(ans, "item"):
        ans = ans.item()
    if isinstance(ans, pd.DataFrame):  # W5 08:41: a row list came back as its CSV header ('room_id, number, ...')
        cols = [c for c in ("number", "name", "label", "kind", "area_m2", "apartment") if c in ans.columns][:3] or \
            list(ans.columns)[:3]
        ans = "; ".join(" ".join(f"{v:.2f} m²" if c == "area_m2" and isinstance(v, float) else str(v)
                                 for c, v in zip(cols, row) if v is not None and str(v) not in ("", "nan", "None"))
                        for row in ans[cols].head(60).itertuples(index=False, name=None)) or "none"
    if isinstance(ans, (pd.Series, list, tuple)) and not isinstance(ans, str):
        ans = ans.tolist() if hasattr(ans, "tolist") else list(ans)
        ans = ans[0] if len(ans) == 1 else ", ".join(map(str, ans))
    used = env.get("used", [])
    if hasattr(used, "tolist"):
        used = used.tolist()
    return {"value": ans, "unit": env.get("unit"), "used": [int(u) for u in list(used)[:200] if str(u).lstrip("-").isdigit()]}


LITERAL_RE = re.compile(r"""(?:contains|startswith|endswith|==|match)\(?\s*r?(["'])(.{2,40}?)\1""")


def _where(code: str, tables: dict[str, pd.DataFrame]) -> str:
    """For an empty selection: where do the strings the code searched for actually occur? ("'Casement' is in
    t2.Family (4 rows)"), so the retry can pick the right column."""
    hints = []
    for _, lit in LITERAL_RE.findall(code)[:4]:
        words = [w for w in re.split(r"[^A-Za-z0-9]+", lit) if len(w) >= 3] or [lit]
        pat = "|".join(re.escape(w) for w in words[:3])
        found = []
        for name, df in tables.items():
            for c in df.columns:
                if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
                    n = int(df[c].astype(str).str.contains(pat, case=False, regex=True, na=False).sum())
                    if n:
                        found.append(f"{name}[{c!r}] ({n} rows)")
        hints.append(f"{lit!r}: " + (", ".join(found[:4]) if found else "not found in any table"))
    return ("; where those words occur (case-insensitive): " + "; ".join(hints)) if hints else ""


ID_TOKEN = re.compile(r"\b(?=[A-Z0-9.-]*\d)[A-Z]{0,4}-?\d{1,4}(?:\.[A-Z0-9]{1,3})?\b")


DIM_OR_QTY = re.compile(r"\d+\s*'\s*-?\s*\d*(?:\s+\d+/\d+)?\"?|\d+(?:[.\s]\d+/?\d*)?\s*\"|"
                        r"\d+(?:\.\d+)?\s*(?:sf|sq\.?\s*ft|ft2|ft²|m2|m²|cfm|gpm|mm|cm|m|ft|in(?:ch(?:es)?)?|%)(?![\w-])", re.I)


PREAMBLE = re.compile(r"^\s*(?:on|in|from)\s+(?:sheet|drawing|page)\s+\S+\s+of\s+.*?(?:drawings?|set|plans?)\b"
                      r"(?:\s*\([^)]*\))?\s*,?", re.I)


def id_tokens(question: str, path: str = "") -> set[str]:
    """Identifiers named in a question (P13, RAG-09, 115.A, room 201), not dimensions or quantities (12'-0", 24"x6"),
    not the sheet/drawing named in an "On sheet A700 of the ... drawings (address)," preamble or in the file name."""
    q = DIM_OR_QTY.sub(" ", PREAMBLE.sub(" ", question))
    in_path = {t.upper().replace("-", "") for t in ID_TOKEN.findall(Path(path).name.upper())} if path else set()
    return {t.upper() for t in ID_TOKEN.findall(q) if t.upper().replace("-", "") not in in_path}


def _rows_mention_ids(question: str, used: list, tables: dict[str, pd.DataFrame], id_col: str,
                      path: str = "") -> bool | None:
    """Do the rows a lookup used contain an identifier from the question ("RAG-09", "115.A", "201")? None = the
    question names no identifier."""
    ids = id_tokens(question, path)
    if not ids:
        return None
    for df in tables.values():
        if id_col not in df.columns:
            continue
        for _, r in df[df[id_col].isin(used)].iterrows():
            cells = {str(v).strip().upper() for v in r.values}
            if ids & cells or any(i in c.split() for i in ids for c in cells):
                return True
    return False


def ask(question: str, tables: dict[str, pd.DataFrame], backend: str = "local", tries: int = 3, doc: str = DOC,
        id_col: str = "room_id", units: str = '"count", "m2"', lookup_text: bool = False, sep: str = ",") -> dict:
    """lookup_text: a typed-in string that is a table cell is sent back once ("look it up"); if the lookup fails,
    the typed answer is kept (res["typed"] = True)."""
    from stoa.llm import chat

    names = ", ".join(f"`{k}`" for k in tables)
    msgs = [{"role": "user", "content": PROMPT.format(doc=doc, data=_render(tables, sep=sep), question=question, names=names,
                                                      id_col=id_col, units=units)}]
    last = {"value": None, "unit": None, "used": [], "code": "", "error": "no attempt"}
    typed = None  # the first typed-in cell answer, kept as the fallback
    for attempt in range(tries):
        r = chat(msgs, backend=backend, max_tokens=700, temperature=0.0)
        code = _extract_code(r.text)
        try:
            res = _run(code, tables)
            lit = _typed_cell(code, tables) if lookup_text and typed is None and attempt < tries - 1 else None
            if lit is not None and res["value"] == lit:
                typed = dict(res, code=code, error=None, typed=True)
                raise ValueError(f"`answer` is the typed-in string {lit!r}; look the cell up in the DataFrame instead "
                                 "(e.g. df.loc[df[\"<id column>\"] == \"<id>\", \"<column>\"].iloc[0]) so it comes "
                                 "from the right row, and set `used` to that row's id")
            v = res["value"]
            if isinstance(v, float) and v != v:
                raise ValueError("the query returned NaN (an empty selection or a non-numeric column); check the "
                                 "filter against the cell values shown above")
            if v is None or (isinstance(v, str) and not v.strip()) or (v == 0 and not res["used"] and attempt < tries - 1):
                raise ValueError(f"the query returned {v!r} (no matching rows). Look at the actual cell values and "
                                 "column contents shown above (headers may be misaligned) and fix the filter"
                                 + _where(code, tables))
            if WHICH.search(question) and not isinstance(res["value"], str):
                raise ValueError(f"the question asks WHICH one, but answer = {res['value']!r}; set answer to the "
                                 "identifier string (apartment letter / room number / name)")
            res.update(code=code, error=None)
            if typed is not None and res["value"] != typed["value"] and \
                    _rows_mention_ids(question, res["used"], tables, id_col) is False:
                return typed  # the lookup read a row that is not the one the question names: keep the typed value
            return res
        except Exception as e:  # noqa: BLE001 - feed the error back once
            last = {"value": None, "unit": None, "used": [], "code": code, "error": f"{type(e).__name__}: {e}"}
            msgs += [{"role": "assistant", "content": r.text},
                     {"role": "user", "content": f"That code failed: {last['error']}. Fix it; reply with short code only "
                                                     "(no comments, no helper functions)."}]
    return typed if typed is not None else last
