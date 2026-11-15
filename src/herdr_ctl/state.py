"""Persistent state (.herdr-flow/state.json) and per-agent locks (.herdr-flow/var/locks)."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .project import Project


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


class State:
    """{session, workspace_id, control:{tab_id,pane_id}, tabs:{label:{...}},
    members:{name:{pane_id,tab_id,workspace_id,tab_label,cwd}}, applied:{name:fingerprint},
    applied_flows, worktrees:{tab:{workspace_id,cwd}}, attention:{name:reason}}"""

    def __init__(self, project: Project):
        self.project = project
        self.data: dict = read_json(project.state_path, {}) or {}
        for key in ("tabs", "members", "applied", "worktrees", "attention"):
            self.data.setdefault(key, {})

    def save(self) -> None:
        write_json(self.project.state_path, self.data)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __setitem__(self, key, value):
        self.data[key] = value

    def member(self, name: str) -> dict | None:
        return self.data["members"].get(name)

    def member_by_pane(self, pane_id: str) -> str | None:
        for name, m in self.data["members"].items():
            if m.get("pane_id") == pane_id:
                return name
        return None


# ------------------------------------------------------------------ locks
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def proc_start(pid: int) -> str | None:
    """Kernel start time of a process (guards against pid reuse); None if unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def same_process_alive(pid: int | None, start: str | None) -> bool:
    if not pid or not _pid_alive(int(pid)):
        return False
    return start is None or proc_start(int(pid)) in (None, start)


class LockBusy(Exception):
    def __init__(self, name: str, holder: dict):
        super().__init__(f"agent '{name}' is locked by {holder.get('owner')} (pid {holder.get('pid')})")
        self.name = name
        self.holder = holder


def lock_path(project: Project, name: str) -> Path:
    return project.var_dir / "locks" / f"{name}.lock"


def lock_holder(project: Project, name: str) -> dict | None:
    p = lock_path(project, name)
    info = read_json(p)
    if info is None:
        # empty/corrupt file (writer died between create and write): reclaim once it is old
        try:
            if time.time() - p.stat().st_mtime > 5:
                p.unlink(missing_ok=True)
                return None
        except FileNotFoundError:
            return None
        return {"owner": "?", "pid": "?"}
    if not _pid_alive(int(info.get("pid", 0))):
        p.unlink(missing_ok=True)  # stale: holder died
        return None
    return info


def acquire(project: Project, name: str, owner: str, pid: int | None = None) -> None:
    p = lock_path(project, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = lock_holder(project, name)
            if holder is None:
                continue  # stale lock removed, retry once
            raise LockBusy(name, holder)
        with os.fdopen(fd, "w") as f:
            json.dump({"owner": owner, "pid": pid or os.getpid(), "ts": time.time()}, f)
        return
    raise LockBusy(name, {"owner": "?", "pid": "?"})


def release(project: Project, name: str, owner: str | None = None) -> None:
    p = lock_path(project, name)
    info = read_json(p)
    if info is None:
        return
    if owner is None or info.get("owner") == owner:
        p.unlink(missing_ok=True)


@contextmanager
def locked(project: Project, name: str, owner: str):
    acquire(project, name, owner)
    try:
        yield
    finally:
        release(project, name, owner)
