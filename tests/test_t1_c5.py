"""T1 cycle 5 (captain 23:04, Ziying afcec-04 'How many doors are in the north wing?' -> ours 12 @0.105 from the VLM with
NO boxes, hers 34): a count with no located object is a guess, not a count. stoa/trust/sanity.uncounted_guard +
support_state's 'unlocated — not counted (no symbols located)'."""
from stoa.contract import Answer, Evidence
from stoa.trust import sanity as SAN
from stoa.trust import support as S

Q = "How many doors are in the north wing?"


def _a(value=12, conf=0.6, solver="pipeline/vlm_lora", ev=(), unit="count", missing=()):
    return Answer(value=value, unit=unit, confidence=conf, evidence=list(ev), solver=solver, missing=list(missing),
                  trace=["intent: count doors"])


def test_afcec04_vlm_count_without_boxes_is_a_guess():
    out = SAN.sanity_guard(Q, _a(conf=0.6), "sheet.png")
    assert out.value == 12  # kept: a refusal scores nothing, the guess is right ~28 % of the time
    assert out.confidence == SAN.UNCOUNTED_CAP < 0.4  # the card's LOW_CONF -> 'Our guess'
    assert out.confidence > 0.3  # upload._weak (<= 0.3) would start a ~60 s vector trace of a PNG
    assert out.missing[0].startswith(SAN.UNCOUNTED)
    assert "select the north wing with 'Ask about an area'" in out.missing[0] and "doors" in out.missing[0]
    assert "searched the whole sheet" in out.missing[0]  # the VLM read the whole sheet, not a crop
    assert "vision model" in out.missing[0]
    st = S.support_state(Q, out)
    assert st["state"] == "unlocated" and st["why"].startswith("not counted — 12 is the vision model's estimate")
    assert st["shown"] == 0


def test_low_confidence_kept_but_flagged():
    out = SAN.sanity_guard(Q, _a(conf=0.105))
    assert out.confidence == 0.105 and out.value == 12
    assert out.missing[0].startswith(SAN.UNCOUNTED)


def test_only_a_searched_area_box_is_not_a_located_item():
    ev = [Evidence(file="s.png", page=0, bbox=[0, 0, 500, 300], note="searched area: the north wing (key plan)")]
    out = SAN.sanity_guard(Q, _a(ev=ev))
    assert out.missing[0].startswith(SAN.UNCOUNTED) and "the marked area" in out.missing[0]
    assert out.evidence == ev  # the searched area stays marked


def test_located_boxes_untouched():
    ev = [Evidence(file="s.png", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note="door") for i in range(12)]
    a = _a(ev=ev, conf=0.8)
    out = SAN.sanity_guard(Q, a)
    assert out.confidence == 0.8 and not out.missing
    assert S.support_state(Q, out)["state"] == "supported"


def test_partial_boxes_stay_partial_not_capped():
    ev = [Evidence(file="s.png", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note="door") for i in range(5)]
    out = SAN.sanity_guard(Q, _a(ev=ev, conf=0.7))
    assert out.confidence == 0.7
    st = S.support_state(Q, out)
    assert st["state"] == "approximate" and "showing 5 of 12" in st["why"]


def test_structured_model_count_exempt():
    out = SAN.sanity_guard("How many doors are on the ground floor?", _a(solver="pipeline/ifc", conf=0.95))
    assert out.confidence == 0.95 and not out.missing


def test_not_a_count_untouched():
    q = "What is the area of the north wing?"
    out = SAN.sanity_guard(q, _a(value=120.0, unit="m2", conf=0.6))
    assert out.confidence == 0.6 and not out.missing


def test_zero_left_to_the_zero_rule():
    out = SAN.uncounted_guard(Q, _a(value=0, conf=0.6))
    assert out.value == 0 and not out.missing


def test_prior_says_typical_value():
    out = SAN.sanity_guard("How many toilets are drawn on this floor plan?", _a(value=1, solver="pipeline/prior",
                                                                              conf=0.76))
    assert out.confidence == SAN.UNCOUNTED_CAP
    assert "a typical value" in out.missing[0] and "toilets" in out.missing[0]
    # the prior's own support line ('a typical value …') still wins
    assert S.support_state("How many toilets are drawn on this floor plan?", out)["state"] == "unlocated"


def test_idempotent():
    once = SAN.sanity_guard(Q, _a())
    twice = SAN.sanity_guard(Q, once)
    assert [m for m in twice.missing if m.startswith(SAN.UNCOUNTED)] == [once.missing[0]]
    assert twice.confidence == once.confidence


def test_items_noun():
    assert SAN._counted_items("How many pressure gauges are shown in the restroom detail?") == "pressure gauges"
    assert SAN._counted_items("Wie viele Türen?") == "items"


def test_legend_box_counts_when_the_legend_is_asked():
    q = "How many items does the overhead pipe repair legend list?"
    ev = [Evidence(file="s.pdf", page=0, bbox=[0, 0, 50, 80], note="legend: overhead pipe repair")]
    out = SAN.sanity_guard(q, _a(value=9, conf=0.75, solver="pipeline/agent", ev=ev))
    assert out.confidence == 0.75 and not out.missing
    # the same legend box for a door count is no located door
    out = SAN.sanity_guard(Q, _a(conf=0.75, ev=ev))
    assert out.missing[0].startswith(SAN.UNCOUNTED)


def test_partial_says_at_least():
    ev = [Evidence(file="s.png", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note="door") for i in range(5)]
    st = S.support_state(Q, SAN.sanity_guard(Q, _a(ev=ev, conf=0.7)))
    assert "at least 5 located" in st["why"]


# --- support: the marked printed line (T1 c5) --------------------------------------------------------------------------
NW = "How many square feet of dormitory does the north wing have?"


def _area(value, *notes, solver="pipeline/raster", unit="ft2"):
    ev = [Evidence(file="s.png", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note=n) for i, n in enumerate(notes)]
    return Answer(value=value, unit=unit, confidence=0.8, evidence=ev, solver=solver)


def test_area_line_with_unit_wins_over_bare_token():
    # live AFCEC A-2 PNG: 'NORTH WING DORMITORY = 6,285 SF' marked, the bare OCR token '6,285' came first
    a = _area(6285.0, "text '6,285'", "NORTH WING DORMITORY = 6,285 SF (printed area table, read from the image's text)")
    st = S.support_state(NW, a)
    assert st["state"] == "supported" and "NORTH WING" in st["why"]


def test_line_for_another_wing_is_unlocated():
    a = _area(6095.0, "WEST WING DORMITORY = 6,095 SF (printed area table)")
    st = S.support_state(NW, a)
    assert st["state"] == "unlocated" and "another part of the building" in st["why"]


def test_line_naming_no_part_keeps_its_state():
    # replay: 56 right / 7 wrong would lose 'supported' (room labels on a floor plan do not repeat the floor's name)
    a = _area(6095.0, "DORMITORY = 6,095 SF (printed area table)")
    assert S.support_state(NW, a)["state"] == "supported"


def test_qualifier_gap():
    g = S.qualifier_gap
    assert g(NW, "NORTH WING DORMITORY = 6,285 SF") is None
    assert g(NW, "WEST WING DORMITORY = 6,095 SF") == ("other", "north wing")
    assert g("What is the total area of the second floor?", "TOTAL SECOND FLOOR = 26,562 SF") is None
    assert g("What is the gross area of the 2nd floor?", "SECOND FLOOR 900 m2") is None
    assert g("What is the area of level 2?", "Level 02: 900 m2") is None
    assert g("What is the area of level 2?", "Level 12: 900 m2") == ("other", "level 2")
    assert g("What is the area of the ground floor?", "FIRST FLOOR 900 m2") == ("other", "ground floor")
    assert g("What is the area of the kitchen?", "KITCHEN 14.5 m2") is None


# --- xr-43 (Ziying m101a-02): 'How many pumps' is not only the main tag family -----------------------------------------
def _tags(*tags, value=None, q="How many pumps are there?"):
    ev = [Evidence(file="m.pdf", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note=f"tag '{t}'") for i, t in enumerate(tags)]
    return q, Answer(value=value or len(tags), unit="count", confidence=0.7, evidence=ev, solver="pipeline/label_count")


def test_generic_pumps_counted_from_one_tag_family_is_approximate():
    q, a = _tags("FP-1", "FP-2", "FP-3", "FP-4", "FP-5")
    st = S.support_state(q, a)
    assert st["state"] == "approximate" and "FP-tagged" in st["why"] and "jockey" in st["why"]


def test_named_family_stays_supported():
    for q in ("How many fuel pumps (FP) are shown on the piping plan?", "How many FP-tagged pumps are there?",
              "How many fuel pumps are there?"):
        q, a = _tags("FP-1", "FP-2", "FP-3", "FP-4", "FP-5", q=q)
        assert S.support_state(q, a)["state"] == "supported", q
    q, a = _tags("CV-1", "CV-2", "CV-3", q="How many non-surge check valves with rate-of-flow controllers are there?")
    assert S.support_state(q, a)["state"] == "supported"  # CV = check valve (replay: 10 right rows)
    q, a = _tags("FP-1", "JP-1", "DP-1")  # mixed families: every pump has its own tag
    assert S.support_state(q, a)["state"] == "supported"


# --- D1 23:22: every mark names the opposite of what was asked -----------------------------------------------------------
def test_roof_area_from_ground_catchment_lines():
    q = "What is the total roof catchment area of the two rainshed buildings?"
    ev = [Evidence(file="h.pdf", page=0, bbox=[0, 0, 9, 9], note="LINED GROUND IMENT - GC1: 128,878 SQ FT"),
          Evidence(file="h.pdf", page=0, bbox=[20, 0, 29, 9], note="INSTALL LINED GROUND CATCHMENT - GC2: 51,000 SQ FT")]
    a = Answer(value=179878.0, unit="ft2", confidence=0.7, evidence=ev, solver="pipeline/area_tags")
    out = SAN.sanity_guard(q, a)
    assert out.value == 179878.0 and out.confidence == SAN.PARTS_CAP
    assert out.missing[0].startswith(SAN.OPPOSITE) and "GROUND" in out.missing[0]
    assert S.support_state(q, out)["state"] == "unlocated"
    # a mark that names both, or a question naming both, is left alone
    ev2 = [Evidence(file="h.pdf", page=0, bbox=[0, 0, 9, 9], note="ROOF AND GROUND CATCHMENT 179,878 SQ FT")]
    assert SAN.opposite_guard(q, a.model_copy(update={"evidence": ev2})).confidence == 0.7


def test_region_ask_does_not_send_the_visitor_to_the_region_tool_again(monkeypatch):
    monkeypatch.delenv("STOA_ASK_CONTEXT", raising=False)
    out = SAN.sanity_guard(Q, _a(), "/x/sheet.region-ab12cd-crop.png")
    assert "inside the selected area" in out.missing[0] and "Ask about an area" not in out.missing[0]
    monkeypatch.setenv("STOA_ASK_CONTEXT", '{"region": [0, 0, 10, 10]}')
    out = SAN.sanity_guard(Q, _a(), "/x/sheet.png")
    assert "We searched the selected area" in out.missing[0]
    monkeypatch.setenv("STOA_ASK_CONTEXT", '{"point": [5, 5], "view": "plan"}')  # a right-click point is no area
    out = SAN.sanity_guard(Q, _a(), "/x/sheet.png")
    assert "Ask about an area" in out.missing[0]
    monkeypatch.delenv("STOA_ASK_CONTEXT")
    out = SAN.sanity_guard(Q, _a(), "/x/sheet.png")
    assert "Ask about an area" in out.missing[0]


def test_region_via_click_context():
    from stoa.agent import click

    with click.use({"region": [[0, 0], [10, 0], [10, 10], [0, 10]], "space": "image_px"}):
        out = SAN.sanity_guard(Q, _a(), "/x/sheet.png")
    assert "inside the selected area" in out.missing[0]
    with click.use({"view": "plan"}):
        out = SAN.sanity_guard(Q, _a(), "/x/sheet.png")
    assert "Ask about an area" in out.missing[0]


def test_mode_switch(monkeypatch):
    monkeypatch.setenv("STOA_T1_UNCOUNTED", "withhold")
    out = SAN.sanity_guard(Q, _a())
    assert out.value is None and out.missing[0].startswith(SAN.UNCOUNTED)
    assert any("STOA_T1_UNCOUNTED=withhold" in t for t in out.trace)
    monkeypatch.setenv("STOA_T1_UNCOUNTED", "off")
    out = SAN.sanity_guard(Q, _a())
    assert out.value == 12 and out.confidence == 0.6 and not out.missing


def test_f8_detector_candidate_note_is_not_a_printed_total():
    # F8 23:26 (night f8973dfe): CubiCasa PNG 'How many doors are shown on this floor plan?' 3 @0.5 (vlm_lora, gold 2) was
    # 'supported — the printed total … is marked' from OUR note 'the answer says 3'
    q = "How many doors are shown on this floor plan?"
    note = "door 1; candidate: the detector (pretrained CubiCasa5K) marks 2 here; the answer says 3"
    ev = [Evidence(file="p.png", page=0, bbox=[0, 0, 9, 9], note=note),
          Evidence(file="p.png", page=0, bbox=[20, 0, 29, 9], note=note.replace("door 1", "door 2"))]
    a = Answer(value=3, unit="count", confidence=0.5, evidence=ev, solver="pipeline/vlm_lora")
    st = S.support_state(q, SAN.sanity_guard(q, a))
    assert st["state"] == "approximate" and "showing 2 of 3" in st["why"]


def test_printed_area_table_cross_check_supports():
    q = "What is the total area of the second floor in square metres?"
    # the real rows' marks: where the reader read it (not a cited text line that lacks the number: that stays unlocated)
    ev = [Evidence(file="a.png", page=0, bbox=[0, 0, 9, 9], note="second floor plan (read here)")]
    a = Answer(value=2467.69, unit="m2", confidence=0.8, evidence=ev, solver="pipeline/raster",
               trace=["cross-check: the sheet's printed area table says 2467.69 m2 ✓"])
    st = S.support_state(q, a)
    assert st["state"] == "supported" and "printed area table" in st["why"]
    a.trace = ["cross-check: the sheet's printed area table says 2400.00 m2 ✓"]  # another value: no
    assert S.support_state(q, a)["state"] != "supported"
    a.trace = ["cross-check: 5 of 6 printed areas match the room outline traced from the wall lines at 1:200"]
    assert S.support_state(q, a)["state"] != "supported"


def test_withheld_uncounted_reading_is_kept_over_a_prior(monkeypatch):
    monkeypatch.setenv("STOA_T1_UNCOUNTED", "withhold")
    out = SAN.sanity_guard(Q, _a())
    assert SAN.flagged_zero(out)  # P1's upload._flagged_zero keeps it over the image pass's dataset prior


# --- xr-40: a count the sheet itself prints (AFCEC A-2 ROOM COUNT table: '48 DORMS' x 3 floors, 'TOTAL =' '144 DORMS') -----
AFCEC_TEXT = ["ROOM CO", "COUNT", "48 DORMS", "48 DORMS", "48 DORMS", "TOTAL =", "144 DORMS", "300 SLAB", "DORMITORY"]


def test_printed_counts():
    from stoa.trust import printedcount as P

    assert {n for n, _ in P.printed_counts("How many dormitory rooms are there on this level?", AFCEC_TEXT)} == {48}
    assert {n for n, _ in P.printed_counts("How many dorm rooms does the whole building have?", AFCEC_TEXT)} == {48, 144}
    assert P.printed_counts("How many accessible (ABA) dorm rooms are drawn on this floor?", AFCEC_TEXT) == []
    assert P.printed_counts("How many different slab types are called out on the plan?", AFCEC_TEXT) == []
    assert {n for n, _ in P.printed_counts("dorm room count", AFCEC_TEXT)} == {48}


def test_printed_count_check(monkeypatch):
    from stoa.trust import printedcount as P

    from stoa.solvers.raster.textlayer import TextItem

    monkeypatch.setattr(P, "_lines", lambda path: ([TextItem(t, [0, 0, 9, 9]) for t in AFCEC_TEXT], "image_px"))
    q = "How many dormitory rooms are there on this level?"
    ev = [Evidence(file="a.png", page=0, bbox=[i * 10, 0, i * 10 + 8, 8], note="room 'DORM'") for i in range(2)]
    a = Answer(value=2, unit="count", confidence=0.7, evidence=ev, solver="pipeline/plan_rooms")
    out = P.check(q, a, "a.png")
    assert out.value == 2 and out.confidence == 0.7 and out.missing[-1].startswith(P.PRINTED_COUNT_MISSING)
    assert S.support_state(q, out)["state"] == "approximate"
    a48 = a.model_copy(update={"value": 48, "evidence": ev[:1]})
    out = P.check(q, a48, "a.png")
    st = S.support_state(q, out)
    assert st["state"] == "supported" and "48 DORMS" in st["why"]
    monkeypatch.setenv("STOA_T1_PRINTEDCOUNT", "0")
    assert P.check(q, a, "a.png").missing == []
