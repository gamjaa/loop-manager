---
id: G-267
title: 지침과 등록 테스트의 공통 Git 경계 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: low
priority: normal
---

## Purpose

지침 허용 정책과 등록 fixture에 남은 별도 Git 실행 책임을 기존 공통 관측·격리 경계에 연결한다. 저장된 발견 18의 `tests/test_guidance_approval.py:275`는 문구·revision 선택 정책만 확인하면서 실제 저장소·commit을 준비한다. 발견 24의 `tests/test_managed_configuration.py:98`과 발견 38의 `tests/support/guidance.py:51`은 최초 init부터 비격리 subprocess를 사용한다. 공용 helper가 이미 있어도 이 중복 실행 경계에는 사용자 template·hook·서명·filter 설정이 유입될 수 있다.

## Acceptance Criteria

- CommonPlansScopeTest의 정책 조합은 git show 종료 코드·본문 및 저장소 유형을 명시적 관측으로 제공하고 실제 `_published_plan_allows_common_plans` 판단을 검사한다. plan_sha/current_revision_sha 선택, 우연한 문구·직접 Acceptance Criteria·저장소 유형의 기존 assertion을 보존하며 해당 정책 case의 Git 실행은 0회다.
- 등록과 지침 fixture에서 필요한 실제 Git 효과는 최초 init/seed부터 후속 직접 명령·운영 adapter와 필요한 자식까지 기존 `tests/support/git.py`의 공통 환경을 사용한다. host global/system 설정·template·hook·서명·filter·ignore/attributes의 우연한 영향은 제외하고 명시적 상속 테스트는 격리된 입력으로 유지한다.
- 별도 문자열 helper와 CompletedProcess helper의 반환·check=False·오류·stderr 계약을 보존한다. 공통 helper의 반환 차이를 숨기지 않고 필요한 얇은 실행 adapter에서 기존 환경을 재사용한다. 새로운 범용 Git 서비스·cache·runner·전역 가변 상태를 추가하지 않는다.
- 사용자 동작과 공개 계약은 운영 CLI·프로젝트 등록/rollback·지침 해석·역할 권한·copy 도구를 그대로 보존한다. 사용자 설정 파일과 운영 checkout의 환경/설정을 수정하지 않으며 테스트 환경 patch는 실패/cleanup 이후 복원된다.
- 저장 형식은 등록·catalog·승인 감사의 SQLite schema, 고정 문서 샘플·배포 파일 bytes, plan identity와 기존 fixture 반환 형태를 보존한다. 실제 copy/배포 및 등록 보존 assertion을 mock으로 바꾸지 않는다.
- 이전/이후 테스트 조건과 assertion을 대응시킨다. 새 실제 Git 조건 조합은 필요한 연결 효과가 있을 때만 추가하며 전체 verify 수집·필수 검증을 축소하지 않는다.

## Implementation

정책 판단과 Git 효과의 책임을 CommonPlansScopeTest에서 분리하고, `tests/support/guidance.py`와 등록 테스트의 실행 adapter는 G-224로 이미 Merge된 `git_command`, `git_test_environment`, `isolated_git_test`를 계약에 맞게 재사용한다. 확인된 지침/등록 소비 경계로만 범위를 제한한다. 공유 helper의 운영 의미와 다른 테스트의 명시적인 설정 입력을 변경하지 않는다.

G-222는 planning/catalog/maintenance·코드맵, G-223은 execution/검증/복구 소비자의 전환을 담당한다. 이 작업은 그 계획에 없는 지침 정책과 등록 wrapper를 맡으며 해당 기존 범위를 재구현하지 않는다. 두 READY 작업 또는 provider 설정 기능의 Merge는 이 국소 테스트 변경의 선행 조건이 아니다.

## 구현 검증 대응

| 변경 경계 | 기존 조건·assertion | 수정 후 계속 확인하는 조건·assertion |
|---|---|---|
| `CommonPlansScopeTest.test_common_plans_scope_requires_direct_acceptance_criterion` | 임시 저장소에 세 계획을 commit해 우연한 문구, 직접 Acceptance Criteria, conjunctive 문구, `current_revision_sha` 우선과 Loop Manager 저장소 유형을 실제 `git show`로 확인했다. | commit·checkout을 제거하고 `git show`의 `CompletedProcess` 반환 코드와 본문, 저장소 유형을 명시적으로 주입한다. 기존 네 정책 판단과 revision 인자 선택을 유지하고 show 오류와 비-Loop-Manager 거부도 검사한다. 실제 판정 함수는 실행하며 `subprocess.Popen` guard로 프로세스 실행을 막는다. |
| `tests/support/guidance.py`의 `git_result`·`git`·`repository` | 별도 `CompletedProcess`/문자열 helper로 저장소를 만들었지만 Git 호출이 사용자 환경을 상속할 수 있었다. | `git_result`가 `git_test_environment()`를 전달하면서 `CompletedProcess`, `check=False`, 합쳐진 stderr와 subprocess 예외 계약을 유지한다. `GuidanceGitAdapterTest`가 명시 환경과 반환·오류 계약을 확인한다. |
| `ManagedConfigurationAndProjectRegistryTest`의 직접 Git adapter | 등록 복구 테스트의 bare init·commit·revision 조회에 class 격리 환경을 상속하는 별도 subprocess adapter가 있었다. | `_git`, bare init 및 revision 조회가 `git_command`를 사용한다. `test_git_adapter_delegates_to_shared_command`는 adapter 위임을 Git 없이 확인하고, 등록 identity·rollback 회귀는 기존 실제 Git assertion을 유지한다. 기존 class의 `isolated_git_test`는 운영 adapter와 자식에 적용된다. |
| G-224 공통 격리 경계 | seed·독립 복사·caller 환경 복원 및 통제 입력의 실제 설정 효과를 기존 `tests/test_git_fixture_seed.py`가 확인한다. | 공통 fixture 구현을 중복하지 않는다. `GitEnvironmentTests.test_environment_restores_after_setup_failure_and_next_test`를 실행해 실패 setup 뒤 caller 환경 복원을 확인한다. |

## Validation

- Worker targeted unit test는 CommonPlansScopeTest의 실제 판단·revision 선택과 예상 밖 subprocess 거부를 확인한다. 실패한 git show·누락한 선언도 관측으로 제공하며 정책 함수를 성공 mock으로 대체하지 않는다.
- 변경한 등록/지침 adapter의 최초 명령·운영 adapter/자식으로 전달하는 환경과 예외 후 호출자 환경 복원을 최소 unit 경계에서 확인한다. G-224의 기존 `tests/test_git_fixture_seed.py` 격리 회귀를 재사용하며 전체 hook/서명 조합을 각 소비자에서 반복하지 않는다.
- Manager 전체 verify는 기존 guidance 입력/legacy 읽기·등록 실패 rollback·catalog identity·실제 copy bytes/재실행 거부·배포된 조회 도구 회귀를 유지해야 한다. 기존 반환 형태와 명시적 설정 상속을 Review한다.
- R-080의 비용 비교는 변경한 모듈과 필수 연관 회귀의 같은 환경·범위에서 Git 호출 수/누적 시간과 모듈 경과 시간을 따로 기록한다. Python/Git 버전·worker 수·자원 제한·동시 부하·임시 경로·캐시/계측/병렬도 및 중앙값 비교 조건을 남긴다. Manager 최종 전체 verify 기록에 연결하되 국소 비교마다 전체 suite를 반복하거나 국소 감소를 전체 절감으로 환산하지 않는다.

## Progress

- [x] 2026-10-12 source revision `c401d563589399dbca83ffe4c9cfe13419ff128e`의 module manifest `c10544e0999d36ee6e8143dedecacb414a03b8f2205fbfd2bbbb0b472d1e00b6` 발견 18·24·38과 인용을 재사용했다. 현재 HEAD·공통 helper의 반환/격리 문맥을 확인했으며 테스트·verify·비용 측정은 실행하지 않았다.
- [x] 2026-10-12 rebase 전 후보에서 정책 관측 분리·공통 환경 연결을 구현하고 targeted 6개 test case를 실행했다. 이 결과는 이후 후보에 재사용하지 않는다.
- [x] 2026-10-12 rebase 후 HEAD `1e1f92aa`에서 CommonPlansScopeTest·GuidanceGitAdapterTest·등록 adapter 위임·실패 setup 뒤 환경 복원의 targeted 5개 test case를 Python 3.12.3으로 다시 실행해 통과했다. 대응표의 이전/이후 조건은 유지했다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 정책 관측, 등록·지침의 서로 다른 Git 반환/오류 계약과 환경 수명을 연결하지만 공통 격리 설계는 이미 구현되어 있다. risk low: 테스트 준비/관측에 국한되며 운영 동작·저장 정책을 바꾸지 않고 코드 rollback으로 복구한다. priority normal이다.
- G-224는 이미 Merge된 공통 경계이므로 새 미충족 선행이 없다. G-222/G-223의 기존 범위를 확장하거나 해당 READY 파일을 수정하지 않는다.
- 발견 20의 직접 실행 수집 위치는 별도의 국소 진입점 교정이며 이 작업의 Git 책임 분리 목표에 추가하지 않는다. 전체 verify의 import 수집은 보존한다.
- 이번 평가에서 대상과 dependency를 확정한다. 완료에는 해당 계획의 targeted test·Manager 전체 verify·Review·사용자 Merge가 필요하며 유지보수 사후 재검증은 요구하지 않는다.
