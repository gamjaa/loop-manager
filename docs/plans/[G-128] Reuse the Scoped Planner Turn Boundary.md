---
id: G-128
title: Reuse the Scoped Planner Turn Boundary
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on: []
complexity: medium
risk: high
priority: normal
---

## Purpose

source revision `f9c10ed921ed8fe39757a36c3ca27b77a0539ee8`에서 `planning/assessment_service.py`의 일반 Planner turn, 파일 검토, 관계 확인, 종합, 발견 수집과 resolution이 scope 시작, Agent 호출, session callback 및 반환 session 저장, scope 종료와 성공한 scope 정리를 각각 구현한다. 입력·결과 정책이 다른 여섯 경로가 동일한 실행 절차를 소유하므로 session 저장이나 scope 처리 변경을 여러 곳에 동시에 반영해야 한다. `workspace_service.py`의 충돌 교정에도 같은 절차가 있으며 그 소비는 G-129에서 연결한다. 줄 수가 아니라 반복된 호출 순서와 저장·권한 경계가 근거다.

기존 `_PlanningAgentPorts`와 `PlanningAgentPort`, `PlanningAgentResult`, `PlanningScopeResult`를 재사용해 공통 실행 절차를 한 경계에 모은다. `agents/invocation_service.py`와 `run_agent_with_retries`는 물리 호출·격리·사용량·CLI 재시도를 이미 담당하므로 다시 구현하지 않는다. compact catalog의 비종료 G-066은 Worker prompt, G-112~G-114는 runtime·투영·설정·mount 판정 목표다. 이 중복을 해결할 선행 READY는 없다. G-125/G-127은 이미 Merge된 서비스 분리·일회성 평가 통합이며 새 계획의 대체 입력이나 미완료 작업으로 세지 않는다.

## Acceptance Criteria

- 공통 경계는 필요한 Agent port, repo, 역할, workspace, prompt, session과 저장 callback만 받는다. coordinator, root module 전체 또는 가변 `_PlanningRun`을 넘기거나 전역 상태를 추가하지 않는다. 여섯 평가 경로는 동일한 실제 구현을 호출한다.
- 사용자 동작, 공개 계약과 저장 형식을 보존한다. CLI, `loop_manager` 공개 helper의 서명·patch 동작, 기존 port method, result kind, prompt 및 결과 protocol, SQLite schema·column·JSON과 transaction 경계는 그대로다. provider 정책, 사용량 감사, mutex와 session-unavailable fallback은 기존 호출 경계를 이용한다.
- session 시작 callback과 반환 session의 저장 순서·횟수, 각 unit/page의 독립 session owner 및 재개 identity를 유지한다. callback 오류의 기존 전파·수집 차이를 공통 helper가 흡수하지 않는다. 호출 예외나 scope 거부에서 성공을 기록하거나 scope 증거를 무조건 정리하지 않는다.
- scope가 성공했을 때만 기존 시점에 정리한다. 일반 turn의 `agent_result_snapshot` 보존과 Agent 종료 오류 처리 우선순위, 각 유지보수 unit의 scope 오류 우선 처리 및 transient/usage/configuration outcome은 호출 측 정책으로 유지한다. 오류 분류와 retry 횟수를 하나의 공통 정책으로 합치지 않는다.
- 원본 재조회, source hash·cycle revision·page key 대조, pending-result marker, unit receipt 및 publication 이후 성공 기준 확정은 각 평가 책임에 남긴다. barrier·revalidation·mutable READY 권한을 확대하지 않는다.
- 새 runtime 파일·설정·DB 저장 필드를 만들지 않는다. `agent_ports.py`에 공통 실행 경계를 두되 서비스에서 사용할 때 생기는 순환 import를 피하도록 기존 result/context 타입은 `planning_contracts.py`에서 직접 참조한다. standalone import에 자원 효과가 없어야 한다.

## Implementation

1. 아래 원본 코드맵과 현재 테스트를 확인해 여섯 경로별 호출 event 순서, callback owner, 오류 우선순위와 성공 기록 시점을 대응시킨다. fake Agent port의 기존 관측 기능을 재사용한다.
2. 기존 `agent_ports.py`에 작은 scoped-turn helper를 두고 기존 result 두 개를 명시적으로 반환한다. scope 검증·provider 호출의 본문은 기존 port에 위임한다. phase별 DB 전이, prompt 생성과 결과 validation은 helper 밖에 둔다.
3. `assessment_service.py`의 여섯 경로만 교체한다. 종합 page의 session 오류 수집 closure, 일반 결과 snapshot 저장과 source 재검증을 보존한다. workspace 충돌 처리의 이동·연결은 G-129에 맡겨 해당 파일을 반복 수정하지 않는다.
4. 관련 unit test에 event 순서와 실패 경계 회귀를 추가하고 각 실제 소비자에서 helper가 사용되는지 확인한다. root export 또는 포괄적인 새 service facade는 추가하지 않는다.

### 원본 코드맵

| 파일 / 원본 SHA256 | 책임·진입점 및 직접 관계 |
| --- | --- |
| `planning/assessment_service.py` / `e108c4fcd6646ef03e1c5e1aa8c1bff9c46eb43a39256c936b33de2bc8c82805` | `_run_planner_turn` 27행, `_review_one_file` 299행, `_reconcile_one_relation` 362행, `_synthesize_one_page` 422행, `_collect_findings_unit` 478행, `_resolve_one_page` 515행. 모두 Agent port를 직접 호출한다. coordinator가 소비하며 `tests/test_planning_assessment.py`·`tests/test_planning_recovery.py`가 검증한다. |
| `planning/agent_ports.py` / `6155d01f63702fbf78ee68f5e53c5c1ae4570186b2219dc1fc96be9f703f7216` | `_PlanningAgentPorts.begin_scope/run_planner/finish_scope/clear_scope`; 물리 호출·mutex·사용량 감사는 기존 adapter에 위임한다. root의 named-effect 구성이 소비한다. 현재 result 타입 import가 coordinator의 호환 export를 경유하므로 새 소비 연결 때 계약 모듈로 바꾼다. |
| `tests/test_planning_assessment.py` / `c32ad38b38cf4dbe76c0ed8d6413faa16f5594a746df82f51d26c40b58d8cea0` | 독립 검토·관계·page session과 변조 report의 호출 전 거부를 검증한다. `tests/support/planning_case.py`의 fake ports를 재사용한다. |

표의 `planning/` 경로는 모두 `loop_manager_internal/planning/` 아래다. SHA와 인용 행은 평가 revision의 원본 identity이며 향후 Worker 검증 결과를 대신하지 않는다.

### 루트 배치 확인

이 계획과 G-129은 기존 내부 package와 test 경계를 사용한다. 추적 루트 13개의 목적지와 소유권은 모두 현재 경로 유지다. 새 루트 파일이나 자동 이동·삭제는 없다.

| 루트 항목 | 실제 참조·사용자 경로 및 소유 책임 |
| --- | --- |
| `.agents/` | 명시적 copy-project-files 스크립트가 배포하는 Specification Skill 탐색 경로; 프로젝트 지침 도구 |
| `.gitignore` | Git의 표준 탐색 파일; config·venv·파생 cache 제외 |
| `AGENTS.md` | registration 서비스의 원격 공통 지침 검증, Agent 자동 로딩과 copy 스크립트; 사용자/공통 지침 |
| `README.md` | 사용자 초기 설정·등록·실행 진입 안내와 제품/설계 링크; 사용자 안내 |
| `config/` | `--config` 기본값, bootstrap의 source 기준 절대 경로 전달, managed_config; 운영 설정 |
| `docs/` | registration·catalog·planning이 읽는 명세, 계획·템플릿과 사용자 입력; 문서 소유자 |
| `loop_manager.py` | run launcher와 README의 직접 CLI, bootstrap runtime 자식 및 공개 helper 소비; 진입점/구성 |
| `loop_manager_internal/` | root와 package의 명시적 import, 두 verify의 compile 목록; 내부 구현 |
| `requirements.txt` | 두 setup 스크립트의 pip 입력; 설치 의존성 계약 |
| `run.ps1` | README/외부 Windows scheduler에서 source bootstrap 실행; 사용자 launcher |
| `run.sh` | README/외부 Linux scheduler에서 source bootstrap 실행; 사용자 launcher |
| `scripts/` | README의 명시적 setup/copy 경로와 Manager의 OS별 verify 탐색; 실행 도구 |
| `tests/` | 두 verify의 unittest discover와 Worker targeted module 경로; 회귀 검증 |

`.git` 제어 경로, 비추적 `.aws/`·`.codex/`와 생성물은 이동 대상으로 삼지 않는다. 기존 루트 정책을 바꾸거나 불명확한 소유권을 추정하지 않는다.

## Validation

- Worker는 추가할 `tests.test_planning_assessment.ScopedPlannerTurnTests`의 targeted unit test와 직접 수정된 소비자의 최소 case만 실행한다. fake Agent/state/event ports를 사용하며 실제 provider, Git mutation이나 전체 suite를 실행하지 않는다.
- 공통 helper의 신규·재개 session, 시작 callback/반환 session 저장, 호출 예외와 callback 실패, scope 성공/실패 및 result snapshot 전달을 확인한다. scope 거부 후 clear/receipt/success baseline이 발생하지 않음을 검증한다.
- 각 여섯 소비 경로에서 session owner, 일반 turn과 unit turn의 오류 우선순위, transient/usage/configuration 결과, source drift 재조회와 pending marker를 비교한다. 기존 `PlanningAssessmentTest.test_assessment_review_unit_precedes_planner_and_preserves_session`, `test_assessment_reconciles_one_relation_before_final_planner`, `test_final_decision_waits_for_one_resolution_without_rerunning_planner` 중 수정 경계에 해당하는 case를 선택한다.
- Manager 전체 verify는 일반·유지보수 planning, legacy/pending 평가 재개, source 변조 거부, publication 전 성공 기준 보존, session fallback·격리와 standalone package import를 확인한다. 기존 실제 Git·CLI fake 회귀와 두 OS의 검증 범위는 줄이지 않는다. Reviewer는 callback/저장 순서와 scope 증거 보존을 읽기 전용으로 검토한다.

## Progress

- [x] 지정 revision의 중복 절차·기존 helper·소비자와 테스트 확인
- [ ] 공통 scoped-turn helper와 여섯 평가 소비자 연결
- [ ] 관련 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-03: 유지보수 barrier 호출에서 확인한 현재 구조만 대상으로 한다. 새 일반 Planner 역할, R-033의 Worker prompt 축약과 provider 정책 변경은 포함하지 않는다.
- 공통 실행 절차와 호출 측의 서로 다른 오류·상태 전이 정책을 분리한다. 사용자 동작이나 승인·저장 계약을 변경해야 한다면 이 계획의 리팩터링으로 처리하지 않는다.
- G-128는 평가 소비자를 독립 검증·Merge할 수 있다. G-129이 이 경계를 재사용해 충돌 교정을 분리하며 DAG 말단은 G-129 하나다.

- 계획 번호는 프로그램의 active/archive 자동 카탈로그에서 다음 연속 번호를 사용한다. `docs/plans/manual/`의 수동 복구 기록은 자동 카탈로그 밖의 감사 문서이며 개정·삭제하거나 실행 입력으로 사용하지 않는다.
