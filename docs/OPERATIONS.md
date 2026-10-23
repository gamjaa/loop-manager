# Loop Manager 운영 안내

첫 설치와 실행은 [README Quick Start](../README.md#quick-start)를 따른다. 이 문서는 설정 조정, 수동 제출과 장애 복구를 다룬다. 아래 `python`은 Loop Manager 저장소의 `.venv` Python을 뜻한다.

## 인증과 실행 조건

Git·`gh`와 선택한 Agent CLI를 설치·인증하고 대상 GitHub 저장소에 접근할 수 있는지 확인한다. Planner의 계획 게시에는 base branch 직접 push 권한이 필요하며 구현 PR은 사용자가 검토·Merge한다.

| 환경 | 실행 조건 |
|---|---|
| Windows | PowerShell, Codex 사용 시 Python 3.13 이상과 초기 설정을 마친 elevated sandbox |
| Linux·macOS | Bash, 공급자별 공통 홈과 호출별 임시 폴더 사용 |
| Codex | CLI 0.159.3 이상, 세 OS의 내장 sandbox 사전 검사 통과 |
| Claude Code | 설정 예시의 모델을 사용하려면 CLI 2.1.284 이상, 검증된 격리 경계 필요 |

Codex는 세 OS에서 상태 루트의 공통 홈을 인증·설정·session 저장소로 직접 사용한다. Windows ACL·Job Object, macOS Seatbelt, Linux 읽기 전용 mount·프로세스 그룹의 경계를 확인할 수 없으면 권한을 완화하지 않고 중단한다. 검증되지 않은 Claude 격리 호출도 거부한다. [Codex 권한 정책](https://developers.openai.com/codex/permissions) · [격리 설계](ARCHITECTURE.md#repository-safety-and-security)

### 전용 공급자 홈과 인증

DB·잠금에 실제 사용하는 `state_dir` 아래의 `automation-codex`·`automation-claude`를 공급자별 공통 홈으로 직접 사용한다. 상태 루트는 `--state-dir`, `LOOP_MANAGER_STATE_DIR`, OS 기본 경로 순서로 선택한다. 외부 `CODEX_HOME`·`CLAUDE_CONFIG_DIR`은 홈 선택에 사용하지 않고 기존 허용 인증 환경변수의 전달은 유지한다. Manager는 설정 파일을 생성하거나 개인 홈에서 가져오지 않는다. CLI가 공통 홈의 설정을 생성·원자적으로 교체하면 다음 호출은 그 홈의 현재 설정을 검사해 사용한다.

| 공급자 | 전용 환경값 | 기본 인증 파일 |
|---|---|---|
| Codex | `CODEX_HOME=<state_dir>/automation-codex` | `~/.codex/auth.json` |
| Claude | `CLAUDE_CONFIG_DIR=<state_dir>/automation-claude` | `~/.claude/.credentials.json` |

전용 인증 위치가 비어 있고 기본 파일이 있으면 그 파일만 절대 경로의 심볼릭 링크로 연결한다. 기존 파일·링크는 덮어쓰지 않는다. 기본 파일이 없으면 빈 파일·끊긴 링크를 만들지 않으며 전용 경로에서 인증을 확인한다. 끊긴 링크·읽기 불가·비공개 경로 검증이나 링크 권한 실패는 자동 복사·권한 확대 없이 진단한다. Windows의 최초 링크 생성에도 심볼릭 링크를 만들 수 있는 실행 환경이 필요하다.

인증 실패 메시지에 실제 사용한 홈과 해당 OS의 로그인·상태 확인 명령을 표시한다. 다음은 Bash 예시이며 `/path/to/state`를 실제 DB의 상태 루트로 바꾼다. PowerShell에서는 `$env:CODEX_HOME='실제 경로'; codex login` 또는 `$env:CLAUDE_CONFIG_DIR='실제 경로'; claude auth login`처럼 현재 셸에 지정한다.

```sh
env CODEX_HOME=/path/to/state/automation-codex codex login
env CODEX_HOME=/path/to/state/automation-codex codex login status
env CLAUDE_CONFIG_DIR=/path/to/state/automation-claude claude auth login
env CLAUDE_CONFIG_DIR=/path/to/state/automation-claude claude auth status
```

링크를 사용하는 인증 갱신은 기본 인증 파일에도 영향을 줄 수 있다. 로그인·로그아웃은 Manager가 자동 실행하지 않는다. CLI 호출 전후에 링크 공유를 확인하며 링크를 사본으로 교체한 결과는 정상 실행으로 사용하지 않는다. 원본 파일의 같은 경로에서 내용 갱신·원자적 교체는 다음 호출에도 반영한다. [Codex 파일·credential store](https://learn.chatgpt.com/docs/auth#credential-storage)와 [Claude 파일·Keychain](https://code.claude.com/docs/en/authentication#credential-management)은 별개다. 파일이 없는 저장소의 비밀을 추출하지 않으며 Linux·macOS Codex의 기본 사용자 저장소와 자동 실행 홈 사이에서 재사용을 확인하지 못한 `keyring`·`auto`·`ephemeral` 설정은 계속 거부한다. 필요한 파일 인증은 CLI의 지원 방식으로 준비하고 조직의 저장 정책을 완화하지 않는다.

기존 Codex 역할·worktree별 runtime 홈의 config·auth·session은 삭제·이동·복제하지 않으며 새 호출 입력으로 연결하지 않는다. 이전 session은 저장된 ID로 재개를 시도하고 사용할 수 없으면 기존 작업·실패 문맥을 보존하는 새 session fallback을 따른다. Codex의 호출별 임시 폴더는 기존 runtime 아래에, Claude의 임시 폴더는 기존 worktree별 로그 경로 아래에 따로 생성한다. 공통 홈을 Agent 도구의 쓰기 허용 범위에 추가하지 않으며 역할별 권한과 명시적 실행값을 유지한다. 공통 홈 선택만으로 프로젝트·관리 설정이 무효화되거나 Claude의 사용자 설정 source 전체 제외·격리 지원이 완료됐다고 간주하지 않는다.

기존 상태 루트가 `/home/ubuntu/.local/state/loop-manager`인 cron은 기존 `automation-codex`를 재사용하므로 `env CODEX_HOME=...`을 제거하고 `bash /home/ubuntu/loop-manager/run.sh --skip-if-running`으로 전환할 수 있다. DB의 상태 루트를 고정하는 `LOOP_MANAGER_STATE_DIR` 등 다른 옵션은 유지한다. 실제 crontab은 자동 수정하지 않는다.

### Linux PATH 캐시

`bash scripts/setup.sh`는 호출 환경의 PATH를 source checkout의 `config/execution-path.txt`에 저장한다. Git·gh와 Codex 또는 Claude가 발견되는 정상 터미널에서 실행하며 별도 로그인 셸이나 `.bashrc`를 자동 실행하지 않는다. 경로 순서·공백·심볼릭 링크는 유지하고 중복·빈 항목·상대 경로·존재하지 않는 디렉터리는 제외한다. setup 준비·경로 검사·저장 실패는 기존 캐시를 보존한다. `config`의 미추적 파일은 폴더 전체가 Git에서 제외되며 기존 `config.ini.example`은 계속 추적한다.

Python source 진입은 bootstrap의 첫 Git 호출 전에 저장된 PATH, 현재 PATH의 추가 디렉터리, 기본 시스템 디렉터리 순서로 합친다. runtime 자식·Agent·verify는 확정된 환경을 상속하며 캐시를 다시 읽지 않는다. 캐시 위치는 source checkout에 고정되고 cwd·`--config`·state_dir 변경과 무관하다. 캐시가 없으면 현재 PATH와 시스템 후보를 사용하므로 PATH를 명시한 기존 cron 설정도 사용할 수 있다.

프로그램이나 Node 등의 의존 실행 파일을 찾지 못하면 같은 계정의 정상 터미널에서 설치 경로를 확인하고 setup을 다시 실행한다. 읽기 불가·빈 파일·손상된 캐시는 bootstrap 전에 중단하며 현재 PATH로 조용히 대체하지 않는다. setup 재실행은 PATH 캐시만 갱신하고 `config.ini`·인증·DB·session은 초기화하지 않는다. 실행 중 캐시 갱신은 다음 source 실행부터 적용된다. 가상환경을 다시 만들어도 `config`의 PATH 캐시는 유지된다.

## 설정

[config.ini.example](../config/config.ini.example)을 `config/config.ini`로 복사해 편집한다. setup은 기존 `.venv`를 재사용하고 `requirements.txt`의 패키지를 설치한다. 현재 외부 Python 의존성은 없으며 추가되면 같은 setup을 다시 실행한다.

| 항목 | 설정 기준 |
|---|---|
| provider 모델·역할 | 선택 provider의 Model alias와 `Model alias,Effort`를 직접 지정한다. `planner`·`planner_check`·Worker level 1~8과 참조 alias는 모두 필요하다. 누락·빈 값·미지원 조합은 실행 전에 거부하며 예시를 fallback으로 읽지 않는다. |
| `planner_check` | 기존 호출 이력·저장 상태의 호환성을 위해 역할 설정을 유지한다. 코드·테스트·설정 변경만으로 새 점검 호출을 만들지 않는다. 유지보수 Planner는 `maintenance_planner`를 사용하며 생략하면 일반 `planner` 설정을 재사용한다. |
| `[codex] fast_mode` · `[claude] fast_mode` | provider별 Fast 선택. 기본값은 `false`이며 공란·공백·section 또는 항목 생략도 off다. CLI 기본 설정이나 저장된 session에서 on이어도 off가 우선하도록 전달한다. `true`는 on이며 `on/off`·`yes/no`·`1/0`도 허용한다. 모델·Effort는 그대로 전달하며 지원·대체 실행은 CLI에 맡긴다. Fast 사용에는 추가 비용 또는 구독 사용량 증가가 발생할 수 있다. |
| `[telegram]` | `token`·`chat_id`가 모두 있으면 알림 사용, 둘 다 없으면 비활성화, 하나만 있으면 설정 오류다. |
| `[loop] max_workers` | 전체 Worker·Reviewer 실제 동시 호출 상한. 기본값 `3`. |
| `[loop] max_tasks_per_project` | 프로젝트별 관리 대상 기본 상한. 기본값 `1`. 프로젝트 DB의 0은 이 값을 상속하고 양수는 개별 상한이다. |
| `[loop] verify_concurrency` | 전체 verify 동시 호출 상한. 기본값 `1`. Worker·Reviewer와 겹칠 수 있다. |
| `[loop] refactoring_review_interval` | 새 MERGED 작업 수에 따른 자동 유지보수 주기. 기본값 `20`. `0`이면 자동 전환을 끄며 누적 수와 실행 중 cycle은 보존한다. `python loop_manager.py maintenance-review <project-path>`로 등록된 프로젝트의 다음 안전한 경계에 점검을 요청할 수 있다. |
| `verify_timeout_seconds` | 확인된 순번 대기를 제외한 전체 verify 실제 실행 상한(초). 기본값 `600`. 필요한 검증이 정상 진행 중이라는 근거가 있을 때 조정한다. |
| `verify_parallelism` | Loop Manager 자체 verify의 모듈 배정 상한. 기본값 `auto`, 양의 정수는 verify별 상한, `1`은 직렬이다. [배정·기록](#verify-배정과-진단) |

Fast 선택은 한 번 실행을 시작할 때 읽어 신규·재개·재시도 호출에 고정한다. 설정 변경은 다음 Manager 실행에 반영하고 session·작업물·Worker level은 Fast 선택만으로 초기화하지 않는다. 호출 로그의 `loop_manager.fast_mode` 기록에서 요청값 `true`·`false`를 확인한다. 이 값은 실제 Fast 적용 성공을 뜻하지 않는다. CLI 진단·실패는 기존 처리 규칙을 따르며 모델별 사전 판정이나 CLI 버그 우회를 추가하지 않는다.

Fast 선택의 CLI 전달 구문은 다음과 같다. Manager는 기존 모델·Effort·지침·권한·sandbox 인자를 보존한 채 이 설정을 합친다. 공란·생략도 아래 off 설정을 전달한다.

| provider | on | off |
|---|---|---|
| Codex | `-c 'service_tier="fast"' -c 'features.fast_mode=true'` | `-c 'service_tier="default"' -c 'features.fast_mode=false'` |
| Claude | 기존 `--settings` JSON에 `"fastMode": true` 추가 | 기존 `--settings` JSON에 `"fastMode": false` 추가 |

실제 config는 commit하지 않고 현재 사용자만 읽을 수 있도록 관리한다. 별도 경로는 전역 옵션으로 지정한다.

```sh
python loop_manager.py --config /path/to/config.ini --state-dir /path/to/state run
```

bootstrap은 source checkout 기준으로 설정 경로를 확정해 runtime에 전달하며 runtime별로 비밀 설정을 복제하지 않는다.

## 프로젝트 문서와 등록

기존 프로젝트는 `.agents/skills/`, `docs/PLANS.md`, `docs/*.template.md`와 필요한 문서를 선택적으로 병합한다. PLANS와 템플릿은 복사된 공통 파일을 사용하고 [예약 경로](AGENTS.common.md#예약-경로)를 유지한다. 복사 스크립트는 자동 Loop에서 실행하지 않는다.

등록 검사는 fetch한 원격 base를 사용한다. `docs/PLANS.md`는 Manager 기준 해시와 일치해야 하고 작성된 ARCHITECTURE 및 PRODUCT 또는 활성 Requirement가 필요하다. 빈 템플릿만 게시하면 등록할 수 없다. [등록 조건](ARCHITECTURE.md#프로젝트-등록)

Requirement는 frontmatter·상태 없이 `docs/requirements/` 바로 아래에 두며 모두 Planner 입력이다. 새 기능·정책 변경을 요청받은 대화형 Agent는 관련 활성 Requirement를 먼저 작성·갱신하거나 충분한 기존 문서를 재사용한다. Skill은 명세를 정리하고 Planner가 ExecPlan을 만든다.

```sh
python loop_manager.py add /path/to/my-project --base main --provider codex
python loop_manager.py remove /path/to/my-project
```

진행 중인 계획·프로젝트 차단·미해결 planning 오류가 없을 때만 등록을 해제할 수 있다. 해제는 registry만 바꾸며 파일·branch·worktree·PR·MERGED 감사는 보존한다.

기본 `run`은 모든 등록 프로젝트를 처리한다. 특정 프로젝트만 실행하려면 경로를 하나 이상 지정한다. 공백이 있는 경로는 shell에서 따옴표로 감싼다.

```sh
python loop_manager.py run /path/to/project-a '/path with spaces/project-b'
```

상대 경로는 명령을 호출한 작업 디렉터리 기준으로 해석한다. 지정 경로는 정규화한 뒤 중복 제거하며 전체 경로의 등록 여부를 처리 전에 확인한다. 하나라도 등록되지 않으면 프로젝트 처리를 시작하지 않는다. `--skip-if-running`은 지정 범위에서도 기존처럼 다른 Loop Manager 실행이 있을 때 조용히 성공 종료한다.

## 프로젝트별 관리 한도

등록된 프로젝트의 관리 대상 상한은 실행 중에도 변경할 수 있다.

```sh
python loop_manager.py config set /path/to/my-project --max-tasks-per-project 4
python loop_manager.py config set /path/to/my-project --max-tasks-per-project 0
python loop_manager.py status /path/to/my-project --json
```

0은 config 기본값 상속, 양수는 개별 상한이다. 음수·정수가 아닌 값과 미등록 대상은 거부한다. 변경은 Agent·Git·bootstrap·Manager 재시작을 실행하지 않으며 다음 안전한 관리 배정에서 DB를 다시 읽는다. 상한 축소는 기존 작업·세션·작업물을 보존하고 새 관리 배정만 보류한다. pause 중에도 저장할 수 있다.

`status <repo>`는 읽기 전용으로 저장값·유효값·출처·현재 DB의 관리 수를 표시한다. 유효값은 조회 명령이 읽은 config 기준이다. 실행 중 Manager의 config·실제 호출 수·반영 여부는 이 조회로 확인하지 않으며 JSON의 `warnings`에도 표시한다. 전체 운영 상태·다른 설정 CLI·Loop Manager 관리 Skill의 안내는 [R-076](requirements/%5BR-076%5D%20운영%20명령어와%20상태%20조회%20및%20Skill%20지원.md)의 별도 목표다.

## 수동 구현과 제출

기존 G를 다른 기기에서 구현하려면 다음을 실행한다.

```sh
python loop_manager.py manual start /path/to/my-project G-012
```

Manager가 선행 작업의 실제 원격 base Merge를 확인한 뒤 소유 branch를 생성·push한다. 안내된 branch를 다른 기기에서 checkout해 코드·테스트와 필요한 R 변경을 push한 뒤 제출한다. 수동 작업 대기는 실행 자리를 점유하지 않아 독립 G는 계속 처리한다.

```sh
python loop_manager.py manual complete /path/to/my-project G-012
```

G 없이 R부터 구현까지 원격 branch에 올렸으면 source branch와 R 경로를 제출한다. `--requirement`는 반복할 수 있다. 다음 예시는 Bash이며 PowerShell에서는 한 줄로 실행한다.

```sh
python loop_manager.py manual submit /path/to/my-project \
  --branch feature/manual --requirement 'docs/requirements/[R-001] Feature.md'
```

Manager가 완성된 G를 생성하고 별도 소유 branch에 source 이력과 함께 보존한다. 복수 G는 같은 최종 후보·PR을 공유하므로 내부 선행 Merge가 필요한 묶음은 분리 제출한다. 이후 수정은 안내된 소유 branch로 push한다. 최초 source의 새 변경은 같은 source·R 범위로 `submit`을 다시 실행해야 반영된다.

`complete`·`submit`은 기본적으로 전체 verify와 읽기 전용 Review를 수행하고 실패하면 자동 교정·재검증한다. 외부 검증을 생략 근거로 쓰려면 해당 단계의 `--skip-verify`·`--skip-review`, 원격 commit의 전체 SHA인 `--expected-head`, `--skip-reason`을 함께 제공한다. 생략은 `SKIPPED_BY_USER`로 기록해 PASS와 구분한다. SHA·base·G/R·환경 또는 관련 단계 입력이 달라지면 기본 검증을 다시 실행한다. `submit`의 G 생성 commit도 SHA를 바꾸므로 source SHA의 생략 승인은 무효화된다.

| 종료 코드 | 의미 |
|---|---|
| `0` | 수동 준비 또는 PR 연결 완료 |
| `1` | 수락 후 대기·자동 복구 중. 저장된 요청은 다음 `run`에서 이어진다. |
| `2` | 인자 오류 |

PR 생성이나 검증 생략은 완료 근거가 아니다. 저장된 동일 branch·PR의 실제 Merge를 확인해야 구성 G 전체가 MERGED가 된다.

직접 작업·대화형 Agent의 로컬 구현도 활성 Requirement에서 시작하지만 ExecPlan·자동 Worker 역할·SQLite 전이·자동 완료 절차를 요구하지 않는다. 구현과 Requirement를 원격 base에 게시하면 Planner는 실제 미충족 작업만 재점검하며 소급·중복 계획이나 자동 검증 성공 기록을 만들지 않는다.

## 차단과 로컬 복구

알림의 원인·질문·확인 대상과 Checkpoint를 먼저 확인한다. 원격 코드·문서·사용자 결정 변경은 commit·push하고 `run`으로 재평가한다. 자동 복구·재시도 중이라는 이유만으로 사용자 수정이 필요한 것은 아니다.

로컬 설정·인증·도구·권한·timeout을 고쳤다면 원격 변경 없이 재검사한다.

```sh
python loop_manager.py recover /path/to/my-project
python loop_manager.py recover /path/to/my-project --plan G-001
```

Windows에서는 `.\.venv\Scripts\python.exe`와 `D:\repo\my-project` 같은 경로를 사용한다. `--plan`은 해당 계획만 검사하며 원격 변경은 기존 `run`의 자동 복구 경로가 처리한다.

다른 작업 중에도 요청을 저장할 수 있다. 실제 복구는 같은 상태 디렉터리의 전역 실행 잠금을 얻은 뒤 진행하며 즉시 실행할 수 없으면 요청 identity와 대기 안내를 출력하고 다음 `run`에서 이어진다. 프로젝트 전체와 서로 다른 계획의 요청은 모두 저장하고 같은 범위의 반복 요청만 미완료 요청을 재사용한다. 접수 순서대로 실행 가능한 요청을 하나씩 처리하며 앞선 요청이 대기 중이어도 뒤의 실행 가능한 요청은 진행한다. 잘못된 대상·미해결 원인·실행 자리 대기는 0이 아닌 종료 코드로 보고한다.

스케줄러의 정상 중복 실행은 `run --skip-if-running`으로 건너뛴다. 옵션 없이 전역 lock이 충돌하면 시각이 포함된 오류와 종료 코드 `1`을 반환하며 lock 외 오류는 그대로 보고한다.

## 상태 보존과 복원

상태·PR·session은 source checkout 밖 사용자 상태 디렉터리의 SQLite에 저장한다.

| OS | 기본 경로 |
|---|---|
| Windows | `%LOCALAPPDATA%\loop-manager\` |
| Linux·macOS | `${XDG_STATE_HOME:-~/.local/state}/loop-manager/` |

**SQLite와 기존 감사 자료를 함께 백업한다.** 과거 `agent-guidance/` snapshot은 감사에만 보존하며 재개 지침은 매 호출 현재 runtime에서 읽는다. `agent-instructions/`의 실행용 파일은 현재 원본으로 재생성할 수 있다. [지침 적용 계약](ARCHITECTURE.md#지침-원본과-호출별-적용)

SQLite가 없거나 손상되면 일반 `run`은 자동 초기화하지 않는다. 백업을 복원하거나 손상 DB를 별도 보관한 뒤 명시적으로 `config init`·재등록한다. 자동 상태 재구성은 지원하지 않는다.

Merge 확인 후 clean하고 PR head에 없는 로컬 commit이 없는 worktree만 Merge·Squash·Rebase 방식과 관계없이 정리한다. 불확실한 작업 공간은 `cleanup_pending`으로 보존한다. 다음 작업·프로젝트 완료를 막지 않으며 안전 조건 충족·경로 부재 시 자동 해제된다.

## Verify 배정과 진단

Worker는 관련 targeted unit test와 verify 스크립트를 준비하고 전체 verify는 Manager가 Agent 밖에서 실행한다. Windows는 `scripts/verify.ps1`, Linux·macOS는 `scripts/verify.sh`를 사용한다. Loop Manager 자체 스크립트는 `.venv`를 우선 사용하고 어디서 호출해도 저장소 루트에서 실행한다. 공통 runner가 전체 unittest·compile을 수행하며 테스트 프로세스의 실제 Agent CLI·외부 API 연결은 차단된다.

`verify_parallelism = auto`는 허용 CPU·실제 부하·메모리 여유와 아직 관측에 반영되지 않은 예약을 확인해 새 테스트 모듈을 배정한다. 정수 상한 안에서도 자원이 부족하면 기다리며 관측 실패 시 직렬로 축소한다. macOS도 CPU·메모리 관측이 불가능하면 직렬로 축소한다. `max_workers` 및 verify 호출 수를 제한하는 `verify_concurrency`와 별개이며 다른 등록 프로젝트의 내부 테스트 병렬도에는 적용하지 않는다.

시간 이벤트·worker 원문·manifest·집계는 Manager 실행에서 `<state-dir>/logs/verify/<실행 ID>/`, 단독 스크립트에서 `.cache/verify/<실행 ID>/`에 남는다. 시간 집계만 읽으려면 다음을 실행한다. 테스트는 실행하지 않는다.

```sh
python -m loop_manager_internal.verification.runner --report <직렬 기록 디렉터리> <auto 기록 디렉터리>
```

비교 범위·관측 환경이 같고 원본의 부하·메모리 제한을 확인한 경우에만 시간을 비교한다. [검증·복구 설계](ARCHITECTURE.md#검증과-재시도)

### Windows Defender와 테스트 임시 폴더

Loop Manager 자체 `scripts/verify.ps1`은 `%USERPROFILE%\.cache\loop-manager\verify\tmp\`를 생성하고 실행 중 `TEMP`·`TMP`·`TMPDIR`로 지정한다. checkout·Codex·상태 DB 위치와 무관한 고정 경로이며 AppData 리다이렉션을 피한다. `LOOP_MANAGER_STATE_DIR`을 지정해도 이 테스트 임시 경로는 바뀌지 않는다. 기본 임시 경로를 사용하는 테스트의 Git 저장소·SQLite 등은 이 폴더 아래에 생성된다. 성공·실패 모두에서 기존 환경 변수와 작업 디렉터리를 복원하며, 동시 실행의 파일을 지우지 않도록 공유 임시 폴더 전체를 삭제하지 않는다. 다른 등록 프로젝트의 verify에는 자동 적용되지 않는다.

Defender 검사로 verify가 느릴 때만 검사 제외를 선택적으로 설정한다. 기본 추천 범위는 테스트 전용 임시 폴더다. 관리자 PowerShell에서 다음을 실행한다.

```powershell
Add-MpPreference -ExclusionPath (Join-Path $env:USERPROFILE '.cache\loop-manager\verify\tmp')
```

폴더 제외 후에도 Defender의 CPU 점유와 verify 지연이 계속되면 성능 기록으로 원인 프로세스를 확인한 뒤 실제 실행 파일의 전체 경로에 대한 프로세스 제외를 검토한다. 프로세스 제외는 그 프로세스가 여는 파일에 적용되므로 폴더 제외보다 범위가 넓다. 실행 파일 이름만으로 모든 같은 이름의 프로세스를 제외하거나 드라이브·사용자 임시 폴더 전체를 제외하지 않는다. 설정은 사용자가 선택하며 스크립트는 변경하지 않는다. [Microsoft 제외 설정 안내](https://learn.microsoft.com/en-us/defender-endpoint/microsoft-defender-antivirus-exclusions-overview#process-exclusions)

같은 사용자 계정의 checkout과 worktree는 같은 테스트 임시 폴더를 사용하므로 실행 checkout마다 제외를 등록할 필요가 없다. 진단 로그는 단독 실행의 `.cache/verify/<실행 ID>/` 또는 Manager 실행의 `<state-dir>/logs/verify/<실행 ID>/`에 계속 남는다. 제외 전후에는 같은 검증 범위와 비슷한 부하에서 시간을 비교한다.

## 재시도와 배정 중단

RETRY 로그는 진행 중인 작업을 수동 제출 다음, 다른 일반 작업보다 먼저 재배정한다는 뜻이다. PLAN_BLOCKED이면 독립 작업은 계속 진행한다. Planner 필요 시 해당 프로젝트, runtime handoff 시 전체 프로젝트의 새 배정이 중단되고 실행 중 호출 반환을 기다린다. verify·실행 자리 대기는 작업 전체 완료로 취급하지 않으며 대기 시간은 timeout에서 제외한다.
