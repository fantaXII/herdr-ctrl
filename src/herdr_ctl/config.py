"""Load and validate .herdr-flow/config.yaml (`hc check`)."""
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
TAB_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,24}$")
DURATION_RE = re.compile(r"^(\d+)\s*(ms|s|m|h)?$")
MAX_PANES_PER_TAB = 4
CONTROL_TAB = "_hc"

TOP_KEYS = {"session", "tabs", "flows"}
TAB_KEYS = {"cwd", "orchestrator", "role_update", "worktree", "limits", "agents", "shells"}
AGENT_KEYS = {"kind", "model", "role", "role_file", "permission", "args"}
ORCH_KEYS = {"kind", "model", "goal", "permission", "args"}
SHELL_KEYS = {"cwd"}
WORKTREE_KEYS = {"branch", "base"}
LIMIT_KEYS = {"max_steps_per_ask", "max_minutes_per_ask", "max_retries_per_agent"}
FLOW_KEYS = {"steps", "tab"}
STEP_KEYS = {"agent", "shell", "prompt", "run", "on_fail", "max_retries", "timeout"}

PERMISSIONS = ("ask", "edit", "auto", "readonly")
ROLE_UPDATE = ("confirm", "auto", "off")
ON_FAIL = ("stop", "continue")
DANGEROUS_ARGS = ("--dangerously-skip-permissions", "bypassPermissions", "--yolo",
                  "--dangerously-bypass-approvals-and-sandbox")
DEFAULT_LIMITS = {"max_steps_per_ask": 20, "max_minutes_per_ask": 120, "max_retries_per_agent": 3}
DEFAULT_ROLES = ["구현", "테스트 작성", "리뷰", "보조"]


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
    args: list[str] = field(default_factory=list)
    permission: str | None = None
    role: str | None = None
    role_file: str | None = None
    goal: str | None = None
    cwd: str | None = None

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
                 root: Path | None):
        self.issues: list[Issue] = []
        self.which = which
        self.integrations = integrations
        self.root = root

    def err(self, path: str, msg: str) -> None:
        self.issues.append(Issue("error", path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.issues.append(Issue("warning", path, msg))

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
        elif kind == "opencode" and "permission" in data and perm != "ask":
            self.warn(f"{path}.permission", "opencode는 시작 옵션으로 권한을 지정할 수 없습니다. opencode 설정 파일에서 지정하세요")
        args = data.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
            self.err(f"{path}.args", "문자열 목록이어야 합니다")
            args = []
        for a in args:
            if any(d in str(a) for d in DANGEROUS_ARGS):
                self.warn(f"{path}.args", f"권한 전체 우회 옵션 '{a}'은 지원하지 않습니다 (permission 사용 권장)")
        model = data.get("model")
        if model is not None and not isinstance(model, str):
            self.err(f"{path}.model", "문자열이어야 합니다")
            model = None
        if model and kind == "opencode" and "/" not in model:
            self.warn(f"{path}.model", f"opencode 모델은 '제공자/모델' 형식입니다 (현재 '{model}')")
        if model and kind not in ("claude", "opencode", None) and kind in AGENT_KINDS:
            self.warn(f"{path}.model", f"'{kind}'의 모델 옵션 매핑을 모릅니다. args로 직접 지정하세요")
        role_file = data.get("role_file")
        if role_file and self.root and not (self.root / role_file).exists():
            self.warn(f"{path}.role_file", f"파일이 없습니다: {role_file}")
        return Member(name=name, type=mtype, tab=tab, kind=kind, model=model,
                      args=[str(a) for a in args], permission=perm, role=data.get("role"),
                      role_file=role_file, goal=data.get("goal"))


def check_data(data: Any, root: Path | None = None, which: Callable[[str], str | None] = shutil.which,
               integrations: dict[str, str] | None = None) -> tuple[Config | None, list[Issue]]:
    c = _Checker(which, integrations, root)
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
    for tname, tdata in tabs_raw.items():
        tpath = f"tabs.{tname}"
        tname = str(tname)
        if not TAB_RE.match(tname) or tname == CONTROL_TAB:
            c.err(tpath, "탭 이름은 영문으로 시작하고 영문/숫자/_- 25자 이하 ('_hc'는 예약됨)")
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
                    m = Member(name=name, type="shell", tab=tname, cwd=mdata.get("cwd"))
                if m:
                    target.append(m)
        for m in ([orch] if orch else []) + agents + shells:
            if m.name in seen:
                c.err(f"{tpath}", f"이름 '{m.name}'이 탭 {seen[m.name]}와 중복됩니다")
            seen[m.name] = tname
        ru = tdata.get("role_update", "confirm")
        if ru not in ROLE_UPDATE:
            c.err(f"{tpath}.role_update", f"{'/'.join(ROLE_UPDATE)} 중 하나")
        wt = tdata.get("worktree")
        if wt is not None:
            if not isinstance(wt, dict) or not wt.get("branch"):
                c.err(f"{tpath}.worktree", "{ branch: <이름> } 형식이어야 합니다")
                wt = None
            else:
                c.keys(wt, WORKTREE_KEYS, f"{tpath}.worktree")
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
                    if not isinstance(v, int) or v < 0:
                        c.err(f"{tpath}.limits.{k}", "0 이상의 정수여야 합니다")
                    else:
                        limits[k] = v
        tab = TabCfg(name=tname, cwd=tdata.get("cwd"), orchestrator=orch, agents=agents,
                     shells=shells, role_update=ru if ru in ROLE_UPDATE else "confirm",
                     worktree=wt, limits=limits)
        if not tab.members:
            c.err(tpath, "agent나 shell이 최소 1개 필요합니다")
        n = len(tab.members)
        if n > MAX_PANES_PER_TAB:
            extra = n - MAX_PANES_PER_TAB
            c.warn(tpath, f"pane {n}개 → 탭당 최대 {MAX_PANES_PER_TAB}개라 {extra}개가 "
                          f"'{tname}-2' 탭 이후로 넘어갑니다")
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
        steps = []
        for i, sdata in enumerate(fdata["steps"]):
            spath = f"{fpath}.steps[{i}]"
            if not isinstance(sdata, dict):
                c.err(spath, "객체여야 합니다")
                continue
            c.keys(sdata, STEP_KEYS, spath)
            agent, shell = sdata.get("agent"), sdata.get("shell")
            if bool(agent) == bool(shell):
                c.err(spath, "agent 또는 shell 중 정확히 하나가 필요합니다")
            if agent:
                if agent not in agent_names:
                    c.err(f"{spath}.agent", f"'{agent}' 없음 (agent: {', '.join(sorted(agent_names)) or '-'})")
                if not sdata.get("prompt"):
                    c.err(f"{spath}.prompt", "agent 단계에는 prompt가 필요합니다")
            if shell:
                if shell not in shell_names:
                    c.err(f"{spath}.shell", f"'{shell}' 없음 (shell: {', '.join(sorted(shell_names)) or '-'})")
                if not sdata.get("run"):
                    c.err(f"{spath}.run", "shell 단계에는 run이 필요합니다")
            on_fail = sdata.get("on_fail", "stop")
            if on_fail not in ON_FAIL:
                c.err(f"{spath}.on_fail", f"{'/'.join(ON_FAIL)} 중 하나")
                on_fail = "stop"
            mr = sdata.get("max_retries", 0)
            if not isinstance(mr, int) or mr < 0:
                c.err(f"{spath}.max_retries", "0 이상의 정수여야 합니다")
                mr = 0
            try:
                timeout_s = parse_duration(sdata.get("timeout"))
            except ValueError as e:
                c.err(f"{spath}.timeout", str(e))
                timeout_s = None
            if i == 0 and "{prev}" in str(sdata.get("prompt") or sdata.get("run") or ""):
                c.warn(spath, "첫 단계에는 {prev}가 없습니다 (빈 값으로 치환됨)")
            steps.append(Step(i, agent, shell, sdata.get("prompt"), sdata.get("run"),
                              on_fail, mr, timeout_s))
        flows[str(fname)] = Flow(str(fname), steps, fdata.get("tab"))

    has_error = any(i.level == "error" for i in c.issues)
    cfg = None if has_error else Config(session=session, tabs=tabs, flows=flows, raw=data)
    return cfg, c.issues


def load_yaml(path: Path) -> tuple[Any, list[Issue]]:
    if not path.exists():
        return None, [Issue("error", str(path), "설정 파일이 없습니다 (hc init으로 생성)")]
    try:
        return yaml.safe_load(path.read_text()), []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (줄 {mark.line + 1}, 칸 {mark.column + 1})" if mark else ""
        return None, [Issue("error", str(path), f"YAML 문법 오류{where}: {getattr(e, 'problem', e)}")]


def check_file(path: Path, **kw) -> tuple[Config | None, list[Issue]]:
    data, issues = load_yaml(path)
    if issues:
        return None, issues
    return check_data(data, root=path.parent.parent, **kw)


def format_issues(issues: list[Issue]) -> str:
    lines = [str(i) for i in issues]
    ne = sum(i.level == "error" for i in issues)
    nw = sum(i.level == "warning" for i in issues)
    lines.append(f"오류 {ne}, 경고 {nw}")
    return "\n".join(lines)


# ---------------------------------------------------------------- agent args
PERMISSION_ARGS = {
    "claude": {
        "ask": [],
        "edit": ["--permission-mode", "acceptEdits"],
        "auto": ["--permission-mode", "auto"],
        # orchestrator: read + hc only (variadic option, so it must come last)
        "readonly": ["--allowedTools", "Read", "Grep", "Glob", "Bash(hc:*)"],
    },
}


def agent_args(m: Member, system_prompt_file: Path | None = None) -> list[str]:
    """Native CLI args passed after `--` to `herdr agent start`."""
    args: list[str] = []
    if m.model:
        if m.kind == "claude":
            args += ["--model", m.model]
        elif m.kind == "opencode":
            args += ["-m", m.model]
    if system_prompt_file and m.kind == "claude":
        args += ["--append-system-prompt-file", str(system_prompt_file)]
    args += list(m.args)
    args += PERMISSION_ARGS.get(m.kind or "", {}).get(m.permission or "", [])
    return args
