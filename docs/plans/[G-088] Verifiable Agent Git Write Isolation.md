---
id: G-088
title: Verifiable Agent Git Write Isolation
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-051
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 Codex의 `--approve-for-me`와 Claude의 `acceptEdits` 호출만으로는 Agent가 선택된 관리 worktree 밖의 파일과 공유 Git metadata를 쓸 수 없다는 증거가 없다. R-023의 무관한 Git 변화 비차단 판단에 앞서 실행별 쓰기 격리를 검증 가능한 상태로 만든다.

## Acceptance Criteria

- Planner·Worker·Reviewer와 유지보수 Planner의 실제 호출에서 선택된 관리 worktree의 허용 파일·index 경계 밖, 기본 source checkout, 다른 worktree 및 shared Git metadata에 대한 Agent 쓰기가 차단됨을 실행 전에 증명한다. provider 내부 동작 때문에 이를 분리해 증명할 수 없는 환경은 격리 확인으로 표시하지 않는다.
- 격리 증거는 provider, 실행 환경, 역할, 선택된 canonical worktree, repository identity 및 Agent session/turn에 결합해 보존한다. 재개·provider 전환·설정 변경·중단 뒤에는 이전 증거를 무조건 재사용하지 않는다.
- 쓰기 격리를 검증할 수 없는 실행과 이를 증명할 수 없는 과거 기록은 기존의 엄격한 shared ref·다른 worktree 감시를 유지한다. 격리 확인 실패를 근거로 Agent의 Git·worktree·push·PR 권한을 확대하거나 무관한 변화를 외부 변경으로 추정하지 않는다.
- 지원하는 Linux·Windows 및 Codex·Claude 호출에서 격리 적용·거부 결과가 명확하며, 비밀값과 기본 worktree의 로컬 상태를 노출하거나 변경하지 않는다. 격리와 기존 허용 변경이 양립하지 않는 경우에는 무인 실행을 안전하게 멈추고 구체적 원인을 남긴다.

## Implementation

- `agent_invocation.py`와 `loop_manager.py`의 provider 호출 경계에 실행 가능한 격리 설정 및 실제 효력 확인을 넣는다. 단순 CLI 인자나 작업 디렉터리 지정만으로 확인했다고 기록하지 않는다.
- 검증된 실행만 식별할 수 있는 최소 durable 증거를 기존 Agent session·snapshot 상태와 연결한다. 선택된 worktree의 허용된 파일·index 처리와 provider 자체의 임시 ref 동작을 분리해, shared metadata 쓰기를 격리할 수 없다면 엄격 판정 경로를 유지한다.
- 호출 직전의 canonical path, worktree 등록, repository identity와 격리 설정을 대조하고 실패·중단·재시작 경로의 증거 수명을 명시한다. G-089는 이 증거가 유효한 경우에만 범위 축소를 사용한다.

## Validation

- Worker는 `tests/test_agent_invocation.py`와 직접 변경한 `tests/test_origin_isolation.py`의 격리·session targeted unit test만 실행한다.
- 선택 worktree 쓰기 허용, 기본 checkout·다른 worktree·shared metadata 쓰기 거부, provider/OS별 설정 불가, 재개·전환·중단 뒤 증거 무효화와 과거 세션의 엄격 fallback을 검증한다.
- 전체 verify에서 기존 Agent 결과 protocol, 승인된 guidance, process lock과 비밀값 격리가 유지돼야 한다.

## Progress

- [ ] 실행별 쓰기 격리와 확인 증거 연결
- [ ] 증거 없는 실행의 엄격 fallback 및 회귀 검증

## Decision Log

- R-023은 격리 증명 없는 실행에서 무관한 Git 변화의 비차단을 허용하지 않는다. 특정 CLI 옵션의 존재를 격리 증명으로 간주하지 않는다.
