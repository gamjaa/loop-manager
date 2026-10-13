- 파일명 형식은 `docs/plans/[G-000] Title.md`다.
- ExecPlan 생성·계획 작성은 매니저가 호출하는 자동 Planner 흐름이 담당한다. 직접 작업·Specification Skill은 관련 Requirement를 작성·갱신하며 ExecPlan을 만들지 않는다. 자동 Planner는 현재 프로젝트에 필요한 ExecPlan을 하나 이상 완성된 형태로 작성할 수 있다.
- ID는 프로젝트 안에서 고유하며 기존 최대 번호 다음 값을 사용한다.
- 파일 상단 frontmatter에는 실행 상태가 아닌 계획의 고정 메타데이터만 기록한다.
- `sources`에는 이 계획의 근거가 된 PRODUCT 또는 Requirement 경로를 기록한다.
- `depends_on`에는 이 계획보다 먼저 PR이 Merge돼야 하는 ExecPlan ID를 기록하며, 선행 작업이 없으면 빈 목록으로 둔다.
- Planner는 작업 범위와 구현 난이도로 `complexity`를, 실패 가능성과 변경 영향으로 `risk`를 작성한다.
- Planner는 `priority`를 `critical`, `high`, `normal`, `low` 중 하나로 기록한다. 안전한 무인 실행을 막는 작업부터 핵심 자동화, 일반 필수 작업, 선택적 편의 순으로 분류한다.
- 새 ExecPlan에는 `priority`가 필수이며, 이 형식 도입 전에 작성돼 값이 없는 기존 ExecPlan은 `normal`로 해석한다.
- Loop Manager는 frontmatter를 코드로 파싱하고 검증한 뒤 실행 상태를 SQLite에 `READY`로 등록한다.
- 실행 상태는 파일에 기록하지 않는다. 같은 프로젝트를 직렬로 실행하더라도 모든 `depends_on`이 SQLite에서 `MERGED`가 되기 전에는 Worker를 시작하지 않는다.
- Loop Manager는 존재하지 않는 ID, 자기 자신에 대한 의존성과 순환 의존성을 코드로 거부한다.
- BLOCKED 해제를 위해 사용자가 ExecPlan을 수정할 수 있으며, Loop Manager는 내용 변경을 감지해 작업을 재개한다.
- `Validation`에는 Worker가 실행할 계획별 targeted unit test, 추가할 테스트 시나리오와 부분/전체 verify가 확인해야 할 계획 고유의 회귀 조건만 기록한다. 자체 저장소의 실행 순서와 최종 전체 검증·승인 조건은 R-086을 따른다.
- 현재 운영체제의 verify 스크립트 선택·실행은 Loop Manager가 담당하므로 `scripts/verify.ps1`과 `scripts/verify.sh` 경로를 ExecPlan마다 반복하지 않는다. 부분 FAIL·일반 선행 FAIL·fallback의 실제 실행 범위를 구분하고 미호출 전체 verify를 성공으로 기록하지 않는다.

## ExecPlan 본문 작성

현재 명세·코드·테스트·기존 계획과 PR을 조사해 실제 미충족 범위만 독립적으로 구현·검증할 작업으로 나눈다. 기존 helper·테스트·구조와 관련 미착수 작업의 개정을 우선하며 과도한 세분화·무관한 목표 결합·완료한 작업의 중복 계획을 피한다. 다음 여섯 제목은 유지한다.

| 절 | 작성 기준 |
|---|---|
| Purpose | 작업의 범위와 구현 결과를 먼저 설명한다. 상위 Requirement 전체를 반복하지 않는다. |
| Acceptance Criteria | 이 작업이 담당하는 객관적인 완료 결과를 적는다. 상위 목표·범위를 축소하지 않는다. |
| Implementation | 완료 여부를 판단할 작업 체크리스트, 주요 방향·책임 경계, 실제 확인한 코드·테스트·설정·문서 경로와 파일별 변경 목적을 적는다. 예정 파일은 구분한다. 필요한 순서·계약·안전 조건과 기존 구조 재사용을 명시하고 함수별 절차는 불필요하게 고정하지 않는다. |
| Validation | 작업과 직접 관련된 추가·수정 테스트, 회귀 조건, 필요한 정상·실패·경계·동시성 시나리오, 기대 결과와 확인한 테스트 경로만 적는다. 일반 전체 verify 순서·스크립트·실행 책임·Reviewer 승인 절차를 반복하지 않는다. |
| Progress | 실제 완료·잔여 핵심 체크리스트와 중요한 상태만 유지한다. 반복 조사·날짜별 SHA·snapshot·재시도 기록을 누적하지 않는다. 복구에 필요한 근거는 보존하거나 실행 이력으로 연결하며 미확인 검증을 완료로 표시하지 않는다. |
| Decision Log | 유지보수에 의미 있는 선택·중요 대안·이유·위험 완화·향후 제약을 남긴다. 다른 절과 상세 실행 로그를 반복하지 않는다. |

Implementation은 수행할 일, Progress는 수행 현황을 기록하며 설명을 중복 복사하지 않는다. 여러 Requirement가 연결되면 작업 항목·공유 구현·관련 완료 조건에 근거 R ID와 조건의 짧은 설명·참조를 연결한다. 모든 Requirement에 새 기준 ID·metadata를 일괄 도입하지 않는다. 변경·삭제 때 남은 목표·공유 구현·필수 dependency를 보존한다.

complexity는 남은 설계·추론 난도로 판단한다. low는 기존 패턴의 국소 변경, medium은 경계가 명확한 여러 모듈·상태 전이·오류 처리, high는 불변식·중단 복구·동시성·재설계의 중요한 판단이 남은 경우다. 파일 수·코드 길이·작업 시간·risk만으로 high를 정하지 않는다.

risk는 구체적인 실패 영향과 복구 가능성이다. low는 국소적이고 쉽게 되돌림, medium은 제한된 동작·진행 오류와 기존 복구, high는 중요한 실행·권한·게시·저장 계약의 광범위한 영향과 수동 복구, critical은 비가역 손실·비밀 노출·소유권 밖 쓰기처럼 복구가 불가능하거나 매우 어려운 영향이다. Git·SQLite·보안 코드라는 이유만으로 높이거나 코드 rollback이 외부 부작용을 복구한다고 가정하지 않는다. 새 계획·개정 권한이 있는 READY 계획에 짧은 근거를 남기고 high complexity의 남은 판단과 high·critical risk의 실제 실패·복구·검증 조건을 설명한다. metadata 보존 mode와 사용자 판단 경계를 우선한다.

갱신은 관련 절을 통합·재작성하고 대체된 설명·중복을 정리한다. 간결화를 이유로 조건·예외·실패 시 보존과 재개·판단 근거·완료 조건을 생략하거나 줄·항목·토큰 수 제한을 두지 않는다. 기존 문서는 실제 개정 대상부터 적용하며 양식만으로 활성·archive 전체를 다시 쓰지 않는다. 상위 명세·공통 규칙은 sources와 필요한 참조로 연결한다.

## Planner 변경 범위

상태·소유권과 실행 mode는 프로그램이 제공한 snapshot과 runtime의 `docs/AGENTS.common.md`+해당 역할 지침을 합친 instruction을 기준으로 판단한다. 정적 역할·권한·검증 규칙은 해당 원본 파일에 두며 작업 prompt에는 작업별 문맥만 둔다.

일반 Planner는 새 `docs/plans/*.md`를 생성하고, 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어만 `docs/FUTURE_IDEAS.md`에 생성하거나 수정할 수 있다.

예외적으로 프로그램이 SQLite 상태와 Git revision에 결합된 PR 전 계획 snapshot을 제공한 경우에만 snapshot 안의 ExecPlan을 같은 ID로 개정할 수 있다. 시작한 계획은 ID·경로와 기존 구현·검증 기록을 보존하며, 미착수 계획만 이름 변경·삭제할 수 있다. 현재 선언·실행 근거와 실제 PR 부재를 게시 전에 재확인한다. snapshot 밖의 기존 ExecPlan·PR 등록·MERGED·archive 계획과 앞에서 허용한 파일 외의 파일은 변경하지 않는다. 기존 `mutable READY` 이름은 이 일반 planning 계약의 호환 이름이며 유지보수의 무소유 READY 제한을 넓히지 않는다.

코드·설계·계획 변경으로 다른 ExecPlan의 경로·구현·검증·dependency가 영향을 받으면, Planner는 허용된 snapshot 안의 해당 계획도 함께 개정한다. 관련성이 있다는 이유로 snapshot 밖의 계획이나 실행 이력의 변경 권한을 확대하지 않는다.

예외적으로 프로그램이 SQLite의 활성 유지보수 주기에서 현재 단계를 `barrier` 또는 `post-refactor revalidation` mode로 지정하고 대상 snapshot을 제공한 경우에만 유지보수 Planner가 기존 ExecPlan을 개정할 수 있다. 대상은 SQLite 상태가 `READY`이고 branch, worktree, workspace reservation, Worker·Reviewer session과 PR이 없는 계획으로 제한한다. 저장된 content hash, plan revision SHA 또는 소유 상태가 snapshot과 다르면 개정을 거부한다.

- `barrier` mode는 기존 ID, 경로, title, sources, complexity, risk, priority, Purpose, Acceptance Criteria와 기존 `depends_on`을 보존하고, 검증된 리팩터링 DAG의 모든 말단 ID만 `depends_on`에 추가한다.
- `post-refactor revalidation` mode는 ID, 경로, title, sources, Purpose와 원래 제품 목표·범위를 보존한다. 최신 구조에 필요한 Acceptance Criteria의 검증된 경로 표현, Implementation, Validation, Progress, Decision Log와 dependency만 개정할 수 있으며 목표와 범위 보존을 확정할 수 없으면 거부한다.
- 유지보수 Planner도 기존 파일을 삭제·이름 변경하거나 대상 snapshot 밖의 계획를 수정할 수 없다. 이 예외는 일반 Planner의 권한을 넓히지 않는다.

사용자가 BLOCKED 해제나 명세 보완을 위해 기존 ExecPlan을 직접 편집한 결과는 유지보수 Planner의 개정 결과가 아니다. 저장소 파일이 변경됐다는 사실만으로 유지보수 예외를 부여하지 않으며, 프로그램이 선택한 mode, SQLite 대상 snapshot과 Agent 실행 전후 diff가 모두 일치할 때만 유지보수 Planner 결과로 검증·게시한다.

## 연관 문서의 정합성

Worker는 현재 작업에 제공된 문서 수정 권한 안에서 코드·설계·계획 변경에 필요한 연관 문서를 함께 갱신하고, 이유·영향과 검증 조건을 현재 ExecPlan에 기록한다. Planner는 위 변경 범위의 영향받는 ExecPlan 개정을 담당하고 Reviewer는 구현·문서·요청의 정합성을 읽기 전용으로 검토한다. 새 목표 추가·사용자 결정을 문서 수정으로 대신하지 않는다.

```markdown
---
id: G-002
title: ExecPlan Title
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-001] Requirement Title.md
depends_on:
  - G-001
complexity: low|medium|high
risk: low|medium|high|critical
priority: critical|high|normal|low
---

## Purpose

...

## Acceptance Criteria

...

## Implementation

- [ ] 핵심 구현 작업 — 확인한 관련 파일과 변경 목적
- [ ] 필요한 계약·안전 순서와 공유 구조 재사용

## Validation

- Worker가 실행할 관련 targeted unit test 또는 검증 시나리오
- 이 작업 고유의 회귀 조건·기대 결과와 확인한 테스트 경로

## Progress

- [ ] 핵심 구현과 관련 테스트

## Decision Log

...
```
