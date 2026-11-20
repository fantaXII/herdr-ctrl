"""Unattended mode (PLAN.md §10): config, hook, answers, runner integration."""
import json
import os
import textwrap
import threading
import time
from pathlib import Path

import pytest

from herdr_ctl import cli
from herdr_ctl import unattended as ua
from herdr_ctl.config import agent_args, check_data
from herdr_ctl.orchestrator import Actor, LimitExceeded, ScopeError, start_ask
from herdr_ctl.roles import render_agent_brief, render_orchestrator_rules
from herdr_ctl.runner import QUESTION, create_job, create_run, execute, load_run, prepare_resume, stop, wait
from herdr_ctl.up import fingerprint, up
from herdr_ctl.watch import detect
from conftest import OUT_RE, make_ctx, make_project

CFG = """
session: projU
tabs:
  X:
    orchestrator: { kind: claude, model: opus }
    unattended: @MODE@
    unattended_policy: { answer_timeout: 30s, max_answers_per_ask: 3, deny: ["git push"] }
    agents:
      x1: { kind: claude, model: sonnet, role: "구현" }
      x2: { kind: claude, model: haiku, role: "테스트" }
  Y:
    orchestrator: none
    agents:
      y1: { kind: claude, model: haiku }
flows:
  one:
    steps:
      - agent: x1
        prompt: "작업. 결과는 {out}"
"""


def make(tmp_path, mode="approve"):
    project = make_project(tmp_path, CFG.replace("@MODE@", mode), name="projU")
    ctx = make_ctx(project)
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
        a.prompts.clear()
    return ctx


@pytest.fixture
def actx(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_QUESTION_POLL_S", "0.02")
    return make(tmp_path, "approve")


USER = Actor("user")
ORCH_X = Actor("orch", "x-orch", "X")
AGENT_X1 = Actor("agent", "x1", "X")


def in_thread(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("condition not met")


# ------------------------------------------------------------------ config
def base(**tab):
    t = {"orchestrator": {"kind": "claude"}, "agents": {"a1": {"kind": "claude"}}}
    t.update(tab)
    return {"session": "s", "tabs": {"T": t}}


def errors(data):
    return [str(i) for i in check_data(data, which=lambda _: "/x")[1] if i.level == "error"]


def warnings(data):
    return [str(i) for i in check_data(data, which=lambda _: "/x")[1] if i.level == "warning"]


def test_config_default_off_and_values():
    cfg, _ = check_data(base(), which=lambda _: "/x")
    assert cfg.tabs[0].unattended == "off"
    for v in ("off", False, "questions", "approve"):  # False: YAML bare `off`
        cfg, issues = check_data(base(unattended=v), which=lambda _: "/x")
        assert cfg is not None, issues
    assert cfg.tabs[0].agents[0].unattended == "approve"
    assert any("unattended" in e for e in errors(base(unattended="yes")))


def test_config_requires_orchestrator():
    data = base(unattended="questions")
    data["tabs"]["T"]["orchestrator"] = "none"
    assert any("orchestrator가 없는" in e for e in errors(data))
    data["tabs"]["T"]["unattended"] = "off"
    assert not errors(data)


def test_config_policy_validation():
    assert not errors(base(unattended_policy={"answer_timeout": "5m", "max_answers_per_ask": 5, "deny": ["rm -rf"]}))
    cfg, _ = check_data(base(unattended_policy={"answer_timeout": "5m"}), which=lambda _: "/x")
    assert cfg.tabs[0].policy["answer_timeout"] == 300
    assert errors(base(unattended_policy={"answer_timeout": "1s"}))
    assert errors(base(unattended_policy={"answer_timeout": "soon"}))
    assert errors(base(unattended_policy={"max_answers_per_ask": -1}))
    assert errors(base(unattended_policy={"max_answers_per_ask": True}))
    assert errors(base(unattended_policy={"deny": "rm"}))
    assert errors(base(unattended_policy={"denny": []}))
    assert errors(base(unattended_policy=[1]))


def test_config_warnings_opencode_and_managed_args():
    data = base(unattended="approve")
    data["tabs"]["T"]["agents"]["o1"] = {"kind": "opencode"}
    assert any("o1" in w and "무인 응답" in w for w in warnings(data))
    data = base()
    data["tabs"]["T"]["agents"]["a1"]["args"] = ["--permission-mode", "plan"]
    assert any("충돌" in w for w in warnings(data))
    data["tabs"]["T"]["agents"]["a1"]["args"] = ["--settings=/tmp/x.json"]
    assert any("충돌" in w for w in warnings(data))


def test_role_update_bare_off_accepted():
    cfg, issues = check_data(base(role_update=False), which=lambda _: "/x")
    assert cfg is not None, issues
    assert cfg.tabs[0].role_update == "off"


def test_permission_args_are_explicit():
    cfg, _ = check_data(base(), which=lambda _: "/x")
    a1 = cfg.tabs[0].agents[0]
    a1.permission = "ask"
    assert agent_args(a1)[-2:] == ["--permission-mode", "default"]
    orch = cfg.tabs[0].orchestrator
    args = agent_args(orch)
    assert args.index("--permission-mode") < args.index("--allowedTools")  # variadic option last
    assert args[args.index("--permission-mode") + 1] == "default"
    assert agent_args(a1, settings_file=Path("/s.json"))[:2] == ["--settings", "/s.json"]


# ------------------------------------------------------------- up / brief
def test_hook_injected_only_for_claude_workers(tmp_path):
    ctx = make(tmp_path, "questions")
    starts = {c[1]: c[4] for c in ctx.h.calls if c[0] == "agent_start"}
    assert "--settings" not in starts["x-orch"] and "--settings" not in starts["y1"]
    i = starts["x1"].index("--settings")
    path = Path(starts["x1"][i + 1])
    assert ctx.project.root not in path.parents  # outside every agent cwd
    data = json.loads(path.read_text())
    entry = data["hooks"]["PermissionRequest"][0]
    assert entry["matcher"] == "AskUserQuestion"
    hook = entry["hooks"][0]
    assert "_perm-hook x1" in hook["command"] and str(ctx.project.root) in hook["command"]
    assert hook["timeout"] == 30 + 60


def test_hook_matcher_approve_and_off(tmp_path):
    ctx = make(tmp_path / "a", "approve")
    path = ctx.project.secure_dir / "hooks" / "x1.json"
    assert json.loads(path.read_text())["hooks"]["PermissionRequest"][0]["matcher"] == "*"
    ctx = make(tmp_path / "o", "off")
    assert not any("--settings" in c[4] for c in ctx.h.calls if c[0] == "agent_start")


def test_fingerprint_and_mode_change_needs_restart(tmp_path):
    ctx = make(tmp_path, "off")
    assert "unattended" not in fingerprint(ctx.project, ctx.cfg.member("x1"))
    ctx.project.config_path.write_text(textwrap.dedent(CFG.replace("@MODE@", "questions")))
    ctx2 = make_ctx(ctx.project, ctx.h)
    changed = {c.name: c for c in detect(ctx2)}
    assert changed["x1"].type == "restart" and "unattended" in changed["x1"].detail
    assert "y1" not in changed and "x-orch" not in changed


def test_briefs_and_rules(tmp_path):
    ctx = make(tmp_path, "approve")
    brief = render_agent_brief(ctx.project, ctx.cfg, ctx.cfg.member("x1")).read_text()
    assert "## QUESTION" in brief
    rules = render_orchestrator_rules(ctx.project, ctx.cfg, ctx.cfg.tab("X")).read_text()
    assert "무인 모드 (approve)" in rules and "hctrl answer" in rules and "deny" in rules
    ctx = make(tmp_path / "off", "off")
    rules = render_orchestrator_rules(ctx.project, ctx.cfg, ctx.cfg.tab("X")).read_text()
    assert "무인 모드" not in rules and "자동으로 승인하지 않습니다" in rules


def test_ask_budget_outside_project(actx):
    start_ask(actx, actx.cfg.tab("X"))
    assert (actx.project.secure_dir / "ask-X.json").exists()
    assert not list(actx.project.flow_dir.rglob("ask-X.json"))


# ------------------------------------------------------------- run_hook
def payload(tool, tinput, cwd=None):
    return json.dumps({"hook_event_name": "PermissionRequest", "tool_name": tool, "tool_input": tinput,
                       "cwd": cwd, "permission_suggestions": [{"type": "addRules", "behavior": "allow"}]})


ASK = {"questions": [{"question": "색을 고르세요", "header": "색", "multiSelect": False,
                      "options": [{"label": "빨강"}, {"label": "파랑"}]}]}


def hook_async(ctx, agent, data):
    box = {}
    t = in_thread(lambda: box.setdefault("out", ua.run_hook(ctx, agent, data)))
    q = until(lambda: ua.pending(ctx.project))
    return t, box, q[0]


def decision(out):
    return json.loads(out)["hookSpecificOutput"]["decision"]


def test_hook_permission_allow(actx):
    actx.h.agents["x1"].status = "blocked"
    t, box, q = hook_async(actx, "x1", payload("Bash", {"command": "npm test"}))
    assert q["kind"] == "permission" and q["hook_pid"] == os.getpid() and "npm test" in q["question"]
    ua.answer(actx, ORCH_X, q["id"], "allow")
    t.join(5)
    d = decision(box["out"])
    assert d == {"behavior": "allow"}  # never updatedPermissions / suggestions
    assert ua.load(actx.project, q["id"])["status"] == "resolved"
    events = [json.loads(l)["event"] for l in ua.audit_path(actx.project).read_text().splitlines()]
    assert sorted(events) == ["answered", "asked", "delivered"] and events[-1] == "delivered"


def test_hook_permission_deny_with_reason(actx):
    actx.h.agents["x1"].status = "blocked"
    t, box, q = hook_async(actx, "x1", payload("Bash", {"command": "make deploy"}))
    ua.answer(actx, USER, q["id"], "deny 배포는 사용자 결정입니다")
    t.join(5)
    d = decision(box["out"])
    assert d["behavior"] == "deny" and "배포는 사용자 결정" in d["message"]


def test_hook_choice_answer(actx):
    actx.h.agents["x1"].status = "blocked"
    t, box, q = hook_async(actx, "x1", payload("AskUserQuestion", ASK))
    assert q["kind"] == "choice" and "1. 빨강 / 2. 파랑" in q["question"]
    with pytest.raises(RuntimeError):
        ua.answer(actx, ORCH_X, q["id"], "초록")
    ua.answer(actx, ORCH_X, q["id"], "2")
    t.join(5)
    d = decision(box["out"])
    assert d["behavior"] == "allow"
    assert d["updatedInput"] == {"questions": ASK["questions"], "answers": {"색을 고르세요": "파랑"}}


def test_choice_answer_forms():
    q = {"kind": "choice", "tool_input": {"questions": [
        {"question": "A?", "options": [{"label": "a1"}, {"label": "a2"}], "multiSelect": True},
        {"question": "B?", "options": [{"label": "b1"}]}]}}
    with pytest.raises(ValueError, match="여러 개"):
        ua.parse_answer(q, "1")
    r = ua.parse_answer(q, "", json.dumps({"A?": "1,a2", "B?": "b1"}))
    assert r["answers"] == {"A?": "a1, a2", "B?": "b1"}
    with pytest.raises(ValueError, match="모든 질문"):
        ua.parse_answer(q, "", json.dumps({"A?": "1"}))
    with pytest.raises(ValueError):
        ua.parse_answer(q, "", "{bad")
    assert ua.parse_answer(q, "deny 모르겠음")["decision"] == "deny"
    p = {"kind": "permission"}
    assert ua.parse_answer(p, "allow") == {"decision": "allow"}
    with pytest.raises(ValueError):
        ua.parse_answer(p, "allow 하지만")
    with pytest.raises(ValueError):
        ua.parse_answer(p, "maybe")
    with pytest.raises(ValueError):
        ua.parse_answer({"kind": "text"}, "  ")


@pytest.mark.parametrize("tool,tinput,why", [
    ("Bash", {"command": "git push origin main"}, "deny 목록"),
    ("Bash", {"command": "cat .herdr-flow/state.json"}, ".herdr-flow"),
    ("Bash", {"command": "vi ~/.claude/settings.json"}, ".claude"),
    ("Bash", {"command": "hctrl answer q0001 allow"}, "hctrl 명령"),
    ("Bash", {"command": "env -u HERDR_PANE_ID python3 -m herdr_ctl.cli answer q1 allow"}, "herdr"),
    ("Write", {"file_path": "/etc/passwd", "content": "x"}, "작업 폴더 밖"),
    ("Edit", {"file_path": "../../outside.txt"}, "작업 폴더 밖"),
    ("Write", {"content": "x"}, "경로가 없습니다"),
])
def test_hook_policy_escalates_to_user(actx, tool, tinput, why):
    out = ua.run_hook(actx, "x1", payload(tool, tinput, cwd=str(actx.project.root)))
    assert out == ""
    assert not ua.pending(actx.project)
    last = json.loads(ua.audit_path(actx.project).read_text().splitlines()[-1])
    assert last["event"] == "escalated" and why in last["reason"]
    assert actx.h.notifications


def test_hook_policy_secure_dir_and_inside_paths(actx):
    secure = str(actx.project.secure_dir / "questions" / "q0001.json")
    assert ua.policy_escalation(actx, actx.cfg.tab("X"), "Bash", {"command": f"cat {secure}"}, None)
    tab = actx.cfg.tab("X")
    # project path containing "herdr" must not count as sensitive by itself
    assert ua.policy_escalation(actx, tab, "Write", {"file_path": str(actx.project.root / "src/a.py")},
                                str(actx.project.root)) is None
    assert ua.policy_escalation(actx, tab, "Write", {"file_path": "src/b.py"}, str(actx.project.root)) is None


def test_hook_falls_through(actx, tmp_path):
    assert ua.run_hook(actx, "x1", "not json") == ""
    assert ua.run_hook(actx, "x1", json.dumps({"hook_event_name": "PreToolUse"})) == ""
    assert ua.run_hook(actx, "x-orch", payload("Bash", {"command": "ls"})) == ""  # orchestrator itself
    assert ua.run_hook(actx, "y1", payload("Bash", {"command": "ls"})) == ""      # tab without unattended
    assert ua.run_hook(actx, "nobody", payload("Bash", {"command": "ls"})) == ""
    assert ua.run_hook(actx, "x1", payload("AskUserQuestion", {"questions": "x"})) == ""
    q = make(tmp_path / "q", "questions")
    assert q.cfg.tab("X").unattended == "questions"
    assert ua.run_hook(q, "x1", payload("Bash", {"command": "ls"})) == ""  # approvals stay with the user
    assert not ua.pending(actx.project) and not ua.pending(q.project)


def test_hook_timeout_and_screen_handled(actx, monkeypatch):
    actx.cfg.tab("X").policy["answer_timeout"] = 0.2
    actx.h.agents["x1"].status = "blocked"
    assert ua.run_hook(actx, "x1", payload("Bash", {"command": "ls"})) == ""
    q = ua.all_questions(actx.project)[-1]
    assert q["status"] == "expired" and "시간 초과" in q["closed_reason"]
    actx.cfg.tab("X").policy["answer_timeout"] = 30
    actx.h.agents["x1"].status = "blocked"

    def human_answers_on_screen(_):  # blocked was seen, then the dialog closes
        actx.h.agents["x1"].status = "working"
    assert ua.run_hook(actx, "x1", payload("Bash", {"command": "ls"}), sleep=human_answers_on_screen) == ""
    q = ua.all_questions(actx.project)[-1]
    assert q["status"] == "expired" and "화면" in q["closed_reason"]


def test_dead_hook_question_expires(actx):
    q = ua.create(actx.project, "permission", "x1", "X", hook_pid=999999999, hook_start="1")
    assert ua.load(actx.project, q["id"])["status"] == "expired"
    with pytest.raises(RuntimeError, match="expired"):
        ua.answer(actx, USER, q["id"], "allow")
    with pytest.raises(KeyError):
        ua.load(actx.project, "../etc")


# ------------------------------------------------------------- answer scope
def test_answer_scope_and_budget(actx, tmp_path):
    def mk(kind="text", tab="X", agent="x1"):
        return ua.create(actx.project, kind, agent, tab, question="?",
                         tool_input=ASK if kind == "choice" else None)
    with pytest.raises(ScopeError):
        ua.answer(actx, AGENT_X1, mk()["id"], "답")
    with pytest.raises(ScopeError):
        ua.answer(actx, ORCH_X, mk(tab="Y", agent="y1")["id"], "답")
    ua.answer(actx, USER, mk(tab="Y", agent="y1")["id"], "사용자는 항상 가능")
    start_ask(actx, actx.cfg.tab("X"))
    for _ in range(3):
        ua.answer(actx, ORCH_X, mk()["id"], "답")
    with pytest.raises(LimitExceeded):
        ua.answer(actx, ORCH_X, mk()["id"], "답")
    q = mk()
    ua.answer(actx, USER, q["id"], "답")
    with pytest.raises(RuntimeError, match="answered"):
        ua.answer(actx, USER, q["id"], "또")
    qctx = make(tmp_path / "q", "questions")
    p = ua.create(qctx.project, "permission", "x1", "X", tool_name="Bash", tool_input={"command": "ls"})
    with pytest.raises(ScopeError, match="approve"):
        ua.answer(qctx, ORCH_X, p["id"], "allow")
    off = make(tmp_path / "o", "off")
    t = ua.create(off.project, "text", "x1", "X", question="?")
    with pytest.raises(ScopeError, match="off"):
        ua.answer(off, ORCH_X, t["id"], "답")
    ua.answer(off, USER, t["id"], "답")


# ---------------------------------------------------------------- runner
def question_then_result(first="## QUESTION\nDB는 postgres와 mysql 중 무엇인가요?"):
    def cb(agent, text):
        m = OUT_RE.search(text)
        if "에 대한 답입니다" in text:
            Path(m.group(1)).write_text("결과: " + text.splitlines()[1] + "\n")
        else:
            Path(m.group(1)).write_text(first + "\n")
        return "done"
    return cb


def test_text_question_answered_during_run(actx):
    actx.h.on_prompt["x1"] = question_then_result()
    rec = create_run(actx.project, actx.cfg.flows["one"])

    def answerer():
        q = until(lambda: ua.pending(actx.project))[0]
        r = until(lambda: load_run(actx.project, rec["id"])["status"] == QUESTION and load_run(actx.project, rec["id"]))
        assert r["steps"][0]["question"] == q["id"]
        assert wait(actx.project, rec["id"], timeout_s=1)["status"] == QUESTION
        ua.answer(actx, ORCH_X, q["id"], "postgres")
    t = in_thread(answerer)
    rec = execute(actx, rec)
    t.join(5)
    assert rec["status"] == "done", rec
    out = Path(rec["steps"][0]["out"])
    assert out.read_text().startswith("결과: postgres")
    assert "DB는" in out.with_name("step-1.q1.md").read_text()
    q = ua.all_questions(actx.project)[0]
    assert q["kind"] == "text" and q["status"] == "resolved" and q["run"] == rec["id"]
    assert "DB는 postgres" in q["question"]


def test_text_question_timeout_then_resume(actx):
    actx.cfg.tab("X").policy["answer_timeout"] = 0.1
    actx.h.on_prompt["x1"] = question_then_result()
    rec = execute(actx, create_run(actx.project, actx.cfg.flows["one"]))
    assert rec["status"] == "blocked" and "hctrl answer q0001" in rec["steps"][0]["error"]
    assert rec["steps"][0]["question"] == "q0001"
    ua.answer(actx, USER, "q0001", "mysql")
    rec = execute(actx, prepare_resume(actx.project, rec["id"]), resume=True)
    assert rec["status"] == "done", rec
    assert "mysql" in Path(rec["steps"][0]["out"]).read_text()
    assert ua.load(actx.project, "q0001")["status"] == "resolved"


def test_text_question_off_mode_user_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_QUESTION_POLL_S", "0.02")
    ctx = make(tmp_path, "off")
    ctx.h.on_prompt["x1"] = question_then_result()
    rec = create_job(ctx.project, "x1", "작업", None)
    in_thread(lambda: ua.answer(ctx, USER, until(lambda: ua.pending(ctx.project))[0]["id"], "그대로"))
    rec = execute(ctx, rec)
    assert rec["status"] == "done"


def test_text_question_limit(actx):
    actx.h.on_prompt["x1"] = lambda a, t: (Path(OUT_RE.search(t).group(1)).write_text("## QUESTION\n또?\n"), "done")[1]

    def answer_all():
        for _ in range(5):
            ua.answer(actx, USER, until(lambda: ua.pending(actx.project))[0]["id"], "응")
    in_thread(answer_all)
    rec = execute(actx, create_run(actx.project, actx.cfg.flows["one"]))
    assert rec["status"] == "failed" and "5회" in rec["steps"][0]["error"]


def test_plain_question_heading_elsewhere_is_not_a_question(actx):
    actx.h.on_prompt["x1"] = lambda a, t: (Path(OUT_RE.search(t).group(1)).write_text("요약\n## QUESTION\n없음\n"), "done")[1]
    assert execute(actx, create_run(actx.project, actx.cfg.flows["one"]))["status"] == "done"
    assert not ua.all_questions(actx.project)


def hook_blocked_cb(ctx, kind="permission"):
    """Fake agent: the turn hits our hook (a question is registered) and blocks."""
    def cb(agent, text):
        ua.create(ctx.project, kind, agent.name, "X", tool_name="Bash", tool_input={"command": "npm test"},
                  question="Bash: npm test", hook_pid=os.getpid(), hook_start=None)
        Path(OUT_RE.search(text).group(1)).write_text("결과\n")
        return "blocked"
    return cb


def test_hook_question_keeps_worker_alive(actx, monkeypatch):
    # herdr flips blocked->working a moment after the hook resolves; the grace covers that lag
    monkeypatch.setenv("HC_HOOK_GRACE_S", "1")
    actx.h.on_prompt["x1"] = hook_blocked_cb(actx)
    rec = create_run(actx.project, actx.cfg.flows["one"])

    def orchestrator():
        r = until(lambda: (lambda x: x if x["status"] == QUESTION else None)(load_run(actx.project, rec["id"])))
        q = ua.load(actx.project, r["steps"][0]["question"])
        assert q["run"] == rec["id"]
        assert any("x1" in str(p) for p in actx.project.var_dir.glob("locks/*.lock"))  # lock still held
        ua.answer(actx, ORCH_X, q["id"], "allow")
        q.update(status="resolved")  # what the hook does after printing the decision
        ua.save(actx.project, q)
        actx.h.agents["x1"].status = "working"
    t = in_thread(orchestrator)
    rec = execute(actx, rec)
    t.join(5)
    assert rec["status"] == "done", rec
    assert rec["steps"][0]["status"] == "ok"


def test_blocked_without_hook_question_waits_for_user_on_screen(actx):
    """A dialog our hook doesn't handle (or a slow hook): tell the user once, keep waiting."""
    a = actx.h.agents["x1"]

    def cb(agent, text):
        out = Path(OUT_RE.search(text).group(1))

        def user_approves_on_screen():
            until(lambda: any("화면에서 직접" in body for _, body in actx.h.notifications))
            out.write_text("결과\n")
            a.status = "working"
        in_thread(user_approves_on_screen)
        return "blocked"
    actx.h.on_prompt["x1"] = cb
    rec = execute(actx, create_run(actx.project, actx.cfg.flows["one"]))
    assert rec["status"] == "done", rec


def test_blocked_wait_respects_step_timeout(actx):
    actx.h.on_prompt["x1"] = lambda a, t: "blocked"
    rec = execute(actx, create_job(actx.project, "x1", "작업", 0.5))
    assert rec["status"] == "timeout"


def test_blocked_in_off_tab_is_final(tmp_path):
    ctx = make(tmp_path, "off")
    ctx.h.on_prompt["x1"] = lambda a, t: "blocked"
    rec = execute(ctx, create_run(ctx.project, ctx.cfg.flows["one"]))
    assert rec["status"] == "blocked"


def test_stop_during_question(actx):
    actx.h.on_prompt["x1"] = question_then_result()
    rec = create_run(actx.project, actx.cfg.flows["one"])
    box = {}
    t = in_thread(lambda: box.setdefault("rec", execute(actx, rec)))
    until(lambda: load_run(actx.project, rec["id"])["status"] == QUESTION)
    stop(actx, rec["id"])
    t.join(5)
    assert box["rec"]["status"] == "stopped"
    assert ua.all_questions(actx.project)[0]["status"] == "expired"


# -------------------------------------------------------------------- cli
@pytest.fixture
def cli_env(actx, monkeypatch):
    monkeypatch.setattr(cli, "make_herdr", lambda session: actx.h)
    return actx


def hctrl(ctx, *args):
    return cli.main(["--project", str(ctx.project.root), *args])


def test_cli_pending_answer_and_scope(cli_env, monkeypatch, capsys):
    ctx = cli_env
    qx = ua.create(ctx.project, "choice", "x1", "X", tool_name="AskUserQuestion", tool_input=ASK)
    qy = ua.create(ctx.project, "text", "y1", "Y", question="Y 질문")
    assert hctrl(ctx, "pending") == 0
    out = capsys.readouterr().out
    assert qx["id"] in out and qy["id"] in out and "빨강" in out
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x-orch")["pane_id"])
    assert hctrl(ctx, "pending") == 0
    out = capsys.readouterr().out
    assert qx["id"] in out and qy["id"] not in out
    assert hctrl(ctx, "answer", qy["id"], "답") == 1
    assert hctrl(ctx, "answer", qx["id"], "파랑") == 0
    assert ua.load(ctx.project, qx["id"])["answer"]["answers"] == {"색을 고르세요": "파랑"}
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x1")["pane_id"])
    assert hctrl(ctx, "pending") == 1
    assert hctrl(ctx, "answer", qy["id"], "답") == 1
    assert "작업 agent" in capsys.readouterr().err


def test_wait_ignores_already_answered_question(actx):
    rec = create_run(actx.project, actx.cfg.flows["one"])
    q = ua.create(actx.project, "text", "x1", "X", question="?")
    rec["status"] = QUESTION
    rec["steps"][0].update(status=QUESTION, question=q["id"])
    from herdr_ctl.runner import save_run
    save_run(actx.project, rec)
    assert wait(actx.project, rec["id"], timeout_s=0.5, poll=0.05)["status"] == QUESTION
    ua.answer(actx, USER, q["id"], "답")
    t0 = time.time()
    wait(actx.project, rec["id"], timeout_s=0.5, poll=0.05)  # keeps waiting: answer not consumed yet
    assert time.time() - t0 >= 0.45


def test_cli_wait_exit_code_on_question(cli_env, capsys):
    ctx = cli_env
    rec = create_run(ctx.project, ctx.cfg.flows["one"])
    rec["status"] = QUESTION
    rec["steps"][0].update(status=QUESTION, question="q0009")
    from herdr_ctl.runner import save_run
    save_run(ctx.project, rec)
    assert hctrl(ctx, "wait", rec["id"]) == 3
    out = capsys.readouterr().out
    assert "q0009" in out and "hctrl pending" in out


def test_cli_perm_hook_reads_stdin(cli_env, monkeypatch, capsys):
    import io
    ctx = cli_env
    monkeypatch.setenv("HERDR_PANE_ID", ctx.state.member("x1")["pane_id"])  # runs inside the worker pane
    monkeypatch.setattr("sys.stdin", io.StringIO(payload("Bash", {"command": "git push"})))
    assert hctrl(ctx, "_perm-hook", "x1") == 0
    assert capsys.readouterr().out == ""
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hctrl(ctx, "_perm-hook", "x1") == 0


def test_policy_allows_only_step_result_files_under_runs(actx):
    tab, root = actx.cfg.tab("X"), str(actx.project.root)
    runs = actx.project.runs_dir.resolve()
    ok = str(runs / "r0001" / "step-1.md")
    assert ua.policy_escalation(actx, tab, "Write", {"file_path": ok, "content": "결과"}, root) is None
    for bad in (runs / "r0001" / "run.json", runs / "r0001" / "stop", runs / "r0001" / "step-1.q1.md",
                actx.project.flow_dir / "state.json", actx.project.var_dir / "locks" / "x1.lock"):
        assert ua.policy_escalation(actx, tab, "Write", {"file_path": str(bad), "content": "x"}, root)
    # Bash touching the runs dir is still the user's
    assert ua.policy_escalation(actx, tab, "Bash", {"command": f"rm {ok}"}, root)


def test_state_home_ignores_environment(monkeypatch):
    from herdr_ctl import project as P
    monkeypatch.setattr(P, "STATE_HOME_OVERRIDE", None)
    real = P.state_home()
    monkeypatch.setenv("HC_STATE_HOME", "/tmp/empty")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/empty")
    monkeypatch.setenv("HOME", "/tmp/empty")
    assert P.state_home() == real and not str(real).startswith("/tmp/empty")


def test_done_flicker_before_hook_question(actx, monkeypatch):
    """Live finding: herdr's prompt --wait matched a transient done before the hook's dialog."""
    monkeypatch.setenv("HC_SETTLE_S", "2")
    monkeypatch.setenv("HC_HOOK_GRACE_S", "1")
    a = actx.h.agents["x1"]

    def cb(agent, text):
        out = Path(OUT_RE.search(text).group(1))

        def later():
            time.sleep(0.3)
            q = ua.create(actx.project, "permission", "x1", "X", tool_name="Bash", tool_input={"command": "ls"},
                          hook_pid=os.getpid())
            a.status = "blocked"
            until(lambda: load_run(actx.project, "r0001")["status"] == QUESTION)
            ua.answer(actx, ORCH_X, q["id"], "allow")
            q = ua.load(actx.project, q["id"])
            q["status"] = "resolved"
            ua.save(actx.project, q)
            out.write_text("결과\n")
            a.status = "working"
        in_thread(later)
        return "done"  # the transient state herdr matched
    actx.h.on_prompt["x1"] = cb
    rec = execute(actx, create_run(actx.project, actx.cfg.flows["one"]))
    assert rec["status"] == "done", rec


def test_really_finished_without_out_still_fails(actx, monkeypatch):
    monkeypatch.setenv("HC_SETTLE_S", "0.3")
    actx.h.on_prompt["x1"] = lambda a, t: "done"
    t0 = time.time()
    rec = execute(actx, create_run(actx.project, actx.cfg.flows["one"]))
    assert rec["status"] == "failed" and "결과 파일" in rec["steps"][0]["error"]
    assert time.time() - t0 < 3
