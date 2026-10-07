---
id: G-208
title: ExecPlan archive의 선택적 검증 port 명시화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-014] Automatic Completed ExecPlan Archiving.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

archive dispatcher가 Protocol 밖의 `definitions_current`를 동적으로 찾아 검증 계약을 달리 적용하는 구조를 명시적인 선택적 capability 경계로 정리한다. 실제 adapter의 검증과 기존 검사가 없는 adapter의 호환 동작을 보존한다.

## Acceptance Criteria

- 기본 archive port와 선택적 원격 definition 검증의 지원 여부가 선언된 계약에서 구분된다. concrete adapter의 검사 누락을 성공으로 오인하지 않고 의도된 legacy 생략은 기존대로 처리한다.
- 후보 CAS, 실제 Merge 증명, bytes/mode 보존, rename-only commit 검사, 원격 경쟁·push 실패의 pending 및 다른 작업 지속이 유지된다. 잘못된 저장 commit과 검증 예외는 기존 재준비 경로를, 새 commit 검사 실패는 pending·push 금지를 유지한다.
- 사용자 동작·공개 계약(façade 호출)·저장 형식과 필드·최초 게시 감사·PR 링크를 보존한다. G-203의 미결정 path/파일명 정책을 선택하거나 archive 목적지 계산을 변경하지 않는다.

## Implementation

- `loop_manager_internal/catalog/execplan_archive.py`의 dispatcher와 `loop_manager_internal/catalog/archive_git.py` adapter 사이에 선택적 검증 책임을 표현한다. 기존 port/adapter를 재사용하고 일반 capability framework를 추가하지 않는다.
- `tests/test_execplan_archive.py`의 FakeGit에 필요한 좁은 override를 사용해 생략·지원·거부·예외를 구분한다. pending 저장·재사용과 실제 검증 순서는 유지한다.

## Validation

- Worker targeted unit: `tests.test_execplan_archive`의 capability 및 commit 검증 사례. 저장 commit 불일치/검증 예외의 재준비, 새 rename-only 검사 실패의 pending·push 부재, 후보 CAS 경쟁을 확인한다.
- Manager 전체 verify: archive 재시작·base 경쟁·정상 후보 분리·MERGED 보존과 catalog/PR identity 호환 회귀를 유지한다. Reviewer는 검증 생략이 concrete adapter로 확대되지 않았는지 확인한다.

## Progress

- [x] 저장된 평가 발견 0·13의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: 모듈 manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/catalog/execplan_archive.py:470`의 `getattr` fallback과 `tests/test_execplan_archive.py:132`의 항상 성공하는 fake. 의미 검토를 반복하지 않았다.
- G-203은 경로 identity 결정이며 이 선택적 검증 경계와 별개다. 추가 기존 READY prerequisite는 없다. complexity low는 기존 adapter의 국소 계약 표현, risk medium은 archive 진행 지연/검사 누락 영향이며 보존된 원본·pending과 재준비로 복구한다.
