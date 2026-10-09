# [R-010] Origin-Isolated Local Worktree Operation

## Context

기본 checkout의 사용자 작업과 자동 계획·구현·프로그램 revision을 분리한다. pinned runtime과 비차단 로컬 동기화는 이미 구현돼 있으므로 전환 전 임시 updater나 과거 FF-only 제한을 다시 구현하지 않는다. 로컬 동기화는 R-056, 공통 Git 오탐 제거는 R-055, runtime 귀속·handoff는 R-020·R-023을 따른다.

## Requirement

### 원격 입력과 사용자 checkout

계획·등록·catalog와 관리 worktree의 시작 기준은 검증한 `origin/<base-branch>`의 immutable OID다. 기본 checkout의 dirty·staged·untracked·ahead·behind·diverged·다른 branch·detached 상태와 로컬 전용 commit은 자동 입력이나 시작·복구 차단 조건이 아니다.

기본 checkout 동기화는 R-056의 부수 작업이다. dirty 상태에서는 로컬 변경을 보존할 수 있는 FF-only만 시도하며 rebase하지 않는다. clean diverged는 소유권과 commit 보존이 확인된 안전한 rebase를 시도할 수 있다. 사용자 operation·경로 충돌·동시 변경·lock·실패는 로컬 상태를 보존하고 동기화만 보류한다. 이 조건 밖에서 reset·clean·stash·강제 checkout·자동 push를 수행하지 않는다.

기본 checkout의 파일·index·HEAD·로컬 base ref는 legacy 실행에도 Agent scope 비교에서 제외한다. 선택 관리 worktree·소유 branch·origin·보안 설정과 실제 동시 writer는 계속 검증한다. 외부 fetch·무관한 Git 변화의 재관측과 BLOCKED 수동 입력 수용은 R-055를 따른다.

### 실행 revision과 운영 입력

run 스크립트는 현재 checkout의 Python 부트스트랩에 인자를 전달한다. 부트스트랩은 다른 Git 변경보다 먼저 process lock을 획득하고, R-023에 따라 등록 self base(미등록이면 기존 기본 branch)의 최신 원격 bootstrap·runtime을 선택한다. source HEAD·동기화 성공을 실행 조건으로 삼지 않는다.

runtime은 같은 repository의 object database를 공유하는 소유권이 검증된 clean detached exact-SHA worktree다. 부모는 자식 종료까지 lock을 유지한다. 외부 fetch로 추적 ref가 바뀌어도 실행 중 revision과 파일을 바꾸지 않고 다음 실행에서 원격을 다시 확인한다. 예상 밖 tracked 변경·operation·소유권 오류는 덮어쓰기 없이 해당 runtime 준비를 보류한다.

source의 기존 `config/config.ini` 및 명시적 상대 `--config`는 source 기준 절대 경로로 전달하고 시작 시 한 번 읽는다. source 파일을 복사·이동·symlink하거나 runtime 경로로 다시 계산하지 않는다. launcher가 선택한 interpreter를 자식에도 사용하며 ignored 파일을 일괄 복제하지 않는다. 표준 Python bytecode 캐시는 R-059를 따른다.

### 역할과 등록

Agent의 직접 commit·fetch·pull·rebase·push·worktree 등록·PR 생성 권한은 추가하지 않는다. 단계별 Git 동기화는 Manager의 기존 경계에서 수행하고 Reviewer는 읽기 전용이다. 등록 시 필수 문서·파일 내용과 PLANS 계약은 로컬 파일이 아닌 원격 tree/blob으로 검사한다. 등록 전용 검사를 실행마다 로컬 checkout에서 반복하지 않는다. 실행 지침은 선택 runtime의 공통·역할별 원본을 사용한다.

## Acceptance Criteria

- run 스크립트의 인자 전달, Git 변경 전 process lock 및 자식 종료까지의 잠금 보유가 유지된다.
- 원격 exact revision·repository·origin·runtime 소유권을 확인하고 로컬 코드·문서·commit은 실행 입력에서 제외한다. 별도 clone을 만들지 않는다.
- config의 source 기준 절대 경로·interpreter 재사용과 ignored 파일 일괄 복제 금지가 유지된다.
- 기본 checkout의 모든 로컬 상태에서 원격 기준 계획·구현·PR 관측은 계속되며 동기화는 R-056의 dirty FF-only·clean rebase·실패 보존 기준을 따른다.
- 외부 fetch·사용자 기본 checkout 변경은 legacy 실행에도 단독 scope 차단 사유가 아니며 실제 선택 자원 위반·origin 변조·동시 writer는 거부한다.
- 실행 중 runtime revision은 고정되고 다음 실행만 최신 원격을 선택한다. runtime 상태 불일치는 reset·clean·강제 재등록으로 해결하지 않는다.
- 원격 tree/blob으로 등록을 검증하고 local-only 문서가 등록 결과·Planner fingerprint·관리 작업 입력을 바꾸지 않는다.
- pinned runtime 전의 임시 source updater와 그 종료·오류 gate를 새 작업으로 계획하지 않는다.

## Product Impact

- 사용자 기본 checkout과 원격 기준 자동 작업·프로그램 실행을 분리한다.
- 부수적인 로컬 동기화 실패는 독립 관리 작업을 멈추지 않는다.

## Non-Goals

- local-only 코드·문서·commit의 자동 입력 채택·게시, 사용자 변경 삭제·stash와 다른 branch 강제 전환.
- Agent Git 쓰기 권한 확대, 실행 중 runtime 전환과 별도 clone·복구 infrastructure 추가.
- 다른 프로세스의 Git lock 제거·우회.

## Open Questions

- 없음
