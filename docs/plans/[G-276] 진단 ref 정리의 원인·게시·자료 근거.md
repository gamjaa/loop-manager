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

- [x] `blocked_checkpoint_state`의 기존 보존·게시 intent/결과를 유지하며 `diagnostic_ref_uses`, `diagnostic_cause_resolutions`, `diagnostic_cleanup_preparations`를 호환 가능하게 추가했다. ref별 복수 원인·origin·확정 OID·진행 중 사용과 준비 basis를 보존한다.
- [x] `diagnostic_preservation.load_verified_artifacts`가 로컬 bundle의 원문·모드·diff·manifest와 고정 orphan tree/commit을 독립적으로 검증한다. 자료 부족·손상·symlink·identity 불일치는 보존 사유이며 기존 생성·비밀값 제외·private index 정책을 유지한다.
- [x] 원인 해제 transaction에서 현재 COMPLETE 또는 소유된 WAITING admission을 정확한 원인에 연결했다. WAITING은 원래 target의 실제 COMPLETE가 추가로 필요하다. 정상 branch 게시 경계의 기존 `publication_remote_oid`와 해결 HEAD의 원격 포함 관계를 소비하며 게시·해소의 순서나 ACTIVE row 부재에 의존하지 않는다.
- [x] `diagnostic_cleanup.assess_ref`의 순수 판정과 관측/저장을 분리했다. 실행 종료 경계에서 저장 ref별로 두 번 관측하고 DB snapshot이 유지될 때만 준비·보류를 저장한다. 후속 삭제자는 basis·현재 사용·원격·자료를 다시 확인해야 한다.
- [x] 기존 원문·복구 fixture를 재사용해 `tests/test_diagnostic_cleanup.py`와 관련 targeted unit을 검증했다. PRODUCT·ARCHITECTURE에 준비 책임·입력 계약·보존 조건과 실제 삭제의 후속 범위를 기록했다.

## Validation

- 정상 게시 및 원인 해소의 순서별 조합, 로컬 해소만 완료, 미게시·미확정 결과, PR Merge 대기와 다른 차단 잔존에서 후보 판정과 다른 상태 불변을 확인한다.
- 같은 ref의 복수 원인·재사용·진행 중 게시, 한 원인의 복수 ref, 원래/base/사용자/다른 작업 ref와 planning 실패 제외를 검사한다. 조회 실패는 부재나 사용 종료가 아니어야 한다.
- 원문 bundle에서 부모 없는 진단 자료의 bytes·모드·diff·manifest를 복구하고 누락·손상·symlink·identity 불일치는 보존해야 한다. 충분한 과거 기록과 불충분한 기록, 관측 경쟁·transaction 중단·재시작을 구분한다.
- 부분/전체 verify의 회귀 조건은 기존 진단 생성·재사용·private index·비밀값 제외·원문/감사 보존과 독립 진행이다. 새 후보 준비가 원격 push·ref 삭제·원인 해제·완료/알림을 실행하지 않는지 확인한다.

## Progress

최신 관리 rebase의 HEAD `7fcbcbb818b0f1bdd3af42a40b028bad49cb02b6`와 base `4334a1e398ccb906163147885e034d55f9b066f4` 사이의 21개 파일 전체 diff, 현재 ExecPlan·R-083·PRODUCT·ARCHITECTURE를 다시 확인했다. 문서 설명의 의미는 유지하고 현재 patch hash로 최종 보고를 교정한다. 현재 후보에서 targeted runner로 cleanup module 25개, artifact recovery class 9개, compatibility export case·fixture import case·START scheduling case·launcher class 각 1개를 새로 실행해 총 38개가 통과했다. 제품 코드·Requirement·frontmatter는 변경하지 않았다. 전체 verify와 native 예제 통합은 Manager 경계에 남기며 아래 검증 기록은 이전 후보의 이력이다.

Manager의 HEAD `1dd16b57cb39fe4da3c66eb07eee323d38e7dc25` 전체 verify 피드백 네 건을 교정했다. 새 compatibility export의 effect 타입 주석을 기존 문자열 계약에 맞추고, 두 진단 테스트가 공유하는 bundle fixture를 `tests/support/diagnostic.py`로 이동했다. START 재평가의 상태 전용 mock에는 실제 observation 계약의 빈 `files`를 명시했다. 기존 assertion·테스트·지원 경계는 유지했다.

Perl native 예제의 기존 로그는 `stage-1` 결과 파일 부재에 따른 `StopIteration`이었다. 단독 launcher가 생성하는 별도 `--execute` 프로세스가 fixture의 고정 자원 관측을 우회하는 경로를 확인하고, 해당 프로세스만 fixture bootstrap으로 연결했다. 실제 project worker는 원래 entrypoint와 자격 증명 제거를 유지한다. 기존 launcher unit에서 모드별 argv·옵션 전달·자원 관측·patch 복원과 자격 증명 격리를 검증했다. native 결과가 없으면 원래 진단을 포함하는 assertion으로 실패하며 결과 부재를 성공으로 바꾸지 않는다. Worker의 통합 실행 제한에 따라 Perl native 예제와 전체 verify는 직접 재실행하지 않았으며 Manager 재검증이 필요하다.

이번 교정 후 targeted runner에서 compatibility 계약 1개, support fixture class 29개, START scheduling case 1개, diagnostic cleanup 25개, artifact recovery 9개, launcher unit 1개가 통과했다(서로 다른 66개). compatibility와 START case는 이 호출에서 관측한 오류를 수정한 뒤 같은 실패 명령으로 재검증했다. 처음 여러 `--unittest`를 지정한 명령은 마지막 case만 실행하므로 각 범위는 별도 명령으로 검증했다. PRODUCT·ARCHITECTURE의 제품/설계 설명은 그대로 유효하며 최종 base 대비 patch hash도 유지된다. 아래 기록은 이전 후보와 구현 세션의 검증이며 이번 native 통합 성공을 의미하지 않는다.

구현 완료 후보이며 전체 verify·Reviewer 검증은 Manager에 인계한다. 저장된 게시 identity·모든 원인 사용·현재 소유권·원인 해소·정상 branch 게시·독립 로컬 자료를 ref별로 결합했다. 원격 삭제·push·원인 해제·새 알림은 준비 판정에 추가하지 않았다. 일반 차단 삭제, 캐시된 성공, 미게시 작업물, 불충분한 과거 기록을 자격으로 승격하지 않는다.

원인 해제는 기존 상태 transaction에 기록한다. 실제 COMPLETE와 WAITING 후 재개를 구분하고 후자는 같은 target의 현재 COMPLETE를 요구한다. 해소 때 미커밋 작업이 남았으면 clean·저장된 게시 HEAD·해소 HEAD 이후 commit을 추가 확인한다. 원래 generation의 acceptance와 마지막 해제 감사가 함께 남은 과거 기록도 소비하며 추정 연결은 하지 않는다. 여러 원인·ref와 실행 중/종료 불명 예약을 각각 확인하고, 관측 또는 DB generation 변경은 준비를 확정하지 않는다. 기존 원문·session·실패·intent·게시 확정과 무관한 차단은 유지한다.

문서 설명 교정에서는 관리 rebase를 수용한 HEAD `83d87ea10c4c25461000a737607484d79d3e1677`과 base `2ac56997f433ad8246d8f3faa6306ec78d1d56d4` 사이의 18개 파일 전체 diff, 최신 ExecPlan과 sources를 다시 확인했다. PRODUCT·ARCHITECTURE의 기존 설명은 유효하며 rebase로 바뀐 최종 patch hash로 보고를 갱신한다. 현재 후보에서 `tests.test_diagnostic_cleanup` 25개, `tests.test_diagnostic_preservation.ArtifactRecoveryTests` 9개, `tests.test_scope_block_revalidation.EvidenceFreeRebaseBlockTests` 29개를 프로젝트 Python과 targeted runner로 새로 실행해 모두 통과했다. 전체 verify는 실행하지 않았고 제품 코드·Requirement·선언은 변경하지 않았다. 아래 표는 앞선 구현 세션의 검증 기록으로 구분한다.

초기 복구점 `d4e63c53dde36b44ac65e70d1997262b4f4af42a`에서 이어 구현했으며 base `000ad57c1049438bf7c985bfcd4038e7c15d9b41` 대비 전체 변경과 새 파일을 확인했다. 프로젝트 Python `python3`과 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 구현 세션에서 다음 최소 unit 범위를 실제 실행했다. 결과 형식 교정 호출에서는 원인에 관련된 cleanup module과 plan publication race class만 다시 실행해 각각 25개·18개 통과를 확인했다.

| `--unittest` 대상 | 결과 |
|---|---|
| `tests.test_diagnostic_cleanup` | 25개 통과: 복수 원인/ref·준비/보류·순서·과거 기록·진행 중 사용·관측 경쟁·쓰기 후 rollback/재시작·실제 composition 연결·실행 결과 불변·runtime handoff |
| `tests.test_diagnostic_preservation.ArtifactRecoveryTests` | 9개 통과: 독립 원문/모드/diff 복구와 손상·symlink·identity, SHA-1/SHA-256 native orphan 계약 |
| `tests.test_diagnostic_preservation.PreservationTests` | 14개 통과: 생성/재사용·비밀 경로 정책·cached origin·중단/재시도 |
| `tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest` | 14개 통과: 원인별 게시·schema·intent·알림 transaction |
| `tests.test_plan_block_revalidation.PlanCheckpointPublicationRaceTests` | 18개 통과: 현재 원인 해제·순서·게시/소유권 경쟁 |
| `tests.test_project_block_revalidation.ProjectCauseRevalidationTests` | 11개 통과: 연결된 project 원인과 독립 상태 보존 |
| `tests.test_scope_block_revalidation.EvidenceFreeRebaseBlockTests` | 29개 통과: COMPLETE/WAITING 복구·차단 해제·generation/transaction 경계 |

정책 조합은 mock Git·실제 SQLite/파일을 사용했다. 기존 artifact native unit 한 곳만 Git 직렬화 형식·부모 부재·HEAD/index 불변과 객체 DB 제거 후 복구를 확인한다. 테스트 삭제·skip·assertion 약화는 하지 않았으며 기존 상태 전용 복구 mock에는 새 선택적 acceptance 부재를 `None`으로 명시했다.

초기 새 테스트는 fixture의 plan path/원격 identity 누락으로 본문 실행 전 오류가 났다. fixture 교정 뒤 원격 parser가 placeholder OID를 거부한 본문 실패는 유효 OID fixture로 수정했다. 기존 plan 회귀의 두 단계 실패는 COMPLETE 상태만 제공하던 공통/개별 mock에 `acceptance=None`을 추가해 수정했다. 두 명령 모두 동일 명령 재실행에서 위 표의 통과를 확인했고 미해결 실패는 없다.

Manager의 미해결 targeted 판정은 최종 보고에 `FAILED_COMMAND`·`RERUN`만 있고 그 사이 실제 이전 결과가 누락돼 발생했다. 이전 실행 기록에서 본문에 실패했던 15개 ID가 모두 후속 성공했음을 대조했다. 검사 기준이나 제품 코드는 바꾸지 않고 명령 사이에 이전 결과를 명시하고 재실행 결과를 별도 줄에 두었다. 제출할 최종 응답 그대로 현재 `parse_worker_result`를 호출해 publication 계약이 수용됨도 확인했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_diagnostic_cleanup
이전 본문 결과: FAILED (failures=6).
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_diagnostic_cleanup
현재 교정 호출 결과: OK (25 tests).

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_plan_block_revalidation.PlanCheckpointPublicationRaceTests
이전 마지막 결과: FAILED (errors=1).
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_plan_block_revalidation.PlanCheckpointPublicationRaceTests
현재 교정 호출 결과: OK (18 tests).

전체 verify·전체 suite·lint/build/type check는 Worker에서 실행하지 않았다. `scripts/verify-inputs.json`의 기존 `loop_manager_internal/**`·`tests/**` glob이 새 코드/테스트를 포함하며 목록 밖 경로의 exclusion identity는 선언하지 않았다. 실제 원격 삭제는 이 작업의 구현 대상이 아니며 후속 사용자 판단 경계를 유지한다.

- [x] 정리 준비 근거와 후보 판정
- [x] 관련 targeted unit 검증과 문서 정합성

## Decision Log

- 정리 준비는 원격 삭제 경계 승인과 독립적으로 Merge할 수 있다. 기존 보존 기록을 소비하되 원인 연결이 부족한 과거 ref를 추정해서 정리하지 않는다. R-083의 실제 삭제는 후속 작업과 별도 사용자 판단에 남긴다.
- complexity high: 같은 ref의 복수 원인·게시 수명, 정상 branch의 해결 결과와 원인 해소 확정, 부모 없는 원문의 복구 증명을 중단·경쟁에도 연결해야 해 원인 감사와 현재 관측을 분리했다.
- risk medium: 잘못된 기록은 정리를 누락하거나 부정확한 후보를 후속 삭제자에게 전달할 수 있다. 이 단계는 원격을 변경하지 않으며 기존 원문/감사를 보존하고 후속 삭제 직전 재검증을 요구해 기록 재관측으로 복구한다.
- priority normal: R-083의 필수 준비 작업이다. 기존 충돌 복구·보존·진행을 막는 선행 gate나 새로운 관리 CLI를 추가하지 않는다.
