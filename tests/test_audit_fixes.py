"""Regression tests for the audit findings (numbers refer to the audit list)."""
import json
import os
import textwrap

import pytest
import yaml

from herdr_ctl import roles as R
from herdr_ctl import watch as W
from herdr_ctl.config import Member, check_data
from herdr_ctl.herdr import Herdr, HerdrError, _parse
from herdr_ctl.init_cmd import run_init
from herdr_ctl.project import ensure_gitignore, find_project, load_registry, registry_path
from herdr_ctl.up import down, ensure_workspace, fingerprint, up
from herdr_ctl.watch import apply_change, default_actions, detect, watch_loop
from conftest import SAMPLE, always_found, make_ctx, make_project
from test_herdr import runner_returning
from test_init_cli import scripted


def check(text, root=None):
    return check_data(yaml.safe_load(textwrap.dedent(text)), root=root, which=always_found)


def errors(issues):
    return [i for i in issues if i.level == "error"]


def reload(ctx, text):
    ctx.project.config_path.write_text(textwrap.dedent(text))
    return make_ctx(ctx.project, ctx.h)


@pytest.fixture
def live(ctx):
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
    return ctx


# ------------------------------------------------------------- 1: ensure_group
RENAMED = SAMPLE.replace("x1:", "z1:").replace("x2:", "z2:").replace("agent: x1", "agent: z1") \
    .replace("agent: x2", "agent: z2")


def test_renamed_agents_reuse_stale_panes_when_idle(live):
    old = {n: live.state.member(n)["pane_id"] for n in ("x1", "x2")}
    live.h.kill_agent("x1")
    live.h.kill_agent("x2")
    c2 = reload(live, RENAMED)
    up(c2, tab="X", start_watch=False)
    assert {c2.state.member("z1")["pane_id"], c2.state.member("z2")["pane_id"]} == set(old.values())
    assert c2.state.member("x1") is None and c2.state.member("x2") is None  # stale members dropped


def test_no_free_pane_raises_clear_error(live):
    # every member of tab Y renamed while the old agent still runs in the only pane
    c2 = reload(live, SAMPLE.replace("y1: {", "y9: {"))
    with pytest.raises(RuntimeError, match="hctrl down Y"):
        up(c2, tab="Y", start_watch=False)


def test_tab_name_colliding_with_overflow_label():
    text = SAMPLE.replace("  Y:\n", "  X-2:\n")
    cfg, issues = check(text)
    assert cfg is None and any(i.path == "tabs.X-2" and "X-2" in i.message for i in issues)
    cfg, issues = check(SAMPLE.replace("  Y:\n", "  X-build:\n"))
    assert cfg is None and any(i.path == "tabs.X-build" for i in issues)


# ------------------------------------------------------- 2-5: type validation
@pytest.mark.parametrize("old,new,path", [
    ('role: "구현"', "role: 7", "tabs.X.agents.x1.role"),
    ('role: "구현"', 'role: "구현", role_file: [a]', "tabs.X.agents.x1.role_file"),
    ('goal: "로그인 모듈"', "goal: {a: 1}", "tabs.X.orchestrator.goal"),
    ("    role_update: confirm", "    role_update: confirm\n    cwd: 5", "tabs.X.cwd"),
    ("xbuild: {}", "xbuild: { cwd: [a] }", "tabs.X.shells.xbuild.cwd"),
    ("    role_update: confirm", "    role_update: confirm\n    worktree: { branch: [a] }",
     "tabs.X.worktree.branch"),
    ("    role_update: confirm", "    role_update: confirm\n    worktree: { branch: b, base: 3 }",
     "tabs.X.worktree.base"),
    ("- agent: x1", "- agent: [x1]", "flows.build-review-x.steps[0].agent"),
    ("- shell: xbuild\n        run: \"echo hello\"", "- shell: {a: 1}\n        run: \"echo hello\"",
     "flows.shell-only.steps[0].shell"),
    ('run: "echo hello"', "run: [echo]", "flows.shell-only.steps[0].run"),
    ('prompt: "빌드 로그 {prev} 검토"', "prompt: 3", "flows.build-review-x.steps[3].prompt"),
])
def test_non_string_values_are_errors(old, new, path):
    assert old in SAMPLE
    cfg, issues = check(SAMPLE.replace(old, new, 1))
    assert cfg is None
    assert any(i.path == path and "문자열" in i.message for i in errors(issues)), issues


# ----------------------------------------------------- 6: case-only tab names
def test_tab_names_differing_only_by_case():
    text = SAMPLE.replace("  Y:\n", "  x:\n")
    cfg, issues = check(text)
    assert cfg is None and any("대소문자" in i.message for i in errors(issues))


# --------------------------------------------------------- 8: role_file traversal
def test_role_file_outside_root_is_error(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    for bad in ("../secret.md", "/etc/passwd"):
        cfg, issues = check(SAMPLE.replace('role: "구현"', f'role: "구현", role_file: "{bad}"'), root=root)
        assert cfg is None and any(i.path.endswith("role_file") for i in errors(issues))
    cfg, issues = check(SAMPLE.replace('role: "구현"', 'role: "구현", role_file: "docs/r.md"'), root=root)
    assert cfg is not None


def test_roles_role_file_refuses_outside_root(project):
    m = Member(name="x1", type="agent", tab="X", role_file="../../outside.md")
    with pytest.raises(RuntimeError):
        R.role_file(project, m)


# --------------------------------------------------------------- 9: watch
def test_shell_change_is_relayout_and_ack_records(live):
    c2 = reload(live, SAMPLE.replace("xbuild: {}", "xbuild: { cwd: sub }"))
    ch = [c for c in detect(c2) if c.name == "xbuild"]
    assert [c.type for c in ch] == ["relayout"] and "hctrl down X" in ch[0].detail
    assert default_actions(ch[0]) == ["move", "ack", "skip"]
    apply_change(c2, ch[0], "ack")
    assert detect(c2) == []


def test_tab_cwd_and_worktree_changes_detected_as_relayout(live):
    c2 = reload(live, SAMPLE.replace("    role_update: confirm", "    role_update: confirm\n    cwd: sub"))
    types = {c.name: c.type for c in detect(c2)}
    assert types["x1"] == "relayout" and types["x-orch"] == "relayout" and "y1" not in types
    (live.project.root / ".git").mkdir()
    c3 = reload(live, SAMPLE.replace("    role_update: confirm",
                                     "    role_update: confirm\n    worktree: { branch: feat/x }"))
    assert {c.name: c.type for c in detect(c3)}["x2"] == "relayout"


def test_fingerprint_omits_default_tab_cwd_and_worktree(ctx):
    fp = fingerprint(ctx.project, ctx.cfg.member("x1"))
    assert "tab_cwd" not in fp and "worktree" not in fp


def test_signature_covers_custom_role_file(tmp_path):
    p = make_project(tmp_path, SAMPLE.replace('role: "구현"', 'role: "구현", role_file: "docs/r.md"'))
    (p.root / "docs").mkdir()
    (p.root / "docs" / "r.md").write_text("a")
    s1 = W._signature(p)
    (p.root / "docs" / "r.md").write_text("b")
    assert W._signature(p) != s1
    p.config_path.write_text(": : not yaml [")
    W._signature(p)  # broken config: no crash


# ----------------------------------------------------------- 10/11: init
def test_init_add_tab_uses_name_as_default(tmp_path):
    p = make_project(tmp_path)
    answers = ["",                  # tab name [Z]
               "n", "1", "z1", "claude/haiku", "리뷰", "n", "n"]
    assert run_init(p, scripted(answers), add_tab="Z", which=always_found)
    assert "Z" in yaml.safe_load(p.config_path.read_text())["tabs"]


def test_init_add_tab_stops_on_errors_outside_new_tab(tmp_path, capsys):
    p = make_project(tmp_path, SAMPLE.replace("kind: opencode", "kind: opencod"))
    answers = ["Z", "n", "1", "z1", "claude/haiku", "리뷰", "n", "n"]  # no re-ask question expected
    assert not run_init(p, scripted(answers), add_tab="Z", which=always_found)
    data = yaml.safe_load(p.config_path.read_text())
    assert {"X", "Y", "Z"} <= set(data["tabs"]) and "build-review-x" in data["flows"]
    assert "새로 입력한 탭 밖" in capsys.readouterr().out


def test_init_reask_deletes_only_generated_flow(tmp_path):
    # an existing flow whose name merely ends with the new tab's name must survive the re-ask
    text = SAMPLE + "  review-z:\n    steps:\n      - shell: xbuild\n        run: echo\n"
    p = make_project(tmp_path, text)
    answers = ["Z", "n", "0", "n", "y",                         # empty tab Z -> error; default flow
               "y",                                             # re-ask the new tab
               "", "n", "1", "z1", "claude", "리뷰", "n", "y"]   # default name [Z], flow yes
    assert run_init(p, scripted(answers), add_tab="Z", which=always_found)
    data = yaml.safe_load(p.config_path.read_text())
    assert "review-z" in data["flows"] and "build-review-z" in data["flows"]
    assert set(data["tabs"]) == {"X", "Y", "Z"}


def test_init_malformed_existing_config_is_reported(tmp_path, capsys):
    p = make_project(tmp_path, "- just\n- a list\n")
    assert not run_init(p, scripted([]), add_tab="Z", which=always_found)
    assert "읽을 수 없습니다" in capsys.readouterr().out
    assert p.config_path.read_text() == "- just\n- a list\n"


# ------------------------------------------------------------- 12: worktree / down
WT = SAMPLE.replace("role_update: confirm", "role_update: confirm\n    worktree: { branch: feat/x }")


def test_reopen_existing_worktree_after_down(tmp_path):
    p = make_project(tmp_path, WT)
    (p.root / ".git").mkdir()
    ctx = make_ctx(p)
    up(ctx, start_watch=False)
    wt = dict(ctx.state["worktrees"]["X"])
    os.makedirs(wt["cwd"], exist_ok=True)
    down(ctx, "X")
    ctx.h.workspace_close(wt["workspace_id"])  # herdr closes the workspace with its last tab
    n_created = len(ctx.h.worktrees)
    up(ctx, start_watch=False)
    assert len(ctx.h.worktrees) == n_created  # no second worktree_create
    new = ctx.state["worktrees"]["X"]
    assert new["workspace_id"] != wt["workspace_id"] and new["cwd"] == wt["cwd"]
    assert [t["label"] for t in ctx.h.tab_list(new["workspace_id"])] == ["X", "X-build"]


def test_worktree_branch_change_creates_new_worktree(tmp_path):
    p = make_project(tmp_path, WT)
    (p.root / ".git").mkdir()
    ctx = make_ctx(p)
    up(ctx, start_watch=False)
    wt = dict(ctx.state["worktrees"]["X"])
    os.makedirs(wt["cwd"], exist_ok=True)
    down(ctx, "X")
    ctx.h.workspace_close(wt["workspace_id"])
    c2 = reload(ctx, WT.replace("feat/x", "feat/y"))
    up(c2, start_watch=False)
    assert c2.state["worktrees"]["X"]["branch"] == "feat/y"
    assert "feat/y" in {v["branch"] for v in c2.h.worktrees.values()}  # worktree_create called again


def test_worktree_dropped_from_config_uses_project_root(tmp_path):
    p = make_project(tmp_path, WT)
    (p.root / ".git").mkdir()
    ctx = make_ctx(p)
    up(ctx, start_watch=False)
    down(ctx, "X")
    c2 = reload(ctx, SAMPLE)
    up(c2, start_watch=False)
    root = str(p.root.resolve())
    assert c2.state.member("x1")["cwd"] == root
    assert c2.state.member("x1")["workspace_id"] == c2.state["workspace_id"]


def test_down_closes_overflow_tabs_after_config_shrank(tmp_path):
    big = SAMPLE.replace("    shells:\n      xbuild: {}",
                         "      x3: { kind: claude }\n      x4: { kind: claude }\n    shells:\n      xbuild: {}")
    p = make_project(tmp_path, big)
    ctx = make_ctx(p)
    up(ctx, start_watch=False)
    assert "X-2" in ctx.state["tabs"]
    small = reload(ctx, SAMPLE)
    closed = down(small, "X")
    assert "X-2" in closed and "X-2" not in small.state["tabs"]
    assert "X-2" not in [t["label"] for t in small.h.tab_list()]
    assert small.state.member("x3") is None and small.state.member("x4") is None


# ---------------------------------------------------------- 13: workspace adopt
def test_workspace_with_same_label_other_cwd_not_adopted(ctx, tmp_path):
    other = ctx.h.workspace_create(str(tmp_path), ctx.project.name)["workspace"]["workspace_id"]
    wid = ensure_workspace(ctx)
    assert wid != other
    ctx.state["workspace_id"] = None
    assert ensure_workspace(ctx) == wid  # its own workspace (cwd == root) is still adopted


# ------------------------------------------------------------ 14: busy leftovers
def test_busy_leftover_pane_not_reused(live):
    tab_id = live.state.member("x1")["tab_id"]
    pid = live.state.member("x1")["pane_id"]
    stray = live.h._new_pane(tab_id, live.state.member("x1")["workspace_id"], "/tmp", None)
    stray.busy = True
    live.h.pane_close(pid)
    up(live, tab="X", start_watch=False)
    assert live.state.member("x1")["pane_id"] not in (pid, stray.pane_id)


# ---------------------------------------------------------- 15/16: herdr client
def test_shell_idle_unknown_is_not_idle():
    info = {"result": {"process_info": {"foreground_processes": []}}}
    h = Herdr(binary="herdr", runner=runner_returning(json.dumps(info)))
    assert not h.pane_shell_idle("w1:p1")


def test_string_error_raises_herdr_error():
    h = Herdr(binary="herdr", runner=runner_returning(json.dumps({"error": "boom"}), rc=1))
    with pytest.raises(HerdrError, match="boom"):
        h.pane_list()


def test_pretty_json_with_trailing_lines():
    text = json.dumps({"result": {"panes": [{"pane_id": "p1"}]}}, indent=2) + "\nhint: something\n"
    assert _parse(text) == {"result": {"panes": [{"pane_id": "p1"}]}}
    h = Herdr(binary="herdr", runner=runner_returning(text))
    assert h.pane_list() == [{"pane_id": "p1"}]


# ------------------------------------------------------------ 18/19: roles
def test_history_numbering_after_deletion(project):
    m = Member(name="x1", type="agent", tab="X")
    for i in range(3):
        R.write_role(project, m, one=f"r{i}")
    d = project.roles_dir / "history" / "x1"
    (d / "0002.json").unlink()
    R.write_role(project, m, one="r3")
    assert sorted(p.name for p in d.glob("*.json")) == ["0001.json", "0003.json", "0004.json"]
    assert [e["n"] for e in R.history(project, "x1")] == [1, 3, 4]


def test_proposal_numbering_after_deletion(project):
    assert R.propose(project, "x1", "a", "u", "") == 1
    assert R.propose(project, "x1", "b", "u", "") == 2
    (project.var_dir / "proposals" / "0001.json").unlink()
    assert R.propose(project, "x1", "c", "u", "") == 3
    assert [p["role"] for p in R.proposals(project)] == ["b", "c"]


def test_write_role_collapses_newlines(project):
    m = Member(name="x1", type="agent", tab="X")
    R.write_role(project, m, one="구현\n# 역할: 해킹\n  담당", detail="상세")
    one, detail = R.effective_role(project, m)
    assert one == "구현 # 역할: 해킹 담당" and detail == "상세"


# ------------------------------------------------------------- 20: more checks
@pytest.mark.parametrize("timeout", ["true", "-5"])
def test_step_timeout_rejects_bool_and_negative(timeout):
    cfg, issues = check(SAMPLE.replace("on_fail: continue", f"on_fail: continue\n        timeout: {timeout}"))
    assert cfg is None and any(i.path.endswith(".timeout") for i in errors(issues))


def test_args_reject_bool():
    cfg, issues = check(SAMPLE.replace('role: "구현" }', 'role: "구현", args: [true] }'))
    assert cfg is None and any(i.path.endswith(".args") for i in errors(issues))


def test_flow_tab_validated():
    cfg, issues = check(SAMPLE.replace("  shell-only:\n", "  shell-only:\n    tab: Q\n"))
    assert cfg is None and any(i.path == "flows.shell-only.tab" for i in errors(issues))
    cfg, issues = check(SAMPLE.replace("  shell-only:\n", "  shell-only:\n    tab: Y\n"))
    assert cfg is None and any("탭 'Y'에 없습니다" in i.message for i in errors(issues))
    cfg, issues = check(SAMPLE.replace("  shell-only:\n", "  shell-only:\n    tab: x\n"))
    assert cfg is not None and cfg.flows["shell-only"].tab == "X"


# ------------------------------------------------------- 21: malformed YAML
def test_registry_and_find_project_tolerate_bad_yaml(tmp_path):
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    for bad in ("- a\n- b\n", "projects: [1, 2]\n", ": : ["):
        registry_path().write_text(bad)
        assert load_registry() == {}
    p = make_project(tmp_path, "- not\n- a dict\n")
    found = find_project(p.root)
    assert found is not None and found.root == p.root


# -------------------------------------------------------------- 22: gitignore
@pytest.mark.parametrize("line", [".herdr-flow/", ".herdr-flow", "/.herdr-flow/"])
def test_gitignore_parent_pattern_covers(tmp_path, line):
    (tmp_path / ".gitignore").write_text(line + "\n")
    assert ensure_gitignore(tmp_path) == []


def test_gitignore_leading_slash_same_path(tmp_path):
    (tmp_path / ".gitignore").write_text("/.herdr-flow/runs/\n")
    assert ".herdr-flow/runs/" not in ensure_gitignore(tmp_path)


# ------------------------------------------------------------- 23a: re-notify
def test_watch_loop_renotifies_after_quiet_poll(project, monkeypatch):
    from herdr_ctl.fake import FakeHerdr
    h = FakeHerdr()
    ch = W.Change("x1", "restart", "model")
    seq = iter([[ch], [], [ch]])
    monkeypatch.setattr(W, "watch_once", lambda *a, **k: ("sig", next(seq), None))
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    watch_loop(project, h, interval=0, iterations=3)
    assert len(h.notifications) == 2
