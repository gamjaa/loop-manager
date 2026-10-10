---
id: G-240
title: 자체 verify의 고정 샘플과 실제 입력 축소
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

자체 verify의 문서·Skill·템플릿·README 및 예제 설정 원본 의존성을 고정 샘플로 대체하고 입력 목록을 실제 검증에 필요한 코드·테스트·스크립트로 좁힌다. 동작 assertion과 필수 수집 범위는 보존한다.

## Acceptance Criteria

- 문서·지침·설정 예제의 정책/안내 문구 검사를 전체 verify에서 제거한다. 파싱·해시·조립·등록·보존·profile 검증 기능은 tests 안의 고정 샘플로 같은 정상/실패 assertion을 유지한다. 실제 원본에서 샘플을 자동 복사·동기화하지 않는다.
- 프로젝트 준비·복사 회귀는 실제 copy-project-files 스크립트와 고정 샘플 원본을 사용해 결과 바이트·목적지·기존 파일 보존·덮어쓰기 거부를 확인한다. production 스크립트 동작을 테스트용 mock 성공으로 대신하지 않는다.
- verify-inputs.json은 실제 소스·테스트/고정 샘플·의존성·verify/copy 스크립트와 소비되는 run.sh/run.ps1을 포함한다. 문서/Skill prose·예제/운영 config·setup·ignore/attributes는 실제 의존성이 없으면 제외하며, 실제로 테스트되는 실행 코드가 .agents 아래 있다면 해당 코드 경로만 포함한다.
- 현재 선택된 `scripts/verify.json`의 version·adapter·프로젝트 interpreter·수집/명령 단계는 실제 실행 선언이므로 입력에 유지한다. unittest 수집·필수 compile와 adapter 적합성의 실행 코드/fixture를 보존하며, `docs/VERIFY.md`의 안내 문구를 실행 계약 검사로 대신하지 않는다.
- 목록을 좁히기 전에 모든 원본 소비자와 실제 검증 의존성을 확인한다. checkout 규칙에 따른 실제 스크립트 bytes·mode·환경은 관측 입력으로 유지한다. 운영 설정 존재/내용만으로 무효화하지 않지만 실제 실행값의 변경은 판단한다.
- compile·필수 테스트·실제 복사·launcher 연결·설정 기능의 검증을 축소/skip하거나 timeout을 늘려 완료하지 않는다. 자체 제외/완전성의 범위·근거와 한계를 PRODUCT·ARCHITECTURE에 반영한다.

## Implementation

`tests/support/origin.py` 등 준비 helper, 실제 원본을 읽는 테스트, `test_verify_inputs.py`·`test_provider_profiles.py`와 manifest를 대조한다. 관련 샘플과 준비 helper를 재사용해 필요한 최소 고정 샘플을 두고 기존 기능별 assertion을 연결한다. 예제 원본의 별도 일관성 확인은 전체 verify 밖에서 수행하며 새 상시 서비스/자동 문서 동기화는 추가하지 않는다.

Merge된 공통 runner의 `test_verify_contract.py`·`test_verify_runtime.py`와 실제 launcher/선언 소비도 대조한다. 정적 prose와 실제 실행 입력을 구분한 결과를 `docs/VERIFY.md`의 진단과 입력 절 및 PRODUCT·ARCHITECTURE에 반영한다. common core·adapter·지원 버전과 기존 선언 오류 거부를 변경하지 않는다.

## Validation

- 변경된 소비자의 최소 targeted unit test로 고정 샘플의 정상·손상·기존 파일 충돌·복사 바이트·파서/profile 경계를 검사한다. 실제 검증 계약과 구현을 대체하는 단순 문구 일치 테스트는 만들지 않는다.
- 전체 verify는 동일 manifest/수집·compile과 필수 Git/프로세스 회귀를 유지하고 문서 prose·예제/운영 config 변경의 비영향을 실제 입력 관측으로 확인한다. G-241의 성공 재사용은 별도이며 이 변경만으로 성공 receipt를 합성하지 않는다.

## Progress

- [x] eb797d3a의 manifest는 문서·Skill·예제 설정을 포함한다. origin fixture와 profile/manifest 회귀가 실제 원본을 읽어 요구한 입력 축소는 미충족이다. 정적 확인만 수행했다.
- [x] 2026-10-09 `bac6f84e`에서 새 `scripts/verify.json`·`docs/VERIFY.md`와 공통 adapter 경계를 읽었다. 선언은 실제 검증 입력이고 입력 목록의 원본 prose 의존성 축소는 남아 있다. 구현·테스트·verify 실행 없음.
- [x] 고정 샘플 전환·입력 목록 교정·16개 targeted unit 검증 완료. `scripts/verify`는 실행하지 않았으며 전체 verify는 Manager 확인 대기.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 여러 테스트의 원본 소비를 같은 고정 fixture 계약으로 전환하는 경계가 명확하다. risk medium: assertion/실제 입력을 누락하면 검증이 약해지므로 전환 대응과 전체 수집·compile을 확인하고 Git diff와 기존 회귀로 복구한다.
- 실제 R-081 구현에서 manifest의 기존 원본 누락을 추가한 수정은 당시 테스트 계약을 보존했다. 현재 R-072의 새 목표는 그 원본 의존성 자체의 제거이므로 이미 수행한 목록 보완을 실패나 새 실행 성공으로 재해석하지 않는다.
- G-148의 공통 runner·unittest 적합성은 현재 원격 구현으로 재사용한다. 이 계획은 원본 소비와 입력 목록만 교정하며 선언 파일·필수 단계·실제 프로세스 연결의 범위를 줄이지 않는다. 현재 지원 문서의 정적 목록 설명도 같은 변경에서 정합하게 갱신한다.
