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
import re
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


@contextlib.contextmanager
def page_on_screen(abs_path: str, page) -> "contextlib.AbstractContextManager[str]":
    """The page the visitor turned to in a multi-page PDF upload (W5 18:55; X1 18:38: the normaliser picks the page from
    the question alone, so 'What is the area of Science Lab 3?' on a 3-page set was answered from page 1). For this one
    question the page pick (stoa.formats.pages.pick_page and G1's stair-note pick, both looked up at call time by
    stoa.formats.normalize.prepare) returns that page; everything else (one-page copy, sheet number, notes, evidence
    mapped back to the uploaded file's page) is the upload solver's own. Yields a note for the trace ('' = no hint)."""
    if page is None or not str(abs_path).lower().endswith(".pdf"):
        yield ""
        return
    try:
        import pymupdf

        import stoa.formats.pages as pages

        with pymupdf.open(abs_path) as d:
            n = d.page_count
        k = int(page)
    except Exception:  # noqa: BLE001 — older trees or an unreadable file: the solver picks the page as before
        yield ""
        return
    if n < 2 or not 0 <= k < n:
        yield ""
        return
    same = lambda p_: Path(str(p_)).resolve() == Path(abs_path).resolve()  # noqa: E731
    orig_pick = pages.pick_page
    pages.pick_page = lambda p_, q="": (k, "the page on screen when the question was asked") if same(p_) else orig_pick(p_, q)
    try:
        import stoa.pipeline as pl
    except Exception:  # noqa: BLE001
        pl = None
    orig_stair = getattr(pl, "_stair_page", None)
    if orig_stair:
        pl._stair_page = lambda p_, q: None if same(p_) else orig_stair(p_, q)
    try:
        yield f"page on screen: {k + 1} of {n} (the visitor turned to it)"
    finally:
        pages.pick_page = orig_pick
        if orig_stair:
            pl._stair_page = orig_stair


HOSTED_TEXT = {"gemini", "nvidia", "api", "verda"}  # stoa.llm backends that leave this server ('verda' = EU-hosted)


@contextlib.contextmanager
def privacy(private: bool, eu: bool = False):
    """Captain 18:53 PRIVATE MODE: the drawing never leaves this server for this one question (no Gemini, no NVIDIA, no
    hosted text model; eu=True still allows Verda's EU-hosted text model). The pipeline's hosted steps are switched off
    by its own arguments (answer(): gemini=0, verify=0, api=0); here the process-wide ones: a hosted free-text backend
    ($STOA_TEXT_BACKEND) falls back to the local server, STOA_MODE=api (no local GPU) is lifted, and STOA_PRIVATE=1 /
    STOA_EU_ONLY=1 tell any reader that checks them (G1 18:53). Restored after the question (the worker serves others)."""
    if not private:
        yield ""
        return
    keep = {k: os.environ.get(k) for k in ("STOA_TEXT_BACKEND", "STOA_MODE", "STOA_PRIVATE", "STOA_EU_ONLY")}
    tb = (os.environ.get("STOA_TEXT_BACKEND") or "").strip().lower()
    if tb in HOSTED_TEXT and not (eu and tb == "verda"):
        os.environ["STOA_TEXT_BACKEND"] = "local"
    if (os.environ.get("STOA_MODE") or "").strip().lower() == "api":
        os.environ.pop("STOA_MODE", None)
    os.environ["STOA_PRIVATE"] = "1"
    if eu:
        os.environ["STOA_EU_ONLY"] = "1"
    try:
        yield ("private mode: no hosted model was called (Gemini, NVIDIA off); the drawing stayed on this server"
               + ("; EU-hosted text model (Verda) allowed" if eu else ""))
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def ask_context(ctx):
    """What the visitor pointed at (web feature plug-ins, web/src/features/registry.ts askWith): {point: [x, y], region:
    [[x, y], ...], view, space: 'pdf_pt' | 'image_px', page}. Handed to the readers as $STOA_ASK_CONTEXT (JSON) for this one
    question (G1 wires 'point' / 'view' / 'region' into the pipeline); restored afterwards (the worker serves others)."""
    if not isinstance(ctx, dict) or not ctx:
        yield ""
        return
    keep = os.environ.get("STOA_ASK_CONTEXT")
    os.environ["STOA_ASK_CONTEXT"] = json.dumps(ctx)
    try:
        parts = []
        if ctx.get("point"):
            parts.append(f"point ({', '.join(f'{float(v):.1f}' for v in ctx['point'][:2])})")
        if ctx.get("region"):
            parts.append(f"region of {len(ctx['region'])} points")
        if ctx.get("view"):
            parts.append(f"view {ctx['view']}")
        yield (f"context: {' + '.join(parts)} in {ctx.get('space', 'drawing')} coordinates"
               + (f" on page {int(ctx['page']) + 1}" if isinstance(ctx.get("page"), int) else ""))
    finally:
        if keep is None:
            os.environ.pop("STOA_ASK_CONTEXT", None)
        else:
            os.environ["STOA_ASK_CONTEXT"] = keep


def answer(req: dict) -> dict:
    """F7 'learn from corrections' (stoa/features/corrections.py, night-F7): wraps the answer; it passes through untouched
    unless the request carries solver_args.learn (the web plugin opts in; since night-F7 24dbc04 the STOA_FEATURES flag
    alone no longer applies corrections, so visitors never inherit each other's). Without the module: the plain one."""
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from stoa.features.corrections import wrap_upload
    except Exception:  # noqa: BLE001 (not on this tree, or broken: never block the answer)
        return _answer(req)
    return wrap_upload(_answer, req)


# T2 00:4x (captain 00:31, audit RECHECK-0026 new problem #1): where the data went is what the NETWORK saw, not only
# which steps say they ran. A Gemini request whose reply missed the 25 s budget leaves no step credit and no cost, so
# data_used said 'local' and the card '🔒 Stayed on our server' (2 of 9 default-mode answers, Stoa's M-101A FP among
# them). Every provider netguard observed is added to data_used (trace line + field) and counted in api_calls.
_NET_PROVIDERS = (("relay", ("forwards outside",)), ("gemini", ("googleapis.com", "generativelanguage")),
                  ("nvidia", ("nvidia.com",)), ("verda", ("verda",)))
_BARE_ADDR = re.compile(r"^\[?[0-9a-f.:]+\]?:\d+$", re.I)


def _net_providers(seen: list[str]) -> list[str]:
    """'generativelanguage.googleapis.com' -> gemini, '…nvidia.com…' -> nvidia, '…verda…' -> verda, a forwarding port
    of this machine -> relay, any other outside host -> hosted. A bare 'address:port' is the connect that follows a
    named lookup, so it counts (as 'hosted') only when no host was named."""
    out: list[str] = []
    bare = False
    for h in seen:
        hl = h.lower()
        name = next((p for p, keys in _NET_PROVIDERS if any(k in hl for k in keys)), None)
        if name is None and _BARE_ADDR.match(hl.strip()):
            bare = True
            continue
        name = name or "hosted"
        if name not in out:
            out.append(name)
    if bare and not any(p != "relay" for p in out):
        out.append("hosted")
    return out


def _network_is_the_truth(ans: Answer, seen: list[str]) -> None:
    provs = _net_providers(seen)
    if not provs:
        return
    i = next((j for j, t in enumerate(ans.trace) if str(t).startswith("data_used:")), None)
    rest = str(ans.trace[i]).split(":", 1)[1].strip() if i is not None else "local"
    have = {t for t in re.split(r"[\s,;+()]+", rest.lower()) if t}
    add = [p for p in provs if p not in have]
    if add:
        line = f"data_used: {rest}, {', '.join(add)}"
        ans.trace = [*ans.trace[:i], line, *ans.trace[i + 1:]] if i is not None else [*ans.trace, line]
    if isinstance(ans.data_used, list):
        ans.data_used = [*ans.data_used, *[p for p in provs if p not in ans.data_used]]
    calls = len([p for p in provs if p != "relay"])  # a forwarding detector is not a hosted API call
    if calls and (ans.cost.api_calls or 0) < calls:
        ans.cost.api_calls = calls


def _answer(req: dict) -> dict:
    """One question about one uploaded file -> the Answer as a JSON-ready dict."""
    abs_path, virtual, question = req["abs"], req["virtual"], str(req.get("question", "")).strip()
    solver_name, solver_args = req.get("solver") or "", dict(req.get("solver_args") or {})
    private, eu = bool(req.get("private")), bool(req.get("eu"))
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
            if private:  # every hosted step off, whatever the request or the server's defaults say
                for k in ("gemini", "verify", "api"):
                    if k in known:
                        solver_args[k] = 0
                if "private" in known:
                    solver_args["private"] = 1
            for k, env in (("gemini", "STOA_API_GEMINI"), ("verify", "STOA_API_VERIFY")):
                if k in known and k not in solver_args and os.environ.get(env, "1") != "0":
                    solver_args[k] = 1
        elif private and (solver_args.get("backend") in HOSTED_TEXT or solver_name in ("gemini_agent",)):
            raise SystemExit(f"private mode: '{solver_name}' calls a hosted model; switch private mode off to use it")
        solver = get_solver(solver_name, **solver_args)
        t0 = time.perf_counter()
        # T2 21:21: which outside hosts this answer really reached (stoa.netguard.watch, feature-detected: not on every
        # tree), so the card's 'where your data went' is checked against the network, not only the solver's own words
        try:
            from stoa import netguard as _ng
        except Exception:  # noqa: BLE001
            _ng = None
        if _ng is not None and hasattr(_ng, "blocked"):
            try:
                _ng.blocked(clear=True)  # count only this answer's refusals
            except Exception:  # noqa: BLE001
                pass
        net = _ng.watch() if _ng is not None and hasattr(_ng, "watch") else contextlib.nullcontext(None)
        with CostMeter() as meter, page_on_screen(abs_path, req.get("page")) as on_page, privacy(private, eu) as priv, \
                ask_context(req.get("context")) as ctx_line, net as hosts:
            solve, ctx = solver.solve, req.get("context")
            # F1 region ask (stoa/features/region.py): a drawn box / lasso (>= 3 points) scopes the question; a 2-point
            # region is a LINE to measure in G1's context shape (KNOWLEDGE 20:27), so it stays with the pipeline
            if isinstance(ctx, dict) and isinstance(ctx.get("region"), list) and len(ctx["region"]) >= 3:
                try:
                    from stoa.features.region import upload_solve

                    solve = upload_solve(solver.solve, ctx, req.get("page"))
                except Exception as e:  # noqa: BLE001 (not on this tree / broken: the plain question)
                    print(f"region hook: {e}", file=sys.stderr)
            ans = solve(abs_path, question)
        if not isinstance(ans, Answer):
            ans = Answer.model_validate(ans)
        rep, met = ans.cost.model_dump(), meter.cost.model_dump()
        ans.cost = ans.cost.model_validate({k: max(rep.get(k, 0) or 0, met.get(k, 0) or 0) for k in set(rep) | set(met)})
        if not ans.cost.wall_seconds:
            ans.cost.wall_seconds = round(time.perf_counter() - t0, 4)
        if on_page:
            ans.trace = [f"upload: {on_page}", *ans.trace]
        if priv:
            ans.trace = [f"data: {priv}", *ans.trace]
        if ctx_line:
            ans.trace = [ctx_line, *ans.trace]
        try:
            refused = _ng.blocked(clear=True) if _ng is not None and hasattr(_ng, "blocked") else []
            if refused:
                ans.trace = [*ans.trace, f"private mode: {len(refused)} outside connection(s) refused ({', '.join(dict.fromkeys(map(str, refused)))})"]
                # T2 cycle 7 (H 01:30): a reader private mode kept off (it forwards page images outside) is named on the
                # card, not left as an unexplained guess (stoa.netguard.kept_off; older netguard: nothing)
                for m in (_ng.kept_off(refused) if hasattr(_ng, "kept_off") else []):
                    if m not in (ans.missing or []):
                        ans.missing = [*(ans.missing or []), m]
            seen = sorted(dict.fromkeys(map(str, hosts or [])))
            if seen:
                ans.trace = [*ans.trace, "network: this answer reached " + ", ".join(seen)]
                used = next((str(t) for t in ans.trace if str(t).startswith("data_used:")), "")
                if used and used.split(":", 1)[1].strip() == "local":
                    ans.trace = [*ans.trace, f"data_used says local but the network saw {', '.join(seen)}"]
                _network_is_the_truth(ans, seen)
        except Exception as e:  # noqa: BLE001  (a check, never the answer's problem)
            print(f"netguard: {e}", file=sys.stderr)
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
