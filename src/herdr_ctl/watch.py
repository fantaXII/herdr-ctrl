"""Config/role change detection (`hc watch`, `hc changes`) and agent restart (`hc restart`)."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .config import check_file, format_issues
from .herdr import READY_STATES, HerdrError, agent_status
from .state import State, acquire, digest, read_json, release, write_json
from .up import Ctx, flows_fingerprint, fingerprint, start_agent, up

HANDOFF_PROMPT = ("[hc] 곧 새 설정으로 다시 시작됩니다. 지금까지의 작업 상태(완료한 것, 진행 중인 것, "
                  "다음에 할 일, 주의점)를 이 파일에 요약해 쓰세요: {path}")


@dataclass
class Change:
    name: str
    type: str  # role | restart | add | remove | flow
    detail: str

    @property
    def needs_restart(self) -> bool:
        return self.type == "restart"


def detect(ctx: Ctx) -> list[Change]:
    applied = ctx.state["applied"]
    changes: list[Change] = []
    names = set()
    for t in ctx.cfg.tabs:
        for m in t.members:
            names.add(m.name)
            old = applied.get(m.name)
            new = fingerprint(ctx.project, m)
            if old is None:
                changes.append(Change(m.name, "add", f"새 {m.type} (탭 {t.name})"))
                continue
            diffs = [k for k in ("kind", "model", "args", "permission", "type", "tab", "cwd")
                     if old.get(k) != new.get(k)]
            if diffs:
                detail = ", ".join(f"{k} {old.get(k)!r} → {new.get(k)!r}" for k in diffs)
                changes.append(Change(m.name, "restart", detail + " (재시작 필요)"))
            elif m.is_agent and old.get("role") != new.get("role"):
                changes.append(Change(m.name, "role", "역할 변경"))
    for name in applied:
        if name not in names:
            changes.append(Change(name, "remove", "설정에서 삭제됨"))
    if ctx.state.get("applied_flows") and ctx.state["applied_flows"] != flows_fingerprint(ctx.cfg):
        changes.append(Change("flows", "flow", "flow 변경 (다음 hc run부터 적용)"))
    return changes


# --------------------------------------------------------------- applying
def notify_role(ctx: Ctx, name: str) -> str:
    """Tell a live agent its role changed (agent must be idle)."""
    m = ctx.cfg.member(name)
    from .up import brief_for
    brief = brief_for(ctx, m)
    st = agent_status(ctx.h, name)
    if st not in READY_STATES:
        raise RuntimeError(f"'{name}' 상태가 {st}라 지금은 알릴 수 없습니다")
    acquire(ctx.project, name, "role-apply")
    try:
        ctx.h.agent_prompt(name, f"[hc] 역할이 변경됐습니다. {brief} 파일을 다시 읽고 새 역할을 따르세요. "
                                 "확인했으면 '확인'이라고만 답하세요.", wait=True, timeout_ms=120000)
    finally:
        release(ctx.project, name, "role-apply")
    ctx.state["applied"][name] = fingerprint(ctx.project, m)
    ctx.state.save()
    return f"{name}: 새 역할을 알렸습니다"


def _wait_shell(ctx: Ctx, pane: str, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ctx.h.pane_shell_idle(pane):
            return True
        time.sleep(0.5)
    return False


def restart(ctx: Ctx, name: str, handoff: bool = True, wait_working: bool = True,
            timeout_s: float = 1800) -> str:
    m = ctx.cfg.member(name)
    if m is None or not m.is_agent:
        raise KeyError(f"'{name}'는 설정된 agent가 아닙니다")
    rec = ctx.state.member(name)
    if not rec:
        raise RuntimeError(f"'{name}'의 창이 없습니다 (hc up)")
    pane = rec["pane_id"]
    acquire(ctx.project, name, "restart")
    try:
        st = agent_status(ctx.h, name)
        if st == "blocked":
            raise RuntimeError(f"'{name}'가 승인 대기 중이라 재시작하지 않습니다. 화면에서 먼저 처리하세요")
        if st is not None and st not in READY_STATES:
            if not wait_working:
                raise RuntimeError(f"'{name}'가 작업 중({st})입니다")
            res = ctx.h.agent_wait(name, timeout_ms=int(timeout_s * 1000))
            st = (res.get("agent") or {}).get("agent_status")
            if st == "blocked":
                raise RuntimeError(f"'{name}'가 승인 대기 중이라 재시작하지 않습니다")
        handoff_path = None
        if handoff and st is not None:
            ctx.project.handoff_dir.mkdir(parents=True, exist_ok=True)
            handoff_path = (ctx.project.handoff_dir / f"{name}.md").resolve()
            handoff_path.unlink(missing_ok=True)
            try:
                ctx.h.agent_prompt(name, HANDOFF_PROMPT.format(path=handoff_path), wait=True,
                                   timeout_ms=int(timeout_s * 1000))
            except HerdrError as e:
                raise RuntimeError(f"인수인계 요청 실패: {e}") from e
            if not (handoff_path.exists() and handoff_path.read_text().strip()):
                raise RuntimeError(f"인수인계 파일이 작성되지 않았습니다: {handoff_path} "
                                   "(인수인계 없이 하려면 --no-handoff)")
        if st is not None:
            _stop_agent(ctx, name, pane)
        if not ctx.h.pane_shell_idle(pane):
            raise RuntimeError(f"'{name}' 창이 쉘 프롬프트로 돌아오지 않았습니다")
        why = start_agent(ctx, m, pane, handoff_path)
        ctx.state.save()
    finally:
        release(ctx.project, name, "restart")
    _tell_orchestrator(ctx, m, f"[hc] '{name}'가 새 설정으로 재시작됐습니다." +
                       (f" 인수인계: {handoff_path}" if handoff_path else ""))
    if why:
        return f"{name}: 재시작했지만 확인 필요 — {why}"
    return f"{name}: 재시작 완료" + (f" (인수인계 {handoff_path.name})" if handoff_path else "")


def _stop_agent(ctx: Ctx, name: str, pane: str) -> None:
    h = ctx.h
    try:
        h.agent_prompt(name, "/exit", wait=False)
    except HerdrError:
        pass
    if _wait_shell(ctx, pane, 15):
        return
    for _ in range(2):
        try:
            h.pane_send_keys(pane, "ctrl+c")
        except HerdrError:
            pass
        if _wait_shell(ctx, pane, 5):
            return


def _tell_orchestrator(ctx: Ctx, m, text: str) -> None:
    tab = ctx.cfg.tab(m.tab)
    if not tab or not tab.orchestrator or tab.orchestrator.name == m.name:
        return
    oname = tab.orchestrator.name
    try:
        if agent_status(ctx.h, oname) in READY_STATES:
            ctx.h.agent_prompt(oname, text, wait=False)
            return
    except HerdrError:
        pass
    ctx.h.notify("hc: orchestrator에게 전달 못 함", text)


def remove_member(ctx: Ctx, name: str) -> str:
    rec = ctx.state.member(name)
    if rec:
        try:
            ctx.h.pane_close(rec["pane_id"])
        except HerdrError:
            pass
    ctx.state["members"].pop(name, None)
    ctx.state["applied"].pop(name, None)
    ctx.state.save()
    return f"{name}: 창을 닫았습니다"


def apply_change(ctx: Ctx, ch: Change, action: str, handoff: bool = True) -> str:
    """action: notify | restart | add | remove | skip | ack"""
    if action == "skip":
        return f"{ch.name}: 건너뜀"
    if ch.type == "flow":
        ctx.state["applied_flows"] = flows_fingerprint(ctx.cfg)
        ctx.state.save()
        return "flow 변경 확인"
    if action == "notify":
        return notify_role(ctx, ch.name)
    if action == "restart":
        return restart(ctx, ch.name, handoff=handoff)
    if action == "add":
        m = ctx.cfg.member(ch.name)
        rep = up(ctx, tab=m.tab, start_watch=False)
        return f"{ch.name}: 추가됨 — " + "; ".join(rep.lines())
    if action == "remove":
        return remove_member(ctx, ch.name)
    raise ValueError(action)


def default_actions(ch: Change) -> list[str]:
    return {"role": ["notify", "restart", "skip"], "restart": ["restart", "skip"],
            "add": ["add", "skip"], "remove": ["remove", "skip"], "flow": ["ack"]}[ch.type]


# ------------------------------------------------------------------ watch
def pending_path(project):
    return project.var_dir / "pending_changes.json"


def save_pending(project, changes: list[Change]) -> None:
    write_json(pending_path(project), [asdict(c) for c in changes])


def load_pending(project) -> list[dict]:
    return read_json(pending_path(project), []) or []

def _signature(project) -> str:
    parts = []
    for p in [project.config_path, *sorted(project.roles_dir.glob("*.md"))] if project.roles_dir.exists() \
            else [project.config_path]:
        try:
            parts.append(f"{p}:{p.stat().st_mtime_ns}")
        except FileNotFoundError:
            parts.append(f"{p}:-")
    return digest("|".join(parts))


def watch_once(project, h, last_sig: str | None, which=None) -> tuple[str, list[Change] | None, str | None]:
    """One poll. Returns (signature, changes or None if unchanged, error text)."""
    sig = _signature(project)
    if sig == last_sig:
        return sig, None, None
    kw = {"which": which} if which else {}
    cfg, issues = check_file(project.config_path, **kw)
    if cfg is None:
        return sig, [], format_issues(issues)
    ctx = Ctx(project, cfg, h, State(project))
    changes = detect(ctx)
    save_pending(project, changes)  # separate file: never rewrites state.json concurrently
    return sig, changes, None


def watch_loop(project, h, interval: float = 2.0, iterations: int | None = None) -> None:
    sig = _signature(project)
    notified: str | None = None
    n = 0
    print(f"[hc watch] {project.flow_dir} 감시 중 (간격 {interval}s)", flush=True)
    while iterations is None or n < iterations:
        n += 1
        time.sleep(interval)
        sig2, changes, err = watch_once(project, h, sig)
        if changes is None:
            continue
        sig = sig2
        if err:
            print("[hc watch] 설정 오류:\n" + err, flush=True)
            h.notify("hc: 설정 오류 (반영 안 함)", err.splitlines()[0])
            continue
        if not changes:
            continue
        key = digest(str([asdict(c) for c in changes]))
        if key == notified:
            continue
        notified = key
        text = ", ".join(f"{c.name} {c.detail}" for c in changes)
        print(f"[hc watch] 변경 감지: {text}", flush=True)
        h.notify("hc: 설정 변경 감지", text[:200] + " — 'hc changes'로 확인")
