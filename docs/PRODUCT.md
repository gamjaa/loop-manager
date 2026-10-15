# Loop Manager

## Summary

외부 스케줄러가 반복 실행하며 여러 로컬 Git 프로젝트의 계획, 구현, 검증과 PR 생성을 자동으로 진행하는 Loop Engineering 관리 프로그램을 만든다.

## Target Users

로컬 Git 프로젝트를 Loop Engineering 하려는 프로그래머

- 프로젝트의 제품 문서와 기술 설계를 작성한다
- 대상 프로젝트를 등록하고 외부 스케줄러로 Loop Manager를 반복 실행한다
- BLOCKED 사유와 생성된 PR을 확인하고 필요한 결정을 내린다
- 검토가 끝난 PR을 Merge한다

## Scope

### Current Goals

- 여러 로컬 GitHub 프로젝트의 필수 문서를 검증해 등록하고 runtime 공통 Agent 지침을 모델 호출에 전달한다.
- Specification Skill로 PRODUCT·Requirement·ARCHITECTURE 작성을 지원한다.
- 미충족 PRODUCT·Requirement 작업을 Worker 시작 전에 ExecPlan catalog로 만들고, 메타데이터·의존성·우선순위에 따라 처리한다.
- 계획별 상태·재시도·Agent session·작업 공간을 SQLite에 보존해 중단 후 재개한다.
- Worker가 구현·관련 테스트·전체 검증 스크립트를 작성하고, 프로그램이 전체 검증과 독립 Review를 실행한다. 실패는 기존 작업에서 교정한다.
- 검증된 작업으로 PR을 생성하고 사용자 Merge를 기다린다.
- 전체 충족이 확인된 Requirement는 일반 planning 게시에서, MERGED ExecPlan은 별도 정리 commit에서 자동 archive한다.
- 같은 프로젝트의 자동 변경은 직렬로, 다른 프로젝트는 설정한 동시 실행 수 안에서 병렬로 처리한다. 자원이 충돌하지 않는 수동 명령의 준비와 로컬 복구 요청 접수는 자동 실행 중에도 수행한다.
- 변경되지 않은 입력에서는 Planner를 반복 호출하지 않는다.
- PR 생성·차단·프로젝트 완료를 Telegram으로 알린다.

### Non-Goals

- 같은 프로젝트 안의 자동 변경 작업 병렬 실행
- 자동 Merge
- 내장 스케줄러
- 지정된 이벤트 외 실시간 상태 알림

## Product Invariants

- 미충족 PRODUCT 또는 활성 Requirement에 근거한 작업만 계획한다. 범위 밖 아이디어는 현재 작업으로 자동 승격하지 않는다.
- 제품 문서는 동작과 장기 규칙, Requirement는 원하는 변화와 Acceptance Criteria, ExecPlan은 구현 계획을 기록한다. 자동 수정 권한은 각 문서의 기존 예외로 제한한다.
- ExecPlan ID는 계획과 실행 이력을 연결하는 안정적인 식별자다. 실행 상태의 원본은 SQLite이며 문서에 상태를 기록하지 않는다.
- 자동 작업은 최신으로 검증한 `origin/<base-branch>`를 기준으로 한다. 기본 worktree의 로컬 변경·commit은 보존하고 자동 입력에 포함하지 않는다.
- Agent의 파일·명령 접근 권한은 provider sandbox와 역할별 쓰기 정책으로 강제한다. 호출 전후 Git refs·config·다른 worktree·HEAD·index·파일 상태 차이는 접근 권한 위반으로 판정하지 않으며, ledger 부재·실패·fallback·재시작에서도 이 비교를 되살리지 않는다.
- 일반 verify는 Agent ledger가 없다는 이유로 repository-wide 비교를 요구하지 않는다. 현재 verify 대상 worktree와 역할에 필요한 자원을 검사하며, 기본 checkout이나 검증과 무관한 ref·worktree 변화는 후보와 실제 검증 입력을 바꾸지 않는 한 성공을 막지 않는다.
- 실행 중인 변경 작업은 다른 작업이 선점하지 않는다. 현재 작업에 적용되는 문서 변경은 각 단계 진입 전에 반영하며, 다른 작업의 선택은 기존 안전한 선택 경계를 따른다.
- 같은 프로젝트의 변경 실행은 하나이며, 모든 선행 ExecPlan이 MERGED인 계획만 시작한다. 서로 다른 프로젝트는 `max_workers` 안에서 병렬 처리한다.
- Agent는 선택된 관리 worktree와 역할별 sandbox에서 작업한다. sandbox 적용을 확인하지 못하면 호출을 중단하며, 역할별 최종 문서 변경·결과·게시 후보는 별도로 검증한다.
- 실패·중단·차단 시 작업물·session·진단을 보존한다. 원인과 안전 조건이 실제로 해소된 작업만 재개한다.
- BLOCKED 해제는 이전 writer 종료와 현재 repository·작업·worktree·branch 연결을 확인한 뒤 현재 후보를 새 입력으로 검증한다. 폐기한 Git 상태 비교에서 생긴 차단·planning 오류·지속 실패는 입력 지문 변경 없이 자동 재평가한다. 과거 snapshot과 복원 intent는 감사로 보존하고 사용자 파일·index·HEAD를 되돌리지 않는다. 실제 충돌·접근 실패·sandbox 오류·독립 승인과 Review 실패는 각 복구 경계에서 유지한다.
- 최종 후보의 관련 테스트·전체 검증·독립 Review가 성공해야 PR을 생성한다. 실행 중인 작업의 MERGED 전이는 연결 PR의 실제 Merge를 근거로 한다. 로컬 DB에 작업 행이 없는 경우에는 archive 문서를 완료 근거로 받아 MERGED 행을 복구한다.
- 사용자 변경을 승인 없이 삭제하거나 덮어쓰지 않는다. 정리 안전성을 확인할 수 없는 worktree는 보존한다.
- 같은 사용자 상태 디렉터리에서는 자동 실행을 하나만 허용한다. 수동 명령의 준비는 계획과 실제 자원의 소유권을 원자적으로 확인·예약한 뒤 동시에 수행할 수 있다. 로컬 복구 요청도 동시에 접수할 수 있으며 실제 복구는 전역 실행 잠금과 프로젝트 실행 자리를 준수한다.
- 동일한 PR·차단·완료 사건은 중복 통지하지 않는다.

문서 입력·등록·작업 선택·실패·게시의 상세 정책은 아래 해당 절에서 정의한다. 저장·검증·Git 적용 방식은 [ARCHITECTURE.md](ARCHITECTURE.md), ExecPlan 형식은 [PLANS.md](PLANS.md), 설치와 실행은 [README.md](../README.md)를 따른다.

### 활성 변경 요구사항과 현재 동작

본문은 현재 Merge된 동작을 설명한다. 아래 활성 Requirement의 변경 범위·보호 조건은 계획·구현에 적용하고, `archive/` 링크는 과거 결정의 배경으로만 참조한다. 문서 작성·로컬 commit만으로 구현 완료로 취급하지 않으며 구현 PR Merge 뒤 장기 규칙을 본문에 반영한다.

| 변경 범위 | 관련 Requirement |
|---|---|
| Planner 역할 선택·변경 중심 검토·최종 확인·draft와 Agent 문서 권한 | [R-049](requirements/archive/%5BR-049%5D%20Input-Specific%20Planner%20Roles.md), [R-057](requirements/archive/%5BR-057%5D%20Change-Focused%20Planning%20and%20Final%20Full%20Verification.md), [R-061](requirements/%5BR-061%5D%20Agent%20%EC%97%AD%ED%95%A0%C2%B7%EB%AC%B8%EC%84%9C%20%EA%B6%8C%ED%95%9C%EA%B3%BC%20draft%20ExecPlan%20%EC%82%AC%EC%9A%A9%EC%9E%90%20%ED%8C%90%EB%8B%A8.md) |
| Worker 최초 level·Review 승격·Planner와 Worker의 명시적 모델 설정 | [R-052](requirements/%5BR-052%5D%20Worker%20%ED%8C%90%EC%A0%95%20%EA%B8%B0%EC%A4%80%20%EA%B5%AC%EC%B2%B4%ED%99%94%EC%99%80%20%EC%B5%9C%EC%B4%88%20%EB%B0%B0%EC%A0%95%20%EC%99%84%ED%99%94.md) |
| 차단 범위·비차단 로컬 base 동기화·Git 통합과 원격 보존·로컬 환경 재검사 | [R-055](requirements/%5BR-055%5D%20%EC%B0%A8%EB%8B%A8%20%EA%B2%BD%EB%A1%9C%20%EC%A0%84%EC%88%98%20%EC%A1%B0%EC%82%AC%EC%99%80%20%EB%B6%88%ED%95%84%EC%9A%94%ED%95%9C%20%EC%9E%91%EC%97%85%20%EC%A4%91%EB%8B%A8%20%EC%B6%95%EC%86%8C.md), [R-056](requirements/%5BR-056%5D%20%EC%9B%90%EA%B2%A9%20%EA%B8%B0%EC%A4%80%EC%9D%84%20%EC%9C%A0%EC%A7%80%ED%95%98%EB%8A%94%20%EB%B9%84%EC%B0%A8%EB%8B%A8%20%EB%A1%9C%EC%BB%AC%20base%20%EB%8F%99%EA%B8%B0%ED%99%94.md), [R-060](requirements/archive/%5BR-060%5D%20Collaborative%20Git%20Recovery%20and%20Remote%20Blocked%20Snapshots.md), [R-062](requirements/archive/%5BR-062%5D%20Local%20Environment%20Recovery%20Command.md), [R-078](requirements/archive/%5BR-078%5D%20Concurrent%20Local%20Recovery%20Requests.md) |
| Merge 대기 PR이 없는 due 리팩터링의 우선 처리 | [R-054](requirements/archive/%5BR-054%5D%20Due%20Refactoring%20Priority.md) |
| 이벤트 시점의 진행·사용자 조치 알림과 무음 구분 | [R-053](requirements/%5BR-053%5D%20%EC%9D%B4%EB%B2%A4%ED%8A%B8%20%EC%8B%9C%EC%A0%90%EC%97%90%20%EB%A7%9E%EC%B6%98%20%EC%A7%84%ED%96%89%20%EC%83%81%ED%99%A9%20%EC%95%8C%EB%A6%BC.md) |
| 테스트별 시간·실제 Agent 없는 테스트·안전한 Python 캐시 | [R-058](requirements/archive/%5BR-058%5D%20Test%20Timing%20and%20Agent-Free%20Test%20Execution.md), [R-059](requirements/%5BR-059%5D%20%EC%95%88%EC%A0%84%ED%95%9C%20Python%20bytecode%20%EC%BA%90%EC%8B%9C%20%EC%82%AC%EC%9A%A9.md) |
| 규칙 기반 작업의 Manager 처리·모델 호출과 입출력 감소 | [R-063](requirements/archive/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md) |
| 전체 문서 보고의 조기 검사·동일 후보의 교정 반복 제한 | [R-087](requirements/%5BR-087%5D%20문서%20보고%20교정%20반복%20제한.md) |
| Requirement 중복 ID·제목 선택과 식별 정보 교정 | [R-071](requirements/%5BR-071%5D%20%EB%82%A8%EC%9D%80%20%EA%B7%9C%EC%B9%99%20%EA%B8%B0%EB%B0%98%20%EC%9E%91%EC%97%85%EC%9D%98%20Manager%20%EC%B2%98%EB%A6%AC.md) |
| 실제 검증 입력만의 목록·문서 원본을 고정 샘플로 대체·일반 verify와 후보 성공 결과 재사용 | [R-072](requirements/%5BR-072%5D%20%EC%9E%85%EB%A0%A5%20%EB%8F%99%EC%9D%BC%EC%84%B1%EC%9D%B4%20%EC%A6%9D%EB%AA%85%EB%90%9C%20%EA%B5%AC%ED%98%84%20%ED%9B%84%EB%B3%B4%20%EC%84%B1%EA%B3%B5%20%EA%B2%B0%EA%B3%BC%20%EC%9E%AC%EC%82%AC%EC%9A%A9.md) |

R-055의 Git 접근 권한 변경은 sandbox 강제, 사후 Git 차이의 접근 위반 판정 제거와 기존 오탐 차단 자동 복구로 반영한다. 원격 원본 전진의 rebase·재검증과 원격 작업 브랜치의 추가 작업 통합·이번 게시 OID에 결합한 조건부 force push는 각각의 동기화·게시 경계에서 처리한다. R-055의 계획·작업 삭제 자동 반영 등 나머지 목표는 해당 활성 Requirement에서 구분하며, 이 Git 변경만으로 전체 Requirement 완료를 선언하지 않는다. R-056은 dirty FF-only·clean rebase, R-082는 단계별 원격 문서 갱신을 담당한다.

R-023의 원격 실행·self handoff 목표는 유지하며 로컬 갱신 gate는 R-056, 역할별 파일 권한은 R-061을 따른다. R-055의 planning 오류와 R-061의 유효한 draft 사용자 판단 대기는 구분한다. draft는 planning 오류가 아니며 독립 작업을 막지 않지만 프로젝트 완료와 관련 Requirement archive를 허용하는 완료 상태도 아니다.

## Product Flows

### 제품 문서 작성 지원

Specification Skill은 사용자가 최초 제품·설계 기준과 이후 변경 요구를 작성·검토하는 독립 도구다. Loop Manager 설치·프로젝트 등록·자동 실행 없이 사용할 수 있으며 문서 형식·식별·Summary 계약을 호환되게 유지하되 코드·실행 절차·권한은 각 도구가 별도로 관리한다.

최초 설계 이후 구현으로 바뀌는 장기 규칙은 Worker가 코드와 같은 PR에서 갱신하며, 최초 작성과 후속 갱신 모두 결론 우선·중복 정리·조건과 실패 의미 보존 기준을 적용한다.

시작 조건:
- 사용자가 PRODUCT, Requirement 또는 ARCHITECTURE 작성이나 검토를 요청한다
- 사용자가 새 기능·정책 변경을 요청하면 대화형 Agent가 관련 Requirement를 작성·갱신한다. 별도 문서 작성 요청은 필요하지 않다

흐름:
1. Specification Skill이 기존 문서와 템플릿을 확인한다
2. 확정된 내용과 사용자 결정이 필요한 내용을 구분한다
3. 문서 역할에 맞는 가장 좁은 범위만 작성하거나 수정한다. 관련 활성 Requirement가 요청과 Acceptance Criteria를 충분히 담으면 재사용하며 중복 생성하지 않는다. ExecPlan 생성·계획 작성은 매니저의 자동 계획 흐름이 담당한다
4. 정상·실패 흐름, 상태 전이, 권한, Product Invariant와 edge case를 점검한다
5. 구현자가 임의로 결정해야 하는 사항이 없을 때까지 사용자와 문서를 보완한다
6. 완성된 Requirement는 `docs/requirements/` 바로 아래에 저장한다

문서 수정본 결과가 `candidate`이면 제안한 UTF-8 문서 내용과 의미 보존 근거를 기존 수정본으로 저장한다. `uncertain`이면 비어 있지 않은 이유만 반환하며 수정본 후보나 사용자 결정을 만들지 않는다. 잘못된 형식은 실패로 남기고 기존 수정본과 결정 상태를 보존한다.

### 프로그램 설정

시작 조건:
- 설정 파일이 없거나 사용자가 설정 모드로 실행한다

흐름:
1. 프로젝트별로 사용할 AI Agent provider를 선택한다 (기본값은 Codex)
2. `add`로 대상 프로젝트를 등록하거나 `remove`로 등록 해제한다
3. 프로젝트의 필수 조건과 공통 파일을 코드로 검증한다
4. 원격 필수 문서를 확인한다
5. `max_workers`를 포함한 유효한 설정을 저장한다

규칙:
- `config init`은 상태 DB를 초기화한다. 등록 목록은 현재 `config list`로 조회하며, 등록·해제는 최상위 `add`·`remove`를 사용한다. `config add`·`config remove` 호환 별칭은 없다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 PLAN_BLOCKED ExecPlan, PROJECT_BLOCKED 레코드가 있거나 해결되지 않은 `planning_error`가 있는 프로젝트는 등록 해제할 수 없다
- 공급자 호출·인증·모델 조회와 격리 검증은 선택된 상태 루트의 `automation-codex`·`automation-claude`를 공급자별 공통 홈으로 직접 사용한다. 호출별 임시 폴더와 작업 권한은 별도로 관리한다. 외부 공급자 홈 지정과 부모 환경을 변경하지 않으며 새 전용 인증 파일은 기본 사용자 인증 파일만 심볼릭 링크로 공유한다. 기존 전용 파일은 보존하고 인증·링크 준비 실패는 해당 공급자의 오류로 처리한다. 개인 설정 전체를 가져오거나 기본 홈으로 fallback하지 않는다
- Telegram token과 chat ID가 모두 비어 있으면 알림 기능을 비활성화하고 전송을 시도하지 않는다
- Telegram token과 chat ID 중 하나만 설정돼 있으면 설정 오류로 처리한다

실패:
- 선택한 AI Agent가 실행 불가하거나 인증돼있지 않다
- Model, Effort, 일부만 입력된 Telegram 설정 또는 `max_workers` 설정이 유효하지 않다
- 프로젝트가 등록 조건을 만족하지 않는다
- `docs/PLANS.md`가 Loop Manager 기준 파일과 일치하지 않는다

### ExecPlan 생성과 자동 처리

시작 조건은 미충족 PRODUCT·Requirement 또는 이어서 처리할 기존 작업이 있는 경우다.

1. 저장된 작업과 대기 PR을 확인한다. 안전 조건이 허용한 Merge·Close 반영을 먼저 수행하고, 최신 원격 입력과 catalog를 확인한다.
2. 기존 변경 작업과 확정 PR 충돌 복구를 우선한다. 유지보수·일반 Planner·READY 선택은 아래 작업 선택 정책을 따른다.
3. Planner가 현재 명세·코드·compact catalog와 허용된 PR 전 계획 snapshot을 확인한다. 새 작업을 계획하고 허용된 기존 계획만 조정한다. 일반·유지보수 Planner 작성과 충돌 교정 writer가 끝나면 다음 검증·교정·게시 전에 안전한 경계에서 원격 입력을 다시 확인한다. 관련 명세나 ExecPlan이 바뀌면 같은 계획 ID·작업물·session을 보존해 최신 입력으로 다시 평가하고, 무관한 문서 변경은 이미 끝난 결과와 단계를 유지한다.
4. 프로그램이 범위·형식·source·의존성을 검증하고 계획을 원격 base에 게시한다. 원격 게시를 확인한 결과만 실행 대상으로 반영한다.
5. dependency를 충족한 READY 하나를 선택해 전용 branch·worktree에서 Worker를 실행한다. Worker는 구현·관련 테스트와 verify 스크립트를 준비하며 Requirement를 이동하지 않는다.
6. 프로그램이 Worker 결과를 검증하고 최신 base를 반영한다. 최종 후보의 전체 검증과 읽기 전용 Review를 수행하며 실패는 같은 작업에서 교정한다.
7. 게시 직전 base나 후보 근거가 바뀌었으면 필요한 교정·후보 확인·검증·Review를 다시 수행한 뒤 기존 PR을 복구하거나 새 PR을 생성한다.
8. 안정된 PR_OPEN은 실행 자리를 비운다. 해당 계획에 직접·간접 의존하지 않는 READY는 순차적으로 시작할 수 있고, 연결 PR의 Merge 확인 뒤 의존 작업이 시작된다.

게시 전 Manager는 새 계획 파일만 활성·archive·등록·취소·예약 ID를 피하는 연속 ID와 일치하는 경로로 정규화한다. 새 계획과 이번 batch에서 수정한 snapshot 계획의 `depends_on` 중 대상이 명확한 batch 참조만 함께 바꾸며 기존 계획 identity·본문·dependency 순서, 사용자 파일과 draft 질문·User Decision은 보존한다. 재배정되는 ID가 본문에 나타나거나 여러 새 목표가 같은 ID를 쓰거나 dependency가 기존 ID와 충돌 ID를 함께 가리키면 자동 선택하지 않고 Planner 또는 사용자 판단 절차에 남긴다.

계획의 `draft: true`는 사용자 판단이 필요한 작성 준비도 선언이다. 유효한 draft는 독립 실행 가능 계획과 같은 batch로 게시·등록하며 질문·선택 영향·원격 링크·판단 revision을 보존한다. draft에는 Worker·workspace 예약·구현 branch·PR을 시작하지 않고, 직접·간접 의존 계획도 대기한다. 독립 READY·planning·PR 관측은 계속한다. 같은 근거와 질문을 새 ID로 반복 게시하지 않는다.

사용자는 원격 Requirement/ExecPlan 본문 또는 기존 User Decision의 Decision·Answer로 질문에 답한다. 현재 질문·근거와 답변을 유일하게 연결할 수 있으면 Revision 생략과 draft 표시 유지를 허용한다. Planner는 이전 질문과 사용자 변경을 비교해 답변의 의미·충분성과 남은 질문을 판단하고 선택을 본문에 통합한다. 사용자 변경 근거는 문맥 diff와 출처·revision으로 전달하며, catalog에 변경 전후 문서 전문이나 같은 근거를 반복하지 않는다. Manager는 저장된 원문과 변경 출처·원격 revision·현재 근거·무소유 상태를 검증하고 해결된 질문·입력 양식만 정리한다. 전부 해소되면 draft를 해제하며, 원격 게시 확인과 같은 DB transaction에서 대기를 해제하고 사용자 입력·결정 근거를 감사로 보존한다. 일부 해소는 남은 질문·draft와 미소비 사용자 입력을 유지한다. 답변을 질문별 인용 줄로 분리할 수 있을 때만 해당 줄을 소비하며, 분리할 수 없는 공유 답변은 보존한다. 거절·보류·불충분·상충·출처 불확실은 임의 승인하지 않는다. Agent의 추론·수정과 표시만 제거한 변경은 사용자 결정이 아니며 기존 dependency·소유권 gate도 유지한다. 문서 수정 draft 승인은 별도 계약이다.

양식 정리 뒤 동일한 목표·source 입력의 재관측은 기존 결정을 유지한다. 진행 기록·Decision Log 변경은 재승인을 요구하지 않는다. 목표·source 등 판단 근거가 바뀐 미착수·무소유 계획은 실행을 보류하고 Planner가 보존된 결정의 유효성을 재검토한다. 기존 선택이 유효하면 이유를 본문 감사에 남기고 원격 게시 확인 뒤 대기를 해제하며, 새 사용자 판단이 필요하면 해당 계획에 필요한 새 질문을 남긴다. 유효성 재검토 자체를 과거 질문의 사용자 알림으로 보내지 않는다. 자동 재평가는 프로젝트별 사용자 질문 목록과 건수에서도 제외하며, 함께 대기 중인 다른 실제 질문의 유효한 목록은 유지한다. 여러 draft가 공유하는 동일 사용자 변경은 Planner 입력에 한 번만 전달하고 각 질문·출처의 연결과 저장된 원문은 유지한다.

Requirement의 추가·변경은 실제 미충족 범위와 관련 기존 작업을 비교해 새 작업 또는 허용된 개정으로 반영한다. 실제 source 삭제는 archive·rename·조회 실패와 구별하고 Planner가 전체 철회·공유 목표 유지·불확실을 판단한다. 공유 작업은 남은 목표·검증·참조를 보존하고 필요한 선행 목표를 단순 dependency 제거로 우회하지 않는다. 삭제된 ExecPlan 목표의 복구 판단은 해당 snapshot 계획의 기존 직접·간접 dependency 경로에 남은 보류를 이유로 거부하지 않으며, 그 관계를 보존하고 삭제 목표별 판단을 검증한 뒤 게시할 수 있다. 새로 추가한 보류 dependency와 복구 대상 밖 계획은 계속 거부하며, 게시 뒤에도 선행 계획과 그 의존 범위의 실행 보류를 유지한다. 검증된 작업 취소는 활성 DB row 제거·ID 재사용 방지·원문과 실행 이력 보존을 함께 적용한다. 취소·예약 row가 없어도 확인 가능한 삭제 revision·identity·원문과 현재 참조 영향을 감사에 보존하고, 확인할 수 없는 근거는 미확인으로 남긴다. 실행 row 없이 등록 전 검증 실패로 hold만 남은 ExecPlan도 원격 삭제 시 저장된 원문·hold revision과 최초 관측 revision, 미확인 사유를 보존하며, 후속 참조가 나타나면 그 영향과 함께 Planner 복구 목표로 전달한다. 과거 archive source 부재는 현재 선언의 source 오류나 독립 작업의 차단이 되지 않으며, 검증된 MERGED 이력은 실행 row와 문서가 없어도 dependency 충족으로 유지한다. 과거 ID가 활성 경로에 다시 나타나면 예약 충돌로 거부한다. 진행 중 writer·PR·소유권이 불확실한 작업은 보류하며 독립 작업은 계속한다. 이미 Merge된 기능의 되돌림은 새 변경 요구로 다룬다.

작성 기준의 원본은 Requirement의 Specification Skill과 ExecPlan의 [PLANS.md](PLANS.md#execplan-본문-작성)다. 실제 조사한 파일·작업 체크리스트와 작업 고유 테스트를 제시하고, 정상·실패·완료 조건을 보존하며 기존 문서를 양식만으로 일괄 재작성하지 않는다.

계획 게시의 원자 적용, 작업 공간 복구와 단계별 Git 처리는 [ARCHITECTURE.md](ARCHITECTURE.md#계획-게시와-작업-공간)를 따른다.

### 실패와 재시도

Planner 결과 검증 실패:
최초 작성·교정 writer가 끝난 직후 최신 원격 명세·ExecPlan을 반영한다. 관련 입력이 바뀌었다면 이전 입력의 결과를 검증·게시하거나 그 결과의 형식·내용 실패로 새 입력의 교정 횟수와 `planning_error`를 먼저 확정하지 않는다. 이전 결과의 교정 횟수는 새 입력에 합산하지 않으며, 그 결과와 진단은 보존하고 독립 실패 원인과 실행 이력도 유지한다.
1. 변경 범위 또는 ExecPlan 형식 오류를 같은 Planner 세션에 전달한다
2. 최대 3회까지 수정과 재검증을 수행한다
3. 계속 실패하면 계획 입력 지문과 원인을 `planning_error`에 저장하고 planning worktree를 보존한다
4. 같은 계획 입력 지문에서는 Planner를 다시 실행하지 않는다
5. 원격 입력이 전진하면 이전 planning worktree·검증 기록·세션을 보존해 반영한다. 오류 원인이 실제로 해소됐는지 재검증하기 전에는 `planning_error`를 지우지 않는다

ExecPlan 게시 실패:
1. 계획 commit을 push하기 전에 `origin/<base-branch>`가 전진하면 미게시 후보를 보존해 관리 worktree에서 rebase하고 최신 catalog·SQLite 권한과 결과를 재검증한다
2. fast-forward push가 거부되면 계획 commit과 오류를 보존하고 다음 실행에서 다시 시도한다
3. push 성공을 원격에서 확인하기 전에는 ExecPlan을 READY로 등록하거나 Worker를 시작하지 않는다
4. 일반·유지보수 batch의 prepared intent와 planning worktree는 push·SQLite 적용 실패 뒤에도 보존한다. 원격에 batch가 있으면 원자 적용을 재개하고, 없으면 보존 후보를 최신 base로 옮겨 재검증한다

Worker targeted unit test 실패:
- 같은 작업의 동일 테스트가 실제 실행에서 연속 3회 실패하면 해당 ExecPlan을 차단한다. Worker 완료 보고의 교정 횟수는 이 기준에 사용하지 않는다.
- 테스트 ID별로 집계하며 다른 테스트의 실행은 서로의 횟수에 영향을 주지 않는다. 해당 테스트의 성공 또는 skip·예상된 실패는 연속 실패를 초기화한다. 후보 코드·commit·Agent 세션 변화만으로 횟수를 초기화하지 않는다.
- 같은 실행 결과는 한 번만 세고 Worker 세션 내부 재실행·CLI 재시도·Manager 재시작 뒤에도 기록을 보존한다. 실행·수집 중단과 근거 부재는 실패 횟수에 넣지 않는다. 실패 보고에 새로운 실행 근거가 없으면 상태를 보존해 이번 실행을 일시 중단하며 다음 실행에서 실제 재검증한다.
- 테스트 실패 외의 문서·완료 보고 오류는 기존 교정 한도를 유지한다. 전체 verify의 실패 지문 및 Review 정책도 별도로 유지한다.

전체 검증 또는 Review 실패:
1. 프로그램이 전체 검증 실패 내용 또는 Reviewer의 blocking issue를 같은 Worker 세션에 전달하고 Reviewer도 저장된 세션을 재사용한다
2. 전체 검증 성공 출력은 Worker에게 전달하지 않는다
3. 전체 검증과 Review 출력을 결정적으로 정규화해 단계별 실패 지문과 발생 횟수를 SQLite에 저장한다
4. 전체 verify의 실패 지문 이력은 Review와 독립적으로 유지한다. 새로운 verify 지문이 나타나는 동안에는 총 실패 횟수와 관계없이 Worker 수정과 전체 검증을 계속한다
5. 같은 verify 지문이 Worker 수정 후 세 번 다시 발생하면 구조화된 원인과 지문을 기록하고 ExecPlan을 `PLAN_BLOCKED`로 변경하며, 중간에 다른 지문이 발생해도 이전 이력을 유지한다. Review의 ISSUE_ID와 진단·해결 이력은 피드백과 감사에 보존하되 발생 횟수만으로 최종 차단하지 않는다
6. 한 외부 실행에서 계획별 Review FAIL에 따른 Worker 교정은 최대 세 번 시작한다. 복수 issue는 한 교정으로 세며 CLI 재시도와 중단된 동일 turn의 복구는 중복 집계하지 않는다. 세 번째 교정의 verify·Review가 성공하면 게시를 계속하고, 추가 교정이 필요하면 Worker 호출 전에 phase·작업물·세션·피드백을 보존해 이번 실행을 일시 중단한다. 다음 외부 실행은 저장된 피드백과 세션으로 재개하며 이 중단은 BLOCKED·checkpoint·차단 알림을 만들지 않는다
7. 각 effective level의 첫 유효 Review FAIL은 기준선이며 교정 결과에 포함하지 않는다. 이후 Review 피드백에 대한 Worker 교정 완료·Manager 검증 통과·유효 Review FAIL이 결합된 결과만 집계한다. Manager 검증은 기본 경로의 전체 verify이며, 일반 선행 경로에서는 현재 후보의 부분 verify다. 부분 후보 연결은 전체 검증 완료 증거와 구분한다. 기준선의 같은 ISSUE_ID가 두 교정 결과에 다시 나타나거나, issue가 달라도 교정 결과가 세 번 연속 FAIL이면 다음 Worker 호출부터 한 단계 올리며 level 8을 상한으로 한다. 복수 issue는 한 교정 결과로 센다. 새 level에서는 기준선을 다시 시작한다
8. 교정 없는 Review 재실행, CLI·Reviewer 프로토콜 재시도, 중단된 동일 Worker turn 재개는 교정 결과를 늘리지 않는다. 전체 verify 실패, Worker 결과 형식 오류, Reviewer 실행 오류와 유효하지 않은 Review는 승격 집계에 포함하지 않고 진행 중인 집계도 초기화하지 않는다. 기준선·미완료 교정·검증 후보 identity와 effective level은 SQLite에 보존해 재시작과 같은 결과 재관측에서 중복 승격하지 않는다
9. 전체 검증 성공은 verify 지문 이력만 초기화한다. 유효 Review PASS는 Review 지문 이력과 진행 중인 교정 집계를 끝내며 effective level과 과거 level별 승격·실패 감사는 유지한다

Worker·Reviewer·Planner·모듈 검토 결과의 명확한 marker 배치·구분자·공백 차이는 Manager가 처리한다. 검증된 호출 입력의 identity만 결합하며 형식 처리만으로 추가 모델 호출·실패 집계·level 승격을 만들지 않는다. 상충 verdict·명시적 identity 불일치와 필수 판단·issue·원본 인용·검증 근거 누락은 기존 역할의 교정 대상으로 남긴다. 빈 원본·공백 chunk의 확정 사실은 재사용하되 내용이 있는 모듈의 의미 검토와 불확실한 관계의 보수적 처리를 유지한다.

과거 `review_retry_exhausted` 차단은 저장된 원인과 현재 작업 공간·branch·원격 OID·후보 유효성·PR·rebase 상태를 재확인한다. Review 횟수 외 유효한 차단이 없고 소유권이 확인되면 기존 세션·실패 피드백·감사를 보존한 채 같은 교정 경로로 재개한다. 안전 근거가 부족하거나 별도 차단 원인이 있으면 그 상태를 유지한다.

PR 없는 게시 checkpoint의 과거 Review 횟수 차단은 현재 repository·origin·작업·branch·worktree와 실제 후보를 확인해 해당 원인만 해제한다. 과거 게시 이력의 부재나 OID 차이를 읽기·통합의 거부 근거로 사용하지 않는다. 수행하지 않은 rebase·게시·검증의 성공을 만들지 않고 남은 Review issue와 독립 차단을 보존한다.

게시 OID가 남았지만 재개 표식이 없는 작업도 동일 계획·branch·관리 worktree와 현재 게시 identity·tree·계보·실제 원격 lease·PR 상태를 재검증해 안전하게 재개한다. 현재 checkpoint 요청이 local-only여도 충분한 과거 게시 영수증은 읽기 전용으로 사용할 수 있다. 새 원인의 차단은 낡은 재개 표식을 무효화하되 게시 이력을 보존한다. 게시 소유권 오탐만 해제하며 미해결 Worker 실패와 별도 차단·승인·Agent 위반은 유지한다. OID 삭제나 로컬·원격 HEAD 일치만으로 검사를 우회하지 않는다.

외부 CLI 실패:
- 저장된 Agent 세션을 찾지 못하거나 재개할 수 없으면 같은 작업 공간과 작업 문맥을 보존해 새 세션으로 한 번 이어간다. 승인·격리 정책 실패와 AI 사용 한도 소진은 새 세션으로 우회하지 않는다
- 실행 실패, timeout 또는 일시적 외부 오류는 현재 프로그램 실행에서 최대 3회까지 재시도한다
- 3회 재시도 후에도 실패하면 상태와 작업 공간을 보존하고 해당 프로젝트 처리를 이번 실행에서 중단한다
- 다음 Loop Manager 실행에서 같은 상태부터 다시 시도한다
- AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않고 즉시 다음 프로젝트로 넘어간다

### 계획 및 프로젝트 차단 해제

| 차단·변경 조건 | 허용되는 처리 |
|---|---|
| PLAN_BLOCKED | 해당 계획의 입력·사용자 결정·같은 PR reopen 조건만 재평가한다. 독립 READY는 계속 선택할 수 있다. |
| PROJECT_BLOCKED | 일반 Agent·Git 변경을 멈춘다. PR 상태·계획별 원격 관측·알림은 계속하며, 실행 자리와 현재 범위가 검증된 연결 계획만 복구·보존할 수 있다. |
| 안전한 원격 후속 commit 또는 base 전진 | 기존 branch·worktree·session과 로컬 작업을 보존해 반영하고 원래 차단 원인을 다시 검사한다. 새 OID나 checkpoint 게시만으로 해제하지 않는다. |
| 삭제·재작성·경쟁·소유권 불명확 또는 미해결 충돌 | 원격 ref를 덮지 않고 차단·작업물과 실제 미게시 이유를 보존한다. |

1. 최종 차단 상태와 원인을 먼저 보존한다. 검증 가능한 대상 작업은 안전한 원격 checkpoint 또는 별도 진단 branch에 보존하고 결과를 알린다. 기존 PR은 저장된 URL·Open 상태·작업 branch·정확한 원격 head를 재확인해 같은 branch에 게시하며 새 PR·Draft PR을 만들지 않는다.
2. 저장된 원인과 현재 입력·작업 상태를 대조한다. 같은 입력의 실패를 반복하지 않으며 다른 계획·planning·사용자 결정의 차단을 함께 해제하지 않는다.
3. 일반적인 입력·승인·PR reopen 해제는 해당 계획의 재시도·실패 지문과 Worker level을 초기화해 READY 또는 PR_OPEN으로 되돌린다. 원격 복구는 기존 session·실패·승격 감사를 보존한다.
4. 후보가 바뀐 작업은 유효한 Worker·targeted test·전체 verify·Review 근거를 다시 확인한다. 아래의 충돌 없는 rebase·동일 verify 입력 예외 외에는 필요한 단계를 다시 수행한다. 첫 PR도 최종 검증과 Review 성공 뒤에만 생성한다.

작업 branch의 현재 입력 수용과 게시 완료 확인은 별도 판단이다. 로컬·원격만의 commit과 미커밋 작업을 보존해 통합하고, 같은 PR의 head 전진은 새 원격 입력으로 처리한다. 과거 게시 영수증은 미완료 push 확인과 감사에 사용하며 수용 허가증으로 요구하지 않는다. 원격·PR 관측 경쟁과 정확한 lease 거부는 제한 재관측·통합·재검증으로 돌아가며 독립 작업은 계속한다. 실제 대상·권한·진행 중 writer의 문제는 해당 작업의 동작을 보류한다.

Manager가 생성·게시한 원격 작업 branch가 PR Merge 전에 실제 삭제되면 해당 작업을 중단하고 명시적 재시작을 기다린다. 조회 실패와 PR Merge 후 정리는 삭제 중단과 구분한다. 일반 run·프로세스 재시작·pause 해제·같은 ref 재생성으로 재개하지 않는다. `reset <repo> <id>`는 삭제로 중단된 자동 작업에 한해 이전 worktree·session·PR·실패·게시 감사를 보존하고 최신 원본의 새 실행을 READY로 준비한다. 이전 성공 근거와 게시 intent는 새 실행에 재사용하지 않는다.

반복 교정으로 해결하지 못한 충돌은 확인한 원본 revision에서 해당 파일과 index entry만 복원하고 재작업한다. 다른 파일의 내용·staging·미추적 작업과 현재 rebase target을 유지한다. 복원 전 확보할 수 있는 원형과 실패 사유를 보존하고, 중단 뒤 이미 복원한 파일이나 새 교정을 덮어쓰지 않는다. 후보 OID 변경은 필요한 verify·Review를 선택하는 입력이며 실제 명세 변경·구현 부족만 Worker 교정으로 연결한다. 진단 ref 충돌은 해당 자료 게시·삭제만 보류한다.

현재 상태 기반 rebase 판정, checkpoint lease, 진단 ref와 중단 복구의 상세 조건은 [ARCHITECTURE.md](ARCHITECTURE.md#차단-작업의-원격-반영과-보존)를 따른다.

### 사용자 승인 및 완료

1. 사용자가 PR을 검토하고 Merge한다. Merge 없이 닫힌 PR은 해당 계획만 차단하며 같은 PR을 다시 열어야 한다.
2. 프로그램이 실제 Merge를 확인해 MERGED와 알림을 기록한다. 관리 worktree는 아래 정리 조건을 만족할 때만 제거한다.
3. 최신 원격 base에서 안전한 MERGED ExecPlan을 내용 보존 archive로 게시한다. 정리 실패는 작업 완료를 되돌리거나 다음 작업을 막지 않는다.
4. 다음 의존 작업을 시작하거나 Planner의 추가 작업 없음과 프로젝트 완료 gate를 확인한다.
5. 모든 완료 조건이 충족되면 프로젝트 완료 이벤트를 전송한다. 일반 planning 게시로 원격 확인된 Requirement 이동만 활성 입력에서 제외된다.

### 실행 종료 및 재개

1. 같은 사용자 상태 디렉터리의 다른 Loop Manager가 실행 중이면 상태를 바꾸지 않고 종료한다
2. 각 프로젝트의 PR, worktree와 저장된 실행 상태를 외부 상태와 대조한다
3. 기본 worktree의 dirty, staged, untracked, ahead, diverged, detached 또는 다른 branch 상태는 원격 SHA 기반 작업을 막지 않는다. 안전한 fast-forward·로컬 commit 보존 rebase 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
4. 프로젝트별로 하나의 작업만 선택하고 전체 동시 실행 수를 `max_workers`로 제한한다
5. 처리 중인 상태, 세션과 작업 공간을 SQLite에 저장한다
6. 중단된 작업은 다음 실행에서 저장된 상태부터 재개한다

관리 Agent의 실제 호출은 사용할 provider 환경에서 인증·모델·설정·권한을 확인한다. 등록 사전 조회의 성공만으로 실제 실행 환경의 성공을 인정하지 않으며, 부모 환경과 사용자 인증 원본을 변경하지 않는다. 호출 승인 비교에 필요한 증거는 해당 실제 호출의 지침·환경·정책과 완료 사실에 결합한다. 확인되지 않은 현재 환경이나 과거 호출은 재사용하지 않는다.

## States and Transitions

ExecPlan 실행 상태는 파일이 아니라 SQLite에만 저장한다.

```text
READY → IMPLEMENTING → VERIFYING → REVIEWING → PR_OPEN → MERGED
VERIFYING 또는 REVIEWING → IMPLEMENTING (재시도)
READY, IMPLEMENTING, VERIFYING 또는 REVIEWING → PLAN_BLOCKED
PLAN_BLOCKED → READY (계획별 해제 조건 충족)
PR_OPEN → PLAN_BLOCKED (Merge 없이 PR 종료)
PLAN_BLOCKED → PR_OPEN (같은 PR을 사용자가 다시 열음)
```

- READY: 유효한 ExecPlan이 등록돼 프로젝트 실행 순서와 `depends_on` 충족을 대기
- IMPLEMENTING: Worker가 구현 또는 실패 수정을 진행 중
- VERIFYING: 전체 검증 스크립트 실행 중
- REVIEWING: Reviewer가 blocking issue를 확인 중
- PR_OPEN: PR 생성 후 사용자 Merge 대기
- MERGED: 연결된 PR의 Merge가 확인된 최종 상태
- PLAN_BLOCKED: 특정 계획의 사용자 결정, 입력 변경, 검증 한도 또는 PR reopen을 기다림

`PROJECT_BLOCKED`는 ExecPlan 상태가 아니라 프로젝트 안전 gate다. 현재 단계와 작업 공간을 보존하고 일반 Planner·유지보수 Planner·Worker·Git 변경을 멈춘다. PR 상태·원격 관측·알림과 검증 가능한 연결 계획의 복구·보존 예외는 위 차단 정책을 따른다.

유지보수 cycle은 ExecPlan 상태와 별도로 프로젝트별 SQLite에 보존한다.

```text
IDLE → ASSESSMENT → IDLE (확정 계획 없음)
ASSESSMENT → PREREQUISITE_WAIT → IDLE (확정 범위 Merge)
ASSESSMENT → REFACTORING → IDLE (확정 범위 Merge)
```

활성 작업은 cycle 시작보다 우선하며, 각 cycle 전이는 기록된 계획 집합의 Merge 또는 검증된 Planner 결과로만 진행한다. 기존 `POST_REFACTOR_REVALIDATION`은 저장된 결과·게시를 복구하고 확정 범위의 Merge를 확인한 뒤 새 평가 없이 종료한다.

외부 CLI의 일시적 실패와 사용 한도 소진은 별도 ExecPlan 상태를 만들지 않고 현재 상태를 보존한다.

`planning_error`, `cleanup_pending`과 ExecPlan archive pending은 ExecPlan 상태가 아니다. `planning_error`는 해당 프로젝트의 새 계획과 완료 처리를 막지만, `cleanup_pending`과 archive pending은 이미 Merge된 작업의 보존·재시도 정보이므로 다음 ExecPlan 실행과 프로젝트 완료를 막지 않는다.

## Business Rules

### PRODUCT, Requirement와 ExecPlan

- `PRODUCT.md`는 현재 Merge된 제품 동작, 경계와 장기 규칙의 기준이다
- 새 Requirement는 원하는 변화·이유·Acceptance Criteria만 기록하며 frontmatter·상태·complexity·risk를 두지 않는다
- 새 Requirement를 만들기 직전에 Specification Skill은 프로젝트 루트의 `python .agents/skills/specification-docs/scripts/next_requirement_id.py`를 실행해 출력된 ID를 사용한다. 조회는 활성·archive 문서의 원본 ID 중 최댓값 다음 후보를 반환하며, 중복이나 번호 공백은 허용하고 중간 번호를 채우지 않는다. 파일을 수정하거나 번호를 예약하지 않는다. 빈 집합은 R-001이고 archive 디렉터리 부재는 빈 집합이다. 조회 오류가 있으면 유효한 ID 없이 실패한다
- 신규 문서는 frontmatter 없이 작성한다. 기존 문서의 ID·제목 조회는 유효한 `id`·`title` frontmatter를 우선하고, frontmatter가 없으면 첫 `# [R-ID] 제목`을 사용한다. 잘못되거나 불완전한 frontmatter는 파일명·본문으로 대체하지 않으며 기존 문서에 frontmatter를 일괄 추가하지 않는다
- 기존 ExecPlan의 Requirement source 문자열은 rename 뒤에도 바꾸지 않는다. 기록된 활성 경로를 먼저 사용하고, 없으면 기존 작업에 한해 같은 파일명의 archive, source 파일명에 적힌 R ID의 유일한 원본 ID 문서 순으로 조회한다. 정확한 경로가 있으면 ID가 중복돼도 그 문서를 유지하며 후보가 없거나 여러 개이거나 조회가 실패하면 source 관계를 확정하지 않는다
- Manager는 실행·planning·유지보수가 원격 쓰기를 점유하지 않는 안전한 경계에서 최신 base의 활성 Requirement 중복 ID·제목·파일명과 수정 가능한 문서의 확정 상대 링크를 모델 없이 교정하고 같은 commit으로 fast-forward 게시한다. 활성·보관 작업 문서와 저장 관계의 참조를 함께 확인하며 archive·ExecPlan·sources·User Decision은 수정하지 않는다. 수동 소유·문서 draft·종료 불명 writer와 기존 게시 예약은 보존한다. 교정 시작 전에 안전 gate가 닫혀 있거나 수동 작업이 Requirement source를 소유하면 교정을 보류하고 기존 수동 작업과 독립 실행을 계속한다
- 교정 전 원문·번호 배정·결과·소유 intent를 보존하고 게시 직전 base·참조·목적지·소유권·허용 diff를 다시 검사한다. 입력 경쟁에서는 이전 후보를 보존하고 최신 입력으로 다시 계산한다. 중단·push 실패에서는 실제 원격 결과를 확인해 같은 후보·commit을 재사용하며 이미 게시된 결과를 다시 push하지 않는다. 조회 실패·변조·게시 결과 제거는 덮어쓰기나 Planner 호출·성공 기록으로 대체하지 않는다. 기존 작업·세션·실패·독립 차단과 후속 verify·Review gate를 유지한다
- 새 ExecPlan의 source와 Planner 입력은 활성 PRODUCT 또는 `docs/requirements/` 바로 아래 문서로 제한한다
- source로 참조하는 비종료 ExecPlan·draft가 있으면 해당 Requirement 전체 충족 확인과 archive를 미룬다. 마지막 연결 ExecPlan의 실제 Merge와 catalog 반영을 확인하면 다음 안전한 작업 선택 경계에서 일반 Planner가 해당 Requirement의 각 Acceptance Criterion을 최신 구현·테스트·실제 검증과 관련 제품·설계 문서에 대조한다. 다른 Requirement의 작업·PR 대기·종료나 프로젝트 전체 완료까지 미루지 않고 독립된 새 작업·유지보수 cycle 선택 전에 확인한다. 계획 없이 직접 구현한 변경도 필요한 명세 검토·최종 확인에서 실제 근거로 판단하며, 작업 작성·Worker 완료·검증 성공·PR 생성·관련 계획의 Merge만으로 전체 충족을 추정하지 않는다
- Manager는 Requirement 원문, 관련 명세·계획 관계, 구현·검증 근거에 결합된 판단을 보존한다. 동일한 유효 입력의 판단은 재사용하고 관련 입력이 바뀌면 해당 Requirement의 연결 작업·사용자 판단이 종료된 다음 안전한 경계에서 다시 확인한다. 충족은 기록·원문 archive로, 미충족은 후속 ExecPlan으로, 사용자 판단은 draft로, 불확실은 완료 보류와 재개 조건으로 반영한다. 같은 경계의 여러 Requirement·명세 변경·프로젝트 최종 확인은 한 호출로 처리할 수 있지만 목적별 근거를 구분하며 서로의 성공을 대신하지 않는다
- 유효한 최종 충족 판단과 source 관계·Merge 상태가 확인되면 Manager가 원문 그대로 일반 planning 게시에서 Requirement를 이동한다. 새 계획과 이동을 함께 게시하거나 이동만 게시할 수 있으며 원격 확인 전에는 활성 입력으로 유지한다. 충돌·불확실성은 덮어쓰기나 임의 완료 없이 보존한다
- archive된 Requirement는 완료한 변경의 보관이며 재평가·새 작업·프로젝트 완료 대기의 입력으로 사용하거나 자동 복원하지 않는다. 기존 ExecPlan의 source 조회·검증을 위한 읽기 호환성과 과거 감사 기록은 보존한다
- `PLAN_BLOCKED` 해제 지문은 원래 활성 Requirement 경로만 사용하고 archive 변경에는 반응하지 않는다
- Requirement는 PRODUCT의 변경 요청이며 ExecPlan의 구현 상세나 실행 상태를 포함하지 않는다
- 현재 활성 Requirement 경로는 Planner 입력과 판단·이동의 경로 key다. 기존 ExecPlan source는 기록된 경로를 보존하며 제목 변경 뒤 고유한 원본 R ID로 같은 문서를 연결한다
- `docs/requirements/` 바로 아래의 모든 `*.md`를 Planner 입력으로 사용한다
- 일반 planning에는 변경 여부와 관계없이 모든 활성 Requirement의 제목·경로와 비어 있지 않은 기존 Summary를 제공한다. 일반 계획을 함께 만드는 유지보수 호출에도 적용하며, 요약이 없으면 제목·경로만 제공한다. 요약은 탐색용이고 관련·불확실한 문서의 본문을 같은 원격 revision에서 확인한다. 요약을 생성하는 모델 호출이나 Planner의 Requirement 수정은 하지 않는다
- PRODUCT·ARCHITECTURE의 현재 기준에서 활성 Requirement가 명시적으로 바꾸는 범위에는 변경 목표를 적용하고 나머지 계약은 보존한다. 변경되지 않은 요구도 유효하며 이전 설계만으로 되돌리는 작업을 만들지 않는다. 활성 목표의 충돌·대체 범위가 모호하면 최근 수정 시점이나 ID로 결정하지 않고 사용자 판단을 남긴다. delta·전체·최종 검토에 같은 해석을 적용하며 기존 전체 검토 조건을 유지한다
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 Planner 입력에서 제외한다
- ExecPlan의 `sources`에는 근거가 된 PRODUCT 또는 Requirement 경로가 있어야 한다

- source와 dependency 변경은 같은 reconciliation 결과를 Requirement 판단 자격·입력 지문·READY 선택에 사용하고 priority 변경은 다음 선택부터 반영한다. complexity·risk 변경은 최초 Worker level을 다시 계산하되 실행 이력이 있는 계획의 effective level과 Review 승격 이력을 낮추지 않는다
- 현재 path와 title은 compact catalog, 로그·알림과 이후 파일 조회에 사용하지만 기존 PR의 ExecPlan 링크는 최초 게시 SHA와 최초 active path를 계속 사용한다
- 최초 또는 비교 가능한 전체 계획 성공 기준이 없거나 PRODUCT·활성 Requirement·ARCHITECTURE가 바뀌면 일반 Planner를 실행한다. draft 사용자 답변의 의미 판단, 새 ExecPlan이 필요한 명시적 수동 제출, 자동 주기·CLI 요청에 따른 유지보수, Requirement 완료 재확인과 프로젝트 최종 확인도 각각의 호출 목적을 유지한다. 조건과 재사용·통합 기준은 R-082의 Planner 실행 조건을 따른다. 일반 Planner는 비종료 compact catalog와 PR 전 계획 snapshot으로 현재 식별 가능한 미충족 작업을 한 번에 계획한다. 실행 이력이 있어도 PR 전이면 같은 ID로 개정하며 작업물·세션·실패·Worker level 이력을 보존한다. R-077의 수동 소유 계획과 start/submit 준비 intent 대상은 snapshot과 게시 직전 검사에서 보호하며, membership 변경은 영향을 받은 개정을 거부한다. 독립 계획은 계속 처리하고 실제 종료·대상 해제 뒤 남은 membership 이력만으로 소유권을 추정하지 않는다. PR 등록 후 Merge 대기 작업의 추가·변경 요구사항은 새 ExecPlan으로 작성한다
- 일반 Planner는 마지막으로 원격 적용한 성공 검토와 비교 가능한 경우 변경·삭제·rename과 현재 catalog부터 확인한다. 영향받은 명세 절·코드·테스트·계획과 dependency를 읽고 불확실하면 범위를 넓힌다. 비교 근거가 없거나 손상되면 활성 명세 전문을 확인하며, 매 변경 검토에서 무관한 문서 전문을 반복하지 않는다
- ExecPlan·코드·테스트·설정의 변경과 문서 수정본 승인 자체는 Planner 실행 조건이 아니다. 미착수 READY의 유무·dependency 대기 여부에 따라 점검 Planner·일반 Planner를 호출하지 않으며 점검 결과 인계도 제거한다. Manager가 선언·source·dependency·사용자 결정·실행 자격을 검증하고, Worker가 최신 ExecPlan·현재 코드로 구현 충족 여부와 필요한 보완·targeted test를 함께 판단한다. 승인된 수정본과 적용할 기존 작업이 확정되면 해당 Worker에 전달하며, 실제 상위 명세 변경은 별도 명세 변경 조건을 적용한다
- 중단·재시작·원격 관측에서는 유효한 결과와 미완료 게시·DB 적용을 먼저 재사용한다. 실제 입력 변경으로 적용 불가한 결과·필수 의미 판단 누락·계획 작성 범위의 충돌만 필요한 세션 교정으로 이어가며, 전체 재계획이나 이미 확정한 단계의 반복을 추가하지 않는다
- `docs/FUTURE_IDEAS.md`는 계획 입력 지문, 활성 Requirement와 프로젝트 완료 판정에서 제외하며 그 항목을 자동으로 Requirement 또는 ExecPlan으로 승격하지 않는다
- MERGED ExecPlan은 Planner 입력에서 제외하며 프로그램만 ID 고유성, 의존성 충족과 감사 이력을 위해 관리한다
- Planner와 Worker의 실행 후보는 `docs/plans/` 바로 아래의 active ExecPlan으로 제한하고, `docs/plans/archive/`는 ID 고유성, 다음 번호, dependency, 복구와 감사 검증에만 포함한다
- 로컬 DB에 필요한 작업 행이 없으면 활성·archive 문서로 복구한다. 활성 문서는 기본 READY 등록과 기존 실행 복구 규칙을 따르고, archive 문서는 추가 PR 조회나 과거 게시 본문 검증 없이 MERGED로 받아들인다. 문서의 형식·ID·source·dependency 검증과 기존 행의 실행·소유권·실패 이력은 보존하며, 양쪽에 문서가 없으면 누락으로 보고한다
- ExecPlan archive-only 이동은 계획 입력 지문, 유지보수 Merge count, 프로젝트 완료 cycle을 바꾸지 않는다
- ExecPlan의 `depends_on`은 선행 ExecPlan ID 목록이며, 모든 선행 상태가 MERGED여야 Worker를 시작할 수 있다
- Worker는 현재 작업과 관련된 테스트를 현재 명세·구현과 대조해 폐기된 동작만 검사하거나 동일 조건·assertion을 다른 테스트가 모두 보장하는 중복 테스트를 제거한다. 제거 근거와 남은 회귀 검증을 보고하며 실패·비용만으로 필수 검증을 약화하지 않는다. 유지보수 평가도 같은 기준으로 조사해 기존 비종료 계획 또는 새 제거 계획에 연결하고 Worker가 정리한다.
- Worker는 Requirement·ExecPlan 구현으로 바뀌는 제품 동작·장기 규칙을 `PRODUCT.md`에, 설계·책임 경계·계약을 `ARCHITECTURE.md`에 코드와 같은 PR에서 반영한다. 현재 작업에 필요한 관련 절만 갱신하며 목표·필수 검증·별도 사용자 결정을 유지한다
- Worker는 현재 작업에 필요한 PRODUCT·ARCHITECTURE·FUTURE_IDEAS·선택된 ExecPlan 본문·공통 및 역할별 지침·사용 안내를 갱신할 수 있다. ExecPlan frontmatter와 다른 ExecPlan은 수정하지 않는다. 활성·archive를 포함한 모든 Requirement는 추가·수정·삭제·rename·이동하지 않는다. 새로운 frontmatter 선언의 교정은 Planner·Manager가 검증하고 공식 선언과 DB에 반영한다. Worker 종료 후 정상 작업 경계에서 시작 선언·공식 원본·본문 경계를 확인할 수 있으면 Worker가 변경한 frontmatter만 공식 선언으로 복원하고 본문·기존 수정·실행 기록은 보존한다. 복원 사실은 Worker에게 전달해 현재 문서 보고를 갱신하며 검증 성공으로 취급하지 않는다. 확인 불가·동시 수정·진행 중 rebase는 기존 오류·복구 경계를 유지한다. 기존 source의 rename 조회 호환을 유지하며 선언 불일치를 Worker의 source 수정으로 우회하지 않는다. rebase로 원본 브랜치에서 유입된 다른 문서는 Worker의 범위 밖 수정으로 판단하지 않으며, 실제 범위 오류는 원본·후보 revision과 변경 경로를 함께 진단한다. 관련 수정에서는 원래 요구·Acceptance Criteria·필수 검증·실패·사용자 결정과 실행 이력을 보존한다. Requirement 완료 판단과 archive 이동은 일반 Planner·Manager의 책임이다
- PR 본문의 문서 보고는 파일명·변경 요약·이유·영향만 제공한다. 추가·삭제·rename을 포함하며 실제 변경 내용·patch·diff 링크·검증용 hash는 본문에 넣지 않는다. Manager는 Worker Summary와 실제 최종 diff/hash를 대조해 누락을 교정하고 rebase·PR head 변경 뒤 보고도 갱신한다. 문서별 이유·영향이 없거나 최종 diff와 결합되지 않은 보고는 해당 Worker의 교정으로 보완하며 오래된 일반 Summary나 patch만으로 대신하지 않는다. Reviewer는 읽기 전용으로 변경 전 목표·변경 후 문서·구현·보고를 검토한다

### 프로젝트 등록 및 실행 조건

- `add <repo> [--base <branch>] [--provider <provider>]`는 기존 프로젝트 검증·등록을 수행하고 `remove <repo>`는 기존 등록 해제 gate를 유지한다. `config init`과 `config list`는 유지하며 이전 `config add/remove` 이름은 허용하지 않는다
- `run [--skip-if-running] [repo...]`는 상대 경로를 CLI 호출 당시 작업 디렉터리 기준으로 절대화하고 중복을 제거한 뒤, 지정한 전체 경로가 등록됐는지 실행 전에 검증한다. 경로를 생략하면 전체 등록 범위를 처리하고, 하나라도 미등록이면 어떤 프로젝트도 처리하지 않는다. 공백이 포함된 경로는 인용해 지정한다
- 선택 실행은 기존 `max_workers`, 프로젝트별 단일 변경 자리, dependency·복구·self handoff와 실패 격리를 유지한다. 범위 밖 프로젝트는 처리하지 않는다
- 대상 경로는 origin이 GitHub 저장소인 Git 프로젝트여야 한다
- fetch된 `origin/<base-branch>` tree에 `docs/PLANS.md`, `docs/ARCHITECTURE.md`가 있어야 한다
- fetch된 `origin/<base-branch>` tree에 `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- 변경되지 않은 PRODUCT/ARCHITECTURE 템플릿만 있는 프로젝트는 작업 가능 상태로 등록하지 않는다
- `docs/PLANS.md`는 Loop Manager 기준 파일과 SHA-256 해시가 같아야 한다
- 자동 작업의 기준은 `origin/<base-branch>`다
- 기본 worktree의 로컬 변경, checkout branch와 ahead·diverged 이력은 자동 작업을 막지 않으며 local-only 내용은 자동 입력에 포함하지 않는다

- 기본 worktree는 equal이면 무변경, ahead이면 보존·skip, behind이면 disjoint 로컬 변경과 index·ignored 파일을 보존하는 safe fast-forward로 처리한다. clean diverged의 확인된 선형 로컬 commit은 durable intent 아래 원격 base 위로 rebase하며 자동 push하지 않는다. dirty 작업·merge commit·겹치는 ignored 경로·사용자 operation·소유권 불확실은 보존·skip한다. 시작 ignore 규칙으로 증명된 index-free 파일의 생성·수정·삭제만으로 정상 완료·실행 직전 검사·중단 복구를 거부하지 않는다. Git 쓰기 직전에는 현재 경로 충돌을 다시 확인하며 파일을 삭제·복원해 우회하지 않는다. 복구는 실제 ref·HEAD·index·권위 있는 파일·operation 전이와 소유 증거가 맞을 때만 완료하고 동일 입력의 실패 Git 쓰기를 반복하지 않는다. 과거 기록의 시작 규칙이나 소유 증거가 부족하면 기록과 사용자 파일을 보존한다.
- 자동 명령의 bootstrap은 process lock 아래 등록된 self base(미등록이면 `main`)의 원격 SHA를 확정하고 그 revision의 bootstrap·runtime을 같은 실행에서 시작한다. source HEAD·branch·동기화 성공은 실행 조건이 아니며 로컬 코드를 실행 입력으로 사용하지 않는다. source 기준 절대 config 경로·interpreter·인자를 전달하고 자식 종료까지 부모 lock을 유지한다. 원격 revision·runtime 소유권 검증 실패는 보존·진단하며 로컬 HEAD나 오래된 코드로 대체 실행하지 않는다.
- Linux source 진입은 setup이 저장한 설치별 PATH, 현재 실행 환경, 기본 시스템 후보 순서로 bootstrap의 첫 외부 명령 전에 프로그램 탐색 환경을 확정한다. 캐시가 없으면 현재 PATH와 시스템 후보로 기존 설치·명시적 cron PATH를 지원하며 손상된 캐시는 진단하고 중단한다. 같은 실행의 runtime·Agent·verify는 확정값을 유지하고 setup의 갱신은 다음 실행부터 반영한다. PATH 캐시는 다른 설정·인증·작업 상태를 초기화하지 않는다.
- runtime의 Python bytecode 캐시는 선택 source·revision·interpreter에 맞는 표준 Python 캐시로 재사용한다. 기본 캐시는 일회성 runtime worktree 밖에 두며 source checkout의 ignored 파일은 runtime 입력이 아니다. 캐시 생성·갱신·부재·손상·쓰기 실패는 현재 원본 실행과 Git 범위 판정을 바꾸지 않고, 사용자가 명시한 bytecode 비활성화나 캐시 경로 선택을 존중한다.

- Loop Manager가 만드는 계획과 구현 변경은 대상 프로젝트 밖의 Loop Manager 관리 경로에 만든 별도 worktree와 branch에만 기록한다
- 자동 실행 중 기본 worktree에서는 위의 safe fast-forward와 소유권·로컬 commit 보존이 검증된 rebase·abort 외에 checkout, pull, reset, stash, clean, commit과 파일 생성을 수행하지 않는다

### 프로젝트 예약 경로

Skill과 Manager가 프로젝트 안에서 고정 위치로 조회·작성하는 다음 경로는 위치를 유지한다.
예약은 모든 파일의 존재·생성 의무를 뜻하지 않으며 필수 문서 조건은 위 등록 규칙을 따른다.
문서 내용 수정, 개별 계획의 rename과 Requirement·ExecPlan archive는 기존 역할·Manager 권한을 따른다.

| 용도 | 예약 경로 |
|---|---|
| 문서 작성 Skill | `.agents/skills/specification-docs/`와 `SKILL.md` |
| 명세·계획 규칙·아이디어 | `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/PLANS.md`, `docs/FUTURE_IDEAS.md` |
| 문서 템플릿 | `docs/PRODUCT.template.md`, `docs/ARCHITECTURE.template.md`, `docs/REQUIREMENT.template.md` |
| Requirement·ExecPlan | `docs/requirements/`·`archive/`, `docs/plans/`·`archive/` |
| 코드맵 | `docs/maintenance/code-map.jsonl` |
| 전체 검증 | `scripts/verify.sh`, `scripts/verify.ps1` |

이 경로들의 루트 `.agents/`·`docs/`·`scripts/`도 고정한다. 프로젝트의 `.gitignore`·`.codex/config.toml`·`.codex/rules/`·`.claude/settings.json`·
`.claude/settings.local.json`은 존재할 때 CLI 입력·격리 검사에 사용되는 위치를 유지한다.
Loop Manager runtime에서만 읽는 `docs/AGENTS.*.md`와 저장소 밖 Manager 상태 경로는
프로젝트 예약 경로가 아니며 프로젝트에 복사하거나 생성하도록 요구하지 않는다.

### 공통 Agent 지침과 직접 작업

- 실행 원본은 bootstrap이 선택한 immutable runtime의 `docs/AGENTS.common.md`와 역할별 `docs/AGENTS.*.md`다. 공통·역할별 파일에는 등록 프로젝트 전반에 적용되는 자동 실행 규칙을 기록한다. Manager 예약 경로·필수 문서·verify 경로 보존은 공통 실행 규칙이다. Loop Manager 자체 개발의 새 루트 항목 제한·instruction 원본 수정 지침은 이 저장소 루트 `AGENTS.md`에 둔다. 공통에는 모든 역할의 기준을, 역할별 파일에는 권한·금지사항과 절차를 기록하며 파일 구성·합성·버전 관리에 대한 설명은 넣지 않는다. Planner·유지보수 Planner는 `AGENTS.planner.md`, Worker와 Reviewer는 각각 `AGENTS.worker.md`, `AGENTS.reviewer.md`를 사용한다. 기존 점검 원본 `AGENTS.planner_check.md`와 저장 snapshot은 호환·감사에 보존하며, R-082의 변경 목표에서는 새 코드 전용 점검을 시작하지 않는다
- 모델 실행 전에 모든 원본의 UTF-8 내용·묶음 SHA-256·source revision·snapshot 참조를 작업에 결합해 Manager 상태 디렉터리와 SQLite에 보존한다. 호출할 때 공통과 해당 역할의 내용만 합치고 실제 전달 내용의 해시를 호출 기록에 남긴다. 합친 실행용 파일은 저장소 밖에서만 생성하며 커밋하지 않는다
- Worker는 고정된 `docs/AGENTS.ponytail.md` 코딩 스타일 지침을 provider·Plugin 설치 여부·활성 상태와 관계없이 항상 한 번 추가해 전달받는다. 다른 역할·사용자 prompt에는 추가하지 않는다. Plugin 설치 목록 조회·캐시·설치 판정은 하지 않는다. 기존 Codex Plugin·App 비활성화와 실행 경계를 유지하고 사용자 설치·설정을 변경하지 않는다. Ponytail이 없는 과거 snapshot은 기존 내용으로 재개하며 명시된 요구사항·안전·필수 검증·역할·게시 조건은 유지한다
- ExecPlan의 Worker·Reviewer·재시도·fallback은 같은 원본 버전에서 각 역할의 지침을 사용한다. 독립 planning·유지보수와 과거 점검 작업도 각 버전을 고정하고 새 작업만 최신 원본을 선택한다. 내용이 같으면 runtime SHA 변경만으로 재계획하지 않으며 새 지침이 진행 작업을 선점하지 않는다. 관련 planning 입력과 성공 결과 재사용은 역할별 원본까지 포함한 내용 해시를 사용한다. 기존 단일 Markdown snapshot은 원래 내용으로 재개하며 새 역할 지침을 덧붙이지 않는다
- Codex의 추가 developer 지침과 Claude의 append system prompt·snapshot 기능으로 기본 지침·기존 추가 지침·프로젝트 규칙·실행 경계를 보존한다. 잘못된 원본·snapshot·저장 실패·미지원 기능·설정 충돌은 모델 실행 전에 거부한다. 버전 없는 과거 세션은 기존 작업물·session·실패 기록을 남기고 최소 문맥의 새 세션으로 복구한다
- Loop Manager 자체 저장소의 작업 worktree에서 공통·역할별 지침 원본 파일은 현재 역할·목표의 일반 파일 권한으로 수정한다. 후보 편집이나 Reviewer의 diff 읽기로 실행 snapshot을 바꾸지 않는다. 변경 PR에 실제 최종 경로·변경 사실·내용·이유·영향·diff 링크를 보고하고 Manager가 누락을 보완한다
- 프로젝트로 복제하는 Specification Skill에는 PRODUCT·ARCHITECTURE·Requirement·FUTURE_IDEAS 작성·검토에 필요한 내용만 둔다. 계획 생성·상태 전이·호출·게시·보관 판정 등 Manager 내부 절차는 제품·설계 문서에서 정의한다. 보관 Requirement의 읽기 전용 범위와 새 변경의 신규 Requirement 작성 기준은 Skill에 유지한다
- 준비 도구는 Specification Skill·문서 템플릿·기본 명세와 작업 폴더를 준비한다. 이 저장소 루트 AGENTS에는 Loop Manager 개발 지침만 둔다.
- 직접 작업·대화형 Agent의 로컬 구현은 사용자 요청의 Requirement를 먼저 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용하고 ExecPlan을 만들지 않는다. 자동 Worker 역할·SQLite 전이·자동 완료 절차도 요구하지 않는다. ExecPlan 생성·계획 작성은 매니저가 호출하는 Planner의 자동 흐름이 담당한다. 직접 구현과 활성 Requirement의 원격 반영 후 실제 미충족 작업만 재점검하며 소급·중복 계획·자동 검증 성공 합성을 하지 않는다. 자동 작업의 ExecPlan·dependency·검증·사용자 Merge gate는 유지한다

### 수동 명령의 준비와 수락

반복 submit은 저장 후보와 생성 당시 원격 기준을 함께 보존한다. 같은 후보의 게시 또는 후속 이력이 확인된 뒤 재중단돼도 같은 요청·전체 G의 결과를 push 없이 확정한다. 확인한 게시 후보가 원격 재작성으로 제거되면 새 후보로 대체 게시하지 않고 이유와 작업물을 보존한다.

`manual start`는 선행 계획의 실제 원격 Merge와 승인·차단·유지보수 조건을 확인하고, 계획 및 작업 자원이 충돌하지 않으면 같은 명령에서 소유 브랜치·워크트리 생성과 원격 푸시를 완료한다. 자동 매니저나 독립된 계획의 실행 여부만으로 준비를 거부하지 않는다. `complete/submit`의 후보 확인·추적 계획 등록·소유 작업 공간 준비도 이 원칙을 따른다.

실행 중인 동일 계획, 겹치는 브랜치·워크트리·게시 대상, Planner 게시 예약과 미완료 Git 작업은 충돌 자원과 소유자를 설명하며 거부한다. 거부된 충돌 요청을 자동 대기 제출로 바꾸지 않는다. 충돌 확인과 소유 예약은 Worker 선택·Planner 게시·다른 수동 명령과 원자적으로 조율하며, 준비 중단 시 저장된 소유권과 작업물을 보존한다. 명령마다 원격 커밋에 고정된 별도 런타임을 사용하고 자기 작업 공간만 정리한다.

수동 구현 대기와 완료·제출 수락 이후의 자동 처리는 구분한다. 수락된 제출은 기존 프로젝트 실행에서 저장소 준비, catalog 확인·정리, 기존 PR 상태 동기화 뒤 실행 자리가 비었을 때 등록 순서로 재개한다. 그 뒤 기존 실행 재개와 일반 coordinator 순서를 유지하며 dependency·차단·유지보수 조건을 통과해야 한다. 별도의 최우선 처리나 현재 작업 종료 직후 즉시 검토를 보장하지 않는다.

complete/submit의 verify와 Review 생략 근거는 후보·base·환경·고정 지침과 각 단계의 실제 운영 입력에 결합한다. verify 입력은 설정된 timeout·parallelism을 포함하고, Review 입력은 Reviewer provider·Model·Effort와 현재 verify 근거를 포함한다. 수락, 단계 실행, 최종 게시와 PR 대기 재평가는 같은 기준을 다시 관측한다. 설정이나 profile을 읽거나 유효성을 확인할 수 없거나 요청에 당시 지문이 없으면 생략을 적용하지 않고 해당 기본 단계를 실행한다. 설정 변경으로 PR 대기 근거가 무효화되면 제출을 자동 재검증 대상으로 되돌린다. 저장·출력에는 설정 원문이나 비밀값을 포함하지 않는다.

### 로컬 환경 복구 요청

`recover <repository> [--plan <id>]`는 등록 대상과 현재 설정을 확인하고 로컬 원인의 제한 재검사를 요청한다. 다른 작업 중에도 요청을 저장하며, 실제 복구는 기존 직렬 실행·소유권·승인 조건에서 진행한다. 즉시 실행할 수 없으면 요청을 보존하고 다음 `run`에서 이어진다. 다른 계획·planning·유지보수가 선택한 계획의 실행 자리를 사용하면 그 작업을 계속하고 복구 요청은 대기한다.

프로젝트 전체와 여러 계획의 미완료 요청을 모두 저장한다. 같은 프로젝트·같은 범위의 재요청은 동일 identity와 이미 소비한 제한 재검사를 유지하며, 다른 범위는 별도 요청으로 접수한다. 실행 가능한 요청 중 접수 순서가 가장 빠른 요청부터 직렬 처리하고, 앞선 대기 요청이 뒤의 실행 가능한 요청을 막지 않는다. 실패·교정·승격 이력을 초기화하지 않는다. 복구 완료는 0, 접수 후 대기·진행 중·미해결·입력 거부는 0이 아닌 종료 코드로 보고한다.

### 작업 선택과 동시 실행

- 명세 변경은 실행 중인 Worker·verify·Reviewer 종료 후 안전한 단계 경계에서 프로젝트 일반 planning을 먼저 수행하고, 관련 ExecPlan 게시·DB 반영을 마친 뒤 현재 작업을 재개한다. 명세·작업 선언이 그대로이면 현재 작업을 먼저 재개한다. 확정 PR 충돌과 소유 Git operation의 복구는 기존 안전 경계에서 먼저 처리한다.
- 작업은 마지막으로 반영한 ExecPlan의 조건 변경과 구현·검증 부족에 따라 대응한다. 작업별 영향·구현 적합성 Planner를 호출하지 않고 필요한 판단·보완은 기존 Worker 세션에서 함께 수행한다. 유효한 구현 충족 근거가 있으면 verify·Review를 계속한다. ID·작업물·세션·실패 이력과 실제 PR·수동 소유·dependency gate를 유지하고 자체 진행 기록은 확인한 delta만 기준에 반영한다. Git 복구와 verify·Review의 입력 증명은 별도로 유지하며 세부 실패·재개 계약은 [R-082](requirements/%5BR-082%5D%20단계별%20원격%20문서%20반영과%20PR%20전%20ExecPlan%20갱신.md#프로젝트-planning과-현재-작업-재개-순서)를 따른다.
- Merge 대기 PR이 있으면 새 유지보수 평가는 보류한다. 독립 계획의 PLAN_BLOCKED 자체는 단계별 유지보수 평가를 막지 않으며, PROJECT_BLOCKED와 현재 변경 실행·공유 안전 gate는 유지한다.
- 같은 프로젝트에 여러 READY ExecPlan이 대기할 수 있다
- 같은 프로젝트에서는 IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR 충돌 복구 중인 ExecPlan 하나만 현재 변경 실행으로 둔다. `PLAN_BLOCKED`와 충돌 복구 중이 아닌 `PR_OPEN`은 이 자리를 점유하지 않는다
- 새 계획 입력은 실행 중인 작업을 다른 작업으로 선점하지 않는다. 프로젝트 Planner가 명세 변경을 관련 ExecPlan에 반영하고 Worker·Manager verify·Reviewer·게시 진입 전 최신 원본·작업 branch를 확인해 기존 규칙으로 rebase한다. 현재 작업 조건이 바뀌지 않았으면 하던 단계를 계속하며, 코드 변경의 충돌 복구·검증 무효화와 증명된 결과 재사용 규칙은 유지한다
- 소유 rebase의 Requirement·ExecPlan 충돌은 Manager가 확인한 최신 원격 base의 원문·경로·삭제로 처리한다. 삭제·rename·archive와 ID·source·dependency·실제 PR 경계를 검증하고 교체 전 문서·작업 형상과 실행 이력을 보존한다. 현재 작업의 유효성이나 소유권을 확인하지 못하면 원인과 작업을 보존해 기존 복구 경계로 연결한다. 문서 충돌만 있으면 Manager가 rebase를 이어가며, 그 외 파일과의 혼합 충돌은 문서만 처리하고 Worker 교정으로 연결한다. PRODUCT·ARCHITECTURE·지침·코드 등은 일반 문서 충돌 해소에서 자동 선택하지 않는다. 반복 교정으로 해결하지 못한 파일은 위 파일별 원본 복원·재작업 경계로 처리한다. 중단·원격 재전진에서도 진행 중 target과 채택 revision을 보존하며 base의 Progress를 현재 후보의 성공으로 사용하지 않는다
- Worker는 최신 작업 문서를 입력으로 구현한다. verify 직전에는 구현 보완이 필요한 경우만 Worker로 돌아간다. Reviewer 직전의 관련 문서 변경은 최신 문서·구현·과거 verify가 확인한 범위와 미검증 조건으로 Review를 먼저 진행하며, 구현 부족은 Worker 교정으로, 검증 근거 부족은 Manager 검증 보완으로 연결한다. 이전 문서 기준의 PASS와 선행 Review를 최신 조건의 검증·게시 승인으로 사용하지 않는다
- READY ExecPlan은 모든 `depends_on`이 MERGED이고 다른 현재 작업이 없을 때만 시작한다
- dependency를 충족하지 못한 READY만 남아도 R-082의 독립적인 Planner 실행 조건이 있으면 해당 판단을 수행한다. 그 조건이 없으면 대기하며 dependency 대기나 코드·ExecPlan 변경만으로 Planner를 호출하지 않는다
- 제품 우선순위는 안전한 무인 실행을 막거나 반복적인 사용자 복구를 요구하는 작업, 정상 경로의 사용자 개입을 제거하는 핵심 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순이다
- 일반·재개·planning 오류 보존 중 독립 READY와 유지보수 prerequisite는 기존 단계가 허용한 실행 후보 안에서 유효 우선순위, 숫자 ExecPlan ID 오름차순으로 선택한다. 유효값은 게시·검증된 같은 프로젝트 catalog에서 자신의 선언값과 자신을 직접·간접 미Merge dependency로 필요로 하는 미완료 목표의 선언값 중 가장 높은 값이며, MERGED에서 전파를 끝낸다. priority 생략은 normal이고 critical·high·normal·low 순서를 유지한다
- 선언값과 dependency·실행 상태는 전파로 변경하지 않는다. 검증된 priority·dependency·catalog 추가/삭제와 실제 Merge 반영 뒤 다음 안전한 선택에서 다시 계산하며, 선택 로그는 선언값·유효값·상향 목표와 dependency 경로를 기록한다. 격리·미확정·유효하지 않은 관계는 기존 검증·보류 경계를 유지한다
- 활성 유지보수 prerequisite 집합은 dependency gate 뒤 유효 우선순위와 숫자 ID 순으로, 리팩터링 DAG는 dependency gate 뒤 숫자 ID 순으로 선택하며 집합 밖 READY는 cycle이 끝날 때까지 시작하지 않는다. 이때도 `PROJECT_BLOCKED`는 일반 실행을 막고, 다른 `PLAN_BLOCKED`·`PR_OPEN`과 dependency로 연결된 계획은 건너뛴다
- 확정 범위의 마지막 계획 Merge와 필요한 원격 관측·게시·catalog 반영 뒤 cycle을 종료하고 장벽 뒤 READY의 일반 선택으로 돌아간다. 별도 사후 재검증이나 이를 대신하는 일반 Planner 호출을 종료 조건으로 추가하지 않는다
- 각 ExecPlan은 고유하고 결정적인 이름의 branch와 별도 관리 worktree에서 작업한다
- 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다

- Loop Manager 자체 저장소의 새 PR Merge가 확인되고 등록 base의 최신 원격 SHA가 실행 runtime의 fast-forward 후속이면 현재 실행을 정상 종료한다. 다음 외부 실행이 최신 runtime을 시작하며 handoff 뒤에는 새 Agent·verify·게시·다른 프로젝트 작업을 시작하지 않는다.
- self Merge 없음, 다른 프로젝트 Merge와 일반 push는 handoff 근거가 아니다. repository·origin·PR base·fetch·ancestry를 확인할 수 없으면 성공으로 처리하지 않고 기존 상태를 보존한다.

- `max_workers` 기본값은 3이며 `config/config.ini`에서 변경할 수 있다
- PLAN_BLOCKED 또는 안정된 PR_OPEN이 있어도 R-082의 Planner 실행 조건을 먼저 평가한다. 해당 조건이 없으면 독립 READY 하나를 유효 우선순위와 숫자 ID 순으로 시작하며, 없으면 대기·완료 조건을 평가한다. 코드·ExecPlan 변경만으로 새로운 계획 호출 조건을 만들지 않는다
- `PROJECT_BLOCKED` 동안에는 새 Planner, maintenance Planner, Worker와 일반 저장소 변경 단계를 시작하지 않는다. 연결 계획이 기존 실행 자리를 점유했거나 자리가 비어 있고 현재 작업 범위를 독립적으로 검증한 경우에만 해당 관리 worktree의 원격 복구·보존 Git 쓰기를 허용한다

### 유지보수 평가

- 기존 활성 cycle을 먼저 복구·완료하고 과거 성공 이력은 유지한다. 새 평가는 최신 원격 입력을 정적으로 확인해 모델 없는 `fast`, 영향받은 모듈의 `targeted`, 전체 모듈의 `full` 점검 중 필요한 범위를 선택한다.
- 프로젝트 최초 실행은 일반 모드의 Planner·READY 선택으로 시작한다. 유지보수 평가 이력이 없다는 이유만으로 자동 cycle이나 정밀 점검을 시작하지 않는다. 첫 자동 유지보수의 주기는 최초 실행 시 확인한 MERGED 수를 기준으로 계산하고 재시작에도 그 기준을 보존한다.
- 자동 유지보수는 첫 주기 기준 또는 직전 cycle 정상 종료 시 마지막 유지보수 Merge까지 포함한 MERGED 수에서 `refactoring_review_interval`만큼 증가했을 때 시작한다. `[loop]`의 기준값은 변경 가능한 0 이상의 정수이며 기본값 20을 유지한다. 0이면 자동 전환을 끄고 새 명시적 `maintenance-review <repo>` 요청은 0·최초 실행·주기 도달 전에도 다음 안전한 경계에서 전환할 수 있다. 값 변경은 누적 Merge 기준을 초기화하거나 활성 cycle을 중단하지 않는다. 첫 정밀 점검은 첫 유지보수 cycle에서 수행한다. 활성 작업·실제 Open PR·프로젝트 안전 gate와 해당 Requirement의 즉시 재확인을 처리한 뒤 due 유지보수는 일반 작업 선택보다 먼저 평가한다. 상세 기준은 R-074·R-076을 따른다.
- 한 번의 평가에서 모듈 묶음 검토·필요한 종합을 수행하고 발견을 새 계획·기존 비종료 계획·근거 있는 제외에 연결해 prerequisite·리팩터링과 필요한 dependency closure를 확정한다. prerequisite Merge나 개선 결과로 새 평가를 시작해 범위를 늘리지 않는다. 주기 전 코드 영향·평가 receipt와 최신 원본의 차이·완료 후보는 자동 평가의 시작 조건이 아니다.
- 각 tiered 평가는 소스 모듈뿐 아니라 문서·자산·manifest·설정까지 추적된 모든 루트 항목의 실제 참조·사용 경로·소유권·유지 이유와 안전한 목적지를 조사한다. 소스 모듈이 없는 tree도 조사 누락이 있으면 완료할 수 없다. 이동 후보는 동작·공개 계약·저장 형식과 경로·배포·설정 참조를 보존하는 새 계획 또는 해당 목표를 포함한 비종료 계획에 연결하고, 계획이 원격 게시·catalog에 반영되기 전에는 평가 성공을 확정하지 않는다.
- 유지보수 Planner는 필요한 선행과 리팩터링 DAG를 판단한다. Manager는 새 리팩터링 DAG의 모든 말단을 필요한 선행 dependency closure 밖의 적격 READY snapshot에 추가한다. 이 편집은 기존 dependency 순서와 계획 본문·metadata를 보존하며, 실행 이력·소유권·PR이 있거나 cycle·snapshot 범위 밖인 계획에는 적용하지 않는다.
- 원본에 결합된 성공 모듈 검토는 해당 묶음의 입력이 같을 때 재사용한다. 검토에서 발견이 없고 일반 계획을 함께 수행할 필요도 없으면 별도 종합·계획 호출을 하지 않는다. 발견이 있으면 새 계획·기존 비종료 계획·근거 있는 제외에 연결해 처리한다.
- 리팩터링은 동작·공개 계약·저장 형식을 보존한다. 각 계획의 targeted test·전체 verify·Review·사용자 Merge를 유지하고 확정 범위가 모두 실제 MERGED인 뒤 종료한다. 계획 없음·기존 계획 연결·근거 있는 제외도 검증·필요한 게시 뒤 종료한다.
- 평가 성공 원본과 cycle 종료 cadence는 별도로 보존한다. 수정 뒤 검토하지 않은 원본의 receipt를 합성하지 않는다. 명시 요청은 cycle 시작에 identity를 결합해 한 번 소비하며 활성 cycle 중 새 요청은 다음 안전한 경계까지 보존한다. 중단·원본 변경은 같은 cycle의 미완료 게시와 필요한 관련 근거만 재검증한다.
- 종전 파일별 평가·게시 결과를 보존한 cycle은 해당 검증·복구 경로로 마친다. 그 이력과 완료 후보의 종전 안정화 판정을 새 단계별 평가의 기본 반복 조건으로 사용하지 않는다.

### 복구 판단과 최종 차단

- 차단 후보는 disposition, 안정적인 원인 code, plan ID, phase, plan/base revision, 진단 지문과 진전 marker로 기록한다. 복구 disposition과 `PLAN_BLOCKED`/`PROJECT_BLOCKED` scope는 별도 기준이며 최종 사용자 조치가 확정된 뒤에만 원인 code로 scope를 정한다
- 일시적 Git·서비스 실패는 현재 실행을 보존해 다음 실행에서 재시도하고, 유효한 최신 base·catalog 및 실제 소유 Git 상태로 하나의 다음 단계를 확정할 수 있으면 상태를 재구성한다. Requirement 이동 실패는 planning 작업에 보존하고 rebase 충돌은 진전과 반복 한도를 기록하며 교정한다. Manager가 처음 rebase 충돌을 관측하거나 Worker 결과 형식 오류를 받으면 현재 실행을 끝내고 다음 실행에서 보존된 Worker 세션으로 재개한다. 진행 중인 Worker·검증·Review 교정은 같은 실행에서 이어갈 수 있다
- 일시 재시도, 상태 재구성, 한도 내 Agent 교정은 현재 phase, branch, worktree, 소유권, Agent session, 승인, 변경, 검증 원문과 실패 이력을 보존하며 차단, 원격 checkpoint, BLOCKED 알림을 만들지 않는다. 한 실행에서 최신 원격 snapshot이 안정되지 않으면 같은 상태로 일시 중단하고 다음 실행에서 다시 판정한다
- 승인·PR reopen·현재 작업 대상·원격 OID 불확실성, 실제 sandbox 정책 실패, 안전한 원본으로 확정할 수 없는 손상, verify·rebase·Worker 결과 형식·Reviewer 프로토콜의 최종 반복 한도 소진은 각 원인의 복구·사용자 조치 경계에서 처리한다. 정상 Git 변경의 과거 snapshot 차이는 최종 차단 사유가 아니다. Review 유발 Worker 교정의 실행별 한도는 작업을 보존하는 일시 중단으로 처리한다. 최종 기록에는 복구 횟수, 마지막 안정 revision, 남은 불확실성과 실제 조치 대상을 포함한다
- 최종 `PLAN_BLOCKED`와 계획에 연결된 `PROJECT_BLOCKED`의 검증 가능한 대상 작업만 안전한 원격 checkpoint 또는 진단 게시 대상이다. BLOCKED 알림은 최종 차단 transaction과 checkpoint 결과 뒤에 생성하고 동일 plan/scope/reason/fingerprint/revision은 한 번만 알린다. 사용자 수정 경로는 원인이 실제 대상을 제공할 때만 표시한다

### Model과 Effort 선택

- Planner가 ExecPlan의 complexity와 risk를 판단한다
- 프로그램은 complexity를 low=1, medium=3, high=5로 환산한다
- 프로그램은 risk를 low·medium·high=0, critical=1로 환산한다
- 두 점수의 합으로 최초 Worker level 1~6을 선택한다. level 7·8은 완료한 Worker 교정 뒤의 Review 반복에 따른 승격으로만 도달한다
- 선택 provider의 Worker level 1~8은 `config/config.ini`에 Model alias·Effort와 참조 alias의 실제 Model 이름을 모두 명시해야 한다. 누락·빈 값·잘못된 형식·CLI 미지원 조합은 Worker 시작 전 오류로 보고하며 코드·CLI 기본값이나 다른 profile로 보충하지 않는다. 예시 설정은 선택 규칙이나 fallback이 아니다
- 실제 provider·Model·Effort는 설정대로 호출·기록한다. Model 또는 Effort가 바뀌면 기존 session 전이 정책을 따르고 작업물·검증 이력은 보존한다
- provider별 Fast 선택은 on/off이며 기본값은 `false`다. 공란·공백·provider section 또는 항목 생략도 off로 처리하고 CLI 기본 설정이나 저장된 session의 Fast 선택보다 우선하도록 요청한다. 모든 역할·Worker level에 공통 적용하고 실행 시작의 선택을 신규·재개·재시도에 유지한다. 변경은 다음 실행부터 반영하며 Fast 선택만으로 session·작업물·검증 이력·Worker level을 초기화하지 않는다
- Fast 선택은 모델·Effort와 독립적이다. Manager는 모델별 Fast 지원을 사전 판정하거나 모델을 교체하지 않고 지원·대체 실행과 진단을 CLI에 맡긴다. 호출 기록의 Fast 선택을 실제 적용 성공으로 간주하지 않으며 CLI 실패는 기존 규칙대로 처리한다. Fast 사용에 따른 추가 비용·사용량 증가와 CLI 버그로 인한 미적용은 provider 정책을 따르고 별도 우회하지 않는다
- 위 실패 정책의 완료한 교정 결과만 effective level을 한 단계씩 올릴 수 있으며 한 ExecPlan 안에서는 level을 낮추지 않는다. 과거 지문 횟수에서 교정 완료를 추정하지 않고, 관계를 확인할 수 없는 기존 기록은 다음 유효 Review부터 기준선을 시작한다
- Codex와 Claude의 level 8도 `config/config.ini`에서 지정한 Model alias와 Effort를 사용하며 CLI가 지원하는 조합인지 검증한다. level 8보다 높은 자동 profile은 없다
- effective level과 level별 Review 승격 이력은 SQLite에 저장하고 새 ExecPlan 또는 정상적인 BLOCKED 해제에서 최초 level로 초기화한다

### Agent 호출 기록

- 프로세스 종료 확인과 호출 결과는 별개다. 종료가 확인된 실패·중단·수집 오류는 Requirement 교정을 계속 보류시키지 않으며 성공 결과로 바꾸거나 재사용하지 않는다. 재시작 때 호출별 종료 근거를 확인·보존하고, 근거 없는 과거 호출은 미확인으로 유지한다. 임의의 종료 코드나 후속 호출 성공으로 이전 writer 종료를 대신하지 않는다.
- Codex는 신규·재개 모두 Agent 이벤트·출력 수신 후 10분 무출력을 감지한다. Manager 진행 로그와 저장량 제한은 타이머에 영향을 주지 않는다. 실행 중인 명령은 기존 전체 제한 시간까지 기다리고, 그 밖의 무응답과 턴 완료·프로세스 종료 뒤 결과 수집 대기는 구분해 종료 확인 후 기존 재시도 정책으로 처리한다.

- 실제 호출 위치는 작업 목적과 가능한 대상 계획·task·batch·파일을 invocation ledger에 기록한다. 호출의 역할·provider·Model·Effort, session 전이·재시도, 결과·사용량과 실행 구간을 이 정보에 연결한다.
- 호출 준비와 실제 CLI 프로세스 실행을 구분한다. 프로세스 생성에 성공한 호출만 실제 실행 수와 사용량·실행 시간에 포함하며, 실행 전 거부·프로세스 생성 실패는 별도 상태로, 경계를 확인할 수 없는 중단·legacy 기록은 미확인으로 보존한다. legacy 목적이나 실행 여부를 추측하지 않는다.
- 호출 기록에는 prompt, 비밀값, 인증 자료 또는 코드·문서 전문을 저장하지 않는다. 실행 관측 저장 실패는 Agent 결과를 바꾸지 않으며, 필수 격리·소유권 검증과 그 증거 저장은 계속 실행 gate로 적용한다.

### 운영 로그

운영 로그는 단계의 실제 시작·결과, 선택·생략·재사용·재시도 이유, 장시간 진행·대기와 다음 행동을 프로젝트·작업·실행별로 확인할 수 있게 한다. Telegram 알림 여부와 독립적으로 기록하며 문구와 요약은 AI 호출 없이 프로그램 코드로 생성한다. 준비·실제 실행·결과 검증·원격 반영을 구분하고 미확인 결과나 진척률을 추측하지 않는다. 공통 포맷·추가 판단 기준·상세 기록 연결은 [운영 로그 정책](ARCHITECTURE.md#운영-로그-정책)을 따른다.

### PR 생성과 Merge 후 정리

- 시작 ignore 규칙으로 증명된 index-free ignored 파일의 생성·수정·삭제는 역할별 결과·planning·검증 입력 오류의 단독 원인이 아니다. tracked·staged·non-ignored 후보와 실제 Git 작업의 index 안전 조건·대상 연결·원격 lease는 계속 확인한다.
- Worker targeted test → Manager verify → Reviewer는 같은 ExecPlan worktree에서 이어간다. ignored 확장자 allowlist·원상 복원 의무·별도 검증 worktree를 추가하지 않는다. 활성 ignored 파일과 들어오는 tracked 경로가 겹치면 파일을 보존하고 해당 Git 쓰기만 보류한다.
- 저장된 ignored-only 오탐은 시작 규칙·역사 결과·현재 소유권과 권위 상태를 검증해 기존 session·결과·단계에서 재개한다. 실제 위반·증거 부족·새로 숨긴 경로·강제 stage·독립 차단은 이 규칙으로 해제하지 않는다.
- 정상 종료한 정확한 Manager 소유 planning/ExecPlan worktree는 ignored-only 상태에서도 전체 수명을 종료할 수 있다. 기본 checkout·소유권 불명·미게시 commit·권위 변경은 보존하며 활성 artifact를 임의로 삭제하지 않는다.

- Worker는 현재 환경의 전체 검증 스크립트를 생성·유지하고, 프로그램이 Agent 밖에서 실행한다.
- Loop Manager 자체 verify는 동일한 unittest manifest와 fixture 계약을 직렬·프로세스 병렬 모드에서 유지하고 전체 compile을 포함한다. 실제 Agent 실행을 테스트 경계에서 차단하며 테스트·fixture·대기·정리의 시간과 중단 진단을 성공 stdout과 분리해 보존한다. Python 가드는 같은 프로세스의 다중 로딩에서도 한 번 설치되며 설치 확인 실패는 보호 없이 진행하지 않는다. 자식 환경 준비의 중복만 제거하고 실행마다 명령/API 거부 검사를 유지한다. 명시·상속 환경의 조율 인증값을 제외하고 부모·호출자 환경, 중첩 Python 보호와 후보 worktree 선택을 보존한다.
- 명시적 공통 verify 연결은 unittest와 Perl TAP adapter를 제공한다. Perl은 선언한 프로젝트 도구로 파일 suite를 수집하고 native plan·assertion·skip/TODO·종료 결과를 대조한다. 도구·연결 오류와 부분 결과를 성공으로 처리하지 않는다. 두 언어 예제는 같은 core의 예약·종료·진단과 순서 있는 필수 compile을 사용한다. Perl의 실제 Agent 호출 경계는 별도 guard로 차단하며 지원 버전·한계는 [VERIFY.md](VERIFY.md#perl-tap-지원과-두-언어-예제)를 따른다.
- 자체 verify는 실제 소스·테스트와 고정 샘플, 의존성, `scripts/verify.json`, verify·copy 스크립트와 소비되는 `run.sh`·`run.ps1`을 입력으로 관측한다. 문서·Skill·README·예제 설정의 정책·안내 문구 일관성은 검사하지 않으며 해당 원본을 tests 안의 고정 샘플로 자동 복사·동기화하지 않는다. 파서·등록·profile·파일 복사 동작 검증, 필수 unittest 수집과 전체 compile은 유지한다. 문서 prose 변경만으로 자체 verify를 다시 실행하지 않으며 자체 verify는 그 문서의 품질을 보증하지 않는다.
- 복구 테스트 준비는 각 테스트가 소유한 실제 파일·Git·SQLite 경계를 독립 복원하고 현재 identity·digest를 확인해 재사용한다. 이전 테스트 성공은 현재 후보의 검증 근거가 아니며 준비 생성·복사·복원·확인과 관측 경로 전체의 비용을 진단에 포함한다.
- 자체 verify의 `verify_parallelism`은 생략 시 `auto`, 양의 정수는 상한, `1`은 직렬이다. 실행량은 코드가 CPU 제한·실제 부하·메모리 여유와 미반영 예약으로 결정하며 등록 프로젝트 수로 고정 분할하지 않는다. 관측 실패는 직렬로 축소하고 자원 대기도 전체 timeout에 포함한다.
- Manager 실행 인스턴스가 동시 verify의 확인·예약을 조율한다. 예약은 해당 자식 종료 또는 미시작을 확인한 뒤 해제하며 종료 불명은 보존한다. 기존 프로젝트 선택과 같은 프로젝트의 ExecPlan 동시 실행 정책은 유지한다. 과거 시간 자료는 현재 후보 검증을 대신하지 않는다.
- 최종 후보에서 관련 targeted test와 전체 verify가 통과하고 Reviewer blocking issue가 없어야 한다.
- 자체 unittest의 부분 경로는 기본 사용하며 `[loop] verify_partial = false`로 끌 수 있다. 옵션 생략 시 true다. Worker 완료·후보 커밋/최신 기준 조정 뒤 빠른 부분 회귀 검사·일반 선행 Reviewer·실제 전체 verify를 수행한다. 모델 없이 변경 전후 전체 AST와 수집 명세로 변경 테스트와 가까운 관련 모듈을 우선 선정하고 공통 검사를 보완한다. 문서 병행 수정·분석 불확실성·고위험 공통 코드·넓은 영향만으로 전환하지 않으며 불확실성과 미선택 범위를 결과에 남긴다. 부분 PASS는 선택 모듈만의 성공이다. 검증 자체의 계약 변경·후보/수집 명세 불일치·대상 부재/전체 필수 대상 또는 Reviewer 환경 증거 부재에서는 기존 전체 verify → Reviewer로 전환한다. Worker targeted test 제한과 최종 전체 검증·게시 조건은 유지한다.
- 일반 선행 FAIL은 전체 verify 없이 기존 Worker 교정·ISSUE_ID·level/반복 정책으로 연결한다. 일반 선행 PASS는 게시 승인이 아니다. 동일 작업 주기의 HEAD/base/전체 diff·명세·사용자 결정·실제 지침/profile·환경/정책과 현재 후보의 실제 전체 verify 성공 receipt가 모두 확인될 때만 추가 Reviewer 없이 최종 승인하며 게시 직전 재확인한다. 증거 부재·변경·실패·중단에서는 기존 최종 Reviewer 또는 안전한 재검증을 수행한다. R-082 문서 변경 선행 Review는 이 재사용 대상이 아니다. 부분 성공은 전체 verify token/receipt·verified_head를 만들거나 전체 검증을 생략하지 않는다.
- 자체 테스트의 Git 관측·정책 조건은 mock을 기본으로 제공하고 실제 판단·저장을 검사한다. snapshot/index/worktree·OS 효과와 부모/자식 연결처럼 실제 실행이 필요한 계약은 통합 회귀로 유지하며, skip·timeout 확대·수집 축소로 검증 비용을 줄이지 않는다. 실제 Git fixture는 사용자 설정·template과 격리하고 필요한 설정만 테스트 입력으로 제공한다.
- Manager가 먼저 안전한 rebase를 시도한다. 충돌 없이 완료되고 source 내용까지 포함한 구현 입력과 완료 Worker 결과가 같으면 SHA 변경만으로 Worker·완료한 targeted test를 반복하지 않는다. 현재 HEAD의 후보 확인만 달라졌으면 필요한 verify·Review를 진행한다. 실제 구현 입력 변경·교정 사유·미해소 충돌은 기존 Worker/session 경로로 처리한다.
- rebase 충돌 중 Progress 체크 상태와 명확한 기존 실행 결과만 바뀌고 구현 조건의 동일성이 증명되면 session·작업물·index·rebase 상태·retry·실패 감사를 보존해 해결을 이어간다. 절 이름만으로 목표·제약·검증 조건·사용자 결정을 제외하지 않으며 불확실한 기록이나 과거 원문 근거 부재는 기존 재확인·차단을 유지한다. 이 구분은 verify·Review 입력을 제외하지 않는다.
- 같은 프로젝트·ExecPlan·작업 주기의 실제 전체 verify 성공과 파일 bytes·경로·종류·mode·목록·명령·환경·runtime·실제 Git/외부 입력의 지문이 같고 현재 후보에서 다시 확인되면 원래 run을 연결해 verify를 재사용한다. 목록 부재·오류·누락 의심, dirty/untracked 입력 변경, 미확인 동적·외부 입력과 유효한 성공 부재는 전체 verify를 실행한다. 일반 스크립트의 입력을 명령 문자열만으로 증명하지 않는다.
- 운영 입력 증명 adapter는 고정된 격리 UTF-8 Python 문법 검증 계약을 지원한다. 해당 전체 verify 계약의 명령·프로젝트 입력·Python/stdlib/native runtime을 실제로 관측한 경우만 재사용하며, 임의 스크립트와 미지원 환경은 전체 실행을 유지한다. 정확한 계약과 지원 범위는 [ARCHITECTURE.md](ARCHITECTURE.md#verify-입력-관측과-생성-요청)에 둔다.
- 새 verify 실행은 과거 성공의 재사용 자격을 먼저 무효화한다. 실행·재사용 판정 중 입력 변경, 새 실패, 저장 중단·경쟁과 다른 작업 주기의 결과로 현재 후보를 통과시키지 않는다. 생략은 새 invocation·성공 실행·미측정 절감량을 만들거나 실패 감사를 초기화하지 않는다. Review·게시 gate는 독립적으로 유지한다.
- 구현 PR 게시에는 Requirement 전체 완료나 archive 이동을 강제하지 않는다. Requirement 이동은 별도 planning 게시에서 최신 판단·원문·목적지와 원격 base를 다시 확인한다.
- 관리 rebase는 자동 Worker 해결과 사용자 수동 해결을 같은 현재 상태 기준으로 수용한다. 현재 소유권·범위·Git 상태·원격 기대값을 검증하며, 과거 turn·stage·receipt·journal 완전성을 완료 조건으로 요구하지 않는다.
- 동일 계획의 기존 원격 작업 branch를 rebase한 결과는 다음 단계 전에 중간 checkpoint 게시 또는 현재 lease에 따른 보류를 확정한다. 새 원격 입력이 오면 낡은 게시 intent를 감사에 남기고 통합·재검증하며 새 lease만 끼워 넣어 push하지 않는다. 실제 push가 확인됐을 때만 완료를 기록하고 이미 확인한 push를 반복하지 않는다. 이 게시와 과거 receipt는 구현·verify·Review·PR 승인을 대신하지 않는다.
- 같은 branch의 기존 PR을 복구해 사용하며 중복 PR을 만들지 않는다. PR은 사용자가 Merge한다.

| Merge 후 작업 공간 | 처리 |
|---|---|
| clean이고 PR head에 포함되지 않은 로컬 commit 없음 | Merge·Squash·Rebase 방식과 원격 head branch 존재 여부에 관계없이 정리한다. |
| 미커밋 변경·미게시 로컬 commit 또는 안전성 불확실 | 경로·원인을 cleanup_pending에 기록하고 보존한다. 이후 안전 조건이 충족되거나 경로가 사라지면 해제한다. |

게시 lease·중단 복구·후보 검증 토큰의 적용은 [ARCHITECTURE.md](ARCHITECTURE.md#관리-rebase와-후보-검증)를 따른다.

### 프로젝트 완료 조건

- 입력 변경 검토가 끝나고 비종료 계획·실행·PR·draft·차단·planning 오류와 활성 또는 due 유지보수가 없는 완료 후보에서 일반 Planner가 최신 원격 PRODUCT(존재하면)·ARCHITECTURE와 남은 활성 Requirement 전문을 구현·테스트·검증 근거와 대조한다. 최근 diff 밖의 미충족 작업은 source에 연결된 후속 계획으로, 필수 사용자 결정은 draft로 남긴다
- 최종 전문 확인의 입력 지문·관측 revision·게시 확인 revision·작업 주기와 결과를 변경 검토 및 Requirement별 판단과 별도로 보존한다. 같은 경계의 Planner 호출에 통합해도 Requirement 한 건의 충족이나 변경 검토의 추가 계획 없음을 프로젝트 전체 성공으로 취급하지 않는다. 동일한 유효 입력·주기의 성공만 재사용하며 입력·작업 주기 변경이나 검토·게시 실패로 성공을 합성하지 않는다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 PLAN_BLOCKED 상태의 ExecPlan과 PROJECT_BLOCKED 레코드가 없어야 한다
- due 또는 활성 유지보수 cycle과 해결되지 않은 유지보수 planning error가 없어야 하며, 실제 Merge·게시·catalog 확인에 결합된 cycle 종료 기준이 있어야 한다. 최신 코드와 평가 receipt의 차이만으로 추가 유지보수를 요구하지 않는다
- 해결되지 않은 `planning_error`가 없어야 한다
- `cleanup_pending`은 프로젝트 완료를 막지 않는다
- Requirement에는 별도의 초안 상태를 두지 않는다. ExecPlan의 draft 준비도·사용자 판단 대기는 실행 phase와 분리하며 프로젝트 완료와 관련 Requirement archive를 막는다
- 프로젝트 완료 알림은 프로젝트별로 관리한다
- 새로운 PRODUCT/Requirement 변경으로 작업 주기가 시작되고 다시 완료되면 새 완료 알림을 전송한다
- Requirement archive 이동만으로 완료 알림을 다시 활성화하지 않는다
- 안정적인 작업 선택에서 최신 입력의 성공한 최종 전문 확인과 활성 Requirement·비종료 ExecPlan·프로젝트 차단·planning error·활성 또는 due 유지보수 작업이 모두 없는 조건을 함께 만족할 때만 완료로 판정한다. 이 최종 확인은 유지보수 종료의 추가 조건이나 종료만을 이유로 한 Planner 호출로 사용하지 않는다
- 시작 가능한 계획이 없지만 활성 Requirement 또는 비종료 ExecPlan이 남으면 계획별 dependency 상태, PR Merge 대기, 구조화된 차단 원인과 확인된 사용자 조치, 활성 Requirement의 대기 이유를 결과·로그에 남기고 별도 대기 사건으로 한 번 알린다
- 실행 가능한 READY, 진행·복구 중인 단계, 일시 오류의 자동 처리 대기와 PROJECT_BLOCKED에는 작업 소진 알림을 만들지 않는다. 프로젝트 차단 외의 PR Merge·계획 차단·사용자 판단·dependency 대기는 개별 조치 알림의 전송 여부와 관계없이 시작 가능한 작업이 없는 상태를 일반 알림으로 알린다

### Telegram 알림 조건

- Telegram token과 chat ID가 모두 설정된 경우에만 알림을 생성하고 전송한다
- 두 값이 모두 비어 있으면 알림을 비활성화하며 작업 흐름에는 영향을 주지 않는다
- 새 ExecPlan은 원격 게시·catalog 반영 뒤 한 번 알리며 작성 완료·새 계획 없음·기존 계획 개정·삭제만으로는 알리지 않는다. 게시된 draft는 프로젝트별 현재 목록의 일반 알림으로 통합하며 전체 건수와 각 ID·제목을 한 줄씩 표시한다. 확인된 원격 문서 링크는 ID에 연결하고 질문·선택 영향은 작업 문서에서 확인하도록 안내한다. 목록 구성·판단 근거가 바뀌면 현재 목록을 알리며 동일 목록·근거와 빈 목록은 반복 통지하지 않는다
- 새 작업 가능 계획 생성·Worker 최초 실제 시작·통지된 차단 후 실제 재개·유지보수 cycle 시작과 정상 종료·프로젝트 완료는 무음이다. PR 검토·Merge·최종 차단·사용자 판단과 작업 소진 대기는 일반 알림이다. 지속 실행 실패는 실제 최종 차단으로 확정될 때 같은 차단 알림에 통합한다
- 일반·빠른·유지보수 Planner의 개별 호출 시작과 Reviewer 시작·종료, 단순 단계 실패·자동 교정·재시도·자동 재개는 별도로 알리지 않는다. 실행 결과·로그·실패 이력은 보존한다. 유지보수 알림은 개별 호출이 아니라 cycle 전이에 연결하며 후속 범위의 실제 Merge 또는 후속 계획 없는 평가·필요한 게시 완료를 정상 종료로 본다
- PR 생성 알림은 프로젝트·ExecPlan·작업 내용·PR 링크와 Merge 대기 상태를 우선 표시한다
- 차단 알림은 확인된 사용자 조치, 차단 작업·scope, 구체적 원인, 확인 대상·질문, 복구 상태·Checkpoint 순으로 구성한다. 확인되지 않은 수정 행동이나 복구 성공은 추측하지 않으며 실제 수정 대상이 확인된 경우에만 그 경로를 안내한다. checkpoint 성공 시 원격 commit link·OID, 미게시 시 local-only 보존 이유와 중요한 오류 근거를 유지한다
- 차단 알림은 plan·scope·원인·지문·revision이 같을 때 같은 사건으로 중복 제거한다
- 지속 실패 판정과 실패·교정·외부 실행 횟수만으로 별도 알림을 만들지 않는다. 같은 대상·원인의 실제 `PLAN_BLOCKED` 또는 `PROJECT_BLOCKED`와 checkpoint 결과가 확정되면 기존 차단 알림만 한 번 만든다. 이전 `persistent_execution_failure` outbox는 전송 직전 같은 대상·원인의 현재 차단을 다시 확인하고, 차단이 없거나 차단 알림과 중복되면 취소 표식을 남겨 미전송 조각을 막는다. 이미 전송한 조각·시도·오류·payload와 실패 감사는 보존한다
- 미해결 `planning_error`는 같은 입력의 반복 실행·재시작, 다른 계획·PR 상태 변경과 단순 원격 base 전진에도 같은 차단 사건을 유지한다. 실제 planning 오류 해제와 사건 해제·미전송 알림 취소를 하나의 SQLite transaction에 기록하며, 소유권 확인이나 정리가 실패하면 모두 미해결로 보존한다. 과거 중복 사건과 전송 감사 기록은 삭제하지 않고 현재 사건의 추가 분할과 중복 알림을 막는다
- BLOCKED 알림의 한 조각 이상 전송이 확인된 차단이 해제되고 해당 계획의 Worker·PR 처리 또는 프로젝트·Planner의 정상 작업이 실제로 시작되면, 이전 scope·계획·원인과 시작한 작업을 담은 재개 알림을 사건당 한 번 전송한다. 단순 해제, 다른 계획의 시작, 미전송 차단과 Telegram 비활성화에는 만들지 않으며 해제된 차단의 미전송 조각은 취소한다. 같은 원인으로 별도 통지된 이후 차단은 새 사건으로 취급한다
- 프로젝트 완료 알림은 해당 프로젝트의 현재 작업 주기가 완료됐을 때 전송한다
- 완료 알림에는 Planner의 추가 작업 없음 확인, 활성 Requirement 없음과 마지막 완료 계획을 포함한다
- 대기 알림은 확인된 사용자 행동이 필요한 항목을 먼저 표시하고 나머지 대기 사유도 보존한다. 자동 복구 가능한 오류만으로 사용자 조치를 요구하지 않는다. 같은 안정적 대기 원인은 재실행·재시작 뒤 중복 알리지 않고, 작업 진행 또는 관련 입력·차단 원인이 바뀐 새 대기 상태는 한 번 다시 알릴 수 있다
- Telegram이 비활성화되거나 전송에 실패해도 완료·대기 이유는 실행 결과와 로그에 남는다
- 동일한 이벤트는 한 번만 전송하며 실패한 알림만 다음 실행에서 재시도한다
- 확정된 새 알림은 같은 실행에서 다음 장시간 Agent·verify 전에 전송을 시도한다. 전송 때문에 작업을 무기한 대기시키지 않으며 정책 변경 전 미전송·부분 전송 알림은 기존 내용·주의 수준·이력으로 이어 보낸다. 기존 상태 해소에 따른 취소 정책은 유지한다
