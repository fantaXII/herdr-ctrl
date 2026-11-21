"""install.sh: installs a .pyz as `hctrl`, never clobbers another install, keeps the old one on failure."""
import os
import shutil
import subprocess
import zipapp
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if not shutil.which("sh") or not shutil.which("python3") or not shutil.which("curl"):
    pytest.skip("needs sh, python3 and curl", allow_module_level=True)


@pytest.fixture
def pyz(tmp_path):
    src = tmp_path / "app"
    shutil.copytree(ROOT / "src" / "herdr_ctl", src / "herdr_ctl", ignore=shutil.ignore_patterns("__pycache__"))
    (src / "__main__.py").write_text("import sys\nfrom herdr_ctl.cli import main\nsys.exit(main())\n")
    out = tmp_path / "hctrl.pyz"
    zipapp.create_archive(src, out, interpreter="/usr/bin/env python3")
    return out


def install(url, bin_dir):
    env = dict(os.environ, HCTRL_URL=url, HCTRL_BIN_DIR=str(bin_dir))
    return subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env)


def test_installs_both_names(pyz, tmp_path):
    bin_dir = tmp_path / "bin"
    r = install(pyz.as_uri(), bin_dir)
    assert r.returncode == 0, r.stderr
    hctrl = bin_dir / "hctrl"
    assert hctrl.is_file() and not hctrl.is_symlink() and os.access(hctrl, os.X_OK)
    assert os.readlink(bin_dir / "herdr_ctl") == "hctrl"
    for name in ("hctrl", "herdr_ctl"):
        v = subprocess.run([str(bin_dir / name), "--version"], capture_output=True, text=True)
        assert v.stdout.startswith("hctrl (herdr_ctl) ")
    assert not [p for p in bin_dir.iterdir() if p.name.startswith(".hctrl.")]  # temp file cleaned
    assert install(pyz.as_uri(), bin_dir).returncode == 0  # re-run = update


def test_refuses_to_replace_pipx_link(pyz, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    venv_bin = tmp_path / "venv" / "hctrl"
    venv_bin.parent.mkdir()
    venv_bin.write_text("#!/bin/sh\n")
    (bin_dir / "hctrl").symlink_to(venv_bin)
    r = install(pyz.as_uri(), bin_dir)
    assert r.returncode == 1
    assert "pipx uninstall herdr-ctl" in r.stderr
    assert os.readlink(bin_dir / "hctrl") == str(venv_bin)


@pytest.mark.parametrize("bad", ["missing", "junk"])
def test_failure_keeps_existing_install(pyz, tmp_path, bad):
    bin_dir = tmp_path / "bin"
    assert install(pyz.as_uri(), bin_dir).returncode == 0
    before = (bin_dir / "hctrl").read_bytes()
    target = tmp_path / "nope.pyz"
    if bad == "junk":
        target.write_text("not a zipapp\n")
    r = install(target.as_uri(), bin_dir)
    assert r.returncode == 1
    assert (bin_dir / "hctrl").read_bytes() == before
    assert not [p for p in bin_dir.iterdir() if p.name.startswith(".hctrl.")]
