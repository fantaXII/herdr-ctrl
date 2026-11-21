"""How hctrl re-invokes itself (control pane, _hc menu, background workers, claude hooks).

Two install shapes:
- package on disk (pipx / `pip install -e .` / tests): `python -m herdr_ctl.cli`
- single-file zipapp (hctrl.pyz from install.sh): the package lives inside the
  archive, so a plain `python -m` would not find it -> run the archive itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Directory that holds the herdr_ctl package: `src/` (or site-packages), or the .pyz file.
PKG_PARENT = Path(__file__).resolve().parent.parent


def is_zipapp() -> bool:
    return PKG_PARENT.is_file()


def self_cmd() -> list[str]:
    """argv prefix that runs hctrl again, whatever the install shape."""
    if is_zipapp():
        return [sys.executable, str(PKG_PARENT)]
    return [sys.executable, "-m", "herdr_ctl.cli"]
