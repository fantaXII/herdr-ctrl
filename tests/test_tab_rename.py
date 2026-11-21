"""Tab id rename in place: config, state, files and live herdr names move; panes and agents stay."""
import textwrap

import pytest
import yaml

from herdr_ctl import cli
from herdr_ctl.init_cmd import write_config
from herdr_ctl.state import acquire, release, read_json
from herdr_ctl.tabrename import RenameError, rename_in_data, rename_tabs
from herdr_ctl.ui import UIServer
from herdr_ctl.up import up
from herdr_ctl.watch import detect
from conftest import SAMPLE, always_found, make_ctx

WITH_ORCH_STEP = SAMPLE + """
  ask-orch:
    tab: X
    steps:
      - agent: x-orch
        prompt: "정리"
"""


def data_of(text=SAMPLE):
    return yaml.safe_load(textwrap.dedent(text))


@pytest.fixture
def live(project):
    ctx = make_ctx(project)
    ctx.h.sessions["projA"] = "running"
    project.ensure_dirs()
    (project.roles_dir / "x-orch.md").write_text("# 역할: 로그인 총괄\n\n상세\n")
    up(ctx, start_watch=False)
    return ctx


def do_rename(ctx, old, new, live=True):
    data = rename_in_data(data_of(ctx.project.config_path.read_text()), old, new)
    return rename_tabs(ctx.project, {old: new}, lambda: write_config(ctx.project, data), ctx.h, live)


def test_rename_in_data_moves_flows_and_orchestrator_steps():
    d = rename_in_data(data_of(WITH_ORCH_STEP), "X", "Login")
    assert list(d["tabs"]) == ["Login", "Y"]  # order kept
    assert d["flows"]["ask-orch"]["tab"] == "Login"
    assert d["flows"]["ask-orch"]["steps"][0]["agent"] == "login-orch"
    assert d["flows"]["build-review-x"]["steps"][0]["agent"] == "x1"


def test_live_rename_keeps_panes_and_agents(live):
    h = live.h
    panes, agents = set(h.panes), {n: id(a) for n, a in h.agents.items()}
    orch_pane = live.state.member("x-orch")["pane_id"]
    report = do_rename(live, "X", "Login")
    assert "탭 ID X → Login" in report and "orchestrator x-orch → login-orch" in report
    assert set(h.panes) == panes  # nothing closed or created
    assert "x-orch" not in h.agents and h.agents["login-orch"].pane_id == orch_pane
    assert {n: id(a) for n, a in h.agents.items() if n != "login-orch"} == \
        {n: v for n, v in agents.items() if n != "x-orch"}  # work agents untouched
    assert {t["label"] for t in h.tab_list()} == {"_hc", "Login", "Login-build", "Y"}
    assert h.panes[orch_pane].label == "hctrl:login-orch"
    # state follows the new id
    st = read_json(live.project.state_path)
    assert {"Login", "Login-build", "Y"} <= set(st["tabs"]) and "X" not in st["tabs"]
    assert st["members"]["x1"]["tab"] == "Login" and st["members"]["xbuild"]["tab_label"] == "Login-build"
    assert "login-orch" in st["members"] and "x-orch" not in st["applied"]
    # orchestrator files follow its new name; it was told
    assert (live.project.roles_dir / "login-orch.md").read_text().startswith("# 역할: 로그인 총괄")
    assert (live.project.flow_dir / "orchestrator-Login.md").exists()
    assert not (live.project.flow_dir / "orchestrator-X.md").exists()
    assert any("login-orch" in p for p in h.agents["login-orch"].prompts)
    # nothing left for `hctrl changes`
    assert detect(make_ctx(live.project, h)) == []


def test_up_after_rename_is_a_noop(live):
    do_rename(live, "X", "Login")
    n = len(live.h.panes)
    rep = up(make_ctx(live.project, live.h), start_watch=False)
    assert not rep.created_tabs and not rep.created_panes and not rep.started and len(live.h.panes) == n


def test_rename_while_stopped_still_moves_files_and_state(project):
    ctx = make_ctx(project)
    ctx.h.sessions["projA"] = "running"
    up(ctx, start_watch=False)
    (project.roles_dir / "x-orch.md").write_text("# 역할: 목표\n")
    do_rename(ctx, "X", "Login", live=False)
    assert (project.roles_dir / "login-orch.md").exists()
    assert "Login" in read_json(project.state_path)["tabs"]
    assert "x-orch" in ctx.h.agents  # herdr not touched when the session is not live


def test_busy_member_refuses_and_changes_nothing(live):
    acquire(live.project, "x1", "job-1")
    before = live.project.config_path.read_text()
    with pytest.raises(RenameError, match="x1"):
        do_rename(live, "X", "Login")
    release(live.project, "x1")
    assert live.project.config_path.read_text() == before
    assert "X" in read_json(live.project.state_path)["tabs"] and "x-orch" in live.h.agents


def test_swap_is_refused(live):
    with pytest.raises(RenameError, match="맞바꾸"):
        rename_tabs(live.project, {"X": "Y", "Y": "X"}, lambda: None, live.h, True)


def test_case_only_rename(live):
    do_rename(live, "Y", "y")
    st = read_json(live.project.state_path)
    assert "y" in st["tabs"] and "Y" not in st["tabs"] and st["members"]["y1"]["tab"] == "y"
    assert detect(make_ctx(live.project, live.h)) == []


def test_cli_tab_rename(live, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_herdr", lambda session: live.h)
    assert cli.main(["--project", str(live.project.root), "tab", "rename", "x", "Login"]) == 0
    assert "탭 ID X → Login" in capsys.readouterr().out
    assert "Login" in data_of(live.project.config_path.read_text())["tabs"]
    assert "login-orch" in live.h.agents
    assert cli.main(["--project", str(live.project.root), "tab", "rename", "Login", "y"]) == 1  # taken


def test_ui_save_with_renames(live):
    srv = UIServer("session", start=live.project, herdr=lambda session=None: live.h, which=always_found)
    from herdr_ctl.state import digest
    data = rename_in_data(data_of(live.project.config_path.read_text()), "X", "Login")
    res = srv.save_config(live.project, {"data": data, "renames": {"X": "Login"},
                                         "version": digest(live.project.config_path.read_text())})
    assert "탭 ID X → Login" in res["renamed"]
    assert "login-orch" in live.h.agents
    assert detect(make_ctx(live.project, live.h)) == []
