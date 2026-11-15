"""`plan_rooms` (G1): room areas read from a vector PDF plan's own content — no dataset folder, model or twin needed.

Stoa's exam is THEIR files uploaded in our UI, and areas are what site engineers ask first. Many plan exports (Revit /
ArchiCAD / IFC viewers, our dataset's IFC sheets) draw every room as a FILLED polygon with its name / number inside,
and print the scale ('Scale 1:200'). Then area = polygon area in PDF points² × (paper metres per point × N)².
Measured on the dataset's IFC plan sheets (experiments/g1/pdf_rooms_probe.py): the light-blue room fills x the printed
1:100 / 1:200 reproduce the IFC net areas to < 0.3% on 9 of 10 models.

Reads: one room's area ('the room Kitchen (414B-1-001-007)', 'How big is the kitchen?'), all rooms' total, the rooms
labelled X (count / total), how many rooms, the largest / smallest room. Evidence = each used polygon's box (pdf_pt).
No printed scale -> the standard scale whose room sizes look most like typical rooms, stated as an assumption and
capped at 0.45 (the user can correct it). Abstains when the sheet has no filled room polygons with labels inside.
"""

from __future__ import annotations

import math
import re
import statistics
from functools import lru_cache
from pathlib import Path

from stoa.contract import Answer, Evidence
from stoa.data import abspath
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

PT_M = 0.0254 / 72  # one PDF point on paper, in metres
STD_SCALES = (20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500)
_SCALE_RX = re.compile(r"(?<![\d.,])1\s*:\s*(\d{1,4})(?![\d.,])")
_SCALE_WORD = re.compile(r"\b(?:scale|ma(?:ß|ss)stab|schaal|échelle|echelle|scala|mittakaava|skala|skaala|m)\b", re.I)
# typical net areas (m²) per room word, for a scale guess when none is printed (log-normal centre, any language label)
_TYPICAL = [(r"bed|schlaf|chamb|camer|slaap|makuu|sov|magamis", 12), (r"living|wohn|séjour|sejour|soggiorno|woon|olohuone|"
            r"vardag|elutuba", 25), (r"kitchen|küche|kuche|cuisine|cucina|keuken|keitti|kök|kok|köök", 9),
            (r"bath|bad\b|bain|bagno|badkamer|kylpy|vannituba", 5), (r"\bwc\b|toilet|sanit", 2), (r"storage|abstell|"
            r"berging|varasto|förråd|cellier|ripostiglio", 3), (r"loggia|balcon|balkon|terras|parveke", 6),
            (r"corridor|korridor|hall|flur|gang|entr|couloir|corridoio|käytävä|koridor|fuajee", 8), (r"office|büro|bureau|"
            r"ufficio|kantoor|toimisto|kontor|kabinet", 15)]
_STOP = {"the", "room", "rooms", "space", "what", "is", "of", "net", "floor", "area", "in", "on", "m2", "m²", "how",
         "big", "large", "this", "plan", "a", "an", "and", "de", "der", "die", "das", "van", "het", "la", "le", "il",
         "ruimte", "raum", "pièce", "piece", "locale", "level", "storey", "story", "total", "all", "which", "largest",
         "smallest", "biggest", "is", "are", "there", "many", "size", "op", "auf", "sur", "su", "netto",
         "vloeroppervlakte", "nettofläche", "wat", "wie", "gross", "groß", "ist", "die", "drawing", "sheet", "shown"}


_STOP2 = {"alle", "aller", "allen", "totale", "gesamte", "gesamt", "netto-fläche", "ruimtes", "räume", "kaikkien",
          "huoneiden", "rooms", "spaces", "toutes", "les", "des", "tutti", "tutte", "dei", "delle", "rum", "rummen"}
_ROOMS_WORDS = {"rooms", "spaces", "räume", "ruimtes", "pièces", "stanze", "huoneet", "rum"}


def _poly_points(d) -> list[tuple[float, float]]:
    pts = []
    for it in d["items"]:
        if it[0] == "l":
            pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
        elif it[0] == "re":
            r = it[1]
            pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif it[0] == "qu":
            q = it[1]
            pts += [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
        elif it[0] == "c":
            pts += [(it[1].x, it[1].y), (it[4].x, it[4].y)]
    out = []
    for p in pts:  # consecutive duplicates (segment ends) out
        if not out or abs(out[-1][0] - p[0]) > 1e-6 or abs(out[-1][1] - p[1]) > 1e-6:
            out.append(p)
    return out


@lru_cache(maxsize=64)
def read_plan(path: str, page: int = 0) -> dict | None:
    """{'rooms': [{'label', 'tokens', 'area_pt2', 'bbox'}], 'scale': N|None, 'scale_note': str, 'fill': rgb} or None."""
    import pymupdf
    from shapely.geometry import Point, Polygon

    with pymupdf.open(str(abspath(path))) as doc:
        if page >= len(doc):
            return None
        pg = doc[page]
        W, H = pg.rect.width, pg.rect.height
        words = [w for w in pg.get_text("words") if re.search(r"\w", w[4])]
        text = pg.get_text()
        groups: dict[tuple, list] = {}
        drawings = pg.get_drawings()
        fills_seen = {tuple(round(x, 2) for x in d["fill"]) for d in drawings if d.get("fill")}
        # the IFC-viewer sheet style of the dataset (room fill + wall fill): its windows are white-filled polygons
        # with a thin black outline (exact on 11 of 14 sheets with door/window gold, experiments/g1 12:58)
        ifc_style = {(0.89, 0.93, 0.97), (0.29, 0.29, 0.29)} <= fills_seen
        windows = []
        if ifc_style:
            for d in drawings:
                if (d.get("type") == "fs" and d.get("fill") and tuple(round(x, 2) for x in d["fill"]) == (1.0, 1.0, 1.0)
                        and tuple(round(x, 2) for x in (d.get("color") or ())) == (0.0, 0.0, 0.0)
                        and abs((d.get("width") or 0) - 0.28) < 0.03 and {it[0] for it in d["items"]} == {"l"}):
                    r = d["rect"]
                    windows.append([round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)])
        for d in drawings:
            f = d.get("fill")
            if not f or d.get("type") not in ("f", "fs"):
                continue
            col = tuple(round(x, 2) for x in f)
            if col in ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0)):
                continue
            pts = _poly_points(d)
            if len(pts) < 3:
                continue
            try:
                P = Polygon(pts).buffer(0)
            except Exception:  # noqa: BLE001
                continue
            if P.is_empty or P.area < 4 or P.area > 0.5 * W * H:
                continue
            groups.setdefault(col, []).append(P)
    best = None
    for col, polys in groups.items():
        rooms = []
        own: dict[int, list] = {}
        for w in words:  # each word belongs to the SMALLEST fill containing it (fills overlap: zones, rooms)
            pt = Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            cont = [i for i, P in enumerate(polys) if P.contains(pt)]
            if cont:
                own.setdefault(min(cont, key=lambda i: polys[i].area), []).append(w)
        for i, P in enumerate(polys):
            inside = own.get(i, [])
            if not inside or not any(re.search(r"[A-Za-zÀ-ÿ]{2,}", w[4]) for w in inside):
                continue
            inside.sort(key=lambda w: (round(w[1] / 4), w[0]))
            label = " ".join(w[4] for w in inside)
            rooms.append({"label": label[:120], "tokens": {t.lower() for t in re.split(r"[\s(),;:]+", label) if t},
                          "area_pt2": P.area, "bbox": [round(v, 1) for v in P.bounds], "perim_pt": P.length,
                          "sides_pt": _sides(P)})
        if len(rooms) >= 2 and (best is None or len(rooms) > len(best[1])):
            best = (col, rooms)
    if best is None:
        return None
    scale, note = _printed_scale(text)
    return {"rooms": best[1], "scale": scale, "scale_note": note, "fill": best[0], "windows": windows,
            "ifc_style": ifc_style, "walls": groups.get((0.29, 0.29, 0.29), []) if ifc_style else []}


_WALLS_Q = re.compile(r"\bw[aä]nde?\b|\bwalls?\b|\bmuren\b|\bmuur\b|\bsein(?:ä|a)t?\b|\bmurs?\b|\bpareti\b|\bv[äa]ggar\b|"
                      r"ściany|\bsciany\b|\bseinte\b", re.I)
_WALL_TOTAL_Q = re.compile(r"\b(total|all|sum|altogether|together|combined|gesamt\w*|insgesamt|zusammen|alle[rn]?|toutes?|"
                      r"tutt[ie]|kaikki\w*|yhteensä|totalt|alla|totale?|totaal|samen|łączn\w*|wszystk\w*|razem)\b", re.I)


def wall_axes(walls, m_per_pt: float) -> list[tuple[float, list]]:
    """(length m, axis polyline pt) per IfcWall fill path: the long axis of its minimum rotated rectangle. Squarish
    small paths (cut columns, same fill) are skipped (E1 13:29: sum ~ model total on 4 of 9 dev sheets within 2 %)."""
    out = []
    for P in walls:
        try:
            R = P.minimum_rotated_rectangle
            xy = list(R.exterior.coords)[:4]
        except Exception:  # noqa: BLE001
            continue
        if len(xy) < 4:
            continue
        e = [((xy[i][0] - xy[(i + 1) % 4][0]) ** 2 + (xy[i][1] - xy[(i + 1) % 4][1]) ** 2) ** 0.5 for i in range(4)]
        long_i = 0 if e[0] >= e[1] else 1
        L, Wd = e[long_i], e[1 - long_i]
        if L <= 0 or (L < 2.0 * max(Wd, 1e-6) and L * m_per_pt < 1.0):
            continue  # a column or a stub
        a, b = (long_i + 1) % 4, (long_i + 3) % 4  # the two short edges start at these corners
        mid = lambda i: ((xy[i][0] + xy[(i + 1) % 4][0]) / 2, (xy[i][1] + xy[(i + 1) % 4][1]) / 2)  # noqa: E731
        p0, p1 = mid(a), mid(b)
        out.append((L * m_per_pt, [[round(p0[0], 2), round(p0[1], 2)], [round(p1[0], 2), round(p1[1], 2)]]))
    return out


def _sides(P) -> tuple[float, float]:
    """(long, short) side of the polygon's minimum rotated rectangle, in points (a room's length x width)."""
    c = list(P.minimum_rotated_rectangle.exterior.coords)
    a = math.dist(c[0], c[1])
    b = math.dist(c[1], c[2])
    return (max(a, b), min(a, b))


_PERIM = re.compile(r"\b(perimeter|circumference|umfang|omtrek|périmètre|perimetre|perimetro|piiri|omkrets|ümbermõõt)\b",
                    re.I)
_DIMS = re.compile(r"\b(how (?:long|wide)|length|width|dimensions?|wie (?:lang|breit)|länge|breite|abmessung\w*|"
                   r"longueur|largeur|lunghezza|larghezza|lengte|breedte|pituus|leveys|längd|bredd|mitat)\b", re.I)


def _printed_scale(text: str) -> tuple[int | None, str]:
    hits = [(m, int(m.group(1))) for m in _SCALE_RX.finditer(text)]
    hits = [(m, n) for m, n in hits if 1 <= n <= 5000]
    if not hits:
        return None, "no scale printed on the sheet"
    near = [(m, n) for m, n in hits if _SCALE_WORD.search(text[max(0, m.start() - 25):m.start()])]
    pool = near or hits
    n = statistics.mode([n for _, n in pool])
    return n, f"printed scale 1:{n}" + ("" if near else " (no 'scale' word next to it)")


def _guess_scale(rooms) -> tuple[int, str]:
    """No printed scale: the standard scale under which the labelled rooms have the most typical sizes."""
    obs = []
    for r in rooms:
        lab = r["label"].lower()
        for rx, typ in _TYPICAL:
            if re.search(rx, lab):
                obs.append((r["area_pt2"], typ))
                break
    if len(obs) < 2:
        return 100, "no scale printed and too few typical rooms to infer one: assumed 1:100"
    def loss(n):
        k = (PT_M * n) ** 2
        return sum(math.log(a * k / t) ** 2 for a, t in obs)
    n = min(STD_SCALES, key=loss)
    return n, f"no scale printed: 1:{n} makes the {len(obs)} labelled rooms the most typical sizes (estimate)"


_TOTAL_Q = re.compile(r"\b(?:total|sum|all|gesamt\w*|alle[rn]?|totale?|totaal|łączn\w*|wszystk\w*|toutes?|tutt[ie]|"
                      r"kaikki\w*|yhteensä)\b", re.I)
_ROOM_WORD = re.compile(r"\b(?:rooms?|spaces?|räume|zimmer|ruimtes?|ruimten|vertrekken|kamers|pomieszcze\w*|pièces|"
                        r"locaux|espaces|stanze|locali|ambienti|huone\w*|tilo\w*|rum|habitaciones|espacios)\b", re.I)
_OTHER_OBJ = re.compile(r"\b(?:doors?|windows?|walls?|stairs?|columns?|beams?|slabs?|türen|fenster|wände|treppen|"
                        r"stützen|deuren|ramen|wanden|trappen|kolommen|balken|drzwi|okien|ścian|schodów|portes|"
                        r"fenêtres|murs|toilet\w*|sinks?|bath\w*|sanitary|kitchens?|bedrooms?|bathrooms?)\b", re.I)
_MODEL_SCOPE = re.compile(r"\b(?:in|of) the (?:whole |entire )?(?:model|building)\b|\b(?:enthält|im) (?:das )?modell|"
                          r"\bbevat het model\b|\bin het model\b|\bw modelu\b|\bdans le modèle\b|\bnel modello\b", re.I)


_APARTMENT_REF = re.compile(r"\b(?i:apartments?|flats?|wohnung(?:en)?|whg\.?|appartements?|appartamento|lägenhet|asunto)"
                            r"\s*(?:[A-Z]{1,2}\d{0,2}|\d{1,3}[A-Z]?)\b")  # PR c6: = pipeline._APARTMENT_REF


def _match(rooms, words: set[str]):
    ids = {w for w in words if re.search(r"\d", w)}
    names = words - ids
    scored = []
    for r in rooms:
        s = 3 * len(ids & r["tokens"]) + len(names & r["tokens"])
        if s:
            scored.append((s, r))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [r for s, r in scored if s == top]


@register("plan_rooms")
class PlanRooms(BaseSolver):
    name = "plan_rooms"
    extensions = (".pdf",)

    def solve(self, path: str, question: str) -> Answer:
        from stoa.solvers.label_count import _STEPS_Q

        if _STEPS_Q.search(question):
            # X1 wild farm: 'How many risers does the main staircase have?' -> lineplan counted the LABEL line
            # '16 RISERS @ 7.5"' as 1; the printed stair note's number is the answer (label_count reads it)
            return self.abstain("stair steps / risers: the printed stair note answers (label_count)")
        a = self._solve(path, question)
        if a.value is not None and str(a.unit) == "m2" and len(a.evidence) > 1 and _TOTAL_Q.search(question):
            # E1 14:40: a floor TOTAL = every IfcSpace fill, labelled or not (stacked same-footprint spaces are separate
            # spaces): Gymzaal '00 Begane grond' 1401.45 vs ref 1401.76 (ours, labelled outlines only: 456.9); on the
            # 72 dataset total-area questions all-outlines is right 55x and never worse
            try:
                from stoa.evidence.ifcplan_pdf import total_area_answer

                b = total_area_answer(path, question)
            except Exception:  # noqa: BLE001
                b = None
            if b is not None and b.value is not None and abs(float(b.value) - float(a.value)) > 0.02 * float(b.value):
                b.solver = self.name
                b.trace = [*(a.trace or []), f"labelled outlines sum to {a.value} m²; every space outline (E1 ifcplan_pdf) "
                                             f"gives {b.value} m² -> using every outline", *(b.trace or [])]
                b.assumptions = [*b.assumptions, f"the {len(a.evidence)} labelled rooms alone sum to {a.value} m²"]
                return b
        if a.value is not None:
            return a
        # E1 13:04: its label matcher (numbers weighted over names, quoted names win) and floor total, as fallbacks
        # when ours finds no match: 'Comunication (-1.1)' (ours: no match; E1 10.43 = gold), Polish / Dutch totals
        try:
            from stoa.evidence.ifcplan_pdf import room_area_answer, total_area_answer

            b = room_area_answer(path, question) or total_area_answer(path, question)
        except Exception:  # noqa: BLE001
            b = None
        if b is None or b.value is None:
            # E1 (captain 14:39, X1 wild): a LINE-ONLY CAD sheet (walls as lines, no room fills, printed 1:N): the asked
            # room found by its label or position, its outline traced from the wall lines (approximate, conf <= 0.6)
            if not any("no filled room polygons" in str(x) for x in (a.missing or [])):
                return a
            try:
                from stoa.evidence.lineplan import answer as lineplan_answer

                b = lineplan_answer(path, question)
            except Exception:  # noqa: BLE001
                b = None
            if b is None or b.value is None:
                return a
            b.solver = f"{self.name}/lineplan"
            b.trace = [*(a.trace or []), "plan_rooms: no filled room polygons -> lineplan (room traced from the "
                                         "wall lines)", *(b.trace or [])]
            return b
        b.solver = self.name
        b.trace = [*(a.trace or []), "plan_rooms found no match -> the ifcplan_pdf reader", *(b.trace or [])]
        return b

    def _solve(self, path: str, question: str) -> Answer:
        from stoa.router.intent import parse

        trace: list[str] = []
        try:
            plan = read_plan(str(path))
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the PDF's vector content ({type(e).__name__})")
        if not plan:
            return self.abstain("no filled room polygons with labels inside on this sheet")
        rooms = plan["rooms"]
        n, note = plan["scale"], plan["scale_note"]
        conf_cap = 0.95
        if n is None:
            n, note = _guess_scale(rooms)
            conf_cap = 0.45
        k = (PT_M * n) ** 2
        trace.append(f"plan_rooms: {len(rooms)} filled room polygons with labels (fill {plan['fill']}); {note}")
        it = parse(question)
        q = question.lower()
        if it.filters.get("apartment") or it.scope == "apartment" or _APARTMENT_REF.search(question):
            # PR c6: intent keeps no apartment filter for a room type ("the largest bedroom in apartment C" =
            # area bedroom [drawing]): the sheet's largest room beat pdf_vector's right 15.9 on uploads
            return self.abstain("plan_rooms does not know which rooms form an apartment", trace)
        assumptions = [f"room areas = the filled room polygons on the sheet × {note.split(':')[0] if n is None else f'1:{n}'}"
                       .replace("printed scale 1", "scale 1"), note]
        missing = [] if conf_cap > 0.5 else ["the drawing scale (a printed '1:N' or a known length) would make the areas sure"]

        def ev(rs, what="room"):
            return [Evidence(file=path, page=0, bbox=r["bbox"], bbox_space="pdf_pt",
                             note=f"{r['label'][:60]}: {round(r['area_pt2'] * k, 2)} m²") for r in rs[:80]]

        def area(r):
            return round(r["area_pt2"] * k, 2)

        scope_toks = {t.lower() for k_ in ("storey", "model", "sheet") if it.filters.get(k_)
                      for t in re.split(r"[\s(),;:]+", str(it.filters[k_])) if t}
        words = {t.lower() for t in re.split(r"[\s'\"‘’“”«»(),?;:]+", question) if t} - _STOP - _STOP2 - scope_toks
        room_f = it.filters.get("room") or it.filters.get("label")
        if room_f:
            words = {t.lower() for t in re.split(r"[\s(),;:]+", str(room_f)) if t} - _STOP or words
        superl = re.search(r"\b(largest|biggest|smallest|größte\w*|grösste\w*|kleinste\w*|grootste|kleinste|plus grand\w*|"
                           r"plus petit\w*|più grand\w*|più piccol\w*|suurin|pienin|största|minsta)\b", q)
        total = re.search(r"\b(total|all|sum|altogether|gesamt\w*|alle[rn]?|toutes?|tutt[ie]|kaikki\w*|yhteensä|totalt|"
                          r"alla|totale?|totaal|samen|łączn\w*|wszystk\w*|razem|suma|todas?|todos)\b", q)
        if it.task == "count" and it.target == "window" and plan.get("ifc_style") and not it.filters.get("model") \
                and plan.get("windows"):  # none found = another window style (warmond sheets), not 'no windows'
            wins = plan["windows"]
            return Answer(value=len(wins), unit="count", confidence=0.75,
                          evidence=[Evidence(file=path, page=0, bbox=b, bbox_space="pdf_pt", note=f"window {i}")
                                    for i, b in enumerate(wins[:150], 1)],
                          trace=trace + [f"windows: {len(wins)} window symbols (white-filled, thin black outline) on "
                                         "this IFC-viewer style sheet"], solver=self.name,
                          assumptions=["windows counted as this sheet style's window symbols (one per window)"],
                          missing=["a symbol drawn twice or a window shown as a gap would change the count"])
        room_word = _ROOM_WORD.search(q)
        if it.task == "count" and (it.target == "room" or (it.target == "unknown" and room_word)) and \
                (not words - {"rooms", "spaces"} or (room_word and not _OTHER_OBJ.search(q))):
            # E1 13:20: gold = EVERY IfcSpace outline (fill #e3edf7), labelled or not: len(space_polygons) was right
            # on all 4 of S0's d4 wrong counts (33 vs 40, 52 vs 56 x2, 7 vs 8); non-English wordings ('Wie viele Räume
            # …', 'Hoeveel ruimtes …', 'Ile pomieszczeń …') fell through to prior/pdf_text (6 of 6 wrong)
            spaces = []
            if plan.get("ifc_style"):
                try:
                    from stoa.evidence.ifcplan_pdf import space_polygons

                    spaces = space_polygons(path)
                except Exception:  # noqa: BLE001
                    spaces = []
            if spaces:
                model_scope = bool(it.filters.get("model")) or bool(_MODEL_SCOPE.search(q))
                n_sp = len(spaces)
                a = Answer(value=n_sp, unit="count", confidence=0.3 if model_scope else 0.8,
                           evidence=[Evidence(file=path, page=0, bbox=sp["bbox"], bbox_space="pdf_pt",
                                              polygon=sp.get("polygon"), note=f"space {i}: {sp.get('label') or '(no label)'}"[:80])
                                     for i, sp in enumerate(spaces[:150], 1)] if spaces and "bbox" in spaces[0] else ev(rooms),
                           trace=trace + [f"rooms counted: {n_sp} space outlines (IfcSpace fill), {len(rooms)} with a label"],
                           solver=self.name,
                           assumptions=[f"a room = a space outline drawn on the sheet (fill #e3edf7), labelled or not: "
                                        f"{n_sp} ({len(rooms)} carry a label)"],
                           missing=(["this is ONE sheet: the model's other storeys/sheets may hold more spaces"]
                                    if model_scope else []))
                if model_scope:
                    a.assumptions.insert(0, f"this sheet shows {n_sp} spaces; the question asks about the whole model")
                return a
            # G1 (X1 21:26 rp-065, AFCEC A-2 PDF): 'How many dormitory rooms are there on this level?' -> 2 @0.70 = the
            # two 'OPEN TO BELOW' fills. A question naming a room KIND counts only polygons labelled like it; none ->
            # not this reader (the printed '48 DORMS' / the labels answer)
            try:
                from stoa.solvers.label_count import _noun_matches, asked_types

                kinds = [s for s in asked_types(question) if s not in ("rooms", "room", "spaces", "space", "spac")]
            except Exception:  # noqa: BLE001
                kinds = []
            if kinds:
                same = [r for r in rooms if any(_noun_matches(w, kinds) for w in
                                                re.findall(r"[A-Za-zÄÖÜäöüßÅå]{3,}", r.get("label") or ""))]
                if not same:
                    # sl-067 'How many accessible (ABA) dorm rooms are drawn on this floor?' = the same 2 fills (gold 2:
                    # a highlight colour marks the asked subset): not an abstention, but not 0.70 either
                    return Answer(value=len(rooms), unit="count", confidence=min(0.45, conf_cap + 0.15),
                                  evidence=ev(rooms), solver=self.name,
                                  trace=trace + [f"rooms counted: {len(rooms)} filled polygons; none is labelled like "
                                                 f"'{kinds[0]}' (labels: " + ", ".join(r["label"][:24] for r in rooms[:6])
                                                 + ") -> the fill colour may mark the asked rooms"],
                                  assumptions=["a room = a filled polygon (its fill colour taken as marking the asked kind)"],
                                  missing=[f"none of the {len(rooms)} filled polygons carries a '{kinds[0]}' label: check "
                                           "the legend for what the fill colour means"])
                if len(same) < len(rooms):
                    return Answer(value=len(same), unit="count", confidence=min(0.6, conf_cap + 0.15),
                                  evidence=ev(same), solver=self.name,
                                  trace=trace + [f"rooms counted: {len(same)} of {len(rooms)} labelled polygons are "
                                                 f"labelled like '{kinds[0]}'"],
                                  assumptions=["a room = a filled polygon labelled like the asked kind; counted: "
                                               + ", ".join(r["label"][:30] for r in same[:12])
                                               + (" …" if len(same) > 12 else "")],
                                  missing=["rooms of that kind drawn without their own filled outline are not counted"])
            # S0 13:11 (d4 lone run): room counts @0.80 were off by 1-8 (rooms without their own outline) -> 0.7
            return Answer(value=len(rooms), unit="count", confidence=min(0.7, conf_cap + 0.25), evidence=ev(rooms),
                          trace=trace + [f"rooms counted: {len(rooms)} labelled polygons"], solver=self.name,
                          assumptions=["a room = a filled, labelled polygon on the sheet; found: "
                                       + ", ".join(r["label"][:30] for r in rooms[:12]) + (" …" if len(rooms) > 12 else "")],
                          missing=["rooms drawn without their own filled outline are not counted"])
        lin = _PERIM.search(q) or _DIMS.search(q)
        if lin and not re.search(r"\b(?:wall|wand|wände|mur|muri|seinä|vägg)", q):  # a room's perimeter / length x width: exact from its outline at the scale
            m = PT_M * n
            hits = _match(rooms, words - {"room"})
            if len(hits) != 1:
                return self.abstain(f"{len(hits)} rooms match {sorted(words)[:5]}; name one room for its "
                                    f"{lin.group(1)}", trace)
            r = hits[0]
            if _PERIM.search(q):
                val, what = round(r["perim_pt"] * m, 2), "perimeter (outline length)"
            else:
                L, Wd = (round(x * m, 2) for x in r["sides_pt"])
                wants_w = re.search(r"wide|width|breit|breite|largeur|larghezza|breedte|leveys|bredd", q)
                both = re.search(r"dimension|abmessung|mitat|\bx\b", q)
                val = f"{L} m x {Wd} m" if both else (Wd if wants_w else L)
                what = "length x width of the outline's smallest enclosing rectangle"
            return Answer(value=val, unit=None if isinstance(val, str) else "m", confidence=round(min(0.85, conf_cap), 3),
                          evidence=ev([r]), trace=trace + [f"room '{r['label']}': {what} = {val} m"], solver=self.name,
                          assumptions=assumptions + [f"{what}, measured on the room's filled outline"], missing=missing)
        if plan.get("walls") and it.task == "length" and _WALLS_Q.search(q) and _WALL_TOTAL_Q.search(q) and \
                not re.search(r"\b(doors?|windows?|t[üu]ren?|fenster|thick|dick|height|hoch|höhe)\b", q):
            # G1 cycle 3 (H 14:17 batch_b-14: '2.OG' total wall length on a lone IfcConvert sheet -> prior 244.8 @0.0;
            # ref 414.9 m): each IfcWall path's long axis at the printed scale, a stated estimate
            m = PT_M * n
            axes = wall_axes(plan["walls"], m)
            if axes:
                tot = round(sum(L for L, _ in axes), 2)
                evs = []
                for i, (L, ln) in enumerate(axes[:150], 1):
                    xs, ys = [x for x, _ in ln], [y for _, y in ln]
                    evs.append(Evidence(file=path, page=0, bbox=[min(xs), min(ys), max(xs), max(ys)], bbox_space="pdf_pt",
                                        polyline=ln, exact=False, note=f"wall {i}: {L:.2f} m (long axis)"))
                # measured (G1 15:10, lone IfcConvert sheets): -5 %, -9 %, +49 % vs the model totals (walls of several
                # thicknesses, split segments, railings in the wall fill); union-area / thickness is worse (-24..+61 %)
                c = min(0.2, conf_cap)
                miss = list(missing)
                if it.filters.get("model"):
                    c = min(c, 0.3)
                    miss.append("the walls of the other storeys: this sheet shows one storey")
                return Answer(value=tot, unit="m", confidence=round(c, 3), evidence=evs, solver=self.name,
                              trace=trace + [f"walls: {len(axes)} wall paths (IfcWall fill) on the sheet; sum of their "
                                             f"long axes = {tot} m"],
                              assumptions=assumptions[1:] + ["ROUGH estimate: total wall length = sum of each drawn "
                                                             "wall's long axis (corners counted twice, split walls "
                                                             "and railings distort it; off by 5-50 % on test sheets)"],
                              missing=miss + ["the model (IFC) for exact wall lengths"])
        if it.task not in ("area", "text", "unknown") and not re.search(r"how big|how large|area|size|fläche|oppervl|"
                                                                        r"surface|superficie|pinta-ala|yta", q):
            return self.abstain("plan_rooms reads room areas only", trace)
        if superl:
            cand = _match(rooms, words) if words - {"room", "rooms"} else rooms
            cand = cand or rooms
            pick = (max if superl.group(1).lower() in ("largest", "biggest") or "gr" in superl.group(1).lower()
                    or "grand" in superl.group(1).lower() or "suurin" in superl.group(1).lower()
                    or "största" in superl.group(1).lower() else min)(cand, key=lambda r: r["area_pt2"])
            want_name = it.task == "text" or re.search(r"\b(which|what room|welche\w*|quel\w*|quale|mikä|vilket|welke)\b", q)
            val = pick["label"] if want_name and it.task != "area" else area(pick)
            return Answer(value=val, unit=None if isinstance(val, str) else "m2", confidence=round(min(0.8, conf_cap), 3),
                          evidence=ev([pick]), trace=trace + [f"{superl.group(1)} of {len(cand)} rooms: {pick['label']} "
                                                              f"{area(pick)} m²"],
                          solver=self.name, assumptions=assumptions, missing=missing)
        if total and not room_f and it.target in ("room", "unknown") and not (words - _ROOMS_WORDS):
            s = round(sum(r["area_pt2"] for r in rooms) * k, 2)
            # S0 13:11: floor totals @0.85 missed most spaces on one sheet (456.9 vs 1401.8) -> 0.7, rooms listed
            return Answer(value=s, unit="m2", confidence=round(min(0.7, conf_cap), 3), evidence=ev(rooms),
                          trace=trace + [f"total of {len(rooms)} room polygons: {s} m²"], solver=self.name,
                          assumptions=assumptions + [f"the {len(rooms)} labelled room outlines on the sheet are summed "
                                                     "(walls, unlabelled areas and rooms without an outline are not): "
                                                     + ", ".join(r["label"][:24] for r in rooms[:10])
                                                     + (" …" if len(rooms) > 10 else "")],
                          missing=[*missing, "rooms drawn without their own filled outline are not in the total"])
        hits = _match(rooms, words)
        ids = {w for w in words if re.search(r"\d", w)}
        if ids and hits and not any(ids & r["tokens"] for r in hits):
            # 'room 2.42 ('Pièce')': the number names the room; a name-only match is another room
            return self.abstain(f"no room labelled {sorted(ids)[:3]} inside a filled outline", trace)
        if not hits:
            return self.abstain(f"no room on the sheet matches {sorted(words)[:6]}", trace)
        if len(hits) == 1:
            r = hits[0]
            alpha = [t for t in r["label"].lower().split() if re.fullmatch(r"[a-zà-ÿ]{3,}", t)]
            if len(alpha) != len(set(alpha)):
                # gymzaal sheet: 'werkkast werkkast werkkast 3 6 4' = three rooms' labels inside ONE outline -> the
                # outline is not that room's (1.12 vs 1.26 m² at 0.95 before this cap)
                conf_cap = min(conf_cap, 0.4)
                missing = [*missing, "several rooms' labels fall inside this one outline; its area may not be the "
                                     "asked room's"]
            return Answer(value=area(r), unit="m2", confidence=round(conf_cap, 3), evidence=ev([r]),
                          trace=trace + [f"room '{r['label']}': polygon {round(r['area_pt2'], 1)} pt² → {area(r)} m²"],
                          solver=self.name, assumptions=assumptions, missing=missing)
        if total or re.search(r"\b(rooms|spaces|räume|pièces|stanze|ruimtes|huoneet)\b", q):
            s = round(sum(r["area_pt2"] for r in hits) * k, 2)
            return Answer(value=s, unit="m2", confidence=round(min(0.75, conf_cap), 3), evidence=ev(hits),
                          trace=trace + [f"{len(hits)} rooms match: total {s} m²"], solver=self.name,
                          assumptions=assumptions + [f"summed the {len(hits)} rooms labelled like that"], missing=missing)
        listed = "; ".join(f"{r['label'][:40]} {area(r)} m²" for r in sorted(hits, key=lambda r: -r["area_pt2"])[:8])
        return Answer(value=area(max(hits, key=lambda r: r["area_pt2"])), unit="m2", confidence=0.3,
                      evidence=ev(hits), trace=trace + [f"{len(hits)} rooms match the question: {listed}"],
                      solver=self.name, assumptions=assumptions + ["several rooms match; the largest is reported"],
                      missing=[f"{len(hits)} rooms match the question ({listed}); name the room (number) to be sure"])


__all__ = ["PlanRooms", "read_plan"]
