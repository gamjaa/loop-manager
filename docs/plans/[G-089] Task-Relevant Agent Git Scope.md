---
id: G-089
title: Task-Relevant Agent Git Scope
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-088
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 `validate_agent_scope`와 저장된 Agent snapshot 판정은 사용하지 않는 branch·tag·ref와 다른 worktree의 독립 변화도 repository 전체 변경으로 비교한다. 격리가 증명된 실행에서는 현재 작업의 안전에 필요한 자원만 감시하고, 자원을 실제 사용하기 직전에 다시 검증한다.

## Acceptance Criteria

- G-088의 유효한 실행별 격리 증거가 있을 때 선택 관리 worktree의 identity·등록·branch·HEAD·index·허용 파일, 현재 단계가 쓰는 소유 branch·원격 base ref·게시 ref·PR head, 영향을 주는 Git config와 operation만 Agent 범위에 포함한다. 기본 source checkout의 local HEAD·index·dirty와 무관한 다른 branch·tag·ref·worktree의 독립 변화만으로 `PLAN_BLOCKED`·`PROJECT_BLOCKED`를 만들지 않는다.
- 범위 밖 파일·index·HEAD, 소유 branch·게시 ref·PR head, 사용 중인 worktree 등록·config·operation의 예상 밖 변화와 영향 여부가 불확실한 변화는 계속 차단한다. 원격 base의 검증된 전진은 기존 fetch·rebase·재검증 절차를 따른다.
- 나중에 사용하게 되는 ref·worktree와 Git 작업 대상은 직전에 실제 값·소유권·예상 OID를 확인한다. snapshot에서 제외됐다는 이유로 오래된 값에 쓰거나 게시하지 않는다.
- 격리 증거가 없거나 불완전한 과거 snapshot은 기존 엄격 비교를 유지한다. Agent의 허용 파일, branch·worktree·push·PR 소유권과 Reviewer의 읽기 전용 계약은 그대로다.

## Implementation

- `agent_scope_decision.py`에 역할·단계별 사용 자원 선택을 명시하고 `loop_manager.py`의 현재/저장 snapshot 검증과 Agent 반환 후 범위 판정에 같은 선택 기준을 적용한다. 증거가 없는 경로는 기존 검사를 사용한다.
- `execution_handlers.py`, `publication_resume.py`와 관리 Git 명령 직전 경계에서 실제로 사용할 ref, PR head 및 worktree를 재조회·검증한다. 선택된 작업과 무관한 변화는 진단에 남기되 Agent 변경으로 귀속하지 않는다.
- G-088의 격리 증거를 검증한 경우에만 범위를 좁히며, 관측 OID나 시간적 동시성만으로 변경 주체를 추정하지 않는다.

## Validation

- Worker는 `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_execution_handlers.py`에서 직접 변경한 범위·게시 전 확인 targeted unit test만 실행한다.
- 격리된 실행 중 무관한 local branch·tag·ref·다른 worktree와 기본 checkout 변화는 비차단, 사용 중인 자원 및 범위 밖 파일·index 변화는 차단, 격리 증거 부재·위조·세션 불일치 시 엄격 fallback을 검증한다.
- 사용 직전 원격 base·소유 branch·게시 ref·PR head 변경과 원격 경쟁은 최신 값으로 재검증하거나 안전 차단하고, 전체 verify에서 기존 workspace 및 PR 소유권 회귀가 없어야 한다.

## Progress

- [ ] 역할·단계별 사용 Git 자원 판정
- [ ] 격리 증거에 따른 엄격/최소 범위 분기
- [ ] Git 사용 직전 소유권 재검증과 targeted 회귀 사례

## Decision Log

- G-088은 비차단에 필요한 쓰기 격리 증거를 제공한다. 이 계획은 Manager 소유 변경의 기준선 갱신을 추정으로 허용하지 않으며 해당 증명은 G-090/G-091이 담당한다.
