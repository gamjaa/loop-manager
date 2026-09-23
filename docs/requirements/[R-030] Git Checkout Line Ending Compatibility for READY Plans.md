# [R-030] Git Checkout Line Ending Compatibility for READY Plans

## Context

Windows의 `core.autocrlf=true` 환경에서는 Git이 보관한 READY ExecPlan 본문과 checkout된 파일의 줄바꿈이 달라질 수 있다. 현재 READY 계획 검증은 두 바이트열을 그대로 비교하므로, Git 기준으로 변경되지 않은 파일도 `READY ExecPlan 파일이 평가 기준 revision과 다릅니다` 오류로 처리한다. 유지보수 평가 테스트에서 이 오류가 재현됐고, 줄바꿈 변환을 끄면 같은 테스트가 통과했다. 실패 원인은 Git checkout의 줄바꿈 변환을 고려하지 않는 계획 검증에 있다.

## Requirement

Loop Manager는 Git 줄바꿈 설정에 따라 checkout된 READY ExecPlan이 Git 기준으로 변경되지 않았다면 계획 검증을 통과시켜야 한다. 실제 본문 변경, 저장된 계획 revision과 Git 본문의 불일치 또는 카탈로그 불일치는 기존처럼 거부하고 원인을 알려야 한다. 사용자의 Git 설정을 변경하거나 계획 본문의 변경을 무조건 허용해서는 안 된다.

## Acceptance Criteria

- `core.autocrlf=true`인 Windows checkout에서 Git 기준으로 변경되지 않은 READY ExecPlan이 계획 검증을 통과하고 다음 작업 단계로 진행된다.
- `core.autocrlf=false`인 checkout에서도 같은 READY 계획 검증이 통과한다.
- LF 또는 CRLF checkout 변환만으로 READY 계획의 변경이나 revision 불일치를 보고하지 않는다.
- 실제 READY 계획 수정, 저장된 plan revision과 Git 본문의 불일치, 카탈로그 불일치는 기존 보호 규칙에 따라 계속 거부한다.
- 서로 다른 Git 줄바꿈 설정을 사용하는 checkout에서 정상 파일과 실제 변경된 파일을 구분하는 회귀 검증이 통과한다.

## Product Impact

- Git 줄바꿈 변환을 사용하는 프로젝트에서도 변경되지 않은 READY 계획이 정상 처리된다.

## Non-Goals

- 사용자의 전역 또는 프로젝트 Git 줄바꿈 설정을 변경하지 않는다.
- 실제 변경된 READY ExecPlan을 자동 승인하지 않는다.

## Open Questions

- 없음
