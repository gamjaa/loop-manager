# [R-081] 게시 checkpoint 재개 소유권의 보존과 재검증

## Context

게시 checkpoint의 `publication_remote_oid`가 남아 있어도 실행 checkpoint의 인정 가능한 재개 증명이 사라지면, 같은 소유 작업의 정상 재개가 `publication_ownership_uncertain`으로 프로젝트 전체 차단에 이른다. 게시 OID와 재개 증명의 수명을 맞추고, 증명이 없는 기존 상태도 현재 소유권을 안전하게 재검증할 수 있어야 한다.

제공된 G-179 조사에서는 2026-10-09 02:13:53 UTC Worker의 targeted unit test 실패 뒤 보존 커밋 `8fdbae480968214d0563027a066d3498c381417d`가 게시됐고, 02:22:43 UTC 게시 소유권 오류로 프로젝트가 차단됐다. 복구 전 인정 가능한 재개 표식은 없었지만 계획의 게시 OID는 유지됐다. 복구 시 로컬·원격 HEAD와 기존 게시 이력의 소유권을 검증한 뒤 기존 복구 경로로 재개했다. 어느 전이가 최초로 표식을 없앴는지는 미확정이다.

작성 시 현재 checkout에서도 다음 조건을 확인했다.

- [게시 소유권 판정](../../loop_manager_internal/execution/execution_ownership_handler.py)은 게시 checkpoint가 있고 재개 증명도 없지만 `publication_remote_oid`가 있는 경우, 현재 Git 소유권 검증에 도달하지 않고 거부한다.
- [계획 차단 전이](../../loop_manager_internal/storage/runtime_state.py)는 `blocked_resume`을 제거하며, 재활성화는 계획의 게시 OID를 유지한다.
- [실행 workspace 재활성화](../../loop_manager_internal/execution/execution_workspace_handler.py)는 유효한 `current_rebase_acceptance`가 없으면 snapshot 대부분을 초기화하고 일부 표식만 보존한다. 이 조건만으로 G-179의 최초 유실 전이를 확정하지 않는다.

운영 증거 위치는 `/home/ubuntu/.local/state/loop-manager/manual-reconciliations/g179-current-state-20261009T025736Z/`의 `before.db`, `proof.json`, `result.json`이다. 이 문서 작성에서는 해당 파일을 직접 열람하지 않았다. 제공된 복구 기록의 `blocked_checkpoints`는 이미 `unpublished/local_only`였고 원래 게시 증명은 `publication_history`에 남아 있었다. 메모리 DB 복사본으로 기존 검증기를 사용한 복구는 운영 게시 영수증을 덮어쓰지 않았다.

[R-055](%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md)와 [G-189](../plans/%5BG-189%5D%20차단과%20재개의%20전체%20경로%20근거%20감사.md)는 전체 경로 감사다. [G-199](../plans/%5BG-199%5D%20격리%20증거%20없는%20legacy%20기본%20checkout%20비교%20경계.md)는 별도의 legacy 격리 정책 결정이다. 과거 [G-103](../plans/archive/%5BG-103%5D%20Published%20Checkpoint%20Review%20Reactivation%20Ownership.md)은 Review 횟수 차단의 재활성화를 다뤘다. 이 요구사항은 그 경로와 검증을 참고하되, 이번 Worker 실패 이후의 게시·차단·원인 해제·재활성화 결함에 대한 전용 재현·수정·회귀 기준을 정의한다.

## Requirement

### 게시와 재개 증명의 연속성

Worker 실패 → 보존 checkpoint 게시 → 차단 및 checkpoint 요청 갱신 → 실제 원인 해제 → 재활성화 → 실행 판정에서, 계획의 게시 OID·게시 이력과 실행 checkpoint의 재개 증명이 함께 유효하게 이어져야 한다. 재차단과 각 저장 경계의 중단·재시작도 포함한다.

관련 ExecPlan에는 각 전이가 읽고 보존·무효화하는 게시 근거와 재개 증명, 유효성을 판단하는 소비자 및 이를 확인하는 테스트를 연결한다. 실제 handler와 SQLite로 확인한 거부 분기와 증명을 유실시키는 전이를 구분한다. G-179의 최초 유실 전이는 운영 증거로 확인된 경우에만 사건 원인으로 기록하며, 확인할 수 없으면 그 한계를 남긴다. 사건의 최초 유실 전이가 미확정이어도 재현된 결함의 수정·회귀 검증은 진행한다.

전이에서는 여전히 유효한 소유권 증명을 보존하거나, 현재 상태로 재검증한 증명을 저장해 다음 실행 판정으로 전달한다. 낡은 표식을 무조건 유지하지 않으며, 표식 부재와 게시 OID 존재의 조합만으로 소유권을 거부하지 않는다. 재개에 필요한 증명과 계획·프로젝트 상태의 확정은 같은 SQLite transaction 및 기존 generation·소유자 검사를 통해 일관되게 반영한다.

### 현재 상태로 안전한 재검증

인정 가능한 재개 증명이 없는 기존 상태는 기존 소유권 검증을 재사용해 다음 조건을 함께 확인한 뒤에만 수용한다.

- 동일 repository·계획·소유 branch·canonical 관리 worktree와 예약·등록 상태.
- 게시 요청과 commit의 identity·tree, 게시 이력 및 마지막으로 채택한 원격 OID의 귀속. 다른 요청·계획의 기록을 같은 OID라는 이유로 채택하지 않는다.
- 현재 로컬 HEAD·branch ref·index·작업 파일·Git operation과 허용 변경 범위. 진행 중인 rebase와 완료된 rebase는 기존 현재 상태 판정으로 구분한다.
- 실제 origin의 정확한 ref·OID에 결합된 원격 lease와 현재 PR의 존재·identity·Open 상태·head. PR 부재도 조회로 확인하며, 관측 실패를 부재로 처리하지 않는다.
- 현재 차단 원인, 실행 자리·dependency·승인과 저장 generation. 확정 전에 상태를 다시 확인하고 관측 변화나 저장 경쟁이 있으면 기존 대기·재평가 경계에서 보존한다.

`publication_remote_oid`를 지워 검사를 우회하거나 로컬·원격 OID 일치만으로 허용하지 않는다. 기존 증명이 유효한 경로와 증명이 없는 경로가 같은 소유권·원격 lease·PR 안전 기준을 적용해야 하며, 기존의 유효한 rebase·Review 재개 경로도 유지한다. 수행하지 않은 rebase 완료나 Agent 격리·범위 검증 성공을 합성하지 않는다.

현재 checkpoint 요청이 `unpublished/local_only`여도 과거 게시 이력이 충분하면 그 역사 기록을 현재 소유 상태와 함께 검증할 수 있어야 한다. 현재 요청을 과거 게시 성공으로 덮거나 `publication_history`·원본 영수증을 다시 쓰지 않는다. 게시 귀속을 확정할 수 없으면 미게시 이유·작업물·원래 차단과 감사를 보존한다.

### 재개와 실패 시 보존

소유권 오탐으로 생긴 차단은 같은 원인을 현재 근거로 재검증한 경우에만 해제한다. 게시 소유권 확인은 Worker의 test 실패 해결이나 구현·verify·Review 성공을 대신하지 않는다. 원래 실패가 남으면 기존 교정·차단 정책을 유지하고, 실제 원인이 해소되면 같은 소유 branch·worktree에서 기존 단계로 이어간다.

실제 Agent 위반, 별도 계획·프로젝트·planning 차단, 사용자 결정·승인·PR reopen 대기는 해제하지 않는다. 원본 snapshot·작업물·session과 미해결 피드백·실패·승격·게시·알림 감사를 기존 정책에 따라 보존한다. 후보나 구현 입력이 바뀌면 기존 Worker 확인·targeted test·Manager verify·Review·최종 게시 gate를 다시 통과한다.

현재 원격 변경·삭제·재작성, 소유권 불일치나 예상 밖 PR 상태는 같은 checkpoint의 단순 재개로 수용하지 않는다. 기존의 검증된 원격 변경·rebase 복구가 적용되는 경우에는 그 경로로 처리하며, lease를 새로 추정해 원격을 덮지 않는다. 일시적인 Git·DB·PR 관측 실패와 경쟁은 성공이나 영구적인 소유권 위반으로 바꾸지 않고 기존 제한 재시도·대기로 보존한다.

중단·재시작은 저장된 의도와 현재 상태를 다시 대조해 완료된 채택을 멱등하게 반영한다. 증명 저장과 원인 해제 중 일부만 확정하거나, 같은 OID에 빈 commit·반복 push·새 PR·중복 차단 알림을 만들지 않는다.

## Acceptance Criteria

- 게시 checkpoint가 있고 계획의 `publication_remote_oid`와 현재 원격 OID가 같으며 인정 가능한 재개 표식은 없는 최소 사례를 실제 handler·SQLite로 재현한다. 수정 전에는 Git 소유권 검사 없이 거부되는 경로를 확인하고, 수정 후에는 같은 사례가 기존 소유권 검사를 실제로 거쳐 유효한 경우에만 정상 재개한다.
- Worker 실패부터 게시·차단/요청 갱신·원인 해제·재활성화·실행 판정까지 게시 OID와 증명의 수명 및 테스트 대응을 직접 수정 ExecPlan에서 확인할 수 있다. 재현된 코드 결함과 G-179 사건의 미확정 최초 유실 전이를 구분하며, 다른 branch fetch/push의 범위 오탐이나 rebase 증명 유실만으로 원인을 단정하지 않는다.
- 유효한 증명 보존, 증명 없는 상태의 현재 재검증, 재차단 뒤 재활성화와 재시작 직후의 실행 판정이 같은 안전 기준을 통과한다. 새 handler·DB 연결로 재개해도 다시 오탐 차단되거나 메모리 상태에만 의존하지 않는다.
- `unpublished/local_only` 현재 요청과 충분한 `publication_history`만 남은 사례는 현재 소유권을 확인해 필요한 복구를 이어간다. 증거가 부족한 사례는 보존·거부하며, 두 경우 모두 현재 요청·운영 게시 영수증·과거 게시 감사를 덮어쓰지 않는다.
- 증명 저장·원인 해제·재활성화 확정 전후 중단과 generation·원인·소유자 경쟁에서 부분 확정이 없고, 재시작은 같은 증거를 다시 확인한다. 정상 재개에 불필요한 commit·push·PR 생성과 중복 알림이 없다.
- 다음 거부·보존 조건을 최소 unit case로 확인한다. 원격 OID 변경·삭제·재작성과 관측 사이 경쟁, 계획·branch·worktree·요청 identity 또는 tree 불일치, 예상 밖 PR 출현·종료·head 변경, 낡거나 변조된 재개 증명, 실제 Agent 범위 위반은 단순 재개로 수용하지 않는다. 다른 차단·승인·미해결 Worker 실패와 원본 작업물·감사는 유지된다.
- Git·DB·PR 관측 오류는 기존 제한 재시도·대기로 처리되며 성공 표식이나 불필요한 최종 소유권 차단을 만들지 않는다. 원격 안전 검사를 생략하거나 실패를 PR 부재·OID 일치로 바꾸지 않는다.
- 테스트는 Git·PR 관측 경계만 mock으로 제공하고 실제 handler·소유권 검증·SQLite 전이를 실행한다. 판정 함수를 성공값으로 대체하지 않는다. 변경 동작의 최소 unit test와 기존 rebase·Review 재개 회귀를 통과하고, Manager 전체 verify·읽기 전용 Review에서 범위·lease·독립 차단 보존을 확인한다. 실제 Git 실행은 [자체 테스트의 Git 경계](../ARCHITECTURE.md#자체-테스트의-git-경계)에 따라 필요한 효과와 assertion이 있는 경우에 한정한다.

## Product Impact

같은 소유 작업의 게시 OID와 유효한 소유권 근거가 차단·재활성화·재시작을 넘어 이어진다. 재개 표식이 없는 기존 상태도 현재 근거로 안전하게 복구하며, 실제 소유권·범위·원격 lease·PR 위반과 독립 차단은 유지한다. 소유권 수용은 구현·검증 완료를 대신하지 않는다.

기존 차단·복구 계약은 [PRODUCT](../PRODUCT.md#계획-및-프로젝트-차단-해제)와 [ARCHITECTURE](../ARCHITECTURE.md#차단-작업의-원격-반영과-보존)를 따른다. 변경 뒤 계속 유지할 규칙은 구현 PR에서 해당 소유 절에 반영한다.

## Non-Goals

- 운영 G-179 DB의 재수정, 수동 복구 재실행, 게시 영수증 조작과 전체 계획의 OID·표식 일괄 초기화.
- R-055/G-189 전체 감사의 대체, G-199의 legacy 격리 정책 결정이나 다른 branch fetch/push의 범위 정책 변경.
- 새로운 복구 CLI·설정·범용 소유권 계층 추가와 소유권·Agent 범위·원격 lease·PR 검사의 완화.
- 보존 checkpoint를 최종 게시 승인·검증 성공으로 취급하거나 기존 Worker·verify·Review를 우회하는 변경.

## Open Questions

- 없음. 최초 표식 유실 전이의 미확정은 조사 범위이며, 안전한 재개와 거부 정책의 사용자 결정 대기는 아니다.
