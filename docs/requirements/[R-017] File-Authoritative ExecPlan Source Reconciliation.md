# [R-017] File-Authoritative ExecPlan Source Reconciliation

## Context

`docs/ARCHITECTURE.md`는 PRODUCT, Requirement, ARCHITECTURE와 ExecPlan 내용을 프로젝트 Git 파일의 원본으로, ExecPlan 실행 상태와 세션·작업 공간·PR 연결을 SQLite의 원본으로 정의한다. 그러나 현재 Requirement archive eligibility는 ExecPlan이 처음 등록될 때 SQLite에 복사한 `sources`와 최신 원격 ExecPlan 파일의 `sources`가 다르면 파일이 유효해도 source 관계를 확정할 수 없다고 판단한다.

2026-09-20에는 READY 상태인 G-031의 `sources`가 원격 파일에서 유효하게 변경됐지만 SQLite에는 최초 값이 남았다. 이후 독립적인 G-035를 시작할 때 전체 catalog의 불일치를 발견해 G-035를 `BLOCKED`로 만들었고, G-035의 해제 fingerprint에는 원인인 G-031이 포함되지 않아 G-031을 바로잡아도 자동으로 복구할 수 없는 상태가 됐다.

ExecPlan의 `sources`는 요구사항 보완, 계획 범위 정정과 안전한 계획 개정에 따라 실행 전이나 실행 수명주기 중 변경될 수 있다. 유효한 원격 파일과 SQLite cache가 다를 때 SQLite의 과거 값을 우선하면 프로젝트 문서가 source of truth라는 계약을 위반하고 무관한 작업을 차단한다.

## Requirement

Loop Manager는 최신으로 검증된 `origin/<base-branch>`의 ExecPlan 파일을 `sources`의 원본으로 사용해야 한다. SQLite의 `sources`, 현재 content hash와 현재 plan revision SHA는 조회와 복구를 위한 동기화 cache이며, 유효한 원격 파일과 다르면 파일 값으로 원자적으로 갱신해야 한다.

동기화 전에 프로그램은 원격 revision을 고정하고 전체 ExecPlan catalog의 ID, 경로, 제목, source 경로 형식, source 존재 여부, dependency 존재와 순환 부재를 코드로 검증해야 한다. 검증 중 원격 base가 바뀌면 일부 행만 갱신하지 않고 최신 revision에서 다시 시작해야 한다. 검증과 SQLite transaction 사이에 대상 행의 상태나 소유권이 바뀌면 stale 결과를 적용하지 않아야 한다.

유효한 `sources` 불일치는 오류나 BLOCKED 사유가 아니다. 프로그램은 다음 Worker 선택, Requirement archive eligibility 계산, BLOCKED 재평가와 게시 직전 재검증처럼 source 관계를 사용하는 경계 전에 SQLite cache를 최신 파일과 동기화하고 같은 실행에서 갱신된 관계로 판단을 계속해야 한다. source가 추가되거나 제거되면 관련 Requirement의 archive 후보를 다시 계산하며 이전 관계에서 만든 archive 후보, 입력 fingerprint와 검증 결과를 재사용하지 않아야 한다.

IMPLEMENTING, VERIFYING, REVIEWING 또는 PUBLISHING 상태의 계획에서 `sources`가 바뀌면 현재 branch, worktree와 Agent 세션을 삭제하지 않는다. 새 source 관계를 저장한 뒤 이전 source 집합에 의존한 Worker 입력, archive 후보와 검증 결과를 무효화하고, 다음 Agent 또는 게시 단계 전에 최신 원격 계획과 source 문서를 관리 worktree에 반영해 같은 계획의 안전한 단계에서 재개해야 한다. 파일 변경 자체로 실행 상태를 MERGED로 추정하거나 다른 계획의 상태를 변경하지 않는다.

최초 계획 게시 commit과 최초 경로는 기존 PR의 불변 링크와 감사 이력을 위해 별도로 보존해야 한다. SQLite의 최신 source cache, 최신 content hash와 현재 plan revision SHA를 갱신해도 최초 게시 SHA를 덮어쓰지 않아야 한다. 완료된 ExecPlan을 archive할 때는 마지막으로 검증·동기화된 원격 파일의 hash와 실제 이동 내용을 비교하고, 최초 게시 hash를 최종 파일 hash로 오인하지 않아야 한다.

원격 ExecPlan 파일이 없거나 형식이 잘못됐거나 ID·경로·dependency graph·source 존재를 검증할 수 없는 경우에는 SQLite를 파일에 맞춰 부분 갱신하지 않는다. 오류는 우연히 실행 중이던 다른 ExecPlan의 계획 내부 BLOCKED로 기록하지 않고, 실제 문제 파일과 원격 revision을 포함한 catalog 또는 프로젝트 범위 오류로 저장해야 한다. 오류 fingerprint에는 실제 원인인 ExecPlan 파일과 필요한 source가 포함돼 해당 파일을 고치면 자동으로 재검증할 수 있어야 한다.

이 Requirement는 사용자가 신뢰된 원격 base에 게시한 유효한 ExecPlan 변경을 동기화하는 규칙이다. Planner, Worker와 Reviewer가 기존 역할 범위를 넘어 ExecPlan을 변경할 권한을 새로 부여하지 않으며, dirty 기본 worktree나 push되지 않은 로컬 파일을 자동 입력으로 사용하지 않는다.

## Acceptance Criteria

- 최신 원격의 유효한 READY ExecPlan `sources`가 SQLite 값과 다르면 Worker 선택 전에 파일 값, 현재 content hash와 현재 plan revision SHA가 하나의 transaction으로 갱신된다.
- source cache 갱신은 ExecPlan의 READY, 실행 중, PR_OPEN, BLOCKED 또는 MERGED 실행 상태를 파일 내용만으로 변경하지 않는다.
- source 추가와 제거 후 Requirement archive eligibility가 같은 실행에서 새 관계로 다시 계산되며 이전 후보를 사용하지 않는다.
- 한 READY 계획의 유효한 source 변경 때문에 독립적인 현재 계획을 BLOCKED로 만들지 않는다.
- G-031의 원격 `sources`가 최초 SQLite 값과 달라도 G-031 cache를 갱신한 뒤 독립적인 G-035의 archive eligibility와 실행을 계속하는 회귀 시나리오를 검증한다.
- 실행 중인 계획의 source가 바뀌면 branch, worktree와 재개 가능한 Agent 세션은 보존하고, 이전 source 관계에서 만든 Worker 입력, archive 후보, verify와 Review 결과는 다음 변경 단계 전에 무효화된다.
- 최초 게시 SHA와 경로는 불변 링크 및 감사용으로 유지되고, 최신 source cache·content hash·plan revision SHA는 별도 현재 값으로 갱신된다.
- 완료 계획 archive는 마지막으로 동기화된 원격 파일 hash를 사용하며 최초 게시 hash가 다르다는 이유만으로 안전한 최종 파일 이동을 거부하지 않는다.
- 원격 base가 동기화 도중 바뀌거나 SQLite 행의 상태·소유권 snapshot이 달라지면 부분 갱신 없이 최신 상태에서 재시도한다.
- push와 SQLite 갱신 사이에 프로세스가 중단돼도 다음 실행이 원격 파일을 다시 읽어 같은 cache 갱신을 중복 부작용 없이 완료한다.
- 파일 누락, 잘못된 frontmatter, 존재하지 않는 source, ID·경로 불일치와 dependency 오류에서는 SQLite cache를 갱신하지 않고 실제 원인 파일을 가리키는 복구 가능한 catalog 또는 프로젝트 오류를 기록한다.
- 유효하지 않은 파일 때문에 다른 ExecPlan을 계획 내부 BLOCKED로 표시하지 않으며, 원인 파일을 수정해 push하면 저장된 오류 fingerprint가 바뀌어 자동 재검증된다.
- dirty 기본 worktree와 push되지 않은 로컬 변경은 source cache 갱신에 사용하지 않는다.
- 유효한 source 변경, source 추가·제거에 따른 archive 후보 변화, 실행 중 변경, 원격 경쟁, stale SQLite snapshot, 중단 복구, 잘못된 파일과 G-031/G-035 회귀를 자동 테스트로 검증한다.

## Product Impact

- ExecPlan 파일의 `sources`가 SQLite cache보다 우선한다.
- 유효한 계획 개정은 수동 DB 수정 없이 다음 실행에서 자동으로 동기화된다.
- source 관계 불일치는 무관한 ExecPlan을 BLOCKED로 만들지 않는다.
- 실행 상태와 외부 PR 상태는 계속 SQLite와 GitHub가 각각 소유한다.

## Non-Goals

- ExecPlan 파일 내용으로 READY, MERGED 또는 다른 실행 상태를 추정하지 않는다.
- invalid ExecPlan이나 존재하지 않는 source를 SQLite에 그대로 복사하지 않는다.
- dirty 기본 worktree, 로컬 전용 commit 또는 push되지 않은 파일을 원격 source of truth보다 우선하지 않는다.
- source 동기화를 Planner, Worker 또는 Reviewer의 파일 변경 권한 확대로 사용하지 않는다.
- 최초 게시 commit과 기존 PR의 불변 계획 링크를 최신 revision으로 바꾸지 않는다.
- source 변경과 무관한 SQLite 실행 이력, 실패 횟수, 세션, branch, worktree와 PR 연결을 초기화하지 않는다.

## Open Questions

- 없음
