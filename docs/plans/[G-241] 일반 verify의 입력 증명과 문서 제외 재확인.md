---
id: G-241
title: 일반 verify의 입력 증명과 문서 제외 재확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-240
  - G-242
complexity: high
risk: high
priority: high
---

## Purpose

일반 자체 verify와 현재 연결된 version 1 unittest runner의 실제 명령·환경·runtime·Git/외부 입력을 증명해 같은 작업 주기의 성공을 재사용한다. 기존 isolated compile 관측을 유지하고 목록 밖 문서의 현재 비영향·완전성을 다시 증명하며, 미확인 입력에서는 전체 실행을 유지한다.

## Acceptance Criteria

- 일반 자체 verify의 실제 성공 뒤 같은 프로젝트·ExecPlan·작업 주기의 현재 입력이 같으면 전체 verify를 생략한다. 문서만의 후보와 문서 변경을 포함한 rebase 모두 지원하고 isolated compile 성공만으로 일반 verify 충족을 주장하지 않는다. 공통 runner 도입을 필수로 요구하지 않는다.
- 실제 명령·유효 실행값/환경·interpreter/의존 runtime·Git/외부 입력과 목록 자체·파일 bytes/경로/kind/mode·미커밋/untracked를 관측한다. tree/SHA·명령 문자열·버전 이름만으로 동적 의존성 부재를 추정하지 않는다. 안전하게 측정할 수 없는 일반 스크립트/환경은 전체 verify를 실행한다.
- 연결된 runner에서는 실제 선택 선언의 version·adapter·수집/명령 단계, 프로젝트 interpreter와 의존성, 별도로 선택된 Manager runtime의 진입점·core·adapter 및 유효 호출 context를 결합한다. venv executable 경로를 임의로 resolve해 환경을 바꾸지 않으며 run/session identity·지원 metadata·manifest 결과만으로 외부 입력 완전성을 추정하지 않는다. 인증 자료는 저장 근거·로그에 노출하지 않는다.
- 목록 밖 문서 변경은 현재 경로·내용·실행 context·목록 입력에 결합한 제외 근거를 다시 확인한다. 실제 비영향이 증명됐으면 현재 completeness를 갱신하고 기존 성공을 비교한다. 폴더/확장자 또는 과거 token·verify PASS만으로 제외를 승인하지 않는다.
- 목록 밖 새 코드·설정·문서 의존성·이동 및 누락 의심은 기존 Worker 확인 경계와 전체 verify로 처리한다. 목록 부재/오류는 전체 실행과 기존 중복 없는 생성 요청이며 작업 branch 부재를 새 요청으로 바꾸지 않는다.
- 새 관련 완료 조건·문서 판단의 미검증 범위가 남으면 코드/명령이 같아도 과거 PASS를 새 조건에 재사용하지 않는다. 유효한 최신 조건의 증거·R-082 선행 Review/검증 보완과 최종 Review/게시를 각각 확인한다.
- 기존 receipt generation·새 run 무효화·실행 전후/재사용 직전 관측·현재 후보 연결을 재사용한다. 실패·관측 중 변경·저장 경쟁·중단·다른 작업 주기는 재사용하지 않고 session/실패 감사를 보존한다. 생략은 새 invocation·성공 run·측정하지 않은 절감량을 만들지 않는다.

## Implementation

`verification/manager_adapter.py`·`inputs.py`, `execution/result_validation.py`·verify/revision/Review/publication handler와 기존 verify_receipts를 확장한다. 자체 검증 계약의 실제 입력을 먼저 조사하고 측정 가능한 관측 adapter와 문서 제외 재확인 책임을 분리한다. 임의 스크립트의 의존성을 모델 추측으로 확정하거나 새로운 범용 분석/캐시 서비스를 만들지 않는다. 기존 성공 token/receipt를 새 저장 체계로 중복하지 않는다. PRODUCT·ARCHITECTURE의 현재 제한된 지원과 새 증명 범위·fallback을 정합하게 반영한다.

`scripts/verify.json`·`docs/VERIFY.md`와 `verification/contract.py`·`entrypoint.py`·`runner.py`·`core.py`·`unittest_adapter.py`의 현재 호출/수집 경계를 재사용한다. Manager runtime과 후보 프로젝트 구현을 각각 읽는 실제 경로, 실행 환경과 동적 소비자를 조사하며 공통 runner를 재구현하거나 비Python/OS 실측을 선행 완료로 요구하지 않는다. 지원 문서의 입력·재사용 한계를 구현한 증명 범위에 맞춰 갱신한다.

## Validation

- 기존 입력/receipt/단계 fixture targeted unit test로 일반 verify의 실제 run→reuse, 문서-only/rebase 제외 재확인, 코드·명령·환경·runtime/Git 변경, 목록 밖 누락·새 조건 미검증·새 실패·CAS/관측 경쟁을 확인한다. 판정 자체는 실제 코드·SQLite로 실행한다.
- 공통 verify 적합성 fixture를 재사용해 선언/adapter·필수 단계 변경, 프로젝트 Python·외부 의존성과 Manager runtime 변경, venv 경로 보존 및 관측 불가 fallback을 확인한다. 선언·manifest의 정적 identity만으로 성공을 재사용하는 경로는 거부한다.
- 전체 verify는 일반 자체 verify 성공의 유효한 증명 연결과 실제 프로세스 생략/필수 실행, 원래 receipt·작업 주기/실패 감사, 중단 재개와 Review/최종 원격 gate를 확인한다. 실제 모델은 호출하지 않는다. 입력 증명이 불가능한 경로를 테스트 편의를 위해 reusable로 표시하지 않는다.

## Progress

- [x] eb797d3a의 일반 result_validation 관측은 runtime_identity=None이고 운영 증명 adapter는 고정 Linux isolated compile만 지원한다. 일반 자체 verify·목록 밖 문서의 새 제외 근거는 남은 목표다. 구현·테스트 실행 없음.
- [x] 2026-10-09 `bac6f84e`의 공통 runner 연결을 정적으로 대조했다. `observe_verify_inputs`의 일반 관측은 계속 runtime_identity=None이며 연결된 runner는 isolated compile 관측에서도 unknown이다. 새 runner의 실행 구현과 입력 증명 미충족을 구분했고 구현·테스트·verify는 실행하지 않았다.
- [ ] 관측·제외 재확인·일반 재사용 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: 일반 runner와 실제 외부 의존성의 완전성·환경 경계, 현재 문서 제외와 최신 완료 조건의 증명을 중단/저장 경쟁과 함께 풀어야 한다. risk high: 잘못된 reusable 판정은 필수 검증을 생략한 후보 게시로 이어지므로 불확실성 fallback·receipt 보존·최종 gate와 수동 PR 복구가 필요하다.
- G-182의 receipt·isolated compile와 G-183의 Review 재사용을 중복 구현하지 않는다. 새 목표는 활성 R-072에 명시됐으며 Manager verify-inputs 누락 요청이 아니다. priority는 제품 작업의 high다.
- 현재 원격 G-148 구현의 version 1·unittest 지원을 재사용하며 남은 목표는 같은 실행 계약의 입력 완전성과 현재 제외 근거다. `docs/VERIFY.md`의 unknown 설명과 현재 코드가 일치하므로 성공 재사용을 이미 구현한 것으로 판단하지 않는다. 보존된 f05cb9c6의 전체 verify는 새 runner·새 증명 조건의 PASS가 아니다.
