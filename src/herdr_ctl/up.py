"""`hctrl up/down/zoom/status`: reconcile config with the live herdr session."""
from __future__ import annotations

import json
import re
import shlex
import time
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONTROL_TAB, Config, Member, TabCfg, agent_args
from .herdr import HerdrError
from .layout import TabGroup, group_tab, source_slot
from .project import Project
from .selfcmd import self_cmd
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
    """How to invoke hctrl from inside panes (works even when not pip-installed)."""
    return self_cmd()


def fingerprint(project: Project, m: Member) -> dict:
    fp = {"kind": m.kind, "model": m.model, "args": list(m.args), "permission": m.permission,
          "role": role_fingerprint(project, m) if m.is_agent else None, "type": m.type,
          "tab": m.tab, "cwd": m.cwd, "v": 2}
    if m.type == "agent" and m.kind == "claude" and m.unattended != "off":
        fp["unattended"] = m.unattended  # absent when off: older state stays compatible
    if m.primary_agent:
        fp["primary_agent"] = m.primary_agent  # absent when unset (same compatibility rule)
    if m.tab_cwd is not None:
        fp["tab_cwd"] = m.tab_cwd  # absent when unset (same compatibility rule)
    if m.worktree:
        fp["worktree"] = dict(m.worktree)
    return fp


def member_cwd(ctx: Ctx, tab: TabCfg, m: Member) -> str:
    wt = ctx.state["worktrees"].get(tab.name) or {}
    # a recorded worktree only counts while the config still asks for that branch
    use_wt = tab.worktree and wt.get("branch") == tab.worktree.get("branch")
    base = Path((wt.get("cwd") if use_wt else None) or ctx.project.root)
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
    root = ctx.project.root.resolve()
    for w in live.values():
        if w.get("cwd") and Path(w["cwd"]).resolve() != root:
            continue  # same label, but another project's workspace
        if w.get("label") == ctx.project.name and w["workspace_id"] not in wt_ids:
            st["workspace_id"] = w["workspace_id"]
            return w["workspace_id"]
    res = h.workspace_create(str(ctx.project.root), ctx.project.name)
    wid = res["workspace"]["workspace_id"]
    st["workspace_id"] = wid
    # The new workspace's first tab becomes the hctrl control tab.
    h.tab_rename(res["tab"]["tab_id"], CONTROL_TAB)
    h.pane_rename(res["root_pane"]["pane_id"], "hctrl:control")
    st["control"] = {"tab_id": res["tab"]["tab_id"], "pane_id": res["root_pane"]["pane_id"]}
    return wid


def ensure_control(ctx: Ctx, wid: str, start_watch: bool) -> None:
    h, st = ctx.h, ctx.state
    panes = {p["pane_id"] for p in h.pane_list(wid)}
    ctl = st.get("control") or {}
    if ctl.get("pane_id") not in panes:
        res = h.tab_create(wid, CONTROL_TAB, str(ctx.project.root))
        pid = res["root_pane"]["pane_id"]
        h.pane_rename(pid, "hctrl:control")
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
    if wt and wt.get("branch") == tab.worktree["branch"] and wt.get("cwd") and Path(wt["cwd"]).is_dir():
        # `hctrl down` without --remove-worktree closed the workspace but kept the worktree: reopen it.
        res = h.workspace_create(wt["cwd"], tab.name)
        how = "다시 열기"
    else:
        base = tab.worktree.get("base") or git_branch(ctx.project.root)
        res = h.worktree_create(main_wid, str(ctx.project.root), tab.worktree["branch"], base, tab.name)
        how = "생성"
    ws = res.get("workspace") or {}
    root_pane = res.get("root_pane") or {}
    wid = ws.get("workspace_id") or root_pane.get("workspace_id")
    if not wid:
        raise HerdrError("worktree_failed", f"unexpected worktree response: {res}")
    cwd = root_pane.get("cwd") or ws.get("cwd")
    rec = {"workspace_id": wid, "cwd": cwd, "branch": tab.worktree["branch"]}
    base = (wt or {}).get("base") if how == "다시 열기" else base
    if base:
        rec["base"] = base  # what "merged?" is checked against when the tab is cleaned up
    st["worktrees"][tab.name] = rec
    if res.get("tab"):
        h.tab_rename(res["tab"]["tab_id"], tab.display)
        st["tabs"][tab.name] = {"tab_id": res["tab"]["tab_id"], "workspace_id": wid}
    report.created_tabs.append(f"{tab.name} (worktree {tab.worktree['branch']} {how})")
    if how == "생성":
        try:  # this workspace has no _hc tab of its own
            h.notify(f"hctrl: 탭 {tab.name} (worktree)",
                     f"{tab.worktree['branch']} 브랜치의 별도 workspace입니다. 재시작·종료는 메인 workspace의 "
                     f"_hc 탭(대상 탭을 {tab.name}로)이나 hctrl ui에서 합니다")
        except HerdrError:
            pass
    return wid


# ----------------------------------------------------------------- panes
def _move_out_of_place_shells(ctx: Ctx, group: TabGroup, tab_id: str | None, panes: dict,
                              report: UpReport) -> None:
    """Shell panes from the older layout (split into the agent tab) move to the build tab:
    an idle one is closed here and recreated there; a busy one is left alone until it finishes."""
    for m in group.members:
        pid = (ctx.state.member(m.name) or {}).get("pane_id")
        if m.type != "shell" or pid not in panes or (tab_id and panes[pid].get("tab_id") == tab_id):
            continue
        if not ctx.h.pane_shell_idle(pid):
            raise RuntimeError(f"'{m.name}' 창에서 명령이 실행 중이라 '{group.title}' 탭으로 옮기지 못했습니다. "
                               f"끝난 뒤 hctrl up {group.cfg.name}을 다시 실행하세요")
        ctx.h.pane_close(pid)
        panes.pop(pid)
        report.created_tabs.append(f"{m.name} → {group.title}")


def ensure_group(ctx: Ctx, group: TabGroup, wid: str, report: UpReport) -> None:
    h, st = ctx.h, ctx.state
    n = len(group.members)
    live_tabs = {t["tab_id"]: t for t in h.tab_list(wid)}
    tab_id = (st["tabs"].get(group.label) or {}).get("tab_id")
    if tab_id not in live_tabs:
        claimed = {r.get("tab_id") for r in st["tabs"].values()} | {(st.get("control") or {}).get("tab_id")}
        tab_id = next((t["tab_id"] for t in live_tabs.values()
                       if t["tab_id"] not in claimed and t.get("label") in (group.title, group.label)), None)
    if tab_id in live_tabs and live_tabs[tab_id].get("label") != group.title:
        h.tab_rename(tab_id, group.title)  # title edited: rename in place, panes and agents untouched
    panes = {p["pane_id"]: p for p in h.pane_list(wid)}
    _move_out_of_place_shells(ctx, group, tab_id, panes, report)
    configured = {m.name for t in ctx.cfg.tabs for m in t.members}
    # Panes of members no longer in the config (e.g. renamed agents) are reusable.
    stale = {r.get("pane_id"): name for name, r in st["members"].items() if name not in configured}
    owned = {r.get("pane_id") for name, r in st["members"].items() if name in configured}
    slots: list[str | None] = []
    for m in group.members:
        rec = st.member(m.name) or {}
        pid = rec.get("pane_id")
        slots.append(pid if pid in panes and panes[pid].get("tab_id") == tab_id else None)
    if tab_id is None:
        first = group.members[0]
        res = h.tab_create(wid, group.title, member_cwd(ctx, group.cfg, first))
        tab_id = res["tab"]["tab_id"]
        report.created_tabs.append(group.label)
        slots[0] = res["root_pane"]["pane_id"]
        report.created_panes.append(slots[0])
    else:
        # Re-use leftover panes in the tab (e.g. an agent exited and left its shell).
        free = [pid for pid, p in panes.items() if p.get("tab_id") == tab_id and pid not in owned
                and pid not in slots and h.pane_shell_idle(pid)]
        for i, pid in enumerate(slots):
            if pid is None and free:
                slots[i] = free.pop(0)
                if slots[i] in stale:
                    st["members"].pop(stale[slots[i]], None)
        if all(pid is None for pid in slots):
            raise RuntimeError(f"탭 '{group.label}'에 쓸 수 있는 창이 없습니다 (다른 프로그램이나 이전 agent가 "
                               f"실행 중). 'hctrl down {group.cfg.name}' 후 'hctrl up {group.cfg.name}'로 다시 배치하세요")
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
        h.pane_rename(pid, f"hctrl:{m.name}")
        st["members"][m.name] = {"pane_id": pid, "tab_id": tab_id, "workspace_id": wid,
                                 "tab_label": group.label, "tab": m.tab, "type": m.type,
                                 "cwd": member_cwd(ctx, group.cfg, m)}


def brief_for(ctx: Ctx, m: Member, handoff: Path | None = None) -> Path:
    tab = ctx.cfg.tab(m.tab)
    if m.type == "orch":
        return render_orchestrator_rules(ctx.project, ctx.cfg, tab, handoff)
    return render_agent_brief(ctx.project, ctx.cfg, m, handoff)


# ------------------------------------------------------------ conversations
RESUME_ARGS = {"claude": "--resume", "opencode": "--session"}
SESSION_ID_RE = re.compile(r"[0-9A-Za-z_-]{4,128}")


# Conversation ids live in the private state home, not in .herdr-flow/state.json: an auto-edit agent
# could otherwise point an orchestrator at a conversation of its choice (`--resume <id>`).
def _sessions_path(project: Project) -> Path:
    return project.secure_dir / "sessions.json"


def load_sessions(project: Project) -> dict:
    from .state import read_json
    raw = read_json(_sessions_path(project)) or {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and SESSION_ID_RE.fullmatch(str(v.get("id") or ""))}


def update_sessions(project: Project, change) -> dict:
    """change(dict) edits the {member: {agent, id, at}} record under its lock; returns the result."""
    from .state import file_lock, write_json
    path = _sessions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(path.with_suffix(".lock")):
        rec = load_sessions(project)
        change(rec)
        write_json(path, rec)
    return rec


def live_session(ctx: Ctx, name: str) -> dict | None:
    """The conversation a running agent is in ({agent, id}), as herdr reports it."""
    try:
        a = ctx.h.agent_get(name)
    except (HerdrError, KeyError):
        return None
    sess = a.get("agent_session") if isinstance(a, dict) else None
    if not isinstance(sess, dict) or not SESSION_ID_RE.fullmatch(str(sess.get("value") or "")):
        return None
    return {"agent": sess.get("agent"), "id": str(sess["value"]), "at": time.time()}


def remember_sessions(ctx: Ctx, tab: TabCfg) -> None:
    """Before a tab's agents stop: note each one's conversation, so reopening can continue it."""
    found = {m.name: s for m in tab.members if m.is_agent and (s := live_session(ctx, m.name)) is not None}
    if found:
        update_sessions(ctx.project, lambda rec: rec.update(found))


def resume_id(ctx: Ctx, m: Member, sess: dict | None) -> str | None:
    """The conversation id to continue for `m`, when its CLI can and the record is its own kind."""
    if not sess or m.kind not in RESUME_ARGS or sess.get("agent") not in (None, m.kind) \
            or not SESSION_ID_RE.fullmatch(str(sess.get("id") or "")):
        return None
    if m.kind == "claude":
        from .orchestrator import claude_transcript
        if claude_transcript(sess["id"]) is None:
            return None  # the transcript is gone: a fresh start is all that is left
    return sess["id"]


def start_agent(ctx: Ctx, m: Member, pane_id: str, handoff: Path | None = None,
                resume: str | None = None) -> str | None:
    """Start agent `m` in `pane_id` (continuing conversation `resume` when given).
    Returns None on success or an attention reason."""
    h = ctx.h
    brief = brief_for(ctx, m, handoff)
    from .unattended import hook_settings
    tab = ctx.cfg.tab(m.tab)
    settings = hook_settings(ctx, tab, m) if tab else None
    args = agent_args(m, brief if m.kind == "claude" else None, settings,
                      handoff_file=ctx.project.handoff_dir / f"{m.name}.md")
    if resume and m.kind in RESUME_ARGS:
        args = [RESUME_ARGS[m.kind], resume] + args
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
    if m.kind != "claude" and not resume:  # a continued conversation already has its brief
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


def ensure_agents(ctx: Ctx, group: TabGroup, report: UpReport, resume: bool = False) -> None:
    h, st = ctx.h, ctx.state
    live = {a["name"]: a for a in h.agent_list() if a.get("name")}
    sessions = load_sessions(ctx.project) if resume else {}
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
        rid = resume_id(ctx, m, sessions.get(m.name)) if resume else None
        why = start_agent(ctx, m, rec["pane_id"], resume=rid)
        if why is None:
            report.started.append(m.name + (" (이전 대화 이어서)" if rid else ""))
        else:
            report.attention[m.name] = why


def up(ctx: Ctx, tab: str | None = None, start_watch: bool = True, resume: bool = False) -> UpReport:
    """Lay out and start the tab(s). `resume`: continue each agent's recorded conversation."""
    report = UpReport()
    tabs = [ctx.cfg.tab(tab)] if tab else ctx.cfg.open_tabs
    if tab and tabs[0] is None:
        raise KeyError(f"탭 '{tab}'이 설정에 없습니다")
    if tab and tabs[0].closed:
        raise RuntimeError(f"탭 '{tabs[0].name}'은 보관(완료)된 탭입니다. 다시 열려면: hctrl tab reopen {tabs[0].name}")
    ctx.project.ensure_dirs()
    wid = ensure_workspace(ctx)
    ensure_control(ctx, wid, start_watch)
    for t in tabs:
        twid = ensure_tab_workspace(ctx, t, wid, report)
        for group in group_tab(t):
            ensure_group(ctx, group, twid, report)
            ctx.state.save()
            ensure_agents(ctx, group, report, resume)
            ctx.state.save()
    ctx.state["applied_flows"] = flows_fingerprint(ctx.cfg)
    ctx.state["session"] = ctx.cfg.session
    ctx.state.save()
    from .orchestrator import register_panes
    register_panes(ctx)  # pane → member identity lives outside the project
    return report


def sync_titles(ctx: Ctx) -> list[str]:
    """Rename live herdr tabs to their configured titles (no relayout, no agent restart)."""
    done = []
    wids = {rec.get("workspace_id") for rec in ctx.state["tabs"].values()} - {None}
    live = {t["tab_id"]: t for wid in wids for t in ctx.h.tab_list(wid)}
    for t in ctx.cfg.open_tabs:
        for group in group_tab(t):
            tab_id = (ctx.state["tabs"].get(group.label) or {}).get("tab_id")
            if tab_id in live and live[tab_id].get("label") != group.title:
                ctx.h.tab_rename(tab_id, group.title)
                done.append(f"{live[tab_id].get('label')} → {group.title}")
    return done


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
        remember_sessions(ctx, t)
        wt = st["worktrees"].get(t.name)
        labels = [g.label for g in group_tab(t)]
        # overflow tabs (X-2, ...) from a larger earlier config
        labels += sorted(lbl for lbl in st["tabs"] if lbl not in labels
                         and re.fullmatch(rf"{re.escape(t.name)}(-build)?(-\d+)?", lbl) and lbl != t.name)
        if wt and remove_worktree:
            # Remove first: closing the worktree's last tab also closes its workspace.
            try:
                h.worktree_remove(wt["workspace_id"])
            except HerdrError as e:
                done.append(f"(worktree 정리 실패: {e})")
            st["worktrees"].pop(t.name, None)
        for label in labels:
            rec = st["tabs"].pop(label, None)
            if rec:
                try:
                    h.tab_close(rec["tab_id"])
                except HerdrError:
                    pass
                done.append(label)
        names = {m.name for m in t.members}
        names |= {n for n, r in st["members"].items() if r.get("tab") == t.name}
        for name in names:
            st["members"].pop(name, None)
            st["applied"].pop(name, None)
            st["attention"].pop(name, None)
    if tab is None:
        ctl = st.get("control")
        if ctl:
            try:
                h.tab_close(ctl["tab_id"])
            except HerdrError:
                pass
            st["control"] = None
    st.save()
    from .orchestrator import register_panes
    register_panes(ctx)  # pane → member identity lives outside the project
    return done


def zoom(ctx: Ctx, name: str) -> None:
    rec = ctx.state.member(name)
    if not rec:
        raise KeyError(f"'{name}'의 창이 없습니다 (hctrl up 먼저)")
    ctx.h.pane_zoom(rec["pane_id"])


def status(ctx: Ctx, tab: str | None = None) -> list[str]:
    from .runner import list_runs
    h, st = ctx.h, ctx.state
    live = {a["name"]: a for a in h.agent_list() if a.get("name")}
    lines = [f"세션 {ctx.cfg.session}  (프로젝트 {ctx.project.root})"]
    tabs = [ctx.cfg.tab(tab)] if tab else ctx.cfg.tabs
    if tab and tabs[0] is None:
        raise KeyError(f"탭 '{tab}'이 설정에 없습니다")
    for t in tabs:
        if t.closed:
            lines.append(f"[{t.name}] 보관됨 (hctrl tab reopen {t.name})")
            continue
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
        lines.append(f"변경 대기: {len(pending)}건 (hctrl changes)")
    from .unattended import pending as pending_questions
    qs = pending_questions(ctx.project)
    if qs:
        lines.append(f"질문 대기: {len(qs)}건 (hctrl pending) — " + ", ".join(f"{q['id']}({q['agent']})" for q in qs[:5]))
    active = [r for r in list_runs(ctx.project) if r.get("status") in ("pending", "running", "question")]
    for r in active:
        lines.append(f"실행 중: {r['id']} ({r.get('flow')}) 단계 {r.get('current', 0) + 1}/{len(r.get('steps', []))}")
    return lines
