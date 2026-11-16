"""Actor detection and orchestrator limits (enforced by hc, not by the LLM)."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import TabCfg, orch_name
from .herdr import HerdrError, agent_status
from .project import state_home
from .state import digest, file_lock, read_json, write_json


class LimitExceeded(Exception):
    pass


class ScopeError(Exception):
    pass


@dataclass
class Actor:
    kind: str  # "user" | "orch" | "agent" | "foreign"
    name: str | None = None
    tab: str | None = None

    def __str__(self) -> str:
        return self.name or "user"


# ------------------------------------------------------------ pane registry
# Which herdr pane belongs to which project member. Kept in the private state home (outside every
# agent cwd), because .herdr-flow/state.json is writable by auto-edit agents: identity must not
# be derivable from files an agent can edit.
def registry_path() -> Path:
    return state_home() / "panes.json"


def _session_from_env(env: dict) -> str | None:
    if env.get("HERDR_SESSION"):
        return env["HERDR_SESSION"]
    m = re.search(r"/sessions/([^/]+)/", env.get("HERDR_SOCKET_PATH", ""))
    return m.group(1) if m else None


def _ancestor_envs(max_depth: int = 30):
    """Environments of our ancestor processes (Linux /proc). A process can drop a variable from
    its own environment (`env -u HERDR_PANE_ID hc ...`) but not from its parents'."""
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            return
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return
        env = {}
        for item in raw.split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep:
                env[k.decode(errors="replace")] = v.decode(errors="replace")
        yield env
        try:
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            return


def pane_identity() -> tuple[str | None, str] | None:
    """(session, pane_id) of the herdr pane we run in: own env first, else nearest ancestor."""
    if os.environ.get("HERDR_PANE_ID"):
        return _session_from_env(os.environ), os.environ["HERDR_PANE_ID"]
    for env in _ancestor_envs():
        if env.get("HERDR_PANE_ID"):
            return _session_from_env(env), env["HERDR_PANE_ID"]
    return None


def _load_registry() -> dict:
    return read_json(registry_path()) or {}


def register_panes(ctx) -> None:
    """Record this project's members' panes (called by hc up/down); drops this project's stale rows."""
    root = str(ctx.project.root.resolve())
    session = ctx.cfg.session
    with file_lock(registry_path().with_suffix(".lock")):
        reg = {k: v for k, v in _load_registry().items() if v.get("root") != root}
        for name, rec in ctx.state["members"].items():
            m = ctx.cfg.member(name)
            if m is None or not rec.get("pane_id"):
                continue
            reg[f"{session}|{rec['pane_id']}"] = {"root": root, "member": name, "type": m.type, "tab": m.tab}
        write_json(registry_path(), reg)


def registered_member(session: str, pane: str) -> dict | None:
    return _load_registry().get(f"{session}|{pane}")


def current_actor(ctx) -> Actor:
    """Who is calling hc? Decided by the herdr pane we run in, looked up in the private registry.

    - pane not managed by any hc project → a person (user)
    - pane managed by THIS project → that member (shell panes are people → user)
    - pane managed by ANOTHER project → "foreign": refused for anything privileged
    """
    ident = pane_identity()
    if ident is None:
        return Actor("user")
    session, pane = ident
    entry = registered_member(session or ctx.cfg.session, pane)  # herdr panes carry HERDR_SESSION
    by_registry = _actor_from_entry(ctx, entry) if entry else Actor("user")
    # state.json (inside the project) can only ever make the caller MORE restricted: it covers
    # sessions started before the registry existed, and forging it gains nothing.
    by_state = Actor("user")
    if (session or ctx.cfg.session) == ctx.cfg.session:
        name = ctx.state.member_by_pane(pane)
        m = ctx.cfg.member(name) if name else None
        if m is not None and m.type != "shell":
            by_state = Actor("orch" if m.type == "orch" else "agent", m.name, m.tab)
    return max(by_registry, by_state, key=lambda a: _RANK[a.kind])


_RANK = {"user": 0, "orch": 1, "agent": 2, "foreign": 3}


def _actor_from_entry(ctx, entry: dict) -> Actor:
    if entry.get("root") != str(ctx.project.root.resolve()):
        return Actor("foreign", entry.get("member"), entry.get("tab"))
    if entry.get("type") == "shell":
        return Actor("user")
    m = ctx.cfg.member(entry.get("member") or "")
    if m is None:
        return Actor("agent", entry.get("member"), entry.get("tab"))  # removed from config: least privilege
    return Actor("orch" if m.type == "orch" else "agent", m.name, m.tab)


def _budget_path(ctx, tab: TabCfg):
    return ctx.project.secure_dir / f"ask-{tab.name}.json"


def _budget_lock(ctx, tab: TabCfg):
    return file_lock(_budget_path(ctx, tab).with_suffix(".lock"))


def start_ask(ctx, tab: TabCfg) -> None:
    with _budget_lock(ctx, tab):
        write_json(_budget_path(ctx, tab), {"started": time.time(), "steps": 0, "repeats": {}})


def enforce(ctx, actor: Actor, targets: list[str], prompt: str | None) -> None:
    """Called before hc send/run. No-op for the user; checks scope + budget for an orchestrator."""
    if actor.kind == "foreign":
        raise ScopeError("다른 프로젝트의 agent 창에서는 이 프로젝트를 조작할 수 없습니다")
    if actor.kind == "agent":
        raise ScopeError("작업 agent는 hc send/run으로 다른 agent에게 지시할 수 없습니다")
    if actor.kind != "orch":
        return
    tab = ctx.cfg.tab(actor.tab)
    foreign = [t for t in targets if (ctx.cfg.member(t) is None or ctx.cfg.member(t).tab != tab.name)]
    if foreign:
        raise ScopeError(f"다른 탭의 agent에게는 지시할 수 없습니다: {', '.join(foreign)}")
    if actor.name in targets:
        raise ScopeError("orchestrator 자신에게는 지시할 수 없습니다")
    path = _budget_path(ctx, tab)
    with _budget_lock(ctx, tab):  # parallel tool calls must not both pass the same budget check
        budget = read_json(path) or {"started": time.time(), "steps": 0, "repeats": {}}
        lim = tab.limits
        minutes = (time.time() - budget["started"]) / 60
        if budget["steps"] >= lim["max_steps_per_ask"]:
            _limit(ctx, tab, f"hc ask 1회당 지시 상한 {lim['max_steps_per_ask']}회에 도달했습니다")
        if minutes >= lim["max_minutes_per_ask"]:
            _limit(ctx, tab, f"hc ask 1회당 시간 상한 {lim['max_minutes_per_ask']}분에 도달했습니다")
        if prompt is not None:
            for t in targets:
                key = f"{t}:{digest(prompt.strip())}"
                if budget["repeats"].get(key, 0) >= lim["max_retries_per_agent"]:
                    _limit(ctx, tab, f"'{t}'에게 같은 지시를 {lim['max_retries_per_agent']}회 반복했습니다")
                budget["repeats"][key] = budget["repeats"].get(key, 0) + 1
        budget["steps"] += 1
        write_json(path, budget)


def consume_answer(ctx, actor: Actor, tab: TabCfg) -> None:
    """Count one `hc answer` by an orchestrator against max_answers_per_ask (hc enforces it)."""
    path = _budget_path(ctx, tab)
    with _budget_lock(ctx, tab):
        budget = read_json(path) or {"started": time.time(), "steps": 0, "repeats": {}}
        used = budget.get("answers", 0)
        cap = tab.policy["max_answers_per_ask"]
        if used >= cap:
            _limit(ctx, tab, f"hc ask 1회당 답변 상한 {cap}회에 도달했습니다")
        budget["answers"] = used + 1
        write_json(path, budget)


def guard(ctx, actor: Actor, command: str, targets: list[str] | None = None,
          budget: bool = False) -> None:
    """Gate for hc commands other than send/run. Users may do anything; work agents may only read;
    an orchestrator may act on its own tab only (optionally consuming its ask budget)."""
    if actor.kind == "user":
        return
    if actor.kind == "foreign":
        raise ScopeError(f"다른 프로젝트의 agent 창에서는 'hc {command}'를 사용할 수 없습니다")
    if actor.kind == "agent":
        raise ScopeError(f"작업 agent는 'hc {command}'를 사용할 수 없습니다")
    if targets is None:
        raise ScopeError(f"orchestrator는 'hc {command}'를 사용할 수 없습니다 (사용자 전용)")
    if budget:
        enforce(ctx, actor, targets, None)
        return
    tab = ctx.cfg.tab(actor.tab)
    foreign = [t for t in targets if ctx.cfg.member(t) is None or ctx.cfg.member(t).tab != tab.name]
    if foreign:
        raise ScopeError(f"다른 탭 대상에는 'hc {command}'를 쓸 수 없습니다: {', '.join(foreign)}")


def _limit(ctx, tab: TabCfg, msg: str) -> None:
    ctx.h.notify(f"hc: 탭 {tab.name} orchestrator 상한", msg)
    raise LimitExceeded(msg + ". 사용자에게 상황을 보고하고 멈추세요. "
                        "(상한은 사용자가 hc ask로 새 지시를 줄 때 초기화됩니다)")


def ask(ctx, tab_name: str, text: str) -> None:
    tab = ctx.cfg.tab(tab_name)
    if tab is None:
        raise KeyError(f"탭 '{tab_name}'이 설정에 없습니다")
    if tab.orchestrator is None:
        raise RuntimeError(f"탭 '{tab.name}'에는 orchestrator가 없습니다 (orchestrator: none). "
                           f"정해진 순서 실행은 hc run <flow>를 쓰세요")
    name = orch_name(tab.name)
    st = agent_status(ctx.h, name)
    if st is None:
        raise RuntimeError(f"orchestrator '{name}'가 실행 중이 아닙니다 (hc up {tab.name})")
    if st == "blocked":
        raise RuntimeError(f"orchestrator '{name}'가 승인 대기 중입니다. 화면에서 먼저 처리하세요")
    if st not in ("idle", "done"):
        raise RuntimeError(f"orchestrator '{name}'가 작업 중({st})입니다. 끝난 뒤 다시 지시하세요")
    start_ask(ctx, tab)
    try:
        ctx.h.agent_prompt(name, text, wait=False)
    except HerdrError as e:
        raise RuntimeError(f"orchestrator에게 전달 실패: {e}") from e
