---
id: G-298
title: 병렬 작업의 차단과 rebase 상태 귀속
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

R-091의 작업별 실행 원본을 차단·rebase의 남은 소비자까지 연결한다. 프로젝트 대표 투영이 A이고 현재 작업이 B일 때 B의 차단·원격 PR head·복구 단계 판단은 B의 상태만 사용한다. 이미 구현된 `selected_execution()`·`project_row()`와 snapshot/CAS를 재사용하며 실행 저장 구조를 다시 설계하지 않는다.

현재 `recovery/block_service.py:block_active_execplan()`은 `projects.plan_path`로 차단 대상을 먼저 선택하고 같은 행의 Agent 지문·역할·phase를 사용한다. `git/direct_rebase_service.py`도 새 rebase의 PR head와 차단 rebase 완료의 phase를 대표 행에서 읽는다. 작업별 dispatch는 대표 행을 갱신하지 않으므로 다른 작업의 근거를 섞거나 정상 rebase를 거부할 수 있다. 투영 없는 차단 rebase 완료의 `service.observe()`는 이번 직접 구현에서 transaction 밖으로 분리됐으므로 재구현하지 않는다. 남은 작업 귀속 교정은 이 관측/짧은 DB 반영 경계를 보존한다.

직접 반영된 `recovery/plan_recovery.py`의 대상별 lease·차단 투영 context, 비동기 차단 재평가, 대상 ref/worktree만의 rebase 지문과 `RuntimeStateRepository`·`ExecutionStatePort`의 transaction 밖 재관측은 재사용한다. 해당 기능과 기존 user-decision 의미 판단을 새로 구현하지 않으며, 함수 단독 회귀를 모든 차단·direct rebase 소비자의 충족으로 확대하지 않는다.

## Acceptance Criteria

- A를 대표 투영에 둔 채 B에서 차단하면 B의 status·원인·지문·Worker/Reviewer 결과와 복구 근거만 갱신한다. A의 단계·session·scope·실패·검증/게시 token과 프로젝트 planning 기록은 보존한다. 투영 부재에서도 명시적으로 선택한 작업을 사용하고 다른 작업을 fallback 대상으로 고르지 않는다.
- rebase 시작·완료·재개는 해당 plan의 PR head/conflict head·phase·checkpoint·현재 후보를 사용한다. A와 B의 OID가 달라도 B의 정확한 remote/lease 조건이 충족하면 A 때문에 거부하지 않으며 B 자체의 불일치·stale generation·작업 identity 변화는 계속 거부한다.
- 선택 context와 명시적 plan ID가 다르면 효과 전에 거부하고 소유 증거를 보존한다. 계획 선택이 없는 프로젝트 조율·planning 및 명시적 차단 작업 복구는 각각의 기존 책임을 유지하며 등록 origin·base·프로젝트 안전 gate는 여전히 프로젝트 원본에서 읽는다.
- 같은 후보의 writer·verify·Review·rebase를 겹치지 않고 최신 base/명세·최종 후보 재검증을 유지한다. 다른 작업의 Merge·차단·교정이 B의 성공 근거를 대신하거나 A의 실행 이력을 초기화하지 않는다. 실제 프로젝트 차단은 범위를 축소하지 않는다.
- R-091: 투영 없는 차단 작업의 direct rebase 완료에서도 Git·원격 재관측 중 DB 쓰기 잠금을 잡지 않는다. 관측 뒤 짧은 transaction에서 대상 identity·현재 투영·checkpoint generation을 재확인하고 acceptance·단계·outbox를 기존 원자 경계로 반영한다. 경쟁 변경은 덮지 않고 미확정 근거를 보존한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/recovery/block_service.py:block_active_execplan()`의 대상 선택과 Agent/phase 근거를 기존 작업 선택·`runtime_state.project_row()`에 연결한다. `block_active_execplan_from_revision()`의 이미 교정된 읽기를 재사용하고 `RuntimeStateRepository.block_plan()`·기존 원인별 결정·알림 경계를 유지한다.
- [ ] 확인한 `loop_manager_internal/git/direct_rebase_service.py`의 `pr_head_oid`·`pr_conflict_head_oid`·차단 완료 phase 읽기를 해당 plan snapshot과 연결한다. 기존 checkpoint generation·current-state 검사·정확한 원격 OID·게시 후 확인을 유지하고 대표 투영을 B로 바꾸어 문제를 숨기지 않는다.
- [ ] 같은 파일의 `_complete_direct_rebase_checkpoint()`에 직접 구현된 transaction 밖 관측과 대상/현재 checkpoint 비교를 소비한다. 작업 귀속 변경에서도 `storage/runtime_state.py:complete_rebase()`·`execution/execution_state.py:complete_rebase()`의 관측 후 소유권/checkpoint 비교와 원자 적용을 유지한다. 시작·대기·완료의 실제 Git 판정과 사용자 operation 보호를 유지하며 과거 관측을 새로운 완료 증거로 만들지 않는다.
- [ ] 확인한 `storage/runtime_state.py`의 `execution_selection`·`projected()`·`scope_owner()`·`transition_from_snapshot()`, `execution/execution_state.py`와 `execution_ports.py`의 호출 경계를 재사용한다. `recovery/blocked_agent_service.py`·`checkpoint_adapter.py`·`git/merge_cleanup_service.py`의 직접 SQL도 현재 선택 작업의 실행 필드를 소비하는 부분만 추적해 같은 누락이 있으면 교정한다. 프로젝트 등록·planning 전용 조회까지 일괄 치환하지 않는다.
- [ ] 확인한 `tests/test_task_scheduling.py`의 병렬 snapshot fixture, `tests/test_scoped_blocking.py`, `tests/test_runtime_state.py`, `tests/test_execution_current_rebase_adoption_phase.py`와 `tests/support/current_rebase.py`의 실제 정책/SQLite fixture에 복수 작업 사례를 연결한다. PRODUCT·ARCHITECTURE의 작업 원본·투영 설명에 실제 보장만 반영한다.

## Validation

- Worker는 위 파일의 변경 관련 최소 unit case를 실행한다. 프로젝트 투영 A·선택 B·서로 다른 branch/worktree·session·scope·PR OID·token을 두고 실제 차단 helper와 rebase handler를 호출한다. Git/process 관측만 double로 제공하며 실제 SQL·snapshot·checkpoint·원인 결정을 검사한다.
- B 차단 뒤 B에만 원인·결과·감사가 남고 A와 planning은 동일해야 한다. 투영 없음·A의 별도 차단·B Reviewer 결과·명시적 plan/context 불일치도 검사한다. 단순 `project_row()` 반환값 검사만으로 소비자 회귀를 대신하지 않는다.
- B의 정상 원격 일치·B 자체의 불일치, 차단 rebase 완료·현재 generation 경쟁·재시작에서 대상별 OID·phase·이력 보존과 필요한 재검증을 확인한다. 다른 후보의 verify/Review/게시 token 재사용은 거부하며 종료 불명 writer와 실제 공유 Git operation 보호는 유지한다.
- `tests/test_runtime_state.py:CurrentRebaseRuntimeTests.test_rebase_reobservation_allows_another_database_writer_before_atomic_apply`의 repository/port 회귀는 재사용하고, `tests/test_execution_current_rebase_adoption_phase.py`·`tests/test_scope_block_revalidation.py`의 기존 fixture로 실제 direct-rebase 완료 소비자에 빠진 경우만 보강한다. 투영 없음·A 투영/선택 B에서 외부 재관측 도중 별도 SQLite 연결의 쓰기가 완료되고, checkpoint/소유 identity 경쟁 시 B의 acceptance·차단 해제만 거부되며 A와 경쟁 변경·미확정 근거가 보존돼야 한다.
- 기존 단일 작업·명시적 차단 복구·대표 투영과 작업 원본 분리 회귀를 유지한다. 정책 조합마다 실제 Git lifecycle을 추가하지 않으며 기존 최소 Git 효과 회귀는 Manager 검증에 남긴다.

## Progress

- [x] revision `1f8b97e6c04868281cd23672396f1fd3d47ee98e`에서 대표 투영 직접 읽기와 새 작업별 선택 경계, 관련 SQLite/handler 회귀를 정적으로 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [x] revision `41f8c16d78f203701a1cdd5cba1cebd6c04be465`에서 대상별 복구 context·lease와 repository/port 재관측의 직접 구현·회귀를 확인했다. 기존 두 소비자의 대표 행 조회와 direct rebase의 투영 없는 완료 transaction은 남아 있음을 정적으로 확인했으며 실행 성공은 기록하지 않았다.
- [ ] 차단·rebase 소비자의 작업 귀속 교정
- [x] 현재 revision의 direct rebase 완료가 외부 재관측을 transaction 밖에서 수행하고 대상/checkpoint를 비교하는 직접 구현을 정적으로 확인했다. 과거 조사 기록은 당시 상태로 보존하며 실행 성공을 뜻하지 않는다.
- [ ] 작업 귀속 교정 뒤 외부 관측/DB 반영 경계의 복수 작업·경쟁·재시작 targeted 회귀

## Decision Log

- 이번 delta에서 완료 재관측의 transaction 분리는 직접 구현됐으므로 남은 Implementation을 그 경계의 소비/회귀로 개정한다. 차단 대상·PR OID·phase의 작업 귀속 목표와 기존 metadata·dependency·기록은 보존한다. 같은 관측 이동을 소급 구현하거나 테스트 성공으로 기록하지 않는다.

- complexity medium: 기존 작업 snapshot·선택 context·CAS의 명확한 경계에 남은 소비자를 연결한다. risk high: 다른 작업의 차단/결과나 PR OID를 사용하면 정상 작업을 중단하거나 잘못된 복구·게시 판단을 만들 수 있다. 효과 전 identity 검증·원본/감사 보존과 소비자별 복수 작업 회귀로 방지하고 불명확한 대상은 보존한다. priority high는 병렬 실행에서 작업 격리를 유지하는 기준이다.
- G-297의 배정/대기와 G-278의 실패 planning 복원은 별도 목표다. G-189의 전수 감사와 중복하지 않도록 확인된 소비자 교정을 먼저 독립 Merge하고 감사는 그 결과를 추적한다. 새 사용자 정책·승인·상태 원본·migration을 만들지 않으며 Model·Effort는 Manager 설정을 따른다.
- 최신 R-055/R-091의 대상별 차단 재평가와 transaction 밖 재관측은 직접 구현을 소비하되 같은 direct-rebase 파일에 남은 귀속·완료 적용만 보완한다. 공통 wrapper·예약·의미 판단을 재설계하지 않으며, PRODUCT 없는 등록 프로젝트의 사용자 질문 source 조회는 G-301이 별도로 담당한다. 최초 level 3에 해당하는 medium complexity와 기존 high risk·priority, ID·경로·sources·dependency·수행 기록을 유지한다.
