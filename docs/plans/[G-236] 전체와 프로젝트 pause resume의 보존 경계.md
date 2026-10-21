---
id: G-236
title: 전체와 프로젝트 pause resume의 보존 경계
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
  - docs/requirements/[R-077] G 계획 수동 구현과 R·코드 일괄 제출.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

자동 실행 중에도 전체/프로젝트 pause를 접수해 새 작업을 막고 대상 프로세스 종료·작업 보존을 확인한다. 범위별 resume는 pause만 해제하며 자동 실행이나 독립 차단 해제를 수행하지 않는다.

## Acceptance Criteria

- pause [repo]는 먼저 요청을 저장하고 대상 Agent·verify·자식 종료를 유한 시간 안에 확인한다. 단계·session·작업물·진단·게시 intent를 보존하며 다른 프로젝트나 다른 기기의 수동 구현 종료를 주장하지 않는다.
- Git·게시 중단 결과는 현재 소유권·실제 원격·기존 복구 증거로 확인한다. 종료·저장·게시 결과 미확인에서는 pause와 미완료 대상을 보존하고 완료·새 자원 사용을 허용하지 않는다. 재시작도 같은 요청을 이어간다.
- 전체 pause의 run은 bootstrap/runtime·원격·Agent·verify·알림 전에 안내·코드 0으로 종료한다. 프로젝트 pause는 해당 대상을 제외하며 명시 repo로 우회하지 않는다. 수락된 수동 제출·복구·유지보수의 자동 후속 처리도 pause를 준수한다.
- 전체/프로젝트 pause는 독립 저장하고 resume [repo]는 해당 범위만 해제한다. 기존 사용자 결정·차단·usage 제한·dependency·PR 대기는 유지하고 resume 자체는 작업을 시작하지 않는다.
- pause 중 조회·설정·제어·기존 수동 준비와 요청 접수는 가능하다. 정상 무변경/완료 0, 안전 거부·종료 미확인 1, 인자 오류 2이며 요청 저장과 실제 중단을 구분한다.
- R-082의 일반 planning 전환 중에도 pause는 실제 Planner·verify·Agent와 자식만 대상으로 종료를 확인한다. `planning_resume_plan_id`로 보존한 원래 작업, 별도 planning worktree·결과·게시 intent·오류와 실행/실패 이력을 유지한다. 보존한 rebase의 HEAD·index·작업물·원래 target·checkpoint도 유지하며 Git 표식만으로 실행 중 자식이 있다고 판단하거나 임의 continue/abort하지 않는다. 사용자 resume는 pause만 해제하고 planning 성공·영향 확정·rebase 완료·원래 작업 재개를 합성하지 않는다. 다음 실행의 planning 게시·DB 적용과 같은 작업 재개는 기존 R-082 gate를 따른다.
- R-091의 프로젝트 planning 배정 중단과 R-076의 사용자 pause를 구별한다. 프로젝트 준비·planning이 작업 배정 중단 중 Git 자원을 사용할 수 있어도 사용자 pause·runtime handoff·실제 자원 잠금은 계속 적용한다. Git 자원 대기 중 pause를 접수하면 새 외부 효과 없이 보존 경계로 반환하고 다른 프로젝트의 자원·작업은 유지한다.
- R-055/PRODUCT의 targeted 기록 수명도 pause·resume에서 보존한다. 호출별 JSONL·디렉터리 identity·process receipt·미소비 결합은 종료/수집·저장이 불확실해도 남긴다. resume 자체는 기록을 소비하거나 도달 표식·실패 이력을 초기화하지 않는다. 다음 자동 실행은 기존 호출 재수집을 거치며 세 번째 실패와 수집 대기는 해당 작업의 기존 차단/대기 경계로 이어진다. 다른 작업의 자료와 살아 있는 수동 writer는 변경하지 않는다.
- R-023/R-091의 코드 변경 handoff·비차단 재시도 대기와 사용자 pause를 구별한다. pause 요청은 대기 중에도 처리하며 전체 pause가 저장되면 새 원격 효과·runtime/단계 호출을 시작하지 않는다. pause는 진행 중 호출의 기존 종료/보존 경계를 적용하고, 정상 handoff는 안전 반환을 기다린다. 사용자 resume가 실패/교정/테스트 이력·재시도 대기·독립 조건을 초기화하거나 handoff 승인·자동 작업 시작을 뜻하지 않는다.

## Implementation

기존 process/resource lock·subprocess 종료/verify 자식 조율·실행 checkpoint·수동 요청 저장을 재사용한다. bootstrap 이전의 pause 조회와 실행 중 요청 접수는 실행 잠금 소유자의 작업을 탈취하지 않는다. 필요한 호환 저장·요청/확인 전이는 기존 runtime/storage 경계에 두고 PRODUCT·ARCHITECTURE와 운영 안내에 반영한다. 새 daemon·실행 엔진·자동 병렬 Worker를 만들지 않는다.

- [ ] 확인한 `loop_manager.py`의 bootstrap 전 진입·`loop_manager_internal/application/commands.py`·`scheduler.py`의 등록된 선택 범위와 `application/project_service.py`의 실행 dispatch 경계에 pause 확인을 연결한다. 이미 반영된 `add/remove`와 다중 repo 정규화·미등록 사전 거부는 재사용하고 전체 pause가 이 준비보다 먼저 종료되는 조건을 유지한다.
- [ ] `loop_manager_internal/storage/runtime_state.py`의 planning 전환/재개와 `planning/selection_service.py`의 오류 보존을 읽는 제어 경계에서 실제 실행 대상과 보존 작업을 구별한다. pause/resume를 프로젝트 planning의 투영 비우기/재투영과 혼동하거나 같은 작업의 재개 identity를 초기화하지 않는다. 최신 base의 `planning/git_ports.py:execution_blocks_planning()`은 보존한 rebase와 다른 Git operation을 이미 구별한다. 이 정상 전환과 `execution/execution_state.py:recover_rebase_checkpoint()`의 소유권 복구를 재구현하지 않고 사용자 pause 전후에 원래 target·index·checkpoint가 보존되도록 연결한다. R-082의 planning 실패 복구 자체는 G-278에 남긴다.
- [ ] 확인한 `runtime/resource_lock.py:resource_locks()`의 대기/획득 뒤 `_check_runtime_handoff()`와 보존 후보의 `runtime/handoff_gate.py`·`runtime/pause.py`를 통합한다. `current_task`가 없는 프로젝트 준비·planning의 Git 접근 허용과 사용자 pause 검사를 함께 유지한다. task의 `PlanningDrain`과 runtime 전체 handoff도 보존하며 같은 scope·프로세스 종료 근거를 재사용한다.
- [ ] 보존 후보에 이미 있는 `loop_manager_internal/runtime/pause.py`·`tests/test_pause.py`와 최신 base의 `agents/invocation_service.py`·`agents/process_receipt.py`·`storage/agent_invocation_state.py`·`storage/targeted_test_state.py`를 통합해 pause 예외 전후에도 호출 결합과 미소비 기록을 유지한다. 후보의 과거 finally 수집과 새 durable 수집·별도 pending 표식을 대조해 필요한 차이만 보완한다. 종료 확인을 새로 구현하거나 수집·receipt 경로를 pause 전용 폴더로 옮기지 않는다. 보존 후보의 코드·기록은 현재 Planner가 수정하지 않는다.
- [ ] 기존 bootstrap 전 pause·handoff launch gate·작업 대기에서 사용자 요청을 확인하는 경계를 보존한다. 최신 R-023의 코드 변경 확인/부모 자동 재시작은 G-311/G-312가, R-091의 비차단 재배정은 G-297/G-313이 담당한다. 이 작업은 그 기능을 선구현하지 않고 pause의 중단·무효과·재개 범위가 후속 통합에서도 소비될 수 있게 기존 경계를 유지한다. 후보의 self-merge 전용 preflight 대응표는 당시 기록으로 보존하며 일반 원격 runtime 판정의 성공으로 사용하지 않는다.

## Validation

- 실제 저장·정책을 실행하는 targeted unit test로 중첩 범위·resume 격리·접수/선택 경합·종료 확인 실패·재시작과 수동/복구 후속 대기를 확인한다. 전체 pause 진입에서 외부 효과는 호출 시 실패 fixture로 차단한다.
- 확인한 `tests/test_bootstrap_entry.py`, `tests/test_run_project_selection.py`, `tests/test_runtime_state.py`, `tests/test_planning_selection.py`의 fixture를 재사용해 전체 pause의 bootstrap 전 무효과, 선택 repo의 pause 제외, planning 전환 전/중/게시 후 DB 미적용의 pause·resume를 최소 unit으로 확인한다. 실제 SQLite의 원래 실행 row와 planning identity/오류·intent가 보존되고, 종료 미확인에서는 다음 실행이 시작되지 않아야 한다. 기존 다중 repo/등록 CLI를 다시 구현·검증하는 작업으로 확대하지 않는다.
- 위 planning fixture와 `tests/test_task_scheduling.py`의 Git 자원 대기/배정 중단 회귀를 연결해, 보존한 rebase가 있는 별도 planning의 자원 대기·획득 직후·게시 후 DB 미적용에서 사용자 pause를 확인한다. 실제 요청 저장·보존 판단을 사용하고 Git/프로세스 관측만 double로 제공한다. 새 fetch/Planner/Worker가 시작되지 않고 원래 HEAD·index·target·checkpoint 및 planning intent가 남아야 한다. resume 명령의 외부 효과 호출 수는 0이며 이후 실행도 기존 planning·소유 rebase gate를 거쳐야 한다. 정상 배정 중단만 있을 때의 planning 허용·task 미호출·runtime handoff 거부는 기존 회귀를 재사용한다.
- 전체 verify는 대상 자식만 종료·예약은 종료 확인 뒤 회수, Git/게시 미확정·session/실패 보존 및 다음 안전한 경계 재개를 확인한다. 실제 프로세스 종료 효과는 필요한 기존 회귀/최소 연결만 유지한다.
- 후보의 `tests/test_pause.py` fixture에 targeted JSONL 생성 뒤 pause, 종료 확인 뒤 수집/DB 실패, 재시작·resume·다음 자동 실행을 연결하는 최소 case를 추가한다. 임시 파일·실제 SQLite를 사용하고 process 종료만 double로 제공한다. 원문·collection/receipt 결합·물리 결과와 사용량 감사·교정 횟수가 보존되고, 재수집은 한 번만 반영돼야 한다. collector·CLI retry 자체의 `tests/test_targeted_test_streak.py`·`tests/test_agent_invocation.py` 회귀는 재사용하며 G-302의 저장된 verify 진입 검사를 이 작업에서 중복 구현하지 않는다.
- handoff gate 또는 작업 재시도/자원 대기 중 실제 pause 요청을 저장해 새 effect 0회·원래 단계/대기/실패 보존·대상 격리를 확인한다. 사용자 resume의 외부 효과도 0회여야 한다. 실제 최신 원격 재선택/자동 runtime 재시작의 연결 회귀는 G-312가 이 pause 계약을 소비해 수행하며 이 작업의 새 선행 조건으로 만들지 않는다.

## Progress

- [x] eb797d3a에는 pause/resume 명령과 저장 gate가 없다. 기존 잠금·자식 종료·checkpoint를 정적으로 확인했다. 실행 없음.
- [ ] 요청·중단 확인·재개 구현과 targeted unit 검증
- [x] 보존 후보 HEAD `26f1ec771c73393083f67d53a7fe08c69c5f579d`를 읽기 전용으로 확인했다. 후보에는 pause 구현·회귀와 기존 실행 기록이 있으며 tracked 변경은 없다. 후보 본문의 31개·개별 7개 PASS 기록과 앞선 실패/교정·metadata 인계 기록은 그대로 보존한다. 이 확인을 최신 base·새 targeted 수집 조건의 검증 성공으로 사용하지 않는다.
- [ ] 보존된 구현의 최신 base 통합과 targeted 기록 보존 연결·최소 회귀
- [x] 이번 호출의 보존 후보 HEAD `bc5adea909fce7602607c811ee443aac115c1112`와 `runtime/pause.py`·`tests/test_pause.py`·후보 본문을 읽기 전용으로 확인했다. 후보에는 미해결 rebase 충돌과 구현·교정 대응표가 남아 있다. 이 중간 형상을 최종 후보나 새 검증 성공으로 판단하지 않았으며 파일·index·Git operation·기존 기록을 변경하지 않았다.
- [ ] 보존한 rebase·별도 planning과 Git 자원 대기 중 사용자 pause의 통합·최소 회귀
- [ ] 최신 R-023/R-091의 정상 handoff·비차단 대기와 사용자 pause 구분의 통합·최소 회귀
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이번 delta는 snapshot token `7186c8ca6368b99b2502fec6b2a3c56e428de0dfd28ec47eb983063575fac7e4`과 원본 hash를 대조했다. 제공된 보존 후보 `/home/ubuntu/.local/state/loop-manager/worktrees/e1b66bcef2ec3ace/G-236`의 HEAD는 `bc5adea909fce7602607c811ee443aac115c1112`이며 rebase 충돌과 후보 본문의 구현/Review 교정 대응표가 남아 있다. 이를 읽기 전용으로 확인했고 기존 파일·index·Git operation·수행 기록은 변경하지 않았다. started ID·경로·dependency·기록을 보존하며 새 조건의 pause 통합만 미충족으로 구별한다. G-311/G-312/G-313의 구현·검증 성공을 요구하거나 추정하지 않고 현재 작업은 기존 재개 경계를 따른다.

- `5903c5aa` → `eeb0ff79` delta와 snapshot token `7edcccd922df2c4ed63ee0fb272a2783c5b7c06898a7268b77f1450c157a4608`, 원본 hash `5630d694c5384eee4b7ddc6b076ad8a3e6b1d4085caa984377de4d17db879d20`을 대조했다. started ID·경로·기존 구현/검증 기록은 보존하고 현재 후보의 실제 경로로 작업·검증 참조를 교정했다. 정상 rebase 보존 planning·누락 checkpoint 복구·프로젝트 Git 접근의 직접 구현은 소비하며 남은 pause 통합만 같은 작업에서 검증한다. 기존 complexity/risk·priority와 dependency는 유지한다. 구현·테스트·verify는 실행하지 않았으며 후보의 충돌 해결과 최종 판정은 Manager/Worker의 기존 경계에 남긴다.

- 이번 delta는 snapshot token `e108f535e66d7a1ec6bbe0c12265d2b209bdfe14e05e4bc20c155efd5dc80e7c`과 원본 hash `3b0c599a3dee14a813b0339e46f23f067cb94a7d25fc1d437fbd903a2819e645`를 대조했다. started ID·경로·기존 기록을 보존하고 R-055의 새 호출 결합/재수집을 기존 pause의 자료 보존 대상으로 연결했다. 후보의 수행/검증 원문은 `/home/jeong/.local/state/loop-manager/worktrees/ae1e641e09ef3149/G-236/docs/plans/[G-236] 전체와 프로젝트 pause resume의 보존 경계.md`에 남아 있다. 요청·기존 종료 제어를 다시 구현하지 않으며 남은 통합·회귀만 Worker에게 전달한다. 후보·Git·실행 이력은 변경하지 않았고 구현·테스트·verify 성공을 새로 기록하지 않았다.

- 2026-10-14 delta 개정: 제공된 started snapshot의 ID·경로·기존 기록을 보존했다. 보존 후보는 비교 기준 revision의 clean 상태이며 새 구현 diff는 없었다. 원격에 추가된 선택 실행 CLI와 R-082의 planning 전환을 pause의 기존 보존 대상·Validation에 연결하고 R-082를 source로 추가했다. 후보를 수정하거나 검증 성공·실행 상태를 만들지 않는다.

- complexity high: 실행 잠금 밖의 요청과 bootstrap 전 gate, 프로세스 종료 및 비원자적인 Git/저장 결과의 수명을 함께 설계해야 한다. risk high: 잘못된 종료 확인·예약 해제는 미완료 작업과 새 실행을 겹치게 하며 작업/intent에 따른 수동 복구가 필요하다. 미확인 보존·범위 격리·중단/경쟁을 검증한다.
- R-076에 명시된 중단 요청만 구현하며 reset이나 제출 취소는 후속 계획으로 분리한다.
