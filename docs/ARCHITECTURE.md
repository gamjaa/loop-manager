# Architecture

## Overview

Loop Manager는 Windows·Linux·macOS에서 동작하는 Python 3 프로그램이다. 외부 스케줄러가 프로그램을 반복 실행하며, 프로젝트 문서와 GitHub 상태를 확인하고 AI Agent CLI를 호출해 계획, 구현, 검증과 PR 생성을 진행한다.

하나의 프로젝트에는 여러 READY ExecPlan을 등록할 수 있지만 한 번에 하나만 처리한다. 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다.

## 문서 경계와 책임

| 기준 | 담당 내용 |
|---|---|
| [README.md](../README.md)·[운영 안내](OPERATIONS.md) | 첫 설치·실행과 상세 설정·수동 제출·장애 대응 |
| [PRODUCT.md](PRODUCT.md)와 활성 Requirement | 현재 제품 동작·장기 규칙과 요청된 변화 |
| 이 문서 | 책임 경계·정보 원본·저장·실행 격리·검증·복구 방식 |
| [PLANS.md](PLANS.md) | 공통 ExecPlan 형식·메타데이터 |
| [Specification Skill](../.agents/skills/specification-docs/SKILL.md) | 제품 문서 작성·검토 기준 |

상태의 의미와 허용 정책은 PRODUCT를 기준으로 하며 여기서는 이를 적용·보존하는 방법을 설명한다. 활성 Requirement의 미구현 설계는 구현 예정임을 구분한다.

현행 구조와 활성 변경 요청은 [PRODUCT의 변경 요구사항 절](PRODUCT.md#활성-변경-요구사항과-현재-동작)에서 연결한다. 아래는 Merge된 구조이며 R-055의 정상 Git 변경·legacy 기본 checkout 제외·BLOCKED 현재 입력·게시 분리와 R-082의 Planner 경계 보완은 구현 목표다. 문서 편집만으로 실행 gate나 검증 성공을 바꾸지 않는다.

[보관된 R-063](requirements/archive/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md)은 규칙 기반 처리의 Manager 이관과 모델 호출 축소의 설계 배경이다. 후보 성공 결과 재사용의 변경 요구는 [R-072](requirements/%5BR-072%5D%20Reuse%20Successful%20Candidate%20Results%20with%20Proven%20Inputs.md)를 따른다. 역할별 입력 동일성의 증거가 없는 경우의 재검증·최신 원격 게시 gate는 유지한다.

## 실행 환경과 시작

- Codex 또는 Claude Code CLI 중 최소 하나가 설치되고 인증돼있어야 한다
- Git과 gh(GitHub CLI)가 설치되고 대상 저장소에 접근할 수 있어야 한다
- Python 3 venv를 만들 수 있어야 한다
- Windows는 PowerShell, Linux·macOS는 Bash 실행 진입점을 제공한다
- 같은 사용자 상태 디렉터리의 자동 실행과 실제 복구 실행은 `<state-dir>/loop-manager.lock`으로 직렬화한다. 수동 준비와 복구 요청 접수는 별도 command runtime과 자원 잠금을 사용한다
- `run.sh`와 `run.ps1`은 Git을 실행하지 않고 source checkout의 Python 부트스트랩만 시작한다
- 부트스트랩은 다른 Git 작업 전에 부모 process lock을 획득하고, 등록 self base(미등록이면 main)의 origin/<base-branch>를 fetch해 exact SHA의 detached runtime worktree를 선택한 뒤 같은 lock을 유지한 채 runtime 자식을 실행한다. `manual`과 `recover`는 command별 부모 lock과 고유 runtime 경로로 자동 실행과 분리한다
- runtime worktree는 source checkout의 object database를 공유하며 사용자 `main`의 파일, index, HEAD와 로컬 전용 commit을 사용하지 않는다. push된 새 SHA는 다음 실행에서만 선택한다
- source checkout의 bootstrap은 기존 `config/config.ini` 또는 명시된 `--config`를 source checkout 기준의 절대 경로로 확정해 runtime 자식에 전달하며 설정 파일을 runtime worktree에 복사·이동·symlink하지 않는다
- launcher가 선택한 source checkout의 Python interpreter를 runtime 자식도 사용한다. source checkout의 ignored 파일과 cache는 runtime worktree로 전달하지 않는다
- `config/config.ini`가 없거나 유효하지 않으면 설정 방법을 안내하고 작업을 시작하지 않는다
- 최초 설정은 사용자가 설정 모드를 명시적으로 실행해 SQLite를 생성한다
- 일반 Loop 모드에서 SQLite가 없거나 손상됐으면 자동으로 초기화하지 않고 복원 또는 명시적 재설정을 안내한 뒤 중단한다

프로그램은 한 번 실행할 때 처리 가능한 작업을 진행한 뒤 종료한다. cron이나 Windows 작업 스케줄러가 다음 실행을 시작한다.

## 설정

Python 표준 라이브러리 `configparser`로 `config/config.ini`를 읽는다.

- `[loop].max_workers`: 동시에 처리할 프로젝트 수, 기본값 3인 양의 정수
- provider별 Model alias와 Effort, Fast on/off 또는 CLI 기본 설정 상속
- 일반 Planner(`planner`)·빠른 점검 Planner(`planner_check`), Reviewer profile
- 선택 provider의 Worker `level1`부터 `level8`까지의 명시적 Model alias와 Effort
- Telegram Bot token과 chat ID

Fast 선택은 Manager 실행 시작의 설정 snapshot에 보관하고 병렬 프로젝트 worker와 수동·복구 실행의 호출 문맥에 결합한다. 명시적 on/off와 CLI 기본 설정 상속을 구분하며 provider section·항목 생략 또는 공란은 상속이다. 설정값 검증은 선택 provider에 한정해 미선택 provider의 잘못된 값이 독립 프로젝트를 막지 않게 한다. 같은 실행의 신규·재개·재시도는 snapshot을 사용하고 다음 실행은 설정을 다시 읽는다. 호출이 끝나면 해당 실행의 문맥을 해제한다.

provider adapter는 기존 모델·Effort·지침·권한·sandbox를 보존하면서 명시적 Fast 선택만 호출 설정에 합친다. 명시적 off는 CLI 기본값의 Fast 선택보다 우선하도록 요청하고 상속은 Fast 설정을 덮어쓰지 않는다. Manager가 지원 모델 목록·계정 조회·추가 실행 gate를 관리하지 않으며 실제 적용·일반 속도 대체·CLI 오류는 provider의 결과로 처리한다. 호출 로그에는 요청한 on/off 또는 상속과 CLI 진단을 남기되 실제 적용 성공을 합성하지 않는다. Fast 선택만으로 session·작업물·검증·Worker 승격 이력을 바꾸지 않고 CLI 버그에 대한 별도 우회 실행을 추가하지 않는다.

실제 `config/config.ini`는 Git에서 제외하고 현재 사용자만 읽을 수 있도록 권장한다. 기본 설정 파일은 source checkout에 그대로 두며 bootstrap이 해당 경로를 runtime 자식에 명시적으로 전달한다. 사용자가 `--config`를 지정하면 bootstrap은 상대 경로를 source checkout 기준으로 절대화한 뒤 전달한다. runtime은 전달된 파일을 시작 시 한 번 읽고 runtime `ROOT`를 기준으로 기본 경로를 다시 계산하지 않는다. 설정 파일을 SHA별 runtime에 복제하지 않으며 Telegram token과 chat ID가 모두 비어 있으면 알림을 비활성화하고, 하나만 설정돼 있으면 설정 오류로 처리한다. 비밀값은 자식 명령행, 로그, AI Agent prompt와 오류 메시지에 포함하지 않는다.

ignored 상태는 runtime 전달 근거가 아니다. `.venv`는 launcher가 선택한 interpreter의 절대 경로를 자식 프로세스에도 사용해 재사용하고, source checkout의 `__pycache__`와 도구 cache 같은 파생 파일은 전달하지 않는다. runtime 자식의 표준 Python bytecode cache는 disposable worktree 바깥의 Manager 상태 경로에 repository·exact revision·interpreter별로 분리한다. 추적된 runtime Python 원본에서 표준 checked-hash `.pyc`를 준비해 같은 revision의 재생성 worktree에서도 원본 수정 시각에 의존하지 않고 재사용한다. 기존 hash·interpreter tag·revision이 맞지 않거나 cache 경로를 쓸 수 없으면 안전하게 다시 만들고, 실패해도 원본 실행을 계속한다. 사용자가 명시한 `-B`, `PYTHONDONTWRITEBYTECODE`와 `PYTHONPYCACHEPREFIX` 선택은 유지한다. cache 준비는 생성·재사용·대체·사용 불가 개수와 소요 시간을 기록한다. 이 cache는 runtime revision·source·검증·Git 소유권의 근거가 아니며 ignored 파일 판정이나 cleanup 검사를 완화하지 않는다. 다른 ignored 파일이 필수 운영 입력이 되면 파일별 외부 경로, 소유권, 권한과 전달 방식을 Architecture에 먼저 정의하며 일반 복사나 runtime clean 검증 예외를 추가하지 않는다.

런타임 상태 디렉터리는 source checkout 밖의 운영체제 사용자 상태 경로를 사용한다.

- Windows: `%LOCALAPPDATA%\loop-manager\`
- Linux·macOS: `${XDG_STATE_HOME:-~/.local/state}/loop-manager/`

SQLite, manager runtime worktree, 프로젝트 관리 worktree, 로그, 임시 파일과 프로세스 lock은 모두 이 경로 아래에 둔다.

SQLite 손실이나 손상에서의 자동 상태 재구성은 지원하지 않는다. 사용자는 상태 디렉터리를 백업하고, 문제 발생 시 백업을 복원하거나 명시적 재설정 후 프로젝트를 다시 등록한다.

## 프로젝트 등록

- 대상 경로가 Git 프로젝트이고 origin이 GitHub 저장소인지 확인한다
- 기준 branch를 확인해 프로젝트 설정에 저장한다
- `docs/PLANS.md`, `docs/ARCHITECTURE.md` 존재 여부를 확인한다
- `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- `docs/PLANS.md`의 SHA-256 해시가 Loop Manager 기준 파일과 다르면 등록을 거부한다
- 변경되지 않은 PRODUCT 또는 ARCHITECTURE 템플릿만 있으면 작업 가능 상태로 등록하지 않는다
- `docs/PLANS.md` 검사는 등록할 때만 수행하며 자동 Loop에서는 다시 검사하지 않는다
- 등록 해제의 비종료 계획·프로젝트 차단·planning 오류 gate는 PRODUCT를 따른다. 해제는 registry만 변경한다
- 등록 해제는 프로젝트 파일, branch, worktree와 PR을 삭제하지 않는다

등록 조건과 runtime 실행 지침 원본 검사는 AI가 아니라 프로그램 코드가 수행한다.

## 문서 입력과 ExecPlan 파싱

프로젝트 문서·계획의 자동 입력은 fetch로 확인한 `origin/<base-branch>`의 특정 commit에서 읽는다. 등록 프로젝트의 기본 checkout에만 있는 파일·수정·commit은 사용자가 push하기 전에는 입력에 포함하지 않는다. 진행 작업은 이 원격 기준에서 만든 관리 worktree의 결과를 보존하며, 선택된 원격 작업 branch의 사용자 수정은 검증 후 반영한다.

ExecPlan의 `Progress` 체크 항목과 본문은 구현 내용·판단·검증 기록이다. 체크가 끝났다는 이유로 `READY`, `PLAN_BLOCKED`, `MERGED` 같은 실행 상태를 바꾸지 않으며, 단계·재시도·session은 SQLite, 실제 PR Merge는 GitHub 확인을 기준으로 한다. 본문 변경은 재평가 입력이고 명시적 `User Decision`은 승인 판정 입력이므로, 본문을 읽지 않거나 변경을 무시한다는 뜻은 아니다.

현재 활성 Requirement 경로는 Planner 입력과 판단·이동의 경로 key이며 `docs/requirements/` 바로 아래 문서만 새 계획 입력이다. 기존 ExecPlan은 `sources` 문자열을 보존한다. Manager는 원격 revision과 worktree에서 같은 Requirement identity helper를 사용해 기록된 활성 경로, 기존 작업에 허용된 같은 파일명의 archive, source 파일명에서 확인한 R ID의 유일한 문서를 순서대로 조회한다. 정확한 경로가 있으면 중복 ID여도 해당 문서를 유지한다. ID 후보가 없거나 여러 개이거나 후보를 읽고 식별할 수 없으면 누락·모호·조회 실패를 구분해 source 관계를 확정하지 않는다. 새 ExecPlan은 정확한 활성 source 경로만 사용할 수 있다. archive는 기존 ExecPlan source의 조회·검증 호환성에만 사용하며 재평가하거나 활성 경로로 자동 복원하지 않는다. FUTURE_IDEAS는 계획 입력·실행 상태가 아니다.

Specification Skill과 함께 배포하는 `.agents/skills/specification-docs/scripts/next_requirement_id.py`는 독립 Python 표준 라이브러리 도구다. 프로젝트 루트의 활성 Requirement 디렉터리와 선택적 archive 디렉터리에서 직접 포함된 `*.md` 일반 파일을 읽으며 Git 추적 여부를 검사하지 않는다. 활성 디렉터리 누락, 심볼릭 링크·일반 파일이 아닌 문서, 파일 접근·UTF-8·식별 오류는 ID를 출력하지 않고 실패하며 archive 디렉터리 부재는 빈 집합이다. 제한된 frontmatter에서 `id`·`title`을 읽고, frontmatter가 없는 문서는 첫 `# [R-ID] 제목`으로 식별한다. 파일명은 원본 판단에 쓰지 않는다. `copy-project-files`는 catalog의 공유 `frontmatter.py`를 Skill 스크립트 옆에 배포하므로 복제된 명령 실행은 Manager 설치에 의존하지 않는다. Manager ExecPlan parser와 조회 명령은 이 단일 파서 원본을 사용한다. 조회는 읽기 전용이며 Git, DB, 네트워크와 Agent 실행에 의존하지 않는다. import 가능한 `requirement_identity` 함수는 후속 Manager 번호 검사에서도 같은 식별 규칙을 재사용할 수 있게 한다.

source 해석은 catalog 검증·문서 읽기·입력 지문·계획 관련성·단계별 문서 변경·Requirement 판단 및 이동에서 같은 resolver를 사용한다. 지문은 기존 source 문자열을 key로 두고 resolver가 찾은 현재 문서 내용을 포함한다. 따라서 경로 변경만으로 기존 작업 관계가 사라지지 않으며, 내용 변경은 일반 입력 변경·재사용 조건에 반영된다.

Requirement 최종 판단은 source로 연결된 비종료 계획·draft와 수동 제출이 없는 시점에 일반 Planner가 담당한다. 관련 계획이 있으면 모두 실제 MERGED여야 하며 연결 계획 없는 직접 구현도 코드·검증·관련 제품·설계 문서로 판단한다. Manager는 유효한 기준별 충족 판단과 입력 지문·source 관계를 확인한 원문만 내용 보존 이동으로 게시한다. 미충족이면 후속 ExecPlan을 연결하며, 원격 확인 전 입력 변경에 따른 이동 철회와 원격 확인 후 archive를 구분한다.

과거 Requirement 감사 row와 저장된 복원 manifest 필드는 읽기 호환성과 이력 보존을 위해 유지한다. 새 감사·복원 판단과 완료 대기를 만들지 않으며 복원 manifest는 새 게시 전 거부한다. 미게시 Manager 복원 영수증은 해당 worktree에서 원문·원격 archive·경로 소유 범위를 확인해 archive로 철회하고 활성 입력을 재평가한다. index reset 중 중단도 같은 영수증으로 재개하고 사용자 수정·충돌·게시된 후보는 덮어쓰지 않는다.

ExecPlan은 [PLANS.md](PLANS.md)의 고정 key/value와 `sources`·`depends_on` 목록만 파싱하며 전체 YAML을 지원하지 않는다. 필수 메타데이터, source, ID 고유성 및 dependency graph를 검증한다. 새 계획에는 priority가 필수이고 도입 전 계획의 생략 값은 normal로 해석한다. priority 형식 변경은 공통 PLANS와 파서를 같은 변경에서 갱신한다.

## Source of Truth

- PRODUCT, Requirement, ARCHITECTURE와 ExecPlan의 게시된 내용: 검증한 원격 commit의 Git 파일. 진행 중인 미게시 변경은 해당 관리 worktree에 보존한다.
- ExecPlan 실행 상태, 의존성 충족 여부, 재시도 횟수, 세션, worktree, branch와 PR 연결: SQLite
- PR Open/Closed/Merged 상태: GitHub
- Model, Effort, 동시 실행 수와 Telegram 설정: `config/config.ini`

SQLite는 `<state-dir>/loop-manager.db`에 저장하며 상태 변경은 transaction으로 기록한다. 문서 본문이나 ExecPlan 본문은 SQLite에 복제하지 않고 경로와 내용 해시만 저장한다.

SQLite에는 등록 프로젝트, 기준 branch, 선택한 provider, 과거 등록·승인 감사, 마지막 성공 Planner의 명세·전체 계획 입력 지문과 `planning_error`, 복구 가능한 planning publication intent, ExecPlan 메타데이터와 의존성, 최초 실행 여부, 실행 상태와 재시도 횟수, AI Agent 세션, worktree, branch, PR, 삭제된 미착수 ExecPlan의 ID 예약·감사 기록, `cleanup_pending`, 알림과 프로젝트 완료 주기를 저장한다.

실행 시작 시 SQLite, Git worktree/branch와 GitHub PR 상태를 대조한다. 서로 다르면 GitHub Merge 상태, 프로젝트 파일, SQLite 실행 정보 순으로 각 소유 영역의 원본을 적용한다.

### 원격 catalog와 실행 이력의 분리

최신으로 검증한 `origin/<base-branch>`의 active 및 archived ExecPlan 파일은 ID를 제외한 현재 계획 선언의 원본이다. SQLite는 최초 게시 path·hash·SHA를 불변 감사 정보로 보존하고 현재 path·title·sources·depends_on·complexity·risk·priority·hash·revision을 별도 cache로 동기화한다

유효한 ExecPlan 선언 변경은 계획 또는 프로젝트 차단 사유가 아니며, catalog 전체가 유효하고 원격 revision과 SQLite 상태·소유권 snapshot이 그대로일 때만 모든 현재 cache delta를 한 transaction으로 반영한다

ExecPlan 번호는 연속일 필요가 없다. 번호 공백을 누락 계획으로 추정하거나 메우지 않으며 사용한 ID는 재사용하지 않는다. 현행 catalog 동기화는 SQLite 실행·취소 이력에 대응하는 원격 파일이 없거나 형식·source·ID·dependency graph가 유효하지 않으면, 다른 계획 상태를 바꾸지 않고 원인 파일을 포함한 프로젝트 범위 진단을 남긴다.

[R-055 구현 예정](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md#계획작업-문서-삭제의-자동-반영): Requirement·ExecPlan 삭제는 Manager의 원격 관측·이력 분류와 일반 Planner의 목표 판단으로 자동 처리한다. 허용된 관련 문서 개정·삭제의 원격 확인 후 현재 DB 관계와 실행 수명에 반영하고, 유지하는 관리 브랜치의 문서를 갱신하며 삭제를 반영한 취소·완료 작업의 소유 로컬·원격 브랜치와 worktree를 보존 검증 뒤 정리한다. 완료·취소·예약 감사는 활성 선언 및 source 조회와 분리해 보존한다. 게시·DB·자원 정리의 확정 지점별로 재개하며, 해결하지 못한 영향 범위와 정리 pending만 보존·보류하고 검증된 독립 계획은 계속한다.

일반·유지보수 planning의 미해결 오류와 입력 지문은 다른 ExecPlan의 차단 해제·PR 상태 변경·project 실행 상태 정리와 독립적으로 보존한다. worktree 경로 부재나 관련 입력이 같은 원격 base 전진만으로 해제하지 않으며, 같은 입력의 반복 실행·재시작에서는 Planner를 다시 호출하지 않는다

관련 planning 입력이 바뀌면 저장된 소유권·workspace identity를 확인하고 관리 worktree에 최신 원격 base를 반영한다. 오류는 입력 전진만으로 해제하지 않고 보존된 결과의 교정·재검증 또는 게시 복구로 원인이 실제 해소된 뒤에만 해제한다. 소유 증거 없는 경로와 복구 실패는 Git 등록·HEAD·index·파일 및 진단·session·작업물을 보존한다

ExecPlan ID만 Git 선언과 SQLite 실행 이력을 잇는 안정적인 identity다. 같은 ID의 유효한 원격 파일에서 path·title·sources·depends_on·complexity·risk·priority 또는 본문이 바뀌면 현재 cache를 갱신하고 상태·실패 이력·Agent session·branch·worktree·workspace·PR 연결은 보존한다

현재 path·title은 compact catalog·로그·조회에 사용한다. 기존 PR의 ExecPlan 링크는 최초 게시 SHA·active path를 유지한다. cache 갱신은 실행 상태·session·branch·worktree·workspace·PR과 최초 게시 감사를 교체하지 않는다.

## 계획 변경 감지

프로젝트별 명세 지문은 최신 `origin/<base-branch>`의 다음 사용자 소유 제품 문서에서 계산한다.

- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`

프로젝트별 전체 계획 입력 지문은 같은 원격 revision의 다음 내용에서 계산한다.

- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`
- archive, `docs/plans/`와 `docs/FUTURE_IDEAS.md`를 제외한 소스, 테스트와 설정 파일

성공한 일반 Planner 주기는 새 계획 게시, PR 전 계획 개정·미착수 계획 삭제 게시, FUTURE_IDEAS-only 게시와 추가 계획 없음 판단을 포함하며, 완료할 때 관측한 명세 지문과 전체 계획 입력 지문을 함께 저장한다. 현재 작업의 단계별 문서 반영은 전체 미충족 작업의 일반 planning 성공으로 기록하지 않는다. 자동 Requirement archive의 내용 보존 이동만으로는 두 기준을 무효화하거나 새 작업 주기와 프로젝트 완료 알림을 활성화하지 않는다.

활성 변경 단계와 maintenance publication이 없는 안전한 선택 경계에서 명세 또는 전체 계획 입력 지문이 마지막 성공 기준과 다르면, PRODUCT의 유지보수·역할 선택 순서를 적용해 READY보다 먼저 필요한 Planner를 한 번 실행한다. 입력이 같으면 활성 유지보수 prerequisite·리팩터링 DAG의 선택을 먼저 적용하고 일반 READY는 priority와 숫자 ID 순으로 시작한다. 성공한 두 지문이 최신 입력과 같으면 빈 catalog 또는 dependency 대기 상태에서도 Planner를 반복 실행하지 않는다.

due 유지보수의 선택·진행 작업의 비선점 정책은 PRODUCT의 [작업 선택](PRODUCT.md#작업-선택과-동시-실행)·[유지보수 평가](PRODUCT.md#유지보수-평가)를 따른다. [보관된 R-054](requirements/archive/%5BR-054%5D%20Due%20Refactoring%20Priority.md)는 설계 배경으로만 참조한다.

## Git과 worktree 실행 모델

자동 입력과 시작·rebase 기준은 fetch된 `origin/<base-branch>` SHA다. 로컬 base가 미게시 commit으로 앞서 있어도 자동 작업은 원격 SHA에서 시작하며 로컬 commit을 자동 push하거나 입력으로 채택하지 않는다. 기본 worktree는 사용자 checkout으로 보존한다.

작업·동기화·복구가 관여하는 branch는 등록한 base와 소유권을 확인한 Loop Manager 생성 branch다. 그 밖의 사용자 branch는 생성·수정·삭제를 되돌리거나 자동 복구 대상으로 삼지 않으며, 그 변화만으로 계획·프로젝트를 차단하지 않는다. 이름이 비슷하다는 이유로 관리 branch로 간주하지 않는다. 실제 사용 중인 origin·관리 branch·worktree 등 공유 자원의 안전 검사는 유지한다.

### 기본 worktree 동기화

| 상태 | 처리 |
|---|---|
| 저장 base branch·Git operation 없음·HEAD가 원격과 equal | dirty 상태도 변경하지 않고 no-op |
| behind-only이며 Git operation·비정상 index·경로 겹침 없음 | 로컬 변경과 index를 보존하는 safe fast-forward |
| ahead-only이며 로컬에 미게시 commit이 있음 | 로컬 commit·checkout을 보존하고 동기화만 건너뜀. 자동 작업은 원격 SHA에서 계속 |
| clean diverged·선형 로컬 commit·durable 소유 intent·경로 충돌 없음 | 원격 base 위로 로컬 commit을 보존하는 rebase. 자동 push 없음 |
| 다른 branch·detached·dirty 또는 비선형 diverged·operation·비정상 index·겹침·동시 권위 변경 | 이유를 기록하고 동기화만 건너뜀 |

경로 겹침은 원격이 변경할 tracked 경로와 staged·unstaged·untracked·ignored 로컬 경로를 대소문자 및 부모·자식 관계까지 비교한다. eligibility 뒤 사용자 변경·Git lock·실패가 있으면 reset·clean·stash·강제 checkout이나 잠금 제거로 우회하지 않는다. safe fast-forward는 이미 fetch한 원격 ref에 대한 `git merge --ff-only` 상당의 적용이며 중복 fetch를 하지 않는다.

source snapshot은 경로 충돌·파일 보존에 필요한 ignored 경로·서명과 Git config·외부 exclude 근거를 수집한다. 실행 직전·정상 완료·재시작 복구의 비교 view는 Agent 복구의 시작 ignore 규칙 검증을 재사용해 시작·저장·현재 index에 없는 경로만 제외한다. tracked ignore 규칙은 blob·index·작업 상태가, 외부 exclude는 저장된 경로·서명이 일치해야 한다. Git 쓰기 직전에 현재 경로를 다시 검사하고 fast-forward는 Git의 `--no-overwrite-ignore`도 적용한다. ignored-only 차이는 같은 실패 입력의 재시도를 허용하지 않으며 rebase abort의 실제 tracked 파일·index·operation 증명도 완화하지 않는다.

source journal의 원본 intent·invocation·관측과 완료 감사는 비교 view로 덮어쓰지 않는다. 실제 전이와 소유 표식이 검증되면 결과를 먼저 기록한 뒤 활성 기록을 종료하며, 결과 기록 후 중단된 경우에도 기존 결과를 보존한다. config 근거가 없는 과거 기록은 시작에 관측된 ignored 경로의 unchanged tracked 규칙을 재구성할 수 있을 때만 차이를 제외한다. 새 경로나 외부 exclude의 시작 근거가 부족하면 기록·파일을 보존하고 거부한다. 안전하게 완료한 뒤 원격 base가 더 전진했으면 현재 원격의 동기화 판단을 이어간다. source 기록은 Agent의 ref·worktree 기준선 후보로 제공하지 않으며 별도 관리 작업의 실행·게시 차단 근거로 사용하지 않는다.

동기화 수행·skip·실패와 관계없이 자동 작업은 원격 SHA에서 계속하고 PR 관측·알림도 유지한다. 로컬 전용 문서·commit은 사용자가 push한 뒤 입력이 된다. 기본 worktree의 사용자 변경은 별도 관리 worktree와 소유 branch가 유지되는 한 Agent 위반으로 귀속하지 않는다.

### 자체 runtime 선택과 handoff

자동 명령의 실행 revision은 다음 순서로 고정한다. `manual`과 `recover`의 병행 command runtime도 같은 원격 SHA·소유권 검증을 적용하며, 전역 잠금 대신 고유 부모 잠금과 `commands/<mutation-id>/<sha>/` 경로를 사용한다.

1. source checkout의 좁은 Python 부트스트랩이 `<state-dir>/loop-manager.lock`을 획득한다
2. lock을 보유한 부모만 source checkout을 anchor로 `git fetch origin`을 실행한다
3. 등록 self base(미등록이면 기존 main 기본값)의 origin/<base-branch>를 immutable commit SHA로 resolve한다
4. `<state-dir>/runtimes/<repository-id>/<sha>/`의 linked worktree가 같은 repository, clean detached HEAD와 exact SHA인지 검증하고, 없을 때만 `git worktree add --detach`로 만든다
5. 부모가 source checkout 기준의 절대 config 경로와 내부 runtime 표식을 전달하고, 현재 Python interpreter로 runtime worktree의 `loop_manager.py`를 자식 실행해 종료 코드를 전달한다
6. 부모는 자식 종료 뒤 lock을 해제한다. source bootstrap의 부수 동기화와 runtime 자식의 일반 프로젝트 동기화는 원격 revision 선택과 별도 경계이며 위 기본 worktree 동기화 조건에서만 수행한다. source HEAD·branch·동기화 성공은 실행 gate가 아니다

runtime 경로가 존재하지만 소유권, repository, detached HEAD, SHA 또는 clean 상태를 증명할 수 없으면 reset, clean, 제거 또는 덮어쓰기 없이 중단한다. 별도 clone은 만들지 않으며 SHA별 runtime 정리는 활성 실행과 분리된 검증된 관리 작업으로만 수행한다.

runtime 자식은 병렬 프로젝트 작업을 제출하기 전에 등록된 self source checkout의 대기 PR을 동기화한다. PR의 실제 base branch가 등록 base와 같음을 확인한 새 self PR Merge만 handoff 근거로 삼는다. Merge 전이·cleanup·알림 outbox를 영속화하고 등록 base의 최신 fetch와 source checkout 안전 동기화를 평가한다. 원격 SHA가 현재 runtime SHA와 같으면 계속 실행하고 fast-forward 후속이면 source 동기화 결과와 관계없이 정상 종료해 다음 외부 실행의 bootstrap에 제어를 넘긴다. handoff 판정 뒤에는 새 Agent·verify·publication 또는 다른 프로젝트 작업을 시작하지 않는다. self Merge가 없거나 다른 프로젝트의 Merge 또는 일반 push만 있으면 handoff하지 않는다. repository·origin·PR base identity, fetch 또는 ancestry를 확인할 수 없으면 성공 handoff로 처리하지 않고 기존 작업 상태를 보존한다

### 선택 전 상태 동기화

1. 프로그램이 `PROJECT_BLOCKED`의 저장소 안전을 확인하고 저장된 모든 Merge 대기 PR의 GitHub identity와 Open·Merged·Closed 상태를 조회한다. 안전 gate가 허용하는 Merge·Close 전이와 worktree cleanup 판정 및 알림 outbox를 기본 worktree 동기화보다 먼저 SQLite에 기록한다. 프로젝트 안전이 증명되지 않으면 기본 worktree 동기화와 일반 저장소 변경 단계를 실행하지 않는다. 계획과 연결된 차단의 검증 가능한 대상 작업 복구·보존은 아래의 계획별 실행 자리와 현재 상태 검증 경계에서만 수행한다. 단, 원격 ExecPlan catalog 오류 차단은 원인 push를 fetch하고 재검증해야 하므로 이 저장소 안전 gate와 별도로 처리한다
2. 프로젝트 안전 gate가 열려 있으면 새 Merge 뒤 `origin/<base-branch>`를 다시 fetch하고 최신 ExecPlan·project 상태를 읽는다. 가능한 ExecPlan archive 게시를 먼저 완료한 뒤 최종 원격 SHA를 기준으로 기본 worktree의 safe fast-forward eligibility와 catalog를 평가한다. 저장된 `PR_OPEN` mirror, 다른 안정된 Open PR 또는 충돌 marker만으로 평가를 생략하지 않는다. 그 뒤 각 `PLAN_BLOCKED`의 계획별 해제 조건 및 `planning_error`의 근거 입력 변경 여부를 독립적으로 확인한다

기존 Open PR도 [GitHub와 PR](#github와-pr)의 Merge 가능 여부를 확인한다. 충돌이 확정된 PR은 안전·dependency·실행 자리 조건을 확인한 뒤 관리 branch를 최신 원격 base에 rebase해 같은 PR을 갱신한다.

그 뒤 실행 중인 변경을 재개하거나 확정 PR 충돌 복구·유지보수·일반 Planner·READY를 선택한다. dependency와 순서는 [PRODUCT의 작업 선택 정책](PRODUCT.md#작업-선택과-동시-실행)을 따른다. Worker·검증·Review·PR 생성 순서는 [제품 처리 흐름](PRODUCT.md#execplan-생성과-자동-처리)을 따른다.

### 계획 게시와 작업 공간

계획을 실행 이력에 적용하기 전에 원격 게시와 catalog 전체의 유효성을 확인한다. 계획·FUTURE_IDEAS 변경이 없으면 관측한 두 입력 지문을 저장하고 planning worktree를 정리해 완료 여부를 확인한다.

1. 생성·개정·rename·삭제·FUTURE_IDEAS 변경을 분류하고 snapshot 원본, SQLite의 현재 실행 근거·PR 부재 및 적용 후 catalog의 ID·source·dependency graph를 검증한다. 시작한 계획의 ID·경로와 실행 이력은 보존한다.
   snapshot의 rename으로 확정되지 않은 미게시 새 파일은 전체 catalog를 읽기 전에 별도로 정규화한다. Manager는 active·archive 파일과 SQLite 등록·취소·수동 예약 및 mutable/held snapshot에서 사용 중인 ID를 모아 새 batch에 연속 번호를 배정하고 ID·title에 맞는 새 경로로 옮긴다. 같은 batch의 서로 다른 새 파일이 같은 ID를 쓰면 배정을 멈춘다. 새 계획과 이번 batch에서 수정한 snapshot 계획의 `depends_on` 중 대상이 확인된 batch ID만 새 ID로 갱신하며, 기존 dependency 순서·계획 identity·본문은 보존한다. 기존 ID와 batch ID가 충돌하는 관계는 의미를 정할 수 없어 오류로 남긴다. 재배정 대상 ID가 본문에 언급돼도 이를 고치거나 대상을 추론하지 않고 오류로 남긴다. draft와 `User Decision`의 원문은 보존한다. 정규화 뒤 기존 전체 catalog·source·dependency 검증을 수행한다.
   정규화는 일반·유지보수 planning 모두에 적용한다. 원본 bytes·mode와 대상 hash를 편집 전에 복구 journal에 기록하고, 완성한 임시 파일을 원자적으로 설치한다. 다음 검증 진입에서 미완료 journal을 확인해 원본을 복구한다. 복구로 제거된 journal 소유 임시·대상 경로는 호출자가 미리 수집한 변경 목록에서 제외하고, 복구 후 현재 변경을 다시 관측해 범위를 검증한다. 예상과 다른 파일이 있으면 덮어쓰지 않고 작업과 journal을 보존한다.
2. 실패는 같은 Planner 세션에서 최대 3회 교정한다. 소진하면 원인·입력 지문을 planning_error에 보존하고 같은 실패 입력의 신규 Planner 호출을 반복하지 않는다. 현행 실행 경로는 이 오류에서 프로젝트 처리를 중단한다. [R-055](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md)는 안전하게 실행 자리를 비운 뒤 이미 게시된 독립 계획의 순차 구현·PR 관측을 계속하도록 변경한다.
3. 검증된 전체 변경을 planning worktree의 한 commit으로 만든다.
4. 아직 게시하지 않은 동안 원격 base가 전진하면 변경·session을 보존해 반영하고 최신 catalog·대상 권한과 관련 문서를 재검증한다. 충돌·원격 재작성·소유권 불명확은 작업을 보존해 복구 경계로 보낸다.
5. 기준과 mutable READY snapshot이 그대로면 복구 가능한 publication intent를 저장하고 base에 fast-forward push한다.
6. 원격 commit을 확인한 뒤 생성 row 등록·개정 cache 갱신·삭제 row 제거와 ID 취소 감사·두 성공 지문·planning 정리를 한 SQLite transaction으로 반영한다. FUTURE_IDEAS-only 변경은 실행 상태를 만들지 않는다.

정규화 결과는 기존 planning result와 분리해 원본 경로·ID, 배정 경로·ID로 일반 planning manifest 또는 유지보수 publication intent에 보존한다. manifest의 기준 revision, 실제 commit 경로와 파일 hash를 대조하고, 원격 확인 및 catalog 등록은 기존 publication intent·lease 경계를 사용한다. 재개는 저장된 manifest를 그대로 사용하며 revision·diff·소유권이 다르면 게시하지 않는다.

planning worktree는 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`, 구현 worktree는 `<state-dir>/worktrees/<project-id>/<execplan-id>/`에 둔다. 구현 branch 이름은 `loop-manager/<execplan-id>`다. 계획은 Worker 시작 전에 base에 게시하며 구현 PR에서 처음 생성하지 않는다.

일반 planning snapshot은 실제 PR이 없는 READY·진행 중 선언을 Git 원문과 SQLite의 선언·실행 근거에 결합한다. 실행 이력·branch·worktree·세션·checkpoint는 개정 금지 사유가 아니며, 게시 직전에 실행 근거와 실제 PR 부재를 다시 확인한다. 개정은 실행 상태·세션·실패 이력을 보존하고 이미 시작한 계획의 effective level을 낮추지 않는다. 이름 변경·삭제와 유지보수 snapshot은 기존 미착수·무소유 READY 범위를 유지한다. 계획 삭제는 활성 row 제거와 ID 취소 감사를 같은 transaction에서 기록하며 ID를 재사용하지 않는다. 최초 activation marker는 이후 READY로 돌아와도 유지한다.

사용자가 BLOCKED의 근거 PRODUCT/Requirement를 기본 branch에서 수정한 경우 변경을 commit하고 push해야 한다. Loop Manager는 변경된 `origin/<base-branch>`를 관리 branch에 rebase한 뒤 재개한다. 작업 rebase는 관리 worktree에서 수행하며, 기본 worktree의 파일·index·HEAD는 위 부수 동기화의 안전한 FF·clean 로컬 commit rebase 조건에서만 바꾼다. 사용자는 안내된 관리 worktree의 ExecPlan을 직접 수정할 수도 있다.

원격 관측은 Agent·verify가 worktree를 사용하는 동안 파일을 바꾸지 않고 다음 안전한 경계에서 적용한다. planning rebase는 작업물과 stash를 보존한 채 Git 상태를 복구하고 새 base를 SQLite에 기록한 뒤 보존 stash를 정리한다. 현재 작업의 문서 반영은 아래 경계를 따른다. 후보 HEAD가 바뀐 실행은 기존 검증·Review 증거의 재사용 자격을 다시 판단하며 게시 직전의 fast-forward·lease·PR·checkpoint 정책을 유지한다.

### 단계별 원격 문서 반영

Worker·Manager verify·Reviewer의 진입·교정·재개 전에 최신 원본과 작업 branch를 확인한다. 현재 ExecPlan·연결 Requirement·적용 PRODUCT·ARCHITECTURE의 변경 경로를 먼저 좁히고, 실제 관련성과 현재 구현의 부족은 현재 worktree를 읽기 전용으로 대조하는 일반 Planner가 판단한다. 무관한 문서·순수 revision 전진만으로 Worker를 반복하지 않는다. 코드 전용 점검 Planner와 유지보수 대상의 미착수 제한은 그대로다.

문서 반영의 이전 선언·발견 단계·과거 Worker/verify 근거·영향 판단·개정 게시 후보는 해당 ExecPlan의 execution checkpoint에 보존한다. 원격 작업 branch의 문서 관측은 기존 채택 권한을 부여하지 않는다. 기존 소유권·원격 OID 검사를 통과한 채택 뒤에만 해당 branch 문서를 입력으로 반영한다. 이 상태는 rebase·교정의 checkpoint 교체에서도 보존하며 다른 작업이나 일반 planning 상태를 덮어쓰지 않는다.

Planner의 단일 현재 계획 수정안은 Manager가 `<state-dir>/worktrees/<project-id>/document-revisions/<execplan-id>/<base-sha>/`의 소유 detached worktree에서 구조·source·dependency·기존 ID·경로를 검증한다. 실행 중 구현 worktree는 수정하지 않는다. 게시 후보·원본 snapshot을 먼저 저장하고 현재 원격 base·실행 근거·실제 PR 부재·허용 diff를 재확인한 뒤 base에 fast-forward 게시한다. push 뒤 중단은 현재 원격 내용과 저장 후보를 확인해 같은 결과를 재사용하며, 반영을 끝낸 작업 branch에서 필요한 단계로 돌아간다. PR 출현·후속 원격 변경·소유권 불확실에서는 작업물을 보존한다.

verify 진입의 구현 부족은 Worker 교정으로, 구현이 충족된 경우는 필요한 verify로 연결한다. Reviewer 진입의 문서 변경은 현재 구현과 과거 검증의 확인 범위·최신 조건 미검증을 구분한 선행 Review로 연결한다. 구현·테스트 수정이 필요한 FAIL은 기존 Worker 교정이며, 검증 실행 근거만 부족한 `VERIFY_REQUIRED`는 Manager verify로 연결한다. 선행 PASS도 최신 검증 없이 게시 token을 만들지 않는다. 변경된 조건에서는 과거 verify 성공 재사용을 새로 증명해야 하며 최종 후보의 verify·Review·게시 gate는 독립적으로 유지한다.

진행 중인 일반·유지보수 planning은 원격 base 전진 시 staged·unstaged·untracked 변경과 세션을 관리 worktree에 보존해 반영하고 최신 catalog·SQLite 권한에서 재검증한다. 시작 revision의 READY 원본과 Planner가 작성한 결과를 구분하며, 관련 근거·의존 문서의 실제 내용이 바뀐 경우에만 해당 계획을 다시 확인한다. 무관한 문서 변경과 내용 보존 이동은 재확인을 만들지 않는다

Worker·Reviewer의 문서 재확인은 현재 ExecPlan 내용 변경에 한정한다. 후보 HEAD가 바뀌면 문서 재확인 여부와 관계없이 Worker의 필요한 targeted test, Manager 전체 verify와 읽기 전용 Review를 최종 후보에서 다시 통과한다

## 실행 자리와 유지보수

프로젝트마다 SQLite 연결과 하나의 변경 실행 자리를 사용한다. 여러 READY·PLAN_BLOCKED·안정된 PR_OPEN의 branch·worktree는 함께 보존할 수 있지만 변경 실행과 PR 충돌 복구는 하나만 진행한다. 서로 다른 프로젝트만 max_workers 안에서 병렬 제출하며 Planner 호출은 전체 프로세스에서 직렬화한다.

dependency·priority·명세 변경·유지보수 DAG의 선택 순서는 PRODUCT를 따른다. 같은 프로젝트의 병렬 파일 충돌 판정·PR 재정렬은 지원하지 않는다.

일반·유지보수 planning이 진행 중이거나 시도·오류·게시 결과를 보존하는 동안 다른 ExecPlan의 차단 복구는 project 실행 투영을 교체하거나 정리하지 않는다. Worker·검증·Review·게시와 Git 변경 복구는 planning이 실행 자리를 유효하게 비운 뒤 재개한다. 안전 gate가 허용한 PR Merge·Close 확인과 해당 계획의 상태·알림 기록은 계속할 수 있다

[R-055 구현 예정](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md): 보존된 planning 오류 자체는 변경 실행 자리의 점유 근거가 아니다. planning worktree·session·미게시 결과·오류는 별도로 보존하고, 진행 중 operation·미확정 게시·실제 공유 안전 문제가 없으면 실행 자리를 비워 기존 독립 계획을 계속한다. 새 Planner 주기와 프로젝트 완료 선언은 오류가 해소될 때까지 보류한다.

### 유지보수 분석과 성공 근거

Manager는 최신 원격 추적 파일의 경로·내용 hash·mode와 분석 버전에 결합된 정적 사실을 저장한다. 변경된 파일만 다시 파싱하되 현재 파일 집합에서 관계를 다시 계산한다. 정적 코드맵·읽기 전용 조회는 심벌·import·직접 참조·관련 테스트·분석 불확실성을 제공하며 의미 검토 성공 증거는 아니다. 정적 사실은 `docs/maintenance/code-map.jsonl`과 Agent 조회용 별도 snapshot에 제공하고, Manager의 운영 SQLite·session·상태를 Agent 입력으로 내보내지 않는다.

PRODUCT의 `fast`·`targeted`·`full` 선택은 정적 비교와 마지막 성공한 전체 정밀 점검의 Merge 기준을 사용한다. `fast`에는 모델 검토 묶음이나 의미 검토 receipt를 만들지 않는다. 정밀 점검은 관련 파일을 모듈 단위의 제한된 묶음으로 구성하고, 원본·mode·정적 사실·분석 버전·평가 조건에 결합된 묶음 identity의 성공 결과를 재사용한다. 전체 점검의 성공 Merge 기준과 최신 정적·부분 평가 기준은 따로 보존한다.

모듈 의미 검토는 읽기 전용 Planner 호출로 필요한 원본과 보존 조건을 확인한다. Manager는 묶음 범위·판단·발견과 실제 원본 인용을 검증해 잠정 결과로 보존한다. 발견이 없고 일반 계획도 필요하지 않으면 추가 종합·계획 호출 없이 no-change 결과를 준비한다. 발견이 있으면 저장된 검토를 입력으로 계획 또는 근거 있는 제외에 연결하며, 파일별 검토·관계 확인·종합 페이지를 다시 기본 단계로 실행하지 않는다.

barrier mode의 유지보수 Planner는 선행과 새 리팩터링 DAG를 판단한다. Manager는 DAG 말단과 SQLite cycle rows로 필요한 선행 dependency closure를 계산한 뒤, closure 밖의 적격 READY snapshot에 누락된 말단 dependency만 기존 순서 뒤에 추가한다. 파일을 수정하기 전 cycle·snapshot 소유권을 다시 확인하고, 예상한 dependency-only diff와 catalog의 acyclic 관계를 검증한다. 실제 Manager diff는 planning result의 hash·publication path에 포함되어 원격 게시·catalog transaction·복구 검증을 통과한다. 게시 본문은 검증한 checkout과 CRLF/LF 줄바꿈 차이만 허용하며 다른 clean filter 내용 변환은 거부한다. 새 계획 등록은 현재 checkout hash와 Git 게시 blob의 감사 hash를 분리해 보존하고, Merge·archive 복구는 publication SHA의 blob hash로 증명한다. 다른 READY 파일 변경, 사용자 결정·draft 해제, snapshot 밖 파일과 실행 중·소유된 계획은 허용하지 않는다. 새 단계별 평가의 일반 계획은 별도 필요한 계획 주기에서 수행한다. 일반 계획 결합 여부와 종전 결과를 이미 보존한 cycle은 해당 mode·게시 검증을 유지해 복구한다.

planning batch의 원격 게시·catalog 반영과 현재 원격 입력 일치를 확인한 뒤 정적·모듈 평가 근거와 해당 성공 기준을 SQLite transaction에서 확정한다. 미게시·검증 실패·원격 source 변경은 성공 기준을 전진시키지 않으며 잠정 근거·publication intent를 보존해 복구한다. 새 리팩터링 계획은 검증된 DAG와 Manager barrier 편집을 포함해 게시한다. 이후 cycle 종료는 R-074의 마지막 Merge·원격 관측·catalog 반영 조건을 따르며 새 cycle에서 사후 재검증을 종료 조건으로 추가하지 않는다.

역할별 기존 parser 경계는 공통 형식 helper로 marker·구분자·공백과 다중 줄 JSON을 정규화하고, 검증된 입력 identity만 누락 결과에 결합한다. 명시적 불일치·중복 선언·중복 JSON key와 의미 근거 누락은 거부한다. Worker·Reviewer·Planner의 저장된 원문은 같은 snapshot에서 다시 해석하며, 모듈 결과는 원문·정규화 결과·원본 revision·scope·protocol을 함께 보존한다. 모듈 묶음 identity에 결과 protocol을 포함해 다른 계약의 receipt를 재사용하지 않는다.

정적 코드맵은 내용 hash·분석 버전에 결합된 공백 여부를 제공한다. 같은 원본 hash·범위의 이전 분석은 활성 assessment 재개에서도 분석 사실만 갱신하고 기존 판단을 보존한다. 파싱이 확인된 빈 파일만 있는 모듈은 모델 묶음을 만들지 않고 assessment의 원본 사실을 보존한다. 빈 파일이 속한 의미 있는 모듈은 검토하며 내용이 있는 원본을 인용해야 한다. 전체가 공백 원본이어도 분석 불확실성이 남은 묶음은 의미 검토를 유지하고 실제 공백 원본을 인용하며, 0 byte 원본만 빈 인용을 허용한다. 미지원·파싱 실패·동적 관계의 불확실성은 유지한다. 복구용 파일 결과에서 누락된 공백 chunk나 compact 결과의 null 공백 항목은 실제 byte 범위·hash·인용·공백 사실로 결합하며, 내용이 있는 chunk의 판단은 생성하지 않는다.

종전 파일별 평가 경로는 이미 보존된 결과·게시의 복구를 위해 유지한다. 실제 검토 파일의 ledger·의미 코드맵·파일별 성공 기준은 그 경로에서만 원본·관계·게시 검증을 거쳐 확정하며, 정적 코드맵이나 새 모듈 평가를 파일별 의미 검토 완료로 기록하지 않는다.

## AI Agent 실행

- 사용자 요청의 새 기능·정책 변경은 대화형 Agent가 관련 Requirement를 먼저 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용한다. 프로젝트로 복제하는 Specification Skill은 제품 명세 작성·검토만 안내하고 Manager 내부 실행 절차를 설명하지 않는다. 이 저장소 루트 AGENTS는 Skill을 참조하며 문서 작성 기준을 반복하지 않는다. 직접 작업에는 ExecPlan 작성을 요구하지 않으며 생성·계획 작성은 매니저가 호출하는 자동 Planner 흐름에서 수행한다
- 정적 자동 실행 규칙은 선택 runtime의 `docs/AGENTS.common.md`와 `docs/AGENTS.planner.md`·`docs/AGENTS.planner_check.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`에 둔다. common에는 모든 역할의 기준만, 역할 파일에는 해당 역할의 권한·금지사항·절차만 남긴다. Skill과 Manager가 프로젝트에서 쓰는 예약 경로는 PRODUCT의 프로젝트 예약 경로 목록을 따르며 공통 지침과 명세 작성 Skill에 위치 보존을 명시한다. runtime 전용 `docs/AGENTS.*.md`는 프로젝트 예약 경로가 아니다. Loop Manager 전용 새 루트 항목 제한·instruction 원본 수정 지침은 이 저장소 루트에 두며 runtime 공통·역할별 합성 대상에 포함하지 않는다
- Manager는 작업 시작 시 모든 원본을 묶음으로 고정하고 내용 해시·source revision·snapshot과 작업·session 관계를 상태 디렉터리와 SQLite에 보존한다. 일반·유지보수·수동 제출 Planner는 common+planner, 점검은 common+planner_check, Worker와 Reviewer는 common+각 역할을 사용한다. 재시도·새 세션·runtime 갱신에도 같은 작업의 원본 버전을 유지한다
- 실제 합친 내용을 Codex의 추가 developer instruction 또는 Claude의 append system prompt로 전달하고 실제 내용 해시를 호출 기록에 남긴다. 기본·기존 추가 지침·프로젝트 규칙과 실행 경계를 보존하며 원본·snapshot 오류, 미지원 전달 기능·설정 충돌·알 수 없는 역할은 모델 실행 전에 거부한다
- Ponytail 원문은 runtime의 `docs/AGENTS.ponytail.md`에 두고 version 2 원본 묶음에 함께 고정한다. Worker는 common+worker+ponytail을 항상 합성하고 다른 역할은 common+역할 지침만 사용한다. version 1 묶음·과거 Markdown snapshot에는 새 원문을 덧붙이지 않는다. Plugin 조회·캐시·설치 판정 없이 기존 provider 전달 경로를 사용한다. Codex의 기존 Plugin·App 비활성화와 쓰기 경계는 유지한다
- Worker의 실제 합성 내용 해시는 기존 invocation ledger에 결합한다. definition snapshot의 `guidance_hash`는 고정 원본 묶음, `worker_guidance_hash`는 해당 작업의 마지막 Worker 전달 내용을 나타낸다. Worker 완료 시 전달 지문과 성공 token을 갱신하고 검증·Review·게시 및 수동 제출 결과 재사용에도 반영한다. 전달 내용 변화는 원본·명세 변경으로 분류하지 않아 진행 중인 rebase 계약을 바꾸지 않는다. Reviewer 호출이 Worker의 전달 해시를 바꾸지 않으며 다른 실제 전달 내용의 결과를 같은 지침의 성공으로 재사용하지 않는다
- 원본 묶음 snapshot은 저장소 밖 Manager 상태 디렉터리의 JSON으로 보존하고 역할별 합친 Markdown은 같은 디렉터리의 실행용 파일로만 생성하며 커밋하지 않는다. 합친 파일은 고정 원본에서 재생성하지만 원본 누락·손상은 최신 내용으로 대체하지 않는다. 기존 단일 Markdown snapshot은 원래 내용으로 재개하며 최신 역할 지침을 덧붙이지 않는다. 관련 planning·성공 결과 재사용에는 모든 역할 원본을 포함한 묶음 해시를 사용한다
- Planner, Worker와 Reviewer의 작업별 prompt는 Loop Manager에 내장하며 목표·snapshot·승인 사실·피드백·결과 형식 등의 문맥을 구성한다. 정적 공통·역할 규칙을 이 사용자 메시지에 중복하지 않는다
- 프로젝트별 prompt 재정의는 지원하지 않으며 프로젝트 파일이나 작업 prompt로 자동 역할 권한을 확대하지 않는다
- 프로그램은 전체 ExecPlan을 코드로 파싱하되 Planner에게는 비종료 계획의 ID, 제목, 상태, sources, depends_on과 priority만 compact catalog로 전달한다
- Planner는 PRODUCT, 활성 Requirement, ARCHITECTURE, 관련 코드·테스트, 비종료 ExecPlan compact catalog와 프로그램이 제공한 PR 전 계획 snapshot을 읽고 현재 식별 가능한 미충족 작업 전체를 한 계획 주기에 반영하며, 범위 밖의 보존 가치가 있는 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
- Planner는 MERGED ExecPlan 메타데이터와 본문을 기본 입력으로 받거나 읽지 않는다. 프로그램만 ID 고유성, 번호 할당, 의존성 충족과 감사 이력을 위해 이를 유지한다
- Planner는 안전한 무인 실행 차단과 반복적인 사용자 복구 제거를 최우선으로 priority를 정하고, 정상 경로 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 계획한다
- Planner는 새 `docs/plans/*.md`를 생성하고 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있다. 프로그램의 PR 전 snapshot 안의 계획만 같은 ID로 개정하며, 시작한 계획의 경로·기존 수행 기록을 보존한다. rename·삭제는 미착수 계획에만 허용한다. snapshot 밖·PR 등록·MERGED·archive 계획과 그 밖의 파일은 변경할 수 없다. 단계별 문서 판단의 수정안은 읽기 전용 결과로 반환하고 Manager가 게시를 검증한다. 프로그램은 Agent 실행 전후 diff와 SQLite snapshot으로 범위를 검증한다
- Worker는 ExecPlan·관련 unit test·verify 스크립트를 구현한다. Requirement·ExecPlan 구현으로 바뀌는 제품 동작·장기 규칙은 PRODUCT에, 설계·책임 경계·계약은 ARCHITECTURE에 같은 변경에서 반영하며 문서 경로만으로 설계 수정을 거부하지 않는다. 현재 구현에 필요한 대응표·구현 기록은 다른 기존 ExecPlan에도 갱신할 수 있다. 다른 계획의 목표를 구현하거나 상태를 문서에 기록하지 않는다. 범위 밖 아이디어는 FUTURE_IDEAS에 기록한다
- Worker는 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행한다. 실패하면 같은 세션에서 수정하고 해당 테스트를 다시 실행하며, 실행 가능한 관련 unit test가 없으면 그 사유를 결과에 포함한다
- Worker는 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check와 전체 검증 스크립트를 직접 실행하지 않는다
- Reviewer는 Worker의 targeted unit test와 프로그램의 전체 verify가 통과한 뒤 실행되며 테스트 command나 verify 스크립트를 다시 실행하지 않는다. 파일을 수정하지 않고 코드, 테스트, verify 스크립트의 diff와 전달된 결과를 읽어 요구사항 충족, 회귀, 테스트 누락과 verify 약화를 포함한 blocking issue를 확인한다
- 프로그램은 각 Agent 실행 전후 diff를 검사한다. Planner의 범위 위반은 `planning_error` 흐름으로 처리하고, Worker 또는 Reviewer의 범위 위반은 해당 ExecPlan을 BLOCKED로 처리한다
- Worker와 Reviewer는 ExecPlan이 PR 생성 준비를 마칠 때까지 각자의 세션을 재사용한다
- 세션을 재개할 수 없으면 같은 worktree, ExecPlan과 저장된 실패 내용을 전달해 새 세션을 시작한다
- 상태 분류, 설정 검증, Git/GitHub 명령과 verify 실행은 프로그램 코드가 담당한다
- Worker는 commit, fetch, pull, rebase, push, worktree 등록과 PR 생성을 수행하지 않는다. Worker 반환 뒤의 모든 Git 쓰기는 Agent 호출 밖의 프로그램 코드가 소유한다
- AI Agent와 verify subprocess에는 역할 수행에 필요한 환경 변수만 전달하고 Telegram token을 포함한 Loop Manager 비밀값을 상속하지 않는다. provider 환경 필터는 비밀이 아닌 `PYTHONDONTWRITEBYTECODE`와 `PYTHONPYCACHEPREFIX`도 보존한다. verify 조율 endpoint·secret·session은 runner 자체 조율에 필요해 runner에만 전달한다. runner의 자식 subprocess에는 해당 세 값을 뺀 환경 복사본을 넘기며 부모 환경과 호출자 mapping은 변경하지 않는다. 명령행 Python에서 선택한 bytecode 정책과 prefix도 자식 Python에 반영한다. 기존 marker·`PYTHONPATH`와 필요한 일반 환경은 자식에서 유지한다

### Ponytail 지침

Worker에 추가하는 [AGENTS.ponytail.md](AGENTS.ponytail.md)는 [Ponytail](https://github.com/DietrichGebert/ponytail)의 코드 단순화·기존 구현 재사용 지침이다. upstream [552acd5](https://github.com/DietrichGebert/ponytail/blob/552acd5efd0aeae2583a12efe39373d2f076f25e/AGENTS.md)의 원문과 [MIT 라이선스](AGENTS.ponytail.LICENSE)를 보존한다. 적용·버전 경계는 위 원본 묶음 계약을 따른다.

축약 원문은 전체 Skill·Hook과 동일한 효과를 보증하지 않는다. Codex 0.161.0의 [command Hook](https://github.com/openai/codex/blob/rust-v0.161.0/codex-rs/hooks/src/engine/command_runner.rs)은 별도 셸 프로세스로 실행되고 외부 MCP에도 내장 파일 sandbox 적용을 보장할 수 없으므로 기존 Plugin·App 비활성화와 쓰기 경계를 유지한다. 사용자 Plugin 설정·설치 상태는 변경하지 않으며 명시된 요구사항·안전·검증·역할·승인·게시 규칙을 보존한다.

### R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)

2026-09-26 사용자 결정으로 provider의 내장 workspace 쓰기 제한을 우선 사용하고, Codex·Claude의 자동 승인 검토를 함께 유지하는 설계를 채택한다. 이 절은 R-023의 구현 대상 설계이며 현재 Merge된 동작의 구현 완료를 뜻하지 않는다. Agent 호출 밖에서 Manager가 Git 쓰기를 소유하는 기존 경계는 유지한다.

Codex를 기본 provider와 기능·검증 수준의 기준으로 삼는다. Claude adapter는 같은 기능을 제공하는 전제로 해당 provider의 명칭·설정·호출 방식에 대응한다. 지원 구현의 검증에는 공식 정책 대조와 모의 호출 테스트를 사용하고, Claude CLI·이용 가능한 계정이 없는 개발 환경에서 실계정 호출 성공을 필수 gate로 요구하지 않는다. 실제 호출 검증은 가능한 환경에서 추가하며 미수행 범위를 기록한다. 이 설계는 Claude의 구현 완료나 실제 실행 성공을 선언하지 않는다.

provider adapter는 실제 호출 직전에 선택된 canonical worktree, Git worktree 등록과 repository identity를 확인하고, 신규 session과 재개 호출에 동일한 쓰기 정책을 명시한다. 허용 경로는 선택된 workspace와 실행별로 소유권을 확인한 최소 임시 경로로 제한한다. 사용자·프로젝트·session 설정의 병합으로 다른 worktree, source checkout, shared Git metadata 또는 넓은 임시 경로가 추가되면 이를 격리 확인으로 인정하지 않는다. workspace 정책과 별도로 기존 Planner·Worker·Reviewer·유지보수 Planner의 허용 파일 및 변경 범위 검사를 유지한다.

- Codex는 R-050에 따라 Manager가 `codex exec`를 직접 호출한다. 선택된 관리 worktree를 `-C`로 지정하고, 쓰기 역할에는 `workspace-write`, Reviewer 및 읽기 전용 역할에는 `read-only`를 적용한다. `-C`는 작업 경로이며 쓰기 제한은 내장 sandbox가 담당한다. 바깥 `codex sandbox` wrapper, `app-server` transport와 중첩 sandbox probe를 호출 조건으로 요구하지 않는다. `approvals_reviewer="auto_review"`를 유지하면서 `approval_policy.granular`의 `sandbox_approval=false`, `request_permissions=false`로 sandbox 밖 실행과 권한 확대 요청을 거부한다. 필요한 나머지 승인 항목은 검토 대상으로 유지한다. `approval_policy="never"`로 전체 자동 검토를 비활성화하거나, 편의 CLI 옵션이 명시한 granular 정책을 덮어쓰게 하지 않는다.
- Codex의 추가 `writable_roots`는 Manager가 허용한 최소 경로만 포함한다. 기본 `/tmp` 및 `$TMPDIR` 쓰기 범위는 `exclude_slash_tmp`, `exclude_tmpdir_env_var`로 제외하고 필요 시 실행별 임시 경로만 추가한다. `.git` pointer file과 그 resolved Git directory를 포함한 보호 경로 및 shared Git directory는 쓰기를 허용하지 않는다. 상속된 profile·규칙·확장 기능이 경계를 우회할 수 없는지도 확인한다.
- Claude의 쓰기 역할은 `--permission-mode auto`를 명시하고 공식 지원 모델·계정·CLI 조건과 유효 설정을 대조한다. 실제 호출에서 보고된 미지원은 해당 호출의 오류로 처리하며, 개발 환경의 계정 부재만으로 별도의 효력 시연을 요구하지 않는다. `acceptEdits`는 편집 자동 승인 모드이므로 자동 검토 유지의 대체 설정으로 사용하지 않는다. Reviewer는 기존 읽기 전용 역할과 편집 도구 금지를 유지하고, 해당 역할에서도 지원되는 자동 검토 경로를 확인한다. 지원되지 않으면 권한 모드를 완화하지 않고 원인을 기록한다.
- Claude는 `sandbox.enabled=true`, `failIfUnavailable=true`, `allowUnsandboxedCommands=false`를 적용하고 `excludedCommands`를 비워 sandbox 밖 재실행을 막는다. sandbox 안 명령을 자동 검토 없이 일괄 승인하지 않도록 `autoAllowBashIfSandboxed=false`를 사용해 permission mode의 검토 흐름을 유지한다. 실제 검토 대상은 provider 정책에 따르며 모든 읽기·편집·명령이 검토된다고 표시하지 않는다.
- Claude의 linked worktree는 기본 sandbox에서 main repository의 shared `.git` 쓰기가 추가 허용될 수 있으므로 resolved shared Git directory와 worktree Git directory를 `sandbox.filesystem.denyWrite`로 명시적으로 거부한다. 설정 병합으로 추가 workspace나 우회 예외가 생기지 않도록 확인한다. Bash sandbox 밖에서 처리하는 Edit·Write·NotebookEdit 등 파일 도구에도 같은 쓰기 경계를 강제한다. Reviewer는 선택된 worktree의 쓰기까지 거부한다.
- 내장 sandbox가 없는 Claude native Windows는 미지원으로 진단하고 해당 Agent 호출을 시작하지 않는다. Linux와 Codex native Windows도 필요한 CLI 기능과 sandbox 초기화를 확인할 수 없으면 동일하게 중단한다. WSL·컨테이너 전환이나 새로운 외부 격리 infrastructure는 이 설계에 포함하지 않는다.

격리 정책 작성과 유효 정책 확인은 provider 호출 경계의 독립 책임으로 두고, 기존 Agent invocation·session·snapshot 저장과 연결한다. SQLite에는 호환 가능한 추가 저장만 사용하며, 개별 호출마다 provider, CLI 버전, 실행 환경, 역할, canonical worktree, repository identity, 유효한 격리 정책의 지문, session/호출 identity와 확인·거부 결과를 보존한다. 증거 저장이나 확인이 실패하면 검증된 실행으로 취급하지 않는다. 재개·provider 전환·설정 변경·중단 뒤에는 이전 증거를 새 호출의 증거로 재사용하지 않는다. 비밀값, 인증 자료와 source checkout의 로컬 파일 내용은 저장하지 않는다.

단순 CLI 옵션, cwd 지정, 자동 검토의 승인 또는 사후 Git diff만으로 호출 전 격리를 증명하지 않는다. 실제 적용되는 정책과 허용 경로 쓰기·금지 경로 쓰기 거부를 확인하며, source checkout의 로컬 상태를 바꾸지 않고 증명해야 한다. Codex 확인은 Agent의 파일 변경·도구 실행에 적용되는 내장 정책을 대상으로 한다. 설치 CLI의 공식 지원 설정, 병합 우선순위와 역할별 실제 허용·거부 동작을 확인하며, exec JSONL에 유효 정책 전용 필드가 없다는 이유로 transport를 바꾸거나 바깥 wrapper를 추가하지 않는다. Codex 프로세스 전체의 mount·내부 쓰기 및 중첩 도구 실행 성공은 증명 조건이 아니다. 정상 인증·session·로그 저장과 PRODUCT의 `refs/codex/turn-diffs/*` 예외는 구분하며 그 밖의 Git·Agent 쓰기 권한은 확대하지 않는다. Claude도 같은 확인 수준을 적용한다. 공식 정책의 지원 조건·설정 병합·resolved Git 경로와 파일 도구 권한을 대조하고 신규·재개·fallback의 호출 구성을 모의 테스트로 검증한다. 계정이 필요한 모델 호출이나 금지 쓰기·자동 검토의 실시간 시연을 호출 전 필수 probe로 추가하지 않는다. 정책 검토·모의 테스트와 실제 호출 검증의 근거를 구분해 저장하고, 실제 호출에서 관측한 미지원·정책 충돌·초기화 실패는 해당 Claude 작업에만 반영한다. Claude의 개발 환경 부재나 선택 프로젝트의 인증 실패 때문에 독립 Codex 작업을 막거나 다른 provider로 임의 전환하지 않는다. 신규 호출에서 요구 정책을 적용·확인할 수 없거나 기존 허용 작업과 양립하지 않으면 원인과 기존 작업물·session을 보존한 채 중단한다. 현행 legacy 감시와 R-055·G-199의 기본 checkout 제외 목표는 아래 실행별 범위 절에서 구분한다. Loop Manager의 Worker 이후 Reviewer 검토는 provider의 실행 전 자동 승인 검토와 별도로 유지한다.

Reviewer의 `read-only`는 workspace 파일 쓰기를 제한한다. 검색·diff·기존 결과 읽기에 필요한 명령은 내장 sandbox 안에서 실행할 수 있다. 테스트 command와 verify를 재실행하지 않는 규칙은 고정된 common+Reviewer 역할 instruction에 유지하며, 쓰기 없는 테스트도 역할상 실행하지 않는다. Worker의 targeted test 결과와 Manager의 전체 verify 결과를 작업별 prompt로 전달하는 기존 흐름을 유지하고, read-only 정책이 테스트 명령을 모두 차단한다고 취급하지 않는다.

설정 의미와 지원 범위는 구현 시 설치된 CLI와 공식 문서를 대조한다: [OpenAI 승인 정책 설정](https://learn.chatgpt.com/docs/config-file/config-reference), [Codex 자동 검토](https://learn.chatgpt.com/docs/sandboxing/auto-review), [Claude sandbox](https://code.claude.com/docs/en/sandboxing), [Claude permission mode](https://code.claude.com/docs/en/permission-modes). 지원되지 않는 key를 무시한 실행을 격리 확인으로 기록하지 않는다.


## Model과 Effort

최초 Worker level은 complexity의 low=1, medium=3, high=5에 critical risk만 1을 가산해 1~6으로 계산한다. low·medium·high risk는 가산하지 않는다. [R-052](requirements/%5BR-052%5D%20Worker%20Classification%20and%20Initial%20Level%20Calibration.md)의 12개 조합은 다음과 같다.

| complexity | risk | 최초 Worker level |
|---|---|---:|
| low | low / medium / high / critical | 1 / 1 / 1 / 2 |
| medium | low / medium / high / critical | 3 / 3 / 3 / 4 |
| high | low / medium / high / critical | 5 / 5 / 5 / 6 |

level 7·8은 완료한 Worker 교정·전체 verify 통과 뒤의 유효 Review FAIL에 따른 승격으로만 도달하며 상한은 8이다. 각 effective level의 첫 FAIL을 기준선으로 삼고 기준선의 같은 ISSUE_ID가 두 교정 결과에 다시 나타나거나 교정 결과가 세 번 연속 FAIL이면 다음 Worker 호출부터 한 단계 올린다. 집계 제외·재시작·PASS 조건은 [PRODUCT의 실패와 재시도](PRODUCT.md#실패와-재시도)를 따른다.

실제 Model·Effort의 원본은 `config/config.ini`다. 선택 provider의 `[<provider>.worker]`에 level 1~8을, `[<provider>.roles]`에 일반 Planner `planner`와 빠른 점검 Planner `planner_check`를 각각 `Model alias,Effort` 형식으로 명시하고 참조 alias를 `[<provider>.models]`에 명시한다. 필수 profile·alias는 `DEFAULT`, 다른 역할·level, 코드·CLI 또는 설정 예시로 보충하지 않는다. section·profile·alias 누락, 빈 Model·Effort, 잘못된 형식과 CLI가 지원하지 않는 Model·Effort 조합은 Agent 실행 전 설정 오류로 거부한다. 미선택 provider의 설정은 요구하지 않는다. 서로 다른 level에 같은 지원 profile을 지정할 수 있고 level 8에도 특정 모델 계열·alias·Effort를 강제하지 않는다.

유지보수 Planner는 별도 profile이 없으면 기존 일반 Planner 연결을 유지하며 Reviewer와 그 밖의 설정 기본값도 유지한다. `config/config.ini.example`의 모델명·alias·Effort는 운영 설정을 작성하는 예시이며 누락 fallback이나 강제 profile이 아니다.

저장된 최초 level은 현재 선언과 산식으로 재계산하되 실행 이력이 있는 계획의 effective level을 낮추지 않고 session·branch·worktree·PR 연결, 작업물과 승격·실패 감사를 보존한다. 산식 변경만으로 BLOCKED를 해제하지 않으며 정상적인 BLOCKED 해제에서만 최초·effective level과 진행 중 승격 이력을 초기화한다. 원격 복구의 감사·session 보존 예외는 아래 중단 복구 계약을 따른다.

## 상태 저장과 중단 복구

상태의 의미·허용 전이는 [PRODUCT.md](PRODUCT.md#states-and-transitions)를 따른다. 프로그램만 상태를 변경하고 다음 phase와 소유 worktree를 작업 시작 전에 한 transaction으로 저장한다. 중단 뒤 저장된 단계와 실제 Git 상태를 대조해 같은 작업을 재개한다.

worktree가 없고 소유 branch가 있으면 재등록할 수 있다. 둘 다 복구할 수 없거나 소유권을 확정할 수 없으면 작업물을 보존해 해당 scope로 차단한다. 원인·plan identity·판단 근거·해제 지문과 상태는 함께 저장하며, scope를 확인할 수 없는 legacy BLOCKED는 PROJECT_BLOCKED로 보수적으로 이관한다.

일반적인 PLAN_BLOCKED는 계획·원래 활성 sources의 내용 해시를 기준으로 재평가한다. 닫힌 PR은 같은 PR reopen을 요구한다. 과거 파일 승인 대기는 일반 작업·소유권 검사를 통과하면 기록을 보존한 채 재개한다. 독립 차단·planning 오류·실패 감사는 함께 지우지 않는다. planning_error는 실제 교정·재검증·게시 복구로 원인이 해소된 뒤에만 지운다. cleanup_pending과 ExecPlan archive pending은 상태 전이를 만들지 않는다.

Worker session binding과 pending transition은 판정 전에 저장 JSON의 객체·필수 필드·문자열·지원 kind·중첩 binding을 검증한다. 손상값은 기존 `STATUS_FAILED` 복구 경계에서 진단하며 원문, phase, 작업물과 실패·level 승격 감사를 보존한다. 손상 pending을 건너뛰어 기존 session을 실행하거나 성공·차단 해제를 만들지 않는다.

Git·journal 파일·SQLite는 단일 원자 transaction이 아니다. 저장 intent, 현재 Git 결과와 SQLite generation을 대조해 이미 끝난 단계를 반복하지 않고 결과만 멱등하게 반영한다. 과거 증거·실패·알림 감사는 삭제하지 않는다.

반복 수동 제출의 저장 후보는 생성 당시 원격 OID·incoming 부모와 결합한다. exact 게시 전이 또는 후보를 포함하는 후속 이력의 equal 관측은 같은 저장소·소유자·종류·대상 ref로 검증한다. equal intent는 정확한 OID 관측 뒤 저장되므로 결과 저장 전 재중단과 후속 관측으로 종료된 전이에도 게시 근거를 유지한다. 종료된 exact 전이의 aborted receipt는 소유 intent·빈 기준 근거·원격 후속 계보를 검증해 원문 그대로 확정한다. 그 관측에 후보가 포함돼 있으면 다음 equal intent가 없어도 게시 후보 제거를 감지한다. 완료 receipt는 변경하지 않으며, 원격에서 게시 후보가 제거되면 새 lease나 대체 후보를 만들지 않는다.

### 관리 rebase와 후보 검증

현재 rebase 정책은 작업 수행 이력을 필수 증거로 삼던 R-045 설계를 대체한다. 자동 Worker 교정과 사용자 수동 해결·stage·continue·완료를 같은 현재 상태 판정으로 다룬다. 과거 기록은 감사 자료로 보존하며 누락 자체를 거부 조건으로 사용하지 않는다.

Worker 결과와 작업 범위를 확인한 뒤 최신 `origin/<base-branch>`를 fetch하고 소유 변경을 commit하여 필요한 rebase를 완료한 다음 게시 후보 HEAD에서 검증 근거와 Review를 판단한다. `RevisionHandler`·`LatestBaseHandler`의 정상 rebase 호출이 충돌 없이 완료되면 source 내용 지문을 포함한 기존 구현 입력 token·archive token·완료 Worker 결과를 대조한다. 일치하는 결과만 현재 수용 HEAD의 `worker_candidate_token`에 checkpoint CAS와 runtime transaction으로 연결해 VERIFYING으로 이어간다. `decide_revision_gate()`가 현재 후보 확인 불일치 등으로 IMPLEMENTING을 요구하면 clean 완료도 그 판정을 유지한다. 구현 교정 중인 작업, 실제 조건 변경, 충돌·미완료·중단 뒤 수용에는 이 예외를 적용하지 않는다. 같은 게시 lease·중간 checkpoint 경계를 유지하며 verify·Review·publication은 독립적으로 무효화한다.

definition snapshot에는 원래 ExecPlan 전문과 전체 body hash를 함께 보존한다. Worker body 비교는 Progress의 체크 상태만 정규화하고 Progress/Decision Log에서 다음 단독 실행 기록 형식만 분리한다(PASS 또는 FAIL, 날짜·명령·test 수는 실제 과거 사실).

```text
- 2026-10-08 실행 기록: `python3 -m unittest tests.test_code` → PASS (2 tests).
```

그 밖의 자유 문장·새 조건·승인은 해당 절 안에서도 입력이다. 두 원문과 hash가 일치해야 기록 분리를 적용하며 legacy snapshot은 원래 revision/path를 다시 읽어 저장 hash를 확인한 경우에만 보강한다. 원문이 없으면 현재 기록으로 합성하지 않는다. 전체 definition·verify 단계 token에는 전체 body hash를 유지해 기록 변경을 Review·승인 예외로 확대하지 않는다.

모든 관리 rebase의 시작·충돌 해결·완료·중단 복구는 현재 repository·계획·canonical worktree·소유 branch·target base·원격 OID와 PR identity/head를 확인한다. Worker 교정 turn, 승인 stage tree, 명령 receipt, reflog 표식, 원본 commit별 replay와 journal 완전성은 완료 조건이 아니며 기존 기록과 실패 감사는 보존한다

자동 Worker 해결과 사용자의 수동 파일 해결·stage·continue·완료를 같은 현재 상태 판정으로 수용한다. 진행 중인 소유 rebase와 미해결 autostash 복원은 충돌 파일·index·operation을 보존하고 기존 Worker 교정 경계로 보낸다. Manager는 현재 범위가 확인된 필요한 path만 stage하고 이미 끝난 Git 단계를 반복하지 않는다

완료 결과는 HEAD·branch ref·worktree 등록·target ancestry, clean index·미해결 operation 부재, 현재 역할·선택 ExecPlan 및 archive 계약에 맞는 최종 변경과 정확한 원격 lease를 확인한 뒤 현재 상태·입력 지문과 SQLite generation에 결합해 한 번만 반영한다. 원본 commit 내용·개수·순서의 변화 자체를 거부하지 않으며 Git 완료 뒤 저장 전 중단도 같은 검사로 복구한다

rebase 채택의 한 검사 구간에서는 `ManagedRebaseService.adopt`가 안정된 이중 관측을 읽기 전용 소유권 소비자에 전달한다. 소비자는 전체 `RebaseExpectation`과 checkpoint generation이 일치할 때만 이를 사용하고 게시 owner를 별도로 검증한다. SQLite 확정에서는 새 이중 관측으로 다시 확인하며 Git·Agent·verify·게시 및 재시작 경계를 넘어 전달 결과를 저장하지 않는다. 최초 검사·소유권 소비·확정 재검증은 각각 `rebase:adoption_inspect`·`rebase:adoption_owner`·`rebase:adoption_confirm`으로 계측한다.

PR 없는 동일 계획의 기존 원격 작업 branch를 관리 rebase한 경우, 현재 repository·계획·worktree·branch·HEAD·ref·등록·target base ancestry·index·operation·허용 변경과 마지막 채택 원격 OID를 확인한 뒤 완료 HEAD를 중간 checkpoint로 게시한다. 다음 Worker·전체 verify·Review·추가 base rebase보다 먼저 게시 또는 안전한 거부를 확정하며 base가 다시 전진해도 완료된 결과를 먼저 처리한다. 기존 PR·신규 원격 branch·다른 계획의 branch는 이 경로로 게시하지 않는다

중간 게시 intent는 정확한 기대 원격 OID와 후보 HEAD, 현재 상태 수용 identity·지문을 SQLite에 보존한다. 게시 직전 실제 ref와 PR 부재를 다시 확인하고 정확한 OID를 명시한 `--force-with-lease`로 같은 ref만 갱신한다. 원격 삭제·재작성·후속 commit·PR 출현·권한 또는 보호 정책 거부에서는 기존 ref와 작업물을 보존하며 lease를 새로 추정하지 않는다

push 전후 및 SQLite 반영 전 중단에서는 같은 intent와 실제 원격 OID·현재 소유 상태를 재대조한다. 원격이 이미 후보 HEAD면 push·commit을 반복하지 않고 완료 기록과 마지막 채택 원격 OID를 한 transaction으로 갱신한다. 일시적 실패는 같은 intent로 재개하며 미게시 rebase가 여러 개 쌓인 경우도 과거 journal·receipt·commit별 replay 연쇄 없이 동일한 현재 상태 판정을 사용한다. 중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인이 아니며 최종 후보의 Worker 확인·targeted test·전체 verify·Review·PR gate를 유지한다

PR 없는 게시 checkpoint의 원격 OID가 rebased HEAD의 조상이 아니어도 동일 계획의 게시 identity·마지막 채택 원격 OID·현재 branch/worktree·target·허용 최종 변경이 일치하면 소유권을 유지한다. 원격 후속 commit·삭제·재작성과 PR head 변경은 수용하지 않으며 journal/replay 완전성을 ownership 조건으로 다시 요구하지 않는다

rebase로 후보 HEAD 또는 ExecPlan·근거 입력이 바뀌면 전체 verify·Review·publication 증거를 무효화해 입력을 다시 판단한다. 위의 정상 호출에서 증명된 clean rebase는 Worker 결과를 보존하며, 그 밖의 수용은 기존 또는 정상 fallback Worker 세션이 현재 입력과 최종 diff를 재확인하고 관련 targeted test 결과를 반환한다. 같은 확인 후보에서는 Worker를 반복 호출하지 않는다. 수용 자체는 구현 완료·게시 승인·차단 해제가 아니며 중간 checkpoint 게시도 최종 성공을 대신하지 않는다.

현재 결과는 선택 branch/worktree의 비교 기준선에만 반영한다. Agent 원본 snapshot·session·허용 범위와 과거 감사를 보존하고 수동 복구를 Manager 실행·Agent 격리 증거로 표시하지 않는다. 실제 Agent 범위 위반, 다른 Manager Git 작업의 증거와 별도 Review·사용자 결정·planning 차단은 기존 경계를 유지한다

저장된 rebase 충돌·완료·소유권 오탐 차단은 실제 원인과 대상 계획의 rebase를 연결하고 현재 Git 상태, checkpoint, 원격 branch·PR 소유권 및 허용 최종 변경을 다시 확인한 경우에만 해당 원인을 해제한다. 과거 작업 기록이나 세션 일치는 해제 조건이 아니다. 다른 Review·사용자 결정·Agent 범위·planning 원인과 실패·알림 감사는 보존한다. 해제 후 후보 revision에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 수행하며 게시 직전 최신 원격 lease를 확인한다

### 차단 작업의 원격 반영과 보존

흐름:
1. `PLAN_BLOCKED` 상태, scope, 구조화 원인과 판단 근거를 SQLite에 먼저 commit하고 PR이 없는 계획에 별도 durable checkpoint request를 기록한다
2. 저장된 branch·결정적 worktree·workspace 소유권·HEAD와 Worker 허용 변경 범위가 모두 일치하고 미해결 Git operation, symlink, index에 없는 ignored 경로의 stage 또는 tracked 경로 충돌, 범위 밖 staged entry, index flag 또는 공유 metadata 불확실성이 없을 때만 검증된 pathspec으로 checkpoint commit을 만든다. 충돌하지 않는 ignored artifact의 잔존만으로 checkpoint를 거부하지 않으며 이미 commit된 변경이나 변경 없음에는 빈 commit을 만들지 않는다
3. 같은 소유 branch의 원격이 없으면 정확한 부재 lease로 생성하고, 저장된 expected OID의 정확한 lease에 결합된 fast-forward만 갱신한다. fast-forward가 아닌 갱신은 PR 없는 동일 소유 branch의 검증된 관리 rebase에만 허용하며 예상하지 못한 원격 commit, branch protection과 권한 거부를 우회하지 않는다
4. commit·push phase와 local/remote OID를 저장해 중단 후 같은 checkpoint를 재개하고, 일시적 push 실패는 실행당 제한 횟수만 재시도한 뒤 local commit과 worktree를 보존한다. checkpoint 결과와 무관하게 ExecPlan은 `PLAN_BLOCKED`로 남고 독립 READY 선택 자리를 점유하지 않는다
5. `planning_error`, 등록·branch 생성 전 실패, 기존 PR 또는 현재 대상 작업의 소유권·범위를 증명할 수 없는 차단에는 원격 쓰기를 수행하지 않고 미게시 이유를 저장한다. `PROJECT_BLOCKED`에서는 검증 가능한 대상 계획의 복구·보존만 15번의 경계에서 허용한다. 차단 알림은 checkpoint 결과 확정 뒤 성공 시 commit OID와 immutable branch link를, 미게시 시 local-only 이유를 포함한다
6. 계획별 입력 변경, 사용자 결정 또는 닫힌 PR reopen 조건은 해당 `PLAN_BLOCKED`만 재평가한다. PR 없는 게시 checkpoint의 저장된 원격 branch가 달라지면 게시 OID와 마지막 채택 head를 포함하는 후속 commit인지 확인한다. 계획·branch·worktree·저장소 소유권, 로컬 HEAD·index·작업 파일·ignored 경로·Git operation 및 허용 변경 범위를 검증한 뒤 로컬 작업을 보존할 수 있을 때만 같은 branch를 fast-forward한다. 삭제·재작성·분기·소유권 불명확·충돌은 구체적인 이유로 계속 차단한다. 새 OID만으로 해제하지 않고 실제 변경과 원인별 조건을 재평가하며, 해결된 사용자 commit은 Worker 검토와 targeted test, 전체 verify, Review를 다시 거친다. 해결되지 않은 검사 결과는 원격 head와 함께 저장해 동일 입력의 반복 시도와 알림을 막는다. 게시 증거가 없는 legacy checkpoint와 닫힌 PR에는 이 경로를 적용하지 않는다
7. 해제 조건이 충족된 계획은 기존 branch와 worktree에서 재개하고 재시도 횟수와 verify·Review 실패 지문 이력을 초기화하며 effective Worker level과 승격 이력을 최초 level로 되돌린 뒤 READY 또는 PR_OPEN으로 변경한다. 같은 branch의 최초 PR은 최종 verify와 Review 성공 뒤에만 생성한다
8. `PROJECT_BLOCKED`는 기본 worktree, 허용되지 않은 공유 ref, 외부 worktree와 workspace 소유권을 다시 검증해 프로젝트 안전이 증명된 경우에만 해제한다. `refs/codex/turn-diffs/*`의 순변경은 해제 조건을 무효화하지 않는다
9. 프로젝트 차단이 해제돼도 별도 계획 원인이 남은 ExecPlan은 `PLAN_BLOCKED`를 유지하며, 다른 차단 계획의 fingerprint와 작업 공간은 변경하지 않는다
10. 저장된 `managed_worktree_conflict`와 계획에 연결된 rebase 완료·소유권 오탐은 같은 현재 상태 서비스로 재평가한다. 특정 구형 오류 문구, 과거 Worker 세션·교정 turn·stage 승인·receipt·journal 완전성이나 입력 변경 지문 부재만으로 복구를 거부하지 않는다. 저장 원인을 해당 계획의 rebase intent와 연결할 수 없으면 차단을 유지한다
11. 현재 repository·reservation·등록 worktree·소유 branch·target base·원격 기대값과 rebase metadata가 일치하면 일반 branch 검사 전에 detached rebase를 인식하고 충돌·사용자 stage를 보존한 채 같은 실행을 재개한다. 이미 완료됐으면 Git 단계를 반복하지 않고 결과를 채택한다. 관측을 transaction 안에서 다시 확인하고 대상 원인 해제와 후보 검증 무효화를 함께 기록한다. 다른 활성 실행·planning 또는 미충족 dependency가 있으면 투영·session·진단을 바꾸지 않고 대기한다
12. rebase 원인 해제는 별도 Review 실패·사용자 결정·PR reopen·실제 Agent 범위 위반과 다른 계획·planning·프로젝트 안전 원인을 해제하지 않는다. 원문·실패·알림 감사와 기존 branch·worktree·session을 보존하며 변경된 후보는 Worker의 현재 입력·최종 diff 확인과 targeted test, Manager 전체 verify, Reviewer 및 기존 게시 ownership·lease·PR identity 조건을 다시 통과해야 한다
13. 교정 소진으로 차단된 진행 중 rebase는 저장된 실제 차단 기준선과 현재 입력·선택 worktree를 비교할 수 있고 모두 같으면 차단을 유지해 동일 Worker 실패를 반복하지 않는다. 입력 또는 수동 해결·stage·continue 결과가 바뀌면 현재 상태를 다시 검사한다. 과거 기준선이나 실패 기록의 부재 자체는 복구 거부 조건이 아니다
14. `PLAN_BLOCKED`와 계획과 연결된 `PROJECT_BLOCKED`의 계획별 원격 관측 입력은 다른 활성 계획·PR 충돌 복구·planning publication·유지보수가 변경 실행 자리를 점유하는 동안 보존한다. 자리가 비었거나 차단된 동일 계획이 점유한 때에만 정확한 원격 ref·PR·계획·branch·worktree·작업 범위를 다시 확인하고, 안전한 작업 branch 후속 commit을 먼저 채택한 뒤 최신 base를 관리 worktree에 반영한다. 계획·Requirement 지문이 같아도 base 전진은 rebase 대상으로 처리한다. PR 없는 기존 branch의 완료 결과는 현재 상태 판정과 중간 checkpoint 게시를 먼저 마친 뒤 원래 원인만 재검사한다. 원인이 남으면 이유와 차단을 유지해 기존 checkpoint 경계로 현재 작업물을 게시하며, 같은 원격 HEAD에는 빈 commit·반복 push를 만들지 않는다. 원인이 해소된 원격 복구는 기존 session·branch·worktree와 실패·승격 감사를 보존하고 후보 검증 토큰을 무효화해 Worker 확인·targeted test·전체 verify·Review부터 다시 진행한다. 저장 단계와 실제 Git 결과를 대조해 중단을 복구하며 동일한 처리 입력과 원인의 검사·rebase·알림을 반복하지 않는다. 별도 사용자 결정·PR reopen·소유권·안전 차단과 다른 계획의 상태는 유지한다

15. 프로젝트 차단 중 기존 branch의 게시 identity와 마지막 채택 원격 OID가 확인되면 동일 소유 branch에 관측 OID의 정확한 lease로 checkpoint를 게시한다. fast-forward 또는 PR 없는 검증된 관리 rebase만 허용하며 원래 차단이 남으면 상태·원인을 보존한다. 게시 계보를 증명할 수 없어도 현재 repository·plan·worktree·branch·target·원격 기대값·허용 변경 범위와 rebase 완료를 검증한 작업물은 결정적 진단 branch에 보존한다. 진단 ref는 계획·원본 branch·base·내용 OID에 결합하며 부재 시에만 생성한다. 같은 OID는 재사용하고 다른 OID는 갱신하지 않는다. 원인·ref·OID와 게시 intent·확정 결과는 SQLite에 보존하며 PR 생성·원래 branch 소유권 승격·차단 해제 근거로 사용하지 않는다
16. 충돌 중이거나 현재 상태·작업 범위를 검증하지 못하면 commit·push하지 않고 작업물·session과 실제 local-only 이유를 보존한다. 원격 삭제·재작성·경쟁·권한·branch protection 오류를 예상 밖 ref의 force push로 우회하지 않는다. 과거 rebase turn·stage 승인·receipt·replay·journal 부재 자체는 현재 상태 판정의 거부 사유가 아니다. 진단 ref 생성·checkpoint commit·push·SQLite 확정 사이의 중단은 저장 intent와 실제 OID를 대조해 복구하며, 같은 입력·같은 결과의 rebase·게시·검사·알림을 반복하지 않는다. 알림에는 확인된 ref·OID 또는 실제 local-only 이유와 보존 경로를 포함한다

17. 저장된 scope 차단이 증명된 격리의 무관한 변화, 검증된 Manager 전이의 낡은 기준선 또는 정상 충돌 stage 해소만으로 생겼다면 입력 지문 변경 없이 재평가한다. 실제 Agent 전후 결과·현재 선택 worktree·사용 자원·checkpoint·게시 소유권을 다시 확인하고 SQLite transaction에서 상태와 Git 결과가 그대로일 때 해당 원인만 해제한다. 진행 rebase는 일반 branch 검사보다 먼저 공통 현재 상태 서비스로 판정하며 `allow_rebase=True`나 metadata 존재만으로 detached HEAD를 수용하지 않는다. 작업물·session·retry·승인·실패 감사와 다른 차단을 보존하고 바뀐 후보의 검증·Review를 무효화한다. source freshness·safe fast-forward는 해제 후 독립 gate로 판단하며 프로젝트 안전 gate가 닫힌 동안 일반 Worker·Git 쓰기를 시작하지 않는다

PR 없는 게시 checkpoint의 Review 횟수 차단을 rebase 없이 해제할 때는 checkpoint 요청 identity·게시 commit의 tree와 identity·마지막 채택 원격 OID 및 현재 원격 ref, 동일 계획의 branch·관리 worktree와 로컬 HEAD·ref·index·작업 파일·등록 상태를 확인한다. 실행 checkpoint에 게시 소유권 근거를 보존하고 재시작과 Worker 교정 뒤에도 다시 대조한다. 이 경로는 rebase 완료 표식을 만들지 않으며, 이후 실제 rebase가 필요하면 관리 rebase의 현재 상태 판정과 정확한 원격 lease를 요구한다. 게시 소유권 오탐으로 생긴 프로젝트 차단은 같은 증거를 재검증한 경우에만 해제하고, 남아 있는 Review issue의 교정·검증과 다른 READY 계획의 선택을 계속한다.

표식 없는 게시 checkpoint의 재개에도 같은 현재 소유권 검증을 적용한다. `checkpoint_ownership.publication_receipt`는 현재 게시 영수증 또는 같은 branch·worktree·PR에 결합된 과거 게시 기록을 읽기 전용으로 선택한다. 구형 flat 기록은 현재 요청의 정확한 기대 OID까지 일치해야 하며, identity·tree·commit 계보·실제 origin ref는 기존 `owned_remote_oid`로 별도 검증한다. 충분한 이력이 없거나 원격이 삭제·변경됐으면 수용하지 않는다. 요청 교체는 초기화 전에 이전 게시 영수증을 `publication_history`에 보존하며, 현재 local-only 결과를 과거 게시 성공으로 덮지 않는다.

무 rebase 재개의 현재 검증 결과는 기존 `published_review_resume` 필드를 일반 게시 재개에도 사용해 실행 checkpoint에 CAS로 저장한다. 재활성화·후보 교정·중단 복구는 이 필드를 보존하고 소비할 때 현재 요청 identity·원격을 다시 검사한다. 새 계획 차단은 `blocked_resume`과 해당 게시 재개 표식을 함께 무효화해 새 원인의 재검증을 요구한다. 게시 소유권 오탐 해제는 transaction 안에서 원인·계획·프로젝트·checkpoint generation·게시 영수증·현재 Git·실제 원격과 저장된 Agent 결과를 재검증하고 증명 저장·대상 원인 해제를 함께 확정한다. 별도 계획 차단·실패 피드백·session·감사는 유지하며 rebase 완료 증명은 생성하지 않는다.

원격 관측 기록은 origin identity·정확한 ref·OID·실패 원인·시각을 계획별로 보존한다. 조회 실패는 마지막 확정 OID를 덮지 않는다. 처리 키는 원래 원인·검사 지문·관측 base/work branch OID·로컬 작업 지문을 포함한다. 같은 키의 확정 결과를 재사용하며 관련 입력이 달라지면 재평가한다.

### 로컬 복구 요청의 접수와 실행

`recover`의 별도 runtime은 짧은 SQLite `BEGIN IMMEDIATE` transaction 안에서 `local_environment_recovery_requests`에 범위별 요청을 생성하거나 같은 범위의 pending 요청을 재사용한다. 기존 프로젝트별 단일 pending 인덱스를 프로젝트 전체·계획별 부분 unique 인덱스로 교체해 감사와 소비 기록을 보존한다. 접수 transaction을 commit한 뒤 전역 process lock을 비차단 방식으로 얻고, 선택한 계획의 실행 자리도 확인해 실행 가능할 때만 기존 복구 executor를 호출한다. 잠금·다른 실행 자리로 대기하면 요청을 완료하거나 제한 재검사를 소비하지 않는다.

등록 프로젝트 scheduler는 worker connection에서 DDL 없이 pending 요청을 접수 시각·삽입 순서로 읽고 실행 가능한 첫 요청을 선택한다. 즉시 명령 실행도 같은 선택을 적용해 앞선 실행 가능 요청이 있으면 자기 요청은 대기로 보존한다. 선택한 범위가 실행 가능하면 같은 executor에 저장된 요청 identity·계획 범위와 현재 설정, 공유 Planner mutex·verify coordinator·runtime mutation·단계 알림 경계를 전달한다. 앞선 요청이 다른 실행 소유자 때문에 대기하면 뒤의 실행 가능한 요청을 선택하고, 그런 요청도 없으면 일반 project runner를 호출하며 대기 요청은 보존한다. 프로젝트별 한 번의 scheduler 방문에서는 선택한 요청 하나를 처리한다. executor는 즉시 실행과 지연 실행의 결과·제한 재검사 소비를 동일하게 기록하며 완료한 요청은 다시 처리하지 않는다.

## 공통 verify 실행 경계

`verification/contract.py`가 `scripts/verify.json`의 version·정적 adapter registry·프로젝트 interpreter·수집 옵션·순서 있는 단계를 검증한다. `result_validation.verify`는 선언 부재만 기존 경로로 실행하고 잘못된 선언은 실패로 반환한다. `manager_adapter`는 고정 runtime의 Python·절대 진입점·protocol version·run/session·timeout과 기존 인스턴스 coordinator를 launcher에 전달한다. launcher는 프로젝트 interpreter를 선택한다. standalone도 같은 `entrypoint.py`와 선언을 사용하며 로컬 관측만 조율한다. 시간 상한을 전달하지 않는 구형 조율 호출은 runner·명령·프로세스/Windows Job 대기에서 바깥 Manager의 상한을 상속하며 새 기본 상한을 중첩하지 않는다. 명시적 상한과 단독 실행의 설정·기본 상한은 유지한다.

`runner.py`는 단계 순서를 유지한다. `core.py`는 도구별 결과 validator·worker IPC 경계를 받아 기존 자원 배정·예약·프로세스 시작·재사용·종료 확인을 수행하며 unittest 타입이나 자체 compile 경로를 소유하지 않는다. 수집과 직렬 command도 예약·전체 timeout을 적용한다. `unittest_adapter.py`는 프로젝트 interpreter의 discovery/`load_tests`·재수집·fixture·native reporter와 명시한 Python compile 범위를 소유한다. 자식 import와 Python 경로는 후보 worktree를 선택하며 Manager runtime 패키지가 후보 코드를 대신하지 않는다.

manifest·이벤트·결과는 version·run·단계·단위/테스트 identity를 결합하고 adapter가 예상 범위·fixture skip 근거·결과 완결성을 대조한다. command 종료 코드는 필수 단계 결과이며 테스트별 결과를 합성하지 않는다. 예약은 session·단위·PID/시작 identity에 결합하고 확인된 종료/미시작만 회수한다. 중단 자료와 종료 불명 예약 journal을 보존하고 다른 run은 제어하지 않는다. 단계별 이벤트를 전체 진단에 집계하며 외부 runtime·의존성·동적 입력이 미확인인 연결은 성공 재사용 근거를 제공하지 않는다.

선언·호출·지원 근거와 adapter 적합성/추가 경계는 [VERIFY.md](VERIFY.md)가 소유한다. 역할별 정적 규칙은 기존 common+해당 역할의 고정 원본 묶음으로 전달하고, 작업 prompt는 선택 선언·지원 버전·참조 문맥만 연결한다. 언어별 지침 사본이나 새로운 배포·snapshot 경계는 만들지 않는다.

## 검증과 재시도

- Worker는 구현과 관련 targeted unit test를 작성하고, 수정 범위에 직접 관련된 최소 unit test command만 실행한다
- targeted unit test 실패는 Worker가 같은 세션에서 수정하고 재실행한다. 실행 가능한 관련 unit test가 없는 변경은 사유를 남기며 전체 검증을 생략하는 근거로 사용하지 않는다
- Worker는 현재 운영체제용 verify 스크립트가 없으면 생성하고 테스트 명령이 바뀌면 갱신한다
  - Windows: `scripts/verify.ps1`
  - Linux: `scripts/verify.sh`
- verify 스크립트는 프로젝트 전체 검증을 수행하고 실패 시 0이 아닌 종료 코드를 반환한다
- 자체 Windows verify는 checkout·runtime·상태 DB 위치와 무관한 `%USERPROFILE%\.cache\loop-manager\verify\tmp\`를 생성하고 `TEMP`·`TMP`·`TMPDIR`로 자식 프로세스에 전달한다. AppData 리다이렉션을 피하며 종료 시 기존 환경을 복원하고, 병렬 실행의 파일을 보존하기 위해 공유 임시 폴더 전체를 삭제하지 않는다. Defender 설정은 스크립트가 변경하지 않고 [운영 안내](OPERATIONS.md#windows-defender와-테스트-임시-폴더)에 따라 사용자가 선택한다
- 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check는 verify 스크립트만 실행한다
- 프로그램은 verify 스크립트를 AI Agent 밖에서 실행한다
- 프로그램은 verify 성공 시 Worker를 다시 호출하거나 성공 출력을 전달하지 않는다
- verify 실패 시에만 종료 코드와 필요한 출력 내용을 같은 Worker 세션에 전달한다
- Reviewer는 성공한 Worker targeted unit test와 전체 verify 결과를 넘겨받고 테스트를 다시 실행하지 않는다
- Reviewer는 코드와 테스트의 diff 및 verify 스크립트 변경이 검증 범위를 약화하지 않았는지 읽기 전용으로 확인한다

verify 지문 이력, Review 지문·level 승격 이력과 실행별 Worker 교정 횟수는 별도로 저장한다. 최종 반복·일시 중단·BLOCKED 해제 조건은 PRODUCT의 실패 정책을 따른다. verify 성공은 verify 이력만, Review 성공은 Review 이력만 초기화하고 effective level·승격 이력은 유지한다.

외부 CLI의 실행 실패, timeout과 일시적 서비스 오류는 프로그램 실행당 최대 3회 재시도한다. 모두 실패하면 현재 상태를 유지하고 해당 프로젝트를 건너뛴다. 다음 Loop Manager 실행에서는 다시 최대 3회 시도한다. AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않는다.

### 자체 테스트의 Git 경계

자체 테스트의 전환 목표는 [R-080](requirements/%5BR-080%5D%20Git%20Mock%20Defaults%20for%20Tests.md)에 따라 Git 호출을 mock으로 처리하는 것을 기본으로 삼는 것이다. 실제 Git은 mock으로 확인할 수 없는 Git 동작이나 프로세스 연결 계약을 검증할 때만 사용하며, 해당 테스트에서 이유와 확인할 효과가 드러나야 한다. 통합 테스트라는 이름이나 기존 fixture의 준비 편의는 실제 Git 실행의 근거가 아니다.

| 검증 대상 | Git 경계 |
|---|---|
| 상태 전이·재시도·차단·복구 정책과 입력 조건 조합 | 관측값·명령 결과를 mock으로 제공하고 실제 판단·저장 로직 실행 |
| 명령 인자·출력 파싱·종료 코드·관측 실패 처리 | 명령 경계를 mock으로 처리하고 실제 구성·파싱·오류 처리 실행 |
| 실제 충돌·rebase·index·worktree·원격 lease의 효과 | 해당 Git 계약을 확인하는 필요한 통합 회귀에서 실제 Git 사용 |
| bootstrap의 부모/자식 실행과 exact-SHA runtime 연결 | 실제 연결 계약을 확인하는 필요한 통합 회귀 유지 |

mock은 기존 Git adapter나 관측 경계에 적용하고 `ExecutionFixture`·`CurrentRebaseFixture`·관측값 생성 도구를 재사용한다. SQLite·파일 보존이 검증 대상이면 실제 저장을 유지한다. 정상·오류·중단과 연속 관측 사이의 HEAD·원격 OID 변화를 재현하며, 검증할 판정 함수나 상태 전이를 성공값으로 대체하지 않는다. 전환 시 기존 검증 조건·assertion의 대응과 필수 통합 회귀를 보존하고, 테스트 skip·timeout 확대·수집 범위 축소로 비용 감소를 대신하지 않는다.

실제 Git fixture의 `tests/support/git.py`는 최초 bare/seed init과 후속 명령에 global/system 설정 및 template을 격리한 환경을 전달한다. `XDG_CONFIG_HOME`도 통제 template 경로로 지정해 기본 전역 ignore·attributes를 제외하고 `GIT_ATTR_NOSYSTEM=1`로 system attributes를 차단한다. 실제 adapter·Python 자식까지 실행하는 테스트는 `isolated_git_test`로 setUp부터 cleanup까지 같은 환경을 상속하며 예외 후 호출자 환경을 복원한다. 저장소별 local 설정은 보존하고 상속 검사는 `git_command`의 명시적 `env`에 통제된 설정 파일을 제공한다. 기본 template은 테스트용 `info/exclude`와 실행 hook이 없는 hooks 디렉터리를 제공한다. G-222·G-223 소비 경계의 전환 완료는 이 공통 경계 제공만으로 선언하지 않는다.

bootstrap 회귀의 부모·자식 CLI 계측은 실제 PID·시각과 테스트 이름을 보존해 전체 verify 기록에 포함한다. 부모 CLI 시간은 자식 실행을 포함하므로 둘을 합산해 전체 실행 시간으로 보고하지 않는다. 비용 개선은 같은 기준 코드·계측·병렬도와 테스트 수집 범위의 전후 실행을 비교하며, 준비·Git·snapshot 누적 시간과 전체 경과 시간을 구분한다.

새 테스트나 시나리오 확장 전에는 기존 테스트가 확인하는 계약과 assertion을 먼저 확인한다. 실제 Git·CLI 실행을 추가할 때는 검증할 효과와 기존 mock 또는 통합 회귀로 충족되지 않는 이유를 테스트에 남긴다. 역할·브랜치·오류를 조합할 때는 각 조건이 실제 연결에 영향을 주는지 구분한다. 공통 등록 중단·runtime 재사용·외부 명령 오류 처리를 모든 역할의 전체 실행에 끼워 넣지 않는다. 조건별 판단·저장 결과는 mock으로 검사하고, 조건의 상호작용 때문에 실제 연결이 필요한 사례는 유지한다. bootstrap의 서로 다른 역할별 저장 상태와 실제 자식 dispatch는 실제 프로세스 회귀로 확인한다.

통합 테스트는 확인할 효과에 도달하면 종료한다. 예를 들어 rebase 중단 회귀는 실제 operation·index·session 보존과 같은 복구로의 재진입을 확인한 뒤 정상적인 pause 결과로 멈출 수 있다. 이후의 반복 실패·교정 소진·최종 차단은 그 정책과 실제 효과를 확인하는 기존 전용 테스트에 맡긴다. 실제 재시도 횟수 자체가 검증 대상인 테스트에서는 필요한 반복을 유지한다. 실행을 줄일 때는 원래 조건과 assertion을 유지하거나 계속 검사하는 테스트로 대응시키며, 새 mock이 검증할 판단을 성공값으로 대신하지 않게 한다.

Git 외의 subprocess도 같은 기준을 적용한다. 최초 import의 의존성과 부작용 검사는 모듈별 새 interpreter가 계약이므로 유지하며, 이미 import된 프로세스를 재사용해 검사를 약화하지 않는다. 실제 프로세스 시작·프로세스 간 전달·OS 효과가 대상이 아닌 오류 처리와 조건 조합은 subprocess 경계를 mock으로 처리한다. 새 전역 cache나 별도 runner를 추가하기 전에 기존 fixture와 관측 경계를 재사용한다.

완료된 전체 기준 검증이 있으면 비용 변경의 전후 비교는 영향을 받는 모듈만 같은 병렬도·계측·수집 범위로 실행한다. 필수 연관 회귀가 있으면 그 모듈을 포함하고, 전체 검증과 모듈 비교의 결과를 구분한다. 모듈의 시간 감소를 전체 verify의 감소로 환산하지 않는다. 이 비교 범위는 개발 중 검증에만 적용하며 전체 verify의 수집·실행 범위를 바꾸지 않는다.

실제 Git이 필요한 자체 복구 통합 테스트의 `RecoveryBoundary`는 한 테스트 안의 직렬 변형만 같은 소유 경로에 복원한다. 다른 테스트·병렬 실행은 별도 경로·원격·DB를 소유한다. Review 프로토콜 변형은 실제 Worker·verify를 통과한 Review 진입 직전을 보존하며, 파일/mode·Git 객체/ref/index/config·SQLite/TEMP 상태의 digest와 복원 후 새 Git snapshot을 대조한다. 교정 Worker의 파일 version도 경계에서 이어받아 실제 새 작업물이 생성되게 한다. 호출 경로·준비 입력 불일치는 복원 전에 거부하며 자료 손상·재현 실패도 실패로 보고한다. 이전 receipt를 현재 증거로 사용하지 않는다. `git_fixture:review_*`와 `git_fixture:boundary_*` 단계는 생성·Worker/verify 준비·복사·복원·identity 및 재현 확인·정리를 기록한다. `git_fixture_seconds`는 같은 PID의 유효한 `start`·`duration` 경계에서 완전히 포함된 하위 fixture 시간만 총계에서 제외한다. 개별 phase event와 duration은 보존하며, 별도 프로세스나 중첩 경계가 불완전한 이벤트는 포함 관계를 추정하지 않는다. duration이 없는 이벤트의 시간을 만들지 않는다.

## GitHub와 PR

등록된 Open PR은 매 상태 동기화에서 Merge 가능 여부도 확인한다. 결과가 미확정이면 다음 조회에서 확인하고, Git 충돌이 확정됐으면 관리 worktree에서 최신 원격 base를 rebase해 해소한다. 바뀐 후보의 Worker 확인·필요한 targeted test·전체 verify·읽기 전용 Review 뒤, 같은 PR identity와 원격 head를 확인해 기존 PR branch를 안전한 lease로 갱신한다. 승인·필수 check 대기는 rebase로 해소되는 충돌로 간주하지 않으며 새 PR을 중복 생성하거나 자동 Merge하지 않는다.

- branch, commit, push와 PR 생성은 프로그램 코드가 수행한다
- Planner의 검증된 새 ExecPlan, snapshot에 결합된 PR 전 계획 개정·미착수 계획 rename·삭제와 `docs/FUTURE_IDEAS.md` 변경은 planning commit으로 `origin/<base-branch>`에 fast-forward push하며 별도 PR을 만들지 않는다
- 하나의 ExecPlan에는 하나의 결정적 branch와 하나의 PR만 연결한다
- PR 생성 전 같은 head branch의 Open, Closed 또는 Merged PR을 조회하고 기존 PR이 있으면 SQLite에 연결한다
- PR 생성 직후 SQLite 기록 전에 중단돼도 다음 실행에서 기존 PR을 찾아 복구하며 중복 PR을 만들지 않는다
- 원격 branch는 있지만 PR이 없는 복구 상황에서 rebase로 commit이 바뀌면, SQLite에 저장된 동일한 Loop Manager 소유 branch임을 확인한 경우에만 `--force-with-lease`로 갱신한다
- PR 생성 전 관련 테스트, 전체 검증과 Reviewer가 모두 성공해야 한다
- PR 생성 직전에 최신 원격 기준 branch를 rebase로 반영하고, 기준이 바뀌었다면 전체 검증과 Review를 다시 수행한다

기존 PR identity와 Merge 상태는 GitHub가 원본이다. Closed PR은 같은 PR reopen을 기다리며 프로그램이 새 PR로 대체하거나 자동 Merge하지 않는다. Merge head와 관리 worktree를 대조해 clean·미게시 local commit 부재를 확인한 때만 강제 옵션 없이 정리한다. Merge 방식이나 base ancestry만으로 push 여부를 판단하지 않는다. 불확실한 정리는 cleanup_pending으로 보존하고 이후 안전 조건 충족 또는 경로 부재를 재평가한다.

### MERGED ExecPlan archive

ExecPlan은 GitHub Merge 확인과 SQLite MERGED 전이 뒤에만 별도 정리 대상으로 삼고, 최신 원격 base의 detached 임시 worktree에서 내용 보존 rename-only commit으로 일괄 archive한다

ExecPlan archive는 원본 hash, regular-file source, 비어 있는 destination과 원격 base 불변성을 검증해 fast-forward로 직접 게시하며, 후보별 충돌·원격 경쟁·push 실패는 MERGED 상태와 다른 작업·완료 처리를 막지 않는 archive pending으로 재시도한다

## Telegram 알림

알림 이벤트·payload·차단 해제와 대기·완료의 의미는 PRODUCT의 알림 정책을 따른다. token과 chat ID가 모두 있어야 SQLite outbox에 이벤트를 저장하고 전송한다. 두 값이 모두 없으면 생성·전송하지 않고 일부만 있으면 설정 오류다.

고유 사건 키로 중복을 방지하고 전송 결과·조각·실패 감사와 재시도 정보를 작업 상태와 분리해 보존한다. 실패한 전송만 다음 실행에서 재시도한다. planning 오류 해제와 해당 사건 해제·미전송 조각 취소는 한 transaction으로 반영하며 과거 전송 감사는 유지한다. 새 PRODUCT·Requirement 작업 주기와 내용 보존 archive를 구분한다.

새 사건 선택에서 계획 작성 결과와 단순 단계 실패를 제외하며, 기존 outbox의 해당 사건은 재작성·삭제하지 않고 이어 전송한다. Planner의 실제 시작은 차단 후 재개 판정을 위해 기록하되 일반 시작 알림은 만들지 않는다. Worker 최초 시작은 저장된 해당 계획의 시작·재개 사건으로 중복 제거한다. 유지보수 시작·종료 사건은 기존 cycle identity와 상태 전이를 같은 SQLite transaction에 기록하고 planning 단계 경계에서 commit 뒤 전송한다. 안정적 작업 소진은 draft·계획 차단도 보존하며 프로젝트 차단을 제외한 대기를 일반 알림으로 전달한다.

`notifications/rendering.py`는 기존 payload만으로 텍스트를 결정적으로 생성하며 모델을 호출하지 않는다. 확인된 질문·수정 대상·PR 상태로 정보 순서를 정하고, 원인·복구 시도·Checkpoint 근거는 보존한다. 긴 메시지는 기존 4096자 조각과 저장된 전송 진행률로 누락 없이 이어 보내며 이미 고정된 outbox 텍스트는 다시 구성하지 않는다.

## Repository Safety and Security

- 등록 프로젝트 기본 worktree의 로컬 변경, ahead·diverged 이력과 checkout branch는 원격 SHA 기반 자동 작업을 막지 않는다. 위 fast-forward·clean diverged rebase의 보존 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
- 사용자 파일 삭제, reset, 일반적인 강제 push와 자동 Merge를 수행하지 않는다
- 계획 commit의 base branch 직접 push는 fast-forward만 허용한다. branch protection 등으로 거부되면 우회하지 않고 계획 오류로 보존한다
- 기본 worktree의 Git 쓰기는 위 안전한 fast-forward와 clean diverged 로컬 commit을 보존하는 소유 rebase·abort에 한정한다. dirty rebase·자동 push·reset·stash·clean·강제 checkout은 수행하지 않는다
- 관리 worktree, SQLite, 임시 파일, 로그와 lock은 source checkout 밖의 `<state-dir>`에만 만든다
- 모든 계획과 구현 변경은 Loop Manager가 만든 관리 worktree 안에서만 수행한다
- AI Agent가 접근하고 수정할 수 있는 경로를 해당 관리 worktree로 제한한다
- fetch·worktree 등록/정리는 공유 metadata를 다루며 기본 checkout 파일·index·HEAD 갱신은 위 소유 동기화 경계로 제한한다. 사용자 operation·동시 변경·ignored 경로 충돌은 덮어쓰지 않는다
- 정상 종료한 정확한 Manager 소유 관리 worktree는 권위 있는 미커밋 변경과 미게시 commit이 없음을 확인한 뒤 수명을 종료한다. ignored-only는 정리를 막지 않지만 활성 작업·기본 checkout·불명확한 소유권·사용자 변경을 삭제하는 근거가 아니다. base ancestry나 GitHub Merge 방식으로 게시 여부를 추정하지 않는다
- PR head 정보나 commit 관계를 확인할 수 없으면 worktree를 보존하고 `cleanup_pending`으로 기록한다
- `cleanup_pending` worktree가 이후 clean해지거나 경로가 사라지면 기록을 지우고 강제 옵션 없이 정리한다
- planning worktree·diff·session과 미게시 결과는 오류·게시 복구 동안 보존한다. 폐기·교체는 소유권과 보존 결과를 검증한 관리 경계에서만 수행한다
- 현행 정리는 Merge된 로컬 관리 branch만 비강제 삭제하며 원격 branch는 자동으로 삭제하지 않는다. 확인된 작업 삭제에 따른 소유 로컬·원격 branch 정리는 위 R-055의 구현 목표이며 현재 Merge 정리의 권한을 확대하지 않는다
- 예외적으로 PR이 아직 없는 동일한 Loop Manager 소유 branch의 rebase 복구에만 `--force-with-lease`를 사용할 수 있다
- 공통 파일 복사 스크립트는 사용자가 명시적으로 실행하는 초기 설정 도구이며 자동 Loop에서 호출하지 않는다
- 등록 프로젝트의 문서와 verify 스크립트는 사용자가 신뢰한 입력으로 취급한다
- Telegram token과 다른 자격 증명을 AI prompt, subprocess 인자와 로그에 노출하지 않는다
- subprocess는 인자 배열로 실행하고 필요한 경우에만 운영체제 shell을 사용한다

### 실행별 Agent 범위와 Manager 전이

저장된 시작·Agent 결과·Manager checkpoint와 BLOCKED의 사전 비교는 같은 ignored 비교 view를 사용한다. 시작 시 tracked ignore 규칙의 blob·index·작업 상태 또는 기록된 외부 exclude의 경로·내용 서명이 저장 결과·현재 상태와 일치할 때만 index에 없는 과거 untracked artifact를 비교에서 제외한다. `!` 규칙으로 ignore를 해제한 경로는 삭제된 뒤에도 권위 입력으로 유지한다. 원본 snapshot과 활성 파일은 수정하지 않으며 새로 숨긴 경로, 강제 stage, index flag와 권위 있는 Git 변경은 기존 검사를 유지한다.

관리 rebase의 연속 관측과 현재 상태 수용도 같은 규칙 검증을 재사용한다. 첫 관측의 snapshot을 단계 시작 근거로 삼고, 증명된 ignored·index-free 파일만 비교·scope·수용 fingerprint에서 제외한다. 나머지 파일과 HEAD·index·공유 config·원격 OID·PR·입력의 변화는 기존 불안정 관측 또는 범위 위반으로 처리한다. 이 관측은 Agent 호출의 저장된 시작 범위나 게시 소유권 증명을 대신하지 않는다.

공통 ignored 충돌 helper는 대상 tracked 경로와 대소문자·부모·자식 관계까지 비교한다. rebase 시작뿐 아니라 continue·skip·abort에도 같은 보존 검사를 적용하며, 대소문자를 구분하지 않는 파일시스템에서 ignored 파일을 tracked 경로의 alias로 덮어쓰지 않는다.

동일 입력의 planning_error도 보존된 작업의 소유권·종료 호출·저장 결과를 확인해 재평가한다. 원본 결과가 저장된 scope 오류를 재현하고 ignored 비교 view만 통과하며 결과 이후의 권위 변경이 없을 때만 해당 오류를 해제한다. 차단 identity·현재 Git 상태를 transaction에서 다시 대조하고 session·retry·실패 진단을 보존한다. Planner의 ignored-only PROJECT_BLOCKED 해제는 현재 planning worktree 소유권과 저장 결과 이후의 권위 변경을 검사하며 확정 transaction에서 소유권을 다시 확인한다. ignored-only PLAN_BLOCKED는 기존 실행 격리·Manager 전이 증거를 적용한 결과가 현재 후보와 같을 때 저장한 차단 전 실행 단계와 Worker·verify·Review 결과를 이어간다. 새 verify 호출은 먼저 이전 완료 표식을 무효화하고, 통과한 호출은 HEAD가 동일하고 tracked·staged·non-ignored 미커밋 변경이 없을 때만 scope 검사 전에 후보·저장 출력에 결합한 성공 token을 기록한다. 저장 verify 결과는 validated base와 definition·출력 token이 현재 후보 HEAD와 일치할 때만 완료 증거로 인정해 `REVIEWING`으로 재개하며, 구형 `verify-stage` token은 시작·저장 결과·현재 후보 HEAD가 같고 ignored 비교 view의 결과·현재 상태가 깨끗하며, 해당 계획의 최신 불변 verify 감사가 계획에 저장된 `current_revision_sha`(verify 감사의 정의 commit) 및 성공·저장 출력과 일치할 때만 해제 transaction에서 출력 결합 token으로 전환한다. 원본 snapshot·결과·감사는 보존하고 이후 revision gate·Review·게시에서도 전환한 증거를 사용한다. 그 증명이 불완전하거나 미커밋 변경이 있으면 기존 commit·새 후보 재검증 경계를 적용한다.

현행 Agent 감시 축소는 호출·역할·session·repository·관리 worktree에 결합된 격리 증거를 요구하며 증거 없는 snapshot은 엄격 비교로 남는다. R-055·G-199는 이 중 legacy 기본 checkout 제외, 일반 verify의 실행 대상 검사와 전체 preflight digest gate 제거를 구현하도록 변경한다. 현재 자원·origin·보안 설정·writer·후보 입력은 계속 검사하고 수행하지 않은 과거 격리 성공을 합성하지 않는다. 신규 호출의 provider 격리는 유지한다.

Agent 시작 이후 Manager의 fetch·worktree 전이는 작업별 전후 기대값·소유 표식·checkpoint와 현재 Git 상태가 일치하는 자원만 비교 기준선에 반영한다. 관측 OID, SQLite 완료 상태나 예정된 cleanup만으로 전체 ref·worktree를 갱신하지 않으며, Git 변경 뒤 receipt 저장 전 중단에서도 같은 증거를 읽기 전용으로 확인한다. 원본 scope와 재개 호출의 비교 시작값을 별도로 보존해 후속 Manager 전이가 실제 Agent 위반을 덮지 않게 하며, Agent 허용 변경·session·격리 증거를 유지한다

현재 소유권과 허용 범위가 확인된 관리 rebase의 충돌 stage 1/2/3에서 stage 0으로의 해소(`ls-files -v`의 `M → H` 포함)는 보안 index flags 변경이 아니다. 신규 Worker 결과와 저장된 scope 차단에 같은 판정을 적용하되, `skip-worktree`·`assume-unchanged`의 예상 밖 설정·해제, 범위 밖 파일·index 변경과 소유권 불명확은 계속 차단한다

Codex가 Agent turn 추적을 위해 자동 생성·갱신·정리하는 `refs/codex/turn-diffs/*`는 게시 또는 workspace 소유권에 사용하지 않는 provider 소유의 비권위 임시 ref다. 그 namespace의 순변경만으로 Agent 범위 위반이나 `PROJECT_BLOCKED`를 만들지 않으며, 작업 소유 branch와 사용 중인 원격 ref는 계속 보호한다

Codex는 Manager가 `codex exec`로 직접 호출하며 선택 workspace의 내장 쓰기 제한과 granular 자동 승인 검토를 유지한다. Reviewer는 읽기 전용이고 테스트·verify 재실행 금지는 별도의 역할 지침으로 유지한다. Codex 프로세스 전체를 감싸는 바깥 sandbox와 중첩 sandbox 호환성은 호출 조건이 아니며, 정상 session·로그 저장을 Agent의 workspace 밖 쓰기 권한과 구분한다

## Verify 입력 관측과 생성 요청

`verification/inputs.py`는 version 1 목록과 작업 파일을 관측하고 `manager_adapter.observe_inputs`
경계로 제공한다. POSIX 상대 경로를 대소문자 구분하여 해석하며 `*`·`?`는 한 경로 segment,
독립된 `**` segment는 0개 이상의 segment와 일치한다. 숨김 파일도 포함한다. 절대 경로·
상위 이동·역슬래시·문자 class·중복 key/경로·미지원 버전은 거부하며 literal 누락은 오류,
glob의 빈 결과는 관측 가능한 빈 집합이다. 목록 자체와 실제 파일의 경로·내용·kind·mode,
실제 명령·환경·측정한 runtime·Git/외부 입력 identity를 결합한다. symlink를 따라가지 않으며
확인되지 않은 입력은 재사용할 수 없다. Git 내부·bytecode는 생성물이고 `.venv`는 별도로
측정한 runtime/외부 의존성 identity로 확인한다.

자체 `scripts/verify-inputs.json`은 `loop_manager.py`, `loop_manager_internal/**`, `tests/**`,
`requirements.txt`, `scripts/verify.json`, `scripts/verify.sh`·`verify.ps1`,
`scripts/copy-project-files.sh`·`copy-project-files.ps1`, `run.sh`·`run.ps1`,
`.agents/skills/specification-docs/scripts/next_requirement_id.py`를 고정 목록으로
관측한다. `tests/**`의 등록·profile·프로젝트 준비 검증은 문서와 설정 예제 대신 고정 샘플을
사용한다. 복사 회귀는 저장소의 실제 copy 스크립트 bytes를 샘플 source root에서 실행해 결과
bytes·경로·기존 파일 보존·재실행 거부를 확인한다. 배포되는 조회 도구와 공통 frontmatter 파서는
실제 실행 코드 bytes를 샘플 source에 배치해 배포 결과와 조회 동작을 확인한다. 샘플은 현재 문서 원본에서 자동 생성하거나
동기화하지 않는다. 따라서 `AGENTS.md`, README, docs와 `.agents`의 prose, `config.ini.example`,
setup·ignore·attributes 변경은 자체 verify 입력이 아니다. 이 제외는 해당 원문의 일관성이나
품질을 확인하지 않으며 필수 테스트·전체 compile 또는 실제 verify/copy 스크립트 검증을
축소하지 않는다.

verify 기록은 실행 시 Python의 bytecode write 정책·cache tag·prefix와 프로젝트 Python cache의
전후 개수, 생성·변경·유지·삭제 수를 남기며 snapshot 소요 시간을 별도 phase로 기록한다.
비교는 bytecode 설정이 같은지 표시하고 실제 run 시간과 cache 변화를 함께 제공한다. cache 존재를
실제 재사용·검증 성공이나 속도 향상으로 대신 판단하지 않는다.

목록 밖 파일의 제외 확인은 경로·내용·kind·mode, 실행 context와 목록에 포함된 모든 입력의
경로·내용·kind·mode에 결합한 completeness token으로 제한한다. 검증 스크립트·설정 변경도
기존 제외 확인을 무효화한다. verify 성공은 이 근거를 만들지 않는다. 관측 API는 실행·성공 저장·
재사용 정책과 분리하며, 목록 부재/오류는 기존 전체 verify를 유지한다.

일반 planning의 prompt 준비는 최신 `origin/<base>`의 고정 revision과 실제 목록을 조회한다.
오래된 planning branch는 새 누락 요청을 만들지 않는다. 원격 활성 계획(draft 포함)과 SQLite의
미종료 계획(PR 포함)에 관련 작업이 있으면 요청을 생략한다. 원격 계획 선언 오류로 중복 여부가
불확실하면 입력 목록 생성 요청만 보류하고, 기존 catalog의 오류 격리와 일반·유지보수 planning을
유지한다. Git 조회 실패는 선언 오류로 숨기지 않는다. Manager 요청은 additive SQLite
`verify_input_requests`에 프로젝트·revision·목적·규칙 원문/버전을 보존하며 같은 요청을 재사용한다.
`manager:verify-inputs` 계획은 요청 선언과 정확한 생성 Purpose를 유지해야 한다. Planner 결과와
원격 catalog 등록에서 실제 등록 요청과 대조하며 임의 예약어·제품 목표 혼합을 거부한다.
예약어는 수동 완료 후보를 포함한 문서 조회와 Requirement retirement에서 제외하고 규칙 내용을
source 지문에 결합한다. 최종 전문 확인의 후속 계획도 등록 요청 검증을 통과한 예약 source를
허용하며 활성 명세 전문·구현 근거 및 draft 조건은 유지한다.
`storage/verify_receipts.py`는 additive `verify_receipt_cycles`와 `verify_receipts`에 작업 주기,
generation, 실제 run·입력 관측·출력·결과와 reuse의 원래 run·현재 HEAD·이유를 보존한다.
일반 activation은 새 주기를 만들고 유지보수의 기존 작업 재개는 주기를 보존한다. 구형 활성
작업은 새 주기부터 시작하며 과거 성공 token을 receipt로 합성하지 않는다. 새 실행 intent는
현재 receipt 선택을 CAS로 교체해 실패·중단 뒤 오래된 성공으로 되돌아갈 수 없게 한다.
실행 전후 입력·HEAD·작업 파일과 runtime 소유 상태를 확인하고 성공 receipt와 후보 token을
같은 transaction에서 확정한다. 저장 경쟁·실패는 현재 후보의 재사용 성공으로 처리하지 않는다.
수동 waiver는 실제 run 성공 receipt가 아니며 기존 수동 후보 계약을 유지한다.

`ValidationEffects.observe_verify_inputs`는 실제 명령·유효 환경·runtime·Git/외부 identity를
증명하는 실행 adapter의 관측 경계다. 기본 임의 프로젝트 스크립트 adapter는 해당 동적 입력을
측정하지 못하며 일반 경로는 `runtime_identity=None`으로 관측하므로 전체 verify를 계속 실행한다.
문자열 분석으로 의존성 없음을 추정하거나 단순 tree/SHA cache로 대체하지 않는다.

운영 port가 연결한 `result_validation.observe_verify_inputs`는 Linux에서
`manager_adapter.ISOLATED_COMPILE_SCRIPT`와 전체 bytes가 같은 아래 계약도 지원한다.
프로젝트가 이 명령을 전체 verify로 선택한 경우에만 적용한다. 실제 명령은 PATH에서 확인한
Python 절대 경로로 `-I -S -B -c`와 같은 프로그램을 직접 실행하며 source를 실행하거나
bytecode를 쓰지 않는다. 관측 목록이 제외하는 `.git`·`.venv`·`__pycache__` 아래 source도
컴파일 대상에서 제외한다. 현재 자체 verify 스크립트와 필수 검증은 그대로 유지한다.

```bash
#!/usr/bin/env bash
# loop-manager: isolated-compile-v1
set -eu
exec python3 -I -S -B -c 'from pathlib import Path; root = Path("src"); assert root.is_dir(); [compile(path.read_text(encoding="utf-8"), str(path), "exec") for path in root.rglob("*.py") if not {".git", ".venv", "__pycache__"}.intersection(path.relative_to(root).parts)]'
```

이 고정 계약은 프로젝트 import·site package·Git·네트워크를 사용하지 않는다. 별도 관측
subprocess가 같은 격리 Python의 executable·sys.path runtime 파일·stdlib·실제 native library
mapping의 bytes와 mode, Python flags·filesystem encoding·사용자·OS identity를 매번 측정한다.
관측 프로그램 자체·실행 환경·cwd도 지문에 결합한다. manifest는 script와 src 입력을 모두
포함해야 하고 입력 누락·symlink·관측 중 변경은 기존 불확실성 계약으로 거부한다. native loader
hook, runtime 측정 불가/timeout, Linux 이외 환경 및 자체 runner의 추가 조율 환경은 unknown이다.
임의 스크립트의 의미를 이 계약으로 추정하지 않으며 새 공통 runner를 필수로 요구하지 않는다.

증명된 관측만 같은 주기의 실제
성공 run과 비교하며 reuse 직전 transaction에서 다시 관측한다. Review·게시 revision gate도
재사용 후보의 입력을 재관측하고 달라졌거나 관측할 수 없으면 VERIFYING으로 돌아간다.
reuse는 verify 프로세스·validation 실행 감사·사용량을 새로 만들거나 실패 이력을 초기화하지
않는다. Reviewer 승인과 최종 publication token·원격 lease는 계속 별도로 판단한다.

## Architecture Change Policy

다음 변경은 ExecPlan을 BLOCKED로 만들고 사용자 승인을 받은 뒤 진행한다.

- SQLite schema 또는 상태 모델의 비호환 변경
- worktree, branch, commit, push와 PR 생성 경계 변경
- 인증 또는 비밀값 저장 방식 변경
- 새로운 외부 infrastructure나 비용 발생 서비스 도입
- 데이터 삭제 또는 비가역 migration
- 같은 프로젝트의 병렬 실행 도입
