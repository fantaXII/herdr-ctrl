"""Finishing a tab: close (windows only), archive (`closed: true`), reopen, clean its files, delete it;
and restarting a whole tab. Shared by the CLI, the _hc menu and the settings UI.

A tab's worktree is only removed when everything in it reached the branch it came from: no uncommitted
or untracked files, and every commit of its branch is in the base (merged, squashed or rebased).
Branches and commits are never removed on a guess.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import TabCfg, check_data, load_yaml
from .herdr import HerdrError
from .up import Ctx, down, git_branch, load_sessions, up, update_sessions

CLEAN_ITEMS = ("work", "handoff", "runs", "roles", "sessions")
CLEAN_LABELS = {"work": "작업 폴더", "handoff": "인수인계", "runs": "실행 기록", "roles": "역할 파일·이력·brief",
                "sessions": "이어 갈 대화 기록", "worktree": "worktree 폴더"}
MODES = ("close", "archive", "delete")


class TabOpError(RuntimeError):
    pass


class WorktreeNotMerged(TabOpError):
    """The tab's worktree still holds work that is not in its base branch: nothing was removed."""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(worktree_summary(info))


def _tab(ctx: Ctx, name: str) -> TabCfg:
    t = ctx.cfg.tab(name)
    if t is None:
        raise KeyError(f"탭 '{name}'이 설정에 없습니다 (탭: {', '.join(x.name for x in ctx.cfg.tabs)})")
    return t


# ------------------------------------------------------------------ worktree
def _git(cwd, *args) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    except FileNotFoundError:
        return 127, ""
    return r.returncode, r.stdout


def worktree_info(ctx: Ctx, tab: TabCfg) -> dict | None:
    """What removing the tab's worktree would lose. None when the tab never had one."""
    rec = ctx.state["worktrees"].get(tab.name)
    if not rec:
        return None
    root = ctx.project.root
    path, branch = rec.get("cwd"), rec.get("branch")
    base = (tab.worktree or {}).get("base") or rec.get("base") or git_branch(root)
    exists = bool(path) and Path(path).is_dir()
    dirty: list[str] = []
    if exists:
        code, out = _git(path, "status", "--porcelain")
        dirty = [ln[3:] for ln in out.splitlines() if ln.strip()] if code == 0 else ["(git status 실패)"]
    has_branch = bool(branch) and _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0
    unmerged: list[str] = []
    known = False  # could the merge state be decided at all?
    if has_branch and base and branch != base:
        if _git(root, "merge-base", "--is-ancestor", branch, base)[0] == 0:
            known = True
        else:
            code, out = _git(root, "cherry", "-v", base, branch)  # "+ <sha> <subject>": not in base yet
            if code == 0:
                known = True
                unmerged = [ln[2:].strip() for ln in out.splitlines() if ln.startswith("+")]
    elif not has_branch:
        known = True  # the branch is already gone: nothing of it can be lost
    merged = known and not unmerged
    return {"tab": tab.name, "path": path, "exists": exists, "branch": branch, "base": base,
            "has_branch": has_branch, "dirty": dirty, "unmerged": unmerged, "merged": merged,
            "safe": merged and not dirty}


def worktree_summary(info: dict) -> str:
    if info["safe"]:
        return f"{info['branch']}: {info['base']}에 모두 들어가 있어 정리해도 됩니다"
    parts = []
    if info["unmerged"]:
        parts.append(f"{info['base']}에 없는 커밋 {len(info['unmerged'])}개: " + ", ".join(info["unmerged"][:5])
                     + (" …" if len(info["unmerged"]) > 5 else ""))
    elif not info["merged"]:
        parts.append(f"{info['base'] or '기준 브랜치'}에 들어갔는지 확인할 수 없음")
    if info["dirty"]:
        parts.append(f"커밋 안 된 파일 {len(info['dirty'])}개: " + ", ".join(info["dirty"][:5])
                     + (" …" if len(info["dirty"]) > 5 else ""))
    return f"⚠ {info['branch']}은 아직 {info['base'] or '기준 브랜치'}에 다 들어가지 않았습니다 — " + "; ".join(parts)


def remove_worktree(ctx: Ctx, tab: TabCfg, force: bool = False, delete_branch: bool = True) -> list[str]:
    """Remove the tab's worktree folder (and its branch when merged). Refuses unmerged work unless force;
    a forced removal keeps the branch."""
    info = worktree_info(ctx, tab)
    if info is None:
        return []
    if not info["safe"] and not force:
        raise WorktreeNotMerged(info)
    root, done = ctx.project.root, []
    rec = ctx.state["worktrees"].get(tab.name) or {}
    try:
        live = {w["workspace_id"] for w in ctx.h.workspace_list()}
    except HerdrError:  # the session is not running: git does it directly
        live = set()
    if rec.get("workspace_id") in live:
        ctx.h.worktree_remove(rec["workspace_id"], force=force)
    elif info["exists"]:
        code, _ = _git(root, "worktree", "remove", *(["--force"] if force else []), info["path"])
        if code != 0:
            raise TabOpError(f"worktree 폴더를 지우지 못했습니다: {info['path']}")
    _git(root, "worktree", "prune")
    done.append(f"worktree 폴더 정리: {info['path']}")
    if delete_branch and info["merged"] and info["has_branch"]:
        # merged (also squash/rebase, which `branch -d` cannot see): -D is safe here
        if _git(root, "branch", "-D", info["branch"])[0] == 0:
            done.append(f"브랜치 삭제: {info['branch']} ({info['base']}에 병합됨)")
    elif info["has_branch"]:
        done.append(f"브랜치 유지: {info['branch']}")
    ctx.state["worktrees"].pop(tab.name, None)
    ctx.state.save()
    return done


# ------------------------------------------------------------------- files
def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def _tab_runs(ctx: Ctx, tab: TabCfg) -> list[Path]:
    """Runs whose every step targeted a member of this tab."""
    from .runner import list_runs
    names = {m.name for m in tab.members}
    out = []
    for rec in list_runs(ctx.project):
        targets = {(s.get("agent") or s.get("shell")) for s in rec.get("steps") or [] if isinstance(s, dict)}
        if targets and targets <= names:
            out.append(ctx.project.runs_dir / rec["id"])
    return out


def clean_plan(ctx: Ctx, tab: TabCfg, items) -> list[dict]:
    """[{item, path, bytes}] that clean() would remove (only paths that exist)."""
    from .roles import role_file
    p = ctx.project
    agents = [m for m in tab.members if m.is_agent]
    paths: list[tuple[str, Path]] = []
    if "work" in items:
        paths.append(("work", p.flow_dir / "work" / tab.name))
    if "handoff" in items:
        paths += [("handoff", p.handoff_dir / f"{m.name}.md") for m in agents]
    if "runs" in items:
        paths += [("runs", d) for d in _tab_runs(ctx, tab)]
    if "roles" in items:
        for m in agents:
            if not m.role_file:  # a custom role_file is the user's own document: never removed
                paths.append(("roles", role_file(p, m)))
            paths += [("roles", p.roles_dir / "history" / m.name), ("roles", p.var_dir / "briefs" / f"{m.name}.md")]
        if tab.orchestrator:
            from .roles import orchestrator_rules_path
            paths.append(("roles", orchestrator_rules_path(p, tab)))
    out = [{"item": it, "path": str(path), "bytes": _size(path)} for it, path in paths if path.exists()]
    if "sessions" in items:
        known = load_sessions(ctx.project)
        for m in agents:
            if m.name in known:
                out.append({"item": "sessions", "path": f"대화 기록 id ({m.name})", "bytes": 0})
    return out


def clean(ctx: Ctx, tab: TabCfg, items) -> list[str]:
    plan = clean_plan(ctx, tab, items)
    done = []
    for row in plan:
        if row["item"] == "sessions":
            continue
        path = Path(row["path"])
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        done.append(f"삭제: {row['path']}")
    if "sessions" in items:
        names = {m.name for m in tab.members}
        gone = sorted(names & set(load_sessions(ctx.project)))
        update_sessions(ctx.project, lambda rec: [rec.pop(n, None) for n in names])
        done += [f"대화 기록 id 삭제: {n}" for n in gone]
    return done


# ------------------------------------------------------------------ config
def _edit_config(ctx: Ctx, change) -> None:
    from .init_cmd import write_config
    data, issues = load_yaml(ctx.project.config_path)
    if not isinstance(data, dict):
        raise TabOpError("config.yaml을 읽을 수 없습니다: " + "; ".join(i.message for i in issues))
    change(data)
    # agent binaries are not what changes here (and were checked when the config was saved)
    cfg, issues = check_data(data, root=ctx.project.root, which=lambda _kind: "found")
    errors = [i for i in issues if i.level == "error"]
    if cfg is None or errors:
        raise TabOpError("바꾼 설정에 오류가 있어 저장하지 않았습니다: " + "; ".join(f"{i.path}: {i.message}" for i in errors))
    write_config(ctx.project, data)
    ctx.cfg = cfg


def _tab_key(data: dict, name: str) -> str:
    for k in data.get("tabs") or {}:
        if str(k).lower() == name.lower():
            return k
    raise KeyError(f"탭 '{name}'이 설정에 없습니다")


def set_closed(ctx: Ctx, name: str, closed: bool) -> None:
    def change(data):
        t = data["tabs"][_tab_key(data, name)]
        if closed:
            t["closed"] = True
        else:
            t.pop("closed", None)
    _edit_config(ctx, change)


def remove_from_config(ctx: Ctx, tab: TabCfg) -> list[str]:
    """Drop the tab, its own flows, and steps of other flows that use its members."""
    from .templates import tab_flows
    names = {m.name for m in tab.members}
    dropped: list[str] = []

    def change(data):
        key = _tab_key(data, tab.name)
        own = set(tab_flows(data, key))
        data["tabs"].pop(key)
        flows = data.get("flows") or {}
        for fname in list(flows):
            f = flows[fname]
            if fname in own:
                flows.pop(fname)
                dropped.append(f"flow 삭제: {fname}")
                continue
            steps = f.get("steps") if isinstance(f, dict) else None
            if isinstance(steps, list):
                keep = [s for s in steps if not (isinstance(s, dict) and (s.get("agent") or s.get("shell")) in names)]
                if len(keep) != len(steps):
                    if keep:
                        f["steps"] = keep
                        dropped.append(f"flow '{fname}'에서 이 탭 단계 {len(steps) - len(keep)}개 제거")
                    else:
                        flows.pop(fname)
                        dropped.append(f"flow 삭제: {fname}")
        if "flows" in data and not data["flows"]:
            data.pop("flows")
    _edit_config(ctx, change)
    return dropped


# -------------------------------------------------------------- operations
def finish(ctx: Ctx, name: str, mode: str, worktree: str = "keep", delete_branch: bool = True,
           items=()) -> list[str]:
    """End a tab. mode: close (windows only) | archive (closed: true) | delete (out of the config).
    worktree: keep | remove (only when merged and clean, else WorktreeNotMerged before anything is
    touched) | force. items: files to clean (delete; archived tabs via clean())."""
    if mode not in MODES:
        raise ValueError(mode)
    tab = _tab(ctx, name)
    if worktree == "remove":
        info = worktree_info(ctx, tab)
        if info is not None and not info["safe"]:
            raise WorktreeNotMerged(info)
    closed = down(ctx, tab.name)
    out = [f"창 닫음: {', '.join(closed)}" if closed else "열린 창 없음"]
    if worktree in ("remove", "force"):
        out += remove_worktree(ctx, tab, force=worktree == "force", delete_branch=delete_branch)
    if mode == "archive" and not tab.closed:
        set_closed(ctx, tab.name, True)
        out.append(f"보관함으로 옮김 (다시 열기: hctrl tab reopen {tab.name})")
    elif mode == "delete":
        if items:
            out += clean(ctx, tab, items)
        out += remove_from_config(ctx, tab)
        update_sessions(ctx.project, lambda rec: [rec.pop(m.name, None) for m in tab.members])
        out.append(f"설정에서 탭 '{tab.name}' 삭제")
    if mode == "archive":
        from .watch import detect, save_pending
        save_pending(ctx.project, detect(ctx))  # the archived tab's members no longer show as "new"
    if mode == "delete" and worktree == "keep" and ctx.state["worktrees"].get(tab.name):
        rec = ctx.state["worktrees"][tab.name]
        out.append(f"worktree 남김: {rec.get('cwd')} ({rec.get('branch')}) — 병합한 뒤 "
                   f"'git worktree remove {rec.get('cwd')}'로 지우세요")
    return out


def reopen(ctx: Ctx, name: str, resume: bool = True) -> list[str]:
    """Open a closed or archived tab again; resume: continue each agent's last conversation."""
    tab = _tab(ctx, name)
    if tab.closed:
        set_closed(ctx, tab.name, False)
    rep = up(ctx, tab=tab.name, start_watch=False, resume=resume)
    return rep.lines()


def restart_tab(ctx: Ctx, name: str, resume: bool = True, handoff: bool = True) -> list[str]:
    """Restart every agent of a tab: work agents first, the orchestrator last."""
    from .state import LockBusy
    from .watch import restart
    tab = _tab(ctx, name)
    order = [m for m in tab.agents] + ([tab.orchestrator] if tab.orchestrator else [])
    out = []
    for m in order:
        if ctx.state.member(m.name) is None:
            out.append(f"{m.name}: 창 없음 (건너뜀)")
            continue
        try:
            out.append(restart(ctx, m.name, handoff=handoff, resume=resume))
        except (RuntimeError, KeyError, HerdrError, LockBusy) as e:
            out.append(f"{m.name}: ✘ {e}")
    return out
