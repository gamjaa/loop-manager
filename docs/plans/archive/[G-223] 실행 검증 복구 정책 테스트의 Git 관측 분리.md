---
id: G-223
title: 실행 검증 복구 정책 테스트의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on:
  - G-224
complexity: medium
risk: medium
priority: normal
---

## Purpose

Worker 결과·verify/Review·실패 이력·수동 후보와 PR 복구의 남은 정책 테스트를 관측 mock으로 전환한다. 현재 `test_worker_publication_validation.py`의 invalid Worker 결과/session 재개와 `test_worker_validation.py`의 반복 verify 실패는 실제 Git을 준비하지만 핵심 판정은 결과·후보 관측·SQLite 이력이다. 직접 반영된 execution_policy_fixture와 재시도/Review 전환은 재구현하지 않는다.

## Acceptance Criteria

- 실행/검증/Review/게시·차단 복구 및 수동 작업 테스트의 관측값으로 판정 가능한 case는 Git 프로세스 없이 실제 handler·coordinator·결과 parser·실패/승격/후보 저장을 검사한다. 실제 Git·파일·원격 lease 효과는 필요한 통합 회귀로 유지한다.
- 성공·실패·timeout/관측 오류·HEAD/remote OID·dirty/index/operation 변화와 중단 뒤 상태를 명시적으로 재현한다. 판단을 성공 mock으로 대체하거나 과거 성공·session·실패 이력을 새 성공으로 합성하지 않는다. 예상 밖 명령은 실패해야 한다.
- 전환 전후의 조건·호출 횟수·반환 kind·session·후보 token·SQLite 이력·게시 거부 assertion을 대응시킨다. 실제 conflict/index/autostash/worktree·lease·checkpoint·중단 재개 및 새 verify receipt의 후보 gate를 보존한다.
- 통합 테스트는 필요한 실제 효과와 같은 복구로의 재진입을 확인하면 종료한다. 재개 뒤 반복 교정 소진·최종 차단·게시가 독립 계약이면 기존 전용 case로 대응하고, 실제 반복 횟수나 연결 자체가 assertion이면 필요한 반복을 유지한다.
- 전체 verify에는 전환 case와 필수 통합 회귀가 남는다. skip·timeout·수집 범위·운영 실행/권한·필수 검증 정책을 비용 절감으로 변경하지 않는다.
- 유지하는 실제 Git 회귀는 G-224의 공통 테스트 환경 격리를 사용한다. 사용자 global/system 설정·template의 우연한 영향은 제외하고 설정 상속이 검증 대상이면 필요한 설정을 격리된 입력으로 명시한다. 사용자 설정과 운영 checkout의 환경은 변경하지 않는다.

## Implementation

- 대상은 `tests/test_execution*.py`, `test_worker*.py`, `test_review*.py`, `test_recovery*.py`, `test_publication*.py`, `test_pr_*.py`, `test_rebase*.py`, `test_managed_rebase*.py`, `test_checkpoint*.py`, `test_blocked_remote_observation.py`, `test_manual*.py`의 남은 실제 Git 사용 case다. assertion에 필요한 effect를 확인해 정책 변형만 분리한다.
- 기존 ExecutionFixture·execution_policy_fixture/run_execution_policy·CurrentRebaseFixture·rebase 관측 생성·runtime DB/feedback fixture를 재사용한다. RecoveryBoundary는 실제 Git/index 복원 자체의 회귀에 유지한다. 파일/SQLite 보존은 실제 저장으로 확인하며 운영 handler와 판정 함수를 mock하지 않는다.
- 실제 Git을 유지하는 실행·rebase·복구·수동 작업 fixture의 직접 subprocess·운영 Git adapter 호출과 필요한 자식에 G-224의 격리 환경을 적용한다. 공통 helper를 중복 구현하지 않고 명시적인 설정·RecoveryBoundary의 config 복원과 실제 index/lease/checkpoint effect assertion을 보존한다.
- 직접 전환된 retry/review escalation·PR 원인 판정과 중단 회귀 축소를 유지한다. G-197의 결과 전달과 G-183의 Review 재사용 목표는 구현하지 않으며 그 작업에서 테스트가 수정돼도 실제 후보·결과 계약을 기준으로 전환한다.
- 공동 fixture는 기존 사용자 호환성을 유지하는 최소 변경으로 제한한다. AGENTS.md와 ARCHITECTURE의 Git 경계를 따르며 assertion 대응·실제 Git 유지 이유를 대상 테스트/계획 기록에 남긴다.
- assertion 대응: `WorkerPublicationValidationTest.test_invalid_worker_result_resumes_same_session_without_verify`는 실제 parser와 coordinator, SQLite를 사용하고 Git 관측 및 프로세스는 거부 경계로 분리한다. 최초 invalid result의 `OPERATIONAL_ERROR`·phase·오류·session, 다음 Worker의 같은 session·오류 prompt, 첫 호출의 verify no-call과 재개 뒤 verify 1회를 유지한다. `WorkerValidationTest.test_repeated_verify_failure_blocks_in_one_run_without_reviewer`는 같은 `execution_policy_fixture`를 사용해 verify 실패 4회, Worker session 재사용, prompt revision, Reviewer no-call, `PLAN_BLOCKED`와 SQLite failure history occurrence 4를 유지하며 최종 차단의 revision/snapshot 관측만 명시 mock한다.
- 수동 후보 assertion은 `ManualWorkTest.test_reviewer_prompt_distinguishes_a_valid_user_verify_waiver`의 명시 Git 관측 mock과 process 거부 경계에서 waiver/secret redaction·verify/review 결과를 검사하고, `ManualCandidate.publication`이 Worker 결과나 외부 targeted test PASS를 합성하지 않는지 확인한다. `test_manual_candidate_reaches_pr_without_fabricating_worker_or_test_evidence`와 기존 게시·rebase·checkpoint·RecoveryBoundary 회귀는 실제 후보 게시·Git/index/중단 효과를 확인하므로 유지한다. 단계별 Review protocol과 retry 이력은 기존 직접 관측 정책 case에 대응하며 G-197/G-183 목표를 이행하지 않는다.
- G-224 환경 대응: 대상 `test_execution*`, `test_worker*`, `test_review*`, `test_recovery*`, `test_publication*`, `test_pr_*`, `test_rebase*`, `test_managed_rebase*`, `test_checkpoint*`, `test_blocked_remote_observation.py`, `test_manual*` 중 실제 Git fixture·운영 adapter·자식 프로세스를 쓰는 테스트 클래스는 기존 `isolated_git_test`를 사용한다. `tests/test_recovery_history.py::RecoveryHistoryTest`도 실제 worktree와 운영 Git adapter를 쓰므로 클래스 전체에 이를 적용한다. 설정 상속 테스트는 G-224의 명시 입력 회귀로 확인하고 실제 index·lease·checkpoint·게시 효과를 유지한다.

## Validation

- Worker는 전환한 Worker 결과 오류/session 재개·반복 verify 실패·단계 이력/Review 프로토콜·수동 후보 정책의 변경된 최소 unit case를 실행한다. 실패 후 같은 session, 금지 단계의 no-call, 실제 SQLite 차단/교정 이력과 관측 변화의 후보 거부를 검사하고 예상 밖 process를 거부한다.
- 전환한 정책 case는 예상 밖 process를 거부하는 경계와 기존 Git 계측으로 Git 실행 0회를 확인한다. 반복 verify 실패는 Worker/verify 4회·같은 session·실패 이력 4회·Reviewer no-call·최종 PLAN_BLOCKED를 유지한다. 변경한 실제 Git 소비 경계의 전달 환경·명시적 설정 입력·config 복원은 최소 unit case로 확인하고 공통 격리 조건 검사는 G-224의 회귀를 재사용한다.
- Manager 전체 verify는 실제 rebase/index/autostash·lease·checkpoint·수동 게시·실패 시 작업물 보존/재시작, `test_verify_receipts.py`·`test_managed_rebase_lifecycle.py`의 새 후보/원문 근거 계약을 함께 통과해야 한다. unit 전환을 통합 계약 삭제로 취급하지 않는다.
- 변경 전후 영향 모듈과 필수 연관 회귀를 같은 환경·대상 집합·Python/Git 버전·계측/캐시 조건·병렬도에서 반복 측정해 중앙값을 비교한다. revision·실제 worker 수·CPU/메모리 제한·동시 부하·임시 경로와 Git 호출 수·누적 시간·fixture/준비·모듈 경과 시간을 남긴다. 기존 전체 기준 기록과 Manager 최종 전체 verify 근거를 연결하며 국소 비교마다 전체 suite를 반복하지 않는다. 실행 횟수 감소는 assertion 대응으로 설명하고 누적 비용을 전체 경과 시간과 더하거나 모듈 개선을 전체 verify 절감으로 환산하지 않는다.

## 비용 비교

2026-10-12 현재 base `3d691b433f39256d54edd12a17894d1db820e3d2`와 candidate `5d8e441bbedacda7d24ad0ac512cef8f4fd3775d`에서 전환된 두 정책 case를 각각 새 Python 프로세스로 3회씩 직렬 실행했다. target 집합은 `tests.test_worker_publication_validation.WorkerPublicationValidationTest.test_invalid_worker_result_resumes_same_session_without_verify`와 `tests.test_worker_validation.WorkerValidationTest.test_repeated_verify_failure_blocks_in_one_run_without_reviewer`이며 각 실행은 정확히 한 case다. 양 revision/case를 1회씩 먼저 실행해 OS 파일 캐시를 데운 뒤, 각 case의 base/candidate 표본을 세 쌍으로 교차 실행하고 쌍마다 먼저 실행하는 revision을 번갈았다. Python 3.12.3, Git 2.43.0, Linux 6.17.0-1020-oracle aarch64, 공통 `TMPDIR=/tmp/loop-manager-g223-cost/tmp`, `python3 -B`, `tests.support.git.git_test_environment()`으로 사용자 Git 설정을 제거한 조건을 사용했다. `-B`로 Python bytecode cache를 비활성화했고 각 표본은 새 프로세스다. OS page cache는 비우지 않고 양쪽 사전 실행 뒤 host가 관리하는 상태를 유지했다. 테스트 내부 tool 병렬도는 쓰지 않고 실제 worker는 1개였다. CPU affinity는 0–1의 2 CPU, cgroup CPU/memory limit 파일은 조회 불가, `RLIMIT_AS`는 무제한이었다. 표본 시작/종료 시 load average와 `/proc/meminfo`의 `MemAvailable`을 기록했다. 1분 load average 범위는 0.388–1.154, 사용 가능 메모리는 약 10.77–10.81 GB였고, Worker가 다른 verify/test를 병행 실행하지 않았다. host 부하는 통제되지 않았으므로 이 수치는 두 국소 case의 비교이며 전체 verify 절감 추정이나 다른 환경의 성능 보장이 아니다.

elapsed는 테스트 ID 로드 직전부터 runner 완료까지의 경과 시간이고, test는 timed result가 기록한 case 시간이다. Git 시간은 Git subprocess 누적 시간, fixture 시간은 `git_fixture:seed_initialization`과 `git_fixture:independent_copy` 준비 구간이다. 각각 세 표본의 중앙값이며 elapsed와 누적 시간을 더하지 않는다.

| 대상 case | 기준 → 후보 elapsed (초) | 기준 → 후보 test (초) | Git 호출 | Git 누적 시간 (초) | seed 준비 (초) | 독립 복사 준비 (초) |
|---|---:|---:|---:|---:|---:|---:|
| invalid Worker 결과/session 재개 | 1.892 → 0.186 | 1.880 → 0.182 | 585 → 0 | 1.193 → 0 | 0.054 → 0 | 0.019 → 0 |
| 반복 verify 실패 후 차단 | 3.791 → 0.288 | 3.777 → 0.284 | 1,313 → 0 | 2.624 → 0 | 0.053 → 0 | 0.018 → 0 |

후보의 실제 Git fixture 구간은 없으며 `execution_policy_fixture`의 메모리 내 준비 비용은 test 시간에 포함된다. 기준의 Git seed 초기화·저장소 복사 비용과 후보의 case 시간을 서로 더하거나 전체 verify 시간으로 환산하지 않았다. 각 표본의 두 case 모두 통과했다.

## Progress

- [x] 2026-10-08 f3ae228의 R-080과 직접 테스트 전환 diff, 실행 관측 helper, 남은 Worker 결과/verify 실패 case 및 실제 conflict·재시작 회귀를 정적으로 확인했다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-10 사용자 요청으로 갱신된 R-080의 Git 환경 격리·반복 verify assertion·Git 실행 0회·비용 측정 조건을 대조하고 G-224 소비 범위와 선행 Merge를 문서에 반영했다. 이 기록 단계에서는 구현·테스트를 하지 않았다.
- [x] 2026-10-12 `test_worker_publication_validation`의 invalid Worker result/session 재개와 `test_worker_validation`의 반복 verify 차단을 `execution_policy_fixture`로 전환했다. 수동 후보의 미확인 targeted test evidence는 기존 mock case에 추가 확인을 두었다. `python3 -B -m unittest tests.test_worker_publication_validation.WorkerPublicationValidationTest.test_invalid_worker_result_resumes_same_session_without_verify tests.test_worker_validation.WorkerValidationTest.test_repeated_verify_failure_blocks_in_one_run_without_reviewer tests.test_manual_work.ManualWorkTest.test_reviewer_prompt_distinguishes_a_valid_user_verify_waiver -v`에서 3개 targeted case가 통과했다.
- [x] 2026-10-12 실제 Git 효과를 유지한 대상 테스트 클래스에 G-224의 `isolated_git_test`를 연결했다. Worker invalid/session 재개·반복 verify·수동 waiver·수동 게시 효과 4개 case와 G-224의 환경 복원·hostile 설정 차단 및 명시 입력 2개 case가 각각 targeted 실행에서 통과했다.
- [x] 2026-10-12 관리 rebase 뒤 현재 base `3d691b433f39256d54edd12a17894d1db820e3d2`의 최종 후보에서 Worker 결과/session 재개·반복 verify 차단·수동 waiver/게시 4개 case와 G-224 Git 환경 복원·명시 입력 2개 case를 다시 실행해 모두 통과했다. 전체 verify는 실행하지 않았다.
- [x] 2026-10-12 base `3d691b433f39256d54edd12a17894d1db820e3d2`와 candidate `5d8e441bbedacda7d24ad0ac512cef8f4fd3775d`의 invalid Worker 결과/session 재개와 반복 verify 차단 case를 파일 cache warm-up 후 교차 순서로 각각 3회 측정해 중앙값, Git 호출/누적 시간, fixture 준비 비용, sample별 load average와 MemAvailable을 기록했다. 현재 후보에서 inherited signing 설정을 제공한 `python3 -B -m unittest tests.test_recovery_history.RecoveryHistoryTest.test_review_failure_then_verify_failure_keeps_stage_counts_independent tests.test_worker_publication_validation.WorkerPublicationValidationTest.test_invalid_worker_result_resumes_same_session_without_verify tests.test_worker_validation.WorkerValidationTest.test_repeated_verify_failure_blocks_in_one_run_without_reviewer -v`가 3개 모두 통과했다. OS page cache는 비우지 않았고 host 부하는 통제하지 않았다. 전체 verify는 실행하지 않았다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 실행/관측/저장 경계와 이미 추가된 fixture를 여러 case에 적용한다. risk medium: 테스트 약화는 후보·실패/복구 회귀를 가릴 수 있어 실제 판단과 통합 effect assertion을 보존한다. 테스트 변경 rollback과 기존 회귀로 복구할 수 있으며 priority normal이다.
- 2026-10-08 초기 범위에서는 G-175/G-182의 실제 MERGED와 직접 구현된 verify receipt·clean rebase·기록 비교를 유지하고 이미 전환한 테스트를 소급 계획하거나 자동 성공으로 기록하지 않으며 G-222/G-224와 독립적으로 검증·Merge할 수 있다고 정했다.
- 2026-10-10: 추가된 공통 Git 환경 격리 구현은 G-224가 맡고 이 작업은 실제 Git 소비 경계에 적용한다. 공통 계약을 사용하기 위해 `depends_on`에 G-224를 추가하며 G-222는 선행으로 요구하지 않는다. ID·경로·기존 수행 기록과 complexity/risk medium·priority normal을 보존한다. 비용 비교의 환경 조건은 R-080이 참조하는 R-065 기준을 따른다.
