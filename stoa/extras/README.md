# stoa/extras

Run-time modules that started as experiments during the hackathon and that the pipeline imports (all imports are
guarded: without them the pipeline still answers, it only loses these routes). Copied unchanged from the team
workspace, except for their import paths.

| Folder | What | Used by |
|---|---|---|
| `room_graph/` | Room graph of a raster (CubiCasa-style) plan: rooms, doors, windows, fixtures and which rooms connect; per-room questions become lookups with evidence | `stoa/pipeline.py` (room-scoped questions, area cross-check), `stoa/solvers/raster`, `stoa/evidence/raster.py` |
| `raster_scale/` | Scale of a raster plan from door widths and printed room areas | `room_graph/graph.py` |
| `kg_qa/` | Questions about links between files of one project (which sheets show this model, does a plan exist for a storey), answered from the project graph `stoa/kg` | `stoa/pipeline.py` |
| `cc5k_model/` | Our re-implementation of the CubiCasa5K multi-task network + inference wrapper. The pretrained weights (CC BY-NC 4.0) are NOT in this repo: set `CC5K_WEIGHTS` and `CC5K_PYTHON` (a Python with torch) to enable it | `stoa/solvers/raster_cc5k`, `room_graph/` |
| `neutralize.py` | Copies a dataset file to a meaningless path (to test uploads without folder hints) | `deploy/verda/lone_split.py` |

The room graph and the CubiCasa5K detector read cached segmentation maps from `$JUNCTION_SHARED/artifacts/R1/`
when they exist; otherwise they run the model live (needs the weights) or stay off.
