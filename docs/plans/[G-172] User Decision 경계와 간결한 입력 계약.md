---
id: G-172
title: User Decision 경계와 간결한 입력 계약
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-069] User Decision 경계와 입력 양식.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

현재 Revision 필수·빈 approve/reject Patch 규칙을 R-069의 명시적 종료와 미리 채운 Patch 계약으로 바꾸며 과거 결정을 호환한다.

## Acceptance Criteria

- fence 밖 독립 줄의 `<!-- END USER DECISION -->`를 앞뒤 공백을 허용하는 정확한 종료로 사용한다. 과거 형식은 다음 guidance heading·최상위 `##` 절·EOF로 끝나며 LF/CRLF·마지막 개행 부재를 지원한다. 중복/짝 없는 marker와 잘못된 field는 진단하고 종료 뒤 메모를 결정으로 읽지 않되 실제 추가 guidance 제목은 대응 검증한다.
- 새 결정의 Revision은 선택적이고 Comment·설명용 HTML comment는 승인 identity나 patch에 포함하지 않는다. 기존 Revision·결정 identity는 보존한다.
- pending의 편집 patch는 승인으로 바뀌지 않는다. approve는 원래 patch와 같은 내용만 허용하고 다른 내용은 replace가 필요하다. reject는 변경을 적용하지 않으며 replace는 완전한 확정 patch를 검증한다. 빈 표현과 동일 원본 patch의 허용된 표현 차이는 같은 결정으로 정규화한다.
- 기본 요청은 pending·빈 backtick Comment·실제 Proposal의 전체 diff·필드 옆 짧은 안내 주석·종료를 제공하고 Revision이나 별도 안내문/표를 생성하지 않는다. Baseline은 사전 승인에만 전체 SHA 조건의 주석과 함께 사용한다. 재요청은 사용자가 편집한 Patch·Comment·메모·결정을 덮지 않는다. 자동 역할의 자기 승인은 금지하며 사용자의 특정 결정에 대한 명시적인 문서 편집 도움을 구분한다.

## Implementation

- documents/guidance_approval.py의 경계/parser·identity 정규화와 기존 PLANS 템플릿·Worker 안내를 함께 갱신한다. 문법 정규화와 결정 의미 검증을 분리하고 fence 내용을 임의 확장하지 않는다.
- preapproval·scope·Decision Log 검증의 소비자가 동일한 parser 계약을 사용하게 한다. 기존 결정·감사 기록을 재작성하거나 삭제하지 않는다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_guidance_approval.py, test_guidance_preapproval.py, test_execution_guidance.py에서 새/구 형식·marker·fence·Comment·각 결정/Patch·legacy identity의 최소 unit case를 실행한다.
- Manager 전체 verify는 사용자 결정 위조 거부, 정확한 patch 적용, baseline LF/CRLF와 사전 승인·거절 후 재개를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 공통 파서·Worker 요청 양식 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 여러 승인 소비자의 경계·identity 호환 때문에 complexity high, 잘못된 사용자 승인 채택 영향으로 risk high다. R-069가 이 범위에서 R-066의 필수 Revision/빈 patch 형식을 대체한다.

- 구현 기록: `guidance_approval.py`에서 fence 밖 종료·legacy 경계, 선택 Revision·Comment·안내 주석, 결정별 Patch 검증과 legacy identity 정규화를 처리한다. 기존 preapproval·scope·Decision Log 소비자는 같은 parser를 그대로 사용한다. Worker prompt와 `docs/AGENTS.common.md`는 원안 diff를 표시한 pending 양식을 안내하며 PRODUCT는 장기 규칙을 기록한다.
- 변경 경계: 이번 Worker 호출의 명시적 지시에 따라 `docs/PLANS.md`는 수정하지 않았다. 해당 파일의 기존 Revision 필수·빈 Patch 예시는 새 계약과 다르므로 문서 정합성 갱신은 별도 권한이 있는 작업에 남긴다. 기준 변경 시 새 요청 생성은 G-173 범위이며 이번 변경에 포함하지 않는다. 새 루트 항목·파일 이동·Requirement 편집은 없다.
- 검증 기록: `GuidanceFormatTest`·`GuidanceInputContractTest` 14개, `CommonGuidanceApprovalCompatibilityTests`·`WorkerGuidanceInputTests` 4개로 총 18개 통과. 실행 명령은 `python3 -m unittest tests.test_guidance_approval.GuidanceFormatTest tests.test_guidance_approval.GuidanceInputContractTest tests.test_guidance_preapproval.CommonGuidanceApprovalCompatibilityTests tests.test_execution_guidance.WorkerGuidanceInputTests`다. `scripts/verify.sh`는 기존 전체 검증 runner와 테스트 자동 탐색을 유지하므로 변경하지 않았고 실행하지 않았다.

- Review 대응: 종료 마커 추가로 기존 최상위 절 경계가 메모로 바뀐 문서는 원래 후속 절 전체를 포함한 호환 hash로도 대조한다. 승인 재개·checkpoint·이중 차단 복구가 같은 대조를 사용하며 저장된 hash·승인 이력은 재작성하지 않는다. 기존 후속 절 수정·삭제는 계속 거부하고 새 메모 제목은 기존 절의 정확한 suffix 대조를 방해하지 않는다. diff의 들여쓴 backtick context 줄은 닫는 fence로 읽지 않는다.
- 추가 검증: `GuidanceInputContractTest`의 두 회귀 case, `LegacyGuidanceMarkerResumeTest`, 기존 parser·preapproval·Worker 양식 unit class와 변경된 checkpoint·이중 차단 소비자 관련 세 case로 총 24개 통과. 기존 절 보존·변조 거부, LF/CRLF·pending 유지·동일 세션·원래 hash 보존과 실제 diff 적용을 확인했다. 전체 verify는 실행하지 않았다.
