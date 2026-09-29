---
id: G-105
title: Blocked Plan Remote Observation
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

차단된 계획의 원격 base와 정확한 작업 branch 변화를 다른 계획의 실행 및 프로젝트 차단과 독립적으로 관측하고, 후속 복구가 사용할 입력을 누락 없이 보존한다.

## Acceptance Criteria

- 매 외부 실행에서 각 `PLAN_BLOCKED` 및 계획과 연결된 `PROJECT_BLOCKED`의 등록 origin, 저장된 base branch와 정확한 계획 작업 branch를 읽기 전용으로 조회한다. 다른 계획이 활성 중이거나 project 실행 자리를 점유해도 관측을 건너뛰지 않는다.
- 계획별로 원격 URL·ref 이름, base·작업 branch OID 또는 조회 실패와 관측 시각을 지속 기록한다. 일시적 조회 실패나 branch 부재를 마지막으로 확정한 OID로 바꾸지 않고, 관측만으로 소유권·차단 해제 또는 게시 가능성을 인정하지 않는다.
- 원래 차단 원인과 검사 입력 지문, 원격 두 OID 및 로컬 작업 지문을 구분해 마지막 처리 입력과 관측 입력을 보존한다. base만 전진하고 계획·Requirement 지문이 같아도 미처리 변경으로 남으며, 작업 branch만 바뀌거나 둘 다 바뀌어도 후속 복구 대상으로 식별된다.
- 같은 입력의 반복 관측은 rebase·재검증·알림을 시작하지 않는다. 읽기 전용 관측은 기본 worktree의 파일·index·HEAD, 다른 계획의 실행 투영과 작업 branch를 바꾸지 않는다.

## Implementation

- `loop_manager.py`의 project 안전 gate 앞에 차단 계획별 원격 조회를 배치하고, `runtime_state.py` 또는 별도 계획별 저장 모듈에 관측·처리 입력을 구분해 보존한다. 조회 실패와 branch 부재를 별도 결과로 표현한다.
- 후속 G-106·G-107이 사용할 변화 판정 경계를 노출하되 이 계획에서는 fetch, rebase, commit, push 및 차단 해제를 수행하지 않는다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_runtime_state.py`와 새 저장 모듈 테스트 중 관련 targeted unit test만 실행한다.
- 실제 원격 ref로 base 단독 전진, 작업 branch 단독 전진, 동시 변경, 삭제, 조회 실패 및 다른 계획 활성 중 관측·재시작을 검증한다. 마지막 확정 OID와 처리 입력이 실패 관측으로 손상되지 않아야 한다.
- 전체 verify는 차단 중 관측이 기본 worktree와 다른 계획의 Git 상태·실행 slot을 변경하지 않는지 확인한다.

## Progress

- [ ] 차단 계획별 원격 관측과 지속 저장
- [ ] 입력 변화 판정 및 실행 slot 독립성 검증

## Decision Log

- 원격 OID 관측과 Git 쓰기 허가는 별개다. 이 계획의 기록만으로 기존 `PROJECT_BLOCKED` 안전 gate를 열지 않는다.
