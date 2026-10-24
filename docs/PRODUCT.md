# Loop Manager

## Summary

Loop Manager는 외부 스케줄러로 반복 실행되며, GitHub와 연결된 여러 로컬 Git 프로젝트에서 제품 명세와 변경 요구를 바탕으로 계획·구현·검증·PR 생성을 자동으로 진행하는 개발 관리 프로그램이다. 작업 상태를 보존해 중단 후 재개를 지원하며, 필요한 의사결정과 최종 PR Merge는 사용자가 맡는다.

## Target Users

GitHub 프로젝트의 명세를 관리하고 개발 자동화를 운영하려는 개발자

- 프로젝트의 제품 문서와 기술 설계를 작성한다
- 대상 프로젝트를 등록하고 외부 스케줄러로 Loop Manager를 반복 실행한다
- BLOCKED 사유와 생성된 PR을 확인하고 필요한 결정을 내린다
- 검토가 끝난 PR을 Merge한다

## Scope

### Current Goals

- 여러 로컬 GitHub 프로젝트의 필수 조건을 확인해 등록하고, 공통 개발 지침에 따라 자동 작업을 진행한다.
- 독립 문서 작성·검토 도구인 Specification Skill을 함께 제공해 PRODUCT·Requirement·ARCHITECTURE 작성을 지원한다. Skill은 Loop Manager 설치·등록·자동 실행 없이도 사용할 수 있다.
- 미충족 PRODUCT·활성 Requirement를 구현·검증할 ExecPlan으로 구체화하고, 의존성·우선순위에 따라 처리한다.
- 작업 상태·작업물·Agent session을 보존해 중단 후 재개한다.
- Worker가 구현·관련 테스트·전체 검증 스크립트를 작성하고, 프로그램이 전체 검증과 독립 Review를 실행한다. 실패는 기존 작업에서 교정한다.
- 검증된 작업으로 PR을 생성하고 사용자 Merge를 기다린다.
- 전체 충족이 확인된 Requirement와 MERGED ExecPlan을 내용 보존 archive로 자동 보관한다.
- 같은 프로젝트의 독립 작업도 프로젝트별 관리 한도와 전체 호출 한도 안에서 병렬로 처리한다. 자원이 충돌하지 않는 수동 명령의 준비와 로컬 복구 요청 접수는 자동 실행 중에도 수행한다.
- 유효한 기존 판단을 재사용하고 동일 입력의 불필요한 Planner 호출을 반복하지 않는다.
- 현재 사용자 조치·PR 검토 요청과 확인된 자동 복구 불가 최종 중단을 Telegram으로 알린다.

### Non-Goals

- 자동 Merge
- 내장 스케줄러
- 지정된 이벤트 외 실시간 상태 알림

## Product Invariants

**문서와 실행 기준**

- 미충족 PRODUCT 또는 활성 Requirement에 근거한 작업만 계획한다. 범위 밖 아이디어는 현재 작업으로 자동 승격하지 않는다.
- PRODUCT·ARCHITECTURE는 현재 동작·설계의 기준, Requirement는 원하는 변화와 Acceptance Criteria, ExecPlan은 구현·검증 작업을 기록한다. 문서별 수정 권한은 [문서 역할과 갱신 책임](#문서-역할과-갱신-책임)을 따른다.
- ExecPlan ID는 계획과 실행 이력을 연결하는 안정적인 식별자다. 실행 상태의 원본은 SQLite이며 문서에 상태를 기록하지 않는다.
- 자동 작업은 최신으로 검증한 `origin/<base-branch>`를 기준으로 한다. 기본 worktree의 로컬 변경·commit은 보존하고 자동 입력에 포함하지 않는다.

**실행 권한과 순서**

- Agent는 선택된 관리 worktree에서 작업한다. provider sandbox와 역할별 쓰기 정책으로 접근 권한을 강제하며 적용을 확인하지 못하면 호출을 중단한다. 최종 문서 변경·결과·게시 후보는 별도로 검증한다.
- 호출 전후 Git refs·config·다른 worktree·HEAD·index·파일 상태 차이는 접근 권한 위반으로 판정하지 않는다. ledger 부재·실패·fallback·재시작에서도 이 비교를 되살리지 않는다.
- 일반 verify는 현재 대상 worktree와 역할에 필요한 자원을 검사한다. Agent ledger 부재를 이유로 repository-wide 비교를 요구하지 않으며, 후보와 실제 검증 입력을 바꾸지 않는 기본 checkout·무관한 ref·worktree 변화는 성공을 막지 않는다.
- 모든 선행 ExecPlan이 MERGED인 계획만 시작하며, 프로젝트별 관리 한도와 전체 구현·Review 및 verify 호출 한도를 각각 지킨다.
- 실행 중인 변경 작업은 다른 작업이 선점하지 않는다. 관련 문서 변경은 각 단계 진입 전에 반영하고 다른 작업은 안전한 선택 경계에서 시작한다.
- 같은 사용자 상태 디렉터리에서는 자동 실행을 하나만 허용한다. 수동 준비는 계획·실제 자원의 소유권을 원자적으로 확인·예약해 병행할 수 있다. 로컬 복구 요청도 병행 접수하되 실제 복구는 전역 실행 잠금과 프로젝트 실행 자리를 준수한다.

**보존·검증·완료**

- 실패·중단·차단 시 작업물·session·진단을 보존한다. 원인과 안전 조건이 실제로 해소된 작업만 재개한다.
- BLOCKED 해제는 이전 writer 종료·현재 작업 대상의 연결·원인 해소와 안전 조건을 확인한 뒤 판정한다. 후보가 바뀌면 필요한 검증을 다시 수행하며 독립된 차단·승인은 유지한다. 상세 조건은 [계획 및 프로젝트 차단 해제](#계획-및-프로젝트-차단-해제), 과거 Git 오탐의 재평가는 [역할 결과와 Manager Git 전이](ARCHITECTURE.md#역할-결과와-manager-git-전이)를 따른다.
- 일반 자동 경로는 최종 후보의 관련 테스트·전체 검증·독립 Review가 성공해야 PR을 생성한다. 수동 제출의 verify·Review 생략은 [수동 명령의 준비와 수락](#수동-명령의-준비와-수락)에 정의한 명시적 사용자 면제 범위와 입력 일치 조건에 한한다.
- 실행 중인 작업의 MERGED 전이는 연결 PR의 실제 Merge를 근거로 한다. 작업 행이 유실된 경우의 문서 기반 복구는 [ExecPlan catalog와 실행 이력](#execplan-catalog와-실행-이력)을 따른다.
- 사용자 변경을 승인 없이 삭제하거나 덮어쓰지 않는다. 정리 안전성을 확인할 수 없는 worktree는 보존한다.
- 동일한 현재 PR·조치·복구 불가 사건의 알림 생성은 중복을 제거하고, 전송 성공이 확인된 조각은 다시 보내지 않는다. 전송 결과가 불확실한 경우의 재시도·중복 수신 가능성은 [Telegram 알림 조건](#telegram-알림-조건)을 따른다.

문서 입력·등록·작업 선택·실패·게시의 상세 정책은 아래 해당 절에서 정의한다. 저장·검증·Git 적용 방식은 [ARCHITECTURE.md](ARCHITECTURE.md), ExecPlan 형식은 [PLANS.md](PLANS.md), 설치와 실행은 [README.md](../README.md)를 따른다.

### 활성 변경 요구사항과 현재 동작

본문은 현재 Merge된 제품의 동작과 채택된 기준을 설명한다. 활성 Requirement는 이 기준에서 바꿀 목표와 보호 조건이며, 명시된 변경 범위에는 해당 목표를 적용하고 나머지 현재 계약은 유지한다. 해석 기준은 [Planner 입력과 변경 해석](#planner-입력과-변경-해석)을 따른다.

미구현 변경을 현재 동작으로 기록하지 않고, 실제 구현 변경과 함께 관련 설계를 갱신한다. 문서 작성·로컬 commit만으로 구현 완료로 취급하지 않는다. 개별 변경 목표와 Acceptance Criteria는 활성 Requirement에서 관리한다.

유효한 draft는 planning 오류와 구분하며 독립 작업을 막지 않는다. 다만 관련 Requirement archive와 프로젝트 완료를 허용하는 완료 상태도 아니다. 상세 조건은 [draft와 사용자 판단](#draft와-사용자-판단)을 따른다.

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
6. 완성된 문서는 대상 문서의 정해진 경로에 반영한다. Requirement는 `docs/requirements/` 바로 아래에 저장한다

작성 대상과 설계 갱신 시점은 [문서 역할과 갱신 책임](#문서-역할과-갱신-책임)을 따른다. Requirement 작성·갱신 중에는 PRODUCT·ARCHITECTURE를 현재 기준으로 참고하고 함께 수정하지 않는다.

### 프로그램 설정

시작 조건:

- 사용자가 최초 실행을 준비하거나 설정·프로젝트 등록을 변경한다

흐름:

1. 사용자가 설정 파일에 사용할 provider의 Model·Effort, 동시 실행 수와 선택적 Telegram 설정을 준비한다.
2. 최초 실행에서는 `config init`으로 설정을 검증하고 상태 DB를 생성한다.
3. `add`로 프로젝트를 등록하면서 provider와 기준 branch를 지정한다. provider 기본값은 Codex이며 기준 branch를 생략하면 `origin/HEAD`를 사용한다.
4. 프로그램이 프로젝트의 필수 조건·공통 파일과 원격 필수 문서를 검증하고, 통과한 프로젝트만 등록한다.
5. `config list`로 등록 결과를 확인한다. 등록 해제가 필요하면 별도 `remove` 명령을 사용한다.

설정 파일 준비와 실행 명령의 상세 사용법은 [운영 안내](OPERATIONS.md#설정)를 따른다.

규칙:

- `config init`은 상태 DB를 초기화한다. 등록 목록은 현재 `config list`로 조회하며, 등록·해제는 최상위 `add`·`remove`를 사용한다. `config add`·`config remove` 호환 별칭은 없다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 PLAN_BLOCKED ExecPlan, PROJECT_BLOCKED 레코드가 있거나 해결되지 않은 `planning_error`가 있는 프로젝트는 등록 해제할 수 없다
- 공급자 호출·인증·모델 조회와 격리 검증은 선택된 상태 루트의 `automation-codex`·`automation-claude`를 공급자별 공통 홈으로 직접 사용한다. 호출별 임시 폴더와 작업 권한은 별도로 관리한다. 외부 공급자 홈 지정과 부모 환경을 변경하지 않으며 새 전용 인증 파일은 기본 사용자 인증 파일만 심볼릭 링크로 공유한다. 기존 전용 파일은 보존하고 인증·링크 준비 실패는 해당 공급자의 오류로 처리한다. 개인 설정 전체를 가져오거나 기본 홈으로 fallback하지 않는다. 준비 CLI 조회가 공통 Codex `config.toml`을 안전하게 생성·수정·원자 교체하면 재검사 후 같은 호출의 지침과 최종 증거에 반영하며, 지침 설정이 바뀌면 기존 추가 지침도 새 설정 기준으로 다시 확인한다. Codex native developer 입력의 추가 도구 정의는 지침 메시지와 분리해 보존하고 전송 ID 차이만 허용하며, 실제 정의 변경이나 알 수 없는 형식은 호출 전에 거부한다. 마지막 지침 조회가 확인한 config와 증거 갱신 시점의 config가 달라지면 안전한 설정이어도 해당 호출을 거부한다. Manager·project/system 설정 변경, 검사 오류·충돌, 조회 사이 발견된 충돌과 최종 검사 뒤 변경은 실제 프로세스 시작 전에 거부한다
- Telegram token과 chat ID가 모두 비어 있으면 알림 기능을 비활성화하고 전송을 시도하지 않는다
- Telegram token과 chat ID 중 하나만 설정돼 있으면 설정 오류로 처리한다

실패:

- 선택한 AI Agent가 실행 불가하거나 인증돼있지 않다
- Model, Effort, 일부만 입력된 Telegram 설정 또는 `max_workers` 설정이 유효하지 않다
- 프로젝트가 등록 조건을 만족하지 않는다
- `docs/PLANS.md`가 Loop Manager 기준 파일과 일치하지 않는다

### ExecPlan 생성과 자동 처리

시작 조건은 미충족 PRODUCT·Requirement 또는 이어서 처리할 기존 작업이 있는 경우다.

프로젝트 전체 원격 branch 동기화는 목록 조회와 필요한 객체 수신을 일괄 처리해 branch별 네트워크 왕복을 반복하지 않는다. 일반 branch 삭제는 제한 재관측으로 처리하고 필수 base 부재·조회 실패와 구분한다. 사용자 checkout·다른 ref·태그와 기존 작업·중단 복구 기록은 보존한다.

1. 저장된 작업과 대기 PR을 확인한다. 안전 조건이 허용한 Merge·Close 반영을 먼저 수행하고, 최신 원격 입력과 catalog를 확인한다.
2. 진행 중 작업의 저장 단계와 확정 PR 충돌의 복구 조건을 확인한다. 명세 변경·유지보수·일반 Planner·READY 선택과 현재 작업 재개의 순서는 [작업 선택 정책](#작업-선택과-동시-실행)을 따른다.
3. [Planner 호출 조건](#planner-호출과-기존-작업-개정)이 있을 때 현재 명세·코드·compact catalog와 허용된 PR 전 계획 snapshot을 확인해 새 작업을 계획하거나 기존 계획을 조정한다. 호출 조건이 없으면 Planner를 거치지 않고 기존 작업을 계속하거나 실행 가능한 READY를 선택한다.
4. 계획을 작성·개정했다면 프로그램이 범위·형식·source·의존성을 검증하고 원격 base에 게시한다. 기존 계획 개정은 snapshot 선정 뒤 게시 직전까지 실제 연결 PR을 다시 확인한다. 그 사이 PR이 나타나면 해당 계획 개정을 게시하지 않고 Planner 결과·작업 공간·실행 이력을 보존해 최신 PR 경계에서 다시 판단한다. PR 이후 추가 범위는 새 ExecPlan 정책으로 인계하며, 한 계획의 PR 경합은 무관한 계획의 후보·실행 이력을 초기화하지 않는다. PR 조회 실패는 부재로 간주하지 않는다. 원격 게시를 확인한 결과만 실행 대상으로 반영한다.
5. 새 작업은 dependency를 충족한 READY 하나를 선택해 전용 branch·worktree에서 Worker를 실행한다. 재개 작업은 저장된 단계에 필요한 처리부터 이어간다. Worker는 구현·관련 테스트와 verify 스크립트를 준비하며 Requirement를 이동하지 않는다.
6. 프로그램이 Worker 결과를 검증하고 최신 base를 반영한 뒤 적용 가능한 검증 경로를 수행한다. 기본 경로는 전체 verify → 읽기 전용 Review이며, 일반 선행 경로는 부분 verify → 읽기 전용 선행 Review → 실제 전체 verify다. 실패는 같은 작업에서 교정하고 선행 PASS의 최종 승인 조건은 [부분 verify와 일반 선행 Review](#부분-verify와-일반-선행-review)를 따른다.
7. 게시 직전 base나 후보 근거가 바뀌었으면 필요한 교정·후보 확인·검증·Review를 다시 수행한 뒤 기존 PR을 복구하거나 새 PR을 생성한다.
8. 안정된 PR_OPEN은 실행 자리를 비운다. 해당 계획에 직접·간접 의존하지 않는 READY는 순차적으로 시작할 수 있고, 연결 PR의 Merge 확인 뒤 의존 작업이 시작된다.

일반·유지보수 Planner 작성과 충돌 교정 writer가 끝나면 다음 검증·교정·게시 전에 안전한 경계에서 원격 입력을 다시 확인한다. 관련 명세나 ExecPlan이 바뀌면 같은 계획 ID·작업물·session을 보존해 최신 입력으로 다시 평가하고, 무관한 문서 변경은 이미 끝난 결과와 단계를 유지한다. 현재 작업의 단계별 반영은 [ARCHITECTURE의 단계별 원격 문서 반영](ARCHITECTURE.md#단계별-원격-문서-반영)을 따른다.

#### 새 계획의 ID와 경로

게시 전 Manager는 새 계획 파일만 활성·archive·등록·취소·예약 ID를 피하는 연속 ID와 일치하는 경로로 정규화한다.

새 계획과 이번 batch에서 수정한 snapshot 계획의 `depends_on` 중 대상이 명확한 batch 참조만 함께 바꾸며 기존 계획 identity·본문·dependency 순서, 사용자 파일과 draft 질문·User Decision은 보존한다.

재배정되는 ID가 본문에 나타나거나 여러 새 목표가 같은 ID를 쓰거나 dependency가 기존 ID와 충돌 ID를 함께 가리키면 자동 선택하지 않고 Planner 또는 사용자 판단 절차에 남긴다.

#### draft와 사용자 판단

계획의 `draft: true`는 사용자 판단이 필요한 작성 준비도 선언이다. 유효한 draft는 독립 실행 가능 계획과 같은 batch로 게시·등록하며 질문·선택 영향·원격 링크·판단 revision을 보존한다. draft에는 Worker·workspace 예약·구현 branch·PR을 시작하지 않고, 직접·간접 의존 계획도 대기한다. 독립 READY·planning·PR 관측은 계속한다. 같은 근거와 질문을 새 ID로 반복 게시하지 않는다.

사용자는 원격 Requirement/ExecPlan 본문 또는 기존 User Decision의 Decision·Answer로 질문에 답한다. 현재 질문·근거와 답변을 유일하게 연결할 수 있으면 Revision 생략과 draft 표시 유지를 허용한다.

Planner는 이전 질문과 사용자 변경을 비교해 답변의 의미·충분성과 남은 질문을 판단하고 선택을 본문에 통합한다. 사용자 변경 근거는 문맥 diff와 출처·revision으로 전달하며, catalog에 변경 전후 문서 전문이나 같은 근거를 반복하지 않는다.

Manager는 저장된 원문과 변경 출처·원격 revision·현재 근거·무소유 상태를 검증하고 해결된 질문·입력 양식만 정리한다. 전부 해소되면 draft를 해제하며, 원격 게시 확인과 같은 DB transaction에서 대기를 해제하고 사용자 입력·결정 근거를 감사로 보존한다.

일부 해소는 남은 질문·draft와 미소비 사용자 입력을 유지한다. 답변을 질문별 인용 줄로 분리할 수 있을 때만 해당 줄을 소비하며 공유 답변은 보존한다. 거절·보류·불충분·상충·출처 불확실은 임의 승인하지 않는다. Agent의 추론·수정과 표시만 제거한 변경은 사용자 결정이 아니며 기존 dependency·소유권 gate도 유지한다. 문서 수정 draft 승인은 별도 계약이다.

양식 정리 뒤 목표·source 입력이 같으면 기존 결정을 유지하며 Progress·Decision Log 변경만으로 재승인을 요구하지 않는다. 판단 근거가 바뀐 미착수·무소유 계획은 실행을 보류하고 Planner가 기존 선택의 유효성을 재검토한다. 유효하면 이유를 본문 감사에 남기고 원격 게시 확인 뒤 대기를 해제하며, 새 사용자 판단이 필요하면 해당 계획에 필요한 새 질문을 남긴다.

자동 재평가는 사용자 질문 알림·목록·건수에서 제외하며 다른 실제 질문의 목록은 유지한다. 여러 draft가 공유하는 동일 사용자 변경은 Planner 입력에 한 번만 전달하고 질문별 출처·원문 연결은 보존한다.

#### Requirement 변경과 작업 취소

Requirement의 추가·변경은 실제 미충족 범위와 관련 기존 작업을 비교해 새 작업 또는 허용된 개정으로 반영한다. 실제 source 삭제는 archive·rename·조회 실패와 구별하고 Planner가 전체 철회·공유 목표 유지·불확실을 판단한다. 일부 Acceptance Criteria의 삭제는 일반 개정으로 처리하고 해당 조건에만 필요한 항목을 제거하며 다른 근거의 목표·공유 구현·필수 검증은 보존한다. 복수 Requirement가 삭제된 작업은 모든 삭제 source의 판단이 전체 철회로 일치할 때만 취소할 수 있다. 판단 누락·상충·불확실, 변경된 삭제 원문·revision과 대상 밖 판단으로 전체 철회를 확정하지 않는다. 사용자 판단 대기와 수동 소유·준비 intent는 철회로 우회하지 않는다.

공유 작업은 남은 목표·검증·참조를 보존하고 필요한 선행 목표를 단순 dependency 제거로 우회하지 않는다. 삭제 목표 복구 대상은 기존 직접·간접 dependency 경로의 선행 보류를 유지한 채 목표별 판단을 검증해 개정안을 게시할 수 있다. 등록된 선행 작업의 실행 보류만으로 유효한 신규·허용 개정의 직접·간접 dependency를 거부하지 않는다. 다만 이번 batch의 신규·개정 선언을 함께 반영한 관계에서 변경 계획이 직접·간접 순환에 참여하면 planning 결과 검증에서 거부한다. 유지보수 게시 재검증은 같은 graph 판단을 반복하고, 일반 게시 후보는 검증한 선언의 bytes와 planning snapshot이 intent/CAS에 일치할 때만 반영한다. 변경 계획과 무관한 기존 보류 순환이나 보류 선행에 단순히 의존하는 관계는 그 사유만으로 거부하지 않는다. 게시 성공으로 선행 계획과 그 의존 범위의 Worker 실행 보류를 해제하지 않으며, 선행 보류 해제 뒤에도 실제 Merge 전에는 실행하지 않는다.

검증된 작업 취소는 활성 DB row 제거·ID 재사용 방지·원문과 실행 이력 보존을 함께 적용한다. 취소·예약 row가 없어도 확인 가능한 삭제 revision·identity·원문·현재 참조 영향을 감사로 보존하며, 확인할 수 없는 근거는 미확인으로 남긴다.

등록 전 검증 실패로 실행 row 없이 hold만 남은 계획도 원격 삭제 시 원문·hold revision·최초 관측 revision·미확인 사유를 보존한다. 후속 참조가 나타나면 그 영향과 함께 Planner 복구 목표로 전달한다. 과거 archive의 source 부재는 활성 선언 오류나 독립 작업의 차단이 되지 않으며, 검증된 MERGED 이력은 실행 row·문서가 없어도 dependency 충족으로 유지한다. 과거 ID의 활성 경로 재등장은 예약 충돌로 거부한다.

진행 중 writer·PR·소유권이 불확실한 작업은 보류하며 독립 작업은 계속한다. 이미 Merge된 기능의 되돌림은 새 변경 요구로 다룬다.

작성 기준의 원본은 Requirement의 Specification Skill과 ExecPlan의 [PLANS.md](PLANS.md#execplan-본문-작성)다. 실제 조사한 파일·작업 체크리스트와 작업 고유 테스트를 제시하고, 정상·실패·완료 조건을 보존하며 기존 문서를 양식만으로 일괄 재작성하지 않는다.

계획 게시의 원자 적용, 작업 공간 복구와 단계별 Git 처리는 [ARCHITECTURE.md](ARCHITECTURE.md#계획-게시와-작업-공간)를 따른다.

### 실패와 재시도

실패 종류별로 집계 단위와 한도를 구분한다. 각 한도는 서로의 횟수를 대신하지 않는다.

| 대상 | 집계 기준 | 한도 도달 시 처리 |
|---|---|---|
| Planner 결과 오류 | 같은 입력의 결과를 최대 3회 교정 | `planning_error`와 결과를 보존하고 동일 실패 입력의 재호출 중단 |
| Worker targeted test | 같은 테스트의 실제 실행이 연속 3회 실패 | 해당 계획 차단 |
| 전체 verify | 같은 지문이 최초 실패 후 3회 재발(총 4회 관측). 연속 여부 무관 | 해당 계획 차단 |
| Review FAIL에 따른 Worker 교정 | 계획별 실제 교정 시작 3회씩 묶음 | 추가 교정은 재시도 대기 후 같은 실행에서 재개 |
| Agent CLI 일시 실패 | 한 호출 요청에서 최초 실행 외 최대 3회 재시도 | 상태를 보존하고 작업별 재시도 대기 후 같은 실행에서 재개 |

#### Planner 결과 검증 실패

최초 작성·교정 writer가 끝난 직후 최신 원격 명세·ExecPlan을 반영한다. 관련 입력이 바뀌었다면 이전 입력의 결과를 검증·게시하거나 그 결과의 형식·내용 실패로 새 입력의 교정 횟수와 `planning_error`를 먼저 확정하지 않는다. 이전 결과의 교정 횟수는 새 입력에 합산하지 않으며, 그 결과와 진단은 보존하고 독립 실패 원인과 실행 이력도 유지한다.

1. 변경 범위 또는 ExecPlan 형식 오류를 같은 Planner 세션에 전달한다
2. 최대 3회까지 수정과 재검증을 수행한다
3. 계속 실패하면 계획 입력 지문과 원인을 `planning_error`에 저장하고 planning worktree를 보존한다
4. 같은 계획 입력 지문에서는 Planner를 다시 실행하지 않는다
5. 원격 입력이 전진하면 이전 planning worktree·검증 기록·세션을 보존해 반영한다. 오류 원인이 실제로 해소됐는지 재검증하기 전에는 `planning_error`를 지우지 않는다

#### ExecPlan 게시 실패

1. 계획 commit을 push하기 전에 `origin/<base-branch>`가 전진하면 미게시 후보를 보존해 관리 worktree에서 rebase하고 최신 catalog·SQLite 권한과 결과를 재검증한다
2. fast-forward push가 거부되면 계획 commit과 오류를 보존하고 다음 실행에서 다시 시도한다
3. push 성공을 원격에서 확인하기 전에는 ExecPlan을 READY로 등록하거나 Worker를 시작하지 않는다
4. 일반·유지보수 batch의 prepared intent와 planning worktree는 push·SQLite 적용 실패 뒤에도 보존한다. 원격에 batch가 있으면 원자 적용을 재개하고, 없으면 보존 후보를 최신 base로 옮겨 재검증한다

#### Worker targeted unit test 실패

- 같은 작업의 동일 테스트가 실제 실행에서 연속 3회 실패하면 정상 Worker 경계에서 해당 ExecPlan을 차단한다. Worker 완료 보고의 교정 횟수는 이 기준에 사용하지 않는다. 세 번째 실패에 도달한 사실은 같은 turn의 후속 성공으로 취소하지 않으며 차단 확정 시 도달 표식을 소비한다.
- 테스트 ID별로 집계하며 다른 테스트의 실행은 서로의 횟수에 영향을 주지 않는다. 확인된 본문 실패·오류와 unittest의 unexpected success는 실패로 누적한다. 해당 테스트의 성공·skip·예상된 실패는 연속 실패를 초기화한다. fixture-only 오류와 실패 여부가 불명확한 결과는 실패 횟수와 기존 이력에 영향을 주지 않는다. 후보 코드·commit·Agent 세션 변화만으로 횟수를 초기화하지 않는다.
- 같은 실행 결과는 한 번만 세고 Worker 세션 내부 재실행·CLI 재시도·Manager 재시작 뒤에도 기록을 보존한다. 호출에 결합한 완료 결과가 파일에 남으면 Manager가 중단돼도 writer 종료 확인 뒤 다시 수집한다. 재수집으로 세 번째 실패가 확인되면 새 Worker·CLI 재시도·verify 전에 해당 작업을 차단한다.
- 실행·수집 중단, 부분 기록과 근거 부재는 실패 횟수에 넣지 않는다. 기록의 귀속·경로·writer 종료가 불명확하거나 수집·저장이 실패하면 원문과 재수집 근거를 보존하고 해당 작업만 일시 중단한다. 살아 있는 writer의 자료·소유 상태를 임의 정리하지 않으며 다른 작업은 계속 진행할 수 있다. 실패 보고에 새로운 실행 근거가 없으면 실제 실패 streak를 늘리지 않고 작업별 재시도 대기 뒤 같은 작업·세션에서 실제 재검증한다. 수집 대기는 기존 자료를 먼저 재수집하며 writer 종료가 불명확하면 새 Worker를 시작하지 않는다.
- 테스트 실패 외의 문서·완료 보고 오류는 기존 교정 한도를 유지한다. 전체 verify의 실패 지문 및 Review 정책도 별도로 유지한다.

#### 전체 verify 실패

실패 내용은 같은 Worker 세션에 전달하고 성공 출력은 전달하지 않는다. verify와 Review의 출력을 결정적으로 정규화해 단계별 실패 지문·발생 횟수를 SQLite에 별도로 저장한다.

- 새로운 verify 지문이 나타나는 동안에는 총 실패 횟수와 관계없이 Worker 수정과 전체 검증을 계속한다.
- 같은 verify 지문이 최초 실패 후 Worker 수정을 거쳐 세 번 다시 발생하면, 즉 총 네 번 관측되면 구조화된 원인·지문을 기록하고 `PLAN_BLOCKED`로 변경한다. 연속 실패일 필요는 없으며 중간에 다른 지문이 발생해도 이전 이력을 유지한다.
- 전체 검증 성공은 verify 지문 이력만 초기화한다.

#### Review 교정과 level 승격

Reviewer의 blocking issue는 같은 Worker 세션에 전달하고 Reviewer도 저장된 세션을 재사용한다. ISSUE_ID와 진단·해결 이력은 피드백·감사에 보존하되, 발생 횟수만으로 최종 차단하지 않는다.

계획별 Review FAIL에 따른 Worker 교정은 실제 시작 세 번씩 묶음으로 배정한다. 복수 issue는 한 교정으로 세며 CLI 재시도·중단된 동일 turn의 복구는 중복 집계하지 않는다. 세 번째 교정의 verify·Review가 성공하면 게시를 계속하고, 추가 교정은 phase·작업물·세션·피드백과 누적 감사를 보존한 재시도 대기 뒤 같은 실행에서 시작한다. runtime·실행 ID 변경으로 묶음 대기를 우회하지 않으며 이 대기는 BLOCKED·차단 알림을 만들지 않는다.

level 승격은 다음 기준을 적용한다.

| 판단 | 기준 |
|---|---|
| 기준선 | 각 effective level의 첫 유효 Review FAIL. 교정 결과 횟수에는 포함하지 않는다. |
| 집계 대상 | Review 피드백에 대한 Worker 교정 완료·Manager 검증 통과·유효 Review FAIL이 결합된 결과. 복수 issue는 한 결과다. |
| Manager 검증 | 기본 경로는 전체 verify, 일반 선행 경로는 현재 후보의 부분 verify. 부분 후보 연결은 전체 검증 완료 증거와 구분한다. |
| 승격 시점 | 기준선의 같은 ISSUE_ID가 두 교정 결과에 다시 나타나거나, issue가 달라도 교정 결과가 세 번 연속 FAIL일 때 다음 Worker 호출부터 한 단계 올린다. |
| 상한·새 기준선 | level 8이 상한이며 새 level에서는 기준선을 다시 시작한다. |
| 집계 제외 | 교정 없는 Review 재실행, CLI·Reviewer 프로토콜 재시도, 중단된 동일 Worker turn 재개, 전체 verify 실패, Worker 결과 형식 오류, Reviewer 실행 오류·유효하지 않은 Review. 진행 중 집계도 초기화하지 않는다. |
| 재시작·중복 방지 | 기준선·미완료 교정·검증 후보 identity·effective level을 SQLite에 보존한다. |
| 유효 Review PASS | Review 지문 이력·진행 중 교정 집계는 끝내고 effective level·과거 level별 승격·실패 감사는 유지한다. |

#### 결과 형식 교정과 과거 차단 복구

Worker·Reviewer·Planner·모듈 검토 결과의 명확한 marker 배치·구분자·공백 차이는 Manager가 처리한다. 검증된 호출 입력의 identity만 결합하며 형식 처리만으로 추가 모델 호출·실패 집계·level 승격을 만들지 않는다. 상충 verdict·명시적 identity 불일치와 필수 판단·issue·원본 인용·검증 근거 누락은 기존 역할의 교정 대상으로 남긴다. 빈 원본·공백 chunk의 확정 사실은 재사용하되 내용이 있는 모듈의 의미 검토와 불확실한 관계의 보수적 처리를 유지한다.

과거 `review_retry_exhausted` 차단은 저장된 원인과 현재 작업 공간·branch·원격 OID·후보 유효성·PR·rebase 상태를 재확인한다. Review 횟수 외 유효한 차단이 없고 소유권이 확인되면 기존 세션·실패 피드백·감사를 보존한 채 같은 교정 경로로 재개한다. 안전 근거가 부족하거나 별도 차단 원인이 있으면 그 상태를 유지한다.

PR 없는 게시 checkpoint의 과거 Review 횟수 차단은 현재 repository·origin·작업·branch·worktree와 실제 후보를 확인해 해당 원인만 해제한다. 과거 게시 이력의 부재나 OID 차이를 읽기·통합의 거부 근거로 사용하지 않는다. 수행하지 않은 rebase·게시·검증의 성공을 만들지 않고 남은 Review issue와 독립 차단을 보존한다.

게시 OID가 남았지만 재개 표식이 없는 작업도 동일 계획·branch·관리 worktree와 현재 게시 identity·tree·계보·실제 원격 lease·PR 상태를 재검증해 안전하게 재개한다. 현재 checkpoint 요청이 local-only여도 충분한 과거 게시 영수증은 읽기 전용으로 사용할 수 있다. 새 원인의 차단은 낡은 재개 표식을 무효화하되 게시 이력을 보존한다. 게시 소유권 오탐만 해제하며 미해결 Worker 실패와 별도 차단·승인·Agent 위반은 유지한다. OID 삭제나 로컬·원격 HEAD 일치만으로 검사를 우회하지 않는다.

#### 외부 CLI 실패

- 저장된 Agent 세션을 찾지 못하거나 재개할 수 없으면 같은 작업 공간과 작업 문맥을 보존해 새 세션으로 한 번 이어간다. 이 전환은 일시 실패 재시도와 별도로 집계한다. 승인·격리 정책 실패와 AI 사용 한도 소진은 새 세션으로 우회하지 않는다
- Agent CLI 실행 실패·timeout·일시적 외부 오류는 한 호출 요청에서 최초 실행 외 최대 3회 재시도한다
- 추가 3회 재시도 후에도 실패하면 해당 작업의 상태·작업 공간·세션을 보존하고 재시도 대기 후 같은 실행에서 저장 단계부터 다시 시도한다. 대기는 실제 Agent·verify 실행 자리를 점유하지 않는다
- AI 사용 한도 소진은 별도의 조건 대기이며 새 세션·provider 전환이나 유료 호출 probe로 확인하지 않는다. 같은 provider·model의 독립된 실제 성공 근거를 확인한 뒤에만 같은 실행에서 재개한다

### 계획 및 프로젝트 차단 해제

| 차단·변경 조건 | 허용되는 처리 |
|---|---|
| PLAN_BLOCKED | 해당 계획의 입력·사용자 결정·같은 PR reopen 조건만 재평가한다. 독립 READY는 계속 선택할 수 있다. |
| PROJECT_BLOCKED | 일반 Agent·Git 변경을 멈춘다. PR 상태·계획별 원격 관측·알림은 계속하며, 실행 자리와 현재 범위가 검증된 연결 계획만 복구·보존할 수 있다. |
| 안전한 원격 후속 commit 또는 base 전진 | 기존 branch·worktree·session과 로컬 작업을 보존해 반영하고 원래 차단 원인을 다시 검사한다. 새 OID나 checkpoint 게시만으로 해제하지 않는다. |
| 삭제·재작성·경쟁·소유권 불명확 또는 미해결 충돌 | 원격 ref를 덮지 않고 차단·작업물과 실제 미게시 이유를 보존한다. |

1. 최종 차단 상태와 원인을 먼저 보존한다. 검증 가능한 대상 작업은 안전한 원격 checkpoint 또는 별도 진단 branch에 보존하고 결과를 알린다. 기존 PR은 저장된 URL·Open 상태·작업 branch·정확한 원격 head를 재확인해 같은 branch에 게시하며 새 PR·Draft PR을 만들지 않는다.
2. 저장된 원인과 현재 입력·작업 상태를 대조한다. 같은 입력의 실패를 반복하지 않으며 다른 계획·planning·사용자 결정의 차단을 함께 해제하지 않는다.
3. 해당 원인의 해제 조건이 충족되면 아래 구분에 따라 저장 단계 재개 또는 차단 해제를 적용한다. 관련 없는 차단·사용자 결정과 과거 감사는 유지한다.
4. 후보가 바뀐 작업은 유효한 Worker·targeted test·전체 verify·Review 근거를 다시 확인한다. 아래의 충돌 없는 rebase·동일 verify 입력 예외 외에는 필요한 단계를 다시 수행한다. 첫 PR도 최종 검증과 Review 성공 뒤에만 생성한다.

| 재개·해제 사유 | 이어갈 상태와 집계 처리 |
|---|---|
| 일시 오류·Review 교정의 실행별 한도·프로세스 중단 | 현재 대상·입력·안전 조건을 재확인하고 저장 단계에서 재개한다. 작업물·session·실패·level 이력을 유지하며 자동으로 차단을 만들지 않는다. |
| 일반 PLAN_BLOCKED의 입력 변경·사용자 승인·같은 PR reopen | 원인 해소를 확인한 뒤 READY 또는 PR_OPEN으로 되돌린다. 해당 계획의 재시도·verify/Review 실패 지문 집계·Worker level·승격 집계는 최초 기준으로 초기화하고 과거 감사는 보존한다. |
| 원격 입력 통합·관리 rebase·과거 오탐의 복구 | 현재 상태로 해당 원인만 재평가한다. 기존 branch·worktree·session·실패·승격 감사를 보존하고 변경된 후보에 필요한 검증부터 이어간다. |
| 원격 작업 branch 삭제로 중단된 자동 작업 | 자동 재개하지 않는다. 아래의 명시적 `reset`으로 이전 작업·감사를 보존하고 새 실행을 준비한다. |

작업 branch의 현재 입력 수용과 게시 완료 확인은 별도 판단이다. 로컬·원격만의 commit과 미커밋 작업을 보존해 통합하고, 같은 PR의 head 전진은 새 원격 입력으로 처리한다. 과거 게시 영수증은 미완료 push 확인과 감사에 사용하며 수용 허가증으로 요구하지 않는다. 원격·PR 관측 경쟁과 정확한 lease 거부는 제한 재관측·통합·재검증으로 돌아가며 독립 작업은 계속한다. 실제 대상·권한·진행 중 writer의 문제는 해당 작업의 동작을 보류한다.

Manager가 생성·게시한 원격 작업 branch가 PR Merge 전에 실제 삭제되면 해당 작업을 중단하고 명시적 재시작을 기다린다. 조회 실패와 PR Merge 후 정리는 삭제 중단과 구분한다. 일반 run·프로세스 재시작·pause 해제·같은 ref 재생성으로 재개하지 않는다. `reset <repo> <id>`는 삭제로 중단된 자동 작업에 한해 이전 worktree·session·PR·실패·게시 감사를 보존하고 최신 원본의 새 실행을 READY로 준비한다. 이전 성공 근거와 게시 intent는 새 실행에 재사용하지 않는다.

반복 교정으로 해결하지 못한 충돌은 확인한 원본 revision에서 해당 파일과 index entry만 복원하고 재작업한다. 다른 파일의 내용·staging·미추적 작업과 현재 rebase target을 유지한다. 복원 전 확보할 수 있는 원형과 실패 사유를 보존하고, 중단 뒤 이미 복원한 파일이나 새 교정을 덮어쓰지 않는다. 후보 OID 변경은 필요한 verify·Review를 선택하는 입력이며 실제 명세 변경·구현 부족만 Worker 교정으로 연결한다. 진단 ref 충돌은 해당 자료 게시·삭제만 보류한다.

저장된 충돌 교정 READY와 stage/continue 대기는 교정에 실제 전달한 구현 조건을 최신 조건과 대조한 뒤 소비한다. 파일 형상이 같아도 조건 충족 근거가 바뀌거나 없으면 이전 결과·교정 이력과 작업물·session·기존 target을 보존하고 같은 Worker의 재확인으로 연결한다. 유효한 입력의 완료 교정은 Worker를 반복하지 않고 필요한 전체 verify·Review로 이어가며, OID 변화나 순수 수행 기록만으로 구현 교정을 다시 요구하지 않는다. continue 뒤 다음 commit에서 충돌하면 이전 교정 승인은 종료하고 같은 session에서 새 충돌을 교정한다. 이전 READY를 반복 소비해 교정 실패로 집계하지 않는다. 실제 결과 오류·사용자 결정·dependency와 독립 차단은 기존 경계를 유지한다.

현재 상태 기반 rebase 판정, checkpoint lease, 진단 ref와 중단 복구의 상세 조건은 [ARCHITECTURE.md](ARCHITECTURE.md#차단-작업의-원격-반영과-보존)를 따른다.

충돌 진단 ref는 원인별 해소 확정, 보존 작업물을 포함한 해결 결과의 정상 작업 branch 원격 반영, 모든 연결 사용의 종료와 원문의 독립적인 로컬 복구 가능성을 확인해 정리 준비 여부를 기록한다. PR Merge나 무관한 차단의 해제를 추가 조건으로 요구하지 않으며 작업 상태·로컬 충돌 해소·ref prefix만으로 자격을 인정하지 않는다. 원격·소유권·자료가 불확실하거나 관측 중 변경되면 해당 준비만 보류하고 작업 진행·기존 이력·알림은 유지한다. 충분한 기록이 있는 과거 ref에도 같은 조건을 적용하며 planning 실패·충돌 외 원인은 대상에서 제외한다. 현재 구현은 준비·보류 근거를 남기고 원격 ref를 삭제하지 않는다. 실제 삭제는 [R-083](requirements/%5BR-083%5D%20충돌%20해결%20후%20원격%20진단%20브랜치%20정리.md)의 후속 경계다.

### 사용자 승인 및 완료

1. 사용자가 PR을 검토하고 Merge한다. Merge 없이 닫힌 PR은 해당 계획만 차단하며 같은 PR을 다시 열어야 한다.
2. 프로그램이 실제 Merge를 확인해 MERGED와 알림을 기록하고 원격 catalog를 반영한다. 관리 worktree는 [Merge 후 정리 조건](#pr-생성과-merge-후-작업-공간)을 만족할 때만 제거한다.
3. 최신 원격 base에서 안전한 MERGED ExecPlan을 내용 보존 archive로 게시한다. 정리 실패는 작업 완료를 되돌리거나 다음 작업을 막지 않는다.
4. 마지막 연결 작업의 Merge와 catalog 반영으로 완료 확인 조건이 충족된 활성 Requirement는 다음 안전한 경계에서 전체 충족을 재확인한다. 독립된 새 작업·유지보수 cycle 선택보다 먼저 처리하며, 판단 결과와 archive는 [Requirement 완료 확인과 archive](#requirement-완료-확인과-archive)를 따른다. 원격 게시로 확인된 이동만 활성 입력에서 제외한다.
5. 기존 진행 작업·다음 의존 작업을 이어가거나, 호출 조건에 따라 Planner의 추가 작업 없음과 [프로젝트 완료 gate](#프로젝트-완료-조건)를 확인한다.
6. 모든 완료 조건이 충족되면 프로젝트 완료 이벤트를 전송한다.

### 실행 종료 및 재개

1. 같은 사용자 상태 디렉터리의 다른 Loop Manager가 실행 중이면 상태를 바꾸지 않고 종료한다
2. 각 프로젝트의 PR, worktree와 저장된 실행 상태를 외부 상태와 대조한다
3. 기본 worktree의 dirty, staged, untracked, ahead, diverged, detached 또는 다른 branch 상태는 원격 SHA 기반 작업을 막지 않는다. 안전한 fast-forward·로컬 commit 보존 rebase 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
4. 프로젝트별 관리 대상을 `max_tasks_per_project`로, 전체 구현·Review 호출을 `max_workers`로, verify 호출을 `verify_concurrency`로 제한한다
5. 처리 중인 상태, 세션과 작업 공간을 SQLite에 저장한다
6. 일시 중단된 작업은 현재 대상·입력·안전 조건을 재확인한 뒤 저장된 단계에서 재개한다. PLAN_BLOCKED·PROJECT_BLOCKED와 원격 작업 branch 삭제로 중단된 작업은 [원인별 해제·재시작 조건](#계획-및-프로젝트-차단-해제)을 먼저 충족해야 한다

같은 운영 DB를 사용하는 작업·프로젝트와 운영 명령은 짧은 상태 조회·확정을 순서대로 처리하며 경합 시 기본적으로 기다린다. DB 사용 순번은 Agent·verify의 실제 실행이나 교정 실패로 계산하지 않고, 접근 제한·취소 때 해당 요청의 상태를 보존한다. 외부 실행·원격 응답 대기는 DB 접근을 점유하지 않으며 읽기 전용 조회와 독립 작업의 진행을 유지한다.

같은 연결의 중첩 처리는 바깥 transaction의 소유권을 유지한다. 재시도 대상으로 명시한 짧은 DB 처리는 일시 잠금 때 전체를 rollback하고 현재 상태·소유권·generation을 다시 확인하며, 이미 수행한 외부 효과는 반복하지 않는다. 재시도와 차례 재획득은 하나의 요청 제한 시간을 공유하고 먼저 기다린 요청 뒤에 참여한다. 한도 소진은 상태를 보존한 DB 대기 종료로 전달하며, 다음 준비·배정이나 최종 차단은 호출자가 실제 조건을 확인해 결정한다.

자동 작업의 공유 Git 자원 경합은 실패 횟수를 늘리지 않고 같은 실행에서 자원을 기다린다. 수동 명령의 실제 자원 충돌 거부는 유지한다. 작업 차단은 현재 선택한 계획의 단계·지문·결과만 사용하며 다른 작업의 대표 투영을 차단 대상으로 대신 사용하지 않는다.

관리 Agent의 실제 호출은 사용할 provider 환경에서 인증·모델·설정·권한을 확인한다. 등록 사전 조회의 성공만으로 실제 실행 환경의 성공을 인정하지 않으며, 부모 환경과 사용자 인증 원본을 변경하지 않는다. 호출 승인 비교에 필요한 증거는 해당 실제 호출의 지침·환경·정책과 완료 사실에 결합한다. 확인되지 않은 현재 환경이나 과거 호출은 재사용하지 않는다.

## States and Transitions

ExecPlan 실행 상태는 파일이 아니라 SQLite에만 저장한다. 주요 전이는 다음과 같다.

```text
READY → IMPLEMENTING → VERIFYING → REVIEWING → PUBLISHING → PR_OPEN → MERGED
REVIEWING → VERIFYING (선행 Review PASS 또는 검증 보완)
VERIFYING 또는 REVIEWING → IMPLEMENTING (구현 교정)
READY, IMPLEMENTING, VERIFYING, REVIEWING 또는 PUBLISHING → PLAN_BLOCKED
PLAN_BLOCKED → READY (일반적인 계획별 해제 조건 충족)
PR_OPEN → PLAN_BLOCKED (Merge 없이 PR 종료)
PLAN_BLOCKED → PR_OPEN (같은 PR을 사용자가 다시 열음)
```

| 상태 | 의미 |
|---|---|
| READY | 유효한 ExecPlan이 실행 순서와 `depends_on` 충족을 대기 |
| IMPLEMENTING | Worker가 구현 또는 실패 수정 중 |
| VERIFYING | 프로그램이 부분·전체 검증을 수행하거나 기존 성공 근거의 재사용 조건을 확인 중 |
| REVIEWING | 독립적인 선행·최종 Review 또는 기존 승인 재사용 조건을 확인 중 |
| PUBLISHING | 승인된 후보를 작업 branch에 게시하고 PR을 생성·갱신 중 |
| PR_OPEN | PR 생성 후 사용자 Merge 대기 |
| MERGED | 완료가 확정된 최종 상태. 실행 중 작업은 연결 PR의 실제 Merge가 근거 |
| PLAN_BLOCKED | 해당 계획의 사용자 결정·입력 변경·검증 한도·PR reopen 대기 |

VERIFYING·REVIEWING 진입이 항상 새 검증 프로세스나 Reviewer 호출을 뜻하지는 않는다. 일반 선행 경로는 부분 verify → 선행 Review → 전체 verify를 거친 뒤 REVIEWING에서 기존 승인 재사용 조건을 확인한다. 근거가 없거나 입력이 달라졌으면 필요한 검증·Review를 수행한다. 상세 조건은 [부분 verify와 일반 선행 Review](#부분-verify와-일반-선행-review)를 따른다.

실행 상태와 실행 자격은 별도로 판단한다. READY라도 다음 조건을 충족하지 못하면 Worker를 시작하지 않는다.

| 조건 | 시작 제한 |
|---|---|
| 문서 준비도·사용자 판단 | `draft: true` 또는 미해결 사용자 판단이 있으면 대기한다. draft는 별도 ExecPlan 실행 상태가 아니다. |
| dependency | 모든 선행 ExecPlan이 MERGED여야 한다. |
| 실행 자리·소유권 | 프로젝트별 관리 한도를 넘거나 충돌하는 수동 소유·예약이 있으면 시작하지 않는다. |
| 프로젝트·선택 범위 | 프로젝트 안전 gate와 현재 유지보수 cycle 등 작업 선택 조건을 통과해야 한다. |

`PROJECT_BLOCKED`는 ExecPlan 상태가 아니라 프로젝트 안전 gate다. 현재 단계와 작업 공간을 보존하고 일반 Planner·유지보수 Planner·Worker·Git 변경을 멈춘다. PR 상태·원격 관측·알림과 검증 가능한 연결 계획의 복구·보존 예외는 위 차단 정책을 따른다.

유지보수 cycle은 ExecPlan 상태와 별도로 프로젝트별 SQLite에 보존한다.

```text
IDLE → ASSESSMENT → IDLE (확정 계획 없음)
ASSESSMENT → PREREQUISITE_WAIT → IDLE (확정 범위 Merge)
ASSESSMENT → REFACTORING → IDLE (확정 범위 Merge)
```

| 유지보수 상태 | 의미·종료 조건 |
|---|---|
| IDLE | 진행 중 cycle이 없으며 다음 평가의 시작 조건을 기다림 |
| ASSESSMENT | 필요한 평가 범위를 검토하고 작업·제외 근거를 확정. 확정 계획이 없으면 검증·필요한 게시를 마친 뒤 IDLE로 복귀 |
| PREREQUISITE_WAIT | 확정한 선행 작업과 필요한 dependency의 실제 Merge를 기다림 |
| REFACTORING | 확정한 리팩터링 등 개선 작업과 필요한 dependency의 실제 Merge를 진행·대기 |
| POST_REFACTOR_REVALIDATION | 과거 cycle의 호환·복구용 상태. 저장 결과·게시를 복구하고 확정 범위 Merge를 확인한 뒤 새 평가 없이 종료 |

활성 작업은 cycle 시작보다 우선하며, 각 cycle 전이는 기록된 계획 집합의 Merge 또는 검증된 Planner 결과로만 진행한다. 작업이 확정된 cycle은 필수 dependency를 포함한 전체 범위의 실제 Merge와 필요한 원격 관측·게시·catalog 반영을 확인한 뒤 IDLE로 돌아간다. PREREQUISITE_WAIT와 REFACTORING은 확정한 작업의 구성에 따라 선택하며, 둘 사이의 순차 전이를 필수로 요구하지 않는다. 선택·종료의 상세 기준은 [유지보수 평가](#유지보수-평가)를 따른다.

외부 CLI의 일시적 실패와 사용 한도 소진은 별도 ExecPlan 상태를 만들지 않고 현재 상태를 보존한다.

`planning_error`, `cleanup_pending`과 ExecPlan archive pending은 ExecPlan 상태가 아니다. `planning_error`는 해당 프로젝트의 새 계획과 완료 처리를 막지만, `cleanup_pending`과 archive pending은 이미 Merge된 작업의 보존·재시도 정보이므로 다음 ExecPlan 실행과 프로젝트 완료를 막지 않는다.

## Business Rules

### PRODUCT, Requirement와 ExecPlan

#### 문서 역할과 갱신 책임

| 문서 | 기록하는 기준 |
|---|---|
| `PRODUCT.md` | 현재 Merge된 제품 동작·정책·경계·장기 규칙 |
| `ARCHITECTURE.md` | 현재 채택한 구조·책임·데이터 형식·호환 계약 |
| Requirement | 현재 기준에서 바꿀 결과·이유·Acceptance Criteria. 구현 상세나 실행 상태를 넣지 않는다. |
| ExecPlan | Requirement·PRODUCT에 근거한 구현·검증 작업. `sources`에 근거 경로를, `depends_on`에 선행 ExecPlan ID 목록을 둔다. 모든 선행 상태가 MERGED여야 Worker를 시작한다. |

Requirement 작성·갱신 단계에서는 PRODUCT·ARCHITECTURE를 현재 기준으로 읽고 함께 수정하지 않는다. 구현으로 바뀐 제품 동작·장기 규칙과 설계·책임·계약은 Worker가 코드와 같은 PR에서 해당 설계 문서에 반영한다. 현재 작업에 필요한 관련 절만 갱신하며 목표·필수 검증·별도 사용자 결정을 유지한다.

#### Requirement 식별과 source 호환

새 Requirement에는 frontmatter·상태·complexity·risk를 두지 않는다. 생성 직전에 Specification Skill은 프로젝트 루트에서 `python .agents/skills/specification-docs/scripts/next_requirement_id.py`를 실행해 출력된 ID를 사용한다.

번호 조회는 활성·archive 문서의 원본 ID 중 최댓값 다음 후보를 반환한다. 중복과 번호 공백을 허용하고 중간 번호를 채우지 않으며, 파일 수정이나 번호 예약은 하지 않는다. 문서가 하나도 없으면 R-001을 반환하고 archive 디렉터리 부재는 빈 집합으로 취급한다. 조회 오류가 있으면 유효한 ID 없이 실패한다.

기존 문서의 ID·제목은 유효한 `id`·`title` frontmatter를 우선하고, frontmatter가 없으면 첫 `# [R-ID] 제목`으로 식별한다. 잘못되거나 불완전한 frontmatter를 파일명·본문으로 대체하거나 기존 문서에 frontmatter를 일괄 추가하지 않는다.

기존 ExecPlan의 Requirement source 문자열은 rename 뒤에도 보존하며 다음 순서로 조회한다.

1. 기록된 활성 경로
2. 해당 경로가 없으면 같은 파일명의 archive 문서
3. 앞의 두 경로가 없으면 source 파일명에 적힌 R ID와 원본 ID가 일치하는 유일한 문서

정확한 경로가 있으면 ID가 중복돼도 그 문서를 유지한다. 후보 부재·중복·조회 실패는 구분해 보고하고 source 관계를 확정하지 않는다. archive·ID 대체 조회는 기존 작업의 호환을 위한 것이며 새 ExecPlan은 정확한 활성 경로를 사용한다.

현재 활성 Requirement 경로는 Planner 입력과 판단·이동의 경로 key다. 기존 source는 고유한 원본 R ID로 같은 문서를 연결한다. Manager는 안전한 base 게시 경계에서 유일한 활성 Requirement의 파일명 변경을 비보관·비draft ExecPlan의 sources에 반영하고 게시 확인 뒤 실행 입력을 갱신한다. 구체적인 조회 계약은 [원격 원본과 source 해석](ARCHITECTURE.md#원격-원본과-source-해석)을 따른다.

#### Requirement 식별 정보의 자동 교정

- Manager는 실행·planning·유지보수가 원격 쓰기를 점유하지 않는 안전한 경계에서 최신 base의 활성 Requirement 중복 ID·제목·파일명과 수정 가능한 문서의 확정 상대 링크를 모델 없이 교정하고 같은 commit으로 fast-forward 게시한다. 활성·보관 작업 문서와 저장 관계의 참조를 함께 확인하며 비보관·비draft ExecPlan의 확정된 활성 source 경로도 같은 게시에 반영하며 본문·다른 선언과 archive·User Decision은 보존한다. 수동 소유·문서 draft·종료 불명 writer와 기존 게시 예약은 보존한다.

  교정 시작 전에 안전 gate가 닫혀 있거나 수동 작업이 Requirement source를 소유하면 교정을 보류하고 기존 수동 작업과 독립 실행을 계속한다

- 교정 전 원문·번호 배정·결과·소유 intent를 보존하고 게시 직전 base·참조·목적지·소유권·허용 diff를 다시 검사한다. 입력 경쟁에서는 이전 후보를 보존하고 최신 입력으로 다시 계산한다. 중단·push 실패에서는 실제 원격 결과를 확인해 같은 후보·commit을 재사용하며 이미 게시된 결과를 다시 push하지 않는다. 조회 실패·변조·게시 결과 제거는 덮어쓰기나 Planner 호출·성공 기록으로 대체하지 않는다. 기존 작업·세션·실패·독립 차단과 후속 verify·Review gate를 유지한다

#### Planner 입력과 변경 해석

Planner가 판단을 위해 읽는 자료, 새 ExecPlan의 목표 출처인 `sources`, 직접 수정할 수 있는 문서는 구분한다. 읽기 권한이나 입력 제공만으로 수정 권한을 얻지 않는다.

| 자료 | Planner의 읽기·판단 범위 | 새 ExecPlan의 `sources` | 일반 Planner의 직접 수정 |
|---|---|---|---|
| PRODUCT | 현재 제품 동작·정책·경계 | 허용 | 금지 |
| ARCHITECTURE | 현재 구조·책임·호환 계약 | 허용하지 않음 | 금지 |
| 활성 Requirement | `docs/requirements/` 바로 아래 모든 `*.md`의 탐색 목록과 관련·불확실한 본문 | 허용 | 금지 |
| ExecPlan | 비종료 compact catalog와 제공된 PR 전 계획 snapshot·관련 본문 | 허용하지 않음. 선행 관계는 `depends_on`으로 기록 | 신규 작성과 허용된 snapshot 안의 개정. rename·삭제는 미착수 계획에 한함 |
| FUTURE_IDEAS | 현재 계획·프로젝트 완료의 근거로 사용하지 않음 | 허용하지 않음 | 범위 밖 아이디어 기록 |

Manager가 등록한 검증 입력 목록 요청의 예약 source `manager:verify-inputs`는 [별도 계약](ARCHITECTURE.md#목록-밖-입력과-누락-요청)을 따른다. 유지보수·읽기 전용 평가·문서 수정본 등 목적별 호출은 지정된 권한과 snapshot 범위만 사용하며 이 표로 쓰기 권한을 확대하지 않는다.

일반 planning에는 변경 여부와 관계없이 모든 활성 Requirement의 제목·경로와 비어 있지 않은 기존 Summary를 제공한다. 일반 계획을 함께 만드는 유지보수 호출에도 적용한다. 요약은 탐색용이며 관련·불확실한 문서의 본문은 목록과 같은 원격 revision에서 확인한다.

| Summary의 작성·조회 상황 | 처리 |
|---|---|
| 신규 Requirement 작성·본문 변경 | 작성자가 [Specification Skill의 작성 기준](../.agents/skills/specification-docs/SKILL.md#quality-guidance)에 따라 Summary를 작성·갱신한다. |
| 기존 문서에 비어 있지 않은 Summary가 있음 | 제목·경로와 기존 요약을 제공한다. 본문을 대신하는 정책·완료 판단 근거로 사용하지 않는다. |
| Summary가 없거나 비어 있음 | 제목·경로만 제공하고 관련성 판단에 필요하면 본문을 읽는다. 요약 생성 모델 호출이나 Planner의 Requirement 수정은 하지 않는다. |
| 문서 조회·UTF-8·식별 오류 또는 필요한 본문 확인 실패 | 오류·불확실성으로 처리하며 정상적인 문서·요약 부재나 무관함으로 바꾸지 않고 목록에서 조용히 제외하지 않는다. |

- PRODUCT·ARCHITECTURE의 현재 기준에서 활성 Requirement가 명시적으로 바꾸는 범위에는 변경 목표를 적용하고 나머지 계약은 보존한다. 변경되지 않은 요구도 유효하며 이전 설계만으로 되돌리는 작업을 만들지 않는다. 활성 목표의 충돌·대체 범위가 모호하면 최근 수정 시점이나 ID로 결정하지 않고 사용자 판단을 남긴다. delta·전체·최종 검토에 같은 해석을 적용하며 기존 전체 검토 조건을 유지한다
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 Planner 입력에서 제외한다
- `docs/FUTURE_IDEAS.md`는 계획 입력 지문, 활성 Requirement와 프로젝트 완료 판정에서 제외하며 그 항목을 자동으로 Requirement 또는 ExecPlan으로 승격하지 않는다

#### Requirement 완료 확인과 archive

- source로 참조하는 비종료 ExecPlan·draft가 있으면 해당 Requirement 전체 충족 확인과 archive를 미룬다. 마지막 연결 ExecPlan의 실제 Merge와 catalog 반영을 확인하면 다음 안전한 작업 선택 경계에서 일반 Planner가 해당 Requirement의 각 Acceptance Criterion을 최신 구현·테스트·실제 검증과 관련 제품·설계 문서에 대조한다.

  다른 Requirement의 작업·PR 대기·종료나 프로젝트 전체 완료까지 미루지 않고 독립된 새 작업·유지보수 cycle 선택 전에 확인한다. 계획 없이 직접 구현한 변경도 필요한 명세 검토·최종 확인에서 실제 근거로 판단하며, 작업 작성·Worker 완료·검증 성공·PR 생성·관련 계획의 Merge만으로 전체 충족을 추정하지 않는다

- Manager는 Requirement 원문, 관련 명세·계획 관계, 구현·검증 근거에 결합된 판단을 보존한다. 동일한 유효 입력의 판단은 재사용하고 관련 입력이 바뀌면 해당 Requirement의 연결 작업·사용자 판단이 종료된 다음 안전한 경계에서 다시 확인한다. 충족은 기록·원문 archive로, 미충족은 후속 ExecPlan으로, 사용자 판단은 draft로, 불확실은 완료 보류와 재개 조건으로 반영한다. 같은 경계의 여러 Requirement·명세 변경·프로젝트 최종 확인은 한 호출로 처리할 수 있지만 목적별 근거를 구분하며 서로의 성공을 대신하지 않는다
- 유효한 최종 충족 판단과 source 관계·Merge 상태가 확인되면 Manager가 원문 그대로 일반 planning 게시에서 Requirement를 이동한다. 새 계획과 이동을 함께 게시하거나 이동만 게시할 수 있으며 원격 확인 전에는 활성 입력으로 유지한다. 충돌·불확실성은 덮어쓰기나 임의 완료 없이 보존한다
- archive된 Requirement는 완료한 변경의 보관이며 재평가·새 작업·프로젝트 완료 대기의 입력으로 사용하거나 자동 복원하지 않는다. 기존 ExecPlan의 source 조회·검증을 위한 읽기 호환성과 과거 감사 기록은 보존한다
- `PLAN_BLOCKED` 해제 지문은 원래 활성 Requirement 경로만 사용하고 archive 변경에는 반응하지 않는다

#### Planner 호출과 기존 작업 개정

Planner는 다음 목적별 조건으로 호출하며 재사용·통합 기준은 R-082를 따른다.

| 목적 | 호출 조건 |
|---|---|
| 일반 계획 | 최초 또는 비교 가능한 전체 계획 성공 기준이 없거나 PRODUCT·활성 Requirement·ARCHITECTURE 또는 공통·역할별 실행 지침의 내용이 변경됨 |
| draft 판단 | 사용자 답변의 의미·충분성 판단이 필요함 |
| 수동 제출 | 명시적 제출에 새 ExecPlan이 필요함 |
| 유지보수 | 자동 주기 또는 CLI 요청에 따른 평가 조건 충족 |
| Requirement 완료 | 마지막 연결 작업 Merge 등 [완료 재확인 조건](#requirement-완료-확인과-archive) 충족 |
| 프로젝트 완료 | [최종 전문 확인](#최종-전문-확인)이 필요한 완료 후보 |

실행 지침 변경은 현재 runtime의 공통·역할별 원본 내용 해시로 비교한다. 내용이 같은 runtime SHA 전진이나 코드·설정만의 변경은 그 자체로 일반 Planner 호출 조건이 아니다.

일반 Planner는 비종료 compact catalog와 PR 전 계획 snapshot으로 현재 식별 가능한 미충족 작업을 한 번에 계획한다. 실제 PR 부재와 현재 선언·revision·소유권·writer 상태가 확인되면 일반 `PLAN_BLOCKED`도 차단 상태, 삭제·참조 복구 대상 여부, 최초 실행 여부만으로 snapshot에서 제외하지 않고 같은 ID·경로로 개정할 수 있다. 독립 catalog hold와 삭제·참조 복구 proof는 기존 보호 조건을 유지한다. 시작한 계획의 개정은 작업물·질문·차단 사유·checkpoint·세션·수행 및 실패·승격 이력을 보존한다. 개정 게시·catalog·DB 반영만으로 차단·사용자 판단을 해제하거나 READY·검증 성공으로 바꾸지 않으며, 확정 뒤 기존 원인별 차단 재평가를 거친다. PR 등록 후 Merge 대기 작업의 추가·변경 요구사항은 새 ExecPlan으로 작성한다.

R-077의 수동 소유 계획과 start/submit 준비 intent 대상은 snapshot·게시 직전 검사에서 보호한다. membership 변경은 영향을 받은 개정을 거부하고 독립 계획은 계속 처리한다. 실제 종료·대상 해제 뒤 남은 membership 이력만으로 소유권을 추정하지 않는다.

- 일반 Planner는 마지막으로 원격 적용한 성공 검토와 비교 가능한 경우 변경·삭제·rename과 현재 catalog부터 확인한다. 영향받은 명세 절·코드·테스트·계획과 dependency를 읽고 불확실하면 범위를 넓힌다. 비교 근거가 없거나 손상되면 활성 명세 전문을 확인하며, 매 변경 검토에서 무관한 문서 전문을 반복하지 않는다
- ExecPlan·코드·테스트·설정 변경과 승인 수정본의 승인·재관측은 Planner 호출 사유가 아니다. READY의 유무·dependency 대기·실행 재개·재시작에서도 점검·일반·문서 영향 Planner를 호출하지 않는다. 실제 명세 변경·draft 답변·Requirement 완료 재확인 등 별도 목적은 본래 호출 조건과 판단을 유지한다. Manager가 승인 revision·원문·source 관계·현재 선언·소유권·실행 자격을 확인해 승인 snapshot의 단 하나의 기존 ExecPlan과 sources가 현재 작업에 정확히 연결되고 자동 Worker 실행 대상이면, 대상이 PRODUCT·ARCHITECTURE일 때 해당 작업의 기존 Worker에 수정본을 전달한다. Requirement 대상은 일반 Worker 권한 범위 밖이므로 새로 연결하거나 적용하지 않고 승인과 미적용 이유를 보존한다. Worker는 최신 작업 기준의 구현 적합성·필요한 보완·targeted test를 같은 세션에서 확인하고, Manager는 원문·source 관계를 다시 검증해 승인 bytes를 적용한다. 대상이 없거나 연결이 여러 개·stale·수동 소유·PR 이후 범위 확장·자동 Worker 대상이 아니면 승인과 미적용 이유를 보존하고, 새 ExecPlan을 만들거나 독립 작업·Planner 판단을 막지 않는다. 대상 파일의 내용이나 mode가 승인과 다르면 덮어쓰지 않고 적용을 보류한다. 구현 이후 원문·source·소유권 재검증이 실패하거나 승인 bytes가 없거나 달라지면 verify·Review·게시를 거부하고 승인과 미적용 이유를 보존한다. 문서 의미·diff가 PASS이고 코드 Review만 FAIL이면 기존 실패·Worker 교정 흐름을 따른다. 승인 bytes가 실제 게시된 뒤의 상위 명세 변경은 별도 명세 변경 조건을 적용한다.

- 중단·재시작·원격 관측에서는 유효한 결과와 미완료 게시·DB 적용을 먼저 재사용한다. 실제 입력 변경으로 적용 불가한 결과·필수 의미 판단 누락·계획 작성 범위의 충돌만 필요한 세션 교정으로 이어가며, 전체 재계획이나 이미 확정한 단계의 반복을 추가하지 않는다

#### ExecPlan catalog와 실행 이력

- source와 dependency 변경은 같은 reconciliation 결과를 Requirement 판단 자격·입력 지문·READY 선택에 사용하고 priority 변경은 다음 선택부터 반영한다. complexity·risk 변경은 최초 Worker level을 다시 계산하되 실행 이력이 있는 계획의 effective level과 Review 승격 이력을 낮추지 않는다
- 현재 path와 title은 compact catalog, 로그·알림과 이후 파일 조회에 사용하지만 기존 PR의 ExecPlan 링크는 최초 게시 SHA와 최초 active path를 계속 사용한다
- MERGED ExecPlan은 Planner 입력에서 제외하며 프로그램만 ID 고유성, 의존성 충족과 감사 이력을 위해 관리한다
- Planner와 Worker의 실행 후보는 `docs/plans/` 바로 아래의 active ExecPlan으로 제한하고, `docs/plans/archive/`는 ID 고유성, 다음 번호, dependency, 복구와 감사 검증에만 포함한다
- 정상적으로 열린 로컬 DB에 필요한 개별 작업 행이 없으면 활성·archive 문서로 보완한다. 활성 문서는 기본 READY 등록과 기존 실행 복구 규칙을 따르고, archive 문서는 추가 PR 조회나 과거 게시 본문 검증 없이 문서에 근거해 MERGED로 수락한다. 이 수락 근거와 실제 PR Merge 관측은 구분하며 유실된 최초 게시·세션·실패 이력을 만들어 내지 않는다. 활성 선언의 형식·ID·source·dependency 검증과 기존 행의 실행·소유권·실패 이력은 보존한다. 양쪽 문서가 없는 활성 작업은 누락으로 보고하되 완전한 Merge proof가 있는 archive 이력은 활성 선언으로 재등록하지 않고 dependency·완료 근거로 유지한다. 미확인 이력을 MERGED로 계산하지 않는다. DB 전체 손실·손상은 백업 복원이나 초기화가 필요하다
- 일반 원격 catalog 동기화에서 오류 작업을 유일하게 식별하고 영향 범위를 확정할 수 있으면 해당 작업과 그 작업에 의존하는 후속 작업을 보류하고, 검증된 독립 작업의 선택·PR 관측은 계속한다. 원격 파일 누락만으로 취소·완료를 확정하지 않는다. 식별 모호성·입력 조회 실패·공유 소유권 등 안전 범위를 확정할 수 없는 오류는 전체 동기화를 보류하며, 상세 경계는 [원격 catalog와 실행 이력의 분리](ARCHITECTURE.md#원격-catalog와-실행-이력의-분리)를 따른다
- ExecPlan archive-only 이동은 계획 입력 지문, 유지보수 Merge count, 프로젝트 완료 cycle을 바꾸지 않는다

#### Worker 문서 수정 범위

Worker의 문서 수정은 현재 작업에 필요한 범위로 제한한다.

| 대상 | Worker 권한 |
|---|---|
| PRODUCT·ARCHITECTURE·FUTURE_IDEAS·공통 및 역할별 지침·사용 안내 | 관련 내용 갱신 |
| `docs/PLANS.md` | 게시된 ExecPlan이 해당 변경을 명시한 경우에만 수정 |
| 선택된 ExecPlan | 구현·검증 기록 등 본문만 갱신. 사용자 결정 작성·변경과 draft 해제 금지 |
| ExecPlan frontmatter·다른 ExecPlan | 수정 금지 |
| 모든 Requirement(활성·archive) | 추가·수정·삭제·rename·이동 금지 |

새로운 frontmatter 선언의 교정은 Planner·Manager가 검증하고 공식 선언과 DB에 반영한다.

Worker 종료 후 정상 작업 경계에서 시작 선언·공식 원본·본문 경계를 확인할 수 있으면 Worker가 변경한 frontmatter만 공식 선언으로 복원하고 본문·기존 수정·실행 기록은 보존한다. 복원 사실은 Worker에게 전달해 현재 문서 보고를 갱신하며 검증 성공으로 취급하지 않는다. 확인 불가·동시 수정·진행 중 rebase는 기존 오류·복구 경계를 유지한다. 기존 source의 rename 조회 호환을 유지하며 선언 불일치를 Worker의 source 수정으로 우회하지 않는다. rebase 전에는 현재 소유 branch와 target의 관계를 기준으로 시작 조건을 판단하고, 오래된 시작 HEAD나 base에만 유입된 변경을 Worker의 권한 밖 수정으로 취급하지 않는다. 충돌 해결 중에는 소유된 중간 상태의 최종 범위·선언 판정을 유예한다. Git rebase 완료 후에는 Manager가 고정된 base의 frontmatter를 반영하고 구현·검증 본문을 보존한 뒤 최종 검증을 수행한다. 동일한 선언 오류를 Worker에게 반복 전달하지 않으며 결과·원인·작업물은 보존한다.

rebase로 원본 브랜치에서 유입된 다른 문서는 Worker의 범위 밖 수정으로 판단하지 않으며, 실제 범위 오류는 원본·후보 revision과 변경 경로를 함께 진단한다. 관련 수정에서는 원래 요구·Acceptance Criteria·필수 검증·실패·사용자 결정과 실행 이력을 보존한다. Requirement 완료 판단과 archive 이동은 일반 Planner·Manager의 책임이다

#### PR 문서 보고

PR 본문의 문서 보고는 최종 diff에서 선택된 ExecPlan을 제외한 모든 변경 문서의 파일명·변경 요약·이유·영향을 제공한다. 선택된 ExecPlan은 `DOCUMENT_CHANGE` 제출 대상에서도 제외하지만 기존 diff 검토·범위 검증과 PR의 ExecPlan 링크·Purpose는 유지한다. 추가·삭제·rename을 포함하며 실제 변경 내용·patch·diff 링크·검증용 hash는 본문에 넣지 않는다.

Manager는 Worker Summary와 실제 최종 diff/hash를 대조하고 rebase·PR head 변경 뒤 보고도 갱신한다. 설명 누락·중복·형식 오류·최종 diff 불일치는 해당 Worker의 교정으로 보완한다. 오래된 일반 Summary나 patch로 대신하지 않으며 불완전한 보고를 게시하거나 보고 반영 전에 게시 완료로 처리하지 않는다.

열린 PR의 보고는 확인된 head를 기준으로 Manager 소유 영역만 교체·제거한다. 사용자가 쓴 본문과 기존 ExecPlan·source의 고정 참조, Purpose는 보존한다. 보고 필드와 최종 diff 결합 방식은 [Worker의 구현과 문서 경계](ARCHITECTURE.md#worker의-구현과-문서-경계)를 따른다.

Reviewer는 읽기 전용으로 변경 전 목표·변경 후 문서·구현·보고를 검토한다

### 프로젝트 등록 및 실행 조건

#### 등록과 선택 실행

- `add <repo> [--base <branch>] [--provider <provider>]`는 기존 프로젝트 검증·등록을 수행하고 `remove <repo>`는 기존 등록 해제 gate를 유지한다. `config init`과 `config list`는 유지하며 이전 `config add/remove` 이름은 허용하지 않는다
- `run [--skip-if-running] [repo...]`는 상대 경로를 CLI 호출 당시 작업 디렉터리 기준으로 절대화하고 중복을 제거한 뒤, 지정한 전체 경로가 등록됐는지 실행 전에 검증한다. 경로를 생략하면 전체 등록 범위를 처리하고, 하나라도 미등록이면 어떤 프로젝트도 처리하지 않는다. 공백이 포함된 경로는 인용해 지정한다
- 선택 실행은 `max_workers`·`verify_concurrency`와 프로젝트별 관리 한도, dependency·복구·self handoff와 실패 격리를 유지한다. 범위 밖 프로젝트는 처리하지 않는다
- 대상 경로는 origin이 GitHub 저장소인 Git 프로젝트여야 한다
- fetch된 `origin/<base-branch>` tree에 `docs/PLANS.md`, `docs/ARCHITECTURE.md`가 있어야 한다
- fetch된 `origin/<base-branch>` tree에 `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- ARCHITECTURE 또는 존재하는 PRODUCT 중 하나라도 변경되지 않은 템플릿이면 등록을 거부한다. 활성 Requirement가 있어도 남아 있는 PRODUCT 템플릿을 허용하지 않는다
- `docs/PLANS.md`는 CRLF를 LF로 통일한 내용이 Loop Manager 기준 파일과 같아야 한다. 이 검사는 등록 시 수행하며 자동 Loop에서 반복하지 않는다

#### 원격 기준과 기본 worktree 보존

자동 작업의 기준은 `origin/<base-branch>`다. 기본 worktree의 로컬 변경, checkout branch와 ahead·diverged 이력은 자동 작업을 막지 않으며 local-only 내용은 사용자가 push한 뒤 입력이 된다.

기본 worktree의 동기화는 등록 base를 checkout한 경우에만 시도하며 다음 조건을 적용한다.

| 기본 worktree 상태 | 처리 |
|---|---|
| 원격과 equal이고 진행 중 Git operation이 없음 | 로컬 변경도 그대로 두고 동기화하지 않는다. |
| behind이고 Git operation·비정상 index·경로 겹침이 없음 | 겹치지 않는 로컬 변경·index·ignored 파일을 보존하며 safe fast-forward한다. |
| ahead | 미게시 commit과 checkout을 보존하고 동기화만 건너뛴다. 자동 push하지 않는다. |
| clean diverged이며 선형 로컬 commit·지속 저장된 소유 intent·경로 안전성이 확인됨 | 원격 base 위로 로컬 commit을 보존하는 rebase를 수행한다. 자동 push하지 않는다. |
| 다른 branch·detached, 진행 중 Git operation, 비정상 index·경로 겹침, dirty 또는 비선형 diverged, 소유권 불확실 | 이유를 기록하고 파일·index·이력을 보존하며 동기화만 건너뛴다. |

동기화의 성공·건너뜀·실패와 관계없이 자동 작업과 PR 관측은 원격 기준으로 계속한다. 시작 ignore 규칙으로 증명된 index-free 파일의 생성·수정·삭제만으로 정상 완료·실행 직전 검사·중단 복구를 거부하지 않는다. Git 쓰기 직전에는 현재 경로 충돌을 다시 확인하며 파일을 삭제·복원해 우회하지 않는다.

복구는 실제 ref·HEAD·index·권위 있는 파일·operation 전이와 소유 증거가 맞을 때만 완료하고 동일 입력의 실패 Git 쓰기를 반복하지 않는다. 과거 기록의 시작 규칙이나 소유 증거가 부족하면 기록과 사용자 파일을 보존한다. 구체적인 충돌·복구 계약은 [기본 worktree 동기화](ARCHITECTURE.md#기본-worktree-동기화)를 따른다.

Loop Manager가 만드는 계획과 구현 변경은 대상 프로젝트 밖의 Loop Manager 관리 경로에 만든 별도 worktree와 branch에만 기록한다. 자동 실행 중 기본 worktree에서는 위의 safe fast-forward와 소유권·로컬 commit 보존이 검증된 rebase·abort 외에 checkout, pull, reset, stash, clean, commit과 파일 생성을 수행하지 않는다.

#### runtime 시작과 실행 환경

- 자동 명령의 bootstrap은 process lock 아래 등록된 self base(미등록이면 `main`)의 원격 SHA를 확정하고 그 revision의 bootstrap·runtime을 같은 실행에서 시작한다. source HEAD·branch·동기화 성공은 실행 조건이 아니며 로컬 코드를 실행 입력으로 사용하지 않는다. source 기준 절대 config 경로·interpreter·인자를 전달하고 자식 종료까지 부모 lock을 유지한다.

  원격 revision·runtime 소유권 검증 실패는 보존·진단하며 로컬 HEAD나 오래된 코드로 대체 실행하지 않는다.

- Linux source 진입은 setup이 저장한 설치별 PATH, 현재 실행 환경, 기본 시스템 후보 순서로 bootstrap의 첫 외부 명령 전에 프로그램 탐색 환경을 확정한다. 캐시가 없으면 현재 PATH와 시스템 후보로 기존 설치·명시적 cron PATH를 지원하며 손상된 캐시는 진단하고 중단한다. 같은 실행의 runtime·Agent·verify는 확정값을 유지하고 setup의 갱신은 다음 실행부터 반영한다. PATH 캐시는 다른 설정·인증·작업 상태를 초기화하지 않는다.
- Linux bootstrap의 Git, 프로젝트 등록의 Git·gh·선택 provider, Agent 사전 확인과 실제 호출에서 필수 CLI 또는 확인된 실행 의존성을 찾지 못하면 기존 원인·출력·실패 결과와 함께 같은 계정의 정상 터미널에서 설치 경로를 확인하고 Loop Manager source checkout의 `bash scripts/setup.sh`를 다시 실행하라고 안내한다. 일반 명령·session·인증·권한 실패는 PATH 탐색 실패로 바꾸지 않으며, 오류 안내는 캐시 갱신·로그인·다른 provider 실행을 시작하지 않는다.
- runtime의 Python bytecode 캐시는 선택 source·revision·interpreter에 맞는 표준 Python 캐시로 재사용한다. 기본 캐시는 일회성 runtime worktree 밖에 두며 source checkout의 ignored 파일은 runtime 입력이 아니다. 캐시 생성·갱신·부재·손상·쓰기 실패는 현재 원본 실행과 Git 범위 판정을 바꾸지 않고, 사용자가 명시한 bytecode 비활성화나 캐시 경로 선택을 존중한다.

#### Claude 개발·지원 범위

Claude 지원은 [R-023](requirements/%5BR-023%5D%20Self-Hosted%20Runtime%20Refresh%20Handoff.md)의 일반 호출 구성을 공식 문서·모의 테스트로 구현하는 범위로 한정한다. 역할별 쓰기 제한·자동 승인 검토를 구성하되 Codex와 같은 실환경 검증 수준이나 모든 OS·CLI·기능 조합의 완전 지원을 완료 조건으로 요구하지 않는다. 실호출 미검증만으로 개발 완료·독립 Codex 작업을 막지 않으며 문서/mock 확인과 실제 실행 확인을 구분한다.

실행 중 조직 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장은 현재 범위 밖이다. 실제 호출에서 관측한 인증·지원·설정 충돌·권한·초기화 오류는 해당 호출에서 보존·처리한다. 실환경·session·OS/CLI·managed 변경의 후속 점검은 [FUTURE_IDEAS](FUTURE_IDEAS.md#claude-실환경-후속-점검)에 두며 현재 완료 조건으로 사용하지 않는다. 현재 구현 지원 여부와 남은 호출 구성은 [provider 계약](ARCHITECTURE.md#provider의-현재-지원과-쓰기-정책)을 따른다.

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

#### 지침 원본과 호출별 적용

자동 Agent는 신규·재개·재시도·fallback마다 현재 실행이 선택한 최신 immutable runtime의 공통·역할별 지침을 읽는다. 진행 중인 작업도 과거 runtime 지침에 고정하지 않으며, runtime 갱신 뒤 다음 호출부터 개선된 규칙을 적용한다. 실행 중인 호출은 기존 안전한 반환 경계까지 유지한다.

- 실행 원본은 runtime의 `docs/AGENTS.common.md`와 역할별 `docs/AGENTS.*.md`다. 등록 프로젝트 전반의 자동 규칙만 두며 Loop Manager 자체 개발 규칙은 이 저장소 루트 `AGENTS.md`에 둔다. 후보 worktree의 동명 파일 편집·diff 검토는 실행 지침을 바꾸지 않는다.
- Worker는 현재 runtime의 `docs/AGENTS.ponytail.md`를 정확히 한 번 추가로 전달받는다. 다른 역할·사용자 prompt에는 추가하지 않으며 Plugin 조회·캐시·설치 판정과 사용자 설정 변경을 하지 않는다. 기존 확장 차단·쓰기 경계·필수 요구·검증 조건을 유지한다.
- 작업별 원본 snapshot은 생성하거나 실행 입력으로 재사용하지 않는다. 과거 snapshot·DB·session·실패 이력은 감사로 보존하고 누락·손상도 현재 원본 선택을 막지 않는다. provider 전달에 필요한 합친 파일만 저장소 밖 실행용으로 생성한다.
- 호출별 실제 전달 내용 해시를 보존하며 과거 호출을 최신 지침으로 소급 변경하지 않는다. session의 작업 귀속은 유지하고 같은 작업의 재개에도 현재 지침을 전달한다.
- 현재 runtime의 공통·모든 역할·Ponytail 내용 해시를 planning·관련 성공 결과 재사용에 반영한다. 내용이 같으면 runtime SHA·위치 변경만으로 재계획하지 않는다. 현재 원본·실행용 파일 오류·전달 기능 미지원은 해당 모델 실행 전에 거부하며 과거 snapshot으로 대체하지 않는다.

runtime 지침만 변경돼도 안전한 runtime 갱신 대상이다. 원본·전달 해시와 호환 기록은 [지침 원본과 호출별 적용](ARCHITECTURE.md#지침-원본과-호출별-적용)을 따른다.

- 지침 전달은 기본·기존 추가 지침·프로젝트 규칙과 실행 경계를 보존한다. 버전 없는 과거 세션은 기존 작업물·session·실패 기록을 남기고 최소 문맥의 새 세션으로 복구한다.
- Worker 사용자 메시지는 최초·교체 session에 현재 계획·작업 공간·결과 형식·미완료 작업을 전달하고, 같은 session에는 확인되지 않은 새 조건·오류·검증 피드백·복구점만 보낸다. 변하지 않은 archive 후보·범위와 이전 결과·verify/Review 원문은 반복하지 않는다. 전달 시작만으로 확인 처리하지 않으며 실패·중단에는 미확인 문맥을 보존해 재전송한다. prompt 축약은 권한·검증 성공의 재사용이나 게시 대기 해소를 뜻하지 않는다.

Loop Manager 자체 작업 worktree의 공통·역할별 지침 원본은 현재 역할·목표의 일반 파일 권한으로 수정한다. 후보 편집이나 Reviewer의 diff 읽기로 실행 snapshot을 바꾸지 않는다. 변경 PR의 보고는 [PR 문서 보고](#pr-문서-보고)를 따르며 Manager가 누락을 보완한다.

#### Skill과 직접 대화형 작업

- 프로젝트로 복제하는 Specification Skill에는 PRODUCT·ARCHITECTURE·Requirement·FUTURE_IDEAS 작성·검토에 필요한 내용만 둔다. 계획 생성·상태 전이·호출·게시·보관 판정 등 Manager 내부 절차는 제품·설계 문서에서 정의한다. 보관 Requirement의 읽기 전용 범위와 새 변경의 신규 Requirement 작성 기준은 Skill에 유지한다
- 준비 도구는 Specification Skill·문서 템플릿·기본 명세와 작업 폴더를 준비한다. 이 저장소 루트 AGENTS에는 Loop Manager 개발 지침만 둔다.
- 직접 대화형 작업에서 새 기능·정책 변경을 구현할 때는 관련 Requirement를 먼저 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용한다. 최초 설계 작성·현행 문서 정리는 Specification Skill의 작성 대상 판단을 따른다.
- 직접 대화형 작업에는 ExecPlan 작성·자동 Worker 역할·SQLite 전이·자동 완료 절차를 요구하지 않는다. ExecPlan 생성·계획 작성은 매니저가 호출하는 Planner의 자동 흐름이 담당한다. 직접 구현과 활성 Requirement의 원격 반영 후 실제 미충족 작업만 재점검하며 소급·중복 계획·자동 검증 성공 합성을 하지 않는다.
- Manager의 `manual start/complete/submit`은 [수동 명령의 준비와 수락](#수동-명령의-준비와-수락)에 따른 별도 관리 절차다. 직접 대화형 작업의 자유가 수동 명령의 소유권·검증·게시 조건을 생략하는 근거가 되지 않으며, 자동 작업의 ExecPlan·dependency·검증·사용자 Merge gate도 유지한다.

### 수동 명령의 준비와 수락

수동 명령의 준비·제출 수락·PR 연결·실제 Merge는 서로 다른 결과다. 세 명령은 등록 프로젝트를 대상으로 다음 역할을 수행한다.

| 명령 | 대상과 결과 |
|---|---|
| `manual start <repository> <G-ID>` | READY 또는 수동 인계가 허용된 차단 G의 소유 branch·worktree를 준비하고 같은 명령에서 원격 게시한다. 이미 준비된 같은 수동 작업은 기존 안내를 재사용한다. |
| `manual complete <repository> <G-ID>` | 수동 시작 또는 submit으로 등록한 G의 원격 후보를 자동 검증·게시 절차로 제출한다. |
| `manual submit <repository> --branch <branch> --requirement <path>` | 원격 branch의 활성 Requirement·구현을 읽어 추적 G와 별도 소유 작업 공간을 준비한다. Requirement는 여러 개 지정할 수 있고 입력 branch·사용자 작업물은 보존한다. |

`start`는 선행 계획의 실제 원격 Merge와 승인·차단·유지보수 조건을 확인한다. verify·Review 재시도 한도, Review 결과 형식 또는 Worker 결과 오류로 차단된 G만 수동 인계할 수 있으며 draft·사용자 판단 대기·catalog 보류·프로젝트 안전 차단을 우회하지 않는다. 계획 및 작업 자원이 충돌하지 않으면 자동 매니저나 독립된 계획의 실행 여부만으로 준비를 거부하지 않는다. `complete/submit`의 후보 확인·추적 계획 등록·소유 작업 공간 준비도 같은 자원 경계를 따른다.

submit의 복수 G는 같은 최종 후보와 PR을 공유한다. 묶음 안에서 선행 G의 Merge가 필요한 경우에는 별도 branch·PR로 나누어 제출한다. 명령과 인자의 상세 사용법은 [수동 구현과 제출](OPERATIONS.md#수동-구현과-제출)을 따른다.

실행 중인 동일 계획, 겹치는 브랜치·워크트리·게시 대상, Planner 게시 예약과 미완료 Git 작업은 충돌 자원과 소유자를 설명하며 거부한다. 거부된 충돌 요청을 자동 대기 제출로 바꾸지 않는다. 충돌 확인과 소유 예약은 Worker 선택·Planner 게시·다른 수동 명령과 원자적으로 조율하며, 준비 중단 시 저장된 소유권과 작업물을 보존한다. 명령마다 원격 커밋에 고정된 별도 런타임을 사용하고 자기 작업 공간만 정리한다.

**수락 후 자동 처리**

수동 구현 대기와 완료·제출 수락 이후의 자동 처리는 구분한다. 수락된 제출은 기존 프로젝트 실행에서 저장소 준비, catalog 확인·정리, 기존 PR 상태 동기화 뒤 실행 자리가 비었을 때 등록 순서로 재개한다. 그 뒤 기존 실행 재개와 일반 coordinator 순서를 유지하며 dependency·차단·유지보수 조건을 통과해야 한다. 별도의 최우선 처리나 현재 작업 종료 직후 즉시 검토를 보장하지 않는다.

`start`의 종료 코드 0은 수동 작업 준비 완료, `complete/submit`의 0은 PR 연결 또는 이미 확인된 Merge를 뜻한다. 0이 아닌 결과에는 수락 후 대기·자동 복구 중과 수락 거부·인자 오류가 모두 있으므로 출력의 수락 여부·보존된 요청·오류를 함께 확인한다. PR 생성과 검증 면제만으로 G를 완료 처리하지 않으며 연결 PR의 실제 Merge를 확인한다.

**반복 제출과 검증 면제**

반복 submit은 저장 후보와 생성 당시 원격 기준을 함께 보존한다. 같은 후보의 게시 또는 후속 이력이 확인된 뒤 재중단돼도 같은 요청·전체 G의 결과를 push 없이 확정한다. 확인한 게시 후보가 원격 재작성으로 제거되면 새 후보로 대체 게시하지 않고 이유와 작업물을 보존한다.

complete/submit에서 검증을 생략하려면 해당 단계에 대응하는 `--skip-verify` 또는 `--skip-review`와 원격 commit 전체 SHA인 `--expected-head`를 지정한다. `--skip-reason`은 선택 입력이며, 생략·빈 문자열·공백은 감사·Reviewer·PR 설명에 `사유 미입력`으로 표시한다. 입력한 사유는 감사에 보존하고 후속 표시에서 `제공 사유`로 구분한다. 생략 옵션 없이 사유만 지정하면 거부하며, 수락 시 원격 HEAD가 다르면 계속 거부한다. 생략은 `SKIPPED_BY_USER`로 기록해 실제 PASS와 구분하며, 같은 후보·단계의 실제 실패를 생략 옵션으로 지우거나 성공으로 바꾸지 않는다.

complete/submit의 verify와 Review 생략 근거는 후보·base·환경·현재 runtime 지침과 각 단계의 실제 운영 입력에 결합한다. verify 입력은 설정된 timeout·parallelism을 포함하고, Review 입력은 Reviewer provider·Model·Effort와 현재 verify 근거를 포함한다. 수락, 단계 실행, 최종 게시와 PR 대기 재평가는 같은 기준을 다시 관측한다.

설정이나 profile을 읽거나 유효성을 확인할 수 없거나 요청에 당시 지문이 없으면 생략을 적용하지 않고 해당 기본 단계를 실행한다. 설정 변경으로 PR 대기 근거가 무효화되면 제출을 자동 재검증 대상으로 되돌린다. 저장·출력에는 설정 원문이나 비밀값을 포함하지 않는다.

### 로컬 환경 복구 요청

`recover <repository> [--plan <id>]`는 등록 프로젝트 전체 또는 지정 G의 로컬 원인을 제한적으로 재검사한다. 다른 작업 중에도 요청을 저장할 수 있지만 실제 복구는 기존 직렬 실행·소유권·승인 조건에서 진행한다. 다른 계획·planning·유지보수가 선택한 계획의 실행 자리를 사용하면 그 작업을 계속하고 복구 요청은 대기한다. 실패·교정·승격 이력과 별도 사용자 판단은 유지한다.

| 접수·실행 상황 | 요청 보존과 결과 |
|---|---|
| 미등록 프로젝트·해당 프로젝트에 없는 G·읽을 수 없는 상태 DB | 요청 실행을 거부한다. 유효하지 않은 대상의 요청이나 새 DB를 만들지 않는다. |
| 유효한 대상의 요청을 저장한 뒤 현재 설정 오류 확인 | 저장한 요청을 보존하고 실행하지 않는다. 설정을 고친 뒤 같은 명령으로 이어간다. |
| 실행 잠금·실행 자리 대기 | 요청과 미소비 재검사를 보존하고 다음 `run`에서 이어간다. |
| 같은 프로젝트·같은 범위의 미완료 요청 재접수 | 같은 요청 ID와 이미 소비한 제한 재검사 기록을 재사용한다. 다른 범위는 별도 요청으로 저장한다. |
| 요청 범위의 복구가 끝남 | 종료 코드 0과 완료 결과를 기록한다. 프로젝트 전체의 명세 충족·작업 완료·PR Merge를 뜻하지 않는다. |
| 대기·진행 중·미해결·입력 거부 | 0이 아닌 종료 코드와 현재 상태를 보고한다. 재검사 진행 중 요청은 보존하고, 실행 결과가 미해결로 종결된 요청은 감사에 남기며 추가 재검사는 새 요청으로 접수한다. |

프로젝트 전체와 여러 계획의 미완료 요청을 모두 보존한다. 실행 가능한 요청 중 접수 순서가 가장 빠른 요청부터 직렬 처리하고 앞선 대기 요청이 뒤의 실행 가능한 요청을 막지 않는다. 저장·실행·소비 기록의 상세 계약은 [로컬 복구 요청의 접수와 실행](ARCHITECTURE.md#로컬-복구-요청의-접수와-실행)을 따른다.

### 작업 선택과 동시 실행

#### 현재 작업과 명세 변경

- 명세 변경은 실행 중인 Worker·verify·Reviewer 종료 후 안전한 단계 경계에서 프로젝트 일반 planning을 먼저 수행하고, 관련 ExecPlan 게시·DB 반영을 마친 뒤 현재 작업을 재개한다. 명세·작업 선언이 그대로이면 현재 작업을 먼저 재개한다. 확정 PR 충돌과 소유 Git operation의 복구는 기존 안전 경계에서 먼저 처리한다.
- 작업은 마지막으로 반영한 ExecPlan의 조건 변경과 구현·검증 부족에 따라 대응한다. 작업별 영향·구현 적합성 Planner를 호출하지 않고 필요한 판단·보완은 기존 Worker 세션에서 함께 수행한다. 유효한 구현 충족 근거가 있으면 verify·Review를 계속한다. ID·작업물·세션·실패 이력과 실제 PR·수동 소유·dependency gate를 유지하고 자체 진행 기록은 확인한 delta만 기준에 반영한다.

  Worker 소유 commit 뒤 완료 기록 저장 전에 중단되면, 복구는 보존된 원문과 실제 commit 결과를 확인해 같은 Progress 기록만 작업 기준에 반영하며 commit을 반복하지 않는다. commit·소유권·원문을 확인할 수 없거나 기록에 새 조건·frontmatter 변경이 섞이면 이전 기준과 미해결 사유를 보존한다. 미적용 기록이 남아 있어도 Worker는 후속 소유 교정 commit을 진행할 수 있으며, 그 commit은 기존 기록을 대체하거나 완료로 만들지 않는다. 원격에 같은 기록 내용이 확인된 경우에만 해당 문서 delta를 planning 기준에서 소비하며, Progress 기록은 verify·Review·실패 이력을 성공으로 바꾸지 않는다.

  Git 복구와 verify·Review의 입력 증명은 별도로 유지하며 세부 실패·재개 계약은 [프로젝트 planning과 작업 문서 반영 기준](ARCHITECTURE.md#프로젝트-planning과-작업-문서-반영-기준)을 따른다.

- 저장된 단계별 문서 개정 후보를 재개할 때 proposed snapshot에서 해당 계획이 PR 때문에 제외된 경우나 prepared 후보의 게시 직전에 해당 branch에 PR이 나타나면 기존 PR의 문서와 작업 기록을 보존하고 후보 게시를 멈춘다. PR 출현은 ExecPlan 입력 오류로 처리하지 않으며 기존 PR 처리 뒤 남은 범위는 새 ExecPlan 기준으로 판단한다.
- 새 계획 입력은 실행 중인 작업을 다른 작업으로 선점하지 않는다. 프로젝트 Planner가 명세 변경을 관련 ExecPlan에 반영하고 Worker·Manager verify·Reviewer·게시 진입 전 최신 원본·작업 branch를 확인해 기존 규칙으로 rebase한다. 현재 작업 조건이 바뀌지 않았으면 하던 단계를 계속하며, 코드 변경의 충돌 복구·검증 무효화와 증명된 결과 재사용 규칙은 유지한다
- 소유 rebase의 Requirement·ExecPlan 충돌은 Manager가 확인한 최신 원격 base의 원문·경로·삭제로 처리한다. 삭제·rename·archive와 ID·source·dependency·실제 PR 경계를 검증하고 교체 전 문서·작업 형상과 실행 이력을 보존한다. 현재 작업의 유효성이나 소유권을 확인하지 못하면 원인과 작업을 보존해 기존 복구 경계로 연결한다. 문서 충돌만 있으면 Manager가 rebase를 이어가며, 그 외 파일과의 혼합 충돌은 문서만 처리하고 Worker 교정으로 연결한다.

  PRODUCT·ARCHITECTURE·지침·코드 등은 일반 문서 충돌 해소에서 자동 선택하지 않는다. 반복 교정으로 해결하지 못한 파일은 위 파일별 원본 복원·재작업 경계로 처리한다. 중단·원격 재전진에서도 진행 중 target과 채택 revision을 보존하며 base의 Progress를 현재 후보의 성공으로 사용하지 않는다

- Worker는 최신 작업 문서를 입력으로 구현한다. verify 직전에는 구현 보완이 필요한 경우만 Worker로 돌아간다. Reviewer 직전의 관련 문서 변경은 최신 문서·구현·과거 verify가 확인한 범위와 미검증 조건으로 Review를 먼저 진행하며, 구현 부족은 Worker 교정으로, 검증 근거 부족은 Manager 검증 보완으로 연결한다. 이전 문서 기준의 PASS와 선행 Review를 최신 조건의 검증·게시 승인으로 사용하지 않는다

#### 실행 자리와 dependency

구현·Review의 실제 호출은 전체 `max_workers`(기본 3), verify 호출은 별도 전체 `verify_concurrency`(기본 1)를 공유한다. verify는 구현·Review와 겹칠 수 있다. 프로젝트별 관리 상한은 DB `max_tasks_per_project`의 양수 값을 사용하고, 기본 0이면 config의 같은 이름 값(기본 1)을 따른다. 세 config 값은 양의 정수다. 실행 중 호출을 선점하거나 상한 축소로 기존 작업을 삭제하지 않는다. 상한을 넘은 종류의 신규 배정만 보류한다.

일반 G 하나는 관리 단위 하나다. 공유 수동 제출 묶음은 구성 G를 직렬로 처리하는 한 단위이며 관리 중인 작업은 다음 단계·교정 때 중복 계산하지 않는다. 실행 자리 점유와 작업 공간·PR·수동 요청의 보존은 구분한다.

| 상태·작업 | 실행 자리와 독립 작업 |
|---|---|
| IMPLEMENTING·VERIFYING·REVIEWING·PUBLISHING 또는 PR 충돌 복구 | 관리 자리를 유지한다. Worker·Reviewer와 verify의 대기·호출 및 게시를 포함하며 실제 호출 때만 해당 종류의 실행 자리를 사용한다. |
| 충돌 복구 중이 아닌 안정된 PR_OPEN | 실행 자리를 점유하지 않는다. 조건을 충족한 독립 READY는 실행할 수 있고 새 유지보수 평가는 보류한다. |
| PLAN_BLOCKED | 미완료 rebase·호출 예약의 존재와 무관하게 관리 개수에서 제외한다. 차단 기록·작업물·checkpoint와 실제 프로세스의 실행 자리·자원 소유 예약은 보존한다. 차단 계획에 의존하지 않는 작업은 기존 gate 안에서 계속하며, 독립 차단 자체는 단계별 유지보수 평가를 막지 않는다. |
| 수동 구현 대기 | 독립 READY 실행을 막지 않지만 소유 branch·worktree·문서 예약은 유지하고 새 유지보수 cycle은 보류한다. |
| 일반·유지보수 planning의 작업 소유·미완료 게시 | 실제로 점유한 실행 자리와 공유 안전 조건을 유지한다. 오류와 결과를 안전하게 보존해 자리를 비운 뒤에는 유효성이 확인된 독립 작업을 계속할 수 있다. |
| PROJECT_BLOCKED | 새 Planner·maintenance Planner·Worker와 일반 저장소 변경을 시작하지 않는다. |

차단된 계획은 다른 작업이 실행 중이어도 대상별로 재평가할 수 있다. 같은 계획의 실행·복구 소유권과 실제 공유 자원 충돌은 보호한다. 명세·작업 본문 변경은 재검사 계기이며, 사용자 판단 차단은 최신 근거가 기존 질문을 해소한 경우에만 재개한다. 문서 수정이나 질문 삭제를 승인으로 간주하지 않으며, 의미 판단의 미완료·불확실성은 기존 차단을 유지한다. rebase 입력 차단은 코드 수정 뒤 안전한 시작이 가능해진 경우에도 재검사하며 문서 변경을 요구하지 않는다.

단계 처리의 일반 예외는 상세 오류를 남기고 해당 작업의 현재 상태·안전 조건을 다시 확인해 재시도한다. 같은 실행에서 3회 실패하면 그 작업만 보류하고, 작업 준비·결과 수집 예외도 독립 작업의 배정을 멈추지 않는다. 사용자 중단·Planner 배정 중단·runtime 교체는 기존 제어 흐름을 유지한다.

같은 프로젝트에 여러 READY ExecPlan이 대기할 수 있다. 현재 단계가 허용한 후보 중 모든 `depends_on`이 MERGED이고 관리 한도와 소유권·draft·catalog 보류·프로젝트 차단 조건을 통과해야 시작한다. dependency를 충족하지 못한 READY만 남아도 [Planner 호출 조건](#planner-호출과-기존-작업-개정)이 있으면 해당 판단을 수행한다. 그 조건이 없으면 대기하며 dependency 대기나 코드·ExecPlan 변경만으로 Planner를 호출하지 않는다.

일반 자동 작업은 G별로 고유하고 결정적인 branch와 별도 관리 worktree를 사용한다. 수동 submit의 복수 G는 같은 묶음의 소유 branch·worktree·최종 후보·PR을 공유하는 예외이며 [수동 명령의 준비와 수락](#수동-명령의-준비와-수락)을 따른다.

필수 종료·Git·게시 정리를 먼저 확인한 뒤 실행 가능한 수락 수동 제출, 일반 RETRY 우선 재배정, 기존 자동 작업의 재개·교정·복구, 일반 신규 순으로 배정한다. 수동 제출은 프로젝트 구분 없이 최초 수락 순서를 교정·verify·Review·게시까지 유지한다. 지금 진행할 수 없는 제출 때문에 다른 적격 작업의 자리를 예약하지 않는다.

일반 RETRY는 같은 작업의 관리 상태를 보존해 실행 대기열에서 수동 제출 다음, 다른 일반 작업보다 먼저 재배정한다. 이미 실행 중인 호출을 선점하지 않고 실행 상한·verify 순번·필수 안전 조건과 기존 교정 한도를 지킨다. 계획 차단은 해당 계획만 제외하며 독립 작업은 같은 실행에서 계속한다. Planner가 필요하면 해당 프로젝트의 신규·후속 배정을 멈추고 실행 중 호출이 안전하게 반환한 뒤 결과 게시·DB 반영을 거쳐 재개한다. runtime 교체는 전체 프로젝트에 같은 배정 중단을 적용하고 상태를 보존해 다음 외부 실행으로 넘긴다.

일반 신규는 관리 작업이 없는 프로젝트에 한 자리씩 먼저, 이후 전역 유효 우선순위와 숫자 G 순으로 배정한다. 실제 신규 호출도 해당 종류의 호출이 없는 적격 프로젝트를 먼저 순환 배정하며 마지막 배정 위치는 재시작에도 유지한다. 후보가 없는 프로젝트를 기다리는 장벽은 두지 않는다. 한 자동 실행 안의 Planner 호출은 프로젝트 간에도 직렬화하며, 별도 수동 명령의 준비는 해당 자원·소유권 경계를 따른다.

PROJECT_BLOCKED의 원격 복구·보존 Git 쓰기는 연결 계획이 기존 실행 자리를 점유했거나 자리가 비어 있고 현재 작업 범위를 독립적으로 검증한 경우에만 해당 관리 worktree에서 허용한다.

#### 우선순위와 유지보수 선택

Planner는 priority를 선언할 때 안전한 무인 실행을 막거나 반복적인 사용자 복구를 요구하는 작업, 정상 경로의 사용자 개입을 제거하는 핵심 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 제품 중요도를 판단한다. Manager는 이 판단을 모델로 반복하지 않고 검증된 catalog의 선언·dependency·상태로 실행 순서를 계산한다. 우선순위는 실행 조건을 통과한 후보 사이에 적용하며 현재 작업을 선점하지 않는다.

- 일반·재개·planning 오류 보존 중 독립 READY와 유지보수 prerequisite는 기존 단계가 허용한 실행 후보 안에서 유효 우선순위, 숫자 ExecPlan ID 오름차순으로 선택한다. 유효값은 게시·검증된 같은 프로젝트 catalog에서 자신의 선언값과 자신을 직접·간접 미Merge dependency로 필요로 하는 미완료 목표의 선언값 중 가장 높은 값이며, MERGED에서 전파를 끝낸다. priority 생략은 normal이고 critical·high·normal·low 순서를 유지한다

  예를 들어 critical 목표가 아직 Merge되지 않은 low 선행 작업을 필요로 하면 그 선행 작업의 유효 우선순위도 critical이 된다. 선행 작업의 원래 low 선언과 dependency는 그대로 유지한다.

- 선언값과 dependency·실행 상태는 전파로 변경하지 않는다. 검증된 priority·dependency·catalog 추가/삭제와 실제 Merge 반영 뒤 다음 안전한 선택에서 다시 계산하며, 선택 로그는 선언값·유효값·상향 목표와 dependency 경로를 기록한다. 격리·미확정·유효하지 않은 관계는 기존 검증·보류 경계를 유지한다
- 활성 유지보수 prerequisite 집합은 dependency gate 뒤 유효 우선순위와 숫자 ID 순으로, 리팩터링 DAG는 dependency gate 뒤 숫자 ID 순으로 선택하며 집합 밖 READY는 cycle이 끝날 때까지 시작하지 않는다. 이때도 `PROJECT_BLOCKED`는 일반 실행을 막고, 다른 `PLAN_BLOCKED`·`PR_OPEN`과 dependency로 연결된 계획은 건너뛴다
- 확정 범위의 마지막 계획 Merge와 필요한 원격 관측·게시·catalog 반영 뒤 cycle을 종료하고 장벽 뒤 READY의 일반 선택으로 돌아간다. 별도 사후 재검증이나 이를 대신하는 일반 Planner 호출을 종료 조건으로 추가하지 않는다
- PLAN_BLOCKED 또는 안정된 PR_OPEN이 있어도 [Planner 호출 조건](#planner-호출과-기존-작업-개정)을 먼저 평가한다. 해당 조건이 없으면 독립 READY 하나를 유효 우선순위와 숫자 ID 순으로 시작하며, 없으면 대기·완료 조건을 평가한다. 코드·ExecPlan 변경만으로 새로운 계획 호출 조건을 만들지 않는다

#### 자체 runtime handoff

비정상 종료로 미완료 SQLite rollback journal이 남으면 self base 조회에서 해당 오류만 제한적으로 복구한다. 기존 데이터의 SQLite 롤백만 허용하며 DB 초기화·등록 변경이나 다른 권한·손상 오류의 우회로 사용하지 않는다. rebase의 이전 실행 이력은 자기 중첩 없이 보존해 반복 복구로 실행 상태 크기가 배증하지 않게 한다.

- Planner·Worker·부분/전체 verify·Reviewer·게시·복구의 실제 단계 진입 전에, 현재 runtime의 등록 self base 원격의 실행 입력을 실제 실행 SHA와 비교한다. 최초에는 즉시 확인하고 이후 성공 관측 완료부터 30초 미만이면 같은 runtime의 병렬 작업도 결과를 재사용한다. 관측 실패는 성공으로 캐싱하지 않는다. 단계 종료와 재시도·사용량·기존 호출 종료·자원 대기 중에는 원격 조회하지 않으며 pause·이미 확정된 종료 신호는 계속 처리한다. 대기 해소 뒤 단계 진입에서 다시 관측 시점을 판단한다. self가 실행 선택 범위 밖이어도 확인하며 PR 상태·조회와 로컬 source 동기화는 교체 조건이 아니다.
- 일반 문서만 전진하면 계속 실행한다. 실행 소스·스크립트·의존성·설정·공통 및 역할 지침이 바뀐 검증된 fast-forward 후속은 전체 launch gate를 닫고 이미 시작한 호출의 반환·정리·저장을 기다린다. 문서 경로 안의 실행 코드와 불명확한 입력은 제외하지 않는다.
- 부모는 소유 인계 자료와 실제 종료를 검증한 뒤 process lock을 유지하며 최신 원격을 다시 선택해 같은 논리 실행을 재시작한다. session·실패/교정·targeted 이력·예약·재시도 대기를 보존하고 동일 실행 입력의 무진전 재시작을 거부한다. 전체 pause는 재선택·새 runtime 시작을 막는다. identity·fetch·ancestry·비교 또는 종료·저장 미확인은 성공으로 처리하지 않고 자료와 기존 작업 상태를 보존한다.

### 유지보수 평가

**평가 시작**

- 기존 활성 cycle을 먼저 복구·완료하고 과거 성공 이력은 유지한다. 주기 도달·명시적 요청으로 새 cycle을 시작할 때는 전체 모듈의 `full` 점검을 선택한다. 정적 비교의 `fast`·`targeted` 지원을 별도의 cycle 시작 조건으로 사용하지 않으며, 재개 시에는 같은 cycle의 확정 범위와 필요한 관련 근거만 다시 확인한다.
- 프로젝트 최초 실행은 일반 모드의 Planner·READY 선택으로 시작한다. 유지보수 평가 이력이 없다는 이유만으로 자동 cycle이나 정밀 점검을 시작하지 않는다. 첫 자동 유지보수의 주기는 최초 실행 시 확인한 MERGED 수를 기준으로 계산하고 재시작에도 그 기준을 보존한다.
- 자동 유지보수는 첫 주기 기준 또는 직전 cycle 정상 종료 시 마지막 유지보수 Merge까지 포함한 MERGED 수에서 `refactoring_review_interval`만큼 증가했을 때 시작한다. `[loop]`의 기준값은 변경 가능한 0 이상의 정수이며 기본값 20을 유지한다. 0이면 자동 전환을 끄고 새 명시적 `maintenance-review <repo>` 요청은 0·최초 실행·주기 도달 전에도 다음 안전한 경계에서 전환할 수 있다. 값 변경은 누적 Merge 기준을 초기화하거나 활성 cycle을 중단하지 않는다.

  첫 정밀 점검은 첫 유지보수 cycle에서 수행한다. 활성 작업·실제 Open PR·프로젝트 안전 gate와 해당 Requirement의 즉시 재확인을 처리한 뒤 주기가 도래했거나 명시적으로 요청된 유지보수를 일반 작업 선택보다 먼저 평가한다.

**한 번의 평가로 범위 확정**

- 한 번의 평가는 한 cycle의 모듈 묶음 검토·필수 루트 조사·필요한 종합과 계획 작성을 포함하며 여러 모델 호출로 수행할 수 있다. 발견을 새 계획·기존 비종료 계획·근거 있는 제외에 연결해 prerequisite·리팩터링과 필요한 dependency closure를 확정한다. prerequisite Merge나 개선 결과로 새 평가를 시작해 범위를 늘리지 않는다. 주기 전 코드 영향·평가 receipt와 최신 원본의 차이·완료 후보는 자동 평가의 시작 조건이 아니다.
- 각 tiered 평가는 소스 모듈뿐 아니라 문서·자산·manifest·설정까지 추적된 모든 루트 항목의 실제 참조·사용 경로·소유권·유지 이유와 안전한 목적지를 조사한다. 소스 모듈이 없는 tree도 조사 누락이 있으면 완료할 수 없다. 이동 후보는 동작·공개 계약·저장 형식과 경로·배포·설정 참조를 보존하는 새 계획 또는 해당 목표를 포함한 비종료 계획에 연결하고, 계획이 원격 게시·catalog에 반영되기 전에는 평가 성공을 확정하지 않는다.
- 유지보수 Planner는 필요한 선행과 리팩터링 DAG를 판단한다. Manager는 새 리팩터링 DAG의 모든 말단을 필요한 선행 dependency closure 밖의 적격 READY snapshot에 추가한다. 이 편집은 기존 dependency 순서와 계획 본문·metadata를 보존하며, 실행 이력·소유권·PR이 있거나 cycle·snapshot 범위 밖인 계획에는 적용하지 않는다.
- 원본에 결합된 성공 모듈 검토는 해당 묶음의 입력이 같을 때 재사용한다. `fast`는 모듈 의미 검토를 생략하지만 필수 루트 조사 근거가 없으면 별도 모델 호출이 필요할 수 있다. 검토에서 발견이 없고 일반 계획을 함께 수행할 필요도 없으면 별도 종합·계획 호출을 하지 않는다. 발견이 있으면 새 계획·기존 비종료 계획·근거 있는 제외에 연결해 처리한다.

**종료와 재개**

- 리팩터링은 동작·공개 계약·저장 형식을 보존한다. 각 계획의 targeted test·전체 verify·Review·사용자 Merge를 유지한다. 평가 성공은 cycle 완료와 다르며, 새 계획과 발견을 연결한 기존 비종료 계획 및 필요한 dependency closure가 모두 실제 MERGED이고 원격·catalog에 반영된 뒤 종료한다. 수행할 작업이 없거나 근거 있는 제외만 있으면 검증·필요한 게시 뒤 종료한다. 유지보수 종료는 프로젝트 전체 완료를 뜻하지 않는다.
- 평가 성공 원본과 cycle 종료 주기는 별도로 보존하며, 다음 자동 주기의 Merge 기준은 cycle이 정상 종료할 때만 전진시킨다. 수정 뒤 검토하지 않은 원본의 receipt를 합성하지 않는다. 명시 요청은 cycle 시작에 identity를 결합해 한 번 소비하며 활성 cycle 중 새 요청은 다음 안전한 경계까지 보존한다. 중단·원본 변경은 같은 cycle의 미완료 게시와 필요한 관련 근거만 재검증한다.
- 종전 파일별 평가·게시 결과를 보존한 cycle은 해당 검증·복구 경로로 마친다. 그 이력과 완료 후보의 종전 안정화 판정을 새 단계별 평가의 기본 반복 조건으로 사용하지 않는다.

### 복구 판단과 최종 차단

Manager는 원인과 현재 작업 상태로 복구 방법을 먼저 판단하고, 사용자 조치가 필요한 최종 차단에서 원인 code에 따라 `PLAN_BLOCKED`·`PROJECT_BLOCKED` 범위를 정한다.

| 조건 | 처리와 재개 |
|---|---|
| 일시적 Git·서비스 실패 | 같은 실행에서 원인별 한도 내 재시도하고, 계속 진행하기 어려우면 작업을 보존해 다음 실행에서 재검사한다. 최신 원격 snapshot이 안정되지 않는 경우도 일시 중단한다. |
| 최신 base·catalog·실제 소유 Git 상태로 다음 단계를 확정할 수 있음 | 현재 상태를 재구성한다. 정상 Git 변경과 과거 snapshot의 차이만으로 최종 차단하지 않는다. |
| Agent 교정이 가능한 오류 | 원인별 진전·반복 한도로 교정한다. Requirement 이동 실패는 planning 작업에 보존한다. Manager가 처음 관측한 rebase 충돌과 Worker 결과 형식 오류는 다음 실행에서 보존된 Worker 세션으로 재개하며, 진행 중인 Worker·검증·Review 교정은 같은 실행에서 이어갈 수 있다. |
| 사용자 조치가 필요하거나 최종 교정 한도를 소진함 | 해당 원인의 복구·사용자 조치 경계에서 최종 차단한다. 승인·PR reopen·현재 작업 대상·원격 OID 불확실성, 실제 sandbox 정책 실패, 안전한 원본을 확정할 수 없는 손상, verify·rebase·Worker 결과 형식·Reviewer 프로토콜의 최종 한도가 해당한다. |

일시 재시도·상태 재구성·한도 내 Agent 교정은 현재 phase·branch·worktree·소유권·Agent session·승인·변경·검증 원문·실패 이력을 보존한다. 이 과정의 일시 중단은 차단 상태·차단용 원격 checkpoint·BLOCKED 알림을 만들지 않는다. 관리 rebase의 중간 게시는 [별도 게시 경계](ARCHITECTURE.md#중간-checkpoint-게시와-원격-입력)를 따른다. Review 유발 Worker 교정의 실행별 한도 도달도 [작업을 보존하는 일시 중단](#review-교정과-level-승격)으로 처리한다.

최종 차단은 검증 가능한 해당 작업만 안전한 원격 checkpoint 또는 진단 게시 대상으로 삼는다. BLOCKED 알림은 최종 차단 확정과 checkpoint 결과 뒤에 생성하며 같은 plan·scope·reason·fingerprint·revision은 한 번만 알린다. 최종 기록에는 복구 횟수·마지막 안정 revision·남은 불확실성·실제 조치 대상을 포함하고, 원인이 실제 수정 대상을 제공할 때만 사용자 수정 경로를 표시한다.

### Model과 Effort 선택

Planner가 ExecPlan의 complexity·risk를 판단하고 프로그램이 두 점수의 합으로 최초 Worker level을 선택한다.

| 항목 | 점수 |
|---|---|
| complexity | low=1, medium=3, high=5 |
| risk | low·medium·high=0, critical=1 |
| 최초 Worker level | 두 점수의 합인 1~6 |

level 7·8은 [Review 교정과 level 승격](#review-교정과-level-승격)에 따른 승격으로만 도달한다.

**명시적 profile과 이력**

- 선택 provider의 Worker level 1~8은 `config/config.ini`에 Model alias·Effort와 참조 alias의 실제 Model 이름을 모두 명시해야 한다. 누락·빈 값·잘못된 형식·CLI 미지원 조합은 Worker 시작 전 오류로 보고하며 코드·CLI 기본값이나 다른 profile로 보충하지 않는다. 예시 설정은 선택 규칙이나 fallback이 아니다
- 실제 provider·Model·Effort는 설정대로 호출·기록한다. 서로 다른 level에 같은 지원 조합을 지정할 수 있어 level 상승이 Model 변경을 보장하지는 않는다. Codex와 Claude의 level 8도 지정한 지원 조합을 사용하며 level 8보다 높은 자동 profile은 없다.
- [Review 교정과 level 승격](#review-교정과-level-승격)의 완료한 교정 결과만 effective level을 한 단계씩 올릴 수 있으며 작업을 재개·재시도하는 동안 낮추지 않는다. 과거 지문 횟수에서 교정 완료를 추정하지 않고, 관계를 확인할 수 없는 기존 기록은 다음 유효 Review부터 기준선을 시작한다.
- effective level과 level별 Review 승격 이력은 SQLite에 저장한다. 새 ExecPlan과 차단 해제 후 일반 재배정은 최초 level에서 시작한다. 원격 입력 반영·과거 오탐 해소로 기존 실행의 원인만 해제하는 복구는 session·실패·승격 이력을 보존하며, 적용 범위는 [계획별 해제와 원격 입력 수용](ARCHITECTURE.md#계획별-해제와-원격-입력-수용)을 따른다.

Worker의 profile 변경은 작업물·검증 이력을 보존하며 다음 기준으로 세션에 반영한다.

| 변경 | 세션 처리 |
|---|---|
| provider·Model·Effort가 같음 | 기존 세션 재개 |
| provider·Model이 같고 Effort만 변경 | 변경한 Effort로 기존 세션 재개 |
| provider 또는 실제 Model 변경 | 같은 작업 공간·작업 문맥을 보존해 새 세션으로 전환 |

이미 저장된 세션 전환이 진행 중이면 그 대상을 먼저 적용하고, 이후 설정 변경으로 진행 중 전환을 바꾸지 않는다.

**Fast 선택**

- provider별 Fast 선택은 on/off이며 기본값은 `false`다. 공란·공백·provider section 또는 항목 생략도 off로 처리하고 CLI 기본 설정이나 저장된 session의 Fast 선택보다 우선하도록 요청한다. 모든 역할·Worker level에 공통 적용하고 실행 시작의 선택을 신규·재개·재시도에 유지한다. 변경은 다음 실행부터 반영하며 Fast 선택만으로 session·작업물·검증 이력·Worker level을 초기화하지 않는다
- Fast 선택은 모델·Effort와 독립적이다. Manager는 모델별 Fast 지원을 사전 판정하거나 모델을 교체하지 않고 지원·대체 실행과 진단을 CLI에 맡긴다. 호출 기록의 Fast 선택을 실제 적용 성공으로 간주하지 않으며 CLI 실패는 기존 규칙대로 처리한다. Fast 사용에 따른 추가 비용·사용량 증가와 CLI 버그로 인한 미적용은 provider 정책을 따르고 별도 우회하지 않는다

### Agent 호출 기록

invocation ledger는 각 호출의 목적·대상과 실제 실행 근거를 연결한다. 호출자는 가능한 대상 계획·task·batch·파일을 명시하고, 역할·provider·실제 Model·Effort·session 전이·재시도·결과·사용량·실행 구간을 같은 호출에 기록한다.

- 호출 준비와 실제 CLI 프로세스 실행을 구분한다. 프로세스 생성이 확인된 시도만 실제 호출 수에 포함하며, 실행 후 실패한 호출도 집계한다. 실행 전 거부·프로세스 생성 실패는 미실행으로, 시작 경계가 불명확한 중단·legacy 기록은 미확인으로 보존한다. 과거 호출의 목적이나 실행 여부를 추측하지 않는다.
- 실제 실행이 확인된 호출의 수집 가능한 사용량·실행 시간만 집계한다. 누락값은 0으로 채우지 않으며 일부 수집값의 합계를 전체 사용량으로 해석하지 않는다. 호출 처리 전체의 경과 시간과 실제 프로세스 실행 시간을 구분하고, 종료를 확인하지 못한 실행 시간은 미확인으로 둔다.
- 프로세스 종료 확인과 호출 결과·결과 검증은 별개다. 종료가 확인된 실패·중단·수집 오류는 Requirement 교정을 계속 보류시키지 않으며 성공 결과로 바꾸거나 재사용하지 않는다. 재시작 때 호출별 종료 근거를 확인·보존하고, 임의의 종료 코드나 후속 호출 성공으로 이전 writer 종료를 대신하지 않는다.
- Codex는 신규·재개 모두 Agent 이벤트·출력 수신 후 10분 무출력을 감지한다. Manager 진행 로그와 저장량 제한은 타이머에 영향을 주지 않는다. 실행 중인 명령은 기존 전체 제한 시간까지 기다리고, 그 밖의 무응답과 턴 완료·프로세스 종료 뒤 결과 수집 대기는 구분해 종료 확인 후 기존 재시도 정책으로 처리한다.
- ledger에는 prompt·비밀값·인증 자료·코드·문서 전문을 저장하지 않는다. 선택적 실행 관측 저장 실패는 Agent 결과를 바꾸지 않으며, 필수 상태·격리·소유권·검증 증거의 저장 조건은 유지한다. 필수 테스트 결과의 수집 실패를 검증 성공으로 취급하지 않는다.

실행 상태·집계 형식과 상세 기록 연결은 [호출 관측과 ledger](ARCHITECTURE.md#호출-관측과-ledger)를 따른다.

### 운영 로그

운영 로그는 단계의 실제 시작·결과, 선택·생략·재사용·재시도 이유, 장시간 진행·대기와 다음 행동을 프로젝트·작업·실행별로 확인할 수 있게 한다. 프로젝트 결과와 전체 실행의 수준은 실제 결과 종류를 따른다. 완료·예상된 대기·사용 한도 대기·runtime 인계는 INFO, 자동 재검사가 가능한 일시 중단은 WARN, 최종 실패·차단은 ERROR다. 전체 실행은 프로젝트별 결과의 가장 높은 수준으로 요약한다. 이 분류는 기존 종료 코드·SQLite 전이·실패 및 Telegram 정책을 바꾸지 않으며, 복구 요청 접수 뒤 대기·진행 중인 비정상 종료 코드만으로 최종 실패를 뜻하지 않는다.

짧은 처리는 소요 시간과 결과를 종료 한 줄로 요약하고, 긴 처리는 30초 주기의 진행 관측을 추가한다. verify 진행에는 현재 run·stage·unit·test, 실제 관측된 완료 수와 자원 대기를 표시한다. 결과 파일 확인·Windows Job 자식 종료 대기·다음 배정·worker 정리는 서로 다른 단계로 남기며 WSL 전체와 Windows native 결과를 각각 연결한다. `verify` phase는 stage identity가 바뀌어도 시작과 종료를 연결하며, FALLBACK·예외 종료 뒤 완료된 phase를 실행 중으로 표시하지 않는다. Agent의 실제 프로세스 시작처럼 별도 판단에 필요한 시작 사건은 관측 시점에 기록한다. 준비·실제 실행·결과 검증·원격 반영을 구분하며 미확인 결과나 진척률을 추측하지 않는다.

수동 start/complete/submit과 `recover`는 확인된 저장소·계획 또는 요청 범위를 로그 문맥에 연결한다. 접수·수락·실행 대기·실제 처리 결과를 구분하며, 범위가 바뀌거나 처리가 끝나면 이전 계획 문맥을 이어 쓰지 않는다. 병렬 프로젝트도 각자 문맥을 유지한다. `document-drafts show/generate/create`는 기존 문서 원문·diff bytes를 stdout으로 출력하고 bootstrap 부모와 runtime 자식의 운영 로그는 stderr로 보낸다. 저장소 인수에 우연히 포함된 옵션 문자열은 출력 계약을 바꾸지 않는다.

운영 로그·선택적 관측의 실패는 성공한 동작을 실패로 바꾸거나 재호출하지 않는다. 필수 상태·격리·소유권·검증 증거가 없으면 해당 성공을 인정하지 않으며 기존 실행·검증 경계에서 처리한다. 공통 포맷·추가 판단 기준·비밀값 보호·상세 기록 연결은 [운영 로그 정책](ARCHITECTURE.md#운영-로그-정책)을 따른다.

### PR 생성과 Merge 후 정리

자동 구현 후보의 새 PR 생성·기존 PR 갱신은 다음 조건을 함께 확인한다. 중단 뒤 기존 PR을 연결하거나 이미 완료된 Merge를 관측하는 일은 새 후보 게시와 구분한다.

| 확인 대상 | 게시 조건 |
|---|---|
| 작업·후보 | 최신 원격 base를 반영한 현재 후보가 작업 입력·완료 Worker 결과에 부합하고 게시할 변경이 있음. 관련 targeted test는 성공하거나 실행 가능한 테스트가 없는 사유가 있어야 한다. |
| 전체 verify | 현재 후보에 유효한 전체 검증 성공 근거가 있음. 아래 재사용 조건을 충족한 기존 run은 연결할 수 있으며 부분 PASS만으로 통과하지 않는다. |
| Review | blocking issue가 없고 현재 후보의 Review 승인 근거가 유효함. 일반 선행 Review의 최종 재사용은 아래 조건을 모두 충족해야 한다. |
| 문서 보고 | [PR 문서 보고](#pr-문서-보고)가 최종 diff와 일치함. |
| 게시 대상·안전성 | 관리 worktree·branch·PR identity·원격 head와 lease를 다시 확인함. 최신 base·후보·대상이 바뀌면 필요한 통합·검증·Review 판단으로 돌아간다. |

게시할 변경이 없으면 빈 PR을 만들지 않고 계획 입력 오류로 보존한다. 검증과 결과 재사용의 구체적인 조건은 아래 절을 따른다.

Worker는 의미 있는 작업 단위의 checkpoint를 요청하고 종료한다. Git add·commit·fetch·rebase·push·PR 조작은 Manager가 전담한다. Manager는 종료한 호출의 작업·branch·worktree·시작 HEAD와 보호 문서·선언·index를 확인하고 허용 파일만 commit해 정확한 원격 OID 또는 branch 부재에 결합한 lease로 게시한다. 중간 보존점과 완료 후보는 별도 결과이며 checkpoint만으로 Worker 완료·targeted test·verify·Review 성공을 기록하지 않는다. 과거 직접 commit도 각 중간 commit의 실제 변경 범위를 확인해 게시하며 최종 diff에서 되돌린 금지 변경은 게시하지 않는다. 생성·게시·결과 저장 중단에는 같은 요청·commit·session을 재관측해 완료한 효과를 반복하지 않는다. intent와 게시 완료의 DB 확정은 잠금 경합 시 변경 전체를 되돌리고 최신 실행·소유권·checkpoint를 다시 확인해 제한 재시도한다. DB 대기 한도 소진은 실행·교정 실패 횟수를 소비하지 않는 일시 대기이며, 취소도 원래 요청과 이미 확인한 게시 효과를 보존한다. 게시 실패에는 commit·index·작업물·실패 근거를 보존하고 해결되기 전 새 Worker 작업을 진행하지 않는다.

원격 작업 branch가 먼저 전진하면 Manager는 저장소·작업·branch 소유권과 정확한 원격 OID를 재확인한다. 안전한 충돌 없는 rebase를 할 수 있으면 원격 commit을 보존해 통합한 뒤 변경된 후보의 검증·Review 근거를 다시 판단한다. 충돌·lease 경쟁·소유권 불명에서는 양쪽 commit을 보존하고 작업을 보류한다.

전체 프로젝트 DB가 없는 새 환경만 기존 원격 branch를 인수할 수 있다. 활성 ExecPlan 선언·작업 ID·등록 저장소·base·정확한 `loop-manager/{G ID}` branch와 원격 HEAD가 일치해야 하며 PR이 있으면 저장소·base·head도 일치해야 한다. 열린 PR은 같은 PR과 branch를 이어가고 검증 근거가 없으면 필요한 검증과 Review를 다시 수행한다. MERGED PR은 merge 결과가 현재 base에 포함됐는지와 무관하게 완료 처리하고 Worker를 실행하지 않는다. 닫혔지만 merge되지 않은 PR은 자동 재사용하지 않고 명시적인 재개 결정을 기다린다. 이 인수는 이전 환경의 Worker가 멈춘 순차 전환을 전제로 하며 동시 개발을 지원하지 않는다.

#### 작업 공간과 ignored 파일 보존

- 시작 ignore 규칙으로 증명된 index-free ignored 파일의 생성·수정·삭제는 역할별 결과·planning·검증 입력 오류의 단독 원인이 아니다. tracked·staged·non-ignored 후보와 실제 Git 작업의 index 안전 조건·대상 연결·원격 lease는 계속 확인한다.
- Worker targeted test → Manager verify → Reviewer는 같은 ExecPlan worktree에서 이어간다. ignored 확장자 allowlist·원상 복원 의무·별도 검증 worktree를 추가하지 않는다. 활성 ignored 파일과 들어오는 tracked 경로가 겹치면 파일을 보존하고 해당 Git 쓰기만 보류한다.
- 원격 작업 통합은 최종 diff에서 사라진 중간 replay 경로와 대소문자·부모/자식 별칭도 보호한다. 보존 commit의 HEAD/index 적용·rebase 시작과 후속 입력의 소유 abort 직전에 현재 충돌을 다시 확인한다. 충돌이나 관측 실패에는 파일·진행 operation·보존 intent를 유지하며 완료 결과의 기록 수용은 쓰기를 반복하지 않는다.
- 저장된 ignored-only 오탐은 시작 규칙·역사 결과·현재 소유권과 권위 상태를 검증해 기존 session·결과·단계에서 재개한다. 실제 위반·증거 부족·새로 숨긴 경로·강제 stage·독립 차단은 이 규칙으로 해제하지 않는다.
- 정상 종료한 정확한 Manager 소유 planning/ExecPlan worktree는 ignored-only 상태에서도 전체 수명을 종료할 수 있다. 기본 checkout·소유권 불명·미게시 commit·권위 변경은 보존하며 활성 artifact를 임의로 삭제하지 않는다.

#### 검증과 독립 Review

Worker는 현재 작업의 테스트를 명세·구현과 대조해 폐기된 동작만 검사하거나 다른 테스트가 같은 조건·assertion을 모두 보장하는 중복 테스트를 제거한다. 제거 근거와 남은 회귀 검증을 보고하며 실패·비용만으로 필수 검증을 약화하지 않는다. 유지보수 평가도 같은 기준으로 조사해 기존 비종료 계획 또는 새 정리 계획에 연결하고 Worker가 정리한다.

- Worker는 현재 환경의 전체 검증 스크립트를 생성·유지하고, 프로그램이 Agent 밖에서 실행한다. 최종 후보의 게시 판단은 위 조건표를 따르며, Reviewer는 검증 근거와 실제 변경을 읽기 전용으로 검토한다.
- 자체 verify는 직렬·프로세스 병렬 모드에서 동일한 unittest manifest·fixture 계약과 전체 compile을 유지한다. 실제 Agent 실행을 테스트 경계에서 차단하며 테스트·준비·대기·정리 시간과 중단 진단은 성공 stdout과 분리해 보존한다.
- 명시적 공통 verify 연결은 unittest와 Perl TAP adapter를 제공한다. 도구·연결 오류와 부분 결과를 성공으로 처리하지 않으며 두 언어는 같은 자원 예약·종료·진단과 필수 compile 순서를 따른다. Perl의 Agent 호출 차단·지원 버전·한계는 [VERIFY.md](VERIFY.md#perl-tap-지원과-두-언어-예제)를 따른다.
- 자체 verify는 실제 소스·테스트·고정 샘플·의존성·verify/copy/실행 스크립트를 관측한다. 문서·Skill·README·예제 설정의 정책·안내 문구 일관성은 검사하지 않고 문서 원본을 고정 샘플로 자동 동기화하지 않는다. 파서·등록·profile·파일 복사 검증과 필수 unittest·전체 compile은 유지하며 verify가 문서 품질을 보증하지는 않는다.

  prose 제외만으로 재사용을 허용하지 않고 아래 코드 입력 목록·누락 확인·실제 전체 성공 근거를 확인한다. 환경·runtime의 완전성 증명은 요구하지 않는다. 일반 코드 관측은 환경 증명 없이 재사용을 판단한다. 자동 문서 제외 확인과 Worker 소비 검토의 적용 범위는 [운영 입력 관측 adapter](ARCHITECTURE.md#운영-입력-관측-adapter)에 명시하며, 환경 증명 부재를 새 정책의 완료 조건으로 유지하지 않는다.
- `verify_parallelism`은 생략 시 `auto`, 양의 정수는 상한, `1`은 직렬이다. 프로그램이 CPU 제한·실제 부하·메모리 여유·미반영 예약으로 실행량을 정하며 등록 프로젝트 수로 고정 분할하지 않는다. 관측 실패는 직렬로 축소한다. 확인된 순번·자원 배정 대기는 timeout에서 제외하고 준비·수집·복사·호출·결과 처리·정리의 실제 경과 시간만 계산한다.
- Manager 인스턴스가 동시 verify의 확인·예약을 조율한다. 자식 종료·미시작을 확인한 뒤 예약을 해제하고 종료 불명은 보존한다. 종료 불명 호출과 미확정 Git operation·게시의 실제 실행 자리·자원 예약은 보존한다. 관리 자리 계산은 위의 작업 상태별 조건을 따르며 과거 시간 자료는 현재 검증을 대신하지 않는다.

가드·자식 환경·예약·결과 완결성은 [공통 verify 실행 경계](ARCHITECTURE.md#공통-verify-실행-경계), Git mock·실행 fixture 격리·복구 경계의 독립성과 비용 진단은 [자체 테스트의 Git 경계](ARCHITECTURE.md#자체-테스트의-git-경계)에 둔다. 테스트 skip·timeout 확대·수집 축소로 비용을 줄이지 않는다.

#### 부분 verify와 일반 선행 Review

- 자체 unittest의 부분 경로는 기본 사용하며 `[loop] verify_partial = false`로 끌 수 있다. 옵션 생략 시 true다. Worker 완료·후보 커밋/최신 기준 조정 뒤 빠른 부분 회귀 검사·일반 선행 Reviewer·실제 전체 verify를 수행한다. 모델 없이 변경 전후 전체 AST와 수집 명세로 변경 테스트와 가까운 관련 모듈을 우선 선정하고 공통 검사를 보완한다. 문서 병행 수정·분석 불확실성·고위험 공통 코드·넓은 영향만으로 전환하지 않으며 불확실성과 미선택 범위를 결과에 남긴다.

  부분 PASS는 선택 모듈만의 성공이다. 검증 자체의 계약 변경·후보/수집 명세 불일치·대상 부재/전체 필수 대상 또는 Reviewer 환경 증거 부재에서는 기존 전체 verify → Reviewer로 전환한다. Worker targeted test 제한과 최종 전체 검증·게시 조건은 유지한다.

- 일반 선행 FAIL은 전체 verify 없이 기존 Worker 교정·ISSUE_ID·level/반복 정책으로 연결한다. 일반 선행 PASS는 게시 승인이 아니다. 동일 작업 주기의 HEAD/base/전체 diff·명세·사용자 결정·실제 지침/profile·환경/정책과 현재 후보의 실제 전체 verify 성공 receipt가 모두 확인될 때만 추가 Reviewer 없이 최종 승인하며 게시 직전 재확인한다. 증거 부재·변경·실패·중단에서는 기존 최종 Reviewer 또는 안전한 재검증을 수행한다.

  R-082 문서 변경 선행 Review는 이 재사용 대상이 아니다. 부분 성공은 전체 verify token/receipt·verified_head를 만들거나 전체 검증을 생략하지 않는다.

#### verify 입력 목록 부재와 생성 계획

- 목록이 없거나 유효하지 않으면 현재 작업의 전체 verify를 실행한다. 목록 생성만을 위한 즉시 Worker·Planner 호출은 하지 않으며, 다음 일반 planning에서 최신 원격 base를 확인한 뒤 생성 계획을 요청한다. 유지보수 전용 planning은 이 요청을 포함하지 않고 일반 planning을 함께 하는 경우에만 포함한다.
- 같은 프로젝트의 미완료 계획·사용자 판단 대기 draft·미Merge PR이 있으면 새 생성 계획을 요청하지 않는다. 목록 생성 계획은 최초 등록에 `priority: critical`을 사용하며 다른 값의 신규 선언은 등록하지 않는다. 기존 유효 우선순위·dependency·유지보수 장벽을 따르고 실행 중 작업을 선점하지 않는다.
- 적용 전에 등록된 계획·draft·PR의 priority와 실행 상태는 그대로 유지한다. 새 조건만으로 기존 요청을 무효화하거나 대체 계획을 추가하지 않으며, 목록과 유효한 성공 증거가 확보될 때까지 전체 verify를 계속 실행한다.

#### 후보 갱신과 성공 결과 재사용

- Manager가 먼저 안전한 rebase를 시도하고 현재 후보의 구현·검증·Review 근거를 각각 재판정한다. 충돌 없이 완료되고 source 내용까지 포함한 구현 입력과 완료 Worker 결과가 같으면 SHA 변경만으로 Worker·완료한 targeted test를 반복하지 않는다. verify·Review 근거는 각 재사용 조건을 충족할 때만 연결하며, 충족하지 않으면 필요한 단계를 수행한다. 실제 구현 입력 변경·교정 사유·미해소 충돌은 기존 Worker/session 경로로 처리한다.
- rebase 충돌 중 Progress 체크 상태와 명확한 기존 실행 결과만 바뀌고 구현 조건의 동일성이 증명되면 session·작업물·index·rebase 상태·retry·실패 감사를 보존해 해결을 이어간다. 절 이름만으로 목표·제약·검증 조건·사용자 결정을 제외하지 않으며 불확실한 기록이나 과거 원문 근거 부재는 기존 재확인·차단을 유지한다. 이 구분은 verify·Review 입력을 제외하지 않는다.
- 같은 프로젝트·ExecPlan·작업 주기의 실제 전체 verify 성공과 현재 후보의 저장소 코드 입력이 같으면 원래 run을 연결해 재사용한다. 소스·테스트·fixture·의존성 선언·검증 스크립트/선언의 bytes·경로·종류·mode와 목록·선택 진입점을 비교하고 dirty/untracked 변경도 반영한다. 목록 부재·오류·코드 입력 누락 의심·비교 실패·실제 변경과 유효한 성공 부재는 전체 verify를 실행한다.
- verify 재사용은 코드 입력 기준으로 판단하며 설치 도구·환경변수·사용자/시스템 설정·native runtime·외부 동적 입력·Git revision 값의 동일성을 요구하지 않는다. 그 변경이나 미확인만으로 재사용을 거부하거나 별도 검증 infrastructure를 요구하지 않으며 환경 재현성을 보장하지 않는다. Review의 문서·diff·지침·승인 조건과 R-086 일반 선행 승인 뒤 실제 전체 실행은 유지한다. 관측 계약과 현재 구현 차이는 [ARCHITECTURE.md](ARCHITECTURE.md#verify-입력-관측과-생성-요청)를 따른다.
- 목록 밖 문서는 현재 내용·경로·종류·mode와 실제 검증 코드의 소비 관계를 재확인한다. Worker의 명시적 소비 검토 근거는 현재 코드·진입점·판정 규칙과 정확한 경로 집합에 결합하며 문서 내용만 바뀌면 현재 관측으로 완전성 근거를 갱신할 수 있다. 코드·경로가 바뀌거나 누락·영향을 확인하지 못하면 전체 실행과 목록 확인을 유지한다. 운영/예제 설정의 내용·존재·부재는 명시한 정확 경로에서 제외하되 검증이 읽는 고정 설정/자료는 계속 비교한다. 과거 PASS·token·폴더·확장자만으로 제외하지 않는다.
- 새 verify 실행은 과거 성공의 재사용 자격을 먼저 무효화한다. 실행·재사용 판정 중 입력 변경, 새 실패, 저장 중단·경쟁과 다른 작업 주기의 결과로 현재 후보를 통과시키지 않는다. 생략은 새 invocation·성공 실행·미측정 절감량을 만들거나 실패 감사를 초기화하지 않는다. Review·게시 gate는 독립적으로 유지한다.
- 구현 PR 게시에는 Requirement 전체 완료나 archive 이동을 강제하지 않는다. Requirement 이동은 별도 planning 게시에서 최신 판단·원문·목적지와 원격 base를 다시 확인한다.
- 관리 rebase는 시작 전에 대상·소유권·target·원격/PR 연결·writer 경합·index 안전·보존 가능 여부를 확인하고, 파일·선언은 비교·복구 기준으로 기록한다. 최신 frontmatter 일치·역할별 전체 diff·문서/archive 최종 내용이나 과거 definition snapshot 존재를 시작 조건으로 요구하지 않는다. Worker 종료 후 해당 호출의 실제 변경을 검사하고, Git 완료 후 Manager가 고정 target의 선택 ExecPlan 선언을 복원한 뒤 전체 최종 결과를 검사한다. 중간 충돌 해결은 최종 후보로 판정하지 않는다. 자동·수동 해결·재시작·기존 차단 재평가에 같은 시점을 적용하며 과거 turn·stage·receipt·journal 완전성을 완료 조건으로 요구하지 않는다.
- 동일 계획의 기존 원격 작업 branch를 rebase한 결과는 다음 단계 전에 중간 checkpoint 게시 또는 현재 lease에 따른 보류를 확정한다. 새 원격 입력이 오면 낡은 게시 intent를 감사에 남기고 통합·재검증하며 새 lease만 끼워 넣어 push하지 않는다. 실제 push가 확인됐을 때만 완료를 기록하고 이미 확인한 push를 반복하지 않는다. 이 게시와 과거 receipt는 구현·verify·Review·PR 승인을 대신하지 않는다.

#### PR 생성과 Merge 후 작업 공간

같은 branch의 기존 Open·Closed·Merged PR을 조회해 연결하며 생성 응답 뒤 저장이 중단돼도 중복 PR을 만들지 않는다. Closed PR은 같은 PR의 reopen을 기다리고 PR은 사용자가 Merge한다.

GitHub의 Merge 확인으로 작업을 MERGED로 기록하고, worktree 정리와 ExecPlan archive는 별도로 처리한다. 정리 보류로 Merge 사실을 되돌리거나 검증된 후속 작업·프로젝트 완료를 막지 않는다.

| Merge 후 작업 공간 | 처리 |
|---|---|
| Manager 소유권이 확인되고 tracked·staged·non-ignored 변경, Git operation과 PR head에 포함되지 않은 로컬 commit이 없음 | 제거 직전 다시 확인해 관리 worktree를 정리한다. ignored-only도 허용하며 Merge·Squash·Rebase 방식과 원격 head branch 존재 여부에 의존하지 않는다. |
| 사용자 변경·미게시 로컬 commit·Git operation·소유권 또는 안전성 불확실 | 경로·원인을 `cleanup_pending`에 기록하고 보존한다. 소유권·안전 조건이 충족되면 재평가하며 소유권이 불명확한 경로를 자동 삭제하지 않는다. |
| 보존 경로가 사라짐 | 부재를 확인해 해당 정리 대기를 해제한다. |

현행 Merge 정리는 worktree 제거까지 수행한다. 로컬 관리 branch의 비강제 삭제 기준은 현재 정리 코드에 반영되지 않은 차이로 남기며, 원격 branch 자동 삭제를 수행한 것으로 기록하지 않는다. MERGED ExecPlan은 [별도 archive 게시](ARCHITECTURE.md#merged-execplan-archive)로 처리하고 실패 시 archive pending으로 보존한다.

게시 lease·중단 복구·후보 검증 토큰의 적용은 [관리 rebase와 후보 검증](ARCHITECTURE.md#관리-rebase와-후보-검증)을 따른다.

### 프로젝트 완료 조건

프로젝트 완료는 **최신 입력의 최종 전문 확인 성공**과 **남은 변경·대기 목표 없음**을 함께 만족해야 한다. 안정적인 작업 선택 경계에서 판정하며 실행 가능한 작업이 없다는 사실만으로 완료하지 않는다. 유지보수 종료 자체를 이유로 최종 확인 Planner를 호출하지 않는다.

#### 최종 전문 확인

입력 변경 검토를 마치고 비종료 계획·실행·PR·draft·차단·planning 오류, 미확정 planning 게시와 활성 또는 due 유지보수가 없는 완료 후보에서 일반 Planner가 최신 원격 PRODUCT(존재하면)·ARCHITECTURE와 남은 활성 Requirement 전문을 구현·테스트·검증 근거와 대조한다. 완료 후보는 최종 확인을 시작할 조건이며 프로젝트 완료 판정과 구분한다.

| 최종 확인 결과 | 처리 |
|---|---|
| 충족 | 근거와 입력 일치를 검증하고 필요한 원격 확인·게시 적용 뒤 최종 확인을 확정한다. 아래 완료 gate도 함께 통과해야 프로젝트 완료다. |
| 미충족 작업 발견 | 활성 명세 source에 연결된 후속 ExecPlan을 남기고 완료하지 않는다. |
| 필수 사용자 판단 | 질문·선택 영향을 가진 draft를 남기고 완료하지 않는다. |
| 근거 누락·입력 불일치·검토 또는 게시 실패 | 성공을 확정하지 않고 진단과 미완료 처리를 보존해 필요한 교정·재확인을 수행한다. |

최종 확인의 입력 지문·관측 revision·게시 확인 revision·작업 주기와 결과는 변경 검토·Requirement별 판단과 별도로 보존한다. 한 Planner 호출에서 여러 목적을 처리해도 Requirement 한 건의 충족이나 일반 검토의 추가 계획 없음을 프로젝트 전체 성공으로 취급하지 않는다. 현재 입력·주기에 유효한 성공만 재사용하며, 뒤이은 일반 검토 성공으로 이전 최종 확인이 유효해지는 것으로 추정하지 않는다. 원문을 보존한 Requirement·ExecPlan archive 이동은 선언과 이동의 유효성을 확인한 경우 기존 최종 확인을 재사용할 수 있다. 결과 형식·확정·재사용 계약은 [프로젝트 최종 확인과 완료 판정](ARCHITECTURE.md#프로젝트-최종-확인과-완료-판정)을 따른다.

#### 완료 gate

| 확인 대상 | 완료 조건 |
|---|---|
| 최종 전문 확인 | 현재 입력·작업 주기에 유효한 충족 결과가 확정됨 |
| 활성 Requirement | 남아 있지 않음. 충족 판단 뒤 archive 게시를 기다리는 문서도 완료를 막음 |
| ExecPlan·수동 작업 | READY·IMPLEMENTING·VERIFYING·REVIEWING·PUBLISHING·PR_OPEN·PLAN_BLOCKED·MANUAL_WORKING 등 비종료 작업과 수동 제출 대기가 없음 |
| catalog·프로젝트 차단 | catalog hold·PROJECT_BLOCKED 레코드가 없음 |
| planning | 일반·유지보수의 미해결 `planning_error`, 보존된 실행·미확정 게시가 없음 |
| 유지보수 | 활성 cycle·미확정 게시·due 평가가 없음. 수행한 cycle의 종료 근거는 보존하되 첫 주기 도달 전이나 자동 유지보수 비활성화 상태에 과거 평가·종료 이력을 필수로 요구하지 않음. 최신 코드와 평가 receipt의 차이만으로 추가 평가를 요구하지 않음 |
| draft | ExecPlan draft·사용자 판단 대기가 없음. Requirement에는 별도 초안 상태가 없으며 관련 draft는 Requirement archive도 막음 |
| 정리 대기 | Merge 후 `cleanup_pending`·ExecPlan archive pending은 완료를 막지 않음. 진행 중 planning 게시·복구와 구분 |

#### 완료 주기와 작업 소진 대기

| 선택 경계의 상태 | 결과·알림 |
|---|---|
| 최종 전문 확인과 모든 완료 gate 통과 | 프로젝트 완료를 기록하며 Telegram은 보내지 않는다. |
| 실행 가능한 작업은 없고 활성 Requirement·비종료 ExecPlan이 남음 | 안정적 작업 소진 대기. dependency·PR Merge·계획 차단·draft·Requirement의 대기 이유와 확인된 조치 대상을 기록하며 별도 대기 Telegram은 보내지 않는다. |
| 실행 가능한 READY·진행/복구 단계·일시 오류 자동 처리·유지보수 또는 최종 확인/게시 처리가 남음 | 해당 처리를 계속하거나 다음 실행을 기다리며 작업 소진 알림을 만들지 않는다. |
| PROJECT_BLOCKED | 프로젝트 차단 경계를 유지하고 별도 작업 소진 알림을 만들지 않는다. |

완료 주기는 유효 planning 입력에 연결하고 내용 보존 archive 이동·재시작과 구분한다. 같은 안정적 대기 원인은 재실행·재시작 뒤 같은 episode로 유지하며 작업이 가능해지거나 관련 입력·차단 원인이 바뀌면 상태를 갱신한다. Telegram 비활성화·전송 실패에도 완료·대기 결과와 이유는 실행 결과·운영 로그·DB에 남는다. 개별 사용자 조치 알림의 선택은 [Telegram 알림 조건](#telegram-알림-조건)을 따른다.

### Telegram 알림 조건

**활성화와 전송 시점**

Telegram token·chat ID가 모두 설정된 경우에만 알림을 생성·전송한다. 모두 비어 있으면 비활성화하며 일부만 설정하면 설정 오류다. 확정된 새 알림은 상태 transaction의 commit 뒤 같은 실행에서 다음 장시간 Agent·verify 전에 전송을 시도한다. 전송 실패로 작업을 무기한 대기시키지 않는다.

**알림 대상과 주의 수준**

| 사건 | 발생 조건·주의 수준 |
|---|---|
| 새 일반 ExecPlan 게시 | 원격 게시·catalog 반영에도 알림을 만들지 않는다. 실제 미해결 draft 질문은 아래 목록으로 전달한다. |
| draft 판단 목록 | 프로젝트의 현재 판단 대기 목록을 일반 알림으로 전달한다. 전체 건수와 ID·제목을 한 줄씩 표시하고 확인된 원격 문서 링크를 ID에 연결한다. 질문·선택 영향은 작업 문서에서 확인한다. |
| Worker 최초 시작 | 선택·호출 준비·실제 시작에서 일반·무음 알림을 만들지 않는다. |
| 통지된 차단 후 실제 재개 | 실제 시작 상태를 보존하며 일반·무음 시작/재개 메시지는 만들지 않는다. |
| 유지보수 시작·정상 종료 | 알림을 만들지 않는다. cycle 범위·실제 Merge 또는 후속 작업 없는 평가·필요한 게시 완료와 종료 상태는 보존한다. |
| PR 검토 후보 게시 | 새 PR 또는 기존 PR에 실제 새 검토 후보가 게시되어 현재 사용자의 검토·Merge가 남았을 때 프로젝트·ExecPlan·작업 내용·PR 링크를 일반 알림으로 전달한다. 충돌 진입, Worker 시작·실패, checkpoint push, 같은 후보의 재확인만으로는 알리지 않는다. 현재 PR·후보 조회가 실패하면 전송을 보류한다. |
| 확인된 차단 조치 | 현재 미해결 Worker 질문·명세 교착·미결 지침 승인, 닫힌 PR 재개, 삭제된 게시 branch의 명시적 재시작 또는 확인된 verify 상한 변경에 구체적인 근거가 있을 때 일반 알림을 전달한다. 상태·실패 횟수만으로 판단하지 않는다. 독립 프로젝트 차단·해제는 남아 있는 계획의 Worker 질문·지침 승인 근거를 대체하지 않는다. |
| 확인된 자동 복구 불가 최종 중단 | 재관측·재시도·자동 교정·조건 대기·다음 실행·안전 복구가 모두 불가능하다는 구체적 근거가 있을 때 원인·보존 상태를 일반 알림으로 전달한다. 사용자 행동이 확인되지 않았다면 조치를 요구하지 않는다. |
| 프로젝트 완료·안정적 작업 소진 대기 | [프로젝트 완료 조건](#프로젝트-완료-조건)에 따라 결과와 이유를 기록하며 알림은 만들지 않는다. |

일반·빠른·유지보수 Planner의 개별 호출 시작, Reviewer 시작·종료와 단순 단계 실패·자동 교정·재시도·자동 재개는 별도 알림을 만들지 않고 실행 결과·운영 로그·실패 이력에 보존한다. Worker가 미해결 targeted test를 보고한 진단은 반복돼도 단순 실패·지속 실패 알림으로 보내지 않는다. 실패·교정·외부 실행 횟수나 지속 실패 집계만으로 별도 알림을 만들지 않는다. `BLOCKED`·`FINAL_BLOCK`·`planning_error`·오류 문구·수정 대상 경로만으로는 사용자 행동 또는 모든 자동 복구 수단의 부재가 입증되지 않는다. 근거가 부족하거나 재시도·자동 교정·다음 실행 재개가 남으면 원래 오류·작업 상태를 보존하고 통지는 보류한다.

차단 메시지는 확인된 사용자 조치 → 작업·scope → 구체적 원인 → 확인 대상·질문 → 복구 상태·Checkpoint 순으로 구성한다. 실제 수정 대상이 있을 때만 경로를 안내하며 수정 행동·복구 성공을 추측하지 않는다. checkpoint 게시 시 원격 commit link·OID, 미게시 시 local-only 보존 이유와 중요한 오류 근거를 유지한다.

**사건의 중복 제거와 재개**

| 사건·변화 | 갱신·취소·재개 |
|---|---|
| 같은 차단 | plan·scope·원인·지문·revision으로 중복 제거한다. 일시 복구 대기로 미전송분을 취소했더라도 같은 조치가 현재 유효하다고 재확인되면 새 사건으로 남은 전송을 처리한다. 이미 전송을 완료한 같은 조치는 반복하지 않으며 이전 취소 감사는 보존한다. 이전 요청의 늦은 성공 확인도 같은 본문을 이어받은 조각에 반영하며 바뀐 질문에는 적용하지 않는다. 해제 뒤 별도로 발생·통지된 차단은 새 사건이다. |
| 같은 미해결 `planning_error` | 오류와 실행·보존 이력은 유지하되 별도 차단 통지를 만들지 않는다. 구정책 사건의 남은 조각은 취소하고 이미 전송한 기록은 보존한다. 반복 실행·재시작은 취소 통지를 되살리지 않는다. |
| draft 목록·판단 근거 변경 | ID·판단 identity·revision이 같으면 반복하지 않는다. 달라지면 현재 목록을 새로 알리고 오래된 목록의 미전송 조각은 취소한다. 빈 목록은 새 알림을 만들지 않으며 해소된 목록도 전송 전에 취소한다. |
| PR 후보·사용자 행동 | 같은 repository·PR·검토 후보·검토/승인·Merge 행동은 한 사건으로 처리한다. URL이나 head SHA 전진만으로 새 사건을 만들지 않으며, 실제 게시된 후보와 남은 행동을 확인할 수 없으면 알리지 않는다. PR 종료·후보 대체·행동 해소가 확인되면 미전송분을 취소하고 이미 전송한 조각과 감사는 보존한다. 같은 후보가 rebase 뒤 다시 게시되면 이미 전송한 조각을 반복하지 않고 남은 조각만 이어 보낸다. PR 조회 실패는 미전송분을 유지해 재확인한다. |
| 차단 원인 해제 | 해당 차단의 미전송 조각을 취소한다. 해제 사실만으로 재개 알림을 만들지 않는다. |
| 차단 해제 뒤 해당 작업의 실제 시작 | 해당 계획의 Worker·PR 처리 또는 프로젝트·Planner의 실제 시작을 기록한다. 통지 확인이 늦게 도착하거나 전송이 실패·중단돼도 시작/재개 메시지를 새로 만들지 않는다. |

생성과 각 미전송 조각 앞에서 현재 대상·원인·남은 조치·자동 복구 상태를 다시 대조한다. PR은 저장된 PR identity와 실제 검토 후보·사용자 행동의 현재성도 확인한다. 해소·대체된 근거와 이미 소비된 재시작 요청은 남은 전송을 취소한다. verify 상한 변경 요청은 현재 읽은 설정과 기록된 timeout 상한이 일치할 때만 유효하며, 설정이 바뀌거나 확인되지 않으면 이전 요청의 전송을 취소한다. 과거 중복 사건·전송 감사는 삭제하지 않는다. 구정책의 진행·완료·대기·재개 사건은 미전송·부분 전송 여부와 무관하게 잔여 조각을 취소한다. 이전 지속 실패 알림은 전송 직전 현재 차단과 대조해 차단 근거 부재·차단 알림 중복 모두 미전송 조각을 취소하고 이미 보낸 기록은 보존한다. 다른 기존 알림도 해당 취소 조건을 적용하며, 계속 유효한 미전송·부분 전송 건은 저장된 본문·주의 수준·진행률을 보존해 이어 보낸다.

**완료·대기와 전송 실패**

완료·대기 이유와 현재 사용자 조치는 실행 결과·로그에서 확인한다. 별도 완료·대기 메시지 없이도 실제 draft 판단 목록과 새 PR 검토 후보 요청은 해당 사건 경계에서 생성·전송한다.

고유 사건 키로 같은 알림의 중복 생성을 막고 전송이 확인된 조각은 반복하지 않는다. 실패·확인되지 않은 조각은 다음 실행에서 저장된 위치부터 재시도한다. Telegram 수신 후 전송 확인을 저장하기 전에 중단되면 같은 조각이 다시 도착할 수 있다. 메시지 분할·고정 본문·전송 확인의 저장 계약은 [Telegram 알림 구조](ARCHITECTURE.md#telegram-알림)를 따른다.
