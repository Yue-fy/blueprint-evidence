"""Sheet-level structure of one drawing page (VP): the views (viewports) printed on it and which one a question means.

    from stoa.sheet import views, pick_views, scope_for
    vs = views("A-101.pdf")                       # [View(kind='floor_plan', title='MODULE 01- ENTRY VESTIBULE - FLOOR PLAN', ...), ...]
    pick = pick_views("How many doors does module 02 have?", vs)
    s = scope_for("A-101.pdf", "How many doors does module 02 have?")   # None = no scoping (use the whole sheet)
    s.contains(x, y)                              # page space: PyMuPDF's unrotated pdf_pt (the CONTRACT evidence space)
"""

from stoa.sheet.north import north_arrow
from stoa.sheet.views import Pick, Scope, View, attach_view_choices, pick_views, scope_for, view_choices, views

__all__ = ["View", "Pick", "Scope", "views", "pick_views", "scope_for", "view_choices", "attach_view_choices", "north_arrow"]
