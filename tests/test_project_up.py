import pytest

from herdr_ctl.fake import FakeHerdr
from herdr_ctl.project import ensure_gitignore, find_project, register
from herdr_ctl.up import down, status, up, zoom
from conftest import SAMPLE, make_ctx, make_project


# ------------------------------------------------------------- project
def test_find_project_registry_subdir(tmp_path):
    root = tmp_path / "a"
    (root / "src" / "deep").mkdir(parents=True)
    register("alpha", root)
    p = find_project(root / "src" / "deep")
    assert p.name == "alpha" and p.root == root.resolve()


def test_find_project_longest_registered_wins(tmp_path):
    outer, inner = tmp_path / "o", tmp_path / "o" / "i"
    inner.mkdir(parents=True)
    register("outer", outer)
    register("inner", inner)
    assert find_project(inner).name == "inner"
    assert find_project(outer).name == "outer"


def test_find_project_config_fallback(tmp_path):
    p = make_project(tmp_path)
    (p.root / "sub").mkdir()
    found = find_project(p.root / "sub")
    assert found.name == "projA" and found.root == p.root.resolve()


def test_find_project_none(tmp_path):
    assert find_project(tmp_path) is None


def test_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/")
    added = ensure_gitignore(tmp_path)
    assert ".herdr-flow/runs/" in added
    assert ensure_gitignore(tmp_path) == []
    assert "node_modules/" in (tmp_path / ".gitignore").read_text()


# ------------------------------------------------------------------ up
def test_up_creates_layout_and_agents(ctx):
    h = ctx.h
    rep = up(ctx, start_watch=False)
    labels = {t["label"] for t in h.tab_list()}
    assert labels == {"_hc", "X", "Y"}
    # X: orch + x1 + x2 + xbuild => 4 panes (grid: 3 splits); Y: 1 pane
    x_tab = ctx.state["tabs"]["X"]["tab_id"]
    assert len([p for p in h.panes.values() if p.tab_id == x_tab]) == 4
    splits = [c for c in h.calls if c[0] == "pane_split"]
    assert [c[2] for c in splits] == ["right", "down", "down"]
    assert set(rep.started) == {"x-orch", "x1", "x2", "y1"}
    assert h.panes[ctx.state.member("x1")["pane_id"]].label == "hc:x1"
    # orchestrator sits in the first (root) pane of tab X
    root = [c for c in h.calls if c[0] == "tab_create" and c[1] == "X"]
    assert root and ctx.state.member("x-orch")["pane_id"] in h.panes


def test_up_passes_model_permission_and_brief(ctx):
    up(ctx, start_watch=False)
    starts = {c[1]: c for c in ctx.h.calls if c[0] == "agent_start"}
    x1_args = starts["x1"][4]
    assert x1_args[:2] == ("--model", "sonnet")
    assert "--append-system-prompt-file" in x1_args and x1_args[-2:] == ("--permission-mode", "acceptEdits")
    assert starts["x2"][4] == ("-m", "anthropic/claude-sonnet-5-5")
    orch_args = starts["x-orch"][4]
    assert "Bash(hc:*)" in orch_args
    rules = ctx.project.flow_dir / "orchestrator-X.md"
    text = rules.read_text()
    assert "x1" in text and "build-review-x" in text and "금지 사항" in text and "로그인 모듈" in text
    # non-claude agent gets its brief as a first prompt
    assert any("briefs/x2.md" in p for p in ctx.h.agents["x2"].prompts)


def test_up_is_idempotent(ctx):
    up(ctx, start_watch=False)
    n_panes, n_tabs, n_agents = len(ctx.h.panes), len(ctx.h.tabs), len(ctx.h.agents)
    rep = up(ctx, start_watch=False)
    assert (len(ctx.h.panes), len(ctx.h.tabs), len(ctx.h.agents)) == (n_panes, n_tabs, n_agents)
    assert rep.started == [] and set(rep.skipped) == {"x-orch", "x1", "x2", "y1"}


def test_up_reuses_pane_of_exited_agent(ctx):
    up(ctx, start_watch=False)
    pane = ctx.state.member("x1")["pane_id"]
    ctx.h.kill_agent("x1")  # herdr forgets the name; the shell pane stays
    n_panes = len(ctx.h.panes)
    rep = up(ctx, start_watch=False)
    assert len(ctx.h.panes) == n_panes
    assert rep.started == ["x1"] and ctx.h.agents["x1"].pane_id == pane


def test_up_recreates_closed_pane(ctx):
    up(ctx, start_watch=False)
    ctx.h.pane_close(ctx.state.member("x2")["pane_id"])
    rep = up(ctx, start_watch=False)
    assert "x2" in rep.started and ctx.state.member("x2")["pane_id"] in ctx.h.panes


def test_up_state_survives_reload(project):
    h = FakeHerdr()
    up(make_ctx(project, h), start_watch=False)
    rep = up(make_ctx(project, h), start_watch=False)  # fresh State read from disk
    assert rep.started == []


def test_up_blocked_start_needs_attention(ctx):
    ctx.h.start_status["x1"] = "blocked"
    rep = up(ctx, start_watch=False)
    assert "x1" in rep.attention and "blocked" in rep.attention["x1"]
    assert "x2" in rep.started  # other agents still started
    assert any("화면" in line for line in rep.lines())
    assert any("확인 필요" in line for line in status(ctx))


def test_five_members_spill_into_second_tab(tmp_path):
    text = SAMPLE.replace("    shells:\n      xbuild: {}",
                          "      x3: { kind: claude }\n    shells:\n      xbuild: {}")
    ctx = make_ctx(make_project(tmp_path, text))
    up(ctx, start_watch=False)
    assert ctx.state.member("xbuild")["tab_label"] == "X-2"
    assert ctx.state.member("x-orch")["tab_label"] == "X"
    labels = [t["label"] for t in ctx.h.tab_list()]
    assert "X-2" in labels


def test_control_tab_runs_watch(ctx):
    up(ctx, start_watch=True)
    runs = [c for c in ctx.h.calls if c[0] == "pane_run"]
    assert runs and "watch" in runs[0][2] and runs[0][1] == ctx.state["control"]["pane_id"]


def test_worktree_tab(tmp_path):
    text = SAMPLE.replace("role_update: confirm", "role_update: confirm\n    worktree: { branch: feat/x }")
    p = make_project(tmp_path, text)
    (p.root / ".git").mkdir()
    ctx = make_ctx(p)
    up(ctx, start_watch=False)
    wt = ctx.state["worktrees"]["X"]
    assert wt["branch"] == "feat/x" and wt["workspace_id"] in ctx.h.worktrees
    assert ctx.state.member("x1")["workspace_id"] == wt["workspace_id"]
    assert ctx.state.member("y1")["workspace_id"] == ctx.state["workspace_id"]
    up(ctx, start_watch=False)  # idempotent: no second worktree
    assert len(ctx.h.worktrees) == 1
    down(ctx, "X", remove_worktree=True)
    assert ctx.h.worktrees == {} and "X" not in ctx.state["worktrees"]


def test_down_and_zoom(ctx):
    up(ctx, start_watch=False)
    zoom(ctx, "x2")
    assert ctx.h.panes[ctx.state.member("x2")["pane_id"]].zoomed
    closed = down(ctx, "X")
    assert closed == ["X"] and "x1" not in ctx.h.agents and "y1" in ctx.h.agents
    assert ctx.state.member("x1") is None
    with pytest.raises(KeyError):
        zoom(ctx, "x1")


def test_status_lines(ctx):
    up(ctx, start_watch=False)
    out = "\n".join(status(ctx))
    assert "[X]" in out and "x-orch" in out and "orchestrator" in out and "xbuild" in out
