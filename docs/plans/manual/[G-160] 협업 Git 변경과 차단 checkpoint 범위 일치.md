---
id: G-160
title: 협업 Git 변경과 차단 checkpoint 범위 일치
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
  - docs/requirements/[R-056] Non-Blocking Local Base Synchronization.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

사용자가 요청한 일반 PR 작업이다. 실행 시 검증된 Git 자원 범위를 차단 checkpoint 검사와 재시작에도 일관되게 적용해 무관한 사용자 branch 변경에 따른 과도한 차단을 방지한다. 자동 Planner 입력과 분리해 manual 계획으로 기록한다.

## Acceptance Criteria

- 검증된 원격 base를 사용하는 실행은 로컬 source 동기화 실패·skip으로 중단되지 않는다. 사용자 변경을 보존한다.
- 현재 호출의 격리·소유권 증거가 검증된 경우만 무관한 B의 ref·등록·branch 설정 변화를 checkpoint 비교에서 제외한다. 실제 A·공유 설정 위반 및 증거 없는 과거 실행은 계속 거부한다.
- 사용자 결정 대기 작업물을 기존 checkpoint 경계로 게시하고, Manager commit 뒤에도 실행 격리 증거를 유지한다. 일시 push 실패는 재시작 후 같은 작업물로 재시도하며 결정 대기는 유지한다.
- B의 상태는 B를 사용할 때 다시 검증한다. 차단 해제·PR 생성·원격 덮어쓰기 권한을 확대하지 않는다.

## Implementation

1. source 동기화, 실행 scope, 차단 복구, checkpoint 검사·게시 및 dispatcher 호출 경로를 조사한다.
2. 기존 invocation 소유권 검사와 자원 projection을 checkpoint에서도 재사용한다. guidance 결과 검사에도 같은 연결·저장소 문맥을 전달한다.
3. checkpoint에 의한 기준선 저장에서 기존 scope identity를 유지하고 재시작 회귀를 추가한다.

## Validation

- Worker는 synthetic Git 관측과 격리된 SQLite를 사용하는 targeted unit test로 무관한 B 변경, 실제 A 위반, 격리 증거 부재, guidance 게시, push 실패·재시작을 검증한다.
- source 동기화 실패 뒤 원격 기준 진행의 기존 unit test를 확인한다.
- Manager 전체 verify에서 실제 Git checkpoint·복구 및 PR 회귀를 확인한다. Worker는 integration·전체 verify를 직접 실행하지 않는다.

## Progress

- [x] 관련 문서와 G-135·136·137·140의 로컬 Merge 이력 확인
- [x] 실패 재현과 최소 수정
- [x] targeted unit test 29개 통과
- [ ] Manager 전체 verify, 독립 Review, 사용자 Merge

## Decision Log

- G-140은 차단 해제의 중단 복구·CAS가 목적이며 이미 로컬 이력의 734c381 (#147)에 구현돼 있다. 이번 자원 비교·guidance checkpoint scope 보존은 기존 계획을 확대하지 않고 별도 계획으로 기록한다.
- G-135(61d64a3), G-136(284eae3), G-137(f6e0841)의 기존 원격 대상 재검사·일시 관측 오류 분류·증거 파싱을 재사용한다. 서버 runtime 적용 여부와 운영 사건은 확인하지 않았다.
- R-060 archive는 기존 보존 정책의 참고 자료로만 읽었으며 활성화하지 않는다. G-138·143·146, 유지보수 평가 저장·게시, 공통 테스트 fixture는 수정하지 않는다.
- source_sync는 RuntimeError/OSError/GitLockError를 failed 결과로 반환하고 project_service·bootstrap·self_runtime은 그 결과를 기록한 뒤 원격 SHA로 진행한다. checkpoint dispatcher도 PLAN_BLOCKED 상태에서 호출된다. 이 경로의 동작을 임의로 변경하지 않는다.
- 수정 전 집중 테스트에서 B local/remote ref·worktree 등록·branch config 변화가 checkpoint PROJECT 위반으로 판정되는 세 경우, guidance 검사 문맥 누락, Manager checkpoint의 scope identity 유실을 재현했다. 게시된 guidance의 이중 차단 복구도 B 변화만으로 거부되는 것을 별도 재현했다.
- checkpoint 안전 검사와 guidance 이중 차단 복구에서 `_agent_git_resources` 및 `scoped_metadata`를 재사용한다. invocation·session·repository·선택 worktree 증거를 매번 확인하며 검증 실패 시 기존 전체 비교로 되돌아간다. snapshot의 원본 metadata는 변경하지 않고 비교 피연산자만 제한한다.
- guidance checkpoint 저장은 기존 `scope_id`·`scope_start_digest`만 유지한다. 과거 증거 없는 snapshot에 새 격리 증거를 부여하지 않는다. 기존 게시 target 재검사, lease, 작업물 보존, local-only 사유·링크 알림 및 retry_pending 처리는 유지한다.
- `scope_recovery.validate_blocked_agent_scope`와 `scope_reconciliation`은 이미 연결·소유 문맥으로 같은 자원 검사를 사용한다. `blocked_plan_reconciliation`의 선택 계획 원격 lease·현재 branch 검사도 유지한다. G-140의 원인별 CAS·복구 stage는 바꾸지 않는다.
- 검증 명령: `py -3 -m unittest tests.test_checkpoint_scope tests.test_scope_services.ScopeValidationTests tests.test_scope_services.PersistedScopeServiceTests tests.test_blocked_checkpoint_state tests.test_scope_project_recovery.ProjectRecoveryTransitionUnitTests -q` (29개 통과). Git·Agent·원격은 unit 경계에서 대체했고, 재시작은 임시 SQLite를 저장·닫기·재열기로 검증했다. 서버 사건이나 실제 push 실패를 재현한 결과가 아니다.
- 기존 scope unit test 두 곳의 `/managed` 문자열을 현재 OS의 canonical path로 정규화했다. assertion과 공통 fixture의 자원 수명은 유지했으며 G-143 범위를 변경하지 않았다.
- Windows/Linux 전체 verify 진입점은 기존 discovery로 새 테스트를 수집하므로 변경하지 않는다. 전체 verify·integration·독립 Review·사용자 Merge는 아직 수행되지 않았다. 시작 HEAD의 별도 문서 commit b0abfa3은 기존 main에 보존하고 이번 PR 기준은 origin/main으로 잡았다.
