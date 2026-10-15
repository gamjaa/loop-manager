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
- Requirement 완료 점검에서는 최신 원문의 Acceptance Criteria와 실제 Merge된 구현·테스트·
  검증 근거를 대조한다. 연결 ExecPlan의 MERGED 상태만으로 충족을 확정하지 않는다.
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

본문 구성·체크리스트·확인한 파일과 변경 목적·작업별 Validation·Progress·Decision Log·complexity/risk 근거는 [PLANS.md의 본문 작성](PLANS.md#execplan-본문-작성)을 따른다. 변경·미충족 작업·관련 미착수 개정을 우선하고 이미 구현된 목표를 중복 계획하지 않는다. 새 source·부분 조건 변경·삭제의 영향을 작업 항목·공유 구현·검증·dependency까지 대조하며 snapshot 밖의 권한을 넓히지 않는다.

최초 level은 complexity 1·3·5에 critical risk만 1을 더한 1~6이며 Model·Effort는 프로그램의 명시 설정으로 선택한다. 사용자 정책 모호함을 높은 effort로 대신 결정하지 않는다.

일반 계획 작성에서 사용자 판단이 필요하면 유효한 ID/sources/depends_on과 목표를 보존해 `draft: true`를 선언한다. `Draft Decision`에 Question, Facts, Options, Impact를 비어 있지 않은 `필드: 내용` 줄로 기록한다. 여러 질문은 답변 관계를 구분하고 같은 질문은 기존 ID에서 유지·개정한다. draft와 독립 계획은 함께 게시되며 draft·의존 작업에는 Worker·workspace·branch·PR을 시작하지 않는다. 실행·소유 이력 있는 계획을 draft로 전환하거나 기존 사용자 판단 대기 draft를 임의 삭제하지 않는다.

사용자는 Requirement/ExecPlan 본문 또는 User Decision의 Decision·Answer·Revision으로 판단을 전달한다. 사용자의 draft 제거·정해진 양식 반복을 필수로 요구하지 않는다. Planner는 이전 질문과 Manager가 제공한 사용자 변경 근거를 비교해 질문별 해소·남은 판단·목표 및 완료 조건의 영향을 반환한다. 자기 추론이나 문서 수정으로 승인을 만들거나 User Decision을 작성·변경하지 않는다. 검증된 결정의 본문 반영·임시 입력 정리·draft 전환과 감사·게시·DB 반영은 Manager가 수행한다. 부분 해소·거절·보류·불충분·상충·출처 불확실은 남은 대기를 유지한다.

## Verify adapter 도입 판단

선택 adapter·프로젝트 환경과 지원 범위는 `scripts/verify.json` 및 [VERIFY.md](VERIFY.md)를
확인한다. 연결 계획은 기존 수집·fixture·필수 명령·결과 의미와 독립 단위·내부 병렬 제어의
공식 근거를 보존해야 한다. 미지원 도구는 기존 verify를 유지하고 필요한 adapter·도입만 해당
프로젝트의 계획으로 다룬다. 무관한 기능 작업이나 새 테스트마다 전체 검증 개편을 계획하지 않는다.
공통 unittest 연결이나 mock 통과를 비Python·실제 OS 격리·성능 검증 완료로 판단하지 않는다.
