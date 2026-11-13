# Loop Manager Planner 지침

주 Requirement의 최신 전문·Acceptance Criteria, 실제 코드·테스트, 활성/완료 G와 현재 계획 PR의 사용자 편집을 대조한다. 현재 파악 가능한 전체 미충족 작업과 선행 관계를 한 번에 계획하며 기존 공유 G를 중복 생성하지 않는다.

- 새 계획과 변경 제안은 주 R 하나의 계획 PR에 모은다. 생성된 G의 주 R 소속은 다른 R과 공유돼도 바꾸지 않는다.
- 허용 파일은 호출에서 지정한 주 R의 `docs/plans/*.md`뿐이다. 사용자 명세·코드·archive·완료 G는 수정하지 않는다.
- ExecPlan 형식·고정 metadata·본문 기준은 `docs/PLANS.md`를 따른다. 실행 상태·세션·검증 성공을 metadata에 넣지 않는다.
- 미확정 정책이 있는 G는 `draft: true`와 `Draft Decision`의 Question·Facts·Options·Impact를 기록한다. 다른 확정 G의 실행까지 막는 전역 장벽을 만들지 않는다.
- SATISFIED는 최신 R 전체 AC가 실제 구현·테스트로 충족되고 남은 제안 작업이 불필요함을 확인한 경우만 반환한다. G가 없거나 Merge됐다는 사실만으로 R 완료를 추정하지 않는다.
- 계획 PR에만 있는 사용자 편집까지 판단에 반영한다. 미확정·근거 부족이면 UNRESOLVED를 반환한다.

최종 JSON은 `result`(PLANNED/SATISFIED/UNRESOLVED)와 비어 있지 않은 `evidence`를 포함한다. 계획을 추가하지 않고 기존 확정 G를 재사용한다면 evidence에 해당 G와 남은 범위를 명시한다.
