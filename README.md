# herdr_ctl

> v0.1.0 구현 완료 (herdr 0.9.1에서 확인). 설계 상세와 구현 메모는 [PLAN.md](PLAN.md)를 참고하세요.

[herdr](https://herdr.dev) 위에서 동작하는 보조 도구입니다.

명령은 **`hc`** 하나입니다(긴 이름 `herdr_ctl`도 같은 명령).

- **`hc`** (인자 없음): 프로젝트 폴더에서 실행하면 그 프로젝트의 herdr 세션만 바로 엽니다.
- **`hc <하위 명령>`**: YAML 설정에 따라 다음을 처리합니다.
  - 탭과 화면 분할을 만들고 agent를 실행합니다.
  - claude → opencode → bash → claude처럼 앞 단계의 결과를 다음 단계로 자동으로 넘기며 실행합니다.

## 요구 사항

- herdr 0.9.1 이상
- Python 3.10 이상
- 사용할 agent의 herdr 연동이 설치돼 있어야 합니다. 확인 방법은 다음과 같습니다.
  ```bash
  herdr integration status        # claude, opencode가 current인지 확인
  herdr integration install <kind> # 필요하면 설치
  ```

## 설치

```bash
cd ~/study/BackEnd/herdr_ctl
pipx install -e .       # hc(=herdr_ctl) 명령 등록 (의존성: PyYAML)
# 테스트: python3 -m pytest -q   (실제 claude까지: HC_LIVE_AGENT=1 python3 -m pytest -q)
```

## 1. 프로젝트 열기 (`hc`)

### 가장 간단한 사용법

```bash
cd ~/study/BackEnd/projA     # 하위 폴더여도 됨
hc                           # = herdr_ctl
```

→ projA 전용 herdr 세션이 바로 열리고, **projA의 창만 보입니다.**
- 처음 열 때 `.herdr-flow/config.yaml`이 있으면 탭, 분할, agent까지 자동으로 구성됩니다.
- 창을 닫아도 세션은 유지됩니다. 다음에 `hc`를 실행하면 하던 화면 그대로 이어집니다.

### 프로젝트 등록

프로젝트를 찾는 기준은 두 가지입니다.

1. `~/.config/herdr_ctl/projects.yaml`에 등록된 경로
2. 그렇지 않으면 `.herdr-flow/config.yaml`이 있는 폴더

```bash
cd ~/study/BackEnd/projA
hc --add projA        # 현재 폴더를 projA로 등록
```

```yaml
# ~/.config/herdr_ctl/projects.yaml
projects:
  projA: ~/study/BackEnd/projA
  herdr_ctl: ~/study/BackEnd/herdr_ctl
```

### 그 밖의 명령

```bash
hc projA      # 어디서든 이름으로 열기
hc --list     # 등록된 프로젝트와 세션 상태
```

- 등록되지 않은 폴더에서 실행하면 "등록할까요?"를 묻고, 등록 → `hc init` → 검사 → 세션 열기까지 이어서 진행합니다. 섞여 있는 default 세션으로 대신 열지 않습니다.
- herdr 화면 안에서는 실행하지 않습니다. herdr 안에 herdr가 겹쳐 뜨기 때문입니다. 일반 터미널에서 실행하세요.

## 2. 화면 배치 설정 (`.herdr-flow/config.yaml`)

### 만들기: `hc init`

직접 쓰지 않아도 됩니다. 질문에 답하면 생성됩니다.

```
$ hc init
탭 이름? X
orchestrator? [claude/opus] / none
  프로젝트 목표 한 줄? (선택) > 로그인/결제 모듈 개발
작업 agent 수? 2
  x1 종류/모델? claude / sonnet
  x1 역할? [구현] >
  x2 종류/모델? opencode / (기본)
  x2 역할? [테스트 작성] >
빌드 쉘 추가? y → 빌드 명령? npm run build
기본 flow(구현→테스트→빌드→검토) 만들까요? y
✔ .herdr-flow/config.yaml 생성
검사 중... ✔ 오류 0, 경고 0
```

탭을 추가할 때는 `hc init --tab Y`를 씁니다.

### 검사: `hc check` (자동)

- `hc init`이 끝나면 **자동으로** 검사합니다. 오류가 있으면 해당 항목만 다시 물어봅니다.
- `hc`, `hc up`, `hc run`, `hc ask`를 실행하기 직전에도 **자동으로** 검사합니다. yaml을 직접 고친 경우도 여기서 잡힙니다.
- 오류가 있으면 실행하지 않고, 경고는 보여주고 계속합니다.
- 언제든 `hc check`로 수동 검사할 수 있습니다.

```
$ hc check
✘ tabs.X.agents.x2.modle: 알 수 없는 키 → 'model'인가요?
✘ flows.build-review-x.steps[2].agent: 'x3' 없음 (탭 X의 agent: x1, x2)
⚠ tabs.Y: pane 5개 → 'Y-2' 탭으로 1개 넘어감
오류 2, 경고 1
```

검사 항목은 다음과 같습니다.
- 문법, 오타, 필수 항목
- 이름 규칙과 중복
- 지원하지 않는 agent 종류
- flow가 없는 agent를 참조하는지
- 탭당 4개 초과
- herdr 연동이나 실행 파일 미설치
- 모델 이름 형식

모델 이름이 실제로 존재하는지는 agent를 띄울 때(`hc up`) 확인됩니다.

### 파일 구조 예시

프로젝트 루트에 둡니다.

```yaml
session: projA
tabs:
  X:                       # 기능 X 작업 탭
    orchestrator: { kind: claude, model: opus, goal: "로그인/결제 모듈 개발" }   # 큰 창 (none이면 없음)
    role_update: confirm   # LLM의 역할 갱신: confirm | auto | off
    worktree: { branch: feat/x }   # 선택: 이 탭은 별도 폴더/브랜치에서 작업
    limits: { max_steps_per_ask: 20, max_minutes_per_ask: 120, max_retries_per_agent: 3 }
    agents:
      x1: { kind: claude,   model: sonnet, role: "구현", permission: edit }
      x2: { kind: opencode, model: anthropic/claude-sonnet-5-5, role: "테스트" }
    shells:
      xbuild: {}           # 빌드용 bash 창
  Y:                       # 기능 Y 작업 탭 (orchestrator 없이 hc만 사용)
    orchestrator: none
    agents:
      y1: { kind: claude, model: opus }
      y2: { kind: opencode }                     # model 생략 시 agent 기본값
      y3: { kind: claude, model: haiku, permission: auto }
```

### 권한 (`permission`)

agent가 파일을 고치거나 명령을 실행할 때마다 승인을 요청하면 자동화가 멈춥니다. 그래서 agent별로 권한을 정합니다.

| 값 | 의미 |
|---|---|
| `ask` | 모든 작업에서 승인 요청 |
| `edit` (작업 agent 기본) | 파일 수정은 자동, 명령 실행은 승인 요청 |
| `auto` | 대부분 자동 |
| `readonly` (orchestrator 기본) | 읽기와 `hc` 명령만 |

모든 권한을 우회하는 옵션은 지원하지 않습니다.

### 작업 폴더 분리 (`worktree`, 선택)

탭 X와 Y가 같은 폴더에서 동시에 작업하면 파일이 충돌합니다. 탭에 `worktree: { branch: feat/x }`를 주면 그 탭은 별도 폴더와 브랜치에서 작업합니다. 이때 그 탭은 herdr에서 별도 workspace로 열립니다. 병합은 직접 하거나 orchestrator에게 시킵니다.

### 모델 지정

- `model`: agent의 모델을 지정합니다. claude는 `opus`, `sonnet`, `haiku` 또는 전체 모델 ID를 쓰고, opencode는 `제공자/모델` 형식입니다.
- `args`: 그 밖의 agent 실행 옵션입니다. agent 프로그램에 그대로 전달됩니다.

### orchestrator와 작업 agent

| 역할 | 하는 일 |
|---|---|
| orchestrator (큰 창) | 흐름을 시작하고(`hc run`), 결과를 검토하고, 실패하면 작업 agent에게 수정을 지시합니다. 직접 코딩은 하지 않습니다. |
| 작업 agent (작은 창) | 구현, 테스트 등 지시받은 일을 하고 결과를 파일로 남깁니다. |
| `hc` (화면 밖) | 정해진 순서를 확실하게 실행하고 결과 파일을 넘깁니다. |

- orchestrator는 자기 탭의 agent에게만 지시하고, 승인 요청(`blocked`)은 사용자에게 넘깁니다.
- 운영 규칙은 `hc up`이 `.herdr-flow/orchestrator-<tab>.md`로 만들어 줍니다. 내용을 직접 고쳐도 됩니다.

### 역할 설정

| 단계 | 언제 / 누가 | 방법 |
|---|---|---|
| 한 줄 역할 | `hc init` 때 | agent마다 "구현", "테스트 작성" 같은 한 줄 (Enter를 치면 기본값) |
| 상세 프롬프트 | 나중에, 사람이 | `hc role x1 --edit` (직접 작성) 또는 `hc role x1 --draft` (LLM이 초안 작성 → 검토 후 저장) |
| 진행 중 갱신 | 프로젝트 진행 중, orchestrator가 | 상황을 보고 자기 탭 agent의 역할을 조정 |

```bash
hc role                         # 역할 목록
hc role x1 "결제 모듈 구현"      # 한 줄 역할 변경
hc role x1 --edit               # 상세 프롬프트 편집 (.herdr-flow/roles/x1.md)
hc role x1 --draft              # LLM(claude -p, HC_DRAFT_CMD로 변경 가능)이 초안 작성
hc role x1 --apply              # 실행 중인 agent에게 바뀐 역할 알리기
hc role --review                # orchestrator가 제안한 변경 승인/거절
hc role x1 --history            # 변경 이력 / --revert <n>으로 되돌리기
```

LLM이 역할을 바꾸는 범위는 탭 설정 `role_update`로 정합니다.

| 값 | 동작 |
|---|---|
| `confirm` (기본) | orchestrator가 바꾸려 하면 **제안**으로 저장되고 알림이 뜹니다. `hc role --review`에서 승인해야 반영됩니다. |
| `auto` | 바로 반영하고 알림만 띄웁니다. |
| `off` | LLM은 역할을 바꿀 수 없습니다. |

- orchestrator는 자기 탭 agent의 역할만 바꿀 수 있고, 자기 자신의 기본 규칙은 바꿀 수 없습니다.
- 작업 agent는 역할을 직접 고치지 않고, 결과 파일에 제안만 남깁니다.
- 모든 변경은 이력으로 남습니다.

### 진행 중 설정 변경 (사용자가 yaml이나 역할 파일을 수정한 경우)

세션이 열려 있는 동안 `config.yaml`이나 `roles/*.md`를 고치면 **자동으로 감지해서 알려줍니다.**

```
🔔 herdr 알림: 설정 변경 감지 — x1 역할, x2 모델. 'hc changes'로 확인하세요.

$ hc changes
x1: 역할 변경 (roles/x1.md, +12 -3)
  [a] 알리기  [r] 재시작  [s] 건너뛰기  [d] diff 보기 > r
x2: model sonnet → opus  (재시작 필요)
  [r] 재시작  [s] 나중에 > r
```

| 변경 | 처리 |
|---|---|
| 역할 / 상세 프롬프트 | 실행 중인 agent에게 **알리기** 또는 **재시작** 중 선택 |
| model / kind / args | **재시작 필요** |
| agent 추가 / 삭제 | 창 추가 / 확인 후 종료 |
| flow만 변경 | 다음 `hc run`부터 적용 (알림만) |

재시작(`hc restart x1`)은 다음 순서로 진행됩니다.

1. 작업 중이면 끝날 때까지 기다립니다.
2. 기존 agent가 지금까지의 작업을 `handoff/x1.md`에 요약합니다.
3. 같은 창, 같은 이름으로 새 설정의 agent를 띄웁니다.
4. 새 agent에게 새 역할과 인수인계 파일을 전달합니다.

재시작하면 대화 맥락은 사라지지만, 인수인계 파일 덕분에 이어서 작업할 수 있습니다. 인수인계를 생략하려면 `--no-handoff`를 붙입니다. orchestrator에게도 재시작 사실이 자동으로 전달됩니다.

설정에 오류가 있으면 반영하지 않고 오류만 알립니다.

### 탭당 창 개수 규칙

| 개수 | 배치 |
|---|---|
| 1 | 전체 화면 |
| 2 | 좌우 분할 |
| 3 (권장) | 왼쪽 큰 창 + 오른쪽 위/아래 |
| 4 (최대) | 2×2 격자 |
| 5 이상 | 남는 창은 자동으로 `X-2`, `X-3` … 탭으로 넘어감 |

쉘 창과 orchestrator도 개수에 포함됩니다. orchestrator는 항상 1번(큰 창) 자리에 옵니다.

```
3개                              4개 (위 예시의 탭 X)
┌──────────────┬─────────┐       ┌──────────────┬─────────┐
│              │   x1    │       │ orchestrator │   x1    │
│ orchestrator ├─────────┤       ├──────────────┼─────────┤
│              │   x2    │       │      x2      │ xbuild  │
└──────────────┴─────────┘       └──────────────┴─────────┘
```

## 3. 창 띄우기와 보기

```bash
hc up           # 설정된 모든 탭/창/agent 생성 (이미 있으면 건너뜀)
hc up X         # 탭 X만
hc status       # 탭별 agent 상태: idle / working / blocked / done / unknown
hc zoom x2      # x2 창만 크게 보기 (한 번 더 실행하면 분할 화면으로 복귀)
hc down X       # 탭 X의 agent와 창 정리
```

## 4. 파이프라인 정의와 실행

같은 `config.yaml`에 `flows`를 추가합니다.

```yaml
flows:
  build-review-x:
    steps:
      - agent: x1
        prompt: "기능 X 구현. 결과 요약을 {out} 에 써줘"
      - agent: x2
        prompt: "{prev} 를 읽고 테스트 추가. 요약은 {out}"
      - shell: xbuild
        run: "npm run build"
        on_fail: continue      # 실패해도 다음 단계로 (stop이면 중단)
        max_retries: 0         # 실패 시 같은 단계 재시도 횟수
      - agent: x1
        prompt: "빌드 로그 {prev} 를 검토하고 문제 있으면 수정"
        timeout: 1800s
```

| 치환자 | 의미 |
|---|---|
| `{out}` | 이번 단계 결과 파일 경로 (`.herdr-flow/runs/<run-id>/step-N.md`, 쉘은 `.log`) |
| `{prev}` | 직전 단계 결과 파일 경로 |

실행은 다음과 같이 합니다.

```bash
hc run build-review-x
```

```
[1/4] x1 (claude)    ▶ 작업 중...  ✔ 완료  step-1.md
[2/4] x2 (opencode)  ▶ 작업 중...  ✔ 완료  step-2.md
[3/4] xbuild (bash)  ▶ npm run build  ✘ exit 1  step-3.log
[4/4] x1 (claude)    ▶ 작업 중...  ✔ 완료  step-4.md
```

- 실행 중에도 각 창에서 agent가 일하는 모습을 볼 수 있습니다.
- **직접 입력은 해당 agent가 잠겨 있지 않을 때만** 하세요. `hc status`에 "잠금: run 0042 사용 중"으로 표시됩니다. 실행 중에 직접 입력하면 완료 판단이 틀어질 수 있습니다.
- agent가 `done`이 돼도 결과 파일(`{out}`)을 쓰지 않았으면 그 단계는 실패로 처리됩니다.
- 서로 다른 탭의 flow는 동시에 실행할 수 있습니다. 예: `hc run build-review-x`와 `hc run build-review-y`
- orchestrator가 있는 탭에서는 orchestrator 창에 "build-review-x 돌리고 결과 검토해줘"라고 말로 시켜도 됩니다. orchestrator는 내부적으로 `hc run --detach`와 `hc wait <run-id>`를 사용합니다.

## 4.1 A / B 실행 구분

**어떤 명령으로 시작하느냐**로 나뉩니다.

| | A. 정해진 순서 | B. orchestrator 지휘 |
|---|---|---|
| 시작 | `hc run <flow>` | `hc ask <tab> "<지시>"` 또는 orchestrator 창에 직접 입력 |
| 동작 | YAML 순서대로 기계적으로 실행 | orchestrator가 판단해서 flow를 돌리거나 agent에게 직접 지시, 결과 검토와 재시도까지 |
| 적합한 일 | 빌드 → 검토 같은 반복 작업 | "X 기능 완성해줘"처럼 판단이 필요한 일 |
| 조건 | 항상 가능 | 탭에 orchestrator가 있어야 함 (`none`이면 오류) |

```bash
hc run build-review-x               # A
hc ask X "로그인 기능 완성하고 빌드까지 통과시켜줘"   # B
```

orchestrator가 있는 탭에서 `hc run`을 써도 됩니다. 이때 orchestrator는 개입하지 않습니다.

### agent 하나에 직접 지시 / 중단

```bash
hc send x1 "로그인 버튼 스타일 수정"           # 잠금 확인 후 지시하고, 끝날 때까지 대기
hc send x1 "..." --detach                     # 바로 job-id 반환 (orchestrator가 주로 사용)
hc wait <job-id>
hc stop <run-id|job-id>                       # 실행 중인 flow나 지시 중단
```

orchestrator에는 상한(`limits`)이 걸립니다. 한 번의 `hc ask`에서 지시할 수 있는 횟수와 시간이 정해져 있고, 넘으면 `hc`가 멈추고 알려줍니다. LLM이 이 상한을 무시할 수 없습니다.

## 5. 멈췄을 때

| 상황 | 동작 | 대응 |
|---|---|---|
| agent가 승인을 요청함 (`blocked`) | 실행이 멈추고 herdr 알림이 뜸 | 해당 창에서 직접 승인한 뒤 `hc resume <run-id>` |
| 쉘 명령 실패 | `on_fail` 설정에 따라 계속하거나 중단 | 로그 확인 후 `hc resume <run-id>` |
| 시간 초과 | 실행이 멈춤. 프롬프트를 자동으로 재전송하지 **않음** | `hc status`로 상태 확인 |
| 중단하고 싶을 때 | `hc stop <run-id>` (agent에게 중단 신호, 잠금 해제) | 필요하면 `hc resume <run-id>` |
| orchestrator 상한 도달 | 추가 지시를 거부하고 알림 | 확인 후 `hc ask`로 다시 지시 |

실행 기록은 `.herdr-flow/runs/<run-id>/`에 단계별 결과 파일과 `run.json`으로 남습니다.

## 하루 사용 흐름

```bash
cd ~/study/BackEnd/projA && hc   # 프로젝트 세션 열기 (첫 실행 시 창 자동 구성)
hc run build-review-x          # A: 정해진 순서대로 자동 실행
hc ask X "로그인 기능 완성해줘"   # B: orchestrator에게 맡기기
hc zoom x1              # 필요할 때 크게 보기
```

## 참고

- 결과는 화면을 읽어서 넘기지 않고 **파일**로 주고받습니다. 정확도를 위해서입니다.
- 세션 안의 `_hc` 탭은 hc 전용입니다(설정 감시 `hc watch`가 실행 중). 닫지 마세요.
- `--detach` 실행은 별도 백그라운드 프로세스로 돌고, 로그는 `.herdr-flow/runs/<id>/worker.log`에 남습니다.
- 한 줄 역할을 바꾸면(`hc role x1 "..."`) yaml이 아니라 `.herdr-flow/roles/x1.md` 첫 줄(`# 역할: ...`)에 저장됩니다. 이 값이 yaml의 `role`보다 우선합니다.
- 쉘 단계는 pane의 쉘 종류와 관계없이 `bash -c`(pipefail)로 실행됩니다.
- `hc stop`은 실행 프로세스가 10초(`HC_STOP_GRACE_S`) 안에 멈추지 않으면 강제 종료합니다. 터미널에서 직접 `hc run` 중인 프로세스도 해당됩니다.
- 방금 띄운 agent에는 첫 지시 전 최대 8초 기다립니다(`HC_READY_GRACE_S`). 지시가 반응 없이 끝나면 자동 재전송하지 않고 실패로 기록합니다(`hc resume <id> --resend`로 직접 재전송).
- herdr 밖에서 `hc`를 실행할 때는 `--session <project>`로 대상 세션을 지정합니다.
- 설계 상세: [PLAN.md](PLAN.md)
