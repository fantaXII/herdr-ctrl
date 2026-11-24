"""Move a running tab to where its config now says it works: the project folder or its own git
worktree (hctrl ui → 작업 위치, i.e. the tab's `worktree` key).

Only what is safe is done automatically; anything else stops BEFORE a change with the reason
(RelocateBlocked), and the user decides:
- worktree → project folder: the agents write a handoff, then the worktree branch is merged into the
  base branch checked out in the project folder (fast-forward when possible, else a merge commit; a
  conflict is undone and stops), the tab's untracked work files (.herdr-flow/work/<tab>/) move over,
  the tab is laid out again in the project folder and the merged worktree is removed (kept, and said
  so, when it holds git-ignored files such as .env: those would be deleted with it).
- project folder → worktree: the agents write a handoff, the tab is laid out again in a new worktree
  of the current branch, and its work files move into it.
Never done for the user: committing, deleting files, resolving conflicts, or taking uncommitted changes
along (the project folder is shared, so whose they are is unknown).
Conversations do not continue across folders: every agent starts fresh with its handoff file.
Once the tab is taken down, the step is recorded (state "relocating"): a retry after a failure from
there on finishes the move with the same handoffs instead of starting over.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import TabCfg
from .herdr import READY_STATES, HerdrError, agent_status
from .project import FLOW_DIR
from .state import LockBusy, acquire, release
from .up import Ctx, down, fingerprint, git_branch, up, work_dir


class RelocateBlocked(RuntimeError):
    """Nothing was changed: the move needs the user first (the message says what)."""


def _git(cwd, *args) -> tuple[int, str, str]:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "", "git이 없습니다"
    return r.returncode, r.stdout, r.stderr.strip()


def _status(path) -> tuple[list[str], list[str]]:
    """(tracked changes, untracked files outside .herdr-flow/) of a checkout."""
    code, out, _ = _git(path, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        raise RelocateBlocked(f"git 상태를 확인할 수 없습니다: {path}")
    tracked, untracked = [], []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        name = ln[3:]
        if ln.startswith("??"):
            if not name.startswith(FLOW_DIR + "/"):
                untracked.append(name)
        else:
            tracked.append(name)
    return tracked, untracked


def _ignored(path) -> list[str]:
    """Git-ignored files of a checkout outside .herdr-flow/ (deleted along with a worktree)."""
    code, out, _ = _git(path, "status", "--porcelain", "--ignored", "--untracked-files=normal")
    return [ln[3:] for ln in out.splitlines() if ln.startswith("!!") and not ln[3:].startswith(FLOW_DIR + "/")] \
        if code == 0 else ["(git status 실패)"]


def _work_files(checkout: Path, tab: TabCfg) -> list[Path]:
    """The tab's work files git does not track (ignored ones included), relative to `checkout`.
    Tracked work files travel with the merge."""
    rel = work_dir(Path("."), tab).as_posix()
    code, out, _ = _git(checkout, "ls-files", "--others", "-z", "--", rel)
    if code != 0:  # not a repository: everything there is untracked
        d = work_dir(checkout, tab)
        return [p.relative_to(checkout) for p in d.rglob("*") if p.is_file()] if d.is_dir() else []
    return [Path(n) for n in out.split("\0") if n]


def move_work_files(src_root: Path, dst_root: Path, tab: TabCfg) -> list[str]:
    """Move the tab's untracked work files between checkouts (never over a different file: checked before)."""
    files = _work_files(src_root, tab)
    for rel in files:
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_root / rel, dst)
    for rel in files:
        (src_root / rel).unlink()
    d = work_dir(src_root, tab)
    while d.is_dir() and d != src_root and not any(d.iterdir()):  # empty folders left behind
        d.rmdir()
        d = d.parent
    return [f"작업 파일 이동: {len(files)}개 ({work_dir(Path('.'), tab).as_posix()})"] if files else []


def _work_clash(src_root: Path, dst_root: Path, tab: TabCfg) -> list[str]:
    return [str(rel) for rel in _work_files(src_root, tab)
            if (dst_root / rel).is_file() and (dst_root / rel).read_bytes() != (src_root / rel).read_bytes()]


def _few(items: list[str], n: int = 5) -> str:
    return ", ".join(items[:n]) + (f" 외 {len(items) - n}개" if len(items) > n else "")


def placement(ctx: Ctx, tab: TabCfg) -> str | None:
    """Where the tab's windows are now: "worktree", "main", or None (not laid out)."""
    recs = [r for m in tab.members if (r := ctx.state.member(m.name))]
    if not recs:
        return None
    wt = ctx.state["worktrees"].get(tab.name) or {}
    return "worktree" if wt.get("workspace_id") and \
        any(r.get("workspace_id") == wt["workspace_id"] for r in recs) else "main"


def direction(ctx: Ctx, tab: TabCfg) -> str | None:
    """"to_main", "to_worktree", or None when the tab already works where the config says."""
    where = placement(ctx, tab)
    if where == "worktree" and not tab.worktree:
        return "to_main"
    if where == "main" and tab.worktree:
        return "to_worktree"
    if where == "worktree" and tab.worktree:
        rec = ctx.state["worktrees"].get(tab.name) or {}
        if rec.get("branch") != tab.worktree.get("branch"):
            raise RelocateBlocked(f"worktree 브랜치를 '{rec.get('branch')}'에서 '{tab.worktree.get('branch')}'로 "
                                  "바로 바꿀 수는 없습니다. 작업 위치를 프로젝트 폴더로 옮긴 뒤 다시 worktree로 바꾸세요")
    return None


def _check_idle(ctx: Ctx, tab: TabCfg) -> None:
    from .tabrename import _busy
    why = _busy(ctx, tab.name, {m.name for m in tab.members})
    for m in tab.members:
        if m.is_agent and ctx.state.member(m.name):
            st = agent_status(ctx.h, m.name)
            if st is not None and st not in READY_STATES:
                why.append(f"'{m.name}'가 {'승인 대기' if st == 'blocked' else '작업'} 중입니다")
    if why:
        raise RelocateBlocked("진행 중인 작업이 있어 옮기지 않았습니다: " + "; ".join(why))


def _check_main_folder_quiet(ctx: Ctx, tab: TabCfg) -> None:
    """A merge changes files under every tab working in the project folder: none may be mid-turn."""
    busy = []
    for t in ctx.cfg.open_tabs:
        if t.name == tab.name or placement(ctx, t) != "main":
            continue
        for m in t.members:
            if m.is_agent and ctx.state.member(m.name):
                st = agent_status(ctx.h, m.name)
                if st is not None and st not in READY_STATES:
                    busy.append(f"{m.name}(탭 {t.name})")
    if busy:
        raise RelocateBlocked("프로젝트 폴더에서 작업 중인 agent가 있어 병합하지 않았습니다: " + _few(busy)
                              + ". 끝난 뒤 다시 반영하세요")


def _handoffs(ctx: Ctx, tab: TabCfg, timeout_s: float) -> dict[str, str]:
    from .watch import collect_handoff
    out = {}
    for m in [m for m in tab.agents] + ([tab.orchestrator] if tab.orchestrator else []):
        if ctx.state.member(m.name) and agent_status(ctx.h, m.name) is not None:
            out[m.name] = str(collect_handoff(ctx, m.name, timeout_s))
    return out


# ------------------------------------------------------------------ checks before anything changes
def _preflight_to_main(ctx: Ctx, tab: TabCfg) -> dict:
    rec = ctx.state["worktrees"].get(tab.name) or {}
    wt, branch, base = rec.get("cwd"), rec.get("branch"), rec.get("base")
    root = ctx.project.root
    if not wt or not Path(wt).is_dir() or not branch:
        raise RelocateBlocked("worktree 폴더나 브랜치 기록이 없습니다. hctrl down/up으로 다시 배치하세요")
    if not base:
        raise RelocateBlocked(f"worktree '{branch}'를 어느 브랜치에서 만들었는지 기록이 없어 병합하지 않았습니다. "
                              "직접 병합한 뒤 다시 반영하세요")
    tracked, untracked = _status(wt)
    if tracked:
        raise RelocateBlocked(f"worktree에 커밋 안 된 변경이 있습니다: {_few(tracked)}. 커밋하거나 정리한 뒤 다시 반영하세요")
    if untracked:
        raise RelocateBlocked(f"worktree에 git에 없는 파일이 있습니다: {_few(untracked)}. "
                              "커밋하거나 지운 뒤 다시 반영하세요 (worktree를 지우면 사라집니다)")
    current = git_branch(root)
    if current != base:
        raise RelocateBlocked(f"프로젝트 폴더가 '{base}'가 아닌 '{current}' 브랜치에 있어 병합하지 않았습니다")
    tracked, _ = _status(root)
    if tracked:
        raise RelocateBlocked(f"프로젝트 폴더에 커밋 안 된 변경이 있어 병합하지 않았습니다: {_few(tracked)}")
    clash = _work_clash(Path(wt), root, tab)
    if clash:
        raise RelocateBlocked(f"프로젝트 폴더에 내용이 다른 같은 작업 파일이 있습니다: {_few(clash)}. "
                              "하나로 정리한 뒤 다시 반영하세요")
    _check_main_folder_quiet(ctx, tab)
    return {"wt": wt, "branch": branch, "base": base}


def _preflight_to_worktree(ctx: Ctx, tab: TabCfg) -> list[str]:
    root = ctx.project.root
    rec = ctx.state["worktrees"].get(tab.name)
    if rec:  # a worktree kept from an earlier move or `hctrl down`: reopening it may bring back old code
        raise RelocateBlocked(f"이 탭의 이전 worktree가 남아 있습니다 ({rec.get('cwd')}, {rec.get('branch')}). "
                              "병합하고 정리하거나(hctrl tab finish), 다른 브랜치 이름을 쓰세요")
    tracked, untracked = _status(root)
    if tracked:
        raise RelocateBlocked(
            "프로젝트 폴더에 커밋 안 된 변경이 있어 옮기지 않았습니다 (누구의 변경인지 알 수 없어 worktree로 "
            f"가져가지 않습니다): {_few(tracked)}. 커밋하거나 정리한 뒤 다시 반영하세요")
    return untracked


def _merge(root: Path, branch: str, base: str) -> str:
    if _git(root, "merge", "--ff-only", branch)[0] == 0:
        return f"병합: {branch} → {base} (fast-forward)"
    code, _, err = _git(root, "merge", "--no-edit", branch)
    if code == 0:
        return f"병합: {branch} → {base} (병합 커밋)"
    if _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] != 0:  # no merge in progress: not a conflict
        raise RelocateBlocked(f"'{branch}'를 '{base}'에 병합하지 못했습니다 (변경 없음): {err or '원인 불명'}")
    if _git(root, "merge", "--abort")[0] != 0:
        raise RelocateBlocked(f"'{branch}' 병합이 충돌했고 되돌리지도 못했습니다. 프로젝트 폴더에서 "
                              "'git merge --abort'로 직접 되돌리세요 (탭은 worktree에 그대로 있습니다)")
    raise RelocateBlocked(f"'{branch}'를 '{base}'에 병합하면 충돌이 나서 되돌렸습니다. "
                          "직접 병합한 뒤 다시 반영하세요 (탭은 worktree에 그대로 있습니다)")


# ------------------------------------------------------------------ the move
def _finish(ctx: Ctx, tab: TabCfg, job: dict) -> list[str]:
    """From `down` on (recorded in state "relocating", so a retry picks up here)."""
    from .tabops import TabOpError, remove_worktree
    root, out = ctx.project.root, []
    if placement(ctx, tab) is not None:
        down(ctx, tab.name)
    rep = up(ctx, tab=tab.name, start_watch=False, handoffs={n: Path(p) for n, p in job["handoffs"].items()})
    out += rep.lines()
    if job["to"] == "to_main":
        wt = Path(job["wt"])
        if wt.is_dir():
            out += move_work_files(wt, root, tab)
            ignored = _ignored(wt)
            if ignored:
                out.append(f"⚠ worktree 폴더는 남겼습니다: git이 무시하는 파일이 있어 함께 지워집니다 ({_few(ignored)}). "
                           f"확인한 뒤 'git worktree remove {wt}'로 지우세요")
            else:
                try:
                    out += remove_worktree(ctx, tab)
                except (TabOpError, HerdrError) as e:
                    out.append(f"⚠ worktree는 남겼습니다: {e}")
    elif job["to"] == "to_worktree":
        wt = (ctx.state["worktrees"].get(tab.name) or {}).get("cwd")
        if wt and Path(wt).is_dir():
            out += move_work_files(root, Path(wt), tab)
        if job.get("left"):  # not lost: they stay in the project folder, the worktree starts without them
            out.append(f"참고: git에 없는 파일은 프로젝트 폴더에 남았습니다: {_few(job['left'])}")
    ctx.state["relocating"].pop(tab.name, None)
    ctx.state.save()
    return out


def relocate_tab(ctx: Ctx, name: str, timeout_s: float = 1800) -> list[str]:
    """Lay tab `name` out again where its config says it works (see the module doc)."""
    tab = ctx.cfg.tab(name)
    if tab is None:
        raise KeyError(f"탭 '{name}'이 설정에 없습니다")
    if ctx.state.get("relocating") is None:
        ctx.state["relocating"] = {}
    job = ctx.state["relocating"].get(tab.name)
    if job is None and all(ctx.state["applied"].get(m.name) == fingerprint(ctx.project, m) for m in tab.members):
        return [f"탭 {tab.name}: 이미 반영됨"]
    names = [m.name for m in tab.members]
    if job is None:
        how = direction(ctx, tab)
        _check_idle(ctx, tab)
        pre = _preflight_to_main(ctx, tab) if how == "to_main" else {}
        left = _preflight_to_worktree(ctx, tab) if how == "to_worktree" else []
    held = []
    try:
        for n in names:  # no hctrl send/run may start on the tab while it moves
            acquire(ctx.project, n, "relocate")
            held.append(n)
        if job is None:
            hand = _handoffs(ctx, tab, timeout_s)  # still nothing changed if an agent cannot write one
            out = []
            if how == "to_main":
                pre = _preflight_to_main(ctx, tab)  # again: the handoff turns may have touched files
                out.append(_merge(ctx.project.root, pre["branch"], pre["base"]))
            job = {"to": how, "handoffs": hand, "wt": pre.get("wt"), "left": left}
            ctx.state["relocating"][tab.name] = job
            ctx.state.save()
            return out + _finish(ctx, tab, job)
        return ["이전에 멈춘 옮기기를 이어서 진행합니다"] + _finish(ctx, tab, job)
    except LockBusy as e:
        raise RelocateBlocked(f"진행 중인 작업이 있어 옮기지 않았습니다: {e}") from e
    finally:
        for n in held:
            release(ctx.project, n, "relocate")
