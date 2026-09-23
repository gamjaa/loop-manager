---
id: G-053
title: Ignored Artifact Tolerance at Git Scope Boundaries
sources:
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
depends_on:
  - G-051
complexity: medium
risk: high
priority: critical
---

## Purpose

`_capture_local_worktree_state`의 `git ls-files --others`와 Agent snapshot 비교가 ignored `__pycache__`까지 변경으로 기록한다. Planner import, Worker test와 전체 verify가 생성한 artifact만으로 자동 실행이 차단되며, 예방 환경 설정을 계속 추가해야 하는 반복 수동 복구 원인이 된다.

## Acceptance Criteria

- ignored-only 파일의 생성·수정·삭제는 Planner·Worker·Reviewer scope와 관리 worktree clean 판정에서 허용한다. Git이 추적하는 파일과 index, non-ignored untracked, HEAD, ref, config, worktree catalog와 operation 변화는 기존대로 검증한다.
- checkout된 tracked 파일이 ignore 규칙에 맞더라도 제외하지 않고, ignored 파일이 이후 publishable 경로와 겹치는 경우 안전 검사를 우회하지 않는다.
- Planner 전용 `PYTHONDONTWRITEBYTECODE` 강제는 제거하고 Codex·Claude 최초 호출과 세션 재개에서 동일 정책을 적용한다.
- 기존 snapshot 직렬화 및 저장 형식, Agent별 수정 허용 경로와 사용자 상태 전이는 유지한다.

## Implementation

- `loop_manager.py`의 `_capture_local_worktree_state`와 비활성 관리 worktree signature에 Git의 exclude-standard 의미를 일관되게 적용한다. 저장된 옛 snapshot은 시작 시점의 ignore 규칙을 증명할 수 있을 때만 정규화하고, 확인할 수 없는 차이는 안전 실패로 남긴다.
- `validate_persisted_agent_scope`의 Git 어댑터가 검증한 snapshot을 `agent_scope_decision.py`의 `decide_persisted_scope`에 전달한다. 순수 판정 모듈에 파일 탐색이나 Git 호출을 넣지 않고 기존 저장 형식과 역할별 권한 검사를 유지한다.
- 관리 worktree cleanliness를 호출별 목적에 맞게 확인하고, tracked/index 보호 검사를 공통 경계에 둔다.
- Planner 호출의 bytecode 예방 설정과 그 설정에 종속된 테스트를 실제 artifact 허용 테스트로 교체한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 Agent snapshot·ignored 파일, `tests/test_agent_scope_decision.py`의 저장된 snapshot 판정, `tests/test_planning_runtime.py`의 Planner 환경 관련 targeted unit test만 실행한다.
- Planner import와 Worker·Reviewer·verify artifact의 생성·수정·삭제, 이전 persisted snapshot, tracked ignored 파일, staged/index flag 및 non-ignored 파일 거부를 검증한다.
- 전체 verify에서 scope 격리, workspace ownership 및 PR 게시 안전 검사가 약화되지 않아야 한다.

## Progress

- [ ] ignored artifact 판정 경계 통일
- [ ] 역할별 artifact·권위 변경 targeted test 갱신

## Decision Log

- `tests/test_loop_manager.py`의 현재 ignored 변경 거부 사례와 PR preflight artifact 사례를 새 계약에 맞게 갱신한다.
- G-051의 분리된 판정 경계는 유지한다. ignore 규칙과 실제 Git 상태의 증명은 `loop_manager.py` 어댑터에서 수행한다.
