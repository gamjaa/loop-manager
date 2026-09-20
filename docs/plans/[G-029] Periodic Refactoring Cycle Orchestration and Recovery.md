---
id: G-029
title: Periodic Refactoring Cycle Orchestration and Recovery
sources:
  - docs/requirements/[R-001] Automation-First Planning and Priority.md
  - docs/requirements/[R-012] Periodic Refactoring Assessment and Execution.md
depends_on:
  - G-028
complexity: high
risk: critical
priority: normal
---

## Purpose

G-027의 주기 상태와 G-028의 제한된 plan revision/publication 경계를 프로젝트 scheduler에 연결해, 안전한 선택 시점의 구조 평가부터 prerequisite READY 처리, 리팩터링 장벽 실행, 마지막 Merge 뒤 기존 READY 재검증과 기준선 전진까지 하나의 중단 복구 가능한 유지보수 주기로 완성한다.

## Acceptance Criteria

- 이 계획이 도입되는 단계에서는 프로젝트에 IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 BLOCKED ExecPlan이 있으면 유지보수 평가나 다른 Worker를 시작하지 않고 기존 상태를 먼저 처리·대기·복구한다. G-031이 Merge된 뒤에는 안정된 `PR_OPEN`이 유지보수 평가는 계속 막되 독립 READY Worker의 실행 자리는 점유하지 않는다.
- 활성 작업이 없는 선택 경계에서 G-027이 최초 평가 또는 interval 도래를 판정하면 READY 존재와 동일 계획 입력 지문에 관계없이 일반 READY 선택 전에 유지보수 Planner를 정확히 한 주기 실행한다.
- 주기 미도달이면 G-024의 READY 소진 우선 및 planning fingerprint gate가 그대로 적용되고 유지보수만을 위한 Planner 호출을 만들지 않는다.
- READY가 남은 유지보수 호출은 구조 평가, 기존 비종료 계획과의 중복 확인, prerequisite 집합 결정 또는 리팩터링 장벽 구성만 수행하며 새 일반 기능 계획을 만들거나 기존 계획의 제품 목표·sources·ID를 바꾸지 않는다.
- READY가 없고 일반 계획 입력 변경과 유지보수 평가가 함께 도래하면 하나의 Planner 호출과 planning worktree에서 현재 미충족 일반 작업과 구조 평가를 함께 수행하고, 생성된 일반 계획은 같은 batch의 리팩터링 장벽이 있으면 그 뒤에 둔다.
- 유지보수 prompt는 현재 origin 기준 코드·테스트, 비종료 compact catalog, G-027의 bounded 변경 문맥과 평가 mode를 포함하고 책임 혼합, 반복 변경 집중, 테스트 경계, 결합도, 오류 복구 및 후속 변경 충돌을 검토하도록 한다. 파일 길이·함수 길이·변경 횟수 하나만으로 계획을 만들도록 지시하지 않는다.
- 기존 비종료 ExecPlan이 같은 구조 문제를 해결하면 새 리팩터링 계획을 만들지 않는다.
- 구체적인 구조 문제와 개선 경계를 확인하지 못한 평가 없음 결과는 기존 READY 파일·dependency를 변경하지 않고 현재 remote commit과 MERGED 기준값으로 평가 이력을 전진시킨 뒤 일반 선택을 계속한다.
- 현재 코드에서 리팩터링보다 먼저 필요한 기존 READY 계획이 있으면 새 리팩터링 계획을 게시하지 않고 최소 dependency-closed prerequisite 집합만 저장한다. 집합 밖 READY는 선택하지 않고, 집합을 모두 Merge한 다음 최신 코드에서 같은 유지보수 주기를 다시 평가한다.
- prerequisite 후보가 READY가 아니거나 소유 작업 공간·세션·PR이 있거나 dependency closure를 충족하지 못하고, 안전한 집합을 결정할 수 없거나 cycle이 필요하면 파일·catalog를 바꾸지 않고 진단 가능한 planning error로 중단한다.
- 현재 코드에서 바로 실행할 리팩터링 DAG가 확정되면 G-028의 단일 batch로 새 계획과 기존 eligible READY의 barrier dependency를 게시하고, 원격 확인 및 SQLite transaction이 완료되기 전에는 Worker를 시작하지 않는다.
- 활성 유지보수 주기에 기록된 실행 가능한 리팩터링 계획이 있으면 일반 priority/ID 정렬보다 먼저 선택한다. 리팩터링 DAG 내부에서는 dependency gate 후 숫자 ID로 결정하며 다른 프로젝트의 scheduler 진행과 전역 Planner mutex는 유지한다.
- 리팩터링 Worker는 일반 Worker와 같은 targeted unit test, 전체 verify, 읽기 전용 Review, PR 및 사용자 Merge 절차를 사용하고 공개 동작·API·저장 형식 또는 Architecture Change Policy 대상 변경을 포함하지 않는다.
- 마지막 리팩터링 계획의 Merge가 확인되면 장벽 뒤 READY Worker보다 post-refactor revalidation Planner를 먼저 실행한다.
- 사후 재검증은 최신 `origin/<base-branch>` 기준으로 장벽 뒤 eligible READY 계획의 파일 경로, Implementation, Acceptance Criteria의 구조 참조, Validation과 dependency를 개정하거나 변경 없음으로 확정하며 ID, 경로, sources와 원래 Purpose를 유지한다.
- 사후 재검증 결과가 같은 제품 목표를 안전하게 보존한다고 확정할 수 없거나 대상 계획의 상태·소유권 snapshot이 달라졌으면 낡은 Worker를 시작하지 않고 planning error와 진단을 보존한다.
- 변경된 revalidation batch는 G-028의 전체 catalog 검증, fast-forward 게시와 원자적 content hash/dependency/plan revision SHA 갱신을 완료한 뒤에만 장벽 뒤 READY를 선택한다. 변경 없음 결과도 해당 최신 remote SHA에서 재검증됐음을 durable state에 기록한다.
- 평가 없음은 즉시, 리팩터링이 있는 주기는 마지막 Merge와 사후 재검증 성공 뒤에만 기준선을 전진시키며 해당 주기의 리팩터링 Merge를 새 interval에 다시 세지 않는다.
- Planner CLI·결과 검증·push·SQLite recovery 실패에서는 평가 기준을 전진시키지 않는다. planning error, planning worktree, mode, 기대 batch와 활성 주기 단계를 보존하고 다음 실행에서 같은 단계로 복구한다.
- 새 일반 입력이 활성 유지보수 주기 중 바뀌어도 진행 중 주기를 중첩하거나 선점하지 않는다. prerequisite 재평가나 사후 재검증의 최신 origin snapshot에서 함께 관찰하되 일반 계획은 R-001의 READY 소진 gate에 따라 처리한다.
- 장기 제품 규칙이 R-001과 R-012에 맞도록 `docs/PRODUCT.md`의 planning trigger, READY 선택, Planner 권한, 상태 복구와 완료 조건을 같은 구현 PR에서 갱신한다.

## Implementation

- G-024의 프로젝트 선택 결정을 활성 ExecPlan 재개, 유지보수 주기 단계, due assessment, READY 선택, 일반 planning과 완료 판정의 명시적 순서로 확장한다.
- 유지보수 Planner 최종 결과에 평가 없음, prerequisite 집합, refactoring batch, post-refactor revalidation을 구분하는 결정적 protocol을 정의하고, 결과 종류와 worktree diff가 일치하지 않으면 correction retry 후 기존 planning error 흐름을 사용한다.
- prerequisite selection을 전체 catalog 상태에서 dependency closure로 검증하고 활성 주기 동안 허용 집합만 `select_ready_execplan` 후보가 되게 한다.
- refactoring batch publication 뒤 cycle plan ID와 terminal ID를 저장해 즉시 선택 override와 마지막 Merge 판정을 구현한다. priority 값 자체는 바꾸지 않는다.
- 마지막 리팩터링 Merge를 PR/GitHub 동기화 뒤 감지해 revalidation mode를 예약하고, G-028의 eligible snapshot과 publication service를 사용해 변경 또는 no-change 결과를 완료한다.
- maintenance planning workspace와 failure fingerprint를 일반 planning input fingerprint뿐 아니라 cycle ID·mode·기준 remote SHA에 결합해 동일 입력에서도 도래 가능한 평가와 단계별 중단 복구를 구분한다.
- 프로젝트 완료 판정은 due 또는 활성 유지보수 주기와 해결되지 않은 maintenance planning error가 있는 동안 완료 알림을 보내지 않고, 정상 기준선 전진 뒤 기존 unfinished catalog 조건을 다시 평가한다.

## Validation

- Worker는 새 유지보수 orchestration test class 또는 test module과 READY/Planner 선택 결정 함수의 관련 개별 test case만 targeted unit test로 실행한다.
- 최초 평가, 기본·사용자 interval의 미도달/도달, 활성 상태별 지연, 동일 fingerprint 평가와 프로젝트별 독립 실행을 검증한다.
- READY 존재 중 maintenance-only prompt, READY 없음에서 일반 planning과 단일 호출 결합, 평가 없음에서 파일 불변·기준선 전진을 검증한다.
- 최소 dependency-closed prerequisite 집합만 선택하고 마지막 prerequisite Merge 뒤 재평가하며 집합 밖 READY, 비eligible 후보와 cycle을 거부하는지 검증한다.
- refactoring DAG publication 뒤 priority보다 우선한 즉시 선택, DAG dependency 순서, 다른 프로젝트 scheduler 진행과 Planner mutex를 검증한다.
- 마지막 리팩터링 Merge 전에는 revalidation을 시작하지 않고, Merge 뒤에는 장벽 뒤 Worker보다 먼저 최신 origin에서 변경·no-change revalidation을 완료하는지 검증한다.
- revalidation의 stale snapshot, 제품 목표 불확실, base 변경, push 실패와 push 후 DB 중단에서 Worker를 시작하지 않고 동일 단계로 복구하는지 검증한다.
- 활성 주기 중첩 방지, 리팩터링 Merge 포함 기준선 전진, 새 입력 동시 발생, planning error/프로세스 중단 재개와 프로젝트 완료 알림 gate를 검증한다.
- 전체 verify에서 일반 READY 소진 정책, new-plan planning, dependency/priority 선택, Agent scope, latest-base publication, Requirement archive와 다중 프로젝트 scheduler가 약화되지 않아야 한다.

## Progress

- [ ] 유지보수 due와 일반 READY/planning 선택 순서 통합
- [ ] 유지보수 Planner protocol 및 combined/maintenance-only 호출 구현
- [ ] prerequisite-only selection과 재평가 구현
- [ ] refactoring barrier 즉시 선택 및 일반 Worker 수명주기 연결
- [ ] post-refactor READY 재검증과 기준선 완료 구현
- [ ] 단계별 중단·planning error 복구 및 PRODUCT 정합화

## Decision Log

- 새 유지보수 권한을 부분 활성화하면 현재 저장소의 남은 READY 계획을 막을 수 있으므로 G-027과 G-028은 dormant foundation으로 Merge하고 이 계획에서 전체 주기를 한 번에 활성화한다.
- 리팩터링 즉시 실행은 priority 값을 왜곡하지 않고 활성 유지보수 주기의 explicit selection override로 구현한다.
- prerequisite 집합과 refactoring DAG는 서로 다른 단계다. 전자는 최신 코드 확보 전 임시 실행 gate이고 후자는 구체적인 구조 문제가 최신 코드에서 확정된 뒤에만 게시한다.
- 사후 재검증 성공 전에는 장벽 뒤 계획이 최신 구조를 안전하게 반영했다고 볼 수 없으므로 프로젝트 완료와 Worker activation을 모두 막는다.
- R-001의 Merge 대기 비점유 정책은 G-026 충돌 복구와 이 계획의 최종 scheduler 경계가 준비된 뒤 G-031에서 활성화한다. 이 계획만으로 여러 Open PR을 허용하지 않는다.
