# [R-016] Planner Bytecode Artifact Isolation

## Context

Loop Manager의 Agent 범위 검사는 `.gitignore`에 포함된 파일도 Agent 실행 전후에 비교한다. 이는 Planner와 Reviewer가 ignored 파일을 이용해 허용 범위 밖의 상태를 남기는 것을 막는 안전 경계이며 유지돼야 한다.

하지만 Planner가 코드와 테스트를 조사하기 위해 Python 모듈을 import하면 Python이 `__pycache__/*.pyc`를 자동으로 생성할 수 있다. 이 파일은 Planner가 의도한 계획 결과가 아니어도 ignored untracked 변경으로 감지되며, 유효한 ExecPlan을 작성한 실행까지 Git 범위 위반과 `planning_error`로 중단시킨다. 발생 여부가 Planner가 선택한 조사 명령과 기존 cache 유무에 따라 달라져 반복 실행의 결과도 불안정해진다.

## Requirement

Loop Manager는 Planner와 Planner가 시작한 자식 프로세스가 Python 모듈을 import하더라도 자동 bytecode cache를 planning worktree 안에 기록하지 않도록 결정적인 실행 환경을 제공해야 한다. 최초 Planner 호출과 저장된 Planner 세션 재개에 같은 경계를 적용하고, 지원하는 모든 Agent provider에서 동일하게 동작해야 한다.

이 예방 조치는 Planner 실행 환경에만 적용한다. Loop Manager 런타임과 Worker의 테스트·빌드·패키징 환경은 변경하지 않는다.

Agent 범위 검사는 ignored 파일의 생성과 변경을 계속 관찰해야 한다. 자동 bytecode 기록을 막기 위해 ignored 파일 전체를 검사 대상에서 제외하거나 `__pycache__`를 Planner 허용 경로로 추가해서는 안 된다. Planner가 자동 import cache가 아닌 ignored 파일을 만들거나 명시적인 bytecode 생성 명령으로 planning worktree를 변경하면 기존과 같이 범위 위반으로 처리한다.

기존 cache를 삭제하거나 planning worktree를 정리해서 문제를 숨기지 않는다. 실행 전부터 있던 ignored 파일은 그대로 보존하고, Planner 실행이 만든 순변경만 기존 snapshot 정책으로 판단한다.

## Acceptance Criteria

- Codex와 Claude Planner의 최초 실행 및 저장된 세션 재개는 Python의 자동 bytecode 기록이 비활성화된 결정적인 환경을 사용한다.
- 깨끗한 planning worktree에서 Planner가 `loop_manager`, `managed_config` 또는 `project_registry`를 import해도 `__pycache__`나 `.pyc` 파일이 생성되지 않는다.
- Planner가 Python으로 코드를 조사한 뒤 유효한 새 `docs/plans/*.md`만 작성하면 bytecode artifact 때문에 Git 범위 위반이나 `planning_error`가 발생하지 않는다.
- 부모 프로세스에 bytecode 관련 환경 변수가 없거나 다른 값이 있어도 Planner 실행 경계는 동일하게 적용된다.
- Worker와 Loop Manager 런타임의 Python bytecode 동작은 이 변경으로 바뀌지 않는다.
- Agent snapshot과 범위 검사는 `.gitignore`에 포함된 untracked 파일의 생성과 변경을 계속 감지한다.
- Planner가 일반 ignored 파일을 생성·변경하거나 명시적으로 `.pyc`를 작성하면 기존과 같이 Git 범위 위반으로 거부된다.
- 기존 `__pycache__`와 `.pyc`를 삭제하거나 worktree를 clean하는 복구 동작을 추가하지 않는다.
- provider별 최초·재개 실행 환경, import 후 cache 미생성, ignored 파일 범위 위반 유지와 Worker 환경 불변을 자동 테스트로 검증한다.

## Product Impact

- Planner는 Python 코드를 조사하면서 자동 생성되는 bytecode cache 때문에 유효한 계획 작업이 중단되지 않는다.
- ignored 파일까지 검사하는 Agent 작업 범위 안전성은 그대로 유지된다.
- Worker의 targeted test와 프로젝트 빌드·패키징 동작은 기존 환경을 유지한다.

## Non-Goals

- ignored 파일을 Agent 범위 검사에서 제외하지 않는다.
- `__pycache__` 또는 `.pyc`를 Planner의 허용 결과로 취급하지 않는다.
- Worker나 Loop Manager 런타임 전체에서 Python bytecode 생성을 비활성화하지 않는다.
- 기존 cache 파일을 자동으로 삭제하거나 관리 worktree를 clean하지 않는다.
- Planner가 명시적으로 요청한 bytecode 빌드 또는 패키징 산출물을 허용하지 않는다.

## Open Questions

- 없음
