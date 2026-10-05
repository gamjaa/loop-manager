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
| `scoped_blocking` / 선택·execution·PR gate | 별도 catalog hold로 기존 승인·차단을 보존한다. READY·maintenance 후보·실행 전환·PR 충돌 선택은 영향 집합을 제외하고 PR 관측은 유지한다. 실행 전환 transaction 안에서도 gate를 확인한다. | 보류한 실행 단계와 세션·검증·실패 이력을 보존하고 교정 뒤 실행 자리가 비면 원래 단계로 복원한다. 독립 승인·차단은 해제하지 않는다. |
| `ProjectCatalogRefresh` / legacy 이관 / Planner | 검증된 view를 소비자들이 재사용한다. 이관 전 새 선언의 엄격한 metadata/source 검사를 적용한다. mutable READY는 보류 계획을 제외하고, 독립 batch 게시도 기존 오류·파일 hash·Git 변경 없음과 영향받지 않는 dependency를 검증한다. | 새 오류·보류 파일 변경·보류 계획에 대한 dependency 추가는 게시를 거부한다. 보류 오류가 있으면 archive cache의 빠른 채택 대신 원격 재검증을 요구한다. |
| 유지보수 revision / 알림 완료 gate | 전체 catalog 성공과 실행 view를 구분해 source revision을 확인한다. 보류 진단을 compact catalog와 작업 대기 사유에 포함하며 모든 PR이 Merge돼도 오류를 제품 완료로 간주하지 않는다. | 실제 원인이 교정되기 전 완료 선언을 보류한다. 다른 프로젝트의 선택·이력·진단에는 쓰지 않는다. |

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증: 격리·소비자 회귀 27개, 기존 catalog·선택·유지보수 회귀 29개, 알림 회귀 2개 통과.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- graph·채택·선택 경계를 함께 변경하므로 complexity high, 잘못된 독립성 판단의 영향으로 risk high다. 원격 삭제와 dependency 목표 복구는 G-164로 분리한다.

- 기존 parser·graph validator, catalog transaction/CAS, runtime 상태 전이와 scoped gate를 재사용했다. graph 오류 판정을 공유하고 보류 실행 처리는 별도 repository 메서드로 분리했다. 새 루트 항목·AGENTS·PRODUCT·Requirement·ARCHITECTURE·PLANS 변경은 없다.
- targeted 검증은 `ScopedCatalogParsingTest`, `ScopedCatalogRefreshTests`, `ExecPlanCatalogSnapshotTest`, `CatalogCacheAndCasTests`와 관련 개별 reconciliation·dependency·maintenance·notification unit case로 제한했다. 초기 fixture의 `ever_started` 불일치와 활성 실행 unique 제약을 확인·수정했고 같은 명령의 최종 재실행은 통과했다. 알림 테스트는 기존 `support` import에 맞게 `tests`를 PYTHONPATH에 추가한 후 같은 명령으로 통과했다.
- 기존 Linux verify 스크립트가 전체 검증 runner를 실행하며 새 unit case를 포함한다. Worker는 전체 verify·integration·end-to-end·전체 lint/build/type check, commit·push·PR 생성을 실행하지 않았다.
