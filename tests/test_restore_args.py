"""Agents herdr resumed by itself after a server restart run without hctrl's launch args
(`claude --resume <id>` only): `up` and opening the project start them again in the same
conversation with their permission mode, role and hooks."""
import os
from pathlib import Path

import pytest

from herdr_ctl import up as up_mod
from herdr_ctl import watch as watch_mod
from herdr_ctl.up import launched_without_hctrl, live_own_agents, reapply_launch_args, up


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(up_mod, "RESTORE_IDLE_S", 0.0)
    monkeypatch.setattr(up_mod, "RESTORE_SETTLE_S", 0.6)
    monkeypatch.setattr(up_mod, "RESTORE_READY_S", 1.0)
    monkeypatch.setattr(watch_mod, "START_CHECK_S", 0.0)


def transcript(name):
    d = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"fake-{name}-session.jsonl").write_text("{}\n")


def starts(h, name):
    return [c for c in h.calls if c[0] == "agent_start" and c[1] == name]


@pytest.fixture
def live(ctx):
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"

    def on_prompt(agent, text):
        if text == "/exit":
            ctx.h.kill_agent(agent.name)
            return "idle"
        return "done"
    ctx.h.default_on_prompt = on_prompt
    return ctx


def test_hctrl_started_agent_is_left_alone(live):
    assert launched_without_hctrl(live, live.cfg.member("y1"), live.state.member("y1")["pane_id"]) is False
    n = len(live.h.calls)
    rep = up(live, start_watch=False)
    assert "y1" in rep.skipped and not [c for c in live.h.calls[n:] if c[0] == "agent_start"]


def test_up_reapplies_args_to_an_agent_herdr_restored(live):
    transcript("y1")
    live.h.server_restore("y1")
    m = live.cfg.member("y1")
    assert launched_without_hctrl(live, m, live.state.member("y1")["pane_id"]) is True
    rep = up(live, start_watch=False)
    assert any(s.startswith("y1 (hctrl 설정 다시 적용") for s in rep.started)
    assert "y1" not in rep.skipped
    args = list(starts(live.h, "y1")[-1][4])
    assert args[:2] == ["--resume", "fake-y1-session"]  # same conversation
    assert args[args.index("--permission-mode") + 1] == "auto"
    assert "--append-system-prompt-file" in args
    # done once: the next up leaves it alone
    n = len(starts(live.h, "y1"))
    rep2 = up(live, start_watch=False)
    assert len(starts(live.h, "y1")) == n and "y1" in rep2.skipped


def test_restored_orchestrator_gets_its_readonly_allowlist_back(live):
    transcript("x-orch")
    live.h.server_restore("x-orch")
    up(live, start_watch=False)
    args = list(starts(live.h, "x-orch")[-1][4])
    assert args[:2] == ["--resume", "fake-x-orch-session"] and "--allowedTools" in args


def test_busy_restored_agent_is_reported_not_stopped(live):
    transcript("y1")
    live.h.server_restore("y1").status = "working"
    n = len(starts(live.h, "y1"))
    rep = up(live, start_watch=False)
    assert len(starts(live.h, "y1")) == n
    assert "hctrl restart y1" in rep.attention["y1"]


def test_opencode_and_unreadable_argv_are_left_alone(live, monkeypatch):
    x2 = live.cfg.member("x2")
    assert launched_without_hctrl(live, x2, live.state.member("x2")["pane_id"]) is None
    monkeypatch.setattr(live.h, "pane_agent_argv", lambda pane, kind: None)
    live.h.server_restore("y1")
    assert reapply_launch_args(live, ["y1", "x2"]) == []


def test_waits_for_a_listed_agent_whose_cli_is_not_up_yet(live, monkeypatch):
    """herdr lists a restored agent by name a moment before its CLI runs in the pane."""
    transcript("y1")
    live.h.server_restore("y1")
    seen = {"n": 0}

    def argv(pane, kind):
        seen["n"] += 1
        return None if seen["n"] < 3 else ["--resume", "fake-y1-session"]
    monkeypatch.setattr(live.h, "pane_agent_argv", argv)
    real_idle = live.h.pane_shell_idle
    monkeypatch.setattr(live.h, "pane_shell_idle", lambda p: True if seen["n"] < 3 else real_idle(p))
    monkeypatch.setattr(up_mod.time, "sleep", lambda s: None)
    lines = reapply_launch_args(live, ["y1"])
    assert lines == ["y1: hctrl 설정 다시 적용 (대화 유지)"]


def test_live_own_agents_lists_members_in_their_pane(live):
    assert set(live_own_agents(live)) == {"x-orch", "x1", "x2", "y1"}


def test_cli_under_another_name_does_not_stall_up(live, monkeypatch):
    """argv unreadable while something else runs in the pane (a wrapper, a node install): no wait."""
    import time
    monkeypatch.setattr(up_mod, "RESTORE_SETTLE_S", 30.0)
    monkeypatch.setattr(live.h, "pane_agent_argv", lambda pane, kind: None)
    t0 = time.time()
    assert reapply_launch_args(live, ["y1", "x1"]) == []
    assert time.time() - t0 < 1.0


def test_restored_agent_that_quits_is_started_anew(live, monkeypatch):
    """herdr resumed a conversation with no transcript yet: claude prints "No conversation found"
    and quits, leaving the pane at its shell. hctrl starts the member there as usual."""
    live.h.server_restore("y1")
    real = up_mod.launched_without_hctrl

    def flagged_then_quits(ctx, m, pane):
        v = real(ctx, m, pane)
        if v and m.name == "y1":
            live.h.kill_agent("y1")
        return v
    monkeypatch.setattr(up_mod, "launched_without_hctrl", flagged_then_quits)
    lines = reapply_launch_args(live, ["y1"])
    assert lines == ["y1: hctrl 설정 다시 적용 (이어 갈 대화가 없어 새로 시작)"]
    args = list(starts(live.h, "y1")[-1][4])
    assert "--resume" not in args and args[args.index("--permission-mode") + 1] == "auto"
