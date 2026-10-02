---
id: G-063
title: Safe Internal Module Package Migration
sources:
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-051
  - G-060
complexity: high
risk: high
priority: critical
---

## Purpose

G-060의 루트 inventory에서 내부 구현으로 확인한 모듈은 루트 고정 탐색 대상이 아니다. 현재는 flat import와 `loop_manager.py`의 `ROOT`, detached runtime 실행 및 테스트 monkeypatch가 경로에 결합돼 있으므로 import graph에 따라 안전하게 하위 패키지로 옮긴다.

## Acceptance Criteria

- G-060의 근거 있는 inventory에서 이동 대상으로 판정한 내부 모듈만 하위 패키지로 옮긴다. 필수 `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `loop_manager.py`, launcher, manifest, config와 verify 지정 경로를 유지한다.
- 사용자 CLI, 외부에 노출된 `loop_manager.py` 호출 계약, config·state 위치, 원격 runtime 선택, Git/PR 동작과 SQLite 저장 형식은 보존한다. 내부 import 경로와 그 경로를 직접 참조하는 테스트는 함께 갱신한다.
- tracked 파일 이동은 Git에서 추적 가능하고 사용자 파일·생성물의 삭제나 자동 이동이 없다. Windows·Linux launcher와 전체 verify는 새 경로에서 정상 동작한다.
- 이동 뒤에도 루트에 남는 항목은 G-060 inventory의 구체적인 탐색 또는 사용자 진입 근거가 있다.
- 하위 패키지는 runtime·저장·Agent·Git/workspace·planning·execution·maintenance·notification 책임을 구분한다. 이동으로 진입점 `loop_manager.py`의 역방향 import, 전체 모듈 주입이나 순환 의존성을 새로 만들지 않는다. 현재 그런 경계를 사용하는 adapter는 후속 G-120~G-125에서 제거할 대상으로 대응표에 기록한다.
- G-119~G-127에서 추가로 분리할 구현·테스트의 목적지를 inventory와 연결하고, 기존 모듈 이동과 후속 책임 분리의 범위를 중복하지 않는다. `full_assessment.py`는 후속 정리까지 실행에 필요한 소비 경로를 유지하며 최종 삭제를 피하기 위한 루트 wrapper를 새로 만들지 않는다.

## Implementation

- G-060 inventory의 import graph에서 leaf부터 내부 모듈을 package namespace로 옮기고, `loop_manager.py`의 import와 필요한 공개 re-export만 보존한다. G-051의 `agent_scope_decision.py`는 `loop_manager.py` 자료형을 실행 시 import하지 않는 경계와 직접 테스트의 import 경로를 함께 점검한다. 모든 내부 모듈에 root wrapper를 남겨 루트 정리 목적을 무효화하지 않는다.
- detached runtime child, 직접 실행 launcher, resource lookup, 테스트 patch target 및 Linux·Windows verify의 모듈 목록을 같은 변경에서 갱신한다. 현재 두 verify 스크립트의 모듈 목록 차이도 이동 대상의 검증 누락 없이 정리한다.
- 파일 이동과 함께 관련 없는 책임 재설계나 storage migration을 수행하지 않는다.
- 루트 진입점과 이름이 충돌하지 않는 내부 namespace(예: `loop_manager_internal/`)를 G-060의 참조 조사로 확정하고 이후 계획에서 같은 경계를 사용한다. 패키지 `__init__.py`는 명시적 export만 담당하며 여러 service를 초기화하거나 전체 구현을 모으지 않는다. `ROOT`와 resource 위치는 저장소 루트 identity로 전달하고 이동한 모듈의 `__file__` 부모를 새 루트로 오인하지 않는다.
- 기존 공개 helper는 확인된 호출 계약에 필요한 명시적 re-export로 보존한다. repository 내부 테스트는 실제 소비하는 모듈의 patch target을 갱신하며 테스트용 전역 patch forwarding·`sys.modules` 교체·임의 attribute proxy를 추가하지 않는다. 새 domain service와 fixture 분리는 후속 계획에서 수행한다.

### 확정 이동 대응표 (2026-10-02)

G-060의 root inventory와 import/test 대응표를 기준으로, 구현 시작 revision
`e202ad8b101fd06b7c0dbb942c298156fc08b887`의 추적 루트 Python 모듈 73개와 실제 참조를 다시 대조했다.
`loop_manager.py`를 제외한 내부 모듈 72개만 아래 위치로 이동한다. 각 항목의 원래 경로는
`<모듈명>.py`, 새 경로는 `loop_manager_internal/<경계>/<모듈명>.py`다.
상태 저장은 기존 repository 모듈을 `storage/`에 모으고 planning coordinator는 `planning/`에 둔다.
이것은 G-060의 책임별 후보를 확정한 경로이며 함수 분리·transaction·저장 형식 변경은 아니다.

| 경계 | 이동 모듈 | 후속 책임 분리 |
| --- | --- | --- |
| `agents/` | `agent_codex_sandbox`, `agent_invocation`, `agent_provider_usage`, `agent_sandbox_exec`, `agent_scope_decision`, `agent_write_isolation` | G-121 |
| `recovery/` | `blocked_checkpoint`, `blocked_plan_reconciliation`, `blocked_remote_observation`, `checkpoint_ownership`, `project_blocked_recovery`, `recovery_dispatcher`, `recovery_runtime`, `scoped_blocking` | G-122 |
| `execution/` | `execution_checkpoint`, `execution_handlers`, `execution_revision`, `execution_runtime`, `execution_state`, `publication_resume`, `validation_feedback`, `verify_process`, `worker_outcome`, `worker_session` | G-123/G-124 |
| `catalog/` | `execplan_archive`, `execplan_catalog`, `project_catalog_refresh`, `requirement_archive`, `requirement_archive_recovery` | G-120/G-122 |
| `maintenance/` | `full_assessment`, `maintenance_file_inventory`, `maintenance_file_publication`, `maintenance_file_review`, `maintenance_findings`, `maintenance_git`, `maintenance_orchestration`, `maintenance_planning`, `maintenance_reconciliation` | G-125/G-127 |
| `planning/` | `planning_catalog`, `planning_runtime` | G-125 |
| `git/` | `managed_rebase_adoption`, `managed_rebase_evidence`, `managed_rebase_observation`, `managed_rebase_publication`, `managed_rebase_runtime`, `managed_rebase_validation`, `manager_git_journal`, `rebase_block_reconciliation`, `rebase_correction`, `rebase_file_evidence` | G-122 |
| `config/` | `managed_config` | G-120 |
| `documents/` | `document_drafts`, `document_publication`, `guidance_approval`, `reviewer_specification` | G-125/G-126 |
| `notifications/` | `notifications`, `work_exhaustion` | G-126 |
| `runtime/` | `project_registry`, `runtime_scope` | G-120/G-123 |
| `quality/` | `quality_guidance` | 분리 없음: 공통 prompt 상수 |
| `storage/` | `agent_invocation_state`, `blocked_checkpoint_state`, `execplan_archive_state`, `execplan_catalog_state`, `execution_checkpoint_state`, `maintenance_file_ledger`, `maintenance_state`, `rebase_correction_state`, `recovery_state`, `requirement_archive_state`, `runtime_state`, `validation_feedback_state` | G-120/G-122/G-125/G-126 |

실제 production 소비자는 `loop_manager.py`의 기존 명시적 import와 G-060 import graph의 내부 module들이다.
같은 모듈명 binding을 유지하면서 import source만 위 namespace로 바꾼다. 직접 테스트 import와
`mock.patch("<module>.<symbol>")`는 실제 소비 모듈의 새 경로로 갱신하며, `loop_manager.<helper>`와
`loop_manager.<module>` 공개 호출은 유지한다. 테스트용 patch forwarding, attribute proxy,
`sys.modules` 교체나 루트 wrapper는 추가하지 않는다. 모든 이동 모듈과 진입점을 기존 AST와
대조한 결과 import source 외 실행 코드 차이가 없다. 빈 package initializer는 service를 import하지 않는다.

| 유지하는 루트 항목 | 실제 참조·사용자 경로·소유권과 목적지 |
| --- | --- |
| `.agents/`, `docs/`, `scripts/`, `AGENTS.md` | 추적 공통 지침·제품 문서·실행 도구. 등록, `ROOT` resource lookup과 공통 파일 복사 script의 고정 탐색 경로이므로 그대로 유지한다. |
| `loop_manager.py`, `run.sh`, `run.ps1` | 추적 사용자 CLI. launcher의 source `.venv` interpreter와 detached runtime의 exact SHA 진입점이므로 그대로 유지한다. |
| `config/`, `requirements.txt`, `.gitignore` | 추적 설정 예시·설치 manifest·Git 제외 규칙. source config는 절대 경로로 child에 전달하며 설치 script와 Git이 기존 위치를 사용한다. |
| `README.md`, `tests/` | 추적 사용자 설치/실행 안내와 `unittest discover -s tests`의 테스트 탐색 경로. G-119는 이 경로 안에서 fixture·테스트 책임을 분리한다. |
| `loop_manager_internal/` | 이번에 추가한 추적 내부 source namespace. 루트 launcher의 기본 Python module 탐색으로 import되며 `loop_manager.py`와 이름이 충돌하지 않는다. 루트에 개별 내부 module이나 wrapper를 남기지 않는다. |
| `.git`, `.codex/`, `.aws/` 및 ignored 생성물 | Git/workspace·provider·사용자 소유 경로. 추적 구현 이동 대상이 아니며 불명확한 사용자 설정·생성물은 읽거나 자동 이동·삭제하지 않는다. |

`ROOT`는 계속 루트 진입점의 `__file__`에서 계산한다. 이동 모듈은 repository identity를 기존
인수/port로 받으며 자기 `__file__` 부모를 repository root로 사용하지 않는다.
`agents/agent_codex_sandbox.py`의 standalone `-I` probe는 인접한 `agent_sandbox_exec.py`를
사용하므로 두 파일을 함께 옮겨 경로 계약을 유지한다. detached child는 여전히
`runtime.path / "loop_manager.py"`를 실행하고 source config·state·선택 SHA 전달은 유지한다.
launcher와 공통 파일 copy script는 고정 경로를 그대로 사용하므로 수정이 필요하지 않다.
두 verify script의 compile 목록은 같은 72개 모듈, package initializer와 루트 진입점을 포함하도록 맞추며,
전체 unittest discovery와 실패 시 nonzero 반환을 유지한다.

| 현재 유지한 결합/fixture | 후속 계획과 목적지 |
| --- | --- |
| 루트 bootstrap/config/SQLite schema와 `_PlanningStatePorts`, `_ExecutionLifecyclePorts`의 조립 | G-120/G-123 → `runtime/`, `config/`, `storage/`; CLI/public helper와 root identity를 보존한다. |
| 루트 Agent invocation·scope adapter와 기존 Agent 테스트 | G-121 → `agents/`; `agent_scope_decision`의 루트 자료형 import는 `TYPE_CHECKING` 아래에만 남으며 실행 시 역방향 import는 없다. |
| `managed_rebase_runtime.build_service`, `project_blocked_recovery` 및 루트 `sys.modules[__name__]`로 전달하는 기존 Git/recovery ports | G-122 → `git/`, `recovery/`; workspace/publication/ownership adapter의 전체 module 주입을 제거한다. 이번 이동에서 추가하지 않는다. |
| `_ExecutionLifecyclePorts`의 `SimpleNamespace` effect 조립과 `execution_handlers`의 단계별 책임 | G-123/G-124 → `execution/`; 기존 실행 순서·복구 token·명시적 ports를 보존하며 책임 분리는 후속 변경에서 수행한다. |
| `_PlanningLifecyclePorts`와 `planning_runtime`의 평가·계획·검증·게시 조정 | G-125 → `planning/`, `maintenance/`; file-review source identity·publication transaction을 보존한다. |
| `notifications`의 저장·렌더링·전송, `work_exhaustion`의 완료 이벤트 | G-126 → `notifications/`, `storage/`; outbox/event identity를 보존한다. |
| 큰 공통 fixture와 테스트 책임 (`test_loop_manager`, execution/planning/recovery tests) | G-119 및 G-120~G-126 → 기존 `tests/` 하위 책임별 module/shared fixture. 이번에는 import/patch target과 이동에 필요한 bytecode 복사 fixture만 갱신한다. |
| `full_assessment`의 일회성 inventory·prompt·result 검증 | G-125/G-127 → 현재 `maintenance/full_assessment.py` 소비 경로를 유지하고 대체 검증 뒤 제거한다. 임시 보고서나 삭제 회피용 wrapper는 추가하지 않는다. |

## Validation

- `tests/test_internal_packages.py`는 각 이동 모듈의 cold import와 실행 시 root 역방향 import 부재, package 초기화, 공개 module/helper identity, 복사된 runtime의 CLI·root resource·외부 config, 두 verify compile 목록을 검증한다. `tests/test_catalog_checkpoint_wiring.py`는 기존 공개 module binding과 catalog/checkpoint compile 경로를 검증한다.
- 기존 `test_loop_manager.AgentScopeValidatorTest.test_planner_import_bytecode_is_ignored_without_hiding_scope_violations`는 complete package 복사와 새 namespace import로 fixture를 갱신하며 ignored bytecode와 범위 위반 판정을 유지한다. Agent native probe의 직접 파일 경로와 공통 파일 copy의 고정 resource도 기존 test case에 assertion을 추가한다.

- Worker는 이동한 모듈의 import·CLI·resource lookup targeted unit test와 `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_managed_configuration.py`의 직접 관련 targeted unit test만 실행한다.
- 동일 SHA runtime 자식의 import, launcher 경로, 공통 파일 복사, root resource lookup, 공개 helper와 테스트 patch target을 검증한다.
- 전체 verify에서 모든 기존 테스트, Python module compile 및 Linux·Windows verify 스크립트의 새 모듈 목록이 통과해야 한다.

## Progress

- [x] G-060 inventory의 내부 모듈 72개를 package로 이동
- [x] runtime·launcher·resource·공개 binding·검증 경로 targeted test 갱신
- [x] 후속 책임 분리 목적지·공개 export·순환 의존성 대응표 확정

Worker targeted 결과 (2026-10-02, 모두 통과):

| 선택한 unit test 범위 | 결과 |
| --- | --- |
| `test_internal_packages.InternalPackageMigrationTests`, `test_catalog_checkpoint_wiring.CatalogCheckpointWiringTest` | 7 tests OK. 새 package 초기화 검증 보완 뒤 같은 선택 명령으로 재확인했다. |
| `test_agent_scope_decision` module, `test_agent_invocation.NativeWriteIsolationTests.test_direct_launch_preserves_sessions_and_excludes_runtime_from_tool_writes`; `test_managed_configuration.ManagedConfigurationAndProjectRegistryTest`의 `test_registration_files_require_active_documents_and_hash_bound_agents_approval`, `test_copy_project_files_distributes_document_quality_guidance`; `test_origin_isolation.OriginIsolatedRegistrationAndExecutionTest`의 `test_bootstrap_holds_lock_around_runtime_and_normalizes_external_config`, `test_runtime_child_does_not_reacquire_process_lock`, `test_launchers_only_start_the_python_bootstrap` | 30 tests OK. |
| `test_loop_manager.AgentScopeValidatorTest.test_planner_import_bytecode_is_ignored_without_hiding_scope_violations` | 1 test OK. |

각 선택은 `PYTHONPATH=tests python3 -B -m unittest -v`에 위 module/class/case를 전달했다.
72개 모듈의 cold import는 첫 행의 unit test 내부에서 확인한다. 원본과 이동 코드의 AST 및
경로만 갱신한 기존 test 53개의 AST 대조는 읽기 전용 diff 조사이며 전체 compile/lint/test 실행이 아니다.
전체 verify·integration/e2e·build·type check·commit·push·PR 생성은 실행하지 않았다.
Loop Manager 전체 검증과 Reviewer/사용자 Merge는 아직 남아 있으며 ExecPlan 완료 상태를 선언하지 않는다.

## Decision Log

- 내부 모듈을 옮기는 순서는 의존성 graph를 따른다. 공개 launcher와 고정 문서 경로는 이동 대상이 아니다.
- G-051은 `agent_scope_decision.py`를 새 루트 모듈로 추가했다. `loop_manager.py`와의 단방향 import 경계 및 테스트 fixture를 이동 영향 범위에 포함한다.
- 2026-10-02: 사용자 요청으로 미착수 계획을 보완한다. 패키지 이동은 G-063, 큰 테스트 분리는 G-119, 기능별 구현 분리는 G-120~G-126, 임시 평가 코드 정리와 남은 발견 확인은 G-127이 담당한다. G-063은 경로 이동과 그에 필요한 import·resource·검증 수정을 끝낸 뒤 Merge하며 모든 책임 분리를 한 PR에 합치지 않는다.
- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 우선하므로 priority를 `critical`로 높인다. G-060의 선행 inventory와 후속 리팩터링 순서는 유지한다.
- 2026-10-02: 내부 namespace는 `loop_manager_internal/`로 확정했다. 기존 책임의 이동만 수행하며 루트 CLI·resource identity와 공개 helper/module binding은 유지한다. 기존 module 주입 adapter, 큰 fixture 분리와 임시 assessment 제거는 위 대응표의 G-119~G-127 범위로 남긴다. 두 verify compile 목록의 차이는 모든 이동 모듈을 포함하는 동일 목록으로 해소했다.
