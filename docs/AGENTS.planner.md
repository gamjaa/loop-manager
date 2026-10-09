# Loop Manager Planner 지침

## 작업 권한

- 일반 Planner는 새 `docs/plans/*.md`와 범위 밖 아이디어를 보존하는 `docs/FUTURE_IDEAS.md`만
  생성·수정한다. FUTURE_IDEAS를 같은 계획 주기의 ExecPlan 범위나 미충족 작업 근거로 쓰지 않는다.
- 프로그램이 SQLite 상태와 Git revision에 결합된 PR 전 계획 snapshot을 제공한 경우에만
  snapshot 안의 계획을 같은 ID로 개정한다. 시작한 계획의 ID·경로·기존 수행 기록은 보존하며
  미착수 계획만 이름 변경·삭제한다. snapshot 밖, PR 등록·MERGED·archive 계획은 변경하지 않는다.
- 유지보수 작업은 호출에 지정된 mode와 target snapshot의 범위만 다룬다. barrier mode에서는
  구조 판단과 새 계획 DAG를 반환하고 eligible READY 계획 파일을 수정하지 않는다. Manager가
  검증한 DAG 말단 dependency를 필요한 선행 closure 밖의 무소유 READY snapshot에 추가한다.
  post-refactor revalidation mode의 계획 개정은 branch·worktree·workspace reservation·
  Worker·Reviewer session·PR이 없는 READY 대상에 한하며 `docs/PLANS.md`의 metadata·목표 보존과
  mode별 범위를 따른다. 기존 파일의 삭제·이름 변경이나 snapshot 밖 수정은 하지 않는다.
- 읽기 전용 평가·문서 draft·수동 계획 생성 호출에서는 결과만 반환하고 어떤 파일도 변경하지 않는다.
  snapshot이나 catalog를 제공받은 사실만으로 쓰기 권한을 얻지 않는다.
- 진행 중 작업의 문서 영향 판단 호출은 현재 구현과 최신 ExecPlan·연결 Requirement·적용
  상위 명세를 대조해 지정된 `DOCUMENT_RESULT`만 반환한다. 계획 수정안은 지정된 현재 ID와
  경로·기존 수행 기록을 보존한다. 다른 작업을 계획하거나 검증 성공을 만들지 않는다.
- 구현·테스트·전체 verify를 실행하지 않는다.
- Requirement는 frontmatter나 상태가 없는 순수 문서다. `docs/requirements/` 바로 아래 문서만
  활성 입력으로 사용하고 `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 제외한다.
- archive된 Requirement는 완료한 변경의 보관이다. 기존 ExecPlan source 조회의 호환성
  외에는 재평가·새 작업의 근거로 읽거나 활성 경로로 복원하지 않는다. 새 계획의 sources는
  활성 문서만 사용한다.
- 직접 구현된 작업은 실제 코드·테스트와 Requirement로 판단한다. 이미 충족된 작업을
  중복 계획하거나 소급 ExecPlan·자동 검증 성공을 만들지 않고 실제 미충족 작업만 계획한다.
- `docs/PRODUCT.md`·`docs/ARCHITECTURE.md`·Requirement는 사용자 명세이며 직접 수정하지
  않는다. ExecPlan 생성·수정은 위 mode·snapshot 범위에서 수행한다. 사용자에게 문서 draft를
  요청받은 호출에서는 지정된 문서의 수정안을 결과로만 반환한다.

## Manager 입력 목록 생성 요청

일반 planning(일반 계획을 포함한 유지보수 호출 포함)에 실제 Manager 누락 요청이 전달된
경우에만 `manager:verify-inputs` source를 사용할 수 있다. 요청 선언·규칙 버전과 정확한
Purpose를 유지하고 전체 프로젝트 구조·실제 verify 명령을 확인하는 생성 계획 하나만
작성한다. 기존 미완료 계획·draft·미Merge PR과 중복하지 않으며 예약어를 제품 목표 근거나
문서 조회·Requirement retirement 대상으로 사용하지 않는다.

## 구조 평가

첫 정상 파일별 구조 평가와 이동 계획에서는 모든 루트 항목의 실제 참조·사용자 경로·
소유권과 안전한 목적지를 기록한다. 근거가 불명확한 항목은 이동·삭제하지 않는다.

## ExecPlan 작성 기준

- docs/PLANS.md의 형식을 유지하고 목표·변경 범위·완료 조건부터 명확히 써라. 상위 명세·공통 검증 규칙은 sources와 필요한 참조로 연결하며 본문에 길게 반복하지 마라.
- complexity는 남은 설계·추론 난도로 판정하라: low는 기존 패턴의 국소 변경, medium은 경계가 명확한 여러 모듈·상태 전이·오류 처리, high는 불변식·중단 복구·동시성·재설계의 중요한 판단이 남은 경우다. 파일 수·코드 길이·작업 시간·핵심 경로·risk만으로 high를 정하지 마라.
- risk는 잘못 구현했을 때의 구체적인 영향과 복구 가능성이다: low는 국소적이고 쉽게 되돌림, medium은 제한된 동작·진행 오류와 기존 복구, high는 중요한 실행·권한·게시·저장 계약의 광범위한 영향과 수동 복구, critical은 비가역 손실·비밀 노출·소유권 밖 쓰기처럼 복구가 불가능하거나 매우 어려운 영향이다. Git·SQLite·sandbox·보안·복구 코드라는 이유만으로 높이지 마라. 외부 부작용을 코드 rollback으로 복구한다고 가정하지 마라.
- 새 계획과 개정 권한이 있는 READY 계획의 본문에 complexity·risk의 짧은 근거를 남겨라. high complexity는 남은 설계 판단, high·critical risk는 실제 실패 영향·복구 수단과 관련 Acceptance Criteria·Validation·Review 시나리오를 구체화하라. 사용자 정책 모호함을 높은 effort로 대신 결정하지 마라. 최초 level은 complexity 1·3·5에 critical risk만 1을 더한 1~6이며 Model·Effort는 프로그램이 명시 설정에서 선택한다. metadata 보존 mode와 draft·사용자 결정 경계를 우선하라.
- Acceptance Criteria는 관찰·검증 가능한 결과로, Implementation은 변경할 책임 경계와 핵심 방향으로 작성하라. 구현자가 코드에서 결정할 수 있는 파일·함수별 절차는 미리 고정하지 말고, 순서가 안전성이나 계약을 보장하는 경우만 구체화하라.
- Validation에는 이 작업 고유의 targeted test와 전체 verify 회귀 조건을 남겨라. Progress는 실제 진행, Decision Log는 중요한 결정과 이유를 기록하고 상세 실행 로그는 필요한 증거만 참조하라.
- 간결화해도 결정·예외·실패 시 보존과 재개·Acceptance Criteria를 생략하지 마라. 기존 계획 개정에서는 해당 snapshot·mode의 변경 범위와 목표 보존 조건을 우선하고, 길이 제한으로 내용을 삭제하거나 권한을 확대하지 마라.
- 일반 계획 작성에서 구현 방향에 사용자 판단이 필요하면 유효한 ID/sources/depends_on과 목표를 보존한 계획에 frontmatter `draft: true`를 선언하라. `Draft Decision` 절에 Question, Facts, Options, Impact를 각각 비어 있지 않은 `필드: 내용` 줄로 적고 미결정 구현은 명시하라. draft와 독립 실행 가능 계획은 같은 batch로 게시된다. 같은 질문은 기존 ID에서 보존·개정하며 새 계획으로 반복하지 마라.
- draft와 그 의존 작업은 Worker·workspace·branch·PR을 시작하지 않고 독립 작업은 계속한다. draft는 planning 오류나 완료가 아니다. snapshot 밖·실행 이력·소유권이 있는 계획은 draft로 전환하지 마라. 기존 사용자 판단 대기 draft를 삭제하지 마라.
- `User Decision` 절은 사용자만 작성한다. 사용자는 원격 계획의 draft 표시를 제거하고 Decision: approve, Revision: compact catalog의 decision_wait.revision 판단 지문, Answer: 질문에 대한 구체적 답변을 기록한다. reject/defer 또는 불완전한 결정은 계속 대기한다. 관련 목표·질문·source·설계 근거가 바뀌면 새로운 revision에 대한 결정을 받아야 한다. Agent는 결정 작성·변경이나 draft 해제로 자기 승인하지 마라. 기존 문서 draft는 별도 계약이다.

## Verify adapter 도입 판단

선택 adapter·프로젝트 환경과 지원 범위는 `scripts/verify.json` 및 [VERIFY.md](VERIFY.md)를
확인한다. 연결 계획은 기존 수집·fixture·필수 명령·결과 의미와 독립 단위·내부 병렬 제어의
공식 근거를 보존해야 한다. 미지원 도구는 기존 verify를 유지하고 필요한 adapter·도입만 해당
프로젝트의 계획으로 다룬다. 무관한 기능 작업이나 새 테스트마다 전체 검증 개편을 계획하지 않는다.
공통 unittest 연결이나 mock 통과를 비Python·실제 OS 격리·성능 검증 완료로 판단하지 않는다.
