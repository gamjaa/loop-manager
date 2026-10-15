---
id: G-291
title: targeted 실행 기록의 호출 결합과 중단 후 재수집
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

물리 Worker 호출의 targeted 기록을 해당 repository·ExecPlan·invocation에 결합하고 Manager 중단 후에도 미반영 결과를 재수집한다. 현재 `invocation_service.run_agent()`는 메모리의 `prepared_launch.attempt`를 `finally`에서 한 번 읽는다. `reconcile_interrupted()`는 미종료 invocation을 정리하지만 그 폴더를 다시 수집하지 않는다. 테스트도 finally가 실행되는 KeyboardInterrupt와 이미 SQLite에 들어간 결과의 재개만 확인한다. 기록 생성 후 finally 이전 종료나 수집/저장 실패에서는 실제 결과 파일이 남아도 다음 호출이 다른 폴더를 사용해 누락될 수 있다.

## Acceptance Criteria

- R-055/PRODUCT: 같은 Worker 세션 내부 재실행·CLI retry·fallback과 Manager 재시작에서 확인된 개별 실행 결과를 잃지 않고 한 번만 반영한다. 기록 폴더·실행 ID를 물리 invocation, repository·ExecPlan·역할·현재 소유 작업에 지속적으로 연결하고 다른 작업/호출의 자료를 가져오지 않는다.
- 완료한 개별 결과가 파일에 남고 SQLite 반영 전 Manager가 중단돼도 재개 경계에서 재관측한다. 일부 줄·실행 미완료·파일 부재를 테스트 실패나 성공으로 합성하지 않는다. 기록 경로·소유권·종료 근거가 불확실하면 그 자료와 해당 작업만 보존·대기하며 다른 작업을 막지 않는다. 살아 있는 writer나 종료 불명 자원을 임의 정리하지 않는다.
- 수집·해석·SQLite 저장 실패의 미소비 원문과 재수집 근거를 보존한다. 재시도에서 기존 중복/상충 검사와 원자 관측을 유지한다. 불확실한 수집을 테스트 세 번 실패나 완료 보고 교정 소진으로 바꾸지 않으며 실제 provider 정책 거부·문서 오류·독립 안전 차단은 기존대로 구분한다.
- 재수집으로 세 번째 실패가 확인되면 새 Worker·CLI retry·verify 진입 전에 기존 `targeted_test_exhausted` 경계에서 처리한다. 차단 확정과 도달 표식 소비는 원자적이고 실행·실패 감사는 남는다. 이미 처리한 기록을 재생해 중복 차단하거나 실패 횟수를 초기화하지 않는다.
- 현재 Codex의 호출별 쓰기 허용 임시 폴더와 프로젝트 Python/runtime runner 분리를 유지한다. 공통 홈·다른 호출·사용자 원본의 쓰기 권한을 넓히지 않고 runtime/모델 교체로 과거 기록 귀속을 잃지 않는다. 기존 파일·DB를 삭제하는 migration이나 비호환 상태 변경은 포함하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/invocation_service.py`의 `observe_launch`·finally 수집과 `agents/agent_invocation.py`의 물리 retry identity를 재사용한다. 프로세스 실행 전에 기록 위치를 영속적으로 결합하고 정상/예외 종료·중단 복구에서 같은 자료를 재관측할 수 있게 한다. Codex 전용 launch 객체가 없는 공급자도 소비할 수 있는 최소 기록 문맥을 두되 실제 Claude 연결은 G-292에 남긴다.
- [ ] 확인한 `loop_manager_internal/storage/agent_invocation_state.py`의 ledger·`reconcile_interrupted()`와 `storage/targeted_test_state.py`의 중복 없는 저장을 연결한다. 기존 scope·호출/프로세스 종료·수동 작업 보호를 재사용하고 필요한 비밀값 없는 결합 정보만 호환되게 보존한다. 광범위한 상태 폴더 탐색으로 경로의 repository·작업 귀속을 추측하지 않는다.
- [ ] 확인한 `loop_manager_internal/verification/targeted.py:collect()`의 경로/크기·identity·완료 줄 검사와 `execution/execution_worker_handler.py`의 호출 전/후 차단·증거 부재 판정을 재수집 경계와 연결한다. 수집 오류를 provider 설정 실패나 rebase 완료 보고 횟수로 오인하지 않도록 `execution_rebase_handler.py`·`recovery/recovery_runtime.py`의 기존 targeted correction/pause를 사용한다. 실제 독립 오류는 숨기지 않는다.
- [ ] 확인한 `tests/test_agent_invocation.py`·`tests/test_targeted_test_streak.py`의 임시 파일·실제 SQLite fixture와 `tests/test_execution_worker_phase.py`의 일반/rebase 경계를 확장한다. 중단 위치를 주입해 durable 재관측을 검사하고 실제 유료 Agent·Git 실행은 추가하지 않는다. PRODUCT·ARCHITECTURE에는 반영한 복구 경계만 통합한다.

## Validation

- Worker는 위 세 모듈에서 변경한 최소 case/class만 실행한다. 파일 생성과 SQLite를 실제로 사용하되 provider·프로세스 종료·Git 관측은 기존 double로 제공한다. finally를 생략한 중단 상태를 저장 fixture로 구성하고 새로운 repository/Manager 실행 전체를 반복하지 않는다.
- 기록 위치 결합 전/후, 완료 JSONL 생성 후 finally 이전, 수집 후 DB commit 이전, DB 반영 후 차단 이전 및 차단 transaction 중단을 각각 대조한다. 재시작 후 확인된 결과는 정확히 한 번 반영되고 세 번째 실패는 다음 Worker 없이 기존 차단으로 이어져야 한다.
- CLI transient retry·session fallback·새 session/후보·같은 호출의 재관측에서 모든 확인된 실행을 동일 작업에 연결한다. 서로 다른 repository·ExecPlan·invocation, 과거/현재 runtime과 수동 실행 중인 writer의 기록이 섞이거나 삭제되지 않아야 한다.
- 부분 마지막 줄·손상 JSON·중복/상충 ID·경로 교체/symlink·기록 부재·읽기 실패와 SQLite rollback을 검사한다. 검증 불가 자료를 실패로 합성하지 않고 기존 결과·원문·미소비 복구 근거를 유지해야 한다. 오류 뒤 정상 재관측도 같은 실행을 중복 집계하지 않아야 한다.
- 일반 Worker와 rebase 교정의 새 근거 부재는 일시 중단이며 기존 문서 보고 한도를 소비하지 않아야 한다. 실제 provider 정책 실패·완료 보고 오류·verify/Review 실패와 독립 사용자 판단은 유지한다. invocation의 물리 종료·사용량 감사가 수집 오류 때문에 미완료로 남지 않는 기존 회귀도 유지한다.

## Progress

- [x] delta의 invocation·runner·저장·Worker/rebase 변경 및 `agent_invocation_state.py`의 재시작 조정을 정적으로 대조했다. DB 반영 후의 재개와 Python finally 종료는 직접 구현된 것으로 구분했다.
- [ ] 물리 호출의 기록 위치 결합·미소비 수집 복구
- [ ] 중단·경합·중복·증거 부재의 최소 unit 회귀

## Decision Log

- G-290의 native 분류와 독립적으로 구현·검증할 수 있다. 그 결과 taxonomy와 기존 결과 스키마를 호환되게 소비하며 source·의존성 변경으로 실제 테스트 실패 한도를 우회하지 않는다. G-189는 두 구현과 Claude 연결 뒤 후속 소비자를 감사한다.
- complexity high: 파일 기록·물리 호출 종료·DB 반영·차단 소비 사이 중단과 살아 있는 writer의 소유권을 일관되게 연결할 판단이 남아 있다. risk high: 누락·오귀속이 필수 차단을 우회하거나 다른 작업을 잘못 차단하며 사후 수동 복구에는 원문과 호출 결합이 필요하다. 미소비 자료·기존 결과를 보존하고 정확한 identity·transaction·중단 회귀로 완화한다. priority high는 실제 실행 근거 보존이라는 필수 무인 계약 때문이다.
- 쓰기 허용 경로·인증 저장·Git 전이·같은 프로젝트 병렬 실행을 넓히지 않는다. 비호환 DB/상태 변경이 필요하면 해당 구현을 멈추고 Architecture Change Policy의 판단을 남긴다. additive 결합의 기술 선택을 사용자 정책의 새 승인으로 만들지 않는다.
- 선택된 unittest version 1·launcher Python 3.11–3.14·기존 필수 compile과 Agent guard를 유지한다. Planner는 구현·테스트·verify를 실행하지 않았으며 기존 KeyboardInterrupt/DB 재개 테스트의 성공을 새 중단 구간의 검증으로 주장하지 않는다.
