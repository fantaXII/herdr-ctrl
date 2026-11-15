"""`hc up/down/zoom/status`: reconcile config with the live herdr session."""
from __future__ import annotations

import json
import shlex
import time
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONTROL_TAB, Config, Member, TabCfg, agent_args
from .herdr import HerdrError
from .layout import TabGroup, group_tab, source_slot
from .project import Project
from .roles import render_agent_brief, render_orchestrator_rules, role_fingerprint
from .state import State, digest, lock_holder


@dataclass
class Ctx:
    project: Project
    cfg: Config
    h: object  # Herdr or FakeHerdr
    state: State


@dataclass
class UpReport:
    created_tabs: list[str] = field(default_factory=list)
    created_panes: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    attention: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = []
        if self.created_tabs:
            out.append("탭 생성: " + ", ".join(self.created_tabs))
        if self.created_panes:
            out.append(f"창 생성: {len(self.created_panes)}개")
        if self.started:
            out.append("agent 시작: " + ", ".join(self.started))
        if self.skipped:
            out.append("이미 실행 중: " + ", ".join(self.skipped))
        for name, why in self.attention.items():
            out.append(f"⚠ {name}: {why}")
        if self.attention:
            out.append("화면을 열어 해당 창에서 확인해 주세요 (예: claude의 '폴더 신뢰' 질문).")
        return out or ["변경 없음"]


def hc_command() -> list[str]:
    """How to invoke hc from inside panes (works even when not pip-installed)."""
    return [sys.executable, "-m", "herdr_ctl.cli"]


def fingerprint(project: Project, m: Member) -> dict:
    return {"kind": m.kind, "model": m.model, "args": list(m.args), "permission": m.permission,
            "role": role_fingerprint(project, m) if m.is_agent else None, "type": m.type,
            "tab": m.tab, "cwd": m.cwd}


def member_cwd(ctx: Ctx, tab: TabCfg, m: Member) -> str:
    base = Path(ctx.state["worktrees"].get(tab.name, {}).get("cwd") or ctx.project.root)
    if tab.cwd:
        base = base / tab.cwd
    if m.cwd:
        base = base / m.cwd
    return str(base.resolve())


# ------------------------------------------------------------ workspaces
def ensure_workspace(ctx: Ctx) -> str:
    h, st = ctx.h, ctx.state
    live = {w["workspace_id"]: w for w in h.workspace_list()}
    wid = st.get("workspace_id")
    if wid in live:
        return wid
    wt_ids = {v.get("workspace_id") for v in st["worktrees"].values()}
    for w in live.values():
        if w.get("label") == ctx.project.name and w["workspace_id"] not in wt_ids:
            st["workspace_id"] = w["workspace_id"]
            return w["workspace_id"]
    res = h.workspace_create(str(ctx.project.root), ctx.project.name)
    wid = res["workspace"]["workspace_id"]
    st["workspace_id"] = wid
    # The new workspace's first tab becomes the hc control tab.
    h.tab_rename(res["tab"]["tab_id"], CONTROL_TAB)
    h.pane_rename(res["root_pane"]["pane_id"], "hc:control")
    st["control"] = {"tab_id": res["tab"]["tab_id"], "pane_id": res["root_pane"]["pane_id"]}
    return wid


def ensure_control(ctx: Ctx, wid: str, start_watch: bool) -> None:
    h, st = ctx.h, ctx.state
    panes = {p["pane_id"] for p in h.pane_list(wid)}
    ctl = st.get("control") or {}
    if ctl.get("pane_id") not in panes:
        res = h.tab_create(wid, CONTROL_TAB, str(ctx.project.root))
        pid = res["root_pane"]["pane_id"]
        h.pane_rename(pid, "hc:control")
        ctl = {"tab_id": res["tab"]["tab_id"], "pane_id": pid}
        st["control"] = ctl
    if start_watch and h.pane_shell_idle(ctl["pane_id"]):
        cmd = hc_command() + ["--project", str(ctx.project.root), "watch"]
        h.pane_run(ctl["pane_id"], " ".join(shlex.quote(c) for c in cmd))


def git_branch(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def ensure_tab_workspace(ctx: Ctx, tab: TabCfg, main_wid: str, report: UpReport) -> str:
    """Workspace that hosts `tab`: the project workspace, or a worktree workspace."""
    if not tab.worktree:
        return main_wid
    h, st = ctx.h, ctx.state
    live = {w["workspace_id"] for w in h.workspace_list()}
    wt = st["worktrees"].get(tab.name)
    if wt and wt.get("workspace_id") in live:
        return wt["workspace_id"]
    base = tab.worktree.get("base") or git_branch(ctx.project.root)
    res = h.worktree_create(main_wid, str(ctx.project.root), tab.worktree["branch"], base, tab.name)
    ws = res.get("workspace") or {}
    root_pane = res.get("root_pane") or {}
    wid = ws.get("workspace_id") or root_pane.get("workspace_id")
    if not wid:
        raise HerdrError("worktree_failed", f"unexpected worktree response: {res}")
    cwd = root_pane.get("cwd") or ws.get("cwd")
    st["worktrees"][tab.name] = {"workspace_id": wid, "cwd": cwd, "branch": tab.worktree["branch"]}
    if res.get("tab"):
        h.tab_rename(res["tab"]["tab_id"], tab.name)
        st["tabs"][tab.name] = {"tab_id": res["tab"]["tab_id"], "workspace_id": wid}
    report.created_tabs.append(f"{tab.name} (worktree {tab.worktree['branch']})")
    return wid


# ----------------------------------------------------------------- panes
def ensure_group(ctx: Ctx, group: TabGroup, wid: str, report: UpReport) -> None:
    h, st = ctx.h, ctx.state
    n = len(group.members)
    live_tabs = {t["tab_id"]: t for t in h.tab_list(wid)}
    tab_id = (st["tabs"].get(group.label) or {}).get("tab_id")
    if tab_id not in live_tabs:
        tab_id = next((t["tab_id"] for t in live_tabs.values() if t.get("label") == group.label), None)
    panes = {p["pane_id"]: p for p in h.pane_list(wid)}
    owned = {m.get("pane_id") for m in st["members"].values()}
    slots: list[str | None] = []
    for m in group.members:
        rec = st.member(m.name) or {}
        pid = rec.get("pane_id")
        slots.append(pid if pid in panes and panes[pid].get("tab_id") == tab_id else None)
    if tab_id is None:
        first = group.members[0]
        res = h.tab_create(wid, group.label, member_cwd(ctx, group.cfg, first))
        tab_id = res["tab"]["tab_id"]
        report.created_tabs.append(group.label)
        slots[0] = res["root_pane"]["pane_id"]
        report.created_panes.append(slots[0])
    else:
        # Re-use leftover panes in the tab (e.g. an agent exited and left its shell).
        free = [pid for pid, p in panes.items() if p.get("tab_id") == tab_id and pid not in owned]
        for i, pid in enumerate(slots):
            if pid is None and free:
                slots[i] = free.pop(0)
    st["tabs"][group.label] = {"tab_id": tab_id, "workspace_id": wid}
    for slot in range(n):
        if slots[slot] is not None:
            continue
        src = source_slot(n, slot)
        if src is not None and slots[src[0]] is not None:
            src_slot, direction, ratio = src
        else:  # layout source missing: split from any existing pane
            src_slot = next(i for i, p in enumerate(slots) if p is not None)
            direction, ratio = "right", None
        m = group.members[slot]
        new = h.pane_split(slots[src_slot], direction, member_cwd(ctx, group.cfg, m), ratio)
        slots[slot] = new["pane_id"]
        report.created_panes.append(new["pane_id"])
    for m, pid in zip(group.members, slots):
        h.pane_rename(pid, f"hc:{m.name}")
        st["members"][m.name] = {"pane_id": pid, "tab_id": tab_id, "workspace_id": wid,
                                 "tab_label": group.label, "tab": m.tab, "type": m.type,
                                 "cwd": member_cwd(ctx, group.cfg, m)}


def brief_for(ctx: Ctx, m: Member, handoff: Path | None = None) -> Path:
    tab = ctx.cfg.tab(m.tab)
    if m.type == "orch":
        return render_orchestrator_rules(ctx.project, ctx.cfg, tab, handoff)
    return render_agent_brief(ctx.project, ctx.cfg, m, handoff)


def start_agent(ctx: Ctx, m: Member, pane_id: str, handoff: Path | None = None) -> str | None:
    """Start agent `m` in `pane_id`. Returns None on success or an attention reason."""
    h = ctx.h
    brief = brief_for(ctx, m, handoff)
    args = agent_args(m, brief if m.kind == "claude" else None)
    try:
        h.agent_start(m.name, m.kind, pane_id, args)
    except HerdrError as e:
        if e.code == "agent_not_ready":
            reason = "시작 중 확인 대기(blocked) — 화면에서 응답 필요"
        else:
            return f"시작 실패: {e}"
        ctx.state["attention"][m.name] = reason
        ctx.state["applied"][m.name] = fingerprint(ctx.project, m)
        return reason
    if m.kind != "claude":
        try:
            h.agent_prompt(m.name, f"{brief} 파일을 읽고 그 역할과 규칙을 따르세요. "
                                   "확인했으면 '준비됨'이라고만 답하세요.", wait=False)
        except HerdrError:
            pass
    ctx.state["attention"].pop(m.name, None)
    ctx.state["applied"][m.name] = fingerprint(ctx.project, m)
    if ctx.state.member(m.name) is not None:
        ctx.state["members"][m.name]["started_at"] = time.time()
    return None


def ensure_agents(ctx: Ctx, group: TabGroup, report: UpReport) -> None:
    h, st = ctx.h, ctx.state
    live = {a["name"]: a for a in h.agent_list() if a.get("name")}
    for m in group.members:
        rec = st.member(m.name)
        if not m.is_agent:
            st["applied"][m.name] = fingerprint(ctx.project, m)
            continue
        if m.name in live:
            if live[m.name].get("pane_id") != rec["pane_id"]:
                report.attention[m.name] = f"같은 이름의 agent가 다른 창({live[m.name].get('pane_id')})에 있습니다"
            else:
                report.skipped.append(m.name)
                st["applied"].setdefault(m.name, fingerprint(ctx.project, m))
            continue
        if not h.pane_shell_idle(rec["pane_id"]):
            report.attention[m.name] = "창에서 다른 프로그램이 실행 중이라 agent를 시작할 수 없습니다"
            continue
        why = start_agent(ctx, m, rec["pane_id"])
        if why is None:
            report.started.append(m.name)
        else:
            report.attention[m.name] = why


def up(ctx: Ctx, tab: str | None = None, start_watch: bool = True) -> UpReport:
    report = UpReport()
    ctx.project.ensure_dirs()
    wid = ensure_workspace(ctx)
    ensure_control(ctx, wid, start_watch)
    tabs = [ctx.cfg.tab(tab)] if tab else ctx.cfg.tabs
    if tab and tabs[0] is None:
        raise KeyError(f"탭 '{tab}'이 설정에 없습니다")
    for t in tabs:
        twid = ensure_tab_workspace(ctx, t, wid, report)
        for group in group_tab(t):
            ensure_group(ctx, group, twid, report)
            ctx.state.save()
            ensure_agents(ctx, group, report)
            ctx.state.save()
    ctx.state["applied_flows"] = flows_fingerprint(ctx.cfg)
    ctx.state["session"] = ctx.cfg.session
    ctx.state.save()
    return report


def flows_fingerprint(cfg: Config) -> str:
    return digest(json.dumps(cfg.raw.get("flows") or {}, sort_keys=True, ensure_ascii=False))


# ------------------------------------------------------------- down/zoom
def down(ctx: Ctx, tab: str | None = None, remove_worktree: bool = False) -> list[str]:
    h, st = ctx.h, ctx.state
    done = []
    tabs = [ctx.cfg.tab(tab)] if tab else ctx.cfg.tabs
    if tab and tabs[0] is None:
        raise KeyError(f"탭 '{tab}'이 설정에 없습니다")
    for t in tabs:
        wt = st["worktrees"].get(t.name)
        if wt and remove_worktree:
            # Remove first: closing the worktree's last tab also closes its workspace.
            try:
                h.worktree_remove(wt["workspace_id"])
            except HerdrError as e:
                done.append(f"(worktree 정리 실패: {e})")
            st["worktrees"].pop(t.name, None)
        for group in group_tab(t):
            rec = st["tabs"].pop(group.label, None)
            if rec:
                try:
                    h.tab_close(rec["tab_id"])
                except HerdrError:
                    pass
                done.append(group.label)
            for m in group.members:
                st["members"].pop(m.name, None)
                st["applied"].pop(m.name, None)
                st["attention"].pop(m.name, None)
    if tab is None:
        ctl = st.get("control")
        if ctl:
            try:
                h.tab_close(ctl["tab_id"])
            except HerdrError:
                pass
            st["control"] = None
    st.save()
    return done


def zoom(ctx: Ctx, name: str) -> None:
    rec = ctx.state.member(name)
    if not rec:
        raise KeyError(f"'{name}'의 창이 없습니다 (hc up 먼저)")
    ctx.h.pane_zoom(rec["pane_id"])


def status(ctx: Ctx) -> list[str]:
    from .runner import list_runs
    h, st = ctx.h, ctx.state
    live = {a["name"]: a for a in h.agent_list() if a.get("name")}
    lines = [f"세션 {ctx.cfg.session}  (프로젝트 {ctx.project.root})"]
    for t in ctx.cfg.tabs:
        for group in group_tab(t):
            lines.append(f"[{group.label}]")
            for m in group.members:
                rec = st.member(m.name)
                if m.type == "shell":
                    state = "shell" if rec else "창 없음"
                else:
                    state = live.get(m.name, {}).get("agent_status") or ("정지" if rec else "창 없음")
                extra = []
                holder = lock_holder(ctx.project, m.name)
                if holder:
                    extra.append(f"잠금: {holder.get('owner')} 사용 중")
                if m.name in st["attention"]:
                    extra.append("확인 필요: " + st["attention"][m.name])
                tag = "orchestrator" if m.type == "orch" else (m.kind or "shell")
                lines.append(f"  {m.name:<14} {tag:<12} {state:<8} " + "; ".join(extra))
    from .watch import load_pending
    pending = load_pending(ctx.project)
    if pending:
        lines.append(f"변경 대기: {len(pending)}건 (hc changes)")
    active = [r for r in list_runs(ctx.project) if r.get("status") in ("pending", "running")]
    for r in active:
        lines.append(f"실행 중: {r['id']} ({r.get('flow')}) 단계 {r.get('current', 0) + 1}/{len(r.get('steps', []))}")
    return lines
