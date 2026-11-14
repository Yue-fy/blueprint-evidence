"""G1 c7: Erin's piping tag-family counts on an image's text layer (no OCR / API call in this test)."""


def test_counts_tags_from_image_lines(monkeypatch):
    import stoa.solvers.label_count as LC
    from stoa.solvers.piping_image import PipingImage

    lines = (("FSCV-1", (10, 10, 60, 20)), ("FSCV-2", (100, 10, 150, 20)), ("FSCV-2", (300, 10, 350, 20)),
             ("16\" SS FROM OPERATING TANKS", (10, 100, 300, 112)), ("CV-1", (400, 40, 430, 50)),
             ("PS-3 PS-4", (500, 60, 560, 70)), ("SHEET M-101A", (900, 900, 980, 912)))
    monkeypatch.setattr(LC, "_image_lines", lambda p: lines)
    s = PipingImage()
    a = s.solve("/tmp/upload/m101.png", "On sheet M-101A, how many distinct FSCV-tagged components are shown?")
    assert a.value == 2 and a.evidence[0].bbox_space == "image_px" and a.confidence <= 0.8
    assert s.solve("/tmp/upload/m101.png", "What is the labeled suction-header size on M-101A?").value is None
    assert not s.can_handle("/tmp/upload/m101.pdf", "How many CV-tagged components?")
