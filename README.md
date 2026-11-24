# herdr_ctl

> 구현 완료 (herdr 0.9.1에서 확인). 버전은 `hctrl --version` 또는 GitHub Releases 참고. 설계 상세와 구현 메모는 [PLAN.md](PLAN.md)를 참고하세요.

[herdr](https://herdr.dev) 위에서 동작하는 보조 도구입니다.

명령은 **`hctrl`** 하나입니다(긴 이름 `herdr_ctl`도 같은 명령).

- **`hctrl`** (인자 없음): 프로젝트 폴더에서 실행하면 그 프로젝트의 herdr 세션만 바로 엽니다.
- **`hctrl <하위 명령>`**: YAML 설정에 따라 다음을 처리합니다.
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

Linux, macOS, Windows(WSL 안에서)에서 동작합니다. hctrl은 herdr를 제어하는 도구라서 **herdr를 먼저 설치**합니다. herdr가 없으면 `hctrl init`이 설치 방법을 안내하고 종료합니다.

```bash
curl -fsSL https://herdr.dev/install.sh | sh   # 또는 brew install herdr / mise use -g herdr
herdr --version                                # 0.9.1 이상
```

그다음 hctrl을 아래 세 가지 방법 중 하나로 설치합니다. 모두 `python3` 3.10 이상이 필요합니다(`python3 --version`으로 확인).

| 방법 | 이런 경우 | 필요한 것 |
|---|---|---|
| [A. 명령 한 줄 (추천)](#a-명령-한-줄로-설치-추천) | 대부분 | curl 또는 wget |
| [B. 직접 다운로드](#b-직접-다운로드해서-설치) | 스크립트를 실행하기 싫거나, 인터넷이 막힌 PC에 옮겨 설치 | 브라우저 |
| [C. 개발용 설치](#c-개발용-설치-소스-수정) | hctrl 소스를 고치면서 사용 | git, pipx |

### A. 명령 한 줄로 설치 (추천)

```bash
curl -fsSL https://raw.githubusercontent.com/fantaXII/herdr-ctrl/main/install.sh | sh
```

설치 스크립트가 하는 일:
1. `python3`가 3.10 이상인지 확인합니다.
2. [GitHub Releases](https://github.com/fantaXII/herdr-ctrl/releases)의 최신 `hctrl.pyz`를 받습니다. PyYAML까지 들어 있는 실행 파일 하나라서 pip, git이 필요 없습니다.
3. 받은 파일이 실제로 실행되는지 확인한 뒤 `~/.local/bin/hctrl`에 설치하고, `herdr_ctl`도 같은 명령으로 연결합니다.
4. `~/.local/bin`이 PATH에 없으면 추가할 줄을 알려 줍니다. herdr가 없으면 설치 명령도 알려 줍니다.

설치 확인:
```bash
hctrl --version        # hctrl (herdr_ctl) 2.1.0
```

`hctrl: command not found`가 나오면 `~/.local/bin`이 PATH에 없는 것입니다. 다음을 실행한 뒤 새 터미널을 엽니다.
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc    # zsh면 ~/.zshrc
```

| 하고 싶은 것 | 명령 |
|---|---|
| 업데이트 | `hctrl update` (v2.2.0 다음 버전부터) 또는 설치 명령을 다시 실행 |
| 새 버전 확인만 | `hctrl update --check` |
| 특정 버전 설치 | `curl -fsSL .../install.sh \| HCTRL_VERSION=v2.1.0 sh` |
| 다른 위치에 설치 | `curl -fsSL .../install.sh \| HCTRL_BIN_DIR=~/bin sh` |
| 삭제 | `rm ~/.local/bin/hctrl ~/.local/bin/herdr_ctl` |

**업데이트와 새 버전 알림**
- 업데이트해도 설정은 그대로입니다. 바뀌는 것은 `~/.local/bin/hctrl` 파일 하나뿐이고, 프로젝트의 `.herdr-flow/`, 프로젝트 목록(`~/.config/herdr_ctl/`), 상태(`~/.local/state/herdr_ctl/`)는 건드리지 않습니다. 이미 떠 있는 세션의 `_hc` 탭은 설치된 파일이 바뀐 것을 알아채고 새 버전으로 스스로 다시 시작합니다. 설정 화면을 열어 둔 동안에는 편집 내용을 지키려고 화면을 닫을 때까지 기다리며, 설정 화면의 `지금 적용`으로 바로 바꿀 수도 있습니다.
- hctrl은 하루 한 번 GitHub에 최신 릴리스를 백그라운드로 확인합니다(실패하면 조용히 넘어가고 1시간 뒤 재시도). 새 버전이 있으면 다음 위치에 알려 줍니다: `hctrl` / `hctrl ui`를 실행한 터미널, 설정 화면 상단 배지(`⬆ v현재 → v최신 업데이트 가능`. 누르면 `지금 업데이트` / `지금 확인` 버튼이 있습니다), 세션의 `_hc` 탭(herdr 알림 포함), `hctrl --version`. agent 창에서는 확인하지 않습니다.
- `hctrl update`는 릴리스의 `hctrl.pyz.sha256` 체크섬과 받은 파일의 버전이 맞을 때만 교체합니다. [C. 개발용 설치](#c-개발용-설치-소스-수정)라면 소스 폴더에서 `git pull`을 하면 됩니다(`-e` 설치라 바로 반영).
- 확인을 끄려면 `HCTRL_NO_UPDATE_CHECK=1`을 설정합니다.

> 예전에 `pipx install -e .`로 설치했다면 설치 스크립트가 덮어쓰지 않고 멈춥니다. `pipx uninstall herdr-ctl`을 먼저 실행하세요.

### B. 직접 다운로드해서 설치

1. [Releases 페이지](https://github.com/fantaXII/herdr-ctrl/releases/latest)의 **Assets**에서 `hctrl.pyz`를 받습니다.
   터미널에서 받으려면:
   ```bash
   curl -fLO https://github.com/fantaXII/herdr-ctrl/releases/latest/download/hctrl.pyz
   ```
   Windows 브라우저로 받았다면 WSL에서 `/mnt/c/Users/<사용자>/Downloads/hctrl.pyz` 경로로 보입니다.
2. PATH에 있는 폴더로 옮기고 실행 권한을 줍니다.
   ```bash
   mkdir -p ~/.local/bin
   mv hctrl.pyz ~/.local/bin/hctrl
   chmod +x ~/.local/bin/hctrl
   ln -sf hctrl ~/.local/bin/herdr_ctl     # 긴 이름도 쓰려면 (선택)
   ```
3. 확인합니다. 안 되면 A의 PATH 안내를 보세요.
   ```bash
   hctrl --version
   ```

설치하지 않고 받은 자리에서 바로 써도 됩니다: `python3 hctrl.pyz --version`.

### C. 개발용 설치 (소스 수정)

```bash
git clone https://github.com/fantaXII/herdr-ctrl.git && cd herdr-ctrl
pipx install -e .       # hctrl(=herdr_ctl) 명령 등록, 소스 수정이 바로 반영됨 (의존성: PyYAML)
# 테스트: python3 -m pytest -q   (실제 claude까지: HC_LIVE_AGENT=1 python3 -m pytest -q)
# 단일 파일 빌드: python3 scripts/build_pyz.py  → dist/hctrl.pyz
#   (같은 e2e를 pyz로: HC_TEST_PYZ=dist/hctrl.pyz python3 -m pytest -q tests/test_live.py)
```

릴리스(`npm run release`)로 GitHub Release가 만들어지면 `.github/workflows/release-pyz.yml`이 `hctrl.pyz`를 빌드해 그 Release에 첨부합니다.

### 설치 후: agent 연동 확인

```bash
herdr integration status          # 쓸 agent(claude, opencode)가 current인지 확인
herdr integration install claude  # 필요하면 설치
```

이제 [처음 사용하기 (UI)](#처음-사용하기-ui)로 넘어가세요.

## 처음 사용하기 (UI)

yaml을 직접 쓰지 않고 브라우저 화면에서 프로젝트를 등록하고 설정해서 여는 방법입니다. **herdr 화면 밖의 일반 터미널**에서 진행합니다.

**1. 프로젝트 폴더에서 `hctrl` 실행**

```bash
cd ~/work/my-app
hctrl
```

- 처음 보는 폴더라서 브라우저에 **런처** 화면이 열립니다. 터미널에는 `✔ hctrl UI: 브라우저를 열었습니다 (...)`가 출력됩니다.
- 브라우저가 자동으로 열리지 않으면 터미널에 나온 주소를 **2분 안에** 브라우저에 붙여 넣습니다. 이 주소는 한 번만 쓸 수 있습니다. 자세한 동작은 아래 [브라우저 열기](#11-설정-화면-hctrl-ui) 표를 보세요.
- 언제든 `Ctrl+C`로 끝낼 수 있습니다.

**2. 프로젝트 추가**

- 런처 아래쪽 **[+ 프로젝트 추가]**의 "폴더 경로"에 지금 폴더가 이미 들어 있습니다.
- 이름을 비우면 폴더 이름(`my-app`)으로 등록됩니다.
- **[추가하고 설정]**을 누르면 설정 화면으로 넘어갑니다.

**3. 설정 만들기**

화면 왼쪽의 **단계 메뉴**(① 프로젝트 → ② 탭 구성 → ③ flow)를 따라가며 오른쪽에서 고칩니다. 대상마다 색이 정해져 있어서 메뉴, 카드, 미리보기, flow 그림에서 같은 색으로 보입니다: 🟣 orchestrator · 🔵 작업 agent · 🟢 쉘 창 · 🟠 flow.

| 단계 | 할 일 |
|---|---|
| ① 프로젝트 | 세션 이름을 정하고, **구성 한눈에 보기**에서 탭마다 어떤 창이 있는지 확인합니다. 카드를 누르면 그 탭으로 이동합니다. |
| ② 탭 구성 | 왼쪽 메뉴에서 탭을 고르거나 **[+ 탭 추가]**로 만듭니다(orchestrator + agent 2 + 쉘로 시작, `hctrl init`과 같은 기본값). 메뉴의 탭 이름 옆 색 점이 그 탭의 창 구성입니다. |
| └ 빠른 구성 | 탭 화면 맨 위 띠에서 agent 수 **[1] [2] [3] [4]**, orchestrator, 쉘 창을 켜고 끕니다. 늘리면 coder → tester → reviewer → helper 순으로 추가되고, 줄이면 뒤에서부터 지웁니다(고친 내용이 있을 때만 확인). 한 화면은 orchestrator 포함 4칸입니다. |
| └ 기본 정보 | 탭 ID(명령용, 기본 `work1`, `work2`…, 영문 또는 숫자로 시작하므로 `1`, `2`도 됨), 표시 이름(herdr 탭에 보이는 이름, 예: `로그인 작업`), 작업 위치(프로젝트 폴더의 현재 브랜치 또는 별도 worktree). |
| └ 🟣 orchestrator | 탭을 지휘할 LLM입니다. "사용"을 체크하면 켜지고(기본 claude/opus), 끄면 `hctrl run` / `send`로만 지시합니다. 권한·역할 정책·무인 모드는 **고급 설정**에 접혀 있고, 기본값이 아니면 접힌 줄에 요약이 보입니다. |
| └ 🔵 작업 agent | agent마다 카드 하나입니다. 기본 이름은 `coder`, `tester`, `reviewer`, `helper`이고(다른 탭과 겹치면 `coder2`…), 카드의 이름 칸에서 자유롭게 바꿀 수 있습니다(flow에도 자동 반영). 역할, 종류 → (opencode면 제공자 →) 모델, (opencode면) 주 agent, 권한을 고르고, 긴 지시는 **[상세]**에서 씁니다([상세]는 한 번 저장한 뒤 사용). |
| └ 🟢 쉘 창 | 빌드나 테스트 명령을 돌릴 bash 창입니다(선택). flow의 쉘 단계가 여기서 실행됩니다. |
| ③ flow | 여러 단계를 정해진 순서로 실행하는 작업 흐름입니다(선택, `hctrl run <이름>`). **예제로 시작하기** 카드에는 고른 탭의 agent 수에 맞춘 예제가 보입니다(예: agent 2개 → 구현→테스트, 구현→테스트→빌드). 각 agent는 한 번씩만 쓰이고, 쉘 창이 있으면 빌드 단계가 들어간 예제도 나옵니다. 추가된 flow는 단계 편집, 순서 변경(↑↓), 단계 삭제(✕), [flow 삭제]가 되고, 순서도의 단계를 누르면 편집 줄로 이동합니다. |

- 고칠 때마다 검사가 돌고, 오른쪽 위에 `검사 ✔` 또는 `오류 N`이 보입니다. 오류가 있으면 [저장] 대신 **[임시 저장]**이 나타나고(아래 참고), 문제가 있는 단계에는 왼쪽 메뉴에 ✘/⚠ 표시가 붙습니다. 아래 **검사 결과**의 항목을 누르면 그 위치로 이동합니다.
- 문제가 있는 입력칸 옆과 왼쪽 메뉴에 ⚠/✘가 붙고, 마우스를 올리면 내용이 보입니다. 탭 화면 맨 위에도 그 탭의 검사 결과가 나옵니다. 새 경고가 있는 채로 저장하면 경고 목록을 보여 주고 한 번 더 묻습니다.
- 권한은 claude agent에서 모두 고를 수 있습니다. opencode는 `auto`(`--auto`)만 고를 수 있고, 그 밖의 종류는 각자의 설정 파일을 따르므로 선택칸이 꺼집니다.
- 처음 설정할 때(config.yaml이 없을 때)는 화면 아래에 **[← 이전] [다음 탭 →] / [③ flow (선택) →] [만들고 세션 열기]**가 나옵니다. 이미 설정이 있으면 아래 막대 없이 오른쪽 위 **[저장]**과 **[저장하고 세션 열기]**를 씁니다(세션 안에서 연 화면이면 저장 후 반영 창).
- 저장하지 않은 변경이 있으면 오른쪽 위에 `● 저장 안 됨`이 보입니다.
- 설정이 아직 완성되지 않아 오류가 남아 있어도 **[임시 저장]**으로 지금 상태를 보관할 수 있습니다. 임시 저장본은 `config.yaml`이 아니라 프로젝트 밖 상태 폴더(`~/.local/state/herdr_ctl/<프로젝트>-<digest>/config.draft.json`)에 저장되므로, 실행 중인 세션이나 `hctrl up`에는 영향이 없습니다. 다음에 설정 화면을 열면 임시 저장본을 이어서 불러오고(`● 임시 저장됨`), 오류를 모두 고치면 버튼이 **[저장]**으로 바뀝니다. 저장하면 임시 저장본은 지워집니다. **[임시본 버리기]**로 `config.yaml` 내용으로 돌아갈 수 있습니다.
- 설명 글이 길게 느껴지면 왼쪽 아래 **? 설명 숨기기**로 접을 수 있습니다(브라우저에 기억).
- 오른쪽 위 **[만들기]**를 누르면 `.herdr-flow/config.yaml`이 생깁니다. 처음 이후로는 이 버튼이 **[저장]**입니다.

**4. 세션 열기**

- 오른쪽 위 **[세션 열기]**를 누르거나, 런처 목록에서 프로젝트의 **[열기]**를 누릅니다.
- 브라우저 화면 서버가 끝나고, `hctrl`을 실행했던 **그 터미널**에 herdr 세션이 열립니다. 설정한 탭, 창, agent가 자동으로 뜹니다.

**5. 다음부터는**

```bash
cd ~/work/my-app && hctrl     # 설정이 있으니 UI 없이 바로 세션이 열림
hctrl my-app                  # 다른 폴더에서 이름으로 열기
hctrl ui                      # 설정을 다시 고치고 싶을 때 (런처)
```

세션 안에서 설정을 고치려면 `_hc` 탭에서 `s` 키를 누릅니다. 실행 중인 세션에 저장하면 **반영 창**이 떠서 바꾼 항목을 골라 **[지금 반영]**할 수 있습니다. 나중에 반영하려면 **[나중에]**를 누르고, 이후 `_hc` 탭의 `c`로 반영합니다.

터미널에서 질문에 답하며 설정하는 방식을 원하면 UI 대신 `hctrl init`을 씁니다([2장](#2-화면-배치-설정-herdr-flowconfigyaml)). UI 화면 각 부분의 자세한 동작은 [1.1 설정 화면](#11-설정-화면-hctrl-ui)에 있습니다.

> 명령 이름이 `hc`가 아니라 `hctrl`인 이유: herdr 설치 스크립트가 `~/.local/bin/hc`(herdr-core)를 함께 설치하므로 이름이 겹칩니다. 그 `hc`는 이 도구와 무관합니다.

## 1. 프로젝트 열기 (`hctrl`)

### 가장 간단한 사용법

```bash
cd ~/study/BackEnd/projA     # 하위 폴더여도 됨
hctrl                           # = herdr_ctl
```

→ projA 전용 herdr 세션이 바로 열리고, **projA의 창만 보입니다.**
- 처음 열 때 `.herdr-flow/config.yaml`이 있으면 탭, 분할, agent까지 자동으로 구성됩니다.
- 창을 닫아도 세션은 유지됩니다. 다음에 `hctrl`을 실행하면 하던 화면 그대로 이어집니다.

### 프로젝트 등록

프로젝트를 찾는 기준은 두 가지입니다.

1. `~/.config/herdr_ctl/projects.yaml`에 등록된 경로
2. 그렇지 않으면 `.herdr-flow/config.yaml`이 있는 폴더

```bash
cd ~/study/BackEnd/projA
hctrl --add projA        # 현재 폴더를 projA로 등록
```

```yaml
# ~/.config/herdr_ctl/projects.yaml
projects:
  projA: ~/study/BackEnd/projA
  herdr_ctl: ~/study/BackEnd/herdr_ctl
```

### 그 밖의 명령

```bash
hctrl projA      # 어디서든 이름으로 열기
hctrl --list     # 등록된 프로젝트와 세션 상태
```

- 등록되지 않은 폴더(또는 설정이 없는 프로젝트)에서 실행하면 **브라우저 설정 화면(런처)**이 뜹니다. 거기서 프로젝트를 추가하고 설정한 뒤 [열기]를 누르면, `hctrl`을 실행한 **같은 터미널**에서 그 프로젝트 세션이 열립니다. 자세한 내용은 [1.1 설정 화면](#11-설정-화면-hctrl-ui)을 보세요. 섞여 있는 default 세션으로 대신 열지 않습니다.
- 터미널이 아닌 곳(파이프, 스크립트)에서 실행하면 화면을 띄우지 않고 `hctrl --add` / `hctrl init` 안내만 합니다.
- herdr 화면 안에서는 실행하지 않습니다. herdr 안에 herdr가 겹쳐 뜨기 때문입니다. 일반 터미널에서 실행하세요.

## 1.1 설정 화면 (`hctrl ui`)

질문에 차례로 답하는 `hctrl init` 대신, 브라우저에서 설정을 보고 고칠 수 있습니다.

| 여는 방법 | 화면 |
|---|---|
| 등록되지 않은 폴더에서 `hctrl` | 런처: 프로젝트 목록, 추가, [열기] |
| 어디서든 `hctrl ui` (프로젝트 폴더면 그 설정부터) | 런처 |
| 세션 안 `_hc` 탭에서 `s` 키 | 그 프로젝트의 설정 화면 (다른 프로젝트 열기는 불가) |

**런처**
- 프로젝트마다 세션 실행 여부와 설정 유무가 보입니다.
- [+ 프로젝트 추가]의 경로는 `hctrl`을 실행한 폴더가 기본값입니다. 추가하면 바로 설정 화면으로 넘어갑니다.
- [열기]를 누르면 화면 서버가 끝나고, 같은 터미널에서 그 프로젝트 폴더로 이동한 것처럼 세션이 열립니다.

**설정 화면**
- 탭, orchestrator, 작업 agent, 쉘 창, flow를 화면에서 고칩니다.
- 모델은 종류 → (opencode면 제공자 →) 모델 순서로 고릅니다. opencode 목록은 `opencode models`에서 가져옵니다. 이 명령은 10~20초 걸릴 수 있어 설정 화면이 뜰 때 백그라운드로 받고(그동안 "모델 목록 불러오는 중…"), 받은 목록은 하루 동안 저장해 두어 다음에는 바로 보입니다. 모델 옆 **↻**로 새로 받을 수 있고, 받지 못하면 직접 입력 칸 옆에 **[다시 시도]**가 나옵니다. 목록에 없으면 "직접 입력…"을 고릅니다.
- 고칠 때마다 `hctrl check`와 같은 검사가 돌고 아래에 결과가 나옵니다. 오류가 있으면 [저장] 대신 [임시 저장]이 나타납니다 (config.yaml에는 쓰지 않고 프로젝트 밖에 보관).
- 오른쪽에 실제 창 배치(탭당 4개, 넘치면 `-2` 탭)가 미리 보입니다.
- agent와 orchestrator 카드의 **[✎ 역할 상세 (prompt)]**로 그 멤버에게 그대로 전달되는 지시문(`roles/<이름>.md`)을 편집합니다. 버튼에 작성된 줄 수가 보이고, 변경 이력이 남습니다.
- 역할에는 **그 멤버 자신의 일만** 적습니다. orchestrator가 보는 작업자 분담표는 각 agent의 한 줄 역할로 자동으로 만들어지고, 결과 보고·결정 요청 방식은 hctrl이 자동으로 넣습니다(편집 창의 "hctrl이 자동으로 넣는 규칙"에서 확인).
- **결정 요청**: orchestrator가 있는 탭의 agent는 설계·방향 같은 결정을 임의로 내리지 않고, 결과 파일에 `결정 필요:` 항목(선택지와 장단점)을 적고 턴을 끝냅니다. orchestrator가 결정해 다음 지시로 보냅니다. flow는 중간의 `결정 필요:`에서 멈추지 않으므로, orchestrator는 결정이 생길 만한 일을 `hctrl send`로 맡깁니다.
- 화면이 다루지 않는 키(`limits`, `args` 등)는 그대로 보존됩니다. 단, yaml의 주석은 저장할 때 사라집니다.
- 그 사이 누가 파일을 고쳤으면 저장하지 않고 다시 불러올지 묻습니다.
- 세션이 실행 중일 때 저장하면 탭 표시 이름은 바로 바뀌고, 창/agent 변경(창 닫기·추가, 재시작, 역할 알리기)은 **반영 창**이 떠서 항목별로 고른 뒤 [지금 반영]으로 적용합니다. [나중에]를 누르면 상단의 "변경 반영 (N)" 버튼이나 `_hc` 탭의 `c`(= `hctrl changes`)로 반영합니다.

**탭 복사**: 탭 화면의 **[⧉ 탭 복사]**로 같은 구성의 팀을 하나 더 만듭니다(예: 기능별 팀). 새 탭 이름을 정하면 멤버 이름에 붙어 바뀌고(`coder` → `coder-login`, orchestrator는 `login-orch`), 이 탭의 flow(`tab:`이 이 탭이거나 모든 단계가 이 탭 멤버인 flow)도 새 이름으로 복사됩니다. 역할 상세(prompt)는 복사 직후부터 각 멤버의 **[✎ 역할 상세]**에서 보고 고칠 수 있고, [저장]할 때 새 멤버의 파일로 만들어집니다(같은 이름의 파일이 이미 있으면 그대로 두고 저장 결과에 알려 줍니다). 저장 전에 새로 추가한 멤버도 같은 방식으로 prompt를 쓸 수 있습니다. 대화 기록과 상태는 복사하지 않습니다. 두 팀이 같은 폴더에서 같은 파일을 고치지 않도록 **별도 worktree**(새 브랜치)가 기본으로 켜져 있습니다(git 저장소일 때).

**팀 템플릿**: 왼쪽 메뉴의 **4 템플릿**에서 팀 구성(멤버·모델·역할 상세 prompt·flow)을 골라 **[+ 이 프로젝트에 추가 →]**로 탭을 만듭니다. 템플릿이 몇 개든 메뉴는 한 줄이고, 템플릿 화면에서 검색과 [전체 | 기본 | 내 템플릿] 필터, 멤버별 역할 상세·flow 미리보기를 볼 수 있습니다.

| 기본 템플릿 | 멤버 (모두 claude) | 하는 일 |
|---|---|---|
| `dev-team` | orchestrator(opus), coder(sonnet), tester(haiku), 쉘 build | 설계·분배 → 구현 → 테스트·git. flow `dev`: 구현 → 테스트 작성·실행 → 실패 수정 |
| `design-review-team` | orchestrator(opus), designer(opus), reviewer(sonnet), critic(opus) | 요구 → 설계 문서 → 일반 리뷰 + **적대적 리뷰** 반복(최대 3회, 사용자 확인) → 코드를 포함한 구현 spec. flow `design-draft`, `design-review`, `design-spec` |
| `debug-team` | orchestrator(opus), investigator(opus), fixer(sonnet), verifier(haiku), 쉘 test | 증상 → root cause 확인 → 실패하는 재현 테스트 → 최소 수정 → 재현 테스트 + 기존 전체 테스트 통과. flow `debug` |

- 작업 내용은 orchestrator 창에 말로 주면 됩니다. orchestrator가 `.herdr-flow/work/<탭 ID>/` 아래 작업 파일(task.md / brief.md / issue.md)에 정리한 뒤 flow를 돌리고, 반복(리뷰 재실행, 수정 후 재검증)과 판정을 맡습니다. 같은 템플릿을 두 번 추가하면(기능별 팀) 두 번째 팀의 멤버·flow 이름과 작업 폴더가 바뀌고, 역할 상세와 flow prompt 속 이름도 함께 바뀝니다.
- 기본 템플릿은 hctrl에 들어 있어 고칠 수 없습니다. 탭으로 추가해 고친 뒤 **[☆ 현재 탭을 템플릿으로 저장]**(템플릿 화면 위쪽)하면 내 템플릿이 됩니다. 같은 이름으로 저장하면 기본 템플릿을 덮어쓰고, 그 내 템플릿을 지우면(**[기본값으로 되돌리기]**) 기본 템플릿이 돌아옵니다.
- 내 템플릿은 프로젝트 밖(`~/.config/herdr_ctl/templates/`)에 저장됩니다. 담기는 것은 탭 구성(모델·역할·권한·limits 등), 그 탭의 flow, 멤버별 역할 상세(prompt) 전문이고, 작업 폴더(`cwd`)·`worktree`·표시 이름은 빠집니다. 저장된 설정으로 만들어지므로 먼저 [저장]하세요. 이름 바꾸기·삭제는 템플릿 화면에서 합니다.
- 내 템플릿이 생기면 목록에서 내 템플릿이 위에 펼쳐지고 기본 템플릿은 접힙니다(펼침 상태는 브라우저가 기억합니다).
- 추가할 때 멤버 이름은 겹치지 않으면 그대로, 겹치면 탭 이름이 붙습니다. 추가한 뒤에는 프로젝트의 파일이라 템플릿을 고쳐도 바뀌지 않습니다.
- 새 프로젝트(config.yaml이 없을 때)에서 추가하면 **기본 탭(work1) 대신 사용**이 켜져 있어 템플릿으로 시작합니다. 명령으로는 `hctrl init --template <이름>`(예: `hctrl init --template debug-team`, 탭 ID = 템플릿 이름).

**탭 이름 바꾸기**: 탭에는 **ID**(`X`, 명령에서 사용: `hctrl ask X ...`)와 **표시 이름**(`title`, herdr 탭에 보이는 이름)이 있습니다. 둘 다 세션이 실행 중이어도 저장 즉시 바뀌고, 창과 agent는 그대로입니다. ID를 바꾸면 orchestrator 이름(`x-orch` → `login-orch`), flow의 `tab`과 orchestrator 단계, 상태 파일, orchestrator 역할 파일이 함께 옮겨지고, orchestrator에게 새 이름을 알립니다. 탭의 agent가 실행 중인 작업(`hctrl send/run`)이나 답을 기다리는 질문이 있으면 끝난 뒤에 바꿀 수 있습니다. 명령으로는 `hctrl tab rename X login`입니다.

```yaml
tabs:
  X:
    title: 로그인 작업     # herdr 탭: '로그인 작업', 넘치면 '로그인 작업-2'
```

**`_hc` 탭 메뉴**: 세션 안의 `_hc` 탭은 설정 감시와 함께 키 메뉴를 보여 줍니다.

```
[s] 설정 열기(브라우저)   [c] 변경 반영   [t] 상태   [p] 답을 기다리는 질문
```

**브라우저 열기**

| 환경 | 동작 |
|---|---|
| WSL | `wslview`, 없으면 `rundll32.exe url.dll,FileProtocolHandler`로 Windows 기본 브라우저를 엽니다 (WSL2는 `127.0.0.1`을 Windows와 공유) |
| Linux 데스크톱 | 기본 브라우저 |
| SSH, 화면 없는 서버 | 주소만 출력합니다. `ssh -L <포트>:127.0.0.1:<포트>`로 연결해 내 PC 브라우저에서 엽니다 |
| 직접 지정 | `HC_BROWSER="firefox --new-tab"`, 열지 않으려면 `HC_BROWSER=none` |

**보안**: 화면에서 agent 권한까지 바꿀 수 있으므로 사용자 전용입니다.
- `127.0.0.1`에만 열립니다.
- 접속 링크의 토큰은 2분 안에 한 번만 쓸 수 있고, 쓰는 순간 브라우저 쿠키로 바뀝니다. 브라우저가 자동으로 열리면 터미널에는 토큰을 출력하지 않습니다.
- 다른 사이트에서 오는 요청(Origin, Host 불일치)은 거부합니다.
- agent나 orchestrator 창에서 `hctrl ui`를 실행하면 거부됩니다.

**세션을 나온 뒤에도 프로젝트 폴더에 머물기 (선택)**: 프로그램은 자기를 실행한 쉘의 폴더를 바꿀 수 없습니다. 아래 한 줄을 `~/.bashrc`(또는 `~/.zshrc`)에 넣으면, `hctrl`로 연 세션에서 빠져나왔을 때 쉘도 그 프로젝트 폴더로 이동합니다.

```bash
eval "$(hctrl shell-init bash)"
```

## 2. 화면 배치 설정 (`.herdr-flow/config.yaml`)

### 만들기: `hctrl ui` 또는 `hctrl init`

직접 쓰지 않아도 됩니다. 브라우저 화면([1.1](#11-설정-화면-hctrl-ui))에서 만들거나, 터미널에서 질문에 답해 만듭니다.

```
$ hctrl init
탭 이름? [work1] >
orchestrator? [claude/opus] / none
  프로젝트 목표 한 줄? (선택) > 로그인/결제 모듈 개발
작업 agent 수? 2
  agent 1 이름? [coder] >
  coder 종류/모델? claude / sonnet
  coder 역할? [구현] >
  agent 2 이름? [tester] >
  tester 종류/모델? opencode / (기본)
  tester 역할? [테스트 작성] >
빌드 쉘 추가? y → 쉘 이름? [build] → 빌드 명령? npm run build
기본 flow(구현→테스트→빌드→검토) 만들까요? y
✔ .herdr-flow/config.yaml 생성
검사 중... ✔ 오류 0, 경고 0
```

[팀 템플릿](#11-설정-화면-hctrl-ui)(기본 `dev-team`, `design-review-team`, `debug-team` 또는 내 템플릿)으로 바로 시작하려면 `hctrl init --template <이름>`을 씁니다(묻는 것 없이 템플릿 탭·flow·역할 상세로 config.yaml을 만듭니다). 탭을 추가할 때는 `hctrl init --tab work2`를 씁니다. 기본 이름은 탭이 `work1`, `work2`…, agent가 역할에 맞춘 `coder`, `tester`, `reviewer`, `helper`(이미 있으면 `coder2`…), 쉘이 `build`입니다.

### 검사: `hctrl check` (자동)

- `hctrl init`이 끝나면 **자동으로** 검사합니다. 오류가 있으면 해당 항목만 다시 물어봅니다.
- `hctrl`, `hctrl up`, `hctrl run`, `hctrl ask`를 실행하기 직전에도 **자동으로** 검사합니다. yaml을 직접 고친 경우도 여기서 잡힙니다.
- 오류가 있으면 실행하지 않고, 경고는 보여주고 계속합니다.
- 언제든 `hctrl check`로 수동 검사할 수 있습니다.

```
$ hctrl check
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

모델 이름이 실제로 존재하는지는 agent를 띄울 때(`hctrl up`) 확인됩니다.

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
  Y:                       # 기능 Y 작업 탭 (orchestrator 없이 hctrl만 사용)
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
| `ask` | 모든 작업에서 승인 요청 (계정 기본 모드가 auto여도 ask로 시작) |
| `edit` (작업 agent 기본) | 파일 수정과 흔한 파일 명령(`echo > 파일` 등)은 자동, 그 밖의 명령 실행은 승인 요청 |
| `auto` | 대부분 자동 |
| `readonly` (orchestrator 기본) | 읽기와 `hctrl` 명령만 자동, 그 밖은 사용자 승인 |
| `bypass` | 모든 권한 확인을 건너뜀 (claude `--permission-mode bypassPermissions`). 무인 모드(`unattended`)가 켜진 탭에서는 오류입니다. `hctrl check`가 항상 경고합니다. orchestrator에 쓰면 승인 없이 코드를 고치고, 프로젝트 밖 상태 폴더도 고칠 수 있어 작업 상한(`limits`)이 우회될 수 있습니다 |

`bypass`는 작업 폴더 밖 파일 수정이나 위험한 명령도 묻지 않고 실행하므로, 격리된 환경(컨테이너, 전용 worktree 등)에서만 쓰세요. `args`에 우회 옵션(`--dangerously-skip-permissions`, `bypassPermissions` 등)을 직접 넣는 것은 여전히 `hctrl check`에서 오류입니다. `permission: bypass`로 명시하세요.

opencode는 시작 옵션으로 `auto`(`--auto`: 명시적으로 거부되지 않은 모든 권한 요청을 자동 승인)만 지정할 수 있습니다. 나머지 값은 적용되지 않고 opencode 설정 파일을 따릅니다. `ask`는 설정 파일에 맡긴다는 뜻이라 경고하지 않고, `edit`/`readonly`/`bypass`는 `hctrl check`가 경고합니다. 승인 창이 뜨지 않으므로 무인 모드(`unattended`)가 켜진 탭에서 opencode `auto`는 오류입니다. `args`에 `--auto`나 `--agent`를 직접 넣으면 `permission`/`primary_agent`와 충돌하므로 `hctrl check`가 경고합니다.

claude는 bypass 모드를 처음 켤 때 확인 창을 띄웁니다. 이 창이 떠 있으면 hctrl이 보내는 첫 지시가 막히므로, 처음 쓰는 PC에서는 터미널에서 `claude --permission-mode bypassPermissions`를 한 번 실행해 수락해 두세요. 수락하면 이후로는 묻지 않습니다. `args`에 `--permission-mode`, `--allowedTools`, `--settings`를 직접 넣으면 hctrl이 넣는 값과 충돌하므로 `hctrl check`가 경고합니다.

### 작업 폴더 분리 (`worktree`, 선택)

탭 X와 Y가 같은 폴더에서 동시에 작업하면 파일이 충돌합니다. 탭에 `worktree: { branch: feat/x }`를 주면 그 탭은 별도 폴더와 브랜치에서 작업합니다. 설정 UI에서는 탭의 **기본 정보 → 작업 위치**에서 "프로젝트 폴더(현재 브랜치 그대로)"와 "별도 worktree(새 브랜치)" 중에 고릅니다(첫 탭 포함). 이때 그 탭은 herdr에서 별도 workspace로 열립니다. 병합은 직접 하거나 orchestrator에게 시킵니다.

**작업 위치 바꾸기 (실행 중인 탭)**: 세션이 열린 상태에서 작업 위치를 바꿔 저장하면 반영 창에 "옮기기"가 나옵니다. hctrl은 안전하게 할 수 있는 경우에만 자동으로 옮기고, 그렇지 않으면 **아무것도 바꾸지 않고** 이유를 알려 줍니다. 대화는 폴더를 건너 이어지지 않으므로, 모든 agent가 인수인계 파일을 쓴 뒤 새 위치에서 새로 시작합니다.

| 방향 | hctrl이 하는 일 | 멈추는 경우 (직접 정리 후 다시 반영) |
|---|---|---|
| worktree → 프로젝트 폴더 | 인수인계 받기 → worktree 브랜치를 기준 브랜치에 병합(가능하면 fast-forward, 아니면 병합 커밋) → 탭을 프로젝트 폴더에 다시 배치 → git에 없는 작업 파일(`.herdr-flow/work/<탭>/`)을 옮김 → worktree 폴더와 (병합된) 브랜치 정리. worktree에 git이 무시하는 파일(`.env` 등)이 있으면 함께 지워지므로 worktree는 남기고 알려 줍니다 | 진행 중인 작업(실행·지시·질문, 작업 중인 agent), worktree의 커밋 안 된 변경이나 git에 없는 파일, 프로젝트 폴더의 커밋 안 된 변경, 프로젝트 폴더가 기준 브랜치가 아니거나 기준 브랜치 기록이 없음, 내용이 다른 같은 작업 파일, 프로젝트 폴더에서 작업 중인 다른 탭의 agent, agent가 인수인계를 쓰지 못함, **병합 충돌**(병합을 되돌리고 탭은 worktree에 그대로) |
| 프로젝트 폴더 → worktree | 인수인계 받기 → 현재 브랜치에서 새 worktree를 만들고 작업 파일을 옮긴 뒤 탭을 다시 배치 | 진행 중인 작업, 프로젝트 폴더의 커밋 안 된 변경(누구의 변경인지 알 수 없어 가져가지 않음), 이 탭의 이전 worktree가 남아 있음. git에 없는 파일은 프로젝트 폴더에 남기고 결과에 알려 줍니다 |

멈추는 경우는 모두 아무것도 바꾸기 전에 멈춥니다. 탭을 닫은 뒤에 실패하면(예: herdr 오류) 다시 반영할 때 같은 인수인계로 이어서 끝냅니다. 옮기는 동안에는 탭 멤버에게 `hctrl send/run`을 할 수 없습니다. hctrl이 대신 커밋하거나, git에 없는 파일을 지우거나, 충돌을 풀지는 않습니다. worktree 브랜치를 다른 이름으로 바로 바꾸는 것은 지원하지 않습니다(프로젝트 폴더로 옮긴 뒤 다시 worktree로).

### 모델 지정

- `model`: agent의 모델을 지정합니다. claude는 `opus`, `sonnet`, `haiku` 또는 전체 모델 ID를 쓰고, opencode는 `제공자/모델` 형식입니다.
- `primary_agent` (opencode 전용): opencode의 주 agent를 `--agent`로 지정합니다. 표시 이름(`Sisyphus - ultraworker`)과 키(`sisyphus`) 모두 대소문자 구분 없이 쓸 수 있습니다. OMO가 있으면 Sisyphus 등, 없으면 build/plan이 주 agent입니다. subagent나 없는 이름을 주면 opencode가 경고만 내고 기본 agent로 대체합니다. 설정 화면의 'agent' 목록은 `opencode agent list`(12~20초)를 백그라운드로 받아 하루 동안 저장합니다(↻로 새로 받음). `hctrl check`는 저장된 목록에 없는 이름을 경고합니다(목록이 없으면 검사 생략). claude 등 다른 종류에 쓰면 경고하고 적용하지 않습니다.
- `args`: 그 밖의 agent 실행 옵션입니다. agent 프로그램에 그대로 전달됩니다.

### orchestrator와 작업 agent

| 역할 | 하는 일 |
|---|---|
| orchestrator (큰 창) | 흐름을 시작하고(`hctrl run`), 결과를 검토하고, 실패하면 작업 agent에게 수정을 지시합니다. 직접 코딩은 하지 않습니다. |
| 작업 agent (작은 창) | 구현, 테스트 등 지시받은 일을 하고 결과를 파일로 남깁니다. |
| `hctrl` (화면 밖) | 정해진 순서를 확실하게 실행하고 결과 파일을 넘깁니다. |

- orchestrator는 자기 탭의 agent에게만 지시하고, 승인 요청(`blocked`)은 사용자에게 넘깁니다.
- 운영 규칙은 `hctrl up`이 `.herdr-flow/orchestrator-<tab>.md`로 만들어 줍니다. 내용을 직접 고쳐도 됩니다.

### 역할 설정

| 단계 | 언제 / 누가 | 방법 |
|---|---|---|
| 한 줄 역할 | `hctrl init` 때 | agent마다 "구현", "테스트 작성" 같은 한 줄 (Enter를 치면 기본값) |
| 상세 프롬프트 | 나중에, 사람이 | `hctrl role x1 --edit` (직접 작성) 또는 `hctrl role x1 --draft` (LLM이 초안 작성 → 검토 후 저장) |
| 진행 중 갱신 | 프로젝트 진행 중, orchestrator가 | 상황을 보고 자기 탭 agent의 역할을 조정 |

```bash
hctrl role                         # 역할 목록
hctrl role x1 "결제 모듈 구현"      # 한 줄 역할 변경
hctrl role x1 --edit               # 상세 프롬프트 편집 (.herdr-flow/roles/x1.md)
hctrl role x1 --draft              # LLM(claude -p, HC_DRAFT_CMD로 변경 가능)이 초안 작성
hctrl role x1 --apply              # 실행 중인 agent에게 바뀐 역할 알리기
hctrl role --review                # orchestrator가 제안한 변경 승인/거절
hctrl role x1 --history            # 변경 이력 / --revert <n>으로 되돌리기
```

LLM이 역할을 바꾸는 범위는 탭 설정 `role_update`로 정합니다.

| 값 | 동작 |
|---|---|
| `confirm` (기본) | orchestrator가 바꾸려 하면 **제안**으로 저장되고 알림이 뜹니다. `hctrl role --review`에서 승인해야 반영됩니다. |
| `auto` | 바로 반영하고 알림만 띄웁니다. |
| `off` | LLM은 역할을 바꿀 수 없습니다. |

- orchestrator는 자기 탭 agent의 역할만 바꿀 수 있고, 자기 자신의 기본 규칙은 바꿀 수 없습니다.
- 작업 agent는 역할을 직접 고치지 않고, 결과 파일에 제안만 남깁니다.
- 모든 변경은 이력으로 남습니다.

**역할이 유지되는 범위**: claude와 opencode는 역할(brief)을 시스템 프롬프트로 받습니다. 그래서 대화를 비워도(claude `/clear`, opencode `/new`) 역할이 남습니다. hctrl은 agent를 띄우기 전에 그 창의 쉘에도 역할을 걸어 둡니다. 그래서 agent를 종료한 뒤 **같은 창에서 직접** `claude`나 `opencode`를 다시 실행해도 역할이 유지됩니다.

| agent | 창 쉘에 걸리는 것 |
|---|---|
| claude | `claude` 함수. `--append-system-prompt-file <brief>`가 없을 때만 붙여 실행합니다. `claude` alias가 있어도 함께 동작하고, 원래 명령은 `command claude`로 쓸 수 있습니다. |
| opencode | `OPENCODE_CONFIG` 환경변수. `~/.local/state/herdr_ctl/<프로젝트>/opencode/<이름>.json`(`instructions`에 brief)을 가리키며, 사용자 opencode 설정과 합쳐집니다. |

- 여기서 쓰는 brief는 `~/.local/state/herdr_ctl/<프로젝트>/briefs/<이름>.md`에 둔 사본입니다. 프로젝트 안의 brief는 agent가 고칠 수 있기 때문입니다. 재시작 때의 인수인계 내용은 그 시작에만 쓰고, 사본에는 넣지 않습니다(opencode에는 인수인계 파일을 첫 지시로 한 번 알립니다).
- hctrl은 쉘에 건 줄이 실제로 실행됐는지 확인합니다. 확인되지 않으면 opencode에는 brief를 첫 지시로 보냅니다.

- 쉘이 bash·zsh 같은 POSIX 계열일 때만 걸어 둡니다. 다른 쉘(fish 등)에서는 쉘에 걸지 않고, opencode에는 첫 지시로 brief를 보냅니다.
- 다른 창이나 새 쉘에서 띄운 agent에는 역할이 없습니다.
- 이미 `OPENCODE_CONFIG`를 쓰고 있었다면 그 창에서는 hctrl의 값으로 바뀝니다.

### 진행 중 설정 변경 (사용자가 yaml이나 역할 파일을 수정한 경우)

세션이 열려 있는 동안 `config.yaml`이나 `roles/*.md`를 고치면 **자동으로 감지해서 알려줍니다.**

```
🔔 herdr 알림: 설정 변경 감지 — x1 역할, x2 모델. 'hctrl changes'로 확인하세요.

$ hctrl changes
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
| flow만 변경 | 다음 `hctrl run`부터 적용 (알림만) |
| 쉘 창, 탭 `cwd`/`worktree`(작업 위치), 소속 탭 변경 | 탭을 다시 배치해야 함. `[m] 옮기기`(UI 기본값)는 인수인계를 받은 뒤 탭을 새 위치에 다시 띄웁니다(아래 "작업 위치 바꾸기"). `[k] 확인`은 창을 옮기지 않고 알림만 지웁니다 |

변경 반영의 재시작(`[r]`)은 다음 순서로 진행됩니다.

1. 작업 중이면 끝날 때까지 기다립니다.
2. 기존 agent가 지금까지의 작업을 `handoff/x1.md`에 요약합니다.
3. 같은 창, 같은 이름으로 새 설정의 agent를 띄웁니다.
4. 새 agent에게 새 역할과 인수인계 파일을 전달합니다.

재시작하면 대화 맥락은 사라지지만, 인수인계 파일 덕분에 이어서 작업할 수 있습니다. 인수인계를 생략하려면 `[f]`(명령으로는 `--no-handoff`)를 고릅니다. orchestrator에게도 재시작 사실이 자동으로 전달됩니다.

**직접 재시작 (skill·MCP를 바꿨을 때 등)**: `hctrl restart x1`은 기본으로 **같은 대화를 이어서** 다시 시작합니다(claude `--resume`, opencode `--session`). 설정·skill·MCP를 다시 불러오면서 지금까지의 대화가 그대로 남습니다. 이어 갈 대화를 찾지 못하면 인수인계 후 새로 시작합니다.

```bash
hctrl restart x1             # 같은 대화로 이어서 (기본)
hctrl restart x1 --fresh     # 인수인계 파일을 남기고 새 대화로
hctrl restart --tab X        # 탭 X 전체 (작업 agent 먼저, orchestrator 마지막)
```

설정에 오류가 있으면 반영하지 않고 오류만 알립니다.

### 탭당 창 개수 규칙

| 개수 | 배치 |
|---|---|
| 1 | 전체 화면 |
| 2 | 좌우 분할 |
| 3 (권장) | 왼쪽 큰 창 + 오른쪽 위/아래 |
| 4 (최대) | 2×2 격자 |
| 5 이상 | 남는 창은 자동으로 `X-2`, `X-3` … 탭으로 넘어감 |

orchestrator는 개수에 포함되며 항상 1번(큰 창) 자리에 옵니다. **쉘 창은 agent 탭에 넣지 않고 별도의 `X-build` 탭**에 모입니다(같은 규칙으로 4개씩, 넘치면 `X-build-2`). 표시 이름이 있으면 `<표시 이름>-build`입니다.

```
탭 X (위 예시)                    탭 X-build
┌──────────────┬─────────┐       ┌──────────────────────┐
│              │   x1    │       │                      │
│ orchestrator ├─────────┤       │        xbuild        │
│              │   x2    │       │                      │
└──────────────┴─────────┘       └──────────────────────┘
```

예전 배치(쉘 창이 agent 탭 안에 분할)로 떠 있는 세션은 다음 `hctrl up` 때 쉘 창이 `X-build` 탭으로 옮겨집니다. 그 창에서 명령이 실행 중이면 옮기지 않고, 끝난 뒤 다시 `hctrl up X`를 실행하라고 알려 줍니다.

## 3. 창 띄우기와 보기

```bash
hctrl up           # 설정된 모든 탭/창/agent 생성 (이미 있으면 건너뜀)
hctrl up X         # 탭 X만
hctrl status       # 탭별 agent 상태: idle / working / blocked / done / unknown
hctrl zoom x2      # x2 창만 크게 보기 (한 번 더 실행하면 분할 화면으로 복귀)
hctrl down X       # 탭 X의 agent와 창 닫기 (설정은 그대로, 다시 열기: hctrl tab reopen X)
hctrl tab rename X login   # 탭 ID 변경 (창과 agent는 그대로)
```

### 탭 끝내기: 잠시 닫기 / 탭 종료 / 탭 설정 삭제

| | 창·agent | hctrl 세션 산출물 (작업 폴더 `work/<탭>/`, 인수인계, 실행 기록, brief, 대화 기록) | 탭 설정 · prompt(역할 상세) | 다시 열면 |
|---|---|---|---|---|
| 잠시 닫기 `hctrl down X` | 닫음 | 남김 | 남김 | 이전 대화 이어서 |
| **탭 종료** `hctrl tab end X` | 닫음 | **지움** | 남김 (`closed: true`) | 같은 구성으로 새 세션 |
| 탭 설정 삭제 `hctrl tab delete X` | 닫음 | 남김 | 탭과 그 flow를 config에서 삭제 (prompt 파일은 남음) | — |

- **탭 종료**는 그 탭의 hctrl 세션을 끝내는 것입니다. 지우기 전에 지울 산출물과 남는 것을 보여 주고 확인합니다. 종료된 탭은 `hctrl up`과 변경 감시에서 빠지고(알림 없음, 다시 생기지 않음), `hctrl tab reopen X`로 새로 시작합니다.
- **worktree 폴더는 기준 브랜치(`worktree.base`, 없으면 만들 때의 브랜치)에 모두 병합됐을 때만** 정리합니다(병합된 브랜치도 삭제). 커밋 안 된 파일이나 병합 안 된 커밋이 있으면(squash·rebase 병합도 인정) 목록을 보여 주고 worktree는 남깁니다. 그래도 지우려면 `--force-worktree` (브랜치는 남김).
- claude·opencode의 **LLM 대화 원본은 어떤 경우에도 지우지 않습니다**(`claude --resume`, `opencode session list`로 찾을 수 있습니다). hctrl이 지우는 "대화 기록"은 어느 agent가 어느 대화였는지 적은 hctrl의 메모뿐입니다.
- 설정 화면에서는 탭 화면 맨 위 **실행 제어** 카드의 [↻ 재시작 ▾] / [■ 탭 종료 ▾] / [🗑 탭 설정 삭제]로 같은 일을 합니다. 종료된 탭은 왼쪽 메뉴 **종료된 탭** 묶음에 있고, [▶ 다시 시작]으로 엽니다.

### _hc 탭 메뉴 (대상 탭)

`_hc` 탭 맨 위에는 지금 고른 **대상 탭**이 항상 보입니다. 키는 그 탭에만 적용되고, worktree 탭(별도 workspace)도 여기서 고릅니다.

```
대상: ◀ [2/3] login ▶  ● 실행 중 · worktree feat/login · 변경 1건
[Tab] 대상 탭 바꾸기  [s] 설정  [c] 변경 반영  [t] 상태  [p] 질문
[r] 재시작  [x] 탭 종료  [o] 탭 열기  [D] 탭 설정 삭제  [?] 도움말
```

- `[Tab]`, `←`/`→`, 숫자 키로 대상을 바꿉니다(`0` = 전체). `[?]`를 누르면 키 설명이 나옵니다.
- `[x]`는 1) 잠시 닫기 / 2) 탭 종료 중에서 고릅니다. 탭 설정 삭제는 실수로 누르지 않도록 대문자 `[D]`입니다.

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
hctrl run build-review-x
```

```
[1/4] x1 (claude)    ▶ 작업 중...  ✔ 완료  step-1.md
[2/4] x2 (opencode)  ▶ 작업 중...  ✔ 완료  step-2.md
[3/4] xbuild (bash)  ▶ npm run build  ✘ exit 1  step-3.log
[4/4] x1 (claude)    ▶ 작업 중...  ✔ 완료  step-4.md
```

- 실행 중에도 각 창에서 agent가 일하는 모습을 볼 수 있습니다.
- **직접 입력은 해당 agent가 잠겨 있지 않을 때만** 하세요. `hctrl status`에 "잠금: run 0042 사용 중"으로 표시됩니다. 실행 중에 직접 입력하면 완료 판단이 틀어질 수 있습니다.
- agent가 `done`이 돼도 결과 파일(`{out}`)을 쓰지 않았으면 그 단계는 실패로 처리됩니다.
- 서로 다른 탭의 flow는 동시에 실행할 수 있습니다. 예: `hctrl run build-review-x`와 `hctrl run build-review-y`
- orchestrator가 있는 탭에서는 orchestrator 창에 "build-review-x 돌리고 결과 검토해줘"라고 말로 시켜도 됩니다. orchestrator는 내부적으로 `hctrl run --detach`와 `hctrl wait <run-id>`를 사용합니다.

## 4.1 A / B 실행 구분

**어떤 명령으로 시작하느냐**로 나뉩니다.

| | A. 정해진 순서 | B. orchestrator 지휘 |
|---|---|---|
| 시작 | `hctrl run <flow>` | `hctrl ask <tab> "<지시>"` 또는 orchestrator 창에 직접 입력 |
| 동작 | YAML 순서대로 기계적으로 실행 | orchestrator가 판단해서 flow를 돌리거나 agent에게 직접 지시, 결과 검토와 재시도까지 |
| 적합한 일 | 빌드 → 검토 같은 반복 작업 | "X 기능 완성해줘"처럼 판단이 필요한 일 |
| 조건 | 항상 가능 | 탭에 orchestrator가 있어야 함 (`none`이면 오류) |

```bash
hctrl run build-review-x               # A
hctrl ask X "로그인 기능 완성하고 빌드까지 통과시켜줘"   # B
```

orchestrator가 있는 탭에서 `hctrl run`을 써도 됩니다. 이때 orchestrator는 개입하지 않습니다.

### agent 하나에 직접 지시 / 중단

```bash
hctrl send x1 "로그인 버튼 스타일 수정"           # 잠금 확인 후 지시하고, 끝날 때까지 대기
hctrl send x1 "..." --detach                     # 바로 job-id 반환 (orchestrator가 주로 사용)
hctrl wait <job-id>
hctrl stop <run-id|job-id>                       # 실행 중인 flow나 지시 중단
```

orchestrator에는 상한(`limits`)이 걸립니다. 사용자의 지시 한 번(`hctrl ask` 또는 orchestrator 창에 직접 입력한 새 메시지)마다 지시할 수 있는 횟수와 시간이 정해져 있고, 넘으면 `hctrl`이 멈추고 알려줍니다. 새 지시를 입력하면 상한이 다시 시작됩니다. 직접 입력은 orchestrator의 대화 기록에서 확인합니다(claude는 대화 기록 파일, opencode는 opencode DB `~/.local/share/opencode/opencode.db`를 읽기 전용으로). `[hctrl]`로 시작하는 hctrl의 알림이나 도구 결과로는 초기화되지 않으므로 LLM이 이 상한을 무시할 수 없습니다. opencode는 플러그인(예: oh-my-opencode의 반복 실행)이 표시 없이 사용자 메시지를 넣을 수 있어서, `[`나 `<`로 시작하는 메시지는 직접 입력으로 보지 않습니다. 그런 메시지로 시작하는 지시라면 `hctrl ask`를 쓰세요.

## 5. 멈췄을 때

| 상황 | 동작 | 대응 |
|---|---|---|
| agent가 승인을 요청함 (`blocked`) | 실행이 멈추고 herdr 알림이 뜸 | 해당 창에서 직접 승인한 뒤 `hctrl resume <run-id>` |
| 쉘 명령 실패 | `on_fail` 설정에 따라 계속하거나 중단 | 로그 확인 후 `hctrl resume <run-id>` |
| 시간 초과 | 실행이 멈춤. 프롬프트를 자동으로 재전송하지 **않음** | `hctrl status`로 상태 확인 |
| 중단하고 싶을 때 | `hctrl stop <run-id>` (agent에게 중단 신호, 잠금 해제) | 필요하면 `hctrl resume <run-id>` |
| orchestrator 상한 도달 | 추가 지시를 거부하고 알림 | 확인 후 orchestrator 창에 새 지시를 입력하거나 `hctrl ask`로 다시 지시 |
| agent가 질문함 (`question`) | 실행은 살아서 답을 기다림, 알림 | `hctrl pending`으로 보고 `hctrl answer <qid> ...` ([6장](#6-무인-모드-unattended)) |

실행 기록은 `.herdr-flow/runs/<run-id>/`에 단계별 결과 파일과 `run.json`으로 남습니다.

## 6. 무인 모드 (`unattended`)

orchestrator가 있는 탭에서 작업 agent의 **질문과 승인 요청에 orchestrator가 대신 답하게** 합니다. **기본은 off**입니다. 켜지 않으면 지금까지처럼 모두 사용자가 처리합니다.

```yaml
tabs:
  X:
    orchestrator: { kind: claude, model: opus }
    unattended: questions        # off(기본) | questions | approve
    unattended_policy:           # 선택
      answer_timeout: 300s       # 이 시간 안에 답이 없으면 사용자에게 넘김
      max_answers_per_ask: 30    # hctrl ask 1회당 orchestrator 답변 상한
      deny: ["git push"]         # 포함되면 무조건 사용자에게
```

| 값 | 작업 agent의 질문 | 권한 승인 (명령 실행, 파일 쓰기 등) |
|---|---|---|
| `off` | 사용자 | 사용자 |
| `questions` | orchestrator | 사용자 |
| `approve` | orchestrator | orchestrator (아래 안전장치 통과 시) |

### 질문의 종류

- **텍스트 질문**: 작업 agent는 결과 파일 첫 줄을 `## QUESTION`으로 쓰고 질문을 남긴 뒤 턴을 끝냅니다(작업 규칙에 안내돼 있습니다). hctrl이 답을 다음 지시로 전달하고 같은 단계를 이어갑니다. 이 방식은 **무인 모드가 off여도** 동작하며, 그때는 사용자가 답합니다. orchestrator가 있는 탭에서는 결정이 아니라 계정·비밀번호·외부 승인처럼 작업에 꼭 필요한 정보를 물을 때만 씁니다 (결정은 `결정 필요:`로 orchestrator에게).
- **선택 질문**: claude의 선택지 질문(AskUserQuestion)입니다.
- **권한 승인**: claude의 "이 명령을 실행할까요?" 대화상자입니다. `approve`일 때만 orchestrator가 답합니다.

선택 질문과 권한 승인은, `hctrl up`이 claude 작업 agent에 넣어 두는 훅을 통해 hctrl이 받습니다. 무인 모드를 바꾸면 해당 agent에 "재시작 필요"가 표시됩니다. opencode agent는 지원하지 않으며, 그 질문은 사용자에게 넘어갑니다.

### 명령

```bash
hctrl pending                         # 답을 기다리는 질문 (orchestrator는 자기 탭 것만)
hctrl answer q0003 "postgres로 하세요"  # 텍스트 질문
hctrl answer q0004 2                  # 선택 질문: 번호 또는 선택지 이름
hctrl answer q0005 --json '{"DB?": "postgres", "언어?": "go"}'   # 선택 질문이 여러 개일 때
hctrl answer q0006 allow              # 권한 승인
hctrl answer q0006 deny "배포는 사용자 결정입니다"   # 거절 (사유가 agent에게 전달됨)
```

- `hctrl wait <id>`는 질문이 생기면 **exit 3**으로 돌아옵니다. 답한 뒤 다시 `hctrl wait <id>`을 실행하세요. 실행 자체는 멈추지 않고 답을 기다립니다.
- 답이 `answer_timeout` 안에 오지 않으면 사용자에게 넘어갑니다.
  - 권한과 선택 질문: 화면의 대화상자가 그대로 있으므로 직접 처리합니다.
  - 텍스트 질문: 실행이 `blocked`로 멈춥니다. `hctrl answer`로 답한 뒤 `hctrl resume <id>`를 실행하세요.
- 화면에서 사람이 먼저 대화상자에 답해도 됩니다. 그러면 그 질문은 만료됩니다.
- hctrl이 처리하지 않는 승인 요청(예: `questions` 모드의 명령 실행 승인, 사용자에게 넘긴 요청)이 생기면, 실행은 끝나지 않고 알림을 띄운 채 기다립니다. 화면에서 직접 승인하면 그대로 이어집니다. 무인 모드가 off인 탭은 지금처럼 `blocked`로 멈추며, `hctrl resume`으로 이어갑니다.

### 안전장치

- **누가 실행했는지 판별**: hctrl은 어느 창이 어느 agent인지를 프로젝트 밖의 등록 정보로 판별하고, 상위 프로세스의 환경도 함께 확인합니다. 그래서 agent가 `state.json`을 고치거나, `env -u ...`로 환경 변수를 지우거나, `hctrl --project 다른프로젝트`를 실행해도 사용자 권한을 얻지 못합니다.
- **답할 수 있는 사람**: 사용자는 언제나 답할 수 있습니다. orchestrator는 자기 탭 질문에만, 모드가 허용하는 종류에만, 상한 안에서만 답할 수 있습니다. 작업 agent는 답할 수 없습니다.
- **항상 사용자에게 넘어가는 승인 요청** (orchestrator에게 묻지 않음):
  - deny 목록에 걸리는 요청
  - `.herdr-flow`, `.claude`, `herdr`, `hctrl`을 건드리는 요청
  - hctrl 상태 저장소에 접근하는 요청
  - 작업 폴더(프로젝트 또는 worktree) 밖의 파일을 쓰는 요청
- hctrl은 "항상 허용"이나 "auto mode 전환" 같은 **영구 권한 변경을 절대 선택하지 않습니다.** 매번 한 번만 허용합니다.
- 질문과 답은 작업 agent가 고칠 수 없도록 프로젝트 밖(`~/.local/state/herdr_ctl/`, `HC_STATE_HOME`으로 변경 가능)에 저장되고, `answers.jsonl`에 모두 기록됩니다.
- 실측 근거와 상세 설계: [docs/unattended-probe.md](docs/unattended-probe.md), [PLAN.md 10장](PLAN.md)

## 하루 사용 흐름

```bash
cd ~/study/BackEnd/projA && hctrl   # 프로젝트 세션 열기 (첫 실행 시 창 자동 구성)
hctrl run build-review-x          # A: 정해진 순서대로 자동 실행
hctrl ask X "로그인 기능 완성해줘"   # B: orchestrator에게 맡기기
hctrl zoom x1              # 필요할 때 크게 보기
```

## 참고

- 결과는 화면을 읽어서 넘기지 않고 **파일**로 주고받습니다. 정확도를 위해서입니다.
- 세션 안의 `_hc` 탭은 hctrl 전용입니다(설정 감시 `hctrl watch`와 키 메뉴가 실행 중). 닫지 마세요.
- `--detach` 실행은 별도 백그라운드 프로세스로 돌고, 로그는 `.herdr-flow/runs/<id>/worker.log`에 남습니다.
- 한 줄 역할을 바꾸면(`hctrl role x1 "..."`) yaml이 아니라 `.herdr-flow/roles/x1.md` 첫 줄(`# 역할: ...`)에 저장됩니다. 이 값이 yaml의 `role`보다 우선합니다.
- 쉘 단계는 pane의 쉘 종류와 관계없이 `bash -c`(pipefail)로 실행됩니다.
- `hctrl stop`은 실행 프로세스가 10초(`HC_STOP_GRACE_S`) 안에 멈추지 않으면 강제 종료합니다. 터미널에서 직접 `hctrl run` 중인 프로세스도 해당됩니다.
- 방금 띄운 agent에는 첫 지시 전 최대 8초 기다립니다(`HC_READY_GRACE_S`). 지시가 반응 없이 끝나면 자동 재전송하지 않고 실패로 기록합니다(`hctrl resume <id> --resend`로 직접 재전송).
- herdr 밖에서 `hctrl`을 실행할 때는 `--session <project>`로 대상 세션을 지정합니다.
- 설계 상세: [PLAN.md](PLAN.md)
