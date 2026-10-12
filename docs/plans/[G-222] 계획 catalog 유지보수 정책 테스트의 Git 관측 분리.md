---
id: G-222
title: 계획 catalog 유지보수 정책 테스트의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on:
  - G-224
complexity: medium
risk: medium
priority: normal
---

## Purpose

계획 입력·catalog·Requirement 보관·유지보수 평가의 남은 정책 테스트를 실제 Git 준비에서 분리한다. `test_planning_fingerprint.py`의 no-change 저장/skip gate와 `test_maintenance_state.py`의 비조상·이력 부재 오류 판정은 관측값으로 검증 가능하지만 repository 생성·commit·push를 실행한다. 이미 전환된 batch/FUTURE_IDEAS 검증은 유지한다.

## Acceptance Criteria

- 이 책임 범위의 실제 Git 사용 case를 관측/파싱·판단/저장·실제 Git 효과로 대조하고 관측값만 필요한 조건 조합은 Git 프로세스 없이 실제 운영 판단·SQLite 결과를 검사한다.
- 원격 문서/tree/blob·revision·입력 지문·Merge/dependency·오류·연속 관측 변화는 명시적 mock으로 재현한다. Planner 선택·검증·fingerprint 계산·archive 허용·유지보수 전이를 성공값으로 대체하지 않으며 예상 밖 명령과 불확실한 관측을 실패/보수적 결과로 드러낸다.
- 기존 조건과 assertion의 전환/유지 위치를 테스트 ID로 대응시킨다. 실제 revision tree·planning commit/fast-forward 게시·archive 파일 보존·원격 경쟁·worktree 효과를 확인하는 통합 회귀에는 필요한 실제 Git과 effect assertion 및 mock으로 부족한 이유를 남긴다.
- 새 실제 Git/CLI 조건 조합은 기존 회귀로 충족되지 않는 연결 효과가 있을 때만 추가한다. 효과 확인 뒤 불필요한 재계획·교정·게시를 반복하지 않고 실패·rollback·중단/재개·사용자 결정 보존을 계속 검사한다.
- 전환 case와 유지한 통합 회귀는 전체 verify에 포함된다. skip·timeout 확대·수집 축소·판단 mock으로 비용 감소를 대신하지 않는다.
- 유지하는 실제 Git 회귀는 G-224의 공통 테스트 환경 격리를 사용한다. 사용자 global/system 설정·template의 우연한 영향은 제외하고 설정 상속이 검증 대상이면 필요한 설정을 격리된 입력으로 명시한다. 사용자 설정과 운영 checkout의 환경은 변경하지 않는다.

## Implementation

- 대상은 `tests/test_catalog*.py`, `test_planning*.py`, `test_planner*.py`, `test_incremental_planning.py`, `test_project_catalog_refresh.py`, `test_requirement_archiving.py`, `test_maintenance*.py`, `test_code_index.py`·`test_code_context.py`의 남은 실제 Git 사용 case와 fixture다. 실제 publication/index/파일 효과와 정책 변형을 함께 검사하면 정책 조건 조합과 최소 통합 회귀로 분리한다.
- 기존 `planner_git_observations`, planning_case/FakePorts, catalog·maintenance·코드맵 fixture와 Git adapter를 우선 재사용한다. 실제 SQLite·대상 파일 검증은 유지하고 새로운 전역 cache·runner·범용 mock 서비스는 추가하지 않는다. 공동 fixture 변경은 기존 실제 Git 사용자를 깨뜨리지 않는 선택 경계로 제한한다.
- 실제 Git을 유지하는 대상의 직접 subprocess·운영 Git adapter 호출과 필요한 자식이 G-224의 격리 환경을 이어받는지 대조하고 누락된 소비 경계에 적용한다. 격리 helper를 중복 구현하지 않으며 명시적인 설정 입력과 revision tree·게시·archive·원격 경쟁의 effect assertion을 보존한다.
- assertion 대응과 실제 Git 유지 이유를 대상 테스트 또는 계획의 검증 기록에 남긴다. ARCHITECTURE의 자체 테스트 Git 경계와 AGENTS.md를 따르며 운영 정책·기존 미결정 draft를 변경하지 않는다.

## Git Boundary Mapping

| Test ID | Boundary and input | Preserved assertion or actual Git effect |
|---|---|---|
| `PlanningFingerprintTest.test_planner_no_change_saves_fingerprint_and_skip_gate_uses_it` | FakeGitPort supplies the pinned base and unchanged planning/specification fingerprints; production SQLite storage persists the completed fingerprint. `subprocess.run` and `Popen` reject process use. | The real `PlanningLifecycleCoordinator` returns `FINGERPRINT_SKIPPED`, leaves the stored SQLite values intact, and does not call the Agent. The changed-input planner invocation remains covered by `PlanningSelectionTest.test_remote_input_identity_controls_completed_result_reuse_after_pause`. |
| `BoundedGitChangeContextTest.test_nonancestor_and_missing_baselines_return_diagnostic_errors` → `MaintenanceObservationFailureTests.test_nonancestor_and_missing_baseline_observations_return_diagnostic_errors` | Explicit `_git` results model a present remote, a present sibling baseline with a confirmed non-ancestor result, and an absent baseline. Unexpected observations and subprocesses fail. | The real classification keeps non-ancestor and missing-history diagnostics distinct from operational observation failure. |
| `BoundedGitChangeContextTest.test_oversized_git_failure_is_not_misreported_as_truncation` | Explicit remote/ref/baseline/revision-list observations and a failed bounded-output result marked overflow. Unexpected observations and subprocesses fail. | The real error path reports failure without claiming the context was truncated. |
| `PlanningFingerprintTest.test_planning_input_fingerprint_uses_origin_tree_and_input_boundary`; `PlanningFingerprintTest.test_planning_input_fingerprint_includes_gitlink_commit` | Real Git revision tree, blob and gitlink semantics. | Keep actual tree/blob boundary, detached HEAD versus origin revision, and gitlink object-type effects. |
| `PlanningFingerprintTest.test_requirement_archive_move_does_not_run_planner` | Real active-to-archive file move and published revision. | Keep file preservation and processed revision assertions across the real archive move. |
| `BoundedGitChangeContextTest.test_normal_empty_and_bounded_remote_ranges_do_not_change_worktree`; `BoundedGitChangeContextTest.test_git_capture_bounds_actual_stdout_and_stderr_without_temp_spooling` | Real commit graph, paths, worktree and bounded process pipes. | Keep ordered range/path results, byte/commit limits, unchanged HEAD/index/worktree, and actual stdout/stderr capture behavior. |

실제 Git을 사용하는 대상 class에는 `tests.support.git.isolated_git_test`를 적용해 seed·직접 subprocess·운영 adapter와 필요한 자식이 같은 격리 환경을 상속하게 했다. global/system 설정과 template은 제외하고 저장소 local 설정은 보존한다. 나머지 테스트의 현재 Git effect assertion은 변경하지 않았다.

## Validation

- Worker는 변경한 최소 targeted unit case를 실행한다. no-change/관련 입력 변화의 Planner 호출·SQLite 지문, 비조상/이력 부재/명령 오류 구분, catalog/draft/dependency와 유지보수 실패 시 보존을 실제 판단으로 확인하고 해당 unit에서 예상 밖 subprocess를 거부한다.
- 전환한 정책 case는 예상 밖 process를 거부하는 경계와 기존 Git 계측으로 Git 실행 0회를 확인한다. 변경한 실제 Git 소비 경계는 전달 환경과 명시적인 설정 상속의 최소 unit case로 확인하고 공통 격리 자체의 hook·서명·template 조건 검사는 G-224의 회귀를 재사용한다.
- Manager 전체 verify는 planning 게시·원격 경쟁·Requirement/archive·실제 source inventory 회귀와 전환 case를 함께 통과해야 한다. 전환 전후 assertion 대응과 수집 범위 보존을 Review한다.
- 변경 전후 영향 모듈과 필수 연관 회귀를 같은 환경·대상 집합·Python/Git 버전·계측/캐시 조건·병렬도에서 반복 측정해 중앙값을 비교한다. revision·실제 worker 수·CPU/메모리 제한·동시 부하·임시 경로와 Git 호출 수·누적 시간·준비/snapshot·모듈 경과 시간을 남긴다. 기존 전체 기준 기록과 Manager 최종 전체 verify 근거를 연결하며 국소 비교마다 전체 suite를 반복하지 않는다. 누적 비용을 전체 경과 시간과 더하거나 모듈 감소를 전체 verify 절감으로 환산하지 않는다.

## Progress

- [x] 2026-10-08 f3ae228의 R-080 전문·설계 경계·기존 관측 fixture·변경 diff와 위의 남은 실제 Git 정책 case를 정적으로 확인했다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-10 사용자 요청으로 갱신된 R-080의 Git 환경 격리·Git 실행 0회·비용 측정 조건을 대조하고 G-224 소비 범위와 선행 Merge를 문서에 반영했다. 구현·테스트·성능 측정은 하지 않았다.
- [x] 2026-10-12 case 대응·관측 mock 전환과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge
- [ ] 같은 환경·범위의 Git 호출·모듈 경과 비용 전후 비교와 Manager 전체 verify 근거 연결

## Decision Log

- complexity medium: 기존 경계에서 여러 관측·저장·오류 case를 분리하며 운영 재설계는 없다. risk medium: 잘못된 대체는 계획/보관/유지보수 회귀를 가릴 수 있어 실제 판단·대응·필수 통합 검증을 유지한다. 테스트 코드 rollback과 기존 회귀로 복구할 수 있으며 priority normal이다.
- 2026-10-08 초기 범위에서는 직접 전환된 catalog_batches·planner_future_ideas의 정책 case를 소급 계획하지 않고 G-223의 실행/수동 작업과 G-224의 runtime/격리/작업 공간 테스트를 선행 구현이 필요 없는 별도 범위로 정했다.
- 2026-10-10: 추가된 공통 Git 환경 격리 구현은 G-224가 맡고 이 작업은 실제 Git 소비 경계에 적용한다. 공통 계약을 사용하기 위해 `depends_on`에 G-224를 추가하며 G-223은 선행으로 요구하지 않는다. ID·경로·기존 수행 기록과 complexity/risk medium·priority normal을 보존한다. 비용 비교의 환경 조건은 R-080이 참조하는 R-065 기준을 따른다.
