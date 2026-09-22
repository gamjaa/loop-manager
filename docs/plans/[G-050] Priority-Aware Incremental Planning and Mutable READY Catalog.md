---
id: G-050
title: Priority-Aware Incremental Planning and Mutable READY Catalog
sources:
  - docs/requirements/[R-024] Priority-Aware Incremental Replanning.md
depends_on:
  - G-048
complexity: high
risk: critical
priority: high
---

## Purpose

현재 일반 planning gate는 `READY`가 하나라도 남으면 최신 제품 명세와 전체 계획 입력의 변화를 확인하지 않는다. dependency 미충족 READY, `PLAN_BLOCKED` 또는 안정된 `PR_OPEN` 때문에 시작 가능한 계획이 없어도 Planner를 실행하지 않으며, 실행 가능한 오래된 READY가 있으면 그 작업을 모두 소진할 때까지 새 긴급 요구를 plan catalog에 넣을 수 없다.

G-048의 planning lifecycle coordinator 경계에 명세 변경 우선 trigger와 no-runnable fallback을 추가한다. 마지막 성공 Planner가 본 명세와 전체 입력을 각각 durable하게 기록해, PRODUCT·ARCHITECTURE·활성 Requirement 변경은 다음 안전한 선택에서 정확히 한 번 우선 계획하고 그 밖에는 실행 가능한 READY를 priority 순으로 처리하다가 시작 가능한 계획이 없고 입력이 바뀐 경우에만 한 번 재계획한다.

일반 Planner가 SQLite와 Git snapshot으로 증명된 미착수 READY를 새 입력에 맞게 수정·rename·삭제할 수 있게 한다. 생성·개정·삭제를 하나의 검증된 planning publication manifest로 만들고, 원격 fast-forward 확인 뒤 catalog·취소 감사·성공 지문을 한 SQLite transaction에 적용해 push 또는 process 중단에도 Git과 DB가 같은 결과로 수렴하게 한다.

## Acceptance Criteria

- project runner가 활성 변경 단계, PR 충돌 복구, 일반·maintenance planning publication과 `PROJECT_BLOCKED`를 먼저 복구·처리한 뒤 도달하는 안전한 선택 경계에서만 새 일반 Planner trigger를 평가한다. 실행 중인 Worker를 선점하거나 같은 프로젝트의 Planner와 Worker를 병렬 실행하지 않는다.
- 최신 `origin/<base-branch>`의 `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`와 `docs/requirements/` 바로 아래 활성 `*.md` 집합·내용으로 별도 명세 지문을 계산한다. 자동 archive의 내용 보존 이동만 있는 경우는 기존 archive-only 예외로 분류한다.
- 마지막 성공 일반 Planner의 명세 지문과 최신 명세 지문이 다르면 실행 가능한 READY, dependency 미충족 READY, `PLAN_BLOCKED` 또는 안정된 `PR_OPEN`이 남아 있어도 일반 Planner를 한 계획 주기 시작한다.
- 명세 지문이 같으면 dependency를 충족한 READY를 `critical`, `high`, `normal`, `low`, 숫자 ID 순으로 Planner보다 먼저 시작한다.
- 명세 지문이 같고 시작 가능한 READY가 없을 때만 기존 전체 계획 입력 지문을 비교한다. 전체 입력이 마지막 성공 기준과 다르면 일반 Planner를 한 번 실행하고, 같으면 Planner 없이 WAITING 또는 완료 결과를 반환한다.
- 새 계획, 기존 계획 개정·삭제, FUTURE_IDEAS-only 또는 no-change로 Planner가 정상 완료된 경우에만 관측한 명세 지문과 전체 계획 입력 지문을 함께 성공 기준으로 저장한다. 같은 원격 입력의 반복 실행은 Planner를 다시 호출하지 않는다.
- validation, Agent CLI, commit·push, remote confirmation 또는 SQLite 적용이 실패하거나 중단되면 성공 지문을 전진시키지 않고 현재 `planning_error`, retry, session 및 worktree 복구 계약을 유지한다.
- 일반 planning 시작 전에 프로그램이 수정 가능한 미착수 READY snapshot을 만든다. 대상은 `READY`이고 durable `ever_started` 값이 false이며 branch, worktree, workspace reservation, Worker·Reviewer session·결과, retry·failure·validation 이력, PR·PR head·conflict recovery와 maintenance 소유권이 모두 없는 계획으로 제한한다.
- `READY → IMPLEMENTING` activation은 같은 transaction에서 `ever_started`를 true로 영구 설정한다. BLOCKED 해제나 다른 복구로 상태가 다시 READY가 돼도 이 값은 false로 돌아가지 않는다.
- 기존 database의 additive migration은 비READY row와 실행 소유·결과·이력이 하나라도 있는 row를 보수적으로 `ever_started=true`로 초기화하고, 아무 실행 증거가 없는 READY만 false로 초기화한다. migration은 기존 상태·session·workspace·PR 정보를 바꾸지 않는다.
- Planner prompt는 비종료 compact catalog와 별도로 변경 가능한 plan ID, path, metadata, content hash와 plan revision에 결합된 bounded snapshot을 제공한다. Planner는 필요할 때 해당 계획 전문을 읽을 수 있지만 snapshot 밖 기존 계획은 read-only로 취급한다.
- Planner는 새 ExecPlan을 만들고 snapshot의 미착수 READY를 같은 ID로 수정·rename·삭제할 수 있다. 개정·rename은 ID를 보존하고 path, title, sources, depends_on, complexity, risk, priority와 본문을 유효한 전체 catalog 안에서 바꿀 수 있다.
- Planner는 실행된 계획, snapshot 밖 계획, MERGED·archived 계획, maintenance 대상과 `AGENTS.md`, PRODUCT, Requirement, ARCHITECTURE, 코드·테스트를 수정·삭제할 수 없다. 범위 위반은 기존 bounded correction budget 안의 Planner validation error가 된다.
- Planner 결과 validator는 Git diff를 create, update/rename, delete와 FUTURE_IDEAS 변경으로 결정적으로 분류하고, snapshot의 원본 bytes·metadata·hash·plan revision 및 SQLite 무소유 상태와 대조한다. rename similarity 추정만으로 identity를 결정하지 않고 보존된 frontmatter ID와 snapshot identity를 사용한다.
- 적용 후 전체 active/archived/cancelled ID 고유성, active path·title 대응, source 존재, dependency 존재·cycle을 게시 전에 검증한다. 삭제 계획을 참조하는 snapshot 밖 또는 이미 시작된 계획이 남거나 같은 batch의 허용된 개정·삭제로 dependency가 해소되지 않으면 batch 전체를 거부한다.
- 새 ExecPlan 번호는 active, archived, MERGED와 취소 감사 ID를 포함한 high-water mark 다음부터 연속적이어야 하며 삭제한 ID를 재사용하지 않는다.
- 삭제는 대상 active `exec_plans` row를 제거하되 별도 취소 감사 저장소에 plan ID, 마지막 path·title·metadata·content hash·plan revision과 삭제 planning commit을 남긴다. 취소 기록은 실행 후보·compact catalog·Requirement archive 후보가 아니지만 ID 고유성 및 planning 복구 입력에는 포함한다.
- 개정된 미착수 READY의 SQLite current path, title, sources, depends_on, priority, content hash, plan revision과 initial/effective Worker level은 새 선언에 맞게 갱신되고 상태는 READY로 유지된다. 최초 게시 감사 정보가 별도로 존재하면 이를 덮어쓰지 않는다.
- 검증된 batch는 planning 시작 base, 미착수 READY snapshot, publication path·hash, 생성·개정·삭제 manifest와 local planning commit을 push 전에 durable intent로 저장한다.
- push 직전 fetch 결과가 시작 base와 다르거나 대상 READY의 상태, `ever_started`, 소유권, metadata, content hash 또는 plan revision이 snapshot과 다르면 commit을 게시하거나 SQLite 일부를 적용하지 않고 최신 base와 snapshot에서 다시 계획한다.
- 원격 fast-forward push와 exact commit 확인 뒤 하나의 SQLite transaction이 새 row 등록, 개정 row compare-and-swap, 삭제 row compare-and-delete와 취소 감사 insert, 성공 지문 및 planning 상태 정리를 함께 적용한다. 중간 오류는 transaction 전체를 rollback한다.
- push 뒤 SQLite transaction 전에 중단되면 다음 실행이 durable intent, 원격 ancestry와 batch 경로·hash를 검증해 같은 transaction을 멱등하게 적용한다. 후속 원격 commit이 batch 경로를 바꿨거나 snapshot 보존을 증명할 수 없으면 추정 적용하지 않고 구체적인 planning error로 보존한다.
- Planner가 게시한 갱신 catalog는 다음 안전한 선택에서 전체 priority/dependency 규칙으로 평가된다. 새 독립 `critical` 또는 `high` 계획은 기존 낮은 priority 대기 계획보다 먼저 선택될 수 있다.
- 활성 maintenance assessment/barrier/post-refactor revalidation의 mode, eligible snapshot과 publication 권한은 바꾸지 않는다. 일반 mutable READY batch와 maintenance batch가 동시에 같은 계획을 소유할 수 없다.
- 다중 프로젝트 scheduler의 전역 Planner 직렬화, `max_workers`, latest-origin planning worktree, validation 3회 한도, FUTURE_IDEAS 분리, planning commit fast-forward-only push, project completion 및 알림 의미를 유지한다.
- R-024가 바꾸는 planning 순서, 미착수 계획 변경 권한, 취소 감사와 recovery transaction은 사용자 승인으로 `docs/ARCHITECTURE.md`에 먼저 반영돼야 하며, Worker 구현 branch에는 ARCHITECTURE diff가 없어야 한다.
- 구현 PR은 장기 제품 규칙인 명세 우선 1회 trigger, no-runnable fallback, 미착수 READY 변경·삭제, ID 비재사용과 원자적 publication 의미를 `docs/PRODUCT.md`에 코드와 함께 반영한다.

## Implementation

- G-048 planning coordinator의 selection input에 pinned remote SHA, 마지막 성공 명세·전체 입력 지문, catalog decision과 stable wait 상태를 제공한다. typed decision을 `resume_active`, `run_specification_planning`, `activate_ready`, `run_fallback_planning`, `stable_wait`, `complete`로 나눠 최상위 scheduler가 문자열 조건을 재구현하지 않게 한다.
- 명세 전용 path predicate와 digest를 기존 `_planning_input_fingerprint`의 Git tree reader 위에 추가한다. archive-only 판정은 이전 성공 revision과 최신 revision의 active Requirement 이동을 기존 helper와 같은 byte-identity 규칙으로 분리한다.
- project planning state에 마지막 성공 명세 지문을 additive하게 저장하고, G-048 coordinator가 no-change, FUTURE_IDEAS-only, 새·개정·삭제 batch의 remote-confirmed 완료에서만 두 성공 지문을 함께 갱신하게 한다.
- G-047 runtime-state repository 경계를 사용해 `ever_started`를 activation과 같은 transaction에 기록하고, mutable snapshot query가 상태뿐 아니라 모든 소유·실행 evidence와 maintenance ownership을 검증하도록 한다.
- 일반 Planner context에 immutable `MutableReadySnapshot`과 create/update/delete 결과 type을 추가한다. prompt에는 허용 ID와 snapshot token을 명시하고 validator는 기존 maintenance snapshot·PlanRevision 검증 helper의 byte/hash/CAS 부분을 공통화하되 maintenance의 목적·본문 보존 제한을 일반 Planner에 잘못 적용하지 않는다.
- 전체 post-edit catalog를 pinned worktree tree에서 파싱하는 validator를 두고 deleted ID reservation까지 포함한 ID allocator와 graph validation을 실행한다. rename은 같은 ID의 삭제·추가 path 쌍으로 정규화한다.
- additive cancellation audit 및 general planning publication intent repository를 추가한다. intent는 commit 전 검증 snapshot과 commit 후 manifest를 단계적으로 저장하고, remote-confirmed apply는 생성·개정·삭제와 project success marker를 한 transaction에서 compare-and-swap한다.
- 현재 일반 `register_execplans`의 new-row-only 경계를 batch apply API로 교체하고, push 후 SQLite 중단 복구가 remote planning commit의 parent/diff와 intent hash를 다시 검증한 뒤 idempotent apply하도록 한다. maintenance publication repository와 공통 원자성 helper는 재사용하되 cycle 전이는 분리한다.
- compact catalog, READY selection, Requirement archive와 project completion query가 cancellation audit을 실행 row로 보지 않도록 하고, 새 ID 계산과 감사 진단만 cancellation repository를 포함하게 한다.
- Planner prompt, `docs/PLANS.md`의 일반 Planner 변경 범위와 scope validator를 같은 계약으로 갱신한다. 변경 가능 조건을 Agent 판단에 맡기지 않고 프로그램이 제공한 snapshot과 post-run diff로 강제한다.

## Validation

- Worker는 G-048 planning coordinator test module, 일반 Planner result/snapshot validator, publication repository와 top-level scheduling adapter의 직접 관련 unit test만 targeted test로 실행한다.
- 실행 가능한 READY와 명세 변경이 함께 있으면 Planner가 정확히 한 번 먼저 선택되고, 성공 뒤 같은 입력에서는 READY Worker가 선택되는 순서를 검증한다.
- 명세 변경이 없을 때 실행 가능한 여러 READY가 priority/ID 순으로 Planner보다 먼저 선택되는지 검증한다.
- dependency 미충족 READY, PLAN_BLOCKED, 안정된 PR_OPEN 및 빈 catalog 각각에서 전체 입력 변경 시 Planner 1회, 성공 뒤 입력 불변 시 stable wait/no-repeat를 검증한다.
- PRODUCT, ARCHITECTURE, 활성 Requirement create/update/delete를 각각 명세 trigger로 검증하고 byte-identical automatic archive-only 이동은 trigger하지 않는지 검증한다.
- IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR conflict recovery, planning publication, maintenance publication과 PROJECT_BLOCKED가 trigger를 보류하고 안전 경계 뒤 같은 지문 차이를 잃지 않는지 검증한다.
- 새 database와 기존 database migration에서 never-started READY만 mutable이고 READY 복귀·소유권·session·result·retry·validation·PR evidence가 하나라도 있는 계획은 제외되는지 검증한다.
- mutable READY의 body/metadata 변경, path/title rename, 여러 계획 동시 개정, 삭제 및 새 계획 혼합 batch를 검증하고 SQLite current cache·Worker level·취소 감사·ID high-water mark 결과를 확인한다.
- snapshot 밖 수정, 실행된 READY 수정, ID 변경, 모호한 rename, dangling dependency, cycle, source 누락, 삭제 ID 재사용과 batch 중 READY activation race를 모두 전체 batch 거부로 검증한다.
- validation 후 base 변경, commit 전 snapshot 변경, push 실패, push 후 SQLite 적용 전 중단, transaction 중간 예외, 이미 적용된 batch 재관측과 후속 remote path drift를 검증한다.
- transaction rollback test는 created row, revised row, deleted row, cancellation audit, project fingerprints와 planning state 중 일부만 남지 않음을 확인한다.
- 다중 프로젝트 scheduler에서 한 프로젝트의 spec-trigger planning과 다른 프로젝트의 Worker 진행, global Planner mutex와 max_workers가 유지되는지 실제 project decision 경로로 검증한다.
- 전체 verify에서 maintenance barrier/revalidation, file-authoritative reconciliation, revision-aware resume, Requirement archive, planning validation recovery, origin isolation, project completion과 notification 회귀가 없어야 한다.

## Progress

- [ ] 명세·전체 입력 dual fingerprint와 typed scheduling decision 구현
- [ ] durable never-started marker 및 mutable READY snapshot 구현
- [ ] 일반 Planner create/update/rename/delete prompt·validation 구현
- [ ] cancellation audit 및 recoverable publication manifest transaction 구현
- [ ] single/multi-project scheduling·race·interruption 회귀 테스트 추가
- [ ] PRODUCT 장기 규칙 갱신 및 사용자 승인 ARCHITECTURE 계약 준수

## Decision Log

- 현재 runtime은 일반 Planner가 기존 ExecPlan을 바꾸면 catalog hash 불일치로 거부하므로, 이번 수동 계획에서는 SQLite상 미착수인 G-048·G-049와 다른 READY 파일을 직접 수정하지 않고 새 G-050을 추가한다.
- G-050은 G-048의 planning coordinator를 사용한 뒤 동작을 추가한다. G-049도 G-048 뒤에 시작 가능하지만 G-050의 `high` priority가 G-049의 `normal`보다 먼저 선택되므로 planning 동작을 고정한 다음 execution coordinator 추출이 이어진다.
- 명세 변경 우선권은 현재 변경 slot을 점유하지 않는 대기 상태만 넘는다. 이미 실행 중인 Worker와 publication을 중단시키는 preemption은 R-024 범위가 아니다.
- 기존 계획 삭제는 ID 재사용과 원격 push 후 DB 복구 문제를 피하기 위해 active execution row의 물리 삭제와 별도 immutable cancellation audit을 한 transaction으로 결합한다. `CANCELLED` 실행 상태를 추가하지 않아 향후 active/archive reconciliation 의미를 혼합하지 않는다.
- modification identity는 frontmatter ID 하나로 고정한다. rename은 ID가 같은 path 교체로만 인정하고 내용 유사도 추정은 사용하지 않는다.
- User Decision (2026-09-22): 남은 READY 때문에 새 계획이 막히지 않도록 명세 변경을 마지막 Planner 이후 한 번 우선 반영하고, 그 밖에는 실행 가능한 READY를 priority 순으로 처리한 뒤 no-runnable 경계에서 변경된 입력만 한 번 재계획하도록 승인했다. 일반 Planner가 DB 안전성을 보장한 미착수 ExecPlan을 생성·수정·삭제하는 제품 정책도 승인했다.
- Architecture Approval (2026-09-22): 사용자가 SQLite 상태·planning publication recovery 경계를 포함한 `docs/ARCHITECTURE.md` 갱신과 게시를 승인했다. G-050은 게시된 R-024와 ARCHITECTURE 계약을 구현하며 Worker branch에서는 ARCHITECTURE를 다시 수정하지 않는다.
