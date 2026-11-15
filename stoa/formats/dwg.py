"""DWG uploads -> DXF (P1; captain 14 Nov 12:52 'DWG: convert to DXF with LibreDWG').

AutoCAD's binary DWG has no Python reader; every solver here reads DXF (ezdxf). to_dxf() runs the first converter it
finds and caches <stem>.dxf next to the upload (or in out_dir):

  1. $STOA_DWG2DXF                      path of a LibreDWG `dwg2dxf` binary
  2. `dwg2dxf` on PATH                  LibreDWG (GPL-3, run as a separate program; nothing is linked)
  3. .venv-dwg/bin/dwg2dxf   the user-space env the captain may create:
                                        micromamba create -p .venv-dwg -c conda-forge libredwg
  4. `ODAFileConverter` on PATH         ODA File Converter (free, closed source)

No converter -> DwgConverterMissing with a sentence the UI can show as is. W5's web/server/normalize_upload.py imports
to_dxf (inbox W5 13:20). Nothing is installed on the team box today (the micromamba download was denied, 13:00).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ENV_BIN = Path(".venv-dwg/bin/dwg2dxf")
# AutoCAD release by the 6-byte magic at the start of a DWG (what the error message names)
VERSIONS = {"AC1009": "R11/R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004",
            "AC1021": "2007", "AC1024": "2010", "AC1027": "2013", "AC1032": "2018"}
MISSING = ("This is an AutoCAD DWG file, and no DWG converter is installed on the server. "
           "Please save it as DXF (AutoCAD: Save As > DXF) or print the sheet to PDF and upload that.")


class DwgConverterMissing(RuntimeError):
    """No dwg2dxf / ODA converter on this machine (message is user-readable)."""


class DwgConvertError(RuntimeError):
    """A converter ran but produced no readable DXF (message is user-readable)."""


def dwg_version(path: str | Path) -> str | None:
    """'2018' etc. from the file's magic bytes; None if it is not a DWG."""
    try:
        head = Path(path).read_bytes()[:6].decode("ascii", "replace")
    except OSError:
        return None
    return VERSIONS.get(head) or (head if head.startswith("AC10") else None)


def converter() -> tuple[str, str] | None:
    """(kind, binary) of the first available converter, or None."""
    env = os.environ.get("STOA_DWG2DXF")
    if env and Path(env).exists():
        return "libredwg", env
    found = shutil.which("dwg2dxf")
    if found:
        return "libredwg", found
    if ENV_BIN.exists():
        return "libredwg", str(ENV_BIN)
    oda = shutil.which("ODAFileConverter")
    if oda:
        return "oda", oda
    return None


def _usable(dxf: Path) -> bool:
    if not dxf.exists() or dxf.stat().st_size < 100:
        return False
    try:
        import ezdxf

        ezdxf.readfile(str(dxf))
        return True
    except Exception:  # noqa: BLE001 — LibreDWG output is sometimes only readable in recover mode
        try:
            from ezdxf import recover

            recover.readfile(str(dxf))
            return True
        except Exception:  # noqa: BLE001
            return False


def to_dxf(path: str | Path, out_dir: str | Path | None = None, timeout: float = 120) -> str:
    """Path of a DXF with the same drawing (cached: reused while newer than the DWG)."""
    src = Path(path)
    # <stem>.dwg.dxf: never overwrite a DXF the visitor uploaded next to the DWG under the same name
    dst = Path(out_dir or src.parent) / f"{src.stem}.dwg.dxf"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and _usable(dst):
        return str(dst)
    ver = dwg_version(src)
    if ver is None:
        raise DwgConvertError(f"{src.name} is not a DWG file (no AutoCAD 'AC10xx' header).")
    conv = converter()
    if conv is None:
        raise DwgConverterMissing(MISSING)
    kind, binary = conv
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "libredwg":
            # -y overwrite, -o output; LibreDWG writes R2000 DXF by default (ezdxf reads it)
            r = subprocess.run([binary, "-y", "-o", str(dst), str(src)], capture_output=True, text=True, timeout=timeout)
            log = (r.stderr or r.stdout or "")[-300:]
        else:  # ODA: ODAFileConverter <in dir> <out dir> <version> <type> <recurse> <audit> [filter]
            with tempfile.TemporaryDirectory() as tin, tempfile.TemporaryDirectory() as tout:
                shutil.copy2(src, Path(tin) / src.name)
                r = subprocess.run([binary, tin, tout, "ACAD2018", "DXF", "0", "1", src.name],
                                   capture_output=True, text=True, timeout=timeout)
                log = (r.stderr or r.stdout or "")[-300:]
                made = Path(tout) / f"{src.stem}.dxf"
                if made.exists():
                    shutil.move(str(made), dst)
    except subprocess.TimeoutExpired as e:
        raise DwgConvertError(f"Converting the DWG ({ver}) took longer than {timeout:.0f} s.") from e
    if not _usable(dst):
        raise DwgConvertError(f"The DWG (AutoCAD {ver}) could not be converted to DXF ({kind}: {log.strip()[:200]}). "
                              "Please save it as DXF or print it to PDF.")
    try:
        fix_converted(dst)
    except Exception:  # noqa: BLE001 — keep the converter's file as it is
        pass
    return str(dst)


def fix_converted(dxf: str | Path) -> list[str]:
    """Repair two LibreDWG artefacts in a converted DXF (P1 14:58, a Swiss DXF -> DWG -> DXF round trip gave apartment
    area 82.36 -> 0); returns what was fixed.
    1. UTF-8 bytes in an R2000 DXF whose header names an ANSI code page: ezdxf decodes them as cp1252 ('mÂ²',
       'Chambre Ã  coucher'), so room labels and printed areas stop matching. When the bytes are valid UTF-8 with
       non-ASCII text and the DXF is older than R2007 (UTF-8 by definition), read it as UTF-8. A genuinely cp1252
       file is not valid UTF-8 and is read as before.
    2. TEXT with a horizontal/vertical alignment but align_point (0, 0, 0) while insert is elsewhere: readers place
       aligned text at align_point, so every centred label ('App. A') sat at the origin. Use insert."""
    import re

    import ezdxf

    raw = Path(dxf).read_bytes()
    fixed = []
    # 3. (P1 c15, QA 02:02) LibreDWG cuts a long MTEXT into fixed-size BYTE chunks (group codes 3 / 1), sometimes inside
    #    a multi-byte UTF-8 character ('...\xe2\x80' | '\xa2...' = the bullet in 'GENERAL DEMO NOTES'); ezdxf decodes
    #    each chunk alone, so the text held lone surrogates and PyMuPDF's insert_text raised: the whole DWG upload failed.
    #    Fixed in the bytes (the character's first bytes move to the next chunk): ezdxf cannot always re-save a
    #    LibreDWG file (this one: 'ByLayer' material missing). STOA_DWG_TEXT_FIX=0 = off.
    if not raw.isascii() and os.environ.get("STOA_DWG_TEXT_FIX", "1") != "0":
        raw2, k = _rejoin_utf8(raw)
        if k:
            _write_atomic(Path(dxf), raw2)
            raw = raw2
            fixed.append(f"{k} texts with a character cut in two by the converter repaired")
    enc = None
    if not raw.isascii():
        try:
            raw.decode("utf-8")
            m = re.search(rb"\$ACADVER\s*\r?\n\s*1\r?\n\s*(AC\d{4})", raw)
            if not (m and m.group(1) >= b"AC1021"):
                enc = "utf-8"
        except UnicodeDecodeError:
            pass
    del raw
    doc = ezdxf.readfile(str(dxf), encoding=enc) if enc else ezdxf.readfile(str(dxf))
    todo = ["text read as UTF-8"] if enc else []
    n = 0
    for e in doc.modelspace().query("TEXT"):
        if (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)) and e.dxf.hasattr("align_point"):
            ap, ins = e.dxf.align_point, e.dxf.insert
            if abs(ap.x) + abs(ap.y) < 1e-12 and abs(ins.x) + abs(ins.y) > 1e-9:
                e.dxf.align_point = ins
                n += 1
    if n:
        todo.append(f"{n} aligned texts placed at their insert point")
    if todo:
        # P1 c15: saved to a temp file first: an ezdxf save that fails half-way (a LibreDWG file it cannot write back)
        # used to leave a 0-byte DXF behind, which the caller then served as 'not a DXF file'
        tmp = Path(dxf).with_name(f".{Path(dxf).name}.{os.getpid()}.tmp")
        try:
            doc.saveas(str(tmp))
            os.replace(tmp, dxf)
            fixed += todo
        except Exception:  # noqa: BLE001 — keep the (byte-repaired) converter file
            with contextlib.suppress(OSError):
                tmp.unlink()
    return fixed


# a value line ending in the first 1-3 bytes of a UTF-8 character, then group code 1 or 3 (the next MTEXT chunk),
# then a value line starting with the character's remaining continuation bytes
_SPLIT = re.compile(rb"([\xc2-\xf4][\x80-\xbf]{0,2})(\r?\n[ \t]*[13]\r?\n)([\x80-\xbf]{1,3})")


def _rejoin_utf8(raw: bytes) -> tuple[bytes, int]:
    """(bytes, n): UTF-8 characters cut in two between MTEXT chunks moved whole into the next chunk."""
    n = 0

    def fix(m):
        nonlocal n
        lead, code, cont = m.group(1), m.group(2), m.group(3)
        need = 2 if lead[0] < 0xE0 else 3 if lead[0] < 0xF0 else 4
        take = need - len(lead)
        if take < 1 or take > len(cont):
            return m.group(0)
        try:
            (lead + cont[:take]).decode("utf-8")
        except UnicodeDecodeError:
            return m.group(0)
        n += 1
        return code + lead + cont

    return _SPLIT.sub(fix, raw), n


def _write_atomic(dst: Path, data: bytes) -> None:
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
