import os

import yaml

from herdr_ctl import cli
from herdr_ctl.config import check_file
from herdr_ctl.fake import FakeHerdr
from herdr_ctl.init_cmd import run_init
from herdr_ctl.project import Project, register
from conftest import always_found, make_project


def scripted(answers):
    it = iter(answers)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"unexpected question: {prompt}")
    return ask


def test_init_defaults_produce_valid_config(tmp_path, capsys):
    root = tmp_path / "shop"
    root.mkdir()
    p = Project("shop", root)
    answers = ["",          # session [shop]
               "",          # tab name [work1]
               "", "", "목표: 결제", "", "",  # orchestrator yes, claude/opus, goal, policy confirm, unattended off
               "", "", "", "", "", "", "",  # 2 agents: name/kind/role x2 (+ count)
               "", "", "",  # shell yes, name, build cmd
               "",          # default flow yes
               "y",         # more tabs? yes -> defaults must not collide with the first tab
               "", "n", "2", "", "", "", "", "", "",  # tab [work2], no orchestrator, 2 agents (defaults)
               "", "", "",  # shell yes, name, build cmd
               "n",         # no default flow
               ""]          # more tabs? no
    assert run_init(p, scripted(answers), which=always_found)
    data = yaml.safe_load(p.config_path.read_text())
    assert data["session"] == "shop"
    w1 = data["tabs"]["work1"]
    assert w1["orchestrator"] == {"kind": "claude", "model": "opus", "goal": "목표: 결제"}
    assert list(w1["agents"]) == ["coder", "tester"]
    assert w1["agents"]["coder"]["role"] == "구현"
    assert w1["agents"]["tester"]["role"] == "테스트 작성"
    assert list(w1["shells"]) == ["build"]
    w2 = data["tabs"]["work2"]
    assert list(w2["agents"]) == ["coder2", "tester2"] and list(w2["shells"]) == ["build2"]
    assert list(data["flows"]) == ["build-review-work1"]
    cfg, issues = check_file(p.config_path, which=always_found)
    assert cfg is not None
    assert ".herdr-flow/runs/" in (root / ".gitignore").read_text()
    assert "오류 0" in capsys.readouterr().out


def test_init_add_tab(tmp_path):
    p = make_project(tmp_path)
    answers = ["Z",                 # tab name
               "n",                 # no orchestrator
               "1", "z1", "claude/haiku", "리뷰",
               "n",                 # no shell
               "n"]                 # no default flow
    assert run_init(p, scripted(answers), add_tab="Z", which=always_found)
    data = yaml.safe_load(p.config_path.read_text())
    assert set(data["tabs"]) == {"X", "Y", "Z"} and data["tabs"]["Z"]["orchestrator"] == "none"
    assert "build-review-x" in data["flows"]


def test_init_rejects_invalid_answers(tmp_path):
    p = Project("t", tmp_path)
    answers = ["", "X", "n", "1",
               "Bad", "a1",          # invalid agent name then valid
               "nokind", "claude",   # invalid kind then valid
               "", "n", "n", "n"]
    assert run_init(p, scripted(answers), which=always_found)
    assert "a1" in yaml.safe_load(p.config_path.read_text())["tabs"]["X"]["agents"]


# ------------------------------------------------------------------ open
def test_open_refuses_inside_herdr(tmp_path, monkeypatch, capsys):
    make_project(tmp_path)
    monkeypatch.chdir(tmp_path / "projA")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/home/u/.config/herdr/herdr.sock")
    assert cli.main([]) == 1
    assert "일반 터미널" in capsys.readouterr().err
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/home/u/.config/herdr/sessions/projA/herdr.sock")
    assert cli.main([]) == 0  # already in this project's session


def test_open_unregistered_non_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 1
    assert "hctrl --add" in capsys.readouterr().err


def test_open_starts_headless_then_up_then_attach(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    register("projA", p.root)
    fake = FakeHerdr("projA")
    fake.binary = "/usr/bin/herdr"
    monkeypatch.setattr(cli, "make_herdr", lambda s: fake)
    execs = []
    monkeypatch.setattr(os, "execvpe", lambda b, argv, env: execs.append((argv, env)))
    monkeypatch.chdir(p.root / ".herdr-flow")  # subdirectory of the project
    assert cli.main([]) == 0
    assert fake.calls[0] == ("start_server", "projA")
    assert "x1" in fake.agents
    assert any(c[0] == "pane_run" and "watch" in c[2] for c in fake.calls)
    argv, env = execs[0]
    assert argv == ["/usr/bin/herdr", "--session", "projA"]
    assert "HERDR_ENV" not in env
    # second open: session already running → no restart, no re-up
    n = len(fake.calls)
    assert cli.main(["projA"]) == 0
    assert not any(c[0] == "start_server" for c in fake.calls[n:])


def test_open_aborts_on_config_error(tmp_path, monkeypatch, capsys):
    p = make_project(tmp_path, "session: projA\ntabs: {}\n")
    monkeypatch.chdir(p.root)
    assert cli.main([]) == 1
    assert "설정 오류" in capsys.readouterr().err


def test_add_and_list(tmp_path, monkeypatch, capsys):
    class H:
        def session_list(self):
            return [{"name": "projA", "status": "running"}]
    monkeypatch.setattr(cli, "Herdr", H)
    p = make_project(tmp_path)
    monkeypatch.chdir(p.root)
    assert cli.main(["--add", "projA"]) == 0
    assert cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "projA" in out and "running" in out


def test_version_flag(capsys):
    from herdr_ctl import __version__
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"hctrl (herdr_ctl) {__version__}"


def test_init_without_herdr_warns_and_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HC_HERDR_BIN", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["init"]) == 1
    err = capsys.readouterr().err
    assert "herdr가 설치되어 있지 않습니다" in err
    assert "curl -fsSL https://herdr.dev/install.sh | sh" in err
    assert not (tmp_path / ".herdr-flow").exists()  # nothing written


def test_init_honours_custom_herdr_bin(tmp_path, monkeypatch):
    fake = tmp_path / "herdr"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HC_HERDR_BIN", str(fake))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.herdr_installed()
    monkeypatch.setenv("HC_HERDR_BIN", str(tmp_path / "missing"))
    assert not cli.herdr_installed()


def test_init_unattended_mode(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    p = Project("shop", root)
    answers = ["", "", "", "", "", "", "maybe", "questions",  # invalid mode re-asked
               "", "", "", "", "", "", "", "", "", "", "", ""]
    assert run_init(p, scripted(answers), which=always_found)
    data = yaml.safe_load(p.config_path.read_text())
    assert data["tabs"]["work1"]["unattended"] == "questions"
