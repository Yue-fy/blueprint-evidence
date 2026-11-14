# `raster` solver: counts, text and measurements on raster drawings

Owner: W3 (overnight 13 → 14 Nov). Solver name `raster`. It handles PNG/JPG/TIF drawings: CubiCasa5K plans,
Swiss Dwellings floors given as PNG, and historical scans (HABS, Finna, Europeana, Wikimedia). Every answer carries
evidence in image pixels (`bbox_space="image_px"`), a confidence measured on held-out or TRAIN data, and
`assumptions`/`missing` when it is an estimate.
Run: `python -m stoa.eval --solver raster --split <split> [--solver-arg twins=0]`. Unit tests: `tests/test_raster.py`.

## What answers what (measured)

| Question kind | Method (module) | Number (split) |
|---|---|---|
| CubiCasa room-label counts (bedrooms MH, WC rooms, balconies PARV, saunas) | one VLM label list per drawing, room codes → classes; 2×2 re-read when the list truncates (`vlm.py`, `classes.py`) | R1's hybrid label policy +13.5 pts on dev; sauna `labels_code` 69% vs labels 62% (TRAIN) |
| CubiCasa sinks / WC seats | rules from room labels: sinks = 1 kitchen + max(1, bathrooms) + WC rooms; WC seats = TRAIN mode given #bedrooms (`gate.json`, `cond_prior.json`) | sinks 69.3% vs prior 59.6%; WC seats 74.6% vs 68.6% (TRAIN 495 drawings) |
| CubiCasa doors / windows / showers | prior (every cheap detector lost to it; see Negative results) | prior: doors 12%, windows 14% |
| Any printed text on a scan or PNG (title block, sheet no., scale, names, notes) | tiled text layer (Qwen3-VL text spotting, ~1100 px tiles at ~2×, overlap merge, disk cache) + one QA call over numbered lines; cited lines become evidence (`textlayer.py`, `textqa.py`) | scan_text 68.8 vs vlm_direct 42.5 (text-judged); cited answers 75% right (54/72); Q's HABS red team 36/36 |
| "How many rooms labelled X" on non-CubiCasa images | exact label match in the text layer, quoted or unquoted (`__init__._text_count`, `apartment.unquoted_label`) | Swiss PNG 36/39 at conf 0.85 |
| Scale of a drawing | number labels of the graphic bar from the text layer, linear fit, then snap to the drawn bar (`scalebar.py`) | Swiss PNG 37/37 within 1% |
| Area of a named room on a PNG with no vector twin | printed area next to the room number, else flood fill × scale (`measure.py`) | held-out 43.3% within 2% vs VLM 36.7%; median error 3.3% vs 10.4% |
| Summed areas (apartment B, all rooms labelled X, whole floor) | unit boundary (red dash-dot, dilated 3-5 px) + printed areas + flood fill for unprinted rooms; numbering gaps make a sum "partial" (`apartment.py`) | 202 twin-less Swiss PNG Qs: 43.6% within 2% vs vlm_direct 0.5%; AUROC 0.844, ECE 0.049 |
| Which apartment is larger / largest | per-unit areas as above, conf by margin (<10%, <5%) | 83/94 vs vlm_direct 46/94; conf ≥ 0.8 12/12 |
| Door count on a PNG plan with no vector twin | swing-arc matched filter at the scale bar's px/m; doors on an apartment boundary counted twice, as the source data does (`doorarcs.py`) | all 100 Swiss dev floors: 12 exact, 31 ±1, 51 ±2 vs the counting LoRA 12 / 27 / 45, one box per door (LoRA none); doubling boundary doors: 12 exact vs 5 without |
| Window count on a PNG plan with no vector twin | wall stretches drawn as 4 thin parallel lines (2 faces + 2 glass) with equal white gaps: per-column/row run scan, lines told apart by ink mass, a mullion splits a run (`windows.py`); 1:100 only. A scale-free pre-scan (`prescan`: the same scan at 50/80/120 px/m, ~0.2 s) runs before the text layer, so a clean plan without such symbols costs no tokens | all 100 Swiss dev floors: 34 exact (74 answered: 46%; 1:200 sheets abstain; the pre-scan changes 0 answers) vs the counting LoRA 10; boxes = the answer on 100%; TRAIN 1:100 recall 0.89 / precision 0.94 |
| Room question on a sheet that shows no floor plan (elevation, section, detail) | sheet-kind veto (`sheet.py`) | low confidence + "this sheet shows no plan" |

Confidence is the measured hit rate of the method, so the pipeline can gate on it. VLM readings of numbers are capped
by kind: sums, superlatives and unprinted numbers ≤ 0.4; a cited label of another room class 0.3 (R1's by-eye
CubiCasa probe: conf ≥ 0.75 went from 2/5 to 5/5 right).

## Methods worth reusing

- **Tile before you read.** A one-shot VLM read of a large or dense sheet truncates or loops (10/20 HABS sheets hit
  max_tokens; Swiss "rooms labelled X" 3/7 one-shot vs 7/7 tiled). Tiles that still truncate are re-read as 2×2
  sub-tiles.
- **Measure from drawn geometry, not from VLM boxes.** VLM boxes for small digits are evenly spaced guesses (a
  scale bar fit with 0.0 px residual was 13% off). Find the drawn bar, wall or arc in pixels and use the text only
  for numbers.
- **Matched filters with a known scale.** Once px/m is known, a door is a quarter ring of radius 0.55-1.35 m.
  Score = ink on the ring − ink just inside and outside it. Reject full circles, and keep a detection only if its
  best radius lies inside the search range (a straight line scores higher as the ring shrinks). Work at full
  resolution: half resolution erased the 1-px arcs (recall 7% vs 91%).
- **Thin-line symbols: classify lines by ink mass, not by min value.** A 1-px line on a slightly tilted wall is
  antialiased over 2 px (black 64+173, grey 238+75), so 'min < 40 = black' misses most windows (recall 0.24 →
  0.69 with mass). Drop coloured overlays first (the red apartment boundary merges with wall faces).
- **Count what the source counts.** The Swiss DXF stores doors on apartment boundaries twice (54/60 TRAIN floors),
  and the gold counts both copies, so an image-only count must add them back.

## Negative results (don't retry without a new idea)

- CubiCasa doors/windows: Qwen3-VL-8B pointing, tiling, direct counts, classical CV arcs, VLM crop verification and a
  conditional prior all lost to the prior on TRAIN. The swing-arc filter does too (7/40 exact vs the prior's 8/40:
  noisy scans, no scale). R1's CubiCasa5K-pretrained detector (`raster_cc5k`, cc5k=1, licence CC BY-NC) is the
  route for those.
- Dimension-line reading (`dimline.py`): 16/126; it needs real tick/arrow detection.
- A walls-only refill for tiny bathroom fills leaks through windows (no gain; reverted).
- Door arcs on Revit-style IFC plan exports (resbim *_plan.png, no scale bar, 40 models, gold = IfcDoor): the door-arc scale fails there (text glyphs at the 12 px floor and grid bubbles outscore the doors; median 14-92 px/m vs ~115 true), best variant 7-8 exact of 40 vs the prior 18. The windows there are not 4-line symbols (the pre-scan finds none: abstain, 0 tokens). The scale-search ceiling (long side / 25) assumes a sheet with margins, not a tight crop. Grid bubbles pass the full-circle test because their centre is found ~3 px off; testing r +-4 px rejects them and drops 0 arcs on 99 Swiss floors (experiments/w3/doorwin/ifc_plan_doors.py, robust_circle_swiss.py). Not wired (freeze).
- Window symbols at 1:200 (~30 px/m): the glass lines render as dark as the faces; dropping the ink test and
  rejecting a 5th / ≥ 6 equally spaced line (stairs) cost 1:100 exact floors or lost on dev (34 → 32). Abstain there.

## Text engine: VLM or PP-OCRv6 (O1, Sat 14 Nov)

`STOA_TEXT_ENGINE=auto` (default, gate pending) = OCR on scans and photos of whole sheets (`textlayer.ocr_suits`: fail the clean-render gate AND long side ≥ 2000 px; HABS/Finna 100%, CubiCasa 5%), the VLM on clean CAD renders and small raster plans (OCR measured worse on CubiCasa room labels: 61 vs 80 labels on 24 plans, upscaling no help). `vlm` reads every text layer with the shared VLM as above. `ocr` reads it with **PP-OCRv6
small** (PaddleOCR 3.7, Apache-2.0) on CPU through `stoa/ocr/` everywhere (NOT for clean renders: Swiss PNG area sums 43.6 → 22.8%,
small printed areas misread). Without the OCR env (Docker) auto = vlm. Any OCR failure falls back to the VLM layer. Env: `stoa/ocr/requirements.txt`.

- Why: a cold HABS sheet cost ~31k LLM tokens and ~60 s of shared GPU for the VLM layer (Q bug 30: up to 101k on one
  sheet); OCR costs 0 tokens and 0 GPU, ~10-45 s CPU (8 threads, depends on load), with detector line boxes.
- Vertical lines (HABS title blocks on the right margin: 'SHEET 3 OF 6 SHEETS', 'CITY, COUNTY, STATE', 'DRAWN BY')
  are read by two extra full-page passes rotated 90° / 270°; neither the VLM layer nor a single OCR pass reads them.
- OCR misreads in scale notes are normalised (`engine.fix_text`: `I'-0"` → `1'-0"`, `8CALE 1/8° - f-0°` →
  `SCALE 1/8" - 1'-0"`, `3/ 8 "` → `3/8"`) before `scales()` parses them.
- PaddleOCR needs numpy 2.3 + opencv-contrib 4.10, so it lives in its own env (`.venv-ocr`,
  `$STOA_OCR_PYTHON`); the main env only reads JSON. Server: `python stoa/ocr/engine.py --serve 8110`
  (`$STOA_OCR_URL`); without it every cold sheet starts a subprocess (+~5 s model load). Batch indexing:
  `python -m stoa.ocr --index SPLIT.jsonl --jobs 3`. Cache: `$JUNCTION_SHARED/artifacts/O1/ocr/` (bump
  `stoa/ocr/__init__.py` VERSION to invalidate).
- `textqa.select_items`: layers longer than 160 lines (OCR reads up to ~600 on a dense HABS sheet) keep title-block
  margins, question words and long lines first, instead of the first 160 in reading order. Neutral on the VLM layer
  (scan_text 60.0 = 60.0).
- Comparison tool: `experiments/o1/compare_layers.py SPLIT.jsonl`. Probe set with catalogue gold (city, county,
  number of sheets): `experiments/o1/historical.jsonl` (`build_historical.py`).

## Kill switches (`--solver-arg`)

`text=0` (no text-layer QA), `offdomain=0` (old CubiCasa behaviour on every image), `twins=0` (pretend no
DXF/PDF/IFC twin exists, to simulate a PNG-only plan), `doors=0` / `windows=0` (off-domain door / window counts abstain), `prescan=0` (always read the scale bar before the window scan), `gate=cls:method`.

Cost on uploads: scans fail the clean-render gate and CubiCasa-style plans fail the window pre-scan BEFORE the text layer. Q's 30 CubiCasa-style window uploads: 0 tokens, 0.47 s mean (run 20261114-105811-raster-dw_upload_windows-6729).

## Data and experiments

TRAIN-only tuning in `experiments/w3/`. Per-object door/window gold in PNG pixels (from the DXF via
`stoa.solvers.vector.pdfmap`): `experiments/w3/doorwin/gold.py`; window detector eval `windows.py [--show STEM]`;
raster vs LoRA on all dev floors: `upload_set.py --what door|window` + `upload_cmp.py`. Probe sets and caches live in
`$JUNCTION_SHARED/artifacts/W3/`. Drawings: Swiss Dwellings v3.0.0 (CC BY 4.0), CubiCasa5K (CC BY-NC 4.0), HABS
(public domain).

## Scale and measurement on uploads (O1, Sat afternoon)

- **Vertical scale bars** (`scalebar._vertical_bars`): the label search runs again on the transposed text layer and
  image; a top-down bar counts only when the drawn bar is found next to its labels (Floorplanner '0m 2m 4m': 193.25
  px/m vs 193.9 measured by hand).
- **User-set scale** (`scalebar.user_scale(px_per_m)`, `solver_args {"px_per_m": X}` on `raster`): replaces any
  detected bar and R1's door-width estimate for that question; CubiCasa-style room areas then come from the
  CubiCasa5K room regions (CC BY-NC 4.0, demo only) at that scale. Notes say "from the scale set by the user".
- **Solid-wall room fill** (`measure.room_area(..., solid=True)`, `room_area_walls`): for furnished presentation plans
  (black walls, grey furniture) the region bounded by near-black walls only, door/window gaps ≤ 1.8 m closed; used
  only when it is 1.1–2× the ink fill, ≤ 120 m² and holds no other room label (Swiss PNG bench: 248 rooms unchanged).
- **Room dimensions** (`measure.room_dims`, `textqa._room_dims_answer`): length / width / 'L × W' of a labelled room
  = the minimum-area rectangle of its measured outline; evidence = outline polygon + measured side polyline.
  `experiments/o1/room_dims_eval.py --solid`: 404 Swiss PNG rooms vs the DXF rectangle, within 2%: length 67%,
  width 55% (rectangular outlines 74% / 62%) -> confidence 0.6 / 0.5, 0.3 when the outline is not a rectangle.
- **Unquoted room names** (`measure.find_label(loose=True)`): a label on the plan that the question names as a whole
  word ('the Salón'); skipped when numbers are printed next to it (the VLM reads printed dimensions instead).
- **cm dimension chains** (`textqa._cm_chain`): a printed 3–4 digit integer answering a length question, on a sheet
  that prints opening sizes in metres (≥ 3 values like 2.50) and never 'mm', is centimetres (1104 -> 11.04 m).
- A cited scale-bar label is never a dimension (`bar_echo`); an area question answered with a length -> 0.05.
- **Printed scale + image DPI** (`scalebar.printed_bar(img, tl)`, cycle 3): a printed '1:100' (or '1:100 @ A1' on an
  A2 copy -> 1:141) and the image's DPI tag give px/m = dpi / 0.0254 / N, trusted only when the image at that DPI is
  a standard sheet (A0–A5 / ANSI / ARCH within 2%; screenshots and PyMuPDF's default 96-dpi tag fail it) and the sheet
  prints one ratio (or exactly one labelled). Swiss PNGs (A3, 150 dpi): 12/12 within 0.25% of the drawn bar. Used by
  the deterministic room readers only, never offered to the VLM as a ruler (with it the VLM measured boxes instead of
  citing dimension strings: Esino '1104' -> an 11.44 m box). Wild LEVEL_11 render: comms long side 5.25 ✓, conference
  7.73 ✓.
- **Opening labels W over H** (`textqa._opening_pair`): '0.90' over '2.10' (or '0.90/2.10') next to a door/window;
  a width question gets the width: for doors the half < 1.6 m when the other is a door height (≥ 1.8 m), otherwise the
  upper line of a horizontal stack. Conf ≤ 0.6.
- **Level differences** (`textqa._level_diff_answer`): 'How much higher is A than B?' -> the VLM picks which printed
  level mark (853.85, +2.85, ±0.00) belongs to A and B, the program subtracts (Esino 855.55 − 853.85 = 1.70 on the
  render and the phone photo). Conf 0.6.
- **Door-swing sectors** (`measure.DOOR_SECTOR_M2`, env `STOA_O1_SECTOR`, 0 = off): the sector between a door leaf, its
  arc and the opening belongs to the corridor's blank, so the hull clip removed it; pieces of another blank inside the
  hull that are door-sized (0.55–1.3 m, square-ish) stay. Swiss bench 90 -> 91 within 2%; LEVEL_11 comms 16.32 -> 16.51.
- **Grounded label counts** (`textqa._label_count`): 'How many W.C.s?' answered N citing N identical labels -> conf 0.7.
- NEGATIVE (cycle 3, `experiments/o1/room_fill_eval.py --textqa --rect F`): snapping a near-rectangular fill to its
  min-area rectangle: within 2% 90 -> 65…71 of 242. A global ×1.015 area factor: +15 on one half of the drawings, −2 on
  the other (not a stable bias) -> not applied.

## Dimension chains and room names across languages (O1, cycle 4)
- `chains.py`: a printed total whose own chain sums to it is the overall dimension on that side ('13 500' = 2500 + 4000 +
  1300 + 2700 + 3000; "32'" = 12' + 8' + 12'; HABS "104'-2\"" = 45'-9" + 14'-7" + 43'-10"). Answers how long / wide / deep
  the building is and the area two overall dimensions enclose, with no scale at all (conf 0.7; area 0.6, footprint 0.4).
  The strings' spacing gives a scale (`chain_bar`) used by the room readers when there is no bar and no printed ratio:
  CAD sheets fit within 0.2-0.5 %, hand-lettered HABS chains only within 2.4-2.8 % (refused: needs <= 1.5 %).
  Kill switch: env `STOA_O1_CHAINS=0`.
- Negative: chains that dimension CLEAR room widths (German 1951 scan) never add up to the overall (walls left out).
  A line-only CAD render leaks in a flood fill, so a chain scale does not give its room areas (lineplan / vectorize do).
- `measure.CONCEPTS`: 'living room' finds WOHNZIMMER / OLOHUONE / OH / SALÓN …, only when no printed name matches and only
  in the question text before a relational word ('the small room next to the living room' names a landmark).
- `textqa._rooms_sum_answer`: 'combined area of the X and the Y' = each room's printed area / outline / chains, summed.
- `textqa.two_lengths`: an area question read as 'A x B' becomes A × B (a rectangle, stated), not a string.

## Whole large-format CAD sheets as PNG (O1, cycle 6)

- **OCR tile seams** (`stoa/ocr/engine.py`): images > 4000 px are read in 4000-px tiles (200 px overlap). A line that
  straddles a tile edge was read twice and `_dedupe` kept the higher score, often the CLIPPED read: D1's 4961-px A3 PNG
  lost 'Scale:1:200@A3' to 'Scale:' (no scale -> no measurement on the whole sheet). Each read now carries its room to
  its own tile's inner edges (text heights, capped at 2); among overlapping reads the one with more room wins. Engine
  tag '+t2'; `stoa.ocr` re-reads tiled images once (cache key '|t2') and skips an OCR server without the tag for them.
- **Large sheet** (`textqa._LARGE`, `_note_sheet`): a printed 1:N on a standard paper (`scalebar.printed_bar`) AND a
  digital render (`_clean`: clean_render OR >= 97.5% pure white; 160 historical scans all below) -> door gaps bridged at
  any resolution (`measure.room_area(large=True)`), big rooms allowed (400 m²), thin-wall fills count as bounded, room
  areas / perimeters at `LARGE_CONF` 0.5. Bench (`experiments/o1/c5/lineonly_bench.py --dpi 300 [--solid] [--bridge 2]`,
  D1's 15 line-only school sheets, 372 rooms) within 2%: hollow walls 119 -> 199, solid walls 145 -> 221 (median |err|
  1.3%). Swiss PNG bench unchanged (90 / 139 / 168). Kill switch `STOA_O1_LARGE_SHEET=0`.
- `_skirting_answer` also answers a plain 'perimeter of room X' (the traced outline, openings included).
- Loose room names by word keys (`measure._lkey`: 'Food bay/practical resources', 'Head/Mtg'); nested hits keep the
  longest. The flood seed skips a blank holding another label when the ring has one without (a label printed across a
  wall; `STOA_O1_SEED_SKIP=0`). Short labels ('Yr 6', 'W. St') count as other rooms in loose mode.
- Guards: a size answered with no number ('A:') -> 0.05; a bare mm dimension ('10,000') cited for an area -> 0.05;
  'how many square metres …' is never a symbol count.
- `doorarcs.detect` runs its ring filters in threads (identical output; `STOA_O1_ARC_THREADS`, default 6).
- Known gaps: courts / furniture drawn thicker than hollow walls bound the fill (Sports Hall 20 of 504 m²); open bays
  delimited by dashed lines (Food bay 15.75 vs 17); the style guesser calls line-only CAD PNGs 'cubicasa5k' (routing,
  G1's pipeline) so the agent answers first.
