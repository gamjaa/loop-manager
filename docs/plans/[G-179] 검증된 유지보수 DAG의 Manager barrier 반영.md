---
id: G-179
title: 검증된 유지보수 DAG의 Manager barrier 반영
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
  - docs/requirements/[R-074] Single-Pass Maintenance Cycle.md
depends_on:
  - G-176
  - G-178
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: medium
risk: high
priority: normal
---

## Purpose

검증된 리팩터링 DAG의 말단 dependency 추가를 Agent 재작성 대신 Manager의 허용된 barrier 작업으로 수행한다.

## Acceptance Criteria

- Planner가 판단한 유효 DAG의 모든 말단을 active barrier mode의 적격 READY snapshot에만 추가한다. 확정된 리팩터링의 미Merge 선행 작업과 그 dependency closure는 장벽 대상에서 제외해 역방향 의존성과 순환을 만들지 않는다. ID/path/title/sources·본문·다른 metadata와 기존 dependency 순서를 보존하고 기존 항목을 중복하지 않는다.
- branch/worktree/reservation/session/PR이 있거나 snapshot/cycle 밖인 계획은 편집하지 않는다. 의미 있는 구조 개정은 Planner 역할에 남기고 R-074가 없앤 사후 재검증을 복원하지 않는다.
- 실제 자동 diff를 전체 catalog 검증·게시·검토에 포함한다. 원격/snapshot 변경·중단/재시작에서 보류 자료와 사용자 변경을 보존하고 검증된 한 batch의 원격 확인 뒤 catalog를 확정한다.

## Implementation

- maintenance/maintenance_validation.py와 planning/planner_validation.py 및 기존 mutable snapshot·barrier·publication 경계를 재사용한다. DAG 판단 결과와 대상 token을 받은 Manager가 기존 선행 closure 계산으로 대상을 확정한 뒤 최소 dependency 편집을 수행한다. Agent가 직접 작성하던 장벽 diff 검증은 Manager가 생성한 실제 diff에도 유지한다.
- 일반 Planner의 권한으로 이 편집을 확대하지 않고 barrier proof와 실제 diff를 대조한다. 기존 순서/내용 보존을 결정적 코드로 검증한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- maintenance authority·planning·publication 테스트에서 다중 DAG 말단, 선행 dependency closure의 장벽 제외, 기존 순서/중복, 소유 대상·cycle 이탈·snapshot 경쟁·재시작의 최소 unit case를 실행한다.
- Manager 전체 verify는 dependency gate·단일 게시·원자 catalog 반영, 사용자 승인/실행 계획 보호와 R-074의 종료 조건을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 b8ac37e의 G-176 구현과 barrier 검증을 확인함. 선행 closure 제외를 보존하면서 남은 Manager 자동 편집만 계획함.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 확정 dependency의 제한된 편집으로 complexity medium, 소유 계획 오수정·barrier 우회 영향으로 risk high다.
- G-176이 구현한 선행 closure와 주기 종료는 재구현하지 않는다. 아직 Agent 작성에 의존하는 장벽 편집만 Manager로 옮기며 동일한 검증·게시 경계를 사용한다.
