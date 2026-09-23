# [R-024] Priority-Aware Incremental Replanning

## Context

현재 Loop Manager는 활성 변경 작업이 없더라도 `READY` ExecPlan이 하나라도 남아 있으면 일반 Planner를 실행하지 않는다. 실행 가능한 `READY`는 우선순위대로 처리할 수 있지만, 모든 `READY`가 미충족 dependency 때문에 시작 불가능하거나 `PLAN_BLOCKED`·안정된 `PR_OPEN`과의 관계 때문에 대기 중이면 최신 PRODUCT, Requirement와 ARCHITECTURE에서 새로 요구한 작업을 ExecPlan으로 만들 수 없다.

이 정책은 기존 카탈로그를 먼저 소진하는 동안 계획 입력이 안정적이라는 전제에는 맞지만, 이미 대기 중인 장기 계획과 독립적인 긴급 작업도 계획·우선순위 선택 단계에 들어오지 못하게 한다. 특히 마지막으로 성공한 Planner 실행 뒤 `docs/PRODUCT.md`, `docs/ARCHITECTURE.md` 또는 활성 Requirement가 바뀌어도 남은 `READY` 때문에 새 정책을 반영한 계획 주기가 시작되지 않는다.

일반 Planner는 현재 새 ExecPlan만 만들 수 있다. 아직 Worker가 한 번도 시작하지 않아 branch, worktree, Agent session 또는 PR이 없는 `READY` 계획도 새 명세와 코드 구조에 맞게 고치거나 제거할 수 없어서, 중복 계획을 추가하거나 오래된 계획을 끝까지 보존해야 한다. 기존 계획 파일만 바꾸면 SQLite의 path, metadata, content hash와 plan revision이 달라져 catalog 불일치가 생길 수 있으므로 Git 게시와 SQLite 반영을 하나의 복구 가능한 planning batch로 관리해야 한다.

## Requirement

Loop Manager는 프로젝트가 새 변경 작업을 시작할 수 있는 안전한 scheduling boundary에 도달할 때 마지막으로 성공한 일반 Planner 실행 이후의 명세 변경을 먼저 확인해야 한다. 명세 입력은 최신 `origin/<base-branch>`의 `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`와 `docs/requirements/` 바로 아래 활성 `*.md`의 집합 및 내용이다. 이 명세 입력이 마지막 성공 기준과 다르면, 실행 가능한 기존 `READY`가 있거나 `PLAN_BLOCKED`, 안정된 `PR_OPEN` 또는 dependency 미충족 `READY`가 남아 있어도 일반 Planner를 정확히 한 계획 주기 실행한다.

명세가 바뀌지 않았으면 기존처럼 dependency가 충족된 `READY`를 `critical`, `high`, `normal`, `low`, 숫자 ID 순으로 먼저 시작한다. 시작 가능한 `READY`가 더 이상 없을 때는 마지막 성공한 일반 Planner 이후 전체 계획 입력이 바뀌었는지 확인하고, 바뀐 경우에만 일반 Planner를 한 계획 주기 실행한다. 전체 계획 입력은 기존 계획 입력 지문 범위인 `AGENTS.md`, PRODUCT, 활성 Requirement, ARCHITECTURE와 `docs/` 제외 소스·테스트·설정을 유지한다.

성공한 Planner 주기는 새 계획 게시, 기존 미착수 계획의 개정·삭제 게시, `docs/FUTURE_IDEAS.md`만의 게시와 변경 없음 판단을 모두 포함한다. 성공 시 그 주기가 관측한 명세 지문과 전체 계획 입력 지문을 함께 저장한다. 이후 두 입력이 모두 같으면 시작 가능한 ExecPlan이 없더라도 Planner를 반복 호출하지 않는다. Planner 검증·CLI·Git 게시 또는 SQLite 적용이 실패하면 성공 기준을 전진시키지 않고 기존 `planning_error` 및 복구 정책으로 같은 계획 주기를 재개한다.

활성 변경 단계인 `IMPLEMENTING`, `VERIFYING`, `REVIEWING`, `PUBLISHING`, PR 충돌 복구, 일반 planning publication 또는 활성 maintenance publication과 `PROJECT_BLOCKED`는 기존처럼 우선 복구·완료해야 한다. 명세 변경은 실행 중인 Worker를 선점하거나 같은 프로젝트에서 두 변경 Agent를 동시에 실행시키지 않는다. `PLAN_BLOCKED`, 안정된 `PR_OPEN`과 dependency 미충족 `READY`처럼 현재 변경 slot을 점유하지 않는 대기 상태만 일반 Planner의 명세 변경 확인을 막지 않는다. 활성 maintenance cycle의 bounded Planner 권한과 barrier/revalidation 순서는 이 Requirement로 확대하지 않는다.

일반 Planner는 한 planning 시작 snapshot에서 다음 계획만 기존 계획 변경 대상으로 사용할 수 있다.

- SQLite 상태가 `READY`다.
- Worker 실행이 한 번도 시작되지 않았다는 durable marker가 있다.
- branch, worktree와 workspace reservation이 없다.
- Worker·Reviewer session, 결과, retry·failure history와 validation 결과가 없다.
- PR, PR head와 PR conflict recovery 정보가 없다.
- 활성 maintenance cycle의 별도 snapshot 또는 publication이 소유하지 않는다.

이 조건을 모두 만족하는 계획을 이 문서에서는 "미착수 READY"라고 한다. 일반 Planner는 새 ExecPlan 작성과 `docs/FUTURE_IDEAS.md` 변경 외에 snapshot의 미착수 READY만 수정, rename 또는 삭제할 수 있다. 같은 ID의 수정·rename은 실행 identity를 보존하고, ID 자체는 바꿀 수 없다. 삭제된 ID는 재사용하지 않으며 SQLite에 취소 감사 기록과 마지막 선언·삭제 planning commit을 보존한다. 실행이 시작됐거나 snapshot 밖인 ExecPlan, MERGED 계획과 `AGENTS.md`를 포함한 다른 파일은 변경하거나 삭제할 수 없다.

프로그램은 Planner 실행 전 미착수 READY의 Git 본문, SQLite metadata·content hash·plan revision과 무소유 상태를 하나의 snapshot으로 고정한다. Planner 결과의 새 계획, 개정, rename, 삭제를 구분하고 적용 후 전체 활성 ExecPlan catalog의 ID·경로 고유성, sources, dependency 존재와 cycle을 검증한다. 삭제되는 계획을 참조하는 dependency는 같은 batch의 허용된 미착수 READY 개정·삭제로 모두 해소돼야 하며, 시작된 계획이나 snapshot 밖 계획에 dangling dependency를 남기는 삭제는 거부한다.

검증된 planning batch는 원격 base가 시작 snapshot과 같은지 다시 확인한 뒤 단일 planning commit으로 fast-forward 게시한다. 프로그램은 원격 게시 전에 복구 가능한 publication intent와 대상 snapshot을 SQLite에 기록하고, 원격에서 commit을 확인한 뒤 새 계획 등록, 기존 계획 cache 개정, 삭제 계획의 취소 감사 기록, 성공 지문과 planning 상태 정리를 하나의 transaction으로 적용한다. 대상 계획의 상태·소유권·revision이 snapshot과 달라졌거나 원격 base가 바뀌면 일부 Git 또는 SQLite 변경을 적용하지 않고 최신 기준에서 계획을 다시 시작한다. push 후 SQLite 반영 전에 중단돼도 저장된 intent와 원격 commit으로 같은 batch를 멱등하게 완료해야 한다.

일반 Planner가 새롭거나 개정된 계획을 게시한 뒤 다음 작업 선택은 갱신된 전체 catalog에 대해 기존 priority와 dependency 규칙을 적용한다. 따라서 기존 대기 계획과 독립적인 새 `critical` 또는 `high` 계획은 다음 안전한 선택 시점에 낮은 우선순위 계획보다 먼저 시작할 수 있다.

자동 Requirement archive로 활성 경로에서 archive 경로로 내용 변경 없이 이동한 것만 관측한 경우에는 기존 archive-only 예외를 유지한다. 이 이동만으로 긴급 명세 계획 주기를 만들거나 성공 지문을 불필요하게 무효화하지 않는다.

## Acceptance Criteria

- 마지막 성공한 일반 Planner 이후 `docs/PRODUCT.md`가 추가·수정·삭제되면, 활성 변경 단계가 끝난 첫 안전한 scheduling boundary에서 남은 미착수 `READY` 유무와 관계없이 Planner를 한 번 실행한다.
- 같은 조건에서 `docs/ARCHITECTURE.md` 또는 `docs/requirements/` 바로 아래 활성 `*.md`의 집합이나 내용이 바뀌어도 Planner를 한 번 실행한다.
- 명세 변경 Planner가 성공한 뒤 원격 명세와 전체 계획 입력이 더 바뀌지 않으면 반복 scheduler 실행에서 Planner를 다시 호출하지 않는다.
- 명세가 바뀌지 않았고 dependency를 충족한 `READY`가 있으면 Planner보다 Worker를 먼저 시작하며, 여러 후보는 기존 priority와 숫자 ID 순서를 유지한다.
- 명세가 바뀌지 않았지만 시작 가능한 `READY`가 없고 전체 계획 입력이 마지막 성공 기준과 다르면 Planner를 한 번 실행한다. 성공 뒤 저장소 입력이 그대로면 dependency 대기 또는 빈 catalog 상태에서도 다시 실행하지 않는다.
- dependency 미충족 `READY`, `PLAN_BLOCKED` 또는 안정된 `PR_OPEN`만 남은 프로젝트도 명세 변경 또는 no-runnable fallback 조건을 만족하면 Planner를 실행할 수 있고, 그 상태와 실행 소유 정보는 보존한다.
- `IMPLEMENTING`, `VERIFYING`, `REVIEWING`, `PUBLISHING`, PR 충돌 복구, 활성 planning publication, 활성 maintenance publication 또는 `PROJECT_BLOCKED` 중에는 새 일반 Planner를 시작하지 않는다. 해당 상태가 안전하게 끝난 뒤 저장된 지문 차이를 다시 평가한다.
- Planner의 변경 허용 snapshot에는 Worker를 한 번도 시작하지 않았고 branch, worktree, reservation, Agent session·결과·retry·validation 이력, PR 및 conflict marker가 없는 `READY`만 포함된다.
- Planner는 snapshot의 미착수 READY 본문과 metadata를 수정하거나 같은 ID로 rename할 수 있다. 게시 후 SQLite의 current path, title, sources, depends_on, priority, content hash, plan revision과 최초 Worker level은 새 선언과 일치하고 상태는 `READY`로 유지된다.
- Planner는 snapshot의 미착수 READY를 삭제할 수 있다. 게시 후 실행 catalog row는 제거되고, ID 재사용을 막는 SQLite 취소 감사 기록에는 기존 identity와 마지막 선언 및 삭제 commit이 남는다.
- 한 번이라도 실행을 시작한 계획, snapshot 밖 계획, MERGED 계획, ownership 또는 execution evidence가 있는 계획을 수정·rename·삭제하면 Planner 검증이 실패하고 같은 세션의 bounded correction 대상으로 돌아간다.
- 삭제된 계획을 snapshot 밖 또는 시작된 비종료 계획이 직접·간접 dependency로 계속 참조하거나 적용 후 dependency graph가 유효하지 않으면 batch 전체를 거부한다.
- 새 계획 ID는 활성·MERGED·archive 및 취소 감사 ID를 모두 포함한 프로젝트 high-water mark 다음부터 연속적으로 할당하며 삭제된 ID를 재사용하지 않는다.
- Planner 결과를 검증한 뒤 대상 READY의 상태·소유권·content hash·plan revision 또는 원격 base가 바뀌면 commit/push/SQLite 부분 적용을 하지 않고 최신 snapshot에서 다시 계획한다.
- 원격 push 뒤 SQLite transaction 전에 프로세스가 중단되면 다음 실행이 publication intent와 원격 commit을 검증해 생성·개정·삭제 및 성공 지문을 정확히 한 번 적용한다.
- SQLite 적용 중 오류가 발생하면 새 row, 개정 row, 취소 감사 기록과 성공 지문 중 일부만 남지 않고 transaction 전체가 rollback된다.
- 새 `critical` 또는 `high` 계획이 대기 중인 낮은 priority 계획과 독립적이면 갱신된 catalog의 다음 선택에서 먼저 시작한다.
- 내용이 같은 Requirement의 자동 archive 이동만으로는 명세 변경 우선 Planner를 실행하지 않으며, 기존 archive-only 완료·알림 규칙을 유지한다.
- Planner validation retry, `planning_error`, 최신 원격 base 재시작, maintenance bounded revision, 다중 프로젝트 scheduler의 전역 Planner 직렬화와 `max_workers` 제한은 유지된다.

## Product Impact

- 새 제품 명세는 남아 있는 READY catalog보다 먼저 한 번 계획에 반영된다.
- 명세 변경이 없을 때는 실행 가능한 READY를 우선순위대로 먼저 처리하고, 시작 가능한 계획이 없을 때만 변경된 전체 입력을 한 번 재계획한다.
- 일반 Planner는 엄격하게 검증된 미착수 READY만 생성·수정·rename·삭제할 수 있다.
- 삭제된 ExecPlan ID는 재사용하지 않고 SQLite 감사 기록으로 보존한다.
- 계획 Git 게시와 SQLite catalog 변경은 복구 가능한 단일 batch로 수렴한다.

## Non-Goals

- 실행 중인 Worker를 중단하거나 이미 시작한 ExecPlan을 일반 Planner가 재작성하는 기능
- 같은 프로젝트에서 Planner와 Worker를 병렬 실행하는 기능
- Planner가 `PLAN_BLOCKED`, `PR_OPEN`, MERGED 또는 maintenance 소유 계획을 수정·삭제하는 기능
- Planner가 PRODUCT, Requirement, ARCHITECTURE, `AGENTS.md` 또는 코드·테스트를 수정하는 기능
- priority만으로 dependency를 무시하거나 자동 Merge하는 기능
- 활성 maintenance barrier 또는 post-refactor revalidation의 bounded 변경 권한을 일반 Planner 권한으로 확대하는 기능

## Open Questions

- 없음
