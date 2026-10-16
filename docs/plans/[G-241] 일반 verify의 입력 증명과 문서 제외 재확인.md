---
id: G-241
title: 일반 verify의 입력 증명과 문서 제외 재확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-240
  - G-242
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

일반 프로젝트 스크립트와 자체 연결 runner의 실제 전체 verify 성공을 같은 프로젝트·ExecPlan·작업 주기에서 저장소 코드 입력이 같을 때 재사용한다. 최신 R-072에 따라 입력 목록·실제 코드 소비와 목록 밖 문서의 현재 제외 근거를 확인하며, 저장소 코드 입력의 누락·변경·비교 불가와 유효한 성공 근거 부재에서는 전체 실행을 유지한다. 실행 머신의 환경·설정·설치 도구·native/외부 동적 입력의 완전성 증명과 통제된 Linux infrastructure 도입은 범위 밖이다.

## Acceptance Criteria

- 일반 자체 verify와 연결된 runner의 실제 전체 성공 뒤 같은 프로젝트·ExecPlan·작업 주기의 저장소 코드 입력이 같으면 원래 run을 연결해 전체 verify를 생략한다. 문서만의 후보와 검증과 무관한 문서 변경을 포함한 rebase 모두 지원하고 isolated compile 성공만으로 일반 verify 충족을 주장하지 않는다. 공통 runner 도입을 필수로 요구하지 않는다. R-086 일반 선행 PASS 뒤에는 입력이 같아도 해당 승인 뒤 실제 전체 run을 유지하며 부분 성공·선행 결과로 전체 receipt를 만들지 않는다.
- 목록 자체와 형식·경로 해석·재사용 판정 규칙, 실제 선택한 저장소 verify 진입점, 소스·테스트·fixture·의존성 선언/lock·검증 스크립트/선언을 관측한다. 연결된 version 1 runner의 adapter·수집·필수 명령 단계도 저장소 선언과 코드 입력으로 반영한다. 파일 bytes·경로·kind·mode, 생성·삭제·이름 변경·미커밋/untracked와 rebase로 유입된 코드 입력을 비교하며 tree/SHA·명령 문자열·manifest 결과만으로 입력 동일성이나 누락 확인을 대신하지 않는다.
- 실행 머신의 환경변수·사용자/시스템 설정·설치된 Python/Git/Bash/Perl·native runtime·외부 파일/동적 입력·Git HEAD/이력/revision 값의 동일성은 verify 지문·완전성 조건에서 제외한다. `runtime_identity=None`이나 외부 identity 미확인·환경 변화만으로 재사용 거부·전체 실행·사용자 판단·새 infrastructure를 요구하지 않는다. 예제·운영 설정은 제외하되 저장소의 의존성 선언·검증 스크립트/선언·테스트가 읽는 고정 설정/자료 변경은 코드 입력으로 비교한다. 기존 환경 측정 자료는 감사로 보존할 수 있으나 재사용 gate로 쓰지 않고 인증 자료는 저장 근거·로그에 노출하지 않는다.
- 현재 자체 Windows의 `scripts/verify_windows.py`·`scripts/verify-windows-tests.json`은 이미 verify 입력 목록에 포함돼 있다. 그 추가를 재구현하지 않으며 bytes·경로·kind·mode 변경의 무효화를 유지한다. launcher의 snapshot·WSL/native 단계가 실제 소비하는 저장소 코드·검증 선언은 반영하고 WSL 도구/환경·설치 interpreter의 증명 부재는 재사용 거부 사유로 삼지 않는다. 같은 원래 SHA나 bundle hash만으로 저장소 코드 입력 관측을 대신하지 않는다.
- 목록 밖 문서 변경은 현재 경로·내용·kind·mode, 선택한 저장소 verify 진입점·판정 규칙과 목록의 코드 입력에 결합한 제외 근거를 다시 확인한다. 검증과 무관함을 확인하면 전체 verify 없이 현재 completeness를 갱신하고 기존 성공을 비교한다. 실제 문서 소비가 생기거나 영향·누락 여부를 확인하지 못하면 재사용하지 않고 전체 실행하며 필요한 목록 확인을 요청한다. 폴더/확장자 또는 과거 token·verify PASS만으로 제외를 승인하지 않는다.
- 목록 밖 새 저장소 코드·테스트/fixture·고정 자료·의존성/검증 선언·문서 소비·이동 및 누락 의심은 기존 Worker 확인 경계와 전체 verify로 처리한다. 저장소 코드 입력을 읽거나 비교할 수 없어도 전체 실행을 유지한다. 목록 부재/오류는 전체 실행과 기존 중복 없는 생성 요청 흐름을 따르며 즉시 Worker·Planner를 호출하거나 작업 branch 부재를 새 요청으로 바꾸지 않는다.
- 현재 `tests/test_execution_path.py`가 읽어 임시 source에서 실행하는 `scripts/setup.sh`를 자체 입력 목록에 포함한다. `tests/test_verify_inputs.py`의 setup 제외 기대와 `docs/VERIFY.md`·ARCHITECTURE의 제외 설명을 실제 소비 관계에 맞게 교정한다. setup bytes·kind·mode 변경은 관측 입력과 기존 제외 근거를 바꾸며, 소비하지 않는 다른 플랫폼 setup·prose까지 포괄적으로 추가하지 않는다.
- verify의 코드 입력 재사용과 Review의 문서·diff·실제 지침·승인 판단을 구분한다. R-082의 새 관련 완료 조건·문서 판단에 미검증 범위가 남으면 코드/명령이 같아도 과거 PASS를 새 조건의 성공으로 쓰거나 게시하지 않는다. 필요한 선행 Review·검증 보완·최종 후보 Review와 최신 원격·lease·소유권·dependency·사용자 승인·게시 gate를 유지한다. R-082 문서 변경 선행 PASS/VERIFY_REQUIRED를 자동으로 최종 승인으로 승격하지 않으며 Reviewer 호출 환경·정책 조건도 완화하지 않는다.
- 기존 receipt generation·새 run 무효화·실행 전후/재사용 직전 코드 입력 관측·현재 후보 연결을 재사용한다. 실패·timeout·미완료·출처 불명·다른 프로젝트/ExecPlan/작업 주기의 결과와 관측 중 코드 변경·저장 경쟁·중단은 재사용하지 않고 session/실패 감사를 보존한다. 과거 receipt는 새 코드 입력 기준·원래 run·작업 주기·후보 연결이 확인될 때만 재평가하고 누락된 과거 입력을 현재 값으로 합성하지 않는다. 생략은 새 invocation·성공 run·측정하지 않은 절감량을 만들지 않는다.

## Implementation

- [ ] `loop_manager_internal/verification/manager_adapter.py`·`inputs.py`, `execution/result_validation.py`·verify/revision/Review/publication handler와 기존 verify_receipts를 재사용해 관측을 R-072의 저장소 코드 입력 기준으로 전환한다. 일반 경로의 `runtime_identity=None`과 환경·Git/외부 identity 미확인에 따른 거부를 제거하고, 코드 입력 관측과 목록 밖 문서 제외 재확인 책임을 분리한다. completeness token은 현재 제외 파일과 목록 코드 입력·선택 저장소 진입점·판정 규칙에 결합하며 관련 파일이나 코드 소비 관계가 바뀌면 재확인한다. 기존 receipt·isolated compile 관측 자료와 실패 감사는 보존하되 새 gate에 외부 환경 증명을 요구하지 않는다. 기존 성공 token/receipt를 새 저장 체계로 중복하거나 과거 입력을 합성하지 않는다. R-086 부분 결과·preliminary_reviews와 전체 verify_receipts, R-082 문서 변경 선행 Review·최종 승인·게시 경계를 유지하고 관련 PRODUCT·ARCHITECTURE 설명을 구현한 코드 기준과 맞춘다.

- [ ] `scripts/verify.json`·`docs/VERIFY.md`와 `verification/contract.py`·`entrypoint.py`·`runner.py`·`core.py`·`unittest_adapter.py`의 현재 호출/수집 경계를 재사용한다. 진입점·core·adapter·의존성 선언 중 실제 읽는 저장소 코드와 검증 선언을 목록·지문에 반영하고 일반 프로젝트 스크립트와 연결 runner에 같은 코드 기준을 적용한다. 저장소 코드 입력 누락과 비교 불가의 전체 실행을 유지하며 외부 동적 소비자의 완전성·고정 toolchain·통제된 Linux 환경을 선행 조건으로 만들지 않는다. 공통 runner나 범용 분석/캐시 서비스를 새로 만들지 않고 venv 실행 경로를 임의로 resolve해 호출 환경을 바꾸지 않는다. 지원 문서는 코드 입력 재사용과 환경 재현성의 범위를 구분해 갱신한다.

- [ ] 직접 반영된 R-089의 Linux setup 회귀는 실제 `scripts/setup.sh`를 복사해 실행한다. 이 현재 의존성을 조사 출발점으로 삼아 `scripts/verify-inputs.json`의 누락과 `tests/test_verify_inputs.py`의 setup 제외 기대를 보완한다. `docs/VERIFY.md`·ARCHITECTURE의 관련 설명을 실제 저장소 소비 관계와 맞추고 소비하지 않는 플랫폼 setup·prose는 제외한다. PATH 저장·bootstrap 적용 자체는 다시 구현하지 않으며 목록 교정만으로 코드 입력 완전성이나 성공 재사용을 인정하지 않는다.

- [ ] 확인한 `scripts/verify_windows.py:runtime()`·`snapshot()`·`wsl_main()`·`native_main()`과 `tests/test_verify_inputs.py`의 두 파일 포함 기대를 재사용해 실제 저장소 코드 입력을 대조한다. 이미 포함된 Windows driver·선택 목록은 중복 추가하지 않고 코드 변경에 따른 무효화를 유지한다. WSL/native의 환경·설치 도구 미확인을 unknown으로 강제하는 기대는 코드 입력 누락·비교 불가와 분리해 교정한다. 두 단계 실행·필수 결과·실패 처리 계약은 유지하며 새 Windows/WSL 재현 환경을 도입하지 않는다.

## Validation

- `tests/test_verify_inputs.py`·`tests/test_verify_receipts.py`와 기존 단계 fixture의 targeted unit test로 유효한 일반 전체 run→reuse, 코드 입력이 같은 문서-only/rebase의 제외 재확인과 원래 run 연결을 확인한다. 소스·테스트·fixture·의존성 선언/lock·저장소 검증 스크립트/선언·선택 진입점·목록/규칙 변경, 파일 생성·삭제·이름·bytes·kind·mode·미커밋/untracked 변경, 목록 밖 누락·읽기/비교 실패에서는 전체 실행을 기대한다. tree/SHA나 선언·manifest만으로 입력 관측을 대신하지 않는다. 판정·SQLite 반영은 실제 코드로 확인하고 외부 호출은 기존 fixture를 사용한다.
- `tests/test_verify_inputs.py:VerifyInputObservationTest`의 command/environment/runtime/Git/unknown fixture와 제외 근거 회귀를 새 기준으로 갱신한다. 같은 코드 입력·유효한 성공·현재 제외 근거 아래에서 환경변수·예제/운영 설정의 내용·존재/부재·사용자/시스템 설정·설치 도구/native runtime·외부 파일/동적 입력·Git revision 값만 바뀌거나 `runtime_identity=None`·외부 identity 미확인이어도 재사용 가능해야 한다. 이 변화만으로 지문·completeness를 무효화하거나 전체 실행·사용자 판단·새 infrastructure를 요구하지 않는 기대와, 저장소 고정 설정/자료·의존성/검증 선언 변경으로 재사용을 거부하는 기대를 분리한다.
- 문서 제외 회귀는 목록 밖 문서의 현재 내용·경로·kind·mode 변경마다 재확인하고 비영향이면 전체 실행 없이 completeness를 갱신해 재사용하는 경우를 다룬다. 저장소 스크립트/선언이 새로 그 문서를 소비하거나 코드 입력·진입점·판정 규칙이 바뀌면 과거 제외 근거를 거부한다. 실제 문서 소비·누락 의심·비교 불가가 남은 경우 전체 실행하며 폴더/확장자·과거 token·verify PASS만으로 허용하지 않는다.
- 공통 verify 적합성 fixture와 Windows 관측 회귀는 선언/adapter·필수 단계·저장소 진입점/core 코드 변경의 무효화, venv 경로 보존을 확인한다. Windows launcher·선택 목록의 bytes·경로·kind·mode 변경과 snapshot의 실제 저장소 코드 누락은 재사용을 막되, WSL/native 환경의 증명 부재만으로 unknown을 기대하지 않는다. 원래 후보 SHA·bundle hash만으로 코드 입력 관측을 대체하지 않으며 테스트만을 위해 성공 receipt나 외부 입력 완전성을 합성하지 않는다.
- `tests/test_verify_inputs.py`의 자체 목록 회귀와 입력 관측 fixture에서 실제 setup 파일의 포함, bytes·kind·mode 변경의 무효화를 확인한다. 기존 `tests/test_execution_path.py`의 Bash fixture와 대응하며 문서 원본을 자동 fixture로 만들거나 목록 교정을 재사용 성공으로 기록하지 않는다.
- 기존 receipt/단계·`tests/test_preliminary_review.py`·revision/publication fixture로 부분 미사용·fallback 경로의 재사용과 R-086 일반 선행 PASS 뒤 필수 실제 전체 run을 별도 확인한다. 부분 성공·선행 PASS·reuse·waiver가 해당 전체 run을 대신하지 않아야 한다. R-082 새 조건의 미검증 범위·문서 변경 선행 PASS/VERIFY_REQUIRED의 자동 최종 승격을 거부하고 Review의 별도 입력·최종 원격/게시 gate를 유지한다. 새 실패·timeout·미완료·출처 불명·다른 작업 주기·코드 관측/CAS 경쟁·중단 재개에서는 오래된 성공을 채택하지 않으며 원래 receipt·session·실패 감사 보존과 과거 입력 합성 금지를 확인한다. 실제 모델은 호출하지 않고 생략을 새 실행 성공이나 미측정 절감량으로 기록하지 않는다.

## Progress

- [x] eb797d3a의 일반 result_validation 관측은 runtime_identity=None이고 운영 증명 adapter는 고정 Linux isolated compile만 지원한다. 일반 자체 verify·목록 밖 문서의 새 제외 근거는 남은 목표다. 구현·테스트 실행 없음.
- [x] 2026-10-09 `bac6f84e`의 공통 runner 연결을 정적으로 대조했다. `observe_verify_inputs`의 일반 관측은 계속 runtime_identity=None이며 연결된 runner는 isolated compile 관측에서도 unknown이다. 새 runner의 실행 구현과 입력 증명 미충족을 구분했고 구현·테스트·verify는 실행하지 않았다.
- [x] 2026-10-13 `041e6ee8`에서 R-089 직접 구현의 `tests/test_execution_path.py`가 실제 setup 스크립트를 소비하지만 목록·고정 회귀·설계 설명은 여전히 제외함을 확인했다. 기존 입력 완전성 목표의 구체적 미충족으로 반영했으며 테스트 실행이나 새 verify 성공은 기록하지 않았다.
- [ ] 관측·제외 재확인·일반 재사용 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- `bbce8cd25cdaa34a547b736cfc2f5d20f96a2a6d`의 Windows driver·선택 목록과 입력 목록 추가는 직접 구현이다. 일반 verify 입력 완전성 목표에 실제 두 환경의 소비 관계와 unknown 보존을 반영했으며, 새 누락 요청·중복 목록 생성·성공 재사용 완료를 만들지 않는다. 기존 metadata·dependency·수행 기록은 유지한다.
- 2026-10-13 mutable snapshot `5fb268cdf4cd026adedd8e23a2a66c68d3d8c12a0d6a57f2453cceed1f83e57c`의 같은 ID·경로·dependency·metadata·기존 기록을 보존했다. 실제 테스트 소비가 생긴 setup의 누락은 기존 R-072 입력 완전성 범위로 처리하며 별도 중복 계획이나 `manager:verify-inputs` 요청을 만들지 않는다.
- 2026-10-12 사용자 요청의 문서 정합화: R-086 일반 선행 승인 뒤의 실제 전체 검증 예외를 명시했다. 부분 실행·선행 Review를 재구현하지 않고 기존 입력 증명·문서 제외 목표, dependency·metadata·기록을 보존한다. source 경로 교정은 새 계획 생성이나 완료 선언이 아니다.

- complexity high: 일반 runner와 실제 외부 의존성의 완전성·환경 경계, 현재 문서 제외와 최신 완료 조건의 증명을 중단/저장 경쟁과 함께 풀어야 한다. risk high: 잘못된 reusable 판정은 필수 검증을 생략한 후보 게시로 이어지므로 불확실성 fallback·receipt 보존·최종 gate와 수동 PR 복구가 필요하다.
- G-182의 receipt·isolated compile와 G-183의 Review 재사용을 중복 구현하지 않는다. 새 목표는 활성 R-072에 명시됐으며 Manager verify-inputs 누락 요청이 아니다. priority는 제품 작업의 high다.
- 현재 원격 G-148 구현의 version 1·unittest 지원을 재사용하며 남은 목표는 같은 실행 계약의 입력 완전성과 현재 제외 근거다. `docs/VERIFY.md`의 unknown 설명과 현재 코드가 일치하므로 성공 재사용을 이미 구현한 것으로 판단하지 않는다. 보존된 f05cb9c6의 전체 verify는 새 runner·새 증명 조건의 PASS가 아니다.

- 2026-10-16 단일 교정: 최신 R-072의 저장소 코드 입력 동일성 계약이 과거 일반 verify의 외부 환경·runtime 완전성 증명 목표와 관련 테스트 기대를 대체한다. `runtime_identity=None`·외부 identity 미확인·환경 변화만으로 전체 실행·사용자 판단·고정 toolchain/통제된 Linux infrastructure를 요구하지 않는다. 저장소 코드 입력 누락·변경·비교 불가와 실제 문서 소비의 재확인, setup·Windows 입력 반영, 유효한 같은 작업 주기의 전체 성공·receipt/실패 감사, R-086 선행 PASS 뒤 실제 전체 run과 R-082 Review·게시 gate는 유지한다. frontmatter와 기존 Progress·Decision Log는 보존하며 이번 교정은 코드 구현·검증 성공·인프라 승인·작업 완료나 Manager 차단 해제를 뜻하지 않는다.
