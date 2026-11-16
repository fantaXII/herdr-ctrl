"""`hc init`: interactive config.yaml generation (followed by an automatic check)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import yaml

from .config import DEFAULT_ROLES, NAME_RE, TAB_RE, check_data, format_issues
from .herdr import AGENT_KINDS
from .project import Project, ensure_gitignore

Ask = Callable[[str], str]


def _q(ask: Ask, prompt: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str:
    while True:
        shown = f"{prompt} [{default}] > " if default else f"{prompt} > "
        ans = ask(shown).strip() or default
        err = validate(ans) if validate else None
        if not err:
            return ans
        print(f"  ✘ {err}")


def _yes(ask: Ask, prompt: str, default: bool = True) -> bool:
    ans = ask(f"{prompt} [{'Y/n' if default else 'y/N'}] > ").strip().lower()
    return default if not ans else ans.startswith("y")


def _kind_model(ask: Ask, prompt: str, default: str) -> tuple[str, str | None]:
    def v(ans):
        kind = ans.split("/", 1)[0].strip()
        return None if kind in AGENT_KINDS else f"지원 kind: {', '.join(AGENT_KINDS)}"
    ans = _q(ask, f"{prompt} (종류/모델, 예: claude/sonnet, opencode/anthropic/claude-sonnet-5-5)", default, v)
    kind, _, model = ans.partition("/")
    return kind.strip(), (model.strip() or None)


def ask_tab(ask: Ask, existing: set[str], used_names: set[str], default: str | None = None) -> tuple[str, dict]:
    taken = {e.lower() for e in existing}

    def valid(a: str) -> bool:
        return bool(TAB_RE.match(a)) and a.lower() not in taken and a != "_hc"
    if not (default and valid(default)):
        default = "X" if valid("X") else ""
    name = _q(ask, "탭 이름", default,
              lambda a: None if valid(a) else "영문으로 시작, 영문/숫자/_- (기존 탭과 중복 불가, 대소문자 무시)")
    tab: dict = {}
    if _yes(ask, "orchestrator(지휘 LLM)를 둘까요?", True):
        kind, model = _kind_model(ask, "  orchestrator", "claude/opus")
        orch: dict = {"kind": kind}
        if model:
            orch["model"] = model
        goal = _q(ask, "  프로젝트 목표 한 줄 (선택)")
        if goal:
            orch["goal"] = goal
        tab["orchestrator"] = orch
        tab["role_update"] = _q(ask, "  orchestrator의 역할 변경 정책 (confirm/auto/off)", "confirm",
                                lambda a: None if a in ("confirm", "auto", "off") else "confirm/auto/off")
        mode = _q(ask, "  무인 모드: 작업 agent의 질문/승인에 orchestrator가 답함 (off/questions/approve)", "off",
                  lambda a: None if a in ("off", "questions", "approve") else "off/questions/approve")
        if mode != "off":
            tab["unattended"] = mode
    else:
        tab["orchestrator"] = "none"
    n = int(_q(ask, "작업 agent 수", "2", lambda a: None if a.isdigit() and 0 <= int(a) <= 8 else "0~8"))
    prefix = re.sub(r"[^a-z0-9]", "", name.lower())[:1] or "a"
    agents = {}
    for i in range(1, n + 1):
        default_name = f"{prefix}{i}"
        while default_name in used_names:
            default_name += "x"
        aname = _q(ask, f"  agent {i} 이름", default_name,
                   lambda a: None if NAME_RE.match(a) and a not in used_names else "소문자 시작, 중복 불가")
        used_names.add(aname)
        kind, model = _kind_model(ask, f"  {aname}", "claude/sonnet" if i % 2 else "opencode")
        role = _q(ask, f"  {aname} 역할 한 줄", DEFAULT_ROLES[min(i - 1, len(DEFAULT_ROLES) - 1)])
        a: dict = {"kind": kind}
        if model:
            a["model"] = model
        a["role"] = role
        agents[aname] = a
    tab["agents"] = agents
    build = None
    if _yes(ask, "빌드/테스트용 쉘 창을 추가할까요?", True):
        sname = _q(ask, "  쉘 이름", f"{prefix}build",
                   lambda a: None if NAME_RE.match(a) and a not in used_names else "소문자 시작, 중복 불가")
        used_names.add(sname)
        build = _q(ask, "  빌드 명령 (flow용)", "npm run build")
        tab["shells"] = {sname: {}}
    tab["_build"] = (list(tab.get("shells", {}) or [None])[0], build)
    return name, tab


def default_flow(tab_name: str, tab: dict) -> dict | None:
    agents = list(tab["agents"])
    shell, build = tab["_build"]
    if not agents:
        return None
    steps = [{"agent": agents[0], "prompt": "요청된 기능을 구현하세요. 결과 요약을 {out} 에 쓰세요."}]
    if len(agents) > 1:
        steps.append({"agent": agents[1], "prompt": "{prev} 를 읽고 테스트를 추가하세요. 요약은 {out}"})
    if shell and build:
        steps.append({"shell": shell, "run": build, "on_fail": "continue"})
    steps.append({"agent": agents[0], "prompt": "{prev} 를 검토하고 문제가 있으면 수정하세요. 요약은 {out}"})
    return {f"build-review-{tab_name.lower()}": {"steps": steps}}


def build_config(session: str, tabs: dict[str, dict], flows: dict) -> dict:
    clean_tabs = {}
    for name, t in tabs.items():
        clean_tabs[name] = {k: v for k, v in t.items() if not k.startswith("_")}
    data = {"session": session, "tabs": clean_tabs}
    if flows:
        data["flows"] = flows
    return data


HEADER = """# herdr_ctl 설정 (hc init이 생성). 수정하면 다음 hc 명령 실행 시 자동 검사됩니다.
# 상세 역할: .herdr-flow/roles/<agent>.md (hc role <agent> --edit)
"""


def write_config(project: Project, data: dict) -> Path:
    project.ensure_dirs()
    text = HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    project.config_path.write_text(text)
    ensure_gitignore(project.root)
    return project.config_path


def run_init(project: Project, ask: Ask = input, add_tab: str | None = None, which=None) -> bool:
    """Interactive init. Returns True when the written config has no errors."""
    existing: dict = {}
    if project.config_path.exists():
        try:
            existing = yaml.safe_load(project.config_path.read_text()) or {}
            if not isinstance(existing, dict):
                raise ValueError("최상위가 객체가 아닙니다")
            for key in ("tabs", "flows"):
                if not isinstance(existing.get(key) or {}, dict):
                    raise ValueError(f"'{key}'가 객체가 아닙니다")
        except (yaml.YAMLError, ValueError) as e:
            existing = {}
            if add_tab is not None:
                print(f"✘ {project.config_path}를 읽을 수 없습니다: {e}\n  파일을 고친 뒤(hc check) 다시 실행하세요")
                return False
        if add_tab is None and not _yes(ask, f"{project.config_path}가 이미 있습니다. 덮어쓸까요?", False):
            return False
    if add_tab is not None and existing:
        session = existing.get("session") or project.name
        tabs = dict(existing.get("tabs") or {})
        flows = dict(existing.get("flows") or {})
    else:
        session = _q(ask, "세션(프로젝트) 이름", project.name,
                     lambda a: None if re.match(r"^[A-Za-z0-9_.-]+$", a) else "영문/숫자/._-")
        tabs, flows = {}, {}
    def used_names() -> set[str]:
        return {n for t in tabs.values() if isinstance(t, dict)
                for n in list(t.get("agents") or {}) + list(t.get("shells") or {})}

    new_flows: dict[str, list[str]] = {}  # tabs created in this session -> flows generated for them

    def add_one(default: str | None, flow_prompt: str) -> None:
        name, tab = ask_tab(ask, set(tabs), used_names(), default)
        new_flows[name] = []
        if _yes(ask, flow_prompt, True):
            gen = default_flow(name, tab) or {}
            flows.update(gen)
            new_flows[name] = list(gen)
        tabs[name] = tab

    first_default = add_tab or None
    while True:
        add_one(first_default, "기본 flow(구현→테스트→빌드→검토)를 만들까요?")
        first_default = None
        if add_tab is not None or not _yes(ask, "탭을 더 추가할까요?", False):
            break
    while True:
        data = build_config(session, tabs, flows)
        kw = {"which": which} if which else {}
        cfg, issues = check_data(data, root=project.root, **kw)
        write_config(project, data)
        print(f"✔ {project.config_path} 생성")
        print("검사 중... " + format_issues(issues).splitlines()[-1])
        errors = [i for i in issues if i.level == "error"]
        for i in issues:
            print("  " + str(i))
        if not errors:
            return True

        def owner(i) -> str | None:
            # the new tab an error belongs to (its tab section or one of its generated flows)
            parts = i.path.split(".")
            if parts[0] == "tabs" and len(parts) > 1 and parts[1] in new_flows:
                return parts[1]
            if parts[0] == "flows" and len(parts) > 1:
                return next((t for t, fl in new_flows.items() if parts[1].split("[")[0] in fl), None)
            return None
        bad_tabs = {owner(i) for i in errors} - {None}
        if not bad_tabs:
            print("  ✘ 새로 입력한 탭 밖의 오류입니다. config.yaml을 직접 고친 뒤 hc check로 확인하세요")
            return False
        if not _yes(ask, "오류가 있는 탭을 다시 입력할까요?", True):
            return False
        for bt in sorted(bad_tabs):
            del tabs[bt]
            for fname in new_flows.pop(bt):
                flows.pop(fname, None)
            print(f"--- 탭 {bt} 다시 입력 ---")
            add_one(bt, "기본 flow를 만들까요?")
