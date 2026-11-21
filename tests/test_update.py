"""New-version notice and `hctrl update` (no network: fetch is injected)."""
import hashlib
import json
import sys
import time

import pytest

from herdr_ctl import __version__, cli, update
from herdr_ctl.watch import _update_notice

CUR = tuple(int(x) for x in __version__.split("."))
NEWER = "v%d.%d.%d" % (CUR[0], CUR[1], CUR[2] + 1)


@pytest.fixture(autouse=True)
def checks_on(monkeypatch):
    monkeypatch.delenv("HCTRL_NO_UPDATE_CHECK", raising=False)


def fake_fetch(tag=NEWER, pyz=b"PYZ", sums=None, calls=None):
    def fetch(url, timeout):
        if calls is not None:
            calls.append(url)
        if url == update.API_LATEST:
            return json.dumps({"tag_name": tag}).encode()
        if url.endswith("hctrl.pyz"):
            return pyz
        if url.endswith(".sha256"):
            if sums is False:
                raise OSError("404")
            return (sums or hashlib.sha256(pyz).hexdigest() + "  hctrl.pyz\n").encode()
        raise AssertionError(url)
    return fetch


def test_version_compare():
    assert update.is_newer(NEWER) and not update.is_newer("v" + __version__)
    assert not update.is_newer("v0.0.1") and not update.is_newer("garbage") and not update.is_newer(None)
    assert update.is_newer("v10.0.0", "9.9.9")


def test_check_caches_and_notice():
    assert update.notice() is None and update.due()
    assert update.check_now(fake_fetch()) == NEWER
    assert update.available() == NEWER and NEWER in update.notice() and "hctrl update" in update.notice()
    assert not update.due()


def test_failed_check_is_silent_and_retried_later():
    def boom(url, timeout):
        raise OSError("offline")
    assert update.check_now(boom) is None
    assert not update.due() and update.due(time.time() + update.RETRY_AFTER_FAIL_S + 1)


def test_disabled_by_env(monkeypatch):
    update.check_now(fake_fetch())
    monkeypatch.setenv("HCTRL_NO_UPDATE_CHECK", "1")
    assert update.available() is None
    spawned = []
    update.write_cache({})
    update.refresh_in_background(spawn=lambda *a, **k: spawned.append(a))
    assert not spawned


def test_background_refresh_only_when_due():
    spawned = []
    update.refresh_in_background(spawn=lambda cmd, **k: spawned.append(cmd))
    assert len(spawned) == 1 and spawned[0][-1] == "_update-check"
    update.refresh_in_background(spawn=lambda cmd, **k: spawned.append(cmd))  # claimed: no second spawn
    assert len(spawned) == 1


def _pyz(tmp_path, version):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "__main__.py").write_text(f"print('hctrl (herdr_ctl) {version}')\n")
    import zipapp
    out = tmp_path / f"new-{version}.pyz"
    zipapp.create_archive(src, out)
    return out.read_bytes()


def test_update_replaces_pyz_after_checksum_and_version(tmp_path):
    target = tmp_path / "hctrl"
    target.write_bytes(b"old")
    data = _pyz(tmp_path, NEWER.lstrip("v"))
    lines = []
    assert update.run_update(fetch=fake_fetch(pyz=data), target=target, out=lines.append) == 0
    assert target.read_bytes() == data and "완료" in lines[-2]
    assert not list(tmp_path.glob(".hctrl.update"))


@pytest.mark.parametrize("sums,pyz_version,msg", [
    (False, None, "체크섬 파일"), ("0" * 64, None, "체크섬이 맞지"), (None, "9.9.9", "버전이 다릅니다")])
def test_update_refuses_bad_downloads(tmp_path, sums, pyz_version, msg):
    target = tmp_path / "hctrl"
    target.write_bytes(b"old")
    data = _pyz(tmp_path, pyz_version or NEWER.lstrip("v"))
    with pytest.raises(update.UpdateError, match=msg):
        update.run_update(fetch=fake_fetch(pyz=data, sums=sums), target=target, out=lambda s: None)
    assert target.read_bytes() == b"old"
    assert not (tmp_path / ".hctrl.update").exists()


def test_update_up_to_date_and_check_only(tmp_path):
    calls, lines = [], []
    assert update.run_update(fetch=fake_fetch(tag="v" + __version__, calls=calls), out=lines.append) == 0
    assert "최신" in lines[0] and calls == [update.API_LATEST]
    calls.clear()
    assert update.run_update(check_only=True, fetch=fake_fetch(calls=calls), out=lines.append) == 0
    assert calls == [update.API_LATEST]


def test_update_refuses_package_install(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "install_target", lambda: None)
    with pytest.raises(update.UpdateError, match="pyz 설치가 아닙니다"):
        update.run_update(fetch=fake_fetch(), out=lambda s: None)


def test_update_command_refused_in_agent_pane(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_managed_pane", lambda: {"type": "agent"})
    assert cli.main(["update"]) == 1
    assert "agent 창" in capsys.readouterr().err


def test_version_flag_shows_cached_notice(monkeypatch, capsys):
    update.check_now(fake_fetch())
    monkeypatch.setattr(cli, "tty", lambda: True)
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out and NEWER in out


def test_hc_watch_notices_once_per_version(monkeypatch):
    monkeypatch.setattr(update, "refresh_in_background", lambda: None)
    notes = []

    class H:
        def notify(self, title, body):
            notes.append(body)
    assert _update_notice(H(), None) is None and not notes
    update.check_now(fake_fetch())
    seen = _update_notice(H(), None)
    assert seen == NEWER and len(notes) == 1
    assert _update_notice(H(), seen) == NEWER and len(notes) == 1
