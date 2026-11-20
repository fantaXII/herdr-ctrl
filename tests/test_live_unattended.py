"""Opt-in: unattended mode end to end with real herdr + claude haiku (HC_LIVE_AGENT=1).

Covers the three question kinds through the real hctrl CLI: a permission request answered via the
PermissionRequest hook, an AskUserQuestion answered via the hook, and a `## QUESTION` result file
answered by the runner. Answers come from `hctrl answer` as the user (the orchestrator path is the
same command with a scope check, covered by unit tests). Costs a few cheap model calls.
"""
import os
import re
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
  X:
    orchestrator: {{ kind: claude, model: haiku }}
    unattended: approve
    unattended_policy: {{ answer_timeout: 240s }}
    agents:
      x1: {{ kind: claude, model: haiku, role: "지시받은 작업만 정확히 수행" }}
"""


@pytest.fixture
def live(tmp_path):
    session = f"hctrl-un-{os.getpid()}"
    root = REPO / ".hctrl-livetest" / session
    (root / ".herdr-flow").mkdir(parents=True)
    (root / ".herdr-flow" / "config.yaml").write_text(textwrap.dedent(CONFIG.format(session=session)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(PYTHONPATH=SRC, HC_CONFIG_HOME=str(tmp_path / "cfg"), HC_READY_GRACE_S="8")

    def hctrl(*args, timeout=400):
        return subprocess.run([sys.executable, "-m", "herdr_ctl.cli", "--project", str(root), *args],
                              capture_output=True, text=True, env=env, timeout=timeout, cwd=str(root))
    h = Herdr(session=session)
    try:
        r = hctrl("up", "--no-watch")
        assert r.returncode == 0, r.stdout + r.stderr
        assert not wait_ready(h, ["x1", "x-orch"]), "agents did not become ready"
        yield hctrl, h, root
    finally:
        try:
            h.session_stop(session)
            time.sleep(0.5)
            h.session_delete(session)
        except Exception:
            pass
        from herdr_ctl.project import Project
        shutil.rmtree(Project(session, root).secure_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def ask_and_answer(hctrl, h, prompt, kind, answer):
    r = hctrl("send", "x1", prompt, "--detach")
    assert r.returncode == 0, r.stderr
    rid = r.stdout.strip().splitlines()[-1]
    r = hctrl("wait", rid, "--timeout", "240")
    screen = h.agent_read("x1", lines=40)
    assert r.returncode == 3, r.stdout + r.stderr + "\n--- pane ---\n" + screen
    r = hctrl("pending")
    m = re.search(r"(q\d{4}) \[" + kind + r"\]", r.stdout)
    assert m, r.stdout
    r = hctrl("answer", m.group(1), *answer)
    assert r.returncode == 0, r.stdout + r.stderr
    r = hctrl("wait", rid, "--timeout", "300")
    assert r.returncode == 0, r.stdout + r.stderr + "\n--- pane ---\n" + h.agent_read("x1", lines=40)
    out = re.search(r"(step-1\.md)", r.stdout)
    assert out, r.stdout
    return rid


def test_unattended_three_kinds(live):
    hctrl, h, root = live
    # 1) permission request via hook (acceptEdits still asks for `python3 -c`)
    rid = ask_and_answer(hctrl, h, "Bash 도구로 정확히 이 명령을 실행하세요: "
                                "python3 -c \"open('live_ok.txt','w').write('LIVE')\" . "
                                "다른 방법을 쓰지 말고, 실행 후 결과 요약을 결과 파일에 쓰세요.",
                         "권한", ["allow"])
    assert (root / "live_ok.txt").read_text() == "LIVE"
    # 2) AskUserQuestion via hook
    rid = ask_and_answer(hctrl, h, "AskUserQuestion 도구로 '색을 고르세요' 질문을 하세요 (선택지: 빨강, 파랑). "
                                "받은 답(색 이름)만 결과 파일에 쓰세요.", "선택", ["2"])
    assert "파랑" in (root / ".herdr-flow" / "runs" / rid / "step-1.md").read_text()
    # 3) text question in the result file
    rid = ask_and_answer(hctrl, h, "도구로 아무것도 조사하지 말고, 결과 파일의 첫 줄에 '## QUESTION', 둘째 줄에 "
                                "'좋아하는 숫자는?'이라고만 쓰고 턴을 끝내세요. 답을 받으면 그 숫자만 결과 파일에 쓰세요.",
                         "텍스트", ["7"])
    assert "7" in (root / ".herdr-flow" / "runs" / rid / "step-1.md").read_text()
