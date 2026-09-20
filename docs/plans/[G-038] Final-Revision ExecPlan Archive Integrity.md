---
id: G-038
title: Final-Revision ExecPlan Archive Integrity
sources:
  - docs/requirements/[R-017] File-Authoritative ExecPlan Source Reconciliation.md
depends_on:
  - G-033
  - G-036
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

- G-033 candidate와 operation identity를 G-036 reconciled definition snapshot의 current path/hash/revision에 결합하고 immutable publication fields 사용을 PR link/audit adapter로 한정한다.
- active-to-archive rename 검증과 G-032 historical catalog integrity API가 동일한 final declaration hash를 사용하도록 통합하되, `exec_plans`의 최초 게시 감사 column은 변경하지 않는다.
- archive dispatcher의 pre-commit 및 pre-push compare-and-swap에 remote base와 current plan revision을 함께 검사하고, stale operation은 삭제나 덮어쓰기 없이 superseded 진단으로 재계산한다.
- 이미 이동된 원격 tree 복구는 archive blob의 ID/frontmatter와 final hash를 모두 확인한 뒤 operation row만 완료 처리한다.

## Validation

- Worker는 ExecPlan archive candidate/operation과 historical catalog integrity의 직접 관련 test class 또는 새 final-revision archive test module만 targeted unit test로 실행한다.
- 최초 게시 뒤 title·body·metadata가 변경된 MERGED 계획을 final hash로 archive하고 최초 hash·path·PR 링크는 유지하는지 검증한다.
- 최초 hash와 current hash가 다른 정상 개정, current source 변조, destination 충돌과 최초 hash만 일치하는 archive를 구분하는지 검증한다.
- candidate 계산 뒤 plan revision 변경, commit 전·push 전 base race와 stale operation recovery가 낡은 파일을 이동하지 않고 최신 후보로 재계산되는지 검증한다.
- push 후 DB 중단, 이미 이동된 동일 final blob, source와 destination 공존 및 여러 후보 중 일부 실패가 멱등하고 격리되는지 검증한다.
- 전체 verify에서 G-032 ID/dependency recovery, G-033 rename-only publication, immutable PR links, archive-only fingerprint와 scheduler 비차단 동작이 유지돼야 한다.

## Progress

- [ ] archive 후보와 operation identity를 current revision/hash에 결합
- [ ] final hash 기반 rename 및 historical integrity 검증 구현
- [ ] stale revision/base 경쟁과 이미 이동된 tree 복구 구현
- [ ] 최초 감사 정보·PR 링크 불변 회귀 테스트 추가

## Decision Log

- G-033은 자동 이동과 비차단 재시도 경계를 제공하고, 이 계획은 R-017이 바꾼 hash 권위만 독립적으로 보강한다.
- 최초 게시 hash는 감사 정보이고 archive 대상의 최종 내용 증명이 아니다. final declaration hash와 최초 감사 hash를 별도 필드와 API로 유지한다.
- active 실행 재개 규칙은 G-037에 두어 archive housekeeping의 후보 검증과 게시 경쟁을 좁은 범위에서 검토할 수 있게 한다.
