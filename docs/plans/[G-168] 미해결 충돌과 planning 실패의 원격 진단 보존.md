---
id: G-168
title: 미해결 충돌과 planning 실패의 원격 진단 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-165
  - G-166
  - G-167
complexity: high
risk: critical
priority: critical
---

## Purpose

자동 해결하지 못한 충돌·최종 계획/프로젝트 차단·planning 실패의 작업 자료를 원래 branch 또는 별도 진단 ref에 안전하게 보존한다.

## Acceptance Criteria

- 현재 작업물·단계·원인을 원격에 남긴다. 원래 branch 게시가 불가능하면 검증된 별도 진단 보존을 시도하고 실제 게시 불가 사유·누락 범위를 기록한다. 새 PR·Draft PR을 생성하지 않는다.
- 진단 commit에서 diff·파일 원형·base/ours/theirs·관련 commit·남은 반영 작업을 확인할 수 있다. rename/delete/binary·충돌 중 수동 수정과 mode를 보존하고 원래 index·rebase를 자료 게시로 변경하지 않는다.
- 보존 대상은 선택 작업의 검증된 허용 자료다. 인증 정보·비밀값·무관한 파일을 게시하지 않으며 충돌 자료가 충분하지 않으면 완전 보존을 주장하지 않는다.
- 수집·commit·push·SQLite 기록·차단 해제의 중단/재시작에서 동일 자료를 중복 게시하지 않는다. 기록에 작업 branch·진단 ref·commit·수정 위치·남은 원인/게시 불가 사유가 있고 사용자 원격 수정 뒤 해결된 원인만 재검사한다.

## Implementation

- 기존 checkpoint_service·blocked_checkpoint_state와 rebase_file_evidence의 원형 blob 수집을 재사용한다. 원본 작업 공간을 변경하지 않는 관리 진단 작업 공간과 보존 manifest/게시 intent를 기존 경계에 최소 추가한다.
- planning 오류의 별도 저장 문맥과 계획/프로젝트 차단을 공통 보존 요청으로 연결하되 owner·scope·자료 완결성을 각각 검증한다. 진단 보존이 구현 승인이나 충돌 해결을 대신하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- checkpoint·rebase evidence·planning 오류 테스트에서 미해결 충돌 각 파일 형태, 수동 수정, 원본 index 불변, 비밀/무관 자료, 원래 branch 불가와 중단의 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 Git 진단 자료 복원 가능성, 원격 경쟁·중복 게시 방지, 사용자 해결 재관측과 독립 승인·차단 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 자료 수집·격리 게시·재시작의 복합 경계로 complexity high, 원형 손실·민감 자료 게시 영향으로 risk critical다. 자료 게시의 실패가 독립 구현 작업을 새로 차단하지 않는다.
- 원격 보존은 기존 checkpoint가 원래 branch를 먼저 시도하는 순서를 유지한다. 미게시·재시도 대기 checkpoint, 연결된 프로젝트 복구 실패와 planning 오류를 공통 `diagnostic_preservation` 경계로 연결한다. 보존된 planning 오류의 재시도는 실행 투영을 복원하지 않고 독립 작업 선택 전에 수행한다.
- 진단 자료는 `<state-dir>/diagnostic-artifacts/<identity>/`의 내용 보존 bundle과 부모 없는 Git commit에 담는다. 원본 Git object 저장소를 재사용하되 관리 임시 index만 사용하며 원래 HEAD·index·ref·rebase를 바꾸지 않는다. 고정 author/date·tree와 원격 ref의 부재 lease로 동일 자료를 재사용하고, 게시 전 intent와 확인 후 receipt를 SQLite에 저장한다. object 정리 뒤에도 bundle과 현재 검증 자료로 같은 commit을 재구성할 수 있다.
- 수집 대상은 현재 작업 범위를 통과한 변경 경로다. 승인 근거 없는 untracked, ignored, symlink·특수 파일, 읽을 수 없는 원형 및 비밀값을 포함한 경로는 diff까지 제외하고 manifest에 누락 이유를 남긴다. base·HEAD·시작/재생 commit·index stage·현재 수동 수정의 bytes·mode·부재를 별도로 보존하며 rename은 두 경로, binary는 원형 blob으로 표현한다. planning stash의 staged·untracked 원본도 선택된 문서 범위에서 보존한다.
- 현재 상태 서비스의 정상 게시 판정은 그대로 유지한다. 닫힌 PR 또는 삭제된 원래 ref의 자료 보존은 동일 PR identity·원격 ref·worktree·rebase·파일 범위를 별도로 검증하며 rebase acceptance나 게시 승인으로 반환하지 않는다. 결과 알림은 현재 원인에 결합된 진단 ref·commit·원래 branch·누락 범위·미게시 사유만 사용한다.

| 생성·재개 경로 | 보존·보호 경계 | 사용자 조치 |
|---|---|---|
| 계획 checkpoint의 Git operation·범위·PR·push 거부 | `checkpoint_service.dispatch_blocked_checkpoints` → 현재 소유권·scope 검사 → 격리 진단 intent; 원래 checkpoint 소유권과 차단은 유지 | 원래 branch 또는 안내된 근거 문서의 원인을 수정 |
| 연결된 프로젝트 복구의 충돌·게시 불가·동일 입력 대기 | `project_blocked_recovery` → 연결 계획·단일 실행 자리 검사 → 공통 보존; 다른 계획·프로젝트 원인은 해제하지 않음 | 현재 연결 작업의 남은 원인 수정 |
| planning 결과 오류·보존된 오류의 다음 실행 | `planning.state_ports` → 별도 planning 문맥·worktree·관리 rebase 검사 → 공통 보존; base에 실패 결과·catalog를 직접 적용하지 않음 | 원격 base의 계획·근거 수정 또는 로컬 환경 복구 |
| 수집/commit/push/SQLite 확정 중단 | 원본 재관측·내용 hash·고정 commit·저장 intent·정확한 원격 OID 대조; ref 경쟁은 갱신 거부 | 실제 인증·권한·경쟁·누락 이유에 따른 수정 |

- 관련 unit case는 `tests/test_diagnostic_preservation.py`에 있으며 실제 Git·원격·Agent effect를 mock한다. 기존 원형 evidence·diagnostic lease·planning 로그·차단 알림의 좁은 unit case를 함께 재사용한다. `tests/test_notification_incidents.py`의 helper import를 저장소 패키지 경로로 맞춰 직접 targeted 실행도 가능하게 한다. 기존 `scripts/verify.sh`는 새 test module을 자동 발견하는 전체 runner를 유지하며 Worker는 이를 실행하지 않는다.
- 루트의 진단 보존 연결도 기존 `_bind_effects`와 명시적인 effects 계약을 사용한다. 연결 프로젝트의 기존 게시가 완료되고 새 입력이 없으면 추가 진단을 생성하지 않으며, 미완료·충돌 대기 자료는 계속 재관측한다. 닫힌 PR의 동일 identity 조회는 차단 해제와 구분한다. 보존 실패 receipt가 초기 알림을 대체할 때 이전 미발송 알림은 취소하고, 동일 receipt의 재시작은 새 알림을 만들지 않는다. Manager용 Git 회귀 case는 원래 index·branch 불변, 진단 ref의 stage 원형·현재 bytes와 중복 push 방지를 확인한다.
- `check-ignore`는 literal pathspec magic을 지원하지 않으므로 ignore 검사에서 `--no-literal-pathspecs`로 상속된 설정도 해제한다. 종료 코드 1인 경로만 원형 수집하고 ignored(0)·검사 실패는 계속 제외한다. unit case는 이 명령 경계와 충돌 stage·수동 수정 원형 보존, 제외 시 누락 표시를 함께 검증한다.
