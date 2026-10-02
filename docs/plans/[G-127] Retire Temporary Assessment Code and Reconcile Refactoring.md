---
id: G-127
title: Retire Temporary Assessment Code and Reconcile Refactoring
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-119
  - G-120
  - G-121
  - G-122
  - G-123
  - G-124
  - G-125
  - G-126
complexity: medium
risk: high
priority: critical
---

## Purpose

기능별 리팩터링이 끝난 뒤 G-060 inventory·발견·코드/테스트 대응표를 실제 결과와 대조한다. 조사에 활용한 `full_assessment.py`와 그 밖에 이번 작업 중 생긴 임시 코드를 제거하고, 아직 쓰이는 일회성 평가·중단 복구 기능은 기존 상시 유지보수 경계로 통합한다.

## Acceptance Criteria

- G-060의 모든 구조·중복 발견은 담당 계획의 실제 결과, 동일 책임으로 유지한 근거 또는 사용자 결정이 필요한 범위와 연결된다. 미처리된 이번 요청 범위를 단순히 FUTURE_IDEAS로 보내거나 완료로 표시하지 않는다.
- `full_assessment.py` 자체와 runtime/test/verify의 해당 module import·compile 참조가 제거된다. G-063에서 이동됐다면 이동한 module도 제거하며 이름만 바꾼 같은 임시 분석 구현을 남기지 않는다.
- runtime에서 여전히 필요한 source-bound inventory·결과 검증·평가/문서 검토 handoff는 기존 유지보수 service에 통합한다. G-125 assessment port가 그 경로를 호출하고 공통 inventory/validation과 중복 구현하지 않는다.
- 미완료 self 일회성 평가, 이전 serialized report·source hash, prerequisite/refactoring/revalidation 관계와 재시작 호환성을 보존한다. 완료 marker·ledger·DB column·감사 자료를 삭제하거나 임의 완료·skip으로 전환하지 않는다.
- 코드에만 남은 과거 계획 ID 전용 분석 prompt·일회성 중복 parser는 실제 소비를 확인해 정리한다. 기존 보고서 형식의 소비가 남으면 최소한의 호환 reader를 유지하고 history 문서는 바꾸지 않는다.
- 이번 리팩터링에서 만든 임시 script·report·stub·미사용 import·compatibility wrapper는 확인된 소비가 없을 때 제거한다. 사용자 파일·운영 DB·로그·백업·평가 감사 기록과 과거 ExecPlan은 정리 대상이 아니다.
- root/package의 책임 분리, 테스트의 단일 수집, 순환 import 부재와 Linux/Windows verify 대상의 일치가 확인된다. 검증 범위·timeout·assertion을 약화해 완료시키지 않는다.

## Implementation

1. 모든 dependency Merge 후 최신 원격 revision에서 G-060 inventory를 현재 추적 source·test·launcher 집합과 대조한다. 같은 source의 기존 평가 근거를 재사용하고 변경된 파일·관계만 다시 확인한다. 크기 변화와 남은 결합을 Progress에 기록하되 줄 수만으로 성공을 판단하지 않는다.
2. 삭제 전 `full_assessment`의 모든 runtime/test/verify 소비와 저장 report를 추적한다. 원본 module의 마지막 inventory와 이전/현재 경로 대응을 이 계획에 남겨 제거 뒤에도 판단 근거를 보존한다.
3. 기존 maintenance inventory·finding validation·assessment service를 통해 source hash와 pending legacy report를 읽는다. 필요한 경우 작은 호환 reader를 같은 책임 경계에 둔다. 신규 평가를 과거 G-051 완료 finding에 영구 결합하는 임시 의존성은 제거하되 과거 보고서를 invalid 처리하지 않는다.
4. 대체 경로로 완료/미완료 일회성 평가, 문서 후보와 cycle 재개를 유지한 뒤 module 및 불필요한 임시 구현을 삭제하고 모든 소비자를 갱신한다. 두 verify에서 삭제한 파일을 compile하지 않으며 새 package와 test는 빠짐없이 검증하게 한다.
5. 실제 사용 중인 공개 facade는 유지하고 소비가 없는 임시 wrapper만 제거한다. 이번 작업 범위의 작은 잔여 결함·중복은 관련 책임 안에서 수정하며, 원래 계획의 AC가 미충족이면 이 계획을 완료하지 않고 해당 근거와 필요한 처리를 기록한다.

## Validation

- Worker는 평가 대체 경로와 legacy report/중단 복구의 targeted unit test, 수정한 잔여 경계의 최소 unit test만 실행한다. 순수 문서/참조 정리만 한 부분은 테스트 비실행 사유를 명시한다.
- 과거 완료 marker에서 재평가하지 않음, 미완료 cycle 재개, source drift·report 변조 거부, 기존 finding/document handoff와 게시 전 성공 기준 보존을 검증한다.
- 실행 없는 파일/import/소비 관계 조사로 임시 module·미사용 wrapper·중복 TestCase 수집 부재, source/test 대응표의 누락 부재와 verify 경로를 확인한다. 감사 문서나 저장 데이터의 문자열은 실행 참조와 구분한다.
- Manager는 현재 운영체제의 전체 verify를 실행하고 Reviewer는 전달된 결과와 코드·test·verify diff를 검토한다. Windows/Linux의 지정 verify 경로와 검증 대상은 모두 보존한다.
- 모든 관련 계획의 targeted test·Manager verify·Review와 사용자 PR Merge가 충족돼야 이번 리팩터링이 완료된다. 문서 작성만으로 완료 또는 SQLite MERGED를 기록하지 않는다.

## Progress

- [x] G-060 발견·경로·테스트 대응표를 현재 구현과 대조 (아래 기록 및 G-060 Implementation)
- [x] 상시 유지보수 평가 경계로 통합하고 legacy/pending cycle 보존
- [x] `full_assessment.py` 및 확인된 runtime/test/verify 참조 제거
- [x] targeted unit test와 정적 참조·수집 집합 확인
- [ ] Manager 전체 verify, 독립 Review 및 모든 관련 사용자 Merge

## Decision Log

- 2026-10-02: 사용자는 전수 평가 준비 코드를 활용하고 작업 완료 후 임시 코드를 제거하도록 요청했다. 현재 `full_assessment.py`는 runtime의 일부이므로 필요한 기능을 보존한 뒤 module·중복·임시 결합을 제거한다. 단순 선삭제나 완료 marker 조작으로 실행을 우회하지 않는다.
- 새 영구 보고서·분석 daemon·schema migration은 만들지 않는다. 기준선·발견·결정은 G-060과 담당 ExecPlan, 이미 존재하는 유지보수 감사 경계에 남긴다.
- 이 마지막 계획은 잔여 범위 확인과 정리를 담당하며 검증·Review·Merge를 대신하지 않는다. 원래 요청과 무관한 기능·정책은 추가하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.

- 2026-10-02 구현 대조: 기준 HEAD `0795a1a90c289d174b1e7ca55d4fab7f993a14d8`와 작업 tree를 조사했다. 추적 루트 13개는 G-060의 유지 근거와 일치하며 새 루트 항목은 없다. `.git`은 worktree 제어 경로, 읽기 전용 `.aws/`·`.codex/`와 파생 `__pycache__/`는 소유권·이동 근거가 없으므로 건드리지 않았다. 작업 tree의 Python source 396개 중 두 verify가 runtime/package 198개를 같은 목록으로 compile한다. TestCase 이름 중복은 없었고 실제 test module은 167개다. 숫자는 coverage나 전체 verify 통과를 뜻하지 않는다.
- 제거 전 `full_assessment.py`의 마지막 대응: `source_inventory` → `maintenance_file_inventory.source_inventory` (기존 report의 경로·SHA256 계약), `inventory_prompt`·`assessment_prompt` → `maintenance_planning`, `parse_assessment_result` → `maintenance_validation`, `FullAssessmentError` → `maintenance_file_inventory.AssessmentError`, `RESULT_PREFIX` → `maintenance_validation.RESULT_PREFIX`. 소비자는 `planning/assessment_port.py` → `assessment_service.py`와 `tests/test_maintenance_planning.py`·`tests/test_planning_assessment.py`였고 두 verify의 compile 참조를 제거했다. 저장 report는 `maintenance_cycles.full_assessment_result`와 `documents/document_drafts.py`가 계속 읽으며 schema·완료 marker·감사 자료는 유지한다.
- 저장 report reader는 같은 revision의 현재 inventory와 각 finding의 source hash를 검증한 뒤 기존 JSON 형식으로 prompt에 전달한다. 과거 `G-051` resolved finding은 계속 읽히지만 신규 평가에는 강제하지 않는다. 오류가 있으면 Planner를 호출하지 않고 기존 bounded validation retry를 사용한다. 기존 prerequisite/refactoring/revalidation, publication과 문서 후보 handoff 코드는 변경하지 않았으며 관련 좁은 회귀 test로 재개·drift·완료 marker를 확인했다. 요청한 R-028은 내용 변경 없이 archive로 이동했다.
