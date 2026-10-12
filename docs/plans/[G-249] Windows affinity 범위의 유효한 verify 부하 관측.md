---
id: G-249
title: Windows affinity 범위의 유효한 verify 부하 관측
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: medium
priority: normal
---

## Purpose

유효한 Windows CPU 시간 차이·메모리 관측이 있어도 affinity 범위가 좁다는 이유로 busy를 unknown으로 바꾸는 분기를 교정한다. 허용 CPU의 보수적인 부하 추정으로 기존 coordinator가 안전하게 추가 배정하도록 한다.

## Acceptance Criteria

- 시스템 8 CPU·허용 affinity 4 CPU의 유효한 후속 저부하 관측에서 busy가 유효하고 추가 worker 배정이 가능하다. 시스템 전체 부하와 허용 범위를 구분하며 허용 CPU 부하를 과소평가하지 않는 추정을 적용한다.
- 첫 샘플·낡은 값·시간 차이/관측 오류에는 기존 직렬 축소를 유지한다. 높은 부하·메모리 부족·미반영 예약·사용자 cap과 평균 부하 보호를 우회하지 않는다. Linux cgroup quota의 usage 관측 부재·오류를 Windows 예외로 허용하지 않는다.
- snapshot/배정 진단에 관측 CPU 범위·affinity·부하 추정 방식과 제한/확대 사유를 남긴다. 시스템 시간을 실제 affinity별 측정값으로 표시하지 않으며 입력 불확실성은 그대로 보존한다.
- 기존 coordinator 확인·예약·공정성·종료 확인·timeout을 유지한다. 고정 병렬도·프로젝트 수 분할·새 OS 실행 제한이나 외부 자원 서비스를 도입하지 않는다.
- PRODUCT·ARCHITECTURE의 실제 부하/관측 실패 설명과 docs/VERIFY.md의 지원 한계를 구현에 맞추고, mock Windows 입력을 실제 Windows 성능 근거로 사용하지 않는다.

## Implementation

verification/resources.py의 windows_sample·ResourceSampler.sample와 기존 Snapshot/decide 경계를 재사용한다. Windows 시스템 시간의 차이를 허용 CPU 범위의 보수적 추정으로 해석하는 책임을 Linux quota/usage 해석과 구분한다. 관측 범위나 delta가 바뀔 때 유효성을 확인하고 기존 평균·snapshot age·메모리/예약 보호와 진단을 유지한다. coordinator 알고리즘이나 다른 OS 관측을 재설계하지 않는다.

## Validation

- Worker는 test_verify_resources.py의 sampler/decision 최소 unit case로 Windows 8/4 첫/후속 샘플, 저부하·고부하·관측 오류·낡은 값·cap/메모리/예약을 확인한다. 실제 sampler→decide→coordinator 연결을 쓰고 API·clock 관측만 mock한다.
- Linux quota/usage 없음과 기존 평균·시간 역행/카운터 오류 회귀를 보존한다. Manager 전체 verify는 다중 session 공정성·예약·종료 불명·관측 회복과 필수 결과/compile 범위를 유지한다.
- Manager가 실제 Windows affinity 제한 환경의 영향 모듈을 같은 revision 쌍·대상·계측/cache·Python/Git·자원/동시 부하 조건에서 반복 비교해 중앙값과 worker 수·진단을 남긴다. 기존 전체 기준과 최종 전체 verify를 연결하며 국소 비교마다 전체 suite를 반복하지 않는다. 미측정 플랫폼 효과나 전체 절감률은 주장하지 않는다.

## Progress

- [x] 2026-10-10 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-065와 Windows sample/ResourceSampler·decide·coordinator 회귀를 정적으로 대조했다. 구현·테스트·실측·verify는 실행하지 않았다.
- [ ] Windows 관측 교정·설명 정합화·targeted unit 검증
- [ ] Manager 전체 verify·실제 영향 범위 비교·읽기 전용 Review·사용자 Merge

## Decision Log

- 현재 sample은 Windows used를 계산한 뒤 usage가 없고 cpus<host_cpus이면 다시 None으로 만든다. 이는 Windows affinity 제한에서도 유효한 후속 관측을 쓰라는 새 R-065를 충족하지 않는다.
- complexity medium: 명확한 OS 관측 경계의 delta 유효성과 범위 추정을 기존 결정에 연결한다. risk medium: 과소 부하 추정은 자원 압력과 verify 지연을 만들 수 있다. 기존 cap·메모리·예약 보호, 신규 배정 중단과 안전 재실행으로 복구하며 데이터/게시 정책은 바꾸지 않는다. priority normal이다.
- G-187의 실제 OS/언어 적합성 자료, G-248의 시점 집계와 독립이다. 기존 예약 알고리즘을 소급 계획하거나 새 infrastructure·사용자 정책을 추가하지 않는다.
