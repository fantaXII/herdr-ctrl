"""hctrl ui: local server security, API, launcher flow."""
import http.client
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_ctl import cli
from herdr_ctl import ui as ui_mod
from herdr_ctl.fake import FakeHerdr
from herdr_ctl.orchestrator import ScopeError
from herdr_ctl.project import load_registry, register
from herdr_ctl.ui import UIServer
from conftest import always_found, make_project


class FakeSessions:
    """herdr stand-in: session list + integration status (+ a FakeHerdr per session for tab renames)."""

    def __init__(self, running=()):
        self.running = set(running)
        self.sessions: dict[str, FakeHerdr] = {}

    def __call__(self, session=None):
        if session is None:
            return self
        return self.sessions.setdefault(session, FakeHerdr(session))

    def session_list(self):
        return [{"name": n, "status": "running"} for n in self.running]

    def integration_status(self):
        return {}


class Client:
    def __init__(self, server: UIServer, host=None):
        self.server = server
        self.host = host or f"127.0.0.1:{server.port}"
        self.cookie = None

    def req(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        h = {"Host": self.host}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r, payload

    def login(self, link=None):
        link = link or self.server.new_link()
        r, _ = self.req("GET", link.split(str(self.server.port), 1)[1])
        assert r.status == 303
        self.cookie = r.getheader("Set-Cookie").split(";")[0]
        return r

    def json(self, method, path, body=None, **kw):
        r, payload = self.req(method, path, body, **kw)
        return r.status, json.loads(payload or b"null")


@pytest.fixture
def herdr():
    return FakeSessions()


@pytest.fixture
def server(tmp_path, herdr):
    s = UIServer("launcher", cwd=tmp_path, herdr=herdr, which=always_found,
                 models=lambda: {"zai": ["zai/glm-5.3"]}).start()
    yield s
    s.stop()


@pytest.fixture
def client(server):
    c = Client(server)
    c.login()
    return c


# ------------------------------------------------------------ security
def test_binds_loopback_only(server):
    assert server.httpd.server_address[0] == "127.0.0.1"


def test_token_is_one_time_and_sets_strict_cookie(server):
    c = Client(server)
    link = server.new_link()
    r = c.login(link)
    cookie = r.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    r2, _ = Client(server).req("GET", link.split(str(server.port), 1)[1])
    assert r2.status == 403  # reused token


def test_no_cookie_or_bad_cookie_is_refused(server):
    c = Client(server)
    assert c.req("GET", "/")[0].status == 403
    assert c.req("GET", "/api/projects")[0].status == 403
    c.cookie = "hctrl_ui=wrong"
    assert c.req("GET", "/api/projects")[0].status == 403


def test_foreign_host_is_refused(server):
    c = Client(server)
    c.login()
    c.host = f"evil.example:{server.port}"  # DNS rebinding
    assert c.req("GET", "/api/projects")[0].status == 403
    c.host = f"localhost:{server.port}"
    assert c.req("GET", "/api/projects")[0].status == 200


def test_writes_need_json_and_same_origin(client, tmp_path):
    body = {"path": str(tmp_path)}
    st, _ = client.json("POST", "/api/projects", body, headers={"Origin": "http://evil.example"})
    assert st == 403
    r, _ = client.req("POST", "/api/projects", raw=b"path=/tmp", headers={"Content-Type": "text/plain"})
    assert r.status == 415
    st, _ = client.json("POST", "/api/projects", body,
                        headers={"Origin": f"http://127.0.0.1:{client.server.port}"})
    assert st == 200


def test_page_has_no_external_resources(client):
    r, payload = client.req("GET", "/")
    assert r.status == 200
    html = payload.decode()
    assert not re.search(r"""(src|href)\s*=\s*["']?https?://""", html)
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")


# ---------------------------------------------------------------- projects
def test_projects_list_and_add(client, tmp_path, herdr):
    p = make_project(tmp_path)
    register("projA", p.root)
    herdr.running = {"projA"}
    st, rows = client.json("GET", "/api/projects")
    assert st == 200 and rows == [{"name": "projA", "root": str(p.root.resolve()), "has_config": True,
                                   "exists": True, "running": True}]
    new = tmp_path / "shop"
    new.mkdir()
    st, res = client.json("POST", "/api/projects", {"path": str(new)})
    assert st == 200 and res["name"] == "shop" and load_registry()["shop"] == str(new.resolve())
    st, res = client.json("POST", "/api/projects", {"path": str(tmp_path / "nope")})
    assert st == 400
    st, res = client.json("POST", "/api/projects", {"path": str(tmp_path), "name": "shop"})
    assert st == 409  # name taken by another folder


# ------------------------------------------------------------------ config
def test_config_check_does_not_write(client, tmp_path):
    p = make_project(tmp_path)
    register("projA", p.root)
    before = p.config_path.read_text()
    st, cfg = client.json("GET", "/api/project/projA")
    assert st == 200 and cfg["exists"] and cfg["issues"] == [] and cfg["data"]["session"] == "projA"
    data = cfg["data"]
    data["tabs"]["X"]["agents"]["x2"]["modle"] = "x"
    st, res = client.json("POST", "/api/project/projA/check", {"data": data})
    assert st == 200 and any("modle" in i["path"] for i in res["issues"])
    assert p.config_path.read_text() == before


def test_config_save_refuses_errors_and_keeps_unmodelled_keys(client, tmp_path):
    p = make_project(tmp_path)
    register("projA", p.root)
    _, cfg = client.json("GET", "/api/project/projA")
    bad = json.loads(json.dumps(cfg["data"]))
    bad["tabs"]["X"]["agents"]["x1"]["kind"] = "nosuchagent"
    st, _ = client.json("PUT", "/api/project/projA/config", {"data": bad, "version": cfg["version"]})
    assert st == 422
    good = cfg["data"]
    good["tabs"]["X"]["agents"]["x1"]["model"] = "opus"
    st, res = client.json("PUT", "/api/project/projA/config", {"data": good, "version": cfg["version"]})
    assert st == 200
    saved = yaml.safe_load(p.config_path.read_text())
    assert saved["tabs"]["X"]["agents"]["x1"]["model"] == "opus"
    assert saved["tabs"]["X"]["limits"]["max_steps_per_ask"] == 3  # not modelled by the UI, kept
    # stale version: someone edited the file in between
    st, _ = client.json("PUT", "/api/project/projA/config", {"data": good, "version": cfg["version"]})
    assert st == 409


def test_new_project_gets_default_config(client, tmp_path):
    root = tmp_path / "my app"
    root.mkdir()
    register("my-app", root)
    st, cfg = client.json("GET", "/api/project/my-app")
    assert st == 200 and not cfg["exists"] and cfg["version"] is None and cfg["issues"] == []
    st, res = client.json("PUT", "/api/project/my-app/config", {"data": cfg["data"], "version": None})
    assert st == 200 and (root / ".herdr-flow" / "config.yaml").exists()
    assert ".herdr-flow/runs/" in (root / ".gitignore").read_text()


def test_save_title_renames_live_tabs(client, tmp_path, herdr):
    from herdr_ctl.up import up
    from conftest import make_ctx
    p = make_project(tmp_path)
    register("projA", p.root)
    ctx = make_ctx(p, herdr("projA"))
    up(ctx, start_watch=False)
    herdr.running = {"projA"}
    _, cfg = client.json("GET", "/api/project/projA")
    cfg["data"]["tabs"]["X"]["title"] = "로그인"
    st, res = client.json("PUT", "/api/project/projA/config", {"data": cfg["data"], "version": cfg["version"]})
    assert st == 200 and res["renamed"] == ["X → 로그인"]
    assert "로그인" in {t["label"] for t in herdr("projA").tab_list()}


def test_models(client):
    st, m = client.json("GET", "/api/models")
    assert st == 200 and m["claude"] == ["opus", "sonnet", "haiku"]
    assert m["opencode"] == {"zai": ["zai/glm-5.3"]} and "opencode" in m["kinds"]


def test_opencode_models_parsing():
    class R:
        stdout = "zai-coding-plan/glm-5.3\nzai-coding-plan/glm-5.3-flash\nanthropic/claude-sonnet-5-5\nnoise line\n"
    got = ui_mod.opencode_models(lambda *a, **k: R())
    assert got == {"zai-coding-plan": ["zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"],
                   "anthropic": ["anthropic/claude-sonnet-5-5"]}

    def missing(*a, **k):
        raise FileNotFoundError("opencode")
    assert ui_mod.opencode_models(missing) == {}


def test_role_detail_roundtrip_records_history(client, tmp_path):
    from herdr_ctl import roles as R
    p = make_project(tmp_path)
    register("projA", p.root)
    st, role = client.json("GET", "/api/project/projA/role/x1")
    assert st == 200 and role["one"] == "구현"
    st, role = client.json("PUT", "/api/project/projA/role/x1", {"one": "결제 구현", "detail": "## 범위\n- 결제"})
    assert st == 200 and role == {"one": "결제 구현", "detail": "## 범위\n- 결제"}
    assert len(R.history(p, "x1")) == 1
    assert client.json("GET", "/api/project/projA/role/nobody")[0] == 404
    assert client.json("GET", "/api/project/projA/role/xbuild")[0] == 404  # shells have no role
    assert client.json("GET", "/api/project/nope/role/x1")[0] == 404


# ------------------------------------------------------------------ open
def test_open_returns_choice(client, server, tmp_path):
    p = make_project(tmp_path)
    register("projA", p.root)
    st, res = client.json("POST", "/api/open", {"name": "projA"})
    assert st == 200 and res["ok"]
    assert server.wait(1).root == p.root.resolve()


def test_open_refuses_project_without_valid_config(client, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    register("empty", root)
    st, _ = client.json("POST", "/api/open", {"name": "empty"})
    assert st == 409


def test_session_mode_cannot_open(tmp_path, herdr):
    p = make_project(tmp_path)
    s = UIServer("session", start=p, herdr=herdr, which=always_found).start()
    try:
        c = Client(s)
        c.login()
        st, _ = c.json("POST", "/api/open", {"name": "projA"})
        assert st == 409
        st, cfg = c.json("GET", "/api/project/projA")  # unregistered start project is still editable
        assert st == 200 and cfg["exists"]
    finally:
        s.stop()


# --------------------------------------------------------------- browser
@pytest.mark.parametrize("env,wsl,which,expect", [
    ({"HC_BROWSER": "firefox --new-tab"}, False, (), ["firefox", "--new-tab", "U"]),
    ({"HC_BROWSER": "none"}, True, ("wslview",), None),
    ({}, True, ("wslview", "explorer.exe"), ["wslview", "U"]),
    ({}, True, ("explorer.exe",), ["explorer.exe", "U"]),
    ({}, True, (), None),
    ({"DISPLAY": ":0"}, False, (), ["<webbrowser>", "U"]),
    ({}, False, (), None),
])
def test_browser_command(monkeypatch, env, wsl, which, expect):
    for k in ("HC_BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "WSL_DISTRO_NAME"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ui_mod, "is_wsl", lambda: wsl)
    monkeypatch.setattr(ui_mod.shutil, "which", lambda exe: f"/bin/{exe}" if exe in which else None)
    assert ui_mod.browser_command("U") == expect


# ------------------------------------------------------------------- CLI
def test_unregistered_folder_non_tty_keeps_old_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "tty", lambda: False)
    assert cli.main([]) == 1
    assert "hctrl --add" in capsys.readouterr().err


def test_unregistered_folder_tty_starts_launcher_then_opens_choice(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    register("projA", p.root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cli, "tty", lambda: True)
    seen = {}

    def fake_launcher(cwd, start=None):
        seen["cwd"], seen["start"] = cwd, start
        return p
    monkeypatch.setattr(ui_mod, "run_launcher", fake_launcher)
    fake = FakeHerdr("projA")
    fake.binary = "herdr"
    fake.session_running = lambda name: True
    monkeypatch.setattr(cli, "make_herdr", lambda session: fake)
    from herdr_ctl.up import up
    from conftest import make_ctx
    up(make_ctx(p, fake), start_watch=False)
    cd_file = tmp_path / "cd"
    monkeypatch.setenv("HC_CD_FILE", str(cd_file))
    execs = []
    monkeypatch.setattr(cli.os, "execvpe", lambda *a: execs.append(a))
    assert cli.main([]) == 0
    assert seen == {"cwd": elsewhere, "start": None}
    assert execs and execs[0][1] == ["herdr", "--session", "projA"]
    assert Path.cwd() == p.root.resolve() and cd_file.read_text() == str(p.root.resolve())


def test_launcher_closed_without_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "tty", lambda: True)
    monkeypatch.setattr(ui_mod, "run_launcher", lambda cwd, start=None: None)
    assert cli.main([]) == 0


def test_registered_without_config_starts_launcher_on_it(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    root.mkdir()
    register("shop", root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "tty", lambda: True)
    seen = {}
    monkeypatch.setattr(ui_mod, "run_launcher", lambda cwd, start=None: seen.setdefault("start", start) and None)
    assert cli.main([]) == 0
    assert seen["start"].name == "shop"


def test_ui_refused_in_agent_pane(monkeypatch):
    monkeypatch.setattr(cli, "_managed_pane", lambda: {"type": "orch"})
    with pytest.raises(ScopeError):
        cli.cmd_ui(cli.build_parser().parse_args(["ui"]))
    assert cli.main(["ui"]) == 1


def test_ui_inside_herdr_is_settings_only(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    monkeypatch.chdir(p.root)
    monkeypatch.setenv("HERDR_ENV", "1")
    called = {}
    monkeypatch.setattr(ui_mod, "run_settings", lambda project: called.setdefault("p", project) and 0)
    monkeypatch.setattr(ui_mod, "run_launcher", lambda *a, **k: pytest.fail("launcher inside herdr"))
    assert cli.main(["ui"]) == 0
    assert called["p"].root == p.root.resolve()


def test_shell_init_wrapper(tmp_path, capsys):
    assert cli.main(["shell-init", "bash"]) == 0
    script = capsys.readouterr().out
    assert "HC_CD_FILE" in script and "command hctrl" in script
    # the wrapper really cds: fake hctrl writes the target dir into HC_CD_FILE
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = tmp_path / "proj dir"
    target.mkdir()
    fake = bindir / "hctrl"
    fake.write_text(f'#!/bin/sh\nprintf %s "{target}" > "$HC_CD_FILE"\nexit 7\n')
    fake.chmod(0o755)
    out = subprocess.run(["bash", "-c", script + '\nhctrl; echo "rc=$? pwd=$PWD"'],
                         capture_output=True, text=True, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"})
    assert f"rc=7 pwd={target}" in out.stdout


def test_token_expires(server, monkeypatch):
    link = server.new_link()
    monkeypatch.setattr(ui_mod, "TOKEN_TTL_S", 0)  # only affects new links
    late = server.new_link()
    c = Client(server)
    r, _ = c.req("GET", late.split(str(server.port), 1)[1])
    assert r.status == 403
    assert c.login(link).status == 303


def test_announce_hides_token_when_browser_opened(server, monkeypatch, capsys):
    monkeypatch.setattr(ui_mod, "open_browser", lambda url: True)
    url = ui_mod.announce(server)
    out = capsys.readouterr().out
    assert "?t=" in url and "?t=" not in out
    monkeypatch.setattr(ui_mod, "open_browser", lambda url: False)
    url = ui_mod.announce(server)
    assert url in capsys.readouterr().out  # no browser: the user needs the full link


@pytest.mark.parametrize("raw,length", [(b"[1, 2]", None), (b"{bad", None), (b"{}", "abc"), (b"{}", "-1")])
def test_malformed_write_requests_get_4xx(client, raw, length):
    headers = {"Content-Type": "application/json"}
    if length is not None:
        headers["Content-Length"] = length
    r, _ = client.req("PUT", "/api/project/x/config", raw=raw, headers=headers)
    assert 400 <= r.status < 500


def test_unexpected_error_is_json_500(client, server, monkeypatch):
    def boom(*a):
        raise TypeError("not serialisable")
    monkeypatch.setattr(server, "list_projects", boom)
    st, body = client.json("GET", "/api/projects")
    assert st == 500 and "TypeError" in body["error"]


def test_expired_tokens_are_purged(server, monkeypatch):
    monkeypatch.setattr(ui_mod, "TOKEN_TTL_S", 0)
    server.new_link()
    server.new_link()
    assert len(server._tokens) == 1


def test_cd_file_not_passed_to_herdr(monkeypatch):
    from herdr_ctl.herdr import clean_env, server_env
    monkeypatch.setenv("HC_CD_FILE", "/tmp/x")
    assert "HC_CD_FILE" not in clean_env() and "HC_CD_FILE" not in server_env()
