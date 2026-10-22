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

Manager 생성 checkpoint의 정확한 lease 게시·사전 역할 범위 검사와 과거 Worker 직접 commit의 commit별 호환 검사, 게시 intent/완료의 원자성을 보완한다. Git·PR·원격 응답 동안 공통 DB 접근을 반환하고, 중단 뒤 완료한 push를 반복하지 않으며 새 Worker 작업 전에 게시 대기를 해결한다.

현재 `push_worker_checkpoint_commits()`는 최종 HEAD까지의 선형 parent 관계를 확인해 각 commit을 push하지만 각 중간 tree의 역할 범위를 검사하지 않는다. `execution_ports.publish_worker_checkpoints()`의 재개 진입도 항상 범위 검사를 수행하지 않는다. 따라서 과거 Worker 직접 commit의 중간 금지 변경을 마지막 commit에서 되돌린 이력이나 복원 전 commit을 최종 diff 검사만으로 게시할 수 있다. 신규 Worker는 직접 commit하지 않으며 Manager의 사전 범위 검사가 필요하다. checkpoint 저장과 실행 row 갱신 사이 및 DB 접근을 점유한 외부 조회도 같은 기존 함수에서 보완한다.

## Acceptance Criteria

- G-285형 정상 Worker 변경 → 종료 및 Manager 보호 문서·index 검사 → Manager commit/push → DB·checkpoint 반영 → 원격 소유권 재확인에서 push 전 `remote_oid`를 재사용해 `publication_ownership_uncertain` 최종 차단을 만들지 않는다. `candidate_changed`와 `pushed`를 별개 사실로 소비한다.
- 저장된 push intent·checkpoint·현재 원격 branch·PR identity를 같은 관측으로 재평가한다. 게시 직후 중단이나 원격 후속 전진은 완료한 효과와 변경된 후속 입력을 구분하며, 실제 타인의 commit이나 다른 origin·branch·PR을 덮어쓰지 않는다. 자동 복구 가능한 관측 차이는 사용자 메시지/프로젝트 차단으로 확대하지 않는다.

- R-093/R-061/R-082: 신규 Worker 요청은 Manager가 허용 파일·index·frontmatter·선언을 검사하고 commit을 생성한다. 과거 Worker 직접 commit·원격 인수/재시작·부분 게시에서는 실제 push 대상 commit마다 허용 결과를 검사한다. 최종 diff에서 사라진 Requirement·다른 ExecPlan·선택 선언/사용자 결정의 금지 변경도 원격에 게시하지 않는다. 원본 base에서 유입된 변경과 Worker가 만든 변경을 구분하고 허용된 코드·선택 본문·제품/설계 변경은 유지한다.
- Manager가 생성했거나 기존 이력에서 인수한 각 commit의 정확한 parent/tree·현재 후보와 작업/branch/worktree·origin·PR 연결을 확인한다. 일부 선행 commit만 게시된 뒤 다음 commit이 거부되면 확인한 복구점과 미게시 이력을 각각 보존하며, 이력을 자동 삭제·재작성해 거부를 우회하지 않는다.
- intent는 실제 push 전에 durable하게 확정한다. push는 저장된 `from` 또는 정확한 부재 lease에 결합한 `target` 하나만 사용한다. 완료 checkpoint와 `publication_remote_oid`·PR head 갱신은 같은 짧은 transaction/CAS로 확정하고 일부 기록만 성공으로 남기지 않는다.
- 원격·Git·PR·통합 관측 중 DB 차례와 미확정 writer transaction을 유지하지 않는다. 적용 때 현재 실행/소유 상태·checkpoint generation·후보를 다시 확인한다. G-303의 DB 요청 재시도는 상태 확정만 재개하며 push·fetch·rebase를 통째로 replay하지 않는다.
- push 전/후·완료 저장 중 중단에서 원격이 target이면 push 없이 확인을 마치고, from이면 같은 commit을 재시도한다. 그 외 원격 OID는 현재 입력 통합·재검증으로 연결한다. 성공 후 원격이 후속 전진한 경우 자기 push 확인과 후속 작업 수용을 구분하며 기존 원격 commit을 덮지 않는다.
- 로컬·원격에 새 작업이 있으면 기존 통합·보존 경계를 재사용한다. 충돌·lease 재변경·불명확한 소유권에서는 양쪽 commit과 intent를 보존하고 해당 게시만 대기한다. 게시 성공은 복구점일 뿐 Worker·targeted test·verify·Review 성공이나 사용자 판단 해소를 만들지 않는다.

## Implementation

- [ ] `execution/execution_workspace_handler.py`·`execution/execution_ownership_handler.py`·`execution/execution_ports.py`의 push 반환값과 이후 checkpoint/원격 재관측 순서를 연결한다. 오래된 `publication_remote_oid`·`remote_oid`로 직후 소유권을 재검사하거나 `candidate_changed`만으로 게시 완료를 추정하는 경로를 수정한다. 신규 Manager commit 생성 자체는 G-306에 맡기고 과거 직접 commit의 호환 검사는 보존한다.

- [ ] 확인한 `loop_manager_internal/git/publication_service.py:push_worker_checkpoint_commits()`의 선형 이력 열거·정확한 lease·성공 확인을 재사용한다. 기존 역할 scope/문서·선언 helper를 각 immutable commit의 실제 변경에 적용할 수 있는 좁은 읽기 경계로 연결한다. 검사 때문에 작업 worktree를 checkout/reset하거나 검증용 별도 worktree를 추가하지 않는다. 중간 tree와 정상 base 유입의 출처를 확인할 수 없으면 게시하지 않는다.
- [ ] 확인한 `execution/execution_ports.py:publish_worker_checkpoints()`·`execution_worker_handler.py`·`execution_workspace_handler.py`의 최초/재개 호출에 같은 검사와 게시 대기 처리를 적용한다. 저장된 Agent HEAD만으로 scope 검사를 생략하지 않고, 현재 operation 때문에 미룬 게시를 새 Worker 실행 허가로 바꾸지 않는다. 로컬 commit 실행 연결은 G-306이 담당한다.
- [ ] 확인한 `storage/execution_checkpoint_state.py`의 generation CAS와 `execution/execution_state.py`·`storage/runtime_state.py`의 실행 snapshot transaction을 재사용해 intent 확정과 완료 확정을 나눈다. 현재 `checkpoint.record()`와 `update_project()`가 개별 호출되는 구간을 짧은 원자 처리로 묶고, G-303의 공통 요청 예산·재시도를 소비한다. 새 원격 게시 ledger나 별도 상태 저장소를 만들지 않는다.
- [ ] 확인한 `recovery/work_branch_integration.py`의 원격 수용·보존·후보 변경 반환과 `git/manager_git_journal.py`의 실제 게시/lease helper를 재사용한다. 원격 변화가 기존 pending을 무효화하면 과거 intent를 감사로 남기고 새 후보를 검사한다. 최신 OID만 낡은 intent에 끼워 넣지 않는다. 일반 rebase 정책 전수 감사와 전체 Git 통합 재설계는 기존 담당 범위에 둔다.
- [ ] PRODUCT·ARCHITECTURE의 checkpoint intent·실패 보존·DB 책임 절에 실제 보완한 조건을 통합한다. 확인한 `tests/test_worker_checkpoint_publication.py`의 commit별 push·저장 중단·stale lease 회귀와 `tests/test_task_remote_inputs.py`의 통합/보존 fixture를 우선 재사용한다.

## Validation

- Worker 정상 종료 → Manager checkpoint push → 최신 DB/PR/원격 OID 확인과 push 후 저장 중단·후속 원격 전진에서 중복 commit·push, stale OID 최종 차단 및 전체 PROJECT_BLOCKED가 없음을 확인한다. 잘못된 origin/branch/PR과 타인 OID 덮어쓰기는 정확한 lease와 소유권으로 계속 거부한다.

- Worker는 `tests/test_worker_checkpoint_publication.py`의 정책 unit과 변경한 실행 handler의 최소 unit case만 실행한다. Git 관측/응답은 mock으로 제공하고 실제 범위 판단·SQLite checkpoint/실행 상태를 검사한다. 기존 실제 Git commit별 lease 회귀는 Manager 검증에서 유지한다.
- 보호 문서 변경 → 후속 되돌림, 선택 선언 변경 → 파일 복원, 정상 코드/선택 본문 commit, 원본에서 유입된 문서와 Worker 변경의 혼합 이력을 구성한다. 모든 push target의 검사가 실행되고 금지 중간 commit은 최초·재개 경로 모두 push되지 않아야 한다. 실제 허용 정책을 성공 mock으로 대체하지 않는다.
- intent 저장 직후·push 성공 직후·checkpoint/실행 row 완료 저장 사이의 중단과 CAS 경쟁을 확인한다. 한 쪽만 갱신된 완료가 확정되지 않고 from/target/후속 OID 각각에서 완료한 push·commit을 반복하지 않아야 한다. 실제 실패·종료 불명·다른 PR/branch는 계속 보존·거부한다.
- 실제 파일 DB와 Event/Barrier를 사용해 PR/원격 대기 중 다른 연결의 조회·쓰기가 진행되는지 확인한다. 관측 뒤 작업/소유권·generation 변경은 경쟁 값을 보존하고 현재 적용만 거부해야 한다. DB 경합으로 외부 효과 호출 수나 Worker 교정 횟수가 늘지 않아야 한다.
- 기존 원격 추가 작업·충돌·lease 경쟁·게시 실패 테스트를 재사용해 새 Worker no-call, 양쪽 commit·dirty/index·session·실패 감사 보존과 성공 token 미생성을 검사한다. 새 실제 Git 회귀는 mock으로 증명할 수 없는 중간 commit의 원격 도달 여부 또는 실제 중단 효과에 한정한다.

## Progress

- [x] 5903c5aa의 commit별 게시·pending 복구·scope 진입과 기존 세 게시 회귀를 정적으로 대조했다. 직접 구현된 순차 push는 재구현 대상으로 제외했다.
- [ ] commit별 역할 범위·최초/재개 검사 연결
- [ ] DB 반환·원자 완료·중단 재개와 최소 회귀 보완

## Decision Log

- 2026-10-22 정책 변경: 신규 Worker checkpoint commit은 G-306의 Manager 전담 효과로 생성한다. 이 계획의 commit별 legacy 범위 검사는 과거 직접 Worker commit·부분 게시·원격 인수에 한정하고, 알림 최종 정책은 R-053/G-189에 연결한다.

- 최종 후보 검사만으로는 이미 원격에 보낸 중간 commit을 보호할 수 없다. 실제 게시 단위와 같은 immutable commit 범위에서 기존 역할 규칙을 검사하며 최종 diff·검증·Review gate도 별도로 유지한다. 다른 보호 문서의 파일/index 복원은 G-284의 목표를 확대하지 않는다.
- G-303은 공통 DB 요청 재시도와 중첩 원자성을 맡고 이 계획은 새 checkpoint 게시 소비자만 연결한다. G-304의 수동/recover, G-305의 Agent 호출 저장과 중복하지 않는다. 기존 R-055 전체 경로 감사에는 이 구현의 후속 소비 검증만 인계한다.
- complexity high는 중간 commit의 출처/역할 판정, 원격 효과와 DB CAS의 분리, 일부 게시·원격 재전진·중단의 보존을 함께 다뤄야 하기 때문이다. risk high는 범위 밖 문서 게시나 잘못된 완료/재시도가 다른 원격 작업을 훼손할 수 있기 때문이다. immutable 대상·정확한 lease·durable intent·짧은 완료 transaction과 실제 보존 회귀로 완화한다. 최초 level은 5다.
