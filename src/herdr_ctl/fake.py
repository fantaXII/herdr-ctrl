"""In-memory stand-in for :class:`herdr_ctl.herdr.Herdr` used by unit tests.

Shell panes really execute commands (``bash -c``) so shell steps, nonces and
exit codes are exercised for real. Agent behaviour is scripted through
``on_prompt`` callbacks.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from .herdr import HerdrError


@dataclass
class FPane:
    pane_id: str
    tab_id: str
    workspace_id: str
    cwd: str
    env: dict = field(default_factory=dict)
    label: str | None = None
    output: str = ""
    zoomed: bool = False
    busy: bool = False  # a non-agent foreground command is running


@dataclass
class FAgent:
    name: str
    kind: str
    pane_id: str
    args: list
    status: str = "idle"
    prompts: list = field(default_factory=list)
    keys: list = field(default_factory=list)


class FakeHerdr:
    def __init__(self, session: str | None = "fake"):
        self.session = session
        self.sessions: dict[str, str] = {}
        self.workspaces: dict[str, dict] = {}
        self.tabs: dict[str, dict] = {}
        self.panes: dict[str, FPane] = {}
        self.agents: dict[str, FAgent] = {}
        self.notifications: list[tuple[str, str]] = []
        self.calls: list[tuple] = []
        self.worktrees: dict[str, dict] = {}
        # name -> callable(agent, text) -> new status ("done"/"blocked"/...) or None
        self.on_prompt: dict[str, Callable] = {}
        self.default_on_prompt: Callable | None = None
        self.start_status: dict[str, str] = {}  # name -> status after start (e.g. blocked)
        self.background_patterns = ["herdr_ctl.cli watch"]
        self._n = 0

    def _id(self) -> int:
        self._n += 1
        return self._n

    # sessions -------------------------------------------------------------
    def session_list(self):
        return [{"name": n, "status": s, "directory": "", "socket": ""} for n, s in self.sessions.items()]

    def session_running(self, name):
        return self.sessions.get(name) == "running"

    def start_server(self, name, log_path=None, wait_s=10.0):
        self.calls.append(("start_server", name))
        self.sessions[name] = "running"

    def session_stop(self, name):
        self.sessions[name] = "stopped"

    def session_delete(self, name):
        self.sessions.pop(name, None)

    # workspaces -------------------------------------------------------------
    def workspace_list(self):
        return [dict(w) for w in self.workspaces.values()]

    def workspace_create(self, cwd, label, env=None):
        wid = f"w{self._id()}"
        self.workspaces[wid] = {"workspace_id": wid, "label": label, "cwd": cwd}
        tab = self._new_tab(wid, "1", cwd, env)
        pane = self._new_pane(tab["tab_id"], wid, cwd, env)
        self.calls.append(("workspace_create", label))
        return {"workspace": dict(self.workspaces[wid]), "tab": dict(tab), "root_pane": self._pane_dict(pane)}

    def workspace_close(self, workspace_id):
        for t in [t for t in self.tabs.values() if t["workspace_id"] == workspace_id]:
            self.tab_close(t["tab_id"])
        self.workspaces.pop(workspace_id, None)

    # tabs -----------------------------------------------------------------
    def _new_tab(self, wid, label, cwd, env):
        tid = f"{wid}:t{self._id()}"
        self.tabs[tid] = {"tab_id": tid, "workspace_id": wid, "label": label}
        return self.tabs[tid]

    def tab_list(self, workspace_id=None):
        return [dict(t) for t in self.tabs.values() if not workspace_id or t["workspace_id"] == workspace_id]

    def tab_create(self, workspace_id, label, cwd, env=None):
        if workspace_id not in self.workspaces:
            raise HerdrError("not_found", workspace_id)
        tab = self._new_tab(workspace_id, label, cwd, env)
        pane = self._new_pane(tab["tab_id"], workspace_id, cwd, env)
        self.calls.append(("tab_create", label))
        return {"tab": dict(tab), "root_pane": self._pane_dict(pane)}

    def tab_rename(self, tab_id, label):
        self.tabs[tab_id]["label"] = label

    def tab_close(self, tab_id):
        for p in [p for p in self.panes.values() if p.tab_id == tab_id]:
            self.pane_close(p.pane_id)
        self.tabs.pop(tab_id, None)

    # panes ----------------------------------------------------------------
    def _new_pane(self, tid, wid, cwd, env):
        pid = f"{wid}:p{self._id()}"
        self.panes[pid] = FPane(pid, tid, wid, cwd, dict(env or {}))
        return self.panes[pid]

    def _pane_dict(self, p: FPane):
        return {"pane_id": p.pane_id, "tab_id": p.tab_id, "workspace_id": p.workspace_id,
                "cwd": p.cwd, "label": p.label}

    def _pane(self, pane_id) -> FPane:
        if pane_id not in self.panes:
            raise HerdrError("pane_not_found", pane_id)
        return self.panes[pane_id]

    def pane_list(self, workspace_id=None):
        return [self._pane_dict(p) for p in self.panes.values()
                if not workspace_id or p.workspace_id == workspace_id]

    def pane_split(self, pane_id, direction, cwd, ratio=None, env=None):
        src = self._pane(pane_id)
        p = self._new_pane(src.tab_id, src.workspace_id, cwd, env)
        self.calls.append(("pane_split", pane_id, direction))
        return self._pane_dict(p)

    def pane_rename(self, pane_id, label):
        self._pane(pane_id).label = label

    def pane_run(self, pane_id, command):
        p = self._pane(pane_id)
        self.calls.append(("pane_run", pane_id, command))
        p.output += f"$ {command}\n"
        if any(pat in command for pat in self.background_patterns):
            p.busy = True  # long-running (e.g. hctrl watch): recorded, not executed
            return
        proc = subprocess.run(["bash", "-c", command], cwd=p.cwd, capture_output=True, text=True)
        p.output += proc.stdout + proc.stderr

    def pane_wait_output(self, pane_id, regex, timeout_ms=None):
        p = self._pane(pane_id)
        for line in p.output.splitlines():
            if re.search(regex, line):
                return {"matched_line": line, "pane_id": pane_id}
        raise HerdrError("timeout", f"no match for {regex}")

    def pane_read(self, pane_id, lines=80, source="recent-unwrapped"):
        return "\n".join(self._pane(pane_id).output.splitlines()[-lines:])

    def pane_zoom(self, pane_id):
        p = self._pane(pane_id)
        p.zoomed = not p.zoomed
        return {"zoom": {"changed": True}}

    def pane_close(self, pane_id):
        self.panes.pop(pane_id, None)
        for n in [n for n, a in self.agents.items() if a.pane_id == pane_id]:
            self.agents.pop(n)

    def pane_send_keys(self, pane_id, *keys):
        self.calls.append(("pane_send_keys", pane_id, keys))

    def pane_shell_idle(self, pane_id):
        p = self._pane(pane_id)
        return not p.busy and not any(a.pane_id == pane_id for a in self.agents.values())

    # agents ---------------------------------------------------------------
    def _agent(self, target) -> FAgent:
        if target in self.agents:
            return self.agents[target]
        for a in self.agents.values():
            if a.pane_id == target:
                return a
        raise HerdrError("agent_not_found", target)

    def agent_list(self):
        return [{"name": a.name, "agent": a.kind, "agent_status": a.status, "pane_id": a.pane_id}
                for a in self.agents.values()]

    def agent_get(self, target):
        a = self._agent(target)
        return {"name": a.name, "agent": a.kind, "agent_status": a.status, "pane_id": a.pane_id}

    def agent_start(self, name, kind, pane_id, args=None, timeout_ms=60000):
        p = self._pane(pane_id)
        if not self.pane_shell_idle(pane_id):
            raise HerdrError("pane_not_available", pane_id)
        if name in self.agents:
            raise HerdrError("name_in_use", name)
        status = self.start_status.get(name, "idle")
        self.agents[name] = FAgent(name, kind, p.pane_id, list(args or []), status)
        self.calls.append(("agent_start", name, kind, pane_id, tuple(args or [])))
        if status == "blocked":
            raise HerdrError("agent_not_ready", f"agent {name} is blocked during startup")
        return {"agent": self.agent_get(name)}

    def agent_prompt(self, target, text, wait=True, timeout_ms=None):
        a = self._agent(target)
        if a.status == "blocked":
            raise HerdrError("agent_blocked", target)
        a.prompts.append(text)
        self.calls.append(("agent_prompt", a.name, text))
        cb = self.on_prompt.get(a.name) or self.default_on_prompt
        new = cb(a, text) if cb else "done"
        a.status = new or "done"
        if a.status == "timeout":
            a.status = "working"
            raise HerdrError("timeout", "prompt wait timed out")
        if a.status == "stalled":
            a.status = "idle"
            raise HerdrError("agent_prompt_stalled", "no observed working state")
        return {"agent": self.agent_get(a.name)}

    def agent_wait(self, target, until=None, timeout_ms=None):
        a = self._agent(target)
        wanted = until or ["idle", "done", "blocked"]
        if a.status in wanted:
            return {"agent": self.agent_get(a.name)}
        if a.status == "working":
            a.status = "idle"  # simulate the turn finishing
            if a.status in wanted:
                return {"agent": self.agent_get(a.name)}
        raise HerdrError("timeout", f"{target} still {a.status}")

    def agent_rename(self, target, name):
        a = self._agent(target)
        if name in self.agents and self.agents[name] is not a:
            raise HerdrError("name_in_use", name)
        self.agents.pop(a.name)
        a.name = name
        self.agents[name] = a
        self.calls.append(("agent_rename", target, name))

    def agent_send_keys(self, target, *keys):
        a = self._agent(target)
        a.keys.extend(keys)
        if "esc" in keys or "ctrl+c" in keys:
            if a.status == "working":
                a.status = "idle"

    def agent_read(self, target, lines=60):
        return ""

    def kill_agent(self, name):
        """Test helper: the agent process exits; pane stays at a shell prompt."""
        self.agents.pop(name, None)

    # misc ---------------------------------------------------------------
    integrations = {"claude": "current", "opencode": "current"}

    def integration_status(self):
        return dict(self.integrations)

    def notify(self, title, body="", sound="request"):
        self.notifications.append((title, body))

    def worktree_create(self, workspace_id, cwd, branch, base, label):
        res = self.workspace_create(cwd + f"/../wt-{branch.replace('/', '-')}", label)
        wid = res["workspace"]["workspace_id"]
        self.worktrees[wid] = {"branch": branch, "base": base}
        return res

    def worktree_remove(self, workspace_id, force=False):
        self.worktrees.pop(workspace_id, None)
        self.workspace_close(workspace_id)
