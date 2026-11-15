import textwrap
from pathlib import Path

import pytest

from herdr_ctl import cli
from herdr_ctl import roles as R
from herdr_ctl.orchestrator import Actor, LimitExceeded, ScopeError, ask, current_actor, enforce, start_ask
from herdr_ctl.up import up
from herdr_ctl.watch import apply_change, detect, restart, watch_once
from conftest import SAMPLE, make_ctx, writes_out


@pytest.fixture
def live(ctx):
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
    return ctx


def reload(ctx, text):
    ctx.project.config_path.write_text(textwrap.dedent(text))
    return make_ctx(ctx.project, ctx.h)


# ---------------------------------------------------------------- detect
def test_no_changes_after_up(live):
    assert detect(live) == []


def test_detect_change_types(live):
    text = (SAMPLE.replace("model: sonnet", "model: opus")
            .replace('role: "테스트"', 'role: "테스트 + 원인 분석"')
            .replace("      y1: { kind: claude, model: haiku, permission: auto }",
                     "      y2: { kind: claude }")
            .replace('run: "echo hello"', 'run: "echo bye"'))
    c2 = reload(live, text)
    kinds = {(c.name, c.type) for c in detect(c2)}
    assert ("x1", "restart") in kinds and ("x2", "role") in kinds
    assert ("y2", "add") in kinds and ("y1", "remove") in kinds and ("flows", "flow") in kinds


def test_role_file_edit_detected(live):
    m = live.cfg.member("x1")
    R.write_role(live.project, m, detail="결제 모듈만 담당")
    assert [(c.name, c.type) for c in detect(live)] == [("x1", "role")]


def test_watch_once_reports_errors_and_changes(live):
    sig, ch, err = watch_once(live.project, live.h, None)
    assert ch == [] and err is None
    live.project.config_path.write_text(textwrap.dedent(SAMPLE).replace("model: sonnet", "modle: sonnet"))
    sig2, ch, err = watch_once(live.project, live.h, sig)
    assert err and "modle" in err
    live.project.config_path.write_text(textwrap.dedent(SAMPLE).replace("model: sonnet", "model: opus"))
    sig3, ch, err = watch_once(live.project, live.h, sig2)
    assert err is None and [(c.name, c.type) for c in ch] == [("x1", "restart")]
    assert watch_once(live.project, live.h, sig3)[1] is None  # unchanged


# --------------------------------------------------------------- restart
def test_restart_with_handoff(live):
    c2 = reload(live, SAMPLE.replace("model: sonnet", "model: opus"))
    pane = c2.state.member("x1")["pane_id"]

    def write_handoff(agent, text):
        if "인수인계" in text or "요약해 쓰세요" in text:
            Path(text.rsplit(": ", 1)[1].strip()).write_text("진행: 로그인 폼 완료")
            return "idle"
        if text == "/exit":
            c2.h.kill_agent(agent.name)
            return "idle"
        return "done"
    c2.h.on_prompt["x1"] = write_handoff
    c2.h.on_prompt["x-orch"] = lambda a, t: "idle"
    msg = restart(c2, "x1")
    assert "재시작 완료" in msg
    new = c2.h.agents["x1"]
    assert new.pane_id == pane and "--model" in new.args and "opus" in new.args
    brief = (c2.project.var_dir / "briefs" / "x1.md").read_text()
    assert "handoff/x1.md" in brief
    assert any("재시작" in p for p in c2.h.agents["x-orch"].prompts)  # orchestrator told
    assert detect(c2) == []


def test_restart_refuses_when_handoff_missing(live):
    live.h.on_prompt["x1"] = lambda a, t: "idle"  # never writes the handoff file
    with pytest.raises(RuntimeError, match="인수인계 파일"):
        restart(live, "x1")
    assert "x1" in live.h.agents  # still running


def test_restart_refuses_blocked(live):
    live.h.agents["x1"].status = "blocked"
    with pytest.raises(RuntimeError, match="승인 대기"):
        restart(live, "x1")


def test_restart_waits_for_working(live):
    live.h.agents["x1"].status = "working"

    def exit_on_cmd(agent, text):
        if text == "/exit":
            live.h.kill_agent(agent.name)
        return "idle"
    live.h.on_prompt["x1"] = exit_on_cmd
    assert "재시작" in restart(live, "x1", handoff=False)


def test_apply_add_and_remove(live):
    text = SAMPLE.replace("      y1: { kind: claude, model: haiku, permission: auto }",
                          "      y1: { kind: claude, model: haiku, permission: auto }\n      y2: { kind: claude }")
    c2 = reload(live, text)
    [ch] = detect(c2)
    assert ch.type == "add"
    apply_change(c2, ch, "add")
    assert "y2" in c2.h.agents
    c3 = reload(c2, SAMPLE)
    [ch] = detect(c3)
    assert ch.type == "remove"
    apply_change(c3, ch, "remove")
    assert "y2" not in c3.h.agents and c3.state.member("y2") is None


def test_notify_role(live):
    R.write_role(live.project, live.cfg.member("x2"), one="리뷰 전담")
    [ch] = detect(live)
    apply_change(live, ch, "notify")
    assert "역할이 변경" in live.h.agents["x2"].prompts[-1]
    assert detect(live) == []


# ------------------------------------------------------------------ roles
def test_effective_role_and_history(project):
    ctx = make_ctx(project)
    m = ctx.cfg.member("x1")
    assert R.effective_role(project, m) == ("구현", "")
    R.write_role(project, m, one="결제 구현", detail="결제 모듈만", actor="user", reason="범위 축소")
    assert R.effective_role(project, m) == ("결제 구현", "결제 모듈만")
    h = R.history(project, "x1")
    assert h[0]["actor"] == "user" and h[0]["before"] == ""
    R.revert(project, m, 1)
    assert R.effective_role(project, m) == ("구현", "")


def test_proposals(project):
    ctx = make_ctx(project)
    pid = R.propose(project, "x1", "리팩터링", "x-orch", "중복 많음")
    assert [p["id"] for p in R.proposals(project)] == [pid]
    R.resolve_proposal(project, ctx.cfg, pid, accept=True)
    assert R.effective_role(project, ctx.cfg.member("x1"))[0] == "리팩터링"
    assert R.proposals(project) == []
    assert R.history(project, "x1")[-1]["actor"] == "x-orch"


# ---------------------------------------------------------- orchestrator
def test_actor_detection(live, monkeypatch):
    assert current_actor(live).kind == "user"
    monkeypatch.setenv("HERDR_PANE_ID", live.state.member("x-orch")["pane_id"])
    a = current_actor(live)
    assert (a.kind, a.name, a.tab) == ("orch", "x-orch", "X")
    monkeypatch.setenv("HERDR_PANE_ID", live.state.member("x1")["pane_id"])
    assert current_actor(live).kind == "agent"


def test_enforce_scope_and_limits(live):
    orch = Actor("orch", "x-orch", "X")
    with pytest.raises(ScopeError):
        enforce(live, orch, ["y1"], "hi")
    with pytest.raises(ScopeError):
        enforce(live, Actor("agent", "x1", "X"), ["x2"], "hi")
    enforce(live, Actor("user"), ["y1"], "hi")  # user is never limited
    start_ask(live, live.cfg.tab("X"))
    enforce(live, orch, ["x1"], "same")
    enforce(live, orch, ["x1"], "same")
    with pytest.raises(LimitExceeded, match="같은 지시"):
        enforce(live, orch, ["x1"], "same")  # max_retries_per_agent = 2
    start_ask(live, live.cfg.tab("X"))
    for i in range(3):
        enforce(live, orch, ["x2"], f"p{i}")
    with pytest.raises(LimitExceeded, match="지시 상한"):
        enforce(live, orch, ["x2"], "p4")  # max_steps_per_ask = 3
    assert live.h.notifications


def test_ask(live):
    with pytest.raises(RuntimeError, match="orchestrator가 없습니다"):
        ask(live, "Y", "do it")
    ask(live, "X", "로그인 완성")
    assert live.h.agents["x-orch"].prompts[-1] == "로그인 완성"
    live.h.agents["x-orch"].status = "working"
    with pytest.raises(RuntimeError, match="작업 중"):
        ask(live, "X", "또")


# ------------------------------------------------------------ cli wiring
@pytest.fixture
def cli_env(live, monkeypatch):
    monkeypatch.setattr(cli, "make_herdr", lambda session: live.h)
    return live


def hc(ctx, *args):
    return cli.main(["--project", str(ctx.project.root), *args])


def test_cli_role_policies(cli_env, monkeypatch, capsys):
    ctx = cli_env
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    assert hc(ctx, "role", "x1", "결제만", "--reason", "범위") == 0  # confirm → proposal
    assert "제안 #1" in capsys.readouterr().out
    assert R.effective_role(ctx.project, ctx.cfg.member("x1"))[0] == "구현"
    assert hc(ctx, "role", "y1", "다른 탭") == 1  # scope
    assert hc(ctx, "role", "x-orch", "나 자신") == 1
    monkeypatch.delenv("HERDR_PANE_ID")
    assert hc(ctx, "role", "--accept", "1") == 0
    assert R.effective_role(ctx.project, ctx.cfg.member("x1"))[0] == "결제만"
    # auto policy applies immediately and tells the agent
    ctx.project.config_path.write_text(textwrap.dedent(SAMPLE).replace("role_update: confirm", "role_update: auto"))
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    ctx.h.agents["x2"].status = "idle"
    assert hc(ctx, "role", "x2", "리뷰 전담") == 0
    assert "역할이 변경" in ctx.h.agents["x2"].prompts[-1]
    ctx.project.config_path.write_text(textwrap.dedent(SAMPLE).replace("role_update: confirm", "role_update: off"))
    assert hc(ctx, "role", "x2", "또 변경") == 1
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x1")["pane_id"])
    assert hc(ctx, "role", "x2", "agent가 변경") == 1


def test_cli_role_list_history_draft(cli_env, monkeypatch, capsys, tmp_path):
    ctx = cli_env
    assert hc(ctx, "role") == 0
    assert "x1" in capsys.readouterr().out
    assert hc(ctx, "role", "x1", "새 역할") == 0
    assert hc(ctx, "role", "x1", "--history") == 0
    assert "새 역할" in capsys.readouterr().out
    fake = tmp_path / "fakeclaude"
    fake.write_text("#!/bin/sh\necho '- 담당: 결제'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HC_DRAFT_CMD", str(fake))
    assert hc(ctx, "role", "x1", "--draft") == 0
    assert (ctx.project.roles_dir / "x1.draft.md").read_text().startswith("- 담당")


def test_cli_send_run_limits(cli_env, monkeypatch, capsys):
    ctx = cli_env
    ctx.h.default_on_prompt = writes_out()
    assert hc(ctx, "send", "x2", "테스트 추가") == 0
    assert hc(ctx, "run", "shell-only") == 0
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    assert hc(ctx, "send", "y1", "다른 탭") == 1
    assert "다른 탭" in capsys.readouterr().err
    assert hc(ctx, "run", "build-review-x") == 0


def test_cli_changes_yes(cli_env, capsys):
    ctx = cli_env
    R.write_role(ctx.project, ctx.cfg.member("x2"), one="리뷰")
    assert hc(ctx, "changes", "--list") == 0
    assert "역할 변경" in capsys.readouterr().out
    assert hc(ctx, "changes", "--yes") == 0
    assert "역할이 변경" in ctx.h.agents["x2"].prompts[-1]
    assert hc(ctx, "changes") == 0
    assert "없습니다" in capsys.readouterr().out


# ---------------------------------------------------- review regressions
def test_orchestrator_cannot_self_approve_or_manage(cli_env, monkeypatch, capsys):
    """H2/M1: orchestrator may propose roles but not approve, edit, revert or run user-only commands."""
    ctx = cli_env
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    assert hc(ctx, "role", "x1", "결제만") == 0
    for args in (["role", "--accept", "1"], ["role", "--reject", "1"], ["role", "--review"],
                 ["role", "x1", "--revert", "1"], ["role", "x1", "--edit"], ["role", "x1", "--draft"],
                 ["up"], ["down"], ["changes", "--yes"], ["ask", "X", "x"],
                 ["restart", "y1"], ["restart", "x-orch"]):
        assert hc(ctx, *args) == 1, args
    assert R.proposals(ctx.project)[0]["status"] == "pending"
    assert R.effective_role(ctx.project, ctx.cfg.member("x1"))[0] == "구현"
    ctx.h.agents["x2"].status = "idle"
    assert hc(ctx, "role", "x2", "--apply") == 0       # own tab: allowed
    assert hc(ctx, "role", "y1", "--apply") == 1       # other tab: refused


def test_work_agent_is_read_only(cli_env, monkeypatch):
    ctx = cli_env
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x1")["pane_id"])
    for args in (["send", "x2", "hi"], ["run", "shell-only"], ["up"], ["role", "x2", "--apply"]):
        assert hc(ctx, *args) == 1, args
    assert hc(ctx, "status") == 0


def test_shell_pane_user_is_user(cli_env, monkeypatch):
    """M2: a person typing hc in a shell pane is the user, not a work agent."""
    ctx = cli_env
    ctx.h.default_on_prompt = writes_out()
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("xbuild")["pane_id"])
    assert current_actor(ctx).kind == "user"
    assert hc(ctx, "send", "y1", "hi") == 0


def test_orchestrator_resume_and_stop_are_scoped(cli_env, monkeypatch):
    ctx = cli_env
    from herdr_ctl.runner import create_job, execute
    ctx.h.on_prompt["y1"] = lambda a, t: "blocked"
    rec = execute(ctx, create_job(ctx.project, "y1", "hi", None))
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    assert hc(ctx, "resume", rec["id"]) == 1
    assert hc(ctx, "stop", rec["id"]) == 1


def test_pending_changes_file_not_state(live):
    from herdr_ctl.watch import load_pending
    R.write_role(live.project, live.cfg.member("x1"), detail="x")
    before = live.project.state_path.read_text()
    watch_once(live.project, live.h, None)
    assert live.project.state_path.read_text() == before
    assert [c["name"] for c in load_pending(live.project)] == ["x1"]
