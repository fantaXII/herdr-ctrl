"""Project location: registry (~/.config/herdr_ctl/projects.yaml) and .herdr-flow/ paths."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

FLOW_DIR = ".herdr-flow"
CONFIG_NAME = "config.yaml"
GITIGNORE_LINES = [".herdr-flow/runs/", ".herdr-flow/handoff/", ".herdr-flow/var/",
                   ".herdr-flow/state.json"]


def config_home() -> Path:
    return Path(os.environ.get("HC_CONFIG_HOME") or Path.home() / ".config" / "herdr_ctl")


def registry_path() -> Path:
    return config_home() / "projects.yaml"


def load_registry() -> dict[str, str]:
    p = registry_path()
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return {str(k): str(v) for k, v in (data.get("projects") or {}).items()}


def save_registry(projects: dict[str, str]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"projects": projects}, allow_unicode=True, sort_keys=True))


def register(name: str, path: Path) -> None:
    reg = load_registry()
    reg[name] = str(path.resolve())
    save_registry(reg)


@dataclass
class Project:
    name: str
    root: Path

    @property
    def flow_dir(self) -> Path:
        return self.root / FLOW_DIR

    @property
    def config_path(self) -> Path:
        return self.flow_dir / CONFIG_NAME

    @property
    def state_path(self) -> Path:
        return self.flow_dir / "state.json"

    @property
    def var_dir(self) -> Path:
        return self.flow_dir / "var"

    @property
    def runs_dir(self) -> Path:
        return self.flow_dir / "runs"

    @property
    def roles_dir(self) -> Path:
        return self.flow_dir / "roles"

    @property
    def handoff_dir(self) -> Path:
        return self.flow_dir / "handoff"

    def ensure_dirs(self) -> None:
        for d in (self.flow_dir, self.var_dir, self.runs_dir, self.roles_dir, self.handoff_dir):
            d.mkdir(parents=True, exist_ok=True)


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def find_project(cwd: Path | None = None) -> Project | None:
    """Registered path that contains cwd (longest wins), else nearest dir with .herdr-flow/config.yaml."""
    cwd = (cwd or Path.cwd()).resolve()
    best: tuple[int, str, Path] | None = None
    for name, path in load_registry().items():
        root = _expand(path)
        if cwd == root or root in cwd.parents:
            score = len(root.parts)
            if best is None or score > best[0]:
                best = (score, name, root)
    if best:
        return Project(best[1], best[2])
    for d in [cwd, *cwd.parents]:
        cfg = d / FLOW_DIR / CONFIG_NAME
        if cfg.exists():
            try:
                data = yaml.safe_load(cfg.read_text()) or {}
            except yaml.YAMLError:
                data = {}
            return Project(str(data.get("session") or d.name), d)
    return None


def project_by_name(name: str) -> Project | None:
    path = load_registry().get(name)
    return Project(name, _expand(path)) if path else None


def ensure_gitignore(root: Path) -> list[str]:
    """Append the transient .herdr-flow paths to <root>/.gitignore. Returns lines added."""
    gi = root / ".gitignore"
    existing = gi.read_text().splitlines() if gi.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if missing:
        with gi.open("a") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("# herdr_ctl (hc)\n" + "\n".join(missing) + "\n")
    return missing
