---
id: G-255
title: rebase 충돌의 최신 계획 문서 자동 채택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Manager 소유 rebase에서 Requirement·ExecPlan 충돌을 관측한 최신 원격 base의 문서로 처리한다. 현재 충돌 action은 문서와 코드 모두 Worker로 넘긴다. 최신 조건을 코드로 채택하는 경계를 먼저 구현하고, Planner 갱신·단계 선택·저장 결과 재평가의 연결은 G-256에서 완성한다.

## Acceptance Criteria

- 자동 처리 대상은 Requirement·ExecPlan 파일의 충돌이다. 필요한 Planner 게시나 사용자 변경이 포함된 immutable base revision의 내용·경로·삭제를 사용하며 Git의 ours/theirs 이름만으로 선택하지 않는다. PRODUCT·ARCHITECTURE·지침과 그 밖의 문서·코드·테스트·설정 충돌은 자동 선택하지 않는다.
- 해당 문서를 base 원문 전체로 교체할 수 있다. 삭제·archive·rename은 현재 catalog의 ID·source·dependency와 실제 PR 경계를 확인하고 반영하며, 삭제된 원문이나 작업 branch의 오래된 조건을 되살리지 않는다. 현재 작업의 유효성이 불확실하면 그 범위와 원인을 보존한다.
- 교체 전 문서·작업 형상과 실행 근거를 기존 checkpoint·복구 경계에 보존한다. 파일과 그 파일의 index 처리에만 한정하고 branch·코드·index 전체 reset, 사용자 operation 중단, 다른 파일의 일괄 형상 선택을 하지 않는다. base의 Progress를 현재 후보의 실행·검증 성공으로 채택하지 않는다.
- 문서 충돌만 남은 경우 Manager가 기존 소유 rebase를 계속 진행하고 문서 충돌 자체로 Worker를 호출하지 않는다. 혼합 충돌은 문서만 처리한 뒤 나머지 충돌을 기존 Worker 교정 경로에 남긴다. 구현 부족·미검증 조건은 별도의 단계 판단과 최종 verify·Review·게시 gate를 유지한다.
- 복수 commit 재생 중 뒤늦게 생긴 문서 충돌, 교체·stage·continue 사이 중단과 원격 재전진에서도 같은 원본과 적용 근거를 대조해 재개한다. 진행 중 operation의 target을 바꾸거나 중첩 rebase를 시작하지 않고, 완료 뒤 남은 최신 base 반영을 이어간다. 소유권·원문·PR 관측 실패는 성공으로 처리하지 않는다.

## Implementation

`git/rebase_correction.py`의 충돌 관측·typed action과 `execution/execution_workspace_handler.py`·`execution_worker_handler.py`의 소유 rebase 연결을 재사용한다. 문서 대상 분류·pinned tree의 원문/삭제 선택과 실제 파일·index 적용을 분리하고, manager_git_journal·execution checkpoint에 기존 소유권과 중단 복구 근거를 결합한다. Worker가 Git continue·완료를 수행하도록 권한을 바꾸지 않는다.

현재 Requirement source resolver와 catalog의 rename·archive·PR 검증을 사용한다. 문서 원형 보존과 Manager가 수행한 변경의 범위 근거를 먼저 기록하고, 다음 Agent의 시작 snapshot이 그 변경을 Agent 위반이나 과거 Worker 성공으로 오인하지 않게 한다. 별도 운영 상태 원본·범용 병합 엔진·루트 파일을 추가하지 않는다. 실제 반영된 동작만 PRODUCT·ARCHITECTURE의 관리 rebase·단계별 문서 반영 설명에 갱신한다.

## Validation

- 최소 targeted unit은 `tests/test_rebase_correction.py`와 execution rebase fixture에서 문서/그 외 파일 분류, immutable base 선택, 삭제·rename·archive, 유효하지 않은 source/dependency·PR 관측 거부와 혼합 충돌의 Worker 전달을 확인한다. 정책 조합의 Git/CLI 관측은 mock하고 실제 판정과 보존을 검사한다.
- `tests/test_managed_rebase_lifecycle.py`의 실제 Git/SQLite 경계를 재사용해 문서-only와 문서+코드 충돌에서 최종 파일 bytes·unmerged index·코드 보존·Worker 호출 여부를 확인한다. 후속 재생 commit의 문서 충돌과 적용/continue 중단도 같은 소유 경계에서 확인하며 불필요한 게시 반복은 추가하지 않는다.
- Manager 전체 verify는 원형·session·실패 이력·target 보존, 소유권 경쟁 거부, Manager만 Git operation 완료, 문서 처리 뒤 미완료 검증의 게시 금지와 기존 lease·checkpoint 회귀를 유지한다. Reviewer는 잘못된 base 채택·다른 파일 덮어쓰기·수행 성공 합성을 blocking issue로 확인한다.

## Progress

- [x] 2026-10-10 delta 기준 `50c699c8f9c149cecb0ddf9b081527942d8e2289`부터 `ab0264d94da1212d68141b28fc55f0204c1c9e09`까지 R-082·R-072 변경, 현재 충돌 action/handler와 관련 회귀를 정적으로 대조했다. 구현·테스트·verify를 실행하지 않았다.
- [ ] 문서 충돌 자동 처리·원형 보존과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 직접 반영된 `5e440444`는 rebase 중 구현 입력 변경의 즉시 최종 차단을 제거하고 최신 revision/source를 Worker에 전달한다. Requirement·ExecPlan 충돌을 base로 자동 처리하는 경로는 없으므로 그 남은 작업만 계획한다. G-242의 판단 형식·PR 경합, G-245의 Planner 종료 갱신과 G-189의 전수 감사는 각 기존 책임으로 유지한다.
- complexity high: 현재 rebase target과 최신 문서 revision을 구분하면서 복수 commit 재생·파일별 index 변화·중단 재개를 같은 원형 보존 계약으로 연결할 판단이 남아 있다. risk high: 잘못된 원문 선택이나 혼합 충돌의 일괄 처리는 작업 문서·코드를 손상시키고 잘못된 후보를 게시할 수 있다. 원형·checkpoint·Git 이력을 보존해 수동 복구할 수 있어야 하며, 코드 rollback만으로 외부 게시가 복구된다고 가정하지 않는다. 해당 실패·보존·재개를 AC와 실제 Git 회귀에서 확인한다.
