# Architecture

## Overview

Loop Manager는 Windows·Linux·macOS에서 외부 스케줄러가 반복 실행하는 Python 3 프로그램이다. Manager가 문서·GitHub 상태·실행 근거를 확인하고 Agent CLI에 판단·구현·Review를 맡기며, Git·검증·게시·상태 전이는 프로그램 코드가 수행한다.

프로젝트별 조율 스레드와 작업별 SQLite 연결·실행 문맥을 분리한다. 전체 Worker·Reviewer 호출은 `max_workers`, verify 호출은 별도 `verify_concurrency`로 제한하고 한 자동 실행의 Planner는 공유 mutex로 직렬화한다. 안정된 PR Merge 대기는 자리를 비울 수 있으며 수동 공유 작업 공간은 [실행 자리와 유지보수](#실행-자리와-유지보수)의 별도 소유 규칙을 따른다.

### 구현 모듈과 의존성 조립

| 경계 | 책임·채택 이유 |
|---|---|
| `loop_manager.py`·bootstrap | CLI 입력을 해석하고 원격 revision·lock·runtime을 선택해 실행 기준을 고정한다. |
| 기능별 서비스·effect 계약 | Agent·Git·Planning·Execution·Recovery 등의 판단과 실행 효과를 각 소유 패키지에 둔다. 서비스는 필요한 effect 계약만 받아 실행 경계를 독립적으로 확인할 수 있다. |
| 패키지별 `bindings.py`·`application/composition.py` | 각 `register()`의 선언을 `create_application(root)`에서 조립하고 누락·중복·호출 불가 의존성을 실행 전에 거부한다. 조립용 이름 조회는 이 계층에 한정한다. |
| `application/legacy_exports.py`·`application/compatibility.py` | 기존 공개 타입·모듈 별칭·순수 helper·함수 시그니처와 클래스 method·호출 시점 함수 교체의 호환성을 유지한다. 내부 서비스에 CLI namespace를 전달하지 않는다. |

callback은 실제 호출 시 연결하므로 등록 순서·상호 참조가 실행 효과를 만들지 않는다. 내부 import·조립은 DB·Git·Agent 프로세스를 시작하지 않으며 독립 구성은 자체 resource root·override를 사용해 CLI를 import하지 않고 만들 수 있다. 새 서비스는 소유 패키지에 등록하고 진입점에 개별 callback을 늘리지 않는다.

## 문서 경계와 책임

| 기준 | 담당 내용 |
|---|---|
| [README.md](../README.md)·[운영 안내](OPERATIONS.md) | 설치·실행, 상세 설정·수동 제출·장애 대응 |
| [PRODUCT.md](PRODUCT.md) | 현재 제품이 보장하는 동작·정책·상태·예외 |
| 활성 Requirement | 현재 기준에서 바꿀 목표와 Acceptance Criteria. 미구현 목표를 현재 동작으로 적용하지 않는다. |
| 이 문서 | 채택한 책임 경계·정보 원본·호환 계약·저장·격리·검증·복구 방식 |
| ExecPlan·[PLANS.md](PLANS.md) | 구현·검증 작업과 그 공통 형식·메타데이터 |
| [Specification Skill](../.agents/skills/specification-docs/SKILL.md) | 독립적인 제품 문서 작성·검토 도구와 작성 기준 |

상태의 의미·허용 정책은 PRODUCT, 그 적용·보존 계약은 이 문서의 해당 절을 원본으로 삼는다. R-055·R-082 등 남은 변경 목표와 현재 구현의 구분은 [활성 변경 요구사항과 현재 동작](PRODUCT.md#활성-변경-요구사항과-현재-동작)에 연결하며 Requirement만으로 현재 실행 권한·gate를 바꾸지 않는다.

Agent 접근 권한은 sandbox에서 강제하며 사후 Git 차이를 권한 위반으로 사용하는 guard는 두지 않는다. 역할별 입력 동일성의 증거가 없는 경우의 재검증·최신 원격 게시 gate는 [성공 receipt와 수동 면제](#성공-receipt와-수동-면제)의 현재 계약을 따른다.

[보관된 R-063](requirements/archive/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md)은 규칙 기반 처리의 Manager 이관과 모델 호출 축소의 설계 배경이다. 후보 성공 결과 재사용의 변경 배경은 [R-072](requirements/%5BR-072%5D%20%EC%9E%85%EB%A0%A5%20%EB%8F%99%EC%9D%BC%EC%84%B1%EC%9D%B4%20%EC%A6%9D%EB%AA%85%EB%90%9C%20%EA%B5%AC%ED%98%84%20%ED%9B%84%EB%B3%B4%20%EC%84%B1%EA%B3%B5%20%EA%B2%B0%EA%B3%BC%20%EC%9E%AC%EC%82%AC%EC%9A%A9.md)에 보존하고 현재 동작의 원본과 구분한다.

## 실행 환경과 시작

한 번의 실행은 처리 가능한 작업을 진행한 뒤 종료한다. cron·Windows 작업 스케줄러가 다음 실행을 시작하며 계속 실행 중인 daemon을 전제로 하지 않는다.

### 실행 전제

Python 3 venv·Git·gh(GitHub CLI)와 선택 provider의 설치·인증·저장소 접근 권한이 필요하다. Windows는 PowerShell, Linux·macOS는 Bash 진입점을 제공한다. 현재 Codex 호출 경계는 CLI 0.159.3 이상을 요구하며 Windows의 Codex 비공개 runtime은 Python 3.13 이상과 준비된 elevated sandbox를 요구한다. 공급자별 준비·거부 조건은 [인증과 실행 조건](OPERATIONS.md#인증과-실행-조건)을 따른다.

### bootstrap과 runtime 경계

| 위치·경계 | 사용하는 원본·보존 규칙 |
|---|---|
| source checkout | launcher·bootstrap·사용자 운영 설정과 선택 Python interpreter의 기준이다. `run.sh`·`run.ps1`은 Git을 실행하지 않고 Python bootstrap만 시작한다. |
| detached runtime | 부모 process lock을 얻은 뒤 등록 self base(미등록이면 main)의 `origin/<base-branch>`를 fetch해 exact SHA로 선택한다. source와 object DB를 공유하되 사용자 checkout의 파일·index·HEAD·local-only commit을 실행 원본으로 사용하지 않는다. push된 새 SHA는 다음 실행에서 선택한다. |
| 상태 디렉터리 | SQLite·관리 worktree·실행 기록·lock을 보관한다. 같은 상태 루트의 자동 실행과 실제 복구 실행은 `<state-dir>/loop-manager.lock`으로 직렬화한다. |
| 수동 준비·복구 요청 접수 | command별 부모 lock·고유 runtime과 자원 잠금으로 자동 실행과 분리한다. 요청 접수는 실제 복구 실행과 구분한다. |

부모는 다른 Git 작업 전에 process lock을 얻고 runtime 자식이 끝날 때까지 유지한다. bootstrap은 기본 `config/config.ini` 또는 명시한 `--config`를 source 기준 절대 경로로 확정해 전달하고 자식의 runtime `ROOT`에서 다시 계산하지 않는다. 설정을 runtime에 복사·이동·symlink하지 않으며 launcher가 선택한 interpreter도 같은 절대 경로로 전달한다.

Linux source 진입은 `loop_manager_internal/config/execution_path.py`의 정리 규칙으로 source의 `config/execution-path.txt`·현재 PATH·존재하는 기본 시스템 후보를 순서대로 합쳐 첫 외부 명령 전에 적용한다. setup은 같은 규칙으로 Git·gh·최소 한 provider 탐색을 확인하고 캐시를 원자적으로 갱신한다. 자식은 확정된 PATH만 상속하며 캐시·source의 다른 ignored 파일을 복사하거나 다시 읽지 않는다.

PATH 복구 안내는 Linux bootstrap의 Git 실행 파일 부재, 등록·Agent 사전 검사의 확인된 탐색 실패와 provider 출력의 명시적인 명령 누락 표식에만 붙인다. 등록·사전 검사는 같은 source checkout에서 setup 재실행을 안내하고, provider 출력은 기존 비밀값 제거 뒤 원인과 종료 결과를 보존한다. 일반 명령·session·인증·권한 실패에는 안내를 붙이지 않는다. 이 경계는 PATH·캐시를 바꾸거나 인증·provider 실행을 시작하지 않는다.

### 설정과 DB 준비 실패

| 검사·실패 | 처리 |
|---|---|
| 운영 INI 부재·형식/값 오류 | 설정 방법을 안내하고 해당 실행·상태 변경을 시작하지 않는다. 수동·복구 요청의 접수와 실행 분리는 각 명령 계약을 따른다. |
| Linux PATH 캐시 부재 | 현재 환경과 기본 시스템 후보로 명령을 탐색한다. |
| PATH 캐시 손상·읽기 실패 | 설정 오류로 중단하고 정상 터미널의 setup 재실행을 안내한다. |
| 최초 DB 준비 | 사용자가 명시한 `config init`에서만 생성한다. |
| 일반 Loop의 DB 부재·손상 | 자동 초기화·전체 상태 재구성 없이 복원 또는 명시적 재설정을 안내하고 중단한다. 정상 DB의 개별 작업 행 보완과 구분한다. |
| 선택적 bytecode cache 준비 실패 | 검증되지 않은 cache를 피하고 원본 실행을 계속한다. 필수 설정·상태 오류로 확대하지 않는다. |

## 설정

Python 표준 라이브러리 `configparser`로 source에서 확정한 운영 INI 경로를 읽는다. source의 `config/` 미추적 로컬 파일은 Git 제외 대상이며 `config/config.ini.example`은 계속 추적한다. 예시는 운영 값의 원본이나 필수 profile의 누락 fallback이 아니다.

| 설정 | 타입·기본값·의미 |
|---|---|
| `[loop].max_workers` | 양의 정수, 기본 3. 전체 Worker·Reviewer 실제 호출 수의 상한 |
| `[loop].max_tasks_per_project` | 양의 정수, 기본 1. DB 저장값이 0인 프로젝트의 관리 대상 상한 |
| `[loop].verify_concurrency` | 양의 정수, 기본 1. 전체 verify 실제 호출 수의 상한 |
| `[loop].refactoring_review_interval` | 0 이상 정수, 기본 20. 정상 종료/최초 실행 기준 이후 Merge 주기이며 0은 자동 유지보수 전환만 비활성화 |
| `[loop].verify_timeout_seconds` | 양의 정수 초, 기본 600. 확인된 순번 대기를 제외한 verify 전체 실제 실행 시간 상한 |
| `[loop].verify_parallelism` | `auto` 또는 양의 정수, 기본 `auto`. 1은 직렬, 나머지 정수는 실행량 상한 |
| `[loop].verify_partial` | boolean, 기본 `true`. 자체 unittest의 부분 검증·일반 선행 Review 경로 사용 여부 |
| `[<provider>].fast_mode` | boolean 또는 공란, 기본 `false`. 선택 provider의 Fast 요청 |
| `[<provider>.roles]` | `Model alias,Effort`. 일반 Planner·Reviewer와 호환용 `planner_check` 등의 profile. profile 존재만으로 새 호출을 시작하지 않는다. |
| `[<provider>.worker]`·`[<provider>.models]` | level 1~8의 명시적 `Model alias,Effort`와 실제 Model 이름. 필수 값·지원 조합 검증은 [Model과 Effort](#model과-effort)를 따른다. |
| `[telegram].token`·`[telegram].chat_id` | 문자열. 모두 비면 비활성화, 일부만 있으면 설정 오류 |

### Fast 설정의 수명과 provider 적용

Fast 선택은 Manager 실행 시작의 설정 snapshot에 보관하고 병렬 프로젝트 worker와 수동·복구 호출 문맥에 결합한다. provider section·항목 생략 또는 공란·공백은 `false`다. 설정 파일 없이 profile을 직접 지정한 단독 호출에도 같은 기본값을 적용하며 `[DEFAULT]`의 값은 사용하지 않는다. 선택 provider만 검증해 미선택 provider의 잘못된 값이 독립 프로젝트를 막지 않게 한다.

같은 실행의 신규·재개·재시도는 snapshot을 사용하고 다음 실행에서 다시 읽는다. 실행 문맥이 끝나면 해제하며 Fast만 바꿔도 session·작업물·검증·Worker 승격 이력을 초기화하지 않는다.

provider adapter는 모델·Effort·지침·권한·sandbox를 보존하며 Fast on/off를 명시한다. off는 CLI 기본값·저장 세션보다 우선하도록 Codex의 일반 service tier·비활성화 override, Claude의 `fastMode: false`로 전달한다. Manager는 별도 Fast 지원 모델 목록·계정 조회·추가 gate·우회 실행을 관리하지 않으며 실제 적용·일반 속도 대체·CLI 오류를 공급자 결과로 처리한다. 로그의 on/off는 요청값이며 실제 적용 성공을 합성하지 않는다.

### 운영 설정과 runtime 캐시

| 입력 | 해석·적용 시점 |
|---|---|
| 설정 경로·interpreter·PATH | source의 bootstrap이 확정해 runtime 자식에 전달한다. runtime마다 운영 설정을 복제하지 않는다. |
| 전역 운영 설정 | runtime 실행 시작에 `ManagedConfig`로 읽어 동시 실행·유지보수·verify·Telegram 실행 문맥에 전달한다. |
| Fast 요청 | 실행 시작 snapshot을 신규·재개·재시도에 유지한다. |
| Model·Effort profile | 역할·Worker level의 호출 준비에서 운영 INI를 해석·검증한다. 실행 시작 설정 전체가 영구 고정된 것으로 취급하지 않으며 진행 중 Worker 세션 전환은 저장 target을 우선한다. |
| 공통·역할별 지침 | [호출별 적용](#지침-원본과-호출별-적용)의 원본 묶음·전달 해시를 따른다. 설정 profile과 수명을 혼동하지 않는다. |

운영 INI는 현재 사용자만 읽을 수 있도록 권장하며 비밀값을 자식 명령행·로그·AI prompt·오류 메시지에 포함하지 않는다. ignored 상태 자체를 runtime 전달 근거로 삼지 않는다. `.venv`는 선택 interpreter 경로로 재사용하고 source의 `__pycache__`·도구 cache 같은 파생 파일은 전달하지 않는다.

기본 runtime bytecode cache는 disposable worktree 밖 상태 경로에서 repository·exact revision·interpreter별로 분리한다. 추적 Python 원본의 checked-hash `.pyc`를 준비해 같은 revision의 worktree를 다시 만들어도 수정 시각에 의존하지 않고 재사용한다. hash·interpreter tag·revision 불일치나 쓰기 실패는 안전하게 재생성·임시 prefix·원본 실행으로 처리한다. 명시적 `-B`·`PYTHONDONTWRITEBYTECODE`·`PYTHONPYCACHEPREFIX`는 유지한다.

cache의 생성·재사용·대체·사용 불가 개수와 소요 시간을 기록하되 runtime revision·source·검증·Git 소유권의 증거로 사용하지 않는다. 다른 ignored 파일을 필수 운영 입력으로 추가하려면 파일별 외부 경로·소유권·권한·전달 방식을 먼저 정의하며 일반 복사·runtime clean 검증 예외를 추가하지 않는다.

### 상태 디렉터리와 백업

상태 루트는 `--state-dir` → `LOOP_MANAGER_STATE_DIR` → 운영체제 기본 경로 순으로 선택하고 사용자 홈 확장·절대 경로 해석을 적용한다. 지정 경로에도 기존 격리·소유권 조건을 적용한다.

- Windows 기본값: `%LOCALAPPDATA%\loop-manager\`
- Linux·macOS 기본값: `${XDG_STATE_HOME:-~/.local/state}/loop-manager/`

SQLite·manager runtime·프로젝트 관리 worktree·로그·관리 임시 파일·process lock은 source 밖 상태 루트에 둔다. 선택적 bytecode 임시 대체와 verify의 임시 경로는 각 cache·[공통 verify 실행 경계](#공통-verify-실행-경계)의 계약을 따른다. SQLite 전체 손실·손상은 자동 재구성하지 않으며 상태 디렉터리 백업 복원 또는 명시적 재설정·재등록이 필요하다.

## 프로젝트 등록

최상위 `add`·`remove`가 등록 검증·해제 서비스와 registry gate를 호출한다. `config init`·`config list`는 유지하고 `config add/remove` parser 별칭은 제공하지 않는다. 등록·runtime 지침 원본 검사는 프로그램 코드가 수행한다.

| 단계 | 검사·결과 |
|---|---|
| 저장소·원격 선택 | 대상이 Git 프로젝트이고 origin이 GitHub인지 확인한다. 기준 branch와 선택 provider를 검증해 등록한다. |
| 고정 원격 문서 | 같은 fetch revision의 `docs/PLANS.md`·`docs/ARCHITECTURE.md`와 PRODUCT 또는 활성 Requirement를 확인한다. 로컬 미게시 문서로 대신하지 않는다. |
| PLANS 호환 | 원격 bytes의 CRLF를 LF로 통일해 기준 파일의 정규화 UTF-8 내용과 비교한다. 불일치는 등록 거부이며 검사는 자동 Loop에서 반복하지 않는다. |
| 초기 설계 유효성 | ARCHITECTURE와 존재하는 PRODUCT가 기준 템플릿 그대로면 등록을 거부한다. 활성 Requirement가 있어도 PRODUCT 검사를 면제하지 않는다. |
| 선택 실행 | `run [--skip-if-running] [repo...]`의 경로를 정규화·중복 제거하고 scheduler 전에 전체 등록 여부를 확인한다. 하나라도 미등록이면 invocation reconciliation·프로젝트 처리를 시작하지 않는다. |
| 등록 해제 | PRODUCT의 비종료 작업·프로젝트 차단·planning 오류 gate를 검사하고 registry만 변경한다. 프로젝트 파일·branch·worktree·PR을 삭제하지 않는다. |

run의 상대 저장소 경로는 부모 CLI의 현재 작업 디렉터리에서 절대화해 공백을 보존한 채 자식에 전달한다. scheduler는 검증된 등록 row만 받아 선택 범위에도 호출·관리 한도와 self-merge 사전 확인·프로젝트별 독립 실행·실패 집계 경계를 적용한다.

## 문서 입력과 ExecPlan 파싱

### 원격 원본과 source 해석

프로젝트 문서·계획의 자동 입력은 fetch로 확인한 `origin/<base-branch>`의 특정 commit에서 읽는다. 등록 프로젝트의 기본 checkout에만 있는 파일·수정·commit은 사용자가 push하기 전에는 입력에 포함하지 않는다. 진행 작업은 이 원격 기준에서 만든 관리 worktree의 결과를 보존하며, 선택된 원격 작업 branch의 사용자 수정은 검증 후 반영한다.

ExecPlan의 `Progress` 체크 항목과 본문은 구현 내용·판단·검증 기록이다. 체크가 끝났다는 이유로 `READY`, `PLAN_BLOCKED`, `MERGED` 같은 실행 상태를 바꾸지 않으며, 단계·재시도·session은 SQLite, 실제 PR Merge는 GitHub 확인을 기준으로 한다. 사용자 본문 변경과 `User Decision`은 질문 해소 판단의 입력이며, 실행 상태는 출처·근거·질문별 해소와 게시를 검증한 Manager만 바꾼다.

현재 활성 Requirement 경로는 Planner 입력과 판단·이동의 경로 key이며 `docs/requirements/` 바로 아래 문서만 새 계획 입력이다. Manager는 안전한 base 게시 경계에서 같은 R ID의 유일한 활성 Requirement로 확인한 파일명 변경을 비보관·비draft ExecPlan의 `sources`에 반영한다. 본문과 다른 선언은 보존하고 게시 확인 뒤 catalog를 갱신한다. 보관·draft 및 아직 정합화하지 않은 기존 ExecPlan은 조회 호환을 유지한다. Manager는 원격 revision과 worktree에서 같은 Requirement identity helper를 사용해 기록된 활성 경로, 기존 작업에 허용된 같은 파일명의 archive, source 파일명에서 확인한 R ID의 유일한 문서를 순서대로 조회한다.

정확한 경로가 있으면 중복 ID여도 해당 문서를 유지한다. ID 후보가 없거나 여러 개이거나 후보를 읽고 식별할 수 없으면 누락·모호·조회 실패를 구분해 source 관계를 확정하지 않는다. 새 ExecPlan은 정확한 활성 source 경로만 사용할 수 있다. archive는 기존 ExecPlan source의 조회·검증 호환성에만 사용하며 재평가하거나 활성 경로로 자동 복원하지 않는다. FUTURE_IDEAS는 계획 입력·실행 상태가 아니다.

source 해석은 catalog 검증·문서 읽기·입력 지문·계획 관련성·단계별 문서 변경·Requirement 판단 및 이동에서 같은 resolver를 사용한다. 지문은 기존 source 문자열을 key로 두고 resolver가 찾은 현재 문서 내용을 포함한다. 따라서 경로 변경만으로 기존 작업 관계가 사라지지 않으며, 내용 변경은 일반 입력 변경·재사용 조건에 반영된다.

### Skill과 Planner의 Requirement 탐색

Specification Skill은 Loop Manager의 내부 모듈·DB·등록 설정·실행 상태 없이 동작하는 독립 문서 도구다. Skill의 `scripts/requirement_documents.py`가 자체 문서 식별·Summary 해석과 로컬 파일 조회를 제공하며 Manager의 catalog parser와 별도로 관리한다. 두 도구는 문서 계약의 의미를 호환되게 유지하고 서로의 스크립트를 호출·import하지 않는다. `copy-project-files`는 Skill 자체의 보조 파일을 복사하며 Manager 내부 parser를 보충하지 않는다.

Skill의 `scripts/next_requirement_id.py`는 프로젝트 루트의 활성 Requirement와 선택적 archive에서 원본 ID 최댓값 다음 번호를 출력한다. 활성 디렉터리 누락, 심볼릭 링크·일반 파일이 아닌 문서, 파일 접근·UTF-8·식별 오류는 실패하며 archive 디렉터리 부재는 빈 집합이다. 제한된 frontmatter의 `id`·`title`을 우선하고 frontmatter가 없으면 첫 `# [R-ID] 제목`으로 식별하며 파일명은 원본 판단에 쓰지 않는다.

Skill의 `scripts/read_requirements.py`는 현재 작업 폴더의 미커밋·미추적 문서를 포함해 조회하고 다음 JSON을 반환한다.

| 필드 | 타입·의미 |
|---|---|
| `has_active` | boolean. 활성 위치에 문서 파일이 있는지 표시 |
| `has_archive` | boolean. archive 위치에 문서 파일이 있는지 표시. 본문은 읽지 않음 |
| `requirements` | 활성 문서 객체의 배열 |
| `requirements[].title` | 원본 문서 제목 문자열 |
| `requirements[].path` | 프로젝트 상대 경로 문자열 |
| `requirements[].summary` | 선택적 문자열. Summary가 없거나 비어 있으면 필드 생략 |

없는 디렉터리·빈 디렉터리는 문서 부재다. 조회·UTF-8·식별 오류는 정상적인 부재로 바꾸지 않는다. 두 명령은 읽기 전용이며 Git·DB·네트워크·Agent 실행에 의존하지 않는다.

일반 planning의 `planning/requirement_overview.py`는 고정된 검토 revision의 Git tree와 blob에서 모든 활성 Requirement를 읽는다. `revision`과 `requirements` 배열을 prompt에 제공하며 제목·경로·선택적 요약의 의미는 Skill과 호환되게 구현한다. archive·템플릿은 제외하고 일반 파일이 아닌 입력·UTF-8·식별 오류와 조회 실패는 오류로 남긴다.

일반 계획을 포함한 유지보수 호출에도 연결하고, 문서 draft 전용·유지보수 전용 호출의 범위는 유지한다. 로컬 수정이나 다른 revision의 요약을 섞지 않는다.

두 도구의 Summary는 코드 블록 밖의 첫 `## Summary`부터 다음 동급·상위 제목 전까지의 내용이다. 빈 요약은 생략하고 다른 절의 내용으로 추론·대체하지 않는다. 추가 모델 호출·요약 생성·파일 변경 없이 탐색 목록을 만들며, 관련 문서의 본문과 Acceptance Criteria가 판단의 원본이다. Writer는 최초 Skill 작성과 Worker의 후속 설계 갱신에서 같은 가독성·필수 계약 보존 기준을 적용한다. 자동 역할의 구체적인 기준은 공통·Worker 지침 원본에 두고 역할별로 합성한다.

### Requirement 교정과 완료 보관

Requirement 교정 계산은 한 원격 tree의 활성·archive 문서, 게시·보관·draft ExecPlan과 저장된 작업 관계의 source 관측, 기본 branch 반영 순서를 입력으로 받는다. 기존 identity helper와 source resolver로 선택·편집 결과와 미확인 사유를 산출하며 Git·DB·Agent 효과는 수행하지 않는다. source 관측 실패는 참조 부재로 바꾸지 않고, 선택된 각 rename 뒤에도 원래 source가 같은 문서로 유일하게 해석되는지 확인한다.

편집은 활성 Requirement의 식별 표현·확정 상대 링크와 비보관·비draft ExecPlan의 확정된 활성 source 경로만 대상으로 한다. ExecPlan 본문·다른 선언과 archive·`User Decision`은 보존한다. 코드 예제는 링크 편집에서 제외하며, 중첩 blockquote와 목록의 접두어·연속 들여쓰기·컨테이너 종료를 확인해 fenced·들여쓴 코드의 원문도 보존한다. 코드 내부의 목록 표식은 새 컨테이너로 해석하지 않으며 코드 종료 뒤 일반 링크 교정을 계속한다. 파일 적용·원격 재검사·게시와 중단 복구는 별도 Manager 경계가 소유한다. 교정 관측과 archive 진입의 journal gate는 [공통 자원 판단](#미확정-git-작업의-단계별-자원-판단)을 사용하며 기존 writer/source·draft·게시 예약과 개별 archive 후보 보호는 유지한다.

Requirement 최종 판단은 source로 연결된 비종료 계획·draft와 수동 제출이 없는 시점에 일반 Planner가 담당한다. 관련 계획이 있으면 모두 실제 MERGED여야 하며 연결 계획 없는 직접 구현도 코드·검증·관련 제품·설계 문서로 판단한다. Manager는 유효한 기준별 충족 판단과 입력 지문·source 관계를 확인한 원문만 내용 보존 이동으로 게시한다. 미충족이면 후속 ExecPlan을 연결하며, 원격 확인 전 입력 변경에 따른 이동 철회와 원격 확인 후 archive를 구분한다.

과거 Requirement 감사 row와 저장된 복원 manifest 필드는 읽기 호환성과 이력 보존을 위해 유지한다. 새 감사·복원 판단과 완료 대기를 만들지 않으며 복원 manifest는 새 게시 전 거부한다. 미게시 Manager 복원 영수증은 해당 worktree에서 원문·원격 archive·경로 소유 범위를 확인해 archive로 철회하고 활성 입력을 재평가한다. index reset 중 중단도 같은 영수증으로 재개하고 사용자 수정·충돌·게시된 후보는 덮어쓰지 않는다.

### 문서 수정본과 ExecPlan 형식

**Manager 문서 수정본 결과.**

Manager의 `document-drafts` 문서 수정본 결과는 application의 `tagged_json`으로 `DOCUMENT_DRAFT_RESULT` 표식과 JSON 형식을 해석한다. 공통 경계는 들여쓰기·표식 표기·다중 줄 JSON과 중복 표식·중복 key·잘못된 JSON을 처리하고, 문서 모듈은 다음 필드 집합·타입·의미를 검증한다. 이 결과 계약은 독립 Specification Skill의 작성 절차와 구분한다.

| `status` | 허용 필드와 처리 |
|---|---|
| `candidate` | `status`·`content`·`meaning_evidence`만 허용하며 모두 문자열이다. 제안한 UTF-8 문서 내용과 의미 보존 근거를 기존 수정본 저장 경계로 전달한다. |
| `uncertain` | `status`·`reason`만 허용한다. `reason`은 공백뿐이지 않은 문자열이어야 하며 수정본 후보나 사용자 결정을 만들지 않는다. |

후보의 내용 문자열은 해석 과정에서 정규화하지 않으며 전체 JSON의 2,100,000 UTF-8 byte 제한과 기존 저장 bytes/hash 계약을 유지한다. 형식 오류는 `DocumentDraftError` 경계로 전달되어 후보 저장이나 사용자 결정을 만들지 않고 기존 수정본·결정 상태를 보존한다.

**승인 수정본과 기존 작업 전달.**

승인 수정본은 독립 Planner 호출이나 새 ExecPlan 생성 대상이 아니다. 다음 조건을 모두 확인할 때만 정상 실행 선택으로 이미 시작한 기존 자동 Worker에게 artifact를 전달한다: 승인 당시의 source 관계가 한 개의 ExecPlan ID·sources로 현재 작업과 정확히 일치하고, 저장된 현재 선언의 path·sources와 repository가 같으며, 현재 단계가 구현 중이고 수동 소유나 PR 연결이 없다. 일반 작업 전달 대상은 Worker checkpoint 허용 범위에 있는 PRODUCT·ARCHITECTURE 문서다. Requirement 대상은 일반 Worker 권한 밖이므로 bind·bytes 적용 전에 `publication_error`에 미적용 이유를 기록하고 승인 artifact를 보존한다. 승인 시 source 관계가 없거나 여러 계획을 가리키면 `publication_error`에 미전달 이유를 기록하며, stale 원문·변경된 source·소유권 불명·수동 소유·PR·자동 실행 대상이 아닌 상태도 작업에 매핑하지 않고 승인 artifact를 보존한다. 이 보류는 독립 작업 선택과 별도 목적의 Planner 판단을 막지 않는다.

`plan_id`의 기존 단일 연결과 compare-and-set을 재사용해 한 승인 artifact를 한 작업에만 결합한다. 연결 뒤에도 승인 snapshot에 포함된 계획의 source 관계를 원격 catalog와 다시 대조한다. source snapshot에 없는 계획을 제외하는 예외는 승인 뒤 생성된 legacy 문서 전용 계획에만 적용해 게시 복구를 유지한다. Worker에는 원본·제안·의미 근거·전체 diff를 입력으로 전달하고 대상 문서 직접 수정은 금지한다. 성공 결과를 확인한 뒤 Manager가 저장된 승인 identity·revision·bytes와 원격 원문·source 관계를 다시 검증하고 대상이 원본 bytes 또는 이미 적용된 승인 bytes일 때만 정확한 승인 bytes를 쓴다. 구현 단계 뒤에는 원문·source·소유권 재검증 실패나 승인 bytes 누락·변형을 연결 해제로 취급하지 않는다. mapped artifact와 미적용 이유를 보존해 문서 guard가 verify·Review·게시를 명시적으로 거부하게 한다. 다른 task 변경은 유지하지만 대상 내용·diff·file mode가 승인과 다르면 덮어쓰지 않고 Worker 교정 경계로 돌린다. 문서 의미·diff가 PASS이고 코드 Review만 FAIL이면 문서 교착으로 분류하지 않고 기존 validation failure·Worker 교정 경로를 따른다. 이후에도 기존 targeted test·전체 verify·의미 보존 Review·PR gate를 통과해야 한다.

기존에 게시된 `DOCUMENT_DRAFT_ID` claim을 가진 전용 계획은 legacy 복구 경로에서 승인 diff만 있는 worktree를 검증·적용한다. 이 claim과 R-027 source 제약은 예전 게시 결과를 위해 유지하며 새 문서 수정본의 작업으로 만들지 않는다.

**ExecPlan 문서 형식.**

ExecPlan은 [PLANS.md](PLANS.md)의 고정 key/value와 `sources`·`depends_on` 목록만 파싱하며 전체 YAML을 지원하지 않는다. 파일 앞의 `---` 경계 안에서 key를 한 번씩 선언하고 목록 항목은 두 칸 들여쓴 `- `로 쓴다. 허용 key 이외의 선언과 중복 key는 거부한다.

| 항목 | 필수 형식·허용 값 |
|---|---|
| `id`·`title` | 문자열. ID는 `G-` 뒤 세 자리 이상의 숫자이며 파일명은 `[id] title.md`와 일치해야 한다. |
| `sources` | 하나 이상의 문자열 목록. 일반 source는 `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 `.md` 경로다. 원본 해석은 [source 규칙](#원격-원본과-source-해석)을 따른다. |
| `depends_on` | ExecPlan ID 문자열 목록. 의존성이 없으면 `[]`로 명시한다. catalog에서 ID 고유성·의존 대상·순환을 검증한다. |
| `complexity`·`risk` | 필수 문자열. complexity는 `low`·`medium`·`high`, risk는 여기에 `critical`을 더한 값이다. |
| `priority` | `critical`·`high`·`normal`·`low`. 새 계획에는 필수이며 도입 전 계획의 생략 값은 `normal`로 해석한다. |
| `draft` | 선택 key이며 생략 시 `false`다. 소문자 `true`·`false`만 허용한다. `true`는 실행 전 사용자 판단이 필요하다는 선언으로 실행 상태와 구분한다. |

`draft: true`는 확정된 Purpose·Acceptance Criteria와 Draft Decision의 Question·Facts·Options·Impact가 필요하다. 사용자 답변·승인 증거와 draft 해제는 [draft 사용자 결정과 게시](#draft-사용자-결정과-게시)를 따른다.

문서 source의 유일한 예외는 등록된 Manager 요청의 `manager:verify-inputs`다. 다른 source와 혼용할 수 없으며 Purpose·`MANAGER_REQUEST`와 실제 등록 요청을 함께 검증한다. 형식과 권한은 [목록 밖 입력과 누락 요청](#목록-밖-입력과-누락-요청)을 따른다.

형식·source·dependency 오류는 실행 가능한 계획으로 수락하지 않는다. 해당 작업만 보류할 수 있는지 전체 catalog를 보류해야 하는지는 [catalog 오류 경계](#원격-catalog와-실행-이력의-분리)에 따라 결정하며 기존 실행 이력을 보존한다. 고정 형식이 바뀌면 공통 PLANS와 파서를 같은 변경에서 갱신한다.

## Source of Truth

| 정보 | 원본 |
|---|---|
| PRODUCT·Requirement·ARCHITECTURE·ExecPlan의 게시 내용과 현재 계획 선언 | 검증한 원격 commit의 Git 파일. 미게시 변경은 해당 관리 worktree에 보존 |
| ExecPlan 실행 상태·의존성 충족·재시도·session·worktree·branch·PR 연결 | SQLite |
| PR Open·Closed·Merged 상태 | GitHub |
| Model·Effort·동시 실행 수·Telegram 설정 | `config/config.ini` |

SQLite는 `<state-dir>/loop-manager.db`에 저장하고 상태 변경은 transaction으로 기록한다. bootstrap의 등록 조회는 읽기 전용으로 시작하며 `SQLITE_READONLY_ROLLBACK`일 때만 기존 DB를 쓰기 연결로 한 번 다시 열어 SQLite의 미완료 트랜잭션 복구를 허용한다. 같은 DB 접근 조율을 유지하고 schema migration·등록 변경·다른 오류의 우회는 하지 않는다. runtime gate의 pause 조회가 DB를 기다리는 동안 같은 실행 context의 pause 재진입만 억제하며 확정된 runtime 종료 결정은 중첩 확인에서도 적용한다. 현재 문서의 원본은 Git이며 경로·내용 해시로 연결한다. 복구·사용자 결정·문서 수정본 검증에 필요한 원문·snapshot은 별도 증거로 SQLite에 보존할 수 있으며 현재 문서의 원본을 대신하지 않는다.

SQLite에는 등록 프로젝트, 기준 branch, 선택한 provider, 과거 등록·승인 감사, 마지막 성공 Planner의 명세·전체 계획 입력 지문과 `planning_error`, 복구 가능한 planning publication intent, ExecPlan 메타데이터와 의존성, 최초 실행 여부, 실행 상태와 재시도 횟수, AI Agent 세션, worktree, branch, PR, 삭제된 미착수 ExecPlan의 ID 예약·감사 기록, `cleanup_pending`, 알림과 프로젝트 완료 주기를 저장한다.

실행 시작 시 원격 문서·Git worktree/branch·SQLite·GitHub PR을 대조하되 전체에 공통 우선순위를 적용하지 않는다. 문서의 현재 선언은 Git에서, 실행 상태·session·재시도·연결·감사는 SQLite에서, 실제 PR 상태는 GitHub에서 확인하고 검증된 전이로 연결한다. 유효한 문서 개정이나 cache 갱신만으로 실행 이력을 초기화하지 않는다.

정상적으로 열린 DB에서 작업 행만 없는 경우의 archive 문서 기반 완료 수락은 아래의 별도 복구 규칙을 따른다. 이때의 완료 근거와 실제 GitHub PR Merge 관측을 혼동하지 않는다.

### 원격 catalog와 실행 이력의 분리

최신으로 검증한 `origin/<base-branch>`의 active 및 archived ExecPlan 파일은 ID를 제외한 현재 계획 선언의 원본이다. SQLite는 최초 게시 path·hash·SHA를 불변 감사 정보로 보존하고 현재 path·title·sources·depends_on·complexity·risk·priority·hash·revision을 별도 cache로 동기화한다

catalog 동기화는 정상적으로 열린 DB에서 개별 작업 행이 없을 때 같은 원격 snapshot의 활성·archive 선언으로 행을 보완한다. 활성 선언은 기본 READY 등록과 기존 실행 복구 규칙을 따르고, archive 선언은 별도 병합 조회나 과거 게시 본문 비교 없이 MERGED로 수락한다. `archive-document` 근거와 관측 revision·본문 hash를 기존 archive 감사에 기록하며 실제 PR Merge 관측과 구분한다. 유실된 최초 게시·세션·실패 이력은 추정해 생성하지 않는다. DB 파일 부재·손상은 이 보완 절차에 진입하기 전에 오류로 처리하며 백업 복원이나 초기화가 필요하다.

기존 게시 감사가 있으면 보존하고, 문서 기반 완료 수락은 재시작에서 추가 Git/PR 검증 없이 재사용한다. 기존 작업 행에는 이 fallback을 적용하지 않으며 정상적인 archive identity 검증과 실행 이력 보존을 유지한다. 선언·source·dependency 검증, 취소·예약 충돌과 snapshot/CAS transaction 경계는 그대로 적용한다.

유효한 ExecPlan 선언 변경은 계획 또는 프로젝트 차단 사유가 아니다. 일반 원격 catalog 동기화는 아래 경계로 실행 가능한 선언과 보류할 작업을 구분하고, 원격 revision과 SQLite 상태·소유권 snapshot이 유지될 때 검증한 cache 변경과 보류 진단을 한 transaction으로 반영한다. snapshot이 달라지면 적용하지 않고 최신 입력으로 다시 확인한다.

| 오류 범위 | 동기화·실행 결과 |
|---|---|
| 작업을 유일하게 식별할 수 있는 metadata·source·dependency 오류 또는 삭제 경위가 확인되지 않은 원격 파일 누락 | 해당 작업과 전이적으로 의존하는 후속 작업을 실행 후보에서 제외하고 진단과 기존 실행 이력을 보존한다. 검증된 독립 작업의 cache 갱신·선택·PR 관측은 계속한다. |
| 중복 ID·파일명과 선언 ID의 불일치·문서 인코딩/조회 실패·일반 파일이 아닌 입력 등 식별·공유 입력의 안전성을 확정할 수 없는 오류 | 프로젝트 범위 진단을 남기고 전체 catalog 동기화를 보류한다. 실패를 빈 catalog나 변경 없음으로 바꾸지 않는다. |
| 진행 중 게시·공유 소유권과 충돌하거나 원격 revision·SQLite snapshot이 달라진 경우 | 기존 게시·소유 작업과 실행 이력을 보존하고 적용을 보류한다. 소유권 정리·게시 재개 또는 최신 snapshot 확인 뒤 다시 처리한다. |

일반·유지보수 planning은 결과 검증에서 active catalog에 같은 batch의 신규·개정 선언을 overlay한다. 일반 metadata 검증에서 제외된 보류 노드는 등록된 SQLite dependency 관계로 연결하고, 보류 원인 전파로 snapshot에서 제외된 기존 노드도 고정 revision에서 확인한 withheld closure에 있을 때만 등록 관계로 graph를 완성한다. 고정 revision의 원문·hash·보류 원인과 미수정 withheld 파일을 확인한 뒤 이 관계를 사용하며, batch 선언이 있으면 해당 ID의 과거 SQLite dependency보다 현재 선언을 우선한다. 공통 graph 검사는 변경 계획이 순환에 참여하는 경우 거부하고, 변경 계획과 무관한 기존 보류 순환은 독립 계획의 전역 오류가 되지 않게 격리한다. 등록되지 않은 보류 선행 참조와 변경 대상 자체의 선언·source·dependency 오류도 거부한다. 유지보수 게시 재검증은 이 helper를 다시 실행한다. 일반 게시 후보는 intent에 저장한 revision·snapshot token과 commit에 포함된 검증 hash를 대조하고 transaction의 CAS에서 SQLite 소유권을 확인한다. 문서 게시·DB 등록과 Worker 실행 자격은 분리하며 선행 보류 해제만으로 실제 Merge gate를 통과하지 않는다.

ExecPlan 번호는 연속일 필요가 없다. 번호 공백을 누락 계획으로 추정하거나 메우지 않으며 사용한 ID는 재사용하지 않는다. 파일 누락만으로 취소·완료를 확정하지 않고 검증된 삭제 처리와 구분한다. 오류 격리와 독립 작업 계속은 현재 동작이며, 아래의 관리 문서·자원 정리 목표와 구분한다.

완료·취소·예약 감사와 실행 row 없는 보류는 활성 선언으로 재등록하지 않는다.

Merge 근거가 검증된 보관 작업의 과거 ID·경로 참조는 삭제 복구의 활성 영향 대상에서 제외한다. 이미 저장된 참조 보류도 같은 기준으로 재평가하며 보관 문서·Merge 이력을 수정하거나 재실행하지 않는다. archive 감사 backfill 전에도 현재 보관 원문·원래 경로·MERGED 실행 identity와 PR 게시 근거가 일치하면 같은 역사 참조로 인정한다. 이후 보관 파일이 사라져도 검증된 durable Merge 이력과 실행 row의 경로·hash가 일치하면 최종 제거의 살아 있는 dependency로 취급하지 않는다. 현재 선언이 바뀌거나 다시 나타나 원문이 다르면 제외하지 않는다. 정리 관측에서 확인한 과거 참조 row 전체를 마지막 writer에서 CAS하며, 과거 depends_on·문서·Merge 기록은 수정하거나 제거하지 않는다.

draft 판단 대기와 검증 피드백 이행 표식은 실행·소유 이력이 아니다. 미착수·무소유 작업의 원격 삭제를 증명하면 해당 표식 때문에 취소를 보류하지 않으며 draft 대기를 해제하고 취소 감사를 보존한다. 실행 row가 사라진 뒤 남은 판단·이행 감사는 같은 ID의 신규 등록을 계속 막는다.

| 보존된 이력 | 현재 catalog·복구에서의 처리 |
|---|---|
| archive 전용 감사 | 원문 파일·source 링크 부재를 활성 선언 오류나 새 등록으로 만들지 않는다. 완전한 Merge proof가 있으면 실행 row·문서 없이도 dependency·완료 판단의 MERGED 근거로 사용하며 미확인 이력은 충족으로 계산하지 않는다. |
| 취소 감사 | 저장된 plan/deletion revision·hash·metadata로 원문·identity를 확인하고 현재 snapshot의 참조 영향과 관측 revision을 기록한다. 근거 부재는 미확인으로 보존하며 취소 ID·원문을 독립 실행을 막는 활성 source로 사용하지 않는다. |
| 실행 row 없이 남은 hold의 원격 삭제 | hold의 원문·revision·최초 관측 revision·미확인 사유를 보존하고 후속 참조가 생기면 그 영향과 함께 Planner 복구 목표로 전달한다. 영향 계획의 복구 판단·게시 전까지 hold를 유지하고 snapshot/SQLite CAS가 바뀌면 감사 갱신을 적용하지 않는다. |
| 과거 ID의 active 재등장 | archive·취소 ID의 예약을 유지해 새 계획 등록을 거부한다. |

`deleted_plan_cleanup.py`는 원격 ExecPlan 부재와 마지막 채택 bytes·ancestor revision, 미Merge CLOSED PR 및 후속 선언의 `DEPENDENCY_RECOVERY` 판단을 확인한다. 기존 작업별 자원 lease와 물리적 writer 종료 검증을 사용하고 수동 소유·미확정 Git 전이는 정리하지 않는다. 현재 경로는 원격 작업 ref가 이미 없을 때 관리 worktree·정확한 로컬 ref를 정리한다. 살아 있는 원격 ref의 lease 기반 삭제는 별도 자원 정리 범위로 남긴다. row 없는 취소 감사는 원래 선언·철회 근거와 현재 자원 부재를 확인해 아래 최종 제거로 연결한다. 현재 Agent 소유·살아 있는 자원·미확인 checkpoint는 계속 보호한다.

일반 취소는 미착수 작업도 원문과 취소 직전 전체 실행 row를 `recovery_evidence`에 보존한다. 대상 block·두 checkpoint·유지보수 cycle도 parent 제거 전 같은 transaction으로 이관한다. `checkpoints`는 테이블명을 key로 전체 row를 보존하며 snapshot/게시 history의 원래 JSON 문자열도 유지한다. 원격 삭제 증명을 합칠 때 이 감사를 덮어쓰지 않는다. foreign key·원자 rollback과 원래 DELETE 불변 trigger의 복원은 최종 제거에서도 유지한다.

최종 제거는 일반 파일 삭제와 분리한다. 미착수 작업도 검증된 원문·마지막 선언·원래/삭제 revision의 ancestry, 원격 부재와 목표 철회 근거를 요구한다. 감사에 같은 ID/source의 withdrawn 판단과 사유가 있거나, 원래 영향 대상의 현재 게시 문서가 `DEPENDENCY_RECOVERY`로 철회/대체 관계를 확인해야 한다. 빈 참조 목록 자체는 철회 증명이 아니다. 취소·MERGED된 과거 참조 주체를 살아 있는 후속 작업으로 다시 만들지 않는다. 등록 전 hold의 게시 approval은 아래 별도 경계를 따른다.

| row 없는 감사 | 제거 자격 |
|---|---|
| 미착수 READY | 빈 retry/실패 이력·현재 Agent 소유 부재. workspace_owned=0이면 branch/worktree·PR/게시 OID도 비어 있어야 함 |
| 시작 이력이 있는 무자원 READY/PLAN_BLOCKED | 같은 ID/source의 보존된 withdrawn 판단 또는 원래 영향 대상의 현재 게시된 DEPENDENCY_RECOVERY 판단, cleanup_pending=false, 실제 executed 호출의 시작 ledger와 현재 종료·결과 인계 |
| 과거 workspace_owned=1 | canonical branch·관리 worktree 쌍과 현재 경로/등록/ref 부재. READY와 과거 활성 단계/PLAN_BLOCKED를 허용하되 실제 Merge·현재 Agent/복구 소유는 보호 |

과거 관리 자원 쌍의 boolean cleanup_pending과 OID는 역사로 읽으며 실행 재개 권한으로 사용하지 않는다. 현재 agent_role·agent_recovery·충돌 PR 소유 근거는 비어 있어야 한다. Agent 입력/결과 문서는 같은 ID, fingerprint는 SHA-256을 확인한다. native GitSnapshot의 짧은 branch 이름과 `refs/heads/` 표기는 같은 canonical branch로 대조한다. baseline은 native V1 형식·tree/OID·stage_trees·원래 scope.head로 계산한 repository별 ref identity를 검증하고 lease 아래 실제 부재를 제거 직전에도 재확인한다. baseline을 이 경로에서 직접 삭제하지 않는다. session·결과·과거 snapshot으로 실제 종료를 대신 증명하지 않는다.

execution checkpoint는 완전한 row·같은 repo/ID·음이 아닌 generation·현재 operation 부재·workspace 보존 0을 요구한다. 퇴역 자원의 snapshot은 native 관측, 문서 개정/판단, 단계 출처·Agent scope, Worker 요청/결과·planning/복구의 알려진 자료 형식으로 읽는다. 부분 workspace 관측도 원래 branch/worktree에 결합하며 무자원 관측의 빈 문자열은 native producer의 빈 branch/worktree/active SHA 묶음에서만 인정한다. 새 자원을 가질 수 있는 미확인 key/version·귀속은 보존한다. 과거 pending/operation/index/stash 관측과 현재 실행 소유권은 구별한다. 자료의 과거 필드가 남았다는 이유로 새 완료를 만들거나 실행을 재개하지 않으며, 실제 호출·예약·현재 operation/native Git journal·현재 자원은 별도로 검사한다. 실행 row/archive 파일 부재와 관계없이 확인된 catalog Merge 이력·완료 archive 관측은 정리 전에 보호한다. stage_result_origins의 version/repo/ID·branch/worktree/PR 및 canonical JSON SHA-256과 retired Agent scope의 native identity를 검증하며 성공 token으로 재소비하지 않는다.

취소 감사로 이관된 blocked_checkpoints는 비어 있지 않은 원인/요청/시각 필드를 요구한다. phase=unpublished이면 비어 있지 않은 종결 사유와 local_only 또는 NULL 결과를 허용한다. phase=requested이면 result와 unpublished_reason이 모두 NULL이어야 하며 원래 단계를 완료로 바꾸지 않는다. 이 분류는 활성 요청의 실행·게시 권한이 아니라 취소 후 감사의 비소유 검사에만 사용한다. 자원·PR·commit/OID·inspection 필드는 NULL, 원격 관측/복구/재시도 flag는 정수 0, publication_history는 빈 배열이어야 하며, 저장된 원인과 빈 자원으로 다시 계산한 request_identity가 일치해야 한다. 보존된 block이 있으면 현재 전체 차단 row 형식·같은 repo/plan/scope=plan·요청과 동일한 reason_code/evidence·시각 필드를 확인한 뒤 nullable 문자열 release_fingerprint도 재계산에 결합한다. recovery_snapshot 원문은 보존하되 종료/자원 해제 근거로 대신 쓰지 않는다. block이 없는 과거 감사는 fingerprint 없는 요청 identity가 확인될 때만 기존 경로를 유지하며, fingerprint 결합을 재구성할 수 없거나 서로 다른 원인의 block이면 보류한다. 미확인 필드·내용·자원 귀속이면 전체 감사를 보존한다. 과거 관리 자원 쌍이 실제로 정리된 더 강한 경로에서는 해당 자원에 결합된 게시/inspection OID·원인·commit identity와 native 게시 이력도 제거할 수 있다. 알려진 rebase 게시 이력과 superseded_request 형식을 확인하고 현재 block은 독립적으로 검증한다. 역사 요청 hash를 현재 원인의 실행 권한으로 재사용하거나 phase/result를 완료로 바꾸지 않는다. 이 검사는 요청의 비소유 확인이며 실행 완료·종료·게시 근거가 아니다. 원래 revision과 삭제 commit이 현재 원격의 ancestor이고 문서가 실제로 사라졌는지도 기존 Git 관측으로 확인한다.

퇴역한 rebase acceptance와 게시 intent/result/history는 기존 native parser로 형식·불변 hash를 읽고 같은 repository/ID·branch/worktree에 결합한다. 문서 rename 전 경로도 고정 G ID가 같아야 한다. 결과 없는 과거 요청·폐기된 정확한 lease·superseded snapshot을 제거해도 push/검증 성공으로 기록하지 않는다. 원래 phase/generation과 실행 token은 재발급하지 않는다. 중첩 operation/superseded history도 같은 검사를 적용하며, 하위 이력에 복제하지 않은 stage_result_origins는 검증한 부모 출처를 사용한다.

base_rebase_restart·work_branch_integration의 현재/과거 snapshot과 baseline은 native identity로 읽고, 별도 보존 ref까지 parent Agent baseline과 같은 lease·부재 검사를 적용한다. 실행 row가 있는 최종 제거에도 같은 inventory를 사용한다. 중첩 history와 재시작 전 checkpoint에 남은 문서 사본은 기존 document namespace·assessment·제거 journal·CAS 경계로 처리한다.

완료된 deleted_branch_restart는 원래 plan/block/복구·checkpoint의 repository/ID, canonical 원본 자원과 snapshot을 확인한다. 전용 사본은 원래 관리 worktree의 형제 `retired/<32자리 hex token>`, ref는 `loop-manager/retired/<G ID>/<같은 token>`이어야 한다. branch/worktree/Git lease 아래 로컬/고정 origin 원격 ref·PR·Git 등록·실제 경로 부재를 확인하고 제거 직전 재관측한다. 현재 실행/프로젝트/수동 source·보존 planning 경로의 alias/조상·자손 참조와 재생성은 보호한다. 원래 baseline·중첩 사본의 inventory와 전용 로그도 함께 처리한다. 살아 있는 자원의 물리 정리는 G-261 책임이며 이 경로는 reset·사본 이동·원격 삭제를 반복하지 않는다. 준비 중 intent와 소유 귀속을 확인할 수 없는 과거 자료를 완료로 바꾸지 않는다.

과거 자원 감사의 PR은 없거나 저장 URL·canonical head branch와 일치하는 CLOSED 미Merge 이력이어야 한다. OPEN·실제 Merge·다른 identity·조회 실패는 보류한다.

이 경로는 실행 row를 임시로 복원하지 않는다. 기존 작업 ID lease와 기록된 branch·worktree·Git 자원 lease 안에서 취소 감사 전체의 CAS, 같은 ID의 프로젝트 투영 부재, 현재 호출 예약·Git operation·수동·게시 소유권을 확인한다. canonical 작업 branch의 원격/로컬 ref가 남거나 실제 PR이 앞의 CLOSED 역사 조건을 만족하지 않으면 보류하며, 원격 관측 실패도 보존한다. 해당 작업 owner의 미확정 Git journal도 보호한다. 기록된 경로·branch의 Git 등록이 남거나 현재 실행 row·프로젝트가 같은 자원을 사용하면 보류한다. 관측 중 경로 재생성도 마지막 CAS에서 거부한다. 원격 관측은 DB writer 밖에서 수행하고 마지막 제거 transaction에서 감사/row 부재·소유권을 다시 확인한다. archive의 귀속 불명 Git 관측과 자원을 대조할 때도 row 없는 취소 감사의 자원 쌍을 사용하며, 확인한 소유자 관계만 최종 제거한다. 살아 있는 자원·현재 Agent 소유·미확인 자원/게시 checkpoint가 남은 rowless 감사의 최종 제거는 이 경로에 포함하지 않는다.

등록 전 hold는 기존 `deleted_goal`의 원문·hash·ID/path·원래 선언/revision과 삭제 관측 revision을 검증하고, `resolve_recovery`가 기록한 `resolved_references`의 commit·path·hash를 현재 원격 문서와 실행 선언에 대조한다. 모든 원래 영향 작업에 게시된 approval이 있어야 하며 기존 `resolved_references()`로 실제 Decision Log·source·유효 대체 dependency를 검사한다. 실행/취소/호출/checkpoint 이력이 있거나 과거 Merge가 확인된 ID는 이 경로에서 제외한다. 원래 revision·삭제·approval commit의 ancestry, canonical PR/ref 부재와 기존 수동·게시 소유권 gate를 재사용하며 hold 전체와 approval 대상 선언을 마지막 transaction에서 재확인한다. 선언 bytes나 후속 row가 관측 중 바뀌면 제거·dependency 덮어쓰기를 거부한다. 공통 cleanup은 실행 row/취소 감사 경로의 참조 복구 row에도 같은 CAS를 적용한다. 실행 row·빈 성공 checkpoint를 임시 생성하지 않고 일반 forget에서 hold와 ID 예약을 제거한 뒤 재사용 방지 표식만 남긴다. 복구 판단 없는 부재는 철회 증거로 쓰지 않는다.

전용 문서 사본은 `catalog/deleted_document_cleanup.py`가 `stage_documents`의 실제 namespace인 `worktrees/<repo hash>/document-revisions/<plan ID>`와 `document-assessments/<plan ID>`를 관리한다. 현재 checkpoint 또는 취소 감사의 version 1 문서 단계·retired 문서 판단·superseded 이력에서 정확한 경로와 commit/base를 읽는다. 판단 사본은 native Planner scope의 worktree·GitSnapshot·generation 결합을 확인한다. namespace에 기록되지 않은 자식·symlink·미확인 snapshot이 있으면 보존한다. 작업/copy의 worktree·Git lease를 유지하고 actual detached 등록·같은 common Git directory와 현재 실행·프로젝트·수동·보존 planning 문맥의 실제 경로 소유권을 대조한다. 삭제 직전에도 checkpoint/감사 CAS·소유권·호출 종료/결과 인계·예약 반환을 확인한다.

확인된 문서 worktree는 기존 `assess_cleanup`과 journal 기반 `remove_cleanup_worktree`로만 정리한다. native journal의 제거 전이도 정확한 document-revisions/assessments namespace·repository hash·plan owner와 detached 등록·OID 및 소유 marker를 검증한다. 생성 전이·일반 외부 경로의 권한은 넓히지 않는다. 정확한 저장 사본의 같은 owner managed_worktree_remove intent만 기존 `_recover_managed_worktree_removal`로 복구하며 현재 프로젝트 pending 판정의 예외도 그 범위로 한정한다. 효과가 없던 intent는 native execute가 원래 상태를 확인해 중단 처리한 뒤 새 제거를 수행하고, 효과와 marker가 있는 intent는 결과 확정만 복구한다. 기존 workspace-reservation-recovery lease 규칙을 재사용하고 다른 owner/kind/자원은 계속 보존한다. dirty·HEAD/index/operation 변경·미확정 등록에는 최종 DB 제거를 보류한다. copy 제거 뒤 등록 해제를 관측하고 빈 대상 namespace만 비재귀적으로 제거한다. 마지막 DB transaction에서는 외부 Git 관측 없이 저장 checkpoint와 사본 재생성 부재를 다시 검사한다. 물리 효과 뒤 기록 중단에는 원래 manifest/감사/호출·사용량을 남기며, 재진입에서 경로·등록 부재가 확인되면 제거를 반복하지 않는다. 해당 copy의 저장 경로로 계산한 로그도 기존 artifact ownership·file queue에 연결해 독립 로그와 분리한다. 현재 실행 row가 있는 경우와 취소 감사만 있는 경우에 같은 정리 경계를 적용한다. 원격 ref·진단 게시 자원의 정리를 이 문서 사본 처리로 대신하지 않는다.

`ProjectCatalogRefresh`의 선언 cache는 원격 파일이 없는 실행 row뿐 아니라 row 없는 취소 감사·파일이 사라진 catalog hold·최종 ID 표식의 잔존 진단 소유 기록과 repository별 `deleted_plan_files` queue도 재진입 조건으로 소비한다. 외부 writer·ref·worktree 반환과 파일 참조 해소는 문서 revision이나 선언 변경 없이 일어나므로 이 근거가 남으면 기존 reconcile/cleanup을 다시 호출한다. 감사와 queue가 없어지면 같은 revision의 cache를 재사용하고 다른 repository의 queue는 영향을 주지 않는다. 이 검사는 자원 해제·삭제 완료의 증명이 아니며 실제 판단은 cleanup의 현재 관측·lease·CAS에 남긴다.

`deleted_plan_state.forget()`은 같은 transaction에서 대상의 `(repo, plan_id)` 소유 상태·감사·호출·사용량과 invocation 하위 기록, 정확한 대상의 복구·알림을 제거하고 `deleted_plan_ids(repo, plan_id)`만 예약한다. 명시적인 삭제 transaction 안에서만 관련 감사의 DELETE 불변 trigger를 해제하고 원래 정의로 복원하며, 실패하면 DDL과 데이터 삭제를 함께 rollback한다. validation result의 report·link·delivery item은 부모보다 먼저 제거한다. 이 표식은 catalog·Planner·수동 번호 배정에서 재등록을 막으며 source·dependency 충족 근거로 사용하지 않는다. 파일 정리는 `deleted_plan_files(repo, plan_id, path)`에 한시적으로 저장해 DB 확정 뒤 관리 `agent-runtime`·`logs`·검증된 `diagnostic-artifacts` 아래 대상 산출물을 제거하고 완료 항목을 삭제한다. 중단 뒤 다음 catalog 관측에서 남은 파일 정리부터 이어간다.

산출물 선택은 invocation의 process receipt·대응 attempt, targeted collection, task-call receipt와 worktree별 로그의 저장 경로·repository/작업 소유 쌍을 대조한다. worktree 로그 참조는 현재 실행 row와 일반 취소 감사의 원래 row뿐 아니라 프로젝트의 현재 worktree·보존 planning 상태·수동 묶음 worktree에서도 같은 provider 로그 경로 규칙으로 읽는다. 프로젝트의 현재 문맥이 실행 slot을 planning에 맡기지 않았고 plan_path가 정확한 G 경로이면 그 repository/작업 소유 쌍을 사용한다. planning·수동 묶음과 귀속 미확인 문맥은 프로젝트 범위의 독립 소유로 보호한다. 보존 planning 상태는 기존 validator로 해석하며 손상·과도한 중첩과 미확인 worktree 경로에는 logs 영역을 미확인 참조로 남긴다. 취소 감사의 row 귀속·worktree 경로가 미확정인 경우도 같은 보호를 적용한다. 겹치지 않는 agent-runtime receipt/collection 정리는 계속한다. 같은 session 이름으로 소유권을 추정하지 않으며 원래 절대 경로와 정규화한 경로의 조상/자손 겹침을 모두 보호한다. 공유 runtime 부모는 통째로 지우지 않고 독립 참조와 겹치지 않는 대상 receipt·attempt만 선택한다. receipt가 없는 전용 collection도 실제 invocation 귀속으로 queue에 등록한다. 동일 receipt의 별칭·독립 worktree 로그·분리할 수 없는 경로 겹침은 DB 제거 전에 보류한다. 산출물 선택과 queue 소비는 같은 관리 경로 검사를 사용해 정규화 경로가 state_dir의 agent-runtime/logs/diagnostic-artifacts 내부이고 그 루트 자체가 아닌지 확인한다. 범위 밖 산출물은 DB 제거 전에 CallDeferred로 보류해 원래 호출·소유 감사와 파일을 남긴다. 루트 바로 아래의 native process receipt는 부모 폴더 대신 기존 receipt/attempt 파일 쌍만 선택해 독립 형제 파일을 보존한다.

파일 queue 소비도 각 파일 제거 직전에 관리 범위·symlink·같은 참조를 새로 읽는다. 원래 invocation이 없는 collection도 미확인 소유로 보호하며 절대 경로를 확인할 수 없으면 queue를 유지한다. 새 독립 참조가 생긴 항목은 한시적 pending과 운영 로그로 남기고 다른 대상의 처리만 계속한다. 이 경우 해당 ID를 파일 정리 완료 결과로 반환하지 않는다. 참조가 안전하게 분리된 뒤 다음 관측에서 남은 항목을 재개하며, 범위 밖·symlink 경로와 조회 불명·파일 제거 오류도 해당 queue 항목의 pending으로 남기며 다른 안전한 항목은 계속 정리한다. 제거 오류의 실제 사유는 운영 로그에 남기고 완료 목록에서는 제외한다. 관리 루트 아래 부모 symlink로의 경로 전환도 소비 직전에 거부한다. 다른 작업의 Git 관측 snapshot·공유 이력, GitHub PR·Git 과거 commit과 운영자가 별도로 보관한 DB 백업은 재작성하지 않는다.

진단 기록은 `storage/deleted_diagnostic_state.py`가 native schema의 scope/owner와 ref 관계로 capture하고 최종 transaction에서 CAS한다. plan/project 범위 모두 실제 owner가 대상인 기록만 선택한다. 현재 프로젝트 원인과 독립 실행·수동 ref 사용은 보호하고, native cleanup preparation/attempt에 독립 소유자가 섞이면 불변 관측을 임의로 분리하지 않는다. diagnostic/worktree/Git lease 아래 SQL에 저장된 origin을 고정해 원격 ref를 조회하고 로컬 ref·실제 PR·worktree 경로/등록 부재도 확인한다. 조회 불명·재생성·소유 자료의 변경은 해당 제거를 보류한다. 이 helper는 Git 효과나 새로운 성공 상태를 만들지 않는다.

진단 bundle은 native `load_verified_artifacts`로 원문·manifest·tree/commit 귀속을 검증한 뒤 기존 파일 queue에 전달한다. 다른 scope/owner의 preservation과 ref use도 실제 경로 참조로 읽어 늦은 독립 사용을 보호한다. 손상·불완전 bundle은 원본 DB 근거와 함께 보존한다. 이전 최종 삭제에서 scope 기반 진단 기록만 남았으면 `deleted_plan_ids`의 정확한 repository/ID 표식으로 재발견한다. 새 실행/취소/hold/checkpoint가 없어야 하고 현재 문서·호출·수동/게시 소유·PR/ref/등록 및 capture CAS를 다시 확인한다. 원래 실행 선언이나 가짜 READY row를 만들지 않으며 일반 취소·단순 예약을 최종 삭제 권한으로 사용하지 않는다.

현재 `remote_deletions.py`는 고정 원격 revision과 마지막 채택 원문·선언·실행/게시 이력을 대조해 실제 Requirement·ExecPlan 삭제를 관측한다. archive·rename·조회 실패를 실제 삭제와 구별하고, 확인된 ExecPlan 삭제의 취소 감사와 Requirement 삭제에 따른 목표 복구 입력을 만든다. 일반 Planner의 철회·유지·대체 판단은 허용된 계획 개정·삭제 게시와 연결하며 Manager가 참조·소유권·현재 PR을 재검증한다. 확인되지 않은 범위는 보류하고 검증된 독립 작업은 계속한다.

원격 삭제 증명·철회 문서 준비·취소 transaction은 `work_claims.cancellation_available`로 호출 예약·invocation, 수동 준비/소유, 저장된 Git operation과 다른 활성 row의 branch/worktree 사용을 재확인한다. 수동 소유는 기존 `planning_ownership_state`를 사용하며 불완전한 소유 근거는 해당 삭제만 보류한다. `task_calls.pending_for_plan`은 결과 인계가 미확정인 NULL/빈 finished_at, unknown/prepared 등 executed/not_executed 외 상태, executed인데 시작 기록이 NULL/빈 값이거나 종료 시각·근거 중 하나가 없는 호출을 보류한다. 종료 필드가 있다는 이유로 미확인 실행 상태나 빠진 시작을 보완하지 않는다. `not_executed`는 결과 인계가 있고 실제 시작 기록이 없을 때만 미실행으로 인정하며 시작 기록과 모순되면 보류한다. 완료 결과 수집과 프로세스 종료를 구분하며 invocation·예약 감사를 삭제해 소유권을 해제하지 않는다. 새 호출 예약과 invocation 시작도 같은 SQLite writer 경계에서 취소 ID를 거부한다.

최종 `deleted_plan_state.forget`도 같은 제거 transaction에서 호출 예약·invocation gate를 재확인한다. 실행 row가 없어도 예약이 반환되거나 실제 종료·결과 인계가 확정되기 전에 invocation·하위 targeted 자료·사용량·파일 제거 queue를 먼저 지우지 않는다. 이 DB 검사는 caller가 transaction 밖에서 수행하는 기존 process receipt·writer/runner 종료·자원 lease 확인을 대체하지 않는다. 종료를 추정하거나 새로운 프로세스 관측을 writer transaction 안에서 수행하지 않으며, 미확정에는 `CallDeferred`로 해당 제거를 보류한다.

원격 ExecPlan 삭제는 증명 작성과 DB 적용 직전에 실제 PR 부재·관리 worktree 소유권·현재 Git operation 및 해당 branch/worktree의 미완료 Manager Git journal을 재관측한다. 기존 소유권 검증과 controlling operation 판정을 재사용하며 잔여 메시지 파일만으로 진행 중 operation을 만들지 않는다. 소유 자원의 state directory나 관측 수단·결과가 없으면 삭제를 보류한다. 이 외부 확인은 SQLite transaction 전에 수행하며 caller transaction이 이미 열렸으면 적용을 보류한다. revision·실행 선언의 DB CAS도 유지한다. catalog cache는 사용 가능한 보류 snapshot도 기억하고, 활성 row의 경로가 그 원격 snapshot에 없으면 같은 revision에서도 삭제 경계를 재확인한다. 적용한 삭제의 일회성 확인 callback은 cache에서 제거해 archive 적용에서 재생하지 않는다.

Requirement 철회의 `prepare_withdrawals`는 같은 외부 자원 helper를 사용해 모든 대상의 DB 소유권·현재 자원 확인을 마친 뒤 보존 영수증과 planning worktree의 삭제를 준비한다. planning context의 state directory와 기존 PR/worktree/Git 관측 callback을 validation port로 전달한다. 영수증을 재사용하는 재개에서도 확인을 생략하지 않으며, 이미 열린 transaction에서는 외부 확인을 실행하지 않는다. 관측 수단 부재·진행 중 operation·journal은 `ExternalObservationError`로 전달해 Planner 형식 교정 횟수를 소비하지 않는다.

일반 planning의 state port는 intent 기록·게시 직전 재기록, 원격 게시 뒤 DB 적용과 저장 manifest 복구에서도 철회 대상의 현재 writer·소유권 및 같은 외부 자원 helper를 확인한다. 복구 대상은 기존 hash·형식 검증을 통과한 manifest에서 읽고, 추가 자원 관측은 transaction 전에 수행한다. DB 적용은 기존 snapshot CAS와 공통 취소 gate를 유지한다. 실제 PR 등록은 기존 `MutableReadyPRConflict` 경로로 전달해 미게시 intent의 대체와 이미 게시된 근거의 보존을 구분한다. 자원 미확인은 intent·Planner 결과·worktree를 보존하고 DB 소비와 cleanup을 시작하지 않는다. 취소 감사가 있으면 오래된 실행 row가 함께 남아 있어도 공통 gate가 재취소·게시를 거부한다.

실행 revision guard는 최신 catalog의 취소·보류 판정을 checkpoint 게시보다 먼저 소비한다. 취소된 작업은 context를 버리고 대기로 반환하며, 보류 중인 작업은 context를 보존하되 다음 실행·저장 결과 소비·게시로 진행하지 않는다. 이미 실제 PR Merge/Close가 확인된 terminal 처리는 이 guard보다 앞선 기존 경계에서 보존한다.

소유가 끝난 과거 수동 묶음의 membership은 현재 writer·준비 intent와 구분하며 취소로 묶음·요청·단계 감사를 다시 쓰지 않는다. 수동 대기 조회는 선언된 구성원에 취소 ID가 있는 묶음을 실행 대상으로 선택하지 않고 새 완료 요청도 거부한다. 집합 조회도 일반 취소 감사와 최종 삭제 ID 표식을 함께 읽어 감사 제거 뒤의 과거 요청 재선택을 막으며, 조회 중 schema를 생성·변경하지 않는다. 과거 요청은 감사 조회에 남기며 다른 묶음의 실행 순서와 요청은 유지한다. 문서 삭제를 수동 stop/reset으로 해석하지 않는다.

최종 제거 transaction은 `manual_work_state.forget_plan`으로 수동 기록도 검증한다. 기존 전체 membership 검증을 통과한 CLOSED 묶음에서 branch/worktree·활성 대상·source 갱신 복구 소유와 게시 영수증이 없어야 한다. 문서 snapshot의 실제 plan ID를 membership과 대조하고 독립 결과의 귀속도 확인한다. 단독 묶음은 모든 과거 요청·단계 결과·문서와 묶음을 제거하며, 공유 묶음은 독립 owner를 유지하고 대상 membership·문서·단계 결과만 제거한다. 남은 문서의 dependency에 대상이 있거나 대상이 공유 owner이면 보류하고 소유자를 임의 재배정하지 않는다. 공유 요청·독립 결과와 나머지 묶음 필드는 보존하며, 단독 묶음의 요청을 독립 묶음도 참조하면 제거하지 않는다. membership이 사라졌어도 대상 snapshot이 남아 있으면 귀속 불일치로 보류한다. 뒤 단계 실패에는 부분 제거도 같은 transaction에서 rollback하며 일반 취소의 수동 감사 보존은 유지한다.

취소 적용 transaction은 `execution_checkpoints`·`blocked_checkpoints`의 해당 요청을 `execplan_cancellations.recovery_evidence.checkpoints`로 옮긴 뒤 활성 요청과 실행 row를 제거한다. `checkpoints`는 테이블명을 key로 하고 해당 row의 모든 필드·값을 보존하는 객체이며, 원래 snapshot·게시 이력의 JSON 문자열도 그대로 유지한다. 원격 삭제 증명을 합칠 때 이 별도 감사를 보존하며 transaction 실패 시 요청·실행 row·감사를 함께 롤백한다. checkpoint repository는 취소 ID의 조회·대기·기록·변경에서 취소 감사를 확인하므로 이전 runtime이 남긴 요청도 현재 요청으로 소비하지 않는다. 확정 게시 영수증은 이 감사에서 조회하며 새 게시 권한으로 재사용하지 않는다. 공통 취소 조회는 일반 취소 감사와 최종 제거의 `deleted_plan_ids`를 모두 확인해 실행 row나 감사가 사라진 뒤에도 호출·checkpoint·알림 재생을 거부한다. 공통 게시 row 등록과 직접 insert도 SQLite writer 경계에서 이 조회를 수행해 저장 후보·문서 fallback·수동 등록의 재생을 거부한다. 기존 transaction이 없으면 BEGIN IMMEDIATE로 writer를 확보하고, 외부 호출·commit 없이 부모 transaction의 원자 적용/rollback을 따른다. archive/legacy 입력 옵션도 ID 예약을 우회하지 않으며 bootstrap 인수 자격 제거·row 생성 전에 거부한다. 정상 등록의 불변 필드 비교와 멱등성은 유지한다. 유지보수 게시 서비스는 prepared 재개를 포함해 fetch 뒤 push 전에 기존 `record_maintenance_intent`를 멱등 재호출한다. 이 경계는 현재 ID 예약과 cycle/context·수동 소유권을 다시 검사하며, 실패하면 저장 intent·후보·세션·기존 planning 오류를 유지하고 게시 적용·worktree 해제로 진행하지 않는다. 외부 관측은 기존 intent 준비 경계에 두며 DB writer 안에서 Git을 호출하지 않는다. 로컬 복구 저장소도 요청 접수·대기 조회·재검사 소비·진행/완료 확정에서 이 조회를 사용한다. 프로젝트 범위 요청은 유지하되 취소된 작업을 subject로 한 재검사 소비는 거부한다.

유지보수 대상의 취소는 같은 transaction에서 `recovery_evidence.maintenance_cycle`에 해당 `maintenance_cycles` row의 전체 필드·값을 보존한다. 원래 대상 배열·요청 identity·게시 영수증·성공 기준은 다시 쓰지 않는다. `MaintenanceRepository.get`은 공통 취소 ID 집합 조회로 취소 감사 또는 최종 삭제 표식이 있는 ID만 현재 prerequisite·refactoring·assessment·revalidation 집합에서 제외하고, 게시 배열은 과거 영수증 그대로 반환한다. 두 조회 모두 repository 범위를 지키며 저장된 cycle 배열이나 기준을 변경하지 않는다. 근거 없는 ID 부재와 남은 작업의 유효하지 않은 dependency는 계속 오류다. 공통 취소 gate는 이 작업을 포함한 cycle의 미확정 게시나 legacy workspace 해제 intent를 보호하며, 해당 소유가 끝난 뒤 취소할 수 있다. 원래 prerequisite·refactoring·assessment 범위에서 취소/최종 삭제 집합에 속하는 부분을 `cancelled_scope_plan_ids` 읽기 전용 tuple로 투영한다. 해당 ID에 현재 실행 row도 있으면 모순된 귀속으로 보류한다. 혼합 cycle도 이 취소 부분과 남은 dependency 범위를 함께 hold 검사에 사용하고, 남은 범위는 모두 실제 MERGED여야 종료한다. 남은 범위가 비었을 때에는 원래 범위의 확정 취소 투영이 있어야 한다. 이 값은 DB 컬럼이나 새 성공 원본이 아니며 범위·취소 관측이 바뀌면 같은 종료 transaction의 재조회와 expected 비교에서 거부한다. cycle service와 finish_cycle은 이 확정 빈 범위 또는 실제 Merge된 남은 범위를 기존 종료 경계로 연결한다. 관련 hold·게시·workspace 해제와 검증된 catalog source/Merge 수 검사는 그대로 유지한다. cycle_id가 없는 이전 assessment의 원래 prerequisite 목록이 비어 있지 않으면 `legacy_prerequisite_wait`를 읽기 전용 bool로 투영해, 현재 목록이 전부 취소로 비어도 기존 확정 대기 경계를 보존한다. 평가 범위 저장은 같은 transaction에서 이 읽기 전용 투영까지 계산한 cycle을 반환하며, Planner는 그 반환값을 완료 CAS의 expected로 사용한다. 일부 배열만 메모리에서 교체해 저장소의 legacy/취소 투영과 다른 cycle을 만들지 않는다. service·상태 진단·종료 저장소가 같은 투영을 사용한다. hold가 남으면 기존 prerequisite_wait로 이어가며 새 평가를 열지 않는다. cycle_id가 있거나 원래 prerequisite이 없는 assessment에는 이 자격을 발급하지 않고, 새 DB 필드나 평가 성공 근거를 만들지 않는다.

삭제 확정의 최종 제거는 일반 취소와 달리 `maintenance_state.forget_plan`으로 공유 cycle의 대상 부분을 제거한다. prerequisite·refactoring·assessment·revalidation과 게시 대상 배열에서 정확히 일치하는 ID만 제외하고, publication ID 배정 mapping은 실제 게시 대상 ID(네 번째 항목)가 일치할 때만 제거한다. 원본 ID가 같아도 다른 대상으로 배정된 독립 mapping은 유지한다. 기존 decoder로 형식을 확인하고 변경이 없으면 row를 쓰지 않는다. 활성 cycle의 마지막 prerequisite/refactoring/assessment 연결을 없애는 제거는 CallDeferred로 롤백한다. cleanup은 catalog 채택 전에 실행되므로 마지막 연결과 취소 근거를 남긴 채 catalog 반영·기존 정상 종료를 먼저 거치고 다음 cleanup에서 제거한다. 이 보류를 위해 새 완료 필드·삭제 감사·원격 성공을 생성하지 않는다.

tiered 유지보수의 `tiered_maintenance_root_plan_links`는 `(repo, cycle, source_revision)`에 결합된 `links_json`으로 계획을 참조하므로 일반적인 repo/plan_id 테이블 삭제에 포함되지 않는다. `maintenance_state.forget_plan`은 legacy cycle row 유무와 관계없이 같은 repository의 이 목록을 먼저 읽어 현재 link 필드 집합·문자열 ID/문서 경로의 일치를 확인한다. 손상·미확인 형식과 현재 cycle의 대상 연결은 전체 최종 제거를 CallDeferred로 보류한다. 정상 종료 commit·실제 Merge 수가 있는 IDLE cycle이며 게시/해제 intent가 없고 저장 연결의 cycle이 현재 revision보다 과거이면 대상 link만 제거한다. 공유 목록은 나머지 항목과 assessment digest·source identity를 보존하며 대상 전용 목록만 삭제한다. 활성/미확정 cycle에서는 이 경로를 적용하지 않고 실제 정상 종료 뒤 재개한다. 남은 과거 부분 목록을 삭제 전의 완전한 연결 근거로 재사용하지 않으며 다음 평가는 현재 source의 새 identity를 사용한다. 부분 제거도 최종 삭제 transaction에서 rollback하고 cycle revision을 전진시켜 오래된 실행 context를 무효화한다. 연결을 삭제해 루트 조사 범위나 계획 충족을 합성하지 않으며, 무관한 연결·다른 repository의 원문과 평가 근거는 유지한다. 현재 연결은 정상 종료 전까지 보호하고 정상 종료 뒤 대상 부분의 최종 제거를 재개한다.

관련 cycle에 publication stage(원격 확인 뒤 해제 전 상태 포함)·legacy workspace 해제 intent가 남으면 전체 제거 transaction을 롤백하고 해당 삭제를 보류한다. 평가 원문은 대상 배열의 변경 유무와 독립적으로 기존 저장 평가 validator로 형식·저장 source identity를 확인한다. 이 검사는 원문에 저장된 경로/hash 목록을 사용하며 현재 Git 파일의 재검증이나 새 평가 성공이 아니다. 형식·source 귀속이 불명확하면 제거를 보류한다. 발견의 plan_ids가 대상을 포함하면 나머지 ID가 같은 repository의 실행 row 또는 row 없는 취소 감사/최종 삭제 표식으로 귀속되는지 확인하고 대상 ID만 제외한다. 여러 대상이 모두 취소됐어도 남은 연결·감사·호출/사용량은 보존한 채 대상별로 순차 제거한다. 취소 표식과 실행 row가 함께 있거나 어느 근거도 없는 ID는 귀속 모순/미확인으로 보류한다. 대상 단독 발견은 해당 기록만 목록에서 제거한다. 공유 발견의 남은 관계를 확인할 수 없으면 원문 전체를 보존하고 제거를 보류한다. 개정 보고서는 기존 validator로 다시 형식을 확인해 같은 transaction에 저장하며 남은 발견·상태·근거와 coverage·source identity·완료 기준은 바꾸지 않는다. 삭제한 발견을 resolved·성공 상태로 만들지 않고, 마지막 cycle 대상의 정상 종료에 필요한 연결은 기존 종료 gate가 확인할 때까지 보존한다. 유효한 무관 평가 원문은 byte 내용을 그대로 보존한다. 대상 전용 발견의 제거를 resolved·새 평가 성공으로 기록하지 않는다. 게시가 없는 목록 개정은 같은 transaction에서 revision을 한 번 전진시켜 과거 context의 CAS를 무효화한다. 독립 대상·다른 repository·phase·요청 identity·성공/Merge 기준은 유지하며, 빈 대상 배열 자체를 완료나 새 성공으로 만들지 않는다. 확정 취소의 원래 범위가 저장 목록에도 남지 않아 종료 근거를 재구성할 수 없는 경우는 계속 보류하며, cycle 종료 판단은 기존 유지보수 흐름의 별도 책임이다.

취소와 자원 정리·기록/파일 제거는 별도 확정 지점을 갖는다. 필수 정리가 남으면 원문·소유 자원과 cleanup_pending/queue를 한시적으로 보존하고, 실제 부재·원인 해소 뒤 같은 revision에서도 확정 지점부터 이어간다. 유지 branch와 planning snapshot의 문서 반영은 기존 단계별 snapshot/안전 rebase 경계를 사용하며 원문·사용자 코드/index 전체를 reset하지 않는다. G-261의 살아 있는 ref/자원 정리와 의미 판단·소유권 불명의 필요한 보류를 이 경로의 성공으로 대신하지 않는다.

일반·유지보수 planning의 미해결 오류와 입력 지문은 다른 ExecPlan의 차단 해제·PR 상태 변경·project 실행 상태 정리와 독립적으로 보존한다. worktree 경로 부재나 관련 입력이 같은 원격 base 전진만으로 해제하지 않으며, 같은 입력의 반복 실행·재시작에서는 Planner를 다시 호출하지 않는다

관련 planning 입력이 바뀌면 저장된 소유권·workspace identity를 확인하고 관리 worktree에 최신 원격 base를 반영한다. 오류는 입력 전진만으로 해제하지 않고 보존된 결과의 교정·재검증 또는 게시 복구로 원인이 실제 해소된 뒤에만 해제한다. 소유 증거 없는 경로와 복구 실패는 Git 등록·HEAD·index·파일 및 진단·session·작업물을 보존한다

ExecPlan ID만 Git 선언과 SQLite 실행 이력을 잇는 안정적인 identity다. 같은 ID의 유효한 원격 파일에서 path·title·sources·depends_on·complexity·risk·priority 또는 본문이 바뀌면 현재 cache를 갱신하고 상태·실패 이력·Agent session·branch·worktree·workspace·PR 연결은 보존한다

현재 path·title은 compact catalog·로그·조회에 사용한다. 기존 PR의 ExecPlan 링크는 최초 게시 SHA·active path를 유지한다. cache 갱신은 실행 상태·session·branch·worktree·workspace·PR과 최초 게시 감사를 교체하지 않는다.

## 계획 변경 감지

명세 지문·전체 계획 입력 지문·계획 선언 비교는 같은 최신 `origin/<base-branch>` revision에서 계산하며 목적을 구분한다.

| 비교 기준 | 입력과 용도 |
|---|---|
| 명세 지문 | `docs/PRODUCT.md`, `docs/requirements/` 바로 아래 `*.md`, `docs/ARCHITECTURE.md`. 일반 Planner의 최초 수립·명세 변경 판단에 사용한다. |
| 전체 계획 입력 지문 | 명세 입력·활성 ExecPlan과 `docs/` 밖의 파일(소스·테스트·설정 등). 실패 입력·게시·성공 기준의 동일성 판단에 사용하며 이 지문의 변경만으로 일반 Planner를 호출하지 않는다. |
| 계획 선언 비교 | 활성 ExecPlan의 경로·Git object 종류·내용 hash를 비교해 계획 개정과 입력 변경을 구분한다. 현재 선언의 유효성은 별도 catalog 검증을 거친다. |

두 지문에는 현재 runtime의 공통·역할별 실행 지침의 내용 hash도 포함한다. 프로젝트 루트 `AGENTS.md`는 파일 입력에서 제외하며 `docs/`에서는 명세 입력과 활성 ExecPlan만 포함한다. 따라서 archive·PLANS·템플릿·FUTURE_IDEAS는 전체 계획 입력 파일에 포함하지 않는다. 경계와 계산은 `catalog/catalog_service.py`·`planning/input_fingerprints.py`가 소유한다.

성공한 일반 Planner 주기는 새 계획 게시, PR 전 계획 개정·미착수 계획 삭제 게시, FUTURE_IDEAS-only 게시와 추가 계획 없음 판단을 포함하며, 완료할 때 관측한 명세 지문과 전체 계획 입력 지문을 함께 저장한다. 현재 작업의 단계별 문서 반영은 전체 미충족 작업의 일반 planning 성공으로 기록하지 않는다. 자동 Requirement archive의 내용 보존 이동만으로는 두 기준을 무효화하거나 새 작업 주기와 프로젝트 완료 알림을 활성화하지 않는다.

일반 Planner의 최초 수립·명세 변경 판단은 비교 가능한 명세 성공 기준을 사용한다. 공통·역할별 실행 지침의 내용 변경도 호출 조건이며, 같은 내용의 runtime SHA 전진이나 코드·테스트·설정·ExecPlan 또는 전체 계획 입력 지문만의 변경은 호출 조건이 아니다. 지침 비교와 과거 성공 기준의 호환 처리는 `planning/selection_service.py`의 `_specification_planning_required`에서 확인한다.

draft 답변·유지보수·Requirement 완료·프로젝트 최종 확인 등 별도 목적은 [Planner 실행과 Requirement 완료 확인](#planner-실행과-requirement-완료-확인)을 따른다.

새 planning이 필요하지 않으면 활성 유지보수 prerequisite·리팩터링 DAG의 선택을 먼저 적용하고, 일반 READY는 priority와 숫자 ID 순으로 시작한다. 빈 catalog·dependency 대기만으로 Planner를 반복 실행하지 않는다.

due 유지보수의 선택·진행 작업의 비선점 정책은 PRODUCT의 [작업 선택](PRODUCT.md#작업-선택과-동시-실행)·[유지보수 평가](PRODUCT.md#유지보수-평가)를 따른다. [보관된 R-054](requirements/archive/%5BR-054%5D%20Due%20Refactoring%20Priority.md)는 설계 배경으로만 참조한다.

## Git과 worktree 실행 모델

자동 입력과 시작·rebase 기준은 fetch된 `origin/<base-branch>` SHA다. 로컬 base가 미게시 commit으로 앞서 있어도 자동 작업은 원격 SHA에서 시작하며 로컬 commit을 자동 push하거나 입력으로 채택하지 않는다. 기본 worktree는 사용자 checkout으로 보존한다.

| 실행 공간 | 역할과 수정·보존 경계 |
|---|---|
| 기본 checkout | launcher·사용자 설정과 사용자 작업의 위치다. 자동 동기화는 아래의 safe fast-forward·소유권과 로컬 commit 보존이 검증된 rebase 조건에서만 수행한다. |
| planning worktree | Planner가 허용된 계획·FUTURE_IDEAS를 작성하고 Manager가 검증·원격 게시·DB 적용을 수행한다. 미게시 후보·session·복구 intent를 보존한다. |
| 구현 worktree | 선택한 ExecPlan의 구현·허용 문서 수정·targeted test·Manager verify·Review를 이어간다. branch·worktree·session과 실행 이력을 작업 identity에 연결한다. |
| detached runtime | 선택한 원격 exact SHA의 프로그램을 실행한다. repository·소유권·clean detached HEAD를 확인할 수 없으면 기존 경로를 덮어쓰지 않고 중단한다. |

기본 checkout의 부수 동기화와 자동 작업의 원격 입력 선택은 별도 경계다. 구체적인 생성·게시·handoff·복구 조건은 아래 절을 따른다.

작업·동기화·복구가 관여하는 branch는 등록한 base와 소유권을 확인한 Loop Manager 생성 branch다. 그 밖의 사용자 branch는 생성·수정·삭제를 되돌리거나 자동 복구 대상으로 삼지 않으며, 그 변화만으로 계획·프로젝트를 차단하지 않는다. 이름이 비슷하다는 이유로 관리 branch로 간주하지 않는다. 실제 사용 중인 origin·관리 branch·worktree 등 공유 자원의 안전 검사는 유지한다.

Agent snapshot과 격리 ledger는 원문을 감사로 보존한다. 공유 ref·config·다른 worktree 변화나 과거 실행·격리 기록의 부재를 Agent 접근 권한 판정에 사용하지 않는다. Manager의 현재 작업 대상 연결과 Git 쓰기의 충돌·보존·lease 조건은 실제 동작 경계에서 검증하며, 예약 자원의 변경 귀속은 아래의 소유권 복구 경계에서 별도로 증명한다.

Agent preflight의 digest는 현재 역할·canonical worktree·repository·작업 identity에 결합한다. mutable Git snapshot을 digest에 넣거나 과거 OID와 일치시키지 않으며, provider가 실제 적용한 sandbox 정책과 설정 identity는 호출·재개·fallback마다 확인한다. 저장된 시작·결과 snapshot은 역할별 결과 유효성·성공 후보의 재사용과 실제 호출에 연결된 예약 workspace의 변경 귀속 증명에 사용한다. snapshot 일치만으로 접근 권한이나 성공을 만들지 않으며 현재 후보가 바뀌면 이전 verify·Review·게시 token을 무효화하고 필요한 단계부터 수행한다.

### 프로젝트별 Git 순번과 자원 잠금

`TaskScheduler`의 프로젝트별 FIFO와 기존 condition을 사용해 같은 프로젝트의 Git 변경 구간을 한 번에 하나씩 실행한다. 기존 `resource_locks`의 Git scope와 공통 Git 명령 adapter의 직접 변경 명령은 같은 순번을 사용한다. archive는 조율 스레드에서 같은 순번을 요청하므로 뒤에 들어온 fetch·게시가 먼저 실행되지 않는다. 읽기 전용 Git 조회·다른 프로젝트·모델·verify 실행은 이 순번으로 직렬화하지 않는다.

순번 뒤에는 `git:project` 프로세스 잠금과 기존 개별 자원 잠금을 확보한다. 외부 CLI·다른 프로세스와의 실제 경합, 후보 writer·수동 소유·미완료 journal 검증은 유지한다. 같은 스레드의 중첩 Git scope와 detached 게시 worktree의 명령은 현재 등록 프로젝트의 순번을 재사용하며, 구체 자원 잠금의 소유자·lease는 각 기존 경계에서 확인한다. Git 순번을 모델 호출·충돌 해결·검증 전체의 실행 자리로 사용하지 않는다.

대기 중에는 DB 요청을 유지하지 않고 기존 `_waiting` 구간으로 실행 시간에서 제외한다. planning·runtime 중단을 재확인하고 취소·예외·종료 때 FIFO 항목과 잠금을 반환해 뒤 요청을 깨운다. 프로젝트 준비용 base fetch의 planning 예외와 이미 확정한 runtime handoff는 유지한다. FIFO는 실행 중 메모리 순서이며 재시작 때 기존 checkpoint·journal·실제 자원에서 미완료 처리를 재구성한다. 새 영속 큐·설정·상주 스케줄러는 추가하지 않는다.

### 미확정 Git 작업의 단계별 자원 판단

`manager_git_journal.has_conflicting_project_mutation()`은 planning 해제·Requirement 교정·archive 진입·수동 준비/slot/release가 사용하는 공통 관측 경계다. 프로젝트의 모든 pending intent를 읽으며 targets 밖이라는 이유로 공유 자원 안전을 추정하지 않는다. 원본 source fast-forward/rebase 기록은 기존 source 동기화가 진단하며 별도 관리 작업의 gate로 승격하지 않는다. 정확한 수동 own-operation 복구는 owner·kind뿐 아니라 생성 worktree 또는 push ref까지 일치해야 한다.

별도 `managed_rebase`의 격리는 다음 근거를 함께 확인할 때만 허용한다.

- 실제 slot의 active와 불변 intent가 일치하고, 현재 소유 완료된 G row의 branch·worktree가 기록과 같다. 다른 작업·수동 묶음·planning의 같은 자원 claim과 기본 branch 충돌은 거부한다.
- `rebase_uses_separate_workspace()`가 원본·이번 단계의 worktree 및 local/published ref와의 분리, intent의 작업 owner/원래 branch, 실제 worktree 등록과 중복 branch 등록 부재를 확인한다.
- 기존 자원 lease를 plan·branch·worktree·Git target·공유 config에 적용하고 이 관측 경계가 직접 보유한 열린 lease만 재사용한다. `plan_recovery.check_stopped_writers()`로 원인 작업의 호출 예약·invocation과 프로젝트 범위 invocation의 실제 종료 또는 미시작 근거를 확인한다. 결과 관측만으로 종료를 합성하지 않는다.
- Git common directory가 같은 저장소임과 include를 적용한 현재 공유 local config·직접 local branch ref의 tip을 관측 전후 비교하고, 소유권·호출 예약·writer 기록·checkpoint와 active/불변 intent도 다시 대조한다. 과거 repository snapshot 비교나 새 lock/journal 서비스는 만들지 않는다.

Git·프로세스 관측과 잠금 대기는 DB 요청 밖에서 수행하며 DB transaction이 열려 있으면 외부 관측 대신 보류한다. 수동 준비·수락·등록과 자리 반환은 별도 rebase를 관측하며 취득한 lease를 호출자의 `ExitStack`에 연결해 상태 적용까지 유지한다. 정상 반환·조기 보류·예외에서는 같은 범위가 lease를 반환하며 수동 후속 자동 실행에 진입하기 전에 보호 범위를 닫는다. 외부 판단 전후의 `project_mutation_identity()`를 비교하고, 짧은 DB CAS에서는 같은 journal·DB identity와 `project_mutation_leases_owned()`의 현재 process/thread 소유 열린 handle을 함께 확인한다. 수동 자리 반환도 Git 충돌 조회 뒤 해당 identity·lease와 선택 row·묶음을 재검사한다. DB에서 lease를 새로 취득하거나 Git·프로세스를 관측하지 않는다. identity가 같아도 lease가 반환됐거나 다른 실행이 소유하면 과거 허용을 재사용하지 않는다. 이 identity는 지속 저장되는 성공 receipt나 재사용 승인 token이 아니다. 기존 source/draft·게시 예약·membership·dependency·프로젝트 안전·archive 유휴 조건은 소비자가 계속 소유한다.

판정은 journal·충돌 파일·index·checkpoint·차단을 변경하지 않는다. 읽을 수 없는 기록과 알 수 없는 operation은 보류하며 완료 확정은 기존 operation별 완료 증명만 사용한다. 기존 operation log 경계에 owner·operation·intent ID·stage·resource/resources·allow/hold·reason·next_condition을 전달하고, 제어는 로그 문구를 파싱하지 않는다. 공통 config 원문과 인증값은 로그에 넣지 않는다.

### 예약 workspace의 소유권 복구

일반 실행 준비와 프로젝트 소유권 차단 재평가는 `git/workspace.py`의 같은 변경 이력 판정을 사용한다. `workspace_owned=2`는 생성 예약이며 실행 기준 `active_sha`를 생성 HEAD로 사용하지 않는다. 봉인된 version 1 `managed_worktree_create`의 owner(`plan:{G-ID}`)·canonical repository/target·branch·생성 HEAD·index와 현재 자원을 대조하고, 현재 branch/lock identity를 유지한 승인된 전이만 연결한다. 다른 작업의 ref/worktree 변화는 이 자원의 비교 대상이 아니다.

| 변경 근거 | 검증과 보존 계약 |
|---|---|
| Manager commit·Worker checkpoint | 기존 checkpoint의 before/after HEAD·message·tree와 실제 commit의 단일 parent·message·tree를 대조한다. 저장된 Worker checkpoint는 repository·작업·branch·worktree도 일치해야 한다. |
| 봉인된 rebase | 기존 managed rebase 검증 결과를 소비한다. 현재 정책의 완료 receipt는 정확한 kind/owner/target과 acceptance identity를 검증한 예약 이력 조회에만 추가 제공한다. `observed.validation_observation`에 판정에 사용한 관측 전체를 저장하고 그 fingerprint가 acceptance의 `state_fingerprint`와 같은지, HEAD·branch·index·dirty·operation·metadata가 이 관측의 투영과 같은지 기록·조회 경계에서 확인한다. metadata는 기존 journal과 동일하게 진행 중이면 object, 없으면 null로 유지한다. 실행·직접 복구 모두 판정 뒤 파일을 별도로 재관측해 receipt를 만들지 않는다. 결합 자료가 없거나 불일치하는 기존 기록은 보존하되 승인된 파일 전이로 사용하지 않는다. 수락 HEAD의 clean index는 Git tree에서 읽고 완료 관측에 봉인된 unstaged/untracked 파일 서명을 함께 이어 사용한다. operation이 해제돼도 이 보존 상태는 이력에 남으며 별도의 rebase 이후 Worker 출력을 요구하지 않는다. |
| 미커밋 Worker 출력 | 저장된 시작 digest·scope와 실제 종료한 invocation의 scope/role을 연결한다. 승인된 시작 상태에서 기록된 결과까지 HEAD·branch·index·파일 서명·operation을 대조한다. 이 자료는 소유권 복구 근거이며 Agent 접근 권한 gate가 아니다. |
| 미완료 Manager operation | commit·push·PR 생성·중간 rebase 게시는 해당 checkpoint intent와 현재 후보를 대조한다. commit 전 staging 중단은 저장된 tree와 현재 index가 같고 도달 가능한 Worker 출력의 파일 서명이 보존된 경우에 연결한다. rebase는 저장된 원래 HEAD·target·generation으로 기존 현재 상태 검증기를 읽기 전용 호출해 원격 lease·파일·index·operation까지 검증한다. Git 실행 전 clean 상태의 START도 이 판정을 거치며 소유권 확정 시 operation을 완료 처리하지 않는다. |
| 기존 원격 인수 | version 1 `remote_adoption`의 repository·작업·base·branch·시작 HEAD를 생성 근거와 대조하고 최초 catalog의 선언별 grant·현재 origin fetch/push URL·PR 연결을 확인한다. 병합되지 않은 CLOSED PR은 workspace 확정 전에 기존 보류 처리로 보낸다. MERGED 또는 mergedAt이 확인된 기존 예약은 공통 이력 검증·최종 재관측·CAS로 소유권을 확정하고 예약 복구 lease를 반환한 뒤 기존 terminal 완료·cleanup helper로 보내고 Worker는 재개하지 않는다. 두 소비 경로는 terminal 처리 대상을 반환값으로 인계하며, cleanup은 자신의 삭제 journal lease와 기존 자원 재검증으로 진행한다. 생성 kind에 한정한 중첩 lease 재사용을 삭제로 확대하지 않는다. cleanup은 확정된 소유권과 미커밋 자료를 보존하며, 불명 PR 상태는 보류한다. 기존 예약을 신규 인수로 바꾸거나 원래 시작 HEAD·PR·검증 이력을 덮어쓰지 않는다. |

등록 관측은 선택 자원의 존재·HEAD·branch·lock과 index tree를 담는다. unmerged index는 `write-tree` 대신 NUL 구분 stage 원문을 보존한 관측으로 표현하고, 전체 snapshot의 stage·파일·operation도 함께 재확인한다. 근거가 없거나 손상·불일치한 상태는 소유권을 확정하지 않으며 reset·강제 checkout·일괄 파일 복원·사용자 operation abort로 우회하지 않는다.

`recovery/plan_recovery.py`가 대상 plan·branch·worktree·Git 자원 lease와 writer 종료/미시작을 먼저 확인한다. 현재 process/thread의 열린 handle로 확인되는 기존 lease만 이어 쓴다. journal의 중첩 생성·생성 복구 호출은 `workspace-reservation-recovery` 소유 Git lease에 한정하고, 생성 owner가 명시되면 해당 plan lease도 확인한다. 같은 thread가 가진 다른 소유자의 Git lease는 빌리지 않으며 다른 복구·수동 묶음·게시 claim과 종료 불명 호출은 보존·거부한다. Git·프로세스·원격 관측은 DB 쓰기 처리 밖에서 수행한다. 확정 직전의 local 자원·PR 관측과 짧은 transaction의 작업·checkpoint generation·호출 예약·invocation·인수 grant/소유 claim 재검사로 낡은 결과의 반영을 막는다. DB 실패에는 소유권/해제 변경을 함께 rollback하고 기존 예약·경쟁 변경·session·실패·성공 감사를 보존한다. 복구만으로 단계 성공이나 최신 base 수용을 기록하지 않는다. 이미 소유권이 확정된 작업의 소유권 차단 해제도 같은 writer 종료·자원 lease 경계를 사용한다. 미완료 rebase는 `START`·`WAITING`·`COMPLETE`의 현재 소유 상태와 동일한 HEAD·branch·파일·index·operation 관측을 읽기 전용으로 검증하고 checkpoint의 원래 HEAD·target·operation을 유지한다. 해제 자체로 frontmatter 정합화·rebase 수용·완료를 수행하지 않는다. 짧은 확정 transaction은 작업·프로젝트 원인뿐 아니라 checkpoint generation·독립 계획 차단·복구 기록·연결 게시 요청과 자원 claim도 다시 비교한다.

### 기본 worktree 동기화

| 상태 | 처리 |
|---|---|
| 저장 base branch·Git operation 없음·HEAD가 원격과 equal | dirty 상태도 변경하지 않고 no-op |
| behind-only이며 Git operation·비정상 index·경로 겹침 없음 | 로컬 변경과 index를 보존하는 safe fast-forward |
| ahead-only이며 로컬에 미게시 commit이 있음 | 로컬 commit·checkout을 보존하고 동기화만 건너뜀. 자동 작업은 원격 SHA에서 계속 |
| clean diverged·선형 로컬 commit·durable 소유 intent·경로 충돌 없음 | 원격 base 위로 로컬 commit을 보존하는 rebase. 자동 push 없음 |
| 다른 branch·detached·dirty 또는 비선형 diverged·operation·비정상 index·겹침·동시 권위 변경 | 이유를 기록하고 동기화만 건너뜀 |

경로 겹침은 원격이 변경할 tracked 경로와 staged·unstaged·untracked·ignored 로컬 경로를 대소문자 및 부모·자식 관계까지 비교한다. eligibility 뒤 사용자 변경·Git lock·실패가 있으면 reset·clean·stash·강제 checkout이나 잠금 제거로 우회하지 않는다. safe fast-forward는 이미 fetch한 원격 ref에 대한 `git merge --ff-only` 상당의 적용이며 중복 fetch를 하지 않는다.

source snapshot은 경로 충돌·파일 보존에 필요한 ignored 경로·서명과 Git config·외부 exclude 근거를 수집한다. 실행 직전·정상 완료·재시작 복구의 비교 view는 Agent 복구의 시작 ignore 규칙 검증을 재사용해 시작·저장·현재 index에 없는 경로만 제외한다. tracked ignore 규칙은 blob·index·작업 상태가, 외부 exclude는 저장된 경로·서명이 일치해야 한다.

Git 쓰기 직전에 현재 경로를 다시 검사하고 fast-forward는 Git의 `--no-overwrite-ignore`도 적용한다. ignored-only 차이는 같은 실패 입력의 재시도를 허용하지 않으며 rebase abort의 실제 tracked 파일·index·operation 증명도 완화하지 않는다.

source journal의 원본 intent·invocation·관측과 완료 감사는 비교 view로 덮어쓰지 않는다. 실제 전이와 소유 표식이 검증되면 결과를 먼저 기록한 뒤 활성 기록을 종료하며, 결과 기록 후 중단된 경우에도 기존 결과를 보존한다. config 근거가 없는 과거 기록은 시작에 관측된 ignored 경로의 unchanged tracked 규칙을 재구성할 수 있을 때만 차이를 제외한다. 새 경로나 외부 exclude의 시작 근거가 부족하면 기록·파일을 보존하고 거부한다.

안전하게 완료한 뒤 원격 base가 더 전진했으면 현재 원격의 동기화 판단을 이어간다. source 기록은 Agent의 ref·worktree 기준선 후보로 제공하지 않으며 별도 관리 작업의 실행·게시 차단 근거로 사용하지 않는다.

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

runtime 자식은 등록된 canonical source·repository/origin·base·실제 runtime 경로/SHA를 검증하고, 선택된 프로젝트 목록과 별도로 self를 찾는다. `application/self_runtime.py`의 원격 관측은 기존 fetch journal·자원 잠금을 사용하며 PR 조회나 source 동기화를 수행하지 않는다. 일반 PR 수명주기의 base identity 검증·Merge 반영은 별도로 유지한다.

실제 단계 진입 전 `RuntimeHandoffGate.observe()`가 관측을 직렬화한다. 실행·planning·복구 coordinator와 실제 Agent/verify 호출 경계가 이를 호출한다. 호출 slot 대기와 RETRY 재등록은 관측하지 않는다. Agent는 기존 provider 진입에서 DB 준비를 마친 뒤 관측하고 verify는 slot·예약과 필수 preflight를 통과한 뒤 관측한다. 최초 관측은 즉시 수행하고 성공 완료의 monotonic 시각을 runtime 전체에서 공유해 30초 미만이면 원격 조회·입력 비교를 재사용한다. 병렬 진입은 잠금 안에서 시각을 다시 확인하며 실패는 성공 시각을 갱신하지 않는다. 단계 종료와 재시도·사용량·기존 호출 종료 대기는 원격 관측을 만들지 않으며 pause·확정된 handoff 신호만 확인한다. 대기가 풀린 뒤 실제 단계 진입에서 같은 30초 기준으로 다시 확인한다. 관측 잠금을 기다리기 전에 완료한 단계의 implicit DB 요청을 확정하고 명시적 transaction 내부 관측은 거부하므로 DB 쓰기 transaction·scheduler condition·launch lock 안에서 원격 관측하지 않는다. 실제 실행 SHA와 원격 commit의 raw diff 및 blob을 비교해 추가·삭제·이동·mode/kind 변경을 포함한다. 알려진 문서 위치의 일반 UTF-8 prose만 제외하고 실행 bit·symlink·shebang·Python 구문과 문서 아래 실행 코드는 포함한다. 알 수 없는 경로와 비교 실패는 문서 전용 성공이 아니다.

원격 SHA/코드가 같으면 실행을 계속한다. 코드가 다른 fast-forward 후속이면 원자 launch gate에 source/origin/base·실행/목표 SHA·mutation identity를 결합해 신규 관리·Agent·verify·게시를 막고 진행 중 호출은 반환·저장을 기다린다. 부모는 자식별 일회용 channel token과 논리 run ID에 결합한 `runtime-handoffs/<mutation>-<token>.json` 및 전용 반환 코드 75를 함께 검증한다. 임의 파일·출력·일반 종료 코드는 재시작 근거가 아니다. 확인한 자료는 consumed로 남기고 재소비하지 않는다. 미확정 반환은 별도 보존 코드 76으로 부모에 전달하며 새 runtime이나 기존 runtime 제거를 허용하지 않는다.

부모는 동일 process lock 아래 최신 등록 base를 다시 fetch·선택하고 관측 목표의 후속인지와 코드 진전을 검증해 최신 revision의 bootstrap 진입점·runtime을 시작한다. 자식별 mutation ID는 새로 만들고 절대 config·interpreter·PATH·인자·bytecode 정책·논리 run ID는 유지한다. scheduler의 external run marker도 이 논리 ID를 사용하므로 물리 runtime 교체가 실패 감사를 새 실행으로 만들지 않는다. 전체 pause의 원본은 G-236의 `pause.db/pauses(scope)`이며 bootstrap 전·재선택 전·새 자식 전과 실행 gate에서 확인한다. 프로젝트 pause는 대상만 제외한다. pause 제어 명령·진행 중 호출 종료 제어는 해당 pause 수명주기가 소유한다.

### 선택 전 상태 동기화

1. 프로그램이 `PROJECT_BLOCKED`의 저장소 안전을 확인하고 저장된 모든 Merge 대기 PR의 GitHub identity와 Open·Merged·Closed 상태를 조회한다. 안전 gate가 허용하는 Merge·Close 전이와 worktree cleanup 판정 및 알림 outbox를 기본 worktree 동기화보다 먼저 SQLite에 기록한다. 프로젝트 안전이 증명되지 않으면 기본 worktree 동기화와 일반 저장소 변경 단계를 실행하지 않는다.

   계획과 연결된 차단의 검증 가능한 대상 작업 복구·보존은 아래의 계획별 실행 자리와 현재 상태 검증 경계에서만 수행한다. 단, 원격 ExecPlan catalog 오류 차단은 원인 push를 fetch하고 재검증해야 하므로 이 저장소 안전 gate와 별도로 처리한다
2. 프로젝트 안전 gate가 열려 있으면 새 Merge 뒤 `origin/<base-branch>`를 다시 fetch하고 최신 ExecPlan·project 상태를 읽는다. 가능한 ExecPlan archive 게시를 먼저 완료한 뒤 최종 원격 SHA를 기준으로 기본 worktree의 safe fast-forward eligibility와 catalog를 평가한다. 저장된 `PR_OPEN` mirror, 다른 안정된 Open PR 또는 충돌 marker만으로 평가를 생략하지 않는다.

   그 뒤 각 `PLAN_BLOCKED`의 계획별 해제 조건 및 `planning_error`의 근거 입력 변경 여부를 독립적으로 확인한다

기존 Open PR도 [GitHub와 PR](#github와-pr)의 Merge 가능 여부를 확인한다. 충돌이 확정된 PR은 안전·dependency·실행 자리 조건을 확인한 뒤 관리 branch를 최신 원격 base에 rebase해 같은 PR을 갱신한다.

그 뒤 마지막 관련 ExecPlan의 실제 Merge와 catalog 반영이 확인된 Requirement를 다음 안전한 경계에서 바로 재확인하고, 실행 중인 변경 재개·확정 PR 충돌 복구·유지보수·일반 Planner·READY 선택을 이어간다. 실행 중 writer·소유 Git operation·실제 프로젝트 안전 gate를 우회하지 않는다.

dependency와 일반 작업 간 순서는 [PRODUCT의 작업 선택 정책](PRODUCT.md#작업-선택과-동시-실행)을, Worker·검증·Review·PR 생성은 [제품 처리 흐름](PRODUCT.md#execplan-생성과-자동-처리)을 따른다.

실행 조율의 `run_project_tasks`는 진입·완료 수집·새 배정 뒤 다음 선택 전에 [MERGED archive](#merged-execplan-archive) 기회를 제공한다. 후보 조회 DB 대기는 별도 보류로 기록하고 독립 배정을 계속한다. 기존 대기 loop에서는 후보·소유 예약·미확정 journal·종료 영수증 metadata·lease 반환만 관측한다. 관련 상태 변화는 새 안전 검사 기회이며 쓰기 권한이나 완료 증거가 아니다. 시도 전 관측만 기억하고 처리 도중 후보나 자원이 바뀌면 다음 배정 전에 다시 검사해 새 MERGED 전이를 놓치지 않는다. 같은 대기 상태에서 원격 조회를 반복하지 않으며, archive가 ref를 전진시켰으면 기존 planning 입력을 재확인한 뒤 다음 작업을 배정한다. planning drain·runtime handoff·사용자 pause는 이 경계에도 적용한다.

### Planner 실행과 Requirement 완료 확인

일반 Planner의 최초 수립·명세 변경 판단은 최신 원격 PRODUCT·활성 Requirement·ARCHITECTURE와 적용된 일반 planning 성공 기준을 비교하며, 지침 변경은 [계획 변경 감지](#계획-변경-감지)의 별도 조건을 따른다.

Planner도 미충족 요구와 필요한 계획을 판단하기 위해 관련 ExecPlan·현재 구현·테스트·설정을 읽는다. 이는 호출 조건과 구분한다. ExecPlan·코드·테스트·설정의 변경이나 전체 저장소 SHA 차이만으로 일반·점검·작업별 영향 Planner를 호출하지 않는다. ExecPlan 선언은 Manager의 catalog·실행 자격 검증에도 사용하고 구현·검증 대응은 Worker·Manager verify·Reviewer의 기존 경계에서 처리한다.

Manager가 유효한 선언·source·dependency·사용자 결정을 반영하고 Worker가 최신 작업 조건과 현재 코드를 대조해 필요한 구현·targeted test를 수행한다.

Planner의 최초 계획 수립·명세 변경·draft 사용자 답변·추적 작업이 필요한 명시적 수동 제출·유지보수·Requirement 완료 재확인·프로젝트 최종 확인은 R-082의 목적별 조건을 적용한다. 같은 경계의 여러 대상·목적은 한 호출로 처리할 수 있으며, Requirement별 판단·프로젝트 전체 planning·최종 전문 확인·유지보수의 입력과 성공 근거를 구분해 보존한다. 재시작·원격 재관측에서는 유효한 결과·게시 intent의 남은 적용부터 복구하고, 실제 입력 변경·필수 판단 누락·계획 작성 범위의 충돌만 해당 세션 교정으로 연결한다.

PR 상태 동기화 뒤 마지막 연결 ExecPlan이 실제 MERGED가 된 활성 Requirement는 유효한 source 관계·관련 draft·현재 입력을 확인해 완료 재확인 대상으로 삼는다. 다른 Requirement의 READY·Open PR·완료나 프로젝트 전체 종료를 기다리지 않으며 독립된 새 작업·유지보수 cycle 선택 전에 처리한다.

각 Acceptance Criterion을 최신 관련 명세·구현·테스트·실제 검증에 대조해 충족은 판단 기록·원문 archive, 미충족은 후속 ExecPlan, 필수 사용자 결정은 draft, 불확실은 보류로 반영한다. 같은 입력의 유효한 판단은 재사용하고 archive 게시·catalog·DB 적용은 기존 소유권·publication transaction·원격 재확인 경계를 유지한다. 대상 관측 중 판단 무효화와 불확실 기록은 각각 짧은 DB transaction에서 저장해 후속 Git 관측·관리 배정 대기까지 쓰기 잠금을 유지하지 않는다.

### 프로젝트 최종 확인과 완료 판정

최종 전문 확인은 일반 변경 검토·Requirement별 충족 판단·유지보수 평가와 별도 증거다. 시작과 완료 gate는 [PRODUCT의 프로젝트 완료 조건](PRODUCT.md#프로젝트-완료-조건)을 따른다. 선택 경계는 비종료 작업·수동 소유·catalog hold·차단·오류와 일반/유지보수의 미확정 게시를 확인한다. 유지보수는 활성·due 여부와 게시 상태를 검사하며, 첫 주기 전이나 자동 전환 비활성화 상태에 과거 정상 종료 기록을 새로 요구하지 않는다.

Planner의 표준 결과는 `FINAL_SPECIFICATION_REVIEW: <JSON object>` 하나이며 기존 결과 정규화·identity 결합 경계를 적용한다.

| 필드 | 형식·검증 |
|---|---|
| `revision`·`input_fingerprint`·`specification_fingerprint`·`work_cycle` | 검증된 현재 attempt의 문자열 identity에 결합한다. 명시된 불일치는 거부하며 원격 SHA 차이만으로 내용을 같다고 추정하지 않는다. |
| `outcome` | `fulfilled`·`followup`·`needs_decision` 중 하나 |
| `reason` | 확인한 규칙·근거·판단 이유를 설명하는 비어 있지 않은 문자열. 비밀값·원문 전문을 넣지 않는다. |
| `evidence` | 비어 있지 않은 프로젝트 상대 경로 문자열 배열. 고정 revision의 일반 blob(mode `100644`·`100755`)이어야 하며 모든 활성 명세와 명세 외 구현·검증 근거를 포함한다. 절대 경로·역슬래시·빈/`.`/`..` 경로 요소, archive Requirement/ExecPlan·활성 ExecPlan·FUTURE_IDEAS를 완료 근거로 사용하지 않는다. |

`fulfilled`에는 미완료 후속 계획·계획 개정·삭제·복원 이동을 남길 수 없다. `followup`에는 새 후속 계획, `needs_decision`에는 새 계획과 적어도 하나의 draft가 필요하다. 후속 계획은 확인한 활성 명세 source에 연결하며 기존 Manager source는 별도 등록 요청 검증을 통과해야 한다. 형식·identity·근거·후속 계획 불일치는 기존 planning 교정 경계로 반환하고 성공을 저장하지 않는다.

`planning_reviews`는 attempt, 일반 성공과 최종 성공을 분리한다. 검증 결과를 attempt에 저장한 뒤 원격 확인·게시 적용 transaction에서 관측 revision·확인 revision·입력 지문·작업 주기·결과를 확정한다. 현재 최종 성공은 최신 일반 성공도 같은 final 결과를 가리킬 때만 사용한다. 후속 일반 검토 성공은 이전 final의 현재 유효성을 해제하되 과거 결과·감사는 보존한다.

최종 확인의 `work_cycle`은 MERGED 수·유지보수 revision·Merge된 작업의 검증 identity에 결합한다. 재사용은 확정된 `fulfilled`의 현재 입력·명세 지문·주기 일치를 요구하며, 원문 보존 archive-only 이동은 유효한 계획 선언과 정확한 이동을 검증한 예외로만 허용한다. 상태 공백·catalog 부재·일반 검토의 추가 계획 없음이나 검토 파일명만으로 완료를 합성하지 않는다.

안정적 완료·작업 소진의 원본은 현재 원격 입력·catalog·SQLite 상태다. 완료 기록의 `cycle_key`는 유효 planning 입력 지문이며 최종 확인의 `work_cycle`과 구분한다. `project_completion_cycles`의 완료 시각·마지막 Merge 계획·세대와 `work_reason_episodes`의 원인 지문·세대는 Telegram 활성 여부와 독립적으로 유지한다. 안정 관측은 기존 선택·복구·유지보수 결과와 전체 대상 상태를 대조하고, 다른 대상의 호출 예약이나 종료 미확인 invocation이 남으면 보류한다. 관리 인계·순번만으로 실제 진행을 만들지 않는다. 완료와 대기 사건은 각각 해당 주기와 episode에 결합한다. Merge 후 worktree cleanup·ExecPlan archive pending은 완료를 막지 않지만 미완료 planning publication·활성 writer·안전 gate를 우회하는 근거로 사용하지 않는다.

### 계획 게시와 작업 공간

계획을 실행 이력에 적용하기 전에 원격 게시와 catalog 전체의 유효성을 확인한다. 계획·FUTURE_IDEAS 변경이 없으면 관측한 두 입력 지문을 저장하고 planning worktree를 정리해 완료 여부를 확인한다.

1. 생성·개정·rename·삭제·FUTURE_IDEAS 변경을 분류하고 새 파일의 ID·경로를 아래 규칙으로 정규화한다. snapshot 원본, SQLite의 현재 실행 근거·PR 부재 및 적용 후 catalog의 ID·source·dependency graph를 검증하며 시작한 계획의 ID·경로와 실행 이력은 보존한다.
2. 실패는 같은 Planner 세션에서 최대 3회 교정한다. 소진하면 원인·입력 지문·결과·worktree·session을 보존하고 같은 실패 입력의 신규 Planner 호출을 반복하지 않는다. 미종료 writer·미확정 게시 intent가 없고 소유권·Git operation·저장된 역할 결과의 유효성을 검증할 수 있으면 실행 자리를 비워 미확인 영향 밖의 게시된 독립 작업과 PR 관측을 계속한다. 해제 조건이 불확실하면 기존 상태를 보존해 대기하며 `planning_error`는 원인이 실제 해소되기 전까지 유지한다.
3. 검증된 전체 변경을 planning worktree의 한 commit으로 만든다.
4. 아직 게시하지 않은 동안 원격 base가 전진하면 변경·session을 보존해 반영하고 최신 catalog·대상 권한과 관련 문서를 재검증한다. 충돌·원격 재작성·소유권 불명확은 작업을 보존해 복구 경계로 보낸다.
5. 기준과 mutable READY snapshot이 그대로면 복구 가능한 publication intent를 저장하고 base에 fast-forward push한다.
6. 원격 commit을 확인한 뒤 생성 row 등록·개정 cache 갱신·삭제 row 제거와 ID 취소 감사·해당 모드의 성공 기준을 한 SQLite transaction으로 반영한다. 기존 프로젝트의 `publication_remote_oid`에 적용 revision을 저장하고 planning 소유권·결과·session을 유지한다. 일반 publication intent도 최종 해제까지 보존한다. FUTURE_IDEAS-only 변경은 실행 상태를 만들지 않는다.
7. DB 밖에서 원격 이력과 게시 경로 보존, 소유 worktree의 HEAD·index·Git operation을 확인하고 worktree를 정리한다. 프로젝트·cycle·intent·catalog·수동 membership과 미종료 호출을 다시 확인한 별도 DB CAS가 planning 자리를 해제한다. 미확정 Git journal은 [공통 자원 판단](#미확정-git-작업의-단계별-자원-판단)으로 별도 관리 rebase의 독립성을 확인하며 최초 게시와 중단 복구에 같은 검사를 적용한다. 원인 intent·충돌·작업 상태는 변경하지 않는다. 적용 뒤 정리 실패는 성공 기준을 되돌리지 않으며 작업 재개는 최종 해제 뒤에만 허용한다.

PR·Git blob·로컬 원문/hash·draft 입력·공식 선언·root 계획 연결과 승격 근거는 DB 적용 전에 호출 내 불변 입력으로 준비한다. DB 확정은 준비한 snapshot·branch/generation·소유권·cycle·intent와 현재 상태를 대조하고 등록·관계·감사를 원자 적용한다. 기존 DB 요청 runner의 재시도는 같은 준비 값을 사용하며 Git·Planner·worktree 제거를 실행하지 않는다. 지침 hash와 성공 지문도 준비 값으로 전달해 DB 적용 중 원본을 다시 읽지 않는다.

복구는 적용 revision이 남은 정리 대기를 prepared intent보다 먼저 처리한다. 제거 뒤 DB 해제가 중단되면 소유 경로의 부재와 Git 등록 제거, 실제 원격 게시 보존을 확인하고 남은 CAS만 수행한다. 게시 후 원본 변경으로 유지보수 재평가가 필요한 경우에는 prepared cycle과 게시 OID를 함께 유지해 재시작 정리 대기를 구분한다. 정리 뒤의 CAS가 cycle 재시작과 자리 해제를 함께 확정하므로 중단 시 기존 intent·결과로 이어갈 수 있다. 아직 게시하지 않은 후보는 기존 소유 worktree의 원격 반영 경계를 따르며, 관측 중 게시 사실이 달라지면 intent와 작업물을 보존한다. SQLite schema와 publication intent·감사 JSON 형식은 유지한다.

#### 정규화 기록과 작업 공간

snapshot의 rename으로 확정되지 않은 미게시 새 파일은 전체 catalog를 읽기 전에 별도로 정규화한다. Manager는 active·archive 파일과 SQLite 등록·취소·수동 예약 및 mutable/held snapshot에서 사용 중인 ID를 모아 새 batch에 연속 번호를 배정하고 ID·title에 맞는 새 경로로 옮긴다. 같은 batch의 서로 다른 새 파일이 같은 ID를 쓰면 배정을 멈춘다.

새 계획과 이번 batch에서 수정한 snapshot 계획의 `depends_on` 중 대상이 확인된 batch ID만 새 ID로 갱신하며, 기존 dependency 순서·계획 identity·본문은 보존한다. 기존 ID와 batch ID가 충돌하는 관계는 의미를 정할 수 없어 오류로 남긴다. 재배정 대상 ID가 본문에 언급돼도 이를 고치거나 대상을 추론하지 않고 오류로 남긴다. draft와 `User Decision`의 원문은 보존한다. 정규화 뒤 기존 전체 catalog·source·dependency 검증을 수행한다.
정규화는 일반·유지보수 planning 모두에 적용한다. 원본 bytes·mode와 대상 hash를 편집 전에 복구 journal에 기록하고, 완성한 임시 파일을 원자적으로 설치한다. 다음 검증 진입에서 미완료 journal을 확인해 원본을 복구한다. 복구로 제거된 journal 소유 임시·대상 경로는 호출자가 미리 수집한 변경 목록에서 제외하고, 복구 후 현재 변경을 다시 관측해 범위를 검증한다. 예상과 다른 파일이 있으면 덮어쓰지 않고 작업과 journal을 보존한다.

정규화 결과는 기존 planning result와 분리해 원본 경로·ID, 배정 경로·ID로 일반 planning manifest 또는 유지보수 publication intent에 보존한다. manifest의 기준 revision, 실제 commit 경로와 파일 hash를 대조하고, 원격 확인 및 catalog 등록은 기존 publication intent·lease 경계를 사용한다. 재개는 저장된 manifest를 그대로 사용하며 revision·diff·소유권이 다르면 게시하지 않는다.

planning worktree는 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`, 구현 worktree는 `<state-dir>/worktrees/<project-id>/<execplan-id>/`에 둔다. 구현 branch 이름은 `loop-manager/<execplan-id>`다. 계획은 Worker 시작 전에 base에 게시하며 구현 PR에서 처음 생성하지 않는다.

#### 개정 snapshot과 작업 취소

일반 planning snapshot은 실제 PR이 없는 READY·진행 중 선언과 일반 `PLAN_BLOCKED` 선언을 Git 원문과 SQLite의 선언·실행 근거에 결합한다. 후보 선택은 공통 취소 감사·최종 삭제 ID 집합을 먼저 제외하며 잔존 실행 row의 상태만으로 그 ID를 되살리지 않는다. 저장 snapshot의 PR 관측 준비와 게시/CAS 재검사도 현재 repository/ID의 공통 취소 조회를 수행한다. 미리 준비한 PR 관측은 이 검사를 대신하지 않으며 다른 repository의 같은 ID 표식은 영향을 주지 않는다. 차단 상태는 삭제·참조 복구 허용 집합과 별도로 판단하며, 독립 catalog hold는 복구 proof로 허용되지 않는 한 후보에서 제외한다. 실행 이력·branch·worktree·세션·checkpoint는 개정 금지 사유가 아니며, 게시 직전에 실행 근거와 실제 PR 부재를 다시 확인한다.

R-077의 수동 소유 계획과 start/submit 준비 intent 대상은 후보에서 제외하고, 계획별 수동 group·membership 지문을 snapshot에 결합해 intent 예약과 게시 transaction에서 현재 소유와 변화를 다시 확인한다. snapshot 이후 시작·등록·membership 변경은 해당 계획의 개정을 거부하고 독립 계획의 게시를 막지 않는다.

실제 종료·정당한 대상 해제 뒤 남은 과거 membership/audit만으로 현재 소유권을 추정하지 않으며, 해당 계획의 불완전한 membership 증거는 그 계획의 개정 게시를 거부한다. 게시 적용은 선언 cache와 revision만 갱신해 기존 status·차단 사유·질문·checkpoint·실행/실패/승격 감사를 유지하고, 이미 시작한 계획의 effective level을 낮추지 않는다. 원격·catalog·DB 반영 확정만으로 차단 원인을 해소하지 않으며 원인별 재평가는 기존 실행 경계에서 수행한다. Planner의 직접 이름 변경·삭제와 유지보수 snapshot은 기존 미착수·무소유 READY 범위를 유지한다.

검증된 Requirement 전체 철회는 Manager가 writer·실행 snapshot·실제 PR 부재를 재확인하고 삭제 전 원문·판단 영수증을 보존해 시작한 작업의 문서 취소를 준비할 수 있다. 원격 문서 자체의 삭제도 원문 hash·ancestor revision·완료된 writer 결과·PR 부재를 증명한 범위만 취소한다. 계획 삭제는 활성 row 제거와 ID 취소 감사를 같은 transaction에서 기록하며 ID를 재사용하지 않는다.

취소 이력은 원문·전체 실행 row·관련 선행 참조와 보존 자원의 정리 대기 이유를 가진다. branch·worktree는 그 증거만으로 제거하지 않는다. 최초 activation marker는 이후 READY로 돌아와도 유지한다.

`remote_deletions`는 한 immutable 원격 tree에서 source resolver로 Requirement의 실제 삭제를 archive·rename과 구별하고 이전 원문·근거 revision을 catalog hold에 보존한다. 과거 revision의 원문도 같은 resolver로 조회해 변경되지 않은 source 문자열이 동일 identity의 rename을 가리킬 수 있게 한다. 반복 관측에도 proof와 시작한 작업의 resume_status를 유지하고, 같은 source의 여러 hold는 영향 ID를 합친다. 원문·원문 revision·삭제 관측 revision이 다른 proof는 합치지 않는다. Planner 입력의 `deleted_goals`는 보존 원문과 `source_revision`을 전달하고 전체 recovery goal을 snapshot token에 결합한다.

`REQUIREMENT_RECOVERY`는 작업 ID `id`, 삭제 경로 `source`, `outcome` (`withdrawn|retained|uncertain`), 공백이 아닌 `reason`의 배열이다. 기존 엄격한 tagged JSON parser로 중복 marker·object key와 잘못된 타입을 거부하고, 각 ID/source 쌍은 현재 snapshot의 영향 대상에 한 번만 나타나야 한다. Manager는 모든 삭제 source의 판단을 작업별로 대조하며 하나라도 전체 철회이면 나머지도 누락 없이 전체 철회여야 한다. 의미 판단은 Planner가 수행하고 일부 AC 변경은 기존 specification fingerprint·일반 개정 경계를 사용한다.

Requirement 전체 철회에 따른 Manager의 문서 삭제 준비는 현재 recovery proof·실행 snapshot·실제 PR 부재와 수동 ownership fingerprint를 기존 게시 직전 검사로 재확인한다. 이 경로에서는 draft·미해결 사용자 판단을 삭제하지 않는다. 확인된 원격 ExecPlan 삭제의 catalog 취소와 구별하며 draft·피드백 표식만으로 실행·소유 이력을 추정하지 않는다. 시작한 문서 원문은 snapshot과 일치해야 하며 모든 source의 판단·원문을 CAS 영수증으로 먼저 보존한 뒤 문서를 제거한다. 보존 후 삭제 전 중단과 삭제 후 재개도 같은 영수증·현재 권한을 검증한다. 영수증과 복수 판단은 기존 withdrawal manifest·취소 감사에 포함되고, publication transaction의 rollback·재개 계약을 따른다.

Manager는 유효한 남은 source·dependency와 현재 snapshot을 검증한 변경만 기존 planning manifest·publication transaction에 반영한다. 삭제된 선행 작업의 목표 판단과 대체 dependency는 기존 DEPENDENCY_RECOVERY 경계를 따른다.

삭제 목표 복구 개정은 snapshot의 원래 dependency graph와 후보 graph를 비교한다. 복구 대상으로 개정한 계획은 기존 직접·간접 edge로 전파된 선행 hold가 남아 있어도 목표·source·identity·전체 graph 검증 뒤 게시할 수 있다. 새 dependency나 경로 변경으로 새롭게 도달하는 hold는 거부한다. 게시 적용은 참조 복구 승인만 해소하고 선행 hold와 Worker 선택 gate는 유지한다. archive·snapshot 밖 선언·기존 Merge 감사는 편집하지 않는다.

#### draft 사용자 결정과 게시

`draft_decisions`는 질문 revision부터 현재 원격 문서까지 `git log --follow`로 경로별 계보와 모든 commit의 author를 관측한다. 마지막 사용자 작성자만으로 혼합 출처를 승인하지 않는다. 저장소 사용자 email과 일치하고 기존 Manager publication·catalog/PR 감사의 commit 또는 그 문서 내용으로 설명되지 않는 변경만 사용자 근거다. identity·계보·조회가 불확실하면 실행 대기를 유지한다.

`execplan_draft_state`는 변경 전후 원문을 DB 감사에 보존하고 Planner에는 출처·revision·문맥 diff로 전달한다. compact catalog의 질문·판단 identity에는 이 근거를 중복 삽입하지 않는다.

| 답변 근거의 변화 | 보존·소비 조건 |
|---|---|
| 관측 기준 revision 전진 | 현재 원문과 같은 미소비 답변은 원래 출처·revision·before/after를 유지한다. 부분 정리한 ExecPlan은 현재 hash·게시 해소 감사 hash·같은 사용자 근거가 일치해야 연결을 유지한다. |
| Progress·Decision Log 변경 | 원격 계보·이전 원문 조회가 확인되고 scope·source 입력·User Decision이 같으면 검증된 답변의 원래 diff를 유지한다. 새 진행 기록을 답변으로 승격하지 않으며 현재 인용·source hash는 계속 검사한다. 목표·답변·판단 입력 변경과 계보 불확실성에는 적용하지 않는다. |
| 질문 일부 해소 | 질문별로 정확히 분리되는 답변 줄만 소비하고 공유 답변·남은 User Decision·사용자 출처·유효한 판단을 보존한다. |

Planner의 `DRAFT_DECISION_RESULT`는 질문별 답변·새 사용자 원문 인용·근거 경로·남은 질문을 반환한다. 공통 strict JSON 파서가 중복 marker/key·비유한 값·잘못된 identity·필드를 거부하고 Manager가 인용을 현재 ExecPlan 원문 또는 source 입력 hash와 대조한다. Agent는 draft·입력 양식을 직접 해제하지 않는다. 같은 revision의 draft 질문에 대해 `outcome` 없이 `resolved: []`와 기존 질문 그대로의 `remaining`을 반환하면 미응답 유지로 처리한다. draft·승인 상태·대기 근거를 보존하고 독립 planning의 게시를 계속한다. 이를 새 사용자 결정이나 질문 해소로 기록하지 않으며, 변경된 질문이나 잘못된 결과는 계속 거부한다.

정리 전 `execplan_drafts`에 준비 영수증을 저장하고 cleanup hash·현재 입력·revision·원격 게시를 같은 publication transaction에서 대조해 질문별 감사와 대기를 확정한다. 부분 해소·중단 재개에도 결정 감사·source·dependency·실행 이력을 유지한다.

양식 없는 일반 계획에도 `draft_scope`를 계산하되 Progress·Decision Log·User Decision은 제외한다. `execplan_draft_state.observe`는 정리된 결정의 scope·source 입력이 바뀌면 기존 소유권·실행 이력 검사 안에서 `pending`과 `reassessment` 근거를 보존하며 같은 입력은 재처리하지 않는다. Planner는 이전 질문별 assessment와 현재 입력을 비교해 `outcome: valid`·`reason` 또는 필요한 새 draft 질문을 반환한다. Manager는 현재 revision·scope·해소 감사를 확인하고 Decision Log에 이유를 남겨 기존 정리 영수증·publication manifest·원격 확인·DB transaction으로 적용한다.

`validation_ports`의 최종 문맥은 계획별 `decisions`와 공통 `user_changes`를 사용한다. 경로와 저장된 변경 전체(before/after·revision·commit·author·검증 여부)가 같을 때만 하나의 근거 ID를 부여하고 계획별 source 경로에서 참조한다. 공통 표에는 projection의 diff만 전달하며 결과의 `resolved.source`와 DB 인용 검증은 근거 ID 대신 원래 source 경로·계획별 원문을 사용한다. 출처·원문이 다른 근거는 합치지 않는다.

질문 목록 생성·전송은 `execplan_draft_state.user_question_ids`로 질문 없는 자동 재평가를 제외하되 전체 pending gate는 유지한다. 빈 목록에서는 기존 목록의 미전송 조각만 취소한다. 전송 경계의 pending·판단 revision·질문·원격 링크가 달라지면 오래된 알림을 취소하고 전송 감사는 보존한다. legacy 개별 알림도 원래 pending/revision 검사를 유지하며 정책 전환·링크 표현 차이만으로 payload·고정 본문·진행률을 바꾸지 않는다.

`needs_semantic_review`는 검증된 사용자 변경·자동 재평가의 판단 revision·원문·결정 identity를 비교한다. 성공한 일반 planning의 `planning_input_state.record_general`과 같은 transaction에서 evidence에 `reviewed_identity`를 기록한다. 같은 입력·Progress·코드 변경만으로 다시 호출하지 않으며 새 답변·판단 근거는 다시 검토한다. 이 기록은 질문 해소·pending 해제 영수증을 대신하지 않는다.

사용자가 BLOCKED의 근거 PRODUCT/Requirement를 기본 branch에서 수정한 경우 변경을 commit하고 push해야 한다. Loop Manager는 변경된 `origin/<base-branch>`를 관리 branch에 rebase한 뒤 재개한다. 작업 rebase는 관리 worktree에서 수행하며, 기본 worktree의 파일·index·HEAD는 위 부수 동기화의 안전한 FF·clean 로컬 commit rebase 조건에서만 바꾼다. 사용자는 안내된 관리 worktree의 ExecPlan을 직접 수정할 수도 있다.

#### 원격 전진과 planning 복구

원격 관측은 Agent·verify가 worktree를 사용하는 동안 파일을 바꾸지 않는다. 일반·유지보수 Planner의 작성 결과는 writer 종료 직후, 결과 검증 전에 원격 base를 다시 확인하고 기존 workspace 갱신·문서 관련성 판단·rebase journal을 적용한다. 관련 명세·ExecPlan 변경은 결과를 확정하지 않고 pending 상태로 보존해 같은 ID·작업물·session의 Planner 재평가로 돌린다.

이전 결과의 검증 교정 횟수는 최신 입력에 합산하지 않으며, 그 결과의 진단과 독립 오류·실행 이력은 보존한다. 무관한 변경은 저장 결과와 다음 단계를 유지하며 검증·재시도를 반복하지 않는다. 충돌 교정은 시작된 rebase/stash journal의 원래 target과 소유권으로 완료하고 base·작업물·journal을 확정한 뒤 원격을 다시 확인한다.

그 사이 새 base가 전진했으면 추가 rebase나 다음 Planner 판단·결과 검증을 겹치지 않고 기존 operation 완료를 보존해 다음 안전한 workspace 경계에서 최신 target을 적용한다. 원격 관측·rebase 성공은 의미 검토나 검증 성공 근거가 아니다. 현재 작업의 문서 반영은 아래 경계를 따른다. 후보 HEAD가 바뀐 실행은 기존 검증·Review 증거의 재사용 자격을 다시 판단하며 게시 직전의 fast-forward·lease·PR·checkpoint 정책을 유지한다.

Planning rebase 교정 journal은 역할 결과 검증을 통과한 계획·`FUTURE_IDEAS.md` 경로와 Manager가 복원한 `docs/maintenance/code-map.jsonl`을 구분한다. 코드맵은 기존 SQLite receipt와 충돌 원본의 bytes/hash를 대조해 관리 worktree에 복원한 경우에만 저장·재조회할 수 있고 Agent prompt·결과 허용 경로에는 포함하지 않는다.

`cycle_revision`·`source_revision`·`artifact_hash` receipt identity는 교정 digest에 결합한다. stash 확정 journal의 `accepting` 재개도 현재 bytes와 receipt identity를 대조하며, receipt가 달라지면 base 채택·stash/journal 정리를 중단하고 기록과 작업물을 보존한다. journal은 기존 target·HEAD·digest 검증을 그대로 적용한다.

### 단계별 원격 문서 반영

Worker·Manager verify·Reviewer·게시의 진입·교정·재개는 writer 종료 후 안전한 경계에서 최신 원본과 작업 branch를 확인한다. 필요한 프로젝트 planning·게시·DB 적용을 먼저 마치고 Worker·Reviewer가 해당 단계에 반영된 최신 작업 문서를 사용한다.

planning 게시의 외부 관측·복구·worktree 정리는 공통 DB 접근 밖에서 수행하고, 준비한 결과의 현재성 검사와 원자 적용만 짧은 DB 요청으로 실행한다. [계획 게시와 작업 공간](#계획-게시와-작업-공간)의 적용·정리·해제 경계를 모두 마친 뒤 원래 실행 단계를 재개한다.

| 변경·확인 결과 | 처리 주체와 다음 단계 |
|---|---|
| PRODUCT·활성 Requirement·ARCHITECTURE 변경 | 프로젝트 일반 planning에서 관련 작업 개정·게시·DB 적용을 완료한 뒤 같은 작업을 재개한다. 프로젝트 실행 투영과 작업 이력의 보존은 아래 책임 표를 따른다. |
| ExecPlan의 구현 조건·sources·dependency 변경 | Manager가 선언 검증과 작업 branch 반영을 담당한다. 구현 충족을 확정할 근거가 없으면 기존 Worker 세션에서 판단·필요한 보완·targeted test를 함께 수행하며 작업별 영향 Planner를 호출하지 않는다. |
| 무관한 문서·순수 revision·진행 기록만 변경 | Worker를 반복하지 않는다. 소유 진행 기록은 검증된 선언 delta만 반영하고 미확인 조건·다른 입력과 전체 planning 성공 기준을 보존한다. |
| verify 진입에서 구현 부족 또는 충족 근거 불명확 | 기존 Worker 교정으로 연결한다. 구현 충족의 유효한 근거가 있으면 필요한 verify를 수행한다. |
| Reviewer 진입에서 문서 변경 | 현재 구현·과거 검증 범위·최신 조건 미검증을 구분하는 선행 Review를 수행한다. 구현·테스트 수정이 필요한 FAIL은 Worker 교정, 검증 실행 근거만 부족한 `VERIFY_REQUIRED`는 Manager verify로 연결한다. |
| 후보 HEAD 변경 | 문서 재확인 여부와 별도로 최종 후보에서 필요한 targeted test·전체 verify·읽기 전용 Review의 유효성을 확인한다. 기존 성공은 입력 동일성·재사용 규칙을 다시 증명한 범위에서만 사용한다. |

선행 Review의 PASS만으로 최신 검증이나 게시 token을 만들지 않는다. verify·Review·게시 gate는 각각 유지하며 코드 전용 점검·일반 Planner 인계를 새로 만들지 않는다. 유지보수의 대상·안전 제한도 유지한다.

문서 반영의 이전 선언·발견 단계·과거 Worker/verify 근거·영향 판단·개정 게시 후보는 해당 ExecPlan의 execution checkpoint에 보존한다. 원격 작업 branch의 문서는 단순 관측 OID만으로 통합 완료나 검증 성공을 만들지 않는다. 현재 작업 대상·입력 유효성·작업물 보존을 확인해 실제 수용한 뒤 해당 문서를 입력으로 반영한다. 이 상태는 rebase·교정의 checkpoint 교체에서도 보존하며 다른 작업이나 일반 planning 상태를 덮어쓰지 않는다.

이전 버전의 단일 현재 계획 개정 후보는 기존 소유 detached worktree와 execution checkpoint에서 복구한다. Manager가 구조·source·dependency·기존 ID·경로·실제 PR 부재와 게시 사실을 재검증하고 미완료 적용부터 이어간다. proposed 후보로 detached worktree의 문서를 쓰기 직전에도 게시에 쓰는 mutable snapshot 검사를 재사용해 현재 실행 row·수동 소유권·삭제 목표 감사와 metadata를 대조한다. worktree 관측 중 취소·최종 삭제나 입력 변경이 확정됐으면 파일 쓰기·결과 검증·commit 전에 거부하며 남은 사본의 최종 정리와 구분한다. 실행 중 구현 worktree는 수정하지 않으며 PR 출현·후속 원격 변경·소유권 불확실에서는 작업물을 보존한다. 이전 문서 영향 Planner checkpoint는 이 복구·감사에 사용하며 새로운 작업별 Planner 호출을 만들지 않는다.

진행 중인 일반·유지보수 planning은 원격 base 전진 시 staged·unstaged·untracked 변경과 세션을 관리 worktree에 보존해 반영하고 최신 catalog·SQLite 권한에서 재검증한다. 시작 revision의 READY 원본과 Planner가 작성한 결과를 구분하며, 관련 근거·의존 문서의 실제 내용이 바뀐 경우에만 해당 계획을 다시 확인한다. 무관한 문서 변경과 내용 보존 이동은 재확인을 만들지 않는다. 관련성 비교는 성공한 revision tree 조회로 정확한 경로의 blob 또는 부재를 확인한다. 조회 실패·잘못된 객체는 부재나 동일 내용으로 축약하지 않고 기존 원격 반영 보류 경계로 전달해 작업물·세션·실패 근거와 미확정 결과를 보존한다.

`DOCUMENT_RESULT`는 기존 legacy checkpoint 복구에서 `application.result_normalization.tagged_json`으로 marker·공백·구분자·다중 줄 JSON을 정규화한다. 중복 marker·JSON key와 필드·evidence 의미 오류는 거부한다. 저장된 원문과 정규화된 판단을 후보 revision·문서 경로·Worker snapshot에 결합하고, 호출 입력이 바뀌면 이전 원문·판단을 superseded 근거로 보존해 새 판단에서 제외한다. 기존 checkpoint는 원문 필드가 없어도 원래 stage·후보·게시 사실을 확인해 복구하며 새 결과에 identity metadata를 일괄 요구하지 않는다.

일반 planning snapshot은 시작 시점에 실제 연결 PR이 없는 후보만 담는다. snapshot 선정 뒤 PR이 생기면 intent 기록·게시 직전 같은 CAS 경계에서 다시 확인해 게시를 미루고 미게시 intent만 supersede하며 Planner 결과·작업 공간·세션·실행 이력을 보존한다. PR이 있는 계획은 새 snapshot에서 제외하고 나머지 계획을 현재 catalog에서 판단한다. PR 조회 실패는 부재가 아니다. 단계별 legacy `proposed` 개정은 `prepare_snapshot()` 결과에서 해당 계획이 PR 때문에 제외되면 연결 PR을 다시 조회하고, 발견 시 checkpoint 후보를 보존해 기존 PR 경로로 넘긴다. `prepared` 개정은 push 직전에 PR을 확인하고 나타나면 후보를 보존한다. PR 조회 실패는 부재로 취급하지 않는다. 확인된 PR 출현은 `PLAN_INPUT_INVALID` 최종 차단 사유가 아니며 해당 계획에만 적용한다.

### 프로젝트 planning과 작업 문서 반영 기준

[R-082](requirements/%5BR-082%5D%20단계별%20원격%20문서%20반영과%20PR%20전%20ExecPlan%20갱신.md#프로젝트-planning과-현재-작업-재개-순서)의 명세 영향 판단은 프로젝트 일반 planning이 담당한다. 일반 Planner는 변경된 명세·관련 ExecPlan·dependency·현재 원격 구현과 필요한 보존 후보를 읽기 전용으로 확인한다. 관련 PR 전 계획 snapshot 검증·게시 intent·catalog 원자 적용을 재사용하며 구현 worktree를 Planner의 쓰기 공간으로 사용하지 않는다.

| 책임 | 저장·실행 경계 |
|---|---|
| 프로젝트 변경 감지·planning 선택 | 병렬 프로젝트 조율기가 fetch로 갱신된 원격 ref를 읽고 새 revision에서 기존 성공 기준·명세 지문·archive 예외를 재평가한다. 자식의 단계·호출 자리 진입 요청도 이 조율기의 확인을 기다리며 별도 작업별 명세 지문 판단은 하지 않는다. 미반영 명세는 해당 프로젝트를 drain하고 기존 호출이 반환한 뒤 일반 planning으로 돌아간다. 명세가 같으면 최신 catalog를 반영하고 계속한다. 관측 실패는 배정을 중단하고 원래 오류를 전달한다. 안전한 경계에서 `projects.planning_resume_plan_id`로 재개할 작업을 고정하고 프로젝트 실행 투영만 비운다. `exec_plans`의 단계·소유권·작업물·세션·실패 이력은 유지한다. 일반 planning이 실행 자리를 사용하며 게시·DB 적용 뒤 같은 작업을 원자적으로 다시 투영한다. 실행 중 Agent·verify와 소유 Git operation은 기존 경계를 유지한다. |
| 개정 게시·DB 적용 | 일반 `planning/publication_service.py`와 `storage/planning_input_state.py`의 성공 확정을 재사용한다. 게시 뒤 DB 미적용은 저장 intent로 먼저 복구하고, 새 원격 입력은 별도 검토한다. 관련 조건의 개정·변경 없음 결과가 확인된 최신 성공 기준에서만 현재 작업을 재개한다. |
| 작업의 실행 대응 | `execution/stage_documents.py`는 본문 구현 조건·sources·dependency 변경의 부족 여부를 확인한다. 구현 입력 token에서 원본 명세 지문만의 변화를 제외하되 verify·Review token은 그 지문을 독립적으로 유지한다. 정확한 이전 token과 snapshot이 확인된 legacy Worker 근거만 새 비교 형식으로 옮기며 과거 성공을 합성하지 않는다. |
| 마지막 반영 기준·진행 기록 | 변경 전 본문을 확보한 소유 commit은 원문·승인 tree와 repo·plan·branch·worktree·parent·message intent를 `document_record` checkpoint에 결합한다. 변경 전 본문을 확보하지 못하면 새 기록 성공을 만들지 않고 기존 기준을 유지한다. 복구는 실제 parent·message·tree가 intent와 일치하는 commit을 식별하고, 현재 후보의 조상 관계·원문·소유 identity·순수 기록을 확인한 뒤 definition과 적용 marker를 generation CAS transaction으로 저장한다. 중단 복구와 후속 checkpoint 교체는 미적용 근거와 확인된 commit을 보존한다. 증명에 실패하면 이전 기준과 미해결 사유를 남긴다. 미적용 기록은 후속 소유 교정 commit을 차단하지 않으며 새 intent나 checkpoint 교체도 그 근거를 덮지 않는다. 원격에서 같은 기록 bytes를 확인한 뒤에만 `planning_input_state.advance_verified_delta`로 해당 선언 delta를 소비한다. 같은 파일의 미확인 조건과 다른 입력은 남기며 전체 planning 성공·verify·Review·실패 이력을 갱신하지 않는다. |

위 코드 경로는 모두 `loop_manager_internal/` 아래다. 작업의 마지막 반영 ExecPlan 기준, 프로젝트 planning 성공 기준과 Git·verify·Review·게시의 입력 증명을 분리한다. `DefinitionSnapshot`·원격 SHA·execution checkpoint는 후자의 복구 근거까지 확인해 유지하며 일괄 제거하지 않는다.

전체 성공 기준은 검증된 `planning_input_baselines.reference_revision`과 프로젝트 지문을 사용하고 자체 delta 반영 revision을 새 전체 성공으로 사용하지 않는다. 이전 작업별 판단·개정 checkpoint도 미게시 후보·게시 사실·미완료 적용을 보존해 복구한다. planning 오류에서는 동일 실패 입력을 반복하지 않고 미확인 영향 밖의 작업만 기존 순서로 진행하며, PR·수동 소유 예외는 R-061·R-077을 유지한다.

## 실행 자리와 유지보수

프로젝트 조율은 준비·planning을 수행하고, 관리 대상으로 선택한 각 작업은 별도 SQLite 연결과 `ContextVar` 실행 선택으로 단계 처리를 진행한다. `exec_plans`가 작업별 단계·session·scope·실패 이력의 원본이며, 병렬 작업은 공유 `projects` 투영을 덮어쓰지 않는다. 프로젝트 조율이 종료할 때 남은 활성 작업을 대표 투영으로 연결하고, 미확정 소유 상태가 있으면 투영을 임의로 비우지 않는다.

관리 순서와 명시 요청의 인계 상태는 `(repo, plan_id)`가 유일한 `task_management_admissions`에 보존한다. 수동 최초 수락 시각·명시 해제의 최초 양수 `priority`·일반 RETRY·일반 재개·신규 분류는 `Task.rank()`가 계산하고 신규 선택에 현재 유효 priority를 사용한다. 호출 큐의 일반 재개는 적격 대기 등록 순서로 처리한다. nullable 정수 `retry_order`는 미소비 RETRY의 재등록 순번, nullable 0/1 `resumed`는 저장된 재개/신규 구분이다. 재등록은 짧은 쓰기 transaction에서 저장된 최대 RETRY 순번 다음으로 확정하고 Task 생성은 이를 복원한다. Agent의 실제 프로세스 시작 관측 또는 verify 프로세스 receipt를 근거로 호출 예약의 0/1 `started`와 재개 자격·cursor·RETRY 소비를 함께 확정한다. 예약만으로는 소비하지 않으며, receipt 재적용은 같은 예약에서 한 번만 반영해 이후 RETRY를 지우지 않는다. 시작 관측 저장 실패는 보존된 receipt로 복구한다. legacy 행에는 증명할 수 없는 RETRY 순번·재개 구분을 합성하지 않고 NULL로 남겨 기존 단계 판단을 사용한다.

0/1 `pending`은 아직 관리 인계에 반영되지 않은 명시 요청, `parked`는 후속 배정 보류, `handoff`는 선택됐지만 밀려난 단위의 반환을 기다리는 자리를 구분한다. 기존 수동 묶음을 한 단위로 비교하고, 현재 적격인 새 요청이 순위 안에 들어올 때만 같은 프로젝트의 마지막 단위를 대기시킨다. 새 요청이 없거나 현재 순위상 편입할 수 없으면 기존 관리 단위를 유지하고 빈자리만 순서대로 채운다. 따라서 과거 수락 행은 한도 축소에 따른 교체 근거가 아니다. 수락·선택·반영은 공통 DB 요청의 짧은 transaction으로 직렬화하며 반복 요청은 최초 순서를 바꾸지 않는다. legacy의 parked 양수 priority 행만 미처리 명시 요청으로 연결한다. 인계 선택은 `handoff`로 재시작에도 복원하고, 자격을 잃은 인계 대상은 요청을 보존한 채 자리를 다시 연다. 실행 중 호출 예약이나 단계 처리가 남으면 교체 대상은 대기하되 실제 예약은 반환 확인까지 유지한다. 관리 대기는 시작·성공·실패·진전의 근거가 아니다.

배정기는 수락된 수동 묶음 또는 저장된 명시 요청을 확인해 미확정 게시가 없는 유지보수 범위 밖 재진입을 같은 `cycle_allows_recovery` 경계로 허용한다. 기존 고정 범위는 확대하지 않으며 미확정 게시·planning·dependency·프로젝트 안전 차단은 계속 검사한다. 수동 재개는 현재 실행 원본이 활성 단계이면 덮어쓰지 않고 대기에서 실행으로 바꿀 때도 기존 상태를 조건으로 갱신한다. 순서·인계 저장은 `exec_plans`의 단계·session·성공 token이나 `execution_checkpoints`의 generation·rebase intent를 변경하지 않는다. 호출과 단계 진입에서도 대기를 재확인한다. 명시적 차단 해제는 같은 transaction에서 차단 해제·감사·새 관리 재진입을 확정하고, 과거 차단 사유·오류·해제 시각은 해제 감사로 보존하며 다른 계획의 대표 투영을 덮어쓰지 않는다.

운영 DB 연결은 공통 `db_access` 경계를 거쳐 파일별 스레드 순번과 프로세스 잠금을 사용한다. 조회 결과는 처리 구간 안에서 물질화해 cursor가 SQLite 읽기 잠금을 유지하지 않으며, 관련 조회·갱신은 같은 연결의 짧은 transaction으로 묶고 commit·rollback·close에서 접근 소유권을 반환한다. 별도 연결을 중첩 대기하는 호출은 거부하고 외부 관측은 DB 처리 밖에서 수행한 뒤 현재 상태·checkpoint·소유 예약을 재확인한다. 일시 DB 경합은 요청의 공통 대기 예산 안에서 처리하고 실제 실행·교정 실패 횟수와 구분한다. 읽기 전용 CLI·bootstrap과 호출 기록도 같은 진입점을 사용하며 독립 정적 snapshot은 별도로 유지한다. 대기 예산은 공통 계층에서 관리하며 SQLite 자체 대기로 중복 소비하지 않는다. 초기 등록·legacy catalog 이관은 독립 메모리 snapshot에서 기존 로직과 필요한 Git·PR 관측을 먼저 수행하고, 관측값과 원본 상태가 같은 DB 처리 안에서 기존 적용 로직을 실행한다. snapshot을 운영 DB에 덮어쓰지 않으며 관측 이후의 계획·소유·감사 변경은 거부한다.

공통 DB 연결의 중첩 context와 runtime transaction은 savepoint를 공유하며 안쪽 helper가 바깥 변경을 commit하지 않는다. schema script도 호출자의 transaction을 암묵 commit하지 않는다. `CoordinatedConnection.run_request(operation)`은 재실행 가능한 짧은 DB 전용 함수를 명시하는 경계다. 함수는 전달받은 연결에서 매 시도 현재 값·소유권·generation과 CAS 조건을 다시 읽고 판단하며 transaction 제어는 요청 경계에 맡긴다. 중첩 요청도 같은 transaction과 savepoint에 참여한다. BUSY/LOCKED이면 변경 전체를 rollback하고 차례를 반납한 뒤 같은 deadline으로 다시 참여한다. 안쪽 helper가 잠금 예외를 잡아도 해당 시도의 부분 변경을 확정하지 않는다. 임의 `with connection` 본문이나 SQL 한 문장을 대신 replay하지 않고, 이미 열린 caller transaction을 재시도 요청이 인수하지 않는다. Agent·Git·verify·전송은 함수 밖에서 수행하며 결과만 새 DB 요청에 전달한다. 요청 한도 소진은 `DatabaseWaitTimeout`으로 전달하고 손상·권한·SQL 오류와 취소는 원래 오류를 유지한다. 이 진입점을 명시적으로 채택한 요청에만 전체 재시도를 적용한다.

스레드는 파일별 condition 대기열을, 프로세스는 기존 `.access.lock` 안의 대기 표식과 OS byte lease를 사용한다. 먼저 등록된 살아 있는 프로세스 요청이 접근 잠금을 얻고, 반환·취소·프로세스 종료로 lease가 풀린 표식은 다음 요청이 회수한다. 대기 요청이 없으면 잠금 파일의 이전 표식을 정리하며 별도 상태 DB를 만들지 않는다. 재시도도 기존 대기 요청 뒤에 등록한다. 스레드·프로세스 순번 대기와 SQLite 재시도·commit·backup은 공통 deadline과 runtime 중단 확인을 사용한다. 명시적 재시도 요청의 시작과 실제 대기·재시도에서 중단을 확인하되, 일반 DB 경계의 즉시 가능한 종료 확인·예약 반환은 runtime handoff 뒤에도 허용한다. backup의 SQLite 내부 sleep은 끄고 BUSY/LOCKED가 확인된 명시적 sleep만 실행 clock에 기록한다. 즉시 획득·실제 복사·DB 처리 시간은 제외하지 않으며 살아 있거나 종료 불명인 프로세스의 시간·종료 확인도 유지한다.

공유 `TaskScheduler`가 관리 배정과 실제 호출 배정을 구분한다. 관리 선택은 프로젝트별 유효 상한 안에서 수동 제출·기존 작업·일반 신규 순서로 확정하며, 일반 신규는 기본 자리 없는 프로젝트부터 순환하고 이후 유효 priority를 사용한다. Worker·Reviewer 호출은 전체 `max_workers`, verify는 별도 `verify_concurrency`를 사용하고 프로젝트 조율·Planner mutex 대기는 이 호출 자리를 사용하지 않는다. 한 자동 실행의 Planner는 직렬이다.

실제 호출 자리가 꽉 찬 동안에는 대기 작업의 유효 우선순위를 반복 계산하지 않는다. 대기 중 프로젝트 중단·종료 불명 예약 확인은 유지하고, 빈자리가 생기면 현재 우선순위를 다시 읽어 기존 배정 순서로 판단한다. 우선순위 계산과 DB 조회는 scheduler condition 잠금 밖에서 수행하고 배정 직전에 자리·중단 조건을 다시 확인한다.

배정기의 공통 condition 안에서는 대기열·메모리 실행 자리만 갱신한다. 순번 대기 후 빈자리와 자신의 순번을 관측하면 condition 밖에서 Git 입력을 갱신한다. 이때 해당 항목은 호출 대기열에서 잠시 빠져 Git lease 대기가 독립 호출의 순번을 막지 않으며 실제 호출 자리·DB 예약도 만들지 않는다. 갱신 뒤 다시 편입해 현재 자격·우선순위·자리·중단 조건을 재검사한다. 호출 자리를 확보하고 공통 condition을 놓은 뒤 DB에서 호출 예약을 확정한다. 예약 뒤 실행 입력·현재 자격과 필수 verify preflight·runtime 검사를 마친 다음 배정 cursor와 시작 표식을 확정한다. 이 callback에서 보류되면 cursor와 RETRY 순서를 소비하지 않고 미시작 자리를 반환한다. 종료 시 DB 예약 반환이 확인된 뒤 메모리 자리를 비우고 확인 실패는 종료 불명으로 보존한다. 실제 호출 직전 작업 snapshot·프로젝트 차단·catalog/draft 보류를 재검사한다. 입력 변경은 실패 횟수를 늘리지 않고 단계 재준비로 돌리며, 지속되는 차단·보류는 해당 작업의 대기로 반환한다.

수동 start·complete·submit은 Git·profile 관측 전에 group, 요청, 구성원별 `exec_plans` 전체 행과 수동 소유 fingerprint를 짧은 공통 DB 요청에서 snapshot으로 확보한다. 외부 관측 뒤 적용 요청은 그 snapshot과 후보 입력을 다시 비교하고, `preparation_gate`의 현재 자원·게시 예약을 함께 검사해 소유권·계획 리비전·상태가 그대로일 때만 예약·수락을 원자 적용한다. 차이가 있으면 충돌로 거부해 경쟁 상태와 기존 작업물을 보존한다. `run_database_request`는 이 DB 전용 재확인·적용만 rollback 후 다시 읽어 재시도하며 Git·profile·Agent·검증·게시를 감싸거나 반복하지 않는다.

자동 실행의 `git:*` 자원 충돌은 기존 프로세스 lease를 매번 다시 획득하며 취소·Planner/runtime 중단을 확인한다. 대기는 DB transaction과 실제 Agent/verify 호출 자리를 점유하지 않으며 복구 실패로 기록하지 않는다. 수동 요청과 계획 소유권 충돌은 기존 거부 경계를 유지한다. 일반 차단은 선택된 ExecPlan 전체 row를 관측 기준으로 삼고 지문·Git·Reviewer 자료를 읽은 뒤 짧은 transaction에서 다시 비교한다. 그 사이 row가 바뀌면 오래된 상태를 차단에 적용하지 않고 재검사를 요구한다. BLOCKED Agent 변경 보존·복구 완료는 명시 plan ID의 실행 선택 context에서 Agent snapshot과 recovery 상태를 읽고 갱신한다. 중단 복구 재진입은 `projects`에 남은 복구의 `owner_plan_id`로 대상만 선택한 뒤 그 plan의 최신 recovery와 snapshot을 사용하므로, 낡은 대표 투영을 완료 상태의 원본으로 다시 소비하지 않는다. 소유자 필드가 없는 기존 복구 기록은 `projects` 호환 projection을 상태 원본으로 읽고 완료·재진입에서도 같은 projection을 기준으로 갱신하며, `plan_path`가 ExecPlan을 가리키면 기존 mirror 규칙으로 대상 행도 동기화한다. 대표 `projects` 투영이 다른 작업을 가리켜도 다른 branch의 snapshot과 비교하거나 그 상태를 덮지 않는다. direct rebase 시작·완료는 명시된 plan ID와 실행 선택 context가 함께 있으면 둘의 일치를 효과 전에 확인한다. 완료 처리는 명시된 작업을 선택해 해당 작업의 phase·checkpoint·candidate를 소비하며 다른 plan의 대표 `projects` 투영과 planning 기록은 보존한다. PR head는 선택된 ExecPlan의 실행 상태에서 읽고 프로젝트 등록 origin·base와 안전 gate는 프로젝트 원본을 따른다.

정상 provider 반환 뒤 session 저장의 DB 오류는 실제 결과·사용량을 예외 결과로 덮지 않고 수집 오류로 기록한다. session 결합 행이 없으면 같은 session의 완료된 invocation과 실제 종료 근거에서 유일한 지침 작업 귀속을 읽을 수 있다. 귀속 충돌과 필수 실행 전 증거 저장 실패는 계속 거부한다.

작업별 일반 RETRY는 프로젝트 조율로 반환하지 않고 실행 대기열의 재시도 순서로 재등록한다. RETRY마다 DB 상태를 다시 준비하고 기존 일시 복구의 같은 실행 3회 한도를 공통으로 적용한다. 단계 처리의 일반 예외는 작업별 상세 오류·traceback을 기록하고 미확정 transaction을 rollback한 뒤 같은 배정 경로로 제한 재시도한다. 예외 3회 실패는 해당 작업만 보류하며 Future의 일반 예외도 작업별 오류로 수집해 조율기의 추가 배정을 계속한다. Planner 배정 중단·runtime handoff·사용자 중단은 별도 제어 신호로 전파한다. 재시도 우선순위는 수동 제출보다 낮고 다른 일반 작업보다 높으며 재등록 순서를 유지한다. 프로젝트 조율기가 명세 변경으로 planning이 필요하다고 판단하면 프로젝트 배정 중단 신호로 관리 편입·실행 대기 호출·후속 단계를 반환시키고 실행 중 처리를 join한 뒤 프로젝트 조율로 돌아간다. 진행 중 rebase의 작업물·index·checkpoint·원래 target은 보존하고 별도 planning worktree에서 Planner 게시·catalog·DB 반영을 먼저 완료한 뒤 작업 배정을 다시 연다. 재개한 실행은 갱신된 베이스와 명세를 기존 revision gate로 확인하며, 보존된 rebase를 완료로 간주하거나 강제로 중단하지 않는다. 다른 진행 중 Git operation과 미종료 호출의 안전 gate는 유지한다. runtime handoff는 기존 전체 launch gate로 새 호출을 막고 진행 중 처리의 반환을 기다린다.

같은 원인·입력 지문·진전 marker의 관측이 세 번 이상인 일시 복구는 재시도 묶음의 상한 뒤 타이머만으로 다시 배정하지 않는다. 기존 `recovery_records.occurrence`와 `pending_action=pause`로 해당 작업을 보존하고 독립 작업을 계속한다. 입력이나 진전이 달라진 관측은 별도의 묶음으로 판단한다. 이 보류는 최종 차단·사용자 결정·성공을 만들지 않으며 다음 외부 실행의 현재 상태 재검사를 막지 않는다. 단계 처리의 일반 예외가 세 번 반복돼도 작업 thread를 무기한 입력 대기에 남기지 않는다. 해당 작업을 일시 보류로 반환하고 기존 입력 지문에 결합해 재배정을 유예하며, 독립 작업 종료 후 이번 실행을 마쳐 다음 실행의 PR 관측·충돌 복구를 허용한다. 준비·단계 처리·결과 수집의 DB 대기 만료는 실패/교정 횟수에 합산하지 않고 같은 작업을 최대 세 번 재시도한 뒤 보존한다. 실제 결과가 이미 확보된 Agent 호출은 기존 결과 저장 재개 경계를 사용해 provider를 반복하지 않는다.

배정 순위는 대기 스레드가 소유한 SQLite 연결에서 현재 수동 수락·RETRY·유효 priority로 다시 계산하며, 실행 불가 항목은 선택에서 제외한다. READY 활성화는 관리 순번을 받은 뒤 기존 selector로 재선택하고 현재 worktree HEAD를 읽는다. 수동 재개도 같은 경계에서 묶음·활성 구성원·요청이 보존됐는지 확인한다. DB 쓰기 transaction을 잡고 순번이나 외부 관측을 기다리지 않는다.

호출 대기의 작업 행·유지보수·수동 묶음과 실행 자격을 예약 전후에 비교한다. execution 포트는 준비한 호출 입력의 재검사 함수를 `ContextVar`로 전달한다. 실제 대기 뒤에는 기존 latest-base fetch를 재사용하고, 호출 직전 현재 실행 snapshot·후보 HEAD·branch·index·dirty 파일·config·Git operation·대상 worktree 등록·원격 base를 대조한다. 무관한 branch ref·worktree 등록은 후보 변경으로 취급하지 않는다. 변경은 `SchedulingChanged`로 기존 재준비에, 현재 자격 부재는 `SchedulingDeferred`로 작업별 일시 보류에 연결하므로 검증 실패·예외 재시도 횟수를 소진하지 않는다. 재검사에 거부된 미시작 예약만 해제하고 호출 cursor·RETRY 순서는 소비하지 않으며, 종료 불명 예약은 기존 receipt 계약대로 보존한다.

`projects.max_tasks_per_project`는 정수 0 이상이고 migration 기본 0이다. 0이면 해당 Manager가 읽은 config 기본값을 상속하며 양수는 보존한다. 배정 경계에서 DB를 재조회한다. `task_call_reservations`는 호출 token·repo·plan·종류·실행 receipt를 보존하고, 재시작 시 미시작 또는 실제 프로세스 종료 증거가 있는 호출만 회수한다. `PLAN_BLOCKED`는 호출 예약이나 `execution_checkpoints.operation`이 남아 있어도 관리 개수에서 제외한다. 실제 호출의 실행 자리·자원 소유권·checkpoint는 삭제하지 않으며 차단 해제 후에는 관리 한도를 다시 확인한다. 차단되지 않은 작업은 미확정 호출·operation의 기존 관리 자리 보존 조건을 유지한다. Agent는 기존 group/Job 종료 receipt, verify는 소유 프로세스·WSL lifecycle receipt를 사용한다. 불확실한 종류의 호출이 상한에 도달하면 해당 단계는 보존하고 다른 종류의 적격 호출을 계속한다.

rebase 관측의 안정성 지문에는 해당 branch·worktree의 등록과 공유 config만 포함하고 무관한 작업의 ref·worktree 변화는 제외한다. 해당 branch의 중복 등록·현재 후보·index·입력·원격 PR의 검증은 유지한다. rebase 완료·차단 해제의 Git·원격 재관측은 쓰기 transaction 전에 마치고, 짧은 DB transaction 안에서 대상 실행 snapshot·현재 투영·checkpoint generation을 다시 비교해 acceptance와 상태·outbox를 함께 적용한다. 대표 투영이 비어 있거나 다른 plan을 가리켜도 명시 대상의 원본과 일치하면 해당 plan만 갱신하고, 대상 자체의 identity·generation·원격 OID 불일치는 계속 거부한다. 게시된 rebase의 원격 lease는 현재 대상·branch·checkpoint 관측이 `WAITING` 또는 `COMPLETE`로 확인될 때만 유지하며, base가 조상에 포함되지 않는 `START` 판단은 완료 증거나 게시 소유권으로 승격하지 않는다. Worker 결과 scope 검사도 대상 checkpoint의 현재 rebase를 다시 관측하며, 관측 실패는 원래 영역·진단을 보존해 거부하고 최종 파일 판정에 전달하지 않는다. 중간 Git 작업이 확인된 `WAITING`만 최종 파일 검사를 미루며, 최종 결과는 `COMPLETE` 또는 현재 파일 정책에 따른 구체적 scope 거부로 판정한다. 반복 충돌 차단은 아직 rebase가 시작되지 않은 `START` 상태만으로 해제하지 않지만, 기존 plan-input correction의 재개 의미는 유지한다. 상위 execution 포트도 외부 재관측 전에 writer를 예약하지 않는다.

프로젝트 차단 복구와 계획 차단 해제는 공통 자원·writer·planning·관리 한도 검사를 사용하되 대상 상태를 구분한다. 계획 차단 해제는 `PLAN_BLOCKED`만 대상으로 삼는다. 프로젝트 차단의 정확한 소유자는 `IMPLEMENTING`·`VERIFYING`·`REVIEWING`·`PUBLISHING`이어도 기존 관리 자리에서 복구와 중간 게시를 이어갈 수 있다. 다른 계획의 투영·원인·자원이나 미종료 호출은 이 예외로 허용하지 않으며, 대상 행 부재는 원래 차단을 보존하고 복구 성공이나 실행 자리 대기로 오인하지 않는다. 중간 게시의 `DirectRebasePublicationStatePort`도 같은 프로젝트 소유자 조건으로 투영 없는 활성 작업을 선택하며, 게시 검사 중에도 그 자격을 재확인한다. 프로젝트 차단 해제·소유자 변경 또는 다른 실행 투영은 기존 선택을 무효화하고, 자리를 얻기 위해 프로젝트 투영을 덮어쓰지 않는다.

차단 재평가는 대상 plan·branch·worktree의 자원 lease와 `selected_execution`으로 기존 복구 경로를 실행한다. 복구 컨텍스트에서만 PLAN_BLOCKED의 투영을 읽을 수 있고 일반 단계 실행의 활성 상태 조건은 유지한다. 배정기는 대상 소유권을 확인한 복구 컨텍스트와 차단 해제 admission을 명시적으로 구분한다. 해당 계획의 차단 상태와 일반 실행 순위 부재가 복구용 Reviewer·verify·해제 transaction을 거부하지 않게 하되 프로젝트 차단의 정확한 소유자 여부·planning 소유·dependency·catalog/draft·유지보수·수동 소유와 실제 호출 예약 검사는 유지한다. 복구 컨텍스트 종료 시 이전 작업 선택을 복원해 일반 실행에 이 예외가 남지 않게 한다. 실행 스레드도 계획 lease를 보유해 종료 정리와 재평가가 겹치지 않으며, 대상 호출 예약·미종료 invocation·같은 소유 자원의 다른 활성 계획을 확인한다. 복구 뒤 다른 계획의 대표 투영을 덮지 않고, 원래 투영이 비어 있거나 대상인 경우에만 기존 단일 작업 호출의 재개 투영을 연결한다. 병렬 조율은 관리 여유에서 변경된 차단 입력을 별도 Future·DB 연결의 재평가로 보낸다. 재평가가 의미 판단을 기다리는 동안에도 다른 신규 배정은 계속하며, 해소 뒤의 짧은 재개 확정은 기존 관리 배정 순서를 사용한다. 코드 수정 뒤의 첫 실행도 원인별 검사를 수행한다.

일반 planning의 관련 입력은 기존 `planning_reviews`의 검토 원문과 revision에 보존하고, 원격·catalog·DB 반영을 확정하는 transaction에서 대상 checkpoint의 `planning_reassessment`로 연결한다. 일반 batch는 개정 대상 ID를, Planner는 직접 sources 밖 활성 Requirement의 영향 관계를 제공한다. 일반 `BLOCKED_PLAN_INPUTS` 결과는 `revision`과 `plans` 배열이며 각 대상은 `plan_id`, 비어 있지 않은 `requirements` 배열을 가진다. Requirement 항목의 필수 문자열은 활성 문서의 정확한 `source`, 목표·검증·dependency에 미치는 `reason`, 검토 revision 원문의 정확한 `quote`다. 중복 marker·key·대상·source, revision 불일치, 현재 차단 작업이 아닌 대상, 잘못된 문서 ID·경로·인용은 검증에서 거부한다. 이전 결과에 이 선언이 없으면 간접 영향 입력을 추정하지 않는다. 이 선언은 질문 해소나 사용자 답변 protocol이 아니다.

`planning_reassessment`는 검토·확정 revision과 관련 Requirement별 영향 근거·출처 revision을 유지한다. 후속 관련 입력은 기존 관계와 함께 보존하고 관련 집합이 바뀌면 새 재평가 입력으로 취급한다. 게시 intent 또는 미확정 검토가 남아 있으면 원인 판단을 보류한다. 확정 transaction의 실패는 예약과 catalog·검토 확정을 함께 rollback하며, 복구는 기존 intent와 검토 원문으로 이어가고 새 Planner 판단을 합성하지 않는다. 작업별 배정 키와 차단 reconciliation 문맥에 확정 입력을 포함해 직접 source 지문이나 이미 처리한 원격 관측이 같아도 새 입력이 소실되지 않게 한다. 원격 관측 identity는 별도로 보존하며, 저장된 평가 단계 뒤 중단은 같은 입력의 원인 판단을 반복하지 않는다. 중단 중 새 planning 입력이 확정되면 기존 대상·원인·저장 문맥을 다시 확인해 재평가 입력과 다음 목표 base를 갱신하고 낡은 평가만 폐기한다. ExecPlan 개정이 동반되면 DB 쓰기 잠금 밖에서 catalog revision이 확정 revision의 이력에 있는지 확인하고, 확정 원문의 정확한 bytes 해시·ID·sources·dependency를 현재 catalog와 대조한다. 일치한 문서 필드(`content_hash`·`sources`·`depends_on`)만 복구 identity에 수용하며 path·branch·worktree·소유권·PR·차단 원인 검사는 유지한다. 짧은 transaction에서 관측한 계획 행·checkpoint generation·저장 문맥·확정 입력이 그대로인지 재검증한 뒤 적용하고, 조회 실패·불일치·경쟁에는 기존 복구 상태를 보존한다. 진행 중인 Git operation과 checkpoint의 고정 target은 유지하며 그 target의 완료·게시 뒤 새 target을 반영한다. 대상 lease·호출 종료·공유 자원·planning 소유권과 기존 dependency를 확인하고 다른 작업의 투영·후보·예약을 유지한다.

사용자 질문은 입력 지문의 차이로 해제하지 않는다. 현재·이전의 명세와 작업 본문을 읽고 링크·frontmatter만의 변경을 제외한 뒤 읽기 전용 Reviewer로 원인을 판단한다. 재평가 문맥의 `comparison_revision`은 rebase 전 입력 revision이며 같은 차단 원인의 재시작·후속 재평가에도 보존한다. 실행용 `active_sha`가 최신 target으로 갱신돼도 비교 기준은 바꾸지 않는다. 이 필드가 없는 과거 진행 문맥은 등록 revision을, 등록 revision도 없으면 현재 실행 revision을 사용하며 원문 조회 실패에는 기존 보류를 유지한다. ARCHITECTURE·ExecPlan·선언된 sources와 확정된 관련 Requirement의 현재 원문은 필수 입력이며, PRODUCT가 선언되지 않은 경우에만 선택 입력으로 취급한다. 정확한 tree 조회와 동일 R-ID resolver가 과거 부재를 확인한 신규 Requirement는 이전 원문 없이 비교할 수 있다. 선택 PRODUCT의 이전·현재 부재도 성공한 tree 조회로 확인한다. 현재 필수 source 누락·Requirement 모호함·tree 또는 blob 조회 실패는 평가를 중단한다. 현재 원격 이력에서 관련 입력의 확정 revision을 확인하며, 후속 명세 변경의 일반 planning이 확정되지 않았으면 판단을 보류한다. 원격 원문을 로컬·runtime 문서로 대체하지 않는다.

`USER_DECISION_ASSESSMENT`의 `resolved`는 bool, `detail`은 비어 있지 않은 이유, `evidence`는 `source`·`quote` 배열이다. 해소에는 변경된 현재 source의 정확한 인용과 ExecPlan 이외의 상위 명세 인용이 필요하다. 삭제·부재·관계 추가만으로 해소하지 않고 질문이 남거나 결과가 불확실하면 차단을 유지한다. checkpoint의 `user_decision_assessment`와 이력은 질문·관련 집합과 내용·후보·확정 입력의 identity, 이전·현재 revision·존재 여부·판단·인용을 보존한다. 링크·표현 정규화 후 같은 의미·후보의 유효한 결과는 현재 인용을 다시 확인해 재사용한다. 저장된 draft 판단은 R-061의 출처·답변 검증에 맡긴다. 확정 planning에 따른 질문 해소는 이전 재개 경로에서도 세션·실패·level을 초기화하지 않고 IMPLEMENTING으로 이어간다. 무효화할 이전 Worker·verify·Review 결과는 checkpoint의 `planning_resume_artifacts`에 남기고 현재 성공 token은 제거한다. 새 원인이나 미완료 Git operation이 있으면 이 전이를 거부한다. 원격·Agent 관측은 DB 쓰기 잠금 밖에서 수행하고, 짧은 transaction에서 원인·작업 identity·checkpoint generation·확정 planning 입력을 재확인한다. Agent 실패·형식/인용 오류·source 또는 원인/CAS 변경은 해소 근거가 되지 않는다. 일반 입력 오류는 현재 metadata를 재검증하고 rebase의 안전한 시작·대기·완료는 기존 Git 소유 증거로 판단한다.

| 저장 상태 | 실행 자리와 보존 책임 |
|---|---|
| 작업별 실행 원본 | `exec_plans`에 단계·작업 identity·소유 자원·session·실패 이력을 유지한다. 프로젝트의 현재 작업을 바꾸어도 이 원본을 초기화하지 않는다. |
| 프로젝트의 현재 작업 투영 | 프로젝트 조율의 대표 작업을 연결한다. 작업 스레드는 자신의 plan 선택으로 원본을 읽고 갱신한다. 프로젝트 planning으로 전환할 때 재개할 작업을 고정하고 투영만 비우며, 검증·게시·DB 적용 후 같은 작업을 다시 투영한다. |
| 보존된 planning 상태 | worktree·session·미게시 결과·오류·실패 이력을 별도로 유지한다. 보존 기록이나 오류 자체가 실행 자리 점유를 뜻하지 않는다. |
| 미확정 publication intent·공유 Git operation | 원격 게시·DB 적용과 소유 operation의 남은 처리를 복구한다. 미확정 게시나 종료 불명 writer의 실제 실행 자리·소유 예약을 다른 작업을 위해 해제하지 않으며, 관리 개수는 작업 상태별 조건을 따른다. |

프로젝트 호환 갱신의 실행 필드 반영은 일반 갱신과 legacy mirror가 같은 경계를 사용한다. planning 진입·투영 해제는 이전 대표 ExecPlan에 반영하지 않고, 명시적 작업 선택이 없는 planning 중의 후속 갱신도 프로젝트에만 저장한다. 이미 소유권이 확정된 작업의 branch·worktree identity가 잘못 저장된 소유권 차단은 해당 작업의 진행 중인 operation이 없는 checkpoint에 저장된 자원 연결과 예상 HEAD로 복구한다. 현재 canonical 경로·branch의 Git 등록·HEAD·index 안전 조건과 두 관측의 작업물·대상 등록 안정성을 확인하고, 기존 호출 종료·작업 및 원래/복구 대상 자원 lease와 다른 소유자의 claim을 검사한다. 짧은 transaction에서 원인·프로젝트·작업·checkpoint를 재확인한 뒤 branch·worktree만 복원하고 이전 값과 차단을 checkpoint 감사에 남긴다. 복원과 감사는 함께 rollback하며 기존 차단 해제 경로가 현재 상태를 다시 검증한다. 근거 누락·현재 HEAD 불일치·진행 중 operation·writer 종료 불명·경쟁 변경에는 복원을 적용하지 않고, 단계·session·실패와 성공 근거를 초기화하거나 Git 작업을 수행하지 않는다.

일반·유지보수 planning이 실행 자리나 미확정 게시·공유 Git operation을 소유하는 동안 다른 ExecPlan의 차단 복구는 프로젝트 투영을 교체·정리하지 않는다. 안전 gate가 허용한 PR Merge·Close 확인과 해당 계획의 상태·알림 기록은 계속할 수 있다.

`planning/selection_service.py`는 worktree·writer 종료·소유권·역할 결과와 Git operation·게시·공유 안전 조건을 검증한 뒤 planning 상태를 보존하고 자리를 비운다. 관련성·검증 근거가 없는 계획과 전이적으로 의존하는 후속 작업은 보류하며, 독립 작업의 재개·선택은 기존 catalog·원격 revision·activation gate를 통과해야 한다. 소유 증거 부족이나 미해결 공유 안전 조건에서는 기존 상태를 보존한다.

오류 원인이 해소되기 전에는 새 Planner 주기와 프로젝트 완료를 선언하지 않으며 같은 입력·재시작에서 오류를 지우거나 호출을 반복하지 않는다. 이 오류 보존·실행 자리 해제는 현재 동작이다. R-055의 남은 문서 삭제·관리 자원 정리 목표는 [원격 catalog와 실행 이력의 분리](#원격-catalog와-실행-이력의-분리)에서 구분한다.

상태별 실행 자격과 dependency·priority·명세 변경·유지보수 DAG의 선택 순서는 [PRODUCT의 작업 선택 정책](PRODUCT.md#작업-선택과-동시-실행)을 따른다. 일반 작업은 독립 branch·worktree를 사용하며 공유 수동 묶음 내부는 직렬이다. 병렬 결과의 통합은 기존 최신 base 확인·소유 rebase·재검증을 따르며 PR 자동 재정렬은 지원하지 않는다.

### 유지보수 분석과 성공 근거

유지보수는 정적 사실 수집, 의미 검토, 게시와 평가 성공, 관련 작업 Merge와 cycle 종료를 차례로 확정한다. 앞 단계의 근거만으로 뒤 단계의 성공을 만들지 않는다.

| 단계 | 확정하는 근거와 다음 단계의 조건 |
|---|---|
| 정적 분석 | 원격 파일의 hash·mode·분석 버전에 결합된 구조 사실이다. 코드맵·조회 결과를 의미 검토 성공으로 사용하지 않는다. |
| 모듈·루트 검토 | 원본 인용·범위·지침·protocol을 검증한 잠정 판단이다. 모듈 발견은 계획 또는 근거 있는 제외에, 루트 이동 후보는 계획에 연결한다. |
| 게시·평가 성공 | 필요한 계획·barrier 변경의 원격 게시와 catalog 반영 및 현재 입력 일치를 확인한 뒤 SQLite transaction으로 평가 근거를 확정한다. 실패 시 잠정 근거·intent를 보존한다. |
| cycle 종료 | 확정된 범위와 dependency closure가 실제 MERGED이고 원격·catalog 반영과 게시·hold 조건이 해소돼야 한다. 이때 다음 자동 주기의 Merge 기준을 갱신하며 프로젝트 전체 완료와 구분한다. |

새 cycle의 시작·`full` 범위 선택과 기존 평가의 재개·재사용은 아래에서 구분한다.

#### 정적 사실과 평가 시작 기준

Manager는 최신 원격 추적 파일의 경로·내용 hash·mode와 분석 버전에 결합된 정적 사실을 저장한다. 변경된 파일만 다시 파싱하되 현재 파일 집합에서 관계를 다시 계산한다. 정적 코드맵·읽기 전용 조회는 심벌·import·직접 참조·관련 테스트·분석 불확실성을 제공하며 의미 검토 성공 증거는 아니다. 정적 사실은 `docs/maintenance/code-map.jsonl`과 Agent 조회용 별도 snapshot에 제공하고, Manager의 운영 SQLite·session·상태를 Agent 입력으로 내보내지 않는다.

유지보수 이력이 없는 프로젝트의 최초 실행 시 확인한 MERGED 수를 첫 자동 주기 기준으로 SQLite에 보존한다. 등록만으로 기준을 설정하지 않으며, 최초 실행의 원격 catalog 검증을 마친 안전한 선택 경계에서 한 번 기록한다. 이 기준은 평가 성공·cycle 정상 종료 근거와 구분하며 재시작에서 다시 설정하지 않는다. 기존 활성 cycle·정상 종료 기준·미확정 게시·실패 이력은 보존한다.

`[loop] refactoring_review_interval`은 기본값 20의 0 이상 정수로 주기를 지정한다. 양수 n에서는 저장된 최초 실행·직전 정상 종료 기준 이후 새 MERGED 수가 n개 이상이면 다음 안전한 경계에서 자동 전환한다. 0은 자동 전환만 끄며 활성 cycle의 진행·종료와 `maintenance-review <repo>`의 명시적 요청은 유지한다.

값 변경·재활성화에서도 같은 Merge 기준을 보존하고, 요청은 다음 안전한 경계에서 기존 pause·실제 Open PR·프로젝트 안전 gate를 준수해 한 번 소비한다. 활성 cycle 중 접수된 새 요청은 다음 cycle의 시작까지 보존한다.

새 cycle의 진입 판단과 평가 범위 선택은 분리한다. 주기 도달·명시적 요청으로 새 cycle을 시작하면 `full`을 강제한다. 전체 점검 성공 이력의 부재나 정적 비교 결과만으로 최초 실행에서 유지보수를 시작하지 않는다. 평가 엔진은 `fast`·`targeted`·`full`을 지원하며 재개 시에는 저장된 범위와 필요한 관련 근거만 다시 확인한다. `fast`는 루트·모듈 검토 unit과 새 의미 receipt가 없는 모델 무호출 경로다. 현재 원본·지침에 검증된 루트 조사 근거가 없으면 정적 fast 조건에 해당하는 모듈 범위는 비운 `targeted`로 분류하고 루트 unit만 생성한다. 모듈 정밀 검토가 필요하거나 기존 전체 점검 조건이 적용되면 그 범위와 루트 unit을 `targeted` 또는 `full`로 함께 처리한다. 미완료 fast assessment 재개도 같은 분류를 적용하고 기존 원문·session·audit를 보존한다.

정밀 점검은 관련 파일을 모듈 단위의 제한된 묶음으로 구성하고, 원본·mode·정적 사실·분석 버전·평가 조건에 결합된 묶음 identity의 성공 결과를 재사용한다. 전체 점검의 성공 Merge 기준과 최신 정적·부분 평가 기준은 따로 보존한다.

#### 의미 검토와 루트 조사

모듈 의미 검토는 읽기 전용 Planner 호출로 필요한 원본과 보존 조건을 확인한다. Manager는 묶음 범위·판단·발견과 실제 원본 인용을 검증해 잠정 결과로 보존한다. 모듈 발견과 루트 이동 후보가 모두 없고 일반 계획도 필요하지 않으면 추가 종합·계획 호출 없이 no-change 결과를 준비한다. 모듈 발견은 새 계획·기존 비종료 계획·근거 있는 제외에, 루트 이동 후보는 새 계획 또는 해당 목표를 포함한 비종료 계획에 연결한다. 저장된 검토를 사용하며 파일별 검토·관계 확인·종합 페이지를 다시 기본 단계로 실행하지 않는다.

같은 tiered assessment에는 별도 `.` 루트 조사 unit을 둔다. unit identity는 `root_review_source`의 원격 tree hash·mode·revision, 모든 root placement와 현재 runtime 지침에 결합하고, 기존 `parse_review`·`validate_root_reviews` 계약으로 제외 분류된 문서·자산·manifest·설정까지 실제 인용을 검증한다. 루트 배치와 validator는 `.gitignore`, `.codex/config.toml`, `.codex/rules/` 하위, `.claude/settings.json`·`settings.local.json`이 inventory에 있을 때만 해당 경로와 부모 루트를 보호하며, 유사 이름이나 부재만으로 위치를 필수화하지 않는다. 모듈 receipt는 assessment의 `guidance_hash`, 루트 unit은 현재 지침의 64자리 소문자 SHA-256 fingerprint인 optional `root_guidance_hash`에 결합한다. 성공 row는 그 루트 fingerprint를 기존 `guidance_hash` 필드에 보존한다. `root_guidance_hash`가 없는 legacy assessment는 이전 `guidance_hash`를 root identity로 사용하고, 지침 identity가 없거나 성공 row의 root receipt가 nullable이면 재사용하지 않는다. 이 분리로 같은 cycle 재개에서 완료한 모듈 판단을 보존하면서 지침이 바뀐 루트 근거만 새 unit으로 검토할 수 있다. 분류 교정은 기존 fast 평가의 관련 모듈 범위를 넓히지 않으며, assessment identity 갱신과 pending session 또는 검증된 동일 지침의 root result 연결은 한 SQLite transaction에서 수행하고 이전 원문·audit도 보존한다.

루트 protocol·scope audit는 모듈 receipt와 분리되며, 정적 inventory나 모듈 결과만으로 루트 완료를 만들지 않는다. 같은 guidance·revision의 검증된 파일별 root review 또는 이전 tiered 성공 결과만 재사용한다. 이전 tiered 성공의 root review는 성공 row에 보존된 루트 지침 hash가 현재 지침 identity와 같고 source revision도 일치할 때만 재사용한다. legacy 성공 row에 루트 조사 근거 또는 지침 identity가 없으면 재사용하지 않고 필요한 루트 unit을 다시 검토한다.

일반 planning의 검토 지문은 원격 파일 내용·지침으로 계산하며 DB의 호출 session·error를 포함하지 않는다. 유지보수 unit의 `session_id`·`error`도 실행 기록으로만 보존하고, 루트 결과의 저장·재검증·기존 assessment 결과 연결에서는 이 두 필드를 제외한 unit을 scope로 사용한다. 기존 audit의 `scope_json`에 두 필드가 포함돼 있어도 원문·정규화 결과와 protocol·source revision·나머지 scope가 모두 유효하면 재사용하며 원래 audit는 덮어쓰지 않는다. 다른 추가 필드·실제 scope 변경·원문 불일치는 계속 거부한다. 작업 문서·참조 명세의 변경 감지와 차단 재평가 입력, 게시 직전 실행·소유권 CAS는 이 범위 정규화와 분리한다.

이동 후보의 링크는 후보 조사 hash와 계획 경로·본문 hash를 별도 저장하고 새 계획 또는 비종료 계획의 보존·검증 조건을 확인한다. 최종 Planner disposition에서는 후보 연결을 임시 보관하고, 전체 planning result 검증이 통과한 뒤 신규 계획 연결에만 ID allocation을 적용하며 allocation의 원본 경로가 임시 계획 경로와 일치하는지 확인한다. 기존 계획 연결은 allocation의 ID가 같아도 원래 ID·경로를 보존한다.

READY barrier dependency 편집이 반영된 최종 계획 ID·경로·본문 hash에 연결한다. collect·intent 저장은 작업 worktree의 최종 계획 본문을 검사하며 promote는 게시 revision의 blob과 catalog의 비종료 계획을 다시 대조한 뒤에만 root review를 성공 baseline에 포함한다. 필수 도구·문서 경로 보호와 기존 cycle·한 번 평가 cadence는 `validate_root_reviews`와 기존 publication 경계에 남긴다.

#### barrier 편집과 성공 확정

barrier mode의 유지보수 Planner는 선행과 새 리팩터링 DAG를 판단한다. Manager는 DAG 말단과 SQLite cycle rows로 필요한 선행 dependency closure를 계산한 뒤, closure 밖의 적격 READY snapshot에 누락된 말단 dependency만 기존 순서 뒤에 추가한다. 파일을 수정하기 전 cycle·snapshot 소유권을 다시 확인하고, 예상한 dependency-only diff와 catalog의 acyclic 관계를 검증한다.

실제 Manager diff는 planning result의 hash·publication path에 포함되어 원격 게시·catalog transaction·복구 검증을 통과한다. 게시 본문은 검증한 checkout과 CRLF/LF 줄바꿈 차이만 허용하며 다른 clean filter 내용 변환은 거부한다. 새 계획 등록은 현재 checkout hash와 Git 게시 blob의 감사 hash를 분리해 보존하고, Merge·archive 복구는 publication SHA의 blob hash로 증명한다.

다른 READY 파일 변경, 사용자 결정·draft 해제, snapshot 밖 파일과 실행 중·소유된 계획은 허용하지 않는다. 새 단계별 평가의 일반 계획은 별도 필요한 계획 주기에서 수행한다. 일반 계획 결합 여부와 종전 결과를 이미 보존한 cycle은 해당 mode·게시 검증을 유지해 복구한다.

planning batch의 평가 근거는 외부 관측과 DB 적용을 분리해 확정한다. 준비 단계는 공통 DB 접근을 해제한 뒤 파일/root의 revision·hash·mode·선정 범위·지침·실제 원본 인용, 게시 blob과 마지막 원격 revision을 확인한다. 파일별 legacy 복구는 저장된 publication intent와 게시 commit의 ancestry를 유지하며, tiered는 저장된 원문·receipt·루트 계획 연결을 같은 원본과 대조한다. 준비 값은 원본·게시 identity와 직렬화한 검토 근거 및 준비 시 DB 입력을 호출 안의 불변 값으로 보존한다.

DB 적용은 준비 값과 현재 프로젝트 소유권·cycle revision·선정 ledger·성공 기준·publication intent를 대조하고, tiered의 assessment·unit·audit·루트 계획 연결도 비교한다. 변경된 입력은 낡은 성공 적용을 거부한다. 루트 계획의 현재 catalog 경로·비종료 상태와 batch의 기존 소유권·generation CAS도 유지한다. 적용 중 Git·원격·지침 파일 관측을 반복하지 않으며, 성공 행·코드맵·성공 기준은 기존 게시 transaction에 함께 참여해 실패 시 rollback한다. 게시 batch·게시 없는 빈 평가 완료·legacy 빈 평가 복구는 상위 transaction에 진입하기 전에 준비하며, 공개 ledger 호출도 같은 준비/적용 경계를 사용한다. 공통 접근이 조율하는 명시적 transaction에서 준비를 요청하면 거부하고 호출자가 준비를 외곽으로 옮기도록 한다.

미게시·관측 실패·원격 source 변경·DB 경쟁은 성공 기준을 전진시키지 않으며 기존 성공 행·잠정 근거·publication intent를 보존해 복구한다. 외부 관측은 DB 적용 재시도에 포함하지 않는다. DB schema와 JSON·receipt 형식, cycle 주기와 legacy 복구 계약은 유지한다. 새 리팩터링 계획은 검증된 DAG와 Manager barrier 편집을 포함해 게시한다. 계획 등록과 나머지 게시 batch 외부 처리는 별도 경계이며 이 근거 적용의 DB 전용 보장에 포함하지 않는다.

archive가 최초 평가 workspace 생성 전에 base를 전진시키면, 게시 intent·평가 결과·완료 근거·보존 workspace가 없는 assessment만 기존 context 재시작으로 최신 base에 맞춘다. 같은 cycle·단계를 유지하고 revision을 올려 낡은 context를 무효화한다. 저장된 결과·workspace·게시 intent는 각자의 기존 복구 경계를 따른다.

평가 성공과 cycle 종료는 별도 상태다. cycle 범위는 선행·리팩터링·발견을 연결한 기존 계획과 그 dependency closure를 포함한다. 범위의 모든 계획이 실제 MERGED이고 원격·catalog에 반영됐으며 미확정 게시·해당 범위의 catalog hold가 없을 때 종료하고 다음 자동 주기의 Merge 기준을 갱신한다. 작업이 없으면 검증·필요한 게시 뒤 종료할 수 있다. 종료 기준 commit은 내용 보존 archive까지 반영한 현재 catalog revision이며, archive 자체를 실제 Merge 수에 더하지 않는다. 마지막 Merge 뒤 새 평가나 사후 재검증을 종료 조건으로 추가하지 않으며, 유지보수 종료를 프로젝트 전체 완료로 기록하지 않는다.

#### 결과 해석과 공백 원본

역할별 기존 parser 경계는 공통 형식 helper로 marker·구분자·공백과 다중 줄 JSON을 정규화하고, 검증된 입력 identity만 누락 결과에 결합한다. 명시적 불일치·중복 선언·중복 JSON key와 의미 근거 누락은 거부한다. Worker·Reviewer·Planner의 저장된 원문은 같은 snapshot에서 다시 해석하며, 모듈 결과는 원문·정규화 결과·원본 revision·scope·protocol을 함께 보존한다. 모듈 묶음 identity에 결과 protocol을 포함해 다른 계약의 receipt를 재사용하지 않는다.

정적 코드맵은 내용 hash·분석 버전에 결합된 공백 여부를 제공한다. 같은 원본 hash·범위의 이전 분석은 활성 assessment 재개에서도 분석 사실만 갱신하고 기존 판단을 보존한다. 파싱이 확인된 빈 파일만 있는 모듈은 모델 묶음을 만들지 않고 assessment의 원본 사실을 보존한다. 빈 파일이 속한 의미 있는 모듈은 검토하며 내용이 있는 원본을 인용해야 한다. 전체가 공백 원본이어도 분석 불확실성이 남은 묶음은 의미 검토를 유지하고 실제 공백 원본을 인용하며, 0 byte 원본만 빈 인용을 허용한다.

미지원·파싱 실패·동적 관계의 불확실성은 유지한다. 복구용 파일 결과에서 누락된 공백 chunk나 compact 결과의 null 공백 항목은 실제 byte 범위·hash·인용·공백 사실로 결합하며, 내용이 있는 chunk의 판단은 생성하지 않는다.

종전 파일별 평가 경로는 이미 보존된 결과·게시의 복구를 위해 유지한다. 실제 검토 파일의 ledger·의미 코드맵·파일별 성공 기준은 그 경로에서만 원본·관계·게시 검증을 거쳐 확정하며, 정적 코드맵이나 새 모듈 평가를 파일별 의미 검토 완료로 기록하지 않는다.

## AI Agent 실행

판단·구현·검토의 역할과 실제 실행 효과의 소유자를 분리한다. 입력을 전달받거나 결과를 반환한 사실만으로 파일·Git·상태 변경 권한을 얻지 않는다.

| 주체 | 주요 입력 | 허용 출력과 책임 |
|---|---|---|
| Planner | 현재 명세·Requirement 목록과 관련 본문·구현·테스트·compact catalog·호출 mode와 snapshot | mode와 snapshot이 허용한 계획·FUTURE_IDEAS 또는 읽기 전용 판단 결과. 구현·검증 명령·Git 게시·사용자 명세 수정은 하지 않는다. |
| Worker | 선택 ExecPlan·sources·dependency·현재 코드와 이전 실패 | 작업 구현·허용 문서·선택 ExecPlan 본문·targeted test와 결과 보고. 선언·다른 계획·Requirement·사용자 결정을 수정하지 않는다. |
| Reviewer | 최신 후보·명세·diff와 전달된 구현·검증 근거 | 읽기 전용 판정·blocking issue·해당 경로의 검증 보완 요청. 파일 수정과 테스트·verify 재실행은 하지 않는다. |
| Manager | 검증한 역할 결과·원격 상태·SQLite 실행 및 소유 증거 | 상태 전이·Git/GitHub·전체 verify·게시·복구를 수행하고 결과의 범위·identity·재사용 조건을 검증한다. |

세부 문서·결과 형식과 현재 runtime 지침·호출 기록·session 경계는 아래 절을 따른다.

### 지침 원본과 호출별 적용

- 사용자 요청의 새 기능·정책 변경은 대화형 Agent가 관련 Requirement를 먼저 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용한다. 최초 설계 작성·현행 문서 정리는 Skill의 작성 대상 판단을 따른다. 프로젝트로 복제하는 Specification Skill은 제품 명세 작성·검토만 안내하고 Manager 내부 실행 절차를 설명하지 않는다. 이 저장소 루트 AGENTS는 Skill을 참조하며 문서 작성 기준을 반복하지 않는다. 직접 대화형 작업에는 ExecPlan 작성을 요구하지 않으며 생성·계획 작성은 매니저가 호출하는 자동 Planner 흐름에서 수행한다. Manager의 `manual` 명령은 직접 대화형 작업과 구분하고 별도 소유권·검증·게시 계약을 적용한다
- 정적 자동 실행 규칙은 선택 runtime의 `docs/AGENTS.common.md`와 `docs/AGENTS.planner.md`·`docs/AGENTS.planner_check.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`에 둔다. common에는 모든 역할의 기준만, 역할 파일에는 해당 역할의 권한·금지사항·절차만 남긴다.

  Skill과 Manager가 프로젝트에서 쓰는 예약 경로는 PRODUCT의 프로젝트 예약 경로 목록을 따르며 공통 지침과 명세 작성 Skill에 위치 보존을 명시한다. runtime 전용 `docs/AGENTS.*.md`는 프로젝트 예약 경로가 아니다. Loop Manager 전용 새 루트 항목 제한·instruction 원본 수정 지침은 이 저장소 루트에 두며 runtime 공통·역할별 합성 대상에 포함하지 않는다

- `agents/common_guidance.py`는 매 호출 현재 `RUNTIME_ROOT`의 공통·역할·Ponytail 원본을 읽고 common+해당 역할을 합성한다. Worker만 Ponytail을 정확히 한 번 포함한다. 작업별 snapshot 선택·저장·복원은 없으며 재시도·재개·새 세션·provider 변경도 현재 runtime을 사용한다. 적용 시점은 [PRODUCT](PRODUCT.md#지침-원본과-호출별-적용)를 따른다.
- Codex의 추가 developer instruction 또는 Claude의 append system prompt로 전달하며 기본·기존 추가 지침·프로젝트 규칙·실행 경계를 보존한다. 역할별 합친 Markdown은 저장소 밖 `agent-instructions/`의 실행용 파일이며 원본 묶음 JSON은 새로 저장하지 않는다. 현재 runtime 원본 오류·실행용 파일 변조·미지원 전달 기능·설정 충돌·알 수 없는 역할은 실행 전에 거부한다.
- 기존 `common_guidance_versions`와 `agent-guidance/`는 감사 자료로 보존하고 실행 입력으로 읽지 않는다. 기존 session의 작업 귀속은 유지하되 snapshot 버전 참조와 분리한다. 과거 snapshot 누락·손상으로 현재 호출을 막지 않으며 현재 원본 오류를 옛 지침으로 우회하지 않는다.
- definition의 `guidance_hash`와 planning 입력은 현재 runtime 전체 원본 내용 해시를 사용한다. `worker_guidance_hash`와 invocation ledger는 마지막 Worker와 각 호출에 실제 전달한 내용의 해시를 보존한다. Reviewer 호출은 Worker 전달 해시를 바꾸지 않는다. 현재 지침과 다른 과거 성공은 동일 입력의 성공으로 재사용하지 않으며 내용이 같으면 runtime SHA·위치 변경만으로 무효화하지 않는다.
- runtime 갱신 비교는 `docs/AGENTS.*.md`를 일반 문서 prose 제외 대상에서 빼므로 지침만 바뀌어도 기존 안전한 handoff로 새 runtime을 선택한다. 실행 중인 호출의 내용은 도중에 바꾸지 않는다.

- Planner, Worker와 Reviewer의 작업별 prompt는 Loop Manager에 내장하며 목표·snapshot·승인 사실·피드백·결과 형식 등의 문맥을 구성한다. 정적 공통·역할 규칙을 이 사용자 메시지에 중복하지 않는다
- Worker 사용자 메시지는 최초·교체 session에 선택 계획·worktree·결과 형식과 미완료 문맥을 보내고 같은 provider/Model session에는 확인되지 않은 새 조건·archive 후보·비검증 오류·checkpoint/선언 복원 사실·검증 피드백 delta를 보낸다. 전체 완료 책임과 완료 조건별 충족 근거·잔여 작업 대조는 매 호출에 안내한다. 같은 session에 이미 전달한 active 검증 피드백은 identity·occurrence·revision과 인식된 실패 이름(없으면 진단 첫 줄 최대 200자)의 짧은 미해결 목록으로 다시 보낸다. 진단 잘림·누락 표식은 유지하고 traceback·이전 결과 원문을 반복하지 않는다. 이 목록은 전달 cursor나 실패 관측·해결 상태를 변경하지 않으며 해당 단계의 새 관측에서 active가 해제되면 사라진다. effort 변경은 같은 session의 재개다. 이전 Worker 결과와 verify/Review 원문은 복사하지 않으며 수동 교정도 전체 G·후보 R·사용자 생략과 미확인 외부 테스트만 유지한다.
- 기존 validation delivery attempt/cursor에 작업 문맥 JSON과 attempt 순서를 결합한다. 호출 전 revision을 고정하고 실제 session에 결합하되 정상 scope·결과 검증 뒤에만 두 채널의 전달을 확인한다. timeout·손상 결과·저장 실패는 미확인 문맥을 재전송하고 호출 중 발생한 새 revision은 확인하지 않는다. 늦게 도착한 과거 attempt가 최신 문맥 cursor를 되돌리지 않는다. source 지문·revision이나 순수 수행 기록만의 변화는 작업 조건 재전송을 만들지 않는다.
- 프로젝트별 prompt 재정의는 지원하지 않으며 프로젝트 파일이나 작업 prompt로 자동 역할 권한을 확대하지 않는다

### 호출 관측과 ledger

호출자는 `InvocationContext`로 목적과 가능한 target을 명시하고 물리 시도별 invocation에 역할·provider·실제 Model·Effort·session 전이·attempt·결과를 연결한다. 유지보수 batch target은 active source revision과 포함된 파일·관계·페이지 identity를 보존한다. CLI 버전은 같은 invocation의 기존 격리 증거에서 확인한다.

| `execution_status` | 의미와 집계 |
|---|---|
| `prepared` | ledger에 호출 준비를 기록했으며 실제 시작은 아직 확인되지 않음. 실제 호출 수에 포함하지 않는다. |
| `executed` | 성공한 provider process 생성이 확인됨. 결과의 성공·실패와 관계없이 실제 호출 수에 포함한다. 종료 여부는 별도 근거로 판단한다. |
| `not_executed` | 실행 전 거부·프로세스 생성 실패 등으로 시작하지 않음. 미실행 수로 분리한다. |
| `unknown` | 시작 경계가 불명확한 중단·legacy 기록. 실제 호출로 추정하지 않으며 미완료 `prepared`와 함께 집계의 실행 미확인 수로 분리한다. |

실제 retry·usage는 `executed`에 한해 집계한다. 사용량의 입력·캐시 입력·캐시 기록 입력·출력·추론 출력 토큰은 각각 정수 또는 `null`이며 누락을 0으로 대체하지 않는다. 수집된 값만 합산하므로 일부 값이 없는 합계는 전체 사용량의 증거가 아니다. 캐시 비율은 필요한 값이 함께 확인된 호출만 사용한다. Codex의 입력 토큰에는 캐시 입력을 포함하고 Claude의 입력 토큰은 비캐시 입력으로 취급하며 공급자별 의미를 보존한다.

ledger의 `duration_ms`는 호출 처리 전체 경과 시간이고 `execution_duration_ms`는 프로세스 시작부터 종료 확인까지의 시간이다. 집계 결과의 `duration_ms`는 후자를 합산한다. 시작은 확인됐지만 종료 대기가 실패하면 `executed`를 유지하고 종료 시각·실행 시간은 미확인으로 둔다. 프로세스 종료 근거 자체는 writer 종료만 증명한다. 별도로 보존한 실제 호출 관측이 있을 때만 원래 결과·사용량·실행 시간을 복구하며, 기존 행에 목적·대상·실행을 역으로 추론하지 않는다.

선택적 실행 관측·부가 기록 실패는 수집 상태로 격리해 Agent 결과를 보존한다. 필수 상태·격리·소유권·검증 증거의 저장 실패는 기존 실행·검증 경계로 처리하며, Worker targeted test 결과 수집 실패를 성공으로 바꾸지 않는다. ledger에는 prompt·비밀값·인증 자료·코드·문서 전문을 추가하지 않으며, 호출별 provider 상세 파일은 [기록 원본과 보호](#기록-원본과-보호)의 별도 진단 기록으로 연결한다.

#### DB 대기와 호출 결과 확정

DB 전용 준비·확정은 `CoordinatedConnection.run_request()`의 공통 예산·rollback/재조회를 사용한다. invocation 생성과 지침 결합, launch의 예약/receipt 결합은 각각 원자 요청이고, 격리·실행 identity·targeted 결합도 CLI 시작 전에 확정한다. targeted 경로·device/inode 검사는 SQL 재시도 밖에서 수행하고 SQL은 미시작·미완료 Worker 소유권을 다시 확인한다. provider·Git·파일 생성·임의 session callback은 이 요청에서 replay하지 않는다. invocation 준비는 launch lock을 잡지 않으며 실제 spawn의 handoff gate는 유지한다. 완료된 callback의 암시적 쓰기는 기존 요청 종료 경계에서 한 번 확정한 뒤 재시도 요청을 연다. 명시적 caller transaction에서는 savepoint로 합류해 DB 연산을 한 번만 수행하며 바깥 본문을 replay하거나 중간 commit하지 않는다.

Manager 전용 process receipt는 기존 PID·호스트/boot·종료 근거와 별도로 `invocation_observation`을 원자 저장한다. 시작·종료 receipt 갱신은 이 관측을 보존하며 경로·platform·invocation identity·형식을 검증한다. 필드는 다음과 같고 관측하지 않은 값은 추가하지 않는다.

| 필드 | version 1 계약 |
|---|---|
| `version`, `identity` | 정수 `1`과 결합된 invocation ID. 다른 버전·identity·알 수 없는 필드는 거부한다. |
| `started_at`, `finished_at`, `duration_ms` | timezone을 포함한 ISO 8601 시각과 0 이상의 정수 실행 시간. 종료 관측은 시작과 실행 시간을 함께 요구하며 첫 관측 시각을 DB 재시도 시각으로 바꾸지 않는다. |
| `session_id` | 관측한 비어 있지 않은 session 문자열. 귀속 충돌은 계속 거부한다. |
| `request_session_id` | 원래 요청 session의 비어 있지 않은 문자열 또는 신규 요청을 뜻하는 null. callback·결과의 session으로 덮지 않으며 원래 요청 지문 검증에 사용한다. |
| `outcome` | 필수 key는 `kind`, `code`, `duration_ms`, `session_id`, `usage`, `collection_status`, `observed_at`이다. 각각 종류·정수/null 코드·0 이상 정수 처리 시간·session 문자열/null·사용량·문자열 수집 상태·timezone 포함 시각이다. 종류는 `success`, `transient_failure`, `usage_limit`, `permanent_configuration`, `timeout`, `exception`, `interrupted`, `database_wait`이고 성공 코드는 0이다. |
| `database_pending` | 저장/전달 확정 대기의 boolean. 관리 호출은 예약 정리와 결과 전달 경계를 지난 뒤 해제한다. |
| `request_sha256`, `message_sha256` | 소문자 16진수 64자리 지문. 요청은 요청 session ID(null은 신규 요청)·prompt·역할/provider/profile·작업 공간·지침·definition/목적/target 등 현재 호출 입력을 결합한다. 메시지는 실제 역할 응답의 지문이며 원문을 새 감사 자료에 복사하지 않는다. |

`usage`는 `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`의 0 이상 정수/null과 `status`의 `unknown`·`known`·`partial`·`invalid`를 요구한다. 관측 object의 version/identity 외 필드는 부분 관측 수집을 위해 선택 사항이며, 역할 응답 전달에는 outcome과 요청/메시지 지문을 모두 요구한다.

`apply_observation()`은 같은 invocation의 ledger만 확정한다. 재시작 정리는 먼저 이 관측을 적용하고 실제 결과가 없을 때 기존 interrupted 처리를 사용한다. 손상·identity 충돌·DB 적용 대기는 해당 행과 예약을 보존한다. 선택적 callback 관측의 일반 SQL 오류에서는 저장 가능한 원래 outcome을 수집 오류로 남기되 누락된 안전 증거를 채우지 않는다. 일시 DB 대기는 이 fallback 대신 같은 관측의 원자 요청을 재시도한다. 미시작 DB 대기의 `database_wait`는 provider 실패 분류와 구별한다. launch callback이 반환되기 전에 거부된 경우에만 `not_started` 종료 근거를 남기며, 기존 receipt나 시작 기록과 모순되는 미시작 근거로 종료·예약 회수·targeted 수집을 확정하지 않는다. 관측 필드가 없는 구형 receipt는 기존 종료 근거로만 취급한다.

관리 실행의 준비 대기 만료는 현재 작업 입력을 재확인한 RETRY 배정으로 이어간다. 결과를 확보한 호출은 원래 스택의 응답/예외를 유지하며 DB 확정만 다시 요청하고, 예약 정리가 대기해도 반환된 응답을 보존한다. 작업 조율기도 완료한 dispatch의 반환값을 보존해 결과 수집·보류 기록만 다시 처리하며 외부 효과를 재실행하지 않는다. 오류 기록의 DB 대기에는 원래 traceback과 저장 traceback을 함께 남기고 해당 저장 요청만 재시도한다. 보류 기록이 확정되기 전에는 일반 retry 묶음의 메모리 횟수도 확정하지 않는다. setup의 일반 예외와 DB 대기는 각각 별도 제한 묶음으로 재준비하고 RETRY 순서를 유지한다. 묶음 소진 뒤 일반 예외는 단계·후보·작업 입력·소유권 또는 기존 recovery identity·진전·다음 동작의 변경을 기다린 뒤 현재 자격을 재검사한다. 로그·session 생성·catalog generation·retry 타이머와 진단 횟수는 재편입 근거가 아니다. DB 묶음은 상태를 변경하지 않는 짧은 쓰기 transaction의 실제 재확보 뒤 같은 요청 준비로 돌아가며 누적 횟수를 초기화하지 않는다. 완료 Future로 수집한 보류도 조율 중 입력이 바뀌면 RETRY로 다시 편입하고, 최종 결과는 작업별 마지막 반환을 사용해 과거 보류가 후속 성공을 덮지 않게 한다. `TaskScheduler`는 확인된 실제 종료와 저장 대기를 구분해 실행 개수만 먼저 반환할 수 있다. 이 구분은 재시작 때 같은 프로세스 종료 근거로 복원하고 저장 대기 예약·관리 작업은 유지한다. 종료 불명은 실행 개수도 유지한다. 대기 중에는 local pause/handoff만 확인하고 runtime 원격 관측은 DB 준비 후 실제 단계 진입에서 수행한다.

활성 작업과 실제 호출 예약이 없으면 대표 실행 투영이 가리키는 작업의 미완료 Git operation을 확인해 오래된 실행 표시를 정리한다. 다른 차단 작업의 미완료 operation은 각 checkpoint에 보존하되 해당 투영의 정리를 막지 않는다. 투영의 소유 작업을 식별할 수 없으면 전체 미완료 operation을 확인하는 보수적 처리를 유지한다.

재시작의 응답 전달은 대기 표식이 있는 기존 호출에 한정한다. 요청 지문에는 원래 요청 session을 포함하고 그 값을 `request_session_id`로 보존한다. 재진입 session은 원래 요청 값 또는 같은 invocation의 callback·결과에서 관측한 session만 허용한다. 따라서 신규 요청 뒤 callback이 저장한 Worker/Planner session으로 재시작해도 원래 요청 session을 사용해 나머지 입력 지문을 검증하며 다른 session 요청은 보류한다. 일반 Planner의 session별 조회 안내 구성은 prompt를 만들기 전에 같은 미전달 receipt의 원래 요청 session으로 판단한다. 실제 재진입 session은 유지하고 복원한 전체 prompt도 기존 지문 검사에 통과해야 하므로, 정상 callback 전이만으로 안내가 빠지는 문제를 막으면서 다른 prompt 변경의 거부는 유지한다. 전달 확정 뒤의 새 Planner 요청은 현재 session으로 안내 구성을 판단한다. `request_session_id`가 없는 구형 관측은 현재 입력으로 원래 지문이 그대로 일치할 때만 전달하며 원래 session을 추정하지 않는다. session 입력 자체가 빠진 구형 요청 지문도 일치로 추정하지 않는다.

Codex adapter는 반환할 역할 응답을 해당 호출 attempt의 UTF-8 `last-message.txt`에 남긴다. Reviewer는 기존 응답 정제 뒤 파일을 원자 교체하며, 다른 역할은 CLI가 같은 attempt 경로에 출력한다. 기존 provider·role별 공유 최신 응답 파일은 최신 응답 소비를 위한 호환 파일로 유지하고 재시작 근거로 읽지 않는다. 요청 지문, ledger의 원래 결과·session·사용량과 session 귀속을 확인하고, 같은 invocation receipt가 결합한 attempt의 `last-message.txt` 원문 또는 그 양끝 공백을 제거한 값이 메시지 지문과 일치할 때만 `AgentResult`를 전달한다. attempt 파일 부재·symlink/경로 치환·지문 불일치·입력 변경·예외 원문 부족은 `CallDeferred`로 후속 처리를 보류하며 provider나 공유 최신 파일을 대체 증거로 사용하지 않는다.

관리 호출은 예약 정리 뒤 응답 반환 전에 기존 receipt의 `database_pending`을 해제한다. 표식 쓰기가 실패하면 원래 요청·응답을 현재 스택에 유지한 채 표식 확정만 재시도한다. 재시도 중 local pause/handoff를 확인하고 이미 반환한 실행 자리를 다시 점유하지 않으며 provider·작업 dispatch를 반복하거나 일반 예외의 `last_error`로 prompt를 바꾸지 않는다. 중단 시 관측 결과는 미전달 상태로 유지하고 재시작은 기존 입력 검증 뒤 복구한다. 전달된 결과를 계속 복구 대상으로 남겨 중복 응답이나 다음 요청의 영구 보류를 만들지 않는다. 이는 호출 성공 재사용·검증/게시 승인과 별개이며 기존 안전 gate와 targeted pending을 유지한다.

### Planner의 입력과 쓰기 범위

프로그램은 전체 ExecPlan을 코드로 파싱하고 비종료·미보관 계획의 metadata만 JSON compact catalog로 전달한다. 기본 필드는 `id`·`title`·`status`·`sources`·`depends_on`·`priority`이며 본문은 포함하지 않는다.

| 조건부 필드 | 제공 조건과 의미 |
|---|---|
| `draft: true`·`decision_wait` | 사용자 판단 대기 계획에 질문·결정 문맥을 제공한다. 사용자 변경 근거는 catalog에 중복하지 않고 별도 판단 문맥으로 전달한다. |
| `catalog_error` | catalog hold의 진단 근거다. 해당 항목의 `status`는 `CATALOG_HELD`로 표시하며 정상 실행 상태와 구분한다. |

catalog와 snapshot은 탐색·검증 입력이며 그 자체로 쓰기 권한을 만들지 않는다.

- Planner는 PRODUCT, 활성 Requirement, ARCHITECTURE, 관련 코드·테스트, 비종료 ExecPlan compact catalog와 프로그램이 제공한 PR 전 계획 snapshot을 읽고 현재 식별 가능한 미충족 작업 전체를 한 계획 주기에 반영하며, 범위 밖의 보존 가치가 있는 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
- Planner는 MERGED ExecPlan 메타데이터와 본문을 기본 입력으로 받거나 읽지 않는다. 프로그램만 ID 고유성, 번호 할당, 의존성 충족과 감사 이력을 위해 이를 유지한다
- Planner는 안전한 무인 실행 차단과 반복적인 사용자 복구 제거를 최우선으로 priority를 정하고, 정상 경로 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 계획한다
- Planner는 새 `docs/plans/*.md`를 생성하고 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있다. 프로그램의 PR 전 snapshot 안의 계획만 같은 ID로 개정하며, 시작한 계획의 경로·기존 수행 기록을 보존한다. rename·삭제는 미착수 계획에만 허용한다. snapshot 밖·PR 등록·MERGED·archive 계획과 그 밖의 파일은 변경할 수 없다.

  프로그램은 최종 게시 diff와 SQLite 계획 snapshot으로 문서 결과를 검증한다.

작업별 문서 영향 Planner는 새로 호출하지 않는다. 이전 버전의 판단·개정 checkpoint와 수정안은 [단계별 문서 반영](#단계별-원격-문서-반영)의 복구·감사에만 사용한다. 사용자가 지정한 문서 draft 호출은 별도 읽기 전용 결과이며 Manager가 후속 저장·게시 경계를 검증한다.

### Worker의 구현과 문서 경계

- Worker는 ExecPlan·관련 unit test·verify 스크립트를 구현한다. 구현으로 바뀌는 제품 동작·장기 규칙은 PRODUCT에, 설계·책임 경계·계약은 ARCHITECTURE에 같은 변경에서 반영한다. 범위 밖 아이디어는 FUTURE_IDEAS에 기록한다.

- 문서별 권한은 [PRODUCT의 Worker 문서 수정 범위](PRODUCT.md#worker-문서-수정-범위)를 따른다. 결과 검증은 다른 ExecPlan과 활성·archive Requirement의 변경을 거부하며 archive 후보 지정도 쓰기 권한을 만들지 않는다. 선언 교정은 Planner·Manager의 검증·게시·catalog 및 DB 반영으로 처리한다.

  scope의 경로 허용은 목표 관련성이나 사용자 판단을 승인하지 않는다. 후보 전체의 변경 범위는 관측한 등록 원본 브랜치 OID와 후보 HEAD의 공통 조상부터 비교해 rebase로 유입된 변경을 Worker 수정으로 오인하지 않는다. 이 원본 증거가 없으면 기존 catalog 기준을 유지하며, 공통 조상 조회 실패는 변경 없음으로 처리하지 않는다. 범위 오류에는 비교 원본·후보 revision과 경로·변경 종류를 남긴다.

  기존 ExecPlan은 파일이 삭제돼도 시작 index와 Git 원본으로 확인하며 결정·실행 기록을 파일째 제거하는 변경을 거부한다. 원본과 수정본을 대조해 draft 해제와 User Decision 변경·삭제를 거부한다. Worker 종료 후의 정상 경계에서는 저장된 시작 ExecPlan과 고정 공식 revision의 frontmatter가 같은지 확인한 뒤 선택된 frontmatter만 원자 교체한다. 현재 frontmatter가 catalog identity를 검증한 공식 선언과 이미 같으면 복원 쓰기 없이 후속 검증을 이어간다. 시작 전 차이가 있더라도 Worker가 공식 선언으로 바로잡은 결과를 복원 실패로 재시도하지 않는다. 본문 bytes·mode를 유지하고 파일이 다시 바뀌면 교체하지 않는다.

  변경 전후 선언·본문 hash는 기존 execution checkpoint에 보존하며 후속 checkpoint에서도 유지한다. index·HEAD·branch와 다른 문서는 복원하지 않고 Worker에 현재 diff·문서 보고 갱신을 전달한다. 진행 중 rebase나 경계·원본이 불명확하면 복원하지 않는다.

  `validate_active_execplan_metadata`는 이 처리를 포함한 Worker 결과를 검증·Review 전에 reconciled catalog의 title·sources·depends_on·complexity·risk·priority와 정확히 비교하고 불일치 필드를 보고한다. Requirement resolver의 경로 호환을 metadata 동등성으로 사용하지 않으며 잘못된 선언은 Worker 진입 전에 Planner·Manager 경계에서 교정한다

- `documents/pr_description.py`는 기존 실행 지침 보고 경계와 publication 호출을 재사용해 최종 base/head의 name-status와 문서별 patch를 검증한다. `docs/` 아래 파일과 저장소의 Markdown·RST·TXT 문서를 대상으로 추가·삭제·rename·copy를 식별하며 rename·copy는 양쪽 경로를 포함한다. 검증 전과 새 PR·기존 PR 게시 경계에서 현재 선택된 ExecPlan 경로를 전달하고, 최종 경로가 일치하는 파일만 문서 보고·`DOCUMENT_CHANGE` 제출 대상에서 제외한다. 이전 Worker 결과에 남은 해당 경로의 보고도 무시한다. 다른 문서의 보고 검사와 선택된 ExecPlan의 diff 검토·범위 검증, PR의 고정 링크·Purpose는 유지한다.

  보고 대상 경로 수집·문서별 patch 계산·저장 근거 재검증은 같은 작업 base와 후보를 사용한다. 작업 base는 해당 후보가 실제 기준으로 삼은 commit이며 최신 원격 OID·프로젝트 대표 투영으로 대체하지 않는다. Worker 결과 결합 시 기록된 실행 base와 현재 HEAD의 공통 조상으로 후보의 작업 기준을 확인한다. 이는 기존 범위 검사의 원본·후보 조상 관계와 같은 기준이며, 조회 실패·빈 결과는 복구 대상으로 남긴다. rebase 전에는 기존 작업 base, rebase 완료 후에는 통합이 확인된 대상 base와 새 후보 HEAD를 결합한다. Worker 결과를 commit하기 전에는 같은 작업 base와 범위 확인·staging된 후보를 비교하고, 검증·새 PR·기존 PR 갱신에서는 해당 후보 HEAD를 비교한다. Worker에 전달하는 보고·교정 문맥에도 실제 비교 base와 후보를 명시해 서로 다른 diff를 보지 않게 한다.

  최신 원격 base 반영은 별도 rebase·복구 경계에서 판단한다. 진행 중 rebase의 중간 상태를 최종 보고로 확정하거나 base 값만 최신으로 바꿔 검증하지 않는다. 작업 base를 확인할 근거가 없으면 원인·후보·작업물을 보존해 기존 복구 경계에서 확인하며 최신 원격이나 임의의 공통 조상으로 조용히 대체하지 않는다. 올바른 비교에서 실제 문서 patch가 변경된 경우의 설명 교정과 게시 전 재검증은 유지한다.

  Worker는 `PR_SUMMARY`의 각 줄 맨 앞에 `DOCUMENT_CHANGE:`를 두고 뒤에 JSON 객체 하나를 기록한다. 변경 문서별로 한 건이며 아래 필드는 모두 비어 있지 않은 문자열이어야 한다.

  | 필수 필드 | 계약 |
  |---|---|
  | `path` | 최종 diff의 저장소 상대 경로. rename·copy는 새 경로, 삭제는 원래 경로를 사용하며 같은 경로를 중복 제출하지 않는다. |
  | `summary` | 해당 문서의 주요 변경 요약 |
  | `reason` | 수정 이유 |
  | `impact` | 제품·설계에 미치는 영향 |

  Worker의 해시 제출은 요구하지 않으며 이전 응답의 `patch_sha256`도 무시한다. Manager는 Worker 결과 수신 뒤 범위가 확인된 후보를 staging하고 문서별 patch를 직접 해시한다. 결과 원문 지문과 문서별 patch 지문을 `manager_metadata`의 작업별 `document-report:` 항목에 저장한다. 검증·게시 직전에 해당 결과의 저장된 지문과 최종 문서별 diff를 코드로 비교한다. 결합 근거 부재·실제 patch 변경은 설명 교정으로 처리하며, 동일한 문서 patch는 base/head SHA 변경만으로 교정하지 않는다. Git 관측은 DB 쓰기 transaction 밖에서 수행하고 지문 저장만 짧게 처리한다.

  PR 문서 보고에는 파일명·변경 요약·이유·영향만 렌더링한다. 실제 patch·diff 링크·검증용 hash는 렌더링하지 않는다. 누락·중복·형식 오류·최종 diff에 없는 경로·patch 불일치는 일반 Summary로 대체하지 않고 기존 `WORKER_CORRECTION`으로 현재 후보의 설명을 보완하며 유효한 다른 문서의 설명도 함께 유지한다. 새로운 PR과 기존 PR 갱신 모두 불완전한 보고를 게시하거나 보고 반영 전에 게시 완료로 처리하지 않는다.

  열린 PR은 확인된 head에서 `<!-- loop-manager:common-guidance:start -->`와 `<!-- loop-manager:common-guidance:end -->` 사이의 Manager 소유 보고만 교체·제거하고 사용자 본문·immutable ExecPlan/source 링크·Purpose를 보존한다. 보고 경계가 없으면 새 보고를 덧붙이고, 경계 중복·짝 누락·순서 오류 또는 현재 PR head·본문 확인 실패는 게시 복구 오류로 처리해 덮어쓰지 않는다.

### 테스트와 Reviewer의 책임

Worker는 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행한다. 실패하면 같은 세션에서 수정·재검증하며 실행 가능한 관련 unit test가 없으면 사유를 결과에 포함한다. 전체 test suite·integration·end-to-end test·프로젝트 전체 lint·build·type check와 전체 verify는 Manager가 Agent 밖에서 실행한다.

Reviewer는 파일을 수정하거나 테스트·verify를 다시 실행하지 않고 최신 명세·후보 diff·전달된 결과로 요구 충족·회귀·테스트 누락·문서 정합성과 verify 약화를 판단한다.

| Review 경로 | 입력과 결과의 범위 |
|---|---|
| 기본 경로 | Worker targeted test와 Manager 전체 verify 통과 후 검토한다. |
| 일반 선행 경로 | 현재 후보의 Manager 부분 verify 결과와 선택·미실행 범위로 검토한다. PASS만으로 게시하지 않으며 [일반 선행 Review와 부분 검증 증거](#일반-선행-review와-부분-검증-증거)의 전체 검증·동일 입력 확인을 유지한다. |
| 문서 변경 선행 경로 | 과거 검증 범위와 최신 조건의 미검증 부분을 구분한다. 구현·테스트 수정은 FAIL, 검증 실행 근거만 부족하면 issue 없는 `VERIFY_REQUIRED`로 반환한다. 두 선행 경로를 혼용하거나 과거 PASS를 최신 검증·게시 승인으로 사용하지 않는다. |

프로그램은 Planner·Worker의 최종 변경을 역할별 게시 경로·문서·결과 계약으로 검증한다. readonly 역할의 접근은 sandbox가 제한하며 외부 파일·Git 변화의 사후 귀속 검사를 수행하지 않는다. 잘못된 Planner 결과는 planning 오류, 잘못된 Worker 결과는 `WORKER_OUTPUT_INVALID`로 처리하며 Git 접근 위반 진단과 구분한다.

### 세션 재사용과 복구

- Worker와 Reviewer는 ExecPlan이 PR 생성 준비를 마칠 때까지 각자의 세션을 재사용한다
- `run_agent_with_retries`는 실패 결과의 세션 부재·재개 불가 문구를 `session_resume_unavailable`로 판별한다. `no rollout found for thread id`와 `No conversation found with session ID`도 포함하며 오류 코드 `-32600`만으로 세션 부재를 판정하지 않는다. 현재 CLI 오류 문구에 의존하므로 공급자 문구가 바뀌면 판별과 회귀 테스트를 함께 갱신한다
- 세션 복구는 현재 세션 ID를 비워 한 번만 새 세션을 시작하고, 같은 worktree와 현재 runtime 지침·작업 문맥·저장된 실패 내용을 전달한다. 기존 `AttemptSequence`는 이 호출을 `fallback`·`session_unavailable`로 기록한다. 승인·격리 정책 실패와 사용 한도 소진은 복구 대상에서 제외하고 새 세션도 실패하면 기존 실패·일시 오류 재시도 경계를 따른다

### 프로그램이 소유하는 실행 효과

- 상태 분류, 설정 검증, Git/GitHub 명령과 verify 실행은 프로그램 코드가 담당한다
- Worker는 checkpoint 요청과 완료 후보를 구분해 반환한다. Git add·commit·fetch·pull·rebase·push·worktree 등록·PR 생성은 Manager가 수행한다. 종료한 Worker의 범위·index·소유권을 확인한 제한 commit과 commit별 게시의 복구 계약은 [Worker checkpoint 게시와 새 환경 인수](#worker-checkpoint-게시와-새-환경-인수)에 둔다. 게시 대기에는 새 Worker를 호출하지 않는다.
- AI Agent와 verify subprocess에는 역할 수행에 필요한 환경 변수만 전달하고 Telegram token을 포함한 Loop Manager 비밀값을 상속하지 않는다. provider 환경 필터는 비밀이 아닌 `PYTHONDONTWRITEBYTECODE`와 `PYTHONPYCACHEPREFIX`도 보존한다. verify 조율 endpoint·secret·session은 runner 자체 조율에 필요해 runner에만 전달한다.

  runner의 자식 subprocess에는 해당 세 값을 뺀 환경 복사본을 넘기며 부모 환경과 호출자 mapping은 변경하지 않는다. 명령행 Python에서 선택한 bytecode 정책과 prefix도 자식 Python에 반영한다. 기존 marker·`PYTHONPATH`와 필요한 일반 환경은 자식에서 유지한다

### Ponytail 지침

Worker에 추가하는 [AGENTS.ponytail.md](AGENTS.ponytail.md)는 [Ponytail](https://github.com/DietrichGebert/ponytail)의 코드 단순화·기존 구현 재사용 지침이다. upstream [552acd5](https://github.com/DietrichGebert/ponytail/blob/552acd5efd0aeae2583a12efe39373d2f076f25e/AGENTS.md)의 원문과 [MIT 라이선스](AGENTS.ponytail.LICENSE)를 보존한다.

적용·버전 경계는 위 원본 묶음 계약을 따른다.

축약 원문은 전체 Skill·Hook과 동일한 효과를 보증하지 않는다. Codex 0.161.0의 [command Hook](https://github.com/openai/codex/blob/rust-v0.161.0/codex-rs/hooks/src/engine/command_runner.rs)은 별도 셸 프로세스로 실행되고 외부 MCP에도 내장 파일 sandbox 적용을 보장할 수 없으므로 기존 Plugin·App 비활성화와 쓰기 경계를 유지한다. 사용자 Plugin 설정·설치 상태는 변경하지 않으며 명시된 요구사항·안전·검증·역할·승인·게시 규칙을 보존한다.

### provider의 현재 지원과 쓰기 정책

현재 관리 Agent 실행의 지원 상태와 채택한 구현 목표를 구분한다. 홈·CLI 옵션·모의 호출이 준비됐다는 사실만으로 실제 격리 호출을 허용하지 않는다.

| provider | 현재 관리 호출 |
|---|---|
| Codex | 지원 CLI·OS와 소유 workspace에서 사전 검증을 통과한 경우 직접 실행한다. 쓰기 역할은 `workspace-write`, Reviewer·읽기 전용 역할은 `read-only`다. |
| Claude | Linux·macOS의 native 호출을 시작 설정 확인 뒤 실행한다. `prepared` 구성 근거와 실제 시작·오류 관측을 구분하며, 세부 지원·거부 조건은 아래 R-023 절을 따른다. |

provider adapter는 실제 호출 직전에 canonical worktree·Git 등록·repository identity를 확인하고 신규 session·재개·retry·fallback마다 역할의 쓰기 정책을 확인한다. 선택 worktree·원본 저장소·Git metadata는 실제 존재와 identity를 확인하되, 등록만 남고 디렉터리가 없는 다른 linked worktree는 존재를 필수 조건으로 삼지 않고 canonical 보호 경로로 유지한다. 다른 worktree의 부재만으로 현재 호출을 막거나 등록을 prune하지 않으며, 필요한 경로 확인 실패에는 원래 오류 종류·경로를 비밀값 제거 후 보존한다. native sandbox probe도 사라진 보호 경로를 단순 생략하지 않고 가장 가까운 기존 상위 경로의 읽기 전용·쓰기 거부를 확인해 새 하위 경로 생성이 불가능함을 검증한다. 상위가 쓰기 가능하거나 확인이 불명확하면 거부하며, probe의 종료 코드·진단은 비밀값을 제거하고 길이를 제한해 격리 오류에 보존한다. 허용 쓰기는 선택 workspace와 소유권을 확인한 최소 임시 경로에 한정한다. 설정 병합으로 source checkout·다른 worktree·shared Git metadata·넓은 임시 경로가 추가되면 격리 확인으로 수락하지 않는다. 역할별 허용 파일·문서·결과 검증은 sandbox와 별도로 유지한다.

- Codex는 R-050에 따라 Manager가 `codex exec`를 직접 호출한다. 선택된 관리 worktree를 `-C`로 지정하고, 쓰기 역할에는 `workspace-write`, Reviewer 및 읽기 전용 역할에는 `read-only`를 적용한다. `-C`는 작업 경로이며 쓰기 제한은 내장 sandbox가 담당한다. 실제 모델 실행을 바깥 `codex sandbox`로 감싸거나 `app-server` transport로 바꾸지 않는다. 별도의 모델 호출 없는 sandbox 사전 검사는 다음 절의 호출 준비에 포함한다.

  `approvals_reviewer="auto_review"`를 유지하면서 `approval_policy.granular`의 `sandbox_approval=false`, `request_permissions=false`로 sandbox 밖 실행과 권한 확대 요청을 거부한다. 필요한 나머지 승인 항목은 검토 대상으로 유지한다. `approval_policy="never"`로 전체 자동 검토를 비활성화하거나, 편의 CLI 옵션이 명시한 granular 정책을 덮어쓰게 하지 않는다.

- Codex의 추가 `writable_roots`는 Manager가 허용한 최소 경로만 포함한다. 기본 `/tmp` 및 `$TMPDIR` 쓰기 범위는 `exclude_slash_tmp`, `exclude_tmpdir_env_var`로 제외하고 필요 시 실행별 임시 경로만 추가한다. `.git` pointer file과 그 resolved Git directory를 포함한 보호 경로 및 shared Git directory는 쓰기를 허용하지 않는다. 상속된 profile·규칙·확장 기능이 경계를 우회할 수 없는지도 확인한다.

CLI·OS별 실행 전제는 [인증과 실행 조건](OPERATIONS.md#인증과-실행-조건)을 따른다. 현재 거부를 해제하거나 다른 provider로 자동 전환하는 권한은 이 표에서 만들지 않는다.

### 준비된 provider 실행 입력과 호출 증거

관리 Codex 호출은 준비된 환경, 현재 정책, 실제 실행과 결과를 각각 확인한다. 등록 사전 조회의 성공이나 과거 호출의 증거로 새 session·재개·제한 retry·fallback의 확인을 대신하지 않는다.

| 경계 | 확인·저장하는 근거 |
|---|---|
| 실행 환경 준비 | `AgentRequest.prepared_launch`가 worktree·repository·role의 공통 홈과 호출별 임시 저장을 provider 조회 전에 선택한다. legacy profile callback도 `prepared_environment`의 같은 필터된 mapping을 사용하며 부모 `os.environ`·호출자 mapping은 바꾸지 않는다. |
| 모델 호출 없는 사전 검사 | 현재 CLI·설정·확장·역할 정책과 별도 `codex sandbox` 안의 파일 도구 경계를 검사한다. version 조회 뒤와 feature 조회 뒤 sandbox probe 전에 설정 정책을 다시 확인하고, sandbox probe 뒤에도 안정된 최종 설정 지문을 계산한다. 조회 사이 설정이 충돌 상태로 바뀌면 다음 Codex 조회 전에 중단한다. 실제 Agent 실행을 감싸는 wrapper나 모델 호출·중첩 도구 시연과 구분한다. |
| 실행 직전 | 같은 환경의 인증·Model/Effort·Fast·native 지침을 확인하고 workspace·설정·CLI와 인증 링크를 재검사한다. native developer의 `additional_tools`는 전송 ID를 제외한 정의를 전후 비교하며 지침 메시지로 해석하지 않는다. 준비 조회가 안전한 설정을 바꿨으면 새 내용을 다시 검사해 최종 격리·실행 지문에 결합한다. 최종 증거를 invocation에 저장한 뒤 process handoff에서도 설정·workspace snapshot을 확인하고 실제 `codex exec`를 시작한다. |
| 종료 | Linux Codex 호출은 subreaper supervisor가 별도 session으로 이탈한 자손도 종료·회수할 때까지 소유한다. process receipt는 supervisor PID에 결합하며, supervisor의 정상 종료 또는 전체 자손의 중단 확인 뒤에만 종료 콜백을 호출한다. 동시 중단 요청은 한 번의 종료 확인을 공유하고, 확인 실패는 supervisor 종료만으로 성공으로 바꾸지 않는다. 다른 플랫폼은 기존 프로세스 그룹 또는 Windows Job의 정리·wait 경계를 유지한다. 시작·writer 종료·결과 성공·검증 성공은 별도 사실로 기록한다. |

`provider_execution.observe`는 모델 실행 없이 현재 Codex Reviewer의 입력을 확인하거나 지정한 실제 invocation의 완료 기록을 읽는다. `execution_identity`의 문자열 필드는 `provider`·`model`·`effort`·`instruction`·`environment`·`policy`이며, `instruction` 지문은 native developer 메시지와 전송 ID를 제외한 `additional_tools` 정의를 함께 포함한다. 도구 정의는 developer instruction에 합성하지 않는다. 지침·환경·정책은 지문으로 저장한다. 실제 호출 identity는 process 시작 전에 한 번 결합하고 현재 설정을 과거 행에 소급하지 않는다. credential 값·그 값의 해시·prompt 원문은 저장하지 않는다.

준비 CLI 조회가 선택 state directory의 공통 `automation-codex/config.toml`을 안전하게 생성·교체하면 새 내용을 재검사해 현재 invocation의 격리·실행 지문에 반영할 수 있다. native 지침 해석은 마지막 성공 조회가 확인한 전체 설정 지문을 호출자에게 전달하며, 증거 갱신은 현재 전체 설정 지문이 그 값과 같을 때만 허용한다. 마지막 조회 뒤 공통 config가 바뀌면 설정이 안전해도 증거를 갱신하지 않고 실행을 거부하며 현재 Reviewer 관측도 identity를 반환하지 않는다. 이후 호출은 현재 설정에서 지침을 다시 해석한다. 이 비교는 기존 invocation의 완료 기록을 수정하지 않는다.

안전한 공통 설정 변경이 지침 native 조회 사이에 발견되면 baseline 지침부터 다시 해석하고 sentinel·최종 override 조회가 같은 설정 identity를 확인한 경우에만 전달 내용을 증명한다. 변경이 반복돼 조회 결과를 한 설정에 결합할 수 없으면 거부한다. Manager `config.ini`, project/system Codex 설정과 그 밖의 준비 입력은 갱신 대상이 아니며 조회 사이 변경되면 다음 Codex 조회 전에 거부한다. version·features·native 지침 조회가 각각 끝난 뒤 정책을 다시 검사해 충돌 파일을 발견하면 후속 조회를 실행하지 않는다. MCP·plugin·hook·profile·권한·exec-policy 충돌, 읽기 실패와 검사 중 불안정한 내용은 거부한다. 저장된 증거 갱신은 process 시작 전의 미완료 invocation에서 공통 home 설정의 안전한 변화에만 허용하며 process handoff에서 전체 설정과 workspace snapshot을 다시 대조한다. 그 밖의 입력 변경은 호출을 시작하지 않고 기존 결과·session·작업 이력을 보존한다.

일회성 임시 경로는 정확히 확인한 소유 디렉터리만 정규화한다. 실제 process 시작·종료·성공·격리·지침과 repository/plan/role/worktree 결합이 없거나 legacy 입력이 누락되면 재사용 증거를 반환하지 않는다. 현재 환경 확인만으로 과거 성공을 합성하지 않는다.

**호출 관측과 writer 종료.** Codex 신규·재개·일회 실행은 같은 출력 pipe를 사용한다. `agent_activity`는 로그 저장·Reviewer 생략 전에 UTF-8 부분 출력과 이벤트를 관측하고 monotonic 무출력 시간·병렬 실행 item·턴 완료를 구분한다. Manager heartbeat는 이 시간을 갱신하지 않는다. 실행 중 item이나 해석 불확실성이 없으면 600초 무출력에서 프로세스 정리를 요청하며, 실행 중 item·불확실성이 있으면 전체 실행 제한을 유지해 기다린다. 턴 완료·process 종료 뒤 결과 수집 무출력도 종료 판단에 포함하고 제한 재시도는 유지한다.

`process_receipt`는 Agent가 쓸 수 있는 attempt 밖의 Manager runtime에 호출별 PID·Linux 호스트/boot identity·종료 근거를 원자 저장하며 invocation은 spawn 전에 receipt 경로를 결합한다. interrupted 정리와 Requirement 교정은 저장된 종료 근거를 복구하고 Linux에서는 같은 호스트의 그룹 소멸·reboot를 재확인할 수 있다. Windows·macOS는 종료 근거가 없으면 미확인 호출을 보존한다.

복구 관측 시각을 실제 종료 시각·소요 시간·결과 코드로 추정하지 않으며 결과 분류·usage·session·성공 재사용 근거를 바꾸지 않는다. Requirement writer gate는 `execution_finished_at`과 실행 전 거부 여부로 판단하며 `result_code`를 종료 근거로 대용하지 않는다.

### 인증 전용 provider 저장소

등록·복구·수동 작업·실행 서비스는 DB·잠금에 실제 사용하는 상태 루트를 provider 경계에 전달한다. 부모 환경과 개인 홈은 보존하고 선택 상태 루트의 공통 홈을 사용한다.

| 저장·실행 영역 | 위치와 책임 |
|---|---|
| Codex 공통 홈 | `CODEX_HOME=<state-dir>/automation-codex`. CLI의 인증·설정·session 저장소로 직접 사용한다. |
| Claude 공통 홈 | `CLAUDE_CONFIG_DIR=<state-dir>/automation-claude`. 준비·조회 환경을 통일하되 홈 선택만으로 관리 Agent 실행 지원을 선언하지 않는다. |
| 기본 인증 파일 | 개인 `~/.codex/auth.json`·`~/.claude/.credentials.json`만 전용 위치에 연결한다. 전용 파일·링크가 이미 있으면 보존하고 개인 설정 전체를 가져오지 않는다. |
| 공통 설정 | Manager가 생성·덮어쓰거나 역할별 값을 저장하지 않는다. CLI의 정상 생성·원자적 교체 뒤 다음 호출은 같은 홈의 현재 설정을 검사한다. |
| 호출별 runtime·임시 경로 | repository·worktree·role에 결합해 임시 파일·로그·결과·호출 증거를 관리한다. 역할별 홈으로 공통 config·auth를 복제·연결하지 않는다. |

`provider_home`은 비공개 경로 검증을 적용하고 전용 인증 위치가 비어 있을 때 기본 파일만 링크한다. 기존 파일·링크를 덮어쓰거나 링크 실패를 복사·개인 홈 fallback으로 우회하지 않는다. 호출 전후 링크 경로를 확인하되 인증 내용은 지문·기록에 넣지 않는다. Manager가 로그인·로그아웃이나 인증 원본 덮어쓰기를 수행하지 않으며 링크를 통한 CLI 인증 갱신은 원본에도 반영될 수 있다. 설정 방법·오류와 OS 인증 저장소의 차이는 [전용 공급자 홈과 인증](OPERATIONS.md#전용-공급자-홈과-인증)을 따른다.

설정 지문·직전 재검사는 실제 소비하는 설정 source를 대상으로 한다. Codex의 공통 홈·프로젝트·관리 설정 확인은 유지하고 Claude는 아래 R-023 계약에 따라 선택 공통 홈의 user 설정과 project·local 및 managed 설정을 소비하며 개인 홈의 설정을 연결하지 않는다. 읽기 실패·소유권 불명·검사 뒤 변경·권한·확장 충돌과 확인하지 못한 인증 방식은 해당 호출에서 거부한다.

Model·Effort·Fast·instruction·sandbox·승인 정책은 명시적 호출 입력으로 제공한다. Agent 도구의 쓰기 허용은 역할별 선택 worktree·해당 호출의 최소 소유 임시 경로에 한정하고 공통 홈·개인 홈·다른 호출의 임시 경로를 추가하지 않는다. Reviewer의 읽기 전용과 기존 ACL·Job Object·sandbox·자동 승인 검토를 유지하며 CLI 자체의 상태 저장과 도구 쓰기 권한을 구분한다.

전용 환경값은 [Codex의 CODEX_HOME](https://learn.chatgpt.com/docs/config-file/environment-variables)·[Claude의 CLAUDE_CONFIG_DIR](https://code.claude.com/docs/en/env-vars)에 대응한다. 실행 환경 준비·조회·실제 호출은 위의 동일 환경 계약을 따른다.

#### R-085: 공급자별 공통 홈과 검증 경계

[R-085](<requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md>)의 기존 홈 호환 규칙을 유지한다. 이전 역할별 홈의 session·설정·인증·작업물·로그와 DB 이력은 삭제·이동·전체 복제·초기화하지 않으며 잔여 config를 새 입력으로 검사·복원하지 않는다.

session은 Manager에 저장된 ID와 repository·worktree·role·작업 결합으로 선택하며 공통 홈의 최근 session으로 대체하지 않는다. 재개 불가는 기존 실패·작업 문맥을 보존하는 새 session fallback으로 연결하고 재개 성공과 구분한다.

호출별 임시 경로·출력·실행값·권한 분리는 기존 fixture·모의 호출의 확인 범위다. 실제 session 재개·동시 모델 실행·Claude CLI 호환성·실계정 인증과 갱신·credential store·다른 OS 실환경의 완료 근거로 확대하지 않는다. 이 미수행 범위는 R-085의 Non-Goals에 남긴 후속 확인이며 공통 홈 전환의 선행·완료 조건으로 추가하거나 기존 미지원 호출을 허용하는 근거로 사용하지 않는다.

### R-023: workspace 쓰기 제한과 자동 승인 검토

Claude adapter는 [R-023](requirements/%5BR-023%5D%20Self-Hosted%20Runtime%20Refresh%20Handoff.md#requirement)의 일반 native 호출을 문서 기반 구성으로 실행하며 OS 쓰기 probe 성공을 합성하지 않는다. `agent_claude_policy`가 역할별 요청 정책을 만들고 `agent_claude_preflight`가 시작 source·지원 조건·소유 경로를 확인한다. `provider_adapters`는 준비·실행·중단을 기존 invocation·session·receipt·targeted 수집에 연결한다. Codex의 직접 exec·native probe·검증 계약과 Manager의 Git 쓰기 책임은 유지한다.

| 책임 경계 | 현재 계약 |
|---|---|
| 파일 도구 | 모든 역할에서 `Edit` 전체 deny와 `--disallowedTools Edit,Write,NotebookEdit`를 적용하고 파일 변경 도구를 노출하지 않는다. 일반 외부 쓰기도 승인할 수 있는 auto classifier에 파일 쓰기 경계를 맡기지 않는다. 파일 변경은 아래 native sandbox의 Bash로 수행한다. `permissions.blockReadsOutsideWorkingDirectories=true`는 읽기 제한이며 쓰기 경계의 대체 근거가 아니다. 추가 directory·allow 규칙 상속을 거부하고 선택 workspace·호출별 소유 임시 경로만 working directories로 사용한다. source·다른 worktree·Git directory·`.git` pointer의 정확 경로와 하위 `Edit(//절대경로)` deny도 보존한다. |
| Bash·하위 프로세스 | native sandbox의 enabled·failIfUnavailable을 켜고 unsandboxed retry·명령 제외·자동 일괄 승인·약한 namespace·Unix socket 확대를 끈다. 같은 보호 경로와 Reviewer workspace를 `/절대경로` denyWrite로 명시한다. 기본 shared Git 허용을 신뢰하지 않으며, 기본 per-user 임시 경로도 거부하고 호출 기록용 attempt와 별도의 짧은 Bash 소유 임시 경로만 추가한다. |
| 자동 승인 검토·확장 | `--permission-mode auto`, `autoMode.classifyAllShell=true`, `--permission-prompts none`을 사용한다. 승인 mode를 acceptEdits·dontAsk·bypass로 대체하지 않는다. native tool 목록을 명시하고 MCP를 strict 빈 구성·tool deny로 제한한다. source에 있는 hook·plugin·credential helper 등 sandbox 밖 명령 경계를 확인하지 못하는 확장과 쓰기·자동 검토 충돌은 시작 전에 거부한다. |
| 시작 source | `--setting-sources user,project,local`로 공통 홈의 `settings.json`, 시작 workspace의 `.claude/settings.json`·legacy `.claude/settings.local.json`과 native Git root의 local 설정을 읽는다. linked worktree의 root local은 기본 checkout에서 읽되 native의 홈 root·다른 소유자 root/Git/설정 directory 예외에서는 cwd local만 검사한다. 별도 Git directory 등 root를 확정할 수 없는 구성은 조회 전에 거부한다. 개인 홈의 settings·`.claude.json`을 공통 홈으로 연결하거나 복제하지 않는다. user→project→local→managed 순위와 배열 병합을 검사하며 각 소비 source의 확장·권한 충돌을 별도로 거부한다. remote cache·macOS Managed Preferences plist·system managed JSON 및 이름순 drop-in의 우선순위·배열 병합을 해석한다. 정책 key가 없는 상위 control-only source도 병합 방식 결정에 사용한다. first-wins에서 가려진 scalar와 모든 admin source에서 읽는 env·filesystem 비활성화·native binary override를 구분한다. 빈 배열로 상속 배열을 지웠다고 판단하지 않는다. |
| 실행·중단 | CLI·인증·doctor를 읽기 전용으로 조회하고 지침·profile 준비 뒤 CLI·설정·canonical repository를 재검사한다. spawn 직전에는 DB/receipt 결합 사이에 바뀐 설정도 재확인한다. native JSONL의 시작 mode·모델·CLI 버전·cwd·도구·확장·재개 session을 대조하고 실제 mode 이탈·권한/인증/보안 오류를 관측하면 프로세스 그룹을 중단한다. 정상적인 unit 명령 실패는 Worker가 교정할 수 있게 둔다. |

지원 기준은 Linux·macOS와 CLI v2.1.307 이상이다. native sandbox와 nonempty objects/refs 정리 보정을 포함한다. `--restricted`는 user/project/local을 모두 무시하므로 공통 홈 설정 소비 계약에 사용하지 않는다. native Windows는 sandbox 미지원으로 거부하고 WSL은 현재 Windows host 정책 상속을 확인하지 못해 해당 시작 확인에서 거부한다. source 조회·형식·초기화·CLI/모델/계정 지원 오류, 유효 경계가 넓어지는 설정과 해석하지 못한 보안 설정도 해당 호출에서 진단한다. 이 조건을 새 OS·외부 격리·계정 도입이나 독립 Codex 작업 차단으로 우회하지 않는다.

인증·model/effort·지침 help 조회도 같은 source 선택과 필터된 공통 홈 환경에서 실행한다. 조회에는 빈 tool 목록·전체 tool deny·빈 strict MCP·hook 비활성화만 전달하고 모델 turn이나 역할별 쓰기 권한을 주지 않는다. model/effort는 `--bare` 대신 이 구성의 `--init-only`로 조회하며, 각 조회 전후의 source 지문을 대조한다. 관리 호출은 인증·doctor 이전 지문을 보존해 조회 중 변경을 거부하고 profile·지침 준비 뒤 및 spawn 직전에 다시 검사한다. 지문에는 소비 경로·resolved 경로·파일 bytes의 SHA-256 또는 부재와 managed drop-in 목록을 담으며 인증 비밀은 담지 않는다. 다음 호출의 정상 생성·수정·원자 교체·삭제·재생성은 새 현재 입력으로 검사한다. 미소비 개인 설정 변화는 이 지문에 들어가지 않는다.

source는 최대 2 MiB의 일반 파일 JSON object와 macOS plist dictionary로 읽는다. 공통 홈·project·local 설정은 실행 UID 소유이고 해당 root 안에 resolve되는 파일만 읽는다. 개인 source로 향하는 링크·읽기 오류·알 수 없는 설정 key를 보존·거부하며 부재로 치환하지 않는다. macOS는 `/Library/Managed Preferences/com.anthropic.claudecode.plist`와 그 OS 사용자별 하위 경로만 읽고 개인 defaults domain을 MDM으로 대체하지 않는다. 두 protected source가 서로 다른 비어 있지 않은 정책을 제공하면 우선순위를 추정하지 않고 거부한다. 해석하지 못하는 managed root key도 native schema에서 무시되는지에 따라 source 순위가 달라질 수 있어 거부한다. permissions·sandbox 및 그 filesystem/network는 object, 권한·경로 목록은 문자열 배열이어야 하며 필수 Boolean은 숫자로 대체할 수 없다. managed JSON drop-in은 숨김·비JSON 파일을 제외하고 이름순으로 병합한다. remote cache의 미지원 wrapper·끊어진 링크·비일반 파일·읽기 실패를 부재나 빈 정책으로 바꾸지 않는다. 경로는 canonical 절대경로로 확인하고 보호 경로 중첩·symlink alias·플랫폼별 의미가 달라질 수 있는 glob root는 거부한다. Claude 인증 조회가 보고한 configDirectory는 선택된 공통 홈과 같아야 한다. 개인 source 원문·인증값은 증거에 저장하지 않는다.

호출별 자료는 기존 `agent_isolation_evidence`에 `prepared`로 결합한다. provider·CLI·환경·role·canonical workspace·repository와 시작 source/configuration digest·요청 정책 digest·요청 session·invocation ID를 연결한다. 기존 snapshot_digest 칸에는 호출 문맥의 지문을 담으며 Git 상태 전후 비교나 접근 권한 증명으로 사용하지 않는다. reason의 JSON은 `basis=documented-native-configuration`, `runtime_enforcement=not_observed`, 요청 정책·병합 설정 지문(`effective_settings_digest`)과 source 경로/내용 지문·인증 method/home을 보존한다. 실제 init·오류는 호출별 JSONL과 invocation 결과/session에 남기고, 실패·중단 시 prepared를 invalidated로 바꾸면서 원래 구성 근거를 유지한다. 저장 실패는 모델 실행을 시작하지 않으며 이전 receipt가 있는 launch를 다음 호출의 준비 근거로 재사용하지 않는다. Claude의 prepared 자료를 Codex verified scope나 선행 Review의 native 정책 검증 재사용 근거로 채택하지 않는다.

Bash 임시 경로는 canonical `/tmp` 아래 `mkdtemp`로 만든 호출별 비공개 `lm-*` 디렉터리다. `CLAUDE_CODE_TMPDIR`·TMPDIR·TMP·TEMP에 이 짧은 root를 전달해 긴 state/hash/attempt 경로에서 발생하는 native 시스템 fallback을 피한다. CLI가 추가하는 `claude-{uid}`도 이 소유 root 안에 남으며 공유 기본 경로의 deny는 유지한다. 이 root는 준비 자료에 결합하고 실행 전 소유권·권한·존재를 다시 확인한다. 호출 기록·targeted 결과·receipt는 기존 state runtime에 유지한다. [공식 임시 경로 계약](https://code.claude.com/docs/en/env-vars)의 변환은 모의 검증하며 native OS 실측으로 표시하지 않는다.

Agent 프로세스는 호출별 새 process group으로 직접 실행하며 종료 확인 뒤 attempt 밖 receipt를 기록한다. 종료를 확인하지 못하면 finished receipt를 만들지 않는다. Worker의 owned attempt에 전달한 targeted runner·기록 경로는 기존 `on_launch_prepared`로 호출에 결합하고, 실제 종료 근거 뒤 같은 수집 경로로 처리한다. Worker가 Bash에서 실행할 targeted runner와 초기 import를 위해 설치된 `loop_manager_internal` 패키지 root만 CLI settings의 `sandbox.filesystem.allowRead`로 연다. 같은 root는 `denyWrite`에 추가하고 working directories·allowWrite에는 넣지 않는다. 따라서 `/home` 아래 설치도 읽기 block에서 최소 Python 패키지만 재개방하며 쓰기 권한을 주지 않는다. 프로젝트 import는 workspace, 결과 기록은 owned attempt의 기존 경계를 사용한다. 다른 역할에는 이 읽기 예외를 부여하지 않는다. 빈 기록에서 성공·실패를 합성하지 않는다. Claude 결과 원문은 기존 원자적 저장 helper로 `<attempt>/last-message.txt`에 보존하며 빈 결과도 파일로 남긴다. 공용 최신 결과 파일은 보조 출력이며 DB 저장 대기 후 복구는 receipt와 호출별 원문의 지문을 대조한다. 신규·재개·session-unavailable fallback은 매번 같은 역할 정책과 새 소유 임시 경로를 준비하며 기존 session·작업물·실패 감사와 사용량/timeout 처리를 보존한다.

파일 변경 도구의 명시적 deny는 [auto 판단 순서](https://code.claude.com/docs/en/permission-modes#how-auto-mode-evaluates-actions)에 따라 classifier보다 먼저 적용한다. Bash 이외 도구에는 [Bash sandbox가 적용되지 않으므로](https://code.claude.com/docs/en/sandboxing#what-runs-outside-the-sandbox) 직접 파일 변경 도구를 사용하지 않는다. runner 읽기 예외는 [읽기 block의 Bash 적용](https://code.claude.com/docs/en/settings-reference#sandboxed-commands-under-the-block)과 [allowRead](https://code.claude.com/docs/en/settings-reference#sandbox-filesystem-allowread) 계약에 근거한다.

공식 근거는 [CLI 옵션](https://code.claude.com/docs/en/cli-reference), [파일 권한](https://code.claude.com/docs/en/permissions#read-and-edit), [sandbox](https://code.claude.com/docs/en/sandboxing), [managed source](https://code.claude.com/docs/en/managed-settings)·[Apple Managed Preferences 경로](https://www.apple.com/education/docs/Apple-ClientManagementWhitePaper.pdf), [permission mode](https://code.claude.com/docs/en/permission-modes)와 [실행 중 mode 관측](https://code.claude.com/docs/en/agent-sdk/permissions#auto-mode)이다. 역할별 정책·신규/재개/fallback·충돌/실패·저장·중단 경계는 모의 단위 검증 범위이며 실제 Claude CLI·계정·OS 정책 적용을 검증했다고 확대하지 않는다. 실환경·managed 갱신 대응은 [후속 확인](FUTURE_IDEAS.md#claude-실환경-후속-점검)에 두며 현재 완료 조건으로 올리지 않는다. Reviewer의 읽기 전용 조사와 프롬프트의 테스트 재실행 금지는 별도로 유지한다.

## Model과 Effort

최초 Worker level은 complexity의 low=1, medium=3, high=5에 critical risk만 1을 가산해 1~6으로 계산한다. low·medium·high risk는 가산하지 않는다.

[R-052](requirements/archive/%5BR-052%5D%20Worker%20%ED%8C%90%EC%A0%95%20%EA%B8%B0%EC%A4%80%20%EA%B5%AC%EC%B2%B4%ED%99%94%EC%99%80%20%EC%B5%9C%EC%B4%88%20%EB%B0%B0%EC%A0%95%20%EC%99%84%ED%99%94.md)의 12개 조합은 다음과 같다.

| complexity | risk | 최초 Worker level |
|---|---|---:|
| low | low / medium / high / critical | 1 / 1 / 1 / 2 |
| medium | low / medium / high / critical | 3 / 3 / 3 / 4 |
| high | low / medium / high / critical | 5 / 5 / 5 / 6 |

level 7·8은 완료한 Worker 교정·현재 후보의 Manager 검증(기본 경로의 전체 verify 또는 일반 선행 경로의 부분 verify) 통과 뒤의 유효 Review FAIL에 따른 승격으로만 도달하며 상한은 8이다. 각 effective level의 첫 FAIL을 기준선으로 삼고 기준선의 같은 ISSUE_ID가 두 교정 결과에 다시 나타나거나 교정 결과가 세 번 연속 FAIL이면 다음 Worker 호출부터 한 단계 올린다. 집계 제외·재시작·PASS 조건은 [PRODUCT의 실패와 재시도](PRODUCT.md#실패와-재시도)를 따른다.

실제 Model·Effort의 원본은 `config/config.ini`다. profile 값은 공백을 정리한 `Model alias,Effort` 두 항목이며 참조 alias의 실제 Model 이름은 `[<provider>.models]`에서 해석한다.

| 대상 | 설정 위치·필수 값 | 누락·상속 경계 |
|---|---|---|
| Worker | `[<provider>.worker]`의 `level1`~`level8` | 각 profile과 참조 alias를 해당 section에 명시한다. `DEFAULT`·다른 level·역할로 보충하지 않는다. |
| 일반·점검 Planner | `[<provider>.roles]`의 `planner`·`planner_check` | 각 profile과 참조 alias를 해당 section에 명시한다. `DEFAULT`·다른 역할로 보충하지 않는다. |
| 유지보수 Planner | `[<provider>.roles]`의 `maintenance_planner` | 기존 INI 상속을 유지하며 항목 자체가 없을 때만 명시된 일반 `planner`를 사용한다. 빈 값·잘못된 값은 오류다. |
| Reviewer | `[<provider>.roles]`의 `reviewer` | 기존 `DEFAULT` 상속을 유지하되 최종 profile·alias가 없으면 오류다. 코드·CLI의 Model 기본값으로 대신하지 않는다. |

선택 provider의 필요한 section은 존재해야 한다. 필수 profile·alias 누락, 빈 Model·Effort, 잘못된 형식·INI 보간과 CLI 미지원 조합은 실행 전 설정 오류로 거부하며 미선택 provider 설정은 요구하지 않는다. 서로 다른 level에 같은 지원 profile을 지정할 수 있고 level 8에도 특정 모델 계열·alias·Effort를 강제하지 않는다. `config/config.ini.example`은 운영 설정의 예시이며 누락 fallback이 아니다.

저장된 최초 level은 현재 선언과 산식으로 재계산하되 실행 이력이 있는 계획의 effective level을 낮추지 않고 session·branch·worktree·PR 연결, 작업물과 승격·실패 감사를 보존한다. 산식 변경만으로 BLOCKED를 해제하지 않는다. 새 ExecPlan과 차단 해제 후 일반 재배정은 현재 선언의 최초 level을 적용하고 진행 중 승격 집계를 초기화한다. 원격 입력 반영·과거 오탐 해소로 기존 실행의 원인만 해제하는 복구는 [계획별 해제와 원격 입력 수용](#계획별-해제와-원격-입력-수용)의 session·실패·승격 이력 보존 경계를 따른다.

### Worker 세션 결합과 전환

세션은 alias 이름이 아니라 실제 provider·Model·Effort에 결합한다. 전환은 작업물·검증·실패·승격 이력을 초기화하지 않는다.

| 현재 세션과 설정 | 선택 결과 |
|---|---|
| 세션 없음 | `start`로 시작한다. |
| provider·실제 Model·Effort가 같음 | 같은 세션을 `resume`한다. |
| provider·실제 Model이 같고 Effort만 변경 | 변경한 Effort로 같은 세션을 `effort_resume`한다. |
| provider·실제 Model 변경 또는 기존 세션의 binding 부재 | `replacement`로 새 세션을 준비하고 이전 세션과 전환 이유를 보존한다. |
| 미완료 pending 전환 존재 | 이후 설정 변경보다 저장된 전환의 kind·target을 우선해 같은 전환을 마친다. |

저장 열의 `NULL`은 binding·전환 부재를 뜻한다. 값이 있으면 JSON 문자열 안의 객체로 다음 필수 key와 타입을 검증한다. 세션 선택 결과의 `start`·`resume`·`effort_resume`는 저장 pending 전환의 kind와 구분한다.

| 저장 객체·필드 | 형식 |
|---|---|
| `SessionBinding` | `provider`·`model`·`effort`가 모두 공백뿐이지 않은 문자열인 객체 |
| `SessionTransition.identity`·`cause` | 공백뿐이지 않은 문자열 |
| `SessionTransition.kind` | `replacement` 또는 세션 재개 불가의 `fallback` |
| `SessionTransition.previous_session` | 공백뿐이지 않은 문자열 또는 JSON `null`. key는 필수다. |
| `SessionTransition.previous_binding` | 유효한 `SessionBinding` 객체 또는 JSON `null`. key는 필수다. |
| `SessionTransition.target` | 유효한 `SessionBinding` 객체 |

코덱·선택은 `execution/worker_session.py`가 소유한다. session 재개 불가의 한 번 새 세션 fallback은 [세션 재사용과 복구](#세션-재사용과-복구)를 따르며 손상 JSON을 fallback으로 숨기지 않는다. 손상값의 원문·phase·작업물·실패·승격 감사는 아래 중단 복구 경계에서 보존한다.

## 상태 저장과 중단 복구

상태의 의미·허용 전이는 [PRODUCT.md](PRODUCT.md#states-and-transitions)를 따른다. 프로그램만 상태를 변경하고 다음 phase와 소유 worktree를 작업 시작 전에 한 transaction으로 저장한다. 중단 뒤 저장된 단계와 실제 Git 상태를 대조해 같은 작업을 재개한다.

SQLite 안의 phase·소유 정보 확정과 Git·journal 파일의 실행 효과는 하나의 원자 transaction이 아니다. 저장 intent·현재 Git 결과·SQLite generation을 대조해 완료한 효과를 반복하지 않고 미완료 적용만 이어간다.

| 중단·손상 상황 | 확인할 원본과 재개·보존 결과 |
|---|---|
| worktree 경로 부재 | 소유 branch와 현재 등록·소유권을 확인할 수 있으면 재등록한다. branch도 복구할 수 없거나 소유권을 확정할 수 없으면 작업물을 보존해 해당 scope로 차단한다. |
| session binding·pending JSON 손상 | 판정 전에 객체·필수 필드·타입·kind·중첩 binding을 검증한다. `STATUS_FAILED`로 진단하고 원문·phase·작업물·실패·승격 감사를 보존한다. 손상 pending을 건너뛰어 기존 세션을 실행하거나 성공·차단 해제를 만들지 않는다. |
| Git 완료 후 SQLite 미적용 | 저장 intent와 실제 결과·현재 generation을 다시 확인해 결과만 멱등 적용한다. 이미 끝난 Git 단계를 반복하거나 과거 증거·실패·알림 감사를 삭제하지 않는다. |
| scope를 확정할 수 없는 legacy BLOCKED | 원인·plan identity·판단 근거·해제 지문·상태를 보존하며 `PROJECT_BLOCKED`로 보수적으로 이관한다. |
| 수동 준비·제출의 게시 중단 | 아래의 소유 intent·후보·원격 OID·후속 계보로 게시 사실을 재확인한다. 완료 receipt는 보존하며 제거된 후보를 새 lease·대체 후보로 다시 게시하지 않는다. |


작업 범위의 복구 관측 저장과 지속 실패 투영은 첫 SQL 쓰기 전에 공통 취소/최종 삭제 ID를 확인한다. 기존 transaction이 없으면 BEGIN IMMEDIATE로 writer를 예약한 뒤 ID를 다시 읽어, 조회와 예약 사이에 확정된 삭제도 거부한다. commit은 기존 호출자에게 맡기며, 직접 예약한 writer의 거부만 rollback하고 호출자 transaction의 변경은 건드리지 않는다. observe는 기존 RecoveryStateConflict로 상태 변경을 알리고 지속 실패의 늦은 투영은 저장 없이 반환해 일반 취소의 기존 감사·episode·오류를 보존한다. `plan:<G ID>` 과거 실패 subject도 같은 작업 ID로 대조한다. 프로젝트 범위와 다른 repository의 같은 ID는 이 작업별 거부 대상이 아니며, 삭제를 복구/실패 해소 성공으로 합성하지 않는다.

복구 관측은 안정적인 원인 code와 repository·plan ID·phase·plan/base revision·진단 fingerprint·진전 marker를 기록한다. 복구 disposition은 `transient_retry`·`state_reconcile`·`agent_correction`·`condition_wait`·`user_action_required`이며 차단 scope와 별도로 판단한다. 원인별 같은 실행의 시도 한도에 도달하면 작업을 보존해 일시 중단하고, 최종 사용자 조치 판정에만 원인 code로 scope를 결합한다. 최종 차단 transaction은 판정의 repository·plan·원인 일치를 검증하며 복구 횟수·마지막 안정 revision·남은 불확실성·실제 조치 대상을 보존한다. 일시 중단은 차단용 게시·알림을 만들지 않고, 최종 차단의 게시·알림은 [차단 작업의 원격 반영과 보존](#차단-작업의-원격-반영과-보존)을 따른다.

rebase 교정 묶음의 소진은 `recovery_records`에 `rebase_conflict/condition_wait/pause`로 저장한다. 대기 fingerprint는 현재 구현 조건·소유 target/원래 HEAD·충돌 stages의 지문, 실행 base·PR URL/head·충돌 복구 head/base OID와 checkpoint·소유 rebase service·원본 byte reader의 가용성에 결합하며, 별도 progress marker는 파일 내용·staged diff·operation 위치·autostash를 구별한다. Worker 진입은 저장된 결과를 먼저 소비하고 진행 중 turn이 없는 동일 대기에서는 새 turn·Agent 호출을 만들지 않는다. session·재시작·시간 경과만으로 지문을 바꾸지 않으며, 실제 입력·파일 진전·복구 자원이 달라지면 기존 소유권·실행 gate에서 다시 판단한다. 파일별 복원 결과의 `paths`에서 `skipped_user_changes`를 뺀 실제 적용 범위가 있어야 재작업으로 전환한다.

PR 복구 선택은 `selected_execution` 안에서 대상 계획의 원본만 수정한다. 해당 계획의 실제 conflict block이 저장됐고 프로젝트 안전 차단이 없으면 실패 반환을 프로젝트 중단으로 전파하지 않으며, 선택 context 밖에서 차단 계획을 hydrate해 독립 작업의 대표 투영을 덮지 않는다. 로컬 사전조건·marker 또는 journal 경로가 미확정이면 `git_input_uncertain/condition_wait/pause`와 원래 진단만 저장한다. 기록 transaction은 관측한 branch/worktree·PR URL/head·충돌 복구 OID가 현재 계획과 같은지 검사하며, 원인 operation과 checkpoint의 operation·원래 HEAD·target을 지문에 결합한다. 대상이 바뀌면 `remote_snapshot_race`로 거부하고 오래된 진단을 새 대상에 덮지 않는다.

충돌 PR 선택은 적격 후보를 우선순위·계획 ID 순으로 순회하고 복구 하나를 시작하면 끝낸다. 선택 호출이 실제로 새 대기 generation을 기록했을 때만 로컬 입력 대기 반환을 소비하고 다음 후보로 넘어간다. 실제 계획별 충돌 차단도 프로젝트 안전 차단이 없으면 다음 후보를 확인한다. 과거 대기 row만으로 새 오류를 숨기지 않으며 프로젝트 안전 차단·그 밖의 오류는 즉시 상위 결과로 전달한다. 일반 실행 재개는 이미 선택에서 처리한 로컬 PR 대기와 같은 차단 PR을 다시 부착하지 않는다. 대기 PR 관측은 PR이 연결된 충돌 차단 계획을 포함하되 OPEN에서는 원인별 재평가에 맡기고 Merge·Close만 기존 원격 전이 소유 경계로 전달한다. 이 반환 소비는 rebase 완료·예약 회수·검증 성공을 만들지 않는다. 현재 operation의 소유권 근거가 일치하지 않으면 `workspace_ownership_uncertain`의 별도 안전 경계를 유지한다.

충돌 최종 판정의 입력 계약은 `RecoveryObservation.conflict_evidence`와 기존 `unavailable_recovery_routes`다. 원인 코드·횟수·차단 scope만으로는 `managed_conflict_unrecoverable`을 최종 차단하지 않으며, 다른 원인에서 conflict block으로 override하거나 충돌 판정을 프로젝트 차단으로 넓히는 것도 거부한다. 저장 경계와 실제 차단 transaction은 같은 계획·현재 PR·소유 checkpoint를 다시 대조한다.

| 근거 필드 | 필수 형식과 결합 |
|---|---|
| `version` | bool을 제외한 정수 `1` |
| `repository`, `base_branch`, `plan_revision`, `pr_url`, `branch`, `worktree`, `worker_session` | 비어 있지 않은 문자열. 등록 origin/base와 계획의 개정·PR·작업 공간·Worker 세션에 정확히 일치 |
| `head_oid`, `base_oid`, `checkpoint_head` | 소문자 16진수 40자리 또는 64자리 전체 Git OID. 저장 PR/head·충돌 복구 OID와 checkpoint의 관측 원격·target·원래 HEAD에 일치 |
| `operation`, `checkpoint_generation` | `rebase`와 bool을 제외한 양의 정수. 현재 checkpoint operation/generation에 일치 |
| `conflict_paths` | 하나 이상의 안전한 저장소 상대 경로 문자열. 기존 rebase 경로 검사를 재사용 |
| `worktree_snapshot` | `GitSnapshot`으로 역직렬화 가능한 문자열. 실제 차단 직전 관측의 HEAD·branch·index·dirty·operation과 일치 |
| `attempted_routes` | 정확히 `worker_correction`, `file_restore_rework`, `revalidation`의 결과·적용 불가 사유를 비어 있지 않은 문자열로 기록. 재검증 성공을 합성하지 않음 |
| `release_conditions` | 하나 이상의 비어 있지 않은 재평가 조건 문자열 |
| `unavailable_recovery_routes` | 기존 `reobservation`, `retry`, `correction`, `condition_wait`, `next_run`, `safe_recovery` 여섯 경로 각각의 비어 있지 않은 불가 사유 |

소유 PR rebase의 Worker는 `pr_conflict_unrecoverable` 관측으로 문제·충돌 경로·세 가지 시도 결과·여섯 자동 경로의 불가 사유·재평가 조건을 보고할 수 있다. 각 설명은 2,000자, 경로는 128개, 재평가 조건은 8개 이내이며 기존 전체 JSON 크기 제한과 안전한 상대 경로 검사를 적용한다. 이 결과에는 완료 게시 필드를 허용하지 않으며 저장된 완료 경계에서도 stage 전에 거부한다. Manager는 현재 실행 조건·소유권과 실제 unmerged 경로를 대조하고 파일별 복원을 먼저 시도한다. 실제 적용 경로가 있으면 재작업하며, 복원 checkpoint가 staged이고 모든 충돌 경로가 사용자 변경 보존으로 제외된 경우에만 의미 관측과 현재 PR·checkpoint를 결합한다. 복원 미적용·자원 부재·입력 변경·잠금은 최종 근거를 만들지 않는다.

최종 생산자는 현재 원격 PR을 두 번 관측하고 fetched base·소유 checkpoint·충돌 지문과 파일 진전을 대조한다. 검증을 실행할 수 없는 unmerged 상태는 재검증 성공 대신 적용 불가로 기록한다. Git 관측과 차단용 snapshot 수집은 SQLite 쓰기 transaction 밖에서 수행한다. 실제 차단 transaction에서 원본·checkpoint·선택 snapshot 필드를 재검사하고 근거 저장과 계획 차단을 함께 반영한다. 실패하면 둘 다 rollback하며 이미 열린 호출자 transaction에서는 최종 판정을 보류한다. managed conflict는 근거의 checkpoint generation을 유지하고 metadata 입력 차단의 별도 checkpoint 영수증을 추가하지 않는다.

완전한 근거만 기존 `final_evidence` JSON에 보존하며 DB schema를 추가하지 않는다. outbox의 현재성 검사는 이 근거를 현재 계획·PR·checkpoint와 대조하고, 충돌 사건에는 그 근거를 action identity에도 결합한다. 원격 head/base 변경이나 mergeable 관측은 관련 recovery record와 해당 block의 보존 snapshot만 대기로 보류한다. 원래 최종 근거·차단·checkpoint·이미 전송한 조각과 독립 원인의 recovery record는 유지한다. 기존 조각별 재검사·취소가 미전송분을 중단하며 원격 head를 소유 근거 없이 채택하거나 차단 해제를 합성하지 않는다. 이 계약은 복구 소유자가 공급한 근거의 검증 경계이며, 진단 문구나 실패 횟수에서 불가 사유를 생성하지 않는다.

현재 `managed_conflict_unrecoverable/final_block`의 자동 재평가는 정규화·Git 쓰기 전에 근거의 PR/checkpoint 현재성, 관련 문서 입력 fingerprint와 저장한 작업물 snapshot의 HEAD·branch·index·dirty·operation을 비교한다. 모두 같으면 새 교정으로 넘기지 않으며 독립 ref 변경은 이 비교에 포함하지 않는다. 실제 입력 변화 또는 기존 원격 관측에 의한 평가 보류는 현재 소유 rebase 검사로 이어진다. 명시적 로컬 복구 요청은 `conflict` 원인으로 기록하며 `metadata`·`verify` 소비와 구분한다. 접수 snapshot의 차단 원인·내용·해제 지문·checkpoint generation을 대조하고 소유권·원격·실행 자리·dependency를 재확인한 뒤 요청 소비와 해당 차단 해제를 같은 transaction에서 반영한다. 중단·경합은 둘 다 보존하고 성공한 요청을 새 차단에 재사용하지 않는다. 재개는 기존 artifact 감사와 진행 operation을 보존하며 검증 성공이나 rebase 완료를 합성하지 않는다.

일반적인 PLAN_BLOCKED는 계획·원래 활성 sources의 내용 해시를 기준으로 재평가한다. 닫힌 PR은 같은 PR reopen을 요구한다. 과거 파일 승인 대기는 일반 작업·소유권 검사를 통과하면 기록을 보존한 채 재개한다. 독립 차단·planning 오류·실패 감사는 함께 지우지 않는다. planning_error는 실제 교정·재검증·게시 복구로 원인이 해소된 뒤에만 지운다. cleanup_pending과 ExecPlan archive pending은 상태 전이를 만들지 않는다.

차단 재평가의 자리는 대상 계획의 호출 종료·branch/worktree 경합·관리 상한과 planning·유지보수 소유권으로 판단한다. 다른 계획의 실행 단계나 대표 투영만으로 독립 복구를 보류하지 않는다. 사용자 질문의 재평가 중 원격·계획·checkpoint가 달라지면 `remote_snapshot_race`로 남겨 같은 원인의 재검사 입력을 보존하며, 최종 거부나 처리 완료로 소비하지 않는다.

**수동 준비·게시 복구.**

수동 명령의 준비·수락은 `application/manual_work.py`와 `manual_concurrency.py`가 소유하며 [PRODUCT의 명령별 역할·결과](PRODUCT.md#수동-명령의-준비와-수락)를 따른다. `start`의 진입 상태는 `READY`·`PLAN_BLOCKED`·기존 `MANUAL_WORKING`이고, 차단 인계는 `verify_retry_exhausted`·`review_retry_exhausted`·`review_protocol_exhausted`·`worker_output_invalid`로 한정한다. draft·catalog hold·프로젝트 차단·활성 유지보수와 계획·자원 소유권을 별도로 검사하며 기존 수동 준비 재호출은 저장된 작업을 재사용한다.

공유 수동 PR의 Merge 확정은 `manual_work_publications`의 PR URL·게시 HEAD·request identity를 현재 묶음과 대조한다. 실제 PR의 URL·head/base branch·HEAD·Merge 관측과 전체 구성 G의 branch·PR·HEAD 및 verify·Review 게시 근거가 일치해야 하며, 소유권·dependency 검증도 유지한다. 미반영 수락 source 후보나 근거 불일치가 있으면 완료를 확정하지 않고 작업물과 기존 근거를 보존한다. 재검증 전환으로 현재 단계 근거가 무효화돼도 확인된 게시 근거와 실제 Merge를 대조하는 경계는 유지한다.

반복 수동 제출의 저장 후보는 생성 당시 원격 OID·incoming 부모와 결합한다. exact 게시 전이 또는 후보를 포함하는 후속 이력의 equal 관측은 같은 저장소·소유자·종류·대상 ref로 검증한다. equal intent는 정확한 OID 관측 뒤 저장되므로 결과 저장 전 재중단과 후속 관측으로 종료된 전이에도 게시 근거를 유지한다. 종료된 exact 전이의 aborted receipt는 소유 intent·빈 기준 근거·원격 후속 계보를 검증해 원문 그대로 확정한다.

그 관측에 후보가 포함돼 있으면 다음 equal intent가 없어도 게시 후보 제거를 감지한다. 완료 receipt는 변경하지 않으며, 원격에서 게시 후보가 제거되면 새 lease나 대체 후보를 만들지 않는다.

### 관리 rebase와 후보 검증

현재 rebase 정책은 작업 수행 이력을 필수 증거로 삼던 R-045 설계를 대체한다. 자동 Worker 교정과 사용자 수동 해결·stage·continue·완료를 같은 현재 상태 판정으로 다룬다. 과거 기록은 감사 자료로 보존하며 누락 자체를 거부 조건으로 사용하지 않는다.

| 판단 경계 | 수용 근거와 후속 처리 |
|---|---|
| Git rebase 결과 수용 | 현재 repository·작업·worktree·branch·target·원격·PR과 최종 변경을 검증하고 checkpoint generation에 결합한다. Git 완료는 구현·검증·게시 성공을 뜻하지 않는다. |
| Worker 구현 결과 | 구현 조건·sources·dependency·archive·현재 runtime 지침과 완료 결과를 대조한다. 원본 명세 지문만의 변경은 Worker 입력 token을 바꾸지 않으며 실제 구현 조건 변경·부족은 기존 Worker 판단·교정으로 연결한다. |
| verify·Review·게시 | 원본 명세·전체 본문·base·후보와 단계별 입력 증명을 유지한다. 후보 변경 후 각 성공 근거의 유효성·재사용 조건을 다시 판단하고 정확한 원격 lease·PR gate를 통과한다. |

재평가는 입력 차단과 충돌·완료 근거 부족을 구분한다. `plan_input_invalid`에 저장된 rebase intent가 연결되면 현재 선언·경로·catalog, 선택 worktree·branch와 원래 HEAD, target·원격, index 상태를 다시 확인한다. 소유한 원래 HEAD에서 안전하게 시작할 수 있으면 입력 차단만 해제하고 intent를 보존해 기존 rebase 시작으로 잇는다. 이 START 판단은 rebase 완료, 검증 성공 또는 게시 lease의 소유 증거가 아니다. 관리 worktree 충돌이나 완료 근거가 부족한 차단은 START만으로 해제하지 않으며 확인된 소유 operation 또는 수용 가능한 완료 결과를 요구한다.


#### 충돌 없는 rebase와 Worker 결과 연결

Worker 결과와 작업 범위를 확인한 뒤 최신 `origin/<base-branch>`를 fetch하고 소유 변경을 commit하여 필요한 rebase를 완료한 다음 게시 후보 HEAD에서 검증 근거와 Review를 판단한다. `RevisionHandler`·`LatestBaseHandler`의 정상 rebase 호출이 충돌 없이 완료되면 기존 Worker 구현 입력 token·archive token·완료 결과를 대조한다. Worker 입력은 구현 조건·sources·dependency·archive·현재 runtime 지침에 결합하며 원본 명세의 내용 지문만의 변화는 제외한다. 명세 지문은 verify·Review의 입력 증명에 별도로 유지한다.

일치하는 결과만 현재 수용 HEAD의 `worker_candidate_token`에 checkpoint CAS와 runtime transaction으로 연결해 VERIFYING으로 이어간다. `decide_revision_gate()`가 실제 조건 변경·미완료·교정으로 IMPLEMENTING을 요구하면 clean 완료도 그 판정을 유지한다. 진행 중인 Git operation의 중간 형상을 완료 Worker로 승인하지 않는다.

문서 자동 채택·중단 뒤의 PR 전 후보는 `execution/stage_results.py`가 보존한 원래 결과로 별도 재연결할 수 있다. 현재 acceptance의 작업·branch·worktree·target·HEAD, 같은 verify cycle의 selection, 원래 완료 Worker·후보 token·archive·session과 현재 definition을 대조한다. 원래 base가 원래 후보와 현재 base의 조상인지 확인하고 각 후보를 자기 base와 비교한 경로·mode·blob·변경 종류가 같은지 검사한다. base의 코드 변경이나 직접 수정한 다른 문서의 차이를 무관한 문서로 제외하지 않는다. 선택 ExecPlan은 기존 본문 정규화가 증명하는 순수 기록만 Worker 비교에서 분리하며 frontmatter와 mode는 유지한다.

관련성 판단은 게시·catalog·DB 확정된 일반 `planning_reviews` 결과를 소비한다. 최신 base에서 확정된 결과가 없으면 그 확정 revision 이후의 전체 delta가 내용·mode를 보존하는 Requirement/ExecPlan archive 이동과 선택 ExecPlan의 순수 기록인지 기존 archive 판정기와 본문 정규화로 증명한다. 다른 추가·수정·삭제·경로 변경, 미확정 planning과 비교 불명은 새 복구 연결에 사용하지 않는다. 직접 sources 밖의 관계를 파일 ID로 추정하거나 별도 영향 Planner를 호출하지 않는다. 기존 dependency·사용자 결정·원격 소유권 gate는 유지한다.

Git 조회 후 짧은 transaction에서 실행 row·checkpoint generation·verify selection·확정 planning을 다시 대조하고 원래 origin과 현재 HEAD·definition·planning delta를 `worker_reconnection`에 결합한다. 같은 acceptance의 연결은 한 번만 적용해 이후 교정이나 실패를 과거 결과로 덮지 않는다. Worker 연결은 VERIFYING으로만 전이하며 verify·Review·publication token을 만들거나 실패 이력을 초기화하지 않는다. 새 연결 근거가 없는 과거 checkpoint는 현재 값으로 보충하지 않고 기존 단계 판정을 따른다.

같은 게시 lease·중간 checkpoint 경계를 유지하며 새 후보에 대한 verify·Review·publication 근거는 각각의 입력 증명·재사용 규칙으로 다시 판단한다. Worker 결과의 연결만으로 다른 단계의 성공을 만들지 않는다.

원래 단계 자료는 승인 token 초기화 전에 checkpoint의 `stage_result_origins`에 보존한다. version 1 항목은 canonical JSON evidence의 SHA-256을 key로 사용하고 `evidence`와 최초 보존 `reason`을 가진다. evidence에는 repo·plan·base·관측 HEAD, 원래 definition·단계 결과/token·session·Agent 입력/결과 snapshot, 원래 verify cycle selection과 실제 receipt, raw 변경 및 Worker용 기록 정규화 변경을 구분해 저장한다. 관측할 수 없는 입력은 null과 오류 종류로 남긴다. rebase intent의 `rebase_stage_origin`은 이 key를 참조하며 operation history에는 origin 집합을 중첩 복사하지 않는다. 채택 transaction의 `rebase_revalidation_audit`도 원래 definition·session·receipt 출처를 보존한다. 일반 revision 무효화와 verify 실행/재연결 역시 이전 Review 원문을 보존하되 현재 Review token은 비운다. 실제 Reviewer 호출 identity·관측은 기존 validation 결과와 invocation ledger에 남으며, 보존 자료만으로 최종 Review 승인을 생성하지 않는다. `stage_results.preserve`와 `reconnect_worker`는 row·checkpoint를 읽기 전에 일반 취소 감사와 최종 삭제 ID 표식을 확인한다. 외부 Git 관측 뒤에는 확정 transaction 안에서 같은 검사를 먼저 수행해, 그 사이 row가 제거돼도 원래 출처나 재연결 snapshot·단계 결과를 새로 쓰지 않는다. 일반 취소의 기존 감사는 그대로 두고 실제 최종 제거를 이 조회로 대신 확정하지 않는다.

#### 구현 조건과 실행 기록의 구분

definition snapshot에는 원래 ExecPlan 전문과 전체 body hash를 함께 보존한다. Worker body 비교는 Progress의 체크 상태만 정규화하고 Progress/Decision Log에서 다음 단독 실행 기록 형식만 분리한다(PASS 또는 FAIL, 날짜·명령·test 수는 실제 과거 사실).

```text
- 2026-10-08 실행 기록: `python3 -m unittest tests.test_code` → PASS (2 tests).
```

그 밖의 자유 문장·새 조건·승인은 해당 절 안에서도 입력이다. 두 원문과 hash가 일치해야 기록 분리를 적용하며 legacy snapshot은 원래 revision/path를 다시 읽어 저장 hash를 확인한 경우에만 보강한다. 원문이 없으면 현재 기록으로 합성하지 않는다. 전체 definition·verify 단계 token에는 전체 body hash를 유지해 기록 변경을 Review·승인 예외로 확대하지 않는다.

저장 교정의 입력 근거는 `rebase_corrections.input_snapshot`의 nullable JSON definition snapshot으로 결과와 함께 보존한다. 이는 Worker 호출에 전달한 definition이며 종료 뒤 갱신된 definition으로 대체하지 않는다. 실제 호출이 invocation ledger에 남긴 Worker 지침 hash는 응답 저장 전에 그 호출 입력에 결합한다. 최초 전달로 빈 지침 hash가 채워져도 동일 교정을 반복하지 않으며 본문·source·제약은 호출 전 snapshot을 유지한다. 기존 DB에는 nullable 열을 추가하고 과거 결과는 입력 근거가 없는 상태로 유지한다. `pending=stage/continue` checkpoint는 검증된 결과와 `rebase_worker_input`에 같은 definition을 결합한다. 저장 결과와 pending 소비는 기존 revision gate로 최신 선언·dependency를 확인하고 Worker·archive token을 비교한다. token은 순수 수행 기록과 원격 revision OID, 일반 planning에서 반영한 source 지문만의 변화를 구현 재실행 근거에서 제외하며 verify·Review의 입력은 계속 별도로 검사한다.

입력 불일치·근거 부재에서는 checkpoint의 `rebase_worker_input_history`에 이전 입력·결과·turn·pending·stage 근거를 보존하고 해당 READY와 pending 승인만 무효화한다. 작업 파일·session·원래 operation·target·문서 채택/파일 복원 intent와 실패 이력은 유지한다. 같은 Worker에 최신 조건과 과거 결과를 전달해 필요한 구현·targeted test를 확인하며 새 Planner 호출은 만들지 않는다. 입력이 유효한 pending은 별도 stage/continue 결과 소비 경로 대신 기존 Worker 완료 경로로 재개한다. 완료 후보는 새 현재 상태 서비스의 전체 최종 검사와 adoption을 거친 뒤에만 검증된 definition snapshot·Worker token을 같은 transaction에서 완료 HEAD에 연결하고 verify·Review·게시 근거는 무효화한다. 진행 중 A에서 B로 바뀐 조건을 충족한 교정은 B를 완료 기준으로 저장하므로 다음 revision gate도 B와 현재 조건을 비교한다.

`pending=continue`는 이전 unmerged index가 모두 해소된 뒤 기록한다. 그 뒤 새 unmerged stage를 관측하면 기존 post-command 경계에서 이전 pending·Worker 결과·입력·stage 근거를 `rebase_worker_input_history`에 `reason=next_conflict`로 보존하고 해당 승인만 소비한다. Git continue 직후 중단된 경우도 recover에서 같은 관측 경계를 거쳐 새 충돌을 등록한다. 부분 stage 중단은 기존 stage 재개를 유지하며 다음 충돌은 기존 session의 새 Worker turn으로 처리한다. 원래 target·문서 채택/파일 복원 intent·교정 및 targeted 실패 이력은 보존한다.

#### 현재 상태 검사와 문서 충돌

모든 관리 rebase의 시작·충돌 해결·완료·중단 복구는 현재 repository·계획·canonical worktree·소유 branch·target base·원격 OID와 PR identity/head를 확인한다. Worker 교정 turn, 승인 stage tree, 명령 receipt, reflog 표식, 원본 commit별 replay와 journal 완전성은 완료 조건이 아니며 기존 기록과 실패 감사는 보존한다

진행 중 rebase의 execution checkpoint가 없으면 현재 Git의 `head-name`·`orig-head`·`onto`와 실제 관리 ref·worktree 등록, 원격 branch·PR을 기존 현재 상태 검사로 대조한다. 확인된 같은 작업의 중간 형상에서는 원격의 현재 OID에 결합한 rebase checkpoint와 workspace 소유권만 복구한다. `recovered_rebase_metadata`는 이때 확인한 세 Git 메타데이터 값의 감사 사본이며 새 성공 근거가 아니다. 외부 관측은 DB 쓰기 transaction 밖에서 반복 확인하고, 작업 snapshot·checkpoint 부재가 그대로인 짧은 transaction에서 함께 저장한다. 파일·index·HEAD·session·실패 이력과 기존 검증 결과는 보존하며 완료·검증·게시 성공을 합성하지 않는다. 관측 경쟁은 재조회하고, 실제 작업·소유권을 확인할 수 없는 경우는 해당 작업에 한정해 보존·보류한다.

rebase의 검사 시점은 [R-055](requirements/%5BR-055%5D%20차단%20경로%20전수%20조사와%20불필요한%20작업%20중단%20축소.md#불필요한-중단의-수정)를 따른다. 시작에는 repository·계획·worktree·branch·시작 HEAD·고정 target·원격/PR·writer·index 안전·작업물 보존 조건만 확인하고 파일·선언을 비교·복구 기준으로 기록한다. 최신 target 선언과의 일치·역할별 전체 diff·문서/archive 최종 내용·과거 definition snapshot 존재는 시작 gate가 아니다. 공통 현재 상태 서비스는 현재 소유 branch가 아직 target을 포함하지 않는 형상을 시작 입력으로 구분한다. 시작 HEAD의 누락·노후화는 최종 범위 위반이 아니며, 실제 새 rebase를 시작할 때 현재 HEAD를 checkpoint와 journal에 결합하고 이전 시작 근거는 감사로 보존한다. 진행 중 operation의 `orig-head`·`head-name`·`onto` 검사는 유지한다. rebase 전 변경 관측은 target과 현재 HEAD의 merge-base를 사용하므로 base에만 추가된 문서·archive를 작업 branch의 삭제·되돌림으로 읽지 않는다. target 자체의 유효한 작업 identity·비draft 선언은 확인하고 현재 worktree의 최종 선언 검사는 유예한다. 시작·중간 형상에서 구성한 서비스가 완료 형상을 관측하면 최종 계약을 새로 구성하도록 요구하며 유예한 검사를 완료 승인으로 재사용하지 않는다.

Worker 종료 후에는 해당 호출이 만든 변경의 역할별 허용 범위를 검사한다. commit 재생·일부 stage·autostash 복원 중간 형상은 전체 최종 후보로 검사하지 않는다. Git 완료 후 현재 checkpoint·서비스·HEAD에서 공식 선언·전체 diff·보호 문서·archive와 작업물 보존을 검사한 뒤 채택한다. 공식 선언 교정과 저장된 과거 복원 intent의 처리는 아래 문서 충돌 경계를 따른다. 최종 금지 변경·잘못된 선언은 거부한다. 충돌 해결 Agent의 sandbox는 계속 적용하며 Git 전후 차이를 접근 권한 위반으로 추정하지 않는다. 관측 중 단계가 바뀌면 재관측하고 최종 archive 계약을 새로 구성한다. `REBASE_HEAD`·`MERGE_MSG`·`MERGE_MODE`·`AUTO_MERGE`만 남은 상태는 controlling operation이나 autostash 복원 대기로 취급하지 않는다. 이 구분은 rebase 채택뿐 아니라 차단 checkpoint·원격 재평가·명령·Agent 복구·bootstrap·source 동기화의 실행 gate에서도 같은 공통 판단을 사용한다. 원본 marker 관측은 보존하며 실제 조작 marker와 알 수 없는 marker는 계속 차단한다.

`execution_checkpoints.snapshot.rebase_operation_history`는 이전 operation의 상태를 순서대로 보존하되 새 항목에서 같은 history 필드를 제외한다. 이전 history는 바깥 배열에 한 번만 유지하므로 새 rebase를 시작할 때 기존 이력 전체가 다시 중첩되지 않는다. 현재 실행·acceptance·원격 lease의 필드와 과거 operation 상태의 감사는 유지한다.

시작 전 Worker branch·target의 merge-base 이후 문서·코드 형상은 소유 operation의 재생·보존 입력으로 기록한다. 이 기록은 역할별 전체 diff의 허용 판정이나 Worker 변경 승인이 아니다. Requirement·ExecPlan의 자동 채택은 고정 target 원문·경로·identity에 결합하고 최종 문서·archive 및 Worker 결과의 판정은 각 완료 시점에 수행한다.

자동 Worker 해결과 사용자의 수동 파일 해결·stage·continue·완료를 같은 현재 상태 판정으로 수용한다. 소유 rebase의 Requirement·ExecPlan 충돌은 `rebase_documents`가 관측한 원격 base의 immutable revision에서 catalog·ID·source·dependency와 실제 PR 경계를 확인한 뒤 해당 문서의 bytes·mode·경로·삭제를 채택한다. PRODUCT·ARCHITECTURE·지침과 코드·테스트·설정은 자동 선택하지 않는다.

rename·archive의 목적지는 같은 원본 ID로 확인하며 현재 작업의 삭제·archive·미반영 rename으로 유효성을 확정할 수 없으면 원인과 범위를 checkpoint에 보존한다.

교체 전의 작업 HEAD·replay commit·문서 원문·stage별 blob·작업 파일과 문서 외 index·dirty·operation 근거는 기존 execution checkpoint에 보존한다. 중간 HEAD의 문서 blob도 보존하며 소유 operation의 같은 HEAD에서만 committed 형상으로 인정하고, staged 형상은 pinned base blob으로 따로 검증한다. 파일별 교체와 pinned blob의 index 적용 전후를 대조하고 소유권·원격 lease·PR을 재확인한다.

적용·stage 중단은 같은 원문으로 재개하고 뒤늦은 replay 충돌도 같은 문서 revision을 사용한다. 후속 catalog·source·rename 판정 오류의 uncertain 기록도 이전 채택 이력과 원문·blob을 유지한다. 모든 재생 위치의 채택 경로를 checkpoint 이력에서 모아 완료 직전 HEAD·index·작업 파일을 원문과 대조한다. 후속 비충돌 재생이 원문을 바꿨으면 완료 채택을 거부하고 checkpoint와 작업물을 기존 복구 경계에 보존한다.

충돌 해결 Worker의 종료 검사도 소유된 중간 상태에서는 최종 변경 범위·선언 판정을 유예하고 구조·operation·원격 소유권을 확인한다. 응답·실제 호출 전에 고정한 입력 snapshot·session·종료 코드·결과 종류는 후속 검사 전에 같은 교정 turn에 함께 보존한다. 검사 오류 뒤 재개에도 당시 입력을 최신 definition으로 대체하지 않는다. 문서 충돌 보존 전후에는 해당 worktree의 HEAD·branch·index·dirty·operation·config와 문서 원문을 재확인하며, 다른 작업의 ref·worktree 목록 변화만으로 보존을 거부하지 않는다. 공식 선언 불일치 교정은 최신 원격 base의 별도 게시·catalog·DB 반영 경계를 사용하고 작업 브랜치에는 rebase로 전달한다. 원격 작업 브랜치가 catalog의 공식 base revision을 아직 포함하지 않았으면 과거 선언과 catalog의 값 차이만으로 통합을 거부하지 않는다. 공식 revision의 선언 유효성과 과거 작업의 ID·draft를 확인하고, base 반영 후 최종 선언 일치를 검사한다. Git 완료 뒤 공식 선언·전체 최종 범위와 작업물 보존을 검증하며 새 frontmatter 복원 commit은 생성하지 않는다. 이미 저장된 과거 frontmatter 복원 intent만 정확한 bytes·소유권·tree 검증을 거쳐 중단 복구한다. 이 복구는 Worker·targeted test·verify·Review 성공 근거가 아니다. 선언 불일치는 Worker 반복 교정으로 보내지 않으며 plan_input_invalid의 소유 rebase도 일반 branch 검사 전에 같은 복구 경계로 재평가한다.

차단 계획의 원본 base 반영에서는 workspace identity·원격 lease·index·작업물 보존을 확인한 뒤 고정 target의 rebase와 중간 게시를 완료한다. 더 최신 base를 반영해야 하면 저장 target을 먼저 끝내고 후속 base까지 반영한 뒤 현재 계획 행으로 catalog metadata 전체 일치를 검사한다. 최종 불일치는 원래 차단 원인 평가·해제와 후속 자료 게시로 넘기지 않는다.

metadata 차단의 자동 재평가는 차단 당시와 현재의 선택 worktree 또는 원격 base의 ExecPlan·source 입력 지문을 비교한다. 차단 당시의 별도 Git 관측이 같은 block과 checkpoint generation에 결합되어 있고 rebase 진행 상태가 현재 소유 원래 HEAD의 START로 돌아온 경우도 계기가 된다. 증거가 없거나 비교할 수 없으면 보존하며, 무관한 SHA·코드·검사 정책 변경과 과거 해제 기록이나 `rebase_block_release_policy`의 유무는 계기가 아니다. 입력 판단을 통과한 뒤 현재 소유권·원격·실행 자리·dependency를 검사하고 저장된 과거 선언 복원 intent가 있으면 위 복구 조건을 적용한다. 이전 release의 같은 WAITING 상태는 자동으로 반복하지 않는다.

명시적 `recover`는 접수 snapshot의 대상·원인·checkpoint generation이 유지되는 metadata 차단을 현재 상태에서 다시 검사한다. 같은 요청의 대상·원인 소비와 차단 해제·checkpoint 전이를 한 transaction에서 확정하므로 중단 시 함께 보존된다. 새 요청은 재검사의 계기이며 소유권·writer 종료·원격·PR·dependency·pause·관리 자리나 독립 차단을 우회하지 않는다. 원래 target·작업물·세션·실패 이력과 이전 정책 감사는 보존하고 START/WAITING 해제를 완료나 검증 성공으로 기록하지 않는다. Git 완료 후 선언·범위 검사와 검증·게시 gate는 유지한다.

rebase target은 바꾸지 않으며 원격 재전진은 기존 operation 완료 후 반영한다. 문서 전용 충돌은 Manager가 continue/skip하고 혼합 충돌의 나머지 파일과 autostash 충돌은 기존 Worker 교정 경계에 남긴다. 문서 Progress는 현재 후보의 실행·검증 성공 증거가 아니며 완료 후의 입력 재평가·verify·Review·게시 gate는 유지한다

완료 결과는 HEAD·branch ref·worktree 등록·target ancestry, clean index·미해결 operation 부재, 현재 역할·선택 ExecPlan 및 archive 계약에 맞는 최종 변경과 정확한 원격 lease를 확인한 뒤 현재 상태·입력 지문과 SQLite generation에 결합해 한 번만 반영한다. 원본 commit 내용·개수·순서의 변화 자체를 거부하지 않으며 Git 완료 뒤 저장 전 중단도 같은 검사로 복구한다.

복구 진입에서는 Worker checkpoint 요청·게시 대기가 남아 있어도 완료 가능한 rebase를 먼저 관측한다. 완료 조건을 충족하면 기존 journal 결과 기록과 현재 상태 채택·DB 확정을 일반 Worker checkpoint 처리보다 먼저 수행하고, 갱신된 실행 상태와 checkpoint로 요청·게시를 재개한다. 저장된 rebase 교정 Worker 결과의 `pending=stage/continue`는 기존 입력·결과 검증과 완료 경로를 유지하며, 일반 Worker 게시 검사가 미확정 rebase operation만을 이유로 그 완료 경로를 막지 않는다. 진행 중이거나 불일치하면 operation·target·작업물·index·commit·session과 감사를 보존하며, operation 값만 지우거나 과거 기준선으로 작업물을 되돌려 완료를 만들지 않는다.

rebase 채택의 한 검사 구간에서는 `ManagedRebaseService.adopt`가 안정된 이중 관측을 읽기 전용 소유권 소비자에 전달한다. 소비자는 전체 `RebaseExpectation`과 checkpoint generation이 일치할 때만 이를 사용하고 게시 owner를 별도로 검증한다. SQLite 확정에서는 새 이중 관측으로 다시 확인하며 Git·Agent·verify·게시 및 재시작 경계를 넘어 전달 결과를 저장하지 않는다.

최초 검사·소유권 소비·확정 재검증은 각각 `rebase:adoption_inspect`·`rebase:adoption_owner`·`rebase:adoption_confirm`으로 계측한다.

#### 중간 checkpoint 게시와 원격 입력

현재 rebase 소유권 관측은 대상 worktree의 HEAD·index·파일·Git operation과 전체 ref·worktree 등록 관계·필요 config를 새로 읽는다. 다른 worktree의 파일·index 상세 상태는 이 판정에 사용하지 않으므로 관측하지 않는다. 같은 branch의 중복 등록과 실제 대상 소유권 검사는 유지하며, Agent 실행 전후 격리 snapshot은 기본 전체 관측을 계속 사용해 다른 worktree의 변경을 검사한다.

진행 중 rebase에서는 저장된 일반 Worker checkpoint 요청을 소비하지 않고 rebase 충돌 처리·완료를 먼저 수행한다. 완료된 중간 게시의 intent·결과 identity와 현재 수용 기록은 과거 차단 checkpoint가 unpublished로 바뀌거나 다음 rebase가 intent를 `retired_rebase_publications`로 옮겨도 원격 소유권 근거로 유지한다. 현재 결과와 같은 ID의 활성 또는 보존 intent를 찾아 같은 계획·branch·worktree·게시 HEAD와 현재 원격 OID를 대조하고 현재 rebase 소유권·범위도 다시 검사한다. 보존 intent 자체의 `confirmed: false`는 해당 폐기 동작이 게시 성공을 새로 확인하지 않았다는 뜻이며, 별도로 확정된 같은 intent의 게시 결과를 취소하지 않는다. 과거 차단 기록의 상태나 활성 intent 포인터 교체만으로 확인된 게시 증거를 거부하지 않는다.

동일 계획의 기존 원격 작업 branch를 관리 rebase한 경우, 현재 repository·계획·worktree·branch·HEAD·등록·target base·index·operation·입력과 연결 PR을 확인한 뒤 완료 HEAD의 중간 checkpoint 게시 또는 현재 lease에 따른 보류를 확정한다. 기존 PR도 같은 URL·head/base branch를 유지하며 이 경로를 사용할 수 있다. 원격 입력이 바뀌면 이전 intent를 성공 없이 감사로 보존하고 통합·재검증으로 돌아간다.

신규 원격 branch·다른 계획의 branch는 기존 게시 intent로 갱신하지 않는다.

중간 게시 intent를 만들 때 기대 원격 OID는 현재 검증된 rebase 수용 기록의 원격 OID를 사용하되, 저장된 수용 입력·마지막 게시·연결 PR head·게시된 차단 checkpoint 중 적어도 하나의 소유 근거와 일치해야 한다. 마지막 수용 입력과 마지막 게시 OID는 서로 다른 시점의 근거이므로 고정 우선순위로 하나를 선택하거나 둘의 일치를 요구하지 않는다. 현재 관측만으로 알려지지 않은 원격을 새 lease로 승인하지 않는다. 이 동일한 소유 근거 기준을 lease 선택·push 직후 소유권 검사·DB 확정에 적용한다. 중간 게시 intent는 선택된 정확한 기대 원격 OID와 후보 HEAD, 현재 상태 수용 identity·지문을 SQLite에 보존한다. 게시 직전 실제 ref와 연결 PR의 identity·head를 다시 확인하고 정확한 OID를 명시한 `--force-with-lease`로 같은 ref만 갱신한다. 새로운 원격 입력은 재관측·통합·재검증 대상으로 처리하고, 삭제·다른 대상·실제 권한 또는 보호 정책 거부는 해당 작업이나 쓰기를 보류한다. 원격 변화를 이전 intent의 새 lease로 끼워 넣지 않는다.

Worker checkpoint 요청·workspace 준비·최종 PR 게시가 호출하는 `publish_worker_checkpoints`는 현재 후보에 맞는 게시 경로를 공통으로 선택한다. 원격 작업 branch가 없는 후보는 부재 lease를 사용하는 최초 게시 경로로 보낸다. 기존 원격 OID를 필요로 하는 중간 rebase 게시의 무효과 반환을 미완료 게시로 반복 소비하지 않는다. 최초 게시 직전 원격이 생기거나 소유 근거와 달라지면 기존 재관측·소유권 경계로 처리한다. 수용된 rebase 후보와 미완료 rebase 게시 intent는 원래 Worker 시작 HEAD의 선형 ancestry 검사보다 먼저 현재 소유권·범위를 재검증하고 정확한 lease로 게시한다. 이전 선형 push intent나 원격 통합이 남으면 그것을 먼저 확정·폐기한 뒤 현재 후보로 경로를 다시 선택한다. 이 판단은 호출자가 별도 fallback을 수행하는 데 의존하지 않는다. 현재 HEAD의 원격 반영과 DB lease·checkpoint 확정이 함께 확인돼야 반환을 완료로 소비하며, 게시가 보류됐으면 명시적인 일시 대기로 반환해 요청·단계·이력을 유지한다.

Worker의 저장된 checkpoint 요청이 수용된 rebase의 후속 commit이면 현재 후보의 소유권·최종 범위를 새로 확인해 같은 중간 게시 경계로 연결한다. 요청 commit 자체가 이후 Manager rebase로 재작성됐으면 `rebase_operation_history`의 동일 요청·원래 commit intent와 실제 원래 parent/tree/message를 대조하고 현재 HEAD의 rebase 수용·소유권·최종 범위를 다시 확인한다. 현재 tree를 과거 tree와 같다고 가정하지 않으며 원래 commit 증거나 현재 수용 근거가 없으면 요청을 보존한다. 과거 rebase 게시 보류나 최종 PR 게시 예정만으로 요청 완료를 기다리게 하지 않는다. 현재 후보와 정확한 lease를 게시 intent에 고정하고 중단 뒤 같은 게시를 재개하며, 원격 확인과 DB 확정 전에는 요청을 소비하지 않는다. 완료 감사의 `worker_checkpoint_requests`에는 현재 `head`와 재작성 전 `original_head`를 함께 남기고 요청 원문·session·전달 identity를 유지한다. 게시만으로 Worker 완료·verify·Review 성공을 만들지 않는다. 게시 확정 transaction은 실행 행의 게시 OID와 checkpoint의 관측 OID·게시 영수증·`accepted_work_branch`를 같은 intent HEAD로 갱신한다. 원래 게시 lease와 수용 근거는 intent와 게시 감사에 보존하며, 과거 DB의 낡은 수용 포인터는 현재 검증된 다른 소유 근거를 무효화하지 않는다.

Worker checkpoint의 미완료 push도 새 원격 입력을 먼저 통합한다. 완료된 같은 branch·worktree 통합의 원래 HEAD에 게시 대상 commit이 포함된 경우만 낡은 intent를 `retired_worker_checkpoints` 감사 배열의 `intent`·`observed_remote`·`confirmed: false`로 보존하고 폐기한다. 이는 push 성공이나 최종 검증 근거가 아니다.

push 전후 및 SQLite 반영 전 중단에서는 같은 intent와 실제 원격 OID·현재 소유 상태를 재대조한다. 원격이 이미 후보 HEAD면 push·commit을 반복하지 않고 완료 기록과 마지막 채택 원격 OID를 한 transaction으로 갱신한다. 일시적 실패는 같은 intent로 재개하며 미게시 rebase가 여러 개 쌓인 경우도 과거 journal·receipt·commit별 replay 연쇄 없이 동일한 현재 상태 판정을 사용한다.

중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인이 아니며 최종 후보의 Worker 확인·targeted test·전체 verify·Review·PR gate를 유지한다

`work_branch_integration.synchronize`는 현재 등록 origin의 fetch/push URL, 작업·worktree·ref·연결 PR을 확인하고 정확한 원격 commit을 받아온다. 로컬·원격의 차이와 현재 Git operation을 분리하며, 필요한 통합은 기존 baseline tree와 보존 commit·현재 관리 rebase 경로를 사용한다. `accepted_work_branch`는 읽고 확인한 입력이며 게시 성공 receipt가 아니다.

비조상 원격 입력은 두 commit과 공통 이력을 확인하고 같은 작업의 로컬 이력에 기존 anchor가 포함되거나 로컬·현재 원격에 전용 patch가 동등하게 보존된 때 기존 통합기로 넘긴다. merge 해소의 patch 동등성만으로 보존을 증명하지 않는다. 진행 중인 소유 통합은 현재 rebase 소유권을 확인하고 기존 conflict baseline·restart intent로 보존한다. 차단 복구의 후속 원격 관측도 같은 연결성 검사를 사용하며 origin·branch·worktree·PR 검증을 유지한다. 완료한 통합의 원래 후보를 기준으로 재검증 필요 여부를 판단하고 원격 수용과 검증 무효화를 하나의 transaction에 반영한다.

같은 입력은 다시 통합하지 않고 실제 Git 쓰기 직전 대상·후보·원격 lease를 재확인한다. 원격 통합은 공통 ignored 충돌 helper로 최종 diff와 로컬 replay commit의 중간 경로를 대소문자·부모/자식 관계까지 비교한다. 보존 HEAD/index 적용과 rebase 시작 직전에 재관측하고, 후속 입력의 소유 rebase abort는 현재 operation의 원래 HEAD 복원 경로 helper로 검사한다. 관측 실패·충돌은 기존 checkpoint와 baseline·restart intent를 보존한 채 쓰기를 거부한다. 이미 완료된 소유 rebase는 이 쓰기 검사 없이 기존 소유권·lease 검증으로 결과만 수용한다.

#### 후보 변경과 과거 차단 재평가

rebase나 사용자 입력으로 후보 HEAD가 바뀌면 후보에 결합된 verify·Review·publication 근거를 다시 판단한다. 구현 목표의 Worker 입력 token이 유효한 OID 변화는 필요한 verify·Review로 이어가며 Worker 재실행을 강제하지 않는다. 필요한 검증 단계의 후보 기준선을 저장해 같은 차이로 단계 무효화를 반복하지 않는다. 명세 변화의 planning 필요 여부는 프로젝트 조율기에서 판단·반영한다. 작업 revision gate는 전체 명세 지문으로 planning을 재요청하지 않고, 최신 베이스의 rebase와 선택된 ExecPlan의 변경·의존성·입력 및 기존 결과의 유효성을 확인해 필요한 단계부터 이어간다. 작업 조건 변화·실제 구현 부족은 Worker·Manager의 기존 단계에서 처리하며 별도 작업별 영향 Planner를 호출하지 않는다.

관측의 안정성 비교에는 선택 branch/worktree, 실제 origin 연결과 ignored 판정에 소비한 입력만 포함한다. 다른 ref·worktree·무관한 Git 설정의 변화는 현재 후보를 무효화하지 않는다. BLOCKED 작업물 보존은 현재 후보의 HEAD·branch·index·파일·operation을 재확인하며 전체 snapshot 직렬화 비교나 과거 Agent 기준선으로의 stash·reset·복원을 수행하지 않는다. 과거 snapshot과 복구 intent는 감사로 보존한다.

현재 결과는 선택 branch/worktree의 비교 기준선에만 반영한다. Agent 원본 snapshot·session·허용 범위와 과거 감사를 보존하고 수동 복구를 Manager 실행·Agent 격리 증거로 표시하지 않는다. 실제 sandbox 정책 오류, 다른 Manager Git 작업의 증거와 별도 Review·사용자 결정·planning 차단은 기존 경계를 유지한다

저장된 rebase 충돌·완료·소유권 오탐 차단은 실제 원인과 대상 계획의 rebase를 연결하고 현재 Git 상태, checkpoint, 원격 branch·PR 소유권 및 허용 최종 변경을 다시 확인한 경우에만 해당 원인을 해제한다. 과거 작업 기록이나 세션 일치는 해제 조건이 아니다. 다른 Review·사용자 결정·sandbox·planning 원인과 실패·알림 감사는 보존한다. 해제 후 후보 revision에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 수행하며 게시 직전 최신 원격 lease를 확인한다

### 차단 작업의 원격 반영과 보존

차단 상태의 저장, 작업 branch checkpoint, 별도 진단 자료와 차단 해제는 각각 확정한다. 보존·게시가 성공해도 원래 차단이나 구현·검증·PR gate를 대신 해제하지 않는다.

#### checkpoint 생성과 게시

1. `PLAN_BLOCKED` 상태, scope, 구조화 원인과 판단 근거를 SQLite에 먼저 commit하고 PR이 없는 계획에 별도 durable checkpoint request를 기록한다
2. 저장된 branch·결정적 worktree·workspace 소유권·HEAD와 Worker 허용 변경 범위가 모두 일치하고 미해결 Git operation, symlink, index에 없는 ignored 경로의 stage 또는 tracked 경로 충돌, 범위 밖 staged entry, index flag 또는 공유 metadata 불확실성이 없을 때만 검증된 pathspec으로 checkpoint commit을 만든다.

   충돌하지 않는 ignored artifact의 잔존만으로 checkpoint를 거부하지 않으며 이미 commit된 변경이나 변경 없음에는 빈 commit을 만들지 않는다
3. 같은 소유 branch의 원격이 없으면 정확한 부재 lease로 생성하고, 저장된 expected OID의 정확한 lease에 결합된 fast-forward만 갱신한다. fast-forward가 아닌 갱신은 PR 없는 동일 소유 branch의 검증된 관리 rebase에만 허용하며 예상하지 못한 원격 commit, branch protection과 권한 거부를 우회하지 않는다
4. commit·push phase와 local/remote OID를 저장해 중단 후 같은 checkpoint를 재개하고, 일시적 push 실패는 실행당 제한 횟수만 재시도한 뒤 local commit과 worktree를 보존한다. checkpoint 결과와 무관하게 ExecPlan은 `PLAN_BLOCKED`로 남고 독립 READY 선택 자리를 점유하지 않는다
5. `planning_error`, 등록·branch 생성 전 실패, 기존 PR 또는 현재 대상 작업의 소유권·범위를 증명할 수 없는 차단에는 원격 쓰기를 수행하지 않고 미게시 이유를 저장한다. `PROJECT_BLOCKED`에서는 검증 가능한 대상 계획의 복구·보존만 [프로젝트 차단과 진단 게시](#프로젝트-차단과-진단-게시)의 경계에서 허용한다. 차단 알림은 checkpoint 결과 확정 뒤 성공 시 commit OID와 immutable branch link를, 미게시 시 local-only 이유를 포함한다

#### 계획별 해제와 원격 입력 수용

유지보수 cycle의 차단 복구와 명시적 로컬 재검사 요청은 READY 선택과 같은 확정 대상·필수 dependency 범위를 사용한다. `prerequisite_wait`·`refactoring`·호환 `post_revalidation`에서 이번 범위의 작업은 cycle 종료를 기다리지 않고 기존 대상 lease·writer 종료·관리 배정 한도·planning 및 원인별 재검사 조건을 거쳐 복구한다. cycle이 차단 작업의 완료를 기다리는 동안 해당 작업의 복구를 `idle`로 제한하지 않는다. 확정 게시 영수증인 `remote_confirmed`는 복구를 막지 않으며, 미확정 게시·평가 중 상태·범위 밖 계획·불명확하거나 순환한 dependency는 기존 대기로 유지한다. archive된 dependency는 검증된 Merge 이력만 사용한다. 이 gate는 사용자 질문·공통 원인의 Agent 평가를 시작하기 전에도 적용해, 범위 밖 또는 자리 없는 작업의 Agent 검사가 허용된 작업보다 먼저 실행되지 않게 한다. 이 배정 허용은 공통 교정 Merge·사용자 판단·실패 원인의 해소나 검증 성공을 대신하지 않는다.

계획별 입력 변경, 사용자 결정 또는 닫힌 PR reopen 조건은 해당 `PLAN_BLOCKED`만 재평가한다. 작업 branch의 저장 OID나 receipt가 현재 원격과 달라도 현재 계획·branch·worktree·등록 origin·PR과 Git operation, 작업물·ignored 경로의 보존 조건을 확인해 새 입력을 수용한다. 로컬·원격의 추가 작업과 확인 가능한 관련 재작성은 양쪽 작업을 보존·통합하고 최신 base를 반영한다.

단순 OID 변경으로 원인을 해제하거나 과거 성공을 재사용하지 않고 실제 변경과 원인별 조건을 재평가한다. 필요한 verify·Review를 수행하며 실제 구현 입력 변경·부족만 Worker 교정으로 처리한다. 같은 입력의 검사·알림을 반복하지 않고 별도 사용자 결정·PR reopen·실제 대상 또는 보존 문제는 유지한다.

차단 해제 후 적용하는 경로에 따라 초기화·보존 범위를 구분한다.

| 재개 경로 | 보존·초기화 범위 |
|---|---|
| 일반 READY 재배정 | 기존 branch·worktree·작업물을 보존한다. 현재 Worker·Reviewer session 연결과 단계 결과, retry·verify/Review 실패 지문·진행 중 승격 집계는 초기화하고 effective Worker level을 현재 선언의 최초 level로 되돌린다. |
| 기존 PR 충돌 복구의 재진입 | 같은 PR·branch·worktree와 기존 Worker session을 유지해 진행 중 복구를 이어간다. 재배정 경로의 retry·실패 지문·승격 집계는 초기화하고 검증·Review 근거는 새 후보에서 다시 판단한다. |
| 원격 입력 통합·과거 오탐의 원인만 해제하는 기존 실행 재개 | session·branch·worktree·실패·승격 감사를 보존한다. 실제 구현 조건 변경·부족과 후보에 필요한 검증·Review만 기존 단계에서 처리한다. |

Merge 대기 PR의 충돌 복구는 다른 활성 작업의 존재만으로 건너뛰지 않는다. planning의 실행 자리 보호·프로젝트 차단·대상 dependency와 catalog 보류·관리 한도를 확인한 뒤, 선택한 PR 작업의 실행 context 안에서 대표 투영과 복구를 수행해 다른 작업의 실행 상태를 보존한다.

현재 실행값의 초기화와 과거 감사 삭제를 혼동하지 않는다. 같은 branch의 최초 PR은 최종 verify와 Review 성공 뒤에만 생성한다.

`PLAN_BLOCKED`와 계획과 연결된 `PROJECT_BLOCKED`의 원격 입력은 다른 활성 계획·PR 복구·planning publication·유지보수가 실행 자리를 점유하면 보존한다. 자리가 비었거나 차단된 같은 계획이 점유한 때 현재 ref·PR·계획·branch·worktree·origin과 작업물을 확인해 원격 작업 입력을 통합하고 최신 base를 반영한다. 완료 결과의 게시 또는 lease 보류를 확정한 뒤 원래 원인만 재검사한다.

원인이 남으면 기존 checkpoint 경계에서 이유와 작업물을 보존하며 같은 원격 HEAD에는 빈 commit·반복 push를 만들지 않는다. 원인이 해소되면 기존 session·branch·worktree·실패·승격 감사를 보존하고 필요한 후보 검증·Review부터 재개한다. 실제 구현 교정은 Worker 경로로 처리한다. 저장 단계와 실제 Git 결과를 대조하고 별도 사용자 결정·PR reopen·실제 대상 문제 및 다른 계획의 상태를 유지한다.

#### 프로젝트 차단과 진단 게시

`PROJECT_BLOCKED` 해제는 원래 차단 원인을 현재 repository·계획·worktree·소유권·runtime 연결과 대조하고 원인별 안전 조건을 통과한 경우에만 수행한다. 기본 checkout·사용자 branch·공유 metadata의 정상적인 변화를 원상복원하거나 과거 snapshot과 일치시키는 조건을 두지 않는다. 실제 대상·소유권·sandbox 정책의 문제와 독립 차단은 유지하며 `refs/codex/turn-diffs/*`의 순변경은 해제 조건을 무효화하지 않는다.

소유권 원인은 고정 code-scope 계약을 유지한다. 기존 `workspace_ownership_uncertain`은 계속 project 범위이며 저장된 행을 일괄 재분류하지 않는다. `workspace_ownership_local`은 plan 범위이고 동일한 typed recovery 원인 `ownership_uncertain`으로 연결한다. `plan_recovery.workspace_ownership_isolated()`는 이번 재평가에서 연속 관측한 현재 metadata로 기본 checkout의 등록, 선택 branch/ref/HEAD·고유 worktree 연결과 lock identity를 확인한다. 확인 구간에도 무관한 ref·다른 worktree·Git 설정의 변경 자체로 범위 축소를 거부하지 않는다. 과거 Agent snapshot의 유무·내용이나 과거 공유 상태와의 일치는 진입 조건이 아니다. 호출자는 대상 HEAD·branch·index·파일·operation의 안정성을 확인하며 대상 등록·lock 변경, 동일 branch의 중복 등록과 현재 근거 누락에는 project 범위를 유지한다. 선택 자원의 설명되지 않은 파일·index·operation은 그 작업의 미해결 원인으로 남으며 이 비교로 소유권을 승인하지 않는다. 이 비교는 미해소 원인의 범위 축소에만 사용하며 전체 원인 해소와 G-322 소유권 복구의 추가 gate로 사용하지 않는다.

`scope_reconciliation`은 G-322의 예약 lease·writer 종료 검증과 공통 이력 복구를 먼저 소비한다. 완전 해소가 안 되고 작업 범위만 입증되면 현재 프로젝트/계획 원인·작업·복구 기록·게시 요청·checkpoint generation과 자원 claim을 짧은 transaction에서 다시 확인한다. 계획 보류와 checkpoint의 `workspace_ownership_hold`를 저장한 뒤 프로젝트 gate를 해제하며 실패에는 함께 rollback한다. 이 선택적 JSON object는 원래 `block`, `resume_status`, 판정에 사용한 현재 관측 쌍 `baseline`·`observation`을 보존한다. 기존 기록의 `baseline`이 과거 Agent snapshot인 경우에도 감사로만 읽고 재개 허가로 사용하지 않는다. 별도 계획 원인이 있으면 해당 block row와 `recovery_snapshot`을 교체하지 않고 추가 hold만 둔다. `workspace_scope_audit`에는 원인 작업·충돌 자원·대기 claim과 교체 전의 일치하는 게시 요청을 남긴다. 기존 DB는 이 필드가 없는 상태로 계속 읽으며 누락 근거로 새 허가를 만들지 않는다.

배정 조회는 checkpoint 본문을 읽거나 해석하지 않고 `execution_checkpoints.workspace_ownership_held`의 0/1 표식을 사용한다. 이 값은 본문의 `workspace_ownership_hold` 존재·비어 있지 않음을 투영하며 checkpoint 저장의 generation CAS와 같은 SQL 변경으로 갱신한다. `(repo, workspace_ownership_held, plan_id)` 인덱스로 별도 계획 원인과 함께 남은 보류도 조회하고 기존 활성 작업 공간 소유자와 합친다. 상세 보류 증거·해제 판단의 원본은 checkpoint 본문이다. 기존 DB는 표식 추가 시 본문을 작업별로 한 번 읽어 채우며 실패하면 schema 변경·표식 갱신을 함께 rollback한다. 본문·generation·감사 이력은 변경하지 않고 이후 초기화에서는 재해석하지 않는다.

계획 재평가는 추가 hold를 일반 질문·입력 해제보다 먼저 처리하고 같은 예약 복구기를 사용한다. 실제 소유권 해소 후 추가 hold만 제거하며, 단독 소유권 차단은 저장 단계로 복원하고 별개 계획 원인은 `PLAN_BLOCKED`로 유지한다. 현재 원인과 일치하는 미완료 checkpoint 요청은 원자적으로 종료하며 게시 영수증·통지의 전송/실패/취소 감사는 보존한다. 해소된 원인이 낡은 요청으로 재게시되지 않고 scope 자체가 사용자 조치나 새 Telegram을 만들지 않는다. 후보·session·실패/승격·성공 token을 소유권 판정으로 교체하지 않으며 다른 작업의 투영과 planning 오류도 유지한다.

READY 선택과 활성화 transaction은 같은 branch·canonical worktree의 보유 작업을 확인한다. 생성 예약이 없는 READY claim은 호출 예약·미종료 invocation·게시 claim이 없을 때만 영향 대기로 취급하며 그 상태도 CAS에 포함한다. 실제 소유자·종료 불명 writer는 이 예외를 사용하지 않는다. 원인 작업의 보류는 기존 관리 개수 제외와 dependency gate를 사용하고, 실제 Agent/verify 호출 자리나 자원 예약은 회수하지 않는다. 프로젝트 차단이 해제돼도 다른 차단 계획의 fingerprint·작업 공간과 독립된 사용자 조치 근거는 바꾸지 않는다.

| 보존·게시 경로 | 허용 조건과 결과 |
|---|---|
| 작업 branch의 checkpoint | 현재 작업 대상·원격 입력·변경 범위·소유권과 정확한 lease를 검증한다. 충돌·미해결 Git operation 또는 검증 불가에서는 해당 branch의 commit·push를 보류하고 작업물·session과 실제 local-only 이유를 보존한다. |
| 별도 진단 branch·자료 | 원본 작업·자료 범위·게시 권한을 별도로 검증한다. 충돌 중에도 원래 HEAD·index·rebase를 바꾸지 않는 자료 수집·게시가 가능하다. 작업 branch가 local-only인 사실과 진단 자료의 게시 결과를 각각 기록한다. |

`diagnostic_preservation`은 검증된 파일·index stage·작업 파일을 `manifest.json`과 blob 자료로 보존하고 Manager 상태 디렉터리의 private index로 별도 orphan commit을 만든다. 범위 밖·비공개 경로와 비밀 자료는 제외하며 수집·intent 저장·게시·확정에서 원본과 소유권의 불변을 다시 확인한다. 이 조건을 증명할 수 없으면 자료와 미게시 이유를 보존한다.

진단 ref는 계획·원본 branch·base·내용 OID에 결합한다. 원격 부재 시에만 생성하고 같은 OID는 재사용하며 다른 OID로 갱신하지 않는다. 진단 ref 충돌은 해당 자료 게시만 보류한다. 원격 삭제·재작성·경쟁·권한·branch protection 오류를 예상 밖 ref의 force push로 우회하지 않는다.

원인·ref·OID·intent·결과는 SQLite에 보존하며 PR 생성·원래 branch의 수용·차단 해제·verify·Review 성공 근거로 사용하지 않는다. 현재 원형·보존·대상 또는 원래 차단을 확정할 수 없으면 이유와 작업물을 보존한다. 과거 rebase turn·stage 승인·receipt·replay·journal 부재 자체는 접근 권한이나 정상 입력·현재 상태 판정의 거부 사유가 아니다.

진단 ref 생성·checkpoint commit·push·SQLite 확정 사이의 중단은 저장 intent와 실제 OID를 대조해 복구한다. 같은 입력·결과의 rebase·게시·검사·알림을 반복하지 않으며 알림에는 확인된 ref·OID 또는 실제 local-only 이유와 보존 경로를 포함한다.

#### 진단 ref의 정리와 중단 재개

`diagnostic_cleanup.cleanup_recorded`는 프로젝트 실행 종료의 안전한 경계에서 저장된 ref만 재관측하고 적격 ref의 삭제·부재 확인을 수행한다. 준비·삭제 실패는 원래 실행 결과와 다른 gate를 유지하며 runtime handoff와 호출자 transaction 안에서는 정리를 시작하지 않는다. 원격 namespace 탐색·원인 해제·알림은 수행하지 않는다. 생성 전용 `push_diagnostic_checkpoint`는 그대로 유지하고 삭제 효과를 별도로 분리한다.

원본은 기존 `diagnostic_preservations`·`blocked_diagnostic_publications`의 intent/결과와 `diagnostic_ref_uses`의 원인별 사용이다. 사용 identity는 repo·scope(`plan`/`project`/`planning`)·owner·ref·cause identity·input identity·등록 origin에 결합하고 원래 branch·tree/commit OID, 게시 대기·확정과 변경 generation을 보존한다. 같은 내용의 ref를 새 원인에서 재사용해도 이전 사용을 덮어쓰지 않는다. origin이 바뀌거나 과거 성공 receipt를 반복 조회해도 미확정 사용의 게시 성공을 추정하지 않는다. 실제 충돌은 원문 manifest에 보존된 unmerged index stage 1/2/3으로 확인하며 원인 code만으로 추정하지 않는다. target·원인 연결이 부족한 과거 intent는 보존한다.

`diagnostic_cause_resolutions`는 원인 해제 transaction에 정확한 원인 snapshot·scope/작업·origin·branch/worktree와 현재 `COMPLETE` acceptance의 해결 HEAD·target·generation·identity를 보존한다. `WAITING` 상태에서 Worker 재개를 위해 해제하면 acceptance 대신 원래 pending target을 남기며, 정리 시 같은 target의 현재 `COMPLETE`를 별도로 확인해야 한다. acceptance의 `repo`와 `remote.repository`는 등록 프로젝트의 로컬 저장소 경로 identity이며 등록 origin과 별도로 대조한다. origin은 해제 시 프로젝트 기록과 게시 사용 기록에 결합하고, 과거 checkpoint의 origin도 해당 게시 사용 기록에서 읽는다. 일반 차단 삭제·활성 row 부재는 해소 증거로 승격하지 않는다. project 원인의 기존 두 identity 형식은 같은 원인 snapshot에서 각각 연결한다. 과거 checkpoint도 정확한 현재 generation의 acceptance와 마지막 해제 감사가 원인을 함께 증명할 때만 소비하며 서로 다른 이력 항목을 추정해 연결하지 않는다.

정상 branch 게시가 해소 확정의 앞이나 뒤에 일어나도 `publication_remote_oid`와 해결 HEAD가 현재 정상 원격 branch에 포함되는지 확인한다. 해소 때 미커밋 작업이 남았거나 과거 기록으로 당시의 clean 상태를 증명할 수 없으면 현재 작업물이 clean이고 HEAD가 저장된 게시 HEAD이며 해소 HEAD 이후의 commit임을 추가로 확인한다. 미게시 작업물을 이전 원격 HEAD의 성공으로 대신하지 않는다. PR Merge·다른 원인의 해제는 이 증명을 대신하거나 지연시키지 않는다.

원문 증명은 기존 `diagnostic-artifacts/<input identity>` bundle에서만 얻는다. 일반 파일의 bytes와 version 1 manifest의 scope·owner·원래 branch·revisions·파일별 index stage/작업 파일·부재·모드를 읽고 blob SHA-256, diff/manifest 완전성과 저장된 orphan tree/commit OID를 대조한다. 모드는 `100644`·`100755`·`120000`이며 원래 symlink의 target bytes도 일반 blob으로 읽고 link를 따라가지 않는다. SHA-1/SHA-256의 고정 orphan 형식을 bytes로 재구성하므로 원격 ref·작업 공간·Git 객체 DB 없이도 복구 근거를 검사한다. 누락·손상·불완전 manifest·지원하지 않는 형식·bundle 내부/부모 symlink·identity 불일치는 해당 준비를 보류한다.

`diagnostic_cleanup_preparations`의 ref별 `basis`는 현재 프로젝트·작업 소유권·원인·사용 generation·진행 중 호출 예약·checkpoint·intent/보존 자료·해소 기록 snapshot의 SHA-256이다. payload에는 boolean `ready`, 보류 `reason`, 저장된 게시 OID인 `expected_oid`와 원격·정상 branch 포함 관계·자료 digest 관측을 남긴다. receipt의 표시 순서 변경은 새 안전 근거로 취급하지 않는다. 준비는 두 번의 외부 관측과 짧은 transaction의 snapshot 대조로 기록하며 삭제 승인·영구 자격이 아니다.

삭제자는 기존 `plan:<ID>`와 보존·프로젝트 진단 게시자가 공유하는 `diagnostic:<ref>` 자원 잠금을 잡고 현재 snapshot·자료·원격을 다시 관측한다. 원래 작업/base와 다른 작업·수동 입력 branch로 사용되는 ref, 실행/종료 불명 호출 예약, 진행 중 checkpoint·보존·게시가 있으면 남긴다. 외부 관측과 push 동안 DB 접근을 반환하며 삭제 intent·단계 적용 transaction에서 현재 소유권·사용 generation·basis를 대조한다. 게시 대상 검증 뒤에도 원격·자료·snapshot을 다시 확인한다. `_validated_push_url`로 검증한 URL을 `_frozen_push_target`으로 고정하고 hook을 비활성화해 `--force-with-lease=refs/heads/<ref>:<저장 OID>`와 단일 삭제 refspec만 보낸다. 자동 tracking ref 갱신이나 일반 Manager journal의 허용 전이를 삭제 성공 근거로 쓰지 않는다.

호환 추가 테이블 `diagnostic_cleanup_attempts`는 `(repo, ref, basis)`별 삭제 근거를 보존한다. 필수 `expected_oid`는 저장된 게시 성공의 40/64자리 소문자 16진수 OID이며 현재 원격 OID를 새 기대값으로 채택하지 않는다. `attempts`는 삭제 시도 예약 횟수이고 JSON `payload`에는 원래 `state`·`facts`와 순서대로 누적한 `events`(`status`, nullable `reason`)를 저장한다. 기존 게시·원문·원인별 사용 기록을 정리 결과로 덮어쓰지 않는다.

| `status` | 의미와 다음 실행 |
|---|---|
| `intent` | 삭제 전 기록. 현재 조건과 원격을 재확인해 재개한다. |
| `pushed` | push 성공만 확인했다. 원격 부재 확인 전에는 완료가 아니다. |
| `absent` | 현재 안전 조건과 원격 부재를 확인했다. 동일 basis를 대조해 완료한다. |
| `complete` | 부재와 SQLite 확정을 마쳤다. 같은 intent로 재생성된 ref를 다시 삭제하지 않는다. |
| `unconfirmed` | 일시 실패·삭제 후 조회 실패 등이다. 다음 정상 실행에서 재관측하며 같은 basis의 시도 예약은 최대 3회다. 부재가 확인되면 추가 push 없이 완료한다. |
| `held` | lease 경쟁·권한/보호 정책 거부다. 같은 basis로 push를 반복하지 않으며 근거 변경 후 재평가한다. |

원격 부재의 최초 관측도 같은 보존·소유권 조건에서 push 없이 확정한다. 모호한 응답·조회 실패는 부재가 아니며 삭제 후 SQLite 쓰기가 실패해도 저장 intent와 다음 실행의 실제 원격으로 복구한다. 정리 intent에 포함된 원인의 과거 보존 재시도와 프로젝트 진단 게시 재시도는 ref를 재생성하지 않는다. 조회·알림용 payload는 과거 `published` 기록을 보존하면서 현재 `result=local_only`, `historical_publication`, `remote_state=absent|cleanup_pending`을 별도로 표시한다. 새 충돌의 별도 cause identity는 기존 보존 정책으로 처리한다. 기존 원문·session·실패·게시 intent·확정 감사와 독립 gate는 유지한다.

#### rebase 차단과 과거 오탐 복구

저장된 `managed_worktree_conflict`와 계획에 연결된 rebase 완료·소유권 오탐은 같은 현재 상태 서비스로 재평가한다. 특정 구형 오류 문구, 과거 Worker 세션·교정 turn·stage 승인·receipt·journal 완전성이나 입력 변경 지문 부재만으로 복구를 거부하지 않는다. 저장 원인을 해당 계획의 rebase intent와 연결할 수 없으면 차단을 유지한다

현재 repository·reservation·등록 worktree·소유 branch·target base·원격 기대값과 rebase metadata가 일치하면 일반 branch 검사 전에 detached rebase를 인식하고 충돌·사용자 stage를 보존한 채 같은 실행을 재개한다. 이미 완료됐으면 Git 단계를 반복하지 않고 결과를 채택한다. 관측을 transaction 안에서 다시 확인하고 대상 원인 해제와 후보 검증 무효화를 함께 기록한다.

다른 활성 실행·planning 또는 미충족 dependency가 있으면 투영·session·진단을 바꾸지 않고 대기한다

rebase 원인 해제는 별도 Review 실패·사용자 결정·PR reopen·실제 sandbox 정책 오류과 다른 계획·planning·프로젝트 안전 원인을 해제하지 않는다. 원문·실패·알림 감사와 기존 branch·worktree·session을 보존하며 변경된 후보는 Worker의 현재 입력·최종 diff 확인과 targeted test, Manager 전체 verify, Reviewer 및 기존 게시 ownership·lease·PR identity 조건을 다시 통과해야 한다

교정 소진으로 차단된 진행 중 rebase는 저장된 실제 차단 기준선과 현재 입력·선택 worktree를 비교할 수 있고 모두 같으면 차단을 유지해 동일 Worker 실패를 반복하지 않는다. 입력 또는 수동 해결·stage·continue 결과가 바뀌면 현재 상태를 다시 검사한다. 과거 기준선이나 실패 기록의 부재 자체는 복구 거부 조건이 아니다

폐기한 Git 진단의 차단은 입력 지문 변경과 과거 snapshot 일치 없이 재평가한다. 현재 작업·runtime 연결, 이전 writer 종료와 실행 자리·Git operation을 확인하고 동일 원인·checkpoint generation·현재 로컬 후보를 transaction에서 대조해 해당 원인만 해제한다. 과거 복원 요청·snapshot·작업물·session·retry·실패 감사와 독립 차단을 보존하고, 해당 지속 실패와 미전송 확인 요청은 해소한다.

진행 중인 관리 rebase는 기존 현재 상태 수용·journal 경계로 연결하고 후보가 달라지면 검증·Review를 다시 수행한다

기존 `publication_ownership_uncertain` 차단은 현재 작업 연결과 입력을 다시 확인해 정상 OID 차이·낡은 기록만 원인인 경우 해제한다. branch·worktree·origin·PR의 실제 불일치와 독립 실패는 유지한다. 해제 감사와 checkpoint generation·원인 일치 확인은 transaction에 결합하고 과거 게시·Agent·session 기록은 유지한다. 현재 입력 수용은 수행하지 않은 Git 동작이나 검증 성공을 만들지 않는다.

과거 게시 receipt·`publication_history`·저장된 push intent는 자기 게시가 실제 완료됐는지 확인하는 자료다. 원격 입력을 읽고 통합하는 데 receipt 존재나 과거 OID 완전 일치를 요구하지 않는다. 새 원격 입력으로 낡은 중간 게시 lease가 무효화되면 intent를 감사로 보존하고 현재 통합 경로에 반환한다. 완료를 확인하지 못한 push를 성공으로 기록하거나 같은 intent에 새 lease를 끼워 넣지 않는다.

#### 삭제 재시작과 파일별 복구

명시적 삭제 재시작은 실행 checkpoint의 `deleted_branch_restart` intent에 이전 행·block·session·publication·correction 감사를 저장한다. 기존 worktree를 별도 경로와 branch로 옮겨 index·파일·미추적·ignored 작업물을 보존하고, 최신 확인 원본에서 새 worktree를 준비한다. 파일 이동·branch 변경·새 공간 준비 사이 중단은 같은 intent로 재개하며 모든 확인 뒤에만 READY로 전환한다.

이전 verify cycle·성공 token·pending correction과 게시 요청은 새 실행과 분리한다. 실제 PR Merge 관측은 중단된 작업에서도 유지하고 재생성된 ref만으로 실행을 재개하지 않는다.

`rebase_documents.resolve`의 파일 복구 모드는 진행 중인 rebase의 target·replay 위치와 복구 기준 revision에 경로별 intent를 결합한다. 반복된 진전 없는 correction의 미해결 파일만 선택해 읽을 수 있는 이전 bytes·index와 실패 사유를 기록하며, 확인한 원본 blob·mode·부재를 해당 경로에만 적용한다. 다른 경로의 head·branch·index·dirty·operation을 대조하고 준비된 intent에서 중단되면 남은 적용만 진행한다.

새 사용자 수정은 덮어쓰지 않으며 복원한 파일은 Worker 재작업과 후보 검증을 거친다.

#### 원격 관측의 중복 방지

원격 관측 기록은 origin identity·정확한 ref·OID·실패 원인·시각을 계획별로 보존한다. 조회 실패는 마지막 확정 OID를 덮지 않는다. 처리 키는 원래 원인·검사 지문·관측 base/work branch OID·로컬 작업 지문을 포함한다. 같은 키의 확정 결과를 재사용하며 관련 입력이 달라지면 재평가한다.

### 로컬 복구 요청의 접수와 실행

`recover`는 범위별 요청의 접수·실행 대기·제한 재검사·종결을 구분한다. 별도 runtime이 등록 프로젝트·선택 G·기존 DB를 검증한 뒤 짧은 SQLite `BEGIN IMMEDIATE` transaction으로 `local_environment_recovery_requests`에 요청을 저장한다. 미등록 대상·없는 G·상태 DB 오류는 접수 전에 거부하며 DB를 자동 생성하지 않는다.

선택 계획이 관리 순번 대기(`parked`)이면 복구 요청은 pending으로 보존하고 일반 프로젝트 조율을 실행한다. 일반 관리 배정이 대기를 해제한 뒤 같은 요청의 복구를 이어간다. 대기 중인 계획을 실행 단계 부재로 오인해 복구만 반복하거나, 복구 요청이 관리 순번 해제와 독립 작업의 진행을 막지 않는다.

| 저장 계약 | 식별·재사용 규칙 |
|---|---|
| 요청 범위 | `(repo, plan_id)`. 프로젝트 전체는 `plan_id = NULL`이며 계획별 범위와 별도 부분 unique 인덱스로 구분한다. |
| 요청 상태 | `pending`·`completed`. 같은 범위의 pending만 같은 request identity로 재사용하고 종결 뒤 후속 재검사는 새 요청으로 접수한다. |
| 실행 결과 | 실행 전에는 결과가 없을 수 있다. `in_progress`는 pending, `resolved`·`unresolved`는 completed로 저장하며 전후 snapshot·소비·과거 결과를 감사로 보존한다. |
| 제한 재검사 소비 | `local_environment_recheck_consumption`의 `(request_id, subject, cause)`별로 기록한다. 같은 pending 요청의 해당 대상·원인 재검사는 반복 소비하지 않는다. |

요청 전 snapshot의 계획 항목은 정수 `checkpoint_generation`을 포함한다. metadata 재검사는 이 값과 해당 scope의 원인 코드·진단 및 해제 지문을 현재 block과 비교한다. generation이 없는 과거 요청이나 비교 불가한 근거는 기존 요청·진단을 보존하며 같은 원인의 재검사 허용으로 추정하지 않는다. metadata 차단의 선택적 checkpoint 관측은 block 사본·generation·Git snapshot과 원격 문서 입력 지문을 결합하며, 지문 부재는 원격 입력 변화의 증거가 아니다.

삭제 확정의 `deleted_plan_state.forget`는 같은 제거 transaction에서 `local_recovery_state.forget_plan`을 호출한다. 실제 `(repo, plan_id)`에 속한 요청 ID를 먼저 확보해 전용 요청과 그 하위 소비를 제거하며, 같은 repository의 공유 요청에서는 대상 subject의 소비만 제거한다. 남은 요청의 전후 snapshot은 `plans`·`blocks`의 `plan_id`, `recoveries`의 `subject`, `current_failures`의 문자열 identity가 정확히 일치하는 항목만 제거한다. 다른 항목·프로젝트 상태·원래 요청 결과와 시각은 유지하고 대상 항목이 없으면 snapshot 원본 bytes도 바꾸지 않는다. 알 수 없는 최상위 필드·잘못된 JSON/배열·항목 identity 부재는 귀속 미확인으로 transaction 전체를 롤백한다. 기존 cleanup은 이 요청 오류를 해당 삭제의 보류로 처리해 다른 대상의 정리를 계속하며, 원인이 해소된 다음 관측에서 남은 제거를 재개한다.

`recovery_unresolved` outbox의 최종 제거는 확보한 요청 ID와 outbox repository의 일치로 결정한다. payload의 오래된 작업 ID만으로 프로젝트·다른 작업의 사건을 삭제하지 않는다. 제거 대상의 부분 전송 본문·cursor·오류도 전용 사건과 함께 지우며, 일반 취소의 감사 보존 규칙을 이 최종 제거에 적용하지 않는다. 중단 뒤에는 남은 요청 관계로 멱등 재개하며 별도 삭제 감사나 익명 사본을 만들지 않는다.

요청 commit 뒤 managed config를 읽고 전역 process lock을 비차단 방식으로 얻는다. 선택한 계획의 실행 자리와 queue 순서가 허용할 때만 기존 복구 executor를 호출한다.

| 접수·실행 결과 | 상태·재개와 명령 결과 |
|---|---|
| 요청 저장 뒤 설정 오류 | commit한 pending 요청을 유지한다. 설정을 고친 뒤 같은 범위·identity로 재개하며 실행 성공으로 처리하지 않는다. |
| 잠금·다른 실행 자리·앞선 실행 가능한 요청 때문에 대기 | 요청과 기존 결과·소비 기록을 보존한다. 요청을 완료하거나 제한 재검사를 소비하지 않고 0이 아닌 결과를 반환한다. |
| executor의 `in_progress` | 같은 pending identity와 진행 snapshot을 보존하며 0이 아닌 결과로 다음 실행에서 이어간다. |
| executor의 `resolved` | completed 결과·snapshot을 보존하고 종료 코드 0을 반환한다. |
| executor의 `unresolved` | completed 결과·snapshot을 보존하고 0이 아닌 결과를 반환한다. 추가 재검사는 새 요청으로 접수한다. |

등록 프로젝트 scheduler는 worker connection에서 DDL 없이 pending을 접수 시각·삽입 순서로 읽고 실행 가능한 첫 요청을 선택한다. 즉시 명령도 같은 순서를 적용해 앞선 실행 가능 요청을 추월하지 않는다. 앞선 요청이 다른 소유자 때문에 대기하면 뒤의 실행 가능한 요청을 선택하고, 그런 요청도 없으면 일반 project runner를 호출하며 대기 요청을 보존한다. 프로젝트별 한 번의 scheduler 방문에서는 선택한 요청 하나를 처리한다.

즉시·지연 실행은 같은 executor에 저장된 요청 identity·계획 범위·현재 설정과 공유 Planner mutex·verify coordinator·runtime mutation·단계 알림 경계를 전달하고 결과·소비를 동일하게 기록한다. 완료한 요청은 다시 처리하지 않는다.

외부 executor 뒤 `complete_request()`·`record_progress()`는 DB 요청 하나에서 `request_id`, `repo`, `plan_id`, `pending` 상태를 확인하고 관측 당시 범위 snapshot과 현재 상태가 같을 때만 전후 snapshot·결과를 기록한다. 잠금 재시도마다 요청과 snapshot을 다시 읽으며 요청 identity·대상·상태가 달라졌으면 기록을 거부하고 pending 요청과 경쟁 변경을 보존한다. 재시도 한도 초과도 pending을 유지한다. 이 경계는 executor를 다시 호출하지 않으므로 Git·Agent·verify·게시·완료 사건은 DB 저장 재시도로 중복되지 않는다.

종료 코드 0은 요청 범위의 원인 해소와 복구 실행 종료를 뜻하며 전체 프로젝트 완료·검증 성공·Merge를 합성하지 않는다. 입력 거부·대기·진행 중·미해결을 성공으로 바꾸거나 별도 차단·실패·교정·승격 이력을 초기화하지 않는다.

## 공통 verify 실행 경계

공통 verify는 도구별 수집·결과 해석을 adapter에 맡기고 실행량·시간 상한·자식 종료와 결과 완결성을 공통 경계에서 확인한다. 선언된 필수 단계와 해당 실행 범위의 결과가 모두 성공으로 완결돼야 한다. 부분 실행의 성공 범위는 [검증과 재시도](#검증과-재시도)에서 구분한다.

### 선언·launcher·adapter 책임

| 책임 경계 | 소유하는 계약 |
|---|---|
| 선언 검사: `verification/contract.py` | `scripts/verify.json`의 version 1·정적 adapter registry·프로젝트 interpreter·수집 옵션·순서 있는 단계를 검증한다. |
| Manager 연결과 launcher | `manager_adapter`가 고정 runtime의 Python·절대 진입점·protocol version·run/session·timeout과 기존 인스턴스 coordinator를 전달하고 launcher가 프로젝트 interpreter를 선택한다. standalone도 같은 `entrypoint.py`·선언을 사용하며 로컬 관측만 조율한다. |
| 단계 실행과 공통 core | `runner.py`가 단계 순서를 유지하고 `core.py`가 도구별 결과 validator·worker callback을 받아 자원 배정·예약·프로세스 재사용·종료 확인을 수행한다. `worker_transport.py`는 공통 worker 생성·IPC와 직렬 예약 제한을 소유한다. unittest 타입이나 자체 compile 경로는 소유하지 않는다. 수집과 직렬 command에도 예약·전체 timeout을 적용한다. |
| unittest: `unittest_adapter.py` | 프로젝트 interpreter의 discovery/`load_tests`·재수집·fixture·native reporter와 명시한 Python compile 범위를 소유한다. 자식 import·Python 경로는 후보 worktree를 선택하며 Manager runtime 패키지가 후보 코드를 대신하지 않는다. |
| Perl: `perl_adapter.py` | 파일 suite 수집·재대조와 native TAP 결과 완결성을 소유한다. 정적 registry의 Perl executable은 Manager Python과 분리하고 공통 Python worker IPC·예약·종료 helper를 재사용한다. |

`result_validation.verify`는 선언이 없을 때만 기존 verify 경로를 실행한다. 잘못된 선언·미지원 version/adapter·실행 도구 부재는 연결 실패이며 기존 경로로 우회하지 않는다. 필수 필드·타입·경로와 단계 형식은 [선언 version 1](VERIFY.md#선언-version-1)을 따른다.

실제 시간은 `ExecutionClock`이 동일 검증의 누계 디렉터리에서 확인한 순번 대기만 제외해 계산한다. coordinator는 정규화한 예산 디렉터리별로 소속 session 전체의 공통 배정 대기를 집계하고, 해당 coordinator의 파일 하나에 `waiting_seconds` 누계를 기록한다. 모든 소속 session의 대기 상태와 1초 이내 heartbeat, 실행 중·종료 불명 예약 부재를 확인한 구간만 더하므로 겹친 session 시간이 중복 공제되지 않는다. 준비 중인 session·heartbeat 단절·활성 또는 종료 불명 예약은 공제를 막으며 다른 예산의 session은 분리한다. session을 종료해도 누계는 유지해 같은 coordinator의 다음 단계와 중첩 clock이 이전 대기를 다시 계산하거나 소비 예산을 초기화하지 않는다. 자체 필수 통합 단계의 상하위 관측 port도 이 공통 누계에 연결한다. Manager·launcher·runner와 Windows/WSL 단계가 이 누계를 공유하고 단계별 남은 예산을 전달하며 테스트 worker에는 조율 환경을 전달하지 않는다. 준비·수집·복사·I/O·실행·결과 처리·정리 시간은 그대로 계산한다. 자체 Windows launcher는 snapshot과 WSL 준비 Git의 종료를 확인하고 두 단계와 소유 복사본 정리 뒤에 예산을 다시 검사한다. 종료 불명 bundle과 예산 오류의 원본 누계는 보존하며 진단 복사나 정리 오류가 기존 실패를 지우지 않는다. 상세 연결은 [시간 예산](VERIFY.md#순번-대기와-시간-예산)을 따른다.

`integration.TrustedRunnerPort`는 고정 runtime 진입점만 실행하는 내부 상위 경계다. guard가 설치된 프로세스의 port 생성·재사용을 거부하고, Linux/Windows 종료 bootstrap과 최종 runner를 모두 isolated UTF-8 Python으로 시작해 후보 작업 디렉터리의 sitecustomize·`PYTHONPATH`를 로딩하지 않는다. 허용한 고정 bootstrap/argv가 아니면 시작 전에 거부하며 상속한 조율·부분 실행 값도 전달하지 않는다. 일반 command/테스트는 계속 `worker_environment()`를 거치며 관측 인증값도 제거한다. 고정 suite를 선언한 상위 runner만 orchestration을 소유하고, 수집·일반 command·예제 test worker에는 기존 guard를 적용한다. 자체 Windows 전체 실행은 기존 WSL 전체·native 핵심 회귀 경계를 사용하며 native 전체 runner의 직접 호출은 거부한다. native 하위 Job 연결로 자체 전체 실행을 대체하지 않는다.

`runner-integration` 선언은 unittest의 마지막 고정 `loop-manager-v1` suite만 허용한다. `integration.py`가 두 언어의 standalone/coordinated 정상·native 실패와 실제 partial/full을 실행하고, parent manifest의 고정 9개 case를 version 1 완료 receipt의 run·stage·revision·platform 및 고유 child run과 대조한다. 각 child의 compile/collection 완료·native report와 테스트 본문 결과·실제 예약 trace를 먼저 검증한다. fixture 오류나 timeout을 의도한 native 실패로 인정하지 않는다. 원래 결과·실행 로그·시작/조율/종료 근거는 child별 진단에 남기며 프로젝트 복사본은 확인된 종료 뒤에만 정리한다. `timing.summarize()`는 root journal/manifest의 run identity와 정확히 일치하는 이벤트만 집계하며, identity가 없는 중첩 fixture 기록도 제외해 하위의 기대 실패가 상위 결과나 fixture 의미를 덮지 않게 하고, 상위 summary의 `integrations`에 필수 단계 결과를 연결한다. 통합 자식의 `parent_run_id`는 상속한 pipeline 값 대신 직접 부모 run을 가리키고, 상위 runner의 기존 parent 연결은 유지한다. root 범위 안에서 phase/run 종료 우선·clock domain·assignment/module/child/partial/Windows 진단과 경과 시간 집계도 유지한다. 부분 실행은 tests에서 종료하고 통합 receipt를 만들지 않는다.

coordinator의 하위 session은 부모 ID·관측 전용 여부·PID/시작 identity에 결합한다. 상위 인증만 해당 하위를 등록·조회·종료할 수 있고, 배정 관측 전용 인증으로는 예약이나 session 생성을 할 수 없다. 상위가 프로세스를 기다리는 동안 `run_verify_process(on_wait=...)`가 0.1초 이하 간격으로 대기 heartbeat를 보내며 실제 예산으로 timeout과 후속 호출을 판정한다. 순수 대기 공제에는 모든 소속 session의 신선한 대기 상태·유효한 하위 시작 identity·활성/종료 불명 예약 부재가 필요하다. standalone 하위의 로컬 resource coordinator는 실제 배정 상태만 상위 관측 session으로 전달하고 로컬 누계 파일은 진단의 `local-budget/`로 분리한다. 상하위는 같은 누계를 사용해 서로 다른 monotonic 원점을 직접 빼지 않는다.

`LOOP_MANAGER_VERIFY_OBSERVER`는 `endpoint`(loopback host와 1–65535 정수 port), 비어 있지 않은 문자열 `session`·`secret`의 내부 JSON이며 자원 조율 환경과 혼용하면 거부한다. `runner-start.json` version 1은 session·PID와 시작 요청/identity 확인 상태를 기록해 후속 조율 조회 실패에도 근거를 남긴다. `coordination.json`은 부모가 관측한 session·PID/identity·예약/실제 acquire-bind-release trace·종료 확인을, `process.json`은 원래 passed·timed_out·exit_code를 보존하며 두 형식도 version 1이다. 조율 snapshot은 먼저 `stopped: false`로 보존하며, `finish_child`의 최종 성공 응답 뒤에만 true로 갱신한다. 하위 생존으로 인한 거부·응답 유실·유효하지 않은 응답은 종료 불명으로 처리하고, 기록을 true로 승격하거나 예제 복사본을 정리하지 않는다. 원래 process 결과가 성공이어도 이 최종 확인을 대신하지 못한다. `process.json`의 budget은 해당 호출의 wall/execution/waiting 시간과 전달한 남은 timeout을, 하위 run_end는 자체 elapsed 및 같은 관측을 기록한다. 검증은 공유 대기 누계가 일치하고 하위 기간이 상위 호출 구간 안에 있으며 실제 소비 예산이 상한 이내인지 확인한다. 서로 다른 monotonic 원점의 절대 시각은 비교하지 않는다. secret은 저장하지 않는다. 현재 trace는 고정 통합 fixture의 작은 범위를 전제로 기존 8 KiB IPC를 사용하며 임의 하위 suite로 확대하려면 streaming 진단이 필요하다.

프로세스 경계는 예외·취소·종료 불명에서도 임시 stdout을 닫기 전에 redaction한 console tail을 남긴다. 진단 저장 오류는 진행 중인 원래 예외에 부가해 종료 불명·취소 의미를 보존한다. port는 프로세스 경계의 `on_stopped` 통지 없이 일반 예외만으로 종료/미시작을 확정하지 않는다. 통지가 없으면 종료 불명으로 유지하며 runner도 본문 또는 최종 정리의 종료 불명에서 소유 예산 디렉터리를 보존한다. 종료 선언과 하위 runner/worker의 현재 시작 identity가 모순되면 해당 예약 트리를 종료 불명으로 보존한다. 부모의 확인된 종료 또는 미시작만 트리를 회수하며 독립 session은 유지한다. Manager adapter는 종료 판정 자체가 실패해도 `retained(parent)`의 소유 하위 예약을 `uncertain-reservations.json`에 남긴다.

`ExecutionClock`은 대기 JSON별 유한한 비음수 `waiting_seconds`를 관측하고 같은 파일 이름의 누계 감소·소실과 읽기/형식 오류·합계 overflow를 거부한다. 상한이 없어도 검증하며 `wsl-lifecycle.json`은 별도 종료 계약으로 분리한다. `VerifyBudgetError`는 파일 원본과 직전 관측값을 누계 디렉터리의 `error-*/`에 즉시 복사하고 같은 clock에서 오류를 유지한다. clock의 소유 누계 디렉터리를 보존하며 runner는 coordinator의 session 종료 갱신 전에 해당 오류 snapshot을 `budget-error/` 진단 경로로 복사한다. 진단 복사 실패도 원래 예산 오류와 함께 보고하고 소유 누계를 정리하지 않는다. runner의 최종 결과 처리와 외부 프로세스 트리 종료 확인 뒤에 남은 예산을 다시 검사하며, 최종 오류는 부분 PASS를 ERROR로 바꾼다. 외부 프로세스 경계는 이미 확인한 종료를 기억하므로 최종 예산 오류로 정지한 자식이나 Job에 종료 명령을 반복하지 않는다.

시간 상한을 전달하지 않는 구형 조율 호출은 runner·명령·프로세스/Windows Job 대기에서 바깥 Manager의 상한을 상속하며 새 기본 상한을 중첩하지 않는다. 명시적 상한과 단독 실행의 설정·기본 상한은 유지한다.

`perl_reporter.pl`은 공식 TAP::Parser로 테스트를 실행하고 즉시 flush하는 native JSON Lines(`*.native.ndjson`) 및 원본 TAP spool을 제공한다.

공통 진단은 EventWriter의 `*.jsonl`만 집계하며 native 원본의 별도 형식과 중단 자료를 보존한다. 파일 ID에 속한 동적 assertion 번호·plan과 parser/exit/signal을 대조해 완결된 suite만 완료로 기록한다. bailout은 plan 완료·정상 종료와 관계없이 suite 완료를 거부한다. nested subtest와 fixture 의미는 Test::More가 소유한다.

EventWriter 이벤트는 `clock_domain=sys.platform`과 run/stage/unit/worker identity를 보존한다. Windows launcher의 parent summary는 `result.json`·`snapshot.json`과 `<run>-wsl`·`<run>-windows`의 summary 경로·run ID·결과·elapsed를 연결하고 각 자식 실행의 clock domain 안에서만 시간을 계산한다. WSL run ID는 `parent_run_id`로 launcher run에 연결된다. native child에도 launcher의 parent run ID를 전달하고, parent는 일치하는 parent identity를 확인한 child summary에서 실제 native run ID를 가져온다. live WSL worker JSONL은 공유 bundle log에서 읽고 cleanup 뒤 형제 디렉터리로 보존한다.

미완료 이벤트의 clock domain이 현재 플랫폼과 다르거나 없는 legacy 기록이고 같은 실행의 종료 시각도 없으면 `elapsed`는 null로 보존한다. `timing.diagnostic()`은 이를 `경과 시간 미확인`으로 출력하고 대상 이벤트를 유지한다. 계산 가능한 시간은 초 단위 소수점 세 자리로 표시하며, 시간 미확인만으로 진단 생성을 실패시키거나 원래 verify 실패 결과의 처리를 막지 않는다.

새 `phase_start`·`phase_end` 쌍은 같은 32자리 소문자 16진수 UUID `phase_id`를 사용한다. `timing.summarize()`와 Manager progress reader는 변경될 수 있는 stage/unit identity 대신 이 ID로 phase 종료를 연결한다. ID가 없는 legacy 이벤트는 같은 run·clock domain·PID·phase 이름·시작 시각으로 연결한다. 따라서 `verify` phase가 stage ID 변경을 가로질러 FALLBACK·예외로 종료되고 `run_end`가 없어도 실행 중 항목으로 남지 않는다.

Manager progress reader는 현재 run의 완성된 줄만 한 번 소비하며 `test_interrupted`·stage 종료·run 종료를 파일 순서와 독립적으로 적용해 늦게 읽은 worker 시작이 active 표시를 되살리지 못하게 한다. 파일 읽기 실패는 미확인으로 남긴다. summary는 각 OS의 child metrics와 전체 parent elapsed를 분리해 PID나 monotonic clock을 합치지 않는다.

`LoopManagerVerifyGuard.pm`은 테스트/compile BEGIN 전에 일반 Perl process·fork·network 경계를 막으며 CORE/XS/threads 우회는 지원하지 않는다. 부분 결과는 native 로그에 보존하고 공통 core가 실제 worker 종료 후 예약을 회수한다. Perl은 unittest AST 부분 선정·시간 큐 재사용에 진입하지 않는다.

### 실행량·예약·시간 큐

`resources.decide()`는 최초 시작과 추가 실행에 동일한 CPU 여유 기준 `min(1.0, 허용 CPU 수 × 0.25)`를 적용한다. 여유는 허용 CPU 수에서 순간 부하와 1초 평균 부하 중 큰 값 및 미반영 CPU 예약을 뺀 값이며, 기준 이상이고 순간 부하가 허용 CPU의 95% 미만일 때 CPU 조건을 통과한다. 2코어에서는 두 경우 모두 0.5코어를 요구한다. 사용자·CPU 병렬도 상한, 메모리 보호, 관측 실패 시 직렬 축소와 예약·종료 확인은 별도로 유지한다.

core는 완료 report와 기존 자식/Windows Job 종료 조건을 먼저 확인하고, 매 acquire의 cap·공정성·미반영 CPU/메모리 예약 판단이 허용하는 pending 모듈을 연속 배정한다. 각 배정 뒤 전체 worker의 즉시 report를 다시 확인하고 수집 가능한 결과가 있으면 다음 acquire 전에 수집한다. 진행이 없거나 acquire가 거절되고 즉시 수집할 report가 없으면 50ms bounded 대기한다. report가 있어도 살아 있는 자식의 종료 확인을 생략하지 않는다. 시작 실패의 미시작 확인, PID bind, idle 전환과 실제 종료 뒤 release 경계는 유지한다.

결과 파일을 처음 관측한 시각, Job의 active child 대기·종료 확인, report 검증 후 module result, 같은 worker의 다음 unit 배정 시각을 JSONL로 연결한다. Job 대기 이벤트는 Windows Job의 active process 수가 worker만 남을 때까지 기다리는 구간을 나타낸다. coordinator의 `resource_wait`와 `worker_shutdown:<worker>` phase는 별도다. Job 상태 조회 오류는 종료 미확인 이벤트로 보존하고 idle·예약 회수 조건을 바꾸지 않는다. 다음 `unit_start`는 `worker_id`와 직전 unit을 포함해 결과 확인부터 재배정까지의 실제 경과와 allocation snapshot을 대조하게 한다.

runner의 실행 큐는 같은 진단 부모 디렉터리의 가장 최근 `summary.json`과 manifest·모듈 report를 읽는다. 큐 재사용은 실행 모드·선택 상태·수집 ID·fixture 소유자·pattern과 현재 단계 및 전체 실행 선언·프로젝트 Python·병렬도/조율·임시 경로·Python 경로·계측 버전이 같은 경우에만 허용한다.

과거 run이 성공·완결되며 종료 불명 예약이 없고 모든 실행 모듈의 단일 유효한 완료 phase duration과 report identity/coverage가 있을 때 독립 단계에서 긴 모듈부터 배정한다. 동률은 수집 순서이고 부재·손상·불완전·비교 불가능에는 전체 수집 순서를 유지한다. manifest는 원래 수집 순서와 NOT_SELECTED를 보존하며 serial과 단계/필수 명령 순서는 바꾸지 않는다. 이 큐 재사용 조건은 비용 비교보다 엄격하다.

`timing.comparison`은 같은 테스트 범위·부분/전체 모드와 환경·bytecode 정책·실행 context를 비교하되 context의 parallelism 값만 제외하므로 `1`과 `auto` 실행을 대조할 수 있다. 과거 시간은 비용 자료이며 현재 성공이나 실행 생략의 근거가 아니다.

### 실제 Agent 호출 차단과 자식 환경

`verification/guard.py`는 interpreter의 `sys`에 둔 설치 근거·lock을 `sitecustomize`의 runpy 로딩과 후보 package import가 공유한다. 실제 audit probe로 hook 등록을 확인하고 wrapper 함수 identity를 대조한다. wrapper 설치 실패 뒤 재진입은 확인된 hook을 재사용하며 완료되지 않은 설치를 성공으로 간주하지 않는다.

Python이 sitecustomize 예외 뒤 실행을 계속하는 것을 막기 위해 설치 실패는 비밀값 없는 고정 진단과 비정상 종료로 처리한다. 가드 원본의 위치는 자식 보호 경로를 정하고 환경의 프로젝트 root는 후보 package 선택을 정한다.

Popen wrapper는 호출자 환경을 복사·정제하고 해당 호출의 thread-local 범위에 전달 dict identity와 내용 사본을 보관한다. audit는 같은 dict·내용일 때만 준비를 생략하고 변경되거나 직접 전달된 환경은 정제한다. 성공·시작 실패·중단 뒤 호출 범위를 복원하며 명령/API 검사는 매 실행 경계에서 수행한다. 환경의 Windows 대소문자·POSIX bytes 별칭을 처리해 조율 인증값을 제외하고 marker·가드 경로·후보 root·bytecode 정책을 유지한다.

이 설치 근거와 호출 범위는 안전 판단 cache나 새 환경 원본이 아니며 임의 언어·모든 외부 subprocess의 보호를 보장하지 않는다. 예약·종료 확인 책임은 기존 core/프로세스 경계에 남는다.

### 결과 완결성과 지원 계약

manifest·이벤트·결과는 version·run·단계·단위/테스트 identity를 결합하고 adapter가 예상 범위·fixture skip 근거·결과 완결성을 대조한다. command 종료 코드는 필수 단계 결과이며 테스트별 결과를 합성하지 않는다. 예약은 session·단위·PID/시작 identity에 결합하고 확인된 종료/미시작만 회수한다. 중단 자료와 종료 불명 예약 journal을 보존하고 다른 run은 제어하지 않는다. 단계별 이벤트를 전체 진단에 집계한다. 성공 재사용의 채택 기준은 R-072의 저장소 코드 입력이며 외부 runtime·동적 입력의 미확인을 별도 거부 조건으로 두지 않는다. 결과 완결성·실제 전체 run·receipt 출처 검증은 유지한다.

상세 호출은 [실행 계약](VERIFY.md#실행), 도구별 지원·적합성은 [unittest](VERIFY.md#unittest-지원과-적합성)·[Perl TAP](VERIFY.md#perl-tap-지원과-두-언어-예제), 새 도구 연결은 [adapter 추가 경계](VERIFY.md#adapter-추가-경계)를 따른다. 지원 계약과 검증된 환경의 범위를 구분하며 미확인 연결을 검증 완료로 간주하지 않는다.

역할별 정적 규칙은 현재 runtime의 common+해당 역할 원본으로 전달하고, 작업 prompt는 선택 선언·지원 버전·참조 문맥만 연결한다. 언어별 지침 사본이나 새로운 배포·snapshot 경계는 만들지 않는다.

## 검증과 재시도

검증 근거는 실행한 범위와 판단 주체에 따라 구분하며, 한 단계의 성공으로 다른 단계의 완료를 대신하지 않는다.

| 근거 | 성공이 뜻하는 범위·다음 gate |
|---|---|
| Worker targeted unit test | 수정 범위에 직접 관련된 최소 unit test의 결과다. 전체 검증을 대신하지 않는다. |
| Manager 부분 verify | 선정한 모듈만의 결과다. 미선택은 NOT_SELECTED이며 전체 성공 token·receipt를 만들지 않는다. |
| Manager 실제 전체 verify | 현재 verify의 전체 테스트·필수 command 결과와 입력을 결합한다. 성공 receipt와 Review·게시 gate는 별도로 확인한다. |
| 전체 verify 성공 receipt 재사용 | 같은 작업 주기의 검증된 입력을 재관측해 원래 성공 run과 연결한다. 일반 선행 Review PASS 뒤에는 실제 전체 실행이 필요하다. 상세 조건은 [성공 receipt와 수동 면제](#성공-receipt와-수동-면제)를 따른다. |
| Reviewer | 실제 검증 범위와 전체 변경을 읽기 전용으로 판단한다. 일반 선행 PASS의 최종 승격은 실제 전체 verify와 동일 입력·호출 근거를 요구하며 [일반 선행 Review와 부분 검증 증거](#일반-선행-review와-부분-검증-증거)를 따른다. |

현재 문서 보고 교정은 [R-087](requirements/%5BR-087%5D%20문서%20보고%20교정%20반복%20제한.md)의 계약을 적용한다. 후보 커밋·최신 base 조정 뒤 전체/부분 검증과 성공 receipt 재사용 판단 전에 최종 변경 문서 전체의 보고 누락·형식·hash 오류를 한 번에 검사하고 게시 직전에도 다시 확인한다.

Worker 교정의 반복 식별은 후보 HEAD·index·dirty 내용 지문, 현재 명세 입력과 base로 결합하며 검증 출력·Agent 세션은 진전으로 보지 않는다. 보고 교정 후에도 verify token의 출력 결합과 입력 재관측·receipt·Review gate를 유지한다.

### 공통 결함 보고의 감사 보존

`ValidationFeedbackRepository`는 결과 감사와 공통 결함 관측을 같은 transaction에 추가한다. `common_defect_reports.result_id`는 불변 `validation_results`를 참조하며 프로젝트·작업·명세 revision·단계·원래 판정과 정제된 원문을 연결한다. 일반 실행에서 보고와 결과의 수정·삭제는 거부한다. 이후 verify 성공으로 활성 피드백이 해소돼도 과거 보고는 유지한다. 확인된 작업 철회의 최종 기록 제거는 아래 공유 관계 보호와 기존 원자 제거 transaction을 통과한 경우의 별도 예외다.

최종 제거는 불변 감사 trigger를 해제하기 전에 `common_defect_state.assert_plan_erasure_available`로 공유 연결을 대조한다. 현재 연결은 기존 소비자와 같이 `(repo, result_id, goal_key)`별 마지막 채택 row로 판단하며, 같은 결과의 다른 목표에 새 연결이 있다는 이유로 대체됐다고 보지 않는다. 삭제 대상이 보고 소유자이거나 교정 작업인 양쪽 관계에서, 미완료·미취소 독립 작업이 현재 연결을 필요로 하면 `CallDeferred`로 해당 삭제를 보류한다. 같은 목표의 후속 채택 연결·독립 작업의 실제 MERGED 상태/교정 Merge 영수증·취소 표식을 확인한 뒤 더 이상 필요한 연결을 잃지 않는 범위만 제거한다. 원본 결과 부재·repository 불일치는 귀속 미확인으로 보존한다.

삭제 대상 자체의 `common_defect_merges` 영수증은 실행 row가 없어도 최종 제거를 거부한다. 이 보호는 단순 Worker 완료나 PR close를 Merge로 취급하지 않는다. 공유 관계 확인은 같은 SQLite 제거 transaction의 읽기 전용 경계에서 수행하며, 허용 뒤 기존 대상별 결과·보고·연결의 제거와 trigger 복원만 진행한다. 독립 원본 결과·현재 후속 연결·다른 repository의 Merge 근거는 변경하거나 삭제 대상의 새 사본으로 복제하지 않는다.

선택적 `COMMON_DEFECT_REPORT:` JSON은 `problem`, `scope`, `evidence`, `locations`만 받는다. 앞의 세 필드는 비어 있지 않은 2,000자 이하 문자열이고 `locations`는 1~8개의 `{path, line}` 객체다. `path`는 traversal·빈 구성 요소·역슬래시·콜론·줄바꿈·탭이 없는 저장소 상대 경로이며 `line`은 1~2,147,483,647의 정수다. 전체 JSON은 UTF-8 12,000바이트 이하이며 중복 선언·중복 key·추가 필드·잘못된 타입은 거부한다. 기존 결과 포맷 정규화와 비밀값 정제를 재사용하며 거부 시 원본 결과와 비밀값을 포함하지 않는 형식 오류를 함께 보존한다. 선택적 보고 오류로 기존 verify·Review 판정을 변경하지 않는다.

`scope`·진단 위치·근거는 보고자의 미확정 관측이다. 저장 시 파일 존재·원인 동일성·명세 인용의 유효성을 확정하지 않으며 서로 다른 결과의 같은 보고도 합치지 않는다. 이 저장 경계는 Planner 호출·계획 채택·차단 해제·검증 성공을 만들지 않는다.

`validation_results.observation`은 Manager가 확보한 문자열 값의 JSON 객체다. 선택 `plan_path`·줄바꿈으로 구분한 `sources`·`remote_revision`·`branch`와 확인 가능한 `candidate_head`, 직렬화된 `candidate_snapshot`, `invocation_id`, `session_id`를 담으며 알 수 없는 필드는 생략한다. 기존 감사 행에는 빈 객체만 추가하고 과거 identity를 만들지 않는다. Worker 보고는 CLI 반환 직후 실제 invocation·session과 반환 시 작업 형상을 연결한다. 후보 관측 실패는 `candidate_observation_error`로 남겨 원래 보고를 보존하며 후보 성공을 추정하지 않는다. Reviewer의 반환 실패·범위/프로토콜 거부·최종 판정과 verify의 원래 결과도 확보한 문맥을 함께 보존한다. 원래 사용자 질문·실패는 결과 원문에 남으며 관측 저장은 기존 결과 해석이나 교정 전이를 대체하지 않는다.

일반 planning은 기존 호출 판단을 통과한 새 turn의 prompt 조립에서만 해당 프로젝트 보고를 읽는다. DB 조회 후 `common-defect-evidence/<SHA-256>.json`에 version 1의 `{version, reports}` snapshot을 원자 교체로 내보내며 경로·digest·보고 수만 현재 catalog prompt에 더한다. snapshot은 보고별 원본 결과·관측 문맥·정상 보고 또는 거부 이유와 기존 채택 연결을 보존한다. 실제 전달 보고 ID는 해당 revision의 planning 검토 attempt에 결합하며 새 호출에서는 이전 결과·연결 제안을 초기화하고 이번 전달 범위를 기록한다. 성공한 general/final receipt와 원본 보고는 보존한다. DB 쓰기 잠금 안에서 파일/Git을 관측하지 않으며 export 실패·결과 재처리에도 보고를 소비·삭제하지 않는다. 새 보고·코드 변경에 따른 호출 조건·polling은 추가하지 않는다.

`COMMON_DEFECT_LINKS`는 `{revision, links}` 형식의 선택적 Planner 결과다. 각 연결은 `reports`(이번 전달 범위의 유효 정수 ID 배열), `plan_id`·`plan_path`, `source`·`quote`, `cause`·`completion`, `code`(`{path, quote}` 배열 1~8개)를 갖는다. 연결은 64개 이하, JSON은 UTF-8 64,000바이트 이하이며 문자열은 비어 있지 않은 4,000자 이하 값이다. 중복 key·중복 보고/동일 목표 연결·추가 필드·잘못된 타입·경로·비밀값은 거부한다. source는 교정 계획이 소유한 현재 PRODUCT 또는 활성 Requirement이고 명세·코드 인용은 고정한 검토 revision의 실제 원문과 대조한다. Requirement rename은 기존 resolver의 고유 활성 R-ID 조회를 재사용하고 source 문자열을 유지하며 archive만 남거나 모호한 입력으로 새 연결을 채택하지 않는다. 계획은 현재 catalog 또는 검증된 생성/개정 결과에 있어야 하며 삭제 결과는 제외한다. ID 재배정은 원래 ID와 경로를 함께 대조해 최종 ID·경로로 결합한다. 이 인용/identity 검증은 Planner의 원인 의미 판단을 대신하지 않는다.

검증된 연결 제안은 기존 planning review receipt에 저장하고 일반 publication 또는 문서 변경 없는 확인의 원격/catalog 확정 transaction에서만 `common_defect_links`에 채택한다. 채택 직전에 대상 계획의 경로·소유 source·정의 revision을 다시 확인하며 새/개정 계획은 실제 게시 revision과 일치해야 한다. source·원인·완료 조건의 명시적 목표 identity가 같으면 READY·draft·미Merge PR을 포함한 현재 소유 계획을 재사용한다. 다른 원인·미확정 보고는 자동 합치지 않는다. 연결 감사는 원본 result ID·목표·교정 계획·검토/게시 revision·인용 근거를 불변으로 유지하며 동일 게시 재처리는 멱등이다. 게시 전 실패·DB rollback·catalog 경쟁에서는 제안과 원래 보고를 보존하고 채택을 확정하지 않는다. 계획 연결은 실제 코드 교정 Merge·필수 검증 또는 차단 해제의 근거와 구분한다.

`common_defect_pending` Worker 결과는 kind 외 필드 없이 유효한 `COMMON_DEFECT_REPORT`를 요구하고 완료 publication 선언을 거부한다. Manager는 `common_defect` 원인의 `condition_wait`로 해당 계획만 보존하며 사용자 판단과 구분한다. `common_defect_pending` 차단만 이 disposition의 저장을 허용하고 다른 승인/안전 차단에는 적용하지 않는다. Worker의 명시적 `needs_user_decision`과 저장 질문·draft·User Decision은 변환하지 않으며 알림 정책은 공통 교정 대기를 사용자 조치로 선택하지 않는다.

`common_defect_merges`는 실제 PR Merge를 관측한 Manager의 완료 transaction에서 PR URL·후보 HEAD·`mergeCommit` OID와 검증 근거를 불변으로 기록한다. 일반 PR 조회도 mergeCommit을 요청하며 squash Merge의 원래 PR HEAD와 실제 Merge OID를 구분한다. 검증 근거는 같은 프로젝트·계획·실행 주기의 전체 run receipt 또는 확인된 재사용과 원래 실제 run, 후보/출력 verify token·PASS review token·정의 digest를 대조한다. `verification`은 이 receipt/origin ID·run/reuse 구분·token·정의 SHA-256·검증 base를 담은 JSON이며 불명이면 JSON null이다. manual 묶음은 `manual_work_publications`의 같은 request·PR·HEAD·base와 전체 구성원별 PASS를 원래 `manual_work_results`에 대조한다. 각 G의 명시적인 개별 Review PASS와 공유 후보를 실제로 실행한 구성원의 native 전체 run/reuse·Review token을 함께 확인하며, 구성원별 자동 token의 부재를 실패로 간주하지 않는다. 묶음 proof는 group/member/execution-plan ID와 게시 영수증 digest를 추가로 보존한다. 사용자 생략이나 구성원 누락·영수증 불일치는 근거로 인정하지 않는다. archive 문서에 근거한 MERGED 상태·면제·부분/미완료/실패·다른 작업의 receipt·후보 불일치·Merge OID 불명은 검증된 교정 반영을 만들지 않는다. 외부 Git·파일·모델 관측을 DB 쓰기 잠금 안에서 하지 않으며 근거 수집이 새 전체 verify 실행을 만들지 않는다.

재평가 입력은 해당 작업의 현재 Worker 원문에 일치하는 최신 보고와 활성 validation feedback의 원본 result에 연결된 목표만 선택한다. 차단 복구의 rebase가 후보 결과를 초기화한 경우에는 현재 `common_defect_pending` 차단 행 전체와 계획 경로가 일치하는 저장된 reconciliation의 원래 Worker 원문을 사용해 같은 불변 result를 조회한다. 후보의 verify·Review 무효화와 원래 실패의 교정 연결을 구분한다. 이후 원격·planning 입력으로 같은 차단의 reconciliation을 다시 만들 때에도, 현재 비어 있는 결과는 동일 차단 행·계획 경로에 고정된 원래 결과로 보존한다. 현재의 새 결과를 과거 결과로 덮지 않으며 다른 차단 원인·계획이나 불명확한 저장 문맥에는 과거 보고를 붙이지 않는다. 피드백 event가 없는 verify/Review 거부도 해당 차단 단계의 최신 원본 결과에 보고가 있을 때만 연결하며, 뒤의 다른 실패에 과거 보고를 붙이지 않는다. 보고·목표별 최신 채택 연결과 실제 Merge/검증 근거를 기존 차단 reconciliation의 input identity에 포함해 반영 뒤 영향 작업만 다시 검사한다. 같은 근거의 무진전 재호출은 기존 완료 관측과 assessment cache로 억제한다.

Scheduler 실행의 프로젝트 준비 단계는 workspace 소유권 hold 복구를 유지하고, 일반 차단의 원인 평가·입력 통합·재개는 대상별 작업 coordinator에 맡긴다. 프로젝트 스케줄러의 최상위 planning 결과가 `WAITING`이어도 작업 coordinator를 실행한다. 실행 가능한 READY와 활성 작업이 모두 없어도 차단 복구를 검사하며, planning의 dependency 대기를 복구 검사 생략으로 소비하지 않는다. 복구와 신규 선택은 각자의 기존 planning 소유권·유지보수 범위·dependency·배정 한도를 유지한다. 준비 단계의 직렬 재평가가 기존 활성 작업을 다시 가로막거나 같은 복구를 중복 실행하지 않는다. Scheduler 없는 기존 실행과 명시적 로컬 복구는 해당 경로를 유지한다. 작업 coordinator는 같은 원인·개정·확정 planning 입력의 차단 재검사를 실행당 한 번으로 유지한다. 다른 작업이 관리 한도를 채웠다는 이유로 완료한 재검사 표시를 지우지 않으며, 원인이 남으면 입력 변경이나 다음 정상 실행에서 다시 확인한다.

교정 계획이 연결됐지만 Merge·검증이 미충족이면 보류하며 평가/적용 직전 같은 원인·후보·checkpoint generation·소유권·공통 교정 입력을 다시 확인한다. 다른 프로젝트·계획은 기존 lease·배정 경계를 유지한다.

확정 연결이 차단 작업 자신을 교정 소유자로 지정하면, 현재 경로·source·개정이 채택된 대상과 일치하고 보고 당시 정의에서 전진했는지 확인한다. 모든 현재 보고가 연결되고 다른 미반영 교정이 없을 때만 `owner_execution`으로 기존 reconciliation의 원격·소유권·후보·generation/CAS와 Worker 재검증 전환을 사용한다. 이 경로는 구현/verify/Review 교정 차단만 실행으로 전환하며 사용자 질문·독립 차단과 완료 판정을 유지한다. `ready`와 Merge 근거는 생성하지 않는다.

공통 교정과 기존 사용자 질문의 의미 판단은 G-319의 `_assess_user_decision_cause` reader·원격 재확인·CAS를 함께 사용한다. 연결된 교정의 Merge OID가 현재 원격 이력에 있어야 하며 원래 질문/실패·관련 상위 명세·현재/이전 원인 코드와 실제 부재·검증 근거를 전달한다. 문서는 기존 의미 정규화를 사용하고 코드는 들여쓰기·리터럴 공백도 입력이므로 원문으로 비교한다. 공통 교정 해소에는 현재 상위 명세 인용과 존재하는 원인 코드 인용을 요구하며, 원인 코드가 전부 삭제됐으면 실제 부재를 보존해 명세와 대조한다. 기존 사용자 질문도 현재 상위 명세가 구체적 선택을 해소한 인용이 있어야 한다. 교정으로 원인이 제거되면 명세 원문이 그대로여도 평가할 수 있지만 Merge·코드 인용만으로 사용자 선택을 승인하지 않는다. `common_defect_assessment`와 기존 `user_decision_assessment`의 질문·입력·인용·history를 구분해 보존하며 불확실한 결과는 해제하지 않는다. 해소 뒤 기존 reconciliation이 작업물·이력을 보존하고 Worker부터 재검증하므로 교정의 성공을 영향 작업의 성공으로 넘기지 않는다.

교정 계획 연결이 없는 현재 공통 결함은 `validation_results`의 현재 Worker 원문 또는 활성 feedback에 결합된 불변 `common_defect_reports`를 입력으로 사용한다. 코드 비교 기준은 해당 보고의 `definition_revision`이며, rebase 후 `active_sha`로 바꾸지 않는다. 보고의 관련 코드 변경·부재가 있을 때만 읽기 전용 원인 재평가를 수행하고 현재 코드·상위 명세 인용과 입력 identity를 검증한다. 인용의 `source`는 전달한 `sources`의 저장소 상대 경로를 정확히 사용하고 행 번호·링크를 붙이지 않으며, `quote`는 해당 현재 원문의 실제 부분 문자열이어야 한다. 추가 파일의 설명은 판정 detail과 구분하고 미확인 인용으로 해소하지 않는다. 연결된 교정 계획의 실제 Merge·검증 gate는 그대로 적용한다. 입력이 바뀌거나 판정이 불확실하면 차단을 유지한다. 공통 구현 결함의 읽기 전용 재평가는 현재 원격의 상위 명세를 읽으며 후속 일반 planning의 명세 반영 확정만을 기다리지 않는다. 실제 진행 중인 planning과 후보·소유권 경계는 유지하고, 기존 사용자 결정 차단은 후속 명세의 planning 확정을 계속 요구한다.

재평가와 해제는 기존 `BlockedPlanReconciler`의 현재 소유권·원격·checkpoint 검사를 통과해야 한다. 공통 결함 대기와 소유 rebase 충돌이 겹치면 rebase 완료 전에 현재 원인을 읽기 전용으로 재평가한다. 해소가 확인된 경우에만 기존 `resume_rebase_block`의 재관측·배정·원인·입력·generation 검사로 차단을 해제하고, 원래 HEAD·target·충돌을 보존해 Worker가 남은 해결을 이어간다. `common_defect_assessment`와 이력은 checkpoint 갱신에서도 보존하며 다음 Worker의 context delta에 전달한다. 현재 원인 해소 판단은 과거 실패 감사를 지우거나 후보의 verify·Review 성공을 생성하지 않는다. 같은 입력의 재평가는 재사용하고 재개 후 현재 후보의 필수 검증을 이어간다.

### targeted test와 실패 수집

- Worker는 구현과 관련 targeted unit test를 작성하고, 수정 범위에 직접 관련된 최소 unit test command만 실행한다
- targeted unit test 실패는 Worker가 같은 세션에서 수정하고 재실행한다. 프로젝트 Python과 Manager runtime의 `verification/targeted.py`는 실행별 ID·기록 폴더와 JUnit의 안정적인 classname·name·파라미터 ID를 결합해 native 개별 결과를 보존한다. unittest subTest는 상위 `test.id()`의 최종 결과 하나로 집계한다. unittest 본문의 failure·error와 `unexpected_success`는 실패이며, 성공·skip·expected failure만 명시된 초기화 결과다. `TestCase`와 `IsolatedAsyncioTestCase`의 표준 `_callSetUp`·`_callTearDown` lifecycle 경로에서 callback의 원래 예외 identity를 대응시켜 동기·비동기 fixture 오류를 `fixture_error`로 남긴다. lifecycle callback이 실행 중임을 추적해 `addSubTest()`가 삼킨 setup·teardown subTest assertion도 해당 fixture에 귀속하며, native subTest 결과와 scope를 함께 기록한다. 본문 subTest는 상위 failure로 유지하고 본문과 teardown이 모두 실패하면 본문 failure 한 건과 별도 fixture 증거를 보존한다. class/module teardown과 같이 테스트 identity에 안전하게 귀속할 수 없는 fixture 오류가 동반된 성공 결과는 `indeterminate`로 관측한다. 수집·중단에는 개별 결과를 만들지 않는다.

  실행 가능한 관련 unit test가 없는 변경은 사유를 남기며 전체 검증을 생략하는 근거로 사용하지 않는다

- 실행 전 `targeted_test_collections`에 물리 invocation과 기록 디렉터리·파일시스템 identity·process receipt 경로를 결합한다. invocation ledger가 repository·ExecPlan·Worker 역할·guidance 작업·scope와 실제 provider/model을 소유한다. 기록 경로는 기존 `state/agent-runtime/<runtime>/<call>`이고 receipt는 쓰기 허용 call 바깥의 `<call>.process.json`이다. 정규 경로·기존 디렉터리·Worker 소유권과 호출별 경로의 유일성을 확인하며 파일시스템 device/inode는 Windows의 큰 식별자도 보존하도록 십진 문자열로 저장한다. 이 additive 테이블이 없는 과거 호출의 기록 위치를 디렉터리 탐색으로 추측하지 않는다.
- invocation 종료의 finally, Manager의 `reconcile_interrupted()`와 Worker 호출 전·CLI retry 전·호출 후 경계가 같은 미소비 기록을 재관측한다. 기존 process receipt 복구로 writer 종료가 확인되고 디렉터리 identity가 일치할 때만 읽는다. 살아 있는 수동 작업은 기존 소유권 보호를 적용한다. receipt 부재·종료 불명·경로 교체·symlink·읽기/해석/저장 오류에서는 원문·결합을 보존하고 해당 작업을 대기한다. 같은 작업의 앞선 호출이 대기하면 후속 호출도 순서를 보존하며 다른 repository·ExecPlan의 수집은 이어간다. runtime·모델 교체는 저장된 호출 귀속을 바꾸지 않는다. `AgentRequest.on_targeted_prepared`는 launch 객체 없이 같은 디렉터리·receipt를 결합하는 port다. 관리 Claude는 native launch의 owned attempt와 receipt를 `on_launch_prepared`로 결합해 같은 수집 경계를 사용한다.
- targeted JSONL의 `phase_start`·`phase_end`·`git_cost`·`git_cost_incomplete`는 시간·Git 비용 진단으로 수집 대상 결과에서 제외한다. 이 진단의 run/version 부재를 개별 테스트 기록 손상으로 취급하지 않으며, 테스트 결과·fixture·실행 오류에는 기존 run identity·version 검증을 유지한다. 원본 진단은 삭제하지 않는다.
- Manager는 완료된 개별 결과를 SQLite `targeted_test_results`·`targeted_test_streaks`에 반영하고, 실행 ID의 invocation 귀속(`targeted_test_run_owners`)과 collection의 `collected` 전이를 같은 transaction에서 확정한다. 미소비 상태는 `pending` 또는 오류 종류를 가진 `error`이며 저장 실패·transaction 중단 뒤에도 재시도한다. repository·ExecPlan·실행 ID·테스트 ID로 중복과 상충 결과를 검사하고 다른 호출의 실행 ID 재사용은 거부한다. `failure`·`error`·`unexpected_success`는 누적하고 `success`·`skip`·`expected_failure`는 초기화한다. `fixture_error`·`indeterminate`는 감사 결과로 기록하되 기존 streak를 늘리거나 초기화하지 않는다. JSONL의 불완전한 마지막 줄과 기록 부재에서 성공·실패를 합성하지 않으며 수집·fixture 오류도 개별 테스트 실패로 세지 않는다. JUnit `<failure>`는 native 실패로, `<skipped>`는 skip으로 기록한다. fixture 여부를 구별할 원본 근거가 없는 `<error>`는 `indeterminate`이며, suite 오류와 명령 종료 코드는 별도 실행 오류로 남겨 개별 테스트 실패를 합성하지 않는다. 기존 저장 결과는 이 분류로 소급 변경하지 않는다. 연속 다섯 번째 실패에 도달하면 새 Worker·CLI retry·verify 전에 `targeted_test_exhausted`로 차단한다. 이후 같은 테스트의 `success`·`skip`·`expected_failure`를 관측하면 실패 횟수와 도달 표식을 함께 초기화한다.

  차단 확정과 도달 표식 소비는 같은 transaction이며 실행·실패 감사는 보존한다. Worker 최종 실패 보고는 `targeted_test_correction`, 수집 대기는 `targeted_collection_pending`으로 구별해 일반·rebase 보고 오류 한도를 소비하지 않는다. 근거 없는 실패 보고는 작업별 재시도 대기 뒤 재검증하고 수집 대기는 기존 자료를 먼저 재수집한다. 수집 대기는 별도 반환 표식으로 전달하며 물리 provider 결과·사용량·종료 감사를 덮지 않는다. 실제 provider 거부·완료 보고 오류·명시적 사용자 결정과 독립 안전 차단은 각 기존 경계에서 처리한다.

- 저장된 VERIFYING 진입은 선택된 repository·ExecPlan의 자료를 Worker 결과·사용자 결정 gate 뒤, 부분 실행·전체 실행·성공 receipt 재사용 전에 다시 관측한다. 기존 Worker 차단 경계가 다섯 번째 실패와 도달 표식 소비를 원자 처리하며, 미수집 자료는 `targeted_collection_pending` 조건 대기로 VERIFYING·Worker 결과·session·후보를 보존한다. 부분·전체 실행은 `scheduled_call("verify")`가 실제 verify slot을 배정한 뒤 process receipt 준비 전에 같은 자료를 재확인한다. 이 경계에서 차단·대기가 생기면 아직 자식 receipt가 없어 기존 task-call 확인 규칙이 미시작 verify 자리만 반환하고 새 verify 실행·실패 receipt를 만들지 않는다. 작업별 repository·ExecPlan 필터를 유지해 다른 작업의 기록·예약은 건드리지 않는다.

### 전체 verify와 Reviewer

- Worker는 현재 운영체제용 verify 스크립트가 없으면 생성하고 테스트 명령이 바뀌면 갱신한다
  - Windows: `scripts/verify.ps1`
  - Linux: `scripts/verify.sh`
- verify 스크립트는 프로젝트 전체 검증을 수행하고 실패 시 0이 아닌 종료 코드를 반환한다
- 자체 Windows verify의 `LOOP_MANAGER_VERIFY_TEMP_DIR` 입력은 launcher 실행 시 선택할 임시 경로다. 미지정이면 `%USERPROFILE%\.cache\loop-manager\verify\tmp\`를 쓰고, 상대 경로는 checkout을 기준으로 절대화한다. Python 조회·실행보다 먼저 경로를 생성하고 쓰기 가능성을 확인한 뒤 `TEMP`·`TMP`·`TMPDIR`에 전달한다. 드라이버와 native 자식은 Python의 실제 `tempfile.gettempdir()`가 선택 경로와 일치하는지 확인하고, launcher `result.json`, native `summary.json`, WSL `summary.json`에 각각의 실효 경로를 기록한다. WSL의 전용 `/tmp/` 경로는 Windows 선택값과 분리하며, 부분 요청 FALLBACK에는 launcher 경로만 기록하고 미실행 단계는 비워 둔다. 경로 선택·사용 실패는 fallback 없이 verify 실패이며 종료 시 기존 환경·cwd를 복원한다. 병렬 실행의 파일을 보존하기 위해 공유 임시 폴더 전체를 삭제하지 않는다. Defender 설정은 스크립트가 변경하지 않고 [운영 안내](OPERATIONS.md#windows-defender와-테스트-임시-폴더)에 따라 사용자가 선택한다
- 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check는 verify 스크립트만 실행한다
- 프로그램은 verify 스크립트를 AI Agent 밖에서 실행한다
- 프로그램은 verify 성공 시 Worker를 다시 호출하거나 성공 출력을 전달하지 않는다
- verify 실패 시에만 종료 코드와 필요한 출력 내용을 같은 Worker 세션에 전달한다
- Reviewer는 Worker targeted unit test와 Manager 검증 결과·실제 범위를 넘겨받고 테스트를 다시 실행하지 않는다. 일반 선행 경로의 부분 결과는 전체 verify 성공을 뜻하지 않는다
- Reviewer는 코드와 테스트의 diff 및 verify 스크립트 변경이 검증 범위를 약화하지 않았는지 읽기 전용으로 확인한다

### 독립된 실패 이력과 CLI 재시도

실패 집계는 원인별로 분리한다. 최종 반복·일시 중단·BLOCKED와 해제 조건은 PRODUCT의 각 실패 정책이 소유한다.

| 실패 근거 | 집계·초기화 경계 |
|---|---|
| [targeted unit test](PRODUCT.md#worker-targeted-unit-test-실패) | 실제 실행·테스트 ID별 연속 결과다. 후보·session 변화로 초기화하지 않으며 연속 다섯 번째 실패로 차단하며 성공·skip·예상된 실패는 실패 횟수와 도달 표식을 함께 초기화한다. |
| [전체 verify](PRODUCT.md#전체-verify-실패) | 정규화한 verify 실패 지문별 발생 이력이다. 전체 verify 성공은 이 이력만 초기화한다. |
| [Review 교정](PRODUCT.md#review-교정과-level-승격) | Review 지문·level 승격 이력과 실제 시작한 Worker 교정 turn과 세 번씩의 묶음 대기를 별도로 저장한다. 유효 Review PASS는 Review 이력·진행 중 교정 집계만 끝내며 effective level·과거 승격 감사는 유지한다. |
| Worker 완료·문서 보고 교정 | 기존 보고 오류 한도를 사용한다. targeted test 교정은 `targeted_test_correction`으로 분리하고 동일 후보·명세·base의 보고 오류 집계는 검증 출력·session 변화로 초기화하지 않는다. |

[Agent CLI 실패](PRODUCT.md#외부-cli-실패)는 위 구현·검증 실패와 별도로 처리한다.

Agent CLI 실행 실패·timeout·일시적 서비스 오류는 한 호출 요청에서 최초 실행 외 최대 3회 재시도한다. 세션 재개 불가로 새 세션을 한 번 시작하는 복구는 별도로 집계한다. 묶음이 소진되면 recovery record의 identity/generation에 결합한 `retry_after`를 저장한다. 작업별 30초 대기는 호출 자리를 점유하지 않고 pause·확정된 handoff 신호를 확인하며 같은 실행에서 저장 단계를 다시 배정한다. runtime 원격 관측은 대기 해소 뒤 실제 단계 진입에서만 수행한다. runtime 교체 후에도 저장된 시각과 누적 감사를 유지한다. Review 교정은 시작한 turn만 세며 같은 피드백 turn의 예약·재처리는 중복 집계하지 않는다. 교정 후보 조회는 취소/최종 삭제 ID이면 None을 반환하며, 예약·시작은 외부 run 기록이 없는 호환 경로도 같은 ID를 확인한다. 저장 값을 쓰는 예약·시작과 retry deadline 생성/소비는 공통 writer 예약 뒤 ID를 다시 읽고, 자격 소멸은 기존 RecoveryStateConflict로 거부해 turn·deadline·감사를 유지한다. scheduler는 재시도 대기 진입과 각 대기 반복에서 ID를 확인하고, 저장/소비 경합도 현재 취소가 확인될 때만 False로 반환한다. 이 반환은 재시도 완료가 아니며 취소 외의 generation/identity 경합은 기존 오류를 그대로 유지한다. AI 사용 한도는 실제 종료가 확인된 같은 provider/model의 독립 성공 기록 이후에만 재개하고 유료 probe·새 session·provider 전환으로 우회하지 않는다. `wait_usage`는 최초 ledger 조회 전과 각 대기 반복, 독립 성공을 읽은 뒤 공통 취소/최종 삭제 ID를 재확인한다. 취소가 확인되면 False로 반환해 한도 감사·사용량·독립 성공 row를 수정하거나 새 대기 통지를 만들지 않는다. 이는 quota 조건 대기의 종료이며, `wait_existing_call`의 실제 프로세스 종료·결과 인계·예약 반환 대기를 해제하는 권한이 아니다. 종료 불명 예약은 해당 token의 기존 receipt로 종료를 재확인한 뒤에만 회수한다.

### 자체 테스트의 Git 경계

테스트 정리는 현재 명세·실행 경로·검증 조건으로 판단한다. 폐기된 동작만 검사하거나 다른 테스트가 같은 조건·assertion을 모두 보장하는 중복 테스트와 참조가 없어진 전용 fixture·helper는 제거하며, 제거 이유와 남은 검증의 대응을 확인한다. 현재 지원하는 호환성·실패·보안·데이터 보존·외부 연동 계약은 유지하고 실패·비용만으로 삭제·skip·assertion 약화를 선택하지 않는다.

자체 테스트의 작성·수정 기준은 Git 관측을 mock으로 제공하고 실제 판단·저장 로직을 검사하는 것이다. 실제 Git·CLI·subprocess는 mock으로 확인할 수 없는 동작·프로세스 연결·OS 효과가 계약일 때 사용한다. 추가·확장 전 기존 테스트의 계약·assertion을 확인하고 새 실행의 효과와 기존 mock·통합 회귀로 충족되지 않는 이유를 남긴다. 통합 테스트라는 이름이나 fixture의 준비 편의만으로 실제 실행을 선택하지 않는다.

테스트 전환·실행 축소는 기존 조건·assertion의 대응과 필요한 통합 회귀를 보존한다. 판단·상태 전이를 성공 mock으로 대체하거나 skip·timeout 확대·수집 범위 축소로 비용 감소를 대신하지 않는다. 기존 fixture·관측 경계를 먼저 재사용하며 SQLite·파일 보존이 검증 대상이면 실제 저장을 유지한다.

이 기준과 기존 테스트의 전환 완료는 구분한다. 남은 전환 범위는 [R-080](requirements/%5BR-080%5D%20Git%20Mock%20Defaults%20for%20Tests.md)과 관련 작업에서 관리하며 공통 helper 제공만으로 G-222·G-223 소비 경계의 전환 완료를 선언하지 않는다.

| 검증 대상 | Git 경계 |
|---|---|
| 상태 전이·재시도·차단·복구 정책과 입력 조건 조합 | 관측값·명령 결과를 mock으로 제공하고 실제 판단·저장 로직 실행 |
| 명령 인자·출력 파싱·종료 코드·관측 실패 처리 | 명령 경계를 mock으로 처리하고 실제 구성·파싱·오류 처리 실행 |
| 실제 충돌·rebase·index·worktree·원격 lease의 효과 | 해당 Git 계약을 확인하는 필요한 통합 회귀에서 실제 Git 사용 |
| bootstrap의 부모/자식 실행과 exact-SHA runtime 연결 | 실제 연결 계약을 확인하는 필요한 통합 회귀 유지 |

#### mock과 실제 Git fixture

mock은 기존 Git adapter·관측 경계에 적용하고 `ExecutionFixture`·`CurrentRebaseFixture`·관측값 생성 도구를 재사용한다. 정상·오류·중단과 연속 관측 사이의 HEAD·원격 OID 변화를 재현한다.

관측 mock은 실제 관측 타입과 collection 형식을 유지한다. rebase 관측은 `tests/support/rebase_observations.py`의 `RebaseObservation` 생성 도구를 사용하며 `operations`는 marker 이름·관측값 쌍의 tuple이다. checkpoint 정책에 전달하는 marker 이름 목록과 혼용하지 않는다. 거부 시나리오는 의도한 판단의 오류 종류·근거를 확인해 fixture 변환 오류가 거부 검증을 대신하지 않게 한다.

실제 Git fixture의 `tests/support/git.py`는 최초 bare/seed init과 후속 명령에 global/system 설정 및 template을 격리한 환경을 전달한다. `XDG_CONFIG_HOME`도 통제 template 경로로 지정해 기본 전역 ignore·attributes를 제외하고 `GIT_ATTR_NOSYSTEM=1`로 system attributes를 차단한다.

실제 adapter·Python 자식까지 실행하는 테스트는 `isolated_git_test`로 setUp부터 cleanup까지 같은 환경을 상속하며 예외 후 호출자 환경을 복원한다. 저장소별 local 설정은 보존하고 상속 검사는 `git_command`의 명시적 `env`에 통제된 설정 파일을 제공한다. 기본 template은 테스트용 `info/exclude`와 실행 hook이 없는 hooks 디렉터리를 제공한다.

bootstrap 회귀의 부모·자식 CLI 계측은 성공과 실패 모두에서 fixture 임시 경로를 정리하기 전에 완전한 이벤트를 회수하고 실제 PID·시각·clock domain과 테스트 이름을 보존해 전체 verify 기록에 포함한다. 누락·읽기 오류·불완전하거나 잘못된 줄은 수집 한계를 진단하며 앞서 확보한 이벤트와 진행 중인 원래 테스트 실패를 유지하고 미완료 phase의 종료를 합성하지 않는다. 정상 경로에서는 수집 완결성과 기존 phase 개수·Git 비용을 검사한다. 상위 원문 기록에 회수 이벤트를 한 번 보존하고 같은 진단 디렉터리의 중첩 진단에서 원문 위치·수집 상태·미완료 위치를 연결한다. 상위 run ID와 일치하지 않는 이벤트는 상위 집계에 합산하지 않는다. 부모 CLI 시간은 자식 실행을 포함하므로 둘을 합산해 전체 실행 시간으로 보고하지 않는다. 비용 개선은 같은 기준 코드·계측·병렬도와 테스트 수집 범위의 전후 실행을 비교하며, 준비·Git·snapshot 누적 시간과 전체 경과 시간을 구분한다.

#### 시나리오 확장과 반복 실행

역할·브랜치·오류를 조합할 때 각 조건이 실제 연결에 영향을 주는지 구분한다. 공통 등록 중단·runtime 재사용·외부 명령 오류 처리를 모든 역할의 전체 실행에 끼워 넣지 않는다. 조건별 판단·저장 결과는 위 mock 기준으로 검사하고, 조건의 상호작용 때문에 실제 연결이 필요한 사례는 유지한다.

bootstrap의 서로 다른 역할별 저장 상태와 실제 자식 dispatch는 실제 프로세스 회귀로 확인한다.

통합 테스트는 확인할 효과에 도달하면 종료한다. 예를 들어 rebase 중단 회귀는 실제 operation·index·session 보존과 같은 복구로의 재진입을 확인한 뒤 정상적인 pause 결과로 멈출 수 있다. 이후의 반복 실패·교정 소진·최종 차단은 그 정책과 실제 효과를 확인하는 기존 전용 테스트에 맡긴다. 실제 재시도 횟수 자체가 검증 대상이면 필요한 반복을 유지하고, 이동한 assertion은 계속 검사하는 기존 전용 테스트로 대응시킨다.

#### 새 interpreter가 필요한 import 회귀

Git 외의 subprocess도 같은 기준을 적용한다. 최초 import의 의존성과 부작용 검사는 entry point·구현 모듈·package initializer마다 새 interpreter가 계약이다. `tests/support/fresh_imports.py`가 현재 Python 파일에서 대상 집합을 수집하고 대상 종류·모듈 이름의 안정적인 해시로 8개 독립 unittest 모듈에 배분한다. 각 대상은 고유 테스트 ID를 가지며 공유 helper의 `legacy_id`가 이전 두 검사와의 대응을 제공한다.

각 분할의 로컬 테스트 클래스가 fixture를 소유하고 `load_tests`가 매 수집·실행 로딩에서 inventory를 다시 읽으므로 대상 추가·삭제와 ID·fixture 변경은 기존 manifest 대조에서 실패한다. 새 interpreter는 SQLite 연결·프로세스 생성과 entry point 선행 로딩을 거부하며 package initializer는 서비스 모듈을 로딩하지 않아야 한다.

분할 모듈의 배정과 실행량은 기존 unittest adapter와 coordinator가 소유한다. worker는 각 대상의 interpreter 하나를 기존 30초 상한 안에서 실행하고 종료를 기다린다. worker 내부 pool·별도 runner·이미 import된 프로세스 재사용 없이 기존 serial/independent 선언, 자원·사용자 상한과 전체 timeout·자식 종료 확인을 유지한다.

동적 import 문자열의 대상 범위를 AST 참조만으로 증명하지 않으며 대상 추가·삭제·이동과 분할/helper 변경의 불확실성은 기존 부분 verify의 실행 없는 fallback으로 처리한다. 미선택 모듈은 NOT_SELECTED로 남고 import 완료로 집계하지 않는다. 실제 interpreter 시작·프로세스 간 전달·OS 효과 외의 오류 처리·조건 조합은 위 mock 기준을 적용한다.

#### 비용 비교와 복구 경계 재사용

완료된 전체 기준 검증이 있으면 비용 변경의 전후 비교는 영향을 받는 모듈만 같은 병렬도·계측·수집 범위로 실행한다. 필수 연관 회귀가 있으면 그 모듈을 포함하고, 전체 검증과 모듈 비교의 결과를 구분한다. 모듈의 시간 감소를 전체 verify의 감소로 환산하지 않는다. 이 비교 범위는 개발 중 검증에만 적용하며 전체 verify의 수집·실행 범위를 바꾸지 않는다.

실제 Git이 필요한 자체 복구 통합 테스트의 `RecoveryBoundary`는 한 테스트 안의 직렬 변형만 같은 소유 경로에 복원한다. 다른 테스트·병렬 실행은 별도 경로·원격·DB를 소유한다. Review 프로토콜 변형은 실제 Worker·verify를 통과한 Review 진입 직전을 보존하며, 파일/mode·Git 객체/ref/index/config·SQLite/TEMP 상태의 digest와 복원 후 새 Git snapshot을 대조한다.

교정 Worker의 파일 version도 경계에서 이어받아 실제 새 작업물이 생성되게 한다. 호출 경로·준비 입력 불일치는 복원 전에 거부하며 자료 손상·재현 실패도 실패로 보고한다. 이전 receipt를 현재 증거로 사용하지 않는다. `git_fixture:review_*`와 `git_fixture:boundary_*` 단계는 생성·Worker/verify 준비·복사·복원·identity 및 재현 확인·정리를 기록한다.

`git_fixture_seconds`는 같은 PID의 유효한 `start`·`duration` 경계에서 완전히 포함된 하위 fixture 시간만 총계에서 제외한다. 개별 phase event와 duration은 보존하며, 별도 프로세스나 중첩 경계가 불완전한 이벤트는 포함 관계를 추정하지 않는다. duration이 없는 이벤트의 시간을 만들지 않는다.

## GitHub와 PR

Worker 요청의 로컬 commit·checkpoint 게시·PR 생성과 최종 후보 게시의 Git 쓰기는 Manager 코드가 수행한다. Worker와 Reviewer의 sandbox에 shared Git·인증 저장소 쓰기 권한을 추가하지 않는다. 게시 대상과 최종 승인 경계를 구분한다.

#### Worker checkpoint 게시와 새 환경 인수

Worker의 `WORKER_OUTCOME`은 `ready_for_validation`, `needs_user_decision`, `checkpoint`를 구분한다. Worker 지침과 최초·재개 prompt는 전체 ExecPlan 구현·교정을 기본 목표로 지정하고, checkpoint는 중간 보존 필요성이 있을 때만 요청하도록 한다. Worker는 본문에 보존 이유·전진한 완료 조건·잔여 작업을 갱신하며 게시 후에도 이를 전체 완료 조건과 대조한다. 동일 잔여 작업의 반복은 원인·접근법 재평가 대상으로 안내하고 범위·정책 변경은 역할 권한에 따른 판단으로 연결한다. 이는 Worker의 수행 지침이며 Manager가 checkpoint 횟수로 완료를 합성하거나 강제 검증·새 차단 상태를 만드는 규칙은 아니다. checkpoint JSON의 필수 필드는 `kind`, 선택된 `plan_id`, 해당 호출의 전체 `start_head` OID이며 별도 `COMMIT_TITLE` 한 줄만 받는다. 중복·손상·다른 작업/HEAD·임의 명령 필드와 완료 publication 선언의 혼합은 거부한다. 완료 후보의 기존 COMMIT_TITLE·PR_SUMMARY·TARGETED_TESTS 계약과 사용자 판단 대기·legacy 완료 결과는 유지한다.

Worker가 종료하면 기존 scope 검사와 선택 frontmatter의 부분 복원 뒤 허용된 호출 변경 경로를 결정한다. 요청 밖 staged entry와 index flags·미확정 operation은 보존하고 commit하지 않는다. `execution_checkpoints.snapshot.worker_checkpoint_request`는 repository·작업·branch·worktree·시작 HEAD·요청 원문·제목·파일별 mode/content 지문·실제 session·호출 ID와 전달 attempt를 고정한다. 실제 호출의 writer 종료 근거를 확인하고 명시적 literal pathspec만 staging한다. 기존 Manager commit intent의 parent/tree/message와 실제 commit을 대조하며 저장 중단 뒤 commit이나 모델 호출을 반복하지 않는다. 원격 게시 확인 뒤 같은 transaction에서 요청 완료·scope 정리·전달 확인을 적용한다. 보존점은 Worker/targeted/verify/Review 성공 token을 만들지 않으며 기존 실패·session을 유지한다. 완료 후보를 Manager가 commit한 때도 다음 Worker 변경 전에 같은 게시 경계를 적용한다.

게시 직전 active ExecPlan·branch·worktree 소유권, origin identity, 관리 branch와 HEAD, Git operation, PR identity/head와 live 원격 OID를 검사한다. 과거 직접 Worker commit·부분 게시·인수 이력은 checkout이나 새 검증 worktree 없이 immutable parent/tree의 경로와 선택 선언·User Decision·mode를 commit별로 검사한다. 등록 base에 실제 포함된 commit은 원본 유입으로 구분한다. 나중에 되돌린 금지 중간 tree도 게시하지 않고 이미 확인한 선행 복구점과 미게시 로컬 이력을 보존한다.

`worker_checkpoint_pending`의 필수 호환 필드는 `{branch, from, target}`이다. `from`은 전체 OID 또는 정확한 부재 `null`, `target`은 게시할 전체 commit OID다. 신규 intent에는 repository·작업·worktree·origin·PR identity와 정확한 parent/tree도 결합한다. Manager는 intent와 완료 각각을 `CoordinatedConnection.run_request()`의 DB 전용 요청으로 확정한다. 매 시도 등록 origin/base·선택 실행 snapshot·checkpoint generation을 재조회해 관측 기준과 비교하고, 변경됐으면 경쟁 값을 보존하고 현재 적용을 거부한다. intent를 durable하게 저장한 뒤 고정 lease로 target 하나만 push하며, Git·PR·원격 관측은 요청 밖에서 수행한다. 완료 요청은 checkpoint의 accepted OID·intent 감사와 execution의 publication_remote_oid·PR head를 함께 갱신한다. BUSY/LOCKED는 요청 전체 rollback 뒤 같은 deadline·취소 경계로 재준비한다. 실행 port의 `run_checkpoint_publication_request()`는 각 callback이 반환하거나 실패할 때 `ExecutionStatePort.expected`를 요청 시작 snapshot으로 되돌려 COMMIT 경합·대기·취소에서 미확정 snapshot이 다음 시도의 검사를 막지 않게 한다. callback 결과와 갱신 snapshot을 함께 반환받아 COMMIT 성공 뒤 실행 snapshot과 게시 owner 기준을 확정한다. 외부 `state.bind()`의 종료 시 갱신은 요청 내부 재시도의 snapshot 복원을 대신하지 않는다. DB 재시도는 이미 수행한 외부 효과를 replay하지 않는다. 게시 port는 `DatabaseWaitTimeout`만 복구 횟수를 소비하지 않는 일시 대기로 반환하고, SQL/권한 오류와 runtime 취소는 원래 오류·제어 신호로 전파한다. 재진입은 보존된 intent와 원격 관측에서 결과 확정을 이어간다.

재시작에서 원격이 target이면 push 없이 확인을 마치고 from이면 동일 commit을 재시도한다. target의 후속 원격 전진은 자기 push 확인과 새 원격 입력 통합을 분리한다. 타인의 commit을 덮거나 낡은 intent에 새 lease만 끼워 넣지 않는다. 게시 직후의 소유권 판단은 완료한 DB·원격 관측에서 다시 시작하며 `pushed`와 후보 통합의 `candidate_changed`를 별개 사실로 소비한다. 불명확한 원격·다른 PR·충돌은 요청·양쪽 commit·index·실패를 보존한 대기이며 새 Worker 호출을 허가하지 않는다.

최초 원격 인수는 아직 소유하지 않은 worktree에만 적용한다. 기존 소유 worktree와 현재 Git·원격 근거로 checkpoint 및 소유권을 복구한 rebase는 `workspace_seen` 표식이 없어도 기존 작업 재개 경로를 사용한다.

DB 부재 초기화에서만 `manager_metadata.remote_adoption_bootstrap`을 JSON `null`로 생성한다. 최초 성공한 catalog CAS가 당시 등록 프로젝트 경로를 키, 프로젝트별 첫 catalog 소비 여부를 bool 값으로 갖는 객체로 고정한다. 프로젝트의 기존 실행 행이 없을 때 첫 catalog의 새 활성 작업에만 `remote_adoption:{repo}:{plan_id}` 자격을 발급한다. 값은 관측 `revision`과 `identity` 배열이며 배열 순서는 등록 GitHub origin·base·active path·본문 hash·plan SHA다. 등록·선언이 이 값과 다르거나 실행 행이 없으면 자격을 쓰지 않는다. 새 행 삽입은 이전 자격을 지우며, 관리 worktree 준비 완료는 `workspace_seen` 기록과 함께 자격을 소비한다. 이후 catalog·추가 등록·반복 init은 소비한 범위를 다시 열지 않는다. 기존 `created_from_absence`는 감사 표식으로만 남고 기존 DB에는 bootstrap 자격을 소급 발급하지 않는다.

새 bootstrap DB의 등록과 최초 catalog 갱신은 같은 DB 부재 초기화 표식을 확인해 legacy 단건 PR 이관을 생략한다. 등록에서 baseline을 만들지 않은 것은 기존 runtime 이관의 근거가 아니다. 고정 catalog의 선언 검증과 CAS가 활성 행·제한된 인수 자격을 함께 등록한 뒤 인수 handler가 PR 이력과 원격 HEAD를 확인한다. 기존 DB의 legacy 이관과 archive 문서 기반 복구는 각 기존 계약을 유지한다.

`execution_checkpoints.snapshot.remote_adoption` version 1은 등록 프로젝트 경로인 `repo`, `plan_id`, 정확한 관리 `branch`, 등록 `base`, 전체 `head` OID를 보존한다. 최초 인수는 로컬 자원이 없는 곳에서 관측 OID를 start point로 사용한다. 기존 자원이 있는 중단 재개는 같은 intent·예약과 Git 등록·HEAD·clean 상태를 요구한다. `make_worktree(require_creation_evidence=True)`는 기존 journal의 봉인된 생성 intent/result·receipt와 현재 등록·잠금·index를 대조한 뒤 재사용하므로 같은 이름·OID의 사용자 자원을 소유 근거로 바꾸지 않는다. 관측 전후 execution snapshot·catalog generation·등록 자격·checkpoint generation을 대조하며 경쟁한 기록을 최신 intent로 소급 수용하지 않는다. 원격 재전진은 원래 인수 HEAD를 보존하고 단계 coordinator의 기존 원격 입력 통합을 Worker dispatch보다 먼저 수행한다. branch 삭제와 미확정 관측에서는 로컬 자료를 덮지 않는다.

PR 이력은 fetch 전에 `gh pr list --head <branch> --state all --limit 1000`으로 관측한다. 목록 상한 도달·조회/응답 실패는 불완전 이력이다. 전체 응답의 URL repository·등록 base·정확한 head branch·전체 head OID·`isCrossRepository=false`를 검증한 뒤 MERGED를 우선하고, MERGED가 없을 때 OPEN 하나를 선택한다. 대상 불일치·복수 OPEN·불명 상태는 거부하며 CLOSED unmerged는 기존 PR의 reopen 또는 명시적 재개 판단을 기다린다. 기본 저장 URL 조회와 수동 제출 연결 정책은 유지한다. 새 환경에는 기존 verify receipt·Review 승인·Worker session을 합성하지 않으며 새 Worker와 정상 검증 gate를 사용한다. 원격 OID·lease는 동시 환경 조율을 제공하지 않는다.

| 게시 대상 | 원격 반영과 PR |
|---|---|
| Planner의 검증된 계획 변경 | 새 ExecPlan, snapshot에 결합된 PR 전 계획 개정·미착수 계획 rename·삭제와 `docs/FUTURE_IDEAS.md` 변경은 planning commit으로 `origin/<base-branch>`에 fast-forward push하며 별도 PR을 만들지 않는다. |
| ExecPlan 구현 후보 | 하나의 결정적 branch와 하나의 PR에 연결한다. 현재 후보의 Worker 결과·관련 테스트·전체 verify·Review·문서 보고·게시 대상 근거를 각각 확인하며 필수 조건은 [PRODUCT의 PR 생성과 Merge 후 정리](PRODUCT.md#pr-생성과-merge-후-정리)를 따른다. |

기존 PR identity와 Merge 상태는 GitHub가 원본이다. `pr_for_branch`는 저장 URL이 있으면 해당 PR을 조회하고, 없으면 같은 head branch의 Open·Closed·Merged 전체 상태를 조회한다. Manager가 받는 관측값은 다음 키를 모두 포함해야 한다.

| 관측 필드 | 타입·해석 |
|---|---|
| `url` | 비어 있지 않은 문자열. 저장된 PR URL과 대조한다. |
| `state` | `OPEN`·`CLOSED`·`MERGED` 문자열. |
| `headRefName`·`baseRefName` | 비어 있지 않은 문자열. 관리 branch·등록 base와 대조한다. |
| `headRefOid`·`mergedAt` | 문자열 또는 `null`. PR head의 commit 확인과 Merge 관측에 사용하며 정리 단계는 실제 commit object·포함 관계도 확인한다. |
| `mergeable` | 문자열 또는 `null`. `CONFLICTING`은 충돌, `MERGEABLE`은 충돌 없음이며 나머지는 미확정으로 해석한다. |

조회 실패·잘못된 JSON·필수 키 누락·형식 오류는 관측 실패이며 PR 부재로 바꾸지 않는다. 저장 PR의 identity 불일치도 정상 연결로 수용하지 않는다. 생성 전 기존 PR이 있으면 SQLite에 연결하며, 생성 직후 저장 전에 중단돼도 다음 실행에서 같은 PR을 찾아 중복 생성을 피한다. Closed PR은 같은 PR의 reopen을 기다리며 프로그램이 새 PR로 대체하거나 자동 Merge하지 않는다.

저장된 PR의 terminal 상태 확인은 자동 재검증 단계 선택보다 앞선다. 수동 묶음이 `AUTOMATIC`이고 구성 G가 `MANUAL_PENDING` 또는 구현·verify·Review·게시 단계여도 일반 PR 상태 동기화와 기존 실행 복구에서 PR 관측을 유지한다. 실제 MERGED와 해당 작업의 게시 근거가 일치하면 재검증을 시작하지 않고 완료를 확정하며, 미병합 CLOSED는 기존 보류·reopen 정책을 따른다. OPEN일 때만 자동 재검증을 계속한다.

등록된 Open PR은 매 상태 동기화에서 Merge 가능 여부를 확인한다. 미확정이면 다음 조회에서 확인하고, Git 충돌이 확정됐을 때 관리 worktree에서 최신 원격 base를 rebase한다. 실제 구현 교정에 필요한 Worker·targeted test와 현재 후보의 전체 verify·읽기 전용 Review 뒤 같은 PR identity·원격 head를 재확인해 안전한 lease로 기존 branch를 갱신한다. 승인·필수 check 대기는 rebase로 해소할 충돌로 간주하지 않는다.

PR 없는 원격 branch의 rebase 복구도 SQLite에 저장된 동일 Manager 소유 branch일 때만 검증된 원격 기대값의 `--force-with-lease`를 허용한다. 게시 직전 최신 base와 후보의 verify·Review token·현재 입력을 대조한다. base·HEAD 변경은 근거별 재판정 사유이며 완료 Worker 결과·verify receipt·일반 선행 Review를 일괄 폐기하거나 일괄 재사용하지 않는다. 조건을 만족하지 않는 근거만 필요한 검증·Review를 다시 수행한다.

실제 Merge 관측과 worktree 제거·branch 정리·ExecPlan archive는 각각 확정한다. 공통 plan별 완료 helper는 기존 수동 제출 검증 뒤 현재 선언·소유 상태를 다시 읽고 PR URL/head·MERGED·완료 감사·cleanup_pending을 먼저 짧은 DB 요청으로 저장한다. 운영 연결은 DB-only callable을 공통 요청 재시도 경계에 전달하며 외부 효과를 replay하지 않는다. 그 뒤 Git 정리와 fetch를 수행하므로 오류·중단에도 완료와 dependency 근거·정리 대기가 남는다. 현재 base ancestry나 branch 존재를 완료 gate로 추가하지 않으며 정상 실행의 catalog 동기화·정리 재검사·archive 게시가 남은 처리를 이어간다. Merge 사실을 정리 성공으로 추정하거나 정리 보류로 MERGED를 되돌리지 않는다.

`assess_cleanup`은 tracked·staged·non-ignored 변경, index flag·Git operation과 PR head OID·commit object·로컬 HEAD의 포함 관계를 검사한다. 정리 소유권은 계획·등록 worktree·branch와 관리 경로를 대조한다. `remove_cleanup_worktree`는 판정 뒤 HEAD·branch·index·파일·operation을 다시 확인하고, 관리 journal로 잠금 해제·제거·확정을 복구하며 강제 옵션을 사용하지 않는다. ignored-only는 정리를 막지 않는다. Merge 방식·base ancestry·원격 branch 존재만으로 안전성을 추정하지 않는다.

정리가 불확실하거나 실행 자리를 사용할 수 없으면 `cleanup_pending`에 경로·원인·PR head·소유 여부를 보존하고 MERGED 전이는 유지한다. 재검사도 소유권·안전 조건을 요구하며 소유권 불명 경로는 자동으로 살피거나 제거하지 않고 부재가 확인될 때만 기록을 해제한다. 미완료 worktree 정리와 아래 ExecPlan archive는 후속 작업·프로젝트 완료의 차단 조건이 아니다.

현행 Merge 정리 구현은 관리 worktree를 제거하며 로컬 branch를 삭제하지 않는다. 로컬 관리 branch 비강제 삭제는 설계 기준과 구현의 차이로 명시하며 원격 branch·진단 ref 삭제의 후속 목표로 확대 해석하지 않는다.

### MERGED ExecPlan archive

ExecPlan은 GitHub Merge 확인과 SQLite MERGED 전이 뒤에만 별도 정리 대상으로 삼는다. 최신 원격 base의 detached 임시 worktree에서 `docs/plans/`의 문서를 같은 파일명의 `docs/plans/archive/`로 내용 보존 rename-only commit해 직접 fast-forward 게시한다.

`ProjectService`는 Requirement 교정의 lifecycle gate와 archive 실행 자원 판단을 분리한다. `ArchiveExecutionResources`는 base 게시·fetch·공유 설정과 후보별 plan/branch/worktree의 기존 lease를 dispatch 동안 유지한다. archive의 lease 요청은 경합 시 기다리지 않으므로 실행 중 호출이나 다음 배정을 선점하지 않는다. 일반·유지보수·교정의 실제 base 게시 예약과 프로젝트 안전 차단을 재확인하고, 단순 phase·유지보수 cycle·다른 작업의 존재는 쓰기 권한이나 거부 근거로 사용하지 않는다. 미확정 Git 기록은 [공통 자원 판단](#미확정-git-작업의-단계별-자원-판단)에 같은 lease stack을 전달하며 원인 rebase의 intent·차단을 보존한다.

archive는 관리 배정 자리를 소비하지 않는다. 관리 배정의 parked·인계·반환은 물리 writer 종료의 근거가 아니다. 후보 자원 검사는 기존 task-call 예약과 종료 영수증을 사용하고, 명시 요청의 우선순위·관리 한도·drain·pause는 조율 경계에 남긴다. 미Merge CLOSED와 삭제 ID·정리 pending은 MERGED 후보에 포함하지 않는다. 독립 삭제 보존 기록은 전역 gate가 아니며 같은 base·journal·후보 자원의 실제 충돌만 보류한다. 삭제 기록·정리 파일의 제거와 복구는 [삭제 재시작과 파일별 복구](#삭제-재시작과-파일별-복구)의 책임으로 유지한다.

보관 근거는 최초 게시본이 아닌 catalog의 현재 최종 문서 hash와 `current_revision_sha`다. operation identity는 base·계획 ID·이전/보관 경로·최종 hash·revision에 결합하며 준비 commit의 실제 rename-only diff와 후보 정의를 게시 전에 다시 확인한다.

완료 item의 활성 경로·최종 hash·revision이 현재 MERGED 정의와 같으면 catalog에 활성 경로가 남아 있어도 archive 후보에서 제외한다. 동일한 완료를 재확인하기 위해 Git 큐와 원격 조회를 반복하지 않는다. 정의가 바뀌면 다시 후보로 판단하며, 원격 게시 뒤 DB 확정이 끊긴 미완료 item의 복구는 유지한다.

| 관측·중단 조건 | 보관·재개 결과 |
|---|---|
| 현재 revision이 있고 일반 파일 source의 최종 hash가 일치하며 destination·다른 동일 ID 보관 문서가 없음 | 안전한 후보만 묶어 게시한다. 충돌한 다른 후보는 pending으로 분리한다. |
| source가 없고 같은 destination에 최종 hash가 일치하는 일반 파일이 이미 있음 | 원격 보관 완료를 관측해 확정하며 새 commit·push를 만들지 않는다. |
| source/destination 충돌·양쪽 부재·다른 동일 ID 보관 문서·비일반 파일·hash 불일치·현재 revision 부재 | 해당 후보의 실제 이유를 archive pending에 보존하고 덮어쓰지 않는다. |
| 대상의 수동 소유·호출 종료 미확인·lease 경합 | 해당 후보를 보존하고 다른 안전한 후보를 계속 처리한다. 후보 writer는 기존 물리적 종료/미실행 증거로 확인한다. |
| push 실패 뒤 같은 base·후보의 준비 commit이 유효함 | 저장한 commit을 검증해 재사용한다. base·후보가 달라졌으면 최신 관측으로 다시 계산하며 낡은 후보를 그대로 게시하지 않는다. |

원격 base·후보 정의·실행 자원은 초기 관측과 commit 준비·게시 직전에 재확인한다. 준비 뒤 자원 보류는 commit·tree를 보존한다. 결과 확정의 Git rename 검증·ref 관측은 DB 요청 밖에서 수행하고, 짧은 transaction은 열린 fetch lease와 catalog snapshot/generation을 재확인해 archive 성공·catalog 이동·archive identity와 Merge 증명 이력·증명된 planning delta를 함께 적용한다. 기존 MERGED 정의의 경로가 현재 archive 또는 그 원본 경로이고 최종 hash가 같은 검증된 archive 문서에 한해, 누락된 이력을 catalog 재반영에서 복구한다. 기존 이력의 진단은 덮어쓰지 않으며 내용 불일치나 plan SHA 부재는 이력 복구의 근거로 사용하지 않는다. snapshot 경쟁이나 lease 반환에는 확정하지 않는다. 원격 게시 뒤 DB 확정이 끊겨도 MERGED 행·현재 원격 catalog와 미완료 item에서 이동을 재발견해 commit/push를 반복하지 않는다.

`execplan_archive_observations`는 `(repo, scope)`를 문자열 key로 사용한다. scope는 실행 전체의 `dispatch`, 후보 G ID 또는 미확정 Git 판단의 `git:<intent_id>`다. Git 판단의 reason에는 allow/hold와 실제 원인을 함께 기록하며 같은 판단의 반복 출력만 억제하고 판단 자체는 매번 수행한다. `category`는 `execution_opportunity`·`shared_resource`·`remote_observation`·`candidate`·`publication_confirmation`·`execution_result`·`complete`를 구분하고, 문자열 `resource`·`reason`·`resume_condition`과 음이 아닌 정수 `attempts`, 마지막 갱신 시각을 보존한다. `attempts`는 해당 scope의 실제 dispatch 결과 관측 누계이며 commit/push 횟수가 아니다. 같은 보류는 로그를 생략해도 실제 시도 누계는 유지하고, 사유 변경·완료와 완료 뒤 재발은 다시 기록한다. 후보 row 없는 진입 전 보류도 실행 scope에 남고, 관측 저장 실패는 기록 미확정 사실과 원인·재개 조건을 로그에 남긴다. 이 관측은 실행 상태·Telegram outbox를 변경하지 않는다.

관측의 nullable 문자열 `owner_plan_id`는 소유가 확인된 전용 Git 관측의 작업 관계다. 기존 DB에는 이 컬럼을 추가하고 과거 값은 `NULL`로 유지한다. archive의 journal callback은 기존 `manager_git_journal`이 immutable intent·현재 DB 소유권·writer 종료·공유 자원을 대조해 `managed_rebase`의 `allow/separate_rebase`를 확정한 경우에만 실제 `plan:<G ID>` owner를 저장한다. 메시지나 resource 문자열의 ID 포함 여부로 관계를 추정하지 않는다. 이후 미확정 판단은 이 관계를 다시 비우며 관측의 시도 누계 의미는 유지한다.

삭제 확정 transaction은 대상 G scope와 확인된 `owner_plan_id`의 전용 관측만 제거한다. `dispatch`·다른 repository·독립 MERGED G·다른 intent의 관측과 시도 누계는 유지한다. 대상 branch/worktree와 resource가 정확히 일치하는 Git 관측의 소유가 미확정이면 최종 제거를 보류하며 재관측을 기다린다. 완료된 archive 관측도 제거 대상에서 보호한다. 관측 정리는 실제 writer·Git intent·물리 자원 정리의 선행 조건을 대신하지 않으며, 일반 취소의 감사 보존 경로에는 적용하지 않는다.

후보는 보류 지문에 고정하지 않고 다음 PR 후속·조율 경계와 재시작에서 DB MERGED 및 최신 catalog로 다시 찾는다. 경쟁·게시·정리 실패는 operation·OID·이유를 보존하며 독립 후속 작업·완료를 막지 않는다. archive-only 이동의 planning·작업 주기·Merge 집계 보존은 기존 검증된 delta 적용을 재사용한다.

### Requirement 교정 게시와 복구

Requirement 교정은 모델 없이 식별 정보·확정 상대 링크를 보정해 같은 commit으로 게시한다. 편집 의미와 source 보존은 [Requirement 교정과 완료 보관](#requirement-교정과-완료-보관)을 따르며, 이 절은 파일 적용·원격 게시와 중단 복구를 소유한다.

PR 관계 동기화 뒤 교정 전용의 기존 lifecycle·source/draft/planning/writer gate 안에서 ExecPlan archive보다 먼저 실행한다. 이 gate가 닫혀도 실제 자원 조건이 허용하면 archive는 별도로 진행한다. 연속 실행 조율의 archive 기회는 새 교정을 시도하지 않으며 준비된 교정 게시가 같은 base를 예약하면 archive를 보류한다. `requirement_correction_publication`은 기존 detached-worktree Git adapter·기록된 fetch·자원 lock·fast-forward push를 재사용하고 기본 checkout·Agent/verify worktree를 편집하지 않는다.

| 진입·실행 조건 | 처리 |
|---|---|
| 진입 전 lifecycle gate가 닫혔거나 DB에 수동 source·planning 예약·원격 catalog의 Requirement 문서 draft·종료 미확인 writer가 이미 확인됨 | 원격 관측·편집 없이 교정만 보류한다. 기존 후보·작업물·실패 원인을 보존하고 기존 수동 verify·Review·PR 복구와 독립 archive를 계속한다. 실제 base 게시 예약은 archive의 별도 자원 gate도 닫는다. |
| 진입 뒤 안전 gate·미확정 Git 쓰기·종료 불명 writer·수동 source 소유·Requirement draft·planning publication의 경쟁 또는 실패 | 후보와 이유를 보존하고 이번 단계를 중단한다. 교정 실패를 비차단 archive 진단으로 삼켜 Planner로 넘기지 않는다. |
| 보류 원인·소유권이 해소됨 | 저장 후보와 현재 원격·관계를 다시 확인해 아래 상태별로 재개한다. |

교정의 사전 진입 판단과 실행 중 관측은 같은 물리적 writer 종료 조회를 사용한다. 진입 판단은 이미 알려진 교정 전용 점유를 실행 실패와 구분할 뿐이며, 진입 뒤 source/draft/예약/writer의 변화와 실제 교정 오류를 삼키지 않는다. catalog·관계·소유권 조회 실패도 빈 입력이나 안전한 보류로 추정하지 않고 기존 `CorrectionPending` 경계로 전파한다.

동일 base의 일반 blob·mode, 활성·archive Requirement, 게시·draft·archive ExecPlan의 sources, SQLite 실행·취소·draft 관계와 planning intake를 모아 G-232의 `plan_requirement_corrections`를 호출한다. 기본 branch의 first-parent 반영 이력으로 순서를 관측하고 구분 불가 순서는 계산 함수의 파일명 정렬에 맡긴다. 조회 실패를 빈 관계로 대체하지 않는다.

수정 가능 범위는 활성 Requirement, 예약되지 않은 PRODUCT·ARCHITECTURE·FUTURE_IDEAS와 비보관·비draft ExecPlan의 확정된 활성 source 경로다. ExecPlan 본문·다른 선언, archive·User Decision은 보존한다.

`requirement_correction_publications`는 편집 전에 원문·mode·목적지·배정·최종 bytes·참조·보류 이유·operation identity를 저장한다. 같은 경로의 수동 준비·planning publication은 prepared 예약을 검사한다.

| 저장 상태 | 재개·확정 조건 |
|---|---|
| `prepared` | 부분 편집·commit 직후 중단·push 실패도 같은 후보·worktree·저장 commit을 검증해 재개한다. push 실패는 원인을 보존한다. |
| `superseded` | base·참조·목적지·소유권 변경으로 이전 intent·작업물·commit·이유를 보존하고 다음 실행에서 새 snapshot을 계산한다. 입력이 돌아오면 최신 외부 입력을 먼저 관측하고, 짧은 SQLite transaction에서 같은 manifest·operation identity·저장 상태를 재확인한 뒤 기존 행을 prepared로 재활성화한다. |
| `confirmed` | push 결과 또는 원격에 포함된 저장 commit과 실제 결과 bytes를 확인해 확정한다. 이미 게시된 후보는 새 commit·push 없이 확정한다. 확정 commit이 원격 이력에 유지되면 감사도 유지하며 뒤이은 정상 rename·archive·실제 삭제는 source resolver로 분류한다. |

파일은 worktree 밖 임시 파일에서 원자적으로 교체하고 rename 결과를 먼저 쓴 뒤 원문을 제거한다. 재개는 원문 또는 저장 결과만 허용하며 index·파일·단일 부모 commit의 전체 tree diff를 대조한다. worktree 파일은 CRLF/LF checkout 차이만 허용하고 index·commit·원격 blob은 저장 bytes와 정확히 일치해야 한다. 실제 내용·mode·허용 밖 변경과 변조된 후보는 덮어쓰거나 게시하지 않는다.

게시 직전 최신 원격·관계·목적지·소유권으로 계산 결과를 다시 확인한다. 문서 자원 lock과 저장된 게시 예약을 유지하며 최종 관계·원격 확인과 push는 DB 처리 밖에서 수행한다. DB 확정은 짧은 transaction에서 같은 예약·intent·저장 상태를 재확인해 적용하며 원격 게시와 DB 변경을 하나의 원자 작업으로 가정하지 않는다. 원격에 저장 후보가 포함돼도 확정 전 결과가 제거·변경됐으면 보존하고 중단한다. confirmed 이후의 실제 Requirement 삭제는 확정 감사를 되돌리지 않고 source resolver가 새 삭제로 분류한다. 확정 commit의 원격 이력 제거·조회 실패도 계속 보류·실패로 처리하며 미확정 후보를 성공으로 바꾸지 않는다.

갱신된 base는 기존 catalog refresh·관련성·입력 지문을 거쳐 후속 verify·Review에 전달한다. 교정 성공은 작업 완료·차단 해제·검증 성공을 뜻하지 않으며 기존 작업·session·실패·독립 차단과 후속 gate를 유지한다.

## 운영 로그 정책

운영 로그는 현재 처리 단계·결과·판단 이유와 다음 행동을 실행 흐름에 따라 설명한다. Telegram의 사건 선택과 독립적으로 자동 처리·교정·재시도·대기도 기록한다.

| 기록 경계 | 책임 |
|---|---|
| 동작을 확정하는 실행 경계 | 실제로 관측한 대상·결과·이유를 전달하고 같은 사건을 한 번 기록한다. 상위 계층은 범위별 결과를 요약한다. |
| 공통 출력 `runtime/operation_events.py` | 실행·프로젝트·계획·단계 문맥을 결합하고 포맷·비밀값 제거·한 줄 출력과 stdout/stderr 분리를 적용한다. |
| 기존 상세 기록 | invocation ledger·Agent 원문·verify 진단은 고유 형식을 유지한다. 운영 로그는 식별자·경로를 연결하고 긴 원문을 반복 복사하지 않는다. |

### 로그 추가 판단 기준

새 동작을 추가하거나 기존 흐름을 바꿀 때, 다음 질문에 답하는 정보가 기존 로그에 없으면 해당 동작을 소유한 경계에서 기록한다. 자동 실행·수동 명령·복구에 같은 기준을 적용한다.

| 판단 질문 | 기록할 정보 |
|---|---|
| 무엇을 시작했고 어떻게 끝났는가? | 의미 있는 처리 단위의 대상·목적·실제 시작과 종료 결과, 소요 시간·다음 단계. 준비·선택과 실제 실행, 프로세스 종료와 결과 검증·원격 반영 확정을 구분한다. |
| 왜 이 경로를 선택하거나 다시 수행하는가? | 생략·재사용·무효화·교정·재시도·재개의 이유와 관측 근거. 반복이면 현재 시도와 적용되는 한도, 이전 결과와 달라진 조건을 포함한다. |
| 출력이 없는 동안 무엇을 하고 있는가? | 장시간 동작의 현재 대상·경과 시간과 관측 가능한 진행량. 실행 중과 자원·외부 응답·조건 충족 대기를 구분하고, 긴 무출력 구간에는 주기적으로 상태를 알린다. |
| 이번 실행은 왜 끝나며 이후 무엇이 필요한가? | 실행 전체·프로젝트 처리의 종료 결과와 남은 작업·대기 이유. 같은 실행의 자동 처리, 다음 외부 실행의 재검사, 사용자 판단·조치와 최종 차단을 구분한다. |

| 처리 성격 | 기록 시점·범위 |
|---|---|
| 일반적인 짧은 처리 | 시작·종료 쌍을 강제하지 않고 소요 시간·결과를 종료 한 줄로 요약한다. |
| 장시간 실행·대기 | 대상·목적·경과 시간과 관측 가능한 진행·대기를 30초 주기로 표시하고 종료 결과를 남긴다. |
| 실제 Agent 프로세스 시작 등 독립 판단에 필요한 사건 | 준비·선택과 구분해 실제 관측 시점에 기록한다. 진행·종료 로그에도 같은 대상·목적을 유지한다. |

짧은 내부 함수·모든 Git/CLI 명령·변화 없는 polling은 나열하지 않는다. 같은 상태·원인은 변화 시점과 필요한 주기적 관측으로 요약하되 실제 새 시도·결과·판단 변경은 숨기지 않는다.

시작·완료·성공은 실제 관측과 해당 동작의 확정 조건을 기준으로 한다. 재사용·사용자 생략을 새 실행 성공으로 표시하지 않으며 종료를 확인하지 못하면 미확인으로 남긴다. 경과 시간·주기적 출력·단순 DB 갱신만으로 실제 진전·정체를 단정하거나 미확인 진척률·성공·재개 시각을 추측하지 않는다.

30초 진행 로그의 출력 자체는 Agent의 실제 출력·진전 근거가 아니며 무출력 타이머를 갱신하지 않는다. 실제 수신 이벤트·실행 중 item·턴 완료에 따른 timeout 판단은 [provider 호출 관측](#준비된-provider-실행-입력과-호출-증거)을 따른다.

### 공통 포맷

운영 로그는 기존 공통 출력 경계에서 다음 형식으로 출력한다. 한 사건은 한 줄로 기록하고 출력 시각의 시간대 offset을 포함한다.

```text
YYYY-MM-DD HH:mm:ss±HH:mm LEVEL [run=<실행 ID> project=<프로젝트> plan=<작업 ID> stage=<단계>] <사건 요약>; <관련 항목>=<값> ...
```

| 항목 | 기준 |
|---|---|
| 시간 | 해당 로그를 출력한 로컬 시각과 UTC offset. 과거 결과를 설명하면 원래 관측 시각을 별도 항목으로 구분한다. 소요 시간은 시계 보정에 영향받지 않는 경과 시간으로 측정한다. |
| `LEVEL` | `INFO`는 정상 진행·판단·예상된 대기, `WARN`은 자동 복구 가능한 실패·관측 또는 전송 문제, `ERROR`는 해당 동작의 최종 실패·차단이다. 수준만으로 프로젝트 전체 실패나 사용자 조치 필요를 추론하지 않고 요약에 처리 상태를 명시한다. |
| `run` | 한 외부 실행과 그 자식·병렬 처리에서 공유하는 실행 식별자의 앞 7자리를 표시한다. 다음 외부 실행은 새 식별자를 쓰며, 원본 식별자는 실행 문맥·기존 기록과의 연결에 유지한다. |
| `project`·`plan` | 프로젝트 경로의 마지막 폴더명과 대상 ExecPlan ID를 표시한다. 원본 프로젝트 경로는 실행 문맥·상세 기록에 유지하며 같은 폴더명의 프로젝트는 요약 표시가 같을 수 있다. 아직 식별되지 않았거나 해당하지 않는 값은 `-`로 표시하며 다른 작업의 값을 이어 쓰지 않는다. |
| `stage` | 현재 처리 단계. 같은 역할을 반복 호출해도 목적·검증 범위·재사용 여부는 사건 요약과 관련 항목으로 구분한다. |
| 사건 요약·관련 항목 | 무엇이 일어났고 다음에 무엇을 하는지 한국어로 요약한다. 설명에 필요한 경우에만 결과·이유·시도·소요 시간·호출 ID·후보/base revision·상세 기록 경로 등을 덧붙이며 모든 항목을 매번 채우지 않는다. `runtime_sha`는 앞 7자리를 표시한다. |

공백이나 구분자가 있는 값은 따옴표와 escape로 경계를 보존하고, 값의 줄바꿈이 별도 사건처럼 출력되지 않게 한다. 병렬 처리에서도 사건별 문맥을 섞지 않고 즉시 출력한다. `INFO`는 stdout, `WARN`·`ERROR`는 stderr로 출력하되, JSON·원문 등 명령의 결과 전용 stdout에는 운영 로그를 섞지 않고 stderr를 사용한다. provider 원문·검증 진단 등 기존 상세 기록의 고유 형식을 이 포맷으로 강제 변환하지 않는다.

프로젝트 결과의 수준은 `ProjectRunKind`로 정한다. `completed`·`waiting`·`usage_limit_pause`·`runtime_handoff`는 INFO, `transient_pause`는 WARN, `blocked`·`configuration_error`·`operational_error`는 ERROR다. 전체 실행 요약은 프로젝트 결과 종류 중 가장 높은 수준을 쓰고, `failed` 집계와 명령 종료 코드는 기존 exit code를 그대로 사용한다. 따라서 복구 요청의 접수·대기·진행 상태를 나타내는 비정상 종료 코드만으로 최종 ERROR를 만들지 않으며, 자동 재검사가 가능한 일시 Git lock도 WARN으로 남긴다.

명령의 결과 stdout 여부는 `main()`이 파싱한 Namespace와 출력 계약으로 고정한다. `status --json`과 `document-drafts show/generate/create`만 운영 로그를 stderr로 보낸다. 문서 수정본은 `review_artifact_bytes()`의 bytes를 그대로 stdout에 쓰며 formatter를 통과시키지 않는다. bootstrap 부모는 파싱된 명령의 출력·repository·plan 문맥으로 runtime 준비를 기록하고, runtime 자식은 전달된 실행 ID와 자신의 파싱 결과로 같은 경계를 다시 선택한다. repository 값에 포함된 `--json`·`--raw` 문자열은 경계를 바꾸지 않는다.

### 기록 원본과 보호

작업 상태와 복구 판단의 원본은 기존 SQLite·Git·GitHub 관측이며 운영 로그를 파싱해 상태 전이·재시도·차단을 결정하지 않는다. 운영 로그의 문구와 요약은 Telegram 메시지와 같이 프로그램 코드가 이미 관측·확정한 결과를 정해진 형식에 대입해 결정적으로 생성한다. 로그 작성·요약에 AI를 사용하지 않으며, 이를 위한 모델·Agent 호출이나 검증·원격 조회를 추가 실행하지 않는다. 상세 근거는 기존 invocation ledger·검증 진단·상태 디렉터리의 기록에 연결하며, 긴 원문을 운영 로그에 반복 복사하지 않는다.

실행 문맥은 bootstrap에서 runtime 자식으로 실행 ID만 전달하고 병렬 프로젝트·단계·호출별 문맥은 `ContextVar`로 격리한다. 수동 명령은 repository와 확인된 계획 또는 branch/Requirement 범위를, 로컬 복구는 repository·plan·request ID를 자신의 접수·실행 로그에 연결한다. 각 경계는 끝날 때 이전 문맥을 복원하고 project 범위로 넘어가면 plan을 명시적으로 비운다. 주기적 진행 표시는 관측 문맥을 복사한 스레드에서 수행하며 SQLite나 작업 상태를 변경하지 않는다. verify는 이미 저장된 진단 행의 새 관측만 읽어 진행량·자원 대기를 표시한다. Agent 원문은 호출 ID별 파일로 보존하며 기존 역할별 최신 파일도 유지하고, 요약에서 invocation ID·상세 경로를 연결한다.

공통 출력 경계에서 비밀값을 제거하고 인증 자료·설정 원문·prompt·코드·문서 전문을 운영 로그에 추가하지 않는다. 로그의 수준·전송 실패는 Telegram 이벤트 선택을 바꾸지 않는다. 선택적인 로그·관측 수집 실패만으로 성공한 동작을 실패로 바꾸거나 재호출하지 않는다. 필수 상태·격리·소유권·검증 증거의 저장 조건은 각 실행·검증 경계가 소유하며, 로그의 실패 격리를 이유로 해당 조건을 생략하지 않는다. Worker targeted test 결과 수집 실패는 기존 검증 실패 경계로 처리한다.

## Telegram 알림

사건 선택·주의 수준·해제·완료·대기 정책은 [PRODUCT의 Telegram 알림 조건](PRODUCT.md#telegram-알림-조건)을 따른다. token·chat ID가 모두 있을 때만 outbox에 새 사건을 저장하고 전송한다. 모두 없으면 생성·전송하지 않으며 일부만 있으면 설정 오류다. 상태 소유 경계가 같은 SQLite transaction에 사건을 기록하고 commit 뒤 즉시 전송 경계가 새 건을 시도한다. 실패한 건은 다음 실행의 미확정 전송 처리로 이어가며 작업 상태와 전송 결과를 분리한다.

일반·유지보수 planning은 확인한 게시 commit의 신규 작업 등록 transaction에서 `plans_published`와 `draft_decisions`를 기록한다. 일반 작업과 draft/질문을 별도 묶음으로 저장해 주의 수준을 구분하고 등록 commit 뒤 정리 전에 즉시 전송을 시도한다. 기존 계획 개정이나 catalog 재관측으로 생성 사건을 만들지 않는다. `record_process_start`는 실제 시작을 기존 incident 기록에 연결하며 일반 시작 알림을 만들지 않는다. 유지보수 cycle 시작/종료, 미해결 targeted test와 단순 단계 실패도 별도 사건을 만들지 않는다. 차단 통지는 아래 공통 조치 판정을 따른다.

조각 claim은 `plans_published`·`blocked`·`pr_created`·`draft_decisions`·기존 개별 `draft_decision`과 `work_waiting`·`project_completed`의 각 소유 근거를 재확인하며 유효하지 않은 사건과 그 밖의 구정책 진행·재개 사건의 잔여 전송을 취소한다. payload·고정 본문·cursor·attempts·오류는 보존한다. 일반 dispatch·즉시 전송·실패 재시도는 같은 claim과 `policy.is_current`를 사용한다. PR은 같은 ExecPlan의 `PR_OPEN`·URL과 저장된 실제 PR head를 확인하고, 직렬화된 `DefinitionSnapshot`·`validated_base_sha`·`pr_head_oid`에서 기대 `review_token`과 `publication_token`을 다시 계산한다. Review 결과와 두 token이 현재 값과 일치해야 검토·Merge 대기로 인정하며 결과 문자열로 사용자 승인을 추론하지 않는다.

신규 PR은 실제 생성 성공 또는 소유권 검증을 통과한 중단 복구 경계가 `pr_creation_publications`에 repository·ExecPlan·PR URL·최초 publication token과 선택적 검토 입력 identity를 기록한다. 생성 이력과 `PR_OPEN` 전이·Outbox 등록은 같은 transaction에 속한다. 복구는 `PUBLISHING`의 `pr_create` checkpoint intent·기대 head·게시 원격 OID가 관측한 PR head와 모두 일치해야 신규 생성으로 인정한다. 기존 URL 연결이나 증명 부재만으로 생성 이력을 만들지 않는다. 신규 사건 identity는 repository·PR URL·`creation:` 접두사와 URL·`review_merge` 행동에 결합되며 안정적인 Git diff·blame 증명은 요구하지 않는다. 현재성은 이 생성 이력과 현재 `PR_OPEN`·URL·Review/publication token으로 판정한다.

기존 PR 갱신은 `pr_review_candidates`에 현재 publication token과 일치하는 안정적인 검토 입력 증명이 있어야 한다. 사건 identity는 repository·PR URL·reviewed change identity·`review_merge` 행동에 결합된다. 신규 생성 때 기록한 후보와 같으면 생성 사건을 재사용하고, 다른 identity가 증명되면 별도 갱신 사건을 만든다. 최초 identity가 없으면 이후 증명만으로 변경을 단정하지 않아 갱신 통지를 보류한다. 최초 publication token에 대한 identity가 나중에 확인되면 생성 이력의 빈 identity만 채운다. 확인된 새 후보는 이전 생성 알림의 미전송분을 취소한다. reviewed change identity는 definition snapshot과 canonical diff identity로 계산한다. Git diff는 raw bytes로 읽어 decode 대체 문자가 서로 다른 파일 내용을 합치지 않게 한다. base/head object ID·숫자 hunk 범위·Git이 생성한 가변 section label을 정규화하고 변경 줄 및 각 hunk 양옆 한 줄의 context는 보존한다. gitlink(mode `160000`)는 `--submodule=short` diff의 mode·이전/새 OID로 증명하며 blob 전용 blame을 호출하지 않는다. 일반 파일의 각 hunk old-side 줄은 `git blame --porcelain`으로 원래 commit과 그 commit에서의 줄 위치에 결합한다. 따라서 양옆 줄까지 같은 반복 구간의 다른 위치를 구분하면서 upstream이 앞에 동일 context를 추가해도 기존 줄의 identity는 유지한다. 현재 base의 절대 줄 번호나 context 출현 순번은 identity에 포함하지 않으며 원래 위치 증명을 얻지 못하면 후보를 보류한다. 따라서 같은 함수 안에서도 위치가 다른 변경·의미 있는 공백·비UTF-8 byte는 구별하며 line offset만 이동한 동일 patch는 rebase 뒤에도 같은 후보가 된다. 현재 publication token은 저장 후보의 유효성을 검증하며, 이미 큐에 저장된 head/token과 달라도 현재 stable candidate identity·PR URL·행동이 같으면 같은 알림을 이어 보낸다. 취소된 PR 알림이 같은 후보로 재확인되면 별도 outbox version을 만들고 원래 payload·취소 시각을 보존한다. 후속 version은 현재 후보 payload와 `continued_from` event key를 기록하고 처음 발생 시각·고정 본문·확인 cursor·attempt/error를 이어받아 남은 조각만 전송한다. 전송 완료가 확인된 version은 재base 후에도 재통지하지 않으며 늦게 확인된 이전 요청은 같은 본문 계보의 cursor에만 반영한다. PR 소유 복구·게시 경계가 외부 PR 관측을 수행하고 확정 상태를 SQLite에 기록한다. 일반 scheduler dispatch와 즉시 project-event flush는 PR 사건을 제외한다. 새 PR은 확인된 게시 경계에서 첫 전송을 시도하고, 기존 PR의 재시도는 성공한 원격 PR 관측 callback 뒤에만 수행한다. 따라서 원격 조회 실패 시 오래된 PR 상태만으로 전송하지 않는다. Merge/Close는 관측 상태를 DB에 반영한 뒤 claim에서 취소하고, 충돌 PR의 원격 head가 저장된 head와 달라 적용을 보류한 경우에는 해당 계획의 미전송 사건을 관측 callback에서 취소한다. 알림 정책은 추가 원격 조회나 Git 쓰기를 하지 않는다. 기존 후보 사건의 증명 누락·불일치는 미전송분을 취소하되 신규 생성 사건은 후보 증명 부재만으로 취소하지 않는다. draft는 저장된 질문 identity·revision과 ExecPlan catalog의 현재 경로·revision, 등록 remote에서 만든 URL snapshot을 함께 검증한다. 질문/행동 identity와 제목·경로·링크 표시를 구분해 링크 변경은 새 판단으로 취급하지 않는다.

`policy.block_action`은 현재 block의 scope·대상·원문 원인과 복구 기록의 최종 원문 근거·현재 계획 revision을 대조한다. 복구 fingerprint는 기존 `RecoveryObservation.identity`를 재계산해 무결성을 확인하며, block의 release fingerprint는 입력·설정 재검사의 원본으로 유지한다. 두 지문은 서로 다른 의미이므로 값의 일치를 요구하지 않는다. 해당 scope의 복구가 pending이거나 필요한 근거가 손상·누락되면 보류한다. `block_recovery`는 최종 근거의 코드 소유 차단 원인으로 scope를 구분하며 생성·정책·claim의 사건 키 계산에 같은 관측을 사용한다. 다른 scope의 최종 recovery와 그 근거를 보존한 복구 완료(`reconcile`) 기록은 현재 계획의 질문·승인 근거를 대체하지 않는다. 계획 차단을 기록할 때 일치하는 최종 recovery를 같은 transaction에서 `execplan_blocks.recovery_snapshot`에 보존한다. 이 선택적 nullable JSON object는 identity·원인 코드·phase·계획/base revision·fingerprint·progress marker·detail·pending_action과 JSON 문자열 final_evidence를 유지한다. final_evidence 안의 검증된 Worker 결정이 질문 원본이다. 공유 recovery 부재 또는 다른 scope의 최종·복구 완료 기록에는 이 snapshot을 재사용하되 현재 block 원인·대상·계획 revision과 identity 무결성은 다시 검사한다. 같은 scope의 pending/손상 기록은 우선하며 과거 snapshot으로 우회하지 않는다. 기존 DB에는 nullable 컬럼을 추가해 행을 보존하며, snapshot이 없는 기존 기록에서 질문을 합성하지 않는다. 계획 차단 교체·해제에는 snapshot도 함께 교체·제거한다. 지침 승인·명세 교착은 자체 pending 승인 행·파싱한 원문을 조치 원본으로 사용하고 사건 키에서 공유 recovery identity를 제외해 독립 scope 변경에 따른 중복 통지도 막는다. 허용 근거는 이미 검증한 Worker 결정의 질문/위치, 파싱·허용 문서 검사를 통과한 SPEC_DEADLOCK, 현재 pending 지침 승인, 같은 닫힌 PR·게시 branch 삭제에 연결된 조치, 기록된 verify 상한과 실제 timeout 결과다. 최종 disposition·예산 소진·user_targets만으로 복구 불가능성을 추론하지 않는다. 조치 없는 최종 중단은 아래 명시적 복구 경로 근거 계약을 만족해야 한다. 원인이 일반 진행/복구/불충분 근거이면 outbox 없이 원본 상태와 진단을 유지한다.

새 blocked payload의 선택적 `action` object는 현재 조치의 snapshot이다. `kind`는 `decision`·`specification`·`guidance`·`reopen_pr`·`restart`·`verify_timeout`·`unrecoverable`이며 질문은 `decision`, 승인 목록은 `proposals`, PR은 `pr_url`, 재시작은 `branch`, 상한은 양의 정수 `seconds`에 결합한다. `guidance.proposals`는 승인 순서대로 `[proposal_id, proposal_hash, target_path, proposal_line, decision_line]` 배열을 보존한다. `unrecoverable`은 원래 recovery의 `identity`를 `recovery_identity`로 보존하며 이 action이 없는 구정책 payload를 복구 불가 통지로 소급 해석하지 않는다. 나머지 기존 payload에 action이 없더라도 현재 원본으로 유효성을 확인할 수 있으면 그대로 전송한다. action이 있으면 현재 조치와 정확히 비교하며 원래 payload·고정 본문을 소급 갱신하지 않는다. 변경된 조치는 기존 incident의 다음 version으로 연결하고 취소·전송 감사를 남긴다. 중복 판정은 현재 outbox payload와 비교하며 incident에 남긴 첫 확인 원문은 감사용으로 보존한다. 같은 조치의 기존 행이 취소됐고 전송 완료가 확인되지 않았다면 현재 근거 재확인 뒤 다음 version의 새 outbox를 만든다. 기존 취소 행·attempt/error·payload는 변경하지 않으며 새 version은 기존 고정 본문·확인 cursor를 승계해 잔여 조각만 전송한다. 미취소 행·전송 완료 행과 이력이 누락된 행은 이 재생성 대상이 아니다. 같은 조치를 이어받은 payload의 선택적 `continued_from`은 직전 outbox event_key 문자열이다. 이전 HTTP 요청의 늦은 성공은 동일한 고정 본문을 가진 이 계보의 후속 version에도 확인 cursor·전체 완료를 전파한다. 계보가 없는 기존 payload는 그대로 지원하며 새 질문·다른 조치로 교체한 version에는 이전 확인을 전달하지 않는다. 취소 시각·원문·attempt/error는 보존한다.

`RecoveryObservation.unavailable_recovery_routes`는 복구 소유자가 이미 확인한 복구 불가 사실만 전달하는 선택적 JSON object다. 최종 판단일 때 `final_evidence`에 보존하며 기존 복구 결정·예산·재개 정책은 바꾸지 않는다. 허용 키는 `reobservation`·`retry`·`correction`·`condition_wait`·`next_run`·`safe_recovery`의 정확한 여섯 개이며 값은 각 경로를 사용할 수 없는 근거인 비어 있지 않은 문자열이다. 현재 원인·지문·revision과 일치하는 최종 기록에 이 근거가 모두 있고 사용자 조치가 없을 때만 `unrecoverable`을 선택한다. 기존 기록의 필드 부재·부분 근거는 보류하며 일반 final 분류에서 자동 생성하지 않는다. 본문에는 원래 원인·보존 결과·경로별 근거를 표시하고 가짜 질문·조치 안내를 붙이지 않는다.

`configure_session`은 credential 없이 연결별 TEMP 상태에 활성 여부와 현재 양의 정수 `verify_timeout_seconds`를 묶는다. scheduler·수동 recover·독립 task 연결은 transaction 밖에서 읽은 설정을 전달한다. 미확인 값은 NULL로 처리하며, 상한 조치는 현재 값·block 지문·실제 verify timeout 결과가 모두 같은 상한일 때만 허용한다. 값 변경·누락은 이전 통지의 잔여 조각을 취소하며 기존 설정·오류·감사를 덮지 않는다.

대기 사건은 원인·목록과 관련 source 관계의 지문 및 episode 세대에 결합한다. 표시 링크·revision과 무관한 보관은 지문에서 제외하며 저장된 source 선언과 확인한 Requirement 관계를 claim에서 대조한다. 완료 사건은 `cycle_key`·완료 세대·최종 확인의 `work_cycle`에 결합하고 현재 receipt·미완료 계획·차단·유지보수·복구·게시 상태를 다시 검사한다. 대기/완료 사건은 독립 게시·질문·PR 통지의 전송 확인을 중복 제거나 완료 근거로 사용하지 않는다. 유효하지 않은 잔여 조각만 취소하며 이전 payload·고정 본문·cursor·실패 및 취소 감사를 보존한다.

사건 투영은 Telegram 설정과 독립적으로 오래된 미전송 사건을 취소한 뒤 새 사건을 선택한다. planning_error enqueue와 과거 planning 사건의 released_at/canceled_at을 해제하던 복원 경로는 제거했다. legacy 통지 채택과 첫 확인 원인·확인 조각의 연결은 감사용으로 유지한다. 원격 관측과 설정 확인의 소유자는 transaction 밖에서 사실을 확보하고 현재 DB에 반영하며, 공통 판정은 전송 transaction에서 해당 현재 원인을 다시 대조한다.

지속 실행 실패 집계는 복구 진단으로 보존하고 독립 outbox 사건을 만들지 않는다. 같은 대상·원인의 실제 `PLAN_BLOCKED`·`PROJECT_BLOCKED`와 checkpoint 결과가 확정되면 기존 BLOCKED outbox만 사용한다.

이전 `persistent_execution_failure` 행은 piece claim transaction에서 현재 recovery·최종 차단 근거·대상 block row를 대조한다. 일치하면 BLOCKED 중복으로, 불일치하면 현재 차단 근거 부재로 `canceled_at`을 기록하고 claim을 해제한다. 행·payload·전송 cursor·이미 보낸 조각·attempts·오류·실패 감사는 보존한다. 취소와 겹친 Telegram 호출의 결과는 이미 전송한 조각으로 확인할 수 있으나 기존 오류를 유지하고 다음 조각은 claim하지 않으며 재시작 뒤에도 이어 보내지 않는다.

| outbox 저장값 | 의미 |
|---|---|
| `event_key`·`event_type`·`payload` | 고유 사건 키·종류와 JSON object를 직렬화한 문맥. payload의 발생 시각·무음·처리 상태·대상·원인은 저장한 의미로 유지한다. |
| `rendered_text` | 첫 조각을 선택할 때 고정하는 본문 문자열. 다음 조각·재시도에서 다시 구성하지 않는다. |
| `confirmed_pieces` | 앞에서부터 전송 확인을 저장한 조각 수. 0부터 시작하며 확인한 조각은 재전송하지 않는다. |
| `attempts`·`delivery_started_at` | 전송 시도와 미확정 진행 관측. 시작 표식만으로 수신·전송 완료를 확정하지 않는다. |
| `sent_at`·`canceled_at`·`last_error` | 전체 조각 전송 확인·남은 전송 취소·비밀값을 제외한 실패 진단. 작업 성공·차단 해제의 근거로 사용하지 않는다. |

전송 상태는 위 저장값으로 판별하며 작업 상태·사건 해제와 구분한다.

| 전송 관측 | 처리·재개 |
|---|---|
| 미시도: `sent_at`·`canceled_at`이 없고 `attempts = 0` | commit 뒤 즉시 전송이 새 건을 선택한다. 첫 조각 선택 시 본문을 고정한다. |
| 전송 중·확인 불명: `delivery_started_at`만 기록됨 | 전송 시도 관측이며 수신·완료 증거가 아니다. 재시작에서는 미확인 조각부터 처리한다. |
| 부분 확인: `confirmed_pieces > 0`이며 전체 확인 전 | 앞에서 확인한 조각을 보존하고 다음 미확인 조각부터 이어간다. |
| 실패: 미완료·미취소 건의 `last_error` | 확인 위치·고정 본문·payload·주의 수준을 유지해 다음 실행에서 재시도한다. |
| 전체 확인: `sent_at`이 기록됨 | 모든 조각의 확인을 저장한 결과다. 다시 전송하지 않는다. |
| 취소: `canceled_at`이 기록됨 | 남은 조각을 보내지 않고 이미 확인한 조각·전송 감사를 보존한다. |

전송할 조각은 transaction에서 선택하고 HTTP 전송 뒤 현재 조각 위치를 대조해 확인 수를 전진시킨다. 고유 사건 키의 중복 삽입 방지와 외부 수신의 중복 방지는 별도다. 수신 후 확인 저장 전 중단·불명확한 실패는 같은 미확인 조각을 재시도하므로 외부 수신의 정확히 한 번을 보장하지 않는다.

차단 해제와 해당 사건 해제·미전송 조각 취소는 같은 transaction에 반영한다. incident의 실제 시작은 기존 원인 해제·작업 identity·첫 시작 시각과 연결해 Telegram 설정과 독립적으로 보존한다. `record_work_start`는 처음 확정한 통지 확인·실제 시작 연결 수를 반환하므로 PR 재개 운영 로그는 메시지 생성에 의존하지 않는다. `resumed_at`은 기존 `confirmed_before` 또는 현재 outbox의 확인된 조각이 있고 해당 작업이 시작됐을 때 한 번 기록한다. 전송 확인 callback은 원래 조각 cursor와 incident 확인 감사를 전진시키고 이 연결만 확정하며 새 메시지는 만들지 않는다. 실패 callback과 시작 복구도 시작/재개 outbox를 만들지 않고, 시작 복구는 취소 사건의 중단된 claim 표식과 저장된 시작만 정리한다. 과거 planning 중복 사건의 확인 이력은 현재 사건에 연결하고 과거 행을 삭제하지 않는다. 안정적 작업 소진은 draft·계획 차단과 실제 안정성이 확인된 프로젝트 차단을 포함하며 완료·대기 상태는 [프로젝트 최종 확인과 완료 판정](#프로젝트-최종-확인과-완료-판정)을 따른다. 새 PRODUCT·ARCHITECTURE·Requirement 변경 등으로 갱신된 완료 주기와 내용 보존 archive를 구분한다.

작업 취소 transaction은 해당 작업의 미전송 실행·판단·차단·시작/재개·PR 알림을 취소하고 계획 범위 incident의 활성 대기를 해제한다. outbox·incident 행, payload·고정 본문·전송 cursor·attempts·오류와 이미 확정한 전송은 유지한다. 생성·재시작 사건 복구·조각 claim도 취소 감사를 재확인하며, 취소와 겹친 전송 확인은 이미 보낸 조각에만 반영하고 다음 조각을 보내거나 새 재개 사건을 만들지 않는다. 프로젝트 범위 차단·실패, 완료·계획 게시 감사는 독립적으로 보존한다. 프로젝트 재개는 기존 원인 작업과 실제 시작 작업을 구분하며 `resumed` payload의 `started_execplan_id`(문자열 또는 `null`)로 실제 작업의 취소 여부를 판단한다. 이전 payload에 이 값이 없으면 저장된 `execplan_id`를 사용한다. 취소 작업을 포함한 draft 목록은 기존 목록을 보존·취소하고 남은 판단 목록으로 다시 구성한다. `recovery_unresolved`는 payload의 작업 ID 대신 `request_id`와 outbox의 repository에 일치하는 로컬 복구 요청의 `plan_id`로 취소 귀속을 확인한다. 일반 취소에서는 그 요청·전송 감사를 보존하며 독립 요청을 변경하지 않는다. 조각 claim은 이전 오류를 지우지 않고, 전송 확인 때 현재 사건의 오류만 해제하므로 취소와 겹친 확인도 오류 감사를 보존한다.

삭제 확정의 알림 정리는 일반 취소와 달리 대상 전용 outbox·incident·전송 참조를 최종 제거한다. `deleted_plan_state.forget`는 같은 repository의 `plan:<G ID>` 또는 과거 G subject에 속한 incident와 `block_notice_events`의 직접 관계를 먼저 확보한다. 자식 참조를 부모 incident보다 먼저 제거하고, payload에 작업 ID가 없는 과거 outbox 버전도 그 관계로 제거한다. 남은 독립 incident가 해당 outbox를 참조하거나 연결된 outbox의 repository가 다르면 전체 제거 transaction을 롤백하고 해당 삭제를 보류한다. 명시적인 관계가 없는 잘못된 payload도 귀속 미확인으로 보존한다.

대상 하나만 포함한 `plans_published`·`draft_decisions`와 과거 `planning_result`의 plans 목록은 전용 기록으로 제거한다. 과거 planning 결과에 별도 execplan_id/started_execplan_id도 있으면 귀속 미확인으로 보류한다. 다른 G가 섞인 목록은 다음 조건에서 payload의 대상 항목만 제외하고 독립 항목과 사건 identity·오류·시각을 유지한다. 이 정리는 과거 planning 결과의 현재 생성·발송 정책을 활성화하지 않는다.

| 혼합 목록의 전송 근거 | 최종 제거에서의 처리 |
|---|---|
| attempts·confirmed_pieces가 0이고 고정 본문·진행 중 claim·sent_at이 없음 | payload만 개정한다. 첫 claim은 남은 목록의 본문을 정상 생성한다. |
| attempts가 양수이고 confirmed_pieces=0·sent_at/진행 중 claim/canceled_at 부재·비어 있지 않은 전송 오류가 있으며, 저장 본문이 현재 렌더러의 원래 payload와 일치함 | 대상 항목을 제외한 payload·본문만 개정한다. 사건 identity·attempts·오류·시각과 미확인 cursor는 유지해 다음 claim이 독립 항목을 재시도한다. 취소는 HTTP 반환 전에 claim을 비울 수 있으므로 canceled_at이 있으면 이 경로로 승인하지 않는다. |
| sent_at이 있고 저장 본문이 현재 렌더러의 원래 payload 결과와 정확히 일치하며 모든 원래 조각의 확인이 있음 | 남은 목록의 본문이 더 길어지지 않음을 확인해 저장 payload·본문을 함께 개정한다. 기존 전송 완료·공유 시도/확인 누계는 유지하며 이 row를 다시 전송하지 않는다. 기존 질문·게시 확인 조회는 남은 항목을 계속 확인된 것으로 읽는다. |
| attempts가 양수이고 0 < confirmed_pieces < 원래 전체 조각 수·sent_at/진행 중 claim/canceled_at 부재·비어 있지 않은 전송 오류·원래 본문 일치가 확인되며, 더 길어지지 않은 개정 본문이 확인된 원래 문자 prefix로 시작하고 그 뒤 미전송 잔여분이 있음 | 저장 payload·본문만 개정하고 cursor·시도·오류·시각을 유지한다. 확인된 조각의 문자열은 그대로이며, 새 확인이나 sent_at을 만들지 않는다. |
| 위 조건 밖의 부분 전송·진행 중 claim·이전 렌더러의 미확인 본문·불완전 확인 | 원래 payload·본문·cursor·오류를 보존하고 `CallDeferred`로 해당 삭제를 보류한다. |

전송 완료 row의 기존 cursor는 실제 공유 전송 감사이며 개정 본문의 새 전송 위치로 재사용하지 않는다. 대상 원인·실제 시작이 포함된 프로젝트 사건의 부분 분리도 미완료이면 보류한다. 이 경계는 일반 취소의 감사 보존이나 독립 사건의 전송 정책을 바꾸지 않는다.

`notifications/rendering.py`는 저장된 payload만으로 텍스트를 결정적으로 생성하며 모델을 호출하지 않는다. 확인된 질문·수정 대상·PR 상태 순으로 정보를 구성하고 원인·복구 시도·Checkpoint 근거를 보존한다. 공통 payload와 `draft_decisions` 목록의 계약은 다음과 같다.

| payload 필드 | 타입·의미 |
|---|---|
| `occurred_at`·`silent`·`status` | 발생 시각은 UTC ISO 8601 문자열, 무음 선택은 boolean, 처리 상태는 문자열이다. 전송 시각·재시도에 맞춰 원래 의미를 다시 쓰지 않는다. |
| `project` | 표시할 프로젝트 이름 문자열. 사건의 저장 범위는 outbox의 `repo`에 결합한다. |
| `plans` | `plans_published`는 신규 일반 작업 배열, `draft_decisions`는 신규 draft와 아직 통지 확인이 없는 질문의 ID 순 배열이다. 생성 항목은 게시 revision과 원본 경로에, 질문은 판단 identity·revision과 현재 경로·링크에 결합한다. |
| `plans[].id`·`plans[].title`·`plans[].source_path` | ExecPlan ID·표시 제목·문서 경로 문자열. 경로는 active `docs/plans/[id] title.md` 형식이다. 질문은 현재 catalog와 evidence 경로를 대조하고, 생성 항목은 게시 시점 경로를 보존한다. 본문은 전체 건수와 항목별 ID·제목을 표시하고 제목의 공백을 정리한다. |
| `plans[].url` | 등록 remote·확인된 revision·경로에서 만든 원격 문서 URL 또는 `null`. draft는 값이 있으면 ID에 링크하고 일반 작업은 URL을 본문에 표시한다. |
| `plans[].publication_revision`·`plans[].publication_path` | draft 생성 항목의 게시 revision과 선택적 게시 원본 경로 문자열. 경로가 없으면 `source_path`가 원본이다. 일반 작업은 payload의 `publication_revision`을 공유한다. |
| `plans[].waiting_for` | 일반 작업 게시 시점에 미병합으로 확인한 선행 작업 ID 문자열 배열이다. 빈 배열은 실행 대기로 표시한다. |
| `plans[].decision_identity`·`plans[].decision_revision` | 질문 항목의 identity·revision 문자열. ID와 결합해 실제 사용자 행동과 질문별 통지 확인을 식별한다. URL·제목·경로 표시와 사건 identity에는 결합하지 않는다. |

`pr_created` payload는 기존 프로젝트·ExecPlan·PR URL·발생 시각·상태 필드와 함께 다음 후보 근거를 요구한다. 이 event type은 새 PR과 기존 PR의 새 게시 후보를 함께 표현하며 본문은 PR이 생성됐다고 단정하지 않는다.

| payload 필드 | 타입·의미 |
|---|---|
| `notice_kind` | 신규 생성은 `creation`. 필드가 없는 legacy 사건은 기존 후보 증명 계약으로 해석하며 생성으로 승격하지 않는다. |
| `candidate_identity` | 신규 생성은 `creation:` + PR URL, 기존 갱신은 `DefinitionSnapshot`과 안정 patch identity로 만든 검토 입력 identity 문자열. 사건 키에 사용하며 base·head OID가 달라져도 같은 검토 입력을 유지한다. |
| `candidate_head_oid` | 해당 게시·검토 입력의 PR head OID 문자열. 현재 후보 확인용이며 단독으로 새 사건을 만들지 않는다. |
| `candidate_publication_token` | payload를 만들 때 확인한 게시 token 문자열. `DefinitionSnapshot`과 현재 검증 base·PR head에서 token을 다시 계산한다. 기존 갱신은 저장된 review candidate proof와도 대조하며 신규 생성은 생성 이력을 확인한다. 사건 identity는 아니며 같은 후보의 rebase 뒤에는 이전 payload 값과 현재 token이 달라질 수 있다. |
| `user_action` | 정확히 `review_merge`. 현재 PR이 열려 있고 이 사용 행동이 남아 있음을 나타내는 알림 계약값이지 자동 Review 결과나 사용자 승인 증거가 아니다. |
| `continued_from` | 선택적 event key 문자열. 취소된 동일 후보의 남은 전송을 새 outbox version으로 이어갈 때만 직전 version을 가리킨다. |

생성과 조각 claim은 같은 사건 identity·행동·URL과 현재 행의 `PR_OPEN`·Review 결과·token을 다시 대조한다. `notice_kind=creation`이면 생성 이력, 그 밖의 legacy·갱신 사건이면 `pr_review_candidates` proof를 검증한다. 기존 Outbox의 payload·고정 본문·cursor·attempt/error·취소 감사는 이관하거나 덮어쓰지 않는다. 생성 이력이 없는 기존 PR은 신규로 소급 분류하지 않고 이미 Merge/Close된 PR은 통지하지 않는다. 누락·불일치 후보는 전송하지 않고 잔여 조각을 취소한다. 후보 identity가 다른 변경이면 새 event key를 사용하며, 같은 후보의 반복 enqueue·복구 확인은 기존 version을 유지한다. 취소된 version은 payload·취소 시각·확인 cursor·attempt/error를 바꾸지 않는다. 같은 후보가 다시 유효해지고 아직 어떤 version도 전송 완료되지 않았으면 다음 version에 최신 후보 payload와 원래 발생 시각·고정 본문·확인 cursor·attempt/error를 연결한다. 이전 version의 늦은 전송 확인은 같은 고정 본문을 이어받은 후속 version에만 cursor를 전파한다.

본문은 Python 문자열의 문자 위치로 최대 4096자씩 나눈다. draft 링크는 각 조각 안의 ID 구간에 `text_link` entity로 붙이며 `offset`·`length`는 UTF-16 code unit 수로 계산한다. 분할 위치와 entity 단위를 구분해 한글·이모지·조각 경계에서도 ID와 URL의 대응을 유지한다.

신규 게시 항목은 ID·게시 revision·원본 경로와 현재 catalog의 동일 등록 근거로 확인한다. `draft_decisions`의 질문은 ID·판단 identity·revision과 현재 pending 질문·경로·제목·등록 remote에서 계산한 URL을 별도로 확인한다. 한 항목에 두 근거가 있으면 게시 원본 경로와 현재 질문 경로를 각각 보존한다. 확인할 수 없는 remote URL은 `null`이며 표시 변경은 질문 identity를 바꾸지 않는다. 목록 생성과 조각 claim은 저장된 고정 본문·`confirmed_pieces`에서 ID가 포함된 확인 조각을 찾아 생성과 질문을 각각 중복 제거한다. 현재성이 바뀌면 원래 행과 확인 cursor·오류를 보존하고 미확인 유효 항목만 새 목록으로 구성한다. 취소와 경합한 늦은 확인도 다음 claim에서 다시 대조한다. 확인 불명 HTTP 요청은 재시도될 수 있으며 이미 취소된 과거 사건은 정책 변경만으로 부활시키지 않는다.

개별 legacy `draft_decision`은 현재 pending 질문과 ID·identity·revision·질문 본문·source path·current URL이 모두 연결될 때만 새로 만들거나 계속 전송한다. 저장 근거가 불완전하거나 오래된 사건은 행·이미 전송된 조각·오류 감사를 보존하고 미전송분을 취소한다. 유효한 legacy 사건은 기존 payload·고정 텍스트·주의 수준·cursor를 유지하며 새 목록 형식으로 소급 재작성하지 않는다. 확인된 legacy 질문도 질문 identity·revision 기준으로 목록 중복을 막는다.

## Repository Safety and Security

- 등록 프로젝트 기본 worktree의 로컬 변경, ahead·diverged 이력과 checkout branch는 원격 SHA 기반 자동 작업을 막지 않는다. 위 fast-forward·clean diverged rebase의 보존 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
- 사용자 파일 삭제, reset, 일반적인 강제 push와 자동 Merge를 수행하지 않는다
- 계획 commit의 base branch 직접 push는 fast-forward만 허용한다. branch protection 등으로 거부되면 우회하지 않고 계획 오류로 보존한다
- 기본 worktree의 Git 쓰기는 위 안전한 fast-forward와 clean diverged 로컬 commit을 보존하는 소유 rebase·abort에 한정한다. dirty rebase·자동 push·reset·stash·clean·강제 checkout은 수행하지 않는다
- 관리 worktree·SQLite·로그·lock 등 지속 관리 자료는 source checkout 밖의 `<state-dir>`에 둔다. 임시 index·hooks·runtime cache fallback 등 Manager 소유 임시 자료는 OS 임시 경로도 사용한다. 이 경로가 Agent 쓰기 권한에 자동으로 포함되는 것은 아니다.
- Loop Manager가 생성·변경하는 계획과 구현 결과는 Manager가 만든 관리 worktree 안에 기록한다. 사용자·독립 Skill의 문서 작성과 자동 실행의 쓰기 경계를 구분한다.
- Agent 도구의 쓰기는 역할별 선택 관리 worktree와 호출별 최소 소유 임시 경로로 제한한다. 읽기 접근까지 worktree 안으로 격리했다고 간주하지 않으며 Reviewer의 read-only와 역할상 테스트·verify 재실행 금지를 구분한다
- fetch·worktree 등록/정리는 공유 metadata를 다루며 기본 checkout 파일·index·HEAD 갱신은 위 소유 동기화 경계로 제한한다. 사용자 operation·동시 변경·ignored 경로 충돌은 덮어쓰지 않는다
- 정상 종료한 정확한 Manager 소유 관리 worktree는 권위 있는 미커밋 변경과 미게시 commit이 없음을 확인한 뒤 수명을 종료한다. ignored-only는 정리를 막지 않지만 활성 작업·기본 checkout·불명확한 소유권·사용자 변경을 삭제하는 근거가 아니다. base ancestry나 GitHub Merge 방식으로 게시 여부를 추정하지 않는다
- PR head 정보나 commit 관계를 확인할 수 없으면 worktree를 보존하고 `cleanup_pending`으로 기록한다
- `cleanup_pending`은 clean 상태만으로 해제하지 않는다. 소유권·PR head·commit 관계·현재 Git 상태를 재검증해 강제 옵션 없이 제거를 확인한 경우 또는 경로 부재를 확인한 경우에 기록을 해제한다. 불확실하면 경로·이유·작업물을 보존하며 소유권 불명 경로는 자동으로 살피거나 제거하지 않는다. 상세 조건은 [Merge 후 정리](#github와-pr)를 따른다.
- planning worktree·diff·session과 미게시 결과는 오류·게시 복구 동안 보존한다. 폐기·교체는 소유권과 보존 결과를 검증한 관리 경계에서만 수행한다
- Merge 뒤 자원별 정리 범위와 branch 삭제의 미구현 차이는 [GitHub와 PR](#github와-pr)을 따른다. R-055의 삭제 처리 목표로 현재 정리 권한을 확대하지 않는다.
- 동일한 Loop Manager 소유 작업 branch 게시에는 현재 원격 OID에 결합한 `--force-with-lease`를 사용한다. 이력 재작성은 현재 입력의 보존·통합과 필요한 검증을 거친 후보만 허용하며 새 lease만 바꾼 재시도는 하지 않는다
- 공통 파일 복사 스크립트는 사용자가 명시적으로 실행하는 초기 설정 도구이며 자동 Loop에서 호출하지 않는다
- 등록 프로젝트의 문서와 verify 스크립트는 사용자가 신뢰한 입력으로 취급한다
- Telegram token과 다른 자격 증명을 AI prompt, subprocess 인자와 로그에 노출하지 않는다
- subprocess는 인자 배열로 실행하고 필요한 경우에만 운영체제 shell을 사용한다

### 역할 결과와 Manager Git 전이

접근 권한, 게시할 역할 결과와 실제 Git 변경은 서로 다른 경계에서 검사한다. 정상 Git 변화나 과거 snapshot의 차이를 Agent의 접근 위반으로 판정하지 않는다.

| 검사 경계 | 현재 판단과 실패 처리 |
|---|---|
| provider sandbox | Agent의 실제 접근 허용·거부를 담당한다. Git 변화·격리 ledger·실패한 호출 이력으로 권한을 추정하지 않는다. |
| 역할 결과 유효성 | 실제 최종 diff·문서 입력·선택 ExecPlan·허용 결과 범위를 검사한다. 실제 결과·문서 입력의 부족은 해당 Worker 교정·계획 검증에서 처리하며 접근 위반과 구분한다. |
| Manager Git 쓰기·후보 수용 | 현재 작업 대상·소유 자원·실행 자리·후보/index·Git operation·origin·원격 OID·PR·입력과 해당 쓰기의 기대값을 확인한다. 원격 경쟁은 재관측·통합·게시 판단의 입력이며 Agent 범위 위반으로 전환하지 않는다. |
| runtime 재개 | 현재 runtime의 정확한 path·SHA·process lock·등록 표식을 확인한다. 실패는 현재 작업 공간 오류로 처리하며 과거 Agent Git snapshot의 완전성으로 대신 판단하지 않는다. |

역할별 허용 결과는 [Worker의 구현과 문서 경계](#worker의-구현과-문서-경계)와 [Planner의 입력과 쓰기 범위](#planner의-입력과-쓰기-범위)를 따른다. Reviewer의 read-only와 역할상 테스트·verify 재실행 금지는 별도 조건으로 유지한다.

ignored 비교 view는 시작 ignore 규칙의 blob·index·작업 상태 또는 기록된 외부 exclude 서명이 일치하는 index-free artifact만 후보 비교에서 제외한다. 과거 snapshot은 감사로 보존하고 이 정규화를 Git 접근 권한 판정에 사용하지 않는다. 강제 staging·tracked 파일과 숨겨진 index 상태는 결과·현재 Git 쓰기의 검사 대상이다.

공통 ignored 충돌 helper는 들어오는 tracked 경로와 대소문자·부모·자식 관계까지 비교한다. rebase 시작·continue·skip·abort에 같은 보존 검사를 적용하며 대소문자를 구분하지 않는 파일시스템에서도 ignored 파일을 tracked 경로의 alias로 덮어쓰지 않는다. stage 1/2/3에서 stage 0으로의 충돌 해소는 새 후보로 검증한다. skip-worktree·assume-unchanged 등 관측을 숨기는 index flags는 실제 Manager Git 안전 조건에서 확인하며 Agent 접근 위반으로 비교하지 않는다.

| 후보·검증 근거 | 저장 단계와 재판정 |
|---|---|
| 저장 결과와 현재 후보가 같거나 definition·base·출력에 결합한 verify token이 현재 HEAD를 증명함 | 유효한 저장 단계에서 이어간다. |
| 후보 변경·현재 근거 불일치 | 현재 후보를 증명하지 못하는 verify·Review·게시 token은 무효화한다. 원래 실행·receipt·실패 감사는 보존하고 Worker 결과·verify receipt·선행 Review는 각각의 재사용 조건으로 다시 판단한다. |
| Review 중 로컬 후보 변경 | 접근 위반을 기록하지 않고 필요한 후보 검증·Review를 수행한다. 실제 문서·구현 입력이 부족한 경우에 Worker 교정으로 연결한다. |
| 구형 verify token | 최신 불변 verify 감사·정의 commit·출력·깨끗한 후보의 일치를 증명한 경우에만 전환한다. 과거 성공을 새 receipt로 합성하지 않는다. |

근거별 적용은 [관리 rebase와 후보 검증](#관리-rebase와-후보-검증), [성공 receipt와 수동 면제](#성공-receipt와-수동-면제), [일반 선행 Review와 부분 검증 증거](#일반-선행-review와-부분-검증-증거)를 따른다.

폐기된 Git 진단의 planning 오류·PLAN_BLOCKED·PROJECT_BLOCKED는 현재 작업 대상·실행 자리·Git operation과 원인을 재확인해 해당 원인만 해제한다. 복구 transaction에서 원인·작업·checkpoint generation·현재 후보를 다시 확인하고 독립 오류·승인·Review 차단을 유지한다. 결과·session·retry·시작/결과/기준 snapshot·실패 진단·해제 audit와 복원 intent는 기존 checkpoint 감사에 보존하며 폐기한 복원 intent의 활성 포인터만 제거한다. 사용자 파일·index·HEAD를 과거 기준선으로 복원하지 않는다. BLOCKED의 새 입력은 같은 현재 대상·Git 안전 조건에서 수용하며 실제 결과·문서 입력 오류는 별도로 검사한다.

Manager fetch·worktree·rebase journal은 실제 미완료 Git 전이의 자원·현재 기대값·소유자·확정 결과를 보존한다. 재시작은 현재 소유 자원과 journal의 실제 결과를 대조해 이어가며 과거 Agent snapshot 차이를 허용하기 위한 receipt gate로 사용하지 않는다. 수행하지 않은 Git 성공이나 sandbox 격리 성공을 합성하지 않는다.

작업 단계의 최신 base 확인은 선택 base ref만 fetch하고 같은 fetch 잠금 안에서 수신한 tracking OID를 확보한다. 다른 작업 branch·태그의 전체 동기화를 반복하지 않는다.

프로젝트 전체 원격 동기화는 branch 목록·OID를 한 번 조회하고 변경된 branch의 객체를 하나의 `fetch --stdin`으로 받는다. 목적 ref 없는 명시적 입력과 빈 `--refmap=`을 사용하고 prune·태그·submodule 갱신을 끈다. 같은 repository의 fetch와 `FETCH_HEAD` 결과 확보는 공유 fetch 잠금으로 보호하며, Git이 알려준 `FETCH_HEAD` 경로에서 branch별 OID를 읽어 조회 결과와 대조한다. 일반 branch 삭제나 OID 경쟁은 전체 재관측·일괄 수신을 한 번만 재시도하고 필수 base 부재·관측 실패는 보존·재시도 경계로 돌린다. 수신이 확인된 뒤 기존 ref별 journal과 예상 이전 OID로 추적 ref를 반영한다. 변경 없는 ref도 미확정 journal을 먼저 처리하므로 단일 fetch로 남긴 과거 기록과 일부 ref 반영 뒤 중단을 같은 형식으로 재개한다.

fetch의 관측·완료·복구 경계는 `symbolic-ref --quiet --no-recurse`로 정확한 추적 ref의 심볼릭 형상을 거부한다. dangling alias와 관측 실패도 부재로 대체하지 않는다. 공용 commit OID helper의 역참조 의미와 journal version 1의 `before`·`expected` OID 형식은 유지하되, 실제 갱신은 직접 ref인 현재 관측과 기존 소유 reflog가 함께 있어야 완료한다. 변경 없는 직접 ref는 기존 equal 관측 계약을 유지한다. 과거 receipt나 같은 referent OID는 새 alias를 승인하는 근거가 아니다. 심볼릭 형상을 만나면 원래 intent·실패 감사·완료한 다른 ref 전이를 보존한다.

실제 쓰기는 프로젝트 Git FIFO 보호 안에서 수행하며, prepare·raw 형상 검사·commit 또는 중단과 자식 회수까지 같은 순번을 유지한다. 일괄·단일·안내용 fetch가 공유하는 helper는 `update-ref --no-deref --stdin`의 `start` → `update <정확한 ref> <새 OID> <이전 OID 또는 0>` → `prepare`로 수행한다. Git이 정확한 대상의 잠금을 잡고 OID CAS를 검증한 응답을 받은 뒤, 그 잠금 안에서 raw 형상을 다시 확인하고 같은 프로세스에 `commit`을 보낸다. 확인 실패·중단 시 commit 없이 stdin을 닫고 자식을 회수해 prepared transaction을 중단한다. 사전 심볼릭 검사만으로 경쟁을 막거나 `--no-deref`만으로 alias를 보존한다고 가정하지 않는다. 이 조합은 관측 뒤 같은 OID의 alias로 바뀐 경우에도 referent를 따라 쓰거나 alias 자체를 치환하지 않으며 ref별 lease·journal과 실제 쓰기 대상을 일치시킨다. 기존 Git transaction 계약을 사용하고, 새 `symref-*` 명령이나 저장소 파일 형식에 의존하지 않는다. [Git update-ref 계약](https://git-scm.com/docs/git-update-ref)의 prepare 잠금·명시적 commit·EOF 중단을 따르며 Git 2.43에서도 제공하는 transaction을 사용한다.

transaction의 stdout 제어 응답과 hook·Git의 stderr 진단은 분리한다. stderr는 별도 임시 파일에 수집해 긴 hook 출력도 prepare 응답을 막지 않게 하고, 종료 후 기존 명령 결과의 진단에 합친다. 잠금 안의 형상 검사가 예외로 종료되면 transaction을 중단·회수한 뒤 원래 예외의 note에 진단을 보존한다. 정상 hook 진단 출력 자체는 ref 갱신을 거부할 근거가 아니다.

Codex의 `refs/codex/turn-diffs/*`와 다른 작업의 정상 공유 ref 변화를 해당 Agent에 귀속하지 않는다. 현재 게시 branch의 원격 tip은 이번 push의 통합·명시적 `--force-with-lease` 조건으로 관측한다.

Codex는 Manager가 `codex exec`로 직접 호출하며 선택 workspace의 내장 쓰기 제한과 granular 자동 승인 검토를 유지한다. Codex 프로세스 전체를 감싸는 바깥 sandbox와 중첩 sandbox 호환성은 호출 조건이 아니며 정상 session·로그 저장을 Agent의 workspace 밖 쓰기 권한과 구분한다.

## 일반 선행 Review와 부분 검증 증거

일반 선행 경로는 부분 verify → 일반 선행 Reviewer → 실제 전체 verify → 최종 승인 판단 순서다. 부분 PASS는 선택 모듈만의 결과이고 선행 PASS도 게시 승인이 아니다. 수동 제출·waiver·문서 draft 및 문서 변경 선행 Review의 별도 경로는 유지하며 선정·전환 조건은 [VERIFY.md](VERIFY.md#영향-기반-부분-실행)를 따른다.

| 실행·관측 결과 | 다음 단계·근거 |
|---|---|
| 실제 Reviewer identity를 관측할 수 없거나 부분 선정이 FALLBACK | 기존 전체 verify → Reviewer로 전환한다. FALLBACK은 전체 discovery 뒤에도 테스트·command를 실행하지 않는다. |
| 부분 verify FAIL 또는 부분 결과의 후보·run identity 불일치 | 기존 검증 실패·Worker 교정으로 처리한다. 선행 Reviewer·전체 verify 성공을 만들지 않는다. |
| 부분 verify PASS | 실제 선택 규모·미선택·불확실성·진단 위치와 전체 변경을 일반 선행 Reviewer에게 전달한다. 미선택은 NOT_SELECTED다. |
| 일반 선행 Review FAIL | 기존 Worker 교정·ISSUE_ID·level 정책으로 연결하며 전체 verify를 먼저 실행하지 않는다. |
| 일반 선행 Review PASS | 해당 승인 뒤 실제 전체 run을 실행한다. 전체 실패는 선행 승인을 무효화한다. |
| 실제 전체 verify PASS | 아래 후보·호출 identity와 해당 전체 run의 정확한 receipt ID가 일치할 때만 추가 Reviewer 없이 최종 승인한다. 불일치·증거 부재는 기존 최종 Review·필요한 재검증으로 처리한다. |

`verification/impact.py`는 기준/후보 커밋의 전체 추적 Python AST를 `maintenance/code_index.py`로 분석하며 양쪽 역방향 의존성을 실제 수집 명세로 정규화한다. `impact_analysis.py`는 원본 불확실성을 보존하고 고정된 동적 참조·정적 재노출·패키지 초기화 관계를 추가한다. 초기화 부작용 때문에 넓은 관계를 유지해야 해도 실제 API 사용을 우선순위 힌트로 추가할 수 있으며 이를 완전성의 증거로 사용하지 않는다.

변경 테스트는 필수이며 직접·가까운 간접 참조를 우선해 기본 24개 모듈 예산으로 선정한다. 변경 테스트는 예산을 초과해도 유지한다. 불확실성·공통 경계·문서 소비 범위 불명·관련 대상 부재에는 수집 명세의 공통 회귀 검사를 보완한다. 전역 불확실성·문서 병행 수정·50% 규모만으로 전체 경로로 전환하지 않는다. 문서 내용/mode·소비 근거·남은 불확실성·미선택 관련 대상은 `selection.analysis`와 전체 변경·승인 입력에 보존한다.

알고리즘 버전·후보·기준·설정·수집 명세 identity를 검증하며 과거 알고리즘 결과를 실행하지 않는다. 검증 계약에는 공통 core·runner·Manager adapter가 사용하는 `execution/verify_process.py`의 실행·timeout·프로세스 트리 종료와 `execution/windows_job.py`의 Job 격리·자식 종료 구현이 포함된다. 두 경로의 수정·삭제·이동은 전체 경로로 전환하며, 프로세스 관련 이름이나 `execution/` 전체로 범위를 넓히지 않는다. 후보/수집 명세 불일치·대상 부재·전체 필수 대상도 전체 경로로 전환해 부분 모드에서 전체 명세를 중복 실행하지 않는다. `runner.py`는 전체 discovery 뒤 선택 모듈만 기존 unittest adapter/core에 전달하고 후속 command는 전체 실행에서 수행한다. manifest·후보 안정성·fixture 소유자·필수 결과와 interpreter·guard·예약·timeout/자식 종료 경계는 유지한다.

`preliminary_reviews`는 전체 `verify_receipts`와 독립된 같은 작업 주기의 상태·후보/입력 identity·부분 결과·선행 결과를 저장한다. generation CAS와 전체 verify cycle 변경으로 오래된 쓰기를 거부한다. `partial_head`는 `verified_head`를 만들지 않으며 Worker 교정 감사에 검증 종류를 보존한다. 부분 실행 중단은 미완료로 남고 전체 경로로 재개한다.

최종 재사용의 후보 identity는 동일 HEAD/base/전체 diff·직렬화된 definition·Worker 결과·Manager 실행 코드·검증 설정·작업 대상을 결합한다. `preliminary_review.py`와 G-262의 `provider_execution.observe`가 Reviewer의 실제 instruction/provider/model/effort/환경/정책에 대해 호출 전·지정 invocation·현재 관측을 대조한다. 지정 invocation은 기존 완료 기록에서 확인하고 현재 관측도 새 CLI/model 호출 없이 인증·profile·native 지침·격리 정책을 확인한다. 현재 설정으로 과거 호출의 identity를 합성하지 않는다.

전체 성공 근거는 해당 선행 승인 뒤 실행한 `full_verify.receipt_id`와 현재 후보의 실제 `run` receipt가 같아야 한다. 다른 성공 receipt·reuse·waiver로 대신하지 않는다. Reviewer entry에서 현재 전체 verify token/receipt를 확인해 최종 token을 만들고 publication의 소유권·원격 확인 경계에서 identity를 재검사한다. 새 성공으로 과거 문서 선행 PASS를 승격하거나 G-183/G-241/G-262/263/264의 별도 목표 완료를 선언하지 않는다.

## Verify 입력 관측과 생성 요청

### 입력 목록의 경로·형식 계약

`scripts/verify-inputs.json`은 UTF-8 JSON object이며 `verification/inputs.py`가 작업 파일과 함께 관측해 `manager_adapter.observe_inputs`로 제공한다.

| 필수 필드 | 타입·유효값 |
|---|---|
| `version` | 정확히 정수 `1`. boolean·다른 버전은 거부한다. |
| `paths` | 항목이 1개 이상인 문자열 배열. 각 항목은 비어 있지 않고 아래 경로·glob 계약을 따르며 중복은 거부한다. |

최상위 키는 두 필드만 허용하며 추가·중복 JSON key는 거부한다. 예를 들어 `{"version":1,"paths":["src/**","scripts/verify.sh"]}`처럼 기록한다.

| 항목 | 계약 |
|---|---|
| 경로 | 대소문자를 구분하는 POSIX 상대 경로. 숨김 파일 포함 |
| glob | `*`·`?`는 한 segment, 독립된 `**` segment는 0개 이상의 segment와 일치 |
| 거부 입력 | 절대 경로·상위 이동·역슬래시·문자 class·중복 key/경로·미지원 버전 |
| 누락 | literal 누락은 오류, glob의 빈 결과는 관측 가능한 빈 집합 |
| 파일 | symlink를 따라가지 않음. 경로·내용·kind·mode를 관측 |
| 실행 | 목록 자체·선택한 저장소 verify 진입점·코드/테스트/의존성·검증 선언의 파일 입력을 결합 |
| 생성물·의존성 | Git 내부·bytecode·`.venv` 등 설치 환경은 비교 대상 밖이다. 저장소 의존성 선언·lock 파일과 테스트 고정 자료는 코드 입력이다. |

관측은 R-072의 저장소 코드 입력 기준을 사용한다. `InputObservation.proven`은 유효 목록·저장소 코드 입력 관측·목록 밖 코드 누락 확인과 `fingerprint`/`reason`으로 판단한다. 파일을 읽거나 비교할 수 없는 경우는 미확인이며 runtime·Git/외부 identity의 `None`은 그 자체로 미확인이 아니다. 기존 환경 관측 자료는 감사로 보존할 수 있으나 코드 기준 재사용 gate로 사용하지 않는다. 목록 밖 제외 근거는 아래 completeness token의 코드 입력에 결합한다.

입력 목록의 부재·오류는 관측을 미확인으로 만들어 전체 verify를 유지한다. 공통 runner 연결 선언 `scripts/verify.json`의 형식·지원 오류는 [공통 verify 실행 경계](#공통-verify-실행-경계)에 따라 실행 실패이며 두 파일의 오류 처리를 혼동하지 않는다.

### 자체 verify 입력과 고정 샘플

자체 `scripts/verify-inputs.json`은 다음 입력을 고정 목록으로 관측한다.

- `loop_manager.py`, `loop_manager_internal/**`, `tests/**`, `requirements.txt`
- `scripts/verify.json`, `scripts/verify.sh`, `scripts/verify.ps1`, `scripts/verify_windows.py`, `scripts/verify-windows-tests.json`, `scripts/setup.sh`
- `scripts/copy-project-files.sh`, `scripts/copy-project-files.ps1`
- `.agents/skills/specification-docs/scripts/**`, `run.sh`, `run.ps1`

저장소 runtime의 공통·역할·Ponytail 원문 `docs/AGENTS.common.md`, `docs/AGENTS.planner.md`, `docs/AGENTS.planner_check.md`, `docs/AGENTS.worker.md`, `docs/AGENTS.reviewer.md`, `docs/AGENTS.ponytail.md`는 자체 verify 목록에서 제외한다. 지침 원문을 직접 읽던 정책 문구 테스트는 테스트가 소유한 고정·임시 원문을 사용하며 `tests/**`의 이 fixture들은 계속 목록에 포함한다. 이 제외는 자체 verify 입력 지문에만 적용한다. `agents/common_guidance.py`의 현재 runtime 원문 선택과 planning·호출별 실제 전달 해시는 그대로 실제 지침을 읽어 계산한다.

`tests/**`의 등록·profile·프로젝트 준비 검증은 문서·설정 예제 대신 고정 샘플을 사용한다. 복사 회귀는 실제 copy 스크립트 bytes를 샘플 source root에서 실행해 결과 bytes·경로·기존 파일 보존·재실행 거부를 확인한다. 배포되는 조회 도구와 Skill 자체 문서 파서는 실제 실행 코드 bytes를 샘플 source에 배치해 배포 결과·조회 동작을 확인한다.

샘플은 현재 문서 원본에서 자동 생성·동기화하지 않는다. `AGENTS.md`·README·docs와 `.agents`의 prose·`config.ini.example`·사용자 운영 설정·소비하지 않는 setup/ignore/attributes 변경은 자체 verify 입력이 아니다. Windows verify driver·선택 목록과 Linux 실행 경로 회귀가 임시 source에 복사해 실행하는 `scripts/setup.sh`는 입력이다. setup의 bytes·kind·mode 변경은 입력 지문과 기존 제외 근거를 바꾼다. 소비하지 않는 다른 플랫폼 setup은 추가하지 않는다. 이 제외는 원문의 일관성·품질을 확인하지 않으며 필수 테스트·전체 compile·실제 verify/copy 스크립트 검증을 축소하지 않는다.

verify 기록은 실행 시 Python의 bytecode write 정책·cache tag·prefix와 프로젝트 Python cache의
전후 개수, 생성·변경·유지·삭제 수를 남기며 snapshot 소요 시간을 별도 phase로 기록한다.
비교는 bytecode 설정이 같은지 표시하고 실제 run 시간과 cache 변화를 함께 제공한다. cache 존재를
실제 재사용·검증 성공이나 속도 향상으로 대신 판단하지 않는다.

### 목록 밖 입력과 누락 요청

목록 밖 파일의 제외 확인은 경로·내용·kind·mode, 선택 저장소 verify 진입점·판정 규칙과 목록에 포함된 코드 입력의
경로·내용·kind·mode에 결합한 completeness token으로 제한한다. 저장소 검증 스크립트·선언 변경도
기존 제외 확인을 무효화하며 실행 머신의 환경·설정·외부 동적 입력은 이 token에 요구하지 않는다. verify 성공은 이 근거를 만들지 않는다. 관측 API는 실행·성공 저장·
재사용 정책과 분리하며, 목록 부재/오류는 기존 전체 verify를 유지한다.

일반 planning의 prompt 준비는 최신 `origin/<base>`의 고정 revision과 실제 목록을 조회한다.
오래된 planning branch는 새 누락 요청을 만들지 않는다. 원격 활성 계획(draft 포함)과 SQLite의
미종료 계획(PR 포함)에 관련 작업이 있으면 요청을 생략한다. 원격 계획 선언 오류로 중복 여부가
불확실하면 입력 목록 생성 요청만 보류하고, 기존 catalog의 오류 격리와 일반·유지보수 planning을
유지한다. Git 조회 실패는 선언 오류로 숨기지 않는다. Manager 요청은 additive SQLite
`verify_input_requests`에 프로젝트·revision·목적·규칙 원문/버전을 보존하며 같은 요청을 재사용한다.
`manager:verify-inputs` 계획은 다음 선언을 유지한다.

| 선언 | 형식·검사 |
|---|---|
| `sources` | `[manager:verify-inputs]`만 사용하며 제품 목표·다른 source를 섞지 않는다. |
| 요청 한 줄 | `MANAGER_REQUEST: ` 뒤에 JSON object를 정확히 한 번 기록한다. key는 `id`·`revision`·`purpose`·`version`만 허용하며 추가·중복 key는 거부한다. |
| `id`·`revision` | ID는 소문자 64자리 16진수 문자열, revision은 소문자 40자리 또는 64자리 commit SHA 문자열이다. |
| `purpose`·`version` | 정확히 `"create-verify-inputs"`와 정수 `1`이며 boolean은 거부한다. |

`## Purpose` 본문은 등록 규칙인 `Maintain scripts/verify-inputs.json from the complete project and actual verify scripts; preserve full verification and review missing inputs.`와 정확히 일치해야 한다. Planner 결과와 원격 catalog 등록에서 프로젝트·저장 요청 ID·revision·목적·규칙 원문/버전을 대조하며 임의 예약어·제품 목표 혼합을 거부한다.

실제 누락 요청에는 새 계획의 `priority: critical`을 명시한다. 신규 Manager source 선언은 요청 ID·revision·목적·규칙 원문·단독 source 범위와 이미 파싱된 metadata의 priority를 모두 검증해야 등록한다. 최초 등록은 `critical`만 허용하며 `high`·`normal`·`low`·priority 생략은 거부한다. 다른 source 계획의 우선순위 해석은 바꾸지 않는다.

신규 판정은 현재 등록 row·게시 intent와 revision-pinned catalog 근거로 수행한다. 등록된 계획·draft·PR의 기존 선언은 재검사로 보류하지 않고 멱등 재개도 새 등록으로 취급하지 않는다. 일반·유지보수 planning의 신규 publication은 catalog 사전 검사와 transaction 내 request 검증을 거친 뒤 최초 row 삽입에서도 priority를 확인한다. 단, 저장된 일반·유지보수 publication intent의 batch/manifest와 원격 commit 포함이 확인된 복구는 이미 게시된 batch의 재개이므로 request identity·scope 검증은 유지하면서 priority를 소급 적용하지 않고 보존된 metadata로 등록을 마친다. priority가 잘못된 신규 계획은 격리되며 SQLite 실행 row나 draft 대기 증거를 만들지 않는다. archive fallback은 과거 선언을 보존하고 이 조건을 소급하지 않는다.

원격 재조정과 후속 planning은 같은 신규 선언 priority 검사로 hold 원인을 구성한다. priority 생략에서 파서가 기록한 오류에 metadata의 기본값으로 별도 priority 원인을 덧붙이지 않아, 변경되지 않은 hold가 후속 planning의 원인 비교를 통과하고 독립 유효 계획을 보존한다.

예약어는 수동 완료 후보를 포함한 문서 조회와 Requirement retirement에서 제외하고 규칙 내용을
source 지문에 결합한다. 최종 전문 확인의 후속 계획도 등록 요청 검증을 통과한 예약 source를
허용하며 활성 명세 전문·구현 근거 및 draft 조건은 유지한다.

### 성공 receipt와 수동 면제

실제 전체 실행·재사용·사용자 면제는 서로 다른 근거다. `storage/verify_receipts.py`의 `verify_receipt_cycles`·`verify_receipts`는 프로젝트·계획·작업 주기·generation과 실제 입력·출력·결과를 보존한다.

| receipt 종류 | 저장 결과·재사용 의미 |
|---|---|
| `run` | 실행 intent의 `pending`에서 실제 완료 `success`·`failure`로 확정한다. 새 intent는 CAS로 현재 선택을 바꾸므로 실패·중단 뒤 이전 성공으로 되돌아가지 않는다. |
| `reuse` | 같은 주기의 증명된 입력을 재관측해 원래 성공 run의 `origin_id`와 현재 HEAD·출력·이유를 연결한 `success`다. 새 verify 실행·validation 실행 감사·사용량을 만들거나 실패 이력을 초기화하지 않는다. |
| `waiver` | 실행하지 않은 사용자 면제의 `waived` 근거다. 수동 결과는 `SKIPPED_BY_USER`이며 실제 PASS나 재사용 가능한 성공 run으로 바꾸지 않는다. |

일반 activation은 새 주기를 만들고 유지보수의 기존 작업 재개는 주기를 보존한다. 구형 활성 작업은 새 주기로 시작하며 과거 성공 token을 receipt로 합성하지 않는다. 실행 전후 입력·HEAD·작업 파일·runtime 소유 상태를 대조하고 성공 receipt·후보 token을 같은 transaction에서 확정한다. 저장 경쟁·실패는 현재 후보의 성공으로 인정하지 않는다.

재사용은 현재 주기에 선택된 실제 전체 성공 run과 현재 저장소 코드 입력 관측이 일치할 때만 허용하며 새 부분 검증 시작보다 먼저 판단한다. 이미 진행한 일반 선행 Review의 PASS·승인 무효화 뒤에는 동일 입력의 과거 성공이 있어도 별도로 연결된 실제 전체 run이 필요하다. 재사용할 성공이 없으면 기존 부분 검증 경로를 따르고, 부분 검증에서 전체 실행으로 이어질 때 입력과 현재 단계 상태를 다시 관측한다. 목록·fingerprint 계산이나 verify 성공 자체로 목록 밖 코드 입력 누락을 해소하지 않는다. 환경 재현성·외부 동적 입력의 동일성은 요구하지 않는다. 실제 지원 범위는 [운영 입력 관측 adapter](#운영-입력-관측-adapter)를 따른다.

| 수동 complete/submit 면제 조건 | 적용 |
|---|---|
| 요청 | 단계별 skip 옵션과 원격 commit 전체 SHA인 `--expected-head`가 필요하다. `--skip-reason`은 선택 입력이며 생략·빈 문자열·공백은 `manual_work_requests.reason`에 빈 문자열로 저장한다. skip 옵션 없는 reason 단독 지정은 거부한다. SHA는 소문자 40자리 또는 64자리 16진수이며 수락 시 관측 HEAD와 일치해야 한다. |
| 실제 실패 | 같은 묶음·단계·후보 HEAD에 실제 FAIL 기록이 있으면 `execution/manual_candidate.py`가 면제를 거부한다. 면제로 PASS를 만들거나 실패를 지우지 않는다. |
| 후보·설정 증명 | 수락한 후보·문서·설정 token이 현재 입력과 같아야 한다. 관측 불가·불일치는 skip·과거 단계 근거를 사용하지 않고 기본 단계를 실행한다. |

`manual_work_requests`는 후보 token과 단계별 유효 설정 token을 보존한다. verify 설정은 기존 managed-config loader의 timeout·parallelism, Review 설정은 verify 설정과 기존 profile resolver가 선택한 provider·Model·Effort를 결합한다. 실행 중 receipt·최종 게시·PR 대기 재평가에 같은 계산을 사용한다.

생략 결과의 감사 `detail`은 `SKIPPED_BY_USER`와 후보 SHA를 유지하면서 사유 제공 여부를 표시한다. Reviewer 입력·PR 설명·상태 출력은 같은 formatter를 쓰며, 사유 제공 여부의 원본은 각 result의 `request_id`가 연결한 request의 `reason`이다. 따라서 formatter는 legacy `detail` 문자열로 의미를 추측하지 않고 실제 제공 사유가 표시 문구와 같아도 보존한다. 출력 경계에는 기존 비밀값 redaction을 적용하며 과거 request·result 원문은 표시를 위해 다시 쓰지 않는다.

각 단계 시작 때 실제 verify timeout·coordinator parallelism과 Reviewer에 전달할 profile을 고정한다. 완료 시 설정을 다시 읽어 수행 당시 지문을 바꾸지 않으며 실행 중 입력 변경 뒤의 현재 근거로 재사용하지 않는다. 새 열이 없는 기존 요청은 `NULL`로 유지해 당시 입력을 소급 추정하지 않는다. SQLite에는 token·비밀값 없는 무효화 사유만 저장하고 설정 원문·Model 인증 자료는 저장하지 않는다.

### 운영 입력 관측 adapter

`ValidationEffects.observe_verify_inputs`는 R-072의 저장소 코드 입력 관측 경계다. 채택 기준은 일반 프로젝트 스크립트·자체 공통 runner·기존 isolated compile에 공통으로 적용한다.

| 관측 대상 | 코드 기준 재사용 계약 |
|---|---|
| 목록·파일 | 유효한 목록과 실제 소스·테스트·fixture·의존성 선언·검증 스크립트/선언의 bytes·경로·kind·mode를 관측한다. 미커밋·untracked도 포함한다. |
| 진입점·판정 규칙 | 선택한 저장소 verify 진입점과 목록 해석·제외 확인·재사용 판정 기준을 결합한다. |
| 범위 밖 | 설치 Python/Git/Bash/Perl·native library·환경변수·사용자/시스템 설정·외부 파일/동적 입력·Git HEAD/이력/revision 값은 지문·완전성의 필수 조건이 아니다. |
| 전체 실행 | 목록/코드 관측 오류·코드 입력 누락 의심·변경·유효한 실제 전체 성공 부재에서는 전체 verify를 실행한다. |

일반 경로와 isolated compile의 입력 관측은 같은 코드 기준을 사용하며 `runtime_identity=None`·환경·Git/외부 identity의 미확인이나 변경만으로 재사용을 거부하지 않는다. 선택한 저장소 진입점과 연결 선언이 목록에서 빠지면 미확인이다. fingerprint는 선택 파일과 진입점의 상대경로, 목록 bytes·해석 규칙, 실행 중인 관측·runner/adapter·receipt 판정 코드 bytes를 결합한다. 예전 환경 지문을 현재 값으로 합성하지 않으며 기존 receipt와 실패 감사는 그대로 보존한다. compile 실행 fast path는 유지하지만 더 이상 설치 runtime을 측정하지 않는다.

문서 제외는 현재 파일 관측과 코드 소비 관계의 재확인을 결합한다. 고정 POSIX launcher·unittest의 제한된 정적 증명은 기존 `impact_analysis.review_document`를 사용한다. 일반 스크립트·native/동적 소비는 Worker가 진입점·수집·테스트의 실제 저장소 소비를 검토하고, 특정 목록 밖 경로가 내용과 무관하게 소비되지 않는다는 근거를 기존 `worker_result`에 기록한다. 새 DB나 프로젝트별 중앙 경로 설정은 두지 않는다. Reviewer에게 저장된 선언을 실제 verify receipt 설명과 함께 전달해 목록 누락과 소비 검토를 확인하게 한다. 선언은 PR Summary나 targeted test 결과에 섞지 않는다.

Worker 결과의 선택적 단일 `VERIFY_INPUT_EXCLUSIONS: <JSON>` 선언은 다음 version 1 계약이다. 읽기 전용 `verification/inputs.py --review-request <worktree> --entrypoint <상대경로>`는 identity만 출력하며 소비 검토나 승인을 대신하지 않는다. Worker는 최종 코드 기준으로 identity를 얻고 실제 확인한 `basis`를 추가한다. 실제 확인한 다른 플랫폼 진입점은 `--entrypoint`를 반복한다.

| 필드 | 의미 |
|---|---|
| `version` | 정수 1. 중복 key·중복 선언·잘못된 형식은 제외 권한으로 채택하지 않는다. |
| `codes` | 검토한 진입점별 현재 코드 fingerprint의 비어 있지 않은 문자열 목록. 선택 파일·목록·진입점·판정 구현과 제외 정책을 결합한다. |
| `outside` | 정렬한 목록 밖 정확 경로 목록의 SHA-256. 경로 추가·삭제·이동은 새 소비 검토가 필요하다. |
| `operating_paths` | 내용·존재 여부와 무관한 운영/예제 설정의 정확한 상대경로 배열. glob·중복·안전하지 않은 경로는 거부하며 manifest 선택이 항상 우선한다. 검증이 소비하는 고정 설정/자료는 여기에 넣지 않는다. |
| `basis` | 해당 경로의 어떤 내용도 실제 검증 소비에 영향을 주지 않는다는 Worker의 구체적인 소비 검토 근거. PASS나 identity 출력은 이 근거가 아니다. |

Manager는 현재 작업의 저장된 Worker 결과에서 이 선언을 읽고, 현재 코드 fingerprint와 목록 밖 경로 집합을 다시 대조한다. 목록 밖 파일은 현재 bytes·kind·mode로 두 번 관측해 새 completeness를 확정한다. 같은 경로의 문서 bytes·mode 변경은 내용에 의존하지 않는 소비 관계가 그대로일 때만 새 근거로 갱신한다. symlink·special 파일·읽기 실패·관측 경쟁·코드나 진입점/규칙 변경·새 목록 밖 파일은 재사용하지 않는다. stale 선언을 다른 증명으로 조용히 대체하지 않는다. 명시한 운영 설정만 내용·존재·부재를 비교에서 제외하며 manifest에 선택된 설정은 계속 코드 입력으로 비교한다. Windows snapshot의 전체 tree 준비·bundle 감사는 코드 입력 비교를 대신하지 않으며, 실제 WSL/native 검증이 소비하는 저장소 코드·선언은 목록에 남긴다.

실행 전후·reuse 확정 직전과 Review/게시 revision gate에서 코드 입력을 재관측한다. 외부 입력 관측은 DB 처리 밖에서 수행하고 짧은 확정 transaction에서 같은 실행 상태·소유 identity를 재확인한다. 코드가 달라졌거나 읽기·비교를 확정할 수 없으면 필요한 verify로 돌아간다. 과거 receipt는 새 기준의 코드 입력·원래 run·작업 주기·후보 연결을 확인할 수 있는 경우만 재평가하고 과거 입력을 현재 값으로 합성하지 않는다. reuse는 새 verify 실행·성공 감사·사용량을 만들거나 새 실패를 지우지 않는다. Reviewer의 명세·diff·실제 지침·승인과 publication token·원격 lease는 별도 조건이다. 일반 선행 Review PASS 뒤 실제 전체 run과 수동 면제의 별도 후보/설정 계약은 유지한다.

## Architecture Change Policy

이미 승인된 Requirement·ExecPlan·사용자 결정의 범위 안에서는 채택한 설계와 호환 계약을 구현하고, 실제 변경과 함께 두 설계 문서를 갱신한다. 같은 결정의 승인을 반복 요청하거나 아래 변경 종류에 해당한다는 이유만으로 ExecPlan을 일괄 차단하지 않는다.

다음 경계에 **새로운 미결정 사항**이 생기거나 승인 범위를 벗어나는 경우에는 관련 구현·게시를 멈추고 근거·선택지·영향·필요한 사용자 결정을 기록한다.

- SQLite schema 또는 상태 모델의 비호환 변경
- worktree, branch, commit, push와 PR 생성 경계 변경
- 인증 또는 비밀값 저장 방식 변경
- 새로운 외부 infrastructure나 비용 발생 서비스 도입
- 데이터 삭제 또는 비가역 migration
- 승인된 호출·관리 한도를 벗어나는 병렬 실행 정책 변경

Planner는 [draft와 사용자 판단](PRODUCT.md#draft와-사용자-판단)으로 미결정 사항을 분리한다. Worker는 사용자 결정이 필요한 결과를 보고하고 Reviewer는 명세 누락·상충을 명세 교착으로 보고한다. Manager가 원인·영향 범위에 맞는 대기·차단 상태와 재개 조건을 기록하며, Agent가 직접 상태를 바꾸거나 독립 작업까지 일괄 중단하지 않는다. 기존 승인·안전·필수 검증 조건은 유지하고 실제 해소가 확인된 원인만 해제한다.
