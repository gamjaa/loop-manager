---
id: G-223
title: 실행 검증 복구 정책 테스트의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Worker 결과·verify/Review·실패 이력·수동 후보와 PR 복구의 남은 정책 테스트를 관측 mock으로 전환한다. 현재 `test_worker_publication_validation.py`의 invalid Worker 결과/session 재개와 `test_worker_validation.py`의 반복 verify 실패는 실제 Git을 준비하지만 핵심 판정은 결과·후보 관측·SQLite 이력이다. 직접 반영된 execution_policy_fixture와 재시도/Review 전환은 재구현하지 않는다.

## Acceptance Criteria

- 실행/검증/Review/게시·차단 복구 및 수동 작업 테스트의 관측값으로 판정 가능한 case는 Git 프로세스 없이 실제 handler·coordinator·결과 parser·실패/승격/후보 저장을 검사한다. 실제 Git·파일·원격 lease 효과는 필요한 통합 회귀로 유지한다.
- 성공·실패·timeout/관측 오류·HEAD/remote OID·dirty/index/operation 변화와 중단 뒤 상태를 명시적으로 재현한다. 판단을 성공 mock으로 대체하거나 과거 성공·session·실패 이력을 새 성공으로 합성하지 않는다. 예상 밖 명령은 실패해야 한다.
- 전환 전후의 조건·호출 횟수·반환 kind·session·후보 token·SQLite 이력·게시 거부 assertion을 대응시킨다. 실제 conflict/index/autostash/worktree·lease·checkpoint·중단 재개 및 새 verify receipt의 후보 gate를 보존한다.
- 통합 테스트는 필요한 실제 효과와 같은 복구로의 재진입을 확인하면 종료한다. 재개 뒤 반복 교정 소진·최종 차단·게시가 독립 계약이면 기존 전용 case로 대응하고, 실제 반복 횟수나 연결 자체가 assertion이면 필요한 반복을 유지한다.
- 전체 verify에는 전환 case와 필수 통합 회귀가 남는다. skip·timeout·수집 범위·운영 실행/권한·필수 검증 정책을 비용 절감으로 변경하지 않는다.

## Implementation

- 대상은 `tests/test_execution*.py`, `test_worker*.py`, `test_review*.py`, `test_recovery*.py`, `test_publication*.py`, `test_pr_*.py`, `test_rebase*.py`, `test_managed_rebase*.py`, `test_checkpoint*.py`, `test_blocked_remote_observation.py`, `test_manual*.py`의 남은 실제 Git 사용 case다. assertion에 필요한 effect를 확인해 정책 변형만 분리한다.
- 기존 ExecutionFixture·execution_policy_fixture/run_execution_policy·CurrentRebaseFixture·rebase 관측 생성·runtime DB/feedback fixture를 재사용한다. RecoveryBoundary는 실제 Git/index 복원 자체의 회귀에 유지한다. 파일/SQLite 보존은 실제 저장으로 확인하며 운영 handler와 판정 함수를 mock하지 않는다.
- 직접 전환된 retry/review escalation·PR 원인 판정과 중단 회귀 축소를 유지한다. G-197의 결과 전달과 G-183의 Review 재사용 목표는 구현하지 않으며 그 작업에서 테스트가 수정돼도 실제 후보·결과 계약을 기준으로 전환한다.
- 공동 fixture는 기존 사용자 호환성을 유지하는 최소 변경으로 제한한다. AGENTS.md와 ARCHITECTURE의 Git 경계를 따르며 assertion 대응·실제 Git 유지 이유를 대상 테스트/계획 기록에 남긴다.

## Validation

- Worker는 전환한 Worker 결과 오류/session 재개·반복 verify 실패·단계 이력/Review 프로토콜·수동 후보 정책의 변경된 최소 unit case를 실행한다. 실패 후 같은 session, 금지 단계의 no-call, 실제 SQLite 차단/교정 이력과 관측 변화의 후보 거부를 검사하고 예상 밖 process를 거부한다.
- Manager 전체 verify는 실제 rebase/index/autostash·lease·checkpoint·수동 게시·실패 시 작업물 보존/재시작, `test_verify_receipts.py`·`test_managed_rebase_lifecycle.py`의 새 후보/원문 근거 계약을 함께 통과해야 한다. unit 전환을 통합 계약 삭제로 취급하지 않는다.
- 기존 계측의 비용 비교는 같은 수집 범위·병렬도의 영향 모듈/필수 연관 회귀에 한정한다. 실행 횟수 감소는 assertion 대응으로 설명하며 전체 verify 시간을 추정하지 않는다.

## Progress

- [x] 2026-10-08 f3ae228의 R-080과 직접 테스트 전환 diff, 실행 관측 helper, 남은 Worker 결과/verify 실패 case 및 실제 conflict·재시작 회귀를 정적으로 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 남은 case 대응·mock 전환과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 실행/관측/저장 경계와 이미 추가된 fixture를 여러 case에 적용한다. risk medium: 테스트 약화는 후보·실패/복구 회귀를 가릴 수 있어 실제 판단과 통합 effect assertion을 보존한다. 테스트 변경 rollback과 기존 회귀로 복구할 수 있으며 priority normal이다.
- G-175/G-182의 실제 MERGED는 유지한다. 직접 구현된 verify receipt·clean rebase·기록 비교와 이미 전환한 테스트를 소급 계획하거나 자동 성공으로 기록하지 않는다. G-222/G-224와 독립적으로 검증·Merge할 수 있다.
