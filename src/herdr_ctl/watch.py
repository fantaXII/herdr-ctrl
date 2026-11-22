"""Config/role change detection (`hctrl watch`, `hctrl changes`) and agent restart (`hctrl restart`)."""
from __future__ import annotations

import os
import select
import subprocess
import sys
import time
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
            diffs = [k for k in ("kind", "model", "args", "permission", "type", "tab", "cwd", "unattended",
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
        if name not in names:
            changes.append(Change(name, "remove", "설정에서 삭제됨"))
    if ctx.state.get("applied_flows") and ctx.state["applied_flows"] != flows_fingerprint(ctx.cfg):
        changes.append(Change("flows", "flow", "flow 변경 (다음 hctrl run부터 적용)"))
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


def restart(ctx: Ctx, name: str, handoff: bool = True, wait_working: bool = True,
            timeout_s: float = 1800) -> str:
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
        handoff_path = None
        hp = (ctx.project.handoff_dir / f"{name}.md").resolve()
        if handoff and st is not None:
            ctx.project.handoff_dir.mkdir(parents=True, exist_ok=True)
            handoff_path = hp
            # an earlier handoff of this agent: it may answer "already written, nothing changed"
            previous = hp.read_text() if hp.exists() else ""
            handoff_path.unlink(missing_ok=True)
            turned = True  # `prompt --wait` returned: the agent finished a turn on it
            try:
                try:
                    ctx.h.agent_prompt(name, HANDOFF_PROMPT.format(path=handoff_path), wait=True,
                                       timeout_ms=int(timeout_s * 1000))
                except HerdrError as e:
                    if e.code != "agent_prompt_stalled":
                        raise RuntimeError(f"인수인계 요청 실패: {e}") from e
                    _await_handoff(ctx, name, handoff_path, timeout_s)
            except HandoffMissing as e:
                turned = e.turned
            if not (handoff_path.exists() and handoff_path.read_text().strip()):
                if not (turned and previous.strip()):
                    raise _missing(name, handoff_path, turned)
                handoff_path.write_text(previous)  # the agent kept its earlier handoff as current
        elif handoff and st is None and hp.exists() and hp.read_text().strip() \
                and hp.stat().st_mtime > float(rec.get("started_at") or 0):
            handoff_path = hp  # an earlier attempt got the handoff written, then failed to start
        if st is not None:
            _stop_agent(ctx, name, pane)
        if not ctx.h.pane_shell_idle(pane):
            raise RuntimeError(f"'{name}' 창이 쉘 프롬프트로 돌아오지 않았습니다")
        _wait_agent_gone(ctx, name)
        why = start_agent(ctx, m, pane, handoff_path)
        if why and why.startswith("시작 실패"):  # one more try: herdr may still be settling the pane
            _sleep(2.0)
            if ctx.h.pane_shell_idle(pane):
                _wait_agent_gone(ctx, name)
                why = start_agent(ctx, m, pane, handoff_path)
        ctx.state.save()
        if why and why.startswith("시작 실패"):
            raise RuntimeError(f"'{name}' {why}. 창은 쉘 상태입니다. 다시 반영하면 "
                               + ("이미 써 둔 인수인계 파일로 " if handoff_path else "") + "시작합니다")
    finally:
        release(ctx.project, name, "restart")
    _tell_orchestrator(ctx, m, f"[hctrl] '{name}'가 새 설정으로 재시작됐습니다." +
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
    """action: notify | restart | add | remove | skip | ack"""
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
    if action == "notify":
        return notify_role(ctx, ch.name)
    if action == "restart":
        return restart(ctx, ch.name, handoff=handoff)
    if action == "fresh":
        return restart(ctx, ch.name, handoff=False)
    if action == "add":
        m = ctx.cfg.member(ch.name)
        rep = up(ctx, tab=m.tab, start_watch=False)
        return f"{ch.name}: 추가됨 — " + "; ".join(rep.lines())
    if action == "remove":
        return remove_member(ctx, ch.name)
    raise ValueError(action)


def default_actions(ch: Change) -> list[str]:
    return {"role": ["notify", "restart", "fresh", "skip"], "restart": ["restart", "fresh", "skip"],
            "relayout": ["ack", "skip"],
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


MENU = "[s] 설정 열기(브라우저)   [c] 변경 반영   [t] 상태   [p] 답을 기다리는 질문"
MENU_COMMANDS = {"c": ["changes"], "t": ["status"], "p": ["pending"]}


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


class Menu:
    """Key actions of the _hc tab. The _hc pane is the user's (never an agent's), so it may open the UI."""

    def __init__(self, project, cooked=None, run: Callable[[list[str]], int] = subprocess.call):
        self.project = project
        self.cooked = cooked
        self.run = run
        self.ui = None

    def handle(self, key: str) -> None:
        key = key.lower()
        if key == "s":
            self.settings()
        elif key in MENU_COMMANDS:
            cmd = hc_command() + ["--project", str(self.project.root)] + MENU_COMMANDS[key]
            with self.cooked() if self.cooked else nullcontext():
                self.run(cmd)
        else:
            return
        print(MENU, flush=True)

    def settings(self) -> None:
        from .ui import UIServer, announce
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

    def __init__(self, new: str, reopen_ui: bool = False):
        super().__init__(new)
        self.new = new
        self.reopen_ui = reopen_ui


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
    print(f"[hctrl watch] {project.flow_dir} 감시 중 (간격 {interval}s)", flush=True)
    if keys is not None:
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
