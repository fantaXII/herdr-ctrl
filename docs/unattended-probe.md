# 무인 모드 실측 기록 (P0/P1)

측정일: 2026-11-15. 환경: herdr 0.9.1, Claude Code v2.1.316 (haiku), opencode 1.19.4 (GLM-4.7), WSL2.
버리는 herdr 세션(`hctrl-p0`, `hctrl-p1`)에서 `hctrl up`으로 agent를 띄우고 `herdr agent prompt/read/send-keys`로 측정했다.

## 1. 화면 기반 (herdr 상태 + 키 입력)

| 상황 | herdr 상태 | 화면 단서 | 응답 키 | 결과 |
|---|---|---|---|---|
| claude 폴더 신뢰 대화상자 (시작 시) | blocked | "Quick safety check", 커서가 "No, exit"에 있음 | `down enter` (숫자 무효) | 수락. **Enter만 보내면 agent가 종료됨** |
| claude Bash 승인 | blocked (3–7초 뒤) | "Bash command", 점선 사이 명령, "Do you want to proceed?" 1 Yes / 2 항상 허용 / 3 auto mode 전환 / 4 No | `1` | 실행, 권한 모드 유지 |
| claude 거부 (Esc) | blocked → done | "Interrupted · What should Claude do instead?" | `esc` | 실행 안 됨, **턴 종료** |
| claude Write 승인 | blocked | "Do you want to create X?" 1 / 2(accept edits 전환) / 3 No. VS Code diff도 열림 | `1` | 생성됨 |
| AskUserQuestion | blocked (권한 요청과 같은 상태) | "☐ 제목", 1..n 선택지, "Type something.", "Chat about this" | 숫자 | 즉시 제출 후 진행 |
| 텍스트로 질문하고 턴 종료 | done | 마지막 메시지가 질문 | (후속 지시) | **상태만으로는 완료와 구분 불가** |
| opencode bash 승인 | blocked (~25초 뒤) | "△ Permission required", "Allow once / Allow always / Reject" | `enter` | Allow once로 실행 |
| opencode 거부 | blocked → done | — | `esc` | 실행 안 됨 |

- 권한 대화상자에는 영구적으로 권한을 넓히는 선택지("항상 허용", "auto mode 전환", "Allow always")가 기본으로 들어 있다. 응답 키를 LLM이 고르게 하면 안 된다.
- `blocked`는 권한 요청, 질문, 신뢰 대화상자를 모두 포함한다. 화면 문구로만 구분할 수 있다.
- herdr `agent get`에는 요청 내용이 없다. claude transcript JSONL 마지막 줄에는 대기 중인 `tool_use`(이름, 입력)가 있다.
- 프롬프트를 보낸 직후 상태가 잠시 이전 턴의 `done`으로 남아 있다. 완료 판정 전에 `working`으로 바뀌는 것을 먼저 기다려야 한다.
- (live 테스트에서 발견) `herdr agent prompt --wait`는 제출 후 **처음 관측한** idle/done에서 반환한다. claude가 생각을 마치고 도구를 호출하기 직전에 잠깐 done/idle을 보일 때가 있다. 그래서 결과 파일이 없을 때 바로 실패로 처리하면, 그 직후 뜬 승인 대화상자(훅 질문)를 놓친다. runner는 결과 파일이 없으면 `HC_SETTLE_S`(기본 3초) 동안 상태를 더 지켜본다.
- 폴더 신뢰 대화상자를 수락한 직후 claude는 몇 초 더 초기화한다. 이때 보낸 프롬프트는 `agent_prompt_stalled`가 된다.

## 2. claude PermissionRequest 훅 (`--settings`로 주입)

| 항목 | 결과 |
|---|---|
| 호출 시점 | 대화상자를 띄우는 것과 **동시에** 훅이 실행된다. 화면에 대화상자가 그대로 보이므로 사람이 먼저 답할 수도 있다 |
| 입력 (stdin JSON) | `hook_event_name`, `tool_name`, `tool_input`, `permission_mode`, `permission_suggestions`(영구 규칙 제안), `session_id`, `transcript_path`, `cwd` |
| 환경 변수 | `HERDR_PANE_ID`, `HERDR_SESSION`, `HERDR_SOCKET_PATH`, `CLAUDE_PROJECT_DIR`, agent 시작 시 넘긴 `HC_*` 포함 → 훅이 어느 agent인지 알 수 있다 |
| 블록 대기 | 훅이 40초 이상 결정을 기다려도 정상 동작한다(훅 timeout 120초로 설정). 대기 중 herdr 상태는 `blocked` |
| allow 반환 | "Allowed by PermissionRequest hook", 대화상자 닫히고 실행된다. 권한 모드 유지 |
| deny + message 반환 | "Denied by PermissionRequest hook", **턴은 계속되고** 모델이 Reason 문구를 받아 대응한다 (Esc와 다름) |
| deny (message 없음) | 실행 안 됨, 모델이 거부를 인지하고 턴 종료 |
| 무출력 | 대화상자로 넘어간다 (사람이 처리) |
| AskUserQuestion | **이 훅을 거친다.** `tool_input.questions`가 구조화되어 들어온다. `allow` + `updatedInput`에 `answers: {질문: 선택 label}`을 넣으면 "User answered Claude's questions"로 처리되고 진행된다 |
| acceptEdits 모드 | `echo > file` 같은 흔한 파일 명령은 묻지 않고 승인한다. 훅도 호출되지 않는다 |

## 3. hctrl 기존 동작에서 발견한 문제

- `permission: ask`는 인자를 넘기지 않아서, claude가 계정 기본값인 auto mode로 시작했다. `--permission-mode default`를 명시하면 manual mode로 시작한다.
- hctrl은 permission 인자를 사용자 `args` 뒤에 붙인다. 그래서 `args`에 넣은 `--permission-mode`가 덮어써지고, `hctrl check`는 경고하지 않는다.
- README는 `edit`를 "명령 실행은 승인 요청"이라고 설명하지만, 흔한 파일 명령은 승인 없이 실행된다.
- 처음 보는 폴더에서는 신뢰 대화상자 때문에 `blocked` 상태로 시작한다. `tests/test_live_agent.py`의 주석과 다르다.

## 4. 설계 결론

- claude 작업 agent의 승인/질문은 **PermissionRequest 훅**으로 처리한다. 화면 파싱이나 키 입력이 필요 없고, 입력이 구조화되어 있으며, 실패하면 자연스럽게 대화상자(사람)로 넘어간다.
- opencode는 훅이 없어 화면 파싱과 고정 키 입력이 필요하다. 1차 범위에서 제외하고 지금처럼 사람에게 넘긴다.
- 텍스트로 묻고 끝나는 질문은 결과 파일 규약(`## QUESTION`)으로 감지한다.
