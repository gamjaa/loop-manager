---
id: G-193
title: 미Merge dependency의 목표 우선순위 전파
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-075] 높은 우선순위 목표의 선행 작업 우선 처리.md
depends_on: []
complexity: medium
risk: medium
priority: critical
---

## Purpose

높은 우선순위 목표를 기다리게 하는 미Merge 선행 작업에도 목표의 우선순위를 반영한다. 현재 일반 READY·유지보수 prerequisite 선택의 선언값 비교를 유효값 비교로 바꾸고, 선택 근거를 기록한다. 실행 자격과 상위 단계 선택은 R-075가 보존하는 기존 계약을 따른다.

## Acceptance Criteria

- 같은 프로젝트의 게시·검증된 미완료 catalog에서 자신의 선언값과 자신을 직접·간접 미Merge dependency로 필요로 하는 모든 목표의 선언값 중 가장 높은 값을 유효 우선순위로 사용한다. MERGED에서 전파를 끝내며 priority 생략의 normal 해석과 기존 네 등급을 유지한다.
- critical A → low B와 독립 high C에서 실행 가능한 B를 먼저 선택한다. critical A → normal B → low D에서는 D가 C보다 먼저 선택되고, D·B의 실제 Merge 뒤에만 각각 B·A가 실행 가능해진다. 공유 선행 작업은 가장 높은 목표를 반영하며 자신의 더 높은 선언값을 낮추지 않는다.
- 기존 단계가 허용한 후보 안에서 priority를 비교하는 일반 READY, 재개 가능한 READY, planning 오류 보존 중 독립 READY와 유지보수 prerequisite 선택은 유효값·숫자 ID 오름차순을 사용한다. dependency 없는 catalog의 결과와 유지보수 리팩터링 DAG의 숫자 ID 선택은 유지한다. 후보 집합 밖 목표의 전파가 허용 집합을 넓히지 않는다.
- G-177(critical)의 미Merge 선행 G-170(high)은 critical로 비교되지만 문서·SQLite의 선언값은 high다. G-177은 G-170·G-176 모두의 실제 Merge를 기다린다. 유효값 계산으로 계획 metadata·dependency·실행 상태를 변경하지 않는다.
- 목표의 priority 하향, dependency 제거, 검증된 추가·삭제와 Merge 반영 뒤 다음 안전한 선택에서 전파를 다시 계산한다. 더 이상 필요한 관계가 아니면 이전 상향이 사라지고 다른 미완료 목표의 공유 관계는 유지된다. 같은 catalog·상태의 재시작에서 선택 결과가 같다.
- 높은 우선순위 경로에 PLAN_BLOCKED·PR_OPEN·사용자 판단 대기만 남으면 기존 정책이 허용한 독립 READY를 선택한다. 우선순위로 차단·승인·dependency를 해제하지 않으며 활성 작업 재개, 확정 PR 충돌 복구, Planner 입력 처리, 프로젝트 안전 gate와 유지보수 우선 처리·장벽을 보존한다.
- 선택 기록에서 선언값·유효값과 상향의 근거 목표·dependency 경로를 확인할 수 있다. 없는 ID·자기 의존·순환·불확실한 catalog 관계는 기존 검증·범위별 보존과 복구 경계로 처리하며 임의의 전파로 해당 작업을 시작하지 않는다.

## Implementation

- catalog/project_selection.py와 maintenance/maintenance_orchestration.py의 후보·dependency·정렬 경계를 재사용한다. 검증된 프로젝트 그래프에서 우선순위와 근거를 계산하는 공통 책임을 두고, 기존 priority 비교 소비자에 연결한다. 전체 그래프의 목표와 현재 단계의 실행 후보를 구분하며 리팩터링 DAG의 정렬은 유지한다.
- catalog reconciliation과 실제 Merge 반영 이후의 동일한 선언·상태를 사용한다. 전파값을 선언 cache에 저장하거나 설정·상태를 추가하지 않고, 불완전하거나 격리된 관계의 판단은 기존 catalog 검증·hold 정책을 따른다. G-165의 planning_preserved_state·미확정 대상과 전이 의존 계획 보류 및 안전한 실행 슬롯 해제 판정도 기존 후보 gate로 보존한다.
- 기존 선택·실행 로그 경계에 계산 근거를 전달한다. 구현 뒤 PRODUCT의 장기 선택 규칙을 R-075에 맞게 갱신한다. 공통 권한·설계·검증은 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조하며 지정된 루트 경로와 기존 helper·fixture를 유지한다.

## Validation

- tests/test_catalog_scheduling.py, tests/test_maintenance_orchestration.py의 기존 SQLite catalog·cycle fixture를 재사용해 직접/다단계/공유 전파, MERGED 단절, 숫자 ID 동률, priority 생략과 dependency 없는 결과의 최소 unit case를 추가·실행한다. G-177/G-170/G-176 사례에서 선택과 선언값 보존을 함께 확인한다.
- priority·dependency·추가/삭제·Merge 변경 뒤의 재계산과 같은 DB 재개, 차단·PR·판단 대기에서 독립 후보 선택, 잘못된 관계의 거부를 targeted unit test로 검증한다. planning 오류 보존 중 독립 후보에도 전파를 적용하되 미확정 대상·MERGED 경유 전이 의존 계획의 보류와 session·작업물 보존을 확인한다. 삭제 입력은 기존 검증된 catalog 변경 경계를 사용하고 취소된 목표를 실행 대상으로 합성하지 않는다.
- Manager 전체 verify는 최신 catalog 선택, 활성 작업·PR 충돌 복구·입력 변경 처리·프로젝트 안전 gate, 유지보수 prerequisite 후보 제한·due 우선 처리·숫자 ID DAG와 기존 단일 실행·승인 계약의 회귀를 확인한다. 선택 기록의 선언값·유효값·근거와 metadata·실행 이력 보존도 확인한다.

## Progress

- [x] 2026-10-06 기준 a197132e3aaaf91967665c4ed5e6e4fd6e90409c의 활성 명세·catalog와 현재 선택 코드·테스트를 대조했다. 일반·재개 READY 및 유지보수 prerequisite가 선언값만 비교하는 미충족 범위를 확인했다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-06 최신 기준 2fca86c3194a89934072172bc4abd98d245eb840에서 Merge된 G-165의 관련 문서·코드·테스트 변경을 다시 대조했다. 새 독립 READY 정렬 소비자와 미확정 대상 보류를 반영했으며 추가 구현 목표나 테스트 실행은 없다.
- [x] 구현 및 관련 targeted unit 검증: 공통 effective_priority 계산을 일반·재개·planning 오류 보존·유지보수 prerequisite 선택에 연결했다. 선언 cache와 실행 gate를 보존하고 선택 로그에 선언값·유효값·목표 경로를 추가했다. 관련 14개 unit test가 통과했다. 전체 verify는 Worker가 실행하지 않았다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-075가 구현 계획의 critical을 명시한다. 기존 catalog에는 이 목표가 없으므로 G-193을 추가하며 G-170 등 선행 작업의 선언값을 편집하지 않는다. 새 계획은 기존 선택 경계를 사용할 수 있어 선행 Merge dependency를 추가하지 않는다.
- complexity medium: dependency 그래프의 전파와 여러 기존 선택 소비자를 연결하지만 상태 전이나 게시 방식의 재설계는 필요하지 않다. risk medium: 오계산은 선택 지연·잘못된 처리 순서를 만들 수 있으나 선언·실행 자격을 보존하고 다음 선택에서 재계산해 복구할 수 있다. 안전 gate·후보 제한의 회귀는 별도로 검증한다.
- G-176·G-179의 확정 범위·cycle 종료·barrier 목표와 R-075의 정렬 변경은 독립적이다. R-054·R-074의 상위 선택 정책을 바꾸지 않고, 해당 계획이나 snapshot 밖 실행 계획을 개정하지 않는다.
- 이전 결과 검증 중 G-165가 Merge돼 관련 문서가 변경됐다. 최신 기준의 catalog/project_selection.py가 추가한 planning 오류 보존 중 priority 비교도 같은 R-075 목표에 포함한다. 슬롯 해제·미확정 범위 판단은 재사용하며 G-165 파일·완료 이력을 수정하거나 중복 계획하지 않는다.

- 공통 계산은 `catalog/effective_priority.py`에 두고 매 선택의 검증·hold 제외 view에서 재계산한다. 불완전·순환 closure는 전파·실행 후보에서 제외하고 planning 미확정 대상과 전이 의존 계획의 보류를 유지한다. 유지보수 범위 계산과 리팩터링 DAG의 숫자 ID 정렬은 재사용한다.
- 기존 루트 진입·composition 파일 `loop_manager.py`에는 선택 로그 effect 연결만 추가했다. 새 루트 항목·파일 이동은 없으며 기존 Linux verify는 unittest discovery와 전체 compile 경로를 이미 제공해 수정이 필요하지 않다.
- targeted 검증의 초기 두 실패는 새 테스트 fixture의 enum 저장값과 cycle 범위 초기화 오류였다. fixture를 수정하고 동일 명령을 재실행해 14개 모두 통과했다.
