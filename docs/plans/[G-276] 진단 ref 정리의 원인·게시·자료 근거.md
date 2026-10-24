---
id: G-276
title: 진단 ref 정리의 원인·게시·자료 근거
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-083] 충돌 해결 후 원격 진단 브랜치 정리.md
depends_on: []
complexity: high
risk: medium
priority: normal
---

## Purpose

충돌 진단 ref의 원인·게시·로컬 자료를 연결해 정리 가능 여부를 재검증할 근거를 만든다. 현재 생성·재사용과 원문 보존을 재사용하며 이 작업에서는 원격 ref를 삭제하지 않는다. 실제 삭제 경계는 별도 사용자 판단 대기 작업에서 다룬다.

## Acceptance Criteria

- R-083의 후보는 저장된 Manager 게시 성공·정확한 ref/OID·등록 origin·작업·충돌 원인으로 식별한다. prefix·작업 상태·로컬 충돌 해소만으로 소유권이나 정리 자격을 인정하지 않는다. 원래 작업 브랜치가 없는 planning 실패와 충돌 외 원인은 제외한다.
- 실제 충돌과 Git operation의 해소, 보존 작업물을 포함한 정상 작업 브랜치의 원격 반영, 해당 원인의 SQLite 해소 확정을 각각 확인한다. PR Merge 대기나 다른 차단의 존재만으로 판정을 미루지 않으며 다른 원인·실행 상태는 바꾸지 않는다.
- 같은 ref의 모든 원인과 진행 중 보존·게시 사용을 확인하고 여러 ref를 각각 판정한다. 미해결·미확정 사용자가 하나라도 있으면 보존한다. 같은 내용의 ref 재사용도 원인 관계를 잃지 않는다.
- 원격 ref 없이 기존 로컬 보존 영역에서 원문 파일·diff·blob·manifest와 모드를 복구할 수 있음을 확인한다. 부모 없는 commit도 동일하게 검사하며 OID 문자열·일시적 Git 객체만으로 자격을 만들지 않는다. 자료 누락·손상·identity 불일치에서는 보존 이유를 남긴다.
- 기존 기록으로 충분히 증명되는 과거 ref도 같은 판정 대상이다. 기록이 부족한 대상은 보존하고 namespace 전체 탐색·삭제나 근거 추정은 하지 않는다. 원문·session·실패·게시 intent·확정 감사는 유지한다.
- 정리 준비 근거는 기존 상태 원본과 원인·게시 generation에 결합한다. 관측 중 원격·사용자·자료·소유권이 달라지면 확정하지 않고 재관측한다. 준비 실패가 작업 재차단·독립 진행 중단·새 알림이나 검증 성공을 만들지 않는다.

## Implementation

- [ ] `loop_manager_internal/storage/blocked_checkpoint_state.py`의 `diagnostic_preservations`, `blocked_diagnostic_publications`, `project_checkpoint_results`를 재사용해 ref별 게시 identity와 복수 원인·진행 중 사용 관계를 보존한다. 정리 준비·보류 근거는 호환 가능한 저장 변경으로 추가하고 기존 intent/감사를 초기화하지 않는다.
- [x] `loop_manager_internal/recovery/diagnostic_preservation.py`의 원문 bundle·manifest·내용 identity를 읽어 로컬 복구 가능성을 검증한다. 과거 진단 방식에 같은 증명이 없으면 보존하며 기존 생성·비밀값 제외·private index 정책은 유지한다.
- [ ] `loop_manager_internal/recovery/project_blocked_recovery.py`, `checkpoint_service.py`, `scoped_blocking.py`와 `loop_manager_internal/git/publication_service.py`의 실제 정상 branch 게시·원인 해소 경계를 조사해 근거를 연결한다. 원인 해소와 원격 반영의 순서가 모두 충족된 후에만 후보를 준비하며 현재 ACTIVE row가 없다는 사실을 해소 증거로 쓰지 않는다.
- [ ] 후보 판정은 부작용과 분리된 책임으로 만들고 현재 저장·관측 helper를 재사용한다. 별도 모듈이 필요하면 `loop_manager_internal/recovery/diagnostic_cleanup.py`는 예정 경로다. 최종 삭제자가 현재 근거를 다시 확인할 수 있는 입력을 제공한다.
- [ ] `tests/test_diagnostic_preservation.py`, `tests/test_project_diagnostic_publication.py`, `tests/test_scoped_blocking.py`의 fixture를 재사용한다. 정리 판정 전용 `tests/test_diagnostic_cleanup.py`는 예정 파일이며 원인·게시·자료의 조합은 모의 Git 관측과 실제 SQLite/파일로 검사한다. PRODUCT·ARCHITECTURE에는 구현된 준비·보존 책임과 실제 삭제 미구현 상태를 구분해 반영한다.

## Validation

- 정상 게시 및 원인 해소의 순서별 조합, 로컬 해소만 완료, 미게시·미확정 결과, PR Merge 대기와 다른 차단 잔존에서 후보 판정과 다른 상태 불변을 확인한다.
- 같은 ref의 복수 원인·재사용·진행 중 게시, 한 원인의 복수 ref, 원래/base/사용자/다른 작업 ref와 planning 실패 제외를 검사한다. 조회 실패는 부재나 사용 종료가 아니어야 한다.
- 원문 bundle에서 부모 없는 진단 자료의 bytes·모드·diff·manifest를 복구하고 누락·손상·symlink·identity 불일치는 보존해야 한다. 충분한 과거 기록과 불충분한 기록, 관측 경쟁·transaction 중단·재시작을 구분한다.
- 부분/전체 verify의 회귀 조건은 기존 진단 생성·재사용·private index·비밀값 제외·원문/감사 보존과 독립 진행이다. 새 후보 준비가 원격 push·ref 삭제·원인 해제·완료/알림을 실행하지 않는지 확인한다.

## Progress

기존 진단 보존·게시 transaction에 `diagnostic_ref_uses`를 연결했다. scope·작업·ref·원인·입력·등록 origin별 사용과 원래 branch·tree/commit OID를 유지하고 게시 대기·확정 및 generation을 기록한다. 같은 ref의 새 원인은 기존 intent·확정 기록과 별도로 보존한다. origin이 바뀌면 별도 사용으로 남기며 과거 성공 receipt의 반복 조회나 다른 origin의 cached 성공을 미확정 사용의 게시 증거로 승격하지 않는다. 기존 원문·게시 intent·최신 결과·알림 경계는 유지한다.

`load_verified_artifacts`는 기존 로컬 bundle의 일반 파일만 읽고 원문 bytes·모드·index stage·diff·manifest와 저장된 원인 설명·scope·작업·branch·base·ref를 대조한다. SHA-1/SHA-256 orphan tree·commit을 bytes로 재구성해 저장 OID와 비교하므로 원격 ref·worktree·일시적 Git 객체 없이도 자료 동일성을 검사한다. 불완전 manifest, 자료 누락·손상·추가, 잘못된 모드·blob identity, 파일/디렉터리 symlink와 읽기 중 변경은 보존 사유로 거부한다. 기존 bundle 재사용에도 같은 일반 파일 읽기를 적용한다. 이 함수의 성공은 로컬 자료 증명이며 정리 자격이나 원인 해소를 뜻하지 않는다.

관리 rebase 수용 후 이번 호출에서 현재 HEAD `000ad57c1049438bf7c985bfcd4038e7c15d9b41` 대비 전체 diff와 최신 sources를 다시 확인하고 프로젝트 Python `python3`과 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 targeted unit을 새로 실행했다.

- `--unittest tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest`: 14개 통과. 복수 원인·ref, origin 변경, schema 보존, 손상 intent 거부와 결과/사용/알림 rollback을 검사했다.
- `--unittest tests.test_diagnostic_preservation.PreservationTests`: 14개 통과. 동일 ref 재사용, 과거 receipt 반복 조회, origin 왕복 변경 뒤 cached 성공 거부, 중단·재시도의 사용 generation과 기존 보존 회귀를 검사했다.
- `--unittest tests.test_diagnostic_preservation.ArtifactRecoveryTests`: 9개 통과. 실제 SQLite·파일로 복구·손상·symlink·관측 경쟁을 검사했다. 직렬화 계약의 독립 확인에 필요한 한 native unit에서만 격리된 임시 저장소의 실제 Git으로 SHA-1/SHA-256 orphan OID·부모 부재·HEAD/index 불변을 확인하고 객체 DB 제거 뒤 bundle 복구를 검사했다. 원격·게시·정책 조합은 실제 Git으로 반복하지 않았다.
- 전체 verify는 Worker에서 실행하지 않았다. 이번 checkpoint는 구현 완료·전체 검증 완료 후보가 아니다.

남은 구현은 원인 해소의 영속 확정과 정상 작업 branch 원격 게시의 연결, 모든 사용자·진행 중 보존/게시 및 관측 경쟁을 확인하는 정리 준비/보류 판정이다. `diagnostic_ref_uses`와 로컬 bundle 증명만으로 정리 자격을 인정하지 않으며 원격 ref 변경·삭제는 추가하지 않았다. PRODUCT·ARCHITECTURE의 준비 책임 반영은 이 판정 구현과 함께 진행한다.

- [ ] 정리 준비 근거와 후보 판정
- [ ] 관련 targeted unit 검증과 문서 정합성

## Decision Log

- 정리 준비는 원격 삭제 경계 승인과 독립적으로 Merge할 수 있다. 기존 보존 기록을 소비하되 원인 연결이 부족한 과거 ref를 추정해서 정리하지 않는다. R-083의 실제 삭제는 후속 작업과 별도 사용자 판단에 남긴다.
- complexity high: 같은 ref의 복수 원인·게시 수명, 정상 branch의 해결 결과와 원인 해소 확정, 부모 없는 원문의 복구 증명을 중단·경쟁에도 연결할 판단이 남아 있다.
- risk medium: 잘못된 기록은 정리를 누락하거나 부정확한 후보를 후속 삭제자에게 전달할 수 있다. 이 단계는 원격을 변경하지 않으며 기존 원문/감사를 보존하고 후속 삭제 직전 재검증을 요구해 기록 재관측으로 복구한다.
- priority normal: R-083의 필수 준비 작업이다. 기존 충돌 복구·보존·진행을 막는 선행 gate나 새로운 관리 CLI를 추가하지 않는다.
