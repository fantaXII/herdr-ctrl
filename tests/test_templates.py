"""Team templates: save a saved tab (members, flows, role details) outside the project, reuse it."""
import pytest
import yaml

from herdr_ctl import cli
from herdr_ctl import roles as R
from herdr_ctl import templates as T
from herdr_ctl.config import check_file
from herdr_ctl.init_cmd import run_init_template
from herdr_ctl.project import Project, register
from conftest import always_found, make_project
from test_ui import Client, client, herdr, server  # noqa: F401 (fixtures)

PINNED = """
session: projA
tabs:
  X:
    title: 로그인 팀
    cwd: sub
    worktree: { branch: feat/x }
    orchestrator: { kind: claude, model: opus, goal: "로그인 모듈" }
    agents:
      x1: { kind: claude, model: sonnet, role: "구현", role_file: docs/x1.md }
      x2: { kind: opencode, model: anthropic/claude-sonnet-5-5, role: "테스트" }
    shells:
      xbuild: { cwd: build }
  Y:
    orchestrator: none
    agents:
      y1: { kind: claude, model: haiku }
flows:
  pinned:
    tab: X
    steps:
      - agent: x2
        prompt: "테스트"
  untabbed:
    steps:
      - agent: x1
        prompt: "구현"
      - shell: xbuild
        run: "make"
  other:
    steps:
      - agent: y1
        prompt: "다른 탭"
"""


@pytest.fixture
def proj(tmp_path):
    p = make_project(tmp_path, PINNED)
    (p.root / ".git").mkdir()  # tab X uses a worktree
    (p.root / "sub").mkdir()
    (p.root / "docs").mkdir()
    (p.root / "docs" / "x1.md").write_text("# 역할: 로그인 구현\n\n## 범위\n- 로그인만\n")
    p.roles_dir.mkdir(parents=True, exist_ok=True)
    (p.roles_dir / "x-orch.md").write_text("# 역할: 로그인 총괄\n")
    return p


def mine():
    return [t for t in T.list_templates() if not t["builtin"]]


# ------------------------------------------------------------ module
def test_build_strips_project_keys_and_keeps_roles(proj):
    t = T.build(proj, "X")
    assert not {"cwd", "title", "worktree"} & set(t["tab"])
    assert t["tab"]["shells"]["xbuild"] == {} and "role_file" not in t["tab"]["agents"]["x1"]
    assert set(t["flows"]) == {"pinned", "untabbed"} and t["flows"]["pinned"]["tab"] == T.TAB_MARK
    assert t["roles"] == {"@orch": "# 역할: 로그인 총괄\n", "x1": "# 역할: 로그인 구현\n\n## 범위\n- 로그인만\n"}


def test_role_file_outside_project_is_not_read(proj, tmp_path):
    (tmp_path / "secret.md").write_text("secret")
    proj.config_path.write_text(proj.config_path.read_text().replace("docs/x1.md", "../secret.md"))
    assert "x1" not in T.build(proj, "X")["roles"]


def test_unknown_tab_and_bad_names(proj):
    with pytest.raises(T.TemplateError, match="먼저 저장"):
        T.build(proj, "Z")
    for bad in ("../x", "Team", "", "a" * 41, "1team"):
        with pytest.raises(T.TemplateError):
            T.path_for(bad)


def test_save_list_load_rename_delete(proj):
    T.save("x-team", T.build(proj, "X"), {"project": "projA", "tab": "X"})
    with pytest.raises(FileExistsError):
        T.save("x-team", T.build(proj, "X"))
    T.save("x-team", T.build(proj, "X"), overwrite=True)
    (T.templates_dir() / "broken.yaml").write_text("tab: [1]\n")  # not offered
    [s] = mine()
    assert s["name"] == "x-team" and s["orch"] and s["agents"] == ["x1", "x2"] and s["shells"] == ["xbuild"]
    T.rename("x-team", "login-team")
    assert T.load("login-team")["tab"]["agents"]["x1"]["model"] == "sonnet"
    with pytest.raises(T.TemplateError, match="저장된 템플릿: login-team"):
        T.load("x-team")
    T.delete("login-team")
    assert mine() == []


def test_instantiate_for_new_project(proj):
    T.save("x-team", T.build(proj, "X"))
    tab, flows, texts = T.instantiate(T.load("x-team"), "x-team")
    assert list(tab["agents"]) == ["x1", "x2"] and flows["pinned"]["tab"] == "x-team"
    assert "tab" not in flows["untabbed"] and set(texts) == {"x-team-orch", "x1"}


# ------------------------------------------------------------ hctrl init --template
def test_init_template_creates_valid_config_with_roles(proj, tmp_path, capsys):
    T.save("x-team", T.build(proj, "X"))
    root = tmp_path / "shop"
    root.mkdir()
    p = Project("shop", root)
    assert run_init_template(p, "x-team", ask=lambda q: "", which=always_found)
    cfg, issues = check_file(p.config_path, which=always_found)
    assert cfg is not None and not [i for i in issues if i.level == "error"]
    assert [t.name for t in cfg.tabs] == ["x-team"] and cfg.session == "shop"
    assert R.effective_role(p, cfg.member("x-team-orch"))[0] == "로그인 총괄"
    assert R.effective_role(p, cfg.member("x1")) == ("로그인 구현", "## 범위\n- 로그인만")
    assert R.history(p, "x1")[-1]["reason"] == "템플릿 x-team"
    out = capsys.readouterr().out
    assert "템플릿 x-team: x-team-orch, x1, x2, xbuild" in out and "역할 상세 2개" in out


def test_init_template_missing_and_existing_config(proj, tmp_path, capsys):
    assert not run_init_template(proj, "nope", ask=lambda q: "")
    assert "저장된 템플릿: debug-team, design-review-team, dev-team" in capsys.readouterr().out  # the built-ins are always there
    T.save("x-team", T.build(proj, "X"))
    before = proj.config_path.read_text()
    assert not run_init_template(proj, "x-team", ask=lambda q: "n")  # keep the existing config
    assert proj.config_path.read_text() == before


def test_init_template_with_tab_is_refused(proj, capsys, monkeypatch):
    monkeypatch.setattr(cli, "herdr_installed", lambda: True)
    assert cli.main(["--project", str(proj.root), "init", "--tab", "Z", "--template", "t"]) == 1
    assert "[+ 템플릿에서 추가]" in capsys.readouterr().err


# ------------------------------------------------------------ settings UI API
def login(client, proj):
    register("projA", proj.root)
    return client.json("GET", "/api/project/projA")[1]


def test_api_save_conflict_overwrite_and_manage(client, proj):
    login(client, proj)
    body = {"project": "projA", "tab": "X", "name": "x-team"}
    st, res = client.json("POST", "/api/templates", body)
    assert st == 200 and [t["name"] for t in res["templates"] if not t["builtin"]] == ["x-team"]
    assert client.json("POST", "/api/templates", body)[0] == 409
    assert client.json("POST", "/api/templates", {**body, "overwrite": True})[0] == 200
    assert client.json("POST", "/api/templates", {**body, "name": "../evil"})[0] == 400
    st, t = client.json("GET", "/api/templates/x-team")
    assert st == 200 and t["roles"]["@orch"].startswith("# 역할:")
    assert client.json("POST", "/api/templates/x-team/rename", {"name": "team2"})[1]["templates"][0]["name"] == "team2"
    assert not [t for t in client.json("POST", "/api/templates/team2/delete", {})[1]["templates"] if not t["builtin"]]
    assert client.json("GET", "/api/templates/team2")[0] == 400


def test_save_with_role_texts_creates_role_files(client, proj):
    cfg = login(client, proj)
    data = yaml.safe_load(proj.config_path.read_text())
    data["tabs"]["team"] = {"orchestrator": {"kind": "claude", "model": "opus"},
                            "agents": {"coder": {"kind": "claude", "model": "sonnet", "role": "구현"}},
                            "shells": {"build": {}}}
    texts = {"team-orch": "# 역할: 총괄\n", "coder": "# 역할: 구현\n\n상세\n", "build": "x", "x1": "덮어쓰기 금지"}
    st, res = client.json("PUT", "/api/project/projA/config",
                          {"data": data, "version": cfg["version"], "role_texts": texts})
    assert st == 200, res
    assert sorted(res["copied"]) == ["coder", "team-orch"]  # shell ignored, x1's file already exists
    assert (proj.roles_dir / "coder.md").read_text() == "# 역할: 구현\n\n상세\n"
    assert (proj.root / "docs" / "x1.md").read_text().startswith("# 역할: 로그인 구현")
    assert client.json("PUT", "/api/project/projA/config",
                       {"data": data, "version": res["version"], "role_texts": ["x"]})[0] == 400


# ------------------------------------------------------------ built-in templates
BUILTINS = ["debug-team", "design-review-team", "dev-team"]


def test_builtins_are_listed_after_user_templates(proj):
    assert T.builtin_names() == BUILTINS
    assert [t["name"] for t in T.list_templates()] == BUILTINS and all(t["builtin"] for t in T.list_templates())
    T.save("x-team", T.build(proj, "X"))
    names = [t["name"] for t in T.list_templates()]
    assert names[0] == "x-team" and names[1:] == BUILTINS
    assert all(t["description"] for t in T.list_templates() if t["builtin"])


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_starts_a_valid_project(name, tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    p = Project("shop", root)
    assert run_init_template(p, name, ask=lambda q: "", which=always_found)
    cfg, issues = check_file(p.config_path, which=always_found)
    assert cfg is not None and issues == []
    t = T.load(name)
    assert len(t["tab"]["agents"]) + 1 <= 4  # one herdr tab: orchestrator + agents fit the 2x2 grid
    assert set(t["roles"]) == {"@orch", *t["tab"]["agents"]}  # every member has a role detail
    for m in t["tab"]["agents"]:
        assert R.effective_role(p, cfg.member(m))[1]


def test_builtin_is_read_only_but_can_be_shadowed_and_restored(proj):
    with pytest.raises(T.TemplateError, match="기본 템플릿"):
        T.delete("dev-team")
    with pytest.raises(T.TemplateError, match="기본 템플릿"):
        T.rename("dev-team", "mine")
    T.save("x-team", T.build(proj, "X"))
    with pytest.raises(T.TemplateError, match="이미 있습니다"):
        T.rename("x-team", "dev-team")
    with pytest.raises(FileExistsError):
        T.save("dev-team", T.build(proj, "X"))
    T.save("dev-team", T.build(proj, "X"), overwrite=True, description="내 dev")
    t = T.load("dev-team")
    assert not t["builtin"] and t["overrides_builtin"] and t["description"] == "내 dev"
    assert [x["name"] for x in T.list_templates()].count("dev-team") == 1
    T.delete("dev-team")  # back to the built-in
    assert T.load("dev-team")["builtin"]


def test_api_builtin_protection(client, proj):
    login(client, proj)
    assert client.json("POST", "/api/templates/dev-team/delete", {})[0] == 400
    st, t = client.json("GET", "/api/templates/debug-team")
    assert st == 200 and t["builtin"] and "investigator" in t["tab"]["agents"]
    st, res = client.json("POST", "/api/templates", {"project": "projA", "tab": "X", "name": "x-team",
                                                     "description": "로그인 팀"})
    assert st == 200 and res["templates"][0]["description"] == "로그인 팀"
