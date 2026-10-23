---
id: G-228
title: runtime 캐시 cold warm 실측 근거 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-059] 안전한 Python bytecode 캐시 사용.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: low
risk: low
priority: normal
---

## Purpose

R-059의 같은 revision 첫 실행·재실행에서 실제 캐시 사용, 실행 시간과 관리 비용을 확인한 근거를 남긴다. Merge된 bootstrap은 checked-hash 생성·재사용 개수와 준비 시간을 계측하고 verify runner는 cache 파일 전후 변화를 기록한다. 현재 테스트는 재사용과 fallback을 확인하지만 동일 workload의 cold/warm 실행 시간을 대조해 보존한 근거는 확인되지 않았다. 캐시 구현과 전체 verify를 다시 설계하지 않는다.

## Acceptance Criteria

- 같은 원격 revision·interpreter·원본·실행 범위의 cold/warm 실행을 실제 Python으로 측정하고 두 실행의 cache 생성/재사용·원본 실행 결과, 전체 경과 시간과 cache 준비·관리 비용을 구분한 근거를 보존한다. workload·revision·interpreter/cache tag·bytecode 정책과 비교 가능한 환경을 명시한다. verify 자료를 사용할 때는 전체/부분 mode·실제 선택 집합과 선행 discovery/부분 실행의 cache 준비 여부를 구분하며 부분→전체 실행을 같은 workload의 cold/warm 쌍으로 간주하지 않는다. fixture 측정을 운영 전체 성능으로 일반화하지 않는다.
- 캐시 없는 실행과 명시적 bytecode 비활성화에서도 같은 원본 결과가 나오는지 확인한다. 기존 `test_runtime_child_reuses_external_bytecode_cache`의 재사용·손상 복구 근거와 연결하며 cache 파일 존재나 mock 시간만으로 실제 사용·개선율을 확정하지 않는다.
- 측정은 별도로 소유한 임시 runtime·cache에서 수행한다. 실제 Agent·외부 서비스·사용자 source/ignored 자료를 실행·변경하지 않고 실제 runtime revision·등록·실행 identity와 후보·파일 보존 조건을 유지한다. R-055를 반영한 R-059에 따라 과거 Git snapshot 차이의 접근 위반 판정을 복원하지 않는다. 측정용 runtime의 검증된 종료·정리 뒤에도 warm cache를 재사용할 수 있고 무관한 사용자 자료는 보존한다.
- 결과에는 관측한 수치와 제한만 기록하고 수행하지 않은 전체 verify·Review 성공을 추가하지 않는다. 측정 실패·중단·서로 다른 입력은 비교 불가로 남기며 기본 캐시 금지·ignore/sandbox 완화로 우회하지 않는다.

## Implementation

- `runtime/bootstrap.py`의 기존 cache 준비 로그와 `verification/timing.py`의 실제 phase·cache 관측을 재사용한다. 현재 bootstrap/runtime 테스트 fixture에서 명시한 동일 workload의 두 Python 실행과 무캐시 대조에 필요한 측정만 연결하며 별도 cache 서비스·설정·범용 benchmark 계층을 만들지 않는다.
- 실제 import·cache 효과를 확인할 Python 자식만 사용하고 Git 관측은 정책 unit에서 기존 double로 제공한다. 실제 worktree 정리의 확인이 필요한 경우 기존 runtime Git fixture에서 해당 효과와 보존 assertion을 한 번 검사하고 조합별 repository/게시를 반복하지 않는다. Worker는 작은 unit만 실행하고 실제 runtime 비교·정리 검증은 Manager 전체 verify에 연결한다.
- 최소 실측 산출물은 `docs/maintenance/`의 해당 작업 근거로 보존하고 실제 실행 revision·명령·입력과 연결한다. 이미 존재하는 로그/산출물로 전체 조건을 증명할 수 있으면 그 근거를 사용하고 중복 계측을 추가하지 않는다. PRODUCT의 캐시 동작과 ARCHITECTURE의 책임은 유지하며 구현 이력·개선율을 제품 계약에 넣지 않는다.

## Validation

- Worker는 `tests/test_bootstrap_foundations.py`와 `tests/test_verify_runtime.py`에서 변경한 수집/비교 경계의 최소 unit case만 실행한다. 실제 측정값·created/reused·write 정책·같은 입력 연결, 비교 불가와 실패 보존을 검사하며 sleep·고정 속도 임계값으로 성능 성공을 만들지 않는다.
- Manager 검증의 고유 회귀는 실제 Python cold/warm·명시적 무캐시 원본 결과, 계측 비용·cache 사용과 runtime 종료·재생성·정리다. 실제 Agent 호출 차단, 사용자 ignored 파일·tracked 원본 보존·현재 Git 작업 조건과 손상/쓰기 불가 fallback을 유지한다. 무관한 ref/worktree 변화는 실패 조건이 아니며 Worker에게 전체 suite·integration·verify를 실행시키지 않는다.
- Review는 보존한 실측 입력과 수치의 출처·비교 가능성·실제 cache 사용 assertion을 대조한다. 전체 verify 로그의 cache 변화 수나 준비 시간만으로 R-059의 실행 시간 비교 기준을 충족했다고 추정하지 않는다.

## Progress

- [x] 2026-10-09 최신 원격 기준의 bootstrap·timing·관련 테스트와 G-162의 남은 측정 계약을 정적으로 대조했다. 제공된 검토 근거와 조회한 저장 기록에서는 동일 입력의 실행 시간/관리 비용을 연결한 cold/warm 산출물을 확인하지 못했다. 구현·테스트·측정은 실행하지 않았다.
- [x] bootstrap runtime 자식 실행의 elapsed·revision·interpreter·cache tag·return code를 기존 operation log에 연결했다. 실제 Python cold/warm cache 준비 로그와 실행 결과를 확인하는 fixture 및 명시적 무캐시 실행을 확인하는 fixture를 targeted 실행했다.
- [x] 2026-10-23 현재 HEAD와 일치한 로컬 `origin/loop-manager/G-228` 추적 ref의 disposable clone에 등록한 runtime worktree로 실제 CLI startup cold/warm·무캐시 실행과 runtime worktree 재생성 후 warm 재사용을 측정해 [`G-228-runtime-bytecode-measurement.json`](../maintenance/G-228-runtime-bytecode-measurement.json)에 입력·수치·제한을 보존했다. fetch하지 않아 원격 최신성은 확인하지 않았다.
- [ ] Manager 실제 비교 근거·전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이번 R-059 변경은 runtime·후보 보존과 sandbox 권한의 책임을 구분한다. 미충족 cold/warm 실측 목표·분류·의존성·과거 기록은 유지하며 폐기된 Git 권한 검사를 새 측정의 완료 조건으로 요구하지 않는다.
- complexity low: 이미 있는 계측·Python fixture의 제한된 결과 연결이며 cache 정책·복구 설계 변경이 없다. risk low: 별도 소유 임시 자료의 측정과 근거 보존에 한정하고 사용자 자료·운영 상태를 변경하지 않는다. 잘못된 비교는 원래 측정과 제한을 보존해 교정할 수 있다. 활성 Acceptance Criterion의 필수 근거 보완으로 priority normal이다.
- G-162의 기본 cache 분리·checked-hash·명시적 환경 전달·손상/쓰기 불가 fallback과 제품·설계 반영은 다시 구현하지 않는다. 계측 코드의 존재와 계획 Merge를 실제 비교 완료로 간주하지 않으며 이 계획은 R-059의 남은 효율 확인 근거만 담당한다. 직접 구현에 소급 ExecPlan·자동 검증 성공을 만들지 않는다.
