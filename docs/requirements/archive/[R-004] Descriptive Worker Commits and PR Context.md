# [R-004] Descriptive Worker Commits and PR Context

## Context

현재 Worker 변경의 commit 제목은 `Implement <ExecPlan ID>: <ExecPlan title>` 형식이라 실제로 무엇이 바뀌었는지 commit 목록만 보고 파악하기 어렵다. ExecPlan 제목은 branch와 PR에서 이미 확인할 수 있으므로 commit 제목은 최종 코드 변경을 설명하는 데 집중해야 한다.

PR 본문도 자동 생성 문구만 포함해 구현의 근거가 된 ExecPlan과 source 문서를 바로 확인하기 어렵다. Requirement는 완료 후 archive로 이동할 수 있으므로 현재 branch 이름을 사용하는 링크는 나중에 깨질 수 있다.

## Requirement

Worker는 구현과 targeted unit test를 마친 뒤 최종 변경 사항을 설명하는 간결한 한 줄 commit 제목을 제공해야 한다. Loop Manager는 이 제목을 Worker 변경 commit에 사용해야 하며, `Implement` 같은 일반적인 접두사나 ExecPlan 제목을 반복하지 않아야 한다.

PR 본문에는 실제 변경 요약, 관련 ExecPlan 링크와 ExecPlan의 목적 요약을 포함해야 한다. ExecPlan의 `sources`에 기록된 PRODUCT 또는 Requirement도 함께 연결하되, ExecPlan을 계획한 기준 commit SHA에 고정된 링크를 사용해 이후 파일이 이동돼도 당시 근거 문서를 열 수 있어야 한다.

## Acceptance Criteria

- Worker가 생성한 commit 제목은 최종 변경을 설명하는 한 줄이며 ExecPlan 제목을 그대로 반복하거나 `Implement <ExecPlan>` 형식만 사용하지 않는다.
- verify 실패 수정 등으로 Worker가 변경을 추가한 경우 최종 commit 제목은 수정 후의 전체 diff를 반영한다.
- commit 제목이 비어 있거나 여러 줄이거나 실제 변경을 설명하지 않는 일반 문구이면 Loop Manager는 같은 Worker 세션에 유효한 제목을 다시 요청하고 임의의 ExecPlan 제목으로 대체하지 않는다.
- Planner가 작성한 계획 commit의 기존 제목 규칙은 변경하지 않는다.
- PR 본문에는 실제 구현 내용을 간단히 설명하는 Summary가 포함된다.
- PR 본문에는 ExecPlan ID와 제목, 해당 ExecPlan 파일의 링크와 Purpose를 바탕으로 한 짧은 설명이 포함된다.
- PR 본문에는 ExecPlan `sources`의 각 경로와 링크가 포함된다.
- ExecPlan과 source 링크는 계획 당시의 commit SHA를 가리켜 Requirement가 archive로 이동하거나 현재 branch에서 경로가 바뀌어도 유효하다.
- 경로의 공백, 대괄호와 비 ASCII 문자가 GitHub URL에서 올바르게 인코딩된다.
- commit 제목 생성, Worker 재요청 조건, PR 본문 구성과 immutable 링크 생성을 자동 테스트로 검증한다.

## Product Impact

- commit 목록만 보고도 각 Worker 변경의 실제 내용을 구분할 수 있다.
- PR에서 구현 목적과 근거 문서를 바로 검토할 수 있다.
- 완료된 Requirement를 archive로 이동해도 기존 PR의 source 링크는 유지된다.

## Non-Goals

- Planner가 생성하는 계획 commit의 제목 형식을 변경하지 않는다.
- PR 제목 형식을 변경하지 않는다.
- PR 본문에 ExecPlan이나 source 문서 전체를 복사하지 않는다.
- Requirement archive 정책을 변경하지 않는다.

## Open Questions

- 없음
