---
id: G-035
title: Planner Bytecode Artifact Isolation
sources:
  - docs/requirements/[R-016] Planner Bytecode Artifact Isolation.md
depends_on: []
complexity: medium
risk: medium
priority: critical
---

## Purpose

Planner와 Planner가 시작한 자식 프로세스의 Python 자동 bytecode 기록만 결정적으로 비활성화해, 코드 조사 import가 planning worktree에 ignored `__pycache__` 또는 `.pyc` 순변경을 남기고 유효한 계획을 `planning_error`로 만드는 문제를 제거한다. Codex와 Claude의 최초 실행 및 저장된 세션 재개에 같은 경계를 적용하되, Loop Manager 런타임과 Worker 환경 및 ignored 파일까지 관찰하는 기존 Agent 범위 검사는 그대로 유지한다.

## Acceptance Criteria

- Codex와 Claude Planner의 최초 실행 및 저장된 세션 재개 subprocess 환경은 부모의 bytecode 관련 환경 변수 존재 여부나 값과 관계없이 Python 자동 bytecode 기록을 비활성화한다.
- Planner 환경 설정은 process 전체의 `os.environ`을 변경하지 않고 해당 Planner CLI와 그 자식 프로세스에만 적용된다. Worker, Reviewer, provider 검증·model 조회와 Loop Manager 자체 런타임의 기존 bytecode 동작은 바뀌지 않는다.
- 깨끗한 planning worktree에서 Planner 환경으로 `loop_manager`, `managed_config`와 `project_registry`를 import해도 새 `__pycache__` 디렉터리나 `.pyc` 파일이 생기지 않는다.
- Python import로 코드를 조사한 Planner가 유효한 새 `docs/plans/*.md`만 작성하면 snapshot과 Planner 범위 검증을 통과하며 bytecode artifact 때문에 `planning_error`가 기록되지 않는다.
- 실행 전에 존재하던 ignored cache 파일은 삭제하거나 변경하지 않고 그대로 보존한다. worktree clean, reset 또는 cache 정리 동작을 추가하지 않는다.
- snapshot과 Agent 범위 검사는 ignored untracked 파일을 계속 포함한다. 일반 ignored 파일이나 Planner가 명시적으로 생성·변경한 `.pyc`는 Planner 허용 결과로 취급하지 않고 기존과 같이 범위 위반으로 거부한다.
- provider별 최초·재개 환경, 부모 환경값 강제 덮어쓰기, 실제 import 후 cache 미생성, 기존 cache 보존, Worker 환경 불변과 ignored 파일 범위 위반 유지가 자동 테스트로 검증된다.

## Implementation

- `loop_manager.py`의 필터링된 Agent 환경 구성 경계에 역할 정보를 전달하고, 역할이 `planner`일 때만 Python 자동 bytecode 기록을 비활성화하는 결정적 환경값을 강제로 설정한다. 부모 값은 신뢰하거나 그대로 전달하지 않으며 전역 환경은 수정하지 않는다.
- Codex와 Claude의 streaming 최초 호출과 non-streaming·resume 호출을 포함한 모든 실제 Planner subprocess가 동일한 역할별 환경을 사용하도록 provider adapter 실행 경계를 연결한다. Worker와 Reviewer 호출, 인증 확인 및 provider model/effort 조회에는 Planner 전용 환경을 적용하지 않는다.
- `capture_git_snapshot`, ignored untracked 수집, `validate_agent_scope`의 Planner 허용 경로는 완화하지 않는다. `__pycache__` 또는 `.pyc` 예외, 사후 삭제, worktree 정리나 오류 은폐 로직을 추가하지 않는다.
- 기존 provider 세션 테스트를 확장해 Codex와 Claude 각각의 최초·재개 호출에 전달된 환경을 검증하고, 역할별 환경 생성 및 실제 Python import를 다루는 회귀 테스트를 추가한다.
- 실제 import와 snapshot을 결합한 Planner 시나리오에서 유효한 새 ExecPlan만 순변경으로 남는 경우를 검증하고, 별도로 ignored 일반 파일과 명시적 `.pyc` 생성·변경이 계속 거부되는지 검증한다.

## Validation

- Worker는 `tests/test_loop_manager.py`에서 역할별 Agent 환경, Codex Planner 최초·재개, Claude Planner 최초·재개와 실제 import cache 격리 테스트만 먼저 targeted unit test로 실행한다.
- Worker는 Planner import 뒤 유효한 ExecPlan만 작성한 snapshot 시나리오와 ignored 일반 파일·명시적 `.pyc` 범위 위반 시나리오를 targeted unit test로 실행한다.
- 전체 verify는 기존 전체 suite와 Python compile 검사를 유지하면서, Worker 및 Manager 환경 회귀 없이 두 provider의 모든 Planner 호출 경로가 같은 bytecode 격리 계약을 지키는지 확인한다.

## Progress

- [ ] 역할별 Planner subprocess 환경 경계를 구현한다.
- [ ] Codex와 Claude의 최초·재개 호출에 공통 경계를 적용한다.
- [ ] import cache 미생성과 Planner 정상 결과 회귀 테스트를 추가한다.
- [ ] ignored 파일 및 비-자동 bytecode 범위 위반 회귀 테스트를 보강한다.
- [ ] 관련 targeted unit test를 실행하고 결과를 기록한다.

## Decision Log

- 2026-09-20: 자동 bytecode 생성은 Planner 프로세스 경계에서 예방하고, snapshot 또는 허용 경로에서 artifact를 예외 처리하지 않기로 했다. 이렇게 해야 자동 import cache만 제거하면서 ignored 파일까지 감시하는 기존 안전 경계를 보존할 수 있다.
- 2026-09-20: provider별 구현을 별도 계획으로 나누지 않았다. 공통 역할별 환경 계약과 Codex·Claude 최초·재개 경로가 함께 적용되고 검증돼야 어느 한 경로의 누락 없이 독립적으로 Merge 가능한 결과가 되기 때문이다.
