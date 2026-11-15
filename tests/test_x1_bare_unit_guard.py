"""X1 c14: bare-integer unit guard v2 (O1's 7c835ff9 + dimension ticks, archive ruler stamps, height questions)."""
from stoa.solvers.raster import textqa as Q


def test_ocr_dimension_strings_with_ticks_or_a_glued_letter_count_as_bare():
    for t in ["430", "-235", "900m", " 195 -", ".450"]:
        assert Q._BARE_DIM.fullmatch(t), t
    for t in ["12.5", "1:100", "2400mm", "abc", "3,40"]:
        assert not Q._BARE_DIM.fullmatch(t), t


def test_a_plot_check_ruler_stamp_is_no_unit_of_the_drawing():
    assert Q._RULER_NOTE.search("100 Plottet i størrelsesorden 1:1 er denne linjale 100 mm.")
    assert not Q._RULER_NOTE.search("MÅL 1:100, all dimensions in mm")


def test_height_questions_in_scope_unless_the_element_is_legitimately_under_1_m():
    def scope(q):
        return bool(Q._HEIGHT_Q.search(q)) and not Q._HEIGHT_SKIP_Q.search(q)
    assert scope("What is the ceiling height of the attic room?")
    assert scope("Hvor høy er kneveggen på loftet?")
    assert scope("How high is the ridge above the attic floor?")
    for q in ["How high is the window sill?", "How tall is the railing?", "Height of the kitchen counter?",
              "How high are the stair risers?"]:
        assert not scope(q), q
