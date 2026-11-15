"""Thin wrapper over the `herdr` CLI.

Every control command of herdr returns one JSON document on stdout:
``{"id": ..., "result": {...}}`` on success or ``{"id": ..., "error": {"code", "message"}}``.
This module turns those into Python dicts / HerdrError and is the only place
that spawns the herdr binary, so tests can swap it for ``FakeHerdr``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable

# Variables herdr injects into managed panes. They point at the *current*
# session's socket, so they must be removed when we target another session.
HERDR_ENV_VARS = (
    "HERDR_ENV",
    "HERDR_SOCKET_PATH",
    "HERDR_PANE_ID",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
)

AGENT_KINDS = (
    "pi claude codex gemini cursor devin agy cline omp mastracode opencode copilot "
    "kimi kiro droid amp grok hermes kilo qodercli qwen letta maki muse"
).split()

READY_STATES = ("idle", "done")


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in HERDR_ENV_VARS:
        env.pop(key, None)
    return env


def server_env() -> dict[str, str]:
    """Env for a new herdr server: no herdr pane context and no parent Claude session markers
    (otherwise agents started inside it inherit e.g. CLAUDE_CODE_CHILD_SESSION)."""
    env = clean_env()
    for key in list(env):
        if key == "CLAUDECODE" or key.startswith("CLAUDE_CODE_"):
            env.pop(key)
    return env


def _parse(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some commands print a trailing hint line; take the last JSON line.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


class Herdr:
    """Real herdr client. ``session=None`` means "the inherited/default session"."""

    def __init__(self, session: str | None = None, binary: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self.session = session
        self.binary = binary or os.environ.get("HC_HERDR_BIN") or shutil.which("herdr") or "herdr"
        self._run = runner or subprocess.run

    # ------------------------------------------------------------------ core
    def _base(self) -> list[str]:
        cmd = [self.binary]
        if self.session:
            cmd += ["--session", self.session]
        return cmd

    def _env(self) -> dict[str, str]:
        return clean_env() if self.session else dict(os.environ)

    def call(self, *args: str, timeout: float | None = None) -> dict[str, Any]:
        cmd = self._base() + [str(a) for a in args]
        try:
            proc = self._run(cmd, capture_output=True, text=True, env=self._env(), timeout=timeout)
        except FileNotFoundError as exc:
            raise HerdrError("herdr_not_found", str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrError("timeout", f"herdr {' '.join(args)} timed out") from exc
        data = _parse(proc.stdout)
        if data is None and proc.returncode != 0:
            data = _parse(proc.stderr or "")  # herdr reports errors as JSON on stderr
        if isinstance(data, dict) and "error" in data:
            err = data["error"] or {}
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise HerdrError("cli_failed", msg or f"exit {proc.returncode}")
        if isinstance(data, dict):
            return data.get("result", data)
        return {}

    def text(self, *args: str) -> str:
        cmd = [self.binary] + [str(a) for a in args]
        proc = self._run(cmd, capture_output=True, text=True, env=clean_env())
        if proc.returncode != 0:
            raise HerdrError("cli_failed", (proc.stderr or proc.stdout).strip())
        return proc.stdout

    # -------------------------------------------------------------- sessions
    def session_list(self) -> list[dict[str, str]]:
        out = self.text("session", "list")
        rows = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows.append({"name": parts[0], "status": parts[1],
                             "directory": parts[2] if len(parts) > 2 else "",
                             "socket": parts[3] if len(parts) > 3 else ""})
        return rows

    def session_running(self, name: str) -> bool:
        return any(r["name"] == name and r["status"] == "running" for r in self.session_list())

    def start_server(self, name: str, log_path: str | None = None, wait_s: float = 10.0) -> None:
        """Start a named session's server without a TUI (verified on herdr 0.9.1)."""
        log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        subprocess.Popen([self.binary, "--session", name, "server"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=server_env(), start_new_session=True)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.session_running(name):
                return
            time.sleep(0.3)
        raise HerdrError("server_start_failed", f"session {name} did not start within {wait_s}s")

    def session_stop(self, name: str) -> None:
        self.text("session", "stop", name)

    def session_delete(self, name: str) -> None:
        self.text("session", "delete", name)

    # ------------------------------------------------------------ workspaces
    def workspace_list(self) -> list[dict]:
        return self.call("workspace", "list").get("workspaces", [])

    def workspace_create(self, cwd: str, label: str, env: dict[str, str] | None = None) -> dict:
        args = ["workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        return self.call(*args)

    def workspace_close(self, workspace_id: str) -> None:
        self.call("workspace", "close", workspace_id)

    # ------------------------------------------------------------------ tabs
    def tab_list(self, workspace_id: str | None = None) -> list[dict]:
        args = ["tab", "list"] + (["--workspace", workspace_id] if workspace_id else [])
        return self.call(*args).get("tabs", [])

    def tab_create(self, workspace_id: str, label: str, cwd: str,
                   env: dict[str, str] | None = None) -> dict:
        args = ["tab", "create", "--workspace", workspace_id, "--label", label, "--cwd", cwd, "--no-focus"]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        return self.call(*args)

    def tab_close(self, tab_id: str) -> None:
        self.call("tab", "close", tab_id)

    def tab_rename(self, tab_id: str, label: str) -> None:
        self.call("tab", "rename", tab_id, label)

    # ----------------------------------------------------------------- panes
    def pane_list(self, workspace_id: str | None = None) -> list[dict]:
        args = ["pane", "list"] + (["--workspace", workspace_id] if workspace_id else [])
        return self.call(*args).get("panes", [])

    def pane_split(self, pane_id: str, direction: str, cwd: str, ratio: float | None = None,
                   env: dict[str, str] | None = None) -> dict:
        args = ["pane", "split", pane_id, "--direction", direction, "--cwd", cwd, "--no-focus"]
        if ratio is not None:
            args += ["--ratio", f"{ratio:.3f}"]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        return self.call(*args)["pane"]

    def pane_rename(self, pane_id: str, label: str) -> None:
        self.call("pane", "rename", pane_id, label)

    def pane_run(self, pane_id: str, command: str) -> None:
        self.call("pane", "run", pane_id, command)

    def pane_wait_output(self, pane_id: str, regex: str, timeout_ms: int | None = None) -> dict:
        args = ["pane", "wait-output", pane_id, "--regex", regex, "--source", "recent-unwrapped"]
        if timeout_ms is not None:
            args += ["--timeout", str(int(timeout_ms))]
        return self.call(*args)

    def pane_read(self, pane_id: str, lines: int = 80, source: str = "recent-unwrapped") -> str:
        res = self.call("pane", "read", pane_id, "--source", source, "--lines", str(lines))
        return res.get("read", res).get("text", "")

    def pane_zoom(self, pane_id: str) -> dict:
        return self.call("pane", "zoom", pane_id, "--toggle")

    def pane_close(self, pane_id: str) -> None:
        self.call("pane", "close", pane_id)

    def pane_send_keys(self, pane_id: str, *keys: str) -> None:
        self.call("pane", "send-keys", pane_id, *keys)

    def pane_shell_idle(self, pane_id: str) -> bool:
        """True when the pane's shell itself is in the foreground (no command/agent running)."""
        info = self.call("pane", "process-info", "--pane", pane_id).get("process_info", {})
        procs = info.get("foreground_processes") or []
        return info.get("foreground_process_group_id") == info.get("shell_pid") or not procs

    # ---------------------------------------------------------------- agents
    def agent_list(self) -> list[dict]:
        return self.call("agent", "list").get("agents", [])

    def agent_get(self, target: str) -> dict:
        return self.call("agent", "get", target)["agent"]

    def agent_start(self, name: str, kind: str, pane_id: str, args: list[str] | None = None,
                    timeout_ms: int = 60000) -> dict:
        cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(timeout_ms)]
        if args:
            cmd += ["--"] + list(args)
        return self.call(*cmd)

    def agent_prompt(self, target: str, text: str, wait: bool = True,
                     timeout_ms: int | None = None) -> dict:
        args = ["agent", "prompt", target, text]
        if wait:
            args.append("--wait")
        if timeout_ms is not None:
            args += ["--timeout", str(int(timeout_ms))]
        return self.call(*args)

    def agent_wait(self, target: str, until: list[str] | None = None,
                   timeout_ms: int | None = None) -> dict:
        args = ["agent", "wait", target]
        for state in until or []:
            args += ["--until", state]
        if timeout_ms is not None:
            args += ["--timeout", str(int(timeout_ms))]
        return self.call(*args)

    def agent_send_keys(self, target: str, *keys: str) -> None:
        self.call("agent", "send-keys", target, *keys)

    def agent_read(self, target: str, lines: int = 60) -> str:
        res = self.call("agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines))
        return res.get("read", res).get("text", "")

    # ---------------------------------------------------------- misc / other
    def integration_status(self) -> dict[str, str]:
        """kind -> status word (e.g. 'current', 'not'), from `herdr integration status`."""
        out = self.text("integration", "status")
        res = {}
        for line in out.splitlines():
            if ":" in line:
                kind, rest = line.split(":", 1)
                res[kind.split()[0].strip()] = rest.strip().split(" ")[0]
        return res

    def notify(self, title: str, body: str = "", sound: str = "request") -> None:
        try:
            self.call("notification", "show", title, "--body", body, "--sound", sound)
        except HerdrError:
            pass  # notifications are best-effort

    def worktree_create(self, workspace_id: str, cwd: str, branch: str, base: str | None,
                        label: str) -> dict:
        # herdr accepts --workspace OR --cwd (mutually exclusive); the repo cwd is explicit.
        args = ["worktree", "create", "--cwd", cwd, "--branch", branch, "--label", label, "--no-focus"]
        if base:
            args += ["--base", base]
        return self.call(*args)

    def worktree_remove(self, workspace_id: str, force: bool = False) -> None:
        args = ["worktree", "remove", "--workspace", workspace_id]
        if force:
            args.append("--force")
        self.call(*args)


def agent_status(h: Herdr, name: str) -> str | None:
    """Live status of a named agent or None when no such live agent exists."""
    for a in h.agent_list():
        if a.get("name") == name:
            return a.get("agent_status")
    return None
