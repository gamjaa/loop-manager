# [R-036] Rebase Before Full Verify

## Context

현재 실행 코드는 Worker 결과를 전체 verify한 뒤 최신 `origin/<base-branch>`를 가져와 rebase하고, 원격 기준이 바뀌었으면 전체 verify를 다시 실행한다. 같은 변경에 대해 rebase 전 검증은 최종 게시 후보를 검증하지 못하면서 시간과 자원을 사용한다. 제품 흐름과 설계 문서도 Worker 결과에 최신 원격 기준을 먼저 반영한 뒤 전체 검증을 실행하는 순서를 기술한다.

## Requirement

Loop Manager는 Worker의 구현 결과와 작업 범위를 확인한 뒤 최신 원격 기준을 가져와 필요한 commit과 rebase를 먼저 마쳐야 한다. rebase 충돌이 나면 기존 복구 계약에 따라 같은 Worker 세션으로 해결하고, Worker가 충돌 해결 과정에서 변경한 코드·테스트의 관련 targeted test를 확인한다. 충돌이 해결되지 않았거나 rebase가 실패한 상태에서는 전체 verify나 Reviewer를 시작하지 않는다.

rebase가 끝나고 게시 후보 revision이 확정된 뒤에만 해당 revision의 전체 verify를 실행한다. 기준이 안정적이고 검증 실패나 후속 변경이 없다면 한 Worker 결과에 대한 전체 verify는 한 번이면 된다. verify가 실패해 Worker가 수정하거나, verify 이후 원격 기준·게시 후보가 다시 바뀌면 기존 성공 결과를 재사용하지 않고 최종 revision에 대해 전체 verify와 Reviewer를 다시 수행한다. 따라서 모든 실행에서 verify가 반드시 한 번만 실행된다고 보장하지 않는다.

## Acceptance Criteria

- Worker 결과 이후 최신 `origin/<base-branch>`를 확인하고 필요한 rebase를 완료한 다음 전체 verify를 처음 실행한다. rebase 전 상태에 대한 선행 전체 verify는 실행하지 않는다.
- rebase 충돌은 기존 Worker 세션과 작업 공간에서 해결하며, 충돌 해결로 수정한 코드·테스트의 관련 targeted test를 확인한다. 충돌이 남은 상태에서는 전체 verify와 Reviewer를 시작하지 않는다.
- 원격 기준이 안정적이고 verify 실패·Worker 수정·게시 후보 변경이 없으면 같은 Worker 결과에 대한 전체 verify를 한 번만 실행한다.
- verify 이후 새로운 원격 commit, rebase, Worker 수정 또는 다른 게시 후보 변경이 생기면 이전 검증·Review 성공을 무효화하고 최종 revision에서 전체 verify와 Reviewer를 다시 수행한다.
- 전체 verify 실패와 Reviewer blocking issue의 기존 Worker 교정, 실패 지문·반복 차단 및 원문 보존 정책은 유지한다.
- 게시 직전 기준 확인과 최종 revision 검증을 생략하지 않으며, 실패하거나 검증되지 않은 변경으로 PR을 게시하지 않는다.

## Product Impact

- 안정적인 원격 기준에서는 최종 게시 후보만 전체 검증해 중복 실행을 줄인다.

## Non-Goals

- 원격 기준과 코드가 바뀌어도 verify를 항상 한 번만 실행하도록 강제하지 않는다.
- Worker의 관련 targeted test나 Reviewer의 읽기 전용 검토를 생략하지 않는다.
- rebase 충돌 복구 또는 PR 게시 안전 조건을 완화하지 않는다.

## Open Questions

- 없음
