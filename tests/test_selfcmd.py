"""hctrl re-invokes itself correctly both from a package dir and from a single-file zipapp."""
import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

from herdr_ctl import selfcmd, ui

PKG = Path(__file__).resolve().parents[1] / "src" / "herdr_ctl"


def test_package_dir_uses_module():
    assert not selfcmd.is_zipapp()
    assert selfcmd.self_cmd() == [sys.executable, "-m", "herdr_ctl.cli"]


def test_ui_page_is_readable():
    assert b"<html" in ui.PAGE.read_bytes().lower()


def test_zipapp_runs_itself(tmp_path):
    src = tmp_path / "app"
    shutil.copytree(PKG, src / "herdr_ctl", ignore=shutil.ignore_patterns("__pycache__"))
    (src / "__main__.py").write_text("import sys\nfrom herdr_ctl.cli import main\nsys.exit(main())\n")
    pyz = tmp_path / "bin" / "hctrl"
    pyz.parent.mkdir()
    zipapp.create_archive(src, pyz, interpreter="/usr/bin/env python3")
    (tmp_path / "bin" / "herdr_ctl").symlink_to("hctrl")  # install.sh's second name

    probe = ("import subprocess; from herdr_ctl import selfcmd, ui; c = selfcmd.self_cmd(); "
             "print(c[1]); print(len(ui.PAGE.read_bytes()) > 0); "
             "print(subprocess.run(c + ['--version'], capture_output=True, text=True).stdout.strip())")
    for entry in ("hctrl", "herdr_ctl"):
        env = dict(os.environ, PYTHONPATH=str(tmp_path / "bin" / entry))
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             env=env, cwd=str(tmp_path), check=True).stdout.splitlines()
        assert out[0] == str(pyz.resolve())
        assert out[1] == "True"
        assert out[2].startswith("hctrl (herdr_ctl) ")
