import re
import textwrap
from pathlib import Path

import pytest

from herdr_ctl import orchestrator as orchestrator_mod
from herdr_ctl import project as project_mod

from herdr_ctl.config import check_file
from herdr_ctl.fake import FakeHerdr
from herdr_ctl.project import Project
from herdr_ctl.state import State
from herdr_ctl.up import Ctx

OUT_RE = re.compile(r"이 파일에 쓰세요: (\S+)")

SAMPLE = """
session: projA
tabs:
  X:
    orchestrator: { kind: claude, model: opus, goal: "로그인 모듈" }
    role_update: confirm
    limits: { max_steps_per_ask: 3, max_minutes_per_ask: 60, max_retries_per_agent: 2 }
    agents:
      x1: { kind: claude, model: sonnet, role: "구현" }
      x2: { kind: opencode, model: anthropic/claude-sonnet-5-5, role: "테스트" }
    shells:
      xbuild: {}
  Y:
    orchestrator: none
    agents:
      y1: { kind: claude, model: haiku, permission: auto }
flows:
  build-review-x:
    steps:
      - agent: x1
        prompt: "기능 X 구현. 결과 요약을 {out} 에 써줘"
      - agent: x2
        prompt: "{prev} 를 읽고 테스트 추가"
      - shell: xbuild
        run: "echo building; exit 3"
        on_fail: continue
      - agent: x1
        prompt: "빌드 로그 {prev} 검토"
  shell-only:
    steps:
      - shell: xbuild
        run: "echo hello"
"""


def always_found(_):
    return "/usr/bin/x"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_CONFIG_HOME", str(tmp_path / "cfghome"))
    monkeypatch.setattr(project_mod, "STATE_HOME_OVERRIDE", tmp_path / "statehome")
    monkeypatch.setenv("HC_HOOK_GRACE_S", "0")
    monkeypatch.setenv("HCTRL_NO_UPDATE_CHECK", "1")  # never reach GitHub from tests
    monkeypatch.setenv("HC_SETTLE_S", "0")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))  # never read real transcripts
    # actor = own env only: pytest itself may run inside a herdr pane
    monkeypatch.setattr(orchestrator_mod, "_ancestor_envs", lambda max_depth=30: iter(()))
    monkeypatch.setenv("HC_READY_GRACE_S", "0")
    monkeypatch.setenv("HC_STALL_WATCH_S", "0")
    for k in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HC_ACTOR_PANE", "HC_PROJECT"):
        monkeypatch.delenv(k, raising=False)


def make_project(tmp_path: Path, text: str = SAMPLE, name: str = "projA") -> Project:
    root = tmp_path / name
    (root / ".herdr-flow").mkdir(parents=True)
    (root / ".herdr-flow" / "config.yaml").write_text(textwrap.dedent(text))
    return Project(name, root)


def make_ctx(project: Project, h=None) -> Ctx:
    cfg, issues = check_file(project.config_path, which=always_found)
    assert cfg is not None, issues
    return Ctx(project, cfg, h or FakeHerdr("projA"), State(project))


def writes_out(content="결과"):
    def cb(agent, text):
        m = OUT_RE.search(text)
        if m:
            Path(m.group(1)).write_text(content + f" by {agent.name}\n")
        return "done"
    return cb


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


@pytest.fixture
def ctx(project):
    return make_ctx(project)


REAL_ANCESTOR_ENVS = orchestrator_mod._ancestor_envs
