"""DXF layer names -> the roles the dxf reader knows (P1, 14 Nov 14:05).

stoa/solvers/vector/floor.py reads rooms, doors, windows and fixtures by the Swiss dataset's layer names
(ROOM_OUTLINE, ROOM_TEXT, UNIT_OUTLINE, DOOR, WINDOW, FIXTURE, WALL). An architect's DXF names them differently (AIA:
A-AREA, A-AREA-IDEN, A-DOOR, A-GLAZ, A-FLOR-FIXT, A-WALL; German Raum / Tür / Fenster / Wand; …). On 30 dev questions
re-asked on Swiss DXFs with AIA layer names: 100% -> 3.3%, and 26 of 60 answers were confident zeros ('0 bathtubs'
@0.99, the empty FIXTURE layer) (up_fmt4, run 20261114-135827-upload-up_fmt4-7345).

    roles = layer_roles(path)           # {'A-DOOR': 'DOOR', ...} for layers whose name says their role
    path2 = relayer(path)               # a copy with entities moved to the role layers (cached), or None
    has_roles(path)                     # does the file use the reader's layer names at all?
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

ROLES = ("ROOM_OUTLINE", "ROOM_TEXT", "UNIT_OUTLINE", "DOOR", "WINDOW", "FIXTURE", "WALL")

# one wording for the upload normaliser and the dxf reader (D32), so an answer never lists the same thing twice
NO_ROLES_MISSING = ("this DXF does not use known layer names (none says which lines are rooms, doors, windows or "
                    "fixtures, e.g. everything on layer 0): counts and areas from it are unreliable; the PDF of the "
                    "sheet would help")


def relayer_note(mapping: dict[str, str]) -> str:
    shown = ", ".join(f"{k} → {v}" for k, v in sorted(mapping.items())[:8])
    return f"DXF layers read by their names as: {shown} (this file does not use the reader's own layer names)"


def blocks_missing(blocks: dict[str, int]) -> str:
    said = ", ".join(f"{n} on {r}" for r, n in sorted(blocks.items()))
    return (f"doors/windows/fixtures here are drawn as blocks ({said}); the DXF reader counts loose lines, so counts "
            "of them may be too low")
_TXT = r"(text|txt|iden|tag|name|label|stempel|nr|num|no\b|nummer|tekst|teksti|texte)"
# most specific first: room TEXT before room OUTLINE, unit before room
_RULES = [
    ("ROOM_TEXT", re.compile(rf"(a-(area|spce|room)-iden|(room|raum|räume|space|spce|ruimte|huone|tila|pi[eè]ce|local|"
                             rf"pomieszcz|area).*{_TXT}|raumstempel|roomtag)", re.I)),
    ("UNIT_OUTLINE", re.compile(r"(a-area-unit|\bunit|apartment|wohnung|\bflat|asunto|logement|woning|mieszkan)", re.I)),
    ("ROOM_OUTLINE", re.compile(r"(a-area(?!-patt|-hatch)|a-spce|\broom|\bräume|\braum(?!stempel)|raumpolygon|space|"
                                r"ruimte|huone|\btila|pi[eè]ce|\blocal|pomieszcz|fl[aä]che)", re.I)),
    # Tür only as a word start/end (Türen, Innentür), never inside a word: 'Structure', 'Furniture' (P1 15:08: a
    # permit DWG's 'Structure' layer was read as doors)
    ("DOOR", re.compile(r"(a-door|door|(?<![a-z])t[uü]r|t[uü]r(?![a-z])|tuer|deur|porte|\bov(i|et)\b|puerta|drzwi)",
                        re.I)),
    ("WINDOW", re.compile(r"(a-glaz|window|fenster|\braam|\bramen\b|fen[eê]tre|ikkuna|ventana|okno|glaz)", re.I)),
    ("FIXTURE", re.compile(r"(fixt|sanit|plumb|p-sanr|sanitär|sanitair|kaluste|wc\b)", re.I)),
    ("WALL", re.compile(r"(a-wall|wall|wand|w[aä]nde|muur|\bmur\b|sein[aä]|muro|[sś]cian)", re.I)),
]


def _scan(path: str | Path) -> tuple[frozenset[str], dict[str, int]]:
    """(layers that hold entities, INSERTs per layer): one parse per file version (cached)."""
    st = Path(path).stat()
    return _scan_cached(str(path), st.st_mtime, st.st_size)


@functools.lru_cache(maxsize=64)
def _scan_cached(path: str, mtime: float, size: int) -> tuple[frozenset[str], dict[str, int]]:
    return _scan_full(path, mtime, size)[:2]


@functools.lru_cache(maxsize=64)
def _scan_full(path: str, mtime: float, size: int):
    """(layers with entities, INSERTs per layer, INSERTs per (layer, block name))."""
    import ezdxf

    layers, inserts, named = set(), {}, {}
    for e in ezdxf.readfile(path).modelspace():
        layers.add(e.dxf.layer)
        if e.dxftype() == "INSERT":
            inserts[e.dxf.layer] = inserts.get(e.dxf.layer, 0) + 1
            k = (e.dxf.layer, str(e.dxf.name))
            named[k] = named.get(k, 0) + 1
    return frozenset(layers), inserts, named


def _layers(path: str | Path) -> set[str]:
    return set(_scan(path)[0])


# a block NAME that says door / window (several languages; Revit family words 'Flush', 'Bifold', 'Casement')
_DOOR_NAME = re.compile(r"(?<!out)(?<!in)door|(?<![a-z])t[uü]r|t[uü]r(?![a-z])|tuer|deur|(?<![a-z])port[ae](?![a-z])|"
                        r"puerta|drzwi|d[oö]rr|(?<![a-z])d[oø]r(?![a-z])|(?<![a-z])ovi(?![a-z])|flush|bifold", re.I)
_WINDOW_NAME = re.compile(r"window|fenster|fen[eê]tre|ventana|finestra|venster|(?<![a-z])raam|f[oö]nster|vindu|"
                          r"ikkuna|okno|casement|double[- _]?hung|glaz", re.I)
# a door's TAG / label / number block is not a door ('Door Tag' on A-DOOR-IDEN)
_NOT_OBJECT_NAME = re.compile(r"(?<![a-z])(?:tags?|iden\w*|labels?|te?xt|anno\w*|legend|marks?|marker|numbers?|"
                              r"symbol|schedule|no|nr|num)(?![a-z])", re.I)


def name_role(block: str) -> str | None:
    """'A-Window-100cm' -> WINDOW, 'Single-Flush - 36 x 84' -> DOOR, 'Fenstertür' -> DOOR, 'Door Tag' -> None."""
    if _NOT_OBJECT_NAME.search(block or ""):
        return None
    door, win = bool(_DOOR_NAME.search(block or "")), bool(_WINDOW_NAME.search(block or ""))
    return "DOOR" if door else "WINDOW" if win else None


def _insert_roles(named: dict) -> dict[tuple[str, str], str]:
    """Role of each (layer, block name): the block's NAME first (X1 dyimba, E1 17:18: 32 'A-Window-100cm' /
    'A-Window-50cm' blocks sit on layer A-Door, next to 2 sofas and a lavabo), else its layer's role. Once a file
    names its doors (windows) that way, an unnamed block on the door (window) layer is something else; a file that
    names none ('Fixed - 36 x 48' on A-GLAZ) keeps the layer's word."""
    by_name = {k: name_role(k[1]) for k in named}
    said = {r for r in by_name.values() if r}
    out = {}
    for (layer, block), r in by_name.items():
        if r is None and layer in ("DOOR", "WINDOW", "FIXTURE") and layer not in said:
            r = layer
        if r is not None:
            out[(layer, block)] = r
    return out


def insert_roles(path: str | Path) -> dict[tuple[str, str], str]:
    st = Path(path).stat()
    return _insert_roles(_scan_full(str(path), st.st_mtime, st.st_size)[2])


def block_counts(path: str | Path) -> dict[str, int]:
    """Block references (INSERT) per role (DOOR / WINDOW / FIXTURE): doors/windows/fixtures drawn as blocks, which the
    reader (it counts loose leaf polylines, arcs and window line triples) does not see. Role by block name, else by
    layer (insert_roles)."""
    st = Path(path).stat()
    named = _scan_full(str(path), st.st_mtime, st.st_size)[2]
    out: dict[str, int] = {}
    for k, r in _insert_roles(named).items():
        out[r] = out.get(r, 0) + named[k]
    return out


def has_roles(path: str | Path) -> bool:
    try:
        return bool(_layers(path) & set(ROLES[:6]))  # WALL alone does not answer anything
    except Exception:  # noqa: BLE001
        return False


# a door/window layer that holds their TAGS or curtain-wall parts, not the doors/windows themselves (Revit/AIA:
# A-DOOR-IDEN = door tags, A-GLAZ-CWMG = curtain-wall mullions, A-GLAZ-CURT = curtain-wall panels, …-ELEV / -HEAD)
_NOT_OBJECT = re.compile(r"(iden|tag|text|txt|anno|\bnr\b|num|cwmg|mull|curt|elev|head|patt|hatch|dims?\b)", re.I)


def role_of(layer: str) -> str | None:
    for role, rx in _RULES:
        if rx.search(layer):
            if role in ("DOOR", "WINDOW", "FIXTURE") and _NOT_OBJECT.search(layer):
                return None
            return role
    return None


def layer_roles(path: str | Path) -> dict[str, str]:
    out = {}
    for lay in sorted(_layers(path)):
        if lay in ROLES:
            continue
        if (role := role_of(lay)) is not None:
            out[lay] = role
    return out


def relayer(path: str | Path, out_dir: str | Path | None = None) -> tuple[str, dict[str, str]] | None:
    """(copy with role layer names, mapping) when the file uses none of the reader's names and some layer names say
    their role; None otherwise (nothing to do, or nothing recognisable)."""
    import ezdxf

    src = Path(path)
    if has_roles(src):
        return None
    mapping = layer_roles(src)
    if not set(mapping.values()) & set(ROLES[:6]):
        return None
    dst = Path(out_dir or src.parent) / f"{src.stem}.roles.dxf"
    if not dst.exists() or dst.stat().st_size == 0 or dst.stat().st_mtime < src.stat().st_mtime:
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            doc = ezdxf.readfile(str(src))
            for role in set(mapping.values()):
                if role not in doc.layers:
                    doc.layers.add(role)
            for e in doc.modelspace():
                new = mapping.get(e.dxf.layer)
                if new:
                    e.dxf.layer = new
            doc.saveas(str(tmp))
        except Exception:  # noqa: BLE001 — ezdxf cannot re-save some converter output (LibreDWG DWG -> DXF: a
            # malformed MATERIAL object, P1 15:20): rename the entities' layer values in the text itself
            _rename_bytes(src, tmp, mapping)
        tmp.replace(dst)  # atomic: a failed save never leaves an empty copy that later calls would reuse
    return str(dst), mapping


def _rename_bytes(src: Path, dst: Path, mapping: dict[str, str]) -> None:
    """Group code 8 (layer) values after the ENTITIES section start -> their role; nothing else changes."""
    raw = src.read_bytes()
    at = max(raw.find(b"ENTITIES"), 0)
    to = {}
    for k, v in mapping.items():
        for enc in ("utf-8", "cp1252"):
            try:
                to[k.encode(enc)] = v.encode()
            except UnicodeEncodeError:
                pass

    def sub(m):
        return m.group(1) + to.get(m.group(2), m.group(2)) + m.group(3)

    body = re.sub(rb"(\n[ \t]*8\r?\n)([^\r\n]*)(\r?\n)", sub, raw[at:])
    dst.write_bytes(raw[:at] + body)
