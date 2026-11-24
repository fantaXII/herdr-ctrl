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
    assert labels == {"_hc", "X", "X-build", "Y"}
    # X: orch + x1 + x2 => 3 panes (main-right); X-build: xbuild alone; Y: 1 pane
    x_tab = ctx.state["tabs"]["X"]["tab_id"]
    assert len([p for p in h.panes.values() if p.tab_id == x_tab]) == 3
    assert ctx.state.member("xbuild")["tab_id"] == ctx.state["tabs"]["X-build"]["tab_id"]
    splits = [c for c in h.calls if c[0] == "pane_split"]
    assert [c[2] for c in splits] == ["right", "down"]
    assert set(rep.started) == {"x-orch", "x1", "x2", "y1"}
    assert h.panes[ctx.state.member("x1")["pane_id"]].label == "hctrl:x1"
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
    assert "Bash(hctrl:*)" in orch_args
    rules = ctx.project.flow_dir / "orchestrator-X.md"
    text = rules.read_text()
    assert "x1" in text and "build-review-x" in text and "금지 사항" in text and "로그인 모듈" in text
    # opencode gets its brief through OPENCODE_CONFIG (system prompt), not as a first prompt
    assert not any("briefs/x2.md" in p for p in ctx.h.agents["x2"].prompts)


def test_up_arms_pane_shells_with_the_role(ctx):
    """The role lives in the pane shell too: a fresh conversation or a hand-started agent keeps it."""
    import json
    import shlex
    import stat
    from herdr_ctl.up import opencode_config_path, sealed_brief_path
    up(ctx, start_watch=False)
    h = ctx.h
    runs = {c[1]: c[2] for c in h.calls if c[0] == "pane_run"}
    x2 = ctx.cfg.member("x2")
    # opencode: OPENCODE_CONFIG → a private config whose instructions is a private copy of the brief
    x2_pane = ctx.state.member("x2")["pane_id"]
    cfg_path = opencode_config_path(ctx.project, x2)
    sealed = sealed_brief_path(ctx.project, x2)
    assert runs[x2_pane].startswith(f" export OPENCODE_CONFIG={shlex.quote(str(cfg_path))}; printf")
    assert json.loads(cfg_path.read_text()) == {"instructions": [str(sealed)]}
    for p in (cfg_path, sealed):  # not editable by the agents (outside the project tree)
        assert not str(p).startswith(str(ctx.project.root))
    assert sealed.read_text() == (ctx.project.var_dir / "briefs" / "x2.md").read_text()
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
    # claude: a shell function adding the private brief when the flag is missing; the start args
    # read a private copy too
    x1, x1_pane = ctx.cfg.member("x1"), ctx.state.member("x1")["pane_id"]
    assert runs[x1_pane].startswith(" function claude {")
    assert str(sealed_brief_path(ctx.project, x1)) in runs[x1_pane]
    x1_args = next(c[4] for c in h.calls if c[0] == "agent_start" and c[1] == "x1")
    i = x1_args.index("--append-system-prompt-file")
    assert x1_args[i + 1] == str(sealed_brief_path(ctx.project, x1, start=True))
    orch = ctx.cfg.member("x-orch")
    assert sealed_brief_path(ctx.project, orch).read_text() == \
        (ctx.project.flow_dir / "orchestrator-X.md").read_text()
    # the shell is armed (and its line ran) before the agent command is typed into it
    i_run = next(i for i, c in enumerate(h.calls) if c[0] == "pane_run" and c[1] == x2_pane)
    i_start = next(i for i, c in enumerate(h.calls) if c[0] == "agent_start" and c[1] == "x2")
    assert i_run < i_start
    assert "__HC_ARM_" in h.panes[x2_pane].output.split("$ ")[-1]


def test_claude_shell_function_adds_the_brief_only_when_missing(ctx, tmp_path):
    """The armed line, run by a real bash: hand-started claude gets the flag, hctrl's start does not
    get it twice, subcommands keep working, and a user alias named claude still works."""
    import subprocess
    from herdr_ctl.up import sealed_brief_path
    up(ctx, start_watch=False)
    x1_pane = ctx.state.member("x1")["pane_id"]
    line = next(c[2] for c in ctx.h.calls if c[0] == "pane_run" and c[1] == x1_pane)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    (fake / "claude").chmod(0o755)
    brief = str(sealed_brief_path(ctx.project, ctx.cfg.member("x1")))

    def argv(cmd, before=""):
        out = subprocess.run(["bash", "-c", before + line + "\n" + cmd], capture_output=True, text=True,
                             env={"PATH": f"{fake}:/usr/bin:/bin"}).stdout
        out = out.split("\n", 1)[1] if out.startswith("__HC_ARM_") else out
        return out.rstrip("|").split("|") if out else []
    assert argv("claude") == ["--append-system-prompt-file", brief]
    assert argv("claude -c") == ["--append-system-prompt-file", brief, "-c"]
    assert argv("claude --append-system-prompt-file /other.md --model x") == \
        ["--append-system-prompt-file", "/other.md", "--model", "x"]
    assert argv("claude --append-system-prompt-file=/o.md") == ["--append-system-prompt-file=/o.md"]
    assert argv("claude 'see --append-system-prompt-file in docs'") == \
        ["--append-system-prompt-file", brief, "see --append-system-prompt-file in docs"]
    assert argv("claude mcp list") == ["--append-system-prompt-file", brief, "mcp", "list"]
    alias = "shopt -s expand_aliases; alias claude='claude --mine'\n"
    assert argv("claude", before=alias) == ["--append-system-prompt-file", brief, "--mine"]


def test_opencode_falls_back_to_a_brief_prompt_without_a_posix_shell(ctx):
    ctx.h.shell_name = "fish"
    up(ctx, start_watch=False)
    assert not any(c[0] == "pane_run" and c[1] == ctx.state.member("x2")["pane_id"] for c in ctx.h.calls)
    assert any("briefs/x2.md" in p for p in ctx.h.agents["x2"].prompts)


def test_login_shell_name_is_armed(ctx):
    ctx.h.shell_name = "-bash"
    up(ctx, start_watch=False)
    assert any(c[0] == "pane_run" and c[1] == ctx.state.member("x2")["pane_id"] for c in ctx.h.calls)
    assert not ctx.h.agents["x2"].prompts


def test_opencode_falls_back_when_the_armed_line_did_not_run(ctx, monkeypatch):
    """No marker in the pane (the shell dropped the line): opencode gets its brief as a first prompt."""
    from herdr_ctl.herdr import HerdrError

    def no_output(pane_id, regex, timeout_ms=None):
        raise HerdrError("timeout", regex)
    monkeypatch.setattr(ctx.h, "pane_wait_output", no_output)
    up(ctx, start_watch=False)
    assert any("briefs/x2.md" in p for p in ctx.h.agents["x2"].prompts)


def test_handoff_stays_out_of_the_shell_brief(ctx, tmp_path):
    """A restart's handoff is for that start only: a later fresh conversation does not resume it."""
    from herdr_ctl.up import sealed_brief_path, start_agent
    up(ctx, start_watch=False)
    for name in ("x1", "x2"):
        m, pane = ctx.cfg.member(name), ctx.state.member(name)["pane_id"]
        ctx.h.kill_agent(name)
        handoff = tmp_path / f"{name}-handoff.md"
        handoff.write_text("이전 상태")
        assert start_agent(ctx, m, pane, handoff) is None
        assert str(handoff) not in sealed_brief_path(ctx.project, m).read_text()
    assert str(tmp_path / "x1-handoff.md") in sealed_brief_path(ctx.project, ctx.cfg.member("x1"),
                                                                start=True).read_text()
    assert any(str(tmp_path / "x2-handoff.md") in p for p in ctx.h.agents["x2"].prompts)


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
                          "      x3: { kind: claude }\n      x4: { kind: claude }\n    shells:\n      xbuild: {}")
    ctx = make_ctx(make_project(tmp_path, text))
    up(ctx, start_watch=False)
    assert ctx.state.member("x4")["tab_label"] == "X-2"
    assert ctx.state.member("x-orch")["tab_label"] == "X"
    assert ctx.state.member("xbuild")["tab_label"] == "X-build"
    labels = [t["label"] for t in ctx.h.tab_list()]
    assert "X-2" in labels and "X-build" in labels


def test_old_layout_shell_moves_to_build_tab(ctx):
    """Sessions laid out before X-build existed: an idle shell pane in tab X moves to X-build."""
    up(ctx, start_watch=False)
    h, st = ctx.h, ctx.state
    old = st.member("xbuild")["pane_id"]
    x_tab = st["tabs"]["X"]["tab_id"]
    h.panes[old].tab_id = x_tab  # as the old layout had it: no X-build tab at all
    h.tab_close(st["tabs"].pop("X-build")["tab_id"])
    up(ctx, start_watch=False)
    assert old not in h.panes
    new = st.member("xbuild")
    assert new["tab_label"] == "X-build" and h.panes[new["pane_id"]].tab_id != x_tab


def test_busy_old_layout_shell_is_not_moved(ctx):
    up(ctx, start_watch=False)
    h, st = ctx.h, ctx.state
    old = st.member("xbuild")["pane_id"]
    h.panes[old].tab_id = st["tabs"]["X"]["tab_id"]
    h.tab_close(st["tabs"].pop("X-build")["tab_id"])
    h.pane_shell_idle = lambda pid: pid != old
    with pytest.raises(RuntimeError, match="실행 중"):
        up(ctx, tab="X", start_watch=False)
    assert old in h.panes


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
    assert closed == ["X", "X-build"] and "x1" not in ctx.h.agents and "y1" in ctx.h.agents
    assert ctx.state.member("x1") is None
    with pytest.raises(KeyError):
        zoom(ctx, "x1")


def test_status_lines(ctx):
    up(ctx, start_watch=False)
    out = "\n".join(status(ctx))
    assert "[X]" in out and "x-orch" in out and "orchestrator" in out and "xbuild" in out
