"""Ending a tab (close / end / reopen / delete), merge-checked worktree removal and
restarting with the conversation kept."""
import subprocess

import pytest
import yaml

from herdr_ctl import cli
from herdr_ctl import tabops as T
from herdr_ctl.up import status, up
from herdr_ctl.watch import detect, restart
from conftest import make_ctx


@pytest.fixture
def live(ctx):
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
    for name in list(ctx.h.agents):  # agents leave on /exit
        ctx.h.on_prompt[name] = lambda a, t: (ctx.h.kill_agent(a.name) if t == "/exit" else None) or "idle"
    return ctx


def saved(ctx):
    return yaml.safe_load(ctx.project.config_path.read_text())


def test_end_removes_session_files_keeps_settings_and_prompts(live):
    """탭 종료: the tab's hctrl session is over. Session files go; settings and prompts stay; the tab is
    no longer laid out or watched."""
    import herdr_ctl.roles as R
    p = live.project
    (p.flow_dir / "work" / "X").mkdir(parents=True)
    p.handoff_dir.mkdir(parents=True, exist_ok=True)
    (p.handoff_dir / "x1.md").write_text("h")
    R.write_role(p, live.cfg.member("x1"), "구현", "## 범위\n- 로그인")
    plan = {r["item"] for r in T.clean_plan(live, live.cfg.tab("X"))}
    assert {"work", "handoff", "briefs"} <= plan and "roles" not in plan
    lines = T.finish(live, "X", "end")
    assert any("종료" in ln for ln in lines)
    assert not (p.flow_dir / "work" / "X").exists() and not (p.handoff_dir / "x1.md").exists()
    assert (p.roles_dir / "x1.md").exists()  # the prompt stays
    from herdr_ctl.up import load_sessions
    assert "x1" not in load_sessions(p)
    assert saved(live)["tabs"]["X"]["closed"] is True and "agents" in saved(live)["tabs"]["X"]
    c2 = make_ctx(p, live.h)
    assert [t.name for t in c2.cfg.open_tabs] == ["Y"] and "x1" not in live.h.agents
    assert detect(c2) == []  # no "new agent" noise for the ended tab
    up(c2, start_watch=False)
    assert "x1" not in c2.h.agents  # a full `up` does not bring it back
    with pytest.raises(RuntimeError, match="종료된 탭"):
        up(c2, tab="X", start_watch=False)
    assert "[X] 종료됨" in "\n".join(status(c2))


def test_close_keeps_config_and_reopen_continues_conversations(live, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    d = tmp_path / "claude" / "projects" / "-p"
    d.mkdir(parents=True)
    (d / "fake-x1-session.jsonl").write_text("{}\n")  # x1's transcript still exists
    T.finish(live, "X", "close")
    assert "closed" not in saved(live)["tabs"]["X"]
    from herdr_ctl.up import load_sessions
    assert load_sessions(live.project)["x1"]["id"] == "fake-x1-session"
    lines = T.reopen(live, "X", resume=True)
    assert any("이전 대화 이어서" in ln for ln in lines)
    assert live.h.agents["x1"].args[:2] == ["--resume", "fake-x1-session"]
    assert live.h.agents["x2"].args[:2] == ["--session", "fake-x2-session"]  # opencode
    assert "--resume" not in live.h.agents["x-orch"].args  # its transcript is gone: fresh start


def test_reopen_an_ended_tab_starts_fresh(live):
    T.finish(live, "X", "end")
    c2 = make_ctx(live.project, live.h)
    T.reopen(c2, "X", resume=True)  # nothing recorded is left to continue
    assert "closed" not in saved(c2)["tabs"]["X"]
    assert "--session" not in c2.h.agents["x2"].args


def test_delete_removes_settings_only(live):
    work = live.project.flow_dir / "work" / "X"
    work.mkdir(parents=True)
    (work / "task.md").write_text("t")
    assert "flow build-review-x" in T.delete_plan(live, live.cfg.tab("X"))
    lines = T.finish(live, "X", "delete")
    data = saved(live)
    assert "X" not in data["tabs"]
    assert not any(s.get("agent") in ("x1", "x2") for f in (data.get("flows") or {}).values() for s in f["steps"])
    assert any("삭제" in ln for ln in lines)
    assert work.exists()  # files stay


def test_restart_resume_keeps_the_conversation(live):
    msg = restart(live, "x2", resume=True)
    assert "대화 유지" in msg
    assert live.h.agents["x2"].args[:2] == ["--session", "fake-x2-session"]


def test_restart_resume_falls_back_without_a_conversation(live):
    msg = restart(live, "y1", resume=True, handoff=False)  # claude without transcript
    assert "새로 시작" in msg and "--resume" not in live.h.agents["y1"].args


def test_restart_tab_orders_orchestrator_last(live):
    lines = T.restart_tab(live, "X", resume=False, handoff=False)
    assert [ln.split(":")[0] for ln in lines] == ["x1", "x2", "x-orch"]


# ---------------------------------------------------------------- worktree
def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def wt(live, tmp_path):
    root = live.project.root
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "init")
    path = tmp_path / "wt-x"
    git(root, "worktree", "add", "-q", "-b", "feat/x", str(path))
    live.state["worktrees"]["X"] = {"workspace_id": "gone", "cwd": str(path), "branch": "feat/x", "base": "main"}
    live.state.save()
    return live, path


def test_unmerged_worktree_is_kept_and_reported(wt):
    ctx, path = wt
    (path / "b.txt").write_text("b\n")
    git(path, "add", "b.txt")
    git(path, "commit", "-q", "-m", "add b")
    (path / "c.txt").write_text("dirty\n")
    info = T.worktree_info(ctx, ctx.cfg.tab("X"))
    assert not info["safe"] and info["unmerged"][0].endswith("add b") and info["dirty"] == ["c.txt"]
    with pytest.raises(T.WorktreeNotMerged, match="커밋 1개"):
        T.finish(ctx, "X", "end", worktree="remove")
    assert "x1" in ctx.h.agents and path.exists()  # refused before anything was touched
    T.finish(ctx, "X", "end", worktree="force")
    assert not path.exists()
    assert subprocess.run(["git", "-C", str(ctx.project.root), "rev-parse", "--verify", "feat/x"],
                          capture_output=True).returncode == 0  # forced: the branch stays


def test_squash_merged_worktree_is_removed_with_its_branch(wt):
    ctx, path = wt
    (path / "b.txt").write_text("b\n")
    git(path, "add", "b.txt")
    git(path, "commit", "-q", "-m", "add b")
    root = ctx.project.root
    git(root, "merge", "-q", "--squash", "feat/x")
    git(root, "commit", "-q", "-m", "squash feat/x")
    info = T.worktree_info(ctx, ctx.cfg.tab("X"))
    assert info["safe"], info
    lines = T.finish(ctx, "X", "close", worktree="remove")
    assert not path.exists() and any("브랜치 삭제: feat/x" in ln for ln in lines)


# --------------------------------------------------------------------- cli
def test_cli_end_reopen_delete(live, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_ctx", lambda args, **kw: make_ctx(live.project, live.h))
    assert cli.main(["tab", "end", "X"]) == 1  # not a tty: needs --yes
    assert "--yes" in capsys.readouterr().err and "closed" not in saved(live)["tabs"]["X"]
    assert cli.main(["tab", "end", "X", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "지우는 것" in out and "남는 것: 탭 설정, prompt" in out
    assert saved(live)["tabs"]["X"]["closed"] is True
    assert cli.main(["tab", "reopen", "X"]) == 0 and "closed" not in saved(live)["tabs"]["X"]
    assert cli.main(["down", "X"]) == 0 and "closed" not in saved(live)["tabs"]["X"]
    assert cli.main(["tab", "delete", "X", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "남는 것  : prompt" in out and "X" not in saved(live)["tabs"]
    assert cli.main(["status"]) == 0


def test_cli_confirm_declined_says_cancelled(live, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_ctx", lambda args, **kw: make_ctx(live.project, live.h))
    monkeypatch.setattr(cli, "tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["tab", "delete", "X"]) == 1
    assert "취소했습니다 (바뀐 것 없음)" in capsys.readouterr().out and "X" in saved(live)["tabs"]


def test_conversation_record_is_private_and_validated(live):
    """state.json is writable by auto-edit agents: a forged conversation id there must never reach
    --resume/--session; the record lives in the private state home and is re-validated on read."""
    import json
    from herdr_ctl.up import _sessions_path, load_sessions
    T.finish(live, "X", "close")
    assert not _sessions_path(live.project).is_relative_to(live.project.root)
    state_file = live.project.flow_dir / "state.json"
    data = json.loads(state_file.read_text())
    data["sessions"] = {"x2": {"agent": "opencode", "id": "worker-conversation"}}
    state_file.write_text(json.dumps(data))
    rec = json.loads(_sessions_path(live.project).read_text())
    rec["x1"] = {"agent": "claude", "id": "../../etc/passwd"}
    _sessions_path(live.project).write_text(json.dumps(rec))
    assert "x1" not in load_sessions(live.project)
    T.reopen(make_ctx(live.project, live.h), "X", resume=True)
    assert live.h.agents["x2"].args[:2] == ["--session", "fake-x2-session"]  # its own record, not state.json's
    assert "--resume" not in live.h.agents["x1"].args
