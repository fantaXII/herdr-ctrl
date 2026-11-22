"""Team templates: one tab (its members, flows and role details) kept outside every project.

Saved from a project's saved config.yaml + role files, loaded into any project (settings UI) or used
to start a new one (`hctrl init --template`). A template is a copy: editing it later never changes
tabs already made from it.

File: config_home()/templates/<name>.yaml
  version, name, saved_at, source: {project, tab}
  tab:    the tab's settings without project-bound keys (cwd, title, worktree, shell cwd, role_file)
  flows:  the tab's flows; `tab: "@tab"` marks a flow that was pinned to the tab
  roles:  {member: role file text}; the orchestrator is "@orch" (its name follows the tab id)
"""
from __future__ import annotations

import copy
import re
import time
from pathlib import Path

import yaml

from .project import Project, config_home

VERSION = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
ORCH = "@orch"
TAB_MARK = "@tab"
DROP_TAB_KEYS = ("cwd", "title", "worktree")


class TemplateError(ValueError):
    pass


def templates_dir() -> Path:
    return config_home() / "templates"


def path_for(name: str) -> Path:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise TemplateError("템플릿 이름: 영문 소문자로 시작, 소문자/숫자/_- 40자 이하")
    return templates_dir() / f"{name}.yaml"


def tab_flows(data: dict, tab_id: str) -> list[str]:
    """Flows that belong to a tab: pinned to it (`tab:`), or untabbed with every step on its members
    (the same rule as the settings UI's tab copy)."""
    t = (data.get("tabs") or {}).get(tab_id) or {}
    mine = set(t.get("agents") or {}) | set(t.get("shells") or {})
    if isinstance(t.get("orchestrator"), dict):
        mine.add(f"{tab_id.lower()}-orch")
    out = []
    for fname, f in (data.get("flows") or {}).items():
        if not isinstance(f, dict):
            continue
        if f.get("tab"):
            if f["tab"] == tab_id:
                out.append(fname)
            continue
        steps = f.get("steps") if isinstance(f.get("steps"), list) else []
        if steps and all(isinstance(s, dict) and (s.get("agent") or s.get("shell")) in mine for s in steps):
            out.append(fname)
    return out


def build(project: Project, tab_id: str) -> dict:
    """Template content from the project's SAVED config.yaml and role files."""
    data = yaml.safe_load(project.config_path.read_text()) if project.config_path.exists() else None
    if not isinstance(data, dict) or not isinstance((data.get("tabs") or {}).get(tab_id), dict):
        raise TemplateError(f"저장된 설정에 탭 '{tab_id}'이 없습니다 (먼저 저장하세요)")
    tab = copy.deepcopy(data["tabs"][tab_id])
    for k in DROP_TAB_KEYS:
        tab.pop(k, None)
    for spec in (tab.get("shells") or {}).values():
        if isinstance(spec, dict):
            spec.pop("cwd", None)
    for spec in (tab.get("agents") or {}).values():
        if isinstance(spec, dict):
            spec.pop("role_file", None)
    flows = {}
    for fname in tab_flows(data, tab_id):
        f = copy.deepcopy(data["flows"][fname])
        if f.get("tab"):
            f["tab"] = TAB_MARK
        flows[fname] = f
    return {"tab": tab, "flows": flows, "roles": _role_texts(project, data, tab_id)}


def _role_texts(project: Project, data: dict, tab_id: str) -> dict[str, str]:
    """Role files of the tab's orchestrator and agents, read from the raw config (works even when the
    config has errors such as a missing opencode binary)."""
    t = data["tabs"][tab_id]
    files = {}
    if isinstance(t.get("orchestrator"), dict):
        files[ORCH] = project.roles_dir / f"{tab_id.lower()}-orch.md"
    root = project.root.resolve()
    for name, spec in (t.get("agents") or {}).items():
        rf = spec.get("role_file") if isinstance(spec, dict) else None
        f = (project.root / rf).resolve() if isinstance(rf, str) and rf else project.roles_dir / f"{name}.md"
        if f.is_relative_to(root):  # never read outside the project
            files[str(name)] = f
    out = {}
    for key, f in files.items():
        try:
            if f.is_file():
                out[key] = f.read_text()
        except OSError:
            continue
    return out


def save(name: str, content: dict, source: dict | None = None, overwrite: bool = False) -> Path:
    p = path_for(name)
    if p.exists() and not overwrite:
        raise FileExistsError(name)
    doc = {"version": VERSION, "name": name, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "source": source or {}, **content}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120))
    tmp.replace(p)
    return p


def load(name: str) -> dict:
    p = path_for(name)
    if not p.exists():
        names = ", ".join(t["name"] for t in list_templates()) or "없음"
        raise TemplateError(f"템플릿 '{name}'이 없습니다. 저장된 템플릿: {names}")
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise TemplateError(f"템플릿 '{name}'을 읽을 수 없습니다: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("tab"), dict):
        raise TemplateError(f"템플릿 '{name}' 형식이 잘못되었습니다 (tab이 없음)")
    flows = doc.get("flows") or {}
    roles = doc.get("roles") or {}
    if not isinstance(flows, dict) or not isinstance(roles, dict) \
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in roles.items()):
        raise TemplateError(f"템플릿 '{name}' 형식이 잘못되었습니다 (flows/roles)")
    return {"name": name, "saved_at": doc.get("saved_at"), "source": doc.get("source") or {},
            "tab": doc["tab"], "flows": flows, "roles": roles}


def summary(t: dict) -> dict:
    tab = t["tab"]
    return {"name": t["name"], "saved_at": t.get("saved_at"), "source": t.get("source") or {},
            "orch": isinstance(tab.get("orchestrator"), dict),
            "agents": list(tab.get("agents") or {}), "shells": list(tab.get("shells") or {}),
            "flows": list(t.get("flows") or {}), "roles": list(t.get("roles") or {})}


def list_templates() -> list[dict]:
    d = templates_dir()
    out = []
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        try:
            out.append(summary(load(p.stem)))
        except TemplateError:
            continue  # a broken or foreign file: not offered
    return out


def delete(name: str) -> None:
    p = path_for(name)
    if not p.exists():
        raise TemplateError(f"템플릿 '{name}'이 없습니다")
    p.unlink()


def rename(old: str, new: str) -> None:
    src, dst = path_for(old), path_for(new)
    if not src.exists():
        raise TemplateError(f"템플릿 '{old}'이 없습니다")
    if dst.exists():
        raise TemplateError(f"템플릿 '{new}'이 이미 있습니다")
    doc = yaml.safe_load(src.read_text())
    doc["name"] = new
    dst.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120))
    src.unlink()


def instantiate(t: dict, tab_id: str) -> tuple[dict, dict, dict[str, str]]:
    """(tab, flows, role texts by member) for a NEW project: member names stay as saved
    (nothing to collide with); the orchestrator follows the tab id."""
    tab = copy.deepcopy(t["tab"])
    flows = {}
    for fname, f in (t.get("flows") or {}).items():
        f = copy.deepcopy(f)
        if f.get("tab") == TAB_MARK:
            f["tab"] = tab_id
        flows[fname] = f
    texts = {}
    for k, v in (t.get("roles") or {}).items():
        texts[f"{tab_id.lower()}-orch" if k == ORCH else k] = v
    return tab, flows, texts
