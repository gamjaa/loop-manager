---
id: G-229
title: 게시 checkpoint 재개 소유권의 연속성 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-081] 게시 checkpoint 재개 소유권의 보존과 재검증.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

게시 OID가 유지됐지만 재개 표식이 없는 정상 작업의 소유권 거부를 수정한다. 현재 소유권 검증과 기존 재개·원인별 해제 경계를 사용하며 게시 영수증, 작업물과 독립 차단을 보존한다.

## Acceptance Criteria

- R-081의 여덟 완료 조건을 구현·테스트 근거와 대조한다. 표식 없는 published 상태 및 과거 게시 이력만 남은 local-only 상태를 실제 handler·SQLite로 검증한다.
- 재차단·원인 해제·재활성화와 새 handler/DB 연결의 재시작에서도 같은 소유권·lease 기준을 통과한다. 로컬·원격 OID 일치만으로 승인하거나 게시 OID를 삭제하지 않는다.
- 원격·PR 변화, 소유 공간·identity·tree 불일치, 낡은 표식, 관측 오류와 실제 Agent 위반은 기존 거부·재시도·보존 경계를 유지한다.
- 원인·generation·게시 영수증 경쟁과 해제 중단에서 새 상태를 보존하고 증명 저장·해제를 부분 확정하지 않는다. 별도 계획 차단·미해결 Worker 실패·session·감사를 유지한다.
- WSL 전체 verify와 R-081·PRODUCT·ARCHITECTURE 기준 읽기 전용 재검증을 마친 뒤 PR을 남긴다. 운영 G-179 상태나 원본 영수증을 수정하지 않는다.

## Implementation

| 전이·소비자 | 게시 OID와 증명의 수명 | 수정·회귀 근거 |
|---|---|---|
| Worker 실패 → `RuntimeStateRepository.block_plan` | 실패와 계획 차단을 저장하고 보존 checkpoint를 요청한다. 게시 OID는 유지되며 기존 `blocked_resume`은 제거된다. | 새 원인에서 `published_review_resume`도 무효화하고 이전 게시 영수증은 보존한다. `test_reblock_discards_old_resume_but_keeps_publication_for_reactivation` |
| checkpoint 게시 → `BlockedCheckpointRepository.finish_published` | 게시 identity·tree·원격 OID와 결과를 확정한다. 게시 소유권은 실패 해결이나 verify 성공이 아니다. | 기존 게시 경계와 `tests.test_blocked_checkpoint_state` 유지 |
| 차단 원인/요청 교체 → `request`·`refresh_current`·`refresh` | 현재 요청의 증명 필드나 게시 phase/result를 초기화할 수 있다. | 초기화 전 소유 공간·요청·게시 근거를 포함한 원본 영수증을 역사 기록으로 남긴다. 역사 게시 보존과 동일 요청 refresh 회귀 |
| 실제 원인 해제 → 기존 계획 복구 | 원래 실패가 해결됐을 때만 READY/기존 교정 단계로 이어간다. | 게시 소유권 수용 자체는 실패를 지우지 않는다. 정상 재개·독립 계획 차단 회귀 |
| `RuntimeStateRepository.activate` → `WorkspaceHandler.recover` | 계획의 게시 OID는 유지하지만 유효한 rebase acceptance가 없으면 snapshot 대부분을 초기화한다. | 기존 게시 재개 표식 보존과 표식 없는 상태의 재검증을 연결한다. `test_markerless_published_oid_revalidates_and_survives_restart` |
| `OwnershipHandler._owned_checkpoint_remote` | 기존 분기는 OID가 있고 표식이 없으면 Git 검증 전에 거부한다. | 기존 identity·tree·계보·worktree·실제 ref 검증을 사용하며, 현재 요청/역사 영수증으로 확인한 근거를 기존 재개 소비자에 연결한다. 소유권·원격·PR·낡은 표식 거부 회귀 |
| `published` → 새 요청의 `unpublished/local_only` | 기존 게시 증거는 역사 기록으로만 남을 수 있다. | 충분한 scoped 또는 정확한 lease에 결합된 구형 flat 영수증을 읽기 전용으로 검증한다. 현재 요청과 게시 감사를 덮지 않는다. 역사·구형·누락/손상 이력 회귀 |
| 게시 소유권 오탐 해제 → `_resume_owned_checkpoint_project_block` | 기존 무 rebase 복구는 Review 원인과 특정 snapshot 필드에 한정됐다. | 같은 검증을 Worker 실패 뒤에도 적용하고 저장된 실제 Agent 결과를 확인한다. 해제 transaction에서 원인·소유자·generation·영수증·Git/PR/원격을 다시 대조한다. 프로젝트 복구·독립 차단·실제 Agent 위반·경쟁·중단 rollback 회귀 |

최초 실패 분기는 메모리 SQLite와 실제 handler로 검증한다. 관측 경계만 대체하고 소유권 검증·상태 전이·저장 판정은 실행한다. 기존 Review/rebase 경로와 필드를 재사용하며 새 복구 CLI·설정·범용 소유권 계층을 추가하지 않는다.

## Validation

- 최소 unit: `python3 -B -m unittest tests.test_published_checkpoint_resume tests.test_execution_failure_policy tests.test_execution_publication_phase tests.test_execution_review_phase tests.test_scope_owned_publication tests.test_blocked_checkpoint_state`.
- 전체: 최종 구현 커밋과 동일한 Git 객체를 WSL의 독립 checkout으로 복제하고 `bash scripts/verify.sh`를 실행한다. 수집 manifest·완료 ID·skip·compile 결과를 `.cache/verify/` 보고서로 확인한다.
- 문서 재검증: R-081의 각 기준을 위 전이·전용 회귀 및 기존 통합 회귀와 대조하고 현재 PRODUCT·ARCHITECTURE와 모순이 없는지 확인한다. 테스트 판정을 성공값으로 대체하거나 운영 복구 성공을 추측하지 않는다.
- 실제 Git의 효과는 기존 Review 재개·관리 rebase·lease 통합 회귀로 확인한다. 같은 정책 조합을 새 Git 저장소·전체 Agent 실행으로 반복하지 않는다.

최종 WSL 검증은 구현 커밋 `78b31c695dfb8153316643d57ff59847e1073586`의 동일 Git 객체를 Ubuntu/Python 3.12의 독립 checkout에서 실행했다. 한 번의 전체 실행에서 206개 모듈·3,323개 discovery ID를 모두 완료했고 결과는 PASS 3,319개·기존 플랫폼 skip 4개, compile PASS다. manifest와 각 모듈의 completed/fixture-skipped ID 집합이 정확히 일치하며 미완료·실패는 없다. 원문·manifest·모듈 결과·compile event·summary는 전용 워크트리의 `.cache/verify/r081-wsl-final/`에 보존했다.

### R-081 기준 재검증

| 기준 | 확인한 근거·결과 |
|---|---|
| 1. 표식 없는 게시 OID 최소 재현 | `origin/main`의 원래 handler와 같은 SQLite 상태는 거부하며 Git 호출 0회다. 수정 handler는 같은 상태에서 기존 Git 소유권 검증을 실행해 수용한다. 전용 markerless 회귀 PASS |
| 2. 전이별 수명과 사건 원인 구분 | 위 Implementation 표와 request/refresh·재차단·재활성화 테스트로 보존·무효화·소비 경계를 대조했다. G-179 최초 유실 전이는 미확정으로 남기며 다른 branch·rebase 원인으로 단정하지 않는다. |
| 3. 정상 재개·재차단·재시작 | 기존 필드 보존, 새 증명 저장, 실제 DB backup과 새 handler/연결의 재시작, 재차단 뒤 활성화 회귀 PASS. 기존 Review·관리 rebase 통합 회귀도 전체 verify에서 PASS |
| 4. 역사 영수증과 현재 local-only 보존 | scoped/구형 flat 이력의 정상 수용, 증거 부족·손상·다른 repository/계획/소유 공간 거부 PASS. 현재 요청 및 역사 영수증 전후 비교에서 덮어쓰기가 없다. |
| 5. 원자 확정·중단·경쟁 | generation·원인·게시 기록·실제 원격 경쟁 거부, 해제 직후 중단의 증명/차단 rollback 및 재시작 PASS. 불필요한 commit·push·PR 없음 assertion과 기존 알림 transaction/dedup 회귀 PASS |
| 6. 실제 위반·독립 차단 보존 | 원격 변경/삭제·identity/tree/lease/workspace 불일치·낡은 표식·예상 밖 PR 거부 PASS. 실제 저장 Reviewer 쓰기의 범위 판정과 미해결 계획 차단·Worker 실패·session 보존 PASS. 기존 dependency·승인·planning·PR reopen 회귀도 전체 verify에서 PASS |
| 7. 관측 오류 | 실제 owner 판정의 Git 실패는 PUBLICATION_RETRY, PR 조회 실패는 RETRY로 보존한다. 손상 이력도 수용하지 않으며 성공 표식·불필요한 최종 차단을 만들지 않는 회귀 PASS. 기존 SQLite 관측 실패 회귀 PASS |
| 8. 최소 unit·전체 verify·문서 정합성 | 전용 18개와 전체 발견 ID의 실행 완료를 확인했다. 판정 함수는 성공값으로 대체하지 않으며 실제 Git 효과는 기존 통합 회귀를 재사용한다. PRODUCT·ARCHITECTURE·R-081의 적용 조건·거부·원인별 해제·보존·검증을 읽기 전용으로 재대조했고 남은 blocking issue는 없다. |

## Progress

- [x] 표식 없는 게시 OID 거부와 전이별 증명 수명 확인
- [x] 현재/역사 게시 영수증의 읽기 전용 검증과 재개 증명 연결
- [x] 재차단·재시작·독립 차단·실제 Agent 위반 및 중단/경쟁 회귀
- [x] 최종 WSL 전체 verify
- [x] 문서 기준 최종 재검증

## Decision Log

- 2026-10-09: 최신 `origin/main`의 `0d4dd6b`에서 전용 워크트리와 `codex/r081-checkpoint-resume`을 만들고 R-081만 복사했다. 원본 checkout의 R-082 등 별도 변경은 가져오지 않았다.
- G-179의 최초 표식 유실 전이는 운영 증거를 직접 열람하지 않아 미확정이다. 코드에서 확인한 거부 분기와 요청 교체·차단·재활성화의 유실 가능 전이를 구분하며, G-189 전체 감사 또는 G-199 격리 정책 결정의 완료로 취급하지 않는다.
- 게시 OID 삭제·HEAD 일치만의 승인·운영 영수증 재작성은 하지 않는다. 기존 `published_review_resume`의 소비·보존 경계를 재사용해 일반 무 rebase 게시 재개까지 적용한다. 새 차단은 이전 표식을 버린 뒤 현재 소유권을 다시 검사하며 rebase 완료·Agent 격리 성공을 합성하지 않는다.
- risk high: 잘못된 소유권 수용은 실제 Agent 위반이나 다른 원격 ref의 변경을 숨길 수 있다. 역사 증거의 부족·손상, 원격·PR 변화와 독립 차단은 거부/보존하고 원인 해제 transaction에서 다시 확인한다.
- 초기 관련 unit 110개가 WSL에서 통과했다. 추가 경쟁·관측 오류 회귀를 포함한 최종 검증 결과는 아래에 별도로 기록한다.
- 첫 전체 verify는 정상 rebase 이후 최초 게시와 역사 영수증 형식의 기존 회귀를 발견했다. checkpoint가 한 번도 게시되지 않은 상태의 기존 PR/최초 게시 소유권 원본은 원래 게시 gate에 맡기며, 기록 OID는 있지만 역사 checkpoint 증명이 부족한 재개는 계속 거부한다. 같은 요청 refresh에서 추가된 원본 scoped 영수증과 기존 flat 감사가 모두 보존되는 assertion으로 보완했다.
- 첫 전체 verify에서 `origin/main`의 `scripts/verify-inputs.json`이 이미 존재하는 `test_repository_manifest_covers_actual_fixture_documents_and_skills`의 설정·AGENTS·PLANS·역할 지침·Specification Skill 입력을 누락한 사실도 확인했다. 기존 계약을 만족하는 입력 목록 다섯 개만 추가하며 테스트 skip·수집 범위 축소로 우회하지 않는다.
