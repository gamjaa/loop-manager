# [R-045] Phase-Bound Managed Rebase Validation and Recovery

## Context

2026-09-27에 G-085는 최신 원격 base를 반영하는 관리 rebase 중 `scripts/verify.sh` 충돌을 만났다. Worker가 양쪽 검증 대상을 보존해 충돌을 해결했고, Manager가 stage와 rebase 계속을 수행했다. Git은 계획 보강 commit을 포함한 7개 commit을 재생해 소유 branch로 복귀했으며 작업 폴더는 clean했다. 충돌 해결 직후 저장한 tree는 그 해결을 반영한 첫 commit의 tree와 같았고 Manager의 rebase 완료 실행 표식도 일치했다.

그러나 완료 검사는 충돌 해결 때 승인한 파일 내용이 후속 commit까지 재생한 최종 작업 폴더에서도 같아야 한다고 요구했다. 뒤의 기존 commit들이 같은 파일을 수정하면서 이 조건이 실패했고, 이미 완료된 Git 작업을 SQLite의 완료 상태로 반영하지 못했다. `Manager rebase 결과의 HEAD·ref·index·등록 상태가 다릅니다.` 오류가 반복됐으며 실제 발견·계획 비교 보완 작업에는 도달하지 못했다.

R-023과 PRODUCT는 Manager 소유 Git 전이를 증명하고 중단 후 같은 작업을 복구하도록 요구한다. 이 Requirement는 그 안전 기준을 유지하면서, 충돌 해결 시점의 증거와 후속 commit 재생 및 최종 상태의 증거를 구분하지 못하는 결함을 보완한다.

## Requirement

Loop Manager는 관리 rebase의 충돌 해결, 후속 commit 재생과 전체 완료를 각 단계에 맞는 증거로 검증해야 한다. 승인된 충돌 해결이 해당 commit에 정확히 반영되고 남은 commit들이 소유 작업으로 올바르게 재생됐다면, 후속 commit이 같은 파일을 다시 변경했다는 이유만으로 완료를 거부하지 않는다.

Git 작업이 이미 끝났지만 완료 기록이 확정되지 않은 경우, 저장된 실행·승인 증거와 실제 Git 상태를 재검증해 동일 작업의 완료를 복구한다. 소유권이나 재생 결과를 증명할 수 없거나 예상 밖 변경이 남으면 작업물·세션·감사를 보존하고 기존 안전 차단을 유지한다. 재시작 자체로 Git 작업이나 완료된 Worker 충돌 교정을 반복하지 않는다.

## Acceptance Criteria

- 충돌 해결 뒤 후속 commit이 같은 파일의 내용이나 mode를 변경하는 정상 rebase가 완료된다. 승인된 해결이 반영된 commit과 최종 HEAD를 구분해 검증하고, 충돌 해결 당시 파일 해시를 최종 작업 폴더에 다시 요구하지 않는다.
- 충돌이 중간 commit에서 발생하거나 여러 번 발생해도 각 해결을 해당 원본 commit과 재생 결과에 연결한다. 이전 해결이 확인됐다는 사실로 이후 미검증 해결을 승인하지 않는다.
- 남은 commit의 누락·순서 변경·예상 밖 추가·변조와 다른 작업의 해결 결과를 탐지한다. 기존 정책이 허용한 무변경·skip은 해당 원본 commit의 처리 근거가 확인될 때만 인정하며, 임의의 skip이나 새로운 자동 이력 변경 정책을 추가하지 않는다.
- 실제 저장·승인된 충돌 해결을 변경하거나 stage에 범위 밖 파일·blob·mode·index flag 변경을 섞으면 거부한다. 정상 후속 commit의 변경과 Worker 승인 뒤의 예상 밖 변경을 구분한다.
- 원격 branch OID 또는 PR head, repository·plan·branch·worktree·실행 identity, target base, 관련 Git 상태가 맞지 않거나 실행 증거가 불완전하면 기존 안전 차단을 유지한다. 최종 HEAD·branch ref·등록 상태와 target base 관계도 확인한다.
- 충돌 없이 완료되는 rebase, 이미 target base를 포함해 변화가 없는 소유 작업, PR 없는 게시 checkpoint branch와 기존 PR 충돌 복구에 같은 단계별 검증 기준을 적용한다. rebase를 수행하지 않은 일반 재개에 rebase 실행 증거를 요구하지 않는다.
- Worker 결과 저장·검증·stage 전후, 충돌 해결 commit 생성 뒤 후속 commit 재생 전후, Git 완료 뒤 journal 확정 전, journal 확정 뒤 SQLite 반영 전의 중단에서 기존 작업을 복구한다. SQLite와 Git을 다시 대조해 이미 수행된 단계는 중복 실행하지 않는다.
- 완료 증거가 확인되면 rebase 실행 checkpoint와 계획 실행 상태·프로젝트 투영을 일관되게 갱신한다. 저장된 세션·피드백·승인·게시 OID 감사는 보존하고, 후보 revision이 달라졌으면 필요한 Worker 확인·targeted test·전체 verify·Review를 다시 수행한 뒤에만 게시한다.
- G-085처럼 구 형식의 intent에 승인된 staged tree와 실행 표식이 남아 있으면, 해당 tree를 실제 해결 commit에 유일하게 대응시키고 나머지 재생 및 현재 소유권까지 증명한 경우에만 완료를 복구한다. HEAD가 최신 base 위에 있고 clean하다는 사실만으로 소유권을 채택하거나 과거 증거를 만들어내지 않는다.
- 이 결함으로 저장된 재시도·계획 또는 프로젝트 차단은 원인별로 재평가한다. 단계별 증거가 모두 확인되고 별도 유효한 원인이 없을 때만 해당 오류를 해제하며, 실제 Review 실패·사용자 결정·다른 계획·planning 상태·실패 및 알림 감사는 보존한다.
- 진단은 충돌 해결 증거, 후속 재생 근거, 최종 소유권 중 어느 단계의 어떤 기대값과 관측값이 불일치했는지 식별한다. 같은 원인의 반복 관측은 기존 실패·알림 사건 규칙을 유지한다.
- 자동 검증은 G-085의 충돌 해결 및 뒤의 여러 commit에 의한 동일 파일 변경을 실제 Git 이력으로 재현하고, 재시작을 포함해 Worker 교정·전체 verify·Review 경로로 진행하는지 확인한다. 잘못된 staged tree, 변조된 후속 commit, 원격 OID 변경, 불완전한 legacy 증거와 다른 세션의 증거는 거부한다. validator 단독 통과나 commit 개수 확인만으로 완료를 판정하지 않는다.

## Product Impact

- 소유 rebase의 정상적인 후속 commit 변경 때문에 자동 작업이 멈추지 않으며, 이미 완료된 Git 작업은 증거를 확인한 뒤 같은 실행 문맥에서 복구된다.
- 충돌 해결의 승인 범위와 실제 commit 반영, 나머지 재생 결과와 최종 게시 소유권은 각각 검증되며, 예상 밖 변경과 소유권 불명확 상태의 안전 차단은 유지된다.
- 기본 worktree 보존, Manager의 Git 쓰기 소유권, Agent 격리, 최종 검증·Review와 원격 게시 lease 조건은 유지된다.

## Non-Goals

- G-085의 발견·계획 비교 출력 한도 기능 자체를 이 Requirement에서 구현하지 않는다.
- Agent 쓰기 격리나 무관한 shared ref·외부 worktree 감시를 완화하지 않는다. 별도 R-023의 기준선·detached HEAD 복구 범위를 포괄 승인하지 않는다.
- 새 Git 쓰기 주체, 일반 강제 push, source checkout rebase, 자동 Merge, 새로운 rebase 방식이나 임의의 commit 삭제·skip 정책을 추가하지 않는다.
- 과거 intent·checkpoint·승인·실패 감사를 삭제하거나 증거가 부족한 작업을 일괄 해제하지 않는다. SQLite의 비호환 migration과 새 외부 infrastructure를 도입하지 않는다.

## Open Questions

- 없음
