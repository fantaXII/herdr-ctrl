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


def preload() -> None:
    """Zipapp only: import every module now. `hctrl update` may replace the .pyz under a running
    process (the _hc watcher, flow workers, the settings server); zipimport would then read the new
    archive with the old offsets on the next lazy import and fail ("bad local file header")."""
    if not is_zipapp():
        return
    import importlib
    import pkgutil
    for top in ("herdr_ctl", "yaml"):
        pkg = importlib.import_module(top)
        for info in pkgutil.walk_packages(pkg.__path__, top + "."):
            if info.name.endswith("__main__"):
                continue
            try:
                importlib.import_module(info.name)
            except ImportError:  # optional parts (e.g. yaml's C loader) may be missing
                pass


def self_cmd() -> list[str]:
    """argv prefix that runs hctrl again, whatever the install shape."""
    if is_zipapp():
        return [sys.executable, str(PKG_PARENT)]
    return [sys.executable, "-m", "herdr_ctl.cli"]
