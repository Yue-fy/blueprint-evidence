"""Evidence precision (E1): ground-truth objects per drawing, and checks that an evidence box sits on the right one.

    from stoa.evidence.groundtruth import objects_for
    objs = objects_for("data/swiss_dwellings/site1000_bldg2082_floor9385.pdf", page=0, space="pdf_pt")
    # [GTObject(kind='door', box=(x0, y0, x1, y1), ident='door 3', ...), ...]   [] when the file has no ground truth

    from stoa.evidence.check import check_item, expected_kinds
    check_item(evidence_dict, question)  # -> ItemCheck(status='right'|'wrong_object'|'off'|'n/a', ...)
"""
