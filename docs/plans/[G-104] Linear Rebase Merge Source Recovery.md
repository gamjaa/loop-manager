---
id: G-104
title: Linear Rebase Merge Source Recovery
sources:
  - docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md
depends_on:
  - G-099
complexity: medium
risk: high
priority: critical
---

## Purpose

G-058의 소유 branch에는 과거 `main`을 병합한 commit이 있다. 기본 선형 rebase의 Git todo는 그 merge commit을 재생하지 않지만, v2 증거는 이를 원본 순서에 기록해 단일 부모 commit으로 읽다가 반복 실패한다. 현재 충돌과 저장된 실행 증거를 보존하면서 실제 재생 순서를 검증한다.

## Acceptance Criteria

- 새 관리 rebase는 Git의 기본 선형 재생에 포함되는 non-merge 원본 commit만 고정하고 순서대로 검증한다.
- 기존 v2 intent가 merge commit을 포함하면 저장된 목록을 수정하지 않는다. 원본 이력과 정확히 일치하고 빠진 merge가 target에 이미 포함된 parent를 자동 병합한 결과일 때만 실제 재생 목록으로 투영한다.
- 수동 merge 해결 내용, target 밖의 parent, 변조된 목록 및 재생 commit 누락·변조는 계속 차단한다.
- G-058처럼 이미 충돌 중인 실행은 같은 journal·checkpoint·Worker 교정 흐름에서 재개하고 완료 전 기존 검증·Review·게시 gate를 유지한다.

## Implementation

- `managed_rebase_evidence.py`에서 기본 선형 rebase의 원본 목록과 이전 v2 목록의 제한된 호환 판정을 분리한다.
- `manager_git_journal.py`는 봉인된 원본 목록을 identity 비교에 유지하고 검증된 재생 목록만 commit 대응에 사용한다. `execution_handlers.py`의 중단된 시작 단계도 같은 호환 판정을 적용한다.

## Validation

- Worker는 `tests/test_managed_rebase_evidence.py`의 관련 targeted unit test만 실행한다.
- 실제 Git 이력에서 자동 merge를 포함한 과거 v2 목록의 충돌 중 재개와 완료를 확인하고, 수동 merge 내용·변조된 목록을 거부한다.
- 전체 verify는 Loop Manager가 기존 스크립트로 실행한다.

## Progress

- [ ] 기본 선형 rebase 원본 목록 수정
- [ ] 저장된 v2 merge 목록의 제한된 복구와 회귀 검증

## Decision Log

- G-099/G-100의 단계별 증거 조건은 유지하고 원본 목록에 merge가 잘못 들어간 경우만 다룬다.
