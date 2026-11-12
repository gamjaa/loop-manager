# Loop Manager

> **문서 구분:** 이 README와 [운영 안내](docs/OPERATIONS.md)는 현재 구현의 사용법을 설명한다. `develop`의 [PRODUCT](docs/PRODUCT.md)·[ARCHITECTURE](docs/ARCHITECTURE.md)는 아직 구현되지 않은 재설계 목표이며, 수동 제출 등 일부 기능 범위가 다르다.

Loop Manager는 여러 로컬 GitHub 프로젝트의 **Planner → Worker → Verify → Reviewer → PR 생성**을 자동화한다. 사용자는 제품 문서를 게시하고, 필요한 결정을 내리고, PR을 검토·Merge한다.

중단된 작업과 Agent 세션은 다음 실행에서 이어진다. 구현·Review는 전체 `max_workers`, 프로젝트별 관리 대상은 `max_tasks_per_project`, verify는 별도 `verify_concurrency` 한도에서 병렬 처리하며 PR·차단·대기·완료를 Telegram으로 알린다.

## Quick Start

Python 3, Git, GitHub CLI(`gh`)와 인증된 Codex 또는 Claude Code CLI를 준비한다. Windows는 PowerShell, Linux·macOS는 Bash를 사용한다. Codex는 0.159.3 이상이며 Windows에서는 Python 3.13 이상과 초기 설정을 마친 elevated sandbox가 필요하다. [인증·실행 조건](docs/OPERATIONS.md#인증과-실행-조건)

아래 명령은 Loop Manager 저장소 루트에서 실행한다.

### 1. 설치와 설정

Linux·macOS:

```sh
bash scripts/setup.sh
cp config/config.ini.example config/config.ini
```

Windows:

```powershell
pwsh -NoProfile -File scripts/setup.ps1
Copy-Item -LiteralPath config/config.ini.example -Destination config/config.ini
```

`config/config.ini`에서 선택 provider의 모델·Effort를 확인한다. Telegram을 사용하려면 `token`과 `chat_id`를 모두 입력하고, 사용하지 않으면 둘 다 비워 둔다. **실제 config는 비밀값을 포함할 수 있으므로 commit하지 않는다.** [설정 항목](docs/OPERATIONS.md#설정)

Linux setup은 정상 터미널의 PATH를 `config/execution-path.txt`에 저장한다. Git·gh와 Codex 또는 Claude를 찾을 수 있는 같은 실행 계정의 터미널에서 실행하며, 설치 경로가 바뀌면 setup을 다시 실행한다. Python 진입부가 bootstrap 전에 이 PATH를 적용하므로 cron에서도 기존 launcher를 사용한다. 캐시가 없으면 cron에 지정한 PATH를 포함한 현재 환경과 시스템 경로를 사용한다. Linux 명령 탐색 실패가 진단되면 같은 계정의 정상 터미널에서 설치 경로를 확인하고 source checkout에서 `bash scripts/setup.sh`를 다시 실행한다. [PATH 저장·복구](docs/OPERATIONS.md#linux-path-캐시)

Windows에서 Defender 검사로 verify가 느리면 [검사 제외 설정](docs/OPERATIONS.md#windows-defender와-테스트-임시-폴더)을 확인한다.

### 2. 프로젝트 문서 준비와 게시

새 프로젝트에는 다음 중 하나를 실행한다.

```sh
bash scripts/copy-project-files.sh /path/to/my-project
```

```powershell
pwsh -File scripts/copy-project-files.ps1 -TargetPath D:\repo\my-project
```

스크립트는 Specification Skill·템플릿·빈 작업 폴더와 PRODUCT·ARCHITECTURE 초안을 준비한다. `.agents`·`docs`가 이미 있으면 중단하므로 기존 프로젝트는 [필요한 파일만 병합](docs/OPERATIONS.md#프로젝트-문서와-등록)한다.

프로젝트에서 다음 문서를 작성한 뒤 **commit·push**한다. 자동 입력은 최신 원격 base이며 로컬 전용 파일·commit은 포함되지 않는다.

| 문서 | 내용 |
|---|---|
| `docs/PRODUCT.md` | 현재 제품의 동작·경계·장기 규칙 |
| `docs/requirements/*.md` | 새 기능·정책 변경과 Acceptance Criteria |
| `docs/ARCHITECTURE.md` | 제품 규칙을 보장하는 구조·설계 계약 |

PRODUCT와 활성 Requirement 중 하나 이상이 필요하다. 초기 제품은 PRODUCT로 정의하고, 운영 중 변경은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)로 관련 활성 Requirement를 작성·갱신한다. ExecPlan은 Manager가 호출하는 Planner가 만든다.

### 3. 등록과 첫 실행

Linux·macOS:

```sh
.venv/bin/python loop_manager.py config init
.venv/bin/python loop_manager.py add /path/to/my-project --provider claude
.venv/bin/python loop_manager.py config list
.venv/bin/python loop_manager.py run
```

Windows:

```powershell
& .\.venv\Scripts\python.exe loop_manager.py config init
& .\.venv\Scripts\python.exe loop_manager.py add D:\repo\my-project --provider claude
& .\.venv\Scripts\python.exe loop_manager.py config list
& .\.venv\Scripts\python.exe loop_manager.py run
```

`init`은 최초 한 번, `add`는 프로젝트마다 실행한다. provider 생략 시 `codex`, base 생략 시 `origin/HEAD`를 사용한다. Planner의 계획 게시에는 base branch 직접 push 권한이 필요하고 구현은 `loop-manager/<execplan-id>` branch의 PR로 제출한다.

새 환경에서 기존 원격 작업을 이어갈 때는 [순차 환경 전환과 최초 복구 범위](docs/OPERATIONS.md#새-환경에서-원격-작업-이어하기)를 먼저 확인한다.

## 기본 사용법

아래 `python`은 설치한 `.venv`의 Python으로 실행한다. `--help`로 각 명령의 옵션을 확인할 수 있다.

자동 유지보수 주기는 `config/config.ini`의 `[loop] refactoring_review_interval`로 설정한다.
기본값은 새로 Merge된 작업 20개이며, `0`이면 자동 전환을 끈다. 이때도 아래 명령으로
유지보수를 요청할 수 있고, 다음 안전한 경계에서 실행한다.

| 목적 | 명령 |
|---|---|
| 프로젝트 검증 및 등록 | `python loop_manager.py add <project-path> [--base <branch>] [--provider <provider>]` |
| 등록된 프로젝트 처리 | `python loop_manager.py run` |
| 지정 프로젝트만 처리 | `python loop_manager.py run <project-path> [<project-path> ...]` |
| 스케줄러에서 중복 실행 건너뛰기 | `python loop_manager.py run --skip-if-running` |
| 등록 목록 | `python loop_manager.py config list` |
| 등록 해제 | `python loop_manager.py remove <project-path>` |
| 다음 안전한 경계에서 유지보수 요청 | `python loop_manager.py maintenance-review <project-path>` |
| 사용자 판단·공통 교정 차단 해제 | `python loop_manager.py unblock <project-path> G-001 [G-002 ...]` |
| 로컬 환경 수정 후 재검사 | `python loop_manager.py recover <project-path> [--plan G-001]` |
| 기존 계획을 수동 구현 | `python loop_manager.py manual start <project-path> G-012` |
| 수동 구현 제출 | [complete·submit 안내](docs/OPERATIONS.md#수동-구현과-제출) |

제품 문서 변경을 push한 뒤 `run`을 실행하면 Planner가 남은 작업을 계획한다. Worker는 구현·관련 unit test, Manager는 전체 verify, Reviewer는 읽기 전용 검토를 맡는다. 의존 작업은 선행 PR의 실제 Merge를 기다리며 안정된 열린 PR이나 계획별 차단이 있어도 독립 READY는 진행한다. 한 프로젝트의 차단·일시 실패는 다른 프로젝트를 막지 않는다.

반복 실행은 cron 또는 Windows 작업 스케줄러에 `run --skip-if-running`으로 등록한다. 같은 성공 입력에 할 일이 없으면 Agent를 반복 호출하지 않는다. 실행 코드는 최신 `origin/main`의 정확한 SHA를 사용하며 source checkout의 로컬 변경은 실행 revision이 아니다. [runtime·동기화 구조](docs/ARCHITECTURE.md#11-자체-runtime-선택과-갱신)

`run`에 경로를 지정하면 해당 등록 프로젝트만 처리한다. 여러 경로와 공백이 있는 경로를 지원하며, 경로를 정규화·중복 제거한 뒤 전체가 등록됐는지 확인한다. 하나라도 등록되지 않았다면 어떤 프로젝트도 처리하지 않는다.

## 운영: PR과 차단 처리

| 상황 | 사용자 조치 |
|---|---|
| PR Merge 대기 | 출력·Telegram 링크로 검토하고 Merge한다. Merge 없이 닫았으면 같은 PR을 다시 연다. |
| 문서·사용자 결정·작업물 때문에 차단 | 원인·질문·확인 대상·Checkpoint를 확인하고 필요한 변경을 commit·push한다. 다음 `run`에서 재평가한다. |
| 로컬 설정·인증·도구·권한·timeout 문제 | 원인을 고친 뒤 `recover`로 재검사한다. [복구 요청](docs/OPERATIONS.md#차단과-로컬-복구) |
| SQLite 없음·손상 | 백업을 복원하거나 손상 DB를 별도 보관한 뒤 명시적으로 `config init`·재등록한다. 일반 `run`은 자동 초기화하지 않는다. |
| `cleanup_pending` | 미커밋·미게시 작업 또는 불확실한 worktree가 보존돼 있다. 안전 조건 충족·경로 부재 시 자동 해제되며 다음 작업·완료를 막지 않는다. |

PR 생성은 완료가 아니다. 같은 PR의 실제 Merge를 확인한 뒤 완료 처리하며 clean하고 PR head에 없는 로컬 commit이 없는 worktree만 정리한다. 안전성을 확인할 수 없으면 보존한다. **SQLite와 기존 상태 디렉터리의 감사 자료를 함께 백업한다.** [상태 보존·복원](docs/OPERATIONS.md#상태-보존과-복원)

## 상세 문서

- [공통 verify](docs/VERIFY.md): 선택 선언·단독 호출·unittest 지원과 adapter 계약
- [운영 안내](docs/OPERATIONS.md): 인증·설정·등록·수동 제출·복구·verify 진단
- [PRODUCT](docs/PRODUCT.md): 제품 동작·상태·완료 조건
- [ARCHITECTURE](docs/ARCHITECTURE.md): 책임·저장·실행 격리·복구·호출별 runtime 지침
- [PLANS](docs/PLANS.md): 공통 ExecPlan 형식
- [Specification Skill](.agents/skills/specification-docs/SKILL.md): 문서 작성·검토

Worker의 코드 품질 지침에는 [Ponytail](https://github.com/DietrichGebert/ponytail)을 사용한다. [재설계의 적용 방식](docs/ARCHITECTURE.md#6-agent-실행과-품질-지침)
