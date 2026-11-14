"""Answer one question about a file the visitor uploaded in the demo (not part of the dataset).

Called by the Vite middleware (web/mock/stoaApi.ts, POST /api/ask on an `upload/...` path): a few long-lived workers
(`ask_upload.py --serve`, one JSON request per stdin line, caches stay warm), or one process per question:

    echo '{"abs": "/tmp/stoa-uploads/ab12/plan.png", "virtual": "upload/ab12/plan.png", "question": "..."}' \
        | $JUNCTION_VENV/bin/python web/server/ask_upload.py

The API (stoa/api.py) only accepts dataset paths, so this does what its POST /ask does (solver, cost meter, intent,
compose plan) for an absolute path outside the dataset. The pipeline routes such a file by its extension (no
template, no vector/IFC twin: the hidden set's "different" drawings). Evidence that points at the uploaded file is
renamed to the virtual path the browser knows (also evidence on a file uploaded TOGETHER with it, in the same folder:
a sheet's IFC model). Prints the Answer as JSON on stdout (solvers' prints go to stderr).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path


def sheet_labels(abs_path: str, virtual: str, out: dict) -> None:
    """A PDF sheet uploaded with its model: the answer cites rooms by GlobalId only (the GlobalId layer of a dataset
    sheet is its SVG twin, which an upload does not have). Find each cited room's printed label on the sheet (its
    number, e.g. '202', next to its name, 'KORIDOR', in the PDF text layer; W1's knowledge graph maps GlobalId -> both)
    and add a box per room found, plus one cross-check line. Nothing else changes (value, confidence, cost)."""
    if not abs_path.lower().endswith(".pdf"):
        return
    ev = out.get("evidence") or []
    guids = list(dict.fromkeys(e["ifc_guid"] for e in ev if e.get("ifc_guid")))
    if not guids or any(e.get("bbox") and e.get("file") == virtual for e in ev):
        return
    from stoa.kg import links

    rooms = {r["guid"]: r for r in links(abs_path).get("rooms") or [] if r.get("guid")}
    want = [rooms[g] for g in guids if g in rooms]
    if not want:
        return
    import fitz

    with fitz.open(abs_path) as doc:
        if not len(doc) or doc[0].rotation:
            return
        words = doc[0].get_text("words")
    by_text: dict[str, list] = {}
    for w in words:
        by_text.setdefault(w[4], []).append(w)

    def near(a, b) -> float:
        return max(abs((a[0] + a[2]) - (b[0] + b[2])) / 2, abs((a[1] + a[3]) - (b[1] + b[3])) / 2)

    found = []
    for r in want:
        num, name = str(r.get("name") or "").strip(), str(r.get("long_name") or "").strip()
        cands = by_text.get(num, []) if num else []
        first = name.split()[0] if name else ""
        best = None
        for w in cands:  # the occurrence with the room's name printed next to it
            names = [x for x in by_text.get(first, []) if near(w, x) <= 40] if first else []
            if names:
                x = min(names, key=lambda x: near(w, x))
                best = (min(w[0], x[0]), min(w[1], x[1]), max(w[2], x[2]), max(w[3], x[3]))
                break
        if best is None and len(cands) == 1:
            best = cands[0][:4]
        if best is not None:
            found.append((r, best))
    if not found:
        return
    for r, b in found:
        label = " ".join(x for x in (str(r.get("long_name") or ""), str(r.get("name") or "")) if x)
        ev.append({"file": virtual, "page": 0, "bbox": [round(b[0] - 2, 1), round(b[1] - 2, 1), round(b[2] + 2, 1), round(b[3] + 2, 1)],
                   "bbox_space": "pdf_pt", "ifc_guid": None, "note": f"sheet label '{label}' (PDF text) = IfcSpace {r['guid']}"})
    out["evidence"] = ev
    n, m = len(found), len(want)
    out.setdefault("trace", []).append(
        f"cross-check: the sheet's own text labels {n} of the {m} rooms the model answer cites"
        + (" ✓" if n == m else " (the rest have no printed label found)") + " (boxes on the sheet)")


def answer(req: dict) -> dict:
    """One question about one uploaded file -> the Answer as a JSON-ready dict."""
    abs_path, virtual, question = req["abs"], req["virtual"], str(req.get("question", "")).strip()
    solver_name, solver_args = req.get("solver") or "", dict(req.get("solver_args") or {})
    # run from the repo root so stoa/ imports like they do for the API
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.chdir(root)
    with contextlib.redirect_stdout(sys.stderr):
        from stoa.contract import Answer, CostMeter
        from stoa.solvers import available, get_solver

        # P1 13:35: the `upload` solver = the pipeline behind the format normaliser (multi-page PDF -> the plan page,
        # generic SVG -> vector PDF, DWG -> DXF, HEIC -> JPEG; evidence comes back on the uploaded file / page). Same
        # answer and cost as `pipeline` on files that need no conversion; older code without it -> `pipeline`.
        if not solver_name:
            solver_name = "upload" if "upload" in available() else "pipeline"
        # the exam path behaves like the API (captain 13:14, G1): the pipeline's second opinions on unsure answers
        # (Gemini planner, Nemotron verifier) are on unless STOA_API_GEMINI / STOA_API_VERIFY = 0 or the request says
        # otherwise; only switches this Pipeline version knows are passed (older code: none)
        if solver_name in ("pipeline", "upload"):
            import inspect

            from stoa.pipeline import Pipeline

            known = inspect.signature(Pipeline.__init__).parameters
            for k, env in (("gemini", "STOA_API_GEMINI"), ("verify", "STOA_API_VERIFY")):
                if k in known and k not in solver_args and os.environ.get(env, "1") != "0":
                    solver_args[k] = 1
        solver = get_solver(solver_name, **solver_args)
        t0 = time.perf_counter()
        with CostMeter() as meter:
            ans = solver.solve(abs_path, question)
        if not isinstance(ans, Answer):
            ans = Answer.model_validate(ans)
        rep, met = ans.cost.model_dump(), meter.cost.model_dump()
        ans.cost = ans.cost.model_validate({k: max(rep.get(k, 0) or 0, met.get(k, 0) or 0) for k in set(rep) | set(met)})
        if not ans.cost.wall_seconds:
            ans.cost.wall_seconds = round(time.perf_counter() - t0, 4)
        out = ans.model_dump()
        name, folder = Path(abs_path).name, Path(abs_path).parent
        vdir = virtual.rsplit("/", 1)[0]
        for e in out.get("evidence") or []:
            f = str(e.get("file") or "")
            if not f or f == abs_path or Path(f).name == name:
                e["file"] = virtual
            elif Path(f).is_absolute() and Path(f).parent == folder:  # a file uploaded with it (the sheet's model)
                e["file"] = f"{vdir}/{Path(f).name}"
        # the browser knows the folder as upload/<id>: no temp paths in the reasoning shown to the visitor
        out["trace"] = [str(t).replace(str(folder), vdir) for t in out.get("trace") or []]
        try:
            sheet_labels(abs_path, virtual, out)
        except Exception as e:  # noqa: BLE001  (boxes are a bonus: never lose the answer for them)
            print(f"sheet_labels: {e}", file=sys.stderr)
        try:
            from stoa.data import source_of
            from stoa.router.compose import decompose
            from stoa.router.intent import parse

            out["intent"] = parse(question).to_dict()
            plan = decompose(source_of(abs_path), question)
            if plan is not None:
                out["plan"] = {"op": plan.op, "parts": plan.labels, "sub_questions": plan.subs}
        except Exception:  # noqa: BLE001
            pass
    return out


MARK = "@@answer@@ "


def serve() -> None:
    """Stay alive and answer one JSON request per stdin line (P1 15:12: a new process per question rebuilt every cache,
    ~2.5 s per ask, 0.06 s warm). Each reply is ONE stdout line starting with MARK (anything else a library prints
    to the real stdout is ignored by the middleware): {"answer": {...}} or {"error": "..."}."""
    real = sys.stdout
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = {"answer": answer(json.loads(line))}
        except (Exception, SystemExit) as e:  # noqa: BLE001  (one bad file or a library's sys.exit must not end the worker)
            import traceback

            traceback.print_exc(file=sys.stderr)
            out = {"error": f"{type(e).__name__}: {e}"}
        real.write(MARK + json.dumps(out, default=str) + "\n")
        real.flush()


def main() -> None:
    if "--serve" in sys.argv:
        return serve()
    print(json.dumps(answer(json.loads(sys.stdin.read() or "{}")), default=str))


if __name__ == "__main__":
    main()
