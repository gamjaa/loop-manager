---
id: G-211
title: revision 경로의 폐기된 Requirement archive 결합 정리
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

정상 execution revision에서 항상 빈 값으로 설치되는 Requirement archive 준비와 도달 불가능한 archive 분기·effect 결합을 정리한다. 일반 planning이 Requirement retirement를 판단하는 현재 책임을 유지한다.

## Acceptance Criteria

- 정상 revision guard는 실행 중 Requirement archive 계산/target 작성/복구 service를 요청하지 않는다. 빈 preparation 뒤의 불필요한 분기와 callback 결합이 정상 경로에서 제거된다.
- document approval identity·Worker/revision 판정·후보 검증 무효화·수동 제출의 기존 경계를 유지한다. 저장된 과거 archive target/token·session·감사를 삭제하거나 새로운 실행 입력으로 채택하지 않는다.
- 과거 저장값 읽기와 공개 façade/effect 호출 호환은 필요한 호환 경계에 남긴다. 사용자 동작·공개 계약(façade/effect 호출)·저장 형식(schema/직렬화/token 의미)·일반 planning의 Requirement 이동 및 실패 보존/재개를 보존한다.

## Implementation

- `loop_manager_internal/execution/execution_revision_handler.py`의 정상 준비·판정에서 archive 전용 결합을 걷어낸다. `execution_revision` 및 관련 effect 조립의 호환 소비자를 보존하고 실제로 쓰이는 과거 기록 읽기까지 제거하지 않는다.
- 기존 문서 승인·manual candidate·candidate token 책임을 재사용한다. Requirement archive의 과거 실행 정책을 되살리거나 별도 retirement 서비스·migration을 만들지 않는다.

## Validation

- Worker targeted unit: `tests.test_execution_revision_gate`와 `tests.test_execution_revision`의 stale archive target/후보 무효화 사례. archive 계산 미호출, 기존 target/token/session 보존, façade 호환과 일반 revision 실패를 확인한다.
- Manager 전체 verify: Worker→verify→Review→게시 gate, 수동 후보, 일반 planning의 retirement/내용 보존 이동과 중단 재개 회귀를 유지한다. Reviewer는 과거 감사 삭제·실행 중 archive 재도입·token 의미 변경을 확인한다.

## Progress

- [x] 저장된 평가 발견 3의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/execution/execution_revision_handler.py:385`의 빈 archive preparation과 저장된 후속 분기 판단을 재사용했다. ancestry 차단 결함이나 과거 기록 폐기를 주장하지 않는다.
- G-182는 rebase 기록 분류/성공 결과 재사용, G-203은 ExecPlan archive 경로로 목표가 다르다. 현재 계약 안에서 정리하므로 추가 기존 READY prerequisite는 없다.
- complexity medium은 정상/호환 소비자와 effect 경계 정리, risk medium은 revision 진행/호환 오류이며 기존 checkpoint·target/token·session 보존과 재개로 복구한다.
