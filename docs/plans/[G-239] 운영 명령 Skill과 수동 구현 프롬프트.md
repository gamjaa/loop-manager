---
id: G-239
title: 운영 명령 Skill과 수동 구현 프롬프트
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
depends_on:
  - G-194
  - G-195
  - G-196
  - G-234
  - G-235
  - G-236
  - G-237
  - G-238
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: medium
priority: normal
---

## Purpose

확정된 운영 CLI를 안내하는 Loop Manager 전용 Skill과 manual start 성공에서 복사할 수 있는 짧은 구현 프롬프트를 제공한다. 명령·대상·검증·후속 조치를 실제 준비 결과에 맞춘다.

## Acceptance Criteria

- Skill·도움말·사용 예시는 새 list/status/usage/logs·add/remove·run 범위·pause/resume·reset·manual cancel/stop 및 유지 명령의 의미·거부·결과 확인을 일치하게 안내한다. explain·제거된 config list/add/remove 호환 명령은 안내하지 않는다.
- 명령 Skill은 자체 저장소에만 남는다. 기존 copy-project-files가 `.agents` 전체를 복사하는 경계를 수정해 등록 프로젝트에는 Specification Skill와 그 ID 조회 지원 파일만 배포하며 다른 기존 준비/덮어쓰기 보호는 유지한다.
- start가 원격 준비를 완료한 경우에만 코드가 G ID·계획 경로·소유 branch/base·PRODUCT/Requirement/ARCHITECTURE 상대 경로·프로젝트 verify·push 뒤 manual complete 명령을 포함한 프롬프트를 생성한다. Manager 실행 위치와 다른 기기 구현 위치를 구분한다.
- 프롬프트는 작업 뒤 계획·PRODUCT·활성 Requirement·ARCHITECTURE에 따라 구현/검증을 재검증하고 누락·모순을 교정하라는 자연어 지시를 포함한다. 별도 Review CLI·Agent 생성 호출은 없다. 미완료 준비·자체 점검을 Manager verify/독립 Review/Merge 성공으로 표시하지 않는다.
- README는 바로 필요한 명령을 먼저 두고 세부 JSON 필드/자료형/단위/nullable·버전·token 포함 관계·종료/오류·대표 정상/부분/미확인 출력은 기존 운영 안내로 연결한다. 필수 지원 환경·권한·복구 주의는 보존한다.

## Implementation

기존 CLI 출력·manual start 결과와 Skill 구조를 재사용한다. 프롬프트 텍스트 생성은 준비 결과에 결합한 결정적 formatter로 두고 모델/설치 탐색을 추가하지 않는다. 복사 범위를 수정하며 G-230 배포 명령도 유지한다. PRODUCT·ARCHITECTURE의 Skill 소유/배포 책임을 반영한다.

## Validation

- targeted unit test로 준비 성공/실패·공백 경로·작업 문서·verify 환경별 명령·전체 내용과 모델 호출 부재를 확인한다. 고정 샘플로 실제 복사 스크립트의 전용 Skill 제외·Specification 파일 보존·충돌 거부를 검사한다.
- 전체 verify는 기존 수동 준비·배포·새 CLI 회귀를 유지한다. 사용자 문서·Skill 원본의 정책 문구를 테스트로 고정하지 않고 실제 출력/복사 동작을 검증한다.

## Progress

- [x] eb797d3a에 전용 명령 Skill·요청 형식의 프롬프트는 없고 복사 도구는 .agents 전체를 배포한다. 정적 확인만 수행했다.
- [ ] 안내·프롬프트·배포 경계 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 여러 CLI 결과와 복사 경계를 연결하지만 새 정책을 정하지 않는다. risk medium: 준비되지 않은 작업 안내 또는 전용 Skill의 외부 배포를 명시적 성공/경로 조건으로 제한하며 문서·배포 결과를 교정해 복구할 수 있다.
- 현재는 명령 구현 계획을 작성하며 Skill 파일을 생성하지 않는다. G-194/195/196의 조회 계약과 G-235~238의 제어를 반복 구현하지 않는다.
