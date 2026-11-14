# Results

How we measured, what we got, and where it still breaks. Every number names the commit or eval run it comes from.
Commit ids refer to the `night` branch of the team's
[workspace repository](https://github.com/Yue-fy/Junction_Hannah_family) (its `main` branch has a squashed history, so these ids are not on it). This clean
repository has a fresh history. Run ids (`YYYYMMDD-HHMMSS-<solver>-<split>-<hash>`) name prediction files written by
`python -m stoa.eval`. The predictions themselves stay in the workspace's results store and are not published.

Most of the engineering and measuring was done by an overnight team of coding agents under the captain's direction,
with one gatekeeper that merged a change only after it passed the same fixed set of runs ("gates" below).

## Contents

- [How we score](#how-we-score)
- [The Stoa dataset: splits and the sealed test](#the-stoa-dataset-splits-and-the-sealed-test)
- [Stoa's own example sheets](#stoas-own-example-sheets)
- [Real drawings from outside the dataset](#real-drawings-from-outside-the-dataset)
- [Scans of the example sheets](#scans-of-the-example-sheets)
- [Dataset files uploaded the way a user would](#dataset-files-uploaded-the-way-a-user-would)
- [Cost, latency and data governance](#cost-latency-and-data-governance)
- [Cost tiers](#cost-tiers)
- [Calibration](#calibration)
- [Evidence](#evidence)
- [Baselines](#baselines)
- [Hybrid upload routing with Gemini](#hybrid-upload-routing-with-gemini)
- [The whole dataset on one Verda GPU](#the-whole-dataset-on-one-verda-gpu)
- [Known weaknesses](#known-weaknesses)
- [Human audit: the expert workbench](#human-audit-the-expert-workbench)
- [Internal audit: a random sample of the shipped demo](#internal-audit-a-random-sample-of-the-shipped-demo)

## How we score

- Stoa's rule: counts must be exact; areas, lengths and volumes must be within 2%. Free-text answers are not scored
  for accuracy (where we report them, an LLM judge graded them, and we say so).
- **Micro** = the share of scored questions answered right. **Weighted** = the accuracy of each bucket (source ×
  question type) weighted by that bucket's share of the full dataset. Our dev and test samples cap each source, so
  weighted re-weights them to the real mix, where CubiCasa5K raster counts are about two thirds of all questions.
- The eval also reports refusals (always 0%), evidence coverage, calibration, cost and latency for every run.
- **A scoring slip, fixed at gate d29.** Up to gate d28 the scorer read the first number of a feet-and-inches gold,
  so `2'-10"` was read as 2 and a right answer of 0.864 m (= 2'-10") counted as wrong; '2 1/2' read as 2. From d29
  (`53d8348`) it reads feet-inch strings, fractions and mixed numbers; none of the 126,208 scored dataset golds reads
  differently, so only answers on US sheets move. Rows tagged with earlier commits keep the old scoring, which only
  ever under-counted them.
- **Evidence coverage** ("evidence on 99% of answers") = the share of answers that point at a location: a box,
  an outline, a measured line or an IFC GlobalId. It is not a check that the location supports the value. On the
  dataset we check box accuracy against ground truth ([Evidence](#evidence)); on uploads there is no such truth,
  and our internal audit found located evidence that did not support the value ([below](#internal-audit-a-random-sample-of-the-shipped-demo)).
- **Mode.** Every number is private (local models only, Gemini and Nemotron off) unless it says hybrid, and every
  number names its commit. Numbers from different commits, inputs or modes are not interchangeable.
- Paired comparisons use a cluster bootstrap over drawings. "W / L" counts questions that flipped right / wrong
  between two runs. Differences under about 2 points on dev, or ±1 question on the small real-drawing sets, are noise.

## The Stoa dataset: splits and the sealed test

**Splits.** `python -m stoa.eval.splits --write` builds fixed splits from `questions.jsonl` (seed 20261113). All
questions about one drawing group (sibling formats, the floors of one Swiss site, the models and sheets of one IFC
project) land in the same split. Where the dataset asks the same question in English and in the drawing's language,
an eval split keeps one of the two. Splits: `train` (gates and LoRA training only), `smoke`, `dev` (2,215 questions),
`test` (2,199 questions, sealed; 2,000 of them are scored, the 199 free-text answers are not), plus generated generalisation sets: `hard` (rewordings, translations, compositions),
`hard2` (operators the dataset never asks: averages, ratios, thresholds, yes/no), and the held-out `hard2q`, `hard2r`
and `hard3`.

**The sealed test.** Only the gatekeeper ran `test`, and only aggregates were read. It was run twice in total:

| Run | Commit | Micro | Weighted | AUROC | ECE | $ per 1,000 Qs | p95 latency |
|---|---|---|---|---|---|---|---|
| Test 1 | `95f2277` | 87.4% | 79.8% | 0.92 | 0.017 | $0.28 | 35 s |
| Test 2 (run `20261114-081200-pipeline-test-b391`) | `bb94fe4` | **87.7%** | **79.7%** | 0.949 | 0.021 | **$0.029** | 1.95 s |

Micro at test 2 = 1,754 right of the 2,000 scored questions (of 2,199). Test 2 vs test 1, paired: +0.3 [−0.1, +0.8], 10 W / 4 L, so accuracy is unchanged within noise. The gain between the
runs is cost (10× cheaper, LLM calls per question 0.90 → 0.44) and latency.

Test 2 by source: Swiss Dwellings 99.0% (800; 100% of counts, 98.0% of areas), IFC models 100% (397), CubiCasa5K
raster counts 70.4% (803, the weakest bucket), IFC-Bench free text 61.8% (199, LLM-judged, not in accuracy). Both
runs used `--solver-arg cc5k=0`: the pretrained CubiCasa5K detector is off, because it was trained on CubiCasa5K
drawings that may overlap the test.

Two things about test 2 are not perfectly clean:

1. **`XCHECK_CAP`.** The confidence cap for a disagreeing cross-check (`XCHECK_CAP = 0.7` in `stoa/pipeline.py`) was
   chosen from test 1's aggregates. It changes confidences only, never answers. So accuracy is unaffected, but test
   2's calibration numbers are slightly optimistic.
2. **IFC 98.2% → 100%.** Test 1 had 7 confident misses on one IFC model. The failure mechanism (not the questions)
   was passed to the IFC reader's owner, who reproduced and fixed it on models outside the test split. Treat the
   IFC gain on test 2 as "a seen failure, fixed".

Sealed generalisation sets on test 2's commit `bb94fe4`: `hard2r` (new operators in a fresh wording: es/et/pl and new
English) 63.0% → 80.4% (20 W / 0 L vs the test-1 code), and `hard3` (derived quantities: prices, rates, constants)
34.6% → 62.2%. On `hard3` the confidence is still unreliable (ECE 0.230; answers at confidence ≥ 0.8 are 73% right).

**Later code on the open splits** (gate d14, commit `0b6f25d`, run `20261114-152554-pipeline-dev-3083`): dev 90.1%
micro / 83.7% weighted, hard 85.2%, hard2 89.7%, IFC models dev 354/354.

**Caveat.** The dataset's questions are generated from templates. Stoa's own test (their files, their questions,
typed into our app) is harder and different. Treat the dataset numbers as an upper bound. The real-drawing exam
below is closer to what Stoa does.

## Stoa's own example sheets

Our main metric since Saturday 16:15. That afternoon Stoa sent four "tricky" sheets and the kinds of question their
own test asks: floor areas of rooms and of room types (e.g. by legend colour), linear metres of baseboards (room
perimeter minus door openings), counts of doors and windows, slab area by slab type, and, "to impress us", piping
plans. The sheets are clean, large-format vector CAD drawings with a text layer, in metric (1:200, 1:50, mm) and
imperial units (1/4" = 1'-0", 1" = 40'-0", ft-in, square feet):

- UK Department for Education, special school plan level 00 (A2, 1:200, line-only CAD with room labels).
- USACE M-101A piping plan of a pressurized hydrant fuelling system (pipe sizes, valves, pumps, gauges, pipe supports).
- USACE S-100F foundation and slab plan (1:50, mm dimension strings, slab-type callouts, control joints).
- AFCEC permanent party dormitory, second floor plan (colour legend by module type, printed room and area tables).

We found the public originals of all four (US federal works are public domain; the DfE sheet is under the Open
Government Licence) and their sibling sheets from the same standard-design sets. One agent wrote questions in Stoa's
kinds with reference answers that are either printed on the sheet (tables, dimension strings, tags, schedules) or
measured by hand at the printed scale, each row saying which. The files are not bundled here.

- **stoa-like dev** (the four sheets, as vector PDF and as Stoa's own 10,000–14,000 px PNGs): open; everyone could
  look at these sheets and fix things.
- **stoa-like sealed** (sibling sheets of the same sets, never looked at for tuning): run only by the gatekeeper,
  aggregates only.

Each file is uploaded alone under a hashed name through the `upload` solver, with Gemini escalation off.

Until gate d17 the dev set had 55 scored questions; from d18 on it has 90 (16 questions our team lead wrote in Stoa's style,
and USACE door counts and toilet skirting, were added), and the sealed set grew from 15 to 32.

| Commit (gate) | stoa-like dev | stoa-like sealed |
|---|---|---|
| `504060a` (d16) | 43.6% (24 / 55; areas 3 / 16) | 40.0% (6 / 15) |
| `4e0a4db` (d17) | 47.3% (26 / 55; 4 W / 2 L) | 40.0% (6 / 15) |
| `517b67b` (d18) | 50.9% (28 / 55); **47.8% (43 / 90)** on the full set; mean latency 70 → 19 s per question | 31.3% (10 / 32) |
| `5a4b058` (d19) | 65.6% (59 / 90; 16 W / 0 L); 0 wrong at confidence ≥ 0.8; cold cache: mean 7.1 s, p95 25.6 s, max 29.5 s | 46.9% (15 / 32; 7 W / 2 L) |
| `68cec39` (d20) | 71.1% (64 / 90; 5 W / 0 L); 0 wrong at confidence ≥ 0.8; evidence on 96%; cold cache: mean 13.2 s, p95 44 s, max 67 s | 46.9% (15 / 32); grown set 32.3% (21 / 65) |
| **`76f8f94` (d22)** | **81.1% (73 / 90; 10 W / 1 L)**; 0 wrong at confidence ≥ 0.8; evidence on 97%; cold cache: mean 7.6 s, p95 28 s | **50.0% (16 / 32)**; grown set **40.0% (26 / 65)**; 39.8% (41 / 103) |
| `1795365` (d23) | 81.1% (73 / 90; 0 W / 0 L); 83 / 117 on the grown open set (1 W / 0 L) | 50.0% (16 / 32); 26 / 65; 40 / 103 (1 L: a count at confidence 0.34); **final set 41.3% (59 / 143)**, of which 19 / 40 on four unseen school sheets |
| `a486439` (d25) | 84.4% (76 / 90); 0 wrong at confidence ≥ 0.8; evidence on 97%; 118 / 150 on the open set grown to 150 (areas 54 / 71, lengths 32 / 40, counts 32 / 39; 97 / 122 on d23's rows: 14 W / 1 L); mean 31 s, p95 125 s per question at machine load 280–315 | 62.5% (20 / 32); 49.2% (32 / 65); 50.5% (52 / 103); 53.8% (77 / 143); 51.4% (89 / 173) on the set grown to 173 |
| `ed95f26` (d26) | 86.7% (78 / 90); 0 wrong at confidence ≥ 0.8; evidence on 98%; 158 / 207 on the open set grown to 207 (57 new rows: 19 piping questions on the M-101A sheet, asked on its PDF, on its PNG and on the whole sheet set, 11 / 19 right on each; 125 / 150 on d25's rows: 7 W / 0 L); mean 17 s, p95 54 s per question | 59.4% (19 / 32); 47.7% (31 / 65); 49.5% (51 / 103); 54.5% (78 / 143); 53.2% (92 / 173) |
| `12b136d` (d27) | 86.7% (78 / 90), the same answers on the 90; 0 wrong at confidence ≥ 0.8; evidence on 98%; 178 / 210 on the open set grown to 210 (175 / 207 on d26's rows: 19 W / 2 L; areas 62 / 74, lengths 57 / 67, counts 59 / 69); mean 5.4 s, p95 22 s per question | 59.4% (19 / 32); 49.2% (32 / 65); 52.4% (54 / 103); 58.0% (83 / 143); 57.2% (99 / 173) |
| `e3a1d9f` (d28) | 86.7% (78 / 90), the same answers on the 90; 25 answers at confidence ≥ 0.8 (11 at d27), 1 of them wrong (a pressure-gauge count on Stoa's PNG of the piping plan, 20 against 21, at 0.80); evidence on 99%; 190 / 216 on the open set grown to 216 (185 / 210 on d27's rows: 8 W / 1 L; areas 64 / 76, lengths 62 / 71, counts 64 / 69); mean 6.4 s, p95 25 s per question | 59.4% (19 / 32); 49.2% (32 / 65); 55.3% (57 / 103); 60.1% (86 / 143); 58.4% (101 / 173) |
| **`f2cd8f1` (d29)** | **86.7% (78 / 90)**, the same answers on the 90; 25 at confidence ≥ 0.8, the same 1 wrong; evidence on 99%; 198 / 249 on the open set grown to 249 (190 / 216 on d28's rows: 0 W / 0 L; the 33 new rows 8 / 33, 5 with the d28 code: the DfE west-facade windows 18, all DfE doors 115, the A-103 doors 2; areas 64 / 76, lengths 63 / 79, counts 71 / 94); mean 7.6 s, p95 32 s per question | 65.6% (21 / 32); 52.3% (34 / 65); 54.4% (56 / 103); 59.4% (85 / 143); **58.4% (101 / 173)** (1 real W / 3 real L: window counts on one EFA ground-floor sheet over by a third; 2 W only from the new scorer) |

Gate d20 added 10 questions on a fitness-centre module sheet to dev (66 / 100 with them) and grew the sealed set to
65 questions with line-only school plans and Stoa-style image copies of the sheets; the common rows are compared above.
By d22 the open set had 117 scored questions (82 / 117 right at d22). The 27 added after d19, mostly on sibling sheets,
are harder: 9 / 27 at d22, 4 / 27 with the d20 code. The sealed set reached 103 questions at d22 and its final 143 at d23 (40 questions on four unseen EFA school sheets in the
style of the DfE sheet that nobody tuned on: 5 / 40 at d20, 19 / 40 at d22 and d23). Gate d21 was not merged (a regression on
lone dataset uploads); d22 = d21 + its fix. What d21 / d22 changed: "1:200 @ A1" printed on an A3 page is read at
the true 1:400; printed dimensions next to a traced room are used for its sides; skirting questions that ignore door openings no longer ask about doors; sums of
printed room areas count each room number once (plans that show the floor twice). Runs: d22
`20261114-180020-upload-stoa_like_dev-e4e0`, `-0b57` (all caches off), `-180020-upload-stoa_like_sealed-9a12`.

What d24 and d25 changed, again general readers: a sheet is split into its views (plans, ceiling plans, 3D views, key
plans), so a count or an area is read in the view the question means, and a question that names no view on a sheet
with several asks which one; printed area tags ("U106 - 468 SF") and area tables are matched by room name, wing and
type; door and window counts come from the vector door swings and window symbols (one evidence box per door, leaf or
window), with a small door detector for images; and when a hosted vision reading disagrees with a value read from the
drawing's own geometry or text, the drawing's value is kept. Both gates were merged for their accuracy but not shipped:
the time per question doubled (d24: mean 9 → 16 s; d25: 31 s at a higher machine load) because readers waited up to
four minutes for the shared door-detector service. Runs: d25 `20261114-190624-upload-stoa_like_dev-34bd`,
`-190623-upload-stoa_like_sealed-4bd6`.

What d26 changed: printed area tables are found for more wordings ("… in m²?", "the west wing's total area", "sqm"), which
fixed the dorm plan's floor totals on its PNG copy. Runs: d26 `20261114-195039-upload-stoa_like_dev-6931`,
`-195039-upload-stoa_like_sealed-f044`. Time per question is not comparable across gates: other jobs loaded the shared
machine differently during each run.

What d27 changed: a deterministic piping-plan reader written by our teammate Erin (tags, callouts, pipe supports and
straight header runs read from the PDF's own text and lines) now answers first on uploaded PDFs. The piping questions
on the M-101A sheet went from 23 to 38 of 39 on its two PDF copies (the sheet alone and inside its 156-page set), and
stay at 13 of 21 on its PNG copy. Door counts on an image sheet read the image's traced vector copy when the door
detector cannot answer (the fitness-centre module doors on its PNG copy, 2 more right), and an inch mark in the cited
line beats a model's foot mark. Its two losses are on a JPEG copy of a school sheet (a room area and the building
length). Runs: d27 `20261114-201523-upload-stoa_like_dev-b617`, `-201523-upload-stoa_like_sealed-4a76`, replay
`-202054-upload-stoa_like_dev-7a85`.

What d29 changed: fixes made from our expert's hand-measured answers on the DfE sheet (see the Human audit): the
windows on a named facade are counted from the window symbols drawn on that side (18, as she counted; 0 before), and
every door symbol of the sheet is counted (115, as she counted; 101 before). Of the 33 questions added to the open set
at d29, 8 are right (5 with the d28 code); the other 216 are unchanged. On the held-out sets the changes are neutral
within noise: siblings 101 = 101 (one real win, three real losses, all window counts on one EFA ground-floor sheet,
over by a third), other real drawings 62 -> 61. From d29 the scorer also reads feet-and-inches and fractions (see How
we score). Runs: d29 `20261114-214036-upload-stoa_like_dev-5459`, replay `-214851-upload-stoa_like_dev-d86d`, wild
`-214944-upload-wild_sealed-2a60`.

What d28 changed: the piping reader also reads image sheets (it runs on the sheet's traced text and lines), so the
piping questions on Stoa's PNG copy of M-101A went from 22 to 27 of 31 (its PDF copy stays at 26 of 27), and a phone
photo of a school sheet in the development set went from 2 to 4 of 5 (a room area got worse: 56.74 m² against 55.0,
just outside 2%). Confidence moved up with it: 78 of the 216 open answers are at 0.8 or more (51 at d27), and 2 of
them are wrong (0 at d27). On the held-out sibling sheets areas carry the score (72 / 108 right), while counts
(14 / 30) and lengths (15 / 35) on drawings nobody tuned on stay weak. Runs: d28
`20261114-211132-upload-stoa_like_dev-a506`, wild `-212458-upload-wild_dev-3031`, `-211927-upload-wild_sealed-66aa`.

What d19 changed, all general readers rather than rules for these four sheets: printed area tables are read by their
TOTAL row or summed by the asked type; piping parts are counted by their distinct tags; slab areas by type are traced
around the slab callout; "how many linear metres of skirting" is a length, not a count. The gain carried over to the
held-out sibling sheets (+5). Runs: d19 `20261114-170611-upload-stoa_like_dev-9e13`, `-170632-upload-stoa_like_dev-e1b1`
(all caches off), `-170611-upload-stoa_like_sealed-642e`; d20 `20261114-171949-upload-stoa_like_dev-cbf3`, `-2169` (all caches
off), `-171949-upload-stoa_like_sealed-8a5b`.

**Three systems on the same 90 dev questions** (same split and scorer): the app with the hosted escalation on (code
`504060a`: Gemini 3.1 Pro for unsure answers, Nemotron verify), Gemini 3.1 Pro asked directly with the drawing, our local
Qwen3-VL-8B asked directly, and our pipeline with no hosted model at d19, d20 and d22.

| Stoa kind (questions) | App, escalation on (`504060a`) | Gemini 3.1 Pro direct | Qwen3-VL-8B direct | Local only, d19 | Local only, d20 | Local only, d22 |
|---|---|---|---|---|---|---|
| **All (90)** | 55 (61%) | 55 (61%) | 23 (26%) | 59 (66%) | 64 (71%) | **73 (81%)** |
| Areas by room (10) | 1 | 0 | 0 | 4 | 4 | 6 |
| Areas by type, from a printed table (15) | 8 | 13 | 4 | 13 | 13 | 13 |
| Baseboard lengths (3) | 1 | 0 | 0 | 1 | 1 | 3 |
| Door counts (2) | 0 | 0 | 0 | 0 | 0 | 1 |
| Slab area by type (7) | 2 | 3 | 0 | 2 | 2 | 3 |
| Piping: tags and pipe sizes (20) | 18 | 17 | 13 | 19 | 19 | 19 |
| Other: label counts, overall dimensions, slab data (33) | 25 | 22 | 6 | 20 | 25 | **28** |
| Answers with evidence | 98% | none | none | 94% | 96% | 97% |
| $ per question: first question on a new sheet / each further question (first three columns: every question; measured on the first 72) | $0.018 | $0.010 | $0.0001 | $0.011 / $0.0008 | $0.011 / $0.0009 | $0.010 / $0.0006 |

The first three columns were run once, at 17:00; the local-only columns are the gate runs. Our cost has two numbers:
reading a new sheet's text layer (OCR and local-model reads of large images) is paid once per file, so the gate's
all-caches-off run (`STOA_TEXT_CACHE=0`) gives the cost of a first question on a new sheet, and its normal run the
cost of each further question on a sheet already read. Accuracy is the same in both (73 / 90 at d22). A hosted
model asked directly pays its full price on every question. Between the 17:00 runs and the gate runs the reference
answers of two rows were corrected (a skirting length and a kitchen area), so a difference of one or two questions is
within noise. Per-kind counts for the local-only columns are the eval's strict scoring (one more room area is right
under a second accepted reading added later). The escalation column was not re-run on d19 / d20 code.

Room areas, Stoa's first priority, are still our weakest kind on these sheets, and every other system does worse.
Line-only CAD sheets have no room fills, so a room is found by its label and traced from the wall lines at the printed
scale; door swings and door gaps break the outline (the DfE laundry comes out right, the kitchen leaks into the next
room). Slab areas by slab type and baseboard lengths need the slab callouts or the door openings joined to the room
geometry, which is where most of the remaining misses are.

## Scans of the example sheets

Stoa expects scanned and photographed prints. To measure what a scan costs us, 91 of the questions on the stoa-like
development sheets are also asked on simulated scans of their sheet (v2 simulator: the page frame kept, the content
skewed inside it, blur and faded lines, at 200 or 300 dpi), once as a PNG and once as an image-only PDF, plus 8
questions on real scans of historical sheets. These are scans of sheets we tuned on, so they show what scanning
alone takes away. Gate d27 (`12b136d`, private, run `20261114-203122-upload-scan_dev-e4ed`), the eval's scoring:

| Upload condition | Right | Areas | Lengths | Counts |
|---|---|---|---|---|
| Scanned PNG | 38.5% (35 / 91) | | | |
| Image-only PDF (scanned) | 41.8% (38 / 91) | | | |
| Both scanned conditions | **40.1% (73 / 182)** | 31.0% (26 / 84) | 42.0% (21 / 50) | 54.2% (26 / 48) |
| Real scans of historical sheets | 5 / 8 | | | |

Paired with the same question on the vector PDF (68 pairs per scanned condition, the
workspace repository's `experiments/x1/cond_table.py`, which also accepts a second reading where a row has one): the vector sheet is right
on 60 of 68, the scanned PNG on 23 and the image-only PDF on 26; a question right on the scan but wrong on the vector
sheet happened once. Against a clean PNG export of the same sheet (23 pairs): 20 right on the export, 12 on its scan.
Room areas suffer most. The likely causes: on a scan the room labels and the printed scale must be read by OCR
instead of from the PDF's text layer, and the faded wall lines that the area tracer follows break up. The held-out
sibling sheets have the same scan set (sealed; run by the gatekeeper only).

## Real drawings from outside the dataset

This was our main metric from Saturday noon until Stoa's example sheets arrived. Stoa told us they would upload their
own files into our app and type their own questions, mostly about areas, then lengths, then counts.

**How the set was built.** One agent collected public drawings that are not in the Stoa dataset: ArchiCAD IFC houses
(opensourceBIM test files), Finnish scans from 1949 and 1971 (Finna), German, Italian and Australian CAD sheets and
Floorplanner exports (Wikimedia Commons), and US drawing sets (OpeningDesign). Some files were also rendered to PNG or
turned into simulated phone photos. Every file was checked against the dataset by SHA-1 and by project id. Reference
answers are printed values on the drawing or measurements by hand, and each row records which. Each file is uploaded
alone under a hashed name, with no sibling files and no folder hint, through the `upload` solver, with Gemini
escalation off. Sources and licences: `experiments/x1/SOURCES.md` in the workspace repository. The files are not
bundled here.

- **wild-dev** (25 files at gate d14, 114 scored questions; open): everyone could look at these files and fix things.
- **wild-sealed** (12 other files, 82 scored questions; held out): kept outside git, run only by the gatekeeper,
  aggregates only, never used for tuning. 45 of its questions are on other versions of OpeningDesign drawing sets whose
  other files appear in the dataset without questions; 37 are on files fully outside it.

**Progress** (solver `upload`, files alone, Gemini off):

| Commit (gate) | wild-dev | wild-sealed |
|---|---|---|
| before `b1ec5a0` | 35.8% | |
| `b1ec5a0` (d11) | 39.3% | 31.7% (26 / 82, same code) |
| `5fc4653` (d12) | 50.9% (13 W / 0 L) | 7 W / 0 L on 48 common rows |
| `91f40d4` (d13) | 56.1% | **43.9%** (36 / 82, 12 W / 2 L) |
| `0b6f25d` (d14) | 64.9% (74 / 114, 10 W / 0 L, CI [+2.0, +13.8]) | 43.9% (1 W / 1 L) |
| exam grown to 44 files: `fc0927a` (= d14 + docs) | 59.6% (81 / 136) | 48.6% (51 / 105) |
| **`504060a` (d16)** | **64.0%** (87 / 136; areas 56, lengths 70, counts 62; 0 wrong at conf ≥ 0.8) | **49.1%** (55 / 112; areas 47, lengths 44, counts 58) |
| exam grown again: `517b67b` (d18) | 70.2% (118 / 168; the 114 rows from d14 78.1%, the 54 rows added since 15:30 53.7%) | 49.2% (59 / 120) |
| `5a4b058` (d19) | 69.6% (117 / 168; 0 W / 1 L) | 49.2% (59 / 120; 2 W / 2 L) |
| `68cec39` (d20) | 69.6% (117 / 168; 0 W / 0 L) | 48.3% (58 / 120; 1 L: a last-resort image guess at confidence 0.05) |
| `76f8f94` (d22) | 72.0% (121 / 168; 4 W / 0 L) | not complete at the merge (76 of 124 rows: 0 W / 0 L against d21) |
| `a486439` (d25, runs `-190624-upload-wild_dev-58f0`, `-wild_sealed-9599`) | 70.8% (119 / 168; 1 W / 3 L against d22; areas 35 / 50, lengths 51 / 68, counts 33 / 50) | 50.8% (61 / 120) |
| `12b136d` (d27, runs `-202557-upload-wild_dev-ac31`, `-202242-upload-wild_sealed-e741`) | 70.8% (119 / 168; 0 W / 0 L against d25) | 50.8% (61 / 120) |
| `e3a1d9f` (d28, runs `-212458-upload-wild_dev-3031`, `-211927-upload-wild_sealed-66aa`) | 70.8% (119 / 168; 0 W / 0 L against d27) | 51.7% (62 / 120) |
| **`f2cd8f1` (d29, run `-214944-upload-wild_sealed-2a60`)** | not run at the merge | **50.8%** (61 / 120; 1 real loss: a length answered with a raw string of several dimensions at confidence 0.65; 1 W / 1 L from the new scorer) |

Gate d14 runs: `20261114-152311-upload-wild_dev-8df4` and `20261114-152311-upload-wild_sealed-dcd8`.

By question type at `0b6f25d`:

| Set | Areas | Lengths | Counts | Wrong at confidence ≥ 0.8 |
|---|---|---|---|---|
| wild-dev | 57.6% | 74.5% | 58.8% | 0 |
| wild-sealed | 40.0% | 36.0% | 59.1% | 4 of 7 confident answers |

What carried over and what did not:

- Most gains carried over: the held-out set went from 31.7% to 43.9% at `91f40d4`. The big general steps were these.
  When every PDF reader declines a vector PDF (text drawn as outlines, no room fills), the page is now rendered and
  read as an image instead of ending at a guess. Rooms on line-only CAD sheets are found by their label and measured
  from the wall lines at the printed scale.
- The last step did **not** carry over: d14 lifted wild-dev lengths from 55% to 74%, and the held-out lengths stayed at
  36%. Those rules (opening labels read as width over height, level differences, printed scale and DPI) fit one family
  of files in the development set.
- **The larger exam.** After d14 the exam grew to 44 files (wild-dev 136 scored questions, wild-sealed 105). The same
  code (`fc0927a` = `0b6f25d` + docs) scores **59.6%** on wild-dev (81 / 136; areas 48.7%, lengths 71.9%, counts 52.5%;
  run `20261114-154002-upload-wild_dev-b4c4`; a second run gave 80 / 136) and **48.6%** on wild-sealed (51 / 105; run
  `20261114-154832-upload-wild_sealed-a46a`). On the 22 development questions added on two new files, d14's own
  changes lost 2 and won 0. In short: we were partly fitting the files we looked at.
- Gate d15 was rejected and is not in this code. It lifted wild-dev further, but it changed which IFC area is
  reported and lost 23 of Stoa's own reference answers on a dataset model.

By file kind on wild-dev (`0b6f25d`): ArchiCAD IFC houses 80% (25 questions), vector PDFs with outlined text 80% (11),
200 dpi renders of vector PDFs 80% (11), unscaled vector PDFs 100% (7). Weakest: phone photos of prints 35% (19),
200 dpi renders of CAD sheets 29% (7), raster-in-PDF sheets with a scale bar 40% (6), a German 1:100 CAD sheet
without room labels 50% (10).

## Dataset files uploaded the way a user would

These probes use dataset files, so they are easier than the real-drawing exam. All are from gate d14 (`0b6f25d`)
unless noted.

- **Plan with its CAD/IFC twins, under meaningless names** (784 dev questions): 97.3%.
- **Plan uploaded alone** (no twin): 83.8% (84.2% at `91f40d4`; 51.3% at `f1bb5d2` before room areas were read from
  IFC-generated plan PDFs without their model). Counts on those lone sheets stay weak, and lengths and volumes are
  flagged as unsure. A Swiss plan PDF without its DXF gets 96.1% of areas and 97.4% of counts from the PDF's vector
  paths alone.
- **Other formats** (182 questions, each file alone under a neutral name): 90.1%. Per format at `e87da4b`: DXF
  96.7%, IFC 96.7%, JPEG 76.7%, multi-page PDF sets 93.3%. A second set (150 questions): 74.0%. SVGs whose text was
  turned into outlines are answered with low confidence and the advice to export with text kept as text.
- **CAD files that don't use the dataset's layer names** (AIA, German or all-on-layer-0; 90 questions): 67.8%, with 0
  answers wrong at confidence ≥ 0.8 (26 before the layer mapping at `e214a67`). The same files uploaded together with
  their PDF: 100%. **DWG** copies converted with LibreDWG (60 questions): 100%.
- **Recognition from content** (`stoa/router/recognize.py`, no model call): 93.3% of 420 dataset files labelled by
  folder; vector formats 100%.

## Cost, latency and data governance

Where a drawing goes is a server setting. Each step of the pipeline, and what it sends where:

| Step | Runs on | What leaves the server |
|---|---|---|
| Recognising the file; geometry readers (IFC, CAD, PDF paths and text, schedules); OCR | the server's CPU | nothing |
| Local model (Qwen3-VL-8B + our LoRA, served by vLLM) | the server at `LLM_BASE_URL`: your own GPU, or one you rent | nothing, if you run that server |
| Gemini 3.1 Pro vision, only for counts below 0.7 and lengths below 0.6 confidence (`STOA_API_GEMINI`) | Google's Gemini API | an image of the sheet (for counts also its four quarters) and the question |
| Gemini 3.1 Pro planner, only for unsure questions of kinds no reader knows | Google's Gemini API | the question, a small image of the sheet and our readers' results |
| Nemotron 3.5 Super VL verifier, only for unsure answers without located evidence (`STOA_API_VERIFY`) | NVIDIA's hosted API | the question, our answer and reasoning, the sheet with the evidence marked and a crop of it |
| No-GPU mode (`STOA_MODE=api`): steps meant for the local model | Google's Gemini API (Gemini 3 Flash) | what the local model would see: page images and the question |
| Optional EU text model for free-text steps (`STOA_TEXT_BACKEND=verda`) | Verda Inference (EU) | text only |

In the web app the **Private** switch (top bar; off by default, remembered per browser, `?private=1` in the URL)
sends `private: 1` with each question. For that question the upload worker turns Gemini, Nemotron and the no-GPU
route off, falls back from a hosted free-text model to the local one (an `eu: 1` request keeps Verda's EU model),
and refuses steps that only exist as hosted models (the Nemotron second-opinion button) with a message.
Underneath, `STOA_PRIVATE=1` makes `stoa.llm` refuse every call to a hosted model before it is sent (our own
servers on loopback or a private network stay allowed; `STOA_EU_ONLY=1` also allows Verda), and the upload's
text-layer warm-up runs that way too, so a private question cannot leak through a background step. Each answer
card then reads "Stayed on our server" or names the hosted step it used. The switch's note shows the measured
trade-off below.

On the server, `STOA_PRIVATE=1` (or the pipeline argument `private=1`) turns every hosted step off for all
questions, and `STOA_EU_ONLY=1` with it still allows Verda's EU text model. The last line of every answer's
reasoning lists where its data went: `data_used: local`, plus `gemini`, `nvidia` or `verda` when used.

The API turns Gemini and Nemotron on by default when their keys are set (per server: `STOA_API_GEMINI=0`,
`STOA_API_VERIFY=0`; per request: `solver_args {"gemini": 0, "verify": 0}`). Private mode = both off and `STOA_MODE` unset.
Every eval number in this document, unless it says otherwise, ran that way. The gates from d24 on also ran with the door/window detector service up (`STOA_DET_URL`; not in this repository), so a fresh clone without it can differ on door and window counts on images.

**Measured per mode.**

| | Private | Hybrid (Gemini + Nemotron) |
|---|---|---|
| Real drawings, held out: right (105 questions, same code `fc0927a`, same rows) | 49.5% (52) | **64.8%** (68; 19 won / 3 lost) |
| Same rows: wrong answers at confidence 0.8 or more | 5 | 2 |
| Same rows: mean / p95 time per question | 7.7 s / 23 s | 28 s / 62 s |
| Real drawings, development set: $ per 1,000 questions (`ad739c9`) | $0.19 | $7.82 |
| Stoa's example sheets in the web app, Gemini step on (`1795365`, 17 questions, 3 rounds) | 12 / 17 (our reading before the Gemini step) | 11 / 17; median 8–12 s, slowest 77–102 s; 15 of 18 answers "expensive" |
| Stoa's example sheets, gate run (`1795365`): the 90 questions in Stoa's kinds; time over all development rows | **81.1%**; mean 9.5 s, p95 37 s | not re-run on this code (61% at `504060a`) |
| Same 90 questions, same code and rows (`a486439`, d25), the hosted answers replayed from the gate's cache | **84.4%** (76) | 82.2% (74; 1 won / 3 lost); 24 of the 90 answers called a hosted model |
| All 150 open stoa-like questions, same run pair | **78.7%** (118) | 76.7% (115; 1 won / 4 lost) |
| Same 90 questions, `ed95f26` (d26), replayed the same way | 86.7% (78) | 86.7% (78; 3 won / 3 lost); 27 of the 90 answers called a hosted model; $0.94 against $4.42 per 1,000 questions as recorded |
| All 207 open stoa-like questions, same run pair | 76.3% (158) | 77.3% (160; 6 won / 4 lost); $0.73 against $2.29 per 1,000 |
| Same 90 questions, `12b136d` (d27), replayed the same way | 86.7% (78) | **88.9%** (80; 2 won / 0 lost); 21 of the 90 answers called a hosted model; $0.82 against $3.24 per 1,000 questions as recorded |
| All 210 open stoa-like questions, same run pair | 84.8% (178) | 86.2% (181; 3 won / 0 lost); $0.50 against $1.59 per 1,000 |
| Same 90 questions, `e3a1d9f` (d28), replayed the same way | 86.7% (78) | **90.0%** (81; 3 won / 0 lost); 21 of the 90 answers called a hosted model; $0.82 against $3.25 per 1,000 questions as recorded |
| All 216 open stoa-like questions, same run pair | 88.0% (190) | 89.4% (193; 3 won / 0 lost); 23 answers called a hosted model; $0.58 against $1.66 per 1,000 |
| Same 90 questions, `f2cd8f1` (d29), replayed the same way | 86.7% (78) | **90.0%** (81; 3 won / 0 lost); 22 of the 90 answers called a hosted model; $0.87 against $3.34 per 1,000 questions as recorded |
| All 249 open stoa-like questions, same run pair | 79.5% (198) | 80.7% (201; 4 won / 1 lost); 31 answers called a hosted model; $0.63 against $1.81 per 1,000 |

The replayed rows (d25 to d29) measure the Gemini step only: a replay makes no new hosted call, and the Nemotron
check had no cached answers in those runs, so it was never called there (the gate logs called it "unavailable").
Nemotron's verdicts were measured once on their own, on 40 unsure answers (Saturday midday): next to evidence boxes
it mostly agrees with them (12 of its 20 agreements were right answers), but on answers without located evidence
its disagreement was informative (7 of 8 wrong). That is why it only halves the confidence of an evidence-less answer
it disagrees with, and never changes a value.

On real drawings from outside the dataset, the hybrid's gain transfers to held-out files (counts and lengths, see
[Hybrid upload routing with Gemini](#hybrid-upload-routing-with-gemini)). On Stoa's own sheets it does not yet: in our
web-app rehearsal the Gemini step replaced 6 right readings of ours (a baseboard length measured on the plan, a door
count read from the printed door labels) and kept 6 of our unsure readings where its own cross-check had the right value. The
better of the two readings would have scored 15 of 17. The fix (a value read from the drawing's own geometry or text
is never overruled by a vision reading; a disagreement is shown to the user as two candidates) is in `a486439`. To
measure it without new hosted calls, every gate from d25 replays the stoa-like questions with the hosted steps on,
served only from the answers those steps gave in earlier runs (`STOA_LLM_REPLAY=1`; a step with no stored answer
fails like an unreachable API, so this is close to, not exactly, live hybrid). On d25 the hosted step still lost
slightly; on d26 it broke even on the 90 and won 2 of 207 overall. Its wins are answers our image reader got wrong
(slab and foundation areas and a wing width on the slab and dorm plans); its d26 losses overrode values read from the
drawing's geometry that the arbitration did not yet protect (door counts from the vector door swings, a width from a
dimension string). d27 protects those readers too, and the hosted step wins 2 of the 90 and 3 of 210 and loses none
(table above). So on Stoa's sheets private mode gives up little: 2 of the 90 questions, at a quarter of the hybrid's cost. The gates' other rows ran with Gemini
off.

Latencies are from a shared 48-core workstation with other jobs running (load 80–170); the first question on a big
new sheet also reads its text layer once (about $0.010, and from a few seconds to over a minute on Stoa's sheets;
further questions on the same sheet: $0.0006).

**EU only, with confidential computing.** The local model is one setting (`LLM_BASE_URL`), so the same server can run
on a rented GPU in the EU. We ran the whole Stoa dataset (127,044 questions, `f551070`) on one Verda RTX PRO 6000 in
Finland: 17.2 minutes, €0.75 (€0.006 per 1,000 questions), and the dev questions inside the run scored 89.05%, exactly
as on our own machine. The instance was a confidential-computing type: we attested the GPU with NVIDIA's local GPU
verifier (`nv-local-gpu-verifier`) and set the GPU's confidential-computing ready state (`nvidia-smi conf-compute
-srs 1`), so drawings stay encrypted while in use. One pitfall: on these GPUs vLLM 0.31 needs
`VLLM_USE_V2_MODEL_RUNNER=0` (its v2 runner reads host memory directly and returns garbage). For free-text steps, Verda
Inference (EU-hosted models, `STOA_TEXT_BACKEND=verda`) split 31 of 32 multi-part questions in 9 languages correctly,
against 29 of 32 for a local 35B model. A production setup (vLLM and our adapters as a scale-to-zero Verda Serverless
Container, adapters in a private bucket in Finland) is scripted in `deploy/verda/PRODUCTION.md` but was not run.

**What stays where.** Uploaded files and the caches built from them (page renders, text layers, answers) stay on the
server that runs the web app: uploads under `STOA_UPLOADS` (default: a `stoa-uploads` folder in the system's temporary
directory), caches under `JUNCTION_SHARED` (default `.stoa/`). Delete those folders to remove them. Our code never
trains on uploads. What a hosted provider receives in hybrid or no-GPU mode falls under that provider's terms.

## Cost tiers

Stoa judges cost on three tiers, not in dollars. Every answer carries one (`stoa.cost.tier`):

- **Cheap:** no AI model. The answer was read from the geometry, the IFC model or the text layer, or it is a
  typical-answer prior.
- **In-between:** a few calls to a small model (our local 8B or a cheap hosted model).
- **Expensive:** a large hosted model (Gemini Pro class, Nemotron 120B) or a lot of model work: more than 30,000
  tokens, more than 6 calls, or more than $0.003 of hosted calls.

Dollar estimates use `stoa/cost.py`: one 96 GB GPU at $2.00 per hour (GPU time estimated from tokens at the
measured throughput, not from latency), CPU at $0.05 per hour, and hosted models at their list prices. Numbers
before `f1bb5d2` used $2.16 per hour, about 5% higher.

| Run | Cheap | In-between | Expensive | $ per 1,000 Qs | p95 latency |
|---|---|---|---|---|---|
| dev, `0b6f25d` | 67% (93.3% right) | 33% (82.6%) | 0% | $0.034 | 2.9 s |
| wild-dev, `0b6f25d` | 34% (83.3%) | 55% (58.5%) | 11% (14.3%) | $0.277 | 16.7 s |
| wild-sealed, `0b6f25d` | 14% (41.7%) | 70% (44.8%) | 16% (41.7%) | $0.868 | 18.7 s |
| Hybrid with Gemini, wild-dev (see below) | 21% | 13% | 66% | $7.82 | 76 s |
| Gemini 3.1 Pro direct, wild set v1 (`9e1c725`) | | | 79% | $7.59 | 32 s |

Share of answers that need no model call: 56% on the sealed test (97.0% right; `bb94fe4`), 44% on the whole dataset
(`f551070`), 67% on dev (`0b6f25d`). These runs read text layers that earlier runs had already built (cached per
file), so they show the cost of a question on a drawing already read; the first question on a new image or large
sheet costs more (on Stoa's example sheets $0.010 against $0.0006, see above).

### Cost by input

The runs above price the GPU, the hosted calls and each request's own wall time on one core. Our text recognition
(PP-OCRv6) is separate: it runs on several cores while it reads a sheet. One cold read of a 6,000 × 3,979 px scan
took 339 s and 1,594 CPU-seconds (about 4.7 busy cores), about $0.02 per scanned sheet, more than the GPU work of
the same question. Re-priced with that time counted at $0.05 per core-hour (the engine reports its wall time, which
stretches with machine load, so the CPU part is good to about a factor of two):

| Run | Input | Questions | Right | Median / p95 time | $ per 1,000 Qs | of which CPU |
|---|---|---|---|---|---|---|
| `ed95f26` (d26), open stoa-like questions, private | vector PDF | 136 | 78% | 4.6 / 41 s | $0.23 | $0.15 |
| | PNG picture | 87 | 73% | 3.9 / 99 s | $1.60 | $0.61 |
| | phone photo | 5 | 4 of 5 | 11 / 124 s | $2.01 | $0.48 |
| Same rows, hybrid (hosted answers replayed, priced as if called) | vector PDF | 136 | 77% | 4.8 / 20 s | $2.09 | $0.10 |
| | PNG picture | 87 | 79% | 2.5 / 52 s | $2.86 | $0.13 |
| `a486439` (d25), simulated scans of the tuned sheets (first simulator), private | scanned PDF | 75 | 29% | 9.0 / 342 s | $6.85 | $5.54 |
| | scanned PNG | 77 | 30% | 8.6 / 176 s | $5.68 | $4.81 |
| `a486439`, simulated scans of the held-out sibling sheets | scanned PDF and PNG | 106 | 22% | 10.2 / 394 s | $9.18 | $7.41 |
| `1795365`, our audit, nothing read before (cold) | vector PDF | 28 | 61% | 8.5 / 41 s | $0.24 | $0.17 |
| | PNG picture | 18 | 67% | 61 / 363 s | $3.38 | $1.17 |
| | simulated scans | 20 | 20% | 242 / 723 s | $10.32 | $6.24 |

The gate runs ask many questions per file, so after the first one a sheet's readings are cached; the audit's runs
start from nothing, so they show what a new upload costs. The $0.03 per 1,000 above is the dataset's own dev mix
(mostly vector drawings and IFC models, with their text already read), not the cost of an upload.

**API-only mode** (`STOA_MODE=api`, no GPU; local-model steps go to Gemini 3 Flash). On 24 dev questions that use
the local model: 81.3% (13 / 16 scored) at $3.62 per 1,000, against 87.5% (14 / 16) at $0.22 locally. For the full dev
mix this is an estimated $0.56 against $0.030 per 1,000. The loss is the CubiCasa counting LoRA, which has no hosted
twin. The sample is small (runs `20261114-122327-…-b8ef` and `20261114-122401-…-735b`).

## Calibration

The confidence of an answer is the measured hit rate of the method that produced it, on `train`. Checks then lower
it: new wording, a translation, two readers that disagree, a wrong-shaped answer, or a prior answer.

| Run | AUROC | ECE | Share at conf ≥ 0.8 | Right at conf ≥ 0.8 |
|---|---|---|---|---|
| Sealed test 2, `bb94fe4` (slightly optimistic, see `XCHECK_CAP`) | 0.949 | 0.021 | 79% | 98.0% |
| dev, `0b6f25d` | 0.954 | 0.030 | 80% | 99.1% |
| hard2r (sealed), `bb94fe4` | 0.74 | 0.188 | 42% | 98.7% |
| hard3 (sealed), `bb94fe4` | 0.62 | 0.230 | | 73.0% |
| wild-dev, `0b6f25d` | 0.808 | 0.099 | 17% | 100% |
| wild-sealed, `0b6f25d` | 0.649 | 0.114 | 9% | 42.9% (3 of 7) |

On the dataset, confidence is well calibrated. On unseen wording (`hard2r`) it is too cautious. On held-out real
drawings it is weak: only a few answers are marked sure, and 4 of the 7 that were are wrong.

## Evidence

- On dev at `0b6f25d`, 99.4% of answers carry evidence and 90.1% are located on a 2D drawing. 97.3% of area answers
  draw the room outline, and 100% of area sums list terms that add up to the answer (281 sums).
- **Box accuracy.** Boxes taken from source geometry (CAD, IFC, PDF text) are marked `exact`. We checked them against
  ground truth (DXF entities mapped onto the PDF, or the IFC plan's SVG twin): 100% of 11,952 boxes on dev are on the
  right object (also 100% of 6,571 on `hard`). Counts whose located objects match the answer one-to-one: 99.3% of 453.
  Estimated boxes (detector, segmentation, VLM) are marked approximate and drawn dashed. They exist only on CubiCasa
  plans, which have no ground truth: 55 of 60 were right when checked by hand.
- Whole dataset on Verda (`f551070`): 311,694 evidence boxes; the same box check found 100% on the right object.
- Sealed test 2 (`bb94fe4`) predates most of this work: 63.2% of answers with evidence, 53.2% located in 2D. Counts
  whose located objects equal the answer: 99.8% of 486.
- Real drawings (`0b6f25d`): evidence on 73.2% of wild-dev answers (53.5% located in 2D) and 71.1% of wild-sealed.
- Uploads of Stoa-style sheets (`1795365`, our internal audit's random sample, private): 43 of 50 answers had located
  evidence, but only 7 of 10 count answers with evidence had as many located objects as the count, and located
  evidence did not stop a slab-area question being answered with an elevation label ("TOP OF SLAB EL. = 10000").
  The dataset's box checks above do not carry over to uploads.

## Baselines

| Baseline | Set | Result | Commit / run |
|---|---|---|---|
| Train prior alone (most common answer) | dev | 26.9% micro / 41.0% weighted | `20261113-235744-prior-dev` |
| Qwen3-VL-8B asked directly (`vlm_direct`) | dev | 32.6% micro / 44.8% weighted | `2ba8914` |
| Our pipeline | dev | 90.1% micro / 83.7% weighted | `0b6f25d` |
| Gemini 3.1 Pro asked directly | wild set v1 (125 Qs incl. 25 on IFC that image-only methods can't read) | 43.8% (counts 80%, lengths 36%, areas 21%), no evidence, 0% at conf ≥ 0.8 | `20261114-145032-vlm_direct-wild-07e2` |
| Local Qwen3-VL-8B asked directly | same | 27.7% (counts 53%, lengths 15%, areas 21%) | same session |
| Our pipeline at that time | same set (109 scored) | 35.8% (counts 38%, lengths 28%, areas 46%) | before `b1ec5a0` |

The Gemini figure is on the first, 125-question version of the real-drawing set, not on the 114 wild-dev questions
above. It shows the pattern that shaped our routing: a frontier model counts well, and our geometry readers measure
areas much better.

Our counting LoRA, on the CubiCasa buckets: dev counts 50.4% (base 8B) → 73.9% (LoRA). On the sealed test 1 it added
+8.8 points to CubiCasa counts [+6.0, +11.9] (`95f2277`, pipeline with vs without the LoRA). On the same counts it
beat a 4× larger local model and hosted Nemotron 3.5 Super VL.

## Hybrid upload routing with Gemini

Captain's decision on Saturday afternoon: areas go to our readers first (they beat Gemini 46% to 21% on real-drawing
areas). Counts go to Gemini 3.1 Pro vision when no structured count reaches 0.7, and lengths when no structured
reader reaches 0.6. Gemini must return boxes for what it counted (up to 200 are kept; an answer with fewer marks than its
count says "showing k of n counted items"), or the measured endpoints. Its confidence comes
from agreement with our estimate or with a second sample, never from its self-report.

On wild-dev (114 questions, same code, runs `20261114-151435-upload-wild_dev-180d` with Gemini on and
`20261114-151437-upload-wild_dev-d094` with it off; hybrid code `ad739c9` on the routing branch):

| | Gemini off | Gemini on |
|---|---|---|
| All | 42.1% | **72.8%** (35 W / 0 L) |
| Counts | 61.8% | 97.1% |
| Lengths | 29.8% | 78.7% |
| Areas | 39.4% | 39.4% |
| Wrong at conf ≥ 0.8 | 1 | 0 |
| $ per 1,000 Qs / p95 latency | $0.19 / 12 s | $7.82 / 76 s |

Caveats. The branch did not yet have the other fixes, so "Gemini off" here is below the gate's 56.1–64.9%.

**Confirmed on the held-out files** (night `fc0927a`, the hybrid as the API ships it: Gemini escalation and Nemotron
verify on, solver `upload`): wild-sealed **64.8%** (68 / 105) against 49.5% (52 / 105) with Gemini off on the same code
and rows (19 won / 3 lost); areas 24 → 29 of 48, lengths 11 → 18 of 28, counts 17 → 21 of 29; wrong at confidence ≥ 0.8
5 → 2. 110 Gemini calls (the day's cap; 11 questions hit it and ran Gemini off, so 64.8% is a slight lower bound), mean
latency 28 s, p95 62 s (Gemini off: 7.7 s / 23 s). So the escalation's gain transfers to unseen files, while most of our
later deterministic tuning did not (development rows we had looked at: 69%; newer unseen rows: 36%). Phone-photo counts
with tiles and two samples take 60–260 s. Gemini is on by default in the API and the web app. It is off in every
gate and in every other number on this page.

## The whole dataset on one Verda GPU

Pipeline at `f551070`, clean configuration (no CC BY-NC weights, no hosted APIs), on all 127,044 questions: **82.6%
of 126,208 scored**, 0 errors, 0 refusals. 44% needed no model call. It took 17.2 minutes on one Verda RTX PRO 6000
(confidential computing) for €0.75. The dev questions inside the run scored 89.05%, exactly as on our own machine with
the same commit. By bucket: CubiCasa counts 74.7% (85,514: train 74.7%, unused 74.7%, dev 73.5%, so the LoRA did not
overfit its roughly 3,900 training drawings), Swiss counts 99.9% and areas 98.0% (37,584), IFC models 99.9–100% (3,062).
Calibration: ECE 0.024, AUROC 0.925; 71% of answers at conf ≥ 0.8, 97.0% of those right. The sealed test rows inside
the run were read as one aggregate only (87.4%). Scripts: `deploy/verda/`.

## Known weaknesses

From the real-drawing exam (above) and from our red team. The red team wrote 992 adversarial questions in 26 families
overnight, each family aimed at the weakness the last one found.

- **Stoa's own kinds of question are the hardest, and our gains on their four example sheets carry over only
  partly.** On the four sheets we answer 86.7% (78 / 90, local models only, gates d26 to d29; 81.1% at d22). On held-out
  sibling sheets of the same sets: 59.4% on the first 32 questions, 49.2% (32 / 65) on the grown set, 58.0% (83 / 143)
  on the set as it stood at d23 and 58.4% (101 / 173) on the final one (d28 and d29; 57.2% at d27, 41.3% on the 143 at d23;
  by kind at d28: areas 72 / 108, counts 14 / 30, lengths 15 / 35); the 25 sibling
  questions added to the open set late on Saturday were 4 / 25 right at d19. On 15 unseen line-only school plans in the
  style of the DfE sheet, 78 of 129 room-area, perimeter and label-count questions are right at d27 (68 at d25, 50 at
  d23, 17 at d19), and 21 of 29 skirting lengths that ignore door openings (18 at d25, 12 at d23, 2 at d22). On 202 questions about the five
  sibling plans of the DfE sheet itself (gold measured two ways), 96 are right at d27 (93 at d25, 94 at d23). The causes we measured at d19 (d22 fixes the second and the fourth): big rooms cut by dash-dot centre
  and overhead lines; sheets drawn at "1:200 @ A1" but stored as A3 pages
  (true scale 1:400); the middle of a solid wall strip taken as the room edge instead of its inner face (areas 1–6%
  too large); skirting questions that say "ignoring door openings" parsed as questions about doors. Room areas on
  line-only sheets (6 of 10 on the example sheets), slab areas by type (3 of 7) and door counts (1 of 2) are the
  weakest kinds (see above).
- **Real drawings are much harder than the dataset.** 43.9–49.2% on held-out real files with local models only (64.8% with the Gemini escalation), against 87.7% on the sealed
  dataset test. The weakest kinds: rendered or photographed copies of plans, totals on IFC models, counts on images
  that are not CubiCasa-style, and dimension-only sheets (room sizes only as dimension chains).
- **Confident misses on held-out real drawings** (4 of 7 confident answers wrong on wild-sealed). On the dataset,
  answers at conf ≥ 0.8 are 98–99% right; do not carry that trust over to arbitrary uploads yet.
- **Gains that only fit the files we looked at.** See "What carried over" above.
- **The table reader runs model-written code, in a sandbox.** For questions over schedules and extracted tables,
  the local model writes a few lines of pandas over the tables we extracted, and `stoa/sandbox.py` runs them: the code
  is checked against an allow-list first (no imports, no names starting with `_`, no methods that read, write or
  convert through files), pandas is replaced by a proxy that offers only what table questions use, a run-time hook
  refuses file opens outside Python's own library folders and every filesystem, process and network call while the
  code runs, and a 3-second deadline stops endless loops. The file read our internal audit did through `pd.read_csv`
  is refused (`tests/test_t2_tableqa_sandbox.py`). It still runs inside the server's own process, so one long call can
  use memory or time until it returns: put the API behind your own access control before you open it to untrusted
  users.
- **The root cause behind most confident misses the red team found:** our checks verify the number that was
  computed, not that it answers every part of the question (the storey, room, scope, kind of quantity, or how many
  answers were asked for). Examples, most of them fixed during the night: a typo in a filter word ('liivng room')
  dropping the filter at the same confidence; "how many X and how many Y" answered with X + Y; "1,200 euros" read as
  1.2; "in the whole building" ignored on a single-floor plan. On the final night build (`637a6df`), 397 of 397
  answers at conf ≥ 0.8 on the first 18 families were right. The open confident misses were on CubiCasa-style uploads.
- **Answer changes under rewording.** In a rehearsal on Saturday's noon code (7 site-engineer questions on 5 sheets,
  reworded and translated), 136 of 310 variants gave a different value from the original wording. These had no
  reference answers, so this measures consistency, not accuracy. The question parser is rule-based and multilingual,
  and a rewording can still send a question down another path.
- **Real Revit CAD files** have no closed room outlines (rooms are text tags, walls are lines). They need the line
  tracer, and are often answered with "missing: room outlines" instead of a number.
- **Latency with escalation.** Hosted Gemini counts on phone photos can take minutes.
- **Large multi-page PDF sets** (25+ pages) are not solved well: the right page is not always found.

## Human audit: the expert workbench

We built an internal expert "workbench" (not in this repository) for a human in the loop. On one page the expert
sees the drawing with our evidence (boxes, or the room outline and measured line we used), the question, our answer,
its confidence and the reference answer. She marks each answer as supported, wrong or can't tell, and can comment,
measure on the drawing (scale by two clicks or the printed 1:N), write her own hard questions, and add physics and
sanity rules. The page also measured how long it takes to verify an answer.

Ziying, a member of our team, checked 34 answers on Saturday
(11:45 to 16:40):

- 25 of 34 were supported by our evidence; 9 were wrong. The median time to verify one answer was **79 s** (63 s
  for a supported answer, 120 s for a wrong one). For areas and lengths, answers with the room outline or measured
  line drawn took a median of 56 s (5 items), with boxes 58 s (8 items), with no evidence 82 s (3 items).
- She confirmed 8 of 8 reference answers that our agent had measured for the real-drawing exam.
- Twice she found our answer supported although the dataset's reference differs: 2 WC seats drawn where the
  reference says 1 (a mirrored neighbour's bathroom is on the sheet), and 10 real doors where the reference says 9
  (one is a sliding door). These are references worth re-checking.

Her verdicts are also a regression test: every answer she judged is solved again on a new commit and compared with
her verdict. On the release code, **all 19 numeric answers she backed are still right (0 regressions)**. Of the 9 she
marked wrong, 2 are right now, 1 was right already (she misread a Finnish question), 2 now also state her value as an
alternative total, and 4 are still wrong.

What her findings changed:

| What she saw | What we measured | What changed |
|---|---|---|
| Unnamed numbered rooms that look like balconies were counted as indoor area | Matches the dataset's reference, but no simple cue reproduces which rooms the dataset drops | Value kept; the answer now also shows the total without those rooms |
| Overlapping boxes on a curved plan; one term of a sum hidden by cross-check text | | Room outlines drawn instead of boxes; each sum term lists its area |
| Confidence does not rise when two independent methods agree (VLM and detector both 10, shown 0.22) | She was right, and the reverse is worse: when they disagree at 0.6–0.9, only 47–48% are right | Agreement now raises confidence and disagreement lowers it; CubiCasa counts on dev: ECE 0.044 → 0.039, AUROC 0.864 → 0.887 |
| A lift is a shaft, and the question excluded shafts | The dataset counts lifts as rooms (31 of 31 such dev questions) | Value kept to match the dataset; the answer now also names the total without the lift |
| A typical-answer guess was shown as 76% sure | The prior wins its gate for that question kind | Open: a prior answer should read as an estimate, not as 76% sure |
| On a real 1:100 sheet we said "not determinable"; she measured about 10 m² using the printed scale and a door as a check | Recognition had found 1:100, but nothing measured with it | The room is now traced at the printed scale: 10.08 m² (her ≈10 m²), with her door check built in (the door-swing radius at the printed scale, 0.90 m here, is stated as a scale check) |
| A floor-area answer of 0.30 confidence was called "confirmed" by the agent, and a clear scale bar was missed | The agent's "confirmation" relayed the same single reading | Answers below 0.4 with no second method now read as a guess ("we are unsure: please check it"), with "Measure the room" as the main button; the same room is now 28.68 m² (reference 28.3) |
| A type sheet prints one area for two identical studios: count every drawn unit, not every label (202.5 m², 4 apartments) | We summed the printed labels (171.5 m², 3 apartments) | Open |

She is not a construction specialist, and 34 answers is a small sample. We report it as a usability signal (can a
careful non-specialist verify our answers quickly?) and as a source of concrete fixes, not as an accuracy number.

**Measured by hand on Stoa's own sheets.** Since Saturday evening she also answers questions on Stoa's example sheets
by hand, writing down where she looked and how she measured. On the first 5 (the DfE school plan), our answer at
`a486439` (gate d25) is within the eval's tolerance on 4: the Multi-Purpose Hall 244.35 m² against her 244.6 m², a
classroom 52.13 against 52.28 m², all eight secondary classrooms together 413.9 against 417.49 m², and the laundry's
skirting 10.13 against 9.96 m. The miss is the hall's skirting, 53.83 m against her 50.4 m: we missed one double door,
and our traced perimeter is 1.65 m too long. Her sixth question, the windows on the west facade of the classroom
wing, she counted as 18; ours said 0 at confidence 0.30: the vector sheet was sent to the CubiCasa5K picture reader,
which does not know CAD drawings, and the facade was not taken into account. So far 4 of 6 agree (areas 3 of 3,
skirting 1 of 2, window counts 0 of 1), and our confidence was 0.30 to 0.55 on all six, right or wrong, so it does
not yet tell them apart. Three of her questions are now in the development set, and gate d27 (`12b136d`) answers
all three within tolerance. With her 8 hand-made answers on the DfE sheet as an eval split, ours agree on 5 at d27, 7 at d28
and all 8 at d29 (`f2cd8f1`), with the west-facade windows (18) and the sheet's door count (115) as she counted.
This is not an independent test: those fixes were made from her answers and written methods. Our confidence on these
8 is still low (0.40 to 0.55 on the right areas and lengths).

## Internal audit: a random sample of the shipped demo

On Saturday evening a separate reviewer (an agent that did not build the product and changed no code) audited the
build our demo ran then, `1795365`, as a first-time judge would: random questions, fresh disk caches, every hosted
model off (`gemini=0`, `verify=0`), the official scorer. It drew 50 questions at random (seed 20261114), stratified by
task, from the open stoa-like set (28 PDF, 18 PNG, 4 phone photos; 18 files, correlated questions, not 50 buildings),
and 20 questions (seed 20261115) from simulated scans of the same development sheets. The machine was heavily loaded
by other jobs (load 296–356 on 48 cores), so its times are upper bounds.

| Sample | Right | Note |
|---|---|---|
| Same build, the 41 questions both runs share | 27 / 41, as in our own run | every right / wrong label reproduced (0 gains, 0 losses) |
| Random 50 open stoa-like questions | 29 / 50 (58%) | a harder, task-stratified mix; on the four example sheets alone 22 / 27 (81%) |
| by input: vector PDF / clean PNG / rotated or set PDF / crop / phone photo | 16 / 25, 12 / 17, 1 / 3, 0 / 1, 0 / 4 | |
| by task: areas and other dimensions / baseboards / counts (not piping) / slab areas / piping tags and sizes | 9 / 18, 3 / 8, 6 / 10, 3 / 6, 8 / 8 | real door questions 0 / 3 |
| Simulated scans | 4 / 20 (20%) | image-only PDF 2 / 11, PNG 2 / 9; 10 matched questions: 6 / 10 clean, 1 / 10 scanned |

Time per question: median 10 s, p95 192 s, max 363 s on the 50; on the scans median 279 s, p95 723 s (OCR hit its
120 s budget again and again). Estimated cost about $1.49 per 1,000 questions with no hosted call, before the OCR engine's CPU time was
priced; with it, the 20 scan questions alone come to about $10 per 1,000 ([Cost by input](#cost-by-input)). In a browser
walkthrough of the deployed web app it got 5 of 7 graded questions right; the two misses were a slab area answered
with an elevation label (10,000 m² at confidence 0.75) and a kitchen area on a scanned copy (48.68 m² against 32.05).

What we take from it: our own numbers reproduce on the same build; the drop is in transfer (other questions, other
inputs, scans and photos), not in measurement. Its five main risks, which we treat as our to-do list: located
evidence that does not support the value (an elevation read as an area; Gemini count evidence capped at 12 boxes);
scans and photos; the web app's privacy wording on that build ("read by local models" while hybrid steps were on;
the Private switch came later); long waits on scans and busy shared services; and project-level workflows (several
uploaded sheets of one building) that work on our sample project but not yet on arbitrary uploads.

Since then, in this code: a cited number must be of the asked kind, and every answer says how well its evidence
supports it (`supported`, `approximate` or `unlocated`); Gemini may return up to 200 boxes, and an answer with
fewer marks than its count says so; each answer names where its data went, and private mode is enforced at the
network layer. Scans and photos, long waits on scans, and arbitrary multi-sheet projects are still open.

