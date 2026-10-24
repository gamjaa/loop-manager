# [R-059] 안전한 Python bytecode 캐시 사용

## Summary

bootstrap·runtime·관리 worktree의 Python bytecode 캐시를 기본적으로 허용해 안전하게 생성·재사용하고 일괄 비활성화의 잔여 경로를 제거한다. 캐시만으로 차단하지 않되 현재 소스·interpreter·revision·소유권과 tracked 경로 보존은 확인하며, 손상·권한 문제는 안전한 재생성 또는 무캐시 실행으로 처리한다.

## Context

현재 관리 worktree의 ignored·index-free 파일은 Agent 범위 위반으로 취급하지 않으며 Planner·Worker 환경에 `PYTHONDONTWRITEBYTECODE`를 강제하지 않는다. 그러나 bootstrap이 실행하는 runtime 자식은 `-B`로 bytecode 생성을 막는다. runtime의 검증·정리와 ignored 캐시 보존 조건까지 고려해 Python 캐시를 안전하게 사용할 수 있는지 확인해야 한다.

기존에 허용된 캐시 사용을 다시 요구하기보다, 남아 있는 일괄 비활성화를 해소하고 캐시가 실행·복구·정리의 오탐을 만들지 않는지 검증한다.

## Requirement

### 캐시 사용과 안전 경계

- Loop Manager의 bootstrap·runtime, 관리 worktree에서 실행하는 Python과 테스트·verify의 bytecode 정책을 조사한다. 캐시 금지로 실제 문제를 예방하는 경로와 과거 오탐을 피하기 위한 일괄 금지를 구분한다.
- Python의 기본 bytecode 캐시를 사용할 수 있게 하고 runtime의 일괄 `-B` 같은 예방적 금지를 안전한 캐시 처리로 대체한다. 사용자가 명시한 캐시 비활성화나 실행 환경에서 캐시를 쓰지 못하는 조건은 존중한다.
- 캐시 유무·생성·갱신·삭제만으로 planning 오류, Agent 범위 위반, 계획·프로젝트 차단 또는 runtime 소유권 오류를 만들지 않는다. 기존 ignored·index-free 판정이 확인된 파생 파일만 해당하며 tracked·staged·non-ignored 변경은 계속 보호한다.
- 캐시가 runtime의 exact SHA·원본 파일·실행 revision·소유권을 대신하지 않는다. interpreter·소스·revision 변화 뒤에도 현재 소스에 맞는 코드가 실행돼야 한다. 오래된 캐시를 최신 원본 또는 성공한 검증의 근거로 사용하지 않는다.
- runtime 캐시가 다시 실행할 때 재사용될 수 있어야 하고, 캐시가 남았다는 이유만으로 정상 runtime 사용·검증된 정리가 매번 실패하지 않아야 한다. Agent의 sandbox·쓰기 범위를 넓히거나 사용자 cache를 관리 소유로 간주하지 않는다.
- 캐시 경로·권한 문제로 사용할 수 없으면 캐시 없이 실행할 수 있게 한다. 캐시가 손상되거나 오래됐을 때는 Python의 정상 재검증·재생성 또는 안전한 무캐시 실행으로 복구하며, 사용자 파일을 삭제하거나 ignore 규칙을 바꿔 실제 위반을 숨기지 않는다.
- checkout·rebase·정리에서 캐시와 원격 tracked 경로가 충돌하는 경우 기존 파일 보존·소유권 검사를 유지한다. 검증된 Manager 소유 파생 캐시만 필요한 범위에서 관리하고, 무관한 ignored 파일·사용자 데이터·소유권 불명확 경로를 함께 삭제하지 않는다.

캐시 문제가 발견되면 영향받은 경로에 필요한 처리만 적용한다. 안전한 일반 캐시까지 일괄 금지하거나 별도 캐시 서비스·범용 저장 계층을 도입하지 않는다. 실제 AI Agent 호출 없이 검증하고 전체 verify는 Manager가 실행한다.

## Acceptance Criteria

- bytecode 비활성화 경로와 원래 목적을 확인하고, 같은 revision의 반복 실행에서 안전한 캐시 생성·재사용이 가능하다. 명시적 무캐시 환경에서도 현재 원본을 정상 실행한다.
- 캐시 생성·갱신·잔존만으로 관리 worktree 또는 runtime에 새로운 차단·소유권 실패·불필요한 cleanup pending이 발생하지 않는다.
- 소스·원격 revision·interpreter가 바뀌면 현재 코드가 실행되고 이전 캐시가 다른 revision의 원본·검증 결과로 채택되지 않는다.
- 캐시 손상·권한 부족·경로 사용 불가에서는 안전하게 재생성하거나 무캐시로 동작한다. 실제 원본·Git·소유권 문제를 캐시 문제로 간주해 무시하지 않는다.
- 캐시가 있는 runtime의 중단·재시작·검증된 정리가 동작하며, 다른 ignored 파일과 사용자 데이터는 그대로 보호된다.
- 캐시 허용 뒤에도 실제 후보·검증 입력과 Manager Git 쓰기의 파일 보존·동기화·게시 조건은 유지된다. 접근 권한의 sandbox 강제와 Git 범위 검사 제거·기존 오탐 자동 복구는 R-055를 따르며, 캐시 때문에 과거 snapshot 비교를 되살리거나 ignore 규칙·sandbox를 완화하지 않는다.
- 캐시와 tracked 경로의 충돌 또는 소유권 불명확은 파일을 보존해 실제 이유를 기록하고, 안전한 다른 원격 기준 작업은 기존 정책에 따라 계속할 수 있다.

## Product Impact

- Python 캐시를 기본적으로 사용할 수 있고 캐시만으로 자동 작업이 차단되지 않는다.
- 캐시 재사용과 현재 원격 실행 revision의 정확성을 함께 보장한다.

## Non-Goals

- 사용자 source checkout의 캐시를 runtime으로 복사하거나 임의 ignored 파일을 관리 캐시로 취급하지 않는다.
- 새로운 캐시 서비스·dependency 또는 Agent 쓰기 권한 확대를 도입하지 않는다.
- 실제 Git 상태·소유권·원본 검사를 생략하거나 사용자 파일을 일괄 삭제하지 않는다.
- 캐시 cold/warm 실행 시간·관리 비용의 비교 측정이나 실측 결과 보존을 완료 조건으로 요구하지 않는다. 특정 실행 시간·성능 향상률 또는 Python 외 모든 도구 cache의 정책 변경도 요구하지 않는다.

## Open Questions

- 없음
