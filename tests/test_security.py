"""Audit fixes: actor identity, budget/lock concurrency, runner robustness."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from herdr_ctl import cli
from herdr_ctl import orchestrator as O
from herdr_ctl import unattended as ua
from herdr_ctl.herdr import HerdrError
from herdr_ctl.orchestrator import Actor, LimitExceeded, ScopeError, current_actor, enforce, start_ask
from herdr_ctl.runner import QUESTION, create_run, execute, load_run, save_run, wait
from herdr_ctl.state import acquire, lock_holder, lock_path, release, LockBusy
from herdr_ctl.up import up
from conftest import OUT_RE, make_ctx, make_project, writes_out

TWO = """
session: {s}
tabs:
  X:
    orchestrator: {{ kind: claude }}
    unattended: approve
    agents:
      x1: {{ kind: claude }}
    shells:
      xs: {{}}
flows:
  one:
    steps:
      - agent: x1
        prompt: "작업 {{out}}"
"""


@pytest.fixture
def two(tmp_path):
    out = []
    for name in ("pa", "pb"):
        ctx = make_ctx(make_project(tmp_path, TWO.format(s=name), name=name))
        up(ctx, start_watch=False)
        for a in ctx.h.agents.values():
            a.status = "idle"
        out.append(ctx)
    return out


def test_registry_lives_outside_projects(two):
    a, _ = two
    reg = json.loads(O.registry_path().read_text())
    assert reg and all(a.project.root not in O.registry_path().parents for _ in [0])
    assert any(v["member"] == "x1" and v["root"] == str(a.project.root.resolve()) for v in reg.values())


def test_actor_from_registry_not_state(two, monkeypatch):
    a, _ = two
    pane = a.state.member("x1")["pane_id"]
    monkeypatch.setenv("HERDR_PANE_ID", pane)
    assert current_actor(a).kind == "agent"
    # a work agent rewriting state.json (auto-approved edit inside its cwd) gains nothing
    a.state["members"]["x1"]["pane_id"] = "w9:p99"
    a.state.save()
    a2 = make_ctx(a.project, a.h)
    assert current_actor(a2).kind == "agent"
    monkeypatch.setenv("HERDR_PANE_ID", a.state.member("xs")["pane_id"])
    assert current_actor(a).kind == "user"  # shell pane = a person
    monkeypatch.setenv("HERDR_PANE_ID", "w77:p77")
    assert current_actor(a).kind == "user"  # unmanaged pane


def test_other_projects_pane_is_foreign(two, monkeypatch):
    a, b = two
    monkeypatch.setenv("HERDR_SESSION", "pa")
    monkeypatch.setenv("HERDR_PANE_ID", a.state.member("x-orch")["pane_id"])
    actor = current_actor(b)  # orchestrator of A running `hc --project B ...`
    assert actor.kind == "foreign"
    with pytest.raises(ScopeError):
        enforce(b, actor, ["x1"], "hi")
    with pytest.raises(ScopeError):
        O.guard(b, actor, "stop", ["x1"])
    q = ua.create(b.project, "text", "x1", "X", question="?")
    with pytest.raises(ScopeError):
        ua.answer(b, actor, q["id"], "답")


def test_session_disambiguates_same_pane_ids(two, monkeypatch):
    a, b = two
    pid = a.state.member("x-orch")["pane_id"]
    monkeypatch.setenv("HERDR_PANE_ID", pid)
    monkeypatch.setenv("HERDR_SESSION", "pb")  # same pane id in another session
    hit = O.registered_member("pb", pid)
    assert current_actor(b).kind == ("orch" if hit and hit["member"] == "x-orch" else "user")


def test_ancestor_env_cannot_be_dropped(two, monkeypatch):
    a, _ = two
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    pane = a.state.member("x1")["pane_id"]
    monkeypatch.setattr(O, "_ancestor_envs", lambda max_depth=30: iter([{"PATH": "/bin"},
                                                                          {"HERDR_PANE_ID": pane, "HERDR_SESSION": "pa"}]))
    assert O.pane_identity() == ("pa", pane)
    assert current_actor(a).kind == "agent"  # `env -u HERDR_PANE_ID hc ...` does not make it the user


def test_real_ancestry_reader_runs():
    from conftest import REAL_ANCESTOR_ENVS
    envs = list(REAL_ANCESTOR_ENVS(3))
    assert envs and isinstance(envs[0], dict)


def test_down_and_reup_refresh_registry(two):
    a, _ = two
    from herdr_ctl.up import down
    down(a)
    reg = json.loads(O.registry_path().read_text())
    assert not any(v["root"] == str(a.project.root.resolve()) for v in reg.values())


def test_init_refused_in_agent_pane(two, monkeypatch, capsys):
    a, _ = two
    monkeypatch.setenv("HERDR_SESSION", "pa")
    monkeypatch.setenv("HERDR_PANE_ID", a.state.member("x-orch")["pane_id"])
    assert cli.main(["--project", str(a.project.root), "init"]) == 1
    assert "사용자 전용" in capsys.readouterr().err


def test_cli_bad_duration_is_message_not_traceback(two, monkeypatch, capsys):
    a, _ = two
    monkeypatch.setattr(cli, "make_herdr", lambda s: a.h)
    assert cli.main(["--project", str(a.project.root), "send", "x1", "hi", "--timeout", "abc"]) == 1
    assert "시간 형식" in capsys.readouterr().err


# --------------------------------------------------------------- concurrency
def test_budget_is_atomic_under_parallel_calls(two):
    a, _ = two
    tab = a.cfg.tab("X")
    tab.limits["max_steps_per_ask"] = 5
    start_ask(a, tab)
    orch = Actor("orch", "x-orch", "X")
    ok, refused = [], []

    def go(i):
        try:
            enforce(a, orch, ["x1"], f"지시 {i}")
            ok.append(i)
        except LimitExceeded:
            refused.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 5 and len(refused) == 7


def test_lock_survives_pid_reuse(two):
    a, _ = two
    p = lock_path(a.project, "x1")
    p.parent.mkdir(parents=True, exist_ok=True)
    # stale lock whose pid now belongs to an unrelated live process (ourselves, other start time)
    p.write_text(json.dumps({"owner": "r0001", "pid": os.getpid(), "start": "1"}))
    assert lock_holder(a.project, "x1") is None
    acquire(a.project, "x1", "r0002")
    assert lock_holder(a.project, "x1")["owner"] == "r0002"
    with pytest.raises(LockBusy):
        acquire(a.project, "x1", "r0003")
    release(a.project, "x1", "r0002")


def test_lock_reclaim_is_exclusive(two):
    a, _ = two
    p = lock_path(a.project, "x1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"owner": "dead", "pid": 999999999}))
    won, lost = [], []

    def go(i):
        try:
            acquire(a.project, "x1", f"r{i}")
            won.append(i)
        except LockBusy:
            lost.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(won) == 1 and len(lost) == 7


def test_answer_at_hook_deadline_is_still_delivered(two, monkeypatch):
    a, _ = two
    a.cfg.tab("X").policy["answer_timeout"] = 0.3
    a.h.agents["x1"].status = "blocked"
    calls = {"n": 0}

    def sleep(_):
        calls["n"] += 1
        if calls["n"] == 3:  # answer lands during the last sleep before the deadline
            time.sleep(0.35)
            q = ua.pending(a.project)[0]
            ua.answer(a, Actor("user"), q["id"], "allow")
    out = ua.run_hook(a, "x1", json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                           "tool_input": {"command": "ls"}}), sleep=sleep)
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "allow"


# ------------------------------------------------------------------- runner
@pytest.fixture
def rx(two, monkeypatch):
    monkeypatch.setenv("HC_QUESTION_POLL_S", "0.02")
    a, _ = two
    for ag in a.h.agents.values():
        ag.prompts.clear()
    return a


def test_unexpected_error_marks_run_failed(rx):
    def boom(agent, text):
        raise HerdrError("cli_failed", "socket gone")
    rx.h.on_prompt["x1"] = boom
    rec = create_run(rx.project, rx.cfg.flows["one"])
    rec_id = rec["id"]
    out = execute(rx, rec)
    assert out["status"] == "failed"  # unknown herdr error from prompt: handled as a failed step
    rx.h.agent_list = lambda: (_ for _ in ()).throw(HerdrError("cli_failed", "socket gone"))
    with pytest.raises(HerdrError):
        execute(rx, create_run(rx.project, rx.cfg.flows["one"]))
    last = sorted(rx.project.runs_dir.iterdir())[-1].name
    rec = load_run(rx.project, last)
    assert rec["status"] == "failed" and "예기치 않은 오류" in rec["steps"][0]["error"]
    assert rec_id != last


def test_wait_does_not_overwrite_finished_run(rx, monkeypatch):
    rec = create_run(rx.project, rx.cfg.flows["one"])
    rec.update(status="running", pid=999999999, pid_start="1")
    save_run(rx.project, rec)
    import herdr_ctl.runner as R
    real = R.load_run
    n = {"i": 0}

    def racing(project, rid):
        n["i"] += 1
        r = real(project, rid)
        if n["i"] == 1:  # worker saves `done` right after our first read
            done = dict(r, status="done")
            save_run(project, done)
        return r
    monkeypatch.setattr(R, "load_run", racing)
    assert wait(rx.project, rec["id"], timeout_s=1)["status"] == "done"


def test_question_cap_creates_no_orphan(rx):
    rx.h.on_prompt["x1"] = lambda a, t: (Path(OUT_RE.search(t).group(1)).write_text("## QUESTION\n또?\n"), "done")[1]

    def answer_all():
        for _ in range(5):
            end = time.time() + 5
            while time.time() < end and not ua.pending(rx.project):
                time.sleep(0.01)
            q = ua.pending(rx.project)[0]
            ua.answer(rx, Actor("user"), q["id"], "응")
    threading.Thread(target=answer_all, daemon=True).start()
    rec = execute(rx, create_run(rx.project, rx.cfg.flows["one"]))
    assert rec["status"] == "failed"
    assert len(ua.all_questions(rx.project)) == 5 and not ua.pending(rx.project)


def test_failed_delivery_keeps_answer_for_resume(rx):
    calls = {"n": 0}

    def cb(agent, text):
        calls["n"] += 1
        out = Path(OUT_RE.search(text).group(1))
        if calls["n"] == 1:
            out.write_text("## QUESTION\nDB?\n")
            return "done"
        if calls["n"] == 2:
            return "stalled"  # delivery of the answer did not observably start
        out.write_text("결과 " + text.splitlines()[1] + "\n")
        return "done"
    rx.h.on_prompt["x1"] = cb
    rec = create_run(rx.project, rx.cfg.flows["one"])
    def answer_when_asked():
        end = time.time() + 10
        while time.time() < end and not ua.pending(rx.project):
            time.sleep(0.01)
        ua.answer(rx, Actor("user"), "q0001", "postgres")
    threading.Thread(target=answer_when_asked, daemon=True).start()
    rec = execute(rx, rec)
    assert rec["status"] == "blocked", rec["steps"][0]["error"]
    assert "hc resume" in rec["steps"][0]["error"]
    assert ua.load(rx.project, "q0001")["status"] == "answered"  # not lost
    from herdr_ctl.runner import prepare_resume
    rec = execute(rx, prepare_resume(rx.project, rec["id"]), resume=True)  # the real resume path
    assert rec["status"] == "done", rec
    assert "postgres" in Path(rec["steps"][0]["out"]).read_text()


def test_second_worker_on_same_run_refused(rx):
    rec = create_run(rx.project, rx.cfg.flows["one"])
    acquire(rx.project, f"run-{rec['id']}", "other-worker")
    with pytest.raises(RuntimeError, match="다른 프로세스"):
        execute(rx, rec)
    assert load_run(rx.project, rec["id"])["status"] == "pending"  # untouched
