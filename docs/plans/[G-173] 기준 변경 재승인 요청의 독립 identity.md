---
id: G-173
title: 기준 변경 재승인 요청의 독립 identity
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-069] User Decision 경계와 입력 양식.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
depends_on:
  - G-172
complexity: high
risk: high
priority: high
---

## Purpose

기준 파일 변경 시 과거 승인 문구를 다시 적용하지 않고 최신 기준에 결합한 새 요청을 보존·재개한다. G-172에서 Merge된 파서와 요청 양식을 재사용하고, 남아 있는 docs/PLANS.md의 결정 작성·재승인 안내를 같은 계약으로 맞춘다.

## Acceptance Criteria

- 대상 내용/mode·확정 patch·Baseline이 의미 있게 바뀌면 이전 결정을 무효화하고 새 request identity와 pending 결정을 만든다. 이전 요청·승인·거절·감사 이력을 보존한다.
- 새 요청은 최신 정확한 제안, 동일 요청 ID의 pending·빈 Comment·원안 전체 diff·종료 마커와 수정 위치를 제공한다. Revision을 생성하거나 요구하지 않고 일반 pending에는 Baseline을 넣지 않는다. 같은 기준의 재관측·재시작은 요청이나 알림을 중복하지 않으며 사용자가 편집한 Patch·Comment·메모를 덮어쓰지 않는다.
- 완전한 새 사용자 결정만 같은 Worker session과 작업물에서 재개한다. 부분 배치·옛 승인·자동 실행 Agent의 자기 승인·scope 위반은 적용하지 않는다. 사용자가 특정 결정과 범위를 명시해 요청한 모델의 문서 작성 도움은 R-069대로 수용하되 Comment·포괄적 허락을 Decision으로 읽지 않는다. 최종 base/patch 재확인·검증·Review·PR gate를 유지한다.
- `docs/PLANS.md`의 일반 요청·사전 승인·승인/거절/대체·재승인 예시와 편집 안내가 R-069의 실제 파서와 Worker 양식에 맞는다. Revision 필수·pending Patch 비움·새 common 제안 안내를 제거하고, 사전 승인 Baseline·배치 일치와 과거 Revision·common 승인 기록의 원래 대상 및 identity 보존을 설명한다. 기존 사용자 결정은 일괄 변환하지 않는다.

## Implementation

- guidance preapproval·실행 guidance·최종 scope 검사와 기존 승인 저장소에 request 생성/소비 lifecycle을 연결한다. 최신 기준 fingerprint와 기존 확정 결정을 별도로 보존한다.
- R-069 parser의 정규화 identity를 사용하되 기존 결정의 의미를 추정해 새 승인으로 이식하지 않는다. 변경된 파일과 pending 자료를 보존한 채 사용자 대기로 전환한다.
- docs/PLANS.md의 AGENTS 결정 절만 현재 요청 lifecycle에 정합하게 갱신한다. G-172의 파서·Worker 양식을 다시 구현하지 않으며 docs/AGENTS.common.md의 고정 runtime snapshot 계약을 변경하지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- guidance·scope 재승인 테스트에서 기준/patch/mode 변경, line ending만 변경, 반복 요청, 배치 일부 승인, 중단·재시작·최종 rebase의 최소 unit case를 실행한다.
- Manager 전체 verify는 정확한 새 patch 적용, 과거 승인 비적용·이력 보존, 거절·대체·독립 차단과 같은 session 재개를 검증한다.
- 문서 예시를 실제 공통 파서로 대조하는 targeted unit case로 Revision 없는 pending·approve·reject·replace, 사전 승인 Baseline, 원안 표시/빈 Patch의 같은 identity와 과거 형식을 확인한다. Manager 전체 verify는 새 요청 생성 전후·결정 등록 전후 중단에서 이력과 편집본이 보존되고 옛 승인으로 재개되지 않는지 확인한다.
- Reviewer는 새 요청과 과거 승인 혼용, 부분 배치의 적용, 편집본 덮어쓰기 및 문서/실제 생성 양식 불일치를 blocking issue로 확인한다. 잘못된 승인 적용은 원본 Git 내용과 승인·요청 감사를 이용한 수동 복원이 필요할 수 있으며, 코드 rollback만으로 이미 수행한 후속 작업이 복구된다고 가정하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-07 ad5450f의 G-172 Merge diff와 R-069, guidance 무효화·재개 코드 및 테스트를 정적으로 대조함. 새 요청 lifecycle과 docs/PLANS.md 정합성은 남아 있으며, 이전 planning 결과의 PRODUCT/G-172 기준 변경을 최신 원격 입력으로 재검토함.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-07 기준 94e9e0b0825d3bd2cbee5e75aac06f9f2a335e0f 및 새 mutable READY token 5b96f9d2d8d50ffcdf25f4829238a01aa03f6a52846d0435d8b315ce6ad09aa3의 원본 hash f348adbe662fdaf4c2a4447e6f662795a932a4c5fba7e0dcd60eacf965beea74를 재확인했다. 추가 PRODUCT/R-077 변경은 수동 자원 준비와 후속 순서이며 승인·요청 목표를 대체하지 않는다. 앞선 허용 개정을 보존하고 PRODUCT·G-172 및 User Decision은 수정하지 않았다.

- complexity high: 과거 승인 identity를 유지하면서 새 요청 생성·사용자 편집·중단 복구·최종 base 재확인을 연결하는 durable 전이 판단이 남아 있다. risk high: 옛 승인 재사용이나 편집본 손실은 승인 계약과 후속 실행을 잘못 진행시킬 수 있고 원본 Git 내용·승인 감사에 따른 수동 복원이 필요하다. 이 범위에서 비가역 손실이나 소유권 밖 쓰기를 새로 허용하지 않으므로 critical로 분류하지 않는다.
- mutable READY token 9843ff4e2fce9d360eb7cf3a46aeeeeae6307ef8303d801a65e8197db34e0a6c와 원본 hash를 대조해 같은 ID·sources·dependency·목표를 보존했다. R-069가 명시적으로 대체한 Revision/자기 승인 규칙과 미반영 docs/PLANS.md 안내를 기존 재승인 책임에서 함께 마무리하며 기존 `_published_plan_allows_common_plans`의 직접 Acceptance Criterion 경계를 사용한다. User Decision은 작성하거나 변경하지 않았다.
