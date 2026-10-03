---
id: G-112
title: Verified Runtime Recreation Baseline
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-127
  - G-129
complexity: medium
risk: high
priority: critical
---

## Purpose

같은 SHA의 runtime을 Manager가 삭제하고 재생성하면 잠금 mutation ID가 바뀐다. 저장된 Agent snapshot과 runtime 등록 전체의 equality 검사가 정상 재시작을 거부하는 결함을 수정한다.

## Acceptance Criteria

- 현재 runtime의 기존 process lock, clean detached exact SHA, durable 생성 표식 검증을 유지한다.
- 이전 runtime 생성 표식과 일치하는 성공 삭제 receipt 및 journal intent/result를 검증한 경우에만 잠금 ID 변경을 기준선에 반영한다.
- 잠금 외 HEAD, detached, 파일 상태 및 등록 필드 변경이나 삭제 증거 누락·변조는 허용하지 않는다.
- selected runtime만 재조정하며 다른 worktree/ref, Agent session, SQLite 실행 상태와 사용자 source 파일을 변경하지 않는다.

## Implementation

- 현재 `loop_manager_internal/runtime/runtime_scope.py`의 `proven_runtime_recreation`, `loop_manager_internal/agents/scope_recovery.py`의 runtime 소유권·process lock 선검증과 `loop_manager_internal/agents/agent_scope_decision.py`의 좁은 잠금 변경 허용을 기준으로 Acceptance Criteria 충족 여부를 확인한다. 이 경계는 현재 코드에 이미 존재하므로 재구현하거나 별도 증거 경로를 추가하지 않는다.
- 원래 거부 조건을 만족하지 못하는 구체적 결함 또는 회귀 누락이 확인된 부분만 기존 책임 경계 안에서 수정한다. 현재 runtime 생성 표식·이전 삭제 receipt·journal intent/result가 일치해야 하며 현재 관측값만으로 기준선을 교체하지 않는다.
- 기존 session·원본 snapshot·Manager checkpoint와 사용자 source 상태를 보존한다. 운영 DB 수동 복구나 전역 차단 해제는 이 계획의 구현에 포함하지 않는다.

## Validation

- Worker는 `tests/test_runtime_scope.py`의 `RuntimeRecreationEvidenceTests`와 `tests/test_agent_scope_decision.py`의 runtime 잠금 재생성·무관 변경 보존 사례 중 변경한 경계의 최소 targeted unit test만 실행한다. 호출 연결을 수정할 때만 `tests/test_scope_services.py`의 직접 관련 사례를 포함한다. 코드·테스트 변경이 없으면 기존 구현·테스트의 충족 근거와 targeted test 미실행 사유를 기록한다.
- 성공 재생성, 증거 누락·변조, 잘못된 소유자, 잠금 외 상태 변경 및 무관한 metadata 보존을 검증한다.
- 기존 전체 verify 스크립트가 새 모듈과 unit test를 포함하도록 유지하며 전체 실행은 Manager에 맡긴다.

## Progress

- [x] 과거 작업 기록: 삭제 증거 검증 및 좁은 scope 연결
- [x] 과거 작업 기록: targeted unit test 총 8개 통과 (현재 revision의 검증 결과가 아님)
- [x] 현재 구현과 테스트를 기준으로 Acceptance Criteria 재확인: 기존 구현 유지, 삭제 증거 거부·snapshot/checkpoint 보존 회귀 테스트 보강
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 보고된 runtime 선검증 실패와 SQLite snapshot, runtime 생성 intent 및 삭제 receipt를 읽기 전용으로 비교했다. 같은 SHA의 정상 삭제·재생성에 따른 잠금 ID 변경을 확인했다. 기존 G-092는 더 넓은 기준선 복구 계획이며 이 수정은 현재 구현의 runtime equality 결함만 다룬다.
- source checkout 밖의 별도 관리 worktree에서 변경을 준비하며 운영 DB를 수동 수정하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 runtime 증거·거부 조건·검증 결과와 목표는 유지하고, 실행 시 리팩터링된 runtime·scope 경로를 확인한다. 과거 targeted test 기록을 새로운 revision의 검증 성공으로 사용하지 않는다.

- 2026-10-03 사용자 요청에 따른 대기 계획 전체 점검: 현재 runtime 증거 검사와 scope decision이 이미 구현돼 있음을 코드·테스트로 확인했다. 현재 PRODUCT/R-023/R-050과 충돌하는 목표는 없으며 원래 증거·거부 조건을 유지한다. 구현 경로를 최신 책임 경계로 갱신하고 중복 구현을 금지했다. 과거 테스트 결과로 현재 verify·Review·사용자 Merge를 대체하지 않는다. SQLite와 실행 상태는 수정하지 않는다.
- 2026-10-03 Worker 재확인: `scope_recovery.py`는 부모 process lock identity, clean detached exact SHA 및 현재 durable 생성 표식을 먼저 검증한다. `runtime_scope.py`는 이전 생성 표식과 일치하는 삭제 receipt·bootstrap intent·완료 result를 요구하며 잠금 외 등록 필드가 달라지면 거부한다. `agent_scope_decision.py`는 선택 runtime의 일치하는 원본 등록만 재조정하고 저장된 결과·checkpoint의 별도 변조와 무관한 metadata를 보존하므로 구현을 재작성하지 않았다.
- 회귀 누락 보강: 삭제 intent 누락, receipt·intent·result의 JSON 손상과 identity·소유자·전후 상태 변조 거부, 증거 파일의 읽기 전용 보존, 정상 재생성 시 원본 snapshot·checkpoint·scope identity·Agent 파일 변경 보존, 저장된 결과·checkpoint의 runtime 등록 위반 보존을 기존 두 테스트 파일에 추가했다. 새 파일·루트 항목이나 별도 증거 경로는 만들지 않았다.
- 현재 변경의 targeted 검증: `python3 -m unittest tests.test_runtime_scope.RuntimeRecreationEvidenceTests tests.test_agent_scope_decision.PersistedScopeDecisionTests.test_runtime_lock_replacement_requires_proof_and_preserves_other_changes tests.test_agent_scope_decision.PersistedScopeDecisionTests.test_runtime_recreation_preserves_checkpoint_and_original_agent_metadata tests.test_agent_scope_decision.PersistedScopeDecisionTests.test_runtime_recreation_keeps_agent_result_and_checkpoint_registration_violations -v` — 9개 통과. 기존 `scripts/verify.sh`의 unittest discovery가 두 파일을 포함하므로 변경하지 않았다. 전체 verify·Review·Merge는 별도 확인 대상이다.
