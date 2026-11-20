"""End-to-end against a real herdr binary, in a throwaway session (shell panes only, no LLM).

Skipped when herdr is missing or HC_SKIP_LIVE=1.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from herdr_ctl.herdr import Herdr

pytestmark = pytest.mark.live
HERDR = shutil.which("herdr")
SRC = str(Path(__file__).resolve().parents[1] / "src")

if not HERDR or os.environ.get("HC_SKIP_LIVE") == "1":
    pytest.skip("real herdr not available", allow_module_level=True)


CONFIG = """
session: {session}
tabs:
  T:
    orchestrator: none
    shells:
      s1: {{}}
      s2: {{}}
      s3: {{}}
flows:
  build:
    steps:
      - shell: s1
        run: "echo compiling; exit 7"
        on_fail: continue
      - shell: s2
        run: "cat {{prev}} | wc -l"
  slow:
    steps:
      - shell: s3
        run: "sleep 30"
"""


@pytest.fixture(scope="module")
def live_project(tmp_path_factory):
    session = f"hctrl-test-{os.getpid()}"
    root = tmp_path_factory.mktemp("liveproj")
    (root / ".herdr-flow").mkdir()
    (root / ".herdr-flow" / "config.yaml").write_text(textwrap.dedent(CONFIG.format(session=session)))
    yield session, root
    h = Herdr()
    try:
        h.session_stop(session)
    except Exception:
        pass
    time.sleep(0.5)
    try:
        h.session_delete(session)
    except Exception:
        pass


def hctrl(root, *args, timeout=60):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["HC_CONFIG_HOME"] = str(root / ".cfghome")
    return subprocess.run([sys.executable, "-m", "herdr_ctl.cli", "--project", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=timeout, cwd=str(root))


def test_live_end_to_end(live_project):
    session, root = live_project
    h = Herdr(session=session)

    # up from outside the session: headless server start + layout
    r = hctrl(root, "up")
    assert r.returncode == 0, r.stderr + r.stdout
    assert h.session_running(session)
    state = json.loads((root / ".herdr-flow" / "state.json").read_text())
    tabs = {t["label"] for t in h.tab_list(state["workspace_id"])}
    assert {"_hc", "T"} <= tabs
    panes = {p["pane_id"]: p for p in h.pane_list()}
    for name in ("s1", "s2", "s3"):
        assert state["members"][name]["pane_id"] in panes
        assert panes[state["members"][name]["pane_id"]].get("label") == f"hctrl:{name}"
    n_panes = len(panes)

    # idempotent
    r = hctrl(root, "up")
    assert r.returncode == 0 and len(h.pane_list()) == n_panes, r.stdout

    # flow: exit code via PIPESTATUS, {prev} passing
    r = hctrl(root, "run", "build", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "exit 7" in r.stdout
    run_dir = sorted((root / ".herdr-flow" / "runs").iterdir())[-1]
    assert "compiling" in (run_dir / "step-1.log").read_text()
    assert (run_dir / "step-2.log").read_text().strip() == "1"

    # second run in the same panes must not match the old completion marker
    r = hctrl(root, "run", "build", timeout=120)
    assert r.returncode == 0 and "exit 7" in r.stdout, r.stdout

    # detach + wait
    r = hctrl(root, "run", "build", "--detach")
    assert r.returncode == 0, r.stderr
    rid = r.stdout.strip().splitlines()[-1]
    r = hctrl(root, "wait", rid, "--timeout", "60", timeout=90)
    assert r.returncode == 0 and "done" in r.stdout, r.stdout + r.stderr

    # stop a long detached run
    r = hctrl(root, "run", "slow", "--detach")
    rid = r.stdout.strip().splitlines()[-1]
    time.sleep(2)
    r = hctrl(root, "stop", rid)
    assert r.returncode == 0 and "stopped" in r.stdout, r.stdout + r.stderr
    assert not (root / ".herdr-flow" / "var" / "locks" / "s3.lock").exists()
    rec = json.loads((root / ".herdr-flow" / "runs" / rid / "run.json").read_text())
    from herdr_ctl.state import _pid_alive
    assert not _pid_alive(rec["pid"])  # the old worker is gone (ctrl+c trap ended the wait)
    r = hctrl(root, "resume", rid, "--detach")  # allowed now, then stop it again
    assert r.returncode == 0, r.stderr
    time.sleep(1.5)
    assert hctrl(root, "stop", rid).returncode == 0

    # status + zoom
    r = hctrl(root, "status")
    assert r.returncode == 0 and "[T]" in r.stdout and "s1" in r.stdout
    assert hctrl(root, "zoom", "s1").returncode == 0

    # control pane is running the watcher
    ctl = state["control"]["pane_id"]
    assert not h.pane_shell_idle(ctl)

    # down closes the tab
    r = hctrl(root, "down")
    assert r.returncode == 0, r.stderr
    assert all(p["pane_id"] != state["members"]["s1"]["pane_id"] for p in h.pane_list())


WT_CONFIG = """
session: {session}
tabs:
  W:
    worktree: {{ branch: feat/hctrl-wt }}
    shells:
      w1: {{}}
  M:
    shells:
      m1: {{}}
"""


def test_live_worktree(tmp_path):
    session = f"hctrl-wt-{os.getpid()}"
    root = tmp_path / f"hcwt{os.getpid()}"
    root.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (root / ".herdr-flow").mkdir()
    (root / ".herdr-flow" / "config.yaml").write_text(textwrap.dedent(WT_CONFIG.format(session=session)))
    h = Herdr(session=session)
    try:
        r = hctrl(root, "up", "--no-watch")
        assert r.returncode == 0, r.stdout + r.stderr
        state = json.loads((root / ".herdr-flow" / "state.json").read_text())
        wt = state["worktrees"]["W"]
        assert wt["workspace_id"] != state["workspace_id"]
        assert wt["cwd"] and Path(wt["cwd"]).exists() and Path(wt["cwd"]).resolve() != root.resolve()
        branches = subprocess.run(["git", "-C", str(root), "worktree", "list"], capture_output=True,
                                  text=True).stdout
        assert "feat/hctrl-wt" in branches
        assert state["members"]["w1"]["cwd"] == str(Path(wt["cwd"]).resolve())
        assert state["members"]["m1"]["workspace_id"] == state["workspace_id"]
        r = hctrl(root, "up", "--no-watch")  # idempotent, no second worktree
        assert r.returncode == 0
        assert subprocess.run(["git", "-C", str(root), "worktree", "list"], capture_output=True,
                              text=True).stdout.count("\n") == branches.count("\n")
        r = hctrl(root, "down", "W", "--remove-worktree")
        assert r.returncode == 0 and "실패" not in r.stdout, r.stdout + r.stderr
        assert "feat/hctrl-wt" not in subprocess.run(["git", "-C", str(root), "worktree", "list"],
                                                   capture_output=True, text=True).stdout
    finally:
        # remove any worktree checkout herdr created outside tmp_path (~/.herdr/worktrees/...)
        listing = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                                 capture_output=True, text=True).stdout
        for line in listing.splitlines():
            if line.startswith("worktree ") and Path(line[9:]).resolve() != root.resolve():
                subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", line[9:]],
                               capture_output=True)
        try:
            (Path.home() / ".herdr" / "worktrees" / root.name).rmdir()  # empty parent herdr leaves
        except OSError:
            pass
        try:
            h.session_stop(session)
            time.sleep(0.5)
            h.session_delete(session)
        except Exception:
            pass
