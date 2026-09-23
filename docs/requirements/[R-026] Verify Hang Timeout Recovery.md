# [R-026] Verify Hang Timeout Recovery

## Context

Loop Manager는 전체 verify 실패를 Worker에게 돌려보내고 같은 실패 지문이 수정 후 세 번 재발하면 계획을 차단한다. 그러나 `scripts/verify` 실행 자체에는 시간 제한이 없다. 테스트 코드가 CPU를 계속 점유하며 종료되지 않으면 실패 결과를 만들지 못해 Worker 수정, Review 또는 차단 단계로 진행할 수 없다. 기존의 실행·세션·rebase 복구 계획과 분리해 verify 프로세스의 종료 경계만 보강해야 한다.

## Requirement

Loop Manager는 전체 verify에 종료 시간 상한을 적용한다. 공통 `verify_timeout_seconds`의 기본값은 1800초이고 사용자가 양의 정수로 조정할 수 있어야 한다. 제한을 넘으면 verify와 그 자식 프로세스의 실행을 끝내 CPU 점유가 남지 않았는지 확인하고, 시간 초과를 실패 결과로 보존한다. Worker에게는 현재 profile·session 선택과 검증 피드백 계약에 따라 제한된 진단을 전달해 테스트 코드 또는 verify 스크립트를 수정하도록 한다.

시간 초과는 기존 verify 실패 지문과 반복 차단 정책을 따른다. 프로세스 종료 여부를 확인할 수 없으면 다음 Worker나 verify를 시작하지 않고 프로젝트 안전 차단과 사용자 알림으로 전환한다. 시간 초과를 성공으로 처리하거나 verify 단계를 건너뛰지 않는다. G-041의 Worker session binding과 G-045의 rebase correction 계약은 이 변경의 수정 대상이 아니다.

## Acceptance Criteria

- 정상 verify는 제한 시간 안에 기존과 같이 성공 또는 실패로 처리된다.
- 종료되지 않는 verify는 기본 1800초 또는 설정한 상한에서 중단되고, 그 자식 테스트 프로세스까지 종료돼 CPU 점유가 남지 않는다.
- verify 시간 초과의 진단과 제한된 출력은 기존 Worker profile·session 및 피드백 계약을 따라 수정 경로에 전달된다.
- Worker 수정 후 동일한 시간 초과가 세 번 재발하면 기존 verify 지문 정책에 따라 해당 계획만 `PLAN_BLOCKED`가 된다. 성공하면 verify 실패 이력이 초기화된다.
- verify 프로세스 종료를 확인할 수 없으면 다음 Agent·verify·저장소 변경을 시작하지 않고 안전 차단 원인과 잔존 프로세스 정보를 사용자에게 알린다.
- `verify_timeout_seconds`가 생략되면 1800초이고 0, 음수, 정수가 아닌 값은 작업 시작 전에 설정 오류로 거부한다.
- 시간 초과 처리 중 사용자 변경과 작업 공간을 임의로 삭제하거나 로그에 비밀값을 노출하지 않는다.
- G-041의 Worker session 전이와 G-045의 rebase 충돌 교정은 이 Requirement의 구현으로 변경되지 않는다.

## Product Impact

- 종료되지 않는 테스트가 검증 단계를 무기한 점유하지 않으며, 수정 가능한 시간 초과는 Worker에게 반환된다.

## Non-Goals

- CPU 사용률 표본만으로 정상적으로 오래 걸리는 검증을 오작동으로 판정하지 않는다.
- 테스트 실패를 숨기거나 verify를 생략해 PR을 게시하지 않는다.
- Agent session 선택, rebase 충돌 복구 또는 전체 실행 오류 알림 정책을 변경하지 않는다.
- 별도 감시 서비스를 도입하지 않는다.

## Open Questions

- 없음
