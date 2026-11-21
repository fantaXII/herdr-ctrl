#!/usr/bin/env python3
"""Build dist/hctrl.pyz: hctrl + pure-Python PyYAML in one executable zipapp.

Usage: python3 scripts/build_pyz.py
Standard library only. PyYAML is fetched from PyPI as a pinned sdist and checked by sha256.
The result runs anywhere with python3 >= 3.10: `./hctrl.pyz --version`.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "pyz"
OUT = ROOT / "dist" / "hctrl.pyz"
# Bump together (https://pypi.org/pypi/PyYAML/json -> urls[packagetype=sdist]).
PYYAML_URL = ("https://files.pythonhosted.org/packages/76/b8/"
              "c0b780928e2fb1c87e9e375818c6f121da2398b071012d1747be300b83f4/pyyaml-6.0.4.tar.gz")
PYYAML_SHA256 = "e24bf71af8a432d7808fabff134d38dfe16d3cbb41f2873bb4b1e42422de0a1a"

MAIN = '''import sys
from herdr_ctl.cli import main
sys.exit(main())
'''


def vendor_yaml(dest: Path) -> None:
    """Copy PyYAML's pure-Python package (no C extension: it is platform specific,
    and hctrl only uses safe_load/safe_dump, which fall back to pure Python)."""
    with urllib.request.urlopen(PYYAML_URL, timeout=60) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != PYYAML_SHA256:
        sys.exit(f"build_pyz: PyYAML sha256 mismatch ({got})")
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = [m for m in tf.getmembers()
                   if "/lib/yaml/" in m.name and m.name.endswith(".py")]
        if not members:
            sys.exit("build_pyz: no lib/yaml/*.py inside the PyYAML sdist")
        for m in members:
            rel = m.name.split("/lib/", 1)[1]          # yaml/xxx.py
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tf.extractfile(m).read())
        lic = next((m for m in tf.getmembers() if m.name.endswith("/LICENSE")), None)
        if lic:
            (dest / "yaml" / "LICENSE").write_bytes(tf.extractfile(lic).read())


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    shutil.copytree(ROOT / "src" / "herdr_ctl", BUILD / "herdr_ctl",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    vendor_yaml(BUILD)
    (BUILD / "__main__.py").write_text(MAIN)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(BUILD, OUT, interpreter="/usr/bin/env python3", compressed=True)
    print(f"built {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
