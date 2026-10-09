# [R-003] Targeted Worker Unit Tests

## Context

현재 Worker는 관련 테스트를 작성하고 전체 검증 스크립트를 직접 실행하지 않지만, 자신이 수정한 범위의 unit test를 실행해야 한다는 책임은 명확하지 않다. 이 때문에 단순한 구현 오류도 전체 verify 단계에서야 발견될 수 있다. 반대로 Worker가 전체 test suite까지 실행하면 Loop Manager가 별도로 수행하는 전체 검증과 중복돼 Agent 시간과 실행 비용이 늘어난다.

Worker에게는 수정 범위의 빠른 피드백만 맡기고, 프로젝트 전체의 회귀 검증은 일관되게 verify 스크립트에 맡겨야 한다.

## Requirement

Worker는 구현과 관련 unit test를 작성하거나 갱신한 뒤, 자신이 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행해야 한다. 실패하면 같은 Worker 세션에서 원인을 수정하고 해당 테스트를 다시 실행해야 한다.

전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check와 `scripts/verify` 실행은 Worker가 수행하지 않는다. Loop Manager 코드가 Worker 종료 후 전체 검증 스크립트를 AI Agent 밖에서 실행하며, 실패한 경우에만 결과를 같은 Worker 세션에 전달한다.

Reviewer는 Worker의 targeted unit test와 Loop Manager의 전체 verify가 통과한 뒤 그 결과를 넘겨받아 읽기 전용 Review만 수행한다. Reviewer는 테스트 command나 `scripts/verify`를 다시 실행하지 않고 코드, 테스트, verify 스크립트의 diff와 전달된 결과를 기준으로 테스트 누락과 검증 약화를 판단한다.

문서·설정만 변경했거나 프로젝트에 실행 가능한 관련 unit test가 없는 경우 Worker는 테스트를 임의로 확대하지 않고 실행하지 못한 사유를 남긴다. 이 경우에도 Loop Manager의 전체 verify는 생략하지 않는다.

정적 검증 책임과 허용·금지 명령은 R-064의 공통·역할별 instruction으로 전달한다. Worker는 `docs/AGENTS.common.md`+`docs/AGENTS.worker.md`, Reviewer는 common+`docs/AGENTS.reviewer.md`를 사용한다. 작업 prompt에는 테스트 대상·결과·실패 피드백 등의 작업별 사실을 전달하고 정적 권한 규칙을 중복하지 않는다.

## Acceptance Criteria

- 실제 Worker 호출의 공통+Worker instruction은 수정한 코드와 새로 작성하거나 변경한 테스트에 직접 관련된 최소 범위의 unit test를 실행하도록 명시한다.
- Worker가 실행하는 테스트 command는 개별 test case, test class, test module 또는 이에 준하는 좁은 범위로 제한된다.
- targeted unit test가 실패하면 Worker는 알려진 실패를 성공으로 보고하지 않고 같은 세션에서 수정한 뒤 관련 unit test를 재실행한다.
- 문서·설정 전용 변경 또는 실행 가능한 관련 unit test가 없는 경우 Worker는 그 사유를 결과에 남기고 전체 test suite로 범위를 확대하지 않는다.
- Worker는 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check 또는 `scripts/verify`를 실행하지 않는다.
- Loop Manager는 Worker가 targeted unit test를 실행했는지와 관계없이 기존 VERIFYING 단계에서 전체 verify 스크립트를 실행한다.
- verify 실패만 기존 재시도 예산과 상태 전이를 사용해 같은 Worker 세션으로 전달되며, targeted unit test의 내부 수정·재실행은 별도 SQLite 재시도 횟수를 소비하지 않는다.
- Reviewer는 Worker의 targeted unit test와 Loop Manager의 전체 verify 성공 후에만 실행되며 테스트 command와 `scripts/verify`를 다시 실행하지 않는다.
- Reviewer는 코드, 테스트, verify 스크립트의 diff와 전달된 테스트 결과만으로 테스트 누락, 회귀 위험과 검증 약화를 검토한다.
- Worker와 Reviewer의 실제 instruction에 전달된 허용·금지 명령 경계와 사용자 prompt의 정적 규칙 중복 부재, Worker 반환 후 전체 verify 실행과 verify 성공 후 Reviewer 호출을 자동 테스트로 검증한다.

## Product Impact

- Worker는 자신의 변경에 직접 관련된 unit test로 빠른 피드백을 얻는다.
- 프로젝트 전체 테스트와 검증은 verify 스크립트의 단일 책임으로 유지된다.
- 중복된 전체 검증을 줄이면서 PR 생성 전 전체 회귀 검증은 그대로 보장된다.

## Non-Goals

- targeted unit test로 전체 verify를 대체하거나 생략하지 않는다.
- Worker가 integration test, end-to-end test 또는 프로젝트 전체 검증을 실행하지 않는다.
- Reviewer가 테스트 또는 전체 검증을 재실행하지 않는다.
- Loop Manager 코드가 변경 파일을 분석해 Worker 대신 targeted unit test command를 선택하지 않는다.

## Open Questions

- 없음
