"""T1 cycle 4: expert rule xr-36 (Ziying, s100f-04) — the printed dimension strings an answer used must add up to the
printed overall dimension ('500 + 4272 + 300 + 9950 + 500 = 15522'). stoa/trust/chaincheck.py."""
import os

import pytest

from stoa.contract import Answer, Evidence
from stoa.trust import chaincheck as C
from stoa.trust import sanity as SAN
from stoa.trust import support as S

pymupdf = pytest.importorskip("pymupdf")

S100F = ".stoa/human/drawings/d1f20c81a3-17b2e2ee.pdf"


def _pdf(tmp_path, texts, name="sheet.pdf"):
    """texts: [(x, y, 'string')] placed at baseline (x, y), 8 pt."""
    doc = pymupdf.open()
    pg = doc.new_page(width=800, height=500)
    for x, y, t in texts:
        pg.insert_text((x, y), t, fontsize=8)
    p = str(tmp_path / name)
    doc.save(p)
    doc.close()
    return p


def _box(pdf, text):
    with pymupdf.open(pdf) as doc:
        for b in doc[0].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                if t == text:
                    return list(ln["bbox"])
    raise AssertionError(text)


def _ans(pdf, *texts, value=26.486, question="What is the area of the slab?"):
    ev = [Evidence(file=pdf, page=0, bbox=_box(pdf, t), note=f"dimension {t} mm (printed; measures {t} mm at 1:50)")
          for t in texts]
    return Answer(value=value, unit="m2", confidence=0.6, evidence=ev, solver="pipeline/slab_area",
                  trace=["slab_area: printed dimensions"])


# a dimension line at y = 100 (x positions ~ proportional to the values), its overall printed beside it at y = 130,
# and the 500 mm foundation-wall width printed elsewhere on the sheet
CHAIN = [(120, 100, "4272"), (215, 100, "300"), (290, 100, "4975"), (420, 100, "4975"), (280, 130, "15522"),
         (600, 400, "500"), (700, 450, "WALL 500")]


def test_verified_with_edge_bands(tmp_path):
    pdf = _pdf(tmp_path, CHAIN)
    out = C.check(_ans(pdf, "4272"), pdf)
    line = out.trace[-1]
    assert line.startswith(C.TAG)
    assert "4272 + 300 + 4975 + 4975 + 2 × 500 (edge bands) = 15522 (the printed overall) ✓" in line
    assert any("add up to the printed overall" in a for a in out.assumptions)
    assert not any(str(m).startswith(C.DIMCHECK_MISSING) for m in out.missing)
    assert out.value == 26.486 and out.confidence == 0.6  # never changes the number


def test_verified_exact_chain_and_own_chain(tmp_path):
    pdf = _pdf(tmp_path, [(100, 100, "2500"), (200, 100, "4000"), (300, 100, "1300"), (220, 130, "7800"),
                          (100, 300, "4975"), (200, 300, "4975"), (150, 330, "9950")])
    out = C.check(_ans(pdf, "4000"), pdf)
    assert "2500 + 4000 + 1300 = 7800 (the printed overall) ✓" in out.trace[-1]
    out = C.check(_ans(pdf, "9950"), pdf)
    assert "'9950' = 4975 + 4975 (its own chain) ✓" in out.trace[-1]


def test_contradicted_marks_approximate(tmp_path):
    # 3000 + 3000 + 3000 = 9000, the overall printed beside the line says 9500 (a misread / missing segment)
    pdf = _pdf(tmp_path, [(100, 100, "3000"), (200, 100, "3000"), (300, 100, "3000"), (200, 130, "9500"),
                          (600, 400, "SLAB")])
    out = C.check(_ans(pdf, "3000"), pdf)
    miss = [m for m in out.missing if str(m).startswith(C.DIMCHECK_MISSING)]
    assert miss and "≠ the printed overall 9500" in miss[0]
    st = S.support_state("What is the area of the slab?", out)
    assert st["state"] == "approximate" and "do not add up" in st["why"]
    assert out.value == 26.486


def test_unchecked_changes_nothing_but_the_trace(tmp_path):
    pdf = _pdf(tmp_path, [(100, 100, "6200"), (600, 400, "4272"), (650, 450, "300")])
    a = _ans(pdf, "6200")
    out = C.check(a, pdf)
    assert "no printed overall found" in out.trace[-1]
    assert out.missing == a.missing and out.assumptions == a.assumptions


def test_no_printed_dimension_evidence_is_untouched(tmp_path):
    pdf = _pdf(tmp_path, CHAIN)
    a = Answer(value=12.0, unit="m2", confidence=0.7, solver="pipeline/area_tags",
               evidence=[Evidence(file=pdf, page=0, bbox=[1, 1, 9, 9], note="text: KITCHEN 12.0 m2")])
    assert C.check(a, pdf) == a
    png = Answer(value=61.69, unit="m2", confidence=0.5, solver="pipeline/slab_area",
                 evidence=[Evidence(file=str(tmp_path / "x.png"), bbox=[1, 1, 9, 9],
                                    note="dimension 6200 mm (printed; the traced slab measures 6176 mm at 1:50)")])
    assert C.check(png, str(tmp_path / "x.png")) == png  # pictures: not checked (no cold OCR read here)


def test_kill_switch_and_sanity_hook(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, CHAIN)
    out = SAN.sanity_guard("What is the area of the slab?", _ans(pdf, "4272"), pdf)
    assert out.trace[-1].startswith(C.TAG)
    monkeypatch.setenv("STOA_T1_DIMCHECK", "0")
    a = _ans(pdf, "4272")
    assert C.check(a, pdf) == a


@pytest.mark.skipif(not os.path.exists(S100F), reason="Stoa's S-100F sheet not on this machine")
def test_s100f_expert_checks():
    """Her two checks on the right block: 500 + 4272 + 300 + 9950 + 500 = 15522 and 500 + 6200 + 500 = 7200."""
    with pymupdf.open(S100F) as doc:
        boxes = {}
        for b in doc[0].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                boxes.setdefault(t, []).append(list(ln["bbox"]))
    ev = [Evidence(file=S100F, page=0, bbox=boxes["4272"][0], note="dimension 4272 mm (printed; measures 4272 mm at 1:50)"),
          Evidence(file=S100F, page=0, bbox=[456, 691, 467, 712], note="dimension 6200 mm (printed; measures 6201 mm at 1:50)")]
    a = Answer(value=26.486, unit="m2", confidence=0.6, evidence=ev, solver="pipeline/slab_area")
    out = C.check(a, S100F)
    line = next(t for t in out.trace if t.startswith(C.TAG + ": 4272"))
    assert "4272 + 300 + 4975 + 4975 + 2 × 500 (edge bands) = 15522 (the printed overall) ✓" in line
    assert "6200 + 2 × 500 (edge bands) = 7200 (the printed overall) ✓" in line
    # 26.486 m² = 4.272 × 6.2 m, both checked -> the card may say 'supported'
    assert out.trace[-1].startswith(C.ALL_CHECKED) and "4.272 × 6.2 m" in out.trace[-1]
    st = S.support_state("What is the area of the 300 SLAB-ON-GRADE W/ D22 @ 300?", out)
    assert st["state"] == "supported" and "adding up to the printed overall" in st["why"]
    assert not any(str(m).startswith(C.DIMCHECK_MISSING) for m in out.missing)
    assert "do not add up" not in S.support_state("What is the area of the 300 SLAB-ON-GRADE W/ D22 @ 300?", out)["why"]


def test_all_checked_needs_every_string_and_the_value(tmp_path):
    pdf = _pdf(tmp_path, [*CHAIN, (100, 300, "2500"), (200, 300, "4000"), (300, 300, "1300"), (220, 330, "7800")])
    # 4.272 m x 4.0 m = 17.088 m², both strings verified -> all checked + supported
    a = _ans(pdf, "4272", "4000", value=17.088)
    out = C.check(a, pdf)
    assert out.trace[-1].startswith(C.ALL_CHECKED)
    assert S.support_state("What is the area of the slab?", out)["state"] == "supported"
    # a value that is NOT their product -> no 'all checked' line (the numbers were checked, the answer was not)
    out = C.check(_ans(pdf, "4272", "4000", value=20.0), pdf)
    assert not any(t.startswith(C.ALL_CHECKED) for t in out.trace)
    # one string unchecked (the lone 6200) -> no 'all checked' line
    pdf2 = _pdf(tmp_path, [*CHAIN, (100, 300, "6200")], name="b.pdf")
    out = C.check(_ans(pdf2, "4272", "6200", value=26.486), pdf2)
    assert not any(t.startswith(C.ALL_CHECKED) for t in out.trace)
    # a length that is one checked printed dimension
    out = C.check(_ans(pdf, "4000", value=4.0, question="How long is the room?").model_copy(update={"unit": "m"}), pdf)
    assert out.trace[-1].startswith(C.ALL_CHECKED)


# --- a cross-reference to another sheet is not a size (S-100F genset, live 'SEE A1/S-513' @0.65) -------------------
@pytest.mark.parametrize("q", ["What's the footprint of the generator set foundation?",
                               "How big is the genset foundation in plan?",
                               "What is the area of the genset foundation?"])
def test_xref_is_not_a_size(q):
    a = Answer(value="SEE A1/S-513", unit=None, confidence=0.65, solver="pipeline/pdf_text",
               evidence=[Evidence(file="x.pdf", page=0, bbox=[1, 1, 9, 9], note="text: GENSET FOUNDATION / SEE A1/S-513")])
    out = SAN.sanity_guard(q, a)
    assert out.value is None and out.confidence == 0.0
    assert out.missing[0].startswith(SAN.XREF_MISSING) and "(A1/S-513)" in out.missing[0]


@pytest.mark.parametrize("q,v", [("Where is the genset foundation detailed?", "SEE A1/S-513"),       # a 'where' question
                                 ("Which detail shows the genset foundation?", "SEE A1/S-513"),
                                 ("What is the area of the genset foundation?", "26.486"),          # a number
                                 ("What is the area of the kitchen?", "SEEDING AREA 12 m2")])        # not 'SEE'
def test_xref_controls(q, v):
    a = Answer(value=v, unit=None, confidence=0.65, solver="pipeline/pdf_text")
    out = SAN.ref_guard(q, a)
    assert out.value == v


# --- the whole drawing's overall size is not a named element's footprint (S-100F genset 419.094 @0.4) -------------
OVERALL_TRACE = ("[raster] overall dimensions: width '27000' = 600 + 3450 + 600 (mm) = 27 m; depth '15522' = 622 + 3200 "
                 "(mm) = 15.522 m; product 419.094 m2")


def _overall(v=419.094):
    return Answer(value=v, unit="m2", confidence=0.4, solver="pipeline/raster", trace=["raster: 419.094 m2", OVERALL_TRACE])


@pytest.mark.parametrize("q,x", [("What's the footprint of the generator set foundation?", "generator set foundation"),
                                 ("What is the area of the pump room?", "pump room"),
                                 ("How big is the genset pad?", "genset pad")])
def test_overall_size_is_not_an_elements(q, x):
    out = SAN.sanity_guard(q, _overall())
    assert out.value is None and out.missing[0].startswith(SAN.OVERALL_SCOPE)
    assert f"not the {x}'s" in out.missing[0] and "27000 x 15522 = 419.094 m2" in out.missing[0]


@pytest.mark.parametrize("q", ["What is the footprint of the house without the porches, in square metres?",
                               "What area do the plan's two overall dimensions enclose?",
                               "What is the footprint of the building?",
                               "How big is the building overall?",
                               "What's the footprint?"])
def test_overall_size_controls(q):
    assert SAN.overall_scope_guard(q, _overall()).value == 419.094


# --- sums whose marks do not name the asked thing (support.off_subject) ---------------------------------------------
def _sum_answer(notes, value, solver="pipeline/area_tags", unit="ft2", exact=None):
    ev = [Evidence(file="x.pdf", page=0, bbox=[10 * i, 10, 10 * i + 9, 19], note=n, exact=exact) for i, n in enumerate(notes)]
    return Answer(value=value, unit=unit, confidence=0.7, evidence=ev, solver=solver)


def test_sum_with_an_off_subject_mark_is_approximate():
    q = "What is the combined area of the two ground catchments?"
    a = _sum_answer(["BUILDING #241: 40,228 SQ FT", "LINED GROUND CATCHMENT GC2: 51,000 SQ FT"], 91228.0)
    st = S.support_state(q, a)
    assert st["state"] == "approximate" and "'BUILDING #241'" in st["why"]
    ok = _sum_answer(["LINED GROUND CATCHMENT GC2: 51,000 SQ FT", "LINED GROUND CATCHMENT GC1: 128,878 SQ FT"], 179878.0)
    assert S.support_state(q, ok)["state"] == "supported"


def test_odd_one_out_and_controls():
    q = "What is the total floor area of the English classrooms?"
    notes = ["English 1: 55 m2", "English 2: 55 m2", "English 3: 55 m2", "ICT Classroom: 62 m2"]
    assert S.off_subject(q, _sum_answer(notes, 227.0).evidence) == "ICT Classroom"
    assert S.off_subject(q, _sum_answer(notes[:3], 165.0).evidence) is None
    # 'and' in the question: two kinds are asked, each mark names one of them
    q2 = "What is the combined area of the classrooms and the offices?"
    assert S.off_subject(q2, _sum_answer(["Classroom 1: 50 m2", "Classroom 2: 50 m2", "Office 1: 12 m2"], 112.0).evidence) is None
    # labels that name nothing the question says (codes / numbers): not checkable, no flag
    q3 = "What is the combined area of the three wings?"
    assert S.off_subject(q3, _sum_answer(["A: 6,095 SF", "B: 6,092 SF", "C: 6,285 SF"], 18472.0).evidence) is None


# --- a part answered with the printed total (sanity.total_for_one_part) ---------------------------------------------
def _cited(line, value, q_unit="m2"):
    return Answer(value=value, unit=q_unit, confidence=0.65, solver="pipeline/pdf_text",
                  evidence=[Evidence(file="x.pdf", page=0, bbox=[1, 1, 9, 9], note=f"text: {line}")])


def test_total_given_for_one_part():
    q = "What is the gross internal floor area of the second floor?"
    a = _cited("3960m2 / Ground: / First: 2540m2 / 2110m2 / Second: / 8610m2", 8610.0)
    assert SAN.total_for_one_part(q, a) == "3960 + 2540 + 2110 = 8610"
    out = SAN.parts_guard(q, a)
    assert out.confidence == SAN.PARTS_CAP and out.missing[0].startswith(S.PARTS_MISSING)
    assert S.support_state(q, out)["state"] == "approximate"
    # controls: the question asks for the total; the value is one of the parts
    assert SAN.total_for_one_part("What is the total gross internal floor area?", a) is None
    assert SAN.total_for_one_part(q, _cited("3960m2 / Ground: / First: 2540m2 / 2110m2 / Second: / 8610m2", 2110.0)) is None


def test_counting_its_is_an_inclusion_cue():
    q = "What's the total common area on this floor, counting its circulation and utility spaces?"
    a = _cited("COMMON SPACE = / 2,911 SF / COMMON SPACE CIRCULATION = 1,240", 2911.0, q_unit="ft2")
    assert SAN._AGG["area"].search(q)
    assert not SAN._AGG["area"].search("What is the floor area of the rooms on the second floor, not counting the stairs?")
