# Data and licences

What this project uses, who made it, and under which licence. Short version:

- **Our code** (everything in this repository unless noted) is MIT-licensed: [LICENSE](../LICENSE).
- **The Stoa dataset is not in this repository.** Every drawing keeps its own licence. Some are NonCommercial (NC),
  ShareAlike (SA) or NoDerivatives (ND). Stoa released the dataset for non-commercial use in the Junction challenge.
- **Our LoRA adapters and the optional CubiCasa5K weights are non-commercial** and are not in this repository.
- **Copyleft dependencies are installed or called, not bundled:** PyMuPDF (AGPL-3.0), IfcOpenShell (LGPL-3.0), and
  LibreDWG (GPL-3.0, run as a separate program).
- **Bundled here:** three small sample drawings (CC BY 4.0, KIT "unrestricted use" and Open Government Licence v3.0),
  one screenshot that shows the Open Government Licence drawing, and recorded answers for the web app's offline mode.
  A few of those answers are about CubiCasa5K plans, which are **non-commercial only**; they are labelled as such (see the
  table at the end).
- **Paid use needs more than our MIT licence:** what must be licensed or replaced first, and what happens to a
  customer's drawings, is under [Before paid use](#before-paid-use).

This page summarises; it is not legal advice. The licence text and credit line of each dataset file are in
`data/<source>/LICENSE.md` inside the dataset.

## The Stoa Junction 2026 dataset (not included)

About 14,000 construction drawings (PDF, DWG, DXF, IFC, SVG, PNG and JPEG) from Europe and the US, in about 30
languages, with about 120,000 questions. Files with the same name and a different extension are the same drawing in
another format. **Questions inherit the licence of the drawing they ask about.** Keep the credit when you show or
share a drawing; NC files are for non-commercial use only; adaptations of SA files must use the same licence; ND files
must stay unchanged. Our web app shows each file's licence and credit next to the drawing.

| Source | What | Licence | Credit (short; full lines in `data/<source>/LICENSE.md`) |
|---|---|---|---|
| `cubicasa5k` | Finnish real-estate floor plans (PNG) | **CC BY-NC-SA 4.0** for all files. The GitHub repository states CC BY-NC 4.0 and the Zenodo record CC BY-NC-SA 4.0; the dataset applies the stricter one | CubiCasa5K, © 2019 CubiCasa. Kalervo et al., "CubiCasa5K: A Dataset and an Improved Multi-Task Model for Floorplan Image Analysis", SCIA 2019 |
| `swiss_dwellings` | Swiss apartment floors as PDF, DXF and PNG, drawn by Stoa from published geometry | **CC BY 4.0** for all files | Swiss Dwellings v3.0.0 by Standfest et al. (Archilyse AG), doi:10.5281/zenodo.7788422. Drawings rendered by Stoa from the dataset geometry (style, numbering, room names, door swings and title block added). Archilyse AG does not endorse these drawings |
| `ifc_models` | Open IFC building models, and floor plans Stoa rendered from them with IfcOpenShell | Per file: MIT (1,016 files), CC BY 4.0 (171), KIT "for unrestricted use" (49), **CC BY-ND 3.0** (10) | Per file. MIT: DigitalHub (RWTH Aachen), Chesapeake Bay Bridge-Tunnel (buildingSMART Community), ResBIM. CC BY 4.0: BIM-SPEED demonstration projects (TU Berlin). KIT: Institute for Automation and Applied Informatics (IAI) / Karlsruhe Institute of Technology. CC BY-ND 3.0: buildingSMART Duplex and Medical-Dental Clinic (published as CC BY 4.0 today; the stricter original licence is applied, files unchanged, no drawings rendered from them). Rendered plans carry their model's licence |
| `ifc_bench` | IFC models with the IFC-Bench v2 questions | Questions: CC BY 4.0. Models per file: MIT (10), CC BY 4.0 (9), **CC BY-ND 3.0** (5), KIT (3), **GPL-3.0** (2) | IFC-Bench v2 (Hellin et al., Technical University of Munich, GNI 2026). GPL-3.0 models: © 2009–2017 BIMserver.org (opensourceBIM TestFiles) |
| `opening_design` | Real drawing sets and CAD files of a US architecture practice, original folders kept | **CC BY-SA 4.0** (989 files), MIT (16 files: repositories 104_Janesville and Schultz_Residence) | OpeningDesign, Madison, Wisconsin, and the project team named in each title block. MIT files: copyright line as given in each repository |
| `habs` | US historic measured drawings | Public domain (1,505 files; works of the U.S. federal government) | Historic American Buildings Survey / Engineering Record / Landscapes Survey, National Park Service; Library of Congress Prints and Photographs Division (credit is a courtesy) |
| `finna` | Drawings from Helsinki City Archives and the Architecture & Design Museum, Helsinki | **CC BY 4.0** (1,799 files) | Per file, as given (Helsingin kaupunginarkisto; Arkkitehtuuri- ja designmuseo), via Finna |
| `europeana` | Drawings from European museums, archives and libraries | Per file: public domain (486), CC BY 4.0 (176), **CC BY-NC 4.0** (113), CC0 (93), **CC BY-NC-SA 4.0** (87), CC BY-SA 4.0 (50), CC BY 3.0 (25), CC BY-SA 3.0 (20), CC BY-SA 3.0 NL (19), **CC BY-NC-SA 3.0** (15) | Per file, as given by each holder, via Europeana |
| `wikimedia` | European, UK and US floor plans from Wikimedia Commons (file list from WAFFLE) | Per file: public domain (1,207), CC BY-SA 4.0 (448), CC BY-SA 3.0 (277), no known copyright restrictions (136), CC0 (75), CC BY 4.0 (60), CC BY 3.0 (35), CC BY-SA 2.5 (21), CC BY 2.5 (17), CC BY-SA 2.0 (10), and a few files under other CC versions or ports, "copyrighted free use" or OGL-UK | Per file, as given on Commons |

File counts are the rows of each source's `LICENSE.md` table. Changes Stoa made are listed there too (mostly renaming,
and downscaling large scans to at most 3,000 px).

**How we used it.** We trained only on the `train` split, evaluated on `dev` and the sealed `test`, and kept the
dataset outside git at all times (`STOA_DATA` points at it). Our LoRA adapters were trained on CubiCasa5K `train`
images (and, for `cc-v3`, Swiss Dwellings PNGs of `train` drawings). The sealed test was run with the pretrained
CubiCasa5K detector switched off.

## Models

| Model | Role | Licence | In this repo? |
|---|---|---|---|
| Qwen3-VL-8B-Instruct (Alibaba Qwen) | Local vision-language model, served by vLLM on our GPU | Apache-2.0 | No; downloaded from Hugging Face (`Qwen/Qwen3-VL-8B-Instruct`) |
| Our LoRA adapters `cc-v2` and `cc-v3` | Counting on raster plans (on top of Qwen3-VL-8B) | **Non-commercial**, because they were trained on CubiCasa5K images (CC BY-NC-SA 4.0) | No |
| CubiCasa5K multi-task checkpoint (Kalervo et al., SCIA 2019; `CubiCasa/CubiCasa5k` on GitHub) | Optional symbol detector for CubiCasa-style plans (`raster_cc5k`), CPU only | **CC BY-NC 4.0** | No; set `CC5K_WEIGHTS` to its path. The network code in `stoa/extras/cc5k_model/` is our own re-implementation, and the checkpoint is loaded as weights only, so no upstream code runs |
| Qwen3.6-35B-A3B (FP8) | Optional second local server; writes free-text answers about IFC models | Apache-2.0 (model card) | No |
| PP-OCRv6 (PaddleOCR 3.7) | Optional OCR text layer for scans, run in its own Python env (`stoa/ocr/requirements.txt`) | Apache-2.0 (code and models) | No; models download on first use |
| Google Gemini 3.1 Pro and Gemini 3 Flash (hosted) | Escalation for uploads, agent planner, API-only mode | Google's Gemini API terms | No; called only with your own `GEMINI_API_KEY` |
| NVIDIA Nemotron 3.5 Super VL (`nvidia/nemotron-3.5-super-vl-120b-a12b`, hosted) | Verifier of unsure answers, second opinion in the app | NVIDIA API terms | No; called only with your own `NVIDIA_API_KEY` |

Note on Qwen3-VL-8B: on our workstation, the copy we used was taken from the Qwen-Image-2.1 bundle, which carries a
research licence. On Verda we downloaded the official Apache-2.0 release from Hugging Face. Its weights were
identical to our copy, tensor by tensor (750 of 750). Use the official release.

`serving/start.sh` also has presets for other models we tried (Qwen3.5-9B, Gemma 4 E4B). They are not used for any
result we report; check each model's card before you use it.

## External programs (called, not linked or bundled)

| Program | Licence | Used for |
|---|---|---|
| LibreDWG `dwg2dxf` | GPL-3.0 | Optional: converts an uploaded DWG to DXF (`stoa/formats/dwg.py`). It runs as a separate process; set `STOA_DWG2DXF` or put it on `PATH`. Without it, DWG uploads are not read |
| vLLM | Apache-2.0 | Serves the local models (`serving/`), in its own environment |
| Caddy | Apache-2.0 | Password front for the optional demo Docker image (`deploy/Dockerfile`, copied from the official image) |

## Python libraries

Installed by `pyproject.toml`, not vendored. Versions are the ones we tested.

| Library | Version | Licence | Note |
|---|---|---|---|
| PyMuPDF | 2.1 | **AGPL-3.0** (or Artifex commercial licence) | PDF text, vector paths and rendering. If you distribute this project, or run a modified copy as a network service, PyMuPDF's AGPL-3.0 terms apply to it |
| IfcOpenShell | 0.9.0 | **LGPL-3.0-or-later** | Reads IFC models; used as an unmodified library |
| ezdxf | 1.4.5 | MIT | Reads DXF files |
| NumPy | 2.5 | BSD-3-Clause (and bundled permissive licences) | |
| pandas | 3.1 | BSD-3-Clause | |
| Shapely | 2.2 | BSD-3-Clause | |
| Pillow | 13.0 | MIT-CMU | |
| opencv-python-headless | 5.0 | Apache-2.0 | |
| pydantic | 2.14 | MIT | |
| FastAPI | 0.162 | MIT | |
| Uvicorn | 0.54 | BSD-3-Clause | |
| openai (Python client) | 3.51 | Apache-2.0 | Client for vLLM and the hosted APIs |
| RapidFuzz | 3.14 | MIT | |
| tqdm | 4.70 | MPL-2.0 and MIT | |
| pytest (extra `dev`) | 9.2 | MIT | |
| PyTorch, CPU build (extra `cc5k`) | ≥ 2.4 | BSD-3-Clause | Only for the optional CubiCasa5K detector |
| pi-heif (extra `formats`) | ≥ 1.0 | BSD-3-Clause; bundles libheif and libde265 (LGPL-3.0) | Only for iPhone HEIC photos; decode-only |
| PaddleOCR, PaddlePaddle, PaddleX | 3.7 / 3.3 / 3.7 | Apache-2.0 | Optional OCR, in a separate environment |

## JavaScript libraries (web app)

From `web/package.json`, installed by npm, not vendored.

| Library | Version | Licence |
|---|---|---|
| PDF.js (`pdfjs-dist`) | 5.4.296 | Apache-2.0 |
| three.js | 0.180.0 | MIT |
| web-ifc | 0.0.72 | MPL-2.0 (used unmodified) |
| Inter font (`@fontsource-variable/inter`) | 5.3 | SIL Open Font License 1.1 |
| Vite | 7.1 | MIT |
| TypeScript | 5.9 | Apache-2.0 |

## Files bundled in this repository

| File | Licence | Credit |
|---|---|---|
| `samples/swiss/plan.pdf`, `.dxf`, `.png` (dataset file `site117_bldg93_floor408`) | CC BY 4.0 | Swiss Dwellings v3.0.0 by Standfest et al. (Archilyse AG), doi:10.5281/zenodo.7788422, CC BY 4.0. Drawing rendered by Stoa from the dataset geometry (derived work; labels, language and numbering added). Archilyse AG does not endorse these drawings. From the Stoa Junction 2026 dataset |
| `samples/fzk_house/fzk_house.ifc` (FZK-Haus) | KIT: "for unrestricted use" | Institute for Automation and Applied Informatics (IAI) / Karlsruhe Institute of Technology (KIT), KIT IFC Examples, https://www.ifcwiki.org/index.php?title=KIT_IFC_Examples. Unchanged, via IFC-Bench v2 and the Stoa Junction 2026 dataset |
| `samples/dfe_school/plan.pdf` (Special School Plan Level 00, SEN-DFE-XX-00-D-X-0201 rev C01) | Open Government Licence v3.0 | UK Department for Education, GOV.UK "A new model design for a special school", unchanged. Contains public sector information licensed under the Open Government Licence v3.0 |
| `docs/img/hero.png` (screenshot of our web app) | Our UI: MIT. The drawing shown: Open Government Licence v3.0 | Special School Plan Level 00 (SEN-DFE-XX-00-D-X-0201), UK Department for Education, GOV.UK "A new model design for a special school". Contains public sector information licensed under the Open Government Licence v3.0 |
| `web/fixtures/` (offline replay for the app) | Our answers: MIT. Dataset questions and data derived from a drawing keep that drawing's licence | Recorded questions and answers about the dataset drawings listed below, plus a little geometry derived from them (the 3D view of the Swiss floor, room regions of one CubiCasa5K plan). No drawing files. **Entries about CubiCasa5K plans are CC BY-NC-SA 4.0, non-commercial use only**: each carries a `"licence"` field saying so, and `web/fixtures/LICENCES.md` lists every drawing with its licence and credit, generated from the dataset's `LICENSE.md` tables |

**Drawings the offline replay (`web/fixtures/`) asks about.** The files themselves are not bundled; the app opens
them from your copy of the dataset (`STOA_DATA`).

| Dataset file(s) | Drawing | Licence | Credit |
|---|---|---|---|
| `ifc_models/bsc-esplanades_1807-ep-ar-v18.ifc` and its five plan sheets `…_plan-0N-N-korrus.pdf` | Maleva tänav 18 apartment building, Tallinn (Esplanades example model) | CC BY 4.0 | © Esplan (esplan.ee), via buildingSMART Community Sample-Test-Files. Plan sheets generated from the IFC by Stoa with IfcOpenShell IfcConvert 0.9.0 (rendered drawing; geometry unmodified) |
| `swiss_dwellings/site1000_bldg2082_floor9385` (PDF, DXF, PNG) | Apartment floor | CC BY 4.0 | Swiss Dwellings v3.0.0 by Standfest et al. (Archilyse AG), doi:10.5281/zenodo.7788422. Rendered by Stoa from the dataset geometry; Archilyse AG does not endorse these drawings |
| `ifc_models/bsc-duplex_duplex-a-20110907.ifc` | Duplex Apartment, architectural model | CC BY-ND 3.0 | BSI (2020) "Duplex Apartment Test Files", buildingSMART International. Only questions and our answers about the unchanged model; nothing derived from it is stored |
| `ifc_bench/4351_arc.ifc` | Unidentified building, architectural model (IFC-Bench v2 project 4351) | GPL-3.0-or-later | © 2009–2017 BIMserver.org (opensourceBIM TestFiles), via IFC-Bench v2 (Hellin et al., TU Munich) |
| `opening_design/Sports_Complex/Sheets - Current Set/A-801 - Door and Finish Schedules & Wall Types.pdf` | Xcel Sports Complex, door and finish schedules | CC BY-SA 4.0 | OpeningDesign, Sports_Complex (github.com/OpeningDesign/Sports_Complex), and the entire project team |
| `habs/vi0026_sheet_00003.jpg` | Creque Marine Railway, Head House and Residence, North Elevation, Charlotte Amalie, St. Thomas, VI | Public domain | HAER, National Park Service; Library of Congress Prints and Photographs Division (loc.gov/pictures/item/vi0026.sheet.00003a) |
| `cubicasa5k/colorful-4539.png`, `high_quality_architectural-816.png`, `high_quality_architectural-14797.png` | Finnish apartment floor plans (images) | **CC BY-NC-SA 4.0: non-commercial use only** (the CubiCasa5K repository states CC BY-NC 4.0; the dataset applies the stricter Zenodo licence). Kept for the non-commercial Junction challenge, cleared with Stoa | CubiCasa5K, © 2019 CubiCasa. Kalervo, Ylioinas, Häikiö, Karhu, Kannala, "CubiCasa5K: A Dataset and an Improved Multi-Task Model for Floorplan Image Analysis", SCIA 2019 |
| `finna/mfa.81125dbb-8b52-48e3-bc9d-1128e233ac93.jpg` | Olympiastadion, site plan, Helsinki, 3.7.1950 | CC BY 4.0 | Lindegren & Jäntti (architects); Arkkitehtuuri- ja designmuseo (Architecture & Design Museum, Helsinki), via Finna (finna.fi/Record/mfa.81125dbb-8b52-48e3-bc9d-1128e233ac93) |

`samples/README.md` has the full credit lines and a few questions with expected answers (`samples/questions.jsonl`).

## Before paid use

Our MIT licence covers our own code. It does not make the data, weights, libraries or hosted models we used free for
commercial use, and Stoa's permission to use them in the challenge is not a licence for a paid product. Before this
project is sold or run as a paid service:

| Component | Today | Before paid use |
|---|---|---|
| The Stoa Junction 2026 dataset | For non-commercial use in the Junction challenge; every file keeps its own licence, some NC, SA or ND | Use only files whose own licence allows it, or other licensed drawings. `stoa/router/train_stats.json` (numbers and hashes of question templates, no text) was computed from its `train` questions: regenerate it from licensed data (`python -m stoa.router.train_stats --write`) |
| Our LoRA adapters `cc-v2`, `cc-v3` (and `cc-wild`) | **Non-commercial**: trained on CubiCasa5K images (CC BY-NC-SA 4.0) | Retrain on drawings licensed for commercial use, or serve the base model without them (`NO_LORA=1` in `serving/start.sh`; counts on CubiCasa-style images get worse, by about 12 points on CubiCasa5K) |
| CubiCasa5K checkpoint (optional, `CC5K_WEIGHTS`) | **CC BY-NC 4.0** | Leave `CC5K_WEIGHTS` unset (the `raster_cc5k` solver then abstains on new images), or obtain a commercial licence from CubiCasa |
| `web/fixtures/` entries about CubiCasa5K plans | **CC BY-NC-SA 4.0**, labelled | Remove the entries whose `"licence"` field says non-commercial |
| Door and window detector (optional service, not in this repository) | Fine-tuned from torchvision's COCO-pretrained Faster R-CNN on renders of Swiss Dwellings `train` floors (CC BY 4.0) | Check the terms of the pretrained weights and of their training images; keep the Swiss Dwellings credit |
| Qwen3-VL-8B-Instruct | Apache-2.0 (official release) | Use the official Hugging Face release (see the note under Models) |
| PyMuPDF | **AGPL-3.0**, or Artifex's commercial licence | A paid service with closed-source changes needs Artifex's commercial licence. Under AGPL-3.0, the whole service, changes included, must be offered in source form to its users |
| IfcOpenShell (LGPL-3.0), web-ifc (MPL-2.0), LibreDWG (GPL-3.0, a separate program) | Used unmodified, or called as a program | Keep them unmodified or publish your changes to them under their own licences; if you hand out LibreDWG with the product, hand out its source too |
| Google Gemini, NVIDIA Nemotron (hosted, optional) | Called with your own API keys | The provider's terms for paid API use apply, including what it may keep from your requests |

**Customer drawings: what the code does today.**

- **Stored.** An upload is saved in `STOA_UPLOADS` (a temporary folder by default), in a sub-folder named by a hash
  of the file's content and name, with its format copies (PNG of a photo, PDF of a DXF) next to it. Readings made from it
  (text layer, OCR, page renders, model replies) are cached on the same server, under `.stoa/` (or
  `JUNCTION_SHARED`), `STOA_CACHE` and `STOA_LLM_CACHE_DIR` (see [CONFIGURATION.md](CONFIGURATION.md)). Our code
  never trains on uploads.
- **Kept.** By default nothing expires on its own: files stay until the operator deletes them (a temporary folder may
  also be cleared when the machine restarts). With the optional `privacy` feature on, `STOA_UPLOAD_TTL_HOURS=<h>`
  deletes each upload, with the readings tied to it, once its newest file is more than h hours old (checked every
  `STOA_UPLOAD_SWEEP_MIN` minutes, default 15); a folder holding a `.keep` file and the shared example sheets are never
  expired. A deployment for customers should set it.
- **Deleted.** With the optional `privacy` feature on (`STOA_FEATURES=privacy`), the app's Privacy panel shows
  what is stored where and has a **Delete my files** button (`POST /feat/privacy/delete`): it removes the upload, its
  format copies and the cached readings tied to it, and lists the caches it cannot tie to the file (model replies and
  PDF page renders, named by a hash of a request) instead of skipping them silently. Without the feature, the
  operator deletes the upload's folder. Either way the hash-named caches must be cleared separately, or kept
  short-lived.
- **Processed.** In private and EU-only mode every step runs on servers you run. In hybrid mode an unsure answer
  may send page images (zoomed quarters for counts), an evidence crop and text read from the sheet to Google or
  NVIDIA ([what each step sends](RESULTS.md#cost-latency-and-data-governance)), and every answer says where its data went (see
  [Cost, latency and data governance](../README.md#cost-latency-and-data-governance)).

For a pilot, write these down with the customer before the first drawing arrives: where the files live, for how
long, who deletes them, and whether hosted models may see them.

## The real-drawing exam (not included)

Our real-drawing exam uses public files from outside the Stoa dataset: Wikimedia Commons, Finna, the opensourceBIM
test files and OpeningDesign, under CC BY, CC BY-SA, MIT and GPL-3.0 licences. The files are not bundled here. Their
sources, licences and credits are listed in `experiments/x1/SOURCES.md` in the team's
[workspace repository](https://github.com/Yue-fy/Junction_Hannah_family). The held-out half of the exam is kept
private on purpose, so its files are not listed publicly.

## Stoa's example sheets (not included)

Our main metric uses the four sheets Stoa sent as examples of their test and other sheets from the same public
drawing sets. Only the DfE sheet is bundled (as a sample); the others are not. Their public originals:

| Sheet | Source | Licence |
|---|---|---|
| Special School Plan Level 00 (SEN-DFE-XX-00-D-X-0201, rev C01; bundled as `samples/dfe_school/plan.pdf`) | UK Department for Education, published on GOV.UK | Open Government Licence v3.0. Contains public sector information licensed under the Open Government Licence v3.0 |
| M-101A Piping Plan, from DoD standard design AW 078-24-28 (Pressurized Hydrant Fueling System Type III) | U.S. Army Corps of Engineers, via the Whole Building Design Guide (wbdg.org) | Public domain (work of the U.S. federal government) |
| S-100F Foundation and Slab Plan, from DoD standard design AW 078-24-33 (Cut and Cover Standards) | U.S. Army Corps of Engineers, via wbdg.org | Public domain (work of the U.S. federal government) |
| A-2 Second Floor Plan T-shape, Permanent Party Enlisted Dormitory standard design drawings (April 2026) | Air Force Civil Engineer Center, via wbdg.org | Public domain (work of the U.S. federal government) |

The held-out sibling sheets come from the same sets and are not listed, so that they stay unseen.

The web app's start view opens these sheets (Stoa's PNG copies of three of them, the S-100F vector PDF, the DfE
Level 01 sheet and our simulated scan of the DfE sheet) by file name. They are not bundled: the PNG copies are
image files Stoa sent us, and publishing them is not our call. `samples/examples/README.md` lists each file with
its source, licence and the sha1 of our copy; put the files in `samples/examples/` (or `STOA_EXAMPLES_DIR`) to
open them in the app.
