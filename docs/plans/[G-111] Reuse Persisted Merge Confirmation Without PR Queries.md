---
id: G-111
title: Reuse Persisted Merge Confirmation Without PR Queries
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

SQLite에 이미 Merge가 확정된 PR은 이후 Loop 실행, 재시작과 archive 복구에서 GitHub에 다시 조회하지 않는다. 처음 Merge를 확인할 때만 GitHub 상태를 사용하고, 확정 이후에는 저장된 실행 상태와 검증된 Merge 이력을 재사용한다.

현재 `observe_waiting_pull_requests()`는 `PR_OPEN`과 PR Close로 차단된 계획만 조회하므로 정상 감시에서 `MERGED`를 제외한다. 그러나 `project_step()`이 실행마다 호출하는 `migrate_legacy_execplans(reconcile_history=True)`의 archive 복구는 모든 archived 계획에 Merge proof를 요청한다. `_LegacyCatalogMergeProof.proof_for()`는 저장된 `MERGED` 상태보다 `pr_for_branch()`를 먼저 호출하고, PR이 없을 때에만 `_stored_git_proof()`를 사용한다. 따라서 이미 완료된 PR도 반복 조회하며 GitHub 조회 실패가 기존 Merge 이력 복구에 영향을 준다.

이 계획은 PRODUCT의 SQLite 실행 상태, 중단 후 재개, Merge 확인과 내용 보존 archive라는 기존 Current Goals 안에서 위 재조회 결함을 수정한다. Merge 여부를 처음 확정하는 조건과 archive 파일을 검증하는 조건은 유지한다.

## Acceptance Criteria

- 동일 repository와 ExecPlan의 `exec_plans.status`가 `MERGED`이면 해당 PR에 대한 `gh pr view`와 `gh pr list`를 호출하지 않는다. active 파일이 아직 남아 있는 경우, archive 완료 또는 pending, 남은 worktree와 stale project projection이 있는 경우에도 적용한다.
- 검증된 archive 이력의 `merge_confirmed`, `identity_verified`, 최초 게시 identity와 Merge proof가 같은 repository·계획에 대응하면 이를 재사용한다. 실행 row가 없는 audit-only 복구도 검증된 저장 이력으로 처리하며, 서로 모순되거나 불완전한 DB 이력을 임의로 정상 상태로 승격하지 않는다.
- 저장된 `MERGED` 계획의 게시 SHA·본문 hash·identity가 불완전하거나 로컬 Git 증명을 확인할 수 없더라도 GitHub 조회로 우회하지 않는다. 기존 `MERGED` 상태를 유지하고 구체적인 catalog diagnostic 또는 archive pending으로 남기며, 확인되지 않은 archive identity로 dependency를 충족시키지 않는다.
- PR을 다시 조회하지 않아도 최신 원격 base의 archive 본문, 최초 게시 identity, 마지막으로 동기화한 본문 hash와 기존 Git 검증은 계속 검사한다. archive 변조, 이전 게시 본문으로의 되돌림, repository·계획 identity 불일치는 기존처럼 거부한다. archive 경로 또는 Git ancestry만으로 처음 Merge를 확정하지 않는다.
- 아직 Merge가 확정되지 않은 `PR_OPEN`, PR Close 차단, 게시 중단 복구와 DB에 확인 이력이 없는 legacy 계획은 필요한 GitHub 조회를 유지한다. Open·Closed·Merged PR을 구분하고 PR 재사용, 재개와 충돌 복구의 기존 안전 검사를 보존한다.
- 최초 GitHub Merge 관측을 SQLite에 commit한 뒤에는 같은 실행의 후속 처리와 다음 실행·Manager 재시작에서 해당 PR을 다시 조회하지 않는다. commit 전에 중단돼 DB에 확정되지 않은 경우의 재조회는 허용한다.
- 저장된 Merge proof의 재사용은 다른 계획의 상태, Worker·Reviewer session, planning 오류와 차단을 변경하지 않는다. worktree 정리, ExecPlan archive, dependency 선택, 유지보수 Merge 집계와 알림 중복 방지는 기존 규칙을 따른다.
- Merge가 확정된 PR만 여러 개 있는 프로젝트는 PR API 호출이 0회다. 확정된 PR과 미확정 PR이 섞인 프로젝트는 미확정 대상만 조회한다. GitHub PR 조회가 실패하는 환경에서도 확정된 계획에 대한 로컬 검증과 정리가 그 조회 실패에 의존하지 않는다.

## Implementation

1. `execplan_catalog_state.py`의 저장 계층과 복구 서비스에 기존 runtime identity 및 검증된 history에서 재사용 가능한 Merge proof를 선택하는 작은 경계를 둔다. repository·계획 ID, 최초 게시 경로·hash·SHA와 proof의 일치 여부를 명시적으로 판정하고, Git/GitHub 호출은 기존 외부 adapter에 남긴다. 기존 schema와 실행 상태 모델을 사용한다.
2. `loop_manager.py`의 `_LegacyCatalogMergeProof.proof_for()`에서 DB의 `MERGED` 여부를 GitHub 호출보다 먼저 판정한다. 저장 상태가 확정됐으면 기존 `_stored_git_proof()` 또는 검증된 history를 사용하며, 로컬 증명 실패도 GitHub fallback의 근거로 사용하지 않는다. 미확정 legacy 복구만 기존 PR 조회 경로로 보낸다.
3. `CatalogRecoveryService.reconcile()`은 검증된 저장 proof를 재사용한 뒤에도 현재 archive 본문과 게시 identity를 비교한다. `CatalogStateRepository.record()`의 원본 감사 정보 보존 규칙을 유지하고, archive 검증 실패가 runtime의 `MERGED`를 되돌리거나 검증되지 않은 dependency를 허용하지 않도록 한다.
4. `project_step()`의 기존 PR 복구, `observe_waiting_pull_requests()`와 실행·게시 복구의 PR 조회 진입점을 점검한다. stale projection 또는 오래된 입력 row가 이미 `MERGED`인 계획을 다시 조회 대상으로 만들 수 있는 곳에는 DB의 정확한 계획 identity를 확인하는 guard를 추가한다. 공통 판정은 재사용하며 계획 상태를 알 수 없는 저수준 `pr_for_branch()`에 추측성 skip을 넣지 않는다.
5. 새 프로세스에서도 저장 상태만으로 조회를 생략하도록 한다. 프로세스 메모리 캐시, PR 조회 주기 설정 또는 `--state open`으로의 일괄 변경에 의존하지 않는다. `cleanup_pending`과 archive 후속 작업은 저장된 PR URL·head OID 및 현재 Git 상태를 사용하며, 소유권·미게시 commit 검사와 기본 worktree 보호를 유지한다.

## Validation

- Worker는 `tests/test_archive_recovery_regressions.py`의 `ArchiveRecoveryRegressionTest`와 `tests/test_execplan_catalog_state.py`의 `ExecPlanCatalogStateTest`에 이번 변경을 검증하는 unit case를 추가하고 해당 case만 실행한다. DB 상태와 외부 Git/GitHub port를 통제하며, MERGED 경로의 `pr_for_branch`는 호출 즉시 실패하는 mock으로 두고 `assert_not_called()`도 확인한다.
- unit case는 저장 MERGED와 유효한 로컬 증명, 게시 증명 누락·로컬 Git 실패, 검증된 audit-only history 재사용, history identity 불일치, 변조된 archive, 개정된 최신 본문과 이전 게시 본문 복원을 각각 포함한다. 필요한 미확정 legacy 조회는 `assert_called_once()` 또는 정확한 대상별 호출 목록으로 확인한다.
- 반복 reconcile과 재연결한 SQLite에서 Merge 상태·proof가 재사용되는지 확인한다. 최초 관측 commit 뒤에는 조회가 0회이고, commit 전 중단에는 기존 미확정 복구가 유지돼야 한다.
- project 조회 guard를 변경하면 `tests/test_loop_manager.py` 또는 `tests/test_execution_handlers.py`에 해당 경계의 targeted unit case만 추가·실행한다. MERGED와 PR_OPEN이 섞인 경우, stale projection과 같은 실행 중 앞 단계가 MERGED로 전이한 경우를 포함한다.
- Manager 전체 verify는 기존 `test_archived_merged_row_recovers_from_stored_state_and_git_ancestry`를 조회 금지 assertion으로 강화한 실제 Git 회귀와 archive·dependency·cleanup·PR 게시 복구를 검증한다. 최초 게시 hash와 최종 개정 hash가 다른 정상 archive, archive 변조, 작업물 보존과 다른 계획·planning 상태의 독립성을 확인한다. 실제 Git integration case와 전체 verify는 Worker가 직접 실행하지 않는다.

## Progress

- [x] 저장 Merge proof 선택과 archived 복구의 PR 조회 제외
- [x] PR 조회 진입점의 MERGED guard와 미확정 조회 보존
- [ ] 관련 targeted unit test 및 Manager 전체 verify 결과 확인

- 2026-10-01: 재개한 Worker에서 세 가지 Review 피드백을 수정했다. 변조 진단 이후에도 확정 Merge 증명과 마지막 정상 archive hash를 보존하고, 최초 게시 경로와 현재 rename 경로를 구분하며, audit-only 복구도 로컬 게시 blob·ancestry를 검증한다. 관련 개별 unit case 12개를 총 13회 실행해 모두 통과했다. Manager 전체 verify와 재Review는 대기 중이다.

- 2026-10-01: Manager 전체 verify에서 archive 후보 계산 unit fixture의 `diagnostic_code` 열 누락이 확인됐다. 축소 history DDL 대신 실제 catalog schema 생성 함수를 사용하도록 수정했다. 실패한 개별 unit case를 동일 명령으로 재실행해 1 test PASS를 확인했다. 전체 verify 재검증은 Manager에 맡긴다.

- 2026-10-01: 오래된 PUBLISHING 등의 project projection에서 PR 복구 조회가 재발하는 Review 피드백을 수정했다. 조회 직전에 repository·계획 ID·branch와 최신 SQLite MERGED 상태를 비교한다. rename 후 projection, 같은 실행의 선행 Merge 전이와 미확정 PR 혼합, 다른 repository의 동일 ID를 검증하는 `tests.test_loop_manager.PersistedMergeProjectQueryTest`의 unit case 3개가 PASS했다. 전체 verify와 재Review는 Manager에 맡긴다.

- 2026-10-01: MERGED 조회 guard 뒤에 남은 실행 projection이 다음 READY와 완료 경로를 막는 Review 피드백을 수정했다. planning 소유권이 없는 동일 계획의 projection을 기존 runtime 정리 경계로 해제하고, 다른 활성 계획이 있으면 그 계획으로 다시 hydrate한다. 실제 planning coordinator와 실행 prepare를 사용하는 unit case 및 planning 보존 case를 추가했다. fixture의 catalog revision·publication audit backfill 기준을 보완한 뒤 동일 targeted 명령 `python3 -m unittest tests.test_loop_manager.PersistedMergeProjectQueryTest`에서 6 tests PASS를 확인했다. 전체 verify와 재Review는 Manager에 맡긴다.

## Decision Log

- 2026-09-30: 사용자가 DB상 이미 Merge된 PR의 재조회 제외를 요청했다. 기존 PRODUCT의 실행 상태·복구·archive 목표 안에서 확정된 사실을 재사용하는 수정으로 계획하며, PRODUCT·Requirement·ARCHITECTURE와 기존 ExecPlan은 변경하지 않는다.
- `MERGED`는 최초 GitHub 확인 뒤 SQLite에 저장된 실행 상태다. 저장 proof가 불완전하다는 이유로 같은 PR을 다시 조회하지 않으며, archive 적합성은 별도의 로컬 identity·본문 검증으로 판단한다.
- GitHub에서 처음 Merge를 확인하기 위한 조회와 아직 미확정인 PR의 복구 조회는 계속 필요하다. 이 계획은 새로운 PR 상태나 자동 Merge, 데이터 삭제·migration, 게시 권한 또는 새로운 infrastructure를 추가하지 않는다.

- 2026-10-01: `merge_confirmed`는 한 번 확인된 Merge 사실을 보존하며, 현재 archive 적합성은 `diagnostic_code`와 함께 판단한다. dependency 선택, 유지보수 Merge 집계·단계 전환, Requirement archive 후보와 대기 사유 계산은 진단이 남은 archive를 검증되지 않은 이력으로 취급한다. 저장 증명 재사용을 위한 외부 port는 PR 조회 없이 로컬 Git 검증만 수행한다.
