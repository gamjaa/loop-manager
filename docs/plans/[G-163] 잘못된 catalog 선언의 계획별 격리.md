---
id: G-163
title: 잘못된 catalog 선언의 계획별 격리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

현재 catalog refresh의 전체 소비자 중단을, 식별·영향 범위를 확정할 수 있는 계획별 오류에서는 최소 범위 중단으로 바꾼다.

## Acceptance Criteria

- 고유하게 식별한 잘못된 선언·source·dependency와 해당 계획의 영향받는 의존 계획을 중단한다. 검증된 독립 READY의 선택·planning 및 기존 PR 관측을 계속하고 다른 프로젝트를 막지 않는다.
- ID 충돌·공유 자원·소유권·범위가 실제로 불확실하면 관련 실행을 거부하며 원인과 영향 범위를 남긴다. 유효하지 않은 전체 catalog를 부분 성공으로 저장하거나 dependency gate를 우회하지 않는다.
- 안전한 원격 개정 뒤 실제 원인만 재검사·해제하고 독립 승인·차단·실행 이력은 보존한다. 중단·재시작·원격 경쟁에서도 검증한 catalog와 보류 오류의 결합이 유지된다.

## Implementation

- catalog/execplan_catalog.py의 오류 정보, project_catalog_refresh.py와 storage/execplan_catalog_state.py의 채택 경계를 분리한다. 독립성이 증명된 실행 view와 보류된 오류를 같은 원격 revision에 결합한다.
- 기존 scoped_blocking·PR preflight·선택 gate를 재사용한다. 모든 catalog 소비자가 계획별 영향 집합을 일관되게 적용하도록 하고 임의의 깨진 문서 parsing으로 신원을 추정하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_project_catalog_refresh.py, test_execplan_catalog.py, test_scoped_blocking.py 중 독립/간접 의존·ID 모호함·공유 문제·동시 개정·재시작의 최소 unit case를 실행한다.
- Manager 전체 verify는 잘못된 입력의 비채택, 유효 독립 작업·등록 프로젝트 scheduler·PR 관측 지속과 안전 차단 보존을 검증한다.

### 구현 대응표

| 경계 | 판정·보호 규칙 | 복구·사용자 조치 |
|---|---|---|
| `execplan_catalog.snapshot_from_documents` | 정상 frontmatter와 파일명으로 신원을 확정한 선언·source·dependency 오류와 간접 의존 계획만 제외한다. 깨진 frontmatter·encoding·비정규 파일·ID 충돌은 전체 거부한다. | 원격 문서를 수정하면 새 revision에서 다시 검증한다. filename만으로 ID를 추측하지 않는다. |
| `CatalogReconciliationService` / `DefinitionStateRepository.apply` | 실행 view·보류 ID/path/hash/원인·revision을 같은 ownership CAS transaction에 저장한다. 잘못된 catalog는 전체 성공 revision으로 저장하지 않는다. 공유 branch·겹치는 worktree·불확실한 Agent scope는 거부한다. | revision·SQLite 경쟁은 rollback 후 재조회한다. 새 잘못된 선언의 ID도 보존하고 원격 교정 후에만 등록한다. 삭제 처리는 G-164 범위다. |
| `scoped_blocking` / 선택·execution·PR gate | 별도 catalog hold로 기존 승인·차단을 보존한다. READY·maintenance 후보·실행 전환·PR 충돌 선택은 영향 집합을 제외하고 PR 관측은 유지한다. 실행 전환 transaction 안에서도 gate를 확인한다. | 보류한 실행 단계와 세션·검증·실패 이력을 보존하고 교정 뒤 Worker와 Planner의 점유가 모두 해제되면 원래 단계로 복원한다. Planner 소유권도 CAS에 포함하고 cache는 실행 자리 해제를 재검사한다. 독립 승인·차단은 해제하지 않는다. |
| `ProjectCatalogRefresh` / legacy 이관 / 일반·유지보수 Planner | 새 선언의 필수 priority·활성 source 규칙과 보류 원인·영향 집합·파일 hash·Git 변경 없음의 증명을 공유한다. mutable READY와 유지보수 barrier snapshot은 보류 계획을 제외하고 독립 batch를 검증한다. | 새 오류·보류 파일 변경·보류 계획에 대한 dependency 추가는 게시를 거부한다. 필수 사후 재검증 대상이 보류되면 해당 재검증을 완료로 처리하지 않는다. 보류 오류가 있으면 archive cache의 빠른 채택 대신 원격 재검증을 요구한다. |
| Planner 변경 조회·오류 진단 | 기존 numstat helper로 실제 unstaged 내용·mode 변경을 확인하고 staged·untracked·기준 revision 변경을 합친다. 새 오류에 기존 hold가 없으면 원래 catalog 진단을 보존한다. | 오래된 index stat만으로 정상 파일을 scope 위반으로 판단하지 않는다. NUL 경로·binary 변경을 보존하고 ID·파일명 오류는 기존 진단으로 교정할 수 있다. |
| 유지보수 revision / 알림 완료 gate | 전체 catalog 성공과 실행 view를 구분해 source revision을 확인한다. 보류 진단을 compact catalog와 작업 대기 사유에 포함하며 모든 PR이 Merge돼도 오류를 제품 완료로 간주하지 않는다. | 실제 원인이 교정되기 전 완료 선언을 보류한다. 다른 프로젝트의 선택·이력·진단에는 쓰지 않는다. |

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증: 격리·소비자 회귀 27개, 기존 catalog·선택·유지보수 회귀 29개, 알림 회귀 2개 통과.
- [x] Manager 검증 피드백 보정: metadata ID 진단, Merge 소비자 fixture 계약, publication assertion, 새 Planner 오류 진단과 내용 기준 변경 조회를 수정하고 해당 최소 unit 15개 및 기존 Planner 보류 경계 2개 통과.
- [x] Reviewer 소비자 경계 보정: 일반·유지보수 Planner의 복합 오류 검증을 통일하고 유지보수 snapshot 격리와 Planner 점유 중 실행 재개 보류·해제·CAS 경쟁을 검증함. 새 unit 5개와 기존 경계 unit 8개 통과.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- graph·채택·선택 경계를 함께 변경하므로 complexity high, 잘못된 독립성 판단의 영향으로 risk high다. 원격 삭제와 dependency 목표 복구는 G-164로 분리한다.

- 기존 parser·graph validator, catalog transaction/CAS, runtime 상태 전이와 scoped gate를 재사용했다. graph 오류 판정을 공유하고 보류 실행 처리는 별도 repository 메서드로 분리했다. 새 루트 항목·AGENTS·PRODUCT·Requirement·ARCHITECTURE·PLANS 변경은 없다.
- targeted 검증은 `ScopedCatalogParsingTest`, `ScopedCatalogRefreshTests`, `ExecPlanCatalogSnapshotTest`, `CatalogCacheAndCasTests`와 관련 개별 reconciliation·dependency·maintenance·notification unit case로 제한했다. 초기 fixture의 `ever_started` 불일치와 활성 실행 unique 제약을 확인·수정했고 같은 명령의 최종 재실행은 통과했다. 알림 테스트는 기존 `support` import에 맞게 `tests`를 PYTHONPATH에 추가한 후 같은 명령으로 통과했다.
- 기존 Linux verify 스크립트가 전체 검증 runner를 실행하며 새 unit case를 포함한다. Worker는 전체 verify·integration·end-to-end·전체 lint/build/type check, commit·push·PR 생성을 실행하지 않았다.
- Manager가 전달한 verify 진단을 읽어 기존 ID·dependency 오류 메시지와 typed catalog 결과 계약을 보존했다. Planner scope 실패는 read-only name-only diff가 복사 fixture의 오래된 index stat까지 반환하는 경계였으므로 기존 `_unstaged_git_paths`를 재사용했다. 외부 Git·Agent·프로젝트 전체 흐름을 실행하는 검증은 Manager에 남기고, 변경한 composition의 실제 내용·binary·staged·untracked 경로와 새 Planner 오류를 fake port unit으로 직접 검증했다. 이번 보정의 targeted 실행은 17개 통과했다.
- Worker 결과 검증 피드백은 통과 보고 뒤의 `실패 없음` 문구가 기존 엄격한 실패 판정에 해당한 것이었다. 같은 최소 unit 명령을 다시 실행하여 15개·2개 통과를 확인하고, 명령·PASS·종료 코드만 담은 최종 증거가 기존 `parse_worker_result` 계약을 통과함을 확인했다. 결과 검증기는 변경하지 않았다.
- Reviewer 지적 세 경계에 공통 보류 snapshot·dependency 검사를 적용했다. 로컬 검사도 원격의 `new_declaration_issues`를 재사용해 기존 risk 오류와 신규 priority 누락·활성 source 누락을 함께 보존한다. 유지보수 후보·재개 context는 보류 ID를 제외하며, 대상 자체가 보류된 필수 사후 재검증은 교정 전까지 기다린다. 실행 재개는 기존 `recovery_slot_available`을 재사용하고 Planner 소유권 변화를 transaction/CAS 및 cache 재검사에 포함했다.
- 이번 최소 unit 최초 실행에서 catalog snapshot adapter의 `isolate_errors` 전달 누락을 수정하고 같은 명령으로 5개 통과했다. 기존 cache 테스트의 class 지정 오류는 해당 검증을 cache/CAS class로 옮겨 기존 assertion을 보존한 뒤 같은 명령으로 8개 통과했다. 새 unit은 실제 Git·Agent 없이 fake port와 메모리 SQLite로 변경된 경계를 검증한다.
