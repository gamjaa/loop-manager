# [R-047] Immediate Remote Checkpoint After Managed Rebase

## Context

PR이 없는 원격 작업 브랜치에 게시된 checkpoint를 관리 rebase하면 로컬 commit OID가 바뀐다. 현재 게시 순서는 최종 전체 검증과 Review까지 원격 브랜치를 이전 OID에 둘 수 있다. G-100에서는 원격 checkpoint `990e0ba`를 새 base에 rebase한 로컬 결과 `811c437`이 게시되지 않은 채 다음 base 갱신을 위한 rebase가 시작됐다. 두 번째 rebase 중 소유권 검사가 이전 원격 OID를 이미 재작성된 로컬 OID의 조상으로 요구해 프로젝트를 차단했다.

rebase 후 원격과 로컬 OID가 다른 상태 자체는 정상이다. 그러나 이 상태를 여러 rebase와 재시작에 걸쳐 유지하면 게시 기준과 복구 계보가 길어진다. 소유 rebase가 안전하게 완료된 즉시 원격 작업 브랜치를 중간 checkpoint로 갱신하고, 검증·Review와 PR 판단은 그 이후 최종 후보에 대해 수행해야 한다.

## Requirement

Loop Manager가 이미 존재하는 동일 ExecPlan 소유의 원격 작업 브랜치를 관리 worktree에서 최신 원격 base에 rebase한 경우, PR이 없고 rebase 완료·작업물 소유권·원본 commit 재생 결과를 증명할 수 있으면 다음 Worker 작업, 전체 검증, Review 또는 추가 base rebase보다 먼저 rebase 결과 HEAD를 같은 원격 작업 브랜치에 중간 checkpoint로 게시한다. 이 게시에는 전체 검증과 Review 성공을 선행 조건으로 요구하지 않는다. 중간 checkpoint는 작업물 보존과 원격 기준 갱신일 뿐, 구현 완료·검증 통과·차단 해제 또는 PR 생성이 아니다.

기존 원격 OID와 게시할 OID가 다르고 fast-forward가 아니면, 마지막으로 채택한 정확한 원격 작업 브랜치 OID를 지정한 `--force-with-lease`만 사용한다. 게시 직전 실제 원격 ref와 PR 상태를 다시 확인한다. 게시가 확인된 뒤에만 새 원격 OID와 rebase 계보를 durable하게 기록한다. 원격 base가 그 사이 더 전진했더라도 안전하게 완료된 rebase 결과의 중간 게시를 먼저 처리하고, 다음 base에 대한 rebase는 별도 작업으로 시작한다.

이전에 게시하지 못한 rebase 결과가 남은 기존 상태도 저장된 관리 rebase 증거의 연쇄로 마지막 채택 원격 HEAD에서 현재 로컬 HEAD까지의 소유권을 검증한다. rebase 후 원격 HEAD가 로컬 HEAD의 Git 조상이 아니라는 이유만으로 소유권을 거부하지 않는다. 증거가 부족하거나 원격 branch가 예상 밖으로 바뀌면 기존 ref를 덮어쓰지 않고 작업물과 차단 이유를 보존한다.

## Acceptance Criteria

- PR 없는 동일 계획의 기존 원격 작업 브랜치에서 관리 rebase가 완료되고 journal, 원본 commit 재생, branch·worktree·HEAD와 작업 범위가 검증되면 전체 verify·Review 전에 결과 HEAD가 같은 원격 branch의 중간 checkpoint로 게시된다. 계획 실행 상태와 검증 결과는 게시만으로 성공 상태가 되지 않는다.
- 관리 rebase 중 충돌, detached HEAD, 미해결 Git operation 또는 소유권·작업 범위 불확실성이 있으면 push하지 않는다. 충돌을 기존 Worker 교정 경로로 해결하고 완료 증거를 확인한 뒤에만 게시한다.
- 원격 작업 HEAD가 로컬 rebase 결과의 조상이 아니어도 저장된 원격 시작 OID, 하나 이상의 관리 rebase 실행·완료 증거와 현재 로컬 HEAD가 끊김 없이 연결되면 소유권을 인정한다. 공통 base 조상, commit 메시지, clean worktree 또는 원격 OID 일치만으로 재생 증거를 대신하지 않는다.
- 같은 branch의 정확한 원격 OID를 게시 직전에 확인하고 그 OID에 결합된 fast-forward 또는 `--force-with-lease`만 수행한다. 원격 삭제·재작성·외부 후속 commit, PR 생성, branch protection 또는 권한 거부가 있으면 덮어쓰지 않고 원인과 마지막 안전 OID를 기록한다.
- push가 성공해 원격 HEAD가 바뀐 뒤 SQLite 기록 전에 중단되면 저장된 intent, 실제 원격 HEAD와 관리 Git 증거를 재확인해 한 번만 게시 완료로 확정한다. push 전 중단이나 일시적 실패에서는 같은 게시를 재개하고, 완료 또는 안전한 거부 전에는 다음 base rebase를 시작하지 않는다. 이미 같은 OID이면 빈 commit과 중복 push를 만들지 않는다.
- 중간 게시 뒤 base, Worker 변경 또는 후보 HEAD가 바뀌면 필요한 targeted test, 전체 verify와 Review를 최종 후보에 대해 다시 수행한다. PR은 기존 완료 기준을 모두 충족한 뒤에만 생성하며 중간 게시를 검증 결과나 PR head 승인으로 재사용하지 않는다.
- 기존 PR이 연결된 브랜치, 원격 작업 브랜치가 없는 신규 계획, 다른 계획 소유 branch에는 이 조기 게시 규칙을 적용하지 않는다. 기존 PR 복구·신규 branch 게시 경계와 기본 worktree 보존 규칙을 유지한다.
- 실제 Git 이력으로 게시 checkpoint → 첫 rebase → 중간 게시 → 추가 base 전진·두 번째 rebase, 첫 게시 전 중단·재시작, 충돌 교정, 원격 경쟁, lease 거부 및 push 후 SQLite 기록 전 중단을 검증한다. G-100처럼 이미 여러 rebase 사이에 게시가 누락된 상태는 증거가 충분할 때만 복구한다.

## Product Impact

- PR 없는 기존 원격 작업 브랜치는 안전하게 완료된 관리 rebase 직후 중간 checkpoint로 갱신된다.
- 검증 전에도 rebase 결과를 원격에서 확인하고 복구할 수 있으며, 최종 검증·Review와 PR 생성 조건은 그대로 적용된다.

## Non-Goals

- 미완료 rebase, 불확실한 소유권이나 검증하지 않은 원격 ref를 강제로 게시하지 않는다.
- 기존 PR head를 조기에 재작성하거나 기본 worktree에서 rebase하지 않는다.
- 중간 checkpoint를 검증 성공, 차단 해제, PR 생성 또는 Merge로 간주하지 않는다.

## Open Questions

- 없음
