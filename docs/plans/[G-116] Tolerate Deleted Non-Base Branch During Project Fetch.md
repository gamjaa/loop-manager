---
id: G-116
title: Tolerate Deleted Non-Base Branch During Project Fetch
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: medium
priority: high
---

## Purpose

PR Merge 후 사용자가 원격 작업 branch를 삭제할 수 있다는 기존 제품 규칙을 유지한다. `remote_base()`가 원격 branch 목록을 읽은 뒤 `_recorded_base_fetch()`로 각 branch를 다시 조회하는 사이 삭제가 발생하면 현재는 프로젝트 전체가 실패한다. 성공한 원격 조회에서 non-base branch 부재가 확정된 경우에만 해당 fetch를 생략한다.

## Acceptance Criteria

- 목록 조회 뒤 삭제된 non-base branch는 tracking ref를 변경하지 않고 건너뛰며 기준 branch 검증과 후속 PR 처리를 계속한다.
- 기준 branch 부재, CLI 실패, 유효하지 않은 응답과 OID는 계속 실패한다.
- 게시·rebase·충돌 복구 등 특정 branch를 요구하는 직접 fetch 호출은 삭제된 branch를 성공으로 처리하지 않는다.
- 기존 fetch journal, CAS와 worktree·로컬 변경 보존 경계를 유지한다. stale tracking ref를 삭제하거나 branch를 재생성하지 않는다.

## Implementation

1. 성공한 `ls-remote`의 빈 결과를 나타내는 구체적 RuntimeError 하위 예외를 추가한다.
2. `remote_base()`의 non-base 반복 조회에서만 해당 예외를 처리한다.
3. unit mock으로 목록과 개별 조회 사이 삭제 및 오류 구분을 검증한다. 전체 verify 스크립트의 자동 test discovery를 유지한다.

## Validation

- Worker: `python3 -m unittest tests.test_project_fetch_branch_deletion -v`로 이번 변경의 unit test만 실행한다.
- Manager 전체 verify: 기존 fetch journal 중단 복구, PR Merge cleanup, 게시·충돌 복구와 신규 branch 삭제 unit 회귀를 확인한다.

## Progress

- [x] branch 부재 예외와 non-base fetch 처리
- [x] targeted unit test: 2026-10-01, 신규 unit test 6개 통과
- [ ] Manager 전체 verify 및 Reviewer 확인 (외부 실행 대기)

## Decision Log

- 2026-10-01: 사용자가 G-115 PR Merge 후 원격 branch 삭제 시 OID 확정 오류를 보고했다. 등록 base는 DB에서 main으로 확인됐다. 기존 PRODUCT와 ARCHITECTURE는 삭제된 원격 branch에서도 저장 PR head로 Merge cleanup을 허용한다. branch 목록과 개별 조회 사이 삭제 경쟁을 수정하며 문서 규칙·게시 권한·schema는 변경하지 않는다.
- 현재 환경의 GitHub DNS 조회가 실패해 실제 원격 상태는 확인하지 못했다. 이 수정은 코드로 확인된 동일 오류 경로를 mock으로 재현한다. DB의 G-115 상태는 PR_OPEN이며 수동으로 변경하지 않는다.
- 기존 Linux verify 스크립트는 `unittest discover -s tests`로 신규 unit test를 포함하므로 변경할 필요가 없다. Worker는 전체 verify와 integration test를 실행하지 않았다. 전체 verify, Reviewer와 사용자 Merge 전에는 계획을 완료로 간주하지 않는다.
