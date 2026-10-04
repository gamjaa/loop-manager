# [R-066] AGENTS 변경 사전 승인

## Context

사용자가 구현 전에 정확한 AGENTS.md 변경안을 승인해도 현재 Loop Manager는 Worker가 제안한 뒤 중단하는 절차만 인식한다. 공통 지침 분리 후 기존 본문을 정리하는 작업처럼 변경 내용과 적용 순서를 미리 확정할 수 있는 경우에는 같은 승인을 다시 요청할 필요가 없다.

## Requirement

사용자는 게시할 ExecPlan의 Decision Log에 기존 Proposal과 User Decision 형식으로 AGENTS.md 변경을 사전 승인할 수 있다. 정확한 patch와 approve 또는 replace 결정, 비어 있지 않은 Revision 및 기준 commit의 전체 SHA를 담은 Baseline이 필요하며 reject도 변경 없는 결정으로 처리한다. 사전 승인 배치는 같은 Baseline을 사용한다. 채팅의 포괄적 허락이나 일반 본문만으로 승인하지 않는다.

Loop Manager는 Worker 시작 전에 원격 기준에서 채택한 계획 revision의 결정과 Baseline의 AGENTS.md를 확인한다. Baseline은 같은 원격 기준 이력의 commit이어야 한다. 현재 작업 파일의 제안·결정과 승인 대상의 기준 내용·파일 mode가 같아야 승인된 변경 범위를 전달한다. 승인 문구를 유지한 채 원격 기준만 갱신해도 승인 기준을 새 파일로 바꾸지 않는다. 실제 적용은 Worker가 계획의 목표와 dependency 조건에 따라 수행하며 Manager는 AGENTS.md를 직접 편집하지 않는다.

기준 파일이 달라졌으면 작업 파일을 보존하고 기존 사용자 결정 대기로 전환한다. 사용자가 최신 기준에 맞는 결정 Revision 또는 대체 patch를 기록한 뒤 재개한다. 사전 승인도 실제 결과와 최신 base 대조, 전체 검증, Review, PR 및 사용자 Merge를 생략하지 않는다.

## Acceptance Criteria

- 원격 기준에 게시된 완전한 승인·거절·대체 결정을 Worker 호출 전에 SQLite에 등록하고 기존 guidance 문맥과 범위 검사에 전달한다.
- 결정의 대상, 제안과 확정 patch, Revision 및 기준 내용이 결합되며 재시작해도 같은 승인을 중복 등록하거나 적용하지 않는다.
- 사전 승인 배치의 모든 결정은 확정돼야 한다. pending만 있는 계획은 기존 절차를 유지하며 pending과 확정 결정이 섞인 배치는 Worker 호출 전에 불완전한 사전 승인으로 거부한다.
- 승인 당시 파일과 현재 파일의 의미 있는 내용 또는 mode가 다르면 Worker를 호출하지 않고 재승인을 기다린다. Git checkout의 LF/CRLF 변환만으로 승인을 무효화하지 않는다.
- 로컬 계획에만 추가한 승인, Worker가 추가하거나 변경한 결정, 다른 계획의 승인과 Scope 위반은 승인으로 인정하지 않는다.
- 사전 승인 이후 추가 변경 제안은 기존 pending 절차를 따른다. 최종 결과가 승인된 내용과 다르거나 최신 base가 바뀌면 기존 재승인 검사를 통과해야 게시할 수 있다.
- docs/PLANS.md와 Worker 지침이 실제 사전 승인 절차를 설명한다.

## Product Impact

- AGENTS.md의 정확한 변경안은 사용자 게시 단계에서 사전 승인할 수 있으며 Worker 실행 중 같은 결정을 다시 요청하지 않는다.
- 승인되지 않은 변경과 변경된 기준에는 기존 사용자 판단 및 게시 검증을 유지한다.

## Non-Goals

- 포괄적인 수정 권한이나 자동 Merge를 추가하지 않는다.
- 공통 지침 주입 또는 기존 AGENTS.md 본문 정리 자체를 구현하지 않는다.
- Planner와 Reviewer에 AGENTS.md 제안·수정 또는 사용자 결정 작성 권한을 주지 않는다.

## Open Questions

- 없음
