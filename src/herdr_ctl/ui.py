"""`hctrl ui`: local browser UI for projects and config.yaml.

Security model (the UI can rewrite agent permissions, so it is user-only):
- binds 127.0.0.1 only; requests must carry Host 127.0.0.1:<port> / localhost:<port> (DNS rebinding)
- a one-time link token (?t=) is exchanged for an HttpOnly SameSite=Strict cookie; the token dies on use
- every other request needs that cookie; writes need a JSON body and a same-origin Origin header
- the CLI refuses to start it from an hctrl-managed agent/orchestrator pane (cli.cmd_ui)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from importlib import resources
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import roles as roles_mod
from .config import (DEFAULT_AGENT_NAMES, DEFAULT_ROLES, DEFAULT_SHELL_NAME, DEFAULT_TAB_BASE, agents_cache_path, check_data, check_file,
                     load_yaml, parse_agent_list)
from .herdr import AGENT_KINDS, Herdr, HerdrError
from .project import Project, _expand, find_project, load_registry, register
from .state import LockBusy, State, digest

COOKIE = "hctrl_ui"
MAX_BODY = 1 << 20
TOKEN_TTL_S = 120.0
PING_S = 30.0  # an open page pings this often (ui_page.html)
IDLE_S = 180.0  # no request for this long: the page is closed (background tabs ping ~once a minute)
INTEGRATIONS_TTL_S = 15.0
OPENCODE_TIMEOUT_S = 60
AGENTS_TIMEOUT_S = 90      # `opencode agent list` prints thousands of lines and takes 12-20 s
MODELS_TTL_S = 24 * 3600   # the opencode model list cached on disk (refresh button in the page)
MODELS_WAIT_S = 0.5        # a request waits this long for a list still loading (the page polls after)
CLAUDE_MODELS = ["opus", "sonnet", "haiku"]
# importlib.resources: works from a package dir and from inside hctrl.pyz alike
PAGE = resources.files("herdr_ctl") / "ui_page.html"


def session_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name) or "project"


def default_config(project: Project) -> dict:
    """Starting point for a project without config.yaml (same defaults as `hctrl init`)."""
    return {"session": session_name(project.name),
            "tabs": {f"{DEFAULT_TAB_BASE}1": {
                "orchestrator": {"kind": "claude", "model": "opus"},
                "agents": {DEFAULT_AGENT_NAMES[0]: {"kind": "claude", "model": "sonnet", "role": DEFAULT_ROLES[0]},
                           DEFAULT_AGENT_NAMES[1]: {"kind": "opencode", "role": DEFAULT_ROLES[1]}},
                "shells": {DEFAULT_SHELL_NAME: {}}}}}


def opencode_models(run: Callable[..., Any] = subprocess.run) -> dict[str, list[str]]:
    """`opencode models` → {provider: [provider/model, ...]} (empty when opencode is missing).

    It can take 10-20 s (longer right after opencode plugins change), hence the generous timeout;
    callers run it in the background."""
    try:
        out = run(["opencode", "models"], capture_output=True, text=True, timeout=OPENCODE_TIMEOUT_S).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    grouped: dict[str, list[str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if re.fullmatch(r"[\w.-]+/\S+", line):
            grouped.setdefault(line.split("/", 1)[0], []).append(line)
    return grouped


def opencode_agents(run: Callable[..., Any] = subprocess.run) -> list[str]:
    """`opencode agent list` → selectable primary agent names (empty when opencode is missing/fails)."""
    try:
        out = run(["opencode", "agent", "list"], capture_output=True, text=True, timeout=AGENTS_TIMEOUT_S).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_agent_list(out or "")


# ------------------------------------------------------------------ browser
def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def browser_command(url: str) -> list[str] | None:
    """How to open `url`: HC_BROWSER > WSL (wslview / rundll32) > desktop default. None = print only.

    Not explorer.exe: given a URL with a query string (?t=...&p=...) it opens File Explorer instead."""
    custom = os.environ.get("HC_BROWSER")
    if custom is not None:
        return None if custom in ("", "none") else shlex.split(custom) + [url]
    if is_wsl():
        if shutil.which("wslview"):
            return ["wslview", url]
        if shutil.which("rundll32.exe"):  # Windows default browser, no shell parsing of & in the URL
            return ["rundll32.exe", "url.dll,FileProtocolHandler", url]
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return ["<webbrowser>", url]
    return None


def open_browser(url: str) -> bool:
    cmd = browser_command(url)
    if cmd is None:
        return False
    if cmd[0] == "<webbrowser>":
        return webbrowser.open(url)
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------- server
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class UIServer:
    """mode "launcher": project list + open (ends the server, the CLI opens the session in the same
    terminal). mode "session": settings only (we are already inside a herdr session)."""

    def __init__(self, mode: str = "launcher", cwd: Path | None = None, start: Project | None = None,
                 port: int = 0, herdr: Callable[[str | None], Any] | None = None,
                 which: Callable[[str], str | None] | None = None,
                 models: Callable[[], dict[str, list[str]]] | None = None, secret: str | None = None,
                 agents: Callable[[], list[str]] | None = None):
        self.mode = mode
        self.cwd = (cwd or Path.cwd()).resolve()
        self.start_project = start
        self.herdr = herdr or (lambda session=None: Herdr(session=session))
        self.which = which or shutil.which
        self._models = models or opencode_models
        # opencode model list: loaded in the background (idle → loading → ready | failed)
        self._oc: dict[str, list[str]] = {}
        self._oc_status = "idle"
        self._oc_done = threading.Event()
        # an injected model loader (tests) without an agent loader must not spawn the real opencode
        self._agents_fn = agents or (opencode_agents if models is None else (lambda: []))
        self._oc_agents: list[str] = []
        self._oc_agents_status = "idle"
        self._integrations: dict | None = None
        self._integrations_at = 0.0  # monotonic time of the last `herdr integration status`
        self._tokens: dict[str, float] = {}  # one-time link token -> expiry (monotonic)
        self.last_seen = time.monotonic()  # last link or API call: an open page pings (see in_use)
        self.restart_requested = False  # session mode: the page asked the _hc watcher to restart
        # cookie value; a restarted _hc watcher passes the old one (with the old port) so an open page
        # keeps working across the restart
        self.secret = secret or secrets.token_urlsafe(32)
        self.page = PAGE.read_bytes()  # read once: `hctrl update` may replace the .pyz while we serve
        self.choice: Project | None = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"ui": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.hosts = (f"127.0.0.1:{self.port}", f"localhost:{self.port}")  # accepted Host / Origin
        self._thread: threading.Thread | None = None

    # lifecycle
    def start(self) -> "UIServer":
        self.refresh_models()  # warm up: `opencode models` is slow
        self.refresh_agents()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def new_link(self, project: str | None = None) -> str:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._tokens = {t: exp for t, exp in self._tokens.items() if exp > now}
            self._tokens[token] = now + TOKEN_TTL_S
        self.last_seen = now
        url = f"http://127.0.0.1:{self.port}/?t={token}"
        return url + (f"&p={quote(project, safe='')}" if project else "")

    def in_use(self) -> bool:
        """A page was open recently (it pings every PING_S while open, even in a background tab)."""
        return time.monotonic() - self.last_seen < IDLE_S

    def redeem(self, token: str) -> bool:
        with self._lock:
            expiry = self._tokens.pop(token, None)
        return expiry is not None and time.monotonic() < expiry

    def wait(self, timeout: float | None = None) -> Project | None:
        self.done.wait(timeout)
        return self.choice

    # projects
    def projects(self) -> dict[str, Project]:
        found = {name: Project(name, _expand(path))
                 for name, path in load_registry().items()}
        if self.start_project and self.start_project.name not in found:
            found[self.start_project.name] = self.start_project
        return found

    def project(self, name: str) -> Project:
        p = self.projects().get(name)
        if p is None:
            raise ApiError(404, f"프로젝트 '{name}'이 없습니다")
        return p

    def running_sessions(self) -> set[str]:
        try:
            return {r["name"] for r in self.herdr(None).session_list() if r["status"] == "running"}
        except (HerdrError, OSError):
            return set()

    def integrations(self, fresh: bool = False) -> dict | None:
        # short-lived cache: checks run on every edit, but an `herdr integration install` done while
        # the page is open must show up on the next load (the _hc server lives as long as the session)
        stale = time.monotonic() - self._integrations_at >= INTEGRATIONS_TTL_S
        if self._integrations is None or fresh or stale:
            self._integrations_at = time.monotonic()
            try:
                self._integrations = self.herdr(None).integration_status()
            except (HerdrError, OSError):
                self._integrations = {}
        return self._integrations or None

    def check(self, project: Project, data: Any) -> list[dict]:
        _, issues = check_data(data, root=project.root, which=self.which, integrations=self.integrations(),
                                primary_agents=self._oc_agents or None)
        return [{"level": i.level, "path": i.path, "message": i.message} for i in issues]

    def _check_fresh(self, project: Project, data: Any) -> list[dict]:
        self.integrations(fresh=True)  # page (re)load: re-read herdr integrations
        return self.check(project, data)

    # ---- opencode model list
    @staticmethod
    def models_cache_path() -> Path:
        from .project import state_home
        return state_home() / "opencode-models.json"

    def refresh_models(self, force: bool = False) -> None:
        """Start loading the opencode model list unless a fresh one is cached (disk) or loading."""
        with self._lock:
            if self._oc_status == "loading":
                return
            if not force and self._oc_status == "ready":
                return
            if not self._oc:
                try:
                    d = json.loads(self.models_cache_path().read_text())
                    if isinstance(d.get("providers"), dict) and d["providers"]:
                        self._oc = d["providers"]  # stale or not, better than nothing while loading
                        if not force and time.time() - float(d.get("at") or 0) < MODELS_TTL_S:
                            self._oc_status = "ready"
                            return
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
            self._oc_status = "loading"
            self._oc_done.clear()
        threading.Thread(target=self._load_models, daemon=True).start()

    def _load_models(self) -> None:
        try:
            got = self._models()
        except Exception:
            got = {}
        with self._lock:
            if got:
                self._oc, self._oc_status = got, "ready"
                try:
                    p = self.models_cache_path()
                    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"at": time.time(), "providers": got}))
                    os.replace(tmp, p)
                except OSError:
                    pass
            else:  # failed: keep an older list if there is one; never cache an empty result
                self._oc_status = "ready" if self._oc else "failed"
            self._oc_done.set()

    # ---- opencode primary agent list (same pattern as the model list, own cache file)
    @staticmethod
    def agents_cache_path() -> Path:
        return agents_cache_path()

    def refresh_agents(self, force: bool = False) -> None:
        with self._lock:
            if self._oc_agents_status == "loading":
                return
            if not force and self._oc_agents_status == "ready":
                return
            if not self._oc_agents:
                try:
                    d = json.loads(self.agents_cache_path().read_text())
                    if isinstance(d.get("agents"), list) and d["agents"]:
                        self._oc_agents = [str(a) for a in d["agents"]]
                        if not force and time.time() - float(d.get("at") or 0) < MODELS_TTL_S:
                            self._oc_agents_status = "ready"
                            return
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
            self._oc_agents_status = "loading"
        threading.Thread(target=self._load_agents, daemon=True).start()

    def _load_agents(self) -> None:
        try:
            got = self._agents_fn()
        except Exception:
            got = []
        with self._lock:
            if got:
                self._oc_agents, self._oc_agents_status = got, "ready"
                try:
                    p = self.agents_cache_path()
                    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"at": time.time(), "agents": got}))
                    os.replace(tmp, p)
                except OSError:
                    pass
            else:
                self._oc_agents_status = "ready" if self._oc_agents else "failed"

    def models(self, wait: float = MODELS_WAIT_S) -> dict:
        if self._oc_status == "idle":  # first use; a failure is retried only on request (↻ / 다시 시도)
            self.refresh_models()
        if self._oc_status == "loading" and wait:
            self._oc_done.wait(wait)
        if self._oc_agents_status == "idle":
            self.refresh_agents()
        return {"kinds": list(AGENT_KINDS), "claude": CLAUDE_MODELS, "opencode": self._oc,
                "opencode_status": self._oc_status, "opencode_agents": list(self._oc_agents),
                "opencode_agents_status": self._oc_agents_status}

    def templates_api(self, method: str, rest: list[str], body: Any) -> Any:
        """Team templates (templates.py): list / get / save from a saved tab / rename / delete."""
        from . import templates as T
        try:
            if rest == [] and method == "GET":
                return {"templates": T.list_templates()}
            if rest == [] and method == "POST":
                project = self.project(str(body.get("project") or ""))
                name, tab = body.get("name"), str(body.get("tab") or "")
                T.path_for(name)  # validate the name first
                try:
                    path = T.save(name, T.build(project, tab), {"project": project.name, "tab": tab},
                                  overwrite=bool(body.get("overwrite")),
                                  description=str(body.get("description") or ""))
                except FileExistsError:
                    raise ApiError(409, f"템플릿 '{name}'이 이미 있습니다") from None
                return {"name": name, "path": str(path), "templates": T.list_templates()}
            if len(rest) == 1 and method == "GET":
                return T.load(rest[0])
            if len(rest) == 2 and rest[1] == "delete" and method == "POST":
                T.delete(rest[0])
                return {"templates": T.list_templates()}
            if len(rest) == 2 and rest[1] == "rename" and method == "POST":
                T.rename(rest[0], body.get("name"))
                return {"templates": T.list_templates()}
        except T.TemplateError as e:
            raise ApiError(400, str(e)) from e
        raise ApiError(404, "없음")

    def update_action(self, rest: list[str]) -> dict:
        """POST /api/update (install the latest release) and /api/update/check (ask GitHub now)."""
        from . import update
        if rest == ["check"]:
            ok = update.check_now() is not None
            if not ok:
                raise ApiError(502, "GitHub에 확인하지 못했습니다 (네트워크를 확인하세요)")
            return {"status": update.status()}
        if rest:
            raise ApiError(404, "없음")
        lines: list[str] = []
        try:
            update.run_update(out=lines.append)
        except update.UpdateError as e:
            raise ApiError(400, str(e)) from e
        except OSError as e:  # urllib errors are OSErrors
            raise ApiError(502, f"다운로드 실패: {e}") from e
        return {"lines": lines, "status": update.status()}

    # -------------------------------------------------------------- API
    def api(self, method: str, path: str, body: Any) -> Any:
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]  # drop "api"
        if parts == ["state"] and method == "GET":
            here = find_project(self.cwd)
            from . import __version__, update
            return {"mode": self.mode, "cwd": str(self.cwd), "cwd_project": here.name if here else None,
                    "start": self.start_project.name if self.start_project else None,
                    "version": __version__, "update": update.status()}
        if parts == ["ping"] and method == "GET":
            from . import __version__, update
            # the page reloads when the server got newer; "update" lets an open page show a version that
            # was installed after it loaded ("installed" state: restart applies it)
            return {"ok": True, "version": __version__, "update": update.status()}
        if parts and parts[0] == "update" and method == "POST":
            return self.update_action(parts[1:])
        if parts and parts[0] == "templates":
            return self.templates_api(method, parts[1:], body)
        if parts == ["restart"] and method == "POST":
            if self.mode != "session":
                raise ApiError(400, "세션 밖에서는 hctrl을 다시 실행하면 새 버전이 적용됩니다")
            self.restart_requested = True  # the _hc watcher picks this up within one tick
            return {"ok": True}
        if parts == ["projects"]:
            if method == "GET":
                return self.list_projects()
            if method == "POST":
                return self.add_project(body)
        if parts == ["models"] and method == "GET":
            return self.models()
        if parts == ["models", "refresh"] and method == "POST":
            self.refresh_models(force=True)
            self.refresh_agents(force=True)
            return self.models()
        if parts == ["open"] and method == "POST":
            return self.open(body)
        if len(parts) >= 2 and parts[0] == "project":
            project = self.project(parts[1])
            rest = parts[2:]
            if rest == [] and method == "GET":
                return self.get_config(project)
            if rest == ["check"] and method == "POST":
                return {"issues": self.check(project, body.get("data"))}
            if rest == ["config"] and method == "PUT":
                return self.save_config(project, body)
            if rest == ["draft"] and method == "PUT":
                return self.save_draft(project, body)
            if rest == ["draft", "discard"] and method == "POST":
                return self.discard_draft(project)
            if rest == ["changes"] and method == "GET":
                return self.get_changes(project)
            if rest == ["apply"] and method == "POST":
                return self.apply_changes(project, body)
            if len(rest) == 2 and rest[0] == "role":
                if method == "GET":
                    return self.get_role(project, rest[1])
                if method == "PUT":
                    return self.put_role(project, rest[1], body)
        raise ApiError(404, "없는 API입니다")

    def list_projects(self) -> list[dict]:
        running = self.running_sessions()
        rows = []
        for name, p in sorted(self.projects().items()):
            session, has_config = None, p.config_path.exists()
            if has_config:
                data, _ = load_yaml(p.config_path)
                session = data.get("session") if isinstance(data, dict) else None
            rows.append({"name": name, "root": str(p.root), "has_config": has_config,
                         "exists": p.root.is_dir(), "running": bool(session and session in running)})
        return rows

    def add_project(self, body: dict) -> dict:
        path = _expand(str(body.get("path") or ""))
        if not path.is_dir():
            raise ApiError(400, f"폴더가 없습니다: {path}")
        name = str(body.get("name") or path.name).strip()
        if not re.fullmatch(r"[\w.-]{1,64}", name):
            raise ApiError(400, "이름은 영문/숫자/._- 64자 이하")
        existing = load_registry().get(name)
        if existing and _expand(existing) != path:
            raise ApiError(409, f"'{name}'은 이미 다른 폴더({existing})에 등록되어 있습니다")
        register(name, path)
        return {"name": name, "root": str(path)}

    def get_config(self, project: Project) -> dict:
        text = project.config_path.read_text() if project.config_path.exists() else None
        if text is None:
            data, parse_error = default_config(project), None
        else:
            data, issues = load_yaml(project.config_path)
            parse_error = issues[0].message if issues else None
        running = False
        if isinstance(data, dict) and data.get("session"):
            running = data["session"] in self.running_sessions()
        return {"name": project.name, "root": str(project.root), "exists": text is not None,
                "version": digest(text) if text is not None else None, "data": data,
                "parse_error": parse_error, "running": running,
                "issues": self._check_fresh(project, data) if data is not None else [],
                "draft": self.load_draft(project), "roles": self.role_summary(project),
                "git": (project.root / ".git").exists()}  # worktree needs a git repository

    def role_summary(self, project: Project) -> dict:
        """{member: lines in its role detail (prompt)} for the saved config's agents and orchestrators."""
        cfg, _ = check_file(project.config_path, which=self.which)
        if cfg is None:
            return {}
        out = {}
        for t in cfg.tabs:
            for m in t.members:
                if m.is_agent:
                    try:
                        detail = roles_mod.effective_role(project, m)[1]
                    except (OSError, RuntimeError):
                        detail = ""
                    out[m.name] = len([ln for ln in detail.splitlines() if ln.strip()])
        return out

    # ---- draft: an unfinished config kept outside the project (config.yaml stays valid)
    @staticmethod
    def draft_path(project: Project) -> Path:
        # secure_dir: outside every agent cwd and outside git; `hctrl up` never reads it
        return project.secure_dir / "config.draft.json"

    def load_draft(self, project: Project) -> dict | None:
        try:
            d = json.loads(self.draft_path(project).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
            return None
        return {"data": d["data"], "base": d.get("base"), "saved_at": d.get("saved_at")}

    def save_draft(self, project: Project, body: dict) -> dict:
        data = body.get("data")
        if not isinstance(data, dict):
            raise ApiError(400, "data는 객체여야 합니다")
        path = self.draft_path(project)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rec = {"data": data, "base": body.get("version"), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
        return {"saved_at": rec["saved_at"], "issues": self.check(project, data)}

    def discard_draft(self, project: Project) -> dict:
        self.draft_path(project).unlink(missing_ok=True)
        return {"ok": True}

    def save_config(self, project: Project, body: dict) -> dict:
        from .init_cmd import write_config
        data = body.get("data")
        current = project.config_path.read_text() if project.config_path.exists() else None
        if (digest(current) if current is not None else None) != body.get("version"):
            raise ApiError(409, "그 사이 config.yaml이 바뀌었습니다. 새로 불러온 뒤 다시 저장하세요")
        issues = self.check(project, data)
        if any(i["level"] == "error" for i in issues):
            raise ApiError(422, "오류가 있어 저장하지 않았습니다")
        renames = body.get("renames") or {}
        if not isinstance(renames, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                    for k, v in renames.items()):
            raise ApiError(400, "renames는 {이전 탭 ID: 새 탭 ID} 객체여야 합니다")
        copies = body.get("copies") or {}
        if not isinstance(copies, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in copies.items()):
            raise ApiError(400, "copies는 {새 멤버: 원본 멤버} 객체여야 합니다")
        old_cfg = check_file(project.config_path, which=self.which)[0] if current is not None else None
        role_texts = body.get("role_texts") or {}
        if not isinstance(role_texts, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                      for k, v in role_texts.items()):
            raise ApiError(400, "role_texts는 {멤버: 역할 상세 전문} 객체여야 합니다")
        tabs = data.get("tabs") if isinstance(data, dict) else None
        renames = {o: n for o, n in renames.items() if o != n and isinstance(tabs, dict) and n in tabs}
        moved: list[str] = []
        if renames and current is not None:
            from .tabrename import RenameError, rename_tabs
            old_data, _ = load_yaml(project.config_path)
            session = old_data.get("session") if isinstance(old_data, dict) else None
            live = bool(session) and session in self.running_sessions()
            try:
                moved = rename_tabs(project, renames, lambda: write_config(project, data),
                                    self.herdr(session) if live else None, live)
            except RenameError as e:
                raise ApiError(409, str(e)) from e
        else:
            write_config(project, data)
        self.discard_draft(project)  # the draft is now the saved config
        renamed = moved + self.sync_live_titles(project)
        copied = self.copy_roles(project, old_cfg, copies) + self.write_role_texts(project, role_texts)
        return {"version": digest(project.config_path.read_text()), "issues": issues, "renamed": renamed,
                "copied": copied, "roles": self.role_summary(project)}

    def write_role_texts(self, project: Project, texts: dict[str, str]) -> list[str]:
        """Team template: create each new member's role file from the template text (never overwrite)."""
        if not texts:
            return []
        cfg, _ = check_file(project.config_path, which=self.which)
        done = []
        for name, text in texts.items():
            m = cfg.member(name) if cfg else None
            if m is None or m.type == "shell":
                continue
            try:
                if roles_mod.write_role_text(project, name, text, roles_mod.role_file(project, m),
                                             reason="팀 템플릿"):
                    done.append(name)
            except (OSError, RuntimeError):
                continue
        return done

    def copy_roles(self, project: Project, old_cfg, copies: dict[str, str]) -> list[str]:
        """Tab copy: give each new member its source's role detail (as its own file, with history).
        Sources come from the config as it was before this save; a member that already has a role
        file keeps it."""
        if not copies or old_cfg is None:
            return []
        new_cfg, _ = check_file(project.config_path, which=self.which)
        done = []
        for new, src in copies.items():
            m, s = (new_cfg.member(new) if new_cfg else None), old_cfg.member(src)
            if m is None or s is None or m.type == "shell" or s.type == "shell" or new == src:
                continue
            try:
                if not roles_mod.role_file(project, s).exists() or roles_mod.role_file(project, m).exists():
                    continue
                one, detail = roles_mod.effective_role(project, s)
                roles_mod.write_role(project, m, one, detail, actor="user", reason=f"탭 복사: {src}")
            except (OSError, RuntimeError):  # unreadable source / role_file outside the project
                continue
            done.append(new)
        return done

    def sync_live_titles(self, project: Project) -> list[str]:
        """Running session: apply tab titles now (other changes go through `hctrl changes`)."""
        from .up import Ctx, sync_titles
        cfg, _ = check_file(project.config_path, which=self.which)
        if cfg is None or cfg.session not in self.running_sessions():
            return []
        try:
            return sync_titles(Ctx(project, cfg, self.herdr(cfg.session), State(project)))
        except HerdrError:
            return []

    # ---- applying saved changes to a running session (same steps as `hctrl changes`)
    def live_ctx(self, project: Project):
        from .up import Ctx
        cfg, _ = check_file(project.config_path, which=self.which)
        if cfg is None or cfg.session not in self.running_sessions():
            return None
        return Ctx(project, cfg, self.herdr(cfg.session), State(project))

    def get_changes(self, project: Project) -> dict:
        from .watch import default_actions, detect
        ctx = self.live_ctx(project)
        if ctx is None:
            return {"running": False, "changes": []}
        return {"running": True, "changes": [{"name": c.name, "type": c.type, "detail": c.detail,
                                              "actions": default_actions(c)} for c in detect(ctx)]}

    def apply_changes(self, project: Project, body: dict) -> dict:
        from .watch import apply_change, default_actions, detect, save_pending
        ctx = self.live_ctx(project)
        if ctx is None:
            raise ApiError(409, "세션이 실행 중이 아니거나 저장된 설정에 오류가 있습니다")
        chosen = body.get("actions") or {}
        results, remaining = [], []
        for ch in detect(ctx):
            action = chosen.get(ch.name, "skip")
            if action not in default_actions(ch) or action == "skip":
                remaining.append(ch)
                continue
            try:
                results.append(apply_change(ctx, ch, action))
            except (RuntimeError, KeyError, HerdrError, LockBusy) as e:
                results.append(f"{ch.name}: ✘ {e}")
                remaining.append(ch)
        save_pending(project, remaining)
        return {"results": results, **self.get_changes(project)}

    def _member(self, project: Project, name: str):
        cfg, _ = check_file(project.config_path, which=self.which)
        if cfg is None:
            raise ApiError(409, "저장된 설정에 오류가 있어 역할을 편집할 수 없습니다 (먼저 저장하세요)")
        m = cfg.member(name)
        if m is None or m.type == "shell":
            raise ApiError(404, f"저장된 설정에 agent '{name}'이 없습니다 (먼저 저장하세요)")
        return m

    def get_role(self, project: Project, name: str) -> dict:
        m = self._member(project, name)
        one, detail = roles_mod.effective_role(project, m)
        cfg, _ = check_file(project.config_path, which=self.which)
        return {"one": one, "detail": detail, "type": m.type, "auto": roles_mod.auto_rules(cfg, m)}

    def put_role(self, project: Project, name: str, body: dict) -> dict:
        m = self._member(project, name)
        one, detail = body.get("one"), body.get("detail")
        if not isinstance(one, str) or not isinstance(detail, str):
            raise ApiError(400, "one, detail은 문자열이어야 합니다")
        roles_mod.write_role(project, m, one, detail, actor="user", reason="hctrl ui")
        return self.get_role(project, name)

    def open(self, body: dict) -> dict:
        if self.mode != "launcher":
            raise ApiError(409, "이미 herdr 세션 안입니다. 다른 프로젝트는 일반 터미널에서 hctrl로 여세요")
        project = self.project(str(body.get("name") or ""))
        cfg, issues = check_file(project.config_path, which=self.which)
        if cfg is None:
            raise ApiError(409, "설정이 없거나 오류가 있어 열 수 없습니다: "
                           + "; ".join(i.message for i in issues if i.level == "error"))
        self.choice = project
        self.done.set()
        return {"ok": True, "message": "터미널로 돌아가세요. 세션을 엽니다."}


class _Handler(BaseHTTPRequestHandler):
    ui: UIServer
    server_version = "hctrl-ui"

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.ui.hosts

    def _authed(self) -> bool:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        got = jar[COOKIE].value if COOKIE in jar else ""
        return secrets.compare_digest(got, self.ui.secret)

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "허용되지 않은 Host"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path == "/" and "t" in q:
            if not self.ui.redeem(q["t"][0]):
                self._send(403, "링크가 만료되었습니다. 터미널에서 다시 여세요.".encode(), "text/plain; charset=utf-8")
                return
            target = "/" + (f"#/project/{quote(q['p'][0], safe='')}" if q.get("p") else "")
            cookie = f"{COOKIE}={self.ui.secret}; HttpOnly; SameSite=Strict; Path=/"
            self._send(303, b"", "text/plain", {"Location": target, "Set-Cookie": cookie})
            return
        if not self._authed():
            self._send(403, "인증되지 않았습니다. 터미널에 표시된 링크로 여세요.".encode(), "text/plain; charset=utf-8")
            return
        if url.path == "/":
            self._send(200, self.ui.page, "text/html; charset=utf-8")
            return
        self._dispatch("GET", url.path, None)

    def _write(self, method: str) -> None:
        if not self._guard():
            return
        if not self._authed():
            self._json(403, {"error": "인증되지 않았습니다"})
            return
        origin = self.headers.get("Origin")
        if origin and origin not in tuple("http://" + h for h in self.ui.hosts):
            self._json(403, {"error": "허용되지 않은 Origin"})
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "JSON만 받습니다"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self._json(413, {"error": "요청 길이가 잘못되었거나 너무 큽니다"})
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "JSON 형식 오류"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "JSON 객체여야 합니다"})
            return
        self._dispatch(method, urlsplit(self.path).path, body)

    def do_POST(self) -> None:
        self._write("POST")

    def do_PUT(self) -> None:
        self._write("PUT")

    def _dispatch(self, method: str, path: str, body: Any) -> None:
        self.ui.last_seen = time.monotonic()
        if not path.startswith("/api/"):
            self._json(404, {"error": "없음"})
            return
        try:
            data = json.dumps(self.ui.api(method, path, body), ensure_ascii=False, default=str)
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
            return
        except Exception as e:  # never a traceback in the user's terminal / _hc pane
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        self._send(200, data.encode(), "application/json; charset=utf-8")


# ------------------------------------------------------------------ runners
def announce(server: UIServer, project: str | None = None) -> str:
    """Open the one-time link. The token is printed only when no browser could be opened: terminal
    text can be read back by other processes (e.g. `herdr pane read` on the _hc pane)."""
    url = server.new_link(project)
    if open_browser(url):
        print(f"✔ hctrl UI: 브라우저를 열었습니다 (http://127.0.0.1:{server.port}, 끝내려면 Ctrl+C)", flush=True)
    else:
        print(f"✔ hctrl UI: {url}\n  브라우저에서 이 주소를 {int(TOKEN_TTL_S // 60)}분 안에 여세요 "
              "(한 번만 쓸 수 있습니다. 끝내려면 Ctrl+C)", flush=True)
    return url


def run_launcher(cwd: Path, start: Project | None = None) -> Project | None:
    """Serve the launcher until a project is chosen (returned) or Ctrl+C (None)."""
    server = UIServer("launcher", cwd=cwd, start=start).start()
    try:
        announce(server, start.name if start else None)
        return server.wait()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return None
    finally:
        server.stop()


def run_settings(project: Project | None) -> int:
    """Settings-only UI (inside a herdr session). Runs until Ctrl+C."""
    server = UIServer("session", cwd=project.root if project else None, start=project).start()
    try:
        announce(server, project.name if project else None)
        server.done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0
