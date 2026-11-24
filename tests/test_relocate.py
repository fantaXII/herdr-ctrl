"""Moving a running tab between the project folder and its worktree (hctrl ui → 작업 위치):
safe cases are done automatically, anything else stops before a change (relocate.py)."""
import subprocess
import textwrap
from pathlib import Path

import pytest

from herdr_ctl.relocate import RelocateBlocked, relocate_tab
from herdr_ctl.up import up
from herdr_ctl.watch import apply_change, detect

from conftest import SAMPLE, make_ctx, make_project

WT = SAMPLE.replace("role_update: confirm", "role_update: confirm\n    worktree: { branch: feat/x }")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def repo(tmp_path, text):
    p = make_project(tmp_path, text)
    git(p.root, "init", "-q", "-b", "main")
    git(p.root, "config", "user.email", "t@example.com")
    git(p.root, "config", "user.name", "t")
    (p.root / "app.txt").write_text("v1\n")
    (p.root / ".gitignore").write_text(".herdr-flow/\n")
    git(p.root, "add", "-A")
    git(p.root, "commit", "-qm", "init")
    return p


def writes_handoff(agent, text):
    if "요약해 쓰세요" in text:
        Path(text.rsplit(": ", 1)[1].strip()).write_text(f"{agent.name}: 진행 상황")
        return "idle"
    return "idle"


def start(p):
    ctx = make_ctx(p)
    ctx.h.git_worktrees = True
    ctx.h.default_on_prompt = writes_handoff
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
    return ctx


def switch(ctx, text):
    """The user saves a new 작업 위치 in hctrl ui: same herdr and state, new config."""
    ctx.project.config_path.write_text(textwrap.dedent(text))
    c2 = make_ctx(ctx.project, ctx.h)
    return c2


def where(ctx):
    wt = ctx.state["worktrees"].get("X") or {}
    return {n: "worktree" if ctx.state.member(n)["workspace_id"] == wt.get("workspace_id") else "main"
            for n in ("x-orch", "x1", "x2", "xbuild")}


def move(ctx):
    ch = [c for c in detect(ctx) if c.type == "relayout"]
    assert ch, detect(ctx)
    return apply_change(ctx, ch[0], "move")


# ------------------------------------------------------------------ worktree → project folder
def test_worktree_to_main_merges_moves_work_files_and_cleans_up(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt = Path(ctx.state["worktrees"]["X"]["cwd"])
    assert set(where(ctx).values()) == {"worktree"}
    (wt / "app.txt").write_text("v2\n")
    git(wt, "commit", "-qam", "feature")
    task = wt / ".herdr-flow" / "work" / "X" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("할 일")
    c2 = switch(ctx, SAMPLE)
    msg = move(c2)
    root = c2.project.root
    assert (root / "app.txt").read_text() == "v2\n" and "fast-forward" in msg
    assert (root / ".herdr-flow" / "work" / "X" / "task.md").read_text() == "할 일"
    assert not wt.exists() and "feat/x" not in git(root, "branch")
    assert set(where(c2).values()) == {"main"} and "X" not in c2.state["worktrees"]
    assert c2.h.panes[c2.state.member("x1")["pane_id"]].cwd == str(root.resolve())
    assert "handoff/x1.md" in (c2.project.var_dir / "briefs" / "x1.md").read_text()  # started with its handoff
    assert detect(c2) == []
    # the other members' changes of the same tab: already done by the first
    assert relocate_tab(c2, "X") == ["탭 X: 이미 반영됨"]


def test_diverged_main_gets_a_merge_commit(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    (wt / "new.txt").write_text("x\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "feature")
    (root / "other.txt").write_text("y\n")
    git(root, "add", "other.txt")
    git(root, "commit", "-qm", "main moved on")
    msg = move(switch(ctx, SAMPLE))
    assert "병합 커밋" in msg and (root / "new.txt").exists() and (root / "other.txt").exists()


@pytest.mark.parametrize("make_dirty, why", [
    (lambda wt, root: (wt / "app.txt").write_text("half done\n"), "worktree에 커밋 안 된 변경"),
    (lambda wt, root: (wt / "notes.txt").write_text("x"), "git에 없는 파일"),
    (lambda wt, root: (root / "app.txt").write_text("user edit\n"), "프로젝트 폴더에 커밋 안 된 변경"),
    (lambda wt, root: git(root, "checkout", "-qb", "other"), "브랜치에 있어"),
])
def test_worktree_to_main_stops_before_any_change(tmp_path, make_dirty, why):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    (wt / "f.txt").write_text("f\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "feature")
    make_dirty(wt, root)
    head = git(root, "rev-parse", "HEAD")
    c2 = switch(ctx, SAMPLE)
    with pytest.raises(RelocateBlocked, match=why):
        move(c2)
    assert git(root, "rev-parse", "HEAD") == head and wt.is_dir()
    assert set(where(c2).values()) == {"worktree"}
    assert any(c.type == "relayout" for c in detect(c2))  # still pending


def test_a_conflict_is_undone_and_the_tab_stays(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    (wt / "app.txt").write_text("theirs\n")
    git(wt, "commit", "-qam", "feature")
    (root / "app.txt").write_text("ours\n")
    git(root, "commit", "-qam", "main edit")
    c2 = switch(ctx, SAMPLE)
    with pytest.raises(RelocateBlocked, match="충돌"):
        move(c2)
    assert git(root, "status", "--porcelain") == "" and not (root / ".git" / "MERGE_HEAD").exists()
    assert (root / "app.txt").read_text() == "ours\n" and set(where(c2).values()) == {"worktree"}


def test_busy_tab_is_not_moved(tmp_path):
    ctx = start(repo(tmp_path, WT))
    ctx.h.agents["x1"].status = "working"
    c2 = switch(ctx, SAMPLE)
    with pytest.raises(RelocateBlocked, match="'x1'가 작업 중"):
        move(c2)
    assert set(where(c2).values()) == {"worktree"}


def test_work_file_clash_stops(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    for base, text in ((wt, "worktree 쪽"), (root, "메인 쪽")):
        f = base / ".herdr-flow" / "work" / "X" / "task.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    with pytest.raises(RelocateBlocked, match="같은 작업 파일"):
        move(switch(ctx, SAMPLE))


# ------------------------------------------------------------------ project folder → worktree
def test_main_to_worktree_takes_the_work_files_along(tmp_path):
    ctx = start(repo(tmp_path, SAMPLE))
    root = ctx.project.root
    task = root / ".herdr-flow" / "work" / "X" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("할 일")
    (root / "scratch.txt").write_text("untracked")
    c2 = switch(ctx, WT)
    msg = move(c2)
    wt = Path(c2.state["worktrees"]["X"]["cwd"])
    assert set(where(c2).values()) == {"worktree"} and "feat/x" in git(root, "branch")
    assert (wt / ".herdr-flow" / "work" / "X" / "task.md").read_text() == "할 일"
    assert not task.exists()  # moved, not copied: one copy to come back with
    assert c2.h.panes[c2.state.member("x1")["pane_id"]].cwd == str(wt)
    assert "scratch.txt" in msg and (root / "scratch.txt").exists()  # left where it was, and said so
    assert detect(c2) == []


def test_main_to_worktree_stops_on_uncommitted_changes(tmp_path):
    ctx = start(repo(tmp_path, SAMPLE))
    (ctx.project.root / "app.txt").write_text("edit\n")
    c2 = switch(ctx, WT)
    with pytest.raises(RelocateBlocked, match="누구의 변경인지"):
        move(c2)
    assert set(where(c2).values()) == {"main"} and "X" not in c2.state["worktrees"]


def test_move_is_the_default_relayout_action(tmp_path):
    """The UI preselects the first action: "옮기기" (move); "확인" (ack) still only records."""
    from herdr_ctl.watch import default_actions
    ctx = start(repo(tmp_path, WT))
    c2 = switch(ctx, SAMPLE)
    ch = [c for c in detect(c2) if c.type == "relayout"]
    assert default_actions(ch[0])[0] == "move"


# ------------------------------------------------------------------ review findings
def test_round_trip_main_worktree_main(tmp_path):
    """The work files go along both ways; the tab ends where it began with the worktree's edits."""
    ctx = start(repo(tmp_path, SAMPLE))
    root = ctx.project.root
    task = root / ".herdr-flow" / "work" / "X" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("v1")
    c2 = switch(ctx, WT)
    move(c2)
    wt = Path(c2.state["worktrees"]["X"]["cwd"])
    (wt / ".herdr-flow" / "work" / "X" / "task.md").write_text("v2 (worktree에서 고침)")
    (wt / "feature.txt").write_text("f\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-qm", "feature")
    for a in c2.h.agents.values():
        a.status = "idle"
    c3 = switch(c2, SAMPLE)
    move(c3)
    assert task.read_text() == "v2 (worktree에서 고침)" and (root / "feature.txt").exists()
    assert not wt.exists() and set(where(c3).values()) == {"main"}


def test_work_file_edited_during_the_handoff_turn_is_kept(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt = Path(ctx.state["worktrees"]["X"]["cwd"])
    task = wt / ".herdr-flow" / "work" / "X" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("처음")

    def handoff_and_update_task(agent, text):
        if "요약해 쓰세요" in text:
            Path(text.rsplit(": ", 1)[1].strip()).write_text("진행")
            if agent.name == "x-orch":
                task.write_text("인수인계하며 갱신")
        return "idle"
    ctx.h.default_on_prompt = handoff_and_update_task
    c2 = switch(ctx, SAMPLE)
    move(c2)
    assert (c2.project.root / ".herdr-flow" / "work" / "X" / "task.md").read_text() == "인수인계하며 갱신"


def test_no_handoff_means_nothing_changed(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    (wt / "f.txt").write_text("f\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "feature")
    head = git(root, "rev-parse", "HEAD")
    ctx.h.default_on_prompt = lambda a, t: "idle"  # never writes its handoff
    c2 = switch(ctx, SAMPLE)
    with pytest.raises(RuntimeError, match="인수인계"):
        move(c2)
    assert git(root, "rev-parse", "HEAD") == head and set(where(c2).values()) == {"worktree"}
    assert not c2.state["relocating"]


def test_a_failed_up_is_finished_by_the_retry(tmp_path, monkeypatch):
    """Taken down, then laying out again failed: the retry continues with the same handoffs and
    still cleans up the worktree."""
    from herdr_ctl import relocate as R
    ctx = start(repo(tmp_path, WT))
    wt = Path(ctx.state["worktrees"]["X"]["cwd"])
    c2 = switch(ctx, SAMPLE)
    real_up = R.up
    calls = []

    def failing_up(*a, **kw):
        calls.append(kw.get("handoffs"))
        if len(calls) == 1:
            raise HerdrError("server_gone", "herdr went away")
        return real_up(*a, **kw)
    from herdr_ctl.herdr import HerdrError
    monkeypatch.setattr(R, "up", failing_up)
    with pytest.raises(HerdrError):
        move(c2)
    assert c2.state["relocating"]["X"]["to"] == "to_main" and wt.is_dir()
    out = relocate_tab(c2, "X")
    assert out[0].startswith("이전에 멈춘") and calls[1] == calls[0] and set(calls[1]) == {"x-orch", "x1", "x2"}
    assert not wt.exists() and set(where(c2).values()) == {"main"} and not c2.state["relocating"]


def test_a_kept_worktree_is_not_silently_reopened(tmp_path):
    ctx = start(repo(tmp_path, SAMPLE))
    ctx.state["worktrees"]["X"] = {"workspace_id": "gone", "cwd": str(tmp_path / "old-wt"), "branch": "feat/x"}
    ctx.state.save()
    c2 = switch(ctx, WT)
    with pytest.raises(RelocateBlocked, match="이전 worktree가 남아"):
        move(c2)


def test_unknown_base_is_not_merged(tmp_path):
    ctx = start(repo(tmp_path, WT))
    ctx.state["worktrees"]["X"].pop("base", None)
    ctx.state.save()
    with pytest.raises(RelocateBlocked, match="기록이 없어"):
        move(switch(ctx, SAMPLE))


def test_ignored_files_keep_the_worktree(tmp_path):
    ctx = start(repo(tmp_path, WT))
    wt, root = Path(ctx.state["worktrees"]["X"]["cwd"]), ctx.project.root
    (root / ".gitignore").write_text(".herdr-flow/\n.env\n")
    git(root, "commit", "-qam", "ignore .env")
    git(wt, "merge", "-q", "main")
    (wt / ".env").write_text("SECRET=1")
    msg = move(switch(ctx, SAMPLE))
    assert "worktree 폴더는 남겼습니다" in msg and ".env" in msg and (wt / ".env").exists()


def test_a_locked_member_stops_the_move(tmp_path, monkeypatch):
    from herdr_ctl import tabrename
    from herdr_ctl.state import acquire
    ctx = start(repo(tmp_path, WT))
    c2 = switch(ctx, SAMPLE)
    monkeypatch.setattr(tabrename, "_busy", lambda *a: [])  # the lock is taken after the check
    acquire(c2.project, "x2", "hctrl send", pid=1)
    with pytest.raises(RelocateBlocked, match="진행 중인 작업"):
        move(c2)
    assert set(where(c2).values()) == {"worktree"}
