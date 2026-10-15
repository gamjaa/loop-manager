---
id: G-239
title: 운영 명령 Skill과 수동 구현 프롬프트
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
  - docs/requirements/[R-077] G 계획 수동 구현과 R·코드 일괄 제출.md
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
  - G-254
complexity: medium
risk: medium
priority: normal
---

## Purpose

확정된 운영 CLI를 안내하는 Loop Manager 전용 Skill과 manual start 성공에서 복사할 수 있는 짧은 구현 프롬프트를 제공한다. 프로젝트 구현·재검증·push까지의 지시는 복사용 프롬프트에, Manager에서 실행할 완료 명령과 선택적 생략 사유 안내는 별도 사용자 출력에 둔다. 명령·대상·검증·후속 조치를 실제 준비 결과에 맞춘다.

## Acceptance Criteria

- Skill·도움말·사용 예시는 새 list/status/usage/logs·add/remove·run 범위·pause/resume·reset·manual cancel/stop 및 유지 명령의 의미·거부·결과 확인을 일치하게 안내한다. explain·제거된 config list/add/remove 호환 명령은 안내하지 않는다.
- R-074/R-076: `maintenance-review <repo>`는 등록 프로젝트의 다음 안전한 경계에 유지보수 전환을 요청하는 기존 명령으로 안내한다. `[loop] refactoring_review_interval`의 기본값 20·0의 자동 전환 비활성화와 0·주기 도달 전의 명시적 요청 가능성을 설명한다. 요청 저장·실제 시작·pause/실행 중 작업/Open PR/프로젝트 안전 gate에 따른 대기를 구분하며 설정 변경으로 누적 Merge 기준·활성 cycle이 초기화된다고 안내하지 않는다.
- 명령 Skill은 자체 저장소에만 남는다. 기존 copy-project-files가 `.agents` 전체를 복사하는 경계를 수정해 등록 프로젝트에는 Specification Skill과 자체 보조 파일을 함께 배포하며 다른 기존 준비/덮어쓰기 보호는 유지한다. `next_requirement_id.py`·`read_requirements.py`·`requirement_documents.py`를 포함하고 Manager 내부 모듈을 복사하거나 설치·DB·실행 상태에 의존하지 않는다.
- R-076: start가 원격 준비를 완료한 경우에만 코드가 G ID·계획 경로·소유 branch/base·PRODUCT/Requirement/ARCHITECTURE 상대 경로·프로젝트 verify·재검증·소유 branch push까지의 지시를 포함한 프롬프트를 생성한다. 복사용 프롬프트에 PR 생성이나 Manager 명령 실행을 지시하지 않는다. 별도 사용자 출력에서 push 뒤 Manager에서 실행할 `manual complete <repo> <id>`를 안내하며 Manager 실행 위치와 다른 기기 구현 위치를 구분한다.
- R-076/R-077: Skill·수동 완료 안내는 complete/submit에서 검증·Review 생략을 사용자가 선택할 때 실제 수행 내용·결과 등 근거를 짧게 작성해 `--skip-reason`으로 전달하도록 안내한다. G-254의 선택 입력 계약을 사용해 사유 미입력도 수락되고 생략 시 전체 `--expected-head`는 필수임을 설명한다. 사유를 추가 수락 조건으로 만들거나 생략 옵션을 자동 선택하지 않으며 수행하지 않은 검증·Review를 완료했다고 작성하지 않는다. 외부 수행·사용자 면제와 Manager의 실제 실행 결과를 구분한다.
- 프롬프트는 작업 뒤 계획·PRODUCT·활성 Requirement·ARCHITECTURE에 따라 구현/검증을 재검증하고 누락·모순을 교정하라는 자연어 지시를 포함한다. 별도 Review CLI·Agent 생성 호출은 없다. 미완료 준비·자체 점검을 Manager verify/독립 Review/Merge 성공으로 표시하지 않는다.
- README는 바로 필요한 명령을 먼저 두고 세부 JSON 필드/자료형/단위/nullable·버전·token 포함 관계·종료/오류·대표 정상/부분/미확인 출력은 기존 운영 안내로 연결한다. 필수 지원 환경·권한·복구 주의는 보존한다.

## Implementation

기존 CLI 출력·manual start 결과와 Skill 구조를 재사용한다. 프롬프트 텍스트 생성은 준비 결과에 결합한 결정적 formatter로 두고 모델/설치 탐색을 추가하지 않는다. 복사 범위를 수정하며 G-230 배포 명령도 유지한다. PRODUCT·ARCHITECTURE의 Skill 소유/배포 책임을 반영한다.

- [ ] 확인한 `loop_manager.py`의 명령 도움말, `application/commands.py`의 요청 접수 출력과 `README.md`·`docs/OPERATIONS.md`를 새 명령 Skill의 안내와 대조한다. README·운영 안내에 직접 반영된 0 설정·요청 예시는 재사용하고 도움말·Skill의 남은 설명만 맞춘다. `config/managed_config.py`·`storage/maintenance_state.py`·`maintenance/tiered_review.py`의 0 지원과 기존 요청 저장·소비는 구현돼 있으므로 다시 만들지 않는다.
- [ ] 예정인 자체 운영 명령 Skill의 경로는 기존 `.agents/skills/` 안에서 정한다. 확인한 `scripts/copy-project-files.sh`·`.ps1`은 현재 `.agents` 전체를 복사하므로 위 배포 조건대로 Specification Skill과 자체 보조 파일만 복사하도록 좁힌다. 사용자 프로젝트로 운영 Skill을 복사하거나 요청 명령을 실제 호출하는 예시 검증을 하지 않는다.
- [ ] 확인한 `application/manual_work.py`의 start 성공 결과·출력과 기존 `validate_options()`·`execution/manual_candidate.py`의 생략 표시를 대조한다. 복사용 formatter는 프로젝트에서의 구현·검증·push까지만 생성하고 별도 사용자 출력이 Manager의 complete 명령을 담당하게 한다. G-254 뒤의 선택적 사유 계약을 Skill·완료 안내에 연결하되 옵션 수락·정규화·감사·Reviewer/PR 표시 구현은 재작성하지 않는다. complete/submit의 생략 안내는 실제 수행 사실을 사용자가 제공하는 절차이며 프롬프트 생성기가 사유나 성공을 합성하지 않는다.

## Validation

- targeted unit test로 준비 성공/실패·공백 경로·작업 문서·verify 환경별 명령·전체 내용과 모델 호출 부재를 확인한다. 고정 샘플로 실제 복사 스크립트의 전용 Skill 제외·Specification 파일 보존·충돌 거부를 검사한다. Manager가 없는 복사 대상에서 ID 조회와 활성/archive 존재 여부·활성 제목/경로/기존 Summary 조회를 실행해 독립 배포를 확인한다.
- `tests/test_manual_work.py`·`tests/support/manual_work.py`의 start 준비·출력 fixture를 재사용해 복사용 프롬프트에는 프로젝트 재검증·push 지시가 있고 PR 생성·Manager 명령 실행 지시는 없으며, 별도 출력에 정확한 repository·G ID의 complete 명령이 있는지 최소 unit case로 검사한다. 준비 실패·미완료에는 준비 완료용 프롬프트가 없어야 한다. 생략 안내 출력을 바꾸면 사용자가 선택한 옵션·필수 전체 expected SHA·선택적 사유를 구분하는 case를 추가하고, 실제 옵션의 사유 생략/빈 값/공백 수락·reason 단독 거부는 G-254의 회귀를 재사용한다. 새 Skill prose 검사나 실제 Manager 실행은 추가하지 않는다.
- `tests/test_tiered_commands.py`의 요청 저장·미등록 거부 fixture를 재사용한다. 출력이나 도움말을 바꾸면 해당 최소 case에서 요청 저장 성공과 실행 미호출을 확인한다. 설정 0·양수 전환의 기존 `tests/test_managed_configuration.py`·`tests/test_maintenance_state.py`·`tests/test_tiered_review.py`를 새로 복제하지 않으며, Skill prose의 내용은 실제 CLI 계약과 읽기 전용으로 대조한다.
- 전체 verify는 기존 수동 준비·배포·새 CLI 회귀를 유지한다. 사용자 문서·Skill 원본의 정책 문구를 테스트로 고정하지 않고 실제 출력/복사 동작을 검증한다.

## Progress

- [x] eb797d3a에 전용 명령 Skill·요청 형식의 프롬프트는 없고 복사 도구는 .agents 전체를 배포한다. 정적 확인만 수행했다.
- [ ] 안내·프롬프트·배포 경계 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이번 full 검토의 snapshot token `e132bfba3ae59a16ffe8946cee88c634ff2fe84884991a2a7a077b51f8849b29`·revision `656432eb4c384eb1146665a87d4342f5e42712fb`와 원본 hash `29d4cd4fbea64d9826c9e886564e24adec8263d8ad0c96160047664804934444`를 확인했다. 활성 R-076의 복사용 프롬프트/별도 완료 안내·선택적 생략 사유 조건을 R-077·현재 `validate_options()` 및 G-254와 대조했다. 기존 프롬프트의 complete 포함 지시를 별도 출력으로 바로잡고, G-254가 위임한 안내 목표와 해당 선행 Merge를 같은 ID에 연결한다. G-254의 CLI 선택 입력 구현을 중복 계획하지 않으며 기존 dependency 순서·metadata·수행 기록을 보존한다. complexity medium·risk medium은 기존 formatter·여러 출력과 배포 경계를 연결하는 난도와 잘못된 실행 위치/생략 안내로 생길 진행 오류에 그대로 적용한다. 구현·테스트·전체 verify와 후보 worktree 변경은 수행하지 않았다.
- 2026-10-15 delta: R-074의 0 지원과 R-076의 명시적 요청은 원격 코드·테스트에 직접 반영됐다. 새 CLI나 소급 구현 계획을 만들지 않고 이 작업의 도움말·Skill 범위에 남은 안내를 통합한다. 고유 R-076의 현재 활성 경로로 source를 정합화했으며 dependency와 기존 목표를 보존했다. 구현·테스트·verify를 실행하지 않았다.
- complexity medium: 여러 CLI 결과와 복사 경계를 연결하지만 새 정책을 정하지 않는다. risk medium: 준비되지 않은 작업 안내 또는 전용 Skill의 외부 배포를 명시적 성공/경로 조건으로 제한하며 문서·배포 결과를 교정해 복구할 수 있다.
- 현재는 명령 구현 계획을 작성하며 Skill 파일을 생성하지 않는다. G-194/195/196의 조회 계약과 G-235~238의 제어를 반복 구현하지 않는다.
