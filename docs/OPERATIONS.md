# Loop Manager 운영 안내

첫 설치와 실행은 [README Quick Start](../README.md#quick-start)를 따른다. 이 문서는 설정 조정, 수동 제출과 장애 복구를 다룬다. 아래 `python`은 Loop Manager 저장소의 `.venv` Python을 뜻한다.

## 인증과 실행 조건

Git·`gh`와 선택한 Agent CLI를 설치·인증하고 대상 GitHub 저장소에 접근할 수 있는지 확인한다. Planner의 계획 게시에는 base branch 직접 push 권한이 필요하며 구현 PR은 사용자가 검토·Merge한다.

| 환경 | 실행 조건 |
|---|---|
| Windows | PowerShell, Codex 사용 시 Python 3.13 이상과 초기 설정을 마친 elevated sandbox |
| Linux·macOS | Bash, 역할별 runtime에서 원본 Agent 인증·설정에 연결 |
| Codex | CLI 0.159.3 이상, 세 OS의 내장 sandbox 사전 검사 통과 |
| Claude Code | 설정 예시의 모델을 사용하려면 CLI 2.1.284 이상, 검증된 격리 경계 필요 |

Windows Codex는 기존 `CODEX_HOME`의 인증·session 저장소를 사용한다. Windows ACL·Job Object, macOS Seatbelt, Linux 읽기 전용 mount·프로세스 그룹의 경계를 확인할 수 없으면 권한을 완화하지 않고 중단한다. 검증되지 않은 Claude 격리 호출도 거부한다. [Codex 권한 정책](https://developers.openai.com/codex/permissions) · [격리 설계](ARCHITECTURE.md#repository-safety-and-security)

## 설정

[config.ini.example](../config/config.ini.example)을 `config/config.ini`로 복사해 편집한다. setup은 기존 `.venv`를 재사용하고 `requirements.txt`의 패키지를 설치한다. 현재 외부 Python 의존성은 없으며 추가되면 같은 setup을 다시 실행한다.

| 항목 | 설정 기준 |
|---|---|
| provider 모델·역할 | 선택 provider의 Model alias와 `Model alias,Effort`를 직접 지정한다. `planner`·`planner_check`·Worker level 1~8과 참조 alias는 모두 필요하다. 누락·빈 값·미지원 조합은 실행 전에 거부하며 예시를 fallback으로 읽지 않는다. |
| `planner_check` | `[codex.roles]` 또는 `[claude.roles]`에서 읽기 전용 점검 Planner에 독립 적용한다. 유지보수 Planner는 `maintenance_planner`를 사용하며 생략하면 일반 `planner` 설정을 재사용한다. |
| `[codex] fast_mode` · `[claude] fast_mode` | provider별 Fast 선택. `true`는 on, `false`는 CLI 기본값이 on이어도 off, 공란·생략은 CLI 기본 설정 상속이다. `on/off`·`yes/no`·`1/0`도 허용한다. 모델·Effort는 그대로 전달하며 지원·대체 실행은 CLI에 맡긴다. Fast 사용에는 추가 비용 또는 구독 사용량 증가가 발생할 수 있다. |
| `[telegram]` | `token`·`chat_id`가 모두 있으면 알림 사용, 둘 다 없으면 비활성화, 하나만 있으면 설정 오류다. |
| `[loop] max_workers` | 동시 처리 프로젝트 수. 기본값 `3`. |
| `verify_timeout_seconds` | 전체 verify 상한(초). 기본값 `600`. 필요한 검증이 정상 진행 중이라는 근거가 있을 때 조정한다. |
| `verify_parallelism` | Loop Manager 자체 verify의 모듈 배정 상한. 기본값 `auto`, 양의 정수는 verify별 상한, `1`은 직렬이다. [배정·기록](#verify-배정과-진단) |

Fast 선택은 한 번 실행을 시작할 때 읽어 신규·재개·재시도 호출에 고정한다. 설정 변경은 다음 Manager 실행에 반영하고 session·작업물·Worker level은 Fast 선택만으로 초기화하지 않는다. 호출 로그의 `loop_manager.fast_mode` 기록에서 요청값 `true`·`false`·`null`(CLI 기본 설정 상속)을 확인한다. 이 값은 실제 Fast 적용 성공을 뜻하지 않는다. CLI 진단·실패는 기존 처리 규칙을 따르며 모델별 사전 판정이나 CLI 버그 우회를 추가하지 않는다.

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
python loop_manager.py config add /path/to/my-project --base main --provider codex
python loop_manager.py config remove /path/to/my-project
```

진행 중인 계획·프로젝트 차단·미해결 planning 오류가 없을 때만 등록을 해제할 수 있다. 해제는 registry만 바꾸며 파일·branch·worktree·PR·MERGED 감사는 보존한다.

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

**SQLite와 `agent-guidance/`의 원본 snapshot을 함께 백업한다.** 원본은 `agent-guidance/<내용 SHA-256>.json`에 저장하며 작업·session 관계는 SQLite에 결합된다. 역할별 실행용 Markdown은 원본에서 재생성할 수 있지만 누락·손상된 원본을 최신 지침으로 바꿔 재개하지 않는다. 과거 단일 Markdown snapshot도 원래 내용으로 유지한다. [snapshot 계약](ARCHITECTURE.md#ai-agent-실행)

SQLite가 없거나 손상되면 일반 `run`은 자동 초기화하지 않는다. 백업을 복원하거나 손상 DB를 별도 보관한 뒤 명시적으로 `config init`·재등록한다. 자동 상태 재구성은 지원하지 않는다.

Merge 확인 후 clean하고 PR head에 없는 로컬 commit이 없는 worktree만 Merge·Squash·Rebase 방식과 관계없이 정리한다. 불확실한 작업 공간은 `cleanup_pending`으로 보존한다. 다음 작업·프로젝트 완료를 막지 않으며 안전 조건 충족·경로 부재 시 자동 해제된다.

## Verify 배정과 진단

Worker는 관련 targeted unit test와 verify 스크립트를 준비하고 전체 verify는 Manager가 Agent 밖에서 실행한다. Windows는 `scripts/verify.ps1`, Linux·macOS는 `scripts/verify.sh`를 사용한다. Loop Manager 자체 스크립트는 `.venv`를 우선 사용하고 어디서 호출해도 저장소 루트에서 실행한다. 공통 runner가 전체 unittest·compile을 수행하며 테스트 프로세스의 실제 Agent CLI·외부 API 연결은 차단된다.

`verify_parallelism = auto`는 허용 CPU·실제 부하·메모리 여유와 아직 관측에 반영되지 않은 예약을 확인해 새 테스트 모듈을 배정한다. 정수 상한 안에서도 자원이 부족하면 기다리며 관측 실패 시 직렬로 축소한다. macOS도 CPU·메모리 관측이 불가능하면 직렬로 축소한다. `max_workers`와 별개이며 다른 등록 프로젝트의 verify에는 적용하지 않는다.

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
