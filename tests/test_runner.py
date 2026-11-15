from pathlib import Path

import pytest

from herdr_ctl.runner import (create_job, create_run, execute, list_runs, load_run, prepare_resume,
                              stop, summary)
from herdr_ctl.state import acquire, lock_holder, release
from herdr_ctl.up import up
from conftest import writes_out


@pytest.fixture
def ready(ctx):
    up(ctx, start_watch=False)
    for a in ctx.h.agents.values():
        a.status = "idle"
        a.prompts.clear()
    ctx.h.default_on_prompt = writes_out()
    return ctx


def run_flow(ctx, name="build-review-x", **kw):
    rec = create_run(ctx.project, ctx.cfg.flows[name])
    return execute(ctx, rec, **kw)


def test_full_flow_passes_files(ready):
    rec = run_flow(ready)
    assert rec["status"] == "done", summary(rec)
    s = rec["steps"]
    assert [x["status"] for x in s] == ["ok", "ok", "failed", "ok"]
    assert s[2]["exit_code"] == 3  # PIPESTATUS gives the command's code, not tee's
    assert "building" in Path(s[2]["out"]).read_text()
    # {prev} of step 2 is step 1's output; step 4 sees the build log
    p2 = ready.h.agents["x2"].prompts[-1]
    assert s[0]["out"] in p2 and "참고 자료" in p2
    p4 = ready.h.agents["x1"].prompts[-1]
    assert s[2]["out"] in p4
    # out paths are absolute
    assert all(Path(x["out"]).is_absolute() for x in s)
    assert load_run(ready.project, rec["id"])["status"] == "done"


def test_shell_nonce_not_fooled_by_old_output(ready):
    r1 = run_flow(ready, "shell-only")
    r2 = run_flow(ready, "shell-only")
    assert r1["status"] == r2["status"] == "done"
    pane = ready.h.panes[ready.state.member("xbuild")["pane_id"]]
    assert pane.output.count("__HC_DONE_") >= 4  # command echo + marker per run
    markers = [c[2] for c in ready.h.calls if c[0] == "pane_run" and "__HC_DONE_" in c[2]]
    assert markers[0] != markers[1] and r1["id"] in markers[0] and r2["id"] in markers[1]


def test_missing_out_fails_step(ready):
    ready.h.on_prompt["x1"] = lambda a, t: "done"  # finishes without writing {out}
    rec = run_flow(ready)
    assert rec["status"] == "failed"
    assert rec["steps"][0]["status"] == "failed" and "결과 파일" in rec["steps"][0]["error"]
    assert rec["steps"][1]["status"] == "pending"
    assert ready.h.notifications


def test_max_retries_reprompts_with_reason(ready, project):
    flow = ready.cfg.flows["build-review-x"]
    flow.steps[0].max_retries = 1
    calls = {"n": 0}

    def flaky(agent, text):
        calls["n"] += 1
        if calls["n"] == 2:
            return writes_out()(agent, text)
        return "done"
    ready.h.on_prompt["x1"] = flaky
    rec = run_flow(ready)
    assert rec["steps"][0]["status"] == "ok" and rec["steps"][0]["attempts"] == 2
    assert "이전 시도가 실패" in ready.h.agents["x1"].prompts[1]


def test_timeout_is_not_resent(ready):
    ready.cfg.flows["build-review-x"].steps[0].max_retries = 3
    ready.h.on_prompt["x1"] = lambda a, t: "timeout"
    rec = run_flow(ready)
    assert rec["status"] == "timeout"
    assert len(ready.h.agents["x1"].prompts) == 1  # no automatic re-send


def test_blocked_then_resume_without_resend(ready):
    out_holder = {}

    def blocks(agent, text):
        from conftest import OUT_RE
        out_holder["out"] = OUT_RE.search(text).group(1)
        return "blocked"
    ready.h.on_prompt["x1"] = blocks
    rec = run_flow(ready)
    assert rec["status"] == "blocked" and rec["steps"][0]["status"] == "blocked"
    # user approves in the pane; the agent finishes its turn and writes the file
    Path(out_holder["out"]).write_text("done after approval")
    ready.h.agents["x1"].status = "idle"
    ready.h.on_prompt.pop("x1")
    rec = prepare_resume(ready.project, rec["id"])
    rec = execute(ready, rec, resume=True)
    assert rec["status"] == "done"
    assert len([p for p in ready.h.agents["x1"].prompts if "기능 X" in p]) == 1


def test_resume_without_output_requires_resend(ready):
    ready.h.on_prompt["x1"] = lambda a, t: "blocked"
    rec = run_flow(ready)
    ready.h.agents["x1"].status = "idle"
    ready.h.on_prompt.pop("x1")
    rec = execute(ready, prepare_resume(ready.project, rec["id"]), resume=True)
    assert rec["status"] == "failed" and "--resend" in rec["steps"][0]["error"]
    rec = execute(ready, prepare_resume(ready.project, rec["id"]), resume=True, resend=True)
    assert rec["status"] == "done"


def test_on_fail_stop(ready):
    ready.cfg.flows["build-review-x"].steps[2].on_fail = "stop"
    rec = run_flow(ready)
    assert rec["status"] == "failed" and rec["steps"][3]["status"] == "pending"


def test_agent_not_running(ready):
    ready.h.kill_agent("x1")
    rec = run_flow(ready)
    assert rec["status"] == "failed" and "hc up" in rec["steps"][0]["error"]


def test_busy_agent_is_waited_for(ready):
    ready.h.agents["x1"].status = "working"
    rec = run_flow(ready)
    assert rec["status"] == "done"


def test_lock_prevents_concurrent_use(ready):
    acquire(ready.project, "x1", "someone-else")
    try:
        ready.cfg.flows["build-review-x"].steps[0].max_retries = 3
        rec = run_flow(ready)
        # recorded in the run (not a crash leaving a 'running' record), and not retried
        assert rec["status"] == "failed" and "잠금" in rec["steps"][0]["error"]
        assert rec["steps"][0]["attempts"] == 1
        assert ready.h.agents["x1"].prompts == []
        assert load_run(ready.project, rec["id"])["status"] == "failed"
    finally:
        release(ready.project, "x1")
    assert lock_holder(ready.project, "x1") is None


def test_locks_released_after_run(ready):
    run_flow(ready)
    assert all(lock_holder(ready.project, n) is None for n in ("x1", "x2", "xbuild"))


def test_stale_lock_is_reclaimed(ready):
    lock = ready.project.var_dir / "locks" / "x1.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"owner": "dead", "pid": 999999999}')
    assert run_flow(ready)["status"] == "done"


def test_stop_flag_stops_run(ready):
    rec = create_run(ready.project, ready.cfg.flows["build-review-x"])

    def stopper(agent, text):
        (ready.project.runs_dir / rec["id"] / "stop").touch()
        return writes_out()(agent, text)
    ready.h.on_prompt["x1"] = stopper
    rec = execute(ready, rec)
    assert rec["status"] == "stopped"
    assert rec["steps"][1]["status"] in ("pending", "stopped")


def test_stop_command_on_pending_record(ready):
    rec = create_run(ready.project, ready.cfg.flows["build-review-x"])
    rec = stop(ready, rec["id"])
    assert rec["status"] == "stopped"


def test_job_send(ready):
    rec = create_job(ready.project, "x2", "테스트 하나 추가", None)
    rec = execute(ready, rec)
    assert rec["status"] == "done" and rec["id"].startswith("j")
    assert "테스트 하나 추가" in ready.h.agents["x2"].prompts[-1]
    assert [r["id"] for r in list_runs(ready.project)] == [rec["id"]]


def test_stalled_prompt_verified_not_resent(ready, monkeypatch):
    import herdr_ctl.runner as runner_mod
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    # stalled but the agent actually did the work (wrote {out})
    def stalls_after_writing(agent, text):
        writes_out()(agent, text)
        return "stalled"
    ready.h.on_prompt["x1"] = stalls_after_writing
    ready.cfg.flows["build-review-x"].steps[0].max_retries = 2
    rec = run_flow(ready)
    assert rec["steps"][0]["status"] == "ok"
    # stalled and nothing written → failed with guidance (retry policy still applies)
    ready.h.on_prompt["x1"] = lambda a, t: "stalled"
    ready.cfg.flows["build-review-x"].steps[0].max_retries = 0
    rec = run_flow(ready)
    assert rec["status"] == "failed" and "stalled" in rec["steps"][0]["error"]


def test_ready_grace_after_fresh_start(ready, monkeypatch):
    import time as _t
    import herdr_ctl.runner as runner_mod
    slept = []
    monkeypatch.setenv("HC_READY_GRACE_S", "8")
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: slept.append(s))
    ready.state["members"]["x2"]["started_at"] = _t.time()
    rec = create_job(ready.project, "x2", "hi", None)
    execute(ready, rec)
    assert slept and 0 < slept[0] <= runner_mod.READY_GRACE_S


# ---------------------------------------------------- review regressions
def test_resume_shell_step_not_fooled_by_old_marker(ready):
    """H1: a resumed shell step must not match the failed attempt's completion marker."""
    flow = ready.cfg.flows["shell-only"]
    flag = ready.project.root / "ok.flag"
    flow.steps[0].run = f"test -f {flag}"
    rec = run_flow(ready, "shell-only")
    assert rec["status"] == "failed" and rec["steps"][0]["exit_code"] == 1
    flag.write_text("fixed")
    rec = execute(ready, prepare_resume(ready.project, rec["id"]), resume=True)
    assert rec["status"] == "done", summary(rec)
    assert rec["steps"][0]["exit_code"] == 0


def test_shell_step_runs_in_bash_with_pipefail(ready):
    """M3: exit code comes from the command even through tee, independent of the pane shell."""
    run_flow(ready, "shell-only")
    line = [c[2] for c in ready.h.calls if c[0] == "pane_run" and "__HC_DONE_" in c[2]][-1]
    assert line.startswith("bash -c ") and "pipefail" in line and "PIPESTATUS" not in line


def test_stalled_failure_is_not_retried(ready, monkeypatch):
    """H3: even with max_retries, a stalled prompt is never re-sent automatically."""
    import herdr_ctl.runner as runner_mod
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    ready.cfg.flows["build-review-x"].steps[0].max_retries = 3
    ready.h.on_prompt["x1"] = lambda a, t: "stalled"
    rec = run_flow(ready)
    assert rec["status"] == "failed" and rec["steps"][0]["attempts"] == 1
    assert len(ready.h.agents["x1"].prompts) == 1


def test_unknown_herdr_error_is_not_retried(ready):
    from herdr_ctl.herdr import HerdrError
    ready.cfg.flows["build-review-x"].steps[0].max_retries = 3

    def boom(agent, text):
        raise HerdrError("io_error", "socket closed")
    ready.h.on_prompt["x1"] = boom
    rec = run_flow(ready)
    assert rec["steps"][0]["attempts"] == 1 and len(ready.h.agents["x1"].prompts) == 1


def test_empty_lock_file_reclaimed_when_old(ready):
    import os, time as _t
    lock = ready.project.var_dir / "locks" / "x1.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    assert lock_holder(ready.project, "x1") == {"owner": "?", "pid": "?"}  # fresh: may be mid-write
    old = _t.time() - 60
    os.utime(lock, (old, old))
    assert lock_holder(ready.project, "x1") is None and not lock.exists()


def test_resume_refused_while_old_worker_alive(ready):
    """Re-review HIGH: resume must not start while a previous worker process still runs."""
    import subprocess as sp
    rec = create_run(ready.project, ready.cfg.flows["shell-only"])
    proc = sp.Popen(["sleep", "30"])
    try:
        rec["pid"], rec["status"] = proc.pid, "stopped"
        from herdr_ctl.runner import save_run
        save_run(ready.project, rec)
        with pytest.raises(RuntimeError, match="살아 있습니다"):
            prepare_resume(ready.project, rec["id"])
    finally:
        proc.kill()
        proc.wait()
    assert prepare_resume(ready.project, rec["id"])["id"] == rec["id"]


def test_stop_kills_unresponsive_worker(ready, monkeypatch):
    import subprocess as sp
    from herdr_ctl.runner import save_run
    monkeypatch.setenv("HC_STOP_GRACE_S", "0.5")
    proc = sp.Popen(["bash", "-c", "trap '' TERM; echo ready; sleep 30"], stdout=sp.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"  # trap installed before we signal it
    try:
        rec = create_run(ready.project, ready.cfg.flows["shell-only"])
        rec["pid"], rec["status"] = proc.pid, "running"
        save_run(ready.project, rec)
        rec = stop(ready, rec["id"])
        proc.wait(timeout=5)
        assert rec["status"] == "stopped" and proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()


def test_shell_marker_survives_ctrl_c(tmp_path):
    """The INT trap prints a completion marker when hc stop sends ctrl+c to the step."""
    import shlex, signal, subprocess as sp, time as _t
    marker = "__HC_DONE_t_0_1_"
    script = (f"trap 'echo {marker}$((128+2))__' INT; set -o pipefail; "
              f"{{ sleep 5 ; }} 2>&1 | tee {shlex.quote(str(tmp_path / 'o'))}; echo {marker}$?__")
    p = sp.Popen(["bash", "-c", script], stdout=sp.PIPE, text=True, start_new_session=True)
    _t.sleep(0.5)
    import os
    os.killpg(p.pid, signal.SIGINT)
    out, _ = p.communicate(timeout=5)
    assert f"{marker}130__" in out


def test_pid_reuse_is_not_mistaken_for_worker(ready):
    """A live pid whose start time differs from the recorded one is not our worker."""
    import os
    from herdr_ctl.runner import save_run
    from herdr_ctl.state import proc_start, same_process_alive
    me = os.getpid()
    assert same_process_alive(me, proc_start(me))
    assert not same_process_alive(me, "1")
    rec = create_run(ready.project, ready.cfg.flows["shell-only"])
    rec["pid"], rec["pid_start"], rec["status"] = os.getppid(), "1", "stopped"
    save_run(ready.project, rec)
    assert prepare_resume(ready.project, rec["id"])["id"] == rec["id"]
