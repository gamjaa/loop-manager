# ExecPlan 작성 기준

ExecPlan은 Requirement를 실제 구현·검증할 작업으로 나눈 문서다. 자동 Planner가 계획 PR로 제안하고 사용자가 Merge해 확정한다. 직접 문서 작업과 Specification Skill은 Requirement를 작성·갱신하며 새 G를 만들지 않는다.

## 식별과 metadata

파일은 `docs/plans/[G-001] 제목.md`, 완료 문서는 `docs/plans/archive/`에 둔다. ID는 활성·보관 문서·원격 브랜치·전체 PR 이력·취소 표식의 최대 번호 다음 값을 사용하며 완료·취소 ID를 재사용하지 않는다.

```yaml
---
id: G-002
title: 작업 제목
sources:
  - docs/requirements/[R-001] 변경 제목.md
depends_on:
  - G-001
complexity: medium
risk: low
priority: normal
draft: false
---
```

- `sources`: 근거 PRODUCT 또는 활성 Requirement 경로 목록. 공유 G는 여러 R과 연결해도 생성 출처인 주 R·계획 PR을 바꾸지 않는다.
- `depends_on`: 실제 구현 PR Merge가 필요한 선행 G 목록. 없으면 `[]`. 존재하지 않는 ID·자기 자신·순환은 거부한다.
- `complexity`: 남은 설계·추론 난도. low는 기존 패턴의 국소 변경, medium은 명확한 여러 경계의 상태·오류 처리, high는 중요한 불변식·동시성·복구 판단이 남은 경우다. 파일 수·시간·risk만으로 high를 정하지 않는다.
- `risk`: low/medium/high/critical. 실패 가능성과 변경 영향으로 판단한다.
- `priority`: critical/high/normal/low. 안전한 자동 실행, 핵심 기능, 일반 필수 작업, 선택적 편의 순으로 사용한다. 생략된 기존 문서는 normal이다.
- `draft`: true/false. 미확정 정책이 있으면 true와 아래 Draft Decision을 기록한다. 실행 상태·세션·검증 성공을 metadata에 넣지 않는다.

base의 유효한 비 draft G만 실행한다. 계획 PR에 draft G가 하나라도 있으면 같은 PR 전체를 Draft로 등록한다. 일반 G와 draft G를 별도 계획 PR로 나누지 않는다. 질문 해소 뒤 같은 PR의 G를 보완하고 Ready로 전환한다.

## 본문

현재 명세·코드·테스트·기존 계획·PR을 조사해 미충족 범위만 작업으로 나눈다. 기존 구현·미착수 계획 개정을 우선하고 완료 작업의 중복 계획·과도한 세분화를 피한다. 다음 제목을 유지한다.

| 절 | 작성 내용 |
|---|---|
| Purpose | 작업 범위와 구현 결과. 상위 R 전체 반복은 생략 |
| Acceptance Criteria | 이 G가 담당할 객관적 완료 결과 |
| Implementation | 실제 확인한 코드·테스트·설정·문서 경로와 변경 목적, 핵심 체크리스트·순서·책임·기존 구조 재사용 |
| Validation | Worker 관련 테스트·회귀 조건·정상/실패/경계/동시성 시나리오와 기대 결과. 일반 Verify/승인 절차 반복은 생략 |
| Progress | 실제 완료·잔여 핵심 체크리스트. 미실행을 성공으로 표시하지 않음 |
| Decision Log | 중요한 선택·대안·이유·위험 완화·제약 |

Implementation은 할 일, Progress는 현재 진척을 기록한다. 상세 조사·SHA·재시도 로그를 반복 축적하지 않는다. 공유 구현은 관련 R과 조건을 짧게 연결하고 비충돌 목표를 보존한다. 실제로 충돌하는 활성 R의 해당 규칙만 높은 R-ID가 우선한다.

미결정 정책은 다음 형식으로 사용자에게 질문한다.

```markdown
## Draft Decision
### Question
결정이 필요한 질문
### Facts
확인한 현재 동작과 근거
### Options
실행 가능한 선택지
### Impact
각 선택의 사용자·데이터·권한 영향
```

## 작성·변경 범위

Planner는 선택한 주 R의 계획 PR 하나에서 관련 미확정 G를 추가·수정·제거한다. 최신 base와 원격 PR의 사용자 편집을 읽고 보존한다. 기존 공유 G를 다른 R 소속으로 바꾸거나 완료/archive G·제품 명세·코드를 수정하지 않는다. 기존 G가 목표를 담당하면 그 관계를 반환하며 중복 G나 내용 없는 PR을 만들지 않는다.

Worker는 선택 G의 최신 base 목표와 해당 구현 브랜치의 사용자 편집을 함께 반영하고 관련 코드·테스트·문서를 갱신한다. 미확정 새 정책은 사용자 판단으로 돌린다. Reviewer는 현재 후보·명세·검증 근거를 읽기 전용으로 대조한다. 각 역할의 실행 지침은 runtime의 공통·역할별 원본을 사용한다.

관련 G가 모두 소진되면 Planner가 R 전체 Acceptance Criteria를 재평가한다. 제안이 모두 불필요하고 최신 구현이 충족되면 사용자 PR 편집을 재확인한 뒤 계획 PR을 Close하고 R을 보관한다. archive 이동 자체로 G 완료나 R 충족을 합성하지 않는다.
