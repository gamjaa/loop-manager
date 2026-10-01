---
id: G-107
title: Project Blocked Remote Recovery and Diagnostic Publication
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-106
complexity: high
risk: critical
priority: critical
---

## Purpose

계획과 연결된 `PROJECT_BLOCKED` 중에도 검증 가능한 대상 계획의 원격 변경 복구와 안전한 작업물 보존을 수행한다. 기존 작업 branch의 게시 계보가 불확실하면 그 ref를 덮지 않는 진단 branch를 사용한다.

## Acceptance Criteria

- 프로젝트 차단은 계획별 읽기 관측과 소유권이 증명된 복구 Git 쓰기의 일괄 금지 조건이 아니다. 해당 계획이 기존 변경 자리를 점유했거나 자리가 비어 있고 작업 범위를 독립적으로 검증한 경우에만 G-106의 후속 commit 채택·rebase·원인 재검증·checkpoint 갱신을 실행한다. 다른 계획의 실행, 새 Agent 시작, 일반 Planner 및 기본 worktree 쓰기는 기존 project 안전 gate를 유지한다.
- 원래 작업 branch의 게시 계보와 마지막 원격 OID가 확인되면 관측 OID에 결합된 fast-forward 또는 PR 없는 동일 소유 branch의 검증된 rebase에 한한 정확한 lease로 게시한다. 원래 프로젝트 차단이 남으면 상태와 이유를 유지하고 원격 checkpoint 결과만 기록한다.
- 기존 branch의 계보를 증명할 수 없어도 대상 작업물의 범위·Git 상태·rebase 완료를 검증할 수 있으면 원래 ref를 덮지 않는 결정적 진단 branch에만 게시한다. 같은 plan·작업물 identity의 동일 OID는 재사용하고 다른 OID가 이미 있으면 갱신하지 않는다. 진단 ref·원본 branch·base·원인·내용 OID를 durable하게 연결하며 PR을 만들거나 원래 branch 소유권·차단 해제 근거로 삼지 않는다.
- 작업 범위·Git 상태를 검증하지 못하거나 충돌 중이면 commit·push하지 않고 작업물·세션과 구체적인 local-only 이유를 보존한다. 원격 삭제·재작성·분기, 권한·branch protection 오류, 원격 경쟁 및 현재 plan·repository·worktree·target·원격 기대값을 확정할 수 없는 legacy 상태는 예상하지 못한 ref의 force push로 우회하지 않는다. 과거 rebase turn·stage 승인·receipt·replay·journal의 누락은 G-109의 현재 상태 판정을 거부할 사유가 아니다.
- 원래 차단 원인만 현재 입력으로 재평가하고 독립된 프로젝트 안전 차단·사용자 결정·다른 계획 차단은 유지한다. 원인 또는 원격·작업물 입력이 같은 반복 실행은 재게시·재검증·알림을 중복하지 않고, 입력이 바뀌면 다시 처리한다.
- 진단 branch 생성·checkpoint commit·push·SQLite 기록 사이에서 중단돼도 실제 원격 OID와 저장 intent를 대조해 같은 작업물을 한 번만 게시 완료로 확정한다. 알림에는 확인된 ref·OID 또는 실제 local-only 사유를 포함한다.

## Implementation

- `loop_manager.py`의 project 안전 gate에 대상 계획과 실행 자리로 한정한 복구 경로를 두고 G-106의 원격 채택·rebase·원인 검사 서비스를 재사용한다. rebase 진행·완료와 수동 해결은 G-109·G-110의 현재 상태 기준을 소비하며 project 예외용 journal/replay gate를 추가하지 않는다. `scoped_blocking.py`에서는 원인별 해제를 적용하고 관련 없는 project block을 자동으로 지우지 않는다.
- `blocked_checkpoint.py`와 저장 모듈에 원래 branch 게시와 분리된 진단 branch intent·결과를 추가한다. `notifications.py`의 차단 사건은 저장된 게시 결과를 참조한다. 원격 ref 생성은 Agent 밖 Manager의 정확한 이름·부재 검사로 제한한다.
- R-046의 project 차단 예외와 안전한 게시 규칙을 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_blocked_checkpoint.py`, `tests/test_blocked_checkpoint_state.py`, `tests/test_scoped_blocking.py`, `tests/test_notifications.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 project 차단 중 base·branch 변경과 재개, 자동·수동 rebase 결과, 동일 ref checkpoint, 게시 계보 불확실 시 진단 branch, 진단 ref 충돌, 원격 경쟁, 충돌 중 local-only 및 push 후 SQLite 반영 전 재시작을 검증한다. rebase 수행 증거가 없어도 현재 상태가 정상인 작업물은 같은 보존 경계를 사용하고, 실제 소유권·범위 불명확은 local-only로 남겨야 한다. 실제 Git integration case는 Worker가 실행하지 않고 Manager 전체 verify에 맡긴다.
- 전체 verify는 다른 계획의 상태·실행 자리와 기본 worktree 보존, 진단 branch의 PR 미생성·소유권 비승격, 알림 중복 방지 및 최종 검증 gate를 확인한다.

## Progress

- [x] project 차단 중 계획별 안전 복구 경계
- [x] 진단 branch 게시·local-only 결과 및 중단 복구
- Worker: 현재 상태 수용·직렬 실행 자리·진단 ref 재사용·충돌·중단 복구·알림의 targeted unit test 통과. 실제 Git 회귀 테스트와 전체 verify는 Manager 실행 대기.
- Manager의 600초 verify 시간 초과를 진단해 Git fixture class의 전역 import로 중복 수집되던 실제 Git 테스트 72개를 제거했다. 기존 고유 검증은 모두 유지하고 수집 회귀 unit test와 Linux verify의 개별 case 진행 출력을 추가했다. 전체 verify 재실행은 Manager 대기.

## Decision Log

- `PROJECT_BLOCKED`의 일반 실행 제한은 유지한다. 이 계획은 R-046에 명시된 검증 가능한 대상 작업의 복구·보존만 예외로 허용한다.
- 2026-09-30: mutable READY snapshot의 기준 revision·원본 hash를 확인해 R-048 완료 정책을 연결했다. G-106을 통해 G-110·G-109를 선행하며 과거 rebase 작업 증거를 project 복구 또는 진단 게시의 필수 조건으로 재도입하지 않는다. 기존 branch의 게시 identity·원격 lease 검증은 유지한다.
- 2026-10-01: project 복구 orchestration은 `project_blocked_recovery.py`로 분리하고 G-106 원격 관측·후속 commit 채택 및 현재 rebase 상태 서비스를 재사용한다. 진단 intent·결과는 원래 branch 게시 소유권과 분리된 additive SQLite 테이블에 저장한다. 진단 ref는 검증된 push URL과 정확한 부재 lease로 생성해 원래 ref와 tracking ref를 변경하지 않는다. 원래 project 원인·투영·다른 plan 및 감사 기록은 보존하고, 현재 후보 검증 토큰만 무효화한다.
- 2026-10-01: project 예외 rebase는 명령별 `core.hooksPath`와 `rebase.updateRefs=false`로 hook과 다른 branch의 자동 갱신을 막는다. project 예외 push도 hook을 비활성화한다. 기본 worktree 및 다른 계획 ref의 쓰기 경계는 유지한다.
- 2026-10-01: verify 피드백 `8262527e49a77b20169ba26cc971a4d307032fe79ff83eb5b88e46bd1258594e`를 읽기 전용으로 조사했다. 04:56:03에 600초 제한으로 종료됐으며 마지막 04:55:38 runtime 출력 뒤 22개 case가 완료됐다. 그 다음 순서의 `test_final_assessment_uses_independent_pages_and_durable_evidence`는 단독 unit 실행에서 1.469초에 통과했다. 실행 없는 discovery에서 기존 1,710개 중 고유 case는 1,638개이고 `BlockedCheckpointGitAdapterTest`의 72개가 전역 fixture import 때문에 두 번 수집됨을 확인했다. fixture를 모듈 경유로 참조해 고유 검증을 보존하고, 재발 방지 unit test와 `-v` 진행 출력을 추가한다. 시간 상한은 변경하지 않고 중복 제거 후 전체 검증을 Manager에 맡긴다.
