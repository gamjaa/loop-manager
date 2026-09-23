---
id: G-038
title: Final-Revision ExecPlan Archive Integrity
sources:
  - docs/requirements/[R-017] File-Authoritative ExecPlan Source Reconciliation.md
depends_on:
  - G-033
  - G-036
  - G-049
complexity: medium
risk: high
priority: critical
---

## Purpose

G-033의 MERGED ExecPlan 정리 경계를 G-036의 최초 게시 감사 정보와 최신 선언 cache 구분에 맞춘다. 완료 계획은 최초 게시 hash가 아니라 마지막으로 검증·동기화된 원격 파일의 현재 hash와 revision을 기준으로 내용 보존 이동하며, 계획 개정이나 정리 중 원격 경쟁이 있어도 최종 선언을 손실하거나 안전한 archive를 영구 대기시키지 않는다.

## Acceptance Criteria

- MERGED archive 후보는 최초 게시 path·hash가 아니라 G-036이 같은 고정 원격 revision에서 검증한 current active path, current content hash와 current plan revision SHA를 사용한다.
- 최초 게시 commit SHA·active path·content hash는 PR 불변 링크와 감사 이력으로 계속 보존되며 current hash와 다르다는 이유만으로 archive를 거부하거나 최초 값으로 덮어쓰지 않는다.
- source active file은 current cache와 같은 regular blob이어야 하고 destination은 결정적으로 계산한 `docs/plans/archive/`의 같은 파일명이어야 한다. 이동 전후 byte hash가 current hash와 같을 때만 내용 보존 rename으로 인정한다.
- plan title 또는 active path가 유효하게 바뀐 이력이 있으면 latest reconciled current path에서 archive하고, SQLite identity와 기존 PR 링크는 plan ID 및 최초 게시 정보로 계속 조회할 수 있다.
- archive 후보 계산 전에 G-036 reconciliation을 수행하고, 후보 검증·commit·push 사이에 base SHA나 plan current revision이 바뀌면 낡은 source를 이동하거나 commit을 게시하지 않고 최신 원격에서 후보를 다시 계산한다.
- archive operation/pending record는 후보의 current revision과 hash를 포함한다. plan 개정으로 operation identity가 바뀌면 이전 실패를 새 파일에 재사용하거나 source·destination을 삭제하지 않고 새 안전 검증으로 수렴한다.
- source가 이미 없고 destination에 같은 plan ID의 current hash가 있으면 성공으로 복구한다. 최초 hash만 일치하거나 active와 archive가 함께 존재하거나 current hash가 다르면 완료를 추정하지 않고 구체적인 pending 진단을 남긴다.
- archived historical catalog의 integrity 검사는 마지막 동기화된 final hash를 사용한다. archive 위치만으로 MERGED를 추정하지 않는 G-032의 GitHub/SQLite 상태 계약은 유지한다.
- 한 후보의 stale revision, hash 불일치 또는 path 충돌은 다른 안전한 MERGED 후보의 batch 이동, READY 선택, Planner, 프로젝트 완료와 알림을 막지 않는다.
- push 뒤 SQLite 기록 전 중단과 archive-only remote commit 재관측에서도 current revision/hash를 기준으로 멱등하게 성공 처리하며 중복 commit, force push 또는 내용 재작성을 만들지 않는다.

## Implementation

- G-033의 `execplan_archive.py` candidate/operation identity가 G-036 `ExecPlanDefinition`의 current path/hash/revision을 필수 입력으로 받게 하고, immutable publication fields는 PR link/audit adapter만 접근하게 한다.
- final declaration integrity와 already-moved tree 판정은 G-032 `execplan_catalog.py`의 historical snapshot validator와 하나의 pure hash/frontmatter API를 공유한다. archive dispatcher나 execution handler가 서로 다른 hash 규칙을 다시 구현하지 않는다.
- `execplan_archive_state.py` repository의 compare-and-swap key에 remote base와 current definition revision을 포함하고, stale operation은 삭제·덮어쓰기 대신 typed `superseded` 결과와 새 candidate identity로 전환한다.
- pre-commit/pre-push revision check와 원격 tree 복구는 G-033의 project-level housekeeping Git port에서 수행한다. active `ExecutionPhaseCoordinator`, `runtime_state.py`의 phase projection과 Worker publication 경계는 변경하지 않는다.

## Validation

- Worker는 `tests/test_execplan_archive.py`, `tests/test_execplan_archive_state.py`와 `tests/test_execplan_catalog.py`의 final-revision 직접 관련 targeted case만 실행한다.
- 최초 게시 뒤 title·body·metadata가 변경된 MERGED 계획을 current definition hash로 archive하고 최초 SHA·path·hash 및 PR link가 repository round trip에서 유지되는지 검증한다.
- 최초/current hash 차이, current source 변조, destination 충돌과 최초 hash만 일치하는 archive를 shared integrity API가 구분하는지 table test로 검증한다.
- candidate 뒤 revision 변경, commit·push 전 base race와 stale operation이 `superseded`로 수렴해 낡은 파일을 stage/push하지 않는지 fake Git port로 검증한다.
- push 후 DB 중단, 이미 이동된 동일 final blob, source/destination 공존과 여러 후보 일부 실패가 중복 commit·force push 없이 격리되는지 검증한다.
- 전체 verify에서 G-032 recovery, G-033 rename-only housekeeping, immutable PR links, archive-only fingerprint와 scheduler 비차단 회귀가 없어야 한다.

## Progress

- [ ] archive candidate/operation identity를 current definition token에 결합
- [ ] shared final-hash·historical integrity API 구현
- [ ] repository CAS와 superseded operation 재계산 구현
- [ ] project housekeeping pre-commit/pre-push 및 remote-tree 복구 통합
- [ ] immutable audit·final revision targeted 회귀 테스트 추가

## Decision Log

- G-033은 자동 이동과 비차단 재시도 경계를 제공하고, 이 계획은 R-017이 바꾼 hash 권위만 독립적으로 보강한다.
- 최초 게시 hash는 감사 정보이고 archive 대상의 최종 내용 증명이 아니다. final declaration hash와 최초 감사 hash를 별도 필드와 API로 유지한다.
- active 실행 재개 규칙은 G-037에 두어 archive housekeeping의 후보 검증과 게시 경쟁을 좁은 범위에서 검토할 수 있게 한다.
- Post-refactor revalidation: final archive integrity는 active execution lifecycle에 새 분기를 만들지 않는다. G-036 snapshot과 G-033 housekeeping repository/Git port 사이의 token 계약으로 제한해 `execution_handlers.py` 충돌을 피한다.
