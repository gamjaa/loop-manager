---
id: G-209
title: 승인 문서 게시의 Git 변경 관측 재사용
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

승인 문서 적용과 최종 worktree 검증에 중복된 Git 변경 경로 관측을 한 책임으로 모아 두 검사의 관측 규칙이 따로 바뀌지 않게 한다.

## Acceptance Criteria

- `apply_approved_bytes`와 `validate_exact_worktree`가 동일 base의 unstaged/cached 변경 및 untracked 경로 수집을 재사용한다. 각 호출 시 새로 관측하며 적용 전 결과를 최종 검증 성공으로 재사용하지 않는다.
- 적용 전에는 원본 또는 이미 적용된 승인 bytes만 허용하고, 최종 검사에서는 정확한 대상 집합·bytes/diff·mode·symlink 및 untracked 거부를 각각 유지한다. Git 관측 실패는 전파하고 적용/게시 성공을 합성하지 않는다.
- 사용자 동작(승인 의미·반복 적용·중단 재개)·공개 계약(호출/예외)·저장 형식(draft/결정/감사)과 CRLF 원문을 보존한다. 승인되지 않은 파일을 수정하지 않는다.

## Implementation

- `loop_manager_internal/documents/document_publication.py` 안에서 Git 변경 관측 경계만 작은 helper로 추출한다. 기존 `_git`와 문서 target 검증을 재사용하고 적용과 최종 검증의 허용 조건은 각 책임에 남긴다.
- 문서 Review·원격 원본/승인 identity 검사와 쓰기 순서를 유지한다. 새로운 승인 양식·저장소나 Git adapter를 만들지 않는다.

## Validation

- Worker targeted unit: `tests.test_maintenance_state`와 `tests.test_execution_documents`의 문서 적용/검증 사례. 반복 적용, 무관한 staged/unstaged/untracked 편집, mode/symlink 거부, CRLF, Git 실패와 적용 후 변경을 확인한다.
- Manager 전체 verify: 명시적 문서 Review·최종 승인 diff·원격 drift 거부와 중단 재개·결정 감사 보존을 유지한다. Reviewer는 공통 관측으로 단계별 허용 범위가 합쳐지지 않았는지 확인한다.

## Progress

- [x] 저장된 평가 발견 1의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/documents/document_publication.py:114`의 diff 경로 수집과 저장된 두 함수 중복 판단을 재사용했다.
- G-173의 사용자 판단 identity, G-171의 Worker 문서 권한을 바꾸지 않는다. 기존 READY prerequisite는 없다. complexity low는 파일 내부 관측 추출, risk medium은 승인 검사 오류의 제한된 영향이며 원본/승인 bytes·draft 감사와 기존 거부 경계로 복구한다.
