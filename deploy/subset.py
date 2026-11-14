"""Copy the part of the dataset a public demo needs into a folder with the dataset's layout (P1).

The full dataset is 12 GB; the demo shows ~15 drawings (web/fixtures). This copies every drawing the fixtures name,
its other formats (same stem: PDF + DXF + PNG twins; an ifc_models sheet's parent .ifc), every sheet cut from the same
IFC model, the questions.jsonl rows on those files, and the dataset README (licences). Anything in --extra is added
the same way. Nothing is uploaded: the captain uploads the folder (deploy/README.md).

    $JUNCTION_VENV/bin/python deploy/subset.py --out /path/to/stoa-demo-data [--extra data/habs/x.jpg ...]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PATH_RE = re.compile(r'data/[A-Za-z0-9_\-]+/[^"\s\\]+?\.(?:pdf|png|jpe?g|ifc|dxf|svg)', re.I)


def fixture_paths(folder: Path) -> set[str]:
    out: set[str] = set()
    for f in glob.glob(str(folder / "*.json")):
        out |= set(PATH_RE.findall(Path(f).read_text(encoding="utf-8", errors="replace")))
    return out


def main(argv=None) -> int:
    sys.path.insert(0, str(REPO))
    from stoa.data import data_root, siblings

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixtures", default=str(REPO / "web" / "fixtures"))
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--max-gb", type=float, default=3.0, help="stop if the copy would exceed this")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    root, out = data_root(), Path(a.out)
    want = fixture_paths(Path(a.fixtures)) | set(a.extra)
    files: set[str] = set()
    for p in sorted(want):
        if not (root / p).is_file():
            print(f"skip (not in dataset): {p}", file=sys.stderr)
            continue
        files.add(p)
        files |= set(siblings(p).values())
        if p.startswith("data/ifc_models/") and p.endswith(".ifc"):  # every sheet cut from this model
            stem = Path(p).stem
            files |= {f"data/ifc_models/{q.name}" for q in (root / "data/ifc_models").glob(f"{stem}_*")}
        m = re.match(r"data/(swiss_dwellings)/(site\d+)_|data/(habs)/(\w+?)_sheet_", p)
        if m:  # the demo offers the other floors of a Swiss site / other sheets of a HABS survey
            src, prefix = (m.group(1), m.group(2) + "_") if m.group(1) else (m.group(3), m.group(4) + "_sheet_")
            files |= {f"data/{src}/{q.name}" for q in (root / "data" / src).glob(f"{prefix}*")}
    files = {f for f in files if (root / f).is_file()}
    size = sum((root / f).stat().st_size for f in files)
    print(f"{len(files)} files, {size / 1e9:.2f} GB from {root}", file=sys.stderr)
    if size > a.max_gb * 1e9:
        print(f"over --max-gb {a.max_gb}; nothing copied", file=sys.stderr)
        return 1
    rows = [x for x in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip() and json.loads(x).get("path") in files]
    print(f"{len(rows)} questions on those files", file=sys.stderr)
    if a.dry_run:
        for f in sorted(files):
            print(f)
        return 0
    for f in sorted(files):
        dst = out / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / f, dst)
    (out / "questions.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    for name in ("README.md", "LICENSE", "LICENSES.md"):
        if (root / name).is_file():
            shutil.copy2(root / name, out / name)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
