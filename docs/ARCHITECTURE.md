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

편집은 활성 Requirement의 식별 표현·확정 상대 링크와 비보관·비draft ExecPlan의 확정된 활성 source 경로만 대상으로 한다. ExecPlan 본문·다른 선언과 archive·`User Decision`은 보존한다. 코드 예제는 링크 편집에서 제외하며, 중첩 blockquote와 목록의 접두어·연속 들여쓰기·컨테이너 종료를 확인해 fenced·들여쓴 코드의 원문도 보존한다. 코드 내부의 목록 표식은 새 컨테이너로 해석하지 않으며 코드 종료 뒤 일반 링크 교정을 계속한다. 파일 적용·원격 재검사·게시와 중단 복구는 별도 Manager 경계가 소유한다.

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

Merge 근거가 검증된 보관 작업의 과거 ID·경로 참조는 삭제 복구의 활성 영향 대상에서 제외한다. 이미 저장된 참조 보류도 같은 기준으로 재평가하며 보관 문서·Merge 이력을 수정하거나 재실행하지 않는다.

draft 판단 대기와 검증 피드백 이행 표식은 실행·소유 이력이 아니다. 미착수·무소유 작업의 원격 삭제를 증명하면 해당 표식 때문에 취소를 보류하지 않으며 draft 대기를 해제하고 취소 감사를 보존한다. 실행 row가 사라진 뒤 남은 판단·이행 감사는 같은 ID의 신규 등록을 계속 막는다.

| 보존된 이력 | 현재 catalog·복구에서의 처리 |
|---|---|
| archive 전용 감사 | 원문 파일·source 링크 부재를 활성 선언 오류나 새 등록으로 만들지 않는다. 완전한 Merge proof가 있으면 실행 row·문서 없이도 dependency·완료 판단의 MERGED 근거로 사용하며 미확인 이력은 충족으로 계산하지 않는다. |
| 취소 감사 | 저장된 plan/deletion revision·hash·metadata로 원문·identity를 확인하고 현재 snapshot의 참조 영향과 관측 revision을 기록한다. 근거 부재는 미확인으로 보존하며 취소 ID·원문을 독립 실행을 막는 활성 source로 사용하지 않는다. |
| 실행 row 없이 남은 hold의 원격 삭제 | hold의 원문·revision·최초 관측 revision·미확인 사유를 보존하고 후속 참조가 생기면 그 영향과 함께 Planner 복구 목표로 전달한다. 영향 계획의 복구 판단·게시 전까지 hold를 유지하고 snapshot/SQLite CAS가 바뀌면 감사 갱신을 적용하지 않는다. |
| 과거 ID의 active 재등장 | archive·취소 ID의 예약을 유지해 새 계획 등록을 거부한다. |

현재 `remote_deletions.py`는 고정 원격 revision과 마지막 채택 원문·선언·실행/게시 이력을 대조해 실제 Requirement·ExecPlan 삭제를 관측한다. archive·rename·조회 실패를 실제 삭제와 구별하고, 확인된 ExecPlan 삭제의 취소 감사와 Requirement 삭제에 따른 목표 복구 입력을 만든다. 일반 Planner의 철회·유지·대체 판단은 허용된 계획 개정·삭제 게시와 연결하며 Manager가 참조·소유권·현재 PR을 재검증한다. 확인되지 않은 범위는 보류하고 검증된 독립 작업은 계속한다.

삭제 관측·취소 경로가 있다는 사실만으로 후속 정리가 모두 완료된 것은 아니다. 취소 감사는 원문·실행 이력·예약과 소유 자원의 `cleanup_pending`을 보존한다. 유지하는 관리 브랜치의 문서 동기화, 취소·완료 작업의 소유 로컬·원격 branch 및 worktree 정리와 각 확정 지점의 중단 복구는 [R-055의 삭제 반영·복구 목표](requirements/%5BR-055%5D%20%EC%B0%A8%EB%8B%A8%20%EA%B2%BD%EB%A1%9C%20%EC%A0%84%EC%88%98%20%EC%A1%B0%EC%82%AC%EC%99%80%20%EB%B6%88%ED%95%84%EC%9A%94%ED%95%9C%20%EC%9E%91%EC%97%85%20%EC%A4%91%EB%8B%A8%20%EC%B6%95%EC%86%8C.md#삭제의-db브랜치-반영과-중단-복구)에 남은 범위다. 현재 Merge 정리와 삭제 처리의 권한을 이 목표로 확대하지 않는다.

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

Agent snapshot과 격리 ledger는 원문을 감사로 보존한다. 공유 ref·config·다른 worktree 변화나 과거 실행·격리 기록의 부재는 접근 권한 판정 또는 재개 조건으로 사용하지 않는다. Manager의 현재 작업 대상 연결과 Git 쓰기의 충돌·보존·lease 조건은 실제 동작 경계에서 검증한다.

Agent preflight의 digest는 현재 역할·canonical worktree·repository·작업 identity에 결합한다. mutable Git snapshot을 digest에 넣거나 과거 OID와 일치시키지 않으며, provider가 실제 적용한 sandbox 정책과 설정 identity는 호출·재개·fallback마다 확인한다. 저장된 시작·결과 snapshot은 역할별 결과 유효성과 성공 후보의 재사용 근거로만 사용한다. 현재 후보가 바뀌면 이전 verify·Review·게시 token을 무효화하고 필요한 단계부터 수행한다.

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

실제 단계 진입 전 `RuntimeHandoffGate.observe()`가 관측을 직렬화한다. 실행·planning·복구 coordinator와 실제 Agent/verify 배정 경계가 이를 호출한다. 최초 관측은 즉시 수행하고 성공 완료의 monotonic 시각을 runtime 전체에서 공유해 30초 미만이면 원격 조회·입력 비교를 재사용한다. 병렬 진입은 잠금 안에서 시각을 다시 확인하며 실패는 성공 시각을 갱신하지 않는다. 단계 종료와 재시도·사용량·기존 호출 종료 대기는 원격 관측을 만들지 않으며 pause·확정된 handoff 신호만 확인한다. 대기가 풀린 뒤 실제 단계 진입에서 같은 30초 기준으로 다시 확인한다. 관측 잠금을 기다리기 전에 완료한 단계의 implicit DB 요청을 확정하고 명시적 transaction 내부 관측은 거부하므로 DB 쓰기 transaction·scheduler condition·launch lock 안에서 원격 관측하지 않는다. 실제 실행 SHA와 원격 commit의 raw diff 및 blob을 비교해 추가·삭제·이동·mode/kind 변경을 포함한다. 알려진 문서 위치의 일반 UTF-8 prose만 제외하고 실행 bit·symlink·shebang·Python 구문과 문서 아래 실행 코드는 포함한다. 알 수 없는 경로와 비교 실패는 문서 전용 성공이 아니다.

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

### Planner 실행과 Requirement 완료 확인

일반 Planner의 최초 수립·명세 변경 판단은 최신 원격 PRODUCT·활성 Requirement·ARCHITECTURE와 적용된 일반 planning 성공 기준을 비교하며, 지침 변경은 [계획 변경 감지](#계획-변경-감지)의 별도 조건을 따른다.

Planner도 미충족 요구와 필요한 계획을 판단하기 위해 관련 ExecPlan·현재 구현·테스트·설정을 읽는다. 이는 호출 조건과 구분한다. ExecPlan·코드·테스트·설정의 변경이나 전체 저장소 SHA 차이만으로 일반·점검·작업별 영향 Planner를 호출하지 않는다. ExecPlan 선언은 Manager의 catalog·실행 자격 검증에도 사용하고 구현·검증 대응은 Worker·Manager verify·Reviewer의 기존 경계에서 처리한다.

Manager가 유효한 선언·source·dependency·사용자 결정을 반영하고 Worker가 최신 작업 조건과 현재 코드를 대조해 필요한 구현·targeted test를 수행한다.

Planner의 최초 계획 수립·명세 변경·draft 사용자 답변·추적 작업이 필요한 명시적 수동 제출·유지보수·Requirement 완료 재확인·프로젝트 최종 확인은 R-082의 목적별 조건을 적용한다. 같은 경계의 여러 대상·목적은 한 호출로 처리할 수 있으며, Requirement별 판단·프로젝트 전체 planning·최종 전문 확인·유지보수의 입력과 성공 근거를 구분해 보존한다. 재시작·원격 재관측에서는 유효한 결과·게시 intent의 남은 적용부터 복구하고, 실제 입력 변경·필수 판단 누락·계획 작성 범위의 충돌만 해당 세션 교정으로 연결한다.

PR 상태 동기화 뒤 마지막 연결 ExecPlan이 실제 MERGED가 된 활성 Requirement는 유효한 source 관계·관련 draft·현재 입력을 확인해 완료 재확인 대상으로 삼는다. 다른 Requirement의 READY·Open PR·완료나 프로젝트 전체 종료를 기다리지 않으며 독립된 새 작업·유지보수 cycle 선택 전에 처리한다.

각 Acceptance Criterion을 최신 관련 명세·구현·테스트·실제 검증에 대조해 충족은 판단 기록·원문 archive, 미충족은 후속 ExecPlan, 필수 사용자 결정은 draft, 불확실은 보류로 반영한다. 같은 입력의 유효한 판단은 재사용하고 archive 게시·catalog·DB 적용은 기존 소유권·publication transaction·원격 재확인 경계를 유지한다.

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

안정적 완료·작업 소진의 원본은 현재 원격 입력·catalog·SQLite 상태다. 완료 기록의 `cycle_key`는 프로젝트의 유효 planning 입력 지문으로 관리하며 최종 확인의 `work_cycle`과 구분한다. `project_completion_cycles`의 완료 시각·마지막 Merge 계획과 `work_reason_episodes`의 snapshot 지문·세대는 Telegram 활성 여부와 독립적으로 유지한다. 완료·대기 outbox는 만들지 않는다. Merge 후 worktree cleanup·ExecPlan archive pending은 완료를 막지 않지만 미완료 planning publication·활성 writer·안전 gate를 우회하는 근거로 사용하지 않는다.

### 계획 게시와 작업 공간

계획을 실행 이력에 적용하기 전에 원격 게시와 catalog 전체의 유효성을 확인한다. 계획·FUTURE_IDEAS 변경이 없으면 관측한 두 입력 지문을 저장하고 planning worktree를 정리해 완료 여부를 확인한다.

1. 생성·개정·rename·삭제·FUTURE_IDEAS 변경을 분류하고 새 파일의 ID·경로를 아래 규칙으로 정규화한다. snapshot 원본, SQLite의 현재 실행 근거·PR 부재 및 적용 후 catalog의 ID·source·dependency graph를 검증하며 시작한 계획의 ID·경로와 실행 이력은 보존한다.
2. 실패는 같은 Planner 세션에서 최대 3회 교정한다. 소진하면 원인·입력 지문·결과·worktree·session을 보존하고 같은 실패 입력의 신규 Planner 호출을 반복하지 않는다. 미종료 writer·미확정 게시 intent가 없고 소유권·Git operation·저장된 역할 결과의 유효성을 검증할 수 있으면 실행 자리를 비워 미확인 영향 밖의 게시된 독립 작업과 PR 관측을 계속한다. 해제 조건이 불확실하면 기존 상태를 보존해 대기하며 `planning_error`는 원인이 실제 해소되기 전까지 유지한다.
3. 검증된 전체 변경을 planning worktree의 한 commit으로 만든다.
4. 아직 게시하지 않은 동안 원격 base가 전진하면 변경·session을 보존해 반영하고 최신 catalog·대상 권한과 관련 문서를 재검증한다. 충돌·원격 재작성·소유권 불명확은 작업을 보존해 복구 경계로 보낸다.
5. 기준과 mutable READY snapshot이 그대로면 복구 가능한 publication intent를 저장하고 base에 fast-forward push한다.
6. 원격 commit을 확인한 뒤 생성 row 등록·개정 cache 갱신·삭제 row 제거와 ID 취소 감사·두 성공 지문·planning 정리를 한 SQLite transaction으로 반영한다. FUTURE_IDEAS-only 변경은 실행 상태를 만들지 않는다.

#### 정규화 기록과 작업 공간

snapshot의 rename으로 확정되지 않은 미게시 새 파일은 전체 catalog를 읽기 전에 별도로 정규화한다. Manager는 active·archive 파일과 SQLite 등록·취소·수동 예약 및 mutable/held snapshot에서 사용 중인 ID를 모아 새 batch에 연속 번호를 배정하고 ID·title에 맞는 새 경로로 옮긴다. 같은 batch의 서로 다른 새 파일이 같은 ID를 쓰면 배정을 멈춘다.

새 계획과 이번 batch에서 수정한 snapshot 계획의 `depends_on` 중 대상이 확인된 batch ID만 새 ID로 갱신하며, 기존 dependency 순서·계획 identity·본문은 보존한다. 기존 ID와 batch ID가 충돌하는 관계는 의미를 정할 수 없어 오류로 남긴다. 재배정 대상 ID가 본문에 언급돼도 이를 고치거나 대상을 추론하지 않고 오류로 남긴다. draft와 `User Decision`의 원문은 보존한다. 정규화 뒤 기존 전체 catalog·source·dependency 검증을 수행한다.
정규화는 일반·유지보수 planning 모두에 적용한다. 원본 bytes·mode와 대상 hash를 편집 전에 복구 journal에 기록하고, 완성한 임시 파일을 원자적으로 설치한다. 다음 검증 진입에서 미완료 journal을 확인해 원본을 복구한다. 복구로 제거된 journal 소유 임시·대상 경로는 호출자가 미리 수집한 변경 목록에서 제외하고, 복구 후 현재 변경을 다시 관측해 범위를 검증한다. 예상과 다른 파일이 있으면 덮어쓰지 않고 작업과 journal을 보존한다.

정규화 결과는 기존 planning result와 분리해 원본 경로·ID, 배정 경로·ID로 일반 planning manifest 또는 유지보수 publication intent에 보존한다. manifest의 기준 revision, 실제 commit 경로와 파일 hash를 대조하고, 원격 확인 및 catalog 등록은 기존 publication intent·lease 경계를 사용한다. 재개는 저장된 manifest를 그대로 사용하며 revision·diff·소유권이 다르면 게시하지 않는다.

planning worktree는 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`, 구현 worktree는 `<state-dir>/worktrees/<project-id>/<execplan-id>/`에 둔다. 구현 branch 이름은 `loop-manager/<execplan-id>`다. 계획은 Worker 시작 전에 base에 게시하며 구현 PR에서 처음 생성하지 않는다.

#### 개정 snapshot과 작업 취소

일반 planning snapshot은 실제 PR이 없는 READY·진행 중 선언과 일반 `PLAN_BLOCKED` 선언을 Git 원문과 SQLite의 선언·실행 근거에 결합한다. 차단 상태는 삭제·참조 복구 허용 집합과 별도로 판단하며, 독립 catalog hold는 복구 proof로 허용되지 않는 한 후보에서 제외한다. 실행 이력·branch·worktree·세션·checkpoint는 개정 금지 사유가 아니며, 게시 직전에 실행 근거와 실제 PR 부재를 다시 확인한다.

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

Planner의 `DRAFT_DECISION_RESULT`는 질문별 답변·새 사용자 원문 인용·근거 경로·남은 질문을 반환한다. 공통 strict JSON 파서가 중복 marker/key·비유한 값·잘못된 identity·필드를 거부하고 Manager가 인용을 현재 ExecPlan 원문 또는 source 입력 hash와 대조한다. Agent는 draft·입력 양식을 직접 해제하지 않는다.

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

이전 버전의 단일 현재 계획 개정 후보는 기존 소유 detached worktree와 execution checkpoint에서 복구한다. Manager가 구조·source·dependency·기존 ID·경로·실제 PR 부재와 게시 사실을 재검증하고 미완료 적용부터 이어간다. 실행 중 구현 worktree는 수정하지 않으며 PR 출현·후속 원격 변경·소유권 불확실에서는 작업물을 보존한다. 이전 문서 영향 Planner checkpoint는 이 복구·감사에 사용하며 새로운 작업별 Planner 호출을 만들지 않는다.

진행 중인 일반·유지보수 planning은 원격 base 전진 시 staged·unstaged·untracked 변경과 세션을 관리 worktree에 보존해 반영하고 최신 catalog·SQLite 권한에서 재검증한다. 시작 revision의 READY 원본과 Planner가 작성한 결과를 구분하며, 관련 근거·의존 문서의 실제 내용이 바뀐 경우에만 해당 계획을 다시 확인한다. 무관한 문서 변경과 내용 보존 이동은 재확인을 만들지 않는다.

`DOCUMENT_RESULT`는 기존 legacy checkpoint 복구에서 `application.result_normalization.tagged_json`으로 marker·공백·구분자·다중 줄 JSON을 정규화한다. 중복 marker·JSON key와 필드·evidence 의미 오류는 거부한다. 저장된 원문과 정규화된 판단을 후보 revision·문서 경로·Worker snapshot에 결합하고, 호출 입력이 바뀌면 이전 원문·판단을 superseded 근거로 보존해 새 판단에서 제외한다. 기존 checkpoint는 원문 필드가 없어도 원래 stage·후보·게시 사실을 확인해 복구하며 새 결과에 identity metadata를 일괄 요구하지 않는다.

일반 planning snapshot은 시작 시점에 실제 연결 PR이 없는 후보만 담는다. snapshot 선정 뒤 PR이 생기면 intent 기록·게시 직전 같은 CAS 경계에서 다시 확인해 게시를 미루고 미게시 intent만 supersede하며 Planner 결과·작업 공간·세션·실행 이력을 보존한다. PR이 있는 계획은 새 snapshot에서 제외하고 나머지 계획을 현재 catalog에서 판단한다. PR 조회 실패는 부재가 아니다. 단계별 legacy `proposed` 개정은 `prepare_snapshot()` 결과에서 해당 계획이 PR 때문에 제외되면 연결 PR을 다시 조회하고, 발견 시 checkpoint 후보를 보존해 기존 PR 경로로 넘긴다. `prepared` 개정은 push 직전에 PR을 확인하고 나타나면 후보를 보존한다. PR 조회 실패는 부재로 취급하지 않는다. 확인된 PR 출현은 `PLAN_INPUT_INVALID` 최종 차단 사유가 아니며 해당 계획에만 적용한다.

### 프로젝트 planning과 작업 문서 반영 기준

[R-082](requirements/%5BR-082%5D%20단계별%20원격%20문서%20반영과%20PR%20전%20ExecPlan%20갱신.md#프로젝트-planning과-현재-작업-재개-순서)의 명세 영향 판단은 프로젝트 일반 planning이 담당한다. 일반 Planner는 변경된 명세·관련 ExecPlan·dependency·현재 원격 구현과 필요한 보존 후보를 읽기 전용으로 확인한다. 관련 PR 전 계획 snapshot 검증·게시 intent·catalog 원자 적용을 재사용하며 구현 worktree를 Planner의 쓰기 공간으로 사용하지 않는다.

| 책임 | 저장·실행 경계 |
|---|---|
| 프로젝트 변경 감지·planning 선택 | 안전한 경계에서 `projects.planning_resume_plan_id`로 재개할 작업을 고정하고 프로젝트 실행 투영만 비운다. `exec_plans`의 단계·소유권·작업물·세션·실패 이력은 유지한다. 일반 planning이 실행 자리를 사용하며 게시·DB 적용 뒤 같은 작업을 원자적으로 다시 투영한다. 실행 중 Agent·verify와 소유 Git operation은 기존 경계를 유지한다. |
| 개정 게시·DB 적용 | 일반 `planning/publication_service.py`와 `storage/planning_input_state.py`의 성공 확정을 재사용한다. 게시 뒤 DB 미적용은 저장 intent로 먼저 복구하고, 새 원격 입력은 별도 검토한다. 관련 조건의 개정·변경 없음 결과가 확인된 최신 성공 기준에서만 현재 작업을 재개한다. |
| 작업의 실행 대응 | `execution/stage_documents.py`는 본문 구현 조건·sources·dependency 변경의 부족 여부를 확인한다. 구현 입력 token에서 원본 명세 지문만의 변화를 제외하되 verify·Review token은 그 지문을 독립적으로 유지한다. 정확한 이전 token과 snapshot이 확인된 legacy Worker 근거만 새 비교 형식으로 옮기며 과거 성공을 합성하지 않는다. |
| 마지막 반영 기준·진행 기록 | 변경 전 본문을 확보한 소유 commit은 원문·승인 tree와 repo·plan·branch·worktree·parent·message intent를 `document_record` checkpoint에 결합한다. 변경 전 본문을 확보하지 못하면 새 기록 성공을 만들지 않고 기존 기준을 유지한다. 복구는 실제 parent·message·tree가 intent와 일치하는 commit을 식별하고, 현재 후보의 조상 관계·원문·소유 identity·순수 기록을 확인한 뒤 definition과 적용 marker를 generation CAS transaction으로 저장한다. 중단 복구와 후속 checkpoint 교체는 미적용 근거와 확인된 commit을 보존한다. 증명에 실패하면 이전 기준과 미해결 사유를 남긴다. 미적용 기록은 후속 소유 교정 commit을 차단하지 않으며 새 intent나 checkpoint 교체도 그 근거를 덮지 않는다. 원격에서 같은 기록 bytes를 확인한 뒤에만 `planning_input_state.advance_verified_delta`로 해당 선언 delta를 소비한다. 같은 파일의 미확인 조건과 다른 입력은 남기며 전체 planning 성공·verify·Review·실패 이력을 갱신하지 않는다. |

위 코드 경로는 모두 `loop_manager_internal/` 아래다. 작업의 마지막 반영 ExecPlan 기준, 프로젝트 planning 성공 기준과 Git·verify·Review·게시의 입력 증명을 분리한다. `DefinitionSnapshot`·원격 SHA·execution checkpoint는 후자의 복구 근거까지 확인해 유지하며 일괄 제거하지 않는다.

전체 성공 기준은 검증된 `planning_input_baselines.reference_revision`과 프로젝트 지문을 사용하고 자체 delta 반영 revision을 새 전체 성공으로 사용하지 않는다. 이전 작업별 판단·개정 checkpoint도 미게시 후보·게시 사실·미완료 적용을 보존해 복구한다. planning 오류에서는 동일 실패 입력을 반복하지 않고 미확인 영향 밖의 작업만 기존 순서로 진행하며, PR·수동 소유 예외는 R-061·R-077을 유지한다.

## 실행 자리와 유지보수

프로젝트 조율은 준비·planning을 수행하고, 관리 대상으로 선택한 각 작업은 별도 SQLite 연결과 `ContextVar` 실행 선택으로 단계 처리를 진행한다. `exec_plans`가 작업별 단계·session·scope·실패 이력의 원본이며, 병렬 작업은 공유 `projects` 투영을 덮어쓰지 않는다. 프로젝트 조율이 종료할 때 남은 활성 작업을 대표 투영으로 연결하고, 미확정 소유 상태가 있으면 투영을 임의로 비우지 않는다.

운영 DB 연결은 공통 `db_access` 경계를 거쳐 파일별 스레드 순번과 프로세스 잠금을 사용한다. 조회 결과는 처리 구간 안에서 물질화해 cursor가 SQLite 읽기 잠금을 유지하지 않으며, 관련 조회·갱신은 같은 연결의 짧은 transaction으로 묶고 commit·rollback·close에서 접근 소유권을 반환한다. 별도 연결을 중첩 대기하는 호출은 거부하고 외부 관측은 DB 처리 밖에서 수행한 뒤 현재 상태·checkpoint·소유 예약을 재확인한다. 일시 DB 경합은 요청의 공통 대기 예산 안에서 처리하고 실제 실행·교정 실패 횟수와 구분한다. 읽기 전용 CLI·bootstrap과 호출 기록도 같은 진입점을 사용하며 독립 정적 snapshot은 별도로 유지한다. 대기 예산은 공통 계층에서 관리하며 SQLite 자체 대기로 중복 소비하지 않는다. 초기 등록·legacy catalog 이관은 독립 메모리 snapshot에서 기존 로직과 필요한 Git·PR 관측을 먼저 수행하고, 관측값과 원본 상태가 같은 DB 처리 안에서 기존 적용 로직을 실행한다. snapshot을 운영 DB에 덮어쓰지 않으며 관측 이후의 계획·소유·감사 변경은 거부한다.

공통 DB 연결의 중첩 context와 runtime transaction은 savepoint를 공유하며 안쪽 helper가 바깥 변경을 commit하지 않는다. schema script도 호출자의 transaction을 암묵 commit하지 않는다. `CoordinatedConnection.run_request(operation)`은 재실행 가능한 짧은 DB 전용 함수를 명시하는 경계다. 함수는 전달받은 연결에서 매 시도 현재 값·소유권·generation과 CAS 조건을 다시 읽고 판단하며 transaction 제어는 요청 경계에 맡긴다. 중첩 요청도 같은 transaction과 savepoint에 참여한다. BUSY/LOCKED이면 변경 전체를 rollback하고 차례를 반납한 뒤 같은 deadline으로 다시 참여한다. 안쪽 helper가 잠금 예외를 잡아도 해당 시도의 부분 변경을 확정하지 않는다. 임의 `with connection` 본문이나 SQL 한 문장을 대신 replay하지 않고, 이미 열린 caller transaction을 재시도 요청이 인수하지 않는다. Agent·Git·verify·전송은 함수 밖에서 수행하며 결과만 새 DB 요청에 전달한다. 요청 한도 소진은 `DatabaseWaitTimeout`으로 전달하고 손상·권한·SQL 오류와 취소는 원래 오류를 유지한다. 이 진입점을 명시적으로 채택한 요청에만 전체 재시도를 적용한다.

스레드는 파일별 condition 대기열을, 프로세스는 기존 `.access.lock` 안의 대기 표식과 OS byte lease를 사용한다. 먼저 등록된 살아 있는 프로세스 요청이 접근 잠금을 얻고, 반환·취소·프로세스 종료로 lease가 풀린 표식은 다음 요청이 회수한다. 대기 요청이 없으면 잠금 파일의 이전 표식을 정리하며 별도 상태 DB를 만들지 않는다. 재시도도 기존 대기 요청 뒤에 등록한다. 스레드·프로세스 순번 대기와 SQLite 재시도·commit·backup은 공통 deadline과 runtime 중단 확인을 사용한다. 명시적 재시도 요청의 시작과 실제 대기·재시도에서 중단을 확인하되, 일반 DB 경계의 즉시 가능한 종료 확인·예약 반환은 runtime handoff 뒤에도 허용한다. backup의 SQLite 내부 sleep은 끄고 BUSY/LOCKED가 확인된 명시적 sleep만 실행 clock에 기록한다. 즉시 획득·실제 복사·DB 처리 시간은 제외하지 않으며 살아 있거나 종료 불명인 프로세스의 시간·종료 확인도 유지한다.

공유 `TaskScheduler`가 관리 배정과 실제 호출 배정을 구분한다. 관리 선택은 프로젝트별 유효 상한 안에서 수동 제출·기존 작업·일반 신규 순서로 확정하며, 일반 신규는 기본 자리 없는 프로젝트부터 순환하고 이후 유효 priority를 사용한다. Worker·Reviewer 호출은 전체 `max_workers`, verify는 별도 `verify_concurrency`를 사용하고 프로젝트 조율·Planner mutex 대기는 이 호출 자리를 사용하지 않는다. 한 자동 실행의 Planner는 직렬이다.

배정기의 공통 condition 안에서는 대기열·메모리 실행 자리만 갱신한다. 호출 자리를 먼저 확보하고 공통 condition을 놓은 뒤 DB에서 호출 예약과 배정 cursor를 같은 transaction으로 확정하며, 실패하면 미시작 자리를 반환한다. 종료 시 DB 예약 반환이 확인된 뒤 메모리 자리를 비우고 확인 실패는 종료 불명으로 보존한다. 호출 대기 중 rank를 갱신하고 실제 호출 직전 작업 snapshot·프로젝트 차단·catalog/draft 보류를 재검사한다. 입력 변경은 실패 횟수를 늘리지 않고 단계 재준비로 돌리며, 지속되는 차단·보류는 해당 작업의 대기로 반환한다.

수동 recover의 연결 수명과 transaction 수명은 분리하며, 수동 완료·제출은 외부 profile 관측 뒤 요청·지침 입력을 짧은 transaction에서 다시 확인한다.

자동 실행의 `git:*` 자원 충돌은 기존 프로세스 lease를 매번 다시 획득하며 취소·Planner/runtime 중단을 확인한다. 대기는 DB transaction과 실제 Agent/verify 호출 자리를 점유하지 않으며 복구 실패로 기록하지 않는다. 수동 요청과 계획 소유권 충돌은 기존 거부 경계를 유지한다. 일반 차단은 선택된 ExecPlan 전체 row를 관측 기준으로 삼고 지문·Git·Reviewer 자료를 읽은 뒤 짧은 transaction에서 다시 비교한다. 그 사이 row가 바뀌면 오래된 상태를 차단에 적용하지 않고 재검사를 요구한다. BLOCKED Agent 변경 보존·복구 완료는 명시 plan ID의 실행 선택 context에서 Agent snapshot과 recovery 상태를 읽고 갱신한다. 중단 복구 재진입은 `projects`에 남은 복구의 `owner_plan_id`로 대상만 선택한 뒤 그 plan의 최신 recovery와 snapshot을 사용하므로, 낡은 대표 투영을 완료 상태의 원본으로 다시 소비하지 않는다. 소유자 필드가 없는 기존 복구 기록은 `projects` 호환 projection을 상태 원본으로 읽고 완료·재진입에서도 같은 projection을 기준으로 갱신하며, `plan_path`가 ExecPlan을 가리키면 기존 mirror 규칙으로 대상 행도 동기화한다. 대표 `projects` 투영이 다른 작업을 가리켜도 다른 branch의 snapshot과 비교하거나 그 상태를 덮지 않는다. direct rebase 시작·완료는 명시된 plan ID와 실행 선택 context가 함께 있으면 둘의 일치를 효과 전에 확인한다. 완료 처리는 명시된 작업을 선택해 해당 작업의 phase·checkpoint·candidate를 소비하며 다른 plan의 대표 `projects` 투영과 planning 기록은 보존한다. PR head는 선택된 ExecPlan의 실행 상태에서 읽고 프로젝트 등록 origin·base와 안전 gate는 프로젝트 원본을 따른다.

정상 provider 반환 뒤 session 저장의 DB 오류는 실제 결과·사용량을 예외 결과로 덮지 않고 수집 오류로 기록한다. session 결합 행이 없으면 같은 session의 완료된 invocation과 실제 종료 근거에서 유일한 지침 작업 귀속을 읽을 수 있다. 귀속 충돌과 필수 실행 전 증거 저장 실패는 계속 거부한다.

작업별 일반 RETRY는 프로젝트 조율로 반환하지 않고 실행 대기열의 재시도 순서로 재등록한다. RETRY마다 DB 상태를 다시 준비하고 기존 일시 복구의 같은 실행 3회 한도를 공통으로 적용한다. 단계 처리의 일반 예외는 작업별 상세 오류·traceback을 기록하고 미확정 transaction을 rollback한 뒤 같은 배정 경로로 제한 재시도한다. 예외 3회 실패는 해당 작업만 보류하며 Future의 일반 예외도 작업별 오류로 수집해 조율기의 추가 배정을 계속한다. Planner 배정 중단·runtime handoff·사용자 중단은 별도 제어 신호로 전파한다. 재시도 우선순위는 수동 제출보다 낮고 다른 일반 작업보다 높으며 재등록 순서를 유지한다. 프로젝트 조율기가 명세 변경으로 planning이 필요하다고 판단하면 프로젝트 배정 중단 신호로 관리 편입·실행 대기 호출·후속 단계를 반환시키고 실행 중 처리를 join한 뒤 프로젝트 조율로 돌아간다. 진행 중 rebase의 작업물·index·checkpoint·원래 target은 보존하고 별도 planning worktree에서 Planner 게시·catalog·DB 반영을 먼저 완료한 뒤 작업 배정을 다시 연다. 재개한 실행은 갱신된 베이스와 명세를 기존 revision gate로 확인하며, 보존된 rebase를 완료로 간주하거나 강제로 중단하지 않는다. 다른 진행 중 Git operation과 미종료 호출의 안전 gate는 유지한다. runtime handoff는 기존 전체 launch gate로 새 호출을 막고 진행 중 처리의 반환을 기다린다.

`projects.max_tasks_per_project`는 정수 0 이상이고 migration 기본 0이다. 0이면 해당 Manager가 읽은 config 기본값을 상속하며 양수는 보존한다. 배정 경계에서 DB를 재조회한다. `task_call_reservations`는 호출 token·repo·plan·종류·실행 receipt를 보존하고, 재시작 시 미시작 또는 실제 프로세스 종료 증거가 있는 호출만 회수한다. `PLAN_BLOCKED`는 호출 예약이나 `execution_checkpoints.operation`이 남아 있어도 관리 개수에서 제외한다. 실제 호출의 실행 자리·자원 소유권·checkpoint는 삭제하지 않으며 차단 해제 후에는 관리 한도를 다시 확인한다. 차단되지 않은 작업은 미확정 호출·operation의 기존 관리 자리 보존 조건을 유지한다. Agent는 기존 group/Job 종료 receipt, verify는 소유 프로세스·WSL lifecycle receipt를 사용한다. 불확실한 종류의 호출이 상한에 도달하면 해당 단계는 보존하고 다른 종류의 적격 호출을 계속한다.

rebase 관측의 안정성 지문에는 해당 branch·worktree의 등록과 공유 config만 포함하고 무관한 작업의 ref·worktree 변화는 제외한다. 해당 branch의 중복 등록·현재 후보·index·입력·원격 PR의 검증은 유지한다. rebase 완료·차단 해제의 Git·원격 재관측은 쓰기 transaction 전에 마치고, 짧은 DB transaction 안에서 대상 실행 snapshot·현재 투영·checkpoint generation을 다시 비교해 acceptance와 상태·outbox를 함께 적용한다. 대표 투영이 비어 있거나 다른 plan을 가리켜도 명시 대상의 원본과 일치하면 해당 plan만 갱신하고, 대상 자체의 identity·generation·원격 OID 불일치는 계속 거부한다. 게시된 rebase의 원격 lease는 현재 대상·branch·checkpoint 관측이 `WAITING` 또는 `COMPLETE`로 확인될 때만 유지하며, base가 조상에 포함되지 않는 `START` 판단은 완료 증거나 게시 소유권으로 승격하지 않는다. Worker 결과 scope 검사도 대상 checkpoint의 현재 rebase를 다시 관측하며, 관측 실패는 원래 영역·진단을 보존해 거부하고 최종 파일 판정에 전달하지 않는다. 중간 Git 작업이 확인된 `WAITING`만 최종 파일 검사를 미루며, 최종 결과는 `COMPLETE` 또는 현재 파일 정책에 따른 구체적 scope 거부로 판정한다. 반복 충돌 차단은 아직 rebase가 시작되지 않은 `START` 상태만으로 해제하지 않지만, 기존 plan-input correction의 재개 의미는 유지한다. 상위 execution 포트도 외부 재관측 전에 writer를 예약하지 않는다.

차단 재평가는 대상 plan·branch·worktree의 자원 lease와 `selected_execution`으로 기존 복구 경로를 실행한다. 복구 컨텍스트에서만 PLAN_BLOCKED의 투영을 읽을 수 있고 일반 단계 실행의 활성 상태 조건은 유지한다. 실행 스레드도 계획 lease를 보유해 종료 정리와 재평가가 겹치지 않으며, 대상 호출 예약·미종료 invocation·같은 소유 자원의 다른 활성 계획을 확인한다. 복구 뒤 다른 계획의 대표 투영을 덮지 않고, 원래 투영이 비어 있거나 대상인 경우에만 기존 단일 작업 호출의 재개 투영을 연결한다. 병렬 조율은 관리 여유에서 변경된 차단 입력을 별도 Future·DB 연결의 재평가로 보낸다. 재평가가 의미 판단을 기다리는 동안에도 다른 신규 배정은 계속하며, 해소 뒤의 짧은 재개 확정은 기존 관리 배정 순서를 사용한다. 코드 수정 뒤의 첫 실행도 원인별 검사를 수행한다.

사용자 질문은 입력 지문의 차이로 해제하지 않는다. 현재·이전의 명세와 작업 본문을 읽고 링크·frontmatter만의 변경을 제외한 뒤 읽기 전용 Reviewer로 원인을 판단한다. ARCHITECTURE와 ExecPlan·선언된 sources는 필수 입력이며, PRODUCT가 선언되지 않은 경우에만 선택 입력으로 취급한다. 선택 PRODUCT의 부재는 이전·현재 revision 각각의 정확한 tree 조회가 성공해 파일이 없음을 확인한 경우에만 표현하며, 조회 실패·필수 source 부재·모호한 Requirement는 평가를 중단하고 기존 차단을 보존한다. source의 내용·존재 변화는 판단 문맥과 identity에 포함된다. `USER_DECISION_ASSESSMENT` JSON의 `resolved`는 bool, `detail`은 비어 있지 않은 이유, `evidence`는 `source`·`quote` 배열이다. 해소 결과에는 변경된 현재 source의 정확한 인용과 ExecPlan 이외의 상위 명세 인용이 필요하며, 삭제·부재 사실만으로 해소하지 않고 질문이 남거나 결과가 불확실하면 차단을 유지한다. checkpoint의 `user_decision_assessment`와 이력에 입력 identity·원격 revision·기존 질문·판단·원문 근거를 보존하고 같은 입력 결과를 재사용한다. Requirement 이름 변경은 기존 R-ID resolver를 사용하며 저장된 draft 판단은 R-061의 출처·답변 검증에 맡긴다. Agent 실패·형식/인용 오류·source 또는 원인/CAS 변경은 해소 근거가 되지 않는다. 일반 입력 오류는 현재 metadata를 재검증하고 rebase의 안전한 시작·대기·완료는 기존 Git 소유 증거로 판단한다.

| 저장 상태 | 실행 자리와 보존 책임 |
|---|---|
| 작업별 실행 원본 | `exec_plans`에 단계·작업 identity·소유 자원·session·실패 이력을 유지한다. 프로젝트의 현재 작업을 바꾸어도 이 원본을 초기화하지 않는다. |
| 프로젝트의 현재 작업 투영 | 프로젝트 조율의 대표 작업을 연결한다. 작업 스레드는 자신의 plan 선택으로 원본을 읽고 갱신한다. 프로젝트 planning으로 전환할 때 재개할 작업을 고정하고 투영만 비우며, 검증·게시·DB 적용 후 같은 작업을 다시 투영한다. |
| 보존된 planning 상태 | worktree·session·미게시 결과·오류·실패 이력을 별도로 유지한다. 보존 기록이나 오류 자체가 실행 자리 점유를 뜻하지 않는다. |
| 미확정 publication intent·공유 Git operation | 원격 게시·DB 적용과 소유 operation의 남은 처리를 복구한다. 미확정 게시나 종료 불명 writer의 실제 실행 자리·소유 예약을 다른 작업을 위해 해제하지 않으며, 관리 개수는 작업 상태별 조건을 따른다. |

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

새 cycle의 진입 판단과 평가 범위 선택은 분리한다. 주기 도달·명시적 요청으로 새 cycle을 시작하면 `full`을 강제한다. 전체 점검 성공 이력의 부재나 정적 비교 결과만으로 최초 실행에서 유지보수를 시작하지 않는다. 평가 엔진은 `fast`·`targeted`·`full`을 지원하며 재개 시에는 저장된 범위와 필요한 관련 근거만 다시 확인한다. `fast`는 모듈 검토 묶음을 만들지 않지만, 필수 루트 조사 근거가 없으면 별도 루트 unit의 모델 검토를 수행한다.

정밀 점검은 관련 파일을 모듈 단위의 제한된 묶음으로 구성하고, 원본·mode·정적 사실·분석 버전·평가 조건에 결합된 묶음 identity의 성공 결과를 재사용한다. 전체 점검의 성공 Merge 기준과 최신 정적·부분 평가 기준은 따로 보존한다.

#### 의미 검토와 루트 조사

모듈 의미 검토는 읽기 전용 Planner 호출로 필요한 원본과 보존 조건을 확인한다. Manager는 묶음 범위·판단·발견과 실제 원본 인용을 검증해 잠정 결과로 보존한다. 모듈 발견과 루트 이동 후보가 모두 없고 일반 계획도 필요하지 않으면 추가 종합·계획 호출 없이 no-change 결과를 준비한다. 모듈 발견은 새 계획·기존 비종료 계획·근거 있는 제외에, 루트 이동 후보는 새 계획 또는 해당 목표를 포함한 비종료 계획에 연결한다. 저장된 검토를 사용하며 파일별 검토·관계 확인·종합 페이지를 다시 기본 단계로 실행하지 않는다.

같은 tiered assessment에는 별도 `.` 루트 조사 unit을 둔다. unit identity는 `root_review_source`의 원격 tree hash·mode·revision, 모든 root placement와 현재 runtime 지침에 결합하고, 기존 `parse_review`·`validate_root_reviews` 계약으로 제외 분류된 문서·자산·manifest·설정까지 실제 인용을 검증한다.

루트 protocol·scope audit는 모듈 receipt와 분리되며, 정적 inventory나 모듈 결과만으로 루트 완료를 만들지 않는다. 같은 guidance·revision의 검증된 파일별 root review 또는 이전 tiered 성공 결과만 재사용한다. 이전 tiered 성공의 root review는 성공 row에 보존된 guidance hash가 현재 지침 identity와 같고 source revision도 일치할 때만 재사용한다.

이동 후보의 링크는 후보 조사 hash와 계획 경로·본문 hash를 별도 저장하고 새 계획 또는 비종료 계획의 보존·검증 조건을 확인한다. 최종 Planner disposition에서는 후보 연결을 임시 보관하고, 전체 planning result 검증이 통과한 뒤 신규 계획 연결에만 ID allocation을 적용하며 allocation의 원본 경로가 임시 계획 경로와 일치하는지 확인한다. 기존 계획 연결은 allocation의 ID가 같아도 원래 ID·경로를 보존한다.

READY barrier dependency 편집이 반영된 최종 계획 ID·경로·본문 hash에 연결한다. collect·intent 저장은 작업 worktree의 최종 계획 본문을 검사하며 promote는 게시 revision의 blob과 catalog의 비종료 계획을 다시 대조한 뒤에만 root review를 성공 baseline에 포함한다. 필수 도구·문서 경로 보호와 기존 cycle·한 번 평가 cadence는 `validate_root_reviews`와 기존 publication 경계에 남긴다.

#### barrier 편집과 성공 확정

barrier mode의 유지보수 Planner는 선행과 새 리팩터링 DAG를 판단한다. Manager는 DAG 말단과 SQLite cycle rows로 필요한 선행 dependency closure를 계산한 뒤, closure 밖의 적격 READY snapshot에 누락된 말단 dependency만 기존 순서 뒤에 추가한다. 파일을 수정하기 전 cycle·snapshot 소유권을 다시 확인하고, 예상한 dependency-only diff와 catalog의 acyclic 관계를 검증한다.

실제 Manager diff는 planning result의 hash·publication path에 포함되어 원격 게시·catalog transaction·복구 검증을 통과한다. 게시 본문은 검증한 checkout과 CRLF/LF 줄바꿈 차이만 허용하며 다른 clean filter 내용 변환은 거부한다. 새 계획 등록은 현재 checkout hash와 Git 게시 blob의 감사 hash를 분리해 보존하고, Merge·archive 복구는 publication SHA의 blob hash로 증명한다.

다른 READY 파일 변경, 사용자 결정·draft 해제, snapshot 밖 파일과 실행 중·소유된 계획은 허용하지 않는다. 새 단계별 평가의 일반 계획은 별도 필요한 계획 주기에서 수행한다. 일반 계획 결합 여부와 종전 결과를 이미 보존한 cycle은 해당 mode·게시 검증을 유지해 복구한다.

planning batch의 원격 게시·catalog 반영과 현재 원격 입력 일치를 확인한 뒤 정적·모듈 평가 근거와 해당 성공 기준을 SQLite transaction에서 확정한다. 미게시·검증 실패·원격 source 변경은 성공 기준을 전진시키지 않으며 잠정 근거·publication intent를 보존해 복구한다. 새 리팩터링 계획은 검증된 DAG와 Manager barrier 편집을 포함해 게시한다.

평가 성공과 cycle 종료는 별도 상태다. cycle 범위는 선행·리팩터링·발견을 연결한 기존 계획과 그 dependency closure를 포함한다. 범위의 모든 계획이 실제 MERGED이고 원격·catalog에 반영됐으며 미확정 게시·해당 범위의 catalog hold가 없을 때 종료하고 다음 자동 주기의 Merge 기준을 갱신한다. 작업이 없으면 검증·필요한 게시 뒤 종료할 수 있다. 마지막 Merge 뒤 새 평가나 사후 재검증을 종료 조건으로 추가하지 않으며, 유지보수 종료를 프로젝트 전체 완료로 기록하지 않는다.

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
- Worker 사용자 메시지는 최초·교체 session에 선택 계획·worktree·결과 형식과 미완료 문맥을 보내고 같은 provider/Model session에는 확인되지 않은 새 조건·archive 후보·비검증 오류·checkpoint/선언 복원 사실·검증 피드백 delta만 보낸다. effort 변경은 같은 session의 재개다. 이전 Worker 결과와 verify/Review 원문은 복사하지 않으며 수동 교정도 전체 G·후보 R·사용자 생략과 미확인 외부 테스트만 유지한다.
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

DB 전용 준비·확정은 `CoordinatedConnection.run_request()`의 공통 예산·rollback/재조회를 사용한다. invocation 생성과 지침 결합, launch의 예약/receipt 결합은 각각 원자 요청이고, 격리·실행 identity·targeted 결합도 CLI 시작 전에 확정한다. targeted 경로·device/inode 검사는 SQL 재시도 밖에서 수행하고 SQL은 미시작·미완료 Worker 소유권을 다시 확인한다. provider·Git·파일 생성·임의 session callback은 이 요청에서 replay하지 않는다. invocation 준비는 launch lock을 잡지 않으며 실제 spawn의 handoff gate는 유지한다. 명시적 caller transaction은 중간 commit 없이 보존한다.

Manager 전용 process receipt는 기존 PID·호스트/boot·종료 근거와 별도로 `invocation_observation`을 원자 저장한다. 시작·종료 receipt 갱신은 이 관측을 보존하며 경로·platform·invocation identity·형식을 검증한다. 필드는 다음과 같고 관측하지 않은 값은 추가하지 않는다.

| 필드 | version 1 계약 |
|---|---|
| `version`, `identity` | 정수 `1`과 결합된 invocation ID. 다른 버전·identity·알 수 없는 필드는 거부한다. |
| `started_at`, `finished_at`, `duration_ms` | timezone을 포함한 ISO 8601 시각과 0 이상의 정수 실행 시간. 종료 관측은 시작과 실행 시간을 함께 요구하며 첫 관측 시각을 DB 재시도 시각으로 바꾸지 않는다. |
| `session_id` | 관측한 비어 있지 않은 session 문자열. 귀속 충돌은 계속 거부한다. |
| `outcome` | 필수 key는 `kind`, `code`, `duration_ms`, `session_id`, `usage`, `collection_status`, `observed_at`이다. 각각 종류·정수/null 코드·0 이상 정수 처리 시간·session 문자열/null·사용량·문자열 수집 상태·timezone 포함 시각이다. 종류는 `success`, `transient_failure`, `usage_limit`, `permanent_configuration`, `timeout`, `exception`, `interrupted`, `database_wait`이고 성공 코드는 0이다. |
| `database_pending` | 저장/전달 확정 대기의 boolean. 관리 호출은 예약 정리와 결과 전달 경계를 지난 뒤 해제한다. |
| `request_sha256`, `message_sha256` | 소문자 16진수 64자리 지문. 요청은 prompt·역할/provider/profile·작업 공간·지침·definition/목적/target 등 현재 호출 입력을 결합한다. 메시지는 실제 역할 응답의 지문이며 원문을 새 감사 자료에 복사하지 않는다. |

`usage`는 `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`의 0 이상 정수/null과 `status`의 `unknown`·`known`·`partial`·`invalid`를 요구한다. 관측 object의 version/identity 외 필드는 부분 관측 수집을 위해 선택 사항이며, 역할 응답 전달에는 outcome과 요청/메시지 지문을 모두 요구한다.

`apply_observation()`은 같은 invocation의 ledger만 확정한다. 재시작 정리는 먼저 이 관측을 적용하고 실제 결과가 없을 때 기존 interrupted 처리를 사용한다. 손상·identity 충돌·DB 적용 대기는 해당 행과 예약을 보존한다. 선택적 callback 관측의 일반 SQL 오류에서는 저장 가능한 원래 outcome을 수집 오류로 남기되 누락된 안전 증거를 채우지 않는다. 일시 DB 대기는 이 fallback 대신 같은 관측의 원자 요청을 재시도한다. 미시작 DB 대기의 `database_wait`는 provider 실패 분류와 구별한다. launch callback이 반환되기 전에 거부된 경우에만 `not_started` 종료 근거를 남기며, 기존 receipt나 시작 기록과 모순되는 미시작 근거로 종료·예약 회수·targeted 수집을 확정하지 않는다. 관측 필드가 없는 구형 receipt는 기존 종료 근거로만 취급한다.

관리 실행의 준비 대기 만료는 현재 작업 입력을 재확인한 RETRY 배정으로 이어간다. 결과를 확보한 호출은 원래 스택의 응답/예외를 유지하며 DB 확정만 다시 요청하고, 예약 정리가 대기해도 반환된 응답을 보존한다. `TaskScheduler`는 확인된 실제 종료와 저장 대기를 구분해 실행 개수만 먼저 반환할 수 있다. 이 구분은 재시작 때 같은 프로세스 종료 근거로 복원하고 저장 대기 예약·관리 작업은 유지한다. 종료 불명은 실행 개수도 유지한다. 대기 중에는 local pause/handoff만 확인하고 runtime 원격 관측은 DB 준비 후 실제 단계 진입에서 수행한다.

재시작의 응답 전달은 대기 표식이 있는 기존 호출에 한정한다. 요청 지문, ledger의 원래 결과·session·사용량과 session 귀속을 확인하고, 기존 attempt의 `last-message.txt` 원문 또는 그 양끝 공백을 제거한 값이 메시지 지문과 일치할 때만 같은 invocation의 `AgentResult`를 전달한다. 원문 부재·교체·지문 불일치·입력 변경·예외 원문 부족은 `CallDeferred`로 후속 처리를 보류하며 provider를 재호출하지 않는다. 이는 호출 성공 재사용·검증/게시 승인과 별개이며 기존 안전 gate와 targeted pending을 유지한다.

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

  기존 ExecPlan은 파일이 삭제돼도 시작 index와 Git 원본으로 확인하며 결정·실행 기록을 파일째 제거하는 변경을 거부한다. 원본과 수정본을 대조해 draft 해제와 User Decision 변경·삭제를 거부한다. Worker 종료 후의 정상 경계에서는 저장된 시작 ExecPlan과 고정 공식 revision의 frontmatter가 같은지 확인한 뒤 선택된 frontmatter만 원자 교체한다. 본문 bytes·mode를 유지하고 파일이 다시 바뀌면 교체하지 않는다.

  변경 전후 선언·본문 hash는 기존 execution checkpoint에 보존하며 후속 checkpoint에서도 유지한다. index·HEAD·branch와 다른 문서는 복원하지 않고 Worker에 현재 diff·문서 보고 갱신을 전달한다. 진행 중 rebase나 경계·원본이 불명확하면 복원하지 않는다.

  `validate_active_execplan_metadata`는 이 처리를 포함한 Worker 결과를 검증·Review 전에 reconciled catalog의 title·sources·depends_on·complexity·risk·priority와 정확히 비교하고 불일치 필드를 보고한다. Requirement resolver의 경로 호환을 metadata 동등성으로 사용하지 않으며 잘못된 선언은 Worker 진입 전에 Planner·Manager 경계에서 교정한다

- `documents/pr_description.py`는 기존 실행 지침 보고 경계와 publication 호출을 재사용해 최종 base/head의 name-status와 문서별 patch를 검증한다. `docs/` 아래 파일과 저장소의 Markdown·RST·TXT 문서를 대상으로 추가·삭제·rename·copy를 식별하며 rename·copy는 양쪽 경로를 포함한다. 검증 전과 새 PR·기존 PR 게시 경계에서 현재 선택된 ExecPlan 경로를 전달하고, 최종 경로가 일치하는 파일만 문서 보고·`DOCUMENT_CHANGE` 제출 대상에서 제외한다. 이전 Worker 결과에 남은 해당 경로의 보고도 무시한다. 다른 문서의 보고 검사와 선택된 ExecPlan의 diff 검토·범위 검증, PR의 고정 링크·Purpose는 유지한다.

  Worker는 `PR_SUMMARY`의 각 줄 맨 앞에 `DOCUMENT_CHANGE:`를 두고 뒤에 JSON 객체 하나를 기록한다. 변경 문서별로 한 건이며 아래 필드는 모두 비어 있지 않은 문자열이어야 한다.

  | 필수 필드 | 계약 |
  |---|---|
  | `path` | 최종 diff의 저장소 상대 경로. rename·copy는 새 경로, 삭제는 원래 경로를 사용하며 같은 경로를 중복 제출하지 않는다. |
  | `summary` | 해당 문서의 주요 변경 요약 |
  | `reason` | 수정 이유 |
  | `impact` | 제품·설계에 미치는 영향 |
  | `patch_sha256` | 해당 문서의 최종 patch stdout을 UTF-8로 인코딩한 SHA-256의 소문자 64자리 16진수. 최종 base/head에서 `git diff --no-ext-diff --no-textconv --find-renames <base> <head> -- <해당 경로들>`로 계산하며 rename·copy는 양쪽 경로를 전달한다. |

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
| Claude | 공통 홈·일부 호출 옵션은 구현돼 있으나 역할·OS와 관계없이 현재 격리 사전 검사에서 거부한다. 사용자 설정 source 전체 제외·내장 쓰기 제한·자동 승인 검토의 남은 목표는 아래 R-023 절을 따른다. |

provider adapter는 실제 호출 직전에 canonical worktree·Git 등록·repository identity를 확인하고 신규 session·재개·retry·fallback마다 역할의 쓰기 정책을 확인한다. 허용 쓰기는 선택 workspace와 소유권을 확인한 최소 임시 경로에 한정한다. 설정 병합으로 source checkout·다른 worktree·shared Git metadata·넓은 임시 경로가 추가되면 격리 확인으로 수락하지 않는다. 역할별 허용 파일·문서·결과 검증은 sandbox와 별도로 유지한다.

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
| 종료 | 프로세스 그룹 또는 Windows Job의 정리·wait 성공 뒤에만 종료 콜백을 호출한다. 시작·writer 종료·결과 성공·검증 성공은 별도 사실로 기록한다. |

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

설정 지문·직전 재검사는 실제 사용하는 공통 홈·프로젝트·관리 설정을 대상으로 한다. 읽기 실패·소유권 불명·검사 뒤 변경·권한·확장 충돌과 기본 사용자 저장소 사이의 재사용을 확인하지 못한 인증 방식은 거부한다. Claude의 설정 디렉터리 밖 개인 source 전체 제외는 아직 남은 목표이며 현재 호출 거부를 유지한다.

Model·Effort·Fast·instruction·sandbox·승인 정책은 명시적 호출 입력으로 제공한다. Agent 도구의 쓰기 허용은 역할별 선택 worktree·해당 호출의 최소 소유 임시 경로에 한정하고 공통 홈·개인 홈·다른 호출의 임시 경로를 추가하지 않는다. Reviewer의 읽기 전용과 기존 ACL·Job Object·sandbox·자동 승인 검토를 유지하며 CLI 자체의 상태 저장과 도구 쓰기 권한을 구분한다.

전용 환경값은 [Codex의 CODEX_HOME](https://learn.chatgpt.com/docs/config-file/environment-variables)·[Claude의 CLAUDE_CONFIG_DIR](https://code.claude.com/docs/en/env-vars)에 대응한다. 실행 환경 준비·조회·실제 호출은 위의 동일 환경 계약을 따른다.

#### R-085: 공급자별 공통 홈과 검증 경계

[R-085](<requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md>)의 기존 홈 호환 규칙을 유지한다. 이전 역할별 홈의 session·설정·인증·작업물·로그와 DB 이력은 삭제·이동·전체 복제·초기화하지 않으며 잔여 config를 새 입력으로 검사·복원하지 않는다.

session은 Manager에 저장된 ID와 repository·worktree·role·작업 결합으로 선택하며 공통 홈의 최근 session으로 대체하지 않는다. 재개 불가는 기존 실패·작업 문맥을 보존하는 새 session fallback으로 연결하고 재개 성공과 구분한다.

호출별 임시 경로·출력·실행값·권한 분리는 기존 fixture·모의 호출의 확인 범위다. 실제 session 재개·동시 모델 실행·Claude CLI 호환성·실계정 인증과 갱신·credential store·다른 OS 실환경의 완료 근거로 확대하지 않는다. 이 미수행 범위는 R-085의 Non-Goals에 남긴 후속 확인이며 공통 홈 전환의 선행·완료 조건으로 추가하거나 기존 미지원 호출을 허용하는 근거로 사용하지 않는다.

### R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)

현재 관리 Claude 호출은 역할·OS와 관계없이 격리 사전 검사에서 거부한다. 아래는 [R-023의 남은 provider 지원 목표](requirements/%5BR-023%5D%20Self-Hosted%20Runtime%20Refresh%20Handoff.md#requirement)에 필요한 설계이며 G-191의 구현 완료나 실제 실행 성공을 선언하지 않는다. Codex는 [현재 provider 계약](#provider의-현재-지원과-쓰기-정책)을 따르고 Manager의 Git 쓰기 책임은 양쪽에 유지한다.

Claude의 채택 범위는 공식 문서에 대응하는 일반적인 호출 구성과 모의 검증이다. provider 내장 파일 권한·명령 sandbox·자동 승인 검토를 조합하되 Codex와 동등한 실환경 검증이나 모든 조합의 완전 지원을 요구하지 않는다. 시작 시 적용하는 설정의 우선순위·병합은 확인하며 추가 허용 경로만으로 범위 밖 쓰기 차단을 인정하지 않는다.

| 미구현 정책 경계 | 구현·확인할 계약 |
|---|---|
| 파일 도구 | 실행별 `--settings`의 `Edit(경로)` 허용·거부를 Edit·Write·NotebookEdit에 적용한다. `Write(경로)`·`NotebookEdit(경로)`를 독립 보호 규칙으로 오인하지 않는다. 파일 권한의 `//절대경로`와 sandbox의 `/절대경로`를 canonical 경로·기준 디렉터리와 대조한다. Reviewer는 선택 workspace 쓰기도 거부한다. |
| 자동 승인 검토 | 쓰기 역할의 `--permission-mode auto`와 해당 모델·계정·CLI의 지원 조건을 확인한다. `acceptEdits`로 대체하지 않는다. Reviewer도 읽기 전용·편집 금지를 유지하는 지원 경로를 확인하며 미지원이면 권한을 완화하지 않는다. 모든 동작이 검토된다고 보장하지 않으며 격리 밖 재실행·쓰기 권한 확대는 검토의 승인으로 허용하지 않는다. |
| Bash·하위 프로세스 | OS 내장 sandbox에 `enabled=true`·`failIfUnavailable=true`·`allowUnsandboxedCommands=false`·빈 `excludedCommands`를 적용하고 병합 후에도 격리 밖 예외가 없는지 확인한다. `autoAllowBashIfSandboxed=false`로 permission mode의 검토 흐름을 유지한다. 파일 도구 규칙만으로 명령 쓰기를 제한했다고 판단하지 않는다. |
| 유효 설정 | `--add-dir`·`permissions.additionalDirectories`·`sandbox.filesystem.allowWrite`는 추가 허용이다. 기본 cwd·임시 경로·파일 권한에서 파생한 허용, 전체 설정 source·우선순위·배열 병합을 확인한다. `--settings`가 전체 교체나 managed 정책 우선이라고 가정하지 않고 빈 배열로 상속 배열이 제거됐다고 판단하지 않는다. |
| Git·임시 경로 | source checkout·다른 worktree·`.git` pointer와 resolved shared/worktree Git directory를 파일 도구의 `Edit` 거부와 `sandbox.filesystem.denyWrite`로 함께 보호한다. linked worktree의 기본 shared Git 허용도 확인한다. 임시 쓰기는 호출별 최소 소유 경로로 제한하며 상속·기본 임시 경로로 넓히지 않는다. |
| 환경·지원 한계 | 내장 sandbox가 없는 Claude native Windows 등 확인된 미지원과 시작 정책 충돌·초기화 실패는 해당 호출에서 처리한다. 실행 중 조직 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장은 현재 범위 밖이다. 이를 위한 hook·polling·WSL·컨테이너·외부 격리를 추가하지 않는다. |
| 호출 증거 | provider·CLI 버전·환경·역할·canonical worktree·repository identity·요청 정책·적용 source·병합 후 유효 정책 지문·session/호출 identity·확인/거부 결과를 기존 invocation·snapshot과 호환 가능한 추가 저장으로 결합한다. 저장·확인 실패는 성공으로 인정하지 않는다. 재개·fallback·provider/설정 변경과 중단 뒤에는 새 호출에 다시 결합한다. |
| 증거 보호·실패 | 문서 조사·모의 검증·실제 호출 관측을 구분하고 비밀값·인증 자료·source 로컬 파일 내용은 저장하지 않는다. 실제 호출에서 관측한 오류의 근거를 보존하며 실시간 managed 변경 감시를 증거 수집 조건으로 요구하지 않는다. 기존 작업물·session·재시도·과거 감사는 보존하며 Git snapshot 접근 검사를 되살리지 않는다. |

설정·CLI 조사는 [Claude CLI 옵션](https://code.claude.com/docs/en/cli-reference), [파일 권한](https://code.claude.com/docs/en/permissions#read-and-edit), [sandbox](https://code.claude.com/docs/en/sandboxing#configure-sandboxing), [설정 우선순위](https://code.claude.com/docs/en/settings#settings-precedence), [permission mode](https://code.claude.com/docs/en/permission-modes)의 기능·버전을 실제 호출 환경과 대조한다. 지원하지 않는 key를 무시한 실행을 격리 확인으로 기록하지 않는다.

개발·완료 기준과 회귀 범위는 [R-023 Acceptance Criteria](requirements/%5BR-023%5D%20Self-Hosted%20Runtime%20Refresh%20Handoff.md#acceptance-criteria)를 따른다. 공식 문서 조사와 신규·재개·fallback 모의 테스트로 지원 구현을 완료할 수 있다. 실제 CLI·계정·유료 모델 호출·금지 쓰기 시연과 실환경 완전성은 필수 조건이 아니다. 문서/mock 확인·실호출 확인·미수행을 구분하고 실제 인증·지원·권한·초기화 오류는 해당 호출에서 보존·처리한다. 미검증만으로 개발 완료·독립 Codex 실행을 막거나 선택 provider를 바꾸지 않는다.

유효 정책과 실제 허용·거부를 확인하되 Codex 프로세스 전체의 mount·내부 쓰기나 중첩 sandbox 성공을 추가 증명 조건으로 요구하지 않는다. exec JSONL의 전용 필드 부재만으로 transport나 바깥 wrapper를 추가하지 않는다. 정상 인증·session·로그와 허용된 임시 ref는 Agent의 범위 밖 쓰기 권한과 구분한다. Reviewer의 읽기·명령·테스트 경계는 [테스트와 Reviewer의 책임](#테스트와-reviewer의-책임)을 따른다.

실제 CLI 정책 조합·인증·session·OS/버전별 동작과 managed 변경 대응의 후속 확인은 [FUTURE_IDEAS](FUTURE_IDEAS.md#claude-실환경-후속-점검)에 둔다. 이 목록을 현재 지원 구현의 선행·완료 조건이나 조직 관리 환경·외부 infrastructure의 필수 선택으로 사용하지 않는다.

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


복구 관측은 안정적인 원인 code와 repository·plan ID·phase·plan/base revision·진단 fingerprint·진전 marker를 기록한다. 복구 disposition은 `transient_retry`·`state_reconcile`·`agent_correction`·`user_action_required`이며 차단 scope와 별도로 판단한다. 원인별 같은 실행의 시도 한도에 도달하면 작업을 보존해 일시 중단하고, 최종 사용자 조치 판정에만 원인 code로 scope를 결합한다. 최종 차단 transaction은 판정의 repository·plan·원인 일치를 검증하며 복구 횟수·마지막 안정 revision·남은 불확실성·실제 조치 대상을 보존한다. 일시 중단은 차단용 게시·알림을 만들지 않고, 최종 차단의 게시·알림은 [차단 작업의 원격 반영과 보존](#차단-작업의-원격-반영과-보존)을 따른다.

일반적인 PLAN_BLOCKED는 계획·원래 활성 sources의 내용 해시를 기준으로 재평가한다. 닫힌 PR은 같은 PR reopen을 요구한다. 과거 파일 승인 대기는 일반 작업·소유권 검사를 통과하면 기록을 보존한 채 재개한다. 독립 차단·planning 오류·실패 감사는 함께 지우지 않는다. planning_error는 실제 교정·재검증·게시 복구로 원인이 해소된 뒤에만 지운다. cleanup_pending과 ExecPlan archive pending은 상태 전이를 만들지 않는다.

**수동 준비·게시 복구.**

수동 명령의 준비·수락은 `application/manual_work.py`와 `manual_concurrency.py`가 소유하며 [PRODUCT의 명령별 역할·결과](PRODUCT.md#수동-명령의-준비와-수락)를 따른다. `start`의 진입 상태는 `READY`·`PLAN_BLOCKED`·기존 `MANUAL_WORKING`이고, 차단 인계는 `verify_retry_exhausted`·`review_retry_exhausted`·`review_protocol_exhausted`·`worker_output_invalid`로 한정한다. draft·catalog hold·프로젝트 차단·활성 유지보수와 계획·자원 소유권을 별도로 검사하며 기존 수동 준비 재호출은 저장된 작업을 재사용한다.

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

일치하는 결과만 현재 수용 HEAD의 `worker_candidate_token`에 checkpoint CAS와 runtime transaction으로 연결해 VERIFYING으로 이어간다. `decide_revision_gate()`가 현재 후보 확인 불일치 등으로 IMPLEMENTING을 요구하면 clean 완료도 그 판정을 유지한다. 구현 교정 중인 작업, 실제 조건 변경, 충돌·미완료·중단 뒤 수용에는 이 예외를 적용하지 않는다.

같은 게시 lease·중간 checkpoint 경계를 유지하며 새 후보에 대한 verify·Review·publication 근거는 각각의 입력 증명·재사용 규칙으로 다시 판단한다. Worker 결과의 연결만으로 다른 단계의 성공을 만들지 않는다.

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

Worker 종료 후에는 해당 호출이 만든 변경의 역할별 허용 범위를 검사한다. commit 재생·일부 stage·autostash 복원 중간 형상은 전체 최종 후보로 검사하지 않는다. Git 완료 후 고정 target의 선택 ExecPlan frontmatter를 본문·mode·다른 작업물 보존 하에 복원하고 갱신한 checkpoint·서비스·HEAD에서 최종 선언·전체 diff·보호 문서·archive를 검사한 뒤 채택한다. 최종 금지 변경·잘못된 선언은 거부한다. 충돌 해결 Agent의 sandbox는 계속 적용하며 Git 전후 차이를 접근 권한 위반으로 추정하지 않는다. 관측 중 단계가 바뀌면 재관측하고 최종 archive 계약을 새로 구성한다. `REBASE_HEAD`·`MERGE_MSG`·`MERGE_MODE`만 남은 상태는 controlling operation이나 autostash 복원 대기로 취급하지 않는다.

`execution_checkpoints.snapshot.rebase_operation_history`는 이전 operation의 상태를 순서대로 보존하되 새 항목에서 같은 history 필드를 제외한다. 이전 history는 바깥 배열에 한 번만 유지하므로 새 rebase를 시작할 때 기존 이력 전체가 다시 중첩되지 않는다. 현재 실행·acceptance·원격 lease의 필드와 과거 operation 상태의 감사는 유지한다.

시작 전 Worker branch·target의 merge-base 이후 문서·코드 형상은 소유 operation의 재생·보존 입력으로 기록한다. 이 기록은 역할별 전체 diff의 허용 판정이나 Worker 변경 승인이 아니다. Requirement·ExecPlan의 자동 채택은 고정 target 원문·경로·identity에 결합하고 최종 문서·archive 및 Worker 결과의 판정은 각 완료 시점에 수행한다.

자동 Worker 해결과 사용자의 수동 파일 해결·stage·continue·완료를 같은 현재 상태 판정으로 수용한다. 소유 rebase의 Requirement·ExecPlan 충돌은 `rebase_documents`가 관측한 원격 base의 immutable revision에서 catalog·ID·source·dependency와 실제 PR 경계를 확인한 뒤 해당 문서의 bytes·mode·경로·삭제를 채택한다. PRODUCT·ARCHITECTURE·지침과 코드·테스트·설정은 자동 선택하지 않는다.

rename·archive의 목적지는 같은 원본 ID로 확인하며 현재 작업의 삭제·archive·미반영 rename으로 유효성을 확정할 수 없으면 원인과 범위를 checkpoint에 보존한다.

교체 전의 작업 HEAD·replay commit·문서 원문·stage별 blob·작업 파일과 문서 외 index·dirty·operation 근거는 기존 execution checkpoint에 보존한다. 중간 HEAD의 문서 blob도 보존하며 소유 operation의 같은 HEAD에서만 committed 형상으로 인정하고, staged 형상은 pinned base blob으로 따로 검증한다. 파일별 교체와 pinned blob의 index 적용 전후를 대조하고 소유권·원격 lease·PR을 재확인한다.

적용·stage 중단은 같은 원문으로 재개하고 뒤늦은 replay 충돌도 같은 문서 revision을 사용한다. 후속 catalog·source·rename 판정 오류의 uncertain 기록도 이전 채택 이력과 원문·blob을 유지한다. 모든 재생 위치의 채택 경로를 checkpoint 이력에서 모아 완료 직전 HEAD·index·작업 파일을 원문과 대조한다. 후속 비충돌 재생이 원문을 바꿨으면 완료 채택을 거부하고 checkpoint와 작업물을 기존 복구 경계에 보존한다.

충돌 해결 Worker의 종료 검사도 소유된 중간 상태에서는 최종 변경 범위·선언 판정을 유예하고 구조·operation·원격 소유권을 확인한다. 응답·실제 호출 전에 고정한 입력 snapshot·session·종료 코드·결과 종류는 후속 검사 전에 같은 교정 turn에 함께 보존한다. 검사 오류 뒤 재개에도 당시 입력을 최신 definition으로 대체하지 않는다. Git rebase가 끝나면 Manager가 고정 target base의 선택된 ExecPlan frontmatter를 복원하며 구현·검증 본문과 mode는 보존한다. 복원 전후 bytes·다른 파일의 상태·commit tree를 execution checkpoint에 기록해 쓰기·stage·commit 중단을 같은 결과로 재개한다. 선택 ExecPlan의 본문은 기존 Worker turn receipt가 없는 저장 rebase에서도 사용자 결정·draft 보호와 현재 소유권·다른 문서의 채택 원문을 검증한 뒤 선언 복원 전후 bytes로 보존할 수 있다. 복원된 정확한 출력만 후속 문서 범위 검사에서 수용하며, 복원 기록을 Worker 완료·targeted test·verify·Review 성공 근거로 사용하지 않는다. 복원 후 전체 최종 범위·선언 검사와 기존 verify·Review를 수행한다. 선언 불일치는 Worker 반복 교정으로 보내지 않으며 plan_input_invalid의 소유 rebase도 일반 branch 검사 전에 같은 복구 경계로 재평가한다.

rebase target은 바꾸지 않으며 원격 재전진은 기존 operation 완료 후 반영한다. 문서 전용 충돌은 Manager가 continue/skip하고 혼합 충돌의 나머지 파일과 autostash 충돌은 기존 Worker 교정 경계에 남긴다. 문서 Progress는 현재 후보의 실행·검증 성공 증거가 아니며 완료 후의 입력 재평가·verify·Review·게시 gate는 유지한다

완료 결과는 HEAD·branch ref·worktree 등록·target ancestry, clean index·미해결 operation 부재, 현재 역할·선택 ExecPlan 및 archive 계약에 맞는 최종 변경과 정확한 원격 lease를 확인한 뒤 현재 상태·입력 지문과 SQLite generation에 결합해 한 번만 반영한다. 원본 commit 내용·개수·순서의 변화 자체를 거부하지 않으며 Git 완료 뒤 저장 전 중단도 같은 검사로 복구한다

rebase 채택의 한 검사 구간에서는 `ManagedRebaseService.adopt`가 안정된 이중 관측을 읽기 전용 소유권 소비자에 전달한다. 소비자는 전체 `RebaseExpectation`과 checkpoint generation이 일치할 때만 이를 사용하고 게시 owner를 별도로 검증한다. SQLite 확정에서는 새 이중 관측으로 다시 확인하며 Git·Agent·verify·게시 및 재시작 경계를 넘어 전달 결과를 저장하지 않는다.

최초 검사·소유권 소비·확정 재검증은 각각 `rebase:adoption_inspect`·`rebase:adoption_owner`·`rebase:adoption_confirm`으로 계측한다.

#### 중간 checkpoint 게시와 원격 입력

동일 계획의 기존 원격 작업 branch를 관리 rebase한 경우, 현재 repository·계획·worktree·branch·HEAD·등록·target base·index·operation·입력과 연결 PR을 확인한 뒤 완료 HEAD의 중간 checkpoint 게시 또는 현재 lease에 따른 보류를 확정한다. 기존 PR도 같은 URL·head/base branch를 유지하며 이 경로를 사용할 수 있다. 원격 입력이 바뀌면 이전 intent를 성공 없이 감사로 보존하고 통합·재검증으로 돌아간다.

신규 원격 branch·다른 계획의 branch는 기존 게시 intent로 갱신하지 않는다.

중간 게시 intent는 정확한 기대 원격 OID와 후보 HEAD, 현재 상태 수용 identity·지문을 SQLite에 보존한다. 게시 직전 실제 ref와 연결 PR의 identity·head를 다시 확인하고 정확한 OID를 명시한 `--force-with-lease`로 같은 ref만 갱신한다. 새로운 원격 입력은 재관측·통합·재검증 대상으로 처리하고, 삭제·다른 대상·실제 권한 또는 보호 정책 거부는 해당 작업이나 쓰기를 보류한다. 원격 변화를 이전 intent의 새 lease로 끼워 넣지 않는다.

push 전후 및 SQLite 반영 전 중단에서는 같은 intent와 실제 원격 OID·현재 소유 상태를 재대조한다. 원격이 이미 후보 HEAD면 push·commit을 반복하지 않고 완료 기록과 마지막 채택 원격 OID를 한 transaction으로 갱신한다. 일시적 실패는 같은 intent로 재개하며 미게시 rebase가 여러 개 쌓인 경우도 과거 journal·receipt·commit별 replay 연쇄 없이 동일한 현재 상태 판정을 사용한다.

중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인이 아니며 최종 후보의 Worker 확인·targeted test·전체 verify·Review·PR gate를 유지한다

`work_branch_integration.synchronize`는 현재 등록 origin의 fetch/push URL, 작업·worktree·ref·연결 PR을 확인하고 정확한 원격 commit을 받아온다. 로컬·원격의 차이와 현재 Git operation을 분리하며, 필요한 통합은 기존 baseline tree와 보존 commit·현재 관리 rebase 경로를 사용한다. `accepted_work_branch`는 읽고 확인한 입력이며 게시 성공 receipt가 아니다.

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

계획별 입력 변경, 사용자 결정 또는 닫힌 PR reopen 조건은 해당 `PLAN_BLOCKED`만 재평가한다. 작업 branch의 저장 OID나 receipt가 현재 원격과 달라도 현재 계획·branch·worktree·등록 origin·PR과 Git operation, 작업물·ignored 경로의 보존 조건을 확인해 새 입력을 수용한다. 로컬·원격의 추가 작업과 확인 가능한 관련 재작성은 양쪽 작업을 보존·통합하고 최신 base를 반영한다.

단순 OID 변경으로 원인을 해제하거나 과거 성공을 재사용하지 않고 실제 변경과 원인별 조건을 재평가한다. 필요한 verify·Review를 수행하며 실제 구현 입력 변경·부족만 Worker 교정으로 처리한다. 같은 입력의 검사·알림을 반복하지 않고 별도 사용자 결정·PR reopen·실제 대상 또는 보존 문제는 유지한다.

차단 해제 후 적용하는 경로에 따라 초기화·보존 범위를 구분한다.

| 재개 경로 | 보존·초기화 범위 |
|---|---|
| 일반 READY 재배정 | 기존 branch·worktree·작업물을 보존한다. 현재 Worker·Reviewer session 연결과 단계 결과, retry·verify/Review 실패 지문·진행 중 승격 집계는 초기화하고 effective Worker level을 현재 선언의 최초 level로 되돌린다. |
| 기존 PR 충돌 복구의 재진입 | 같은 PR·branch·worktree와 기존 Worker session을 유지해 진행 중 복구를 이어간다. 재배정 경로의 retry·실패 지문·승격 집계는 초기화하고 검증·Review 근거는 새 후보에서 다시 판단한다. |
| 원격 입력 통합·과거 오탐의 원인만 해제하는 기존 실행 재개 | session·branch·worktree·실패·승격 감사를 보존한다. 실제 구현 조건 변경·부족과 후보에 필요한 검증·Review만 기존 단계에서 처리한다. |

현재 실행값의 초기화와 과거 감사 삭제를 혼동하지 않는다. 같은 branch의 최초 PR은 최종 verify와 Review 성공 뒤에만 생성한다.

`PLAN_BLOCKED`와 계획과 연결된 `PROJECT_BLOCKED`의 원격 입력은 다른 활성 계획·PR 복구·planning publication·유지보수가 실행 자리를 점유하면 보존한다. 자리가 비었거나 차단된 같은 계획이 점유한 때 현재 ref·PR·계획·branch·worktree·origin과 작업물을 확인해 원격 작업 입력을 통합하고 최신 base를 반영한다. 완료 결과의 게시 또는 lease 보류를 확정한 뒤 원래 원인만 재검사한다.

원인이 남으면 기존 checkpoint 경계에서 이유와 작업물을 보존하며 같은 원격 HEAD에는 빈 commit·반복 push를 만들지 않는다. 원인이 해소되면 기존 session·branch·worktree·실패·승격 감사를 보존하고 필요한 후보 검증·Review부터 재개한다. 실제 구현 교정은 Worker 경로로 처리한다. 저장 단계와 실제 Git 결과를 대조하고 별도 사용자 결정·PR reopen·실제 대상 문제 및 다른 계획의 상태를 유지한다.

#### 프로젝트 차단과 진단 게시

`PROJECT_BLOCKED` 해제는 원래 차단 원인을 현재 repository·계획·worktree·소유권·runtime 연결과 대조하고 원인별 안전 조건을 통과한 경우에만 수행한다. 기본 checkout·사용자 branch·공유 metadata의 정상적인 변화를 원상복원하거나 과거 snapshot과 일치시키는 조건을 두지 않는다. 실제 대상·소유권·sandbox 정책의 문제와 독립 차단은 유지하며 `refs/codex/turn-diffs/*`의 순변경은 해제 조건을 무효화하지 않는다.

프로젝트 차단이 해제돼도 별도 계획 원인이 남은 ExecPlan은 `PLAN_BLOCKED`를 유지하며 다른 차단 계획의 fingerprint·작업 공간을 바꾸지 않는다.

| 보존·게시 경로 | 허용 조건과 결과 |
|---|---|
| 작업 branch의 checkpoint | 현재 작업 대상·원격 입력·변경 범위·소유권과 정확한 lease를 검증한다. 충돌·미해결 Git operation 또는 검증 불가에서는 해당 branch의 commit·push를 보류하고 작업물·session과 실제 local-only 이유를 보존한다. |
| 별도 진단 branch·자료 | 원본 작업·자료 범위·게시 권한을 별도로 검증한다. 충돌 중에도 원래 HEAD·index·rebase를 바꾸지 않는 자료 수집·게시가 가능하다. 작업 branch가 local-only인 사실과 진단 자료의 게시 결과를 각각 기록한다. |

`diagnostic_preservation`은 검증된 파일·index stage·작업 파일을 `manifest.json`과 blob 자료로 보존하고 Manager 상태 디렉터리의 private index로 별도 orphan commit을 만든다. 범위 밖·비공개 경로와 비밀 자료는 제외하며 수집·intent 저장·게시·확정에서 원본과 소유권의 불변을 다시 확인한다. 이 조건을 증명할 수 없으면 자료와 미게시 이유를 보존한다.

진단 ref는 계획·원본 branch·base·내용 OID에 결합한다. 원격 부재 시에만 생성하고 같은 OID는 재사용하며 다른 OID로 갱신하지 않는다. 진단 ref 충돌은 해당 자료 게시만 보류한다. 원격 삭제·재작성·경쟁·권한·branch protection 오류를 예상 밖 ref의 force push로 우회하지 않는다.

원인·ref·OID·intent·결과는 SQLite에 보존하며 PR 생성·원래 branch의 수용·차단 해제·verify·Review 성공 근거로 사용하지 않는다. 현재 원형·보존·대상 또는 원래 차단을 확정할 수 없으면 이유와 작업물을 보존한다. 과거 rebase turn·stage 승인·receipt·replay·journal 부재 자체는 접근 권한이나 정상 입력·현재 상태 판정의 거부 사유가 아니다.

진단 ref 생성·checkpoint commit·push·SQLite 확정 사이의 중단은 저장 intent와 실제 OID를 대조해 복구한다. 같은 입력·결과의 rebase·게시·검사·알림을 반복하지 않으며 알림에는 확인된 ref·OID 또는 실제 local-only 이유와 보존 경로를 포함한다.

#### 진단 ref의 정리 준비 근거

`diagnostic_cleanup`은 프로젝트 실행 종료의 안전한 경계에서 저장된 ref만 관측하고 준비·보류 기록을 갱신한다. 원격 namespace 탐색·push·ref 삭제·원인 해제·알림은 수행하지 않는다. 준비 실패는 원래 실행 결과를 유지하며 runtime handoff에서는 준비를 수행하지 않는다. 실제 원격 삭제는 후속 경계에서 별도 구현한다.

원본은 기존 `diagnostic_preservations`·`blocked_diagnostic_publications`의 intent/결과와 `diagnostic_ref_uses`의 원인별 사용이다. 사용 identity는 repo·scope(`plan`/`project`/`planning`)·owner·ref·cause identity·input identity·등록 origin에 결합하고 원래 branch·tree/commit OID, 게시 대기·확정과 변경 generation을 보존한다. 같은 내용의 ref를 새 원인에서 재사용해도 이전 사용을 덮어쓰지 않는다. origin이 바뀌거나 과거 성공 receipt를 반복 조회해도 미확정 사용의 게시 성공을 추정하지 않는다. 실제 충돌은 원문 manifest에 보존된 unmerged index stage 1/2/3으로 확인하며 원인 code만으로 추정하지 않는다. target·원인 연결이 부족한 과거 intent는 보존한다.

`diagnostic_cause_resolutions`는 원인 해제 transaction에 정확한 원인 snapshot·scope/작업·origin·branch/worktree와 현재 `COMPLETE` acceptance의 해결 HEAD·target·generation·identity를 보존한다. `WAITING` 상태에서 Worker 재개를 위해 해제하면 acceptance 대신 원래 pending target을 남기며, 준비 시 같은 target의 현재 `COMPLETE`를 별도로 확인해야 한다. acceptance의 `repo`와 `remote.repository`는 등록 프로젝트의 로컬 저장소 경로 identity이며 등록 origin과 별도로 대조한다. origin은 해제 시 프로젝트 기록과 게시 사용 기록에 결합하고, 과거 checkpoint의 origin도 해당 게시 사용 기록에서 읽는다. 일반 차단 삭제·활성 row 부재는 해소 증거로 승격하지 않는다. project 원인의 기존 두 identity 형식은 같은 원인 snapshot에서 각각 연결한다. 과거 checkpoint도 정확한 현재 generation의 acceptance와 마지막 해제 감사가 원인을 함께 증명할 때만 소비하며 서로 다른 이력 항목을 추정해 연결하지 않는다.

정상 branch 게시가 해소 확정의 앞이나 뒤에 일어나도 `publication_remote_oid`와 해결 HEAD가 현재 정상 원격 branch에 포함되는지 확인한다. 해소 때 미커밋 작업이 남았거나 과거 기록으로 당시의 clean 상태를 증명할 수 없으면 현재 작업물이 clean이고 HEAD가 저장된 게시 HEAD이며 해소 HEAD 이후의 commit임을 추가로 확인한다. 미게시 작업물을 이전 원격 HEAD의 성공으로 대신하지 않는다. PR Merge·다른 원인의 해제는 이 증명을 대신하거나 지연시키지 않는다.

원문 증명은 기존 `diagnostic-artifacts/<input identity>` bundle에서만 얻는다. 일반 파일의 bytes와 version 1 manifest의 scope·owner·원래 branch·revisions·파일별 index stage/작업 파일·부재·모드를 읽고 blob SHA-256, diff/manifest 완전성과 저장된 orphan tree/commit OID를 대조한다. 모드는 `100644`·`100755`·`120000`이며 원래 symlink의 target bytes도 일반 blob으로 읽고 link를 따라가지 않는다. SHA-1/SHA-256의 고정 orphan 형식을 bytes로 재구성하므로 원격 ref·작업 공간·Git 객체 DB 없이도 복구 근거를 검사한다. 누락·손상·불완전 manifest·지원하지 않는 형식·bundle 내부/부모 symlink·identity 불일치는 해당 준비를 보류한다.

`diagnostic_cleanup_preparations`의 ref별 `basis`는 현재 프로젝트·작업 소유권·원인·사용 generation·진행 중 호출 예약·checkpoint·intent/보존 자료·해소 기록 snapshot의 SHA-256이다. payload에는 boolean `ready`, 보류 `reason`, 저장된 게시 OID인 `expected_oid`와 원격·정상 branch 포함 관계·자료 digest 관측을 남긴다. 판정은 부작용과 분리하며 외부 관측을 두 번 대조하고 짧은 쓰기 transaction에서 snapshot이 같을 때만 기록한다. 준비 기록은 삭제 승인·완료나 영구 자격이 아니다. 후속 삭제자는 현재 basis·모든 사용·origin·원격 OID·자료를 재확인해야 하며 이전 준비 기록만 소비할 수 없다. 기존 원문·session·실패·게시 intent·확정 감사는 유지한다.

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

| 저장 계약 | 식별·재사용 규칙 |
|---|---|
| 요청 범위 | `(repo, plan_id)`. 프로젝트 전체는 `plan_id = NULL`이며 계획별 범위와 별도 부분 unique 인덱스로 구분한다. |
| 요청 상태 | `pending`·`completed`. 같은 범위의 pending만 같은 request identity로 재사용하고 종결 뒤 후속 재검사는 새 요청으로 접수한다. |
| 실행 결과 | 실행 전에는 결과가 없을 수 있다. `in_progress`는 pending, `resolved`·`unresolved`는 completed로 저장하며 전후 snapshot·소비·과거 결과를 감사로 보존한다. |
| 제한 재검사 소비 | `local_environment_recheck_consumption`의 `(request_id, subject, cause)`별로 기록한다. 같은 pending 요청의 해당 대상·원인 재검사는 반복 소비하지 않는다. |

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

종료 코드 0은 요청 범위의 원인 해소와 복구 실행 종료를 뜻하며 전체 프로젝트 완료·검증 성공·Merge를 합성하지 않는다. 입력 거부·대기·진행 중·미해결을 성공으로 바꾸거나 별도 차단·실패·교정·승격 이력을 초기화하지 않는다.

## 공통 verify 실행 경계

공통 verify는 도구별 수집·결과 해석을 adapter에 맡기고 실행량·시간 상한·자식 종료와 결과 완결성을 공통 경계에서 확인한다. 선언된 필수 단계와 해당 실행 범위의 결과가 모두 성공으로 완결돼야 한다. 부분 실행의 성공 범위는 [검증과 재시도](#검증과-재시도)에서 구분한다.

### 선언·launcher·adapter 책임

| 책임 경계 | 소유하는 계약 |
|---|---|
| 선언 검사: `verification/contract.py` | `scripts/verify.json`의 version 1·정적 adapter registry·프로젝트 interpreter·수집 옵션·순서 있는 단계를 검증한다. |
| Manager 연결과 launcher | `manager_adapter`가 고정 runtime의 Python·절대 진입점·protocol version·run/session·timeout과 기존 인스턴스 coordinator를 전달하고 launcher가 프로젝트 interpreter를 선택한다. standalone도 같은 `entrypoint.py`·선언을 사용하며 로컬 관측만 조율한다. |
| 단계 실행과 공통 core | `runner.py`가 단계 순서를 유지하고 `core.py`가 도구별 결과 validator·worker IPC를 받아 자원 배정·예약·프로세스 시작·재사용·종료 확인을 수행한다. unittest 타입이나 자체 compile 경로는 소유하지 않는다. 수집과 직렬 command에도 예약·전체 timeout을 적용한다. |
| unittest: `unittest_adapter.py` | 프로젝트 interpreter의 discovery/`load_tests`·재수집·fixture·native reporter와 명시한 Python compile 범위를 소유한다. 자식 import·Python 경로는 후보 worktree를 선택하며 Manager runtime 패키지가 후보 코드를 대신하지 않는다. |
| Perl: `perl_adapter.py` | 파일 suite 수집·재대조와 native TAP 결과 완결성을 소유한다. 정적 registry의 Perl executable은 Manager Python과 분리하고 공통 Python worker IPC·예약·종료 helper를 재사용한다. |

`result_validation.verify`는 선언이 없을 때만 기존 verify 경로를 실행한다. 잘못된 선언·미지원 version/adapter·실행 도구 부재는 연결 실패이며 기존 경로로 우회하지 않는다. 필수 필드·타입·경로와 단계 형식은 [선언 version 1](VERIFY.md#선언-version-1)을 따른다.

실제 시간은 `ExecutionClock`이 동일 검증의 누계 디렉터리에서 확인한 순번 대기만 제외해 계산한다. coordinator는 실행 중·종료 불명 자식이 없는 session의 정상 배정 heartbeat 구간만 대기로 기록한다. 자식 일부가 실행 중이면 부모 시간도 계속 흐른다. Manager·launcher·runner와 Windows/WSL 단계가 이 누계를 공유하고 단계별 남은 예산을 전달하며 테스트 worker에는 조율 환경을 전달하지 않는다. 준비·수집·복사·I/O·실행·결과 처리·정리 시간은 그대로 계산한다. 상세 연결은 [시간 예산](VERIFY.md#순번-대기와-시간-예산)을 따른다.

시간 상한을 전달하지 않는 구형 조율 호출은 runner·명령·프로세스/Windows Job 대기에서 바깥 Manager의 상한을 상속하며 새 기본 상한을 중첩하지 않는다. 명시적 상한과 단독 실행의 설정·기본 상한은 유지한다.

`perl_reporter.pl`은 공식 TAP::Parser로 테스트를 실행하고 즉시 flush하는 native JSON Lines(`*.native.ndjson`) 및 원본 TAP spool을 제공한다.

공통 진단은 EventWriter의 `*.jsonl`만 집계하며 native 원본의 별도 형식과 중단 자료를 보존한다. 파일 ID에 속한 동적 assertion 번호·plan과 parser/exit/signal을 대조해 완결된 suite만 완료로 기록한다. bailout은 plan 완료·정상 종료와 관계없이 suite 완료를 거부한다. nested subtest와 fixture 의미는 Test::More가 소유한다.

EventWriter 이벤트는 `clock_domain=sys.platform`과 run/stage/unit/worker identity를 보존한다. Windows launcher의 parent summary는 `result.json`·`snapshot.json`과 `<run>-wsl`·`<run>-windows`의 summary 경로·run ID·결과·elapsed를 연결하고 각 자식 실행의 clock domain 안에서만 시간을 계산한다. WSL run ID는 `parent_run_id`로 launcher run에 연결된다. native child에도 launcher의 parent run ID를 전달하고, parent는 일치하는 parent identity를 확인한 child summary에서 실제 native run ID를 가져온다. live WSL worker JSONL은 공유 bundle log에서 읽고 cleanup 뒤 형제 디렉터리로 보존한다.

새 `phase_start`·`phase_end` 쌍은 같은 32자리 소문자 16진수 UUID `phase_id`를 사용한다. `timing.summarize()`와 Manager progress reader는 변경될 수 있는 stage/unit identity 대신 이 ID로 phase 종료를 연결한다. ID가 없는 legacy 이벤트는 같은 run·clock domain·PID·phase 이름·시작 시각으로 연결한다. 따라서 `verify` phase가 stage ID 변경을 가로질러 FALLBACK·예외로 종료되고 `run_end`가 없어도 실행 중 항목으로 남지 않는다.

Manager progress reader는 현재 run의 완성된 줄만 한 번 소비하며 `test_interrupted`·stage 종료·run 종료를 파일 순서와 독립적으로 적용해 늦게 읽은 worker 시작이 active 표시를 되살리지 못하게 한다. 파일 읽기 실패는 미확인으로 남긴다. summary는 각 OS의 child metrics와 전체 parent elapsed를 분리해 PID나 monotonic clock을 합치지 않는다.

`LoopManagerVerifyGuard.pm`은 테스트/compile BEGIN 전에 일반 Perl process·fork·network 경계를 막으며 CORE/XS/threads 우회는 지원하지 않는다. 부분 결과는 native 로그에 보존하고 공통 core가 실제 worker 종료 후 예약을 회수한다. Perl은 unittest AST 부분 선정·시간 큐 재사용에 진입하지 않는다.

### 실행량·예약·시간 큐

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

### targeted test와 실패 수집

- Worker는 구현과 관련 targeted unit test를 작성하고, 수정 범위에 직접 관련된 최소 unit test command만 실행한다
- targeted unit test 실패는 Worker가 같은 세션에서 수정하고 재실행한다. 프로젝트 Python과 Manager runtime의 `verification/targeted.py`는 실행별 ID·기록 폴더와 JUnit의 안정적인 classname·name·파라미터 ID를 결합해 native 개별 결과를 보존한다. unittest subTest는 상위 `test.id()`의 최종 결과 하나로 집계한다. unittest 본문의 failure·error와 `unexpected_success`는 실패이며, 성공·skip·expected failure만 명시된 초기화 결과다. `TestCase`와 `IsolatedAsyncioTestCase`의 표준 `_callSetUp`·`_callTearDown` lifecycle 경로에서 callback의 원래 예외 identity를 대응시켜 동기·비동기 fixture 오류를 `fixture_error`로 남긴다. lifecycle callback이 실행 중임을 추적해 `addSubTest()`가 삼킨 setup·teardown subTest assertion도 해당 fixture에 귀속하며, native subTest 결과와 scope를 함께 기록한다. 본문 subTest는 상위 failure로 유지하고 본문과 teardown이 모두 실패하면 본문 failure 한 건과 별도 fixture 증거를 보존한다. class/module teardown과 같이 테스트 identity에 안전하게 귀속할 수 없는 fixture 오류가 동반된 성공 결과는 `indeterminate`로 관측한다. 수집·중단에는 개별 결과를 만들지 않는다.

  실행 가능한 관련 unit test가 없는 변경은 사유를 남기며 전체 검증을 생략하는 근거로 사용하지 않는다

- 실행 전 `targeted_test_collections`에 물리 invocation과 기록 디렉터리·파일시스템 identity·process receipt 경로를 결합한다. invocation ledger가 repository·ExecPlan·Worker 역할·guidance 작업·scope와 실제 provider/model을 소유한다. 기록 경로는 기존 `state/agent-runtime/<runtime>/<call>`이고 receipt는 쓰기 허용 call 바깥의 `<call>.process.json`이다. 정규 경로·기존 디렉터리·Worker 소유권과 호출별 경로의 유일성을 확인하며 파일시스템 device/inode는 Windows의 큰 식별자도 보존하도록 십진 문자열로 저장한다. 이 additive 테이블이 없는 과거 호출의 기록 위치를 디렉터리 탐색으로 추측하지 않는다.
- invocation 종료의 finally, Manager의 `reconcile_interrupted()`와 Worker 호출 전·CLI retry 전·호출 후 경계가 같은 미소비 기록을 재관측한다. 기존 process receipt 복구로 writer 종료가 확인되고 디렉터리 identity가 일치할 때만 읽는다. 살아 있는 수동 작업은 기존 소유권 보호를 적용한다. receipt 부재·종료 불명·경로 교체·symlink·읽기/해석/저장 오류에서는 원문·결합을 보존하고 해당 작업을 대기한다. 같은 작업의 앞선 호출이 대기하면 후속 호출도 순서를 보존하며 다른 repository·ExecPlan의 수집은 이어간다. runtime·모델 교체는 저장된 호출 귀속을 바꾸지 않는다. `AgentRequest.on_targeted_prepared`는 launch 객체 없이 같은 디렉터리·receipt를 결합하는 port이며 Claude의 실제 전달 연결은 별도 작업이다.
- Manager는 완료된 개별 결과를 SQLite `targeted_test_results`·`targeted_test_streaks`에 반영하고, 실행 ID의 invocation 귀속(`targeted_test_run_owners`)과 collection의 `collected` 전이를 같은 transaction에서 확정한다. 미소비 상태는 `pending` 또는 오류 종류를 가진 `error`이며 저장 실패·transaction 중단 뒤에도 재시도한다. repository·ExecPlan·실행 ID·테스트 ID로 중복과 상충 결과를 검사하고 다른 호출의 실행 ID 재사용은 거부한다. `failure`·`error`·`unexpected_success`는 누적하고 `success`·`skip`·`expected_failure`는 초기화한다. `fixture_error`·`indeterminate`는 감사 결과로 기록하되 기존 streak를 늘리거나 초기화하지 않는다. JSONL의 불완전한 마지막 줄과 기록 부재에서 성공·실패를 합성하지 않으며 수집·fixture 오류도 개별 테스트 실패로 세지 않는다. JUnit `<failure>`는 native 실패로, `<skipped>`는 skip으로 기록한다. fixture 여부를 구별할 원본 근거가 없는 `<error>`는 `indeterminate`이며, suite 오류와 명령 종료 코드는 별도 실행 오류로 남겨 개별 테스트 실패를 합성하지 않는다. 기존 저장 결과는 이 분류로 소급 변경하지 않는다. 세 번째 실패 도달은 같은 turn에서 뒤에 성공하더라도 소비 전까지 보존하고 새 Worker·CLI retry·verify 전에 `targeted_test_exhausted`로 차단한다.

  차단 확정과 도달 표식 소비는 같은 transaction이며 실행·실패 감사는 보존한다. Worker 최종 실패 보고는 `targeted_test_correction`, 수집 대기는 `targeted_collection_pending`으로 구별해 일반·rebase 보고 오류 한도를 소비하지 않는다. 근거 없는 실패 보고는 작업별 재시도 대기 뒤 재검증하고 수집 대기는 기존 자료를 먼저 재수집한다. 수집 대기는 별도 반환 표식으로 전달하며 물리 provider 결과·사용량·종료 감사를 덮지 않는다. 실제 provider 거부·완료 보고 오류·명시적 사용자 결정과 독립 안전 차단은 각 기존 경계에서 처리한다.

- 저장된 VERIFYING 진입은 선택된 repository·ExecPlan의 자료를 Worker 결과·사용자 결정 gate 뒤, 부분 실행·전체 실행·성공 receipt 재사용 전에 다시 관측한다. 기존 Worker 차단 경계가 세 번째 실패와 도달 표식 소비를 원자 처리하며, 미수집 자료는 `targeted_collection_pending` 조건 대기로 VERIFYING·Worker 결과·session·후보를 보존한다. 부분·전체 실행은 `scheduled_call("verify")`가 실제 verify slot을 배정한 뒤 process receipt 준비 전에 같은 자료를 재확인한다. 이 경계에서 차단·대기가 생기면 아직 자식 receipt가 없어 기존 task-call 확인 규칙이 미시작 verify 자리만 반환하고 새 verify 실행·실패 receipt를 만들지 않는다. 작업별 repository·ExecPlan 필터를 유지해 다른 작업의 기록·예약은 건드리지 않는다.

### 전체 verify와 Reviewer

- Worker는 현재 운영체제용 verify 스크립트가 없으면 생성하고 테스트 명령이 바뀌면 갱신한다
  - Windows: `scripts/verify.ps1`
  - Linux: `scripts/verify.sh`
- verify 스크립트는 프로젝트 전체 검증을 수행하고 실패 시 0이 아닌 종료 코드를 반환한다
- 자체 Windows verify는 checkout·runtime·상태 DB 위치와 무관한 `%USERPROFILE%\.cache\loop-manager\verify\tmp\`를 생성하고 `TEMP`·`TMP`·`TMPDIR`로 자식 프로세스에 전달한다. AppData 리다이렉션을 피하며 종료 시 기존 환경을 복원하고, 병렬 실행의 파일을 보존하기 위해 공유 임시 폴더 전체를 삭제하지 않는다. Defender 설정은 스크립트가 변경하지 않고 [운영 안내](OPERATIONS.md#windows-defender와-테스트-임시-폴더)에 따라 사용자가 선택한다
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
| [targeted unit test](PRODUCT.md#worker-targeted-unit-test-실패) | 실제 실행·테스트 ID별 연속 결과다. 후보·session 변화로 초기화하지 않으며 세 번째 실패 도달 표식은 차단 확정까지 보존한다. |
| [전체 verify](PRODUCT.md#전체-verify-실패) | 정규화한 verify 실패 지문별 발생 이력이다. 전체 verify 성공은 이 이력만 초기화한다. |
| [Review 교정](PRODUCT.md#review-교정과-level-승격) | Review 지문·level 승격 이력과 실제 시작한 Worker 교정 turn과 세 번씩의 묶음 대기를 별도로 저장한다. 유효 Review PASS는 Review 이력·진행 중 교정 집계만 끝내며 effective level·과거 승격 감사는 유지한다. |
| Worker 완료·문서 보고 교정 | 기존 보고 오류 한도를 사용한다. targeted test 교정은 `targeted_test_correction`으로 분리하고 동일 후보·명세·base의 보고 오류 집계는 검증 출력·session 변화로 초기화하지 않는다. |

[Agent CLI 실패](PRODUCT.md#외부-cli-실패)는 위 구현·검증 실패와 별도로 처리한다.

Agent CLI 실행 실패·timeout·일시적 서비스 오류는 한 호출 요청에서 최초 실행 외 최대 3회 재시도한다. 세션 재개 불가로 새 세션을 한 번 시작하는 복구는 별도로 집계한다. 묶음이 소진되면 recovery record의 identity/generation에 결합한 `retry_after`를 저장한다. 작업별 30초 대기는 호출 자리를 점유하지 않고 pause·확정된 handoff 신호를 확인하며 같은 실행에서 저장 단계를 다시 배정한다. runtime 원격 관측은 대기 해소 뒤 실제 단계 진입에서만 수행한다. runtime 교체 후에도 저장된 시각과 누적 감사를 유지한다. Review 교정은 시작한 turn만 세며 같은 피드백 turn의 예약·재처리는 중복 집계하지 않는다. AI 사용 한도는 실제 종료가 확인된 같은 provider/model의 독립 성공 기록 이후에만 재개하고 유료 probe·새 session·provider 전환으로 우회하지 않는다. 종료 불명 예약은 해당 token의 기존 receipt로 종료를 재확인한 뒤에만 회수한다.

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

실제 Git fixture의 `tests/support/git.py`는 최초 bare/seed init과 후속 명령에 global/system 설정 및 template을 격리한 환경을 전달한다. `XDG_CONFIG_HOME`도 통제 template 경로로 지정해 기본 전역 ignore·attributes를 제외하고 `GIT_ATTR_NOSYSTEM=1`로 system attributes를 차단한다.

실제 adapter·Python 자식까지 실행하는 테스트는 `isolated_git_test`로 setUp부터 cleanup까지 같은 환경을 상속하며 예외 후 호출자 환경을 복원한다. 저장소별 local 설정은 보존하고 상속 검사는 `git_command`의 명시적 `env`에 통제된 설정 파일을 제공한다. 기본 template은 테스트용 `info/exclude`와 실행 hook이 없는 hooks 디렉터리를 제공한다.

bootstrap 회귀의 부모·자식 CLI 계측은 실제 PID·시각과 테스트 이름을 보존해 전체 verify 기록에 포함한다. 부모 CLI 시간은 자식 실행을 포함하므로 둘을 합산해 전체 실행 시간으로 보고하지 않는다. 비용 개선은 같은 기준 코드·계측·병렬도와 테스트 수집 범위의 전후 실행을 비교하며, 준비·Git·snapshot 누적 시간과 전체 경과 시간을 구분한다.

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

Worker의 `WORKER_OUTCOME`은 `ready_for_validation`, `needs_user_decision`, `checkpoint`를 구분한다. checkpoint JSON의 필수 필드는 `kind`, 선택된 `plan_id`, 해당 호출의 전체 `start_head` OID이며 별도 `COMMIT_TITLE` 한 줄만 받는다. 중복·손상·다른 작업/HEAD·임의 명령 필드와 완료 publication 선언의 혼합은 거부한다. 완료 후보의 기존 COMMIT_TITLE·PR_SUMMARY·TARGETED_TESTS 계약과 사용자 판단 대기·legacy 완료 결과는 유지한다.

Worker가 종료하면 기존 scope 검사와 선택 frontmatter의 부분 복원 뒤 허용된 호출 변경 경로를 결정한다. 요청 밖 staged entry와 index flags·미확정 operation은 보존하고 commit하지 않는다. `execution_checkpoints.snapshot.worker_checkpoint_request`는 repository·작업·branch·worktree·시작 HEAD·요청 원문·제목·파일별 mode/content 지문·실제 session·호출 ID와 전달 attempt를 고정한다. 실제 호출의 writer 종료 근거를 확인하고 명시적 literal pathspec만 staging한다. 기존 Manager commit intent의 parent/tree/message와 실제 commit을 대조하며 저장 중단 뒤 commit이나 모델 호출을 반복하지 않는다. 원격 게시 확인 뒤 같은 transaction에서 요청 완료·scope 정리·전달 확인을 적용한다. 보존점은 Worker/targeted/verify/Review 성공 token을 만들지 않으며 기존 실패·session을 유지한다. 완료 후보를 Manager가 commit한 때도 다음 Worker 변경 전에 같은 게시 경계를 적용한다.

게시 직전 active ExecPlan·branch·worktree 소유권, origin identity, 관리 branch와 HEAD, Git operation, PR identity/head와 live 원격 OID를 검사한다. 과거 직접 Worker commit·부분 게시·인수 이력은 checkout이나 새 검증 worktree 없이 immutable parent/tree의 경로와 선택 선언·User Decision·mode를 commit별로 검사한다. 등록 base에 실제 포함된 commit은 원본 유입으로 구분한다. 나중에 되돌린 금지 중간 tree도 게시하지 않고 이미 확인한 선행 복구점과 미게시 로컬 이력을 보존한다.

`worker_checkpoint_pending`의 필수 호환 필드는 `{branch, from, target}`이다. `from`은 전체 OID 또는 정확한 부재 `null`, `target`은 게시할 전체 commit OID다. 신규 intent에는 repository·작업·worktree·origin·PR identity와 정확한 parent/tree도 결합한다. Manager는 intent와 완료 각각을 `CoordinatedConnection.run_request()`의 DB 전용 요청으로 확정한다. 매 시도 등록 origin/base·선택 실행 snapshot·checkpoint generation을 재조회해 관측 기준과 비교하고, 변경됐으면 경쟁 값을 보존하고 현재 적용을 거부한다. intent를 durable하게 저장한 뒤 고정 lease로 target 하나만 push하며, Git·PR·원격 관측은 요청 밖에서 수행한다. 완료 요청은 checkpoint의 accepted OID·intent 감사와 execution의 publication_remote_oid·PR head를 함께 갱신한다. BUSY/LOCKED는 요청 전체 rollback 뒤 같은 deadline·취소 경계로 재준비한다. 실행 port의 `run_checkpoint_publication_request()`는 각 callback이 반환하거나 실패할 때 `ExecutionStatePort.expected`를 요청 시작 snapshot으로 되돌려 COMMIT 경합·대기·취소에서 미확정 snapshot이 다음 시도의 검사를 막지 않게 한다. callback 결과와 갱신 snapshot을 함께 반환받아 COMMIT 성공 뒤 실행 snapshot과 게시 owner 기준을 확정한다. 외부 `state.bind()`의 종료 시 갱신은 요청 내부 재시도의 snapshot 복원을 대신하지 않는다. DB 재시도는 이미 수행한 외부 효과를 replay하지 않는다. 게시 port는 `DatabaseWaitTimeout`만 복구 횟수를 소비하지 않는 일시 대기로 반환하고, SQL/권한 오류와 runtime 취소는 원래 오류·제어 신호로 전파한다. 재진입은 보존된 intent와 원격 관측에서 결과 확정을 이어간다.

재시작에서 원격이 target이면 push 없이 확인을 마치고 from이면 동일 commit을 재시도한다. target의 후속 원격 전진은 자기 push 확인과 새 원격 입력 통합을 분리한다. 타인의 commit을 덮거나 낡은 intent에 새 lease만 끼워 넣지 않는다. 게시 직후의 소유권 판단은 완료한 DB·원격 관측에서 다시 시작하며 `pushed`와 후보 통합의 `candidate_changed`를 별개 사실로 소비한다. 불명확한 원격·다른 PR·충돌은 요청·양쪽 commit·index·실패를 보존한 대기이며 새 Worker 호출을 허가하지 않는다.

최초 원격 인수는 아직 소유하지 않은 worktree에만 적용한다. 기존 소유 worktree와 현재 Git·원격 근거로 checkpoint 및 소유권을 복구한 rebase는 `workspace_seen` 표식이 없어도 기존 작업 재개 경로를 사용한다.

DB 없는 원격 branch 인수 표식은 두 단계로 저장한다. `manager_metadata.created_from_absence=1`은 DB 파일이 없던 초기화에서만 생성되고, `workspace_seen:{repo}:{plan_id}`는 해당 계획의 관리 worktree 준비가 완료되면 저장된다. 기존·손상 DB와 일부 행의 부재는 이 경로로 전환하지 않는다. worktree 생성 전 `execution_checkpoints.snapshot.remote_adoption`에는 version 1과 repository·plan ID·정확한 branch·등록 base·전체 head OID를 저장한다. 재시작은 표식의 모든 identity를 다시 대조하며 기존 local branch/worktree가 있을 때 저장된 HEAD와 일치하지 않으면 연결하거나 덮어쓰지 않는다. 새 worktree는 저장한 전체 OID에서 생성한다.

원격 인수 시 `gh pr list --head <branch> --state all`로 branch의 전체 PR 이력을 확인한다. 목록이 상한에 도달하거나 조회가 실패하면 PR 부재로 추정하지 않는다. 해당 branch의 실제 MERGED 기록은 merge commit의 현재 base 조상 여부와 원격 branch 존속에 관계없이 완료로 넘기고 Worker를 시작하지 않는다. 정확히 일치하는 OPEN PR 하나는 기존 URL과 branch를 재연결하며 여러 OPEN PR·repository/base/head 불일치·불명 상태는 보류한다. CLOSED unmerged만 있으면 기존 PR의 reopen 또는 명시적 재개 판단을 기다린다. 새 환경은 DB 검증 증거를 승계하지 않으므로 검증·Review gate에서 현재 후보에 필요한 단계를 다시 수행한다. 원격 OID·lease는 동시 환경 조율을 제공하지 않으며 이전 환경의 writer 종료 뒤 순차 인수만 지원한다.

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

등록된 Open PR은 매 상태 동기화에서 Merge 가능 여부를 확인한다. 미확정이면 다음 조회에서 확인하고, Git 충돌이 확정됐을 때 관리 worktree에서 최신 원격 base를 rebase한다. 실제 구현 교정에 필요한 Worker·targeted test와 현재 후보의 전체 verify·읽기 전용 Review 뒤 같은 PR identity·원격 head를 재확인해 안전한 lease로 기존 branch를 갱신한다. 승인·필수 check 대기는 rebase로 해소할 충돌로 간주하지 않는다.

PR 없는 원격 branch의 rebase 복구도 SQLite에 저장된 동일 Manager 소유 branch일 때만 검증된 원격 기대값의 `--force-with-lease`를 허용한다. 게시 직전 최신 base와 후보의 verify·Review token·현재 입력을 대조한다. base·HEAD 변경은 근거별 재판정 사유이며 완료 Worker 결과·verify receipt·일반 선행 Review를 일괄 폐기하거나 일괄 재사용하지 않는다. 조건을 만족하지 않는 근거만 필요한 검증·Review를 다시 수행한다.

Merge 확인·worktree 제거·branch 정리·ExecPlan archive는 각각 결과를 기록한다. Merge 사실을 정리 성공으로 추정하거나 정리 보류로 MERGED를 되돌리지 않는다.

`assess_cleanup`은 tracked·staged·non-ignored 변경, index flag·Git operation과 PR head OID·commit object·로컬 HEAD의 포함 관계를 검사한다. 정리 소유권은 계획·등록 worktree·branch와 관리 경로를 대조한다. `remove_cleanup_worktree`는 판정 뒤 HEAD·branch·index·파일·operation을 다시 확인하고, 관리 journal로 잠금 해제·제거·확정을 복구하며 강제 옵션을 사용하지 않는다. ignored-only는 정리를 막지 않는다. Merge 방식·base ancestry·원격 branch 존재만으로 안전성을 추정하지 않는다.

정리가 불확실하거나 실행 자리를 사용할 수 없으면 `cleanup_pending`에 경로·원인·PR head·소유 여부를 보존하고 MERGED 전이는 유지한다. 재검사도 소유권·안전 조건을 요구하며 소유권 불명 경로는 자동으로 살피거나 제거하지 않고 부재가 확인될 때만 기록을 해제한다. 미완료 worktree 정리와 아래 ExecPlan archive는 후속 작업·프로젝트 완료의 차단 조건이 아니다.

현행 Merge 정리 구현은 관리 worktree를 제거하며 로컬 branch를 삭제하지 않는다. 로컬 관리 branch 비강제 삭제는 설계 기준과 구현의 차이로 명시하며 원격 branch·진단 ref 삭제의 후속 목표로 확대 해석하지 않는다.

### MERGED ExecPlan archive

ExecPlan은 GitHub Merge 확인과 SQLite MERGED 전이 뒤에만 별도 정리 대상으로 삼는다. 최신 원격 base의 detached 임시 worktree에서 `docs/plans/`의 문서를 같은 파일명의 `docs/plans/archive/`로 내용 보존 rename-only commit해 직접 fast-forward 게시한다.

보관 근거는 최초 게시본이 아닌 catalog의 현재 최종 문서 hash와 `current_revision_sha`다. operation identity는 base·계획 ID·이전/보관 경로·최종 hash·revision에 결합하며 준비 commit의 실제 rename-only diff와 후보 정의를 게시 전에 다시 확인한다.

| 관측·중단 조건 | 보관·재개 결과 |
|---|---|
| 현재 revision이 있고 일반 파일 source의 최종 hash가 일치하며 destination·다른 동일 ID 보관 문서가 없음 | 안전한 후보만 묶어 게시한다. 충돌한 다른 후보는 pending으로 분리한다. |
| source가 없고 같은 destination에 최종 hash가 일치하는 일반 파일이 이미 있음 | 원격 보관 완료를 관측해 확정하며 새 commit·push를 만들지 않는다. |
| source/destination 충돌·양쪽 부재·다른 동일 ID 보관 문서·비일반 파일·hash 불일치·현재 revision 부재 | 해당 후보의 실제 이유를 archive pending에 보존하고 덮어쓰지 않는다. |
| push 실패 뒤 같은 base·후보의 준비 commit이 유효함 | 저장한 commit을 검증해 재사용한다. base·후보가 달라졌으면 최신 관측으로 다시 계산하며 낡은 후보를 그대로 게시하지 않는다. |

원격 base·후보 정의는 commit 준비와 게시 직전에 재확인한다. 경쟁·게시·정리 실패는 보존한 operation·OID·이유로 재시도하며 MERGED 상태·독립 후속 작업·프로젝트 완료를 막지 않는다.

### Requirement 교정 게시와 복구

Requirement 교정은 모델 없이 식별 정보·확정 상대 링크를 보정해 같은 commit으로 게시한다. 편집 의미와 source 보존은 [Requirement 교정과 완료 보관](#requirement-교정과-완료-보관)을 따르며, 이 절은 파일 적용·원격 게시와 중단 복구를 소유한다.

PR 관계 동기화 뒤 기존 housekeeping 안전 gate 안에서 ExecPlan archive보다 먼저 실행한다. `requirement_correction_publication`은 기존 detached-worktree Git adapter·기록된 fetch·자원 lock·fast-forward push를 재사용하고 기본 checkout·Agent/verify worktree를 편집하지 않는다.

| 진입·실행 조건 | 처리 |
|---|---|
| 진입 전 안전 gate가 닫혔거나 수동 작업이 Requirement source를 소유함 | 원격 관측·편집 없이 교정만 보류한다. 기존 후보·작업물·실패 원인을 보존하고 기존 수동 verify·Review·PR 복구와 독립 실행을 계속한다. |
| 진입 뒤 안전 gate·미확정 Git 쓰기·종료 불명 writer·수동 source 소유·Requirement draft·planning publication의 경쟁 또는 실패 | 후보와 이유를 보존하고 이번 단계를 중단한다. 교정 실패를 비차단 archive 진단으로 삼켜 Planner로 넘기지 않는다. |
| 보류 원인·소유권이 해소됨 | 저장 후보와 현재 원격·관계를 다시 확인해 아래 상태별로 재개한다. |

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

일반 계획 게시·Worker 최초 시작·유지보수 cycle 시작/종료는 outbox 사건을 만들지 않는다. `enqueue_progress`는 draft 판단·PR 사건 종류만 허용한다. `record_process_start`는 실제 시작을 기존 incident 기록에 연결하며 일반 시작 알림을 위한 과거 outbox 조회는 수행하지 않는다. 유지보수 저장소는 알림용 계획 목록 조회·enqueue 없이 기존 cycle 상태·범위·Merge 확인·종료를 기록한다. 현재 draft 판단 목록은 `draft_decisions`로 저장한다. 미해결 targeted test와 단순 단계 실패는 별도 단순/지속 실패 사건을 만들지 않는다. 차단 통지는 아래 공통 조치 판정을 따른다.

조각 claim은 `blocked`·`pr_created`·`draft_decisions`·기존 개별 `draft_decision` 외 사건의 잔여 전송을 취소한다. 구정책 진행·완료·대기·재개 사건에도 적용하며 payload·고정 본문·cursor·attempts·오류는 보존한다. 일반 dispatch·즉시 전송·실패 재시도는 같은 claim을 사용한다. `policy.is_current`를 생성과 각 claim의 공통 판정으로 사용한다. PR은 같은 ExecPlan의 `PR_OPEN`·URL과 저장된 실제 PR head를 확인하고, 직렬화된 `DefinitionSnapshot`·`validated_base_sha`·`pr_head_oid`에서 기대 `review_token`과 `publication_token`을 다시 계산한다. Review 결과와 두 token이 현재 값과 일치하고 `pr_review_candidates`에 같은 publication token의 검토 입력 증명이 있어야 게시 후보로 인정한다. 결과 문자열로 사용자 승인을 추론하지 않는다. 사건 identity는 repository·PR URL·reviewed change identity·`review_merge` 사용자 행동에 결합된다. reviewed change identity는 definition snapshot과 canonical diff identity로 계산한다. Git diff는 raw bytes로 읽어 decode 대체 문자가 서로 다른 파일 내용을 합치지 않게 한다. base/head object ID·숫자 hunk 범위·Git이 생성한 가변 section label을 정규화하고 변경 줄 및 각 hunk 양옆 한 줄의 context는 보존한다. gitlink(mode `160000`)는 `--submodule=short` diff의 mode·이전/새 OID로 증명하며 blob 전용 blame을 호출하지 않는다. 일반 파일의 각 hunk old-side 줄은 `git blame --porcelain`으로 원래 commit과 그 commit에서의 줄 위치에 결합한다. 따라서 양옆 줄까지 같은 반복 구간의 다른 위치를 구분하면서 upstream이 앞에 동일 context를 추가해도 기존 줄의 identity는 유지한다. 현재 base의 절대 줄 번호나 context 출현 순번은 identity에 포함하지 않으며 원래 위치 증명을 얻지 못하면 후보를 보류한다. 따라서 같은 함수 안에서도 위치가 다른 변경·의미 있는 공백·비UTF-8 byte는 구별하며 line offset만 이동한 동일 patch는 rebase 뒤에도 같은 후보가 된다. 현재 publication token은 저장 후보의 유효성을 검증하며, 이미 큐에 저장된 head/token과 달라도 현재 stable candidate identity·PR URL·행동이 같으면 같은 알림을 이어 보낸다. 취소된 PR 알림이 같은 후보로 재확인되면 별도 outbox version을 만들고 원래 payload·취소 시각을 보존한다. 후속 version은 현재 후보 payload와 `continued_from` event key를 기록하고 처음 발생 시각·고정 본문·확인 cursor·attempt/error를 이어받아 남은 조각만 전송한다. 전송 완료가 확인된 version은 재base 후에도 재통지하지 않으며 늦게 확인된 이전 요청은 같은 본문 계보의 cursor에만 반영한다. PR 소유 복구·게시 경계가 외부 PR 관측을 수행하고 확정 상태를 SQLite에 기록한다. 일반 scheduler dispatch와 즉시 project-event flush는 PR 사건을 제외한다. 새 PR은 확인된 게시 경계에서 첫 전송을 시도하고, 기존 PR의 재시도는 성공한 원격 PR 관측 callback 뒤에만 수행한다. 따라서 원격 조회 실패 시 오래된 PR 상태만으로 전송하지 않는다. Merge/Close는 관측 상태를 DB에 반영한 뒤 claim에서 취소하고, 충돌 PR의 원격 head가 저장된 head와 달라 적용을 보류한 경우에는 해당 계획의 미전송 사건을 관측 callback에서 취소한다. 알림 정책은 추가 원격 조회나 Git 쓰기를 하지 않으며 누락·불일치 후보는 미전송분을 취소한다. draft는 기존 질문 목록·판단 identity/revision을 재사용한다.

`policy.block_action`은 현재 block의 scope·대상·원문 원인과 복구 기록의 최종 원문 근거·현재 계획 revision을 대조한다. 복구 fingerprint는 기존 `RecoveryObservation.identity`를 재계산해 무결성을 확인하며, block의 release fingerprint는 입력·설정 재검사의 원본으로 유지한다. 두 지문은 서로 다른 의미이므로 값의 일치를 요구하지 않는다. 해당 scope의 복구가 pending이거나 필요한 근거가 손상·누락되면 보류한다. `block_recovery`는 최종 근거의 코드 소유 차단 원인으로 scope를 구분하며 생성·정책·claim의 사건 키 계산에 같은 관측을 사용한다. 다른 scope의 최종 recovery와 그 근거를 보존한 복구 완료(`reconcile`) 기록은 현재 계획의 질문·승인 근거를 대체하지 않는다. 계획 차단을 기록할 때 일치하는 최종 recovery를 같은 transaction에서 `execplan_blocks.recovery_snapshot`에 보존한다. 이 선택적 nullable JSON object는 identity·원인 코드·phase·계획/base revision·fingerprint·progress marker·detail·pending_action과 JSON 문자열 final_evidence를 유지한다. final_evidence 안의 검증된 Worker 결정이 질문 원본이다. 공유 recovery 부재 또는 다른 scope의 최종·복구 완료 기록에는 이 snapshot을 재사용하되 현재 block 원인·대상·계획 revision과 identity 무결성은 다시 검사한다. 같은 scope의 pending/손상 기록은 우선하며 과거 snapshot으로 우회하지 않는다. 기존 DB에는 nullable 컬럼을 추가해 행을 보존하며, snapshot이 없는 기존 기록에서 질문을 합성하지 않는다. 계획 차단 교체·해제에는 snapshot도 함께 교체·제거한다. 지침 승인·명세 교착은 자체 pending 승인 행·파싱한 원문을 조치 원본으로 사용하고 사건 키에서 공유 recovery identity를 제외해 독립 scope 변경에 따른 중복 통지도 막는다. 허용 근거는 이미 검증한 Worker 결정의 질문/위치, 파싱·허용 문서 검사를 통과한 SPEC_DEADLOCK, 현재 pending 지침 승인, 같은 닫힌 PR·게시 branch 삭제에 연결된 조치, 기록된 verify 상한과 실제 timeout 결과다. 최종 disposition·예산 소진·user_targets만으로 복구 불가능성을 추론하지 않는다. 조치 없는 최종 중단은 아래 명시적 복구 경로 근거 계약을 만족해야 한다. 원인이 일반 진행/복구/불충분 근거이면 outbox 없이 원본 상태와 진단을 유지한다.

새 blocked payload의 선택적 `action` object는 현재 조치의 snapshot이다. `kind`는 `decision`·`specification`·`guidance`·`reopen_pr`·`restart`·`verify_timeout`·`unrecoverable`이며 질문은 `decision`, 승인 목록은 `proposals`, PR은 `pr_url`, 재시작은 `branch`, 상한은 양의 정수 `seconds`에 결합한다. `guidance.proposals`는 승인 순서대로 `[proposal_id, proposal_hash, target_path, proposal_line, decision_line]` 배열을 보존한다. `unrecoverable`은 원래 recovery의 `identity`를 `recovery_identity`로 보존하며 이 action이 없는 구정책 payload를 복구 불가 통지로 소급 해석하지 않는다. 나머지 기존 payload에 action이 없더라도 현재 원본으로 유효성을 확인할 수 있으면 그대로 전송한다. action이 있으면 현재 조치와 정확히 비교하며 원래 payload·고정 본문을 소급 갱신하지 않는다. 변경된 조치는 기존 incident의 다음 version으로 연결하고 취소·전송 감사를 남긴다. 중복 판정은 현재 outbox payload와 비교하며 incident에 남긴 첫 확인 원문은 감사용으로 보존한다. 같은 조치의 기존 행이 취소됐고 전송 완료가 확인되지 않았다면 현재 근거 재확인 뒤 다음 version의 새 outbox를 만든다. 기존 취소 행·attempt/error·payload는 변경하지 않으며 새 version은 기존 고정 본문·확인 cursor를 승계해 잔여 조각만 전송한다. 미취소 행·전송 완료 행과 이력이 누락된 행은 이 재생성 대상이 아니다. 같은 조치를 이어받은 payload의 선택적 `continued_from`은 직전 outbox event_key 문자열이다. 이전 HTTP 요청의 늦은 성공은 동일한 고정 본문을 가진 이 계보의 후속 version에도 확인 cursor·전체 완료를 전파한다. 계보가 없는 기존 payload는 그대로 지원하며 새 질문·다른 조치로 교체한 version에는 이전 확인을 전달하지 않는다. 취소 시각·원문·attempt/error는 보존한다.

`RecoveryObservation.unavailable_recovery_routes`는 복구 소유자가 이미 확인한 복구 불가 사실만 전달하는 선택적 JSON object다. 최종 판단일 때 `final_evidence`에 보존하며 기존 복구 결정·예산·재개 정책은 바꾸지 않는다. 허용 키는 `reobservation`·`retry`·`correction`·`condition_wait`·`next_run`·`safe_recovery`의 정확한 여섯 개이며 값은 각 경로를 사용할 수 없는 근거인 비어 있지 않은 문자열이다. 현재 원인·지문·revision과 일치하는 최종 기록에 이 근거가 모두 있고 사용자 조치가 없을 때만 `unrecoverable`을 선택한다. 기존 기록의 필드 부재·부분 근거는 보류하며 일반 final 분류에서 자동 생성하지 않는다. 본문에는 원래 원인·보존 결과·경로별 근거를 표시하고 가짜 질문·조치 안내를 붙이지 않는다.

`configure_session`은 credential 없이 연결별 TEMP 상태에 활성 여부와 현재 양의 정수 `verify_timeout_seconds`를 묶는다. scheduler·수동 recover·독립 task 연결은 transaction 밖에서 읽은 설정을 전달한다. 미확인 값은 NULL로 처리하며, 상한 조치는 현재 값·block 지문·실제 verify timeout 결과가 모두 같은 상한일 때만 허용한다. 값 변경·누락은 이전 통지의 잔여 조각을 취소하며 기존 설정·오류·감사를 덮지 않는다.

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

차단 해제와 해당 사건 해제·미전송 조각 취소는 같은 transaction에 반영한다. incident의 실제 시작은 기존 원인 해제·작업 identity·첫 시작 시각과 연결해 Telegram 설정과 독립적으로 보존한다. `record_work_start`는 처음 확정한 통지 확인·실제 시작 연결 수를 반환하므로 PR 재개 운영 로그는 메시지 생성에 의존하지 않는다. `resumed_at`은 기존 `confirmed_before` 또는 현재 outbox의 확인된 조각이 있고 해당 작업이 시작됐을 때 한 번 기록한다. 전송 확인 callback은 원래 조각 cursor와 incident 확인 감사를 전진시키고 이 연결만 확정하며 새 메시지는 만들지 않는다. 실패 callback과 시작 복구도 시작/재개 outbox를 만들지 않고, 시작 복구는 취소 사건의 중단된 claim 표식과 저장된 시작만 정리한다. 과거 planning 중복 사건의 확인 이력은 현재 사건에 연결하고 과거 행을 삭제하지 않는다. 안정적 작업 소진은 draft·계획 차단을 포함하되 프로젝트 차단을 제외하며 완료·대기 상태는 [프로젝트 최종 확인과 완료 판정](#프로젝트-최종-확인과-완료-판정)을 따른다. 새 PRODUCT·ARCHITECTURE·Requirement 변경 등으로 갱신된 완료 주기와 내용 보존 archive를 구분한다.

`notifications/rendering.py`는 저장된 payload만으로 텍스트를 결정적으로 생성하며 모델을 호출하지 않는다. 확인된 질문·수정 대상·PR 상태 순으로 정보를 구성하고 원인·복구 시도·Checkpoint 근거를 보존한다. 공통 payload와 `draft_decisions` 목록의 계약은 다음과 같다.

| payload 필드 | 타입·의미 |
|---|---|
| `occurred_at`·`silent`·`status` | 발생 시각은 UTC ISO 8601 문자열, 무음 선택은 boolean, 처리 상태는 문자열이다. 전송 시각·재시도에 맞춰 원래 의미를 다시 쓰지 않는다. |
| `project` | 표시할 프로젝트 이름 문자열. 사건의 저장 범위는 outbox의 `repo`에 결합한다. |
| `plans` | `draft_decisions`의 판단 대기 항목 배열. ID 순으로 구성한다. |
| `plans[].id`·`plans[].title` | ExecPlan ID·제목 문자열. 본문은 전체 건수와 항목별 ID·제목을 표시하고 제목의 공백을 정리한다. |
| `plans[].url` | 확인된 원격 문서 URL 문자열 또는 `null`. 값이 있을 때 ID에 링크하고 없으면 일반 텍스트로 표시한다. |
| `plans[].decision_identity`·`plans[].decision_revision` | 저장된 판단 identity(문자열 또는 `null`)·판단 revision 문자열. ID와 함께 중복·현재성 판단에 사용한다. |

`pr_created` payload는 기존 프로젝트·ExecPlan·PR URL·발생 시각·상태 필드와 함께 다음 후보 근거를 요구한다. 이 event type은 새 PR과 기존 PR의 새 게시 후보를 함께 표현하며 본문은 PR이 생성됐다고 단정하지 않는다.

| payload 필드 | 타입·의미 |
|---|---|
| `candidate_identity` | `DefinitionSnapshot`과 안정 patch identity로 만든 검토 입력 identity 문자열. 사건 키에 사용하며 base·head OID가 달라져도 같은 검토 입력을 유지한다. |
| `candidate_head_oid` | 해당 게시·검토 입력의 PR head OID 문자열. 현재 후보 확인용이며 단독으로 새 사건을 만들지 않는다. |
| `candidate_publication_token` | payload를 만들 때 확인한 게시 token 문자열. `DefinitionSnapshot`과 현재 검증 base·PR head에서 token을 다시 계산하고 저장된 review candidate proof와 대조한다. 사건 identity는 아니며 같은 후보의 rebase 뒤에는 이전 payload 값과 현재 token이 달라질 수 있다. |
| `user_action` | 정확히 `review_merge`. 현재 PR이 열려 있고 이 사용 행동이 남아 있음을 나타내는 알림 계약값이지 자동 Review 결과나 사용자 승인 증거가 아니다. |
| `continued_from` | 선택적 event key 문자열. 취소된 동일 후보의 남은 전송을 새 outbox version으로 이어갈 때만 직전 version을 가리킨다. |

생성과 조각 claim은 같은 후보 identity·행동·URL을 다시 대조하고 현재 행의 `PR_OPEN`·Review 결과·token·`pr_review_candidates` proof도 검증한다. 누락·불일치 후보는 전송하지 않고 잔여 조각을 취소한다. 후보 identity가 다른 변경이면 새 event key를 사용하며, 같은 후보의 반복 enqueue·복구 확인은 기존 version을 유지한다. 취소된 version은 payload·취소 시각·확인 cursor·attempt/error를 바꾸지 않는다. 같은 후보가 다시 유효해지고 아직 어떤 version도 전송 완료되지 않았으면 다음 version에 최신 후보 payload와 원래 발생 시각·고정 본문·확인 cursor·attempt/error를 연결한다. 이전 version의 늦은 전송 확인은 같은 고정 본문을 이어받은 후속 version에만 cursor를 전파한다.

본문은 Python 문자열의 문자 위치로 최대 4096자씩 나눈다. draft 링크는 각 조각 안의 ID 구간에 `text_link` entity로 붙이며 `offset`·`length`는 UTF-16 code unit 수로 계산한다. 분할 위치와 entity 단위를 구분해 한글·이모지·조각 경계에서도 ID와 URL의 대응을 유지한다.

draft 목록의 ID·판단 identity·revision이 같으면 반복 생성하지 않는다. 목록·판단 근거가 바뀌면 새 목록을 만들고 오래된 목록의 미전송 조각은 취소한다. 빈 목록은 새 알림을 만들지 않으며 각 조각 전송 전 현재 구성·근거를 재검사해 해소·변경된 목록을 취소한다. 취소된 목록의 확인 조각과 감사는 보존한다.

기존 개별 `draft_decision`과 다른 계속 유효한 과거 사건은 저장된 payload·고정 텍스트·주의 수준·진행률로 이어 전송하며 새 목록 형식으로 소급 재작성하지 않는다.

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

프로젝트 전체 원격 동기화는 branch 목록·OID를 한 번 조회하고 변경된 branch의 객체를 하나의 `fetch --stdin`으로 받는다. 목적 ref 없는 명시적 입력과 빈 `--refmap=`을 사용하고 prune·태그·submodule 갱신을 끈다. 같은 repository의 fetch와 `FETCH_HEAD` 결과 확보는 공유 fetch 잠금으로 보호하며, Git이 알려준 `FETCH_HEAD` 경로에서 branch별 OID를 읽어 조회 결과와 대조한다. 일반 branch 삭제나 OID 경쟁은 전체 재관측·일괄 수신을 한 번만 재시도하고 필수 base 부재·관측 실패는 보존·재시도 경계로 돌린다. 수신이 확인된 뒤 기존 ref별 journal과 예상 이전 OID로 추적 ref를 반영한다. 변경 없는 ref도 미확정 journal을 먼저 처리하므로 단일 fetch로 남긴 과거 기록과 일부 ref 반영 뒤 중단을 같은 형식으로 재개한다.

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

재사용은 현재 주기에 선택된 실제 전체 성공 run과 현재 저장소 코드 입력 관측이 일치할 때만 허용한다. 목록·fingerprint 계산이나 verify 성공 자체로 목록 밖 코드 입력 누락을 해소하지 않는다. 환경 재현성·외부 동적 입력의 동일성은 요구하지 않는다. 실제 지원 범위는 [운영 입력 관측 adapter](#운영-입력-관측-adapter)를 따른다. 일반 선행 PASS 뒤에는 별도로 연결된 실제 전체 run이 필요하다.

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
