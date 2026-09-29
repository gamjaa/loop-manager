# [R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh

## Context

`PLAN_BLOCKED` 계획의 원격 작업 브랜치에 사용자가 후속 commit을 게시하면 R-039의 조건에 따라 이를 확인할 수 있다. 그러나 다른 계획이 실행 중이면 차단 계획의 재평가가 시작되지 않고, 게시 checkpoint가 없는 계획은 작업 브랜치의 변경을 독립적인 신호로 확인하지 않는다. `PROJECT_BLOCKED`에서는 PR 상태와 안전 해제 검사 외의 원격 base 갱신 및 계획 입력 대조도 중단된다. 그 결과 차단 중에 원격 base나 작업 브랜치가 바뀌어도 계획의 rebase, 원인 재검증과 현재 작업물 게시가 진행되지 않을 수 있다.

R-015는 안전한 차단 작업물을 PR 없이 원격 checkpoint에 보존하고, R-039는 게시된 작업 브랜치의 안전한 후속 commit을 채택한다. 이 Requirement는 두 흐름을 차단 중의 지속적인 원격 관측과 base 반영까지 연결한다. 미완료 Git 작업이나 소유권 불확실성을 단순히 해제 근거로 취급하지 않는다.

## Requirement

Loop Manager는 `PLAN_BLOCKED` 및 계획과 연결된 `PROJECT_BLOCKED` 동안에도 매 실행에서 원격 base와 해당 계획의 정확한 원격 작업 브랜치를 확인한다. 관측한 OID, 조회 실패 및 마지막으로 처리한 입력을 계획별로 보존한다. 다른 계획이 실행 중이어도 원격 변경 관측은 계속하되, 같은 프로젝트의 rebase·checkpoint 게시와 새 Agent 시작은 기존 직렬 실행 경계에서 처리한다. 차단된 활성 계획은 자신이 점유한 실행 자리에서 복구를 계속할 수 있고, 다른 차단 계획의 Git 쓰기는 그 자리가 빌 때까지 대기한다.

원격 작업 브랜치가 바뀌었으면 기존 게시 checkpoint의 후속 관계와 로컬 작업 보존 조건을 먼저 검사해 안전한 사용자 commit을 반영한다. 원격 base가 바뀌었으면 해당 계획의 관리 작업 공간에서 최신 base를 rebase로 반영한다. 두 브랜치가 모두 바뀌었으면 작업 브랜치의 변경을 확인·채택한 뒤 확정된 base에 rebase한다. rebase 전후의 계획·branch·worktree, 입력 OID, commit 계보와 Git 작업 증거를 저장하고 재시작 후에도 같은 작업을 중복 실행하지 않는다.

변경 반영이 끝나면 저장된 원래 차단 원인을 현재 계획 문서, 근거 문서와 작업물에 대해 다시 검사한다. 원인이 해결되고 다른 유효한 차단이 없을 때만 기존 계획을 재개하며, 변경된 후보 revision에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 수행한다. 원인이 남아 있으면 차단 상태와 이유를 보존하고 안전한 현재 작업물을 원격에서 확인할 수 있게 checkpoint로 게시한다. 기존 원격 head와 동일하면 빈 commit을 만들거나 같은 push를 반복하지 않고 그 OID를 현재 게시 결과로 표시한다.

`PROJECT_BLOCKED` 자체는 이 계획별 원격 관측·복구·게시의 일괄 금지 조건이 아니다. 기존 작업 브랜치의 게시 계보를 증명하지 못해도 검증 가능한 작업물은 rebase한 뒤 원격 ref를 덮어쓰지 않는 별도의 진단 branch에 게시할 수 있다. 이 경우 원래 작업 브랜치와의 관계를 표시한다. 대상 작업물의 범위·Git 상태를 검증할 수 없거나 rebase 충돌이 미해결이면 commit·push하지 않고 작업물과 세션을 보존하며 원격 게시 불가 사유를 알린다. 진단 branch 게시는 원래 작업 브랜치의 소유권 확인, 차단 해제 또는 PR 생성으로 간주하지 않는다.

## Acceptance Criteria

- `PLAN_BLOCKED`와 계획에 연결된 `PROJECT_BLOCKED`에서도 매 실행 원격 base와 정확한 작업 브랜치의 OID 또는 조회 실패가 관측된다. 다른 계획이 활성인 동안에는 관측 결과를 잃지 않는다. rebase·push는 실행 자리가 비었거나 차단된 동일 계획이 그 자리를 점유하고 대상 작업의 검증 조건을 충족할 때 처리한다.
- 원격 base만 전진하고 계획·Requirement 내용 지문이 같아도 차단 계획은 변경된 base를 반영할 대상으로 기록한다. 작업 브랜치만 안전한 후속 commit으로 바뀐 경우에도 사용자 변경을 채택해 원인을 재평가한다. 두 ref가 함께 바뀌면 작업 브랜치 변경을 먼저 확인하고 최신 base로 rebase한다.
- 완료된 rebase는 정확한 원본 commit, target base, 소유 Git 작업과 최종 branch·worktree 상태로 검증한다. rebase 충돌은 기존 교정 경로로 넘기거나 미해결 상태를 보존하며, 충돌 중인 index나 detached HEAD를 checkpoint로 게시하지 않는다.
- 원래 차단 원인이 해소되면 그 원인만 해제한다. 사용자 결정 대기, 닫힌 PR의 reopen 조건, 별도의 소유권·안전 차단 및 다른 계획의 차단은 원격 OID 변경만으로 해제되지 않는다. 재개한 revision은 필요한 검증과 Review를 통과하기 전까지 PR을 만들지 않는다.
- 원인이 남고 작업 공간이 안전하면 현재 내용을 같은 계획의 원격 작업 브랜치에 checkpoint로 게시한다. 기존 게시 ref의 갱신은 관측한 원격 OID에 결합된 fast-forward 또는 기존 PR 없는 소유 branch의 검증된 rebase에 한한 정확한 lease를 사용한다. 재게시 후에도 계획은 차단 상태다.
- 프로젝트 차단 중에도 대상 계획의 독립적인 작업 범위를 검증할 수 있으면 위 rebase·게시를 진행할 수 있다. 원래 작업 브랜치의 게시 계보가 불확실하면 그 ref를 덮어쓰지 않는 별도 진단 branch로만 게시한다. 진단 branch는 같은 내용의 재실행에 중복 생성되지 않고, 기존의 다른 내용을 덮어쓰지 않으며 PR을 만들지 않는다. 작업물 범위나 Git 상태를 검증하지 못하면 local-only 이유를 남긴다.
- 새로 게시할 내용이 없고 원격 head가 확인된 현재 결과와 같으면 빈 commit과 반복 push를 만들지 않는다. 원인과 원격 head가 같을 때 반복 rebase, 재검증, 알림을 만들지 않으며 관련 원격 ref나 작업물 입력이 바뀌면 다시 처리한다.
- 원격 branch 삭제·재작성·분기, 조회와 fetch·push 사이의 OID 경쟁, 기존 PR, 권한·branch protection 오류, 불완전한 legacy 증거와 미해결 Git 작업은 사용자 commit과 로컬 작업물을 보존하며 구체적인 원인으로 기록한다. 예상하지 못한 원격 ref를 force push하지 않는다.
- fetch, rebase, checkpoint commit, push, 원인 재검증과 SQLite 반영 사이에서 중단돼도 저장된 OID·작업 identity·실제 Git 결과를 대조해 중복 commit·push나 잘못된 차단 해제를 피한다.
- 실제 Git 이력으로 base 전진, 작업 브랜치 후속 commit, 동시 변경, 지속 차단의 게시, 해결 후 재검증, 프로젝트 차단 중 복구, rebase 충돌, 원격 경쟁과 재시작을 검증한다.

## Product Impact

- 차단 중에도 원격 base와 작업 브랜치의 변경을 놓치지 않고, 안전하게 반영한 최신 작업물을 원격에서 확인할 수 있다.
- `PROJECT_BLOCKED` 중 게시 계보가 불확실해도 검증 가능한 작업물의 복구·별도 진단 branch 게시를 허용하되, 확인되지 않은 기존 원격 ref를 덮어쓰거나 차단을 자동 해제하지 않는다.

## Non-Goals

- 자동 PR 생성·Merge, 기본 worktree의 rebase, 기존 PR의 head 재작성 또는 다른 계획의 branch 변경을 허용하지 않는다.
- 미해결 충돌이나 검증할 수 없는 작업물의 강제 게시를 요구하지 않는다.
- 새 원격 OID나 checkpoint 게시 자체를 구현 완료 또는 사용자 결정으로 간주하지 않는다.

## Open Questions

- 없음
