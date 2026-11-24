"""Load and validate .herdr-flow/config.yaml (`hctrl check`)."""
from __future__ import annotations

import difflib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .herdr import AGENT_KINDS

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TAB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,24}$")  # "1", "2" are fine tab ids
DURATION_RE = re.compile(r"^(\d+)\s*(ms|s|m|h)?$")
MAX_PANES_PER_TAB = 4
CONTROL_TAB = "_hc"

TOP_KEYS = {"session", "tabs", "flows"}
TAB_KEYS = {"title", "cwd", "orchestrator", "role_update", "worktree", "limits", "agents", "shells", "closed",
            "unattended", "unattended_policy"}
AGENT_KEYS = {"kind", "model", "effort", "primary_agent", "role", "role_file", "permission", "args"}
ORCH_KEYS = {"kind", "model", "effort", "primary_agent", "goal", "permission", "args"}
SHELL_KEYS = {"cwd"}
WORKTREE_KEYS = {"branch", "base"}
LIMIT_KEYS = {"max_steps_per_ask", "max_minutes_per_ask", "max_retries_per_agent"}
POLICY_KEYS = {"answer_timeout", "max_answers_per_ask", "deny"}
FLOW_KEYS = {"steps", "tab"}
STEP_KEYS = {"agent", "shell", "prompt", "run", "on_fail", "max_retries", "timeout"}

# "bypass" skips every permission check (claude bypassPermissions): work agents only, opt-in per agent
PERMISSIONS = ("ask", "edit", "auto", "readonly", "bypass")
ROLE_UPDATE = ("confirm", "auto", "off")
ON_FAIL = ("stop", "continue")
UNATTENDED = ("off", "questions", "approve")
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --effort
DANGEROUS_ARGS = ("--dangerously-skip-permissions", "bypassPermissions", "--yolo",
                  "--dangerously-bypass-approvals-and-sandbox")
DEFAULT_LIMITS = {"max_steps_per_ask": 20, "max_minutes_per_ask": 120, "max_retries_per_agent": 3}
DEFAULT_POLICY = {"answer_timeout": 300.0, "max_answers_per_ask": 30, "deny": []}
# args that collide with what hctrl itself passes (permission flags, injected hook settings)
# opencode: --agent / --auto come from primary_agent / permission
OPENCODE_MANAGED_ARGS = ("--agent", "--auto")
MANAGED_ARGS = ("--permission-mode", "--allowedTools", "--allowed-tools", "--disallowedTools",
                "--disallowed-tools", "--settings")
DEFAULT_ROLES = ["구현", "테스트 작성", "리뷰", "보조"]
# default agent names, one per DEFAULT_ROLES entry (the UI page keeps the same list: ui_page.html ROLE_NAMES)
DEFAULT_AGENT_NAMES = ["coder", "tester", "reviewer", "helper"]
DEFAULT_TAB_BASE = "work"     # new tabs: work1, work2, ...
DEFAULT_SHELL_NAME = "build"


def unique_name(base: str, used: set[str]) -> str:
    """base, base2, base3 ... — the first one not in `used`."""
    if base not in used:
        return base
    k = 2
    while f"{base}{k}" in used:
        k += 1
    return f"{base}{k}"


def next_tab_id(existing: set[str]) -> str:
    """Smallest free work<N> (tab IDs are compared case-insensitively)."""
    taken = {e.lower() for e in existing}
    k = 1
    while f"{DEFAULT_TAB_BASE}{k}" in taken:
        k += 1
    return f"{DEFAULT_TAB_BASE}{k}"


@dataclass
class Issue:
    level: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        mark = "✘" if self.level == "error" else "⚠"
        return f"{mark} {self.path}: {self.message}"


@dataclass
class Member:
    name: str
    type: str  # "orch" | "agent" | "shell"
    tab: str  # configured tab name (e.g. "X")
    kind: str | None = None
    model: str | None = None
    primary_agent: str | None = None  # opencode only (--agent)
    effort: str | None = None  # claude only (--effort)
    args: list[str] = field(default_factory=list)
    permission: str | None = None
    role: str | None = None
    role_file: str | None = None
    goal: str | None = None
    cwd: str | None = None
    unattended: str = "off"  # the tab's unattended mode (set for work agents)
    tab_cwd: str | None = None  # the tab's cwd (layout input: moving it needs a re-layout)
    worktree: dict | None = None  # the tab's worktree {branch, base} (layout input)

    @property
    def is_agent(self) -> bool:
        return self.type in ("orch", "agent")


@dataclass
class TabCfg:
    name: str
    cwd: str | None
    orchestrator: Member | None
    agents: list[Member]
    shells: list[Member]
    role_update: str = "confirm"
    worktree: dict | None = None
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    unattended: str = "off"
    policy: dict = field(default_factory=lambda: dict(DEFAULT_POLICY, deny=[]))
    title: str | None = None  # herdr tab label shown on screen; `name` stays the id used by commands
    closed: bool = False  # ended (hctrl tab end): kept in config with its prompts, never laid out or watched

    @property
    def display(self) -> str:
        return self.title or self.name

    @property
    def members(self) -> list[Member]:
        return ([self.orchestrator] if self.orchestrator else []) + self.agents + self.shells


@dataclass
class Step:
    index: int
    agent: str | None
    shell: str | None
    prompt: str | None
    run: str | None
    on_fail: str = "stop"
    max_retries: int = 0
    timeout_s: float | None = None

    @property
    def target(self) -> str:
        return self.agent or self.shell or "?"


@dataclass
class Flow:
    name: str
    steps: list[Step]
    tab: str | None = None


@dataclass
class Config:
    session: str
    tabs: list[TabCfg]
    flows: dict[str, Flow]
    raw: dict

    @property
    def open_tabs(self) -> list[TabCfg]:
        """Tabs that run (ended `closed: true` tabs are left out of up and watch)."""
        return [t for t in self.tabs if not t.closed]

    def member(self, name: str) -> Member | None:
        for t in self.tabs:
            for m in t.members:
                if m.name == name:
                    return m
        return None

    def tab(self, name: str) -> TabCfg | None:
        for t in self.tabs:
            if t.name.lower() == name.lower():
                return t
        return None

    def tab_of(self, name: str) -> TabCfg | None:
        m = self.member(name)
        return self.tab(m.tab) if m else None


TITLE_MAX = 30


def valid_title(title) -> bool:
    return (isinstance(title, str) and 0 < len(title.strip()) <= TITLE_MAX
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in title))


def orch_name(tab: str) -> str:
    return f"{tab.lower()}-orch"


def parse_duration(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = DURATION_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"잘못된 시간 형식: {value!r} (예: 90s, 30m, 2h)")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def _suggest(key: str, allowed: set[str]) -> str:
    near = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
    return f" → '{near[0]}'인가요?" if near else f" (허용: {', '.join(sorted(allowed))})"


class _Checker:
    def __init__(self, which: Callable[[str], str | None], integrations: dict[str, str] | None,
                 root: Path | None, primary_agents: list[str] | None = None):
        self.primary_agents = primary_agents  # cached opencode primary agent names (None = unknown)
        self.issues: list[Issue] = []
        self.which = which
        self.integrations = integrations
        self.root = root

    def err(self, path: str, msg: str) -> None:
        self.issues.append(Issue("error", path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.issues.append(Issue("warning", path, msg))

    def text(self, data: dict, key: str, path: str) -> str | None:
        """Optional string value: anything else is an error and reads as None."""
        v = data.get(key)
        if v is not None and not isinstance(v, str):
            self.err(f"{path}.{key}", "문자열이어야 합니다")
            return None
        return v

    def inside_root(self, rel: str) -> bool:
        if self.root is None:
            p = Path(rel)
            return not p.is_absolute() and ".." not in p.parts
        return (self.root / rel).resolve().is_relative_to(self.root.resolve())

    def keys(self, data: dict, allowed: set[str], path: str) -> None:
        for k in data:
            if k not in allowed:
                self.err(f"{path}.{k}", "알 수 없는 키" + _suggest(str(k), allowed))

    def agent_member(self, name: str, data: Any, path: str, tab: str, mtype: str) -> Member | None:
        if not isinstance(data, dict):
            self.err(path, "객체여야 합니다 (예: { kind: claude })")
            return None
        self.keys(data, ORCH_KEYS if mtype == "orch" else AGENT_KEYS, path)
        kind = data.get("kind")
        if not kind:
            self.err(f"{path}.kind", "필수 항목입니다")
        elif kind not in AGENT_KINDS:
            self.err(f"{path}.kind", f"지원하지 않는 kind '{kind}'" + _suggest(str(kind), set(AGENT_KINDS)))
        else:
            if not self.which(kind):
                self.warn(f"{path}.kind", f"'{kind}' 실행 파일이 PATH에 없습니다")
            if self.integrations is not None and self.integrations.get(kind) != "current":
                self.warn(f"{path}.kind",
                          f"herdr '{kind}' 연동이 설치되지 않았습니다 (herdr integration install {kind})")
        perm = data.get("permission", "readonly" if mtype == "orch" else "edit")
        if perm not in PERMISSIONS:
            self.err(f"{path}.permission", f"'{perm}' 대신 {'/'.join(PERMISSIONS)} 중 하나")
        elif perm == "bypass" and mtype == "orch" and kind == "claude":
            # allowed (outside unattended tabs, checked per tab), but it lifts the orchestrator's guard rails
            self.warn(f"{path}.permission", "bypass: orchestrator가 승인 없이 코드를 고치고 명령을 실행합니다. "
                                            "프로젝트 밖 상태 폴더도 고칠 수 있어 작업 상한(limits)이 우회될 수 "
                                            "있습니다. 처음 쓰는 PC라면 claude --permission-mode "
                                            "bypassPermissions를 한 번 직접 실행해 수락해 두세요")
        elif perm == "bypass" and kind == "claude":
            self.warn(f"{path}.permission", "bypass: 모든 권한 확인을 건너뜁니다. 작업 폴더 밖 파일 수정이나 "
                                            "위험한 명령도 묻지 않고 실행합니다. 처음 쓰는 PC라면 claude가 시작할 때 "
                                            "확인 창을 띄우니, claude --permission-mode bypassPermissions를 "
                                            "한 번 직접 실행해 수락해 두세요")
        elif kind == "opencode" and "permission" in data and perm not in ("ask", "auto"):
            self.warn(f"{path}.permission", f"opencode는 auto(--auto)만 지원하고 '{perm}'은 적용되지 않습니다(ask는 설정 파일에 맡김). "
                                            "나머지는 opencode 설정 파일을 따릅니다")
        args = data.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) and not isinstance(a, bool)
                                                 for a in args):
            self.err(f"{path}.args", "문자열 목록이어야 합니다")
            args = []
        for a in args:
            if any(d in str(a) for d in DANGEROUS_ARGS):
                self.err(f"{path}.args", f"권한 전체 우회 옵션 '{a}'은 지원하지 않습니다 (permission 사용)")
            elif kind == "opencode" and str(a).split("=", 1)[0] in OPENCODE_MANAGED_ARGS:
                self.warn(f"{path}.args", f"'{a}'는 hctrl이 primary_agent / permission 값으로 직접 지정하는 옵션이라 "
                                          "충돌합니다. primary_agent / permission 값을 쓰세요")
            elif kind == "claude" and str(a).split("=", 1)[0] == "--effort" and data.get("effort"):
                self.warn(f"{path}.args", f"'{a}'와 effort 값이 겹칩니다. effort 값만 쓰세요")
            elif kind == "claude" and str(a).split("=", 1)[0] in MANAGED_ARGS:
                self.warn(f"{path}.args", f"'{a}'는 hctrl이 permission/무인 모드로 직접 지정하는 옵션이라 "
                                          "충돌합니다. permission 값을 쓰세요")
        model = data.get("model")
        if model is not None and not isinstance(model, str):
            self.err(f"{path}.model", "문자열이어야 합니다")
            model = None
        if model and kind == "opencode" and "/" not in model:
            self.warn(f"{path}.model", f"opencode 모델은 '제공자/모델' 형식입니다 (현재 '{model}')")
        if model and kind not in ("claude", "opencode", None) and kind in AGENT_KINDS:
            self.warn(f"{path}.model", f"'{kind}'의 모델 옵션 매핑을 모릅니다. args로 직접 지정하세요")
        primary = self.text(data, "primary_agent", path)
        if primary is not None and not primary.strip():
            primary = None
        if primary and kind != "opencode":  # includes kind unset (the UI reads it as claude)
            self.warn(f"{path}.primary_agent", "opencode 전용 설정이라 적용되지 않습니다")
            primary = None
        elif primary and self.primary_agents:
            low = primary.strip().lower()
            if not any(low == n.lower() or low == n.split(" - ", 1)[0].lower() for n in self.primary_agents):
                self.warn(f"{path}.primary_agent", f"'{primary}'은 캐시된 opencode primary agent 목록에 없습니다. "
                                                   "subagent이거나 없는 이름이면 opencode가 기본 agent로 대체합니다 "
                                                   f"(목록: {', '.join(self.primary_agents)})")
        effort = self.text(data, "effort", path)
        if effort is not None and not effort.strip():
            effort = None
        if effort and effort not in EFFORTS:
            self.err(f"{path}.effort", f"{' | '.join(EFFORTS)} 중 하나여야 합니다 (현재 '{effort}')")
            effort = None
        elif effort and kind not in ("claude", None):  # kind unset: the UI reads it as claude
            self.warn(f"{path}.effort", "claude 전용 설정이라 적용되지 않습니다")
            effort = None
        role_file = self.text(data, "role_file", path)
        if role_file and not self.inside_root(role_file):
            self.err(f"{path}.role_file", f"프로젝트 폴더 안의 경로여야 합니다: {role_file}")
            role_file = None
        elif role_file and self.root and not (self.root / role_file).exists():
            self.warn(f"{path}.role_file", f"파일이 없습니다: {role_file}")
        return Member(name=name, type=mtype, tab=tab, kind=kind, model=model,
                      primary_agent=primary.strip() if primary else None, effort=effort,
                      args=[str(a) for a in args], permission=perm, role=self.text(data, "role", path),
                      role_file=role_file, goal=self.text(data, "goal", path))


def check_data(data: Any, root: Path | None = None, which: Callable[[str], str | None] = shutil.which,
               integrations: dict[str, str] | None = None,
               primary_agents: list[str] | None = None) -> tuple[Config | None, list[Issue]]:
    c = _Checker(which, integrations, root, primary_agents)
    if not isinstance(data, dict):
        c.err("config", "최상위는 객체여야 합니다")
        return None, c.issues
    c.keys(data, TOP_KEYS, "config")
    session = data.get("session")
    if not session or not isinstance(session, str) or not re.match(r"^[A-Za-z0-9_.-]+$", session):
        c.err("session", "필수 항목이며 영문/숫자/._- 만 사용할 수 있습니다")
    tabs_raw = data.get("tabs")
    if not isinstance(tabs_raw, dict) or not tabs_raw:
        c.err("tabs", "탭이 최소 1개 필요합니다")
        tabs_raw = {}
    tabs: list[TabCfg] = []
    seen: dict[str, str] = {}
    tab_names = [str(t) for t in tabs_raw]
    for tname, tdata in tabs_raw.items():
        tpath = f"tabs.{tname}"
        tname = str(tname)
        if not TAB_RE.match(tname) or tname == CONTROL_TAB:
            c.err(tpath, "탭 이름은 영문 또는 숫자로 시작하고 영문/숫자/_- 25자 이하 ('_hc'는 예약됨)")
        if [t.lower() for t in tab_names].count(tname.lower()) > 1:
            c.err(tpath, "대소문자만 다른 탭 이름이 있습니다 (탭 이름은 대소문자를 구분하지 않습니다)")
        for other in tab_names:
            if other != tname and re.fullmatch(rf"{re.escape(other)}(-build)?(-\d+)?", tname, re.IGNORECASE):
                c.err(tpath, f"'{other}' 탭이 만드는 탭 이름('{other}-2', '{other}-build' 등)과 겹칩니다. 다른 이름을 쓰세요")
        if not isinstance(tdata, dict):
            c.err(tpath, "객체여야 합니다")
            continue
        c.keys(tdata, TAB_KEYS, tpath)
        orch = None
        o = tdata.get("orchestrator", "none")
        if o not in (None, "none"):
            orch = c.agent_member(orch_name(tname), o, f"{tpath}.orchestrator", tname, "orch")
        agents, shells = [], []
        for section, mtype, target in (("agents", "agent", agents), ("shells", "shell", shells)):
            sec = tdata.get(section) or {}
            if not isinstance(sec, dict):
                c.err(f"{tpath}.{section}", "객체여야 합니다")
                continue
            for name, mdata in sec.items():
                mpath = f"{tpath}.{section}.{name}"
                name = str(name)
                if not NAME_RE.match(name):
                    c.err(mpath, "이름 규칙 위반: 소문자로 시작, 소문자/숫자/_- 32자 이하")
                if mtype == "agent":
                    m = c.agent_member(name, mdata, mpath, tname, "agent")
                else:
                    mdata = mdata or {}
                    if not isinstance(mdata, dict):
                        c.err(mpath, "객체여야 합니다 ({} 가능)")
                        continue
                    c.keys(mdata, SHELL_KEYS, mpath)
                    m = Member(name=name, type="shell", tab=tname, cwd=c.text(mdata, "cwd", mpath))
                if m:
                    target.append(m)
        for m in ([orch] if orch else []) + agents + shells:
            if m.name in seen:
                c.err(f"{tpath}", f"이름 '{m.name}'이 탭 {seen[m.name]}와 중복됩니다")
            seen[m.name] = tname
        title = tdata.get("title")
        if title is not None and not valid_title(title):
            c.err(f"{tpath}.title", f"표시 이름은 1~{TITLE_MAX}자 문자열이며 줄바꿈/제어 문자를 쓸 수 없습니다")
            title = None
        if title is not None:
            # herdr labels must stay unique: up() falls back to finding tabs by label
            others = [str(t) for t in tabs_raw if str(t) != tname]
            others += [str(d["title"]).strip() for t, d in tabs_raw.items()
                       if str(t) != tname and isinstance(d, dict) and isinstance(d.get("title"), str)]
            if title.strip() == CONTROL_TAB or title.strip().lower() in {o.lower() for o in others}:
                c.err(f"{tpath}.title", f"표시 이름 '{title.strip()}'이 다른 탭의 ID/표시 이름 또는 '_hc'와 겹칩니다")
                title = None
        ru = tdata.get("role_update", "confirm")
        if ru is False:  # YAML 1.1 reads a bare `off` as false
            ru = "off"
        if ru not in ROLE_UPDATE:
            c.err(f"{tpath}.role_update", f"{'/'.join(ROLE_UPDATE)} 중 하나")
        wt = tdata.get("worktree")
        if wt is not None:
            if not isinstance(wt, dict) or not wt.get("branch"):
                c.err(f"{tpath}.worktree", "{ branch: <이름> } 형식이어야 합니다")
                wt = None
            else:
                c.keys(wt, WORKTREE_KEYS, f"{tpath}.worktree")
                for k in WORKTREE_KEYS:
                    c.text(wt, k, f"{tpath}.worktree")
                if root and not (root / ".git").exists():
                    c.err(f"{tpath}.worktree", "git 저장소가 아니어서 worktree를 쓸 수 없습니다")
        limits = dict(DEFAULT_LIMITS)
        lim = tdata.get("limits") or {}
        if not isinstance(lim, dict):
            c.err(f"{tpath}.limits", "객체여야 합니다")
        else:
            c.keys(lim, LIMIT_KEYS, f"{tpath}.limits")
            for k, v in lim.items():
                if k in LIMIT_KEYS:
                    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                        c.err(f"{tpath}.limits.{k}", "0 이상의 정수여야 합니다")
                    else:
                        limits[k] = v
        unattended = tdata.get("unattended", "off")
        if unattended is False:  # YAML 1.1 reads a bare `off` as false
            unattended = "off"
        if unattended not in UNATTENDED:
            c.err(f"{tpath}.unattended", f"{'/'.join(UNATTENDED)} 중 하나")
            unattended = "off"
        elif unattended != "off":
            if orch is None:
                c.err(f"{tpath}.unattended", "orchestrator가 없는 탭에서는 무인 모드를 쓸 수 없습니다 (off로 두세요)")
            for m in agents:
                if m.permission == "bypass":
                    # no permission dialog → the hook never runs, and the agent can write the answer store
                    c.err(f"{tpath}.agents.{m.name}.permission",
                          "무인 모드 탭에서는 bypass를 쓸 수 없습니다 (승인 훅이 동작하지 않고 답변 저장소를 보호할 수 없음)")
            if orch is not None and orch.permission == "bypass":
                # the orchestrator answers agents' questions/approvals: it must not be able to forge the store
                c.err(f"{tpath}.orchestrator.permission",
                      "무인 모드 탭의 orchestrator에는 bypass를 쓸 수 없습니다 (답변 저장소와 상한을 고칠 수 있음)")
            for m in agents:
                if m.kind == "opencode" and m.permission == "auto":
                    # --auto approves silently: no approval prompt ever reaches the hook/orchestrator
                    c.err(f"{tpath}.agents.{m.name}.permission",
                          "무인 모드 탭에서는 opencode auto(--auto)를 쓸 수 없습니다 (승인 창이 뜨지 않아 승인을 처리할 수 없음)")
            if orch is not None and orch.kind == "opencode" and orch.permission == "auto":
                c.err(f"{tpath}.orchestrator.permission",
                      "무인 모드 탭의 orchestrator에는 opencode auto(--auto)를 쓸 수 없습니다 (승인 창이 뜨지 않음)")
            for m in agents:
                if m.kind and m.kind != "claude":
                    c.warn(f"{tpath}.agents.{m.name}",
                           f"'{m.kind}'는 무인 응답을 지원하지 않습니다. 이 agent의 질문/승인은 사용자에게 넘어갑니다")
        policy = dict(DEFAULT_POLICY, deny=[])
        pol = tdata.get("unattended_policy") or {}
        if not isinstance(pol, dict):
            c.err(f"{tpath}.unattended_policy", "객체여야 합니다")
        else:
            c.keys(pol, POLICY_KEYS, f"{tpath}.unattended_policy")
            if "answer_timeout" in pol:
                try:
                    t = parse_duration(pol["answer_timeout"])
                    if t is None or not 10 <= t <= 3600:
                        raise ValueError("10초 이상 1시간 이하여야 합니다 (예: 300s, 10m)")
                    policy["answer_timeout"] = t
                except ValueError as e:
                    c.err(f"{tpath}.unattended_policy.answer_timeout", str(e))
            if "max_answers_per_ask" in pol:
                v = pol["max_answers_per_ask"]
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    c.err(f"{tpath}.unattended_policy.max_answers_per_ask", "0 이상의 정수여야 합니다")
                else:
                    policy["max_answers_per_ask"] = v
            if "deny" in pol:
                d = pol["deny"]
                if not isinstance(d, list) or not all(isinstance(x, str) and x.strip() for x in d):
                    c.err(f"{tpath}.unattended_policy.deny", "비어 있지 않은 문자열 목록이어야 합니다")
                else:
                    policy["deny"] = list(d)
        for m in agents:
            m.unattended = unattended
        tcwd = c.text(tdata, "cwd", tpath)
        wt_fp = {k: wt[k] for k in sorted(WORKTREE_KEYS) if wt.get(k) is not None} if wt else None
        for m in ([orch] if orch else []) + agents + shells:
            m.tab_cwd, m.worktree = tcwd, wt_fp
        tab = TabCfg(name=tname, cwd=tcwd, orchestrator=orch, agents=agents,
                     shells=shells, role_update=ru if ru in ROLE_UPDATE else "confirm",
                     worktree=wt, limits=limits, unattended=unattended, policy=policy,
                     title=title.strip() if title else None, closed=tdata.get("closed") is True)
        if "closed" in tdata and not isinstance(tdata["closed"], bool):
            c.err(f"{tpath}.closed", "true 또는 false")
        if not tab.members:
            c.err(tpath, "agent나 shell이 최소 1개 필요합니다")
        # orchestrator + agents share tab X; shells get their own tab X-build (see layout.group_tab)
        for what, n, over in (("agent", len(tab.members) - len(tab.shells), f"{tab.display}-2"),
                              ("쉘", len(tab.shells), f"{tab.display}-build-2")):
            if n > MAX_PANES_PER_TAB:
                c.warn(tpath, f"{what} 창 {n}개 → 탭당 최대 {MAX_PANES_PER_TAB}개라 "
                              f"{n - MAX_PANES_PER_TAB}개가 '{over}' 탭 이후로 넘어갑니다")
        if root and tab.cwd and not (root / tab.cwd).exists():
            c.warn(f"{tpath}.cwd", f"경로가 없습니다: {tab.cwd}")
        tabs.append(tab)

    flows: dict[str, Flow] = {}
    flows_raw = data.get("flows") or {}
    if not isinstance(flows_raw, dict):
        c.err("flows", "객체여야 합니다")
        flows_raw = {}
    agent_names = {m.name for t in tabs for m in t.members if m.is_agent}
    shell_names = {m.name for t in tabs for m in t.members if m.type == "shell"}
    for fname, fdata in flows_raw.items():
        fpath = f"flows.{fname}"
        if not isinstance(fdata, dict) or not isinstance(fdata.get("steps"), list) or not fdata["steps"]:
            c.err(fpath, "steps 목록이 필요합니다")
            continue
        c.keys(fdata, FLOW_KEYS, fpath)
        ftab_name = c.text(fdata, "tab", fpath)
        ftab = None
        if ftab_name is not None:
            ftab = next((t for t in tabs if t.name.lower() == ftab_name.lower()), None)
            if ftab is None:
                c.err(f"{fpath}.tab", f"'{ftab_name}' 탭이 없습니다 (탭: {', '.join(t.name for t in tabs) or '-'})")
        steps = []
        for i, sdata in enumerate(fdata["steps"]):
            spath = f"{fpath}.steps[{i}]"
            if not isinstance(sdata, dict):
                c.err(spath, "객체여야 합니다")
                continue
            c.keys(sdata, STEP_KEYS, spath)
            if bool(sdata.get("agent")) == bool(sdata.get("shell")):
                c.err(spath, "agent 또는 shell 중 정확히 하나가 필요합니다")
            agent, shell = c.text(sdata, "agent", spath), c.text(sdata, "shell", spath)
            prompt, run = c.text(sdata, "prompt", spath), c.text(sdata, "run", spath)
            if ftab is not None and (agent or shell) and (agent or shell) in agent_names | shell_names \
                    and (agent or shell) not in {m.name for m in ftab.members}:
                c.err(spath, f"'{agent or shell}'는 flow의 탭 '{ftab.name}'에 없습니다")
            if agent:
                if agent not in agent_names:
                    c.err(f"{spath}.agent", f"'{agent}' 없음 (agent: {', '.join(sorted(agent_names)) or '-'})")
                if not prompt:
                    c.err(f"{spath}.prompt", "agent 단계에는 prompt가 필요합니다")
            if shell:
                if shell not in shell_names:
                    c.err(f"{spath}.shell", f"'{shell}' 없음 (shell: {', '.join(sorted(shell_names)) or '-'})")
                if not run:
                    c.err(f"{spath}.run", "shell 단계에는 run이 필요합니다")
            on_fail = sdata.get("on_fail", "stop")
            if on_fail not in ON_FAIL:
                c.err(f"{spath}.on_fail", f"{'/'.join(ON_FAIL)} 중 하나")
                on_fail = "stop"
            mr = sdata.get("max_retries", 0)
            if not isinstance(mr, int) or isinstance(mr, bool) or mr < 0:
                c.err(f"{spath}.max_retries", "0 이상의 정수여야 합니다")
                mr = 0
            try:
                if isinstance(sdata.get("timeout"), bool):
                    raise ValueError("시간 값이어야 합니다 (예: 90s, 30m, 2h)")
                timeout_s = parse_duration(sdata.get("timeout"))
                if timeout_s is not None and timeout_s < 0:
                    raise ValueError("0 이상이어야 합니다")
            except ValueError as e:
                c.err(f"{spath}.timeout", str(e))
                timeout_s = None
            if i == 0 and "{prev}" in (prompt or run or ""):
                c.warn(spath, "첫 단계에는 {prev}가 없습니다 (빈 값으로 치환됨)")
            steps.append(Step(i, agent, shell, prompt, run, on_fail, mr, timeout_s))
        flows[str(fname)] = Flow(str(fname), steps, ftab.name if ftab else None)

    has_error = any(i.level == "error" for i in c.issues)
    cfg = None if has_error else Config(session=session, tabs=tabs, flows=flows, raw=data)
    return cfg, c.issues


def str_keys(data: Any) -> Any:
    """A tab id written without quotes (`1:`) is an int in YAML: tab ids and flow `tab:` are strings."""
    if not isinstance(data, dict):
        return data
    if isinstance(data.get("tabs"), dict):
        data["tabs"] = {str(k): v for k, v in data["tabs"].items()}
    for f in (data.get("flows") or {}).values() if isinstance(data.get("flows"), dict) else []:
        if isinstance(f, dict) and isinstance(f.get("tab"), int) and not isinstance(f.get("tab"), bool):
            f["tab"] = str(f["tab"])
    return data


def load_yaml(path: Path) -> tuple[Any, list[Issue]]:
    if not path.exists():
        return None, [Issue("error", str(path), "설정 파일이 없습니다 (hctrl init으로 생성)")]
    try:
        return str_keys(yaml.safe_load(path.read_text())), []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (줄 {mark.line + 1}, 칸 {mark.column + 1})" if mark else ""
        return None, [Issue("error", str(path), f"YAML 문법 오류{where}: {getattr(e, 'problem', e)}")]


HIDDEN_AGENTS = {"compaction", "summary", "title"}  # opencode-internal agents
_AGENT_LINE = re.compile(r"^(\S.*?) \((primary|subagent|all)\)$")


def parse_agent_list(text: str) -> list[str]:
    """`opencode agent list` → primary/all agent names (deduped, in order, internal ones left out).

    Only unindented `name (mode)` lines count; the permission JSON that follows each is ignored."""
    seen: list[str] = []
    for line in text.splitlines():
        m = _AGENT_LINE.match(line.rstrip())
        if m and m.group(2) in ("primary", "all") and m.group(1) not in HIDDEN_AGENTS and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def agents_cache_path() -> Path:
    from .project import state_home
    return state_home() / "opencode-agents.json"


def cached_primary_agents() -> list[str] | None:
    """The cached opencode primary agent list, or None (no cache: the name check is skipped)."""
    import json
    try:
        d = json.loads(agents_cache_path().read_text())
        got = d.get("agents")
        if isinstance(got, list) and got and all(isinstance(a, str) for a in got):
            return got
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def check_file(path: Path, **kw) -> tuple[Config | None, list[Issue]]:
    data, issues = load_yaml(path)
    if issues:
        return None, issues
    kw.setdefault("primary_agents", cached_primary_agents())
    return check_data(data, root=path.parent.parent, **kw)


def format_issues(issues: list[Issue]) -> str:
    lines = [str(i) for i in issues]
    ne = sum(i.level == "error" for i in issues)
    nw = sum(i.level == "warning" for i in issues)
    lines.append(f"오류 {ne}, 경고 {nw}")
    return "\n".join(lines)


# ---------------------------------------------------------------- agent args
PERMISSION_ARGS = {
    "opencode": {"auto": ["--auto"]},   # the rest follows opencode's own config file
    "claude": {
        # explicit: without a mode claude starts in the account default (which may be auto)
        "ask": ["--permission-mode", "default"],
        "edit": ["--permission-mode", "acceptEdits"],
        "auto": ["--permission-mode", "auto"],
        "bypass": ["--permission-mode", "bypassPermissions"],
        # orchestrator: read + hctrl only; anything else asks the user (variadic option, so it must come last)
        "readonly": ["--permission-mode", "default", "--allowedTools", "Read", "Grep", "Glob", "Bash(hctrl:*)"],
    },
}


def agent_args(m: Member, system_prompt_file: Path | None = None,
               settings_file: Path | None = None, handoff_file: Path | None = None) -> list[str]:
    """Native CLI args passed after `--` to `herdr agent start`."""
    args: list[str] = []
    if m.model:
        if m.kind == "claude":
            args += ["--model", m.model]
        elif m.kind == "opencode":
            args += ["-m", m.model]
    if system_prompt_file and m.kind == "claude":
        args += ["--append-system-prompt-file", str(system_prompt_file)]
    if m.primary_agent and m.kind == "opencode":
        args += ["--agent", m.primary_agent]
    if m.effort and m.kind == "claude":
        args += ["--effort", m.effort]
    args += list(m.args)
    if settings_file and m.kind == "claude":
        args += ["--settings", str(settings_file)]
    perm = list(PERMISSION_ARGS.get(m.kind or "", {}).get(m.permission or "", []))
    if handoff_file and m.kind == "claude" and m.permission in ("ask", "readonly"):
        # restart asks the agent to write its own handoff file: allow exactly that one file
        # ("//" = absolute path in Claude Code permission rules). allowedTools is variadic, so it stays last.
        rule = f"Edit(/{Path(handoff_file).resolve()})"
        perm += [rule] if "--allowedTools" in perm else ["--allowedTools", rule]
    return args + perm
