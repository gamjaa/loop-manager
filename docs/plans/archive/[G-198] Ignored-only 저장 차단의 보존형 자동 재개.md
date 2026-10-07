---
id: G-198
title: Ignored-only 저장 차단의 보존형 자동 재개
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

이전 snapshot의 ignored artifact만으로 남은 planning_error와 scope BLOCKED를 같은 입력에서도 안전하게 재평가해 보존된 작업을 재개한다. R-022의 일반 ignored 분류·정상 정리는 재사용하고, 저장 차단 복구와 역할별 실행 흐름의 누락된 회귀 근거를 완성한다.

## Acceptance Criteria

- 시작 ignore 규칙을 증명하고 권위 있는 상태·현재 소유권이 모두 유효한 Planner·Worker·Reviewer의 저장 차단은 입력 지문 변경, 사용자 cache 삭제나 수동 SQLite 수정 없이 자동 재개한다. 해당 planning_error, PLAN_BLOCKED 또는 PROJECT_BLOCKED의 원인만 해제하고 같은 session·worktree·결과·단계를 이어간다.
- 저장된 시작·Agent 결과·차단 결과·Manager checkpoint를 사용하는 모든 비교에 같은 증명된 ignored-only 분류를 적용한다. 역사 결과의 재검사와 BLOCKED의 사전 dirty 비교에서도 같은 판정이 성립하며 원본 snapshot·작업물·retry·실패 감사는 보존한다. 완료된 호출·검증을 ignored 차이 때문에 다시 실행하지 않는다.
- tracked·staged·non-ignored untracked, non-default index flag, HEAD·branch·ref·worktree·config·operation 차이가 함께 있거나 시작 ignore 규칙·소유권을 증명할 수 없으면 차단과 원인을 보존한다. 실행 중 새로 숨긴 경로와 강제 stage는 허용하지 않으며 실제 Agent 위반을 낡은 기준선으로 치환하지 않는다.
- 재개 확정 시 현재 차단 identity와 검사한 Git·작업 상태를 다시 대조한다. 중단·재시작·반복 관측에도 진행·해제·알림을 중복하지 않고 다른 승인·PR·안전 차단을 해제하지 않는다. ignored-only 원인이 아닌 동일 입력의 planning 오류는 기존 대기·교정 한도를 유지한다.
- 자동 회귀는 임의 ignored 파일의 생성·수정·삭제를 Planner·Worker·Reviewer별로 확인하고, Worker targeted test → Manager 전체 verify → Reviewer가 같은 관리 worktree에서 artifact를 남겨도 게시 후보 처리로 이어지는 순서를 검증한다. 실제 Planner import bytecode, persisted 차단 재개와 권위 변경 혼합 거부를 함께 확인한다.
- 활성 ignored 파일의 복구 삭제·clean·reset·stash·강제 stage와 별도 검증 worktree를 도입하지 않는다. 기존 Git 쓰기 경로 겹침 거부, 종료 worktree 소유권·미게시 commit 보호와 PR preflight 보존은 유지한다.

## Implementation

- `agents/scope_observation.py`의 시작 ignore 증명·legacy 정규화와 `scope_recovery.py`의 persisted 검사를 재사용한다. 저장 상태에서 비교용 view를 만드는 책임을 일관되게 공유해 일반 재개, 역사 Agent 결과 검사와 BLOCKED 검사 사이의 분류 차이를 없앤다. 원본 저장 값을 현재 snapshot으로 덮어쓰거나 공유 metadata 차이를 일괄 제외하지 않는다.
- `recovery/scope_reconciliation.py`의 프로젝트·계획 scope 복구 및 raw dirty preflight를 같은 증명 경계에 연결한다. 선택 worktree의 파생 상태를 조정해야 한다면 권위 입력으로 재구성 가능한 부분만 다루고 다른 worktree·ref의 변화를 ignored로 추정하지 않는다.
- `planning/selection_service.py`의 보존 오류 gate에서 기존 오류와 저장된 실행·결과가 ignored-only 오탐임을 검증할 수 있는 경우를 재평가한다. workspace 소유권과 현재 cause를 확인한 뒤 기존 복구·게시 상태 전이로 이어가며, 일반 실패의 입력 변경 gate나 결과 검증을 우회하지 않는다.
- 기존 scope·planning recovery·Worker 검증 fixture를 확장해 누락된 역할별 동작과 같은 worktree 실행 연결을 검증한다. 현재 전체 verify의 unittest discovery를 유지한다. 자동 역할·검증·승인 경계는 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 구조·계획 작성과 실행별 Agent 범위·중단 복구·검증 설계는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다.

## Validation

- 변경한 책임에 직접 관련된 최소 unit case를 실행한다. `tests/test_scope_ignored_artifacts.py`, `tests/test_scope_block_revalidation.py`, `tests/test_planning_error_recovery.py`의 fixture를 재사용해 저장 시작/결과 snapshot의 ignored 생성·수정·삭제와 같은 지문·재시작 재개, 증명 불가·권위 변경 혼합의 안전 실패를 확인한다.
- `tests/test_worker_validation.py` 또는 기존 실행 handler unit 경계에서 artifact를 만드는 verify 대역과 Agent 대역으로 동일 worktree·기존 순서·결과 보존을 확인한다. 테스트 안에서 실제 프로젝트 전체 verify를 실행하지 않는다. 각 역할의 빠진 생성·수정·삭제 조합을 채우고 이미 있는 개별 scope 검사를 복제하지 않는다.
- Manager 전체 verify는 기존 역할별 게시 범위·shared metadata·workspace ownership·rebase/publication lease·Reviewer 읽기 전용 검사를 유지한다. 기존 ignored 경로 겹침·PR preflight·worktree cleanup 회귀와 이번 복구 사례가 함께 통과하고, 정상 결과나 실제 위반이 ignored 존재 때문에 뒤바뀌지 않아야 한다.

## Progress

- [x] 2026-10-06 기준 de2c655609c667f6032eb3608116a801a806766f의 활성 명세·코드·테스트와 비종료 catalog를 대조함. 일반 persisted 정규화는 있으나 BLOCKED/raw 결과 비교와 동일 입력 planning_error gate의 누락을 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-07 구현 및 관련 targeted unit 검증: 저장 시작·역사 결과·BLOCKED 비교 view, 동일 입력 planning_error 재개와 단계·결과 보존을 구현함. 관련 회귀 109개 통과.
- [x] 2026-10-07 Manager 전체 verify: 201개 모듈, 3,086개 성공·2개 skip, 종료 코드 0.
- [x] 2026-10-07 origin/main rebase 및 계획 대조 검토: 이미 최신 기준 60b956d에 있었으며, ignore 해제 규칙의 삭제 오분류·Planner PROJECT_BLOCKED 소유권 검사 누락·증명된 실행 격리에서의 단계 무효화를 재현하고 보완함. Manager checkpoint 비교도 역할별 생성·수정·삭제 회귀에 포함함.
- [x] 2026-10-07 검토 보완 후 전체 verify 재실행: 201개 모듈, 3,090개 성공·2개 skip, 종료 코드 0. 같은 G-198 worktree에서 검증했으며 재개 단계·소유권 변경·독립 원인 보존 회귀가 통과함.
- [x] 2026-10-07 review delta 보완: 저장 verify의 base·정의·후보 token 증명 시 차단 재개를 REVIEWING으로 이어가도록 보완하고, 해당 targeted unit 1개 통과.
- [x] 2026-10-07 추가 review delta 보완: verify 재호출 전 이전 성공 표식을 지우고, 통과한 호출의 후보·출력 결합 token을 scope 검사 전에 저장함. 저장 출력이 token과 다르면 차단 재개에서 완료 검증으로 인정하지 않으며 관련 targeted unit 7개 통과.
- [x] 2026-10-07 재개 세션 review delta 보완: verify 성공 token은 권위 있는 미커밋 변경이 없는 동일 HEAD에만 발급하며 혼합 scope 차단은 VERIFYING에서 기존 commit·재검증을 유지함. 구형 token은 깨끗한 저장·현재 snapshot과 최신 성공 감사의 definition·출력을 확인하고 해제 transaction에서 재확인한 뒤 출력 결합 token으로 전환함. 현재 후보의 관련 unit 경계 84개, 보완 후 직접 관련 case 5개 통과. 새 fixture의 Mock 구성 오류 두 차례를 수정하고 동일 명령 최종 통과; 전체 verify는 실행하지 않음.
- [x] 2026-10-07 결과 보고 재검증: 현재 worktree의 dirty 후보 증거 거부·구형 token 재개·stale token·빈 출력 직접 관련 unit case 8개를 다시 실행해 모두 통과함. 이전 보고의 명령 앞 실패 설명이 결과 계약에서 미대응 실패로 해석됨을 확인했고, 제품의 실패 검사 정책은 변경하지 않음.
- [x] 2026-10-07 revision 의미 대조 보완: 구형 verify 감사는 DefinitionSnapshot의 base revision 대신 감사 생산자가 사용하는 계획의 current_revision_sha와 비교함. base와 제출 정의 commit이 다른 재개 및 잘못된 감사 revision 거부를 확인하는 targeted unit 3개 통과. fixture의 계획 필드 갱신 위치를 수정한 뒤 동일 명령으로 재검증함.
- [ ] 사용자 Merge

## Decision Log

- R-022 기준 14의 자동 재개는 기존 일반 세션 회귀만으로 충족되지 않는다. `scope_recovery.py`의 역사 결과·BLOCKED 검사와 `scope_reconciliation.py`의 raw 비교가 정규화 전 값을 사용하고, planning 오류 gate는 동일 입력을 재검사 전에 보류한다. 완료된 G-051·G-053의 일반 허용·정리 목표를 반복하지 않고 이 누락을 보완한다.
- R-022 기준 19의 기존 테스트는 일부 역할·파일 동작과 개별 안전 경계를 검증하지만 Worker targeted test부터 verify·Reviewer까지의 ignored 생성·수정·삭제 연결과 저장 차단 재개를 모두 입증하지 않는다. 관련 fixture 안에서 필요한 회귀만 추가한다.
- 분류와 재개 계약이 명확하고 기존 helper·서비스를 재사용하므로 complexity medium이다. 잘못된 정규화가 실제 Agent 변경과 공유 Git 위반을 숨길 수 있어 risk high, 무인 재개 누락을 해결하므로 priority high다.
- runtime bytecode 환경 정책은 G-162, 일반 실패·결과 계약과 Reviewer 검증 근거는 기존 담당 계획이 유지한다. 이 계획은 ignored-only 원인만 다루며 완료 후 G-189의 전체 차단 경로 감사에 연결한다.
