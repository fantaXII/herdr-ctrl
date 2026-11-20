"""Opt-in: real claude agent (haiku) through hctrl, in a throwaway herdr session.

Run with HC_LIVE_AGENT=1. Costs a few cheap model calls. claude shows a folder-trust
dialog for the new throwaway folder; the test accepts it (tests/livehelp.py).
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
from livehelp import wait_ready

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")

if os.environ.get("HC_LIVE_AGENT") != "1" or not shutil.which("herdr") or not shutil.which("claude"):
    pytest.skip("set HC_LIVE_AGENT=1 to run the real-agent test", allow_module_level=True)

CONFIG = """
session: {session}
tabs:
  A:
    orchestrator: none
    agents:
      a1: {{ kind: claude, model: haiku, role: "간단한 작업 수행" }}
    shells:
      sh: {{}}
flows:
  hello:
    steps:
      - agent: a1
        prompt: "{{out}} 파일에 정확히 HELLO_HC 한 줄만 쓰세요. 다른 작업은 하지 마세요."
      - shell: sh
        run: "grep -c HELLO_HC {{prev}}"
"""


def hctrl(root, *args, timeout=300):
    env = dict(os.environ, PYTHONPATH=SRC, HC_CONFIG_HOME=str(root / ".cfghome"), HC_READY_GRACE_S="8")
    return subprocess.run([sys.executable, "-m", "herdr_ctl.cli", "--project", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=timeout, cwd=str(root))


def test_real_claude_agent_flow():
    session = f"hctrl-agent-{os.getpid()}"
    root = REPO / ".hctrl-livetest" / session
    (root / ".herdr-flow").mkdir(parents=True)
    (root / ".herdr-flow" / "config.yaml").write_text(textwrap.dedent(CONFIG.format(session=session)))
    h = Herdr(session=session)
    try:
        r = hctrl(root, "up", "--no-watch")
        assert r.returncode == 0, r.stdout + r.stderr
        # a throwaway folder shows claude's folder-trust dialog first: accept it
        assert not wait_ready(h, ["a1"]), "agent did not become ready: " + h.agent_read("a1", lines=30)
        r = hctrl(root, "run", "hello", timeout=300)
        screen = h.agent_read("a1", lines=40) if r.returncode else ""
        assert r.returncode == 0, r.stdout + r.stderr + "\n--- pane ---\n" + screen
        run_dir = sorted((root / ".herdr-flow" / "runs").iterdir())[-1]
        assert "HELLO_HC" in (run_dir / "step-1.md").read_text()
        assert (run_dir / "step-2.log").read_text().strip() == "1"
        rec = json.loads((run_dir / "run.json").read_text())
        assert rec["status"] == "done"
    finally:
        try:
            h.session_stop(session)
            time.sleep(0.5)
            h.session_delete(session)
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)
