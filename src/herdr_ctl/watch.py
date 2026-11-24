"""Config/role change detection (`hctrl watch`, `hctrl changes`) and agent restart (`hctrl restart`)."""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from typing import Callable

import yaml

from .config import check_file, format_issues
from .herdr import READY_STATES, HerdrError, agent_status
from .state import State, acquire, digest, read_json, release, write_json
from .up import Ctx, flows_fingerprint, fingerprint, hc_command, start_agent, up

HANDOFF_PROMPT = ("[hctrl] 곧 새 설정으로 다시 시작됩니다. 지금까지의 작업 상태(완료한 것, 진행 중인 것, "
                  "다음에 할 일, 주의점)를 이 파일에 요약해 쓰세요. 전에 쓴 적이 있어도 지금 상태로 "
                  "다시 써야 합니다 (파일을 새로 만드세요): {path}")


@dataclass
class Change:
    name: str
    type: str  # role | restart | relayout | add | remove | flow
    detail: str

    @property
    def needs_restart(self) -> bool:
        return self.type == "restart"


LAYOUT_KEYS = ("type", "tab", "cwd", "tab_cwd", "worktree")


def detect(ctx: Ctx, tab: str | None = None) -> list[Change]:
    """Config changes not applied to the running session yet (archived tabs are not watched).
    `tab`: only that tab's members."""
    applied = ctx.state["applied"]
    changes: list[Change] = []
    names = set()
    for t in ctx.cfg.open_tabs:
        if tab is not None and t.name.lower() != tab.lower():
            names.update(m.name for m in t.members)
            continue
        for m in t.members:
            names.add(m.name)
            old = applied.get(m.name)
            new = fingerprint(ctx.project, m)
            if old is None:
                changes.append(Change(m.name, "add", f"새 {m.type} (탭 {t.name})"))
                continue
            diffs = [k for k in ("kind", "model", "effort", "primary_agent", "args", "permission", "type", "tab", "cwd", "unattended",
                                 "tab_cwd", "worktree")
                     if old.get(k) != new.get(k)
                     and not (k in ("tab_cwd", "worktree") and "v" not in old)]  # pre-upgrade: never recorded
            detail = ", ".join(f"{k} {old.get(k)!r} → {new.get(k)!r}" for k in diffs)
            # Pane placement (tab, cwd, worktree) and shells can't change in place: restart reuses the pane.
            if diffs and (m.type == "shell" or old.get("type") == "shell"
                          or any(k in LAYOUT_KEYS for k in diffs)):
                changes.append(Change(m.name, "relayout",
                                      detail + f" (재배치 필요: hctrl down {t.name} && hctrl up {t.name})"))
            elif diffs:
                changes.append(Change(m.name, "restart", detail + " (재시작 필요)"))
            elif m.is_agent and old.get("role") != new.get("role"):
                changes.append(Change(m.name, "role", "역할 변경"))
    for name in applied:
        was = (ctx.state.member(name) or {}).get("tab")
        if name not in names and (tab is None or (was or "").lower() == tab.lower()):
            changes.append(Change(name, "remove", "설정에서 삭제됨"))
    if tab is None and ctx.state.get("applied_flows") and ctx.state["applied_flows"] != flows_fingerprint(ctx.cfg):
        changes.append(Change("flows", "flow", "flow 변경 (다음 hctrl run부터 적용)"))
    return changes


# --------------------------------------------------------------- applying
def notify_role(ctx: Ctx, name: str) -> str:
    """Tell a live agent its role changed (agent must be idle)."""
    m = ctx.cfg.member(name)
    from .up import brief_for, seal_brief
    seal_brief(ctx, m)  # the copy a fresh conversation / hand-started agent reads its role from
    brief = brief_for(ctx, m)
    st = agent_status(ctx.h, name)
    if st not in READY_STATES:
        raise RuntimeError(f"'{name}' 상태가 {st}라 지금은 알릴 수 없습니다")
    acquire(ctx.project, name, "role-apply")
    try:
        ctx.h.agent_prompt(name, f"[hctrl] 역할이 변경됐습니다. {brief} 파일을 다시 읽고 새 역할을 따르세요. "
                                 "확인했으면 '확인'이라고만 답하세요.", wait=True, timeout_ms=120000)
    finally:
        release(ctx.project, name, "role-apply")
    ctx.state["applied"][name] = fingerprint(ctx.project, m)
    ctx.state.save()
    return f"{name}: 새 역할을 알렸습니다"


STALLED_IDLE_S = 90.0  # a stalled handoff prompt: give up if the agent stays idle with no file this long
_sleep = time.sleep    # tests replace these two
_now = time.time


class HandoffMissing(RuntimeError):
    """The agent answered the handoff prompt (turned=True) or never started on it, without a file."""

    def __init__(self, msg: str, turned: bool):
        super().__init__(msg)
        self.turned = turned


def _missing(name: str, path, turned: bool) -> HandoffMissing:
    return HandoffMissing(f"인수인계 파일이 작성되지 않았습니다: {path} (인수인계 없이 재시작하려면 "
                          f"변경 반영에서 [f] 또는 hctrl restart {name} --no-handoff)", turned)


def _await_handoff(ctx: Ctx, name: str, path, timeout_s: float) -> None:
    """herdr's `prompt --wait` gives up (agent_prompt_stalled) when the agent does not LOOK busy
    within 5 s, but the prompt was already typed in. Wait for the file and for the turn to end."""
    start = _now()
    seen_busy = False
    idle_since = None
    while _now() - start < timeout_s:
        st = agent_status(ctx.h, name)
        if st == "blocked":
            raise RuntimeError(f"'{name}'가 인수인계 중 승인 대기에 걸렸습니다. 화면에서 처리한 뒤 다시 반영하세요")
        if st is None:
            raise RuntimeError(f"'{name}'가 인수인계 중에 종료됐습니다")
        seen_busy = seen_busy or st not in READY_STATES
        written = path.exists() and path.read_text().strip()
        if written and st in READY_STATES:
            return
        if not written and not seen_busy and _now() - start > STALLED_IDLE_S:
            break  # the prompt never started a turn
        if not written and seen_busy and st in READY_STATES:  # the turn ended without writing it
            idle_since = idle_since or _now()
            if _now() - idle_since > 5:
                raise _missing(name, path, turned=True)
        else:
            idle_since = None
        _sleep(1.0)
    raise _missing(name, path, turned=seen_busy)


def _wait_agent_gone(ctx: Ctx, name: str, timeout_s: float = 10.0) -> bool:
    """The shell prompt can be back a moment before herdr notices the agent exited; starting the
    new agent in that gap is refused. Wait until herdr no longer lists it."""
    deadline = time.time() + timeout_s
    while True:
        try:
            if not any(a.get("name") == name for a in ctx.h.agent_list()):
                return True
        except HerdrError:
            pass
        if time.time() >= deadline:
            return False
        _sleep(0.3)


def _wait_shell(ctx: Ctx, pane: str, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ctx.h.pane_shell_idle(pane):
            return True
        time.sleep(0.5)
    return False


def collect_handoff(ctx: Ctx, name: str, timeout_s: float = 1800) -> Path:
    """Ask live agent `name` (idle) to write its handoff file and wait for it. Raises HandoffMissing
    (or RuntimeError) when no file came."""
    hp = (ctx.project.handoff_dir / f"{name}.md").resolve()
    ctx.project.handoff_dir.mkdir(parents=True, exist_ok=True)
    # an earlier handoff of this agent: it may answer "already written, nothing changed"
    previous = hp.read_text() if hp.exists() else ""
    hp.unlink(missing_ok=True)
    turned = True  # `prompt --wait` returned: the agent finished a turn on it
    try:
        try:
            ctx.h.agent_prompt(name, HANDOFF_PROMPT.format(path=hp), wait=True,
                               timeout_ms=int(timeout_s * 1000))
        except HerdrError as e:
            if e.code != "agent_prompt_stalled":
                raise RuntimeError(f"인수인계 요청 실패: {e}") from e
            _await_handoff(ctx, name, hp, timeout_s)
    except HandoffMissing as e:
        turned = e.turned
    if not (hp.exists() and hp.read_text().strip()):
        if not (turned and previous.strip()):
            raise _missing(name, hp, turned)
        hp.write_text(previous)  # the agent kept its earlier handoff as current
    return hp


def restart(ctx: Ctx, name: str, handoff: bool = True, wait_working: bool = True,
            timeout_s: float = 1800, resume: bool = False) -> str:
    """Stop agent `name` and start it again in the same pane with the current config.
    resume: continue the same conversation (reloads skills/MCP/settings, keeps the context); when the
    agent's CLI cannot, falls back to a handoff (or a fresh start with handoff=False)."""
    m = ctx.cfg.member(name)
    if m is None or not m.is_agent:
        raise KeyError(f"'{name}'는 설정된 agent가 아닙니다")
    rec = ctx.state.member(name)
    if not rec:
        raise RuntimeError(f"'{name}'의 창이 없습니다 (hctrl up)")
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
        from .up import live_session, resume_id, update_sessions
        rid = resume_id(ctx, m, live_session(ctx, name)) if resume and st is not None else None
        if rid:
            update_sessions(ctx.project, lambda rec: rec.update({name: {"agent": m.kind, "id": rid, "at": time.time()}}))
        handoff_path = None
        hp = (ctx.project.handoff_dir / f"{name}.md").resolve()
        if handoff and st is not None and not rid:
            handoff_path = collect_handoff(ctx, name, timeout_s)
        elif handoff and st is None and hp.exists() and hp.read_text().strip() \
                and hp.stat().st_mtime > float(rec.get("started_at") or 0):
            handoff_path = hp  # an earlier attempt got the handoff written, then failed to start
        if st is not None:
            _stop_agent(ctx, name, pane)
        elif not ctx.h.pane_shell_idle(pane):
            _stop_stray_agent(ctx, pane)  # an agent CLI herdr lost track of (or started by hand)
        if not ctx.h.pane_shell_idle(pane):
            running = ", ".join(_foreground(ctx, pane)) or "다른 프로그램"
            raise RuntimeError(f"'{name}' 창이 쉘 프롬프트로 돌아오지 않았습니다 ({running} 실행 중). "
                               "창에서 직접 종료한 뒤 다시 반영하세요")
        _wait_agent_gone(ctx, name)
        why = start_agent(ctx, m, pane, handoff_path, resume=rid)
        if why and why.startswith("시작 실패"):  # one more try: herdr may still be settling the pane
            _sleep(2.0)
            if ctx.h.pane_shell_idle(pane):
                _wait_agent_gone(ctx, name)
                why = start_agent(ctx, m, pane, handoff_path, resume=rid)
        ctx.state.save()
        if why and why.startswith("시작 실패"):
            raise RuntimeError(f"'{name}' {why}. 창은 쉘 상태입니다. 다시 반영하면 "
                               + ("이미 써 둔 인수인계 파일로 " if handoff_path else "") + "시작합니다")
    finally:
        release(ctx.project, name, "restart")
    _tell_orchestrator(ctx, m, f"[hctrl] '{name}'가 " + ("같은 대화로 다시 시작됐습니다 (설정·skill·MCP 다시 불러옴)."
                                                         if rid else "새 설정으로 재시작됐습니다.") +
                       (f" 인수인계: {handoff_path}" if handoff_path else ""))
    how = " (대화 유지)" if rid else (f" (인수인계 {handoff_path.name})" if handoff_path else "")
    if resume and not rid and st is not None:
        how += " — 이어 갈 대화를 찾지 못해 " + ("인수인계로 새로 시작" if handoff_path else "새로 시작")
    if why:
        return f"{name}: 재시작했지만 확인 필요 — {why}"
    return f"{name}: 재시작 완료{how}"


def _stop_agent(ctx: Ctx, name: str, pane: str) -> None:
    h = ctx.h
    try:
        h.agent_prompt(name, "/exit", wait=False)
    except HerdrError:
        pass
    if _wait_shell(ctx, pane, 15):
        return
    for _ in range(2):
        _ctrl_c_burst(ctx, pane)
        if _wait_shell(ctx, pane, 5):
            return


def _ctrl_c_burst(ctx: Ctx, pane: str) -> None:
    """Ctrl+C pressed in quick succession: claude quits only on a second press within about a
    second (the first interrupts a turn or clears the input); one press every few seconds never
    gets it out."""
    for i in range(3):
        if i:
            _sleep(0.3)
            if ctx.h.pane_shell_idle(pane):
                return
        try:
            ctx.h.pane_send_keys(pane, "ctrl+c")
        except HerdrError:
            return


def _foreground(ctx: Ctx, pane: str) -> list[str]:
    try:
        return [n for n in ctx.h.pane_foreground_names(pane) if n]
    except HerdrError:
        return []


def _stop_stray_agent(ctx: Ctx, pane: str) -> None:
    """Quit an agent CLI running in the pane that herdr does not list as an agent (herdr lost it,
    or it was started there by hand). Anything else (a build, an editor) is left alone."""
    from .herdr import AGENT_KINDS
    if not any(n in AGENT_KINDS for n in _foreground(ctx, pane)):
        return
    for _ in range(2):
        _ctrl_c_burst(ctx, pane)
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
    ctx.h.notify("hctrl: orchestrator에게 전달 못 함", text)


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
    """action: notify | restart | add | remove | skip | ack | move (relayout: the whole tab, relocate.py)"""
    if action == "skip":
        return f"{ch.name}: 건너뜀"
    if ch.type == "flow":
        ctx.state["applied_flows"] = flows_fingerprint(ctx.cfg)
        ctx.state.save()
        return "flow 변경 확인"
    if ch.type == "relayout" and action == "ack":
        m = ctx.cfg.member(ch.name)
        if m is None:
            raise KeyError(f"'{ch.name}'가 설정에 없습니다")
        ctx.state["applied"][ch.name] = fingerprint(ctx.project, m)
        ctx.state.save()
        return f"{ch.name}: 확인함 (창 배치는 hctrl down {m.tab} && hctrl up {m.tab}로 반영)"
    if ch.type == "relayout" and action == "move":
        m = ctx.cfg.member(ch.name)
        if m is None:
            raise KeyError(f"'{ch.name}'가 설정에 없습니다")
        from .relocate import relocate_tab
        return f"탭 {m.tab}: " + "; ".join(relocate_tab(ctx, m.tab))
    if action == "notify":
        return notify_role(ctx, ch.name)
    if action == "restart":
        return restart(ctx, ch.name, handoff=handoff)
    if action == "fresh":
        return restart(ctx, ch.name, handoff=False)
    if action == "add":
        m = ctx.cfg.member(ch.name)
        rep = up(ctx, tab=m.tab, start_watch=False, resume=True)  # a closed tab continues its conversations
        return f"{ch.name}: 추가됨 — " + "; ".join(rep.lines())
    if action == "remove":
        return remove_member(ctx, ch.name)
    raise ValueError(action)


def default_actions(ch: Change) -> list[str]:
    return {"role": ["notify", "restart", "fresh", "skip"], "restart": ["restart", "fresh", "skip"],
            "relayout": ["move", "ack", "skip"],
            "add": ["add", "skip"], "remove": ["remove", "skip"], "flow": ["ack"]}[ch.type]


# ------------------------------------------------------------------ watch
def pending_path(project):
    return project.var_dir / "pending_changes.json"


def save_pending(project, changes: list[Change]) -> None:
    write_json(pending_path(project), [asdict(c) for c in changes])


def load_pending(project) -> list[dict]:
    return read_json(pending_path(project), []) or []

def _custom_role_files(project) -> list:
    """role_file paths named in config.yaml (best effort: a broken config just yields none)."""
    try:
        data = yaml.safe_load(project.config_path.read_text())
        root = project.root.resolve()
        out = set()
        for tdata in (data.get("tabs") or {}).values():
            for adata in ((tdata or {}).get("agents") or {}).values():
                rf = (adata or {}).get("role_file")
                if isinstance(rf, str) and rf:
                    p = (project.root / rf).resolve()
                    if p.is_relative_to(root):
                        out.add(p)
        return sorted(out)
    except Exception:
        return []


def _signature(project) -> str:
    parts = []
    roles = sorted(project.roles_dir.glob("*.md")) if project.roles_dir.exists() else []
    for p in [project.config_path, *roles, *_custom_role_files(project)]:
        try:
            # content, not just mtime: two quick edits can share an mtime tick (coarse fs clocks)
            parts.append(f"{p}:{p.stat().st_mtime_ns}:{digest(p.read_text(errors='replace'))}")
        except OSError:
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


MENU_LINES = ("[Tab] 대상 탭 바꾸기  [s] 설정  [c] 변경 반영  [t] 상태  [p] 질문",
              "[r] 재시작  [x] 탭 종료  [o] 탭 열기  [D] 탭 설정 삭제  [?] 도움말")
MENU = "  ".join(MENU_LINES)
MENU_COMMANDS = {"c": ["changes"], "t": ["status"], "p": ["pending"]}
HELP = """\
[hctrl] _hc 메뉴 도움말 — 맨 위 '대상'이 지금 고른 탭입니다. 키는 그 탭에만 적용됩니다.
  [Tab] / ←→ / 숫자   대상 탭 바꾸기 (0 = 전체). worktree 탭(별도 workspace)도 여기서 고릅니다.
  [s] 설정 화면(브라우저)   [c] 변경 반영   [t] 상태   [p] 답을 기다리는 질문
  [r] 재시작: 탭 전체 또는 agent 하나. 기본은 같은 대화로 이어서(설정·skill·MCP 다시 불러옴),
      [f]를 고르면 인수인계 파일을 남기고 새 대화로 시작
  [x] 1) 잠시 닫기  창만 닫음. 다시 열면 이전 대화 이어서
      2) 탭 종료    이 탭의 hctrl 세션을 끝내고 산출물(작업 폴더·인수인계·실행 기록·대화 기록)을 지움.
                    탭 설정과 prompt(역할 상세)는 남고, 다시 열면 새 세션으로 시작
      worktree는 기준 브랜치(main 등)에 모두 병합됐을 때만 정리하고, 아니면 남깁니다.
  [o] 닫힌·종료된 탭 열기
  [D] 탭 설정 삭제 (대문자 D): config.yaml에서 탭과 그 flow를 지움. prompt·작업 파일은 남음
  LLM(claude/opencode)의 대화 원본은 어떤 경우에도 지우지 않습니다.
"""


class TtyKeys:
    """Single key presses from the terminal (cbreak), with cooked mode back for interactive commands."""

    def __init__(self, fd: int | None = None):
        self.fd = sys.stdin.fileno() if fd is None else fd
        self.saved = None

    def __enter__(self) -> "TtyKeys":
        import termios
        import tty
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self._no_flow_control()
        return self

    def _no_flow_control(self) -> None:
        """Ctrl+S (XOFF) must not freeze the pane's output: turn IXON/IXOFF off."""
        import termios
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def __exit__(self, *exc) -> None:
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def __call__(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], max(timeout, 0))
        return os.read(self.fd, 1).decode(errors="ignore") if ready else None

    @contextmanager
    def cooked(self):
        import termios
        import tty
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            yield
        finally:
            tty.setcbreak(self.fd)
            self._no_flow_control()


def _width(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _clip(text: str, cols: int) -> str:
    out, w = [], 0
    for ch in text:
        cw = _width(ch)
        if w + cw > cols:
            break
        out.append(ch)
        w += cw
    return "".join(out)


class Menu:
    """Key actions of the _hc tab. The _hc pane is the user's (never an agent's), so it may open the UI.

    Keys act on the target tab shown in the header ("전체" = every tab). With `fixed`, the header stays
    pinned at the top of the pane (a terminal scroll region keeps command output below it)."""

    def __init__(self, project, cooked=None, run: Callable[[list[str]], int] = subprocess.call,
                 fixed: bool = False, ask: Callable[[str], str] = input):
        self.project = project
        self.cooked = cooked
        self.run = run
        self.ui = None
        self.fixed = fixed
        self.ask = ask
        self.target: str | None = None  # None: every tab
        self._shown: tuple | None = None
        self._esc = 0  # inside an escape sequence (arrow keys arrive as ESC [ C, one byte at a time)
        self._set_default_target()

    # ---------------------------------------------------------- target tab
    def tabs(self) -> list[dict]:
        """[{name, closed, running, worktree, changes}] from the saved config and state (no herdr call)."""
        cfg, _ = check_file(self.project.config_path, which=lambda _k: "found")
        if cfg is None:
            return []
        st = State(self.project)
        pending = load_pending(self.project)
        out = []
        for t in cfg.tabs:
            names = {m.name for m in t.members}
            running = any((st.member(n) or {}).get("pane_id") for n in names)
            out.append({"name": t.name, "closed": t.closed, "running": running,
                        "worktree": (t.worktree or {}).get("branch"),
                        "changes": sum(1 for c in pending if c.get("name") in names)})
        return out

    def _set_default_target(self) -> None:
        tabs = self.tabs()
        self.target = tabs[0]["name"] if len(tabs) == 1 else None

    def cycle(self, step: int = 1) -> None:
        names = [None] + [t["name"] for t in self.tabs()]
        i = names.index(self.target) if self.target in names else 0
        self.target = names[(i + step) % len(names)]

    def pick(self, n: int) -> None:
        tabs = self.tabs()
        if n == 0:
            self.target = None
        elif n <= len(tabs):
            self.target = tabs[n - 1]["name"]

    def header_lines(self) -> list[str]:
        tabs = self.tabs()
        names = [t["name"] for t in tabs]
        if self.target not in names:
            self.target = None
        if self.target is None:
            run = sum(1 for t in tabs if t["running"] and not t["closed"])
            arch = sum(1 for t in tabs if t["closed"])
            info = f"탭 {len(tabs)}개 · 실행 {run}" + (f" · 종료 {arch}" if arch else "")
            head = f"대상: ◀ [전체] ▶  {info}"
        else:
            t = tabs[names.index(self.target)]
            state = "■ 종료됨" if t["closed"] else ("● 실행 중" if t["running"] else "○ 닫힘")
            bits = [state] + ([f"worktree {t['worktree']}"] if t["worktree"] else []) \
                + ([f"변경 {t['changes']}건"] if t["changes"] else [])
            head = f"대상: ◀ [{names.index(self.target) + 1}/{len(names)}] {self.target} ▶  " + " · ".join(bits)
        return [head, *MENU_LINES]

    def draw(self, force: bool = False) -> None:
        lines = self.header_lines()
        key = (tuple(lines), shutil.get_terminal_size().lines if self.fixed else 0)
        if key == self._shown and not force:
            return
        self._shown = key
        if not self.fixed:
            print("\n".join(lines), flush=True)
            return
        size = shutil.get_terminal_size()
        rows, cols = size.lines, size.columns
        top = len(lines) + 1  # + a rule under the header
        out = ["\x1b7", "\x1b[r"]  # save cursor, whole screen scrolls while drawing
        for i, line in enumerate(lines + ["─" * cols]):
            out.append(f"\x1b[{i + 1};1H\x1b[2K{_clip(line, cols)}")
        out.append(f"\x1b[{top + 1};{rows}r\x1b8")  # only the lines below the header scroll
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def start(self) -> None:
        if self.fixed:
            top = len(self.header_lines()) + 2
            sys.stdout.write(f"\x1b[2J\x1b[{top};1H")
        self.draw(force=True)

    def stop(self) -> None:
        if self.fixed:
            sys.stdout.write("\x1b[r")
            sys.stdout.flush()

    # --------------------------------------------------------------- keys
    def _hctrl(self, *args: str) -> int:
        cmd = hc_command() + ["--project", str(self.project.root)] + list(args)
        with self.cooked() if self.cooked else nullcontext():
            return self.run(cmd)

    def _input(self, prompt: str) -> str:
        with self.cooked() if self.cooked else nullcontext():
            try:
                return self.ask(prompt).strip().lower()
            except EOFError:
                return ""

    def _need_tab(self) -> dict | None:
        tabs = {t["name"]: t for t in self.tabs()}
        if self.target is None or self.target not in tabs:
            print("[hctrl] 대상 탭을 먼저 고르세요: [Tab] 또는 숫자 키", flush=True)
            return None
        return tabs[self.target]

    def _escape(self, key: str) -> bool:
        """Swallow escape sequences; ←/→ move the target tab. True when the key was part of one."""
        if key == "\x1b":
            self._esc = 1
            return True
        if self._esc == 1:
            self._esc = 2 if key in "[O" else 0
            return True
        if self._esc == 2:
            if key.isdigit() or key == ";":  # parameters, e.g. ESC [ 1 ; 5 C
                return True
            self._esc = 0
            if key in "CD":
                self.cycle(1 if key == "C" else -1)
                self.draw(force=self.fixed)
            return True
        return False

    def handle(self, key: str) -> None:
        if not key or self._escape(key):
            return
        if key == "\t":
            self.cycle()
        elif key.isdigit():
            self.pick(int(key))
        elif key == "?":
            print(HELP, flush=True)
        elif key == "D":  # uppercase only: deleting settings should not be one stray keypress away
            self.delete()
        else:
            key = key.lower()
            if key == "s":
                self.settings()
            elif key in MENU_COMMANDS:
                extra = []
                if self.target is not None:
                    extra = ["--tab", self.target] if key == "c" else ([self.target] if key == "t" else [])
                self._hctrl(*MENU_COMMANDS[key], *extra)
            elif key in "rxo":
                getattr(self, {"r": "restart", "x": "finish", "o": "reopen"}[key])()
            else:
                return
        if self.fixed:
            self.draw(force=True)
        else:
            print(MENU, flush=True)
            self.draw()

    def restart(self) -> None:
        t = self._need_tab()
        if t is None:
            return
        if t["closed"] or not t["running"]:
            print(f"[hctrl] '{t['name']}' 탭은 실행 중이 아닙니다. [o]로 여세요", flush=True)
            return
        who = self._input(f"재시작: [Enter] 탭 '{t['name']}' 전체, 또는 agent 이름 > ")
        how = self._input("[Enter] 같은 대화로 이어서 (설정·skill·MCP 다시 불러옴)  [f] 새 대화 (인수인계) > ")
        target = ["--tab", t["name"]] if not who else [who]
        self._hctrl("restart", *target, *(["--fresh"] if how == "f" else []))

    def finish(self) -> None:
        t = self._need_tab()
        if t is None:
            return
        if t["closed"]:
            print(f"[hctrl] '{t['name']}'은 이미 종료된 탭입니다: [o] 다시 시작  [D] 탭 설정 삭제", flush=True)
            return
        ans = self._input(f"'{t['name']}' 탭: [1] 잠시 닫기 (다시 열면 대화 이어서)  "
                          "[2] 탭 종료 (세션 산출물 정리, 설정·prompt는 남음)  [Enter] 취소 > ")
        if ans == "1":
            self._hctrl("down", t["name"])
        elif ans == "2":
            self._hctrl("tab", "end", t["name"])  # lists what goes and what stays, then asks
        else:
            print("[hctrl] 취소했습니다 (바뀐 것 없음)", flush=True)

    def reopen(self) -> None:
        t = self._need_tab()
        if t is None:
            return
        if t["running"] and not t["closed"]:
            print(f"[hctrl] '{t['name']}' 탭은 이미 실행 중입니다", flush=True)
            return
        if t["closed"]:  # an ended tab has no conversation left to continue
            self._hctrl("tab", "reopen", t["name"], "--fresh")
            return
        fresh = self._input("[Enter] 이전 대화 이어서  [f] 새로 시작 > ") == "f"
        self._hctrl("tab", "reopen", t["name"], *(["--fresh"] if fresh else []))

    def delete(self) -> None:
        t = self._need_tab()
        if t is None:
            return
        self._hctrl("tab", "delete", t["name"])  # lists what goes and what stays, then asks

    def settings(self) -> None:
        from . import __version__, update
        from .ui import UIServer, announce
        new = update.disk_version()
        if new and new != __version__:
            # A newer hctrl is installed: pressing [s] must open that version's UI, not the old process's page.
            # Pressing it is an explicit choice, so it wins over the "UI in use" deferral in _upgrade();
            # cmd_watch re-execs and resumes the same port/cookie, so an already open page keeps working.
            raise Reexec(new, reopen_ui=True, announce=True)
        if self.ui is None:  # one server per watch process; each press mints a fresh one-time link
            self.ui = UIServer("session", cwd=self.project.root, start=self.project).start()
        announce(self.ui, self.project.name)

    def resume_ui(self, port: int, secret: str) -> bool:
        """After a watcher restart: serve the settings UI again on the same port with the same cookie,
        so a page that was open keeps working (it reloads itself into the new version)."""
        from .ui import UIServer
        try:
            self.ui = UIServer("session", cwd=self.project.root, start=self.project, port=port,
                               secret=secret).start()
        except OSError as e:
            print(f"[hctrl watch] 설정 UI를 같은 주소(포트 {port})로 다시 열지 못했습니다 ({e.strerror or e}). "
                  "[s]로 새로 여세요", flush=True)
            return False
        return True

    def close(self) -> None:
        if self.ui is not None:
            self.ui.stop()
            self.ui = None


def _pause(interval: float, keys, menu) -> None:
    if keys is None:
        time.sleep(interval)
        return
    deadline = time.monotonic() + interval
    while (left := deadline - time.monotonic()) > 0:
        key = keys(left)
        if key and menu is not None:
            menu.handle(key)


UPDATE_POLL_S = 600.0  # how often the _hc tab re-reads the (daily) update cache


def _update_notice(h, seen: str | None) -> str | None:
    """Print (and notify) a newer release once per version; refresh the daily cache in the background."""
    from . import update
    update.refresh_in_background()
    tag = update.available()
    if tag and tag != seen:
        msg = update.notice()
        print(f"[hctrl] ℹ {msg}", flush=True)
        try:
            h.notify("hctrl: 새 버전", msg)
        except Exception:  # a notice must never stop the watcher
            pass
    return tag or seen


class Reexec(Exception):
    """The installed hctrl changed under this watcher: restart it as the new version (see cmd_watch)."""

    def __init__(self, new: str, reopen_ui: bool = False, announce: bool = False):
        super().__init__(new)
        self.new = new
        self.reopen_ui = reopen_ui
        self.announce = announce  # the user pressed [s]: the new process must open a fresh link even if it resumed the UI


def _upgrade(menu: Menu | None, told: str | None) -> str | None:
    """Raise Reexec when the installed hctrl differs from this process and the settings UI is not in
    use (an open page may hold unsaved edits). Returns the version already reported as deferred."""
    from . import __version__, update
    ui = menu.ui if menu else None
    new = update.disk_version()
    if ui is not None and ui.restart_requested:  # "지금 적용" in the settings UI
        raise Reexec(new or __version__, reopen_ui=True)
    if not new or new == __version__:
        return told
    if ui is not None and ui.in_use():
        if told != new:
            print(f"[hctrl watch] 새 버전 v{new} 설치됨 — 설정 UI를 쓰는 중이라 닫은 뒤 자동으로 전환합니다",
                  flush=True)
        return new
    raise Reexec(new)


def watch_loop(project, h, interval: float = 2.0, iterations: int | None = None,
               keys: Callable[[float], str | None] | None = None, menu: Menu | None = None) -> None:
    sig = _signature(project)
    notified: str | None = None
    n = 0
    if keys is not None and menu is not None:
        menu.start()
    print(f"[hctrl watch] {project.flow_dir} 감시 중 (간격 {interval}s)", flush=True)
    if keys is not None and menu is None:
        print(MENU, flush=True)
    update_seen, update_at = None, -UPDATE_POLL_S  # interactive _hc only: new-version notice (cache read, no network)
    upgrade_told: str | None = None
    while iterations is None or n < iterations:
        n += 1
        if keys is not None:
            upgrade_told = _upgrade(menu, upgrade_told)  # one stat per tick; the zip is read on change only
        if keys is not None and time.monotonic() - update_at >= UPDATE_POLL_S:
            update_at = time.monotonic()
            update_seen = _update_notice(h, update_seen)
        _pause(interval, keys, menu)
        sig2, changes, err = watch_once(project, h, sig)
        if menu is not None:
            menu.draw()  # header: target tab state and its pending changes
        if changes is None:
            continue
        sig = sig2
        if err:
            print("[hctrl watch] 설정 오류:\n" + err, flush=True)
            h.notify("hctrl: 설정 오류 (반영 안 함)", err.splitlines()[0])
            continue
        if not changes:
            notified = None  # nothing pending: the same change set later is news again
            continue
        key = digest(str([asdict(c) for c in changes]))
        if key == notified:
            continue
        notified = key
        text = ", ".join(f"{c.name} {c.detail}" for c in changes)
        print(f"[hctrl watch] 변경 감지: {text}", flush=True)
        h.notify("hctrl: 설정 변경 감지", text[:200] + " — 'hctrl changes' 또는 _hc 탭에서 [c]")
