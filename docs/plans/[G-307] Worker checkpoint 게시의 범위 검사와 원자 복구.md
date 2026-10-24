---
id: G-307
title: Worker checkpoint 게시의 범위 검사와 원자 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-093] 원격 작업 브랜치로 새 환경에서 이어하기.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-303
complexity: high
risk: high
priority: high
---

## Purpose

직접 구현된 checkpoint별 범위 검사·정확한 lease 게시·원자 완료를 재사용해 G-303의 제한된 DB 요청 재시도를 게시 상태 확정에 연결한다. 잠금 경합 뒤 현재 실행/소유권·checkpoint를 다시 준비하되 완료한 commit·push·외부 관측을 재실행하지 않는다.

현재 `git/publication_service.py:push_worker_checkpoint_commits()`는 각 immutable commit을 검사하고 intent와 완료를 짧은 snapshot/CAS transaction으로 저장한다. `execution_ports.publish_worker_checkpoints()`와 workspace handler는 `pushed`·`candidate_changed`를 구별하고 push 뒤 context를 재구성한다. 유효한 `current_rebase_acceptance`에 결합한 재작성 이력은 checkpoint 순차 push 대신 기존 최종 게시 경로로 넘기는 처리도 직접 구현돼 있다. 해당 구현과 실제 중간 commit/저장 중단 회귀는 재구현하지 않으며 G-303의 DB 전용 요청 재준비·공통 deadline/취소만 이 저장 소비자에 적용한다.

## Acceptance Criteria

- R-093/R-091: intent 확정과 게시 완료 확정은 각각 DB 전용 원자 요청으로 실행한다. transaction 안 BUSY/LOCKED는 요청 전체 rollback·최신 소유권/실행 snapshot·checkpoint generation 재조회 후 제한 재시도한다. SQL 한 문장만 재실행하거나 낡은 값으로 CAS를 우회하지 않는다.
- G-303의 공통 요청 예산·취소를 유지하고 경합/요청 한도 소진은 게시 대기로 반환한다. Worker 교정/일반 단계 실패를 소비하거나 실제 외부 효과를 성공으로 합성하지 않는다. 실제 실행 상태·작업 소유권/generation 변경은 경쟁 값을 보존하고 현재 적용만 거부/재관측한다.
- intent는 push 전에 durable하게 저장한다. 완료 checkpoint·`publication_remote_oid`·PR head는 함께 확정하고 부분 완료를 남기지 않는다. intent 저장/완료 저장 경합과 중단에서 이미 확인된 commit·push는 반복하지 않는다. Git/PR/원격 조회 동안 DB 접근·미확정 transaction을 유지하지 않는다.
- 직접 구현된 Manager 요청·literal staging·보호 문서/index 검사와 과거 직접 commit의 immutable parent/tree별 범위 검사를 유지한다. 최종 diff에서 되돌린 금지 중간 commit도 게시하지 않고 정상 base 유입은 Worker 변경과 구별한다. 실제 origin·branch·PR·from/target lease를 보존한다.
- push 성공 뒤 원격 후속 전진은 자기 push 확인과 새 통합/검증 입력으로 나눈다. `pushed`만 있을 때도 최신 context를 사용하며 게시 대기 해결 전 새 Worker 호출은 0회다. 일반 불확실 OID/소유권의 단계 재분류는 G-313, 새 환경 인수/PR terminal은 G-308이 담당한다. checkpoint는 Worker/targeted/verify/Review 성공이 아니다.
- 재작성 이력의 기존 예외는 pending intent가 없고 유효한 acceptance의 repository·작업/경로·worktree·branch·target base·원격 OID가 현재 입력과 일치하며 acceptance HEAD가 현재 HEAD의 조상일 때만 유지한다. 후속 로컬 commit이 있어도 이 분기는 push 0회·checkpoint 완료 변경 없이 최종 게시 경로로 넘기며, DB 재시도가 이를 게시 성공으로 확정하지 않는다. 다른 target/원격·불명확한 acceptance·미완료 intent의 기존 거부·보존은 유지한다.

## Implementation

- [x] 확인한 `loop_manager_internal/git/publication_service.py`의 `persist()`·intent/완료 snapshot 적용과 `storage/execution_checkpoint_state.py`·`storage/runtime_state.py:_transaction()`을 G-303의 `CoordinatedConnection.run_request()`에 연결한다. transaction 밖에서 확인한 immutable Git 자료와 현재 DB 값을 구별하고 요청이 재준비할 DB 조회/판단만 callback에 둔다. 중첩 caller transaction을 임의 commit하거나 전체 게시 함수를 callback으로 감싸지 않는다.
- [x] 확인한 `execution/execution_ports.py:publish_worker_checkpoints()`·`execution/worker_checkpoint.py`·`execution/execution_workspace_handler.py`의 pending 복구와 결과 계약을 재사용해 DB 요청 소진/취소를 같은 intent/commit/session의 보존된 대기로 소비한다. 재관측 후 결과 저장만 재개하며 추가 모델 호출·commit·push를 만들지 않는다. `current_rebase_acceptance`가 가리키는 기존 최종 게시 반환은 DB 완료 callback 밖에 두고, 실제 push 확인·후속 입력 통합·재작성 후보의 최종 검증/게시를 혼합하지 않는다. 새로운 대기 엔진·ledger·설정은 추가하지 않는다.
- [x] 확인한 `tests/test_worker_checkpoint_publication.py`의 실제 SQLite·원격 게시 중단/범위 정책 fixture와 `tests/test_db_access.py`의 제한 요청 fixture를 재사용한다. PRODUCT·ARCHITECTURE의 checkpoint/DB 소유 절에는 실제 보완한 소비 계약만 통합한다. 공통 DB 엔진은 G-303, 수동/Agent 저장은 G-304/G-305에 남긴다.

## Validation

- Worker는 `tests/test_worker_checkpoint_publication.py`와 변경한 handler의 최소 unit case를 실행한다. 실제 파일 DB의 경합을 제어해 intent·완료 요청 rollback→현재 상태 재준비→한 번의 원자 확정을 확인한다. Git/PR/전송은 기존 double을 사용하고 push 횟수와 두 상태 row의 실제 값을 검사한다.
- 완료 저장 중 BUSY/LOCKED·한도 소진·취소·경쟁 generation/소유권 변경에서 원격 효과를 한 번만 유지하고 양쪽 row·경쟁 변경·원래 intent/session을 보존한다. 요청 소진 뒤 재진입은 새 Worker/commit/push 없이 완료 관측/저장을 이어가며 비잠금 SQL/권한 오류를 DB 대기로 바꾸지 않는다.
- 기존 `test_confirmed_push_and_later_remote_input_are_separate_facts`·`test_publication_completion_rolls_back_both_rows_and_does_not_repeat_push`·`test_manager_request_survives_commit_completion_interruption_without_new_commit`·`test_pr_wait_releases_database_and_checkpoint_race_prevents_push`를 재사용한다. 경합 뒤 재준비/취소에 빠진 assertion만 보완한다.
- 직접 추가된 `test_accepted_rebase_and_later_commit_wait_for_final_publication`의 재작성 HEAD/후속 commit·push 0회·원격/저장 checkpoint 보존과 target 변경 거부를 유지한다. DB 요청 연결 뒤 이 최종 게시 반환이 intent 생성·완료 확정·과거 lease 치환으로 바뀌지 않는지 기존 fixture에서 확인하며 같은 실제 rebase lifecycle을 DB 경합 조합마다 복제하지 않는다.
- `test_intermediate_forbidden_document_is_never_pushed_after_its_revert`와 literal paths/index·정확한 lease·부분 게시 실제 Git 회귀는 유지하며 동일 lifecycle을 DB 조합마다 반복하지 않는다. 실제 Git·OS 실행 여부와 mock 결과를 구별한다.

## Progress

- [x] 5903c5aa의 commit별 게시·pending 복구·scope 진입과 기존 세 게시 회귀를 정적으로 대조했다. 직접 구현된 순차 push는 재구현 대상으로 제외했다.
- [x] 현재 revision의 #294 직접 구현과 commit별 scope·원자 완료·push 후 context 재구성 및 관련 회귀를 정적으로 확인했다. 기존 두 미완료 항목 중 scope/원자 저장 자체는 재구현 대상에서 제외하고 G-303 요청 재시도 소비만 남겼다. 테스트 실행·성공을 뜻하지 않는다.
- [x] 지정 delta의 유효 rebase acceptance/후속 commit을 최종 게시 경로로 넘기는 구현과 원격·checkpoint 불변 회귀를 정적으로 대조했다. 새로운 checkpoint 게시 구현이나 검증 성공으로 기록하지 않는다.
- [x] intent/완료 DB 요청의 제한 재시도와 대기/취소 소비 연결
- [x] 실제 경합·경쟁/중단 보존의 최소 targeted 회귀


### 구현·targeted 검증 결과 (2026-10-24)

- `persist()`의 intent/완료 CAS를 각각 DB 전용 `run_request()`로 실행한다. 각 시도는 등록 origin/base·전체 실행 snapshot·checkpoint를 다시 읽고 경쟁 변경을 거부한다. 요청 밖에서 관측한 Git/PR 자료는 재실행하지 않으며 owner 메모리는 commit 성공 후 갱신한다. 일반 SQLite 연결의 기존 원자 transaction 경계도 유지한다.
- 게시 port는 DB 대기 한도 소진만 `TRANSIENT_PAUSE`로 반환해 일반 복구·교정 횟수를 소비하지 않는다. runtime handoff와 비잠금 SQL 오류는 전파하고 기존 요청·session·pending intent를 보존한다. 기존 pending 복구·Worker 진입 gate와 재작성 acceptance의 최종 게시 반환은 재사용했다.
- 프로젝트 Python `python3`와 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 `--unittest tests.test_worker_checkpoint_publication` 실행: 17 tests PASS. 신규 DB 회귀는 intent/완료 각각에서 실제 파일 SQLite reader가 COMMIT을 막는 경합을 발생시켜 rollback·재준비, generation/session 경쟁 변경 거부, 실제 deadline 소진, runtime 취소와 비잠금 SQL 오류 뒤 재진입을 확인했다. publication OID·PR head·checkpoint 감사의 원자성, 원래 요청/session 보존과 push 1회를 검사했다. Git/PR은 이 DB 조합에서 double이며 기존 모듈의 실제 Git 범위·lease·부분 게시·중단·rebase 회귀도 통과했다.
- `--unittest tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_pending_checkpoint_request_prevents_a_new_worker_turn`: 1 test PASS. pending 요청에서 새 Worker·verify 호출이 없고 요청을 보존함을 확인했다. 신규 production port 회귀는 timeout의 일시 대기와 SQL 오류/runtime handoff 전파 및 실행 row 불변을 확인했다.
- 관련 테스트의 삭제·skip·assertion 약화는 없으며 전체 verify·Windows native 실행·Review·게시·Merge 판정은 수행하지 않았다. Manager 후속 검증 대상이다.

## Decision Log

- revision `de8fbaeabb6d64d731620930a2ac1719d5ec5d13`과 snapshot token `14eb9a908420444bab35d4c6f7f9039a1192960ef2ad385e939e7ebfb62312c4`의 원문 hash를 대조했다. 정상 재작성의 checkpoint no-push 처리는 직접 구현으로 소비하고 남은 DB 요청 보완의 보존 조건에 연결한다. legacy rebase의 `accepted_work_branch` 우선 판단도 `execution_ownership_handler.py`와 `tests/test_execution_checkpoint_recovery_phase.py:test_legacy_rebase_uses_accepted_remote_before_old_observation`에 직접 구현돼 있어 새 소유권 교정 목표를 만들지 않는다. G-313의 남은 원인 분류·재평가와 기존 ID·경로·sources·dependency·기록을 유지하며 구현·테스트·verify·최종 전문 확인은 수행하지 않았다.

- snapshot token `41c4344480fa2e611f329b73b10ee282021edebf66ec448d2eb1623329109377`과 지정 revision `16115a547319c40aab74e67094536c43bdae7fce`의 원문 hash를 대조했다. 같은 ID·경로·sources·G-303 dependency와 조사 기록을 보존한다. Manager checkpoint 요청·로컬 효과·게시 경계는 현재 직접 구현을 사용하고 남은 intent/완료 DB 요청 재시도만 이 계획에서 보완한다.
DEPENDENCY_RECOVERY: {"deleted_id":"G-306","outcome":"obsolete","replacement_ids":[],"sources":["docs/PRODUCT.md","docs/requirements/[R-093] 원격 작업 브랜치로 새 환경에서 이어하기.md","docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md","docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md"],"reason":"보존 원문의 제한 checkpoint 요청·완료 구분·Worker 종료/범위/index 검사·Manager commit·게시 대기·중단 재개는 worker_outcome.py, WorkerHandler와 worker_checkpoint.py의 save_request/resume_request에 직접 구현돼 있다. 현재 지침의 Worker 직접 Git 쓰기 금지와 tests/test_worker_outcome.py, tests/test_execution_worker_phase.py의 pending no-call 및 tests/test_worker_checkpoint_publication.py의 commit 저장 중단/immutable 범위/literal staging 회귀를 정적으로 대조했다. 별도 선행 구현은 필요하지 않다. G-303의 DB 요청 API를 소비하는 이 계획의 남은 재준비·공통 예산·취소/원자 완료와 기존 회귀는 보존하며 삭제를 구현·검증·Merge 성공으로 사용하지 않는다."}
- Worker의 Git 쓰기 권한을 넓히지 않는다. 신규 Manager 요청과 legacy immutable 이력 검사는 현재 구현을 사용하며 G-284의 별도 보호 파일/index 복원과 G-308의 인수/terminal을 중복 구현하지 않는다.
- complexity high(최초 level 5): 비가역 원격 효과 사이의 DB 재준비·generation/소유권·공통 예산을 연결한다. risk high: 잘못된 callback replay/완료 적용은 중복 게시나 경쟁 상태 훼손을 만들 수 있어 DB 전용 callback·정확한 intent/lease·원자 CAS·실제 경합 회귀를 유지한다. priority high와 Manager의 Model·Effort 설정을 유지한다. Planner는 구현·테스트·verify를 실행하지 않았다.
