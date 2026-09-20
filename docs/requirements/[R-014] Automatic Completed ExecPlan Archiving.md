# [R-014] Automatic Completed ExecPlan Archiving

## Context

ExecPlan의 실행 상태는 SQLite에만 저장되므로 `docs/plans/`의 파일만 확인해서는 어떤 계획이 완료됐는지 바로 구분할 수 없다. MERGED 계획과 아직 처리할 계획이 같은 폴더에 계속 쌓이면 사람이 현재 작업 범위를 파악하기 어렵다.

단순히 완료된 파일을 이동하면 기존 경로를 사용하는 SQLite 기록, 의존성 검증, ID 번호 할당, 작업 복구와 PR의 계획 링크가 깨질 수 있다. 특히 구현 branch에서 작업과 함께 계획 파일을 이동하면 Worker 재시도, verify, Review와 PR 게시 중에 활성 계획의 경로가 사라져 복구 흐름이 복잡해진다.

완료 여부는 계속 SQLite와 GitHub Merge 상태로 판단해야 한다. 구현 작업이 끝난 뒤 Loop Manager가 AI Agent와 분리된 정리 단계에서 완료 계획만 자동으로 이동해야 한다.

이 Requirement는 `[R-005] Automatic Requirement Archiving`의 Non-Goal 중 ExecPlan을 archive하지 않는다는 부분만 대체한다. Requirement archive의 완료 조건과 동작은 변경하지 않는다.

## Requirement

Loop Manager는 ExecPlan 구현 PR에 계획 파일 이동을 포함하지 않아야 한다. Worker, verify, Reviewer와 PR 게시가 진행되는 동안 해당 ExecPlan은 계속 최초 `docs/plans/[G-000] Title.md` 경로에 있어야 한다.

사용자가 구현 PR을 Merge하고 다음 실행에서 GitHub Merge가 확인되면 Loop Manager는 먼저 SQLite 상태를 MERGED로 변경해야 한다. 그 후 최신 `origin/<base-branch>`를 기준으로 AI Agent가 사용하지 않는 별도 임시 worktree를 만들고, 기본 branch의 `docs/plans/` 바로 아래에 남아 있는 MERGED ExecPlan을 `docs/plans/archive/`로 일괄 이동해야 한다.

이 문서에서 archive 정리 commit은 Loop Manager 코드가 완료된 계획 파일의 내용은 바꾸지 않고 경로만 이동하기 위해 자동으로 만드는 commit을 뜻한다. 별도 ExecPlan을 만들거나 Planner, Worker와 Reviewer를 호출하지 않으며 PR도 생성하지 않는다. Loop Manager는 정리 commit을 `origin/<base-branch>`에 fast-forward로 직접 push해야 한다.

계획 파일 이동 전후의 내용 hash는 SQLite에 저장된 원본 ExecPlan hash와 같아야 한다. 원본이 일반 파일이고 archive 목적지가 존재하지 않으며 해당 계획의 GitHub Merge가 확인된 경우에만 이동한다. source 누락, hash 불일치, symlink, 목적지 충돌 또는 Merge 상태 불확실성이 있으면 덮어쓰거나 완료 상태를 추정하지 않는다.

하나의 후보에 문제가 있어도 다른 안전한 MERGED 계획은 같은 정리 commit으로 이동할 수 있다. 이동하지 못한 계획은 SQLite에 archive 대기 사유를 기록하고 이후 실행에서 다시 확인한다. archive 실패는 이미 확인된 MERGED 상태를 되돌리거나 다음 READY 계획, Planner와 프로젝트 완료 처리를 막지 않는다.

정리 commit을 만들기 직전과 push 직전에 원격 base branch를 다시 확인해야 한다. 원격이 바뀌었으면 rebase나 force push를 하지 않고 임시 worktree를 폐기한 뒤 최신 원격 기준으로 후보를 다시 계산한다. branch protection, 권한 또는 일시적인 네트워크 문제로 fast-forward push가 실패하면 원격을 우회하지 않고 archive 대기 상태로 남겨 다음 실행에서 재시도한다.

SQLite의 ExecPlan `path`는 Planner가 게시한 최초 활성 경로를 안정적인 식별자로 유지한다. archive 경로는 최초 경로의 파일명을 사용해 결정적으로 계산한다. 기존 PR 본문의 ExecPlan 링크도 최초 게시 commit SHA와 최초 경로를 사용하는 불변 링크로 유지한다.

Loop Manager는 활성 실행 후보를 `docs/plans/` 바로 아래에서만 찾고 `archive/`의 계획은 실행하거나 Planner와 AI Agent의 기본 입력으로 전달하지 않는다. 다만 다음 검증과 복구에는 활성 계획, archived 계획과 SQLite의 전체 메타데이터를 함께 사용해야 한다.

- 프로젝트 전체에서 ExecPlan ID가 고유한지 확인한다.
- 다음 ExecPlan 번호를 과거 최대 ID 다음으로 할당한다.
- `depends_on` 대상의 존재와 MERGED 상태를 확인한다.
- SQLite를 재구성하거나 기존 설치 이력을 이관한다.
- archived 계획의 내용이 저장된 원본 hash와 일치하는지 확인한다.

활성 계획이 archived 계획에 의존하는 것은 허용하며, 해당 archived 계획의 Merge가 SQLite와 GitHub 이력으로 확인된 경우에만 의존성이 충족된 것으로 본다. archive에 파일이 있다는 사실만으로 의존성이 충족됐다고 판단하지 않는다.

새 서버 등록이나 SQLite 복구 시 archived 계획은 관련 PR의 Merge 또는 동일한 수준의 Git 이력으로 완료를 증명할 수 있을 때만 MERGED로 등록한다. 완료 여부를 증명할 수 없는 archived 계획이 있으면 READY로 실행하거나 다시 이동하지 않고 구체적인 진단을 기록한다.

archive 기능을 도입하는 ExecPlan의 구현 PR도 자신의 계획 파일을 이동하지 않는다. 기능 도입 PR이 Merge된 뒤 새 런타임의 첫 정리 단계가 해당 계획을 포함해 기존에 MERGED됐지만 아직 활성 경로에 남아 있는 모든 계획을 상태와 이력으로 계산해 이동한다. 별도의 bootstrap ExecPlan이나 특정 ExecPlan ID 목록을 사용하지 않는다.

archive 정리 commit은 계획 입력 변경, 새 작업 주기, Planner 재실행 또는 프로젝트 완료 알림을 발생시키지 않는다.

## Acceptance Criteria

- ExecPlan 구현 PR에는 해당 계획 파일의 archive 이동이 포함되지 않는다.
- PR이 Open, Closed 또는 검증·Review 중인 동안 계획 파일은 최초 `docs/plans/[G-000] Title.md` 경로에 남는다.
- GitHub Merge 확인과 SQLite MERGED 전이가 끝난 뒤에만 해당 계획이 archive 후보가 된다.
- Loop Manager는 최신 `origin/<base-branch>`의 별도 임시 worktree에서 MERGED 계획을 `docs/plans/archive/`로 이동한다.
- archive 정리 commit에는 내용이 동일한 계획 파일의 경로 이동만 포함되며 제품 코드나 다른 문서를 변경하지 않는다.
- archive 이동은 Loop Manager 코드가 수행하고 Planner, Worker, Reviewer 또는 다른 AI Agent를 호출하지 않는다.
- archive를 위한 별도 ExecPlan, branch 또는 PR을 생성하지 않는다.
- 정리 commit은 최신 원격 base branch에 fast-forward push하고 force push, merge commit과 자동 rebase를 사용하지 않는다.
- 원격 base branch가 정리 도중 변경되면 기존 정리 commit을 push하지 않고 최신 원격에서 후보를 다시 계산한다.
- 계획 파일의 원본과 archive 내용이 저장된 hash와 다르거나 목적지가 이미 존재하면 해당 파일을 덮어쓰지 않는다.
- 이동할 수 없는 계획에는 archive 대기 사유를 저장하며 이미 확인된 MERGED 상태와 다른 작업 진행을 되돌리지 않는다.
- 안전한 다른 MERGED 계획은 충돌한 후보와 분리해 archive할 수 있다.
- push 실패 후에도 다음 실행에서 같은 계획을 중복 이동하거나 중복 commit하지 않고 다시 시도할 수 있다.
- SQLite에는 최초 `docs/plans/[G-000] Title.md` 경로를 유지하며 archive 이동 때문에 기존 경로 식별자나 PR의 불변 링크를 변경하지 않는다.
- Planner와 Worker 선택은 `docs/plans/` 바로 아래의 비종료 계획만 대상으로 하며 archive 본문을 기본 AI 입력으로 읽지 않는다.
- ID 중복 검사와 다음 번호 할당은 활성 및 archived ExecPlan 전체를 고려한다.
- archived ExecPlan을 가리키는 `depends_on`은 해당 계획의 GitHub Merge가 확인된 경우 정상적으로 충족된다.
- archive에 파일만 있고 Merge를 증명할 수 없으면 완료로 추정하거나 실행하지 않는다.
- 새 환경에서 SQLite를 재구성해도 활성 계획과 완료 계획을 구분하고 기존 ID 및 의존성 관계를 보존한다.
- 기능 도입 PR에는 기존 계획과 자신의 계획 이동이 포함되지 않으며, Merge 후 첫 정리 단계가 상태로 계산한 기존 MERGED 계획을 일괄 archive한다.
- 별도 bootstrap ExecPlan이나 특정 계획 ID를 만들거나 코드에 고정하지 않는다.
- archive 정리 commit만으로 Planner가 다시 실행되거나 새로운 프로젝트 완료 알림이 전송되지 않는다.
- PR 미Merge, Merge 후 일괄 이동, 내용 변조, 목적지 충돌, 일부 후보 실패, 원격 변경 경쟁, push 실패와 재시도, ID 연속성, archived 의존성, 신규 환경 복구와 기존 MERGED 계획 이관을 자동 테스트로 검증한다.

## Product Impact

- `docs/plans/`만 확인해 아직 완료되지 않은 ExecPlan 목록을 파악할 수 있다.
- 완료된 ExecPlan은 구현 작업과 분리된 자동 정리 단계에서 `docs/plans/archive/`로 이동한다.
- 계획 파일 이동이 Worker 재시도, verify, Review와 구현 PR 게시에 영향을 주지 않는다.
- 계획 파일을 정리해도 기존 ID, 의존성, 복구 정보와 PR 링크는 유지된다.

## Non-Goals

- 구현 branch나 구현 PR에서 ExecPlan 파일을 archive하지 않는다.
- archive 파일 위치를 ExecPlan 완료 상태의 원천으로 사용하지 않는다.
- ExecPlan frontmatter에 실행 상태를 추가하지 않는다.
- 사용자가 수동으로 이동한 계획을 자동으로 MERGED 처리하지 않는다.
- archive 정리를 위한 별도 ExecPlan이나 PR을 만들지 않는다.
- archive push 실패를 우회하기 위해 force push하거나 branch protection을 무시하지 않는다.
- archived ExecPlan을 Planner 또는 AI Agent의 기본 작업 입력으로 다시 제공하지 않는다.
- archived ExecPlan의 내용을 자동으로 수정하거나 삭제하지 않는다.
- Requirement archive의 eligibility와 이동 규칙을 변경하지 않는다.

## Open Questions

- 없음
