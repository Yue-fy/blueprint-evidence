"""Run model-written pandas code on our own tables without letting it touch anything else (audit 19:51, risk #3).

Table QA (stoa/solvers/vector/tableqa.py) asks a local model to write a few lines of pandas over tables we
extracted, then runs them. The question text and the drawing's own text reach that prompt, so the code is
untrusted: before this module, `pd.read_csv('<any path>')` read a canary file on the captain's machine.

Four layers, each enough to stop the audit's canary on its own:
1. `check(code)`: an AST allow-list. No import / class / global / with / yield / async; no names or attributes
   starting with '_'; no frame / code introspection (gi_frame, f_globals, tb_frame, co_*); no reflective
   builtins (open, eval, exec, compile, getattr, globals, ...); no attribute that reads, writes or converts
   through files (read_* / to_* / from_* except the in-memory ones table QA needs: to_numeric, to_list, ...);
   no string constant naming such a method or containing '__' (pandas dispatches strings: df.agg('to_csv'));
   `.query(...)` only with a literal string that passes the same check; no huge constants or powers.
2. Modules are proxies (`pd`, `re`, `math`, `statistics`) that expose only what table QA uses (pd.to_numeric,
   pd.isna, pd.DataFrame, ...): no pd.io, no statistics.sys, no re.enum.
3. While the code runs, a thread-scoped audit hook (PEP 578) refuses file opens outside the Python library
   folders, every os.* filesystem/process call, sockets, subprocesses, ctypes, shutil, tempfile and sqlite
   (PermissionError), so a string built at run time (df.agg('to_' + 'csv', path)) still writes nothing.
4. A deadline (default 3 s, STOA_SANDBOX_SECONDS) on the code's own lines: `while True: pass` raises TimeoutError.
Only the given DataFrames (copies) are in the namespace.

Residual risk, written down: this is in-process. A C-level allocation in one call ('x' * n with a run-time n)
can still use memory or time until it returns; the deadline only interrupts between lines of the model's code.
"""

from __future__ import annotations

import ast
import math
import os
import re
import statistics
import sys
import sysconfig
import threading
import time
from typing import Any

import pandas as pd

TIME_LIMIT_S = float(os.environ.get("STOA_SANDBOX_SECONDS", "3"))

FORBIDDEN_NAMES = {  # reflective builtins; none is in the namespace, this also stops the code naming them
    "open", "eval", "exec", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
    "breakpoint", "help", "memoryview", "super", "__import__", "__builtins__",
}
# in-memory conversions table QA needs; every other read_* / to_* / from_* name is I/O or a format writer
IO_OK = {"to_numeric", "to_datetime", "to_timedelta", "to_list", "to_numpy", "to_dict", "to_frame", "to_records",
         "from_dict", "from_records", "to_period", "to_timestamp", "to_pydatetime", "to_offset"}
IO_PREFIX = re.compile(r"^(read|to|from)_")
FORBIDDEN_ATTRS = {
    # introspection that leads back to real globals / builtins
    "mro", "f_globals", "f_locals", "f_back", "f_builtins", "f_code", "gi_frame", "gi_code", "gi_yieldfrom",
    "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "tb_frame", "tb_next", "func_globals",
    "format_map", "with_traceback",
    # writers / renderers that are not prefixed to_ (numpy, matplotlib through df.plot, Styler)
    "tofile", "dump", "dumps", "save", "savefig", "plot", "hist", "boxplot", "style", "pipe",
    "eval",  # df.eval / pd.eval: a second expression language; query() is enough and is checked below
}
FRAME_ATTR = re.compile(r"^(f|gi|cr|ag|tb|co)_")
# Try: a caught timeout would switch the deadline off (CPython drops a trace function that raised)
FORBIDDEN_NODES = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal,
                   ast.With, ast.AsyncWith, ast.AsyncFor, ast.Await, ast.Yield, ast.YieldFrom, ast.Try,
                   getattr(ast, "TryStar", ast.Try))
FORMAT_FIELD = re.compile(r"\{[^}:!]*[.\[]")  # "{0.gi_frame}".format(x): attribute lookups inside format strings
MAX_CONST = 10 ** 9


def _io_name(s: str) -> bool:
    return bool(IO_PREFIX.match(s)) and s not in IO_OK


def _check_tree(tree: ast.AST, where: str = "code") -> None:
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            raise ValueError(f"{where} uses a forbidden construct ({type(node).__name__})")
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id in FORBIDDEN_NAMES:
                raise ValueError(f"{where} uses a forbidden name ({node.id})")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("_") or a in FORBIDDEN_ATTRS or FRAME_ATTR.match(a) or _io_name(a):
                raise ValueError(f"{where} uses a forbidden attribute (.{a}): only the given DataFrames, in memory")
        elif isinstance(node, (ast.FunctionDef, ast.Lambda)):
            args = node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x]:
                if a.arg.startswith("_"):
                    raise ValueError(f"{where} uses a forbidden name ({a.arg})")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
                raise ValueError(f"{where} uses a forbidden name ({node.name})")
        elif isinstance(node, ast.keyword):
            if node.arg and (node.arg.startswith("_") or node.arg in ("buf", "path_or_buf", "filepath_or_buffer")):
                raise ValueError(f"{where} uses a forbidden argument ({node.arg}=)")
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, str) and ("__" in v or _io_name(v.strip()) or v.strip() in FORBIDDEN_ATTRS
                                       or FORMAT_FIELD.search(v)):
                raise ValueError(f"{where} uses a forbidden string ({v[:40]!r})")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > MAX_CONST:
                raise ValueError(f"{where} uses a too large constant")
            if isinstance(v, (bytes, bytearray)):
                raise ValueError(f"{where} uses a bytes literal")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            e = node.right
            if not (isinstance(e, ast.Constant) and isinstance(e.value, (int, float)) and abs(e.value) <= 4):
                raise ValueError(f"{where} uses a power with a large or computed exponent")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "query":
            if not node.args or not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                raise ValueError(f"{where}: .query() takes a literal string only")
            q = node.args[0].value
            if "@" in q or "`" in q:
                raise ValueError(f"{where}: .query() may not reference variables (@) or quoted names (`)")
            try:
                sub = ast.parse(q, mode="eval")
            except SyntaxError:
                continue  # pandas will raise its own error; nothing runs
            _check_tree(sub, where=".query() string")
            for n in ast.walk(sub):
                if isinstance(n, ast.Call):
                    raise ValueError(f"{where}: .query() string may not call functions")


def check(code: str) -> ast.Module:
    """Raise ValueError (message suitable to feed back to the model) unless `code` passes the allow-list."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"code does not parse: {e.msg} (line {e.lineno})") from None
    _check_tree(tree)
    return tree


class _Proxy:
    """A module with only the listed attributes (AST rule: no '_' attributes, so the proxy's own fields are out of
    reach). Nested proxies for sub-namespaces (pd.api.types)."""

    __slots__ = ("_name", "_ns")

    def __init__(self, name: str, ns: dict[str, Any]):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_ns", ns)

    def __getattr__(self, attr: str) -> Any:
        ns = object.__getattribute__(self, "_ns")
        if attr in ns:
            return ns[attr]
        raise AttributeError(f"{object.__getattribute__(self, '_name')}.{attr} is not available in table QA")

    def __setattr__(self, attr: str, value: Any) -> None:
        raise AttributeError("read-only")

    def __repr__(self) -> str:
        return f"<{object.__getattribute__(self, '_name')} (table QA subset)>"


def _pick(mod: Any, names: list[str]) -> dict[str, Any]:
    return {n: getattr(mod, n) for n in names if hasattr(mod, n)}


PD_NAMES = ["DataFrame", "Series", "Index", "NA", "NaT", "isna", "isnull", "notna", "notnull", "to_numeric",
            "to_datetime", "to_timedelta", "concat", "merge", "cut", "qcut", "unique", "Timestamp", "Timedelta",
            "Categorical", "factorize", "get_dummies", "crosstab", "pivot_table", "melt", "wide_to_long",
            "date_range", "Interval", "IntervalIndex", "MultiIndex", "Int64Dtype", "Float64Dtype", "StringDtype",
            "BooleanDtype", "CategoricalDtype"]
PD_TYPES = ["is_numeric_dtype", "is_string_dtype", "is_object_dtype", "is_float_dtype", "is_integer_dtype",
            "is_bool_dtype", "is_datetime64_any_dtype", "is_categorical_dtype", "is_list_like", "is_scalar",
            "is_number"]
RE_NAMES = ["search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split", "escape", "compile",
            "IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "VERBOSE", "X", "ASCII", "A", "UNICODE", "U"]
MATH_NAMES = [n for n in dir(math) if not n.startswith("_")]
STAT_NAMES = ["mean", "median", "median_low", "median_high", "mode", "multimode", "stdev", "pstdev", "variance",
              "pvariance", "fmean", "geometric_mean", "harmonic_mean", "quantiles"]


def proxies() -> dict[str, Any]:
    types = _Proxy("pd.api.types", _pick(pd.api.types, PD_TYPES))
    api = _Proxy("pd.api", {"types": types})
    return {"pd": _Proxy("pd", {**_pick(pd, PD_NAMES), "api": api}), "re": _Proxy("re", _pick(re, RE_NAMES)),
            "math": _Proxy("math", _pick(math, MATH_NAMES)),
            "statistics": _Proxy("statistics", _pick(statistics, STAT_NAMES))}


# ---- layer 3: the audit hook (process-wide once, active only on a thread inside run()) ----------------------------

_TL = threading.local()
_ACTIVE = [0]  # threads inside run(): the hook returns at once when 0 (it sees every audit event of the process)
_HOOKED = False
_HOOK_LOCK = threading.Lock()


def _lib_dirs() -> tuple[str, ...]:
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    for k in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_paths().get(k)
        if p:
            dirs.add(p)
    dirs.update(p for p in sys.path if p and "site-packages" in p)
    return tuple(sorted({os.path.realpath(d) + os.sep for d in dirs if d}))


LIB_DIRS: tuple[str, ...] = ()
BLOCK_PREFIX = ("os.", "socket.", "subprocess.", "ctypes.", "shutil.", "tempfile.", "sqlite3.", "urllib.",
                "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.", "nntplib.", "telnetlib.", "webbrowser.",
                "glob.", "winreg.", "msvcrt.", "_winapi.", "fcntl.", "resource.", "syslog.", "pty.", "mmap.")
# not sys.settrace: CPython raises that event itself when it drops a trace function that raised (our deadline)
BLOCK_EVENTS = {"builtins.input", "builtins.breakpoint", "sys.addaudithook",
                "sys.unraisablehook", "sys.excepthook", "cpython.run_command", "cpython.run_file",
                "cpython.run_module", "cpython.run_startup", "pickle.find_class", "code.__new__",
                "gc.get_referrers", "gc.get_referents", "gc.get_objects"}
LIB_READ_EVENTS = {"os.listdir", "os.scandir"}  # the import system lists library folders for lazy pandas imports


def _in_lib(path: Any) -> bool:
    if isinstance(path, int):  # an already-open descriptor (os.fdopen by the import system)
        return False
    try:
        p = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError):
        return False
    return p.startswith(LIB_DIRS) and ".env" not in os.path.basename(p)


def _hook(event: str, args: tuple) -> None:
    if not _ACTIVE[0] or not getattr(_TL, "on", False):
        return
    if event == "open":
        path, mode = args[0], (args[1] or "r")
        if isinstance(mode, str) and set(mode) & set("wax+"):
            raise PermissionError("table QA code may not write files")
        if not _in_lib(path):
            raise PermissionError("table QA code may not read files")
        return
    if event in LIB_READ_EVENTS:
        if args and _in_lib(args[0]):
            return
        raise PermissionError(f"table QA code may not list folders ({event})")
    if event in BLOCK_EVENTS or event.startswith(BLOCK_PREFIX):
        raise PermissionError(f"table QA code may not use {event}")


def _install_hook() -> None:
    global _HOOKED, LIB_DIRS
    if _HOOKED:
        return
    with _HOOK_LOCK:
        if not _HOOKED:
            LIB_DIRS = _lib_dirs()
            sys.addaudithook(_hook)
            _HOOKED = True


# ---- layer 4: the deadline ------------------------------------------------------------------------------------------

FILENAME = "<tableqa>"


class _Deadline(BaseException):
    """Not an Exception: pandas' own `except Exception` fallbacks must not swallow it and run the code again."""


def run(code: str, tables: dict[str, pd.DataFrame], builtins: dict[str, Any],
        time_limit: float | None = None) -> dict[str, Any]:
    """Check, then run `code` with the proxies, the given tables (copies) and `builtins`; return the namespace.
    Raises ValueError (refused by the check), PermissionError (refused at run time), TimeoutError, or whatever
    the code itself raised."""
    check(code)
    compiled = compile(code, FILENAME, "exec")
    env: dict[str, Any] = {**proxies(), "__builtins__": dict(builtins)}
    env.update({k: v.copy() for k, v in tables.items()})
    limit = TIME_LIMIT_S if time_limit is None else time_limit
    deadline = time.monotonic() + limit

    def local(frame, event, arg):  # noqa: ANN001
        if time.monotonic() > deadline:
            raise _Deadline
        return local

    def tracer(frame, event, arg):  # noqa: ANN001
        if frame.f_code.co_filename == FILENAME:
            if time.monotonic() > deadline:
                raise _Deadline
            return local
        return None

    _install_hook()
    old = sys.gettrace()
    sys.settrace(tracer)  # before _TL.on: the hook refuses sys.settrace while the code runs
    with _HOOK_LOCK:
        _ACTIVE[0] += 1
    _TL.on = True
    try:
        exec(compiled, env)  # noqa: S102 - checked AST, proxies, audit hook, deadline (module docstring)
    except _Deadline:
        raise TimeoutError(f"table QA code ran over {limit:g} s; use vectorised pandas, no loops") from None
    finally:
        _TL.on = False
        with _HOOK_LOCK:
            _ACTIVE[0] -= 1
        sys.settrace(old)
    return env
