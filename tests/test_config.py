import textwrap

import yaml

from herdr_ctl import cli
from herdr_ctl.config import agent_args, check_data, format_issues, parse_duration
from herdr_ctl.layout import SPLIT_PLANS, group_tab
from conftest import SAMPLE, always_found, make_project


def check(text, **kw):
    kw.setdefault("which", always_found)
    return check_data(yaml.safe_load(textwrap.dedent(text)), **kw)


def paths(issues, level=None):
    return {i.path for i in issues if level is None or i.level == level}


def test_sample_is_valid():
    cfg, issues = check(SAMPLE)
    assert cfg is not None, format_issues(issues)
    assert [m.name for m in cfg.tab("X").members] == ["x-orch", "x1", "x2", "xbuild"]
    assert cfg.member("x-orch").permission == "readonly"
    assert cfg.member("x1").permission == "edit"
    assert cfg.flows["build-review-x"].steps[2].on_fail == "continue"


def test_typo_key_suggestion():
    cfg, issues = check(SAMPLE.replace("model: sonnet", "modle: sonnet"))
    assert cfg is None
    msg = [str(i) for i in issues if i.path == "tabs.X.agents.x1.modle"]
    assert msg and "'model'인가요?" in msg[0]


def test_missing_agent_reference():
    cfg, issues = check(SAMPLE.replace("- agent: x2", "- agent: x3"))
    assert cfg is None
    hit = [i for i in issues if i.path == "flows.build-review-x.steps[1].agent"]
    assert hit and "x3" in hit[0].message and "x1" in hit[0].message


def test_bad_name_and_duplicate():
    text = SAMPLE.replace("y1: {", "X1: {")
    _, issues = check(text)
    assert "tabs.Y.agents.X1" in paths(issues, "error")
    text = SAMPLE.replace("y1: {", "x1: {")
    _, issues = check(text)
    assert any("중복" in i.message for i in issues)


def test_more_than_four_panes_warns_but_valid():
    text = SAMPLE.replace("    shells:\n      xbuild: {}",
                          "      x3: { kind: claude }\n      x4: { kind: claude }\n    shells:\n      xbuild: {}")
    cfg, issues = check(text)
    assert cfg is not None
    w = [i for i in issues if i.level == "warning" and i.path == "tabs.X"]
    assert w and "X-2" in w[0].message


def test_dangerous_args_error():
    # audit #7: bypass flags used to be warned about and then passed through; now rejected
    text = SAMPLE.replace('role: "구현" }', 'role: "구현", args: ["--dangerously-skip-permissions"] }')
    cfg, issues = check(text)
    assert cfg is None
    assert any(i.level == "error" and "우회" in i.message for i in issues)


def test_bypass_permission_for_work_agents():
    text = SAMPLE.replace('role: "구현" }', 'role: "구현", permission: bypass }')
    cfg, issues = check(text)
    assert cfg is not None
    assert "tabs.X.agents.x1.permission" in paths(issues, "warning")
    assert agent_args(cfg.member("x1"))[-2:] == ["--permission-mode", "bypassPermissions"]


def test_bypass_refused_for_orchestrator_and_unattended_tabs():
    cfg, issues = check(SAMPLE.replace('goal: "로그인 모듈" }', 'goal: "로그인 모듈", permission: bypass }'))
    assert cfg is None and "tabs.X.orchestrator.permission" in paths(issues, "error")
    text = SAMPLE.replace('role: "구현" }', 'role: "구현", permission: bypass }').replace(
        "role_update: confirm", "role_update: confirm\n    unattended: questions")
    cfg, issues = check(text)
    assert cfg is None and "tabs.X.agents.x1.permission" in paths(issues, "error")


def test_unknown_kind_and_bad_values():
    text = SAMPLE.replace("kind: opencode", "kind: opencod").replace("on_fail: continue", "on_fail: maybe")
    cfg, issues = check(text)
    errs = paths(issues, "error")
    assert "tabs.X.agents.x2.kind" in errs
    assert "flows.build-review-x.steps[2].on_fail" in errs


def test_timeout_format():
    assert parse_duration("90s") == 90 and parse_duration("30m") == 1800 and parse_duration("2h") == 7200
    text = SAMPLE.replace('prompt: "빌드 로그 {prev} 검토"', 'prompt: "x"\n        timeout: soon')
    _, issues = check(text)
    assert "flows.build-review-x.steps[3].timeout" in paths(issues, "error")


def test_integration_and_path_warnings():
    _, issues = check(SAMPLE, integrations={"claude": "current", "opencode": "not"})
    assert any("integration install opencode" in i.message for i in issues)
    _, issues = check(SAMPLE, which=lambda k: None)
    assert any("PATH" in i.message for i in issues)


def test_opencode_model_format_warning():
    _, issues = check(SAMPLE.replace("model: anthropic/claude-sonnet-5-5", "model: sonnet"))
    assert any(i.level == "warning" and "제공자/모델" in i.message for i in issues)


def test_worktree_requires_git(tmp_path):
    text = SAMPLE.replace("role_update: confirm", "role_update: confirm\n    worktree: { branch: feat/x }")
    _, issues = check(text, root=tmp_path)
    assert "tabs.X.worktree" in paths(issues, "error")
    (tmp_path / ".git").mkdir()
    cfg, issues = check(text, root=tmp_path)
    assert cfg is not None and cfg.tab("X").worktree == {"branch": "feat/x"}


def test_yaml_syntax_error(tmp_path):
    p = make_project(tmp_path, "session: a\ntabs: [\n")
    from herdr_ctl.config import check_file
    cfg, issues = check_file(p.config_path)
    assert cfg is None and "YAML 문법 오류" in issues[0].message


def test_agent_args_mapping():
    cfg, _ = check(SAMPLE)
    assert agent_args(cfg.member("x1")) == ["--model", "sonnet", "--permission-mode", "acceptEdits"]
    assert agent_args(cfg.member("x2")) == ["-m", "anthropic/claude-sonnet-5-5"]
    assert agent_args(cfg.member("y1")) == ["--model", "haiku", "--permission-mode", "auto"]
    o = agent_args(cfg.member("x-orch"), system_prompt_file="/r.md")
    assert o[:4] == ["--model", "opus", "--append-system-prompt-file", "/r.md"]
    assert o[-5:] == ["--allowedTools", "Read", "Grep", "Glob", "Bash(hctrl:*)"]


def test_layout_grouping():
    cfg, _ = check(SAMPLE)
    groups = group_tab(cfg.tab("X"))
    # shells never share the agent tab: they get their own X-build tab
    assert [g.label for g in groups] == ["X", "X-build"] and len(groups[0].members) == 3
    assert [m.name for m in groups[1].members] == ["xbuild"] and groups[1].title == "X-build"
    text = SAMPLE.replace("    shells:\n      xbuild: {}",
                          "      x3: { kind: claude }\n      x4: { kind: claude }\n    shells:\n      xbuild: {}")
    cfg, _ = check(text)
    groups = group_tab(cfg.tab("X"))
    assert [g.label for g in groups] == ["X", "X-2", "X-build"]
    assert groups[0].members[0].name == "x-orch"
    assert [m.name for m in groups[1].members] == ["x4"]
    assert len(SPLIT_PLANS[3]) == 2 and len(SPLIT_PLANS[4]) == 3


def test_cli_check_exit_codes(tmp_path, monkeypatch, capsys):
    class NoHerdr:
        def integration_status(self):
            return {"claude": "current", "opencode": "current"}
    monkeypatch.setattr(cli, "Herdr", NoHerdr)
    p = make_project(tmp_path)
    assert cli.main(["--project", str(p.root), "check"]) == 0
    p.config_path.write_text(textwrap.dedent(SAMPLE).replace("model: sonnet", "modle: sonnet"))
    assert cli.main(["--project", str(p.root), "check"]) == 1
    assert "modle" in capsys.readouterr().out
    p.config_path.write_text(textwrap.dedent(SAMPLE).replace(
        "    shells:\n      xbuild: {}", "      x3: { kind: claude }\n    shells:\n      xbuild: {}"))
    assert cli.main(["--project", str(p.root), "check"]) == 0  # warnings only
