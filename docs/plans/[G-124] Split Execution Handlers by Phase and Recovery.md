---
id: G-124
title: Split Execution Handlers by Phase and Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-123
complexity: high
risk: high
priority: critical
---

## Purpose

`execution_handlers.py`는 4,902줄이고 하나의 `ExecutionHandlers`가 workspace 준비, rebase 복구, Worker, verify, Review, publication과 문서 승인 처리를 맡는다. 대응 테스트도 7,521줄이다. 기존 phase coordinator와 effect port를 유지하면서 단계별 실행과 공통 검증 경계를 분리한다.

## Acceptance Criteria

- workspace/recovery, revision·validation gate, Worker 실행, verify, Review, publication과 문서 승인 처리는 별도 책임으로 분리된다. 기존 `ExecutionHandlers`는 좁은 구성·위임만 담당하고 같은 규모의 거대한 context/service로 본문을 옮기지 않는다.
- 각 단계는 필요한 typed port와 immutable 실행 context만 받는다. 전체 effects 객체, 임의 `SimpleNamespace` 또는 root module을 넘겨 의존성을 숨기지 않으며 불필요한 전역 가변 상태를 추가하지 않는다.
- session/profile 결정, Worker 결과·scope 확인, feedback delta, latest-base reconciliation, archive eligibility와 validation token의 동일 계약을 기존 service로 재사용한다. 단계마다 필요한 재검증 시점은 보존한다.
- IMPLEMENTING→VERIFYING→REVIEWING→PUBLISHING 전이, retry·usage pause·Review 교정 한도, 중단 후 같은 세션 재개, rebase와 중간 checkpoint, archive 변경 시 재검증을 보존한다.
- Worker targeted test, Manager 전체 verify와 Reviewer 읽기 전용 검토의 책임을 유지한다. 단계별 refactor를 이유로 직접 전체 verify를 호출하거나 Reviewer에게 test 실행을 넘기지 않는다.
- 큰 테스트 파일은 단계별 unit test와 recovery/revision 경계 테스트로 나뉘고 G-119 support를 이용한다. 성공·실패·race·재시작 시나리오와 assertion은 그대로 유지한다.

## Implementation

1. `guard`·`recover`·`run` 등 큰 method의 입력, 효과와 상태 확정을 G-060 대응표에 연결한다. `execution_runtime`의 coordinator와 `execution_revision`, Worker/session, feedback, checkpoint/rebase의 기존 경계를 우선한다.
2. contract/effects는 독립 module로 두고 workspace/recovery, Worker, validation, Review, publication, document 처리의 좁은 handler를 구성한다. 공유하는 실행 context에 새 가변 필드를 누적하지 않는다.
3. 같은 계약의 중복 로직만 담당 service로 통합한다. 서로 다른 단계의 lease·scope·archive 확인을 한 번의 오래된 결과로 대체하지 않는다.
4. `tests/test_execution_handlers.py`를 단계·검증 경계별 모듈로 나눈다. fixture 생성과 phase별 효과 fake를 support로 분리하되 fixture가 구현의 상태 전이를 대신 수행하지 않게 한다.
5. G-123의 application assembly는 새 handler를 조합하도록 최소한으로 갱신한다. 외부 호출 계약과 typed outcome·예외·저장 결과를 보존한다.

## Validation

- Worker는 변경한 각 단계의 정상 결과와 직접 관련 실패·재개 경계만 선택해 targeted unit test를 실행한다. 분할된 모든 테스트를 합쳐 전체 suite처럼 실행하지 않는다.
- Worker session 유지/교체, stale revision token 거부, verify 실패 전달, Review 교정 한도, base 변경 후 재검증, archive eligibility 변화와 게시 실패 재개를 검증한다.
- effect 호출 순서와 transaction 결과를 확인해 partial success를 완료로 기록하지 않는지 검증한다. 문서 승인·정책 결정의 기존 사용자 경계도 보존한다.
- Manager 전체 verify에서 모든 실행 단계·복구·publication 시나리오가 통과해야 한다. Reviewer는 단일 handler에 책임이 다시 누적되거나 공유 gate가 약화되지 않았는지 diff와 전달 결과를 확인한다.

## Progress

- [ ] phase·recovery·validation 책임과 기존 service 대응표 확인
- [ ] 단계별 handler·좁은 port 추출 및 중복 제거
- [ ] 실행 테스트 분리와 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 이미 분리된 `execution_runtime` coordinator를 다시 만들지 않는다. phase 전이는 coordinator, 외부 효과는 adapter, phase의 작업은 해당 handler가 담당하도록 기존 경계를 완성한다.
- 실제 저장 형식과 실행 결과를 보존하는 리팩터링이며 새로운 phase, retry 정책과 검증 생략을 도입하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
