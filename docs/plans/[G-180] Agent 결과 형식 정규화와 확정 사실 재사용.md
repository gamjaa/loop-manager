---
id: G-180
title: Agent 결과 형식 정규화와 확정 사실 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
depends_on:
  - G-176
complexity: medium
risk: high
priority: normal
---

## Purpose

의미가 하나인 표식·공백·정형 identity의 처리를 기존 결과 경계에서 수행하고 확정된 빈 chunk 사실을 다시 모델에게 쓰게 하지 않는다.

## Acceptance Criteria

- Worker·Reviewer·Planner·모듈 검토의 실제 결과에서 의미가 확정된 marker 배치/구분자/공백을 정규화한다. 검증된 호출 입력의 ID/revision/manifest/digest/link만 결합하고 형식 교정만으로 모델 호출·실패 횟수·level 승격을 만들지 않는다.
- 상충 verdict·명시적 identity 불일치·필수 판단/issue/원본 인용/검증 근거 누락은 성공으로 보충하지 않는다. 의미 교정은 해당 기존 역할에서 계속한다.
- 기존 code map의 빈 파일/공백 chunk와 확정된 관계/정형 정보는 사실 그대로 재사용한다. 동적·미지원·파싱 실패 관계를 확정하지 않으며 빈 파일이 속한 의미 있는 모듈 검토를 유지한다.
- 원본 revision·scope·protocol과 실제 원문/정규화 결과를 결합해 중단 뒤 같은 처리를 안전하게 재개한다. 과거 평가 단계를 새 기본 흐름으로 추가하지 않는다.

## Implementation

- execution/result_validation.py·planning 및 maintenance 결과 parser의 공통 정규화 책임을 필요한 최소 helper로 묶는다. 각 역할의 의미 검증과 결과 저장·범위 검사를 보존한다.
- maintenance의 현재 code index·모듈 manifest 생성에서 객관적 사실 재작성만을 제거한다. 원본을 확인할 수 없는 결과는 보수적으로 기존 의미 판단에 남긴다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Worker result·review protocol·planning/모듈 result 테스트에서 허용 형식 변형, 모순/누락/identity 오류, 빈 chunk·동적 관계와 실제 no-call의 최소 unit case를 실행한다.
- Manager 전체 verify는 필수 판단·원본 증거·Review·scope, 교정 이력·승격과 code map 원본 결합을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-07 결과 marker·구분자·공백·다중 줄 JSON 정규화, 검증된 호출 identity 결합, 공백 파일·chunk 사실 재사용과 원문·정규화·scope·protocol 재개 검증을 구현함. Worker·Reviewer·Planner·모듈·파일 결과의 targeted unit 검증을 통과함. (이전 실행 기록이며 외부 결과가 현재 수동 후보에 제공되지 않아 현재 검증 근거로 승계하지 않음.)
- [x] 2026-10-07 `bash scripts/verify.sh` 전체 verify 통과 기록: 199개 모듈, 3,038개 성공·2개 skip, 종료 코드 0. (이전 실행 기록이며 현재 수동 후보의 전체 verify 근거는 NOT_RUN.)
- [x] 2026-10-07 `origin/main` 60b956d rebase 뒤 AC와 실제 경로를 대조하고 회귀를 보완했다는 이전 실행 기록: 관련 테스트 84개 통과. (외부 결과가 현재 수동 후보에 제공되지 않아 현재 검증 근거로 승계하지 않음.)
- [x] 2026-10-07 rebase 후 upstream verify 입력·최종 전문 검증·모듈 평가 관련 테스트 60개와 전체 verify 3,101개 성공·2개 skip을 기록함. 보고서 경로: `.cache/verify/6abde7eaf42d4b0eb0bc30e1a45ef3f7/summary.json`. (이전 실행 기록이며 현재 수동 후보의 전체 verify 근거는 NOT_RUN.)
- [x] 2026-10-07 수동 후보의 rebase 충돌 파일에서 upstream PASS/FAIL 복구 경계 검증과 형식 정규화 no-retry 검증을 함께 보존함. 기존 중복 테스트만 통합하고 충돌 표식을 제거함.
- [x] 2026-10-07 현재 후보의 targeted unit 28개 통과: Worker/Reviewer/Planner 결과, 복구 이력, 파일·모듈 결과, 빈 chunk·공백 module 및 no-call 검증. 전체 verify는 실행하지 않음.
- [ ] 현재 후보의 Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 기존 parser의 제한된 normalization과 code-index 사실 소비로 complexity medium, 허위 성공/근거 생성 영향으로 risk high다.
