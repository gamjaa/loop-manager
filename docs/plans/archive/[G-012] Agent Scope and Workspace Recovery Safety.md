---
id: G-012
title: Agent Scope and Workspace Recovery Safety
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

무인 Agent 실행이 역할별 파일 경계와 Loop Manager 소유 Git 작업 공간을 벗어나지 못하게 검증하고, 중단 후 복구가 불가능한 상태에서 사용자 변경을 잃지 않도록 안전하게 차단한다.

## Acceptance Criteria

- Planner, Worker와 Reviewer 호출 전후에 Git HEAD, index와 worktree 변경을 코드로 비교하고 역할별 허용 범위를 검증한다.
- Planner는 새 `docs/plans/*.md`만 만들 수 있고, Worker는 구현·테스트·verify 스크립트와 허용된 `PRODUCT.md`, 선택된 ExecPlan 및 `FUTURE_IDEAS.md`만 변경할 수 있으며 Requirement, ARCHITECTURE, AGENTS 또는 다른 ExecPlan을 변경할 수 없다.
- Reviewer가 tracked, staged 또는 untracked 변경을 만들거나 HEAD를 바꾸면 Review 결과와 관계없이 현재 ExecPlan을 `BLOCKED`로 만든다.
- Worker나 Planner가 직접 commit, branch 전환, 허용되지 않은 파일 변경 또는 관리 worktree 밖의 결과를 만들면 게시하지 않고 진단 가능한 원인과 변경을 보존한다.
- 저장된 worktree가 없고 Loop Manager 소유 branch가 있으면 같은 branch로 worktree를 복구한다.
- 저장된 worktree와 branch를 모두 복구할 수 없거나 worktree가 다른 branch/소유 상태를 가리키면 origin base에서 새 branch를 만들어 작업을 잃은 것처럼 진행하지 않고 `BLOCKED`로 만든다.
- 계획·구현 branch와 worktree 경로가 해당 프로젝트와 ExecPlan의 결정적 소유 경계와 일치하는지 확인한다.
- 안전 검증이나 차단 과정은 대상 프로젝트 기본 worktree의 파일, index와 HEAD를 수정하지 않고 Agent 환경에 Telegram token 등 Loop Manager 비밀값을 전달하지 않는다.
- 범위 위반 뒤에도 기존 변경과 세션·상태를 보존하며, 사용자가 ExecPlan 또는 근거 문서를 바꾼 정상 BLOCKED 해제 규칙만 사용한다.

## Implementation

- `loop_manager.py`의 Git 상태 검사와 worktree 생성 로직을 역할별 snapshot/diff 및 소유권 검증 함수로 분리한다.
- Agent가 시작할 때의 commit과 파일 상태를 저장하고 반환 직후 상태 전이 전에 검증한다.
- 활성 worktree 복구를 `기존 worktree 검증`, `소유 branch에서 재생성`, `복구 불가 차단`의 명시적 상태 전이로 만든다.
- 범위 위반 진단은 경로와 변경 종류를 포함하되 비밀값이나 파일 내용을 불필요하게 로그에 복사하지 않는다.

## Validation

- Worker는 역할별 scope validator와 workspace recovery 테스트 class만 targeted unit test로 실행한다.
- Planner 기존 파일 수정, Worker 사용자 소유 문서 변경, Reviewer 변경, Agent commit과 branch 전환을 각각 차단하는 테스트를 추가한다.
- worktree만 사라진 복구, branch와 worktree가 모두 사라진 상태, 잘못 연결된 worktree와 정상 재개를 검증한다.
- 전체 verify에서 기존 Planner batch 검증, Worker commit, read-only Review와 기본 worktree 보존 동작이 유지돼야 한다.

## Progress

- [ ] 역할별 Git snapshot과 변경 범위 검증 구현
- [ ] 관리 branch/worktree 소유권 및 복구 상태 구현
- [ ] 범위 위반 BLOCKED 진단과 상태 보존 구현
- [ ] 안전 경계 회귀 테스트 추가

## Decision Log

- 안전을 증명할 수 없는 작업 공간은 자동 재생성하지 않고 사용자 확인이 가능한 BLOCKED 상태로 보존한다.
- 현재 목표에 필요한 Git 안전 경계를 테스트 가능한 책임으로 분리하되 상태 모델이나 branch 명명 규칙은 바꾸지 않는다.
