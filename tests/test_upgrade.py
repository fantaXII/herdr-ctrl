"""Installed-vs-running version: status badge states, the _hc watcher restarting as the new version."""
import os
import zipfile

import pytest

from herdr_ctl import __version__, cli, selfcmd, update
from herdr_ctl import watch as W
from herdr_ctl.fake import FakeHerdr
from herdr_ctl.ui import UIServer
from conftest import always_found
from test_ui import Client, FakeSessions

CUR = tuple(int(x) for x in __version__.split("."))
NEXT = "%d.%d.%d" % (CUR[0], CUR[1], CUR[2] + 1)
AFTER = "%d.%d.%d" % (CUR[0], CUR[1], CUR[2] + 2)


@pytest.fixture(autouse=True)
def checks_on(monkeypatch):
    monkeypatch.delenv("HCTRL_NO_UPDATE_CHECK", raising=False)
    update._disk.clear()


def installed(monkeypatch, tmp_path, version, name="hctrl"):
    """Pretend this process runs from a .pyz whose file on disk now holds `version`."""
    pyz = tmp_path / name
    with zipfile.ZipFile(pyz, "w") as z:
        z.writestr("herdr_ctl/__init__.py", f'"""x"""\n__version__ = "{version}"\n')
    monkeypatch.setattr(selfcmd, "PKG_PARENT", pyz)
    return pyz


def latest(tag):
    update.write_cache({"checked_at": 1e12, "latest": tag, "failed": False})


# ------------------------------------------------------------ disk_version / status
def test_disk_version_source_checkout_is_this_version():
    assert update.disk_version() == __version__
    assert update.status()["state"] is None


def test_disk_version_reads_pyz_and_follows_replacement(monkeypatch, tmp_path):
    pyz = installed(monkeypatch, tmp_path, NEXT)
    assert update.disk_version() == NEXT
    os.replace(installed(monkeypatch, tmp_path, AFTER, "new"), pyz)
    monkeypatch.setattr(selfcmd, "PKG_PARENT", pyz)
    assert update.disk_version() == AFTER


def test_disk_version_unreadable_is_none(monkeypatch, tmp_path):
    bad = tmp_path / "hctrl"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr(selfcmd, "PKG_PARENT", bad)
    assert update.disk_version() is None and update.status()["state"] is None


def test_status_states(monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path, __version__)
    latest("v" + NEXT)
    s = update.status()
    assert s["state"] == "update" and s["installed"] == __version__ and s["pyz"]
    installed(monkeypatch, tmp_path, NEXT)  # updated on disk, this process still old
    s = update.status()
    assert s["state"] == "installed" and s["installed"] == NEXT and s["version"] == __version__
    assert update.available() is None  # nothing left to download: no "hctrl update" notice
    latest("v" + AFTER)
    assert update.status()["state"] == "update" and f"v{NEXT}" in update.notice()


def test_package_install_points_to_git_pull(monkeypatch):
    monkeypatch.setattr(update, "install_target", lambda: None)
    monkeypatch.setattr(update, "latest_release", lambda fetch: "v" + NEXT)
    with pytest.raises(update.UpdateError, match="git pull"):
        update.run_update(out=lambda s: None)


# ------------------------------------------------------------ _hc watcher restart
class FakeUI:
    def __init__(self, busy=False, asked=False):
        self.busy, self.restart_requested = busy, asked

    def in_use(self):
        return self.busy


def menu_with(project, ui):
    m = W.Menu(project)
    m.ui = ui
    return m


def test_no_restart_when_versions_match(project):
    assert W._upgrade(menu_with(project, None), None) is None


def test_restart_when_installed_version_changed(project, monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path, NEXT)
    with pytest.raises(W.Reexec) as e:
        W._upgrade(menu_with(project, FakeUI(busy=False)), None)
    assert e.value.new == NEXT and not e.value.reopen_ui


def test_restart_waits_for_open_settings_page(project, monkeypatch, tmp_path, capsys):
    installed(monkeypatch, tmp_path, NEXT)
    m = menu_with(project, FakeUI(busy=True))
    told = W._upgrade(m, None)
    assert told == NEXT and "닫은 뒤" in capsys.readouterr().out
    assert W._upgrade(m, told) == NEXT and capsys.readouterr().out == ""  # said once
    m.ui.busy = False
    with pytest.raises(W.Reexec):
        W._upgrade(m, told)


def test_apply_now_from_settings_page_restarts_and_reopens(project, monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path, NEXT)
    with pytest.raises(W.Reexec) as e:
        W._upgrade(menu_with(project, FakeUI(busy=True, asked=True)), None)
    assert e.value.reopen_ui


def test_watch_loop_raises_reexec(project, monkeypatch, tmp_path):
    monkeypatch.setattr(W, "watch_once", lambda *a, **k: ("sig", None, None))
    monkeypatch.setattr(update, "refresh_in_background", lambda: None)
    installed(monkeypatch, tmp_path, NEXT)
    with pytest.raises(W.Reexec):
        W.watch_loop(project, FakeHerdr(), interval=0.01, iterations=3, keys=lambda t: None, menu=W.Menu(project))


def test_cmd_watch_execs_installed_version(project, monkeypatch):
    class Keys:
        cooked = None

        def __call__(self, timeout):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            events.append("tty restored")
    events = []

    def loop(*a, **k):
        raise W.Reexec(NEXT, reopen_ui=True)
    monkeypatch.setattr(cli, "tty", lambda: True)
    monkeypatch.setattr(W, "TtyKeys", Keys)
    monkeypatch.setattr(W, "watch_loop", loop)
    monkeypatch.setattr(update, "refresh_in_background", lambda: None)
    monkeypatch.delenv(cli.REOPEN_UI_ENV, raising=False)
    monkeypatch.setattr(os, "execv", lambda path, argv: events.append(argv))
    cli.main(["--project", str(project.root), "watch"])
    assert events[0] == "tty restored"
    assert events[1][-5:] == ["--project", str(project.root), "watch", "--interval", "2.0"]
    assert os.environ.pop(cli.REOPEN_UI_ENV) == "1"


# ------------------------------------------------------------ settings UI API
@pytest.fixture
def session_ui(tmp_path):
    s = UIServer("session", cwd=tmp_path, herdr=FakeSessions(), which=always_found, models=lambda: {}).start()
    yield s
    s.stop()


def test_state_reports_version_status(session_ui):
    c = Client(session_ui)
    c.login()
    st, res = c.json("GET", "/api/state")
    assert st == 200 and res["update"]["version"] == __version__ and res["update"]["state"] is None


def test_page_in_use_until_idle(session_ui, monkeypatch):
    c = Client(session_ui)
    c.login()
    assert c.json("GET", "/api/ping") == (200, {"ok": True}) and session_ui.in_use()
    session_ui.last_seen -= 1000
    assert not session_ui.in_use()


def test_restart_request_only_in_session_mode(session_ui, tmp_path):
    c = Client(session_ui)
    c.login()
    assert c.json("POST", "/api/restart", {})[0] == 200 and session_ui.restart_requested
    launcher = UIServer("launcher", cwd=tmp_path, herdr=FakeSessions(), which=always_found, models=lambda: {}).start()
    try:
        c2 = Client(launcher)
        c2.login()
        st, res = c2.json("POST", "/api/restart", {})
        assert st == 400 and "다시 실행" in res["error"] and not launcher.restart_requested
    finally:
        launcher.stop()


def test_update_endpoints(session_ui, monkeypatch):
    c = Client(session_ui)
    c.login()
    monkeypatch.setattr(update, "latest_release", lambda fetch: "v" + NEXT)  # no network
    monkeypatch.setattr(update, "check_now", lambda fetch=update._fetch: latest("v" + NEXT) or "v" + NEXT)
    st, res = c.json("POST", "/api/update/check", {})
    assert st == 200 and res["status"]["state"] == "update" and res["status"]["latest"] == "v" + NEXT
    # source checkout: no pyz to replace -> the user is told to git pull
    st, res = c.json("POST", "/api/update", {})
    assert st == 400 and "git pull" in res["error"]
    monkeypatch.setattr(update, "check_now", lambda fetch=update._fetch: None)
    st, res = c.json("POST", "/api/update/check", {})
    assert st == 502
