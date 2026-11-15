"""Actor detection and orchestrator limits (enforced by hc, not by the LLM)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .config import TabCfg, orch_name
from .herdr import HerdrError, agent_status
from .state import digest, read_json, write_json


class LimitExceeded(Exception):
    pass


class ScopeError(Exception):
    pass


@dataclass
class Actor:
    kind: str  # "user" | "orch" | "agent"
    name: str | None = None
    tab: str | None = None

    def __str__(self) -> str:
        return self.name or "user"


def current_actor(ctx) -> Actor:
    """Who is calling hc? Decided by the herdr pane we run in (HERDR_PANE_ID)."""
    # The orchestrator's allowed Bash prefix is `hc` (Bash(hc:*)), so it cannot launch hc with a
    # modified environment (e.g. `env -u HERDR_PANE_ID hc ...`).
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return Actor("user")
    name = ctx.state.member_by_pane(pane)
    if not name:
        return Actor("user")
    m = ctx.cfg.member(name)
    if m is None:
        return Actor("user")
    if m.type == "shell":
        return Actor("user")  # a person typing in a shell pane
    return Actor("orch" if m.type == "orch" else "agent", name, m.tab)


def _budget_path(ctx, tab: TabCfg):
    return ctx.project.var_dir / f"ask-{tab.name}.json"


def start_ask(ctx, tab: TabCfg) -> None:
    write_json(_budget_path(ctx, tab), {"started": time.time(), "steps": 0, "repeats": {}})


def enforce(ctx, actor: Actor, targets: list[str], prompt: str | None) -> None:
    """Called before hc send/run. No-op for the user; checks scope + budget for an orchestrator."""
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


def guard(ctx, actor: Actor, command: str, targets: list[str] | None = None,
          budget: bool = False) -> None:
    """Gate for hc commands other than send/run. Users may do anything; work agents may only read;
    an orchestrator may act on its own tab only (optionally consuming its ask budget)."""
    if actor.kind == "user":
        return
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
    raise LimitExceeded(msg + ". 사용자에게 상황을 보고하고 멈추세요.")


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
