"""_hc tab key menu: hctrl watch keeps polling while handling s/c/t/p."""
import pytest

from herdr_ctl import ui as ui_mod
from herdr_ctl import watch as W
from herdr_ctl.fake import FakeHerdr


def scripted_keys(*keys):
    seq = list(keys)

    def get(timeout):
        return seq.pop(0) if seq else None
    return get


def test_keys_dispatch_while_polling(project, monkeypatch, capsys):
    polls = []
    monkeypatch.setattr(W, "watch_once", lambda *a, **k: (polls.append(1) or "sig", None, None))
    ran = []
    menu = W.Menu(project, run=lambda cmd: ran.append(cmd[-1]) or 0)
    settings = []
    monkeypatch.setattr(menu, "settings", lambda: settings.append(1))
    W.watch_loop(project, FakeHerdr(), interval=0.3, iterations=3, keys=scripted_keys("c", "t", "x", "P", "s"),
                 menu=menu)
    assert ran == ["changes", "status", "pending"] and settings == [1]
    assert len(polls) == 3  # change detection kept running
    assert W.MENU in capsys.readouterr().out


def test_menu_commands_target_project_and_use_cooked_tty(project):
    events = []

    class Cooked:
        def __enter__(self):
            events.append("cooked")

        def __exit__(self, *a):
            events.append("cbreak")
    menu = W.Menu(project, cooked=Cooked, run=lambda cmd: events.append(cmd) or 0)
    menu.handle("c")
    assert events[0] == "cooked" and events[2] == "cbreak"
    assert events[1][-3:] == ["--project", str(project.root), "changes"]


def test_settings_reuses_one_server_with_fresh_links(project, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_mod, "open_browser", lambda url: opened.append(url) or True)
    menu = W.Menu(project)
    try:
        menu.handle("s")
        server = menu.ui
        menu.handle("s")
        assert menu.ui is server and server.mode == "session"
        assert len(opened) == 2 and opened[0] != opened[1]
        assert all(f":{server.port}/?t=" in u and u.endswith("&p=projA") for u in opened)
    finally:
        menu.close()
    assert menu.ui is None


def test_non_tty_watch_has_no_menu(project, monkeypatch, capsys):
    monkeypatch.setattr(W, "watch_once", lambda *a, **k: ("sig", None, None))
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    monkeypatch.setattr(W.select, "select", lambda *a: pytest.fail("stdin read without a tty"))
    W.watch_loop(project, FakeHerdr(), interval=0, iterations=2)
    assert W.MENU not in capsys.readouterr().out


def test_cmd_watch_without_tty_uses_plain_loop(project, monkeypatch):
    from herdr_ctl import cli
    seen = {}
    monkeypatch.setattr(cli, "tty", lambda: False)
    monkeypatch.setattr(W, "watch_loop", lambda p, h, interval, **kw: seen.update(kw=kw))
    assert cli.main(["--project", str(project.root), "watch"]) == 0
    assert seen["kw"] == {}


def test_tty_keys_on_a_real_pty():
    import os
    import pty
    import termios
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with W.TtyKeys(slave) as keys:
            assert not termios.tcgetattr(slave)[3] & termios.ICANON  # cbreak: no Enter needed
            assert keys(0.01) is None
            os.write(master, b"s")
            assert keys(1) == "s"
            with keys.cooked():
                assert termios.tcgetattr(slave)[3] & termios.ICANON
            assert not termios.tcgetattr(slave)[3] & termios.ICANON
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def _ixon(fd):
    import termios
    return bool(termios.tcgetattr(fd)[0] & termios.IXON)


def test_ttykeys_disables_ixon_and_restores():
    import os
    import pty
    import termios
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[0] |= termios.IXON | termios.IXOFF
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        before = termios.tcgetattr(slave)
        with W.TtyKeys(slave) as keys:
            now = termios.tcgetattr(slave)
            assert not now[0] & (termios.IXON | termios.IXOFF)
            with keys.cooked():
                assert termios.tcgetattr(slave) == before
                assert _ixon(slave)
            assert not termios.tcgetattr(slave)[0] & (termios.IXON | termios.IXOFF)
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_open_browser_detaches_stdin(monkeypatch):
    import subprocess
    seen = {}
    monkeypatch.setattr(ui_mod, "browser_command", lambda url: ["somebrowser", url])
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw))
    assert ui_mod.open_browser("http://x") is True
    assert seen["stdin"] is subprocess.DEVNULL


class _BusyUI:
    port, secret = 4321, "s3cret"

    def in_use(self):
        return True

    def stop(self):
        pass


def test_settings_restarts_into_installed_newer_version_even_when_ui_in_use(project, monkeypatch):
    # an old settings server (in use) must not be reused once a newer hctrl is installed on disk
    from herdr_ctl import update
    monkeypatch.setattr(update, "disk_version", lambda: "99.0.0")
    monkeypatch.setattr(ui_mod, "open_browser", lambda url: pytest.fail("old UI link was opened"))
    menu = W.Menu(project)
    menu.ui = _BusyUI()
    with pytest.raises(W.Reexec) as e:
        menu.handle("s")
    assert e.value.new == "99.0.0" and e.value.reopen_ui is True


def test_settings_restarts_even_without_a_running_ui(project, monkeypatch):
    from herdr_ctl import update
    monkeypatch.setattr(update, "disk_version", lambda: "99.0.0")
    menu = W.Menu(project)
    with pytest.raises(W.Reexec) as e:
        menu.handle("s")
    assert e.value.reopen_ui is True and menu.ui is None


def test_settings_reuses_ui_when_disk_version_matches(project, monkeypatch):
    from herdr_ctl import __version__, update
    monkeypatch.setattr(update, "disk_version", lambda: __version__)
    opened = []
    monkeypatch.setattr(ui_mod, "open_browser", lambda url: opened.append(url) or True)
    menu = W.Menu(project)
    try:
        menu.handle("s")
        server = menu.ui
        menu.handle("s")
        assert menu.ui is server and len(opened) == 2
    finally:
        menu.close()


# ------------------------------------------------------- target tab in the header
def test_target_tab_scopes_menu_commands(project):
    ran = []
    answers = iter(["", "f", "2"])
    menu = W.Menu(project, run=lambda cmd: ran.append(cmd[cmd.index("--project") + 2:]) or 0,
                  ask=lambda prompt: next(answers))
    assert menu.target is None and "[전체]" in menu.header_lines()[0]
    menu.handle("c")
    assert ran[-1] == ["changes"]  # every tab, as before
    menu.handle("\t")
    assert menu.target == "X" and "[1/2] X" in menu.header_lines()[0]
    menu.handle("c")
    menu.handle("t")
    assert ran[-2:] == [["changes", "--tab", "X"], ["status", "X"]]
    menu.handle("2")
    assert menu.target == "Y"
    menu.handle("0")
    assert menu.target is None
    menu.handle("1")
    menu.handle("r")  # not running: nothing is run
    assert ran[-1] == ["status", "X"]


def test_finish_and_reopen_keys_build_tab_commands(project, monkeypatch):
    ran = []
    answers = iter(["2", "", "f", "1"])
    menu = W.Menu(project, run=lambda cmd: ran.append(cmd[cmd.index("--project") + 2:]) or 0,
                  ask=lambda prompt: next(answers))
    monkeypatch.setattr(menu, "tabs", lambda: [{"name": "X", "closed": False, "running": True, "worktree": None,
                                                "changes": 0}])
    menu.target = "X"
    menu.handle("x")
    assert ran[-1] == ["tab", "end", "X"]
    menu.handle("r")
    assert ran[-1] == ["restart", "--tab", "X", "--fresh"]
    menu.handle("x")
    assert ran[-1] == ["down", "X"]
    menu.handle("d")  # lowercase d does nothing: deleting settings is uppercase D only
    assert ran[-1] == ["down", "X"]
    menu.handle("D")
    assert ran[-1] == ["tab", "delete", "X"]


def test_help_explains_the_keys(project, capsys):
    W.Menu(project).handle("?")
    out = capsys.readouterr().out
    assert "잠시 닫기" in out and "탭 종료" in out and "탭 설정 삭제" in out and "병합" in out


def test_arrow_keys_move_the_target_and_run_nothing(project):
    ran = []
    menu = W.Menu(project, run=lambda cmd: ran.append(cmd) or 0)
    for k in "\x1b[C":
        menu.handle(k)
    assert menu.target == "X"
    for k in "\x1b[D\x1b[D":
        menu.handle(k)
    assert menu.target == "Y"
    for k in "\x1b[1;5C":  # ctrl+→: parameters are swallowed too
        menu.handle(k)
    assert ran == [] and menu.target is None
