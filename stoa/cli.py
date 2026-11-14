"""Command line: ask one question about one drawing, or answer a whole file of questions (batch, for Stoa).

    $JUNCTION_VENV/bin/python -m stoa.cli ask data/cubicasa5k/colorful-10052.png "How many windows?"
    $JUNCTION_VENV/bin/python -m stoa.cli ask <path> "<question>" --solver vlm_direct --json
    $JUNCTION_VENV/bin/python -m stoa.cli intent "Wie viele Fenster sind auf diesem Grundriss eingezeichnet?"

Batch (P1; input = the dataset's questions.jsonl format, one {"path", "question", ...} per line):

    $JUNCTION_VENV/bin/python -m stoa.cli answer --in questions.jsonl --out answers.jsonl [--root DIR] [--workers 4]

`path` is relative to --root (default: the input file's folder when the first path exists there, else $STOA_DATA);
absolute paths work too. Each output line = the input row + value, unit, confidence, evidence, assumptions, missing,
solver, cost (tokens, GPU s, API calls, usd with its GPU / API / CPU split; stoa/cost.py) and error. A re-run with the
same --out skips rows already answered (--fresh starts over). `--api` (= STOA_MODE=api) uses no local GPU model:
local VLM steps go to a hosted model (stoa/llm.py api_route; see deploy/README.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def _kv(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def solve_one(solver, path: str, question: str):
    """(Answer, cost dict, error) for one question: cost = field-wise max of what the solver reported and what the
    LLM client recorded (as in the eval), plus wall seconds and the USD split from stoa/cost.py."""
    import traceback

    from stoa import cost as costmod
    from stoa.contract import Answer, CostMeter

    t0, err = time.perf_counter(), None
    with CostMeter() as m:
        try:
            ans = solver.solve(path, question)
            if not isinstance(ans, Answer):
                ans = Answer.model_validate(ans)
        except Exception as e:  # noqa: BLE001  (one bad row must not stop a batch)
            err = f"{type(e).__name__}: {e}"
            ans = Answer.abstain(getattr(solver, "name", "?"), "exception", [traceback.format_exc(limit=4)[-800:]])
    rep, met = ans.cost.model_dump(), m.cost.model_dump()
    cost = {k: max(rep.get(k, 0) or 0, met.get(k, 0) or 0) for k in set(rep) | set(met)
            if isinstance(rep.get(k, met.get(k)), (int, float))}
    if not cost.get("wall_seconds"):
        cost["wall_seconds"] = round(time.perf_counter() - t0, 4)
    ans.cost = ans.cost.model_validate(cost)
    b = costmod.breakdown(cost)
    cost.update({k: round(v, 7) for k, v in b.items()})
    cost["tier"] = costmod.tier(cost)["tier"]  # cheap | in-between | expensive (Stoa's cost judgement)
    return ans, cost, err


def _resolve(path: str, root: Path | None) -> str:
    """Dataset-relative paths stay relative when the root is $STOA_DATA (solvers resolve them); otherwise absolute."""
    from stoa.data import data_root

    p = Path(path)
    if p.is_absolute() or root is None or root.resolve() == data_root().resolve():
        return path
    return str((root / p).resolve())


def _guess_root(rows: list[dict], inp: Path) -> Path | None:
    first = next((r["path"] for r in rows if r.get("path")), None)
    if first and not Path(first).is_absolute() and (inp.parent / first).exists():
        return inp.parent
    return None  # $STOA_DATA


def cmd_answer(a) -> int:
    from stoa.solvers import get_solver

    inp, out = Path(a.inp), Path(a.out)
    rows = [json.loads(x) for x in inp.read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.limit:
        rows = rows[: a.limit]
    root = Path(a.root) if a.root else _guess_root(rows, inp)
    done: set[tuple[str, str]] = set()
    if out.exists() and not a.fresh:
        for x in out.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(x)
                done.add((d.get("path"), d.get("question")))
            except json.JSONDecodeError:
                pass
    todo = [r for r in rows if (r.get("path"), r.get("question")) not in done]
    print(f"{len(rows)} questions, {len(rows) - len(todo)} already in {out}, {len(todo)} to answer "
          f"(root {root or '$STOA_DATA'}, solver {a.solver}, mode {'api' if os.environ.get('STOA_MODE') == 'api' else 'local'})",
          file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    solver = get_solver(a.solver, **_kv(a.solver_arg))
    lock, totals = threading.Lock(), {"n": 0, "answered": 0, "errors": 0, "usd": 0.0, "gpu_usd": 0.0, "api_usd": 0.0}
    fh = open(out, "w" if a.fresh else "a", encoding="utf-8")

    def one(row: dict) -> None:
        if not row.get("path") or not row.get("question"):
            res = {**row, "value": None, "confidence": 0.0, "error": "row needs 'path' and 'question'"}
        else:
            path = _resolve(row["path"], root)
            ans, cost, err = solve_one(solver, path, row["question"])
            mine = {"value": ans.value, "unit": ans.unit, "confidence": round(ans.confidence, 4),
                    "evidence": [e.model_dump(exclude_none=True) for e in ans.evidence],
                    "assumptions": ans.assumptions, "missing": ans.missing, "solver": ans.solver,
                    "cost": cost, "error": err}
            if a.trace:
                mine["trace"] = ans.trace
            # keep every input field; one we would overwrite (e.g. the reference 'unit') moves to 'input_<key>'
            res = {**{(f"input_{k}" if k in mine else k): v for k, v in row.items()}, **mine}
        with lock:
            fh.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            totals["n"] += 1
            totals["answered"] += res.get("value") is not None
            totals["errors"] += bool(res.get("error"))
            for k in ("usd", "gpu_usd", "api_usd"):
                totals[k] += (res.get("cost") or {}).get(k, 0) or 0
            if not a.quiet:
                v = " ".join(str(res.get("value")).split())[:50]
                print(f"[{totals['n']}/{len(todo)}] {v:50s} conf {res.get('confidence', 0):.2f}"
                      f"  {row.get('question', '')[:60]}", file=sys.stderr)

    t0 = time.time()
    try:
        if a.workers > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(a.workers) as ex:
                list(ex.map(one, todo))
        else:
            for r in todo:
                one(r)
    finally:
        fh.close()
    n = max(totals["n"], 1)
    print(f"done: {totals['n']} answered into {out} in {time.time() - t0:.0f}s; value given {totals['answered']}/{totals['n']}, "
          f"errors {totals['errors']}; cost ${totals['usd']:.4f} (GPU ${totals['gpu_usd']:.4f} + API ${totals['api_usd']:.4f}"
          f" + CPU), ${1000 * totals['usd'] / n:.3f} per 1k questions", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m stoa.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("ask", help="answer a question about a drawing")
    a1.add_argument("path", help="dataset-relative path, e.g. data/swiss_dwellings/<file>.pdf, or an absolute path")
    a1.add_argument("question")
    # P1: `upload` = the pipeline behind stoa.formats.normalize (DWG/SVG/HEIC/multi-page PDF); same as `pipeline`
    # on files that need no conversion
    a1.add_argument("--solver", default="upload")
    a1.add_argument("--solver-arg", action="append", default=[])
    a1.add_argument("--json", action="store_true", help="print the full Answer JSON")
    a1.add_argument("--api", action="store_true", help="API-only mode (STOA_MODE=api): no local GPU model")
    a2 = sub.add_parser("intent", help="show the parsed intent of a question")
    a2.add_argument("question")
    a3 = sub.add_parser("answer", help="answer every question of a jsonl file (dataset format) into a jsonl file")
    a3.add_argument("--in", dest="inp", required=True, help='jsonl, one {"path": ..., "question": ...} per line')
    a3.add_argument("--out", required=True, help="jsonl to write (appended; rows already there are skipped)")
    a3.add_argument("--root", help="folder the paths are relative to (default: the input's folder if the paths exist "
                                   "there, else $STOA_DATA)")
    a3.add_argument("--solver", default="upload")
    a3.add_argument("--solver-arg", action="append", default=[])
    a3.add_argument("--workers", type=int, default=4)
    a3.add_argument("--limit", type=int, default=0)
    a3.add_argument("--fresh", action="store_true", help="overwrite --out instead of resuming")
    a3.add_argument("--trace", action="store_true", help="include each answer's reasoning trace")
    a3.add_argument("--quiet", action="store_true")
    a3.add_argument("--api", action="store_true", help="API-only mode (STOA_MODE=api): no local GPU model")
    a = ap.parse_args(argv)
    if getattr(a, "api", False):
        os.environ["STOA_MODE"] = "api"

    if a.cmd == "intent":
        from stoa.router.intent import parse

        print(json.dumps(parse(a.question).to_dict(), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "answer":
        return cmd_answer(a)

    from stoa import cost as costmod
    from stoa.data import abspath
    from stoa.solvers import get_solver

    if not Path(a.path).is_absolute() and Path(a.path).is_file() and not abspath(a.path).is_file():
        # RL 16:16: 'ask samples/x.pdf "…"' from a fresh clone: a path that exists relative to the CWD wins over $STOA_DATA
        a.path = str(Path(a.path).resolve())
    if not abspath(a.path).is_file():
        print(f"no such file: {a.path}", file=sys.stderr)
        return 2
    solver = get_solver(a.solver, **_kv(a.solver_arg))
    ans, cost, _err = solve_one(solver, a.path, a.question)
    if a.json:
        print(ans.model_dump_json(indent=1))
        return 0
    print(f"answer: {ans.value} {ans.unit or ''}   (confidence {ans.confidence:.2f}, solver {ans.solver})")
    for x in ans.assumptions:
        print(f"  assumes: {x}")
    for x in ans.missing:
        print(f"  missing: {x}")
    for e in ans.evidence[:20]:
        where = f"guid {e.ifc_guid}" if e.ifc_guid else f"page {e.page} bbox {[round(v, 1) for v in (e.bbox or [])]} {e.bbox_space or ''}"
        print(f"  evidence: {e.file} {where}  {e.note}")
    if len(ans.evidence) > 20:
        print(f"  ... {len(ans.evidence) - 20} more evidence items")
    print("trace:")
    for t in ans.trace:
        print(f"  - {t}")
    c = ans.cost
    print(f"cost: {c.llm_calls} LLM calls ({c.api_calls} hosted), tokens {c.llm_tokens_in}/{c.llm_tokens_out}, "
          f"GPU {c.gpu_seconds:.2f}s, wall {c.wall_seconds:.2f}s; {costmod.describe(cost)}")
    t = costmod.tier(cost)
    print(f"cost tier: {t['label']} ({t['why']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
