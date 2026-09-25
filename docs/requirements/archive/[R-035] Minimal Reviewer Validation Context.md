# [R-035] Minimal Reviewer Validation Context

## Context

Reviewer는 Worker의 변경을 읽기 전용으로 검토하고, 실행 가능한 관련 targeted test와 Loop Manager의 전체 verify가 통과한 뒤에만 호출된다. 관련 targeted test가 없다면 Worker는 그 사유를 남긴다. 현재 Reviewer prompt에는 Worker의 테스트 결과와 성공한 verify 출력의 끝부분이 최대 6,000자까지 포함된다. 통과 사실은 Reviewer 진입 조건으로 이미 확인되므로 성공 결과를 다시 입력할 이유가 없다.

## Requirement

Loop Manager는 Reviewer prompt에서 정상 통과한 targeted test와 전체 verify의 결과·성공 로그를 제외해야 한다. Reviewer 호출 전에는 현재 검토 대상 revision에 대한 필수 검증이 완료됐는지 프로그램이 확인해야 하며, 그 통과 사실을 Reviewer에게 반복 설명하지 않는다. 관련 targeted test를 실행할 수 없었던 사유처럼 검토 판단에 필요한 예외적 사실이 있으면 그 사실만 간결하게 전달한다.

Reviewer는 코드·테스트·verify 스크립트의 변경과 근거 문서를 읽고 테스트 누락, 검증 범위 약화와 요구사항 미충족을 판단해야 한다. 최초 전체 verify는 최신 원격 기준의 rebase와 필요한 충돌 해결을 마친 변경에 대해 실행한다. 필수 검증이 실패했거나 현재 revision에 대한 성공 기록이 없으면 Reviewer를 시작하지 않는다. verify 이후 원격 기준이나 작업 결과가 다시 바뀌면 새 revision의 필수 검증을 다시 통과한 뒤 Reviewer를 호출한다. 기존 검증 결과와 출력의 저장은 유지하되 Reviewer prompt의 기본 입력으로 사용하지 않는다.

## Acceptance Criteria

- 정상 통과한 targeted test와 전체 verify의 상태·명령·원문·끝부분·개별 통과 내역은 Reviewer prompt에 포함하지 않는다.
- Reviewer는 코드·테스트·verify 스크립트 diff와 근거 문서를 확인해 테스트 누락과 검증 약화를 판단한다. 관련 targeted test 미실행 사유처럼 이 판단에 필요한 예외적 사실만 제한적으로 전달한다.
- 필수 검증 실패, 성공 기록 누락 또는 검증 대상과 Reviewer가 검토할 revision의 불일치가 있으면 Reviewer를 시작하지 않는다.
- 최초 전체 verify는 최신 원격 기준 반영과 충돌 해결 뒤에 실행한다. verify 이후 새 변경으로 revision이 달라지면 필수 검증을 다시 통과시킨 뒤 Reviewer를 호출한다.
- Reviewer 입력을 줄여도 검증 결과·출력의 기존 저장, 실패 시 Worker 피드백과 Reviewer의 읽기 전용·테스트 재실행 금지 규칙은 유지된다.

## Product Impact

- Reviewer는 실제 변경과 필요한 예외적 검증 정보만 확인하고 이미 통과한 검증 결과를 반복 입력으로 받지 않는다.

## Non-Goals

- Reviewer가 전체 verify나 테스트를 다시 실행하지 않는다.
- 전체 verify의 실행 범위와 통과 기준을 완화하지 않는다.
- 프로젝트별 성공 로그 파서를 추가하지 않는다.

## Open Questions

- 없음
