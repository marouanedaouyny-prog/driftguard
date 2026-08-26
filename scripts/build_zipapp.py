"""Build a zero-dependency zipapp: dist/driftguard.pyz.

Stdlib-only alternative to PyInstaller (ARCHITECTURE §6, Phase 5): the
package is pure Python, so `python -m zipapp` produces a single-file
executable that runs on any machine with Python 3.11+.

Usage:  python scripts/build_zipapp.py
Smoke:  python dist/driftguard.pyz --version
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGING = DIST / "_staging"
INTERPRETER = "/usr/bin/env python3"


def _clean() -> None:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)


def _stage() -> None:
    shutil.copytree(ROOT / "driftguard", STAGING / "driftguard")
    for junk in ("__pycache__", "*.pyc"):
        for hit in STAGING.rglob(junk):
            if hit.is_dir():
                shutil.rmtree(hit)
            else:
                hit.unlink()
    (STAGING / "__main__.py").write_text(
        "from driftguard.__main__ import main\n"
        "import sys\n"
        "sys.exit(main(sys.argv[1:]))\n",
        encoding="utf-8")


def main() -> int:
    _clean()
    _stage()
    out = DIST / "driftguard.pyz"
    zipapp.create_archive(STAGING, out, interpreter=INTERPRETER)
    shutil.rmtree(STAGING)
    print(f"built {out}")
    smoke = subprocess.run([sys.executable, str(out), "--version"],
                           capture_output=True, text=True)
    if smoke.returncode != 0:
        print(f"smoke failed: {smoke.stderr}", file=sys.stderr)
        return 1
    print(f"smoke ok: {smoke.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())