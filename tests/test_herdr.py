import json
import subprocess

import pytest

from herdr_ctl.herdr import Herdr, HerdrError, clean_env


def runner_returning(stdout, rc=0, stderr="", seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    return run


def test_parses_result():
    out = json.dumps({"id": "x", "result": {"type": "workspace_list", "workspaces": [{"workspace_id": "w1"}]}})
    h = Herdr(binary="herdr", runner=runner_returning(out))
    assert h.workspace_list() == [{"workspace_id": "w1"}]


def test_error_json_raises_with_code():
    out = json.dumps({"id": "x", "error": {"code": "agent_not_ready", "message": "blocked"}})
    h = Herdr(binary="herdr", runner=runner_returning(out, rc=1))
    with pytest.raises(HerdrError) as e:
        h.agent_start("a", "claude", "w1:p1")
    assert e.value.code == "agent_not_ready"


def test_nonjson_failure_raises():
    h = Herdr(binary="herdr", runner=runner_returning("", rc=2, stderr="boom"))
    with pytest.raises(HerdrError) as e:
        h.pane_list()
    assert e.value.code == "cli_failed" and "boom" in e.value.message


def test_empty_output_success():
    h = Herdr(binary="herdr", runner=runner_returning(""))
    h.pane_run("w1:p1", "echo hi")  # `pane run` prints nothing on success


def test_session_strips_herdr_env(monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/other.sock")
    monkeypatch.setenv("HERDR_ENV", "1")
    seen = []
    h = Herdr(session="projA", binary="herdr", runner=runner_returning('{"result":{}}', seen=seen))
    h.call("pane", "list")
    cmd, kw = seen[0]
    assert cmd[:3] == ["herdr", "--session", "projA"]
    assert "HERDR_SOCKET_PATH" not in kw["env"] and "HERDR_ENV" not in kw["env"]


def test_no_session_keeps_env(monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/cur.sock")
    seen = []
    h = Herdr(binary="herdr", runner=runner_returning('{"result":{}}', seen=seen))
    h.call("pane", "list")
    assert seen[0][1]["env"]["HERDR_SOCKET_PATH"] == "/tmp/cur.sock"


def test_clean_env():
    env = clean_env({"HERDR_PANE_ID": "a", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_session_list_parse():
    table = ("name                 status   directory        socket\n"
             "default              running  /a               /a/herdr.sock\n"
             "projA                stopped  /b               /b/herdr.sock\n")
    h = Herdr(binary="herdr", runner=runner_returning(table))
    rows = h.session_list()
    assert rows[1] == {"name": "projA", "status": "stopped", "directory": "/b", "socket": "/b/herdr.sock"}
    assert h.session_running("default") and not h.session_running("projA")


def test_shell_idle_detection():
    info = {"result": {"process_info": {"foreground_process_group_id": 10, "shell_pid": 10,
                                         "foreground_processes": [{"pid": 10, "name": "bash"}]}}}
    h = Herdr(binary="herdr", runner=runner_returning(json.dumps(info)))
    assert h.pane_shell_idle("w1:p1")
    info["result"]["process_info"]["foreground_process_group_id"] = 99
    h = Herdr(binary="herdr", runner=runner_returning(json.dumps(info)))
    assert not h.pane_shell_idle("w1:p1")


def test_error_json_on_stderr():
    err = json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no activity"}})
    h = Herdr(binary="herdr", runner=runner_returning("", rc=1, stderr=err))
    with pytest.raises(HerdrError) as e:
        h.agent_prompt("a1", "hi")
    assert e.value.code == "agent_prompt_stalled"


def test_server_env_strips_claude_markers(monkeypatch):
    from herdr_ctl.herdr import server_env
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    env = server_env()
    assert "CLAUDE_CODE_CHILD_SESSION" not in env and "CLAUDECODE" not in env and "HERDR_PANE_ID" not in env
