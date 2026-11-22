"""Settings UI tab copy: saving with `copies` gives the new members their source's role detail."""
import yaml

from herdr_ctl import roles as R
from herdr_ctl.config import check_file
from herdr_ctl.project import register
from conftest import always_found, make_project
from test_ui import client, herdr, server  # noqa: F401 (fixtures)


def copied_config(p, new_tab="tb"):
    """What the page sends after copying tab ta: members renamed with the new tab id."""
    data = yaml.safe_load(p.config_path.read_text())
    src_id = next(iter(data["tabs"]))
    t = yaml.safe_load(yaml.safe_dump(data["tabs"][src_id]))
    t["agents"] = {f"{n}-{new_tab}": v for n, v in t.get("agents", {}).items()}
    t["shells"] = {f"{n}-{new_tab}": v for n, v in (t.get("shells") or {}).items()}
    data["tabs"][new_tab] = t
    return src_id, data


def setup(client, tmp_path):
    p = make_project(tmp_path)
    register("projA", p.root)
    st, cfg = client.json("GET", "/api/project/projA")
    assert st == 200
    return p, cfg


def test_copies_role_detail_with_history(client, tmp_path):
    p, cfg = setup(client, tmp_path)
    src_id, data = copied_config(p)
    agent = next(iter(data["tabs"][src_id]["agents"]))
    shell = next(iter(data["tabs"][src_id].get("shells") or {}), None)
    m = check_file(p.config_path, which=always_found)[0].member(agent)
    R.write_role(p, m, "결제 구현", "## 범위\n- 결제만")
    copies = {f"{agent}-tb": agent}
    if shell:
        copies[f"{shell}-tb"] = shell  # shells have no role: ignored
    st, res = client.json("PUT", "/api/project/projA/config",
                          {"data": data, "version": cfg["version"], "copies": copies})
    assert st == 200, res
    assert res["copied"] == [f"{agent}-tb"]
    new = check_file(p.config_path, which=always_found)[0].member(f"{agent}-tb")
    assert R.effective_role(p, new) == ("결제 구현", "## 범위\n- 결제만")
    assert R.history(p, f"{agent}-tb")[-1]["reason"] == f"탭 복사: {agent}"


def test_existing_role_file_is_kept_and_missing_source_skipped(client, tmp_path):
    p, cfg = setup(client, tmp_path)
    src_id, data = copied_config(p)
    agents = list(data["tabs"][src_id]["agents"])
    (p.roles_dir).mkdir(parents=True, exist_ok=True)
    (p.roles_dir / f"{agents[0]}-tb.md").write_text("# 역할: 내 것\n")
    R.write_role(p, check_file(p.config_path, which=always_found)[0].member(agents[0]), "원본", "x")
    copies = {f"{a}-tb": a for a in agents}  # agents[1] (if any) has no role file
    st, res = client.json("PUT", "/api/project/projA/config",
                          {"data": data, "version": cfg["version"], "copies": copies})
    assert st == 200 and res["copied"] == []
    assert (p.roles_dir / f"{agents[0]}-tb.md").read_text() == "# 역할: 내 것\n"


def test_bad_copies_refused(client, tmp_path):
    p, cfg = setup(client, tmp_path)
    _, data = copied_config(p)
    st, res = client.json("PUT", "/api/project/projA/config",
                          {"data": data, "version": cfg["version"], "copies": ["x"]})
    assert st == 400 and "copies" in res["error"]


def test_project_reports_git(client, tmp_path):
    p, _ = setup(client, tmp_path)
    assert client.json("GET", "/api/project/projA")[1]["git"] is False
    (p.root / ".git").mkdir()
    assert client.json("GET", "/api/project/projA")[1]["git"] is True
