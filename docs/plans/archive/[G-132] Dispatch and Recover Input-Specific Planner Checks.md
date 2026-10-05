---
id: G-132
title: Dispatch and Recover Input-Specific Planner Checks
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-049] Input-Specific Planner Roles.md
depends_on:
  - G-130
  - G-131
complexity: high
risk: high
priority: high
---

## Purpose

현재 코드 전용 입력 변경도 일반 Planner를 READY 전에 실행한다. G-130의 입력 분류와 G-131의 읽기 전용 역할을 선택 경계에 연결해 미착수 READY가 있으면 좁게 점검하고, 변경이 필요하거나 불확실할 때만 일반 Planner에 인계한다. 점검·인계·성공 기준을 입력과 대상에 결합해 중단·재시작이나 원격 경쟁에서 오래된 판단으로 Worker를 시작하지 않게 한다.

## Acceptance Criteria

- 최초 계획·비교 가능한 성공 기준 부재, 문서/지침/사용자 active-plan 변경 또는 코드와 문서의 동시 변경에는 일반 Planner를 선택한다. 코드·테스트·설정만 바뀌면 무소유·미착수 READY가 하나 이상 있을 때 점검을 먼저 실행하며, 수정 가능한 READY가 없으면 일반 Planner가 미충족 작업을 확인한다.
- dependency 대기 중인 무소유 READY도 점검 대상이다. 실행 이력이 있는 READY, branch·worktree·reservation·Worker/Reviewer session·PR 등 소유 증거가 있는 계획은 점검이 수정 권한을 부여하는 대상으로 삼지 않는다. compact catalog의 다른 비종료 계획은 필요한 문맥으로만 사용한다.
- 수정 불필요 결과에서는 Agent 파일 변경과 일반 Planner 호출 없이 기존 priority·숫자 ID·dependency 규칙으로 다음 작업을 선택한다. 점검한 입력과 대상이 같으면 재실행·재시작 뒤 같은 점검을 반복하지 않는다. dependency가 미충족이면 계속 대기한다.
- 수정 필요·판단 불확실에서는 영향을 받은 예정 작업, 확인한 변경과 인계 사유를 보존해 같은 최신 입력에 일반 Planner를 한 번 시작한다. 일반 Planner는 자신의 Model·Effort·session과 기존 mutable READY 권한을 사용하며, 점검 결과 자체로 snapshot 밖 계획을 수정하거나 권한을 확대하지 않는다.
- 점검 성공은 예정 작업의 유지 가능성만 확인한 기준이다. 전체 PRODUCT·Requirement의 추가 작업 없음이나 유지보수 평가 성공으로 기록하지 않는다. 점검 뒤 mutable READY가 없어져 전체 작업 확인이 필요하면 일반 Planner 기준으로 판단한다. 인계·불확실·실행/설정/형식/scope 실패만으로 성공 기준을 전진시키지 않는다.
- 입력 revision·분류별 지문, 비교 기준, 대상 계획 선언과 SQLite 상태·소유권, 점검 결과·적용 단계 및 인계 상태를 durable하게 결합한다. 결과 저장·확정·인계 시작·일반 게시 사이의 중단에서 기존 session·작업물·오류를 보존하며 같은 결과나 인계를 중복 확정하지 않는다.
- 점검 중·결과 적용 전·Worker activation 직전에 문서·코드·대상 상태나 소유권이 바뀌면 오래된 결과를 적용하지 않고 최신 catalog·snapshot으로 역할을 다시 선택한다. 관련 내용과 대상은 같고 SHA만 전진한 경우에는 검증된 동일성을 확인해 재사용하며 revision 전진만으로 Agent를 반복 호출하지 않는다.
- 활성 실행을 선점하지 않으며 확정 PR 충돌 복구, 활성/due 유지보수, planning publication 복구와 프로젝트 안전 gate의 기존 순서를 유지한다. 점검 중에는 project 실행 자리를 보호하고 다른 계획의 hydration·차단 복구가 점검 worktree·scope를 덮어쓰지 않는다. 빠른 점검 실패는 기존 planning 오류·재시도 경계를 따르며 가짜 성공·프로젝트 완료를 만들지 않는다.
- 로그와 기존 호출 사용량 기록에서 일반 계획·빠른 점검, 세 결과, 선택 Model·Effort 및 인계 사유를 확인할 수 있다. 전달 문맥과 저장된 판단에는 비밀값과 source checkout의 로컬 내용이 포함되지 않는다.

## Implementation

1. `loop_manager_internal/planning/planning_contracts.py`의 결정 값과 `choose_planning_decision`, `selection_service.py`의 안전한 선택을 G-130의 분류 결과에 연결한다. 점검 eligibility는 기존 `MutableReadySnapshot` 생성·CAS 규칙과 `runtime_state.py`의 실행 이력/소유권 판정에서 얻는다. runnable 여부만으로 대상 유무를 판단하지 않는다.
2. 점검 수행·세 결과 처리·인계는 planning 패키지의 집중된 service에 둔다. `PlanningLifecycleCoordinator`와 기존 workspace/agent/state/git ports를 조합하고, 이미 큰 selection/assessment service에 새 저장·호출·결과 처리 책임을 모두 누적하지 않는다. 검증된 detached planning worktree와 기존 process/planner mutex를 재사용하며 새 root 항목이나 실행 infrastructure는 추가하지 않는다.
3. storage 경계에 필요한 최소 additive 점검 기록을 둔다. 입력·대상 identity와 pending/result/accepted/handoff 상태, 역할별 session·오류·retry를 명시하고 저장·적용을 transaction과 기대값 비교로 보호한다. 기존 general success, 유지보수 ledger와 publication intent를 점검 상태로 재해석하지 않는다. `runtime_state.py`의 planning slot 판정 및 `project_state_adapter.py`의 투영 보호에는 필요한 연결만 추가한다.
4. G-131의 호출 결과는 provider 성공, 읽기 전용 scope 및 parser 검증 뒤 저장한다. 수정 불필요 적용 직전 최신 원격 입력과 SQLite mutable 대상의 무소유 상태를 재대조하고 transaction 안에서 해당 대상이 그대로인지 확인한다. Worker 시작 전에도 기존 최신 base/catalog 재선택 gate를 유지한다. 바뀐 입력은 기존 결과를 감사로 보존한 채 새 역할 선택으로 보낸다.
5. 인계는 동일 입력·대상에 결합된 intent로 기록하고 일반 Planner의 prompt에 좁은 변경 근거를 전달한다. 점검 session을 재사용하지 않으며 일반 Planner 시작/재개 이후의 검증 교정·rebase·게시 복구는 기존 lifecycle이 소유한다. 중단된 동일 일반 계획의 재개를 두 번째 인계 호출로 세지 않는다. 원격이나 대상이 달라지면 이전 인계 근거를 그대로 확정하지 않는다.
6. `planning_error`와 scope 증거의 보존·해제, 외부 CLI retry/usage-limit 및 결과 형식 교정 한도는 기존 planning 경계를 따른다. 기존 오류를 새 점검 성공으로 덮거나 유지보수·다른 계획의 차단을 지우지 않는다. 점검 결과·사유의 로그는 기존 event/usage 경계를 통해 남긴다.
7. 구현 후 장기 역할 선택과 제한된 성공 기준을 같은 PR의 PRODUCT에 간결하게 반영한다. R-049 및 ARCHITECTURE는 수정하지 않는다. 현재 OS verify에 필요한 새 내부 모듈과 테스트의 검증이 포함되도록 유지하며 전체 실행은 Manager에 맡긴다.

## Validation

- Worker는 `tests/test_planning_selection.py`의 코드/문서 변경·READY·activation 경쟁 사례, `tests/test_planning_assessment.py`의 호출/검증 사례 및 새 점검 service/storage의 직접 관련 메서드만 완전한 unittest 이름으로 실행한다. 기존 `test_source_change_with_ready_runs_planner_once_before_activation`는 R-049의 점검 경로와 수정 가능 대상 조건을 표현하는 회귀로 갱신한다.
- 최초/비교 기준 없음, 문서·사용자 계획 변경, 코드 전용 mutable READY 있음/없음, dependency 대기, 실행 이력 있는 READY, 세 결과와 한 번의 인계, optional profile fallback 및 실제 서로 다른 profile 적용을 검증한다. 점검 성공만으로 완료·전체 계획/유지보수 성공 기준이 바뀌지 않는지 확인한다.
- 저장·확정·인계·일반 게시 경계에 중단을 주입해 재시작 시 같은 결과의 중복 적용·호출이 없는지 검증한다. pending/accepted/handoff를 구분하고 형식·scope·CLI·설정 실패가 성공으로 해석되지 않는지 확인한다. input·snapshot·상태·reservation 변경과 SHA만의 전진을 각각 검증한다.
- `tests/test_runtime_state.py`, `tests/test_planning_publication.py`, `tests/test_planning_error_recovery.py` 및 기존 maintenance/PR 충돌 fixture 중 직접 수정한 slot·오류 보존 경계만 targeted 검증한다. 별도 planning 결과와 session이 active-plan 투영으로 덮이지 않고 기존 복구 선택 순서가 유지돼야 한다.
- Manager 전체 verify는 실제 프로젝트 선택에서 코드 변경 후 점검→READY/대기 및 점검→일반 계획→게시 경로, 원격 경쟁 뒤 재선택, 중단 복구와 완료 판정 분리를 확인한다. Worker의 targeted test와 Manager 전체 verify가 통과한 결과를 Reviewer가 읽기 전용으로 검토한다.

## Progress

- [ ] 선행 G-130·G-131 Merge 확인
- [x] 입력·mutable READY에 따른 역할 선택과 집중된 점검 service 연결
- [x] durable 결과 적용·인계·재시작 및 최신 입력 gate 구현
- [x] 오류·실행 자리·완료 기준 분리와 관련 targeted 회귀 검증
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-03: 현재 `choose_planning_decision`은 변경된 전체 입력에 일반 fallback planning만 선택하며 `test_source_change_with_ready_runs_planner_once_before_activation`도 일반 Planner 호출을 기대한다. 점검 결과 저장과 인계 상태는 현재 구현에 없다.
- G-130은 입력·게시 provenance를, G-131은 읽기 전용 호출·profile·결과 계약을 담당한다. 두 계획이 MERGED인 뒤 실행 선택·저장·복구를 연결한다. 기존 G-113의 Planner 슬롯 보호와 현재 구현된 session·publication 복구 경계를 재사용하며 해당 결함을 별도 재구현하지 않는다.
- 제한된 점검의 성공으로 일반 계획 전체 성공을 덮지 않는다. 이후 코드 변경의 비교에는 마지막 유효한 점검 기준을 사용할 수 있지만, 예정 대상이 없어졌거나 전체 미충족 작업을 판단할 때에는 일반 Planner 확인이 필요하다.
- priority는 `high`다. 입력 변경에 맞는 자동 역할 선택과 오래된 결과의 Worker activation 방지를 구현한다. R-049가 요청한 선택 정책 안에서 현재 Git·게시·검증 책임과 SQLite 실행 상태 모델을 유지한다.
- 2026-10-04: 점검은 기존 PLANNING 슬롯과 관리 worktree를 사용하고 입력·대상에 결합된 별도 SQLite 단계(`pending`/`result`/`accepted`/`handoff`/`superseded`)에 session·결과·오류를 보존한다. 입력 또는 대상이 달라진 이전 판단은 감사 이력에 남기고 새 snapshot에서 다시 선택한다. `accepted`는 일반 계획 성공 지문을 바꾸지 않으며 인계는 점검 session을 일반 Planner에 전달하지 않는다. R-049는 요청대로 본문 변경 없이 archive로 이동했고 장기 역할 규칙은 PRODUCT에 기록했다.
