"""Rename a tab id in place (`hctrl tab rename`, UI save).

The tab id keys config, state, a few files and (through the orchestrator name `<id>-orch`) a live
herdr agent. All of them move together, so panes and agents keep running: no relayout, no restart.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from .config import TAB_RE, CONTROL_TAB, check_file, orch_name
from .herdr import READY_STATES, HerdrError, agent_status
from .state import State, lock_holder, same_process_alive
from .up import Ctx


class RenameError(RuntimeError):
    pass


def rename_in_data(data: dict, old: str, new: str) -> dict:
    """config.yaml data with tab `old` renamed to `new` (order kept; flows follow, incl. orchestrator steps)."""
    tabs = data.get("tabs") or {}
    data["tabs"] = {(new if str(k) == old else k): v for k, v in tabs.items()}
    o_old, o_new = orch_name(old), orch_name(new)
    for f in (data.get("flows") or {}).values():
        if not isinstance(f, dict):
            continue
        if isinstance(f.get("tab"), str) and f["tab"].lower() == old.lower():
            f["tab"] = new
        for st in f.get("steps") or []:
            if isinstance(st, dict) and st.get("agent") == o_old:
                st["agent"] = o_new
    return data


def _relabel(label: str, old: str, new: str) -> str | None:
    """X, X-2, X-build, X-build-2 → the same with the new id; None when `label` is not old's."""
    m = re.fullmatch(rf"{re.escape(old)}((?:-build)?(?:-\d+)?)", label)
    return new + m.group(1) if m else None


def _busy(ctx: Ctx, old: str, names: set[str]) -> list[str]:
    from .runner import list_runs
    from .unattended import pending
    why = []
    for n in sorted(names):
        holder = lock_holder(ctx.project, n)
        if holder:
            why.append(f"'{n}'를 {holder.get('owner')}가 사용 중입니다")
    for r in list_runs(ctx.project):
        if r.get("status") not in ("pending", "running", "question"):
            continue
        if r.get("pid") and not same_process_alive(r["pid"], r.get("pid_start")):
            continue  # left behind by a dead worker
        if any((s.get("agent") or s.get("shell")) in names for s in r.get("steps") or []):
            why.append(f"실행 {r['id']}이 진행 중입니다 (hctrl stop {r['id']})")
    if pending(ctx.project, tab=old):
        why.append("답을 기다리는 질문이 있습니다 (hctrl pending)")
    return why


def check(ctx: Ctx, renames: dict[str, str]) -> None:
    """Refuse renames that cannot move cleanly. `ctx` holds the config from before the rename."""
    olds = {o.lower() for o in renames}
    problems = []
    for old, new in renames.items():
        tab = ctx.cfg.tab(old)
        if tab is None or tab.name != old:
            problems.append(f"탭 '{old}'이 설정에 없습니다")
            continue
        if not TAB_RE.match(new) or new == CONTROL_TAB:
            problems.append(f"'{new}': 탭 ID는 영문으로 시작하고 영문/숫자/_- 25자 이하입니다")
        elif new.lower() in olds and new.lower() != old.lower():
            problems.append(f"'{old}' → '{new}': 이름을 서로 맞바꾸는 변경은 한 번에 할 수 없습니다. 나눠서 저장하세요")
        names = {m.name for m in tab.members}
        problems += [f"탭 '{old}': {w}" for w in _busy(ctx, old, names)]
    if problems:
        raise RenameError("탭 ID를 바꿀 수 없습니다:\n- " + "\n- ".join(problems))


def _move(src: Path, dst: Path) -> None:
    if not src.exists() or src == dst:
        return
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def _migrate(ctx: Ctx, old: str, new: str) -> None:
    """state.json keys and the per-tab / per-orchestrator files."""
    p, st = ctx.project, ctx.state
    o_old, o_new = orch_name(old), orch_name(new)
    st["tabs"] = {(_relabel(k, old, new) or k): v for k, v in st["tabs"].items()}
    if old in st["worktrees"]:
        st["worktrees"][new] = st["worktrees"].pop(old)
    for rec in st["members"].values():
        if rec.get("tab") == old:
            rec["tab"] = new
            rec["tab_label"] = _relabel(rec.get("tab_label") or old, old, new) or rec.get("tab_label")
    for key in ("members", "applied", "attention"):
        if o_old != o_new and o_old in st[key]:
            st[key][o_new] = st[key].pop(o_old)
    names = {m.name for m in ctx.cfg.tab(old).members} - {o_old} | {o_new}
    for n in names:
        fp = st["applied"].get(n)
        if isinstance(fp, dict) and fp.get("tab") == old:
            fp["tab"] = new
    # files
    (p.flow_dir / f"orchestrator-{old}.md").unlink(missing_ok=True)  # re-rendered under the new id
    _move(p.secure_dir / f"ask-{old}.json", p.secure_dir / f"ask-{new}.json")
    (p.secure_dir / f"ask-{old}.lock").unlink(missing_ok=True)
    if o_old != o_new:
        tab = ctx.cfg.tab(old)
        if tab.orchestrator and not tab.orchestrator.role_file:
            _move(p.roles_dir / f"{o_old}.md", p.roles_dir / f"{o_new}.md")
        _move(p.roles_dir / "history" / o_old, p.roles_dir / "history" / o_new)
        _move(p.handoff_dir / f"{o_old}.md", p.handoff_dir / f"{o_new}.md")
        (p.var_dir / "briefs" / f"{o_old}.md").unlink(missing_ok=True)


def _live(ctx: Ctx, renames: dict[str, str], report: list[str]) -> None:
    """Running session: rename the orchestrator agents and the herdr tab labels, tell orchestrators."""
    from .roles import render_orchestrator_rules
    from .up import sync_titles
    h = ctx.h
    live = {a["name"] for a in h.agent_list() if a.get("name")}
    for old, new in renames.items():
        o_old, o_new = orch_name(old), orch_name(new)
        tab = ctx.cfg.tab(new)
        if tab is None or tab.orchestrator is None:
            continue
        if o_old != o_new and o_old in live:
            h.agent_rename(o_old, o_new)
            report.append(f"orchestrator {o_old} → {o_new}")
        rec = ctx.state.member(o_new)
        if rec and rec.get("pane_id"):
            try:
                h.pane_rename(rec["pane_id"], f"hctrl:{o_new}")
            except HerdrError:
                pass
        rules = render_orchestrator_rules(ctx.project, ctx.cfg, tab)
        text = (f"[hctrl] 이 탭의 ID가 '{old}'에서 '{new}'로 바뀌었고, 당신의 이름은 이제 '{o_new}'입니다. "
                f"{rules} 파일을 다시 읽고 따르세요. 확인했으면 '확인'이라고만 답하세요.")
        try:
            if agent_status(h, o_new) in READY_STATES:
                h.agent_prompt(o_new, text, wait=False)
            else:
                report.append(f"{o_new}: 작업 중이라 이름 변경을 알리지 못했습니다 (끝난 뒤 hctrl role {o_new} --apply)")
        except HerdrError:
            pass
    report += [f"탭 {r}" for r in sync_titles(ctx)]


def rename_tabs(project, renames: dict[str, str], write: Callable[[], None], h=None,
                live: bool = False) -> list[str]:
    """Rename tab ids. `write()` saves the new config.yaml (between the state move and the live steps).
    `live`: the session is running and `h` talks to it."""
    renames = {o: n for o, n in renames.items() if o != n}
    if not renames:
        write()
        return []
    cfg, issues = check_file(project.config_path)
    if cfg is None:
        raise RenameError("현재 config.yaml에 오류가 있어 탭 ID를 옮길 수 없습니다")
    state = State(project)
    ctx = Ctx(project, cfg, h, state)
    check(ctx, renames)
    for old, new in renames.items():
        _migrate(ctx, old, new)
    state.save()
    write()
    report = [f"탭 ID {o} → {n}" for o, n in renames.items()]
    cfg2, issues = check_file(project.config_path)
    if cfg2 is None:
        return report
    ctx2 = Ctx(project, cfg2, h, state)
    if live and h is not None:
        try:
            _live(ctx2, renames, report)
        except HerdrError as e:
            report.append(f"⚠ herdr 반영 중 오류: {e} (hctrl up으로 다시 맞출 수 있습니다)")
        state.save()
    from .orchestrator import register_panes
    register_panes(ctx2)
    return report
