---
id: G-028
title: Guarded Maintenance Plan Revision and Catalog Publication
sources:
  - docs/requirements/[R-001] Automation-First Planning and Priority.md
  - docs/requirements/[R-012] Periodic Refactoring Assessment and Execution.md
depends_on:
  - G-021
  - G-022
  - G-027
complexity: high
risk: critical
priority: normal
---

## Purpose

유지보수 Planner가 새 리팩터링 ExecPlan을 만들고 아직 시작하지 않은 READY 계획에 dependency 장벽 또는 최신 구조에 맞는 안전한 개정을 적용할 수 있도록, 일반 Planner보다 좁고 결정적인 변경 권한과 원자적 publication 경계를 제공한다. 기능은 재사용 가능한 검증·게시 API로 추가하고 G-029 전에는 scheduler에서 호출하지 않는다.

## Acceptance Criteria

- 일반 Planner는 G-021의 새 ExecPlan 및 FUTURE_IDEAS 범위만 유지하며 기존 ExecPlan을 수정할 수 없다. 기존 계획 수정 권한은 코드가 명시한 유지보수 assessment/barrier 또는 post-refactor revalidation mode에만 열린다.
- 수정 가능한 계획은 SQLite 상태가 READY이고 branch, worktree, workspace reservation, Worker·Reviewer session과 PR이 전혀 없는 행으로 제한한다. 파일만 READY처럼 보여도 runtime 소유 상태를 확인할 수 없으면 거부한다.
- 장벽 mode는 기존 계획의 ID, 경로, title, sources, complexity, risk, priority, Purpose, Acceptance Criteria와 기존 `depends_on`을 보존하고, 검증된 리팩터링 DAG 말단 ID만 `depends_on`에 추가할 수 있다.
- 사후 재검증 mode는 ID, 파일 경로, title, sources와 Purpose로 표현된 원래 제품 목표를 보존한다. 최신 구조에 필요한 Acceptance Criteria의 경로 표현, Implementation, Validation, Progress, Decision Log와 dependency만 개정할 수 있고 목표 의미나 범위를 보존했는지 확정할 수 없는 결과는 거부한다.
- 유지보수 Planner는 기존 파일을 삭제·이름 변경하거나 READY 이외 계획 및 작업 공간·세션·PR이 생긴 계획을 수정하지 못한다.
- 새 리팩터링 계획은 구체적인 구조 문제와 대상 책임 경계, 사용자 동작·공개 계약·저장 형식 보존 조건, targeted unit test와 전체 verify 회귀 조건을 포함하고, 여러 경계는 독립적으로 검증·Merge 가능한 DAG로 분리한다.
- 새 리팩터링 계획은 기본 `priority: normal`을 사용한다. 더 높은 priority는 기존 priority 정의에 해당하는 데이터 손실·보안·자동 실행 차단·반복 수동 복구 근거가 계획 본문에 명시된 경우에만 허용하고 장벽 순서를 위해 인위적으로 상향하지 않는다.
- 새 리팩터링 계획과 개정된 READY 계획 전체를 합친 catalog에서 ID 연속성, source, dependency 존재, 기존 dependency 보존 및 순환 부재를 검증한다.
- 새 계획과 기존 READY 개정은 하나의 planning commit에만 포함하며, 게시 직전 `origin/<base-branch>`가 평가 기준 SHA와 다르면 rebase하거나 부분 적용하지 않고 최신 기준의 동일 유지보수 단계를 다시 시작한다.
- fast-forward push를 원격에서 확인한 뒤 하나의 SQLite transaction으로 새 계획을 등록하고 개정 계획의 `depends_on`, content hash와 plan revision SHA를 갱신하며 G-027의 유지보수 주기 revision도 함께 기록할 수 있다.
- push 뒤 SQLite transaction 전 중단된 경우 원격 commit과 보존된 주기 정보를 대조해 동일 batch를 idempotent하게 등록·갱신한다. 원격 파일이나 기존 DB metadata가 기대 batch와 다르면 덮어쓰지 않고 planning error로 중단한다.
- 새 계획만 있는 일반 Planner publication, FUTURE_IDEAS 보조 변경, 계획 없음 결과와 기존 correction retry 동작은 유지된다.
- 공통 `docs/PLANS.md`와 Planner prompt는 일반 계획에서 기존 파일 수정 금지, 유지보수 mode의 제한된 장벽·재검증 예외 및 사용자 편집과 구분되는 검증 규칙을 설명한다.
- 이 계획이 Merge된 뒤에도 유지보수 due 판단이 scheduler를 선점하지 않으며 G-029가 전체 수명주기를 연결하기 전에는 새 권한과 publication API를 자동 호출하지 않는다.

## Implementation

- Planner scope/result validation을 일반, maintenance barrier, maintenance revalidation mode로 분리하고 mode는 SQLite의 활성 유지보수 주기와 기대 단계에서만 코드가 선택하게 한다.
- Markdown section과 frontmatter의 전후 snapshot을 비교해 각 mode의 immutable field와 허용 section을 검증한다. 장벽 변경은 dependency 추가만으로, 재검증은 목표 보존과 최신 구현·검증 경계 개정으로 구분한다.
- eligible READY snapshot에 plan ID, content hash, plan revision SHA와 소유 상태를 결합하고 Planner 반환 뒤와 publication 직전에 동일성을 다시 확인한다.
- 기존 `validate_planner_result`와 `register_execplans`를 일반 batch와 maintenance revision batch가 공유할 수 있는 catalog 검증 및 publication service로 분리하되, 새 계획 전용 호출자의 기존 계약은 보존한다.
- planning commit과 remote 확인 뒤 new row insert, revised row update와 maintenance cycle update를 하나의 transaction으로 적용하고 recovery 시 동일 commit을 재검증한다.
- G-021의 FUTURE_IDEAS 허용 범위와 분리된 maintenance prompt/result contract를 추가하되 top-level project scheduler에는 아직 연결하지 않는다.

## Validation

- Worker는 Planner scope/result validation과 catalog publication service의 관련 test class 또는 새 maintenance plan revision test module만 targeted unit test로 실행한다.
- 일반 Planner의 기존 계획 수정 거부와 maintenance mode가 없는 직접 API 호출 거부를 검증한다.
- READY 무소유 계획의 barrier dependency 추가, 기존 dependency 보존, terminal 전체 추가 및 전체 DAG 검증을 확인한다.
- READY라도 branch, worktree, reservation, session, PR이 있거나 content hash·revision이 달라진 계획과 비READY 계획의 변경을 거부하는지 검증한다.
- 장벽 mode의 body·priority·source·ID 변경, dependency 제거와 파일 삭제·rename을 거부하고, 재검증 mode의 구현·테스트 경계 개정은 허용하되 Purpose·식별자·source 변경을 거부하는지 검증한다.
- 새 리팩터링 계획의 필수 근거·동작 보존·검증 section, 기본 priority, 정당화 없는 상향과 독립 DAG/cycle 오류를 검증한다.
- new+revised batch의 단일 commit, stale base 재시작, fast-forward 이외 push 거부, 원격 확인 뒤 원자적 DB 갱신과 push-after-crash idempotent recovery를 검증한다.
- 전체 verify에서 일반 Planner correction, FUTURE_IDEAS, compact catalog, new-plan publication, workspace 소유권과 입력 fingerprint 회귀가 없어야 한다.

## Progress

- [ ] 일반·장벽·재검증 Planner mode와 scope 분리
- [ ] eligible READY snapshot 및 Markdown 불변 조건 검증
- [ ] 리팩터링 DAG와 barrier batch catalog 검증
- [ ] new/revised 계획 원자적 publication 및 recovery 구현
- [ ] 공통 PLANS 계약과 dormant API 회귀 테스트 추가

## Decision Log

- 장벽 추가와 사후 재검증은 기존 계획 수정 금지의 일반 예외이므로 G-021의 Planner 범위 계약이 Merge된 뒤 더 좁은 mode로 확장한다.
- barrier mode는 기계적으로 dependency 추가만 허용하고, 더 넓은 본문 개정은 실제 리팩터링 Merge 뒤 revalidation mode에서만 허용한다.
- base push와 SQLite 사이 중단이 기존 계획 파일과 catalog row를 어긋나게 만들 수 있으므로 새 계획 등록과 개정 계획 갱신을 하나의 복구 가능한 publication 단위로 취급한다.
- origin-only planning 및 사용자 기본 worktree 비간섭 경계를 재사용하기 위해 G-022 뒤에 구현한다.
