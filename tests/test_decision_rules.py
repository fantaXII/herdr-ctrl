"""Tabs with an orchestrator: agents hand decisions back in their result (`결정 필요:`), not as `## QUESTION`."""
from herdr_ctl.roles import auto_rules, render_agent_brief, render_orchestrator_rules
from herdr_ctl.ui import UIServer
from conftest import always_found


def test_agent_brief_routes_decisions_to_the_orchestrator(ctx):
    brief = render_agent_brief(ctx.project, ctx.cfg, ctx.cfg.member("x1")).read_text()
    assert "결정 필요:" in brief and "'x-orch'" in brief
    assert "## QUESTION" in brief  # still there, for information only the user has


def test_no_orchestrator_no_decision_rules(ctx):
    brief = render_agent_brief(ctx.project, ctx.cfg, ctx.cfg.member("y1")).read_text()
    assert "결정 필요:" not in brief and "## QUESTION" in brief


def test_orchestrator_rules_explain_decision_requests(ctx):
    rules = render_orchestrator_rules(ctx.project, ctx.cfg, ctx.cfg.tab("X")).read_text()
    assert "## 작업자의 결정 요청" in rules and "hctrl send" in rules
    assert "x1: claude/sonnet — 역할: 구현" in rules  # the division of work comes from one-line roles


def test_auto_rules_match_what_is_sent(ctx):
    m = ctx.cfg.member("x1")
    brief = render_agent_brief(ctx.project, ctx.cfg, m).read_text()
    assert auto_rules(ctx.cfg, m).strip() in brief
    assert "작업자의 결정 요청" in auto_rules(ctx.cfg, ctx.cfg.member("x-orch"))


def test_ui_role_shows_auto_rules(ctx):
    srv = UIServer("session", start=ctx.project, which=always_found)
    r = srv.get_role(ctx.project, "x1")
    assert r["type"] == "agent" and "결정 필요:" in r["auto"]
    r = srv.get_role(ctx.project, "x-orch")
    assert r["type"] == "orch" and "작업자의 결정 요청" in r["auto"]
