# [R-050] Codex 직접 실행과 내장 sandbox 경계

## Context

현재 Merge된 G-088은 Codex 프로세스 전체를 바깥 `codex sandbox`로 감싼다. 진행 중인 G-097은 유효한 자동 검토 설정을 확인하려고 내부 호출을 `codex exec`에서 `codex app-server`로 변경했다. Codex의 도구 명령이 다시 내장 sandbox를 사용하면서, Codex 0.159.3에서는 바깥 sandbox가 가린 소켓 경로와 안쪽 sandbox 초기화가 충돌해 Reviewer가 실행되지 않는다.

사용자는 Manager가 `codex exec`를 직접 실행하고, 선택한 작업 경로와 Codex 내장 sandbox 정책으로 Agent의 쓰기 범위를 제한하는 방향을 선택했다. Codex 프로세스 전체의 쓰기를 별도 sandbox로 증명하려는 조건을 제거하되, workspace 보호와 자동 승인 검토는 유지한다. Reviewer의 읽기 전용 정책과 테스트 재실행 금지는 서로 다른 규칙으로 명시한다.

이 Requirement는 R-023의 Codex 실행 및 격리 확인 범위를 구체화하고 변경한다. 현재 Merge된 구현의 완료를 뜻하지 않으며, G-088·G-097의 과거 검증 및 실패 기록을 변경하거나 기존 계획을 자동 완료시키지 않는다.

## Requirement

### 직접 실행과 역할별 경계

Manager는 Codex를 직접 비대화형 실행한다. 별도의 바깥 sandbox로 Codex 프로세스 전체를 감싸거나 중첩 sandbox 호환 환경을 요구하지 않는다. Codex가 도구 실행을 위해 사용하는 내장 sandbox는 유지한다.

각 호출은 선택한 canonical workspace를 작업 경로로 지정하고 역할에 맞는 내장 쓰기 정책을 적용한다. 쓰기 역할은 workspace의 허용 파일을 변경할 수 있고, Reviewer 및 읽기 전용 역할은 workspace 파일을 변경할 수 없다. 작업 경로 지정만으로 쓰기가 제한됐다고 간주하지 않는다. source checkout, 다른 worktree와 보호 Git 경로를 Agent의 쓰기 허용 범위에 추가하지 않는다. 필요한 임시 쓰기는 실행에 소유권이 있는 최소 경로로 제한한다.

신규 호출, session 재개와 지원되는 session-unavailable fallback에도 같은 경계를 적용한다. 재개를 위해 sandbox를 해제하거나 쓰기 경계를 넓히지 않는다. 기존 역할별 허용 파일 검사, Manager의 Git·commit·push·PR 소유권 및 runtime handoff 경계를 유지한다.

### 자동 검토와 확인 범위

Codex의 자동 승인 검토를 유지하고, sandbox 밖 실행과 권한 확대 요청은 거부한다. 전체 승인 검토를 비활성화하거나 편의 실행 옵션이 명시한 정책을 덮어쓰게 하지 않는다. sandbox 안에서 이미 허용된 모든 동작에 자동 검토가 발생한다고 표시하지 않는다.

설치된 CLI의 지원 기능, 적용 설정과 병합 우선순위, 선택 workspace 및 역할별 내장 sandbox의 실제 허용·거부 동작을 확인하고 결과를 호출에 결합해 보존한다. 단순 인자 존재나 사후 diff만으로 확인했다고 기록하지 않는다. 직접 실행의 출력이 자동 검토 설정을 별도 필드로 반환하지 않는다는 이유만으로 다른 transport나 바깥 sandbox를 요구하지 않는다.

확인 대상은 Agent의 파일 변경과 도구 실행에 적용되는 내장 정책이다. Codex 프로세스 전체와 모든 자손의 mount 상태, 내부 파일 쓰기 전체 및 중첩 sandbox 실행 성공을 별도 증명 조건으로 요구하지 않는다. Codex의 정상적인 인증·session·로그 저장은 workspace 쓰기 허용 경로와 구분하며, 이를 위해 인증 자료를 복사하거나 선택 worktree 밖의 Agent 쓰기 권한을 추가하지 않는다. 기존 PRODUCT가 허용한 `refs/codex/turn-diffs/*` 임시 ref 예외는 유지하지만, 소유 branch·remote ref·tag 등 그 밖의 Git 쓰기 예외를 확대하지 않는다.

필요한 내장 정책을 지원하지 않거나 설정 충돌·sandbox 초기화 실패·자동 검토 실패로 적용할 수 없으면 기존 작업물, session과 재시도 이력을 보존하고 구체적인 비밀값 없는 원인으로 중단한다. 바깥 wrapper를 제거했다는 이유만으로 과거 호출의 격리 증거나 차단을 일괄 성공·해제로 바꾸지 않는다.

### Reviewer와 검증 책임

Reviewer의 `read-only`는 파일 쓰기 제한이며 명령 실행 전체의 금지가 아니다. 파일 검색, diff와 기존 검증 결과 읽기에 필요한 명령은 읽기 전용 경계 안에서 사용할 수 있다.

테스트·검증 스크립트를 재실행하지 않는 규칙은 Reviewer 지침에 별도로 유지한다. Reviewer는 Worker의 targeted test 결과와 Manager의 전체 verify 결과를 받아 코드·테스트·검증 스크립트의 diff로 판단한다. 파일 쓰기 없이 실행 가능한 테스트도 Reviewer에게 허용하지 않는다. 내장 sandbox만으로 테스트 명령을 식별해 모두 차단한다고 주장하지 않는다.

Worker는 관련 최소 targeted unit test, Manager는 전체 verify, Reviewer는 읽기 전용 검토를 담당하는 기존 책임 분리를 유지한다.

## Acceptance Criteria

- 쓰기 역할과 Reviewer의 실제 Codex 호출은 직접 `codex exec`를 사용하며, 바깥 `codex sandbox` wrapper와 `codex app-server`를 실행 경로로 요구하지 않는다. Codex 자체의 도구 sandbox는 유지된다.
- 신규·재개·session-unavailable fallback에서 선택 workspace와 역할별 정책이 유지된다. 쓰기 역할의 허용 파일 쓰기는 성공하고 source checkout·다른 worktree·보호 Git 경로 쓰기는 거부된다. Reviewer의 workspace 파일 쓰기는 거부된다.
- 기본의 넓은 임시 쓰기 범위와 상속 설정의 추가 writable 경로가 허용 범위를 넓히지 않는다. 보호 경로 검증은 source checkout의 로컬 상태를 변경하지 않는다.
- 자동 검토는 유지하면서 sandbox 밖 실행과 권한 확대를 거부한다. 정책을 덮어쓰는 설정 또는 자동 검토 비활성화는 성공한 호출로 기록되지 않는다.
- 내장 정책의 적용·거부 확인 결과가 CLI·환경·역할·workspace·session 및 호출에 결합된다. 출력에 유효 정책 전용 필드가 없다는 사실이나 Codex 내부의 정상 session·로그 저장만으로 중첩 실행을 요구하거나 호출을 거부하지 않는다.
- 중첩 sandbox와 소켓 마스크 호환성 사전 검사는 직접 호출의 시작 조건이 아니다. 직접 호출의 내장 sandbox 초기화가 실패하면 상태를 보존하고 중단하며, 더 넓은 권한으로 재실행하지 않는다.
- 기존 session의 지원되는 재개와 결과·사용량·timeout·중단 처리가 유지된다. 재개 불가 시 기존 지원 fallback을 사용하며 session·작업물을 삭제하거나 무조건 새 session으로 교체하지 않는다.
- Reviewer는 읽기 전용 검색·diff 명령을 사용할 수 있고 파일 변경은 거부된다. Reviewer 프롬프트에는 테스트 command 및 verify 재실행 금지가 유지되며, 테스트 통과 여부는 전달받은 결과로 판단한다.
- 기존 Codex 임시 ref 예외 외의 Git·게시 권한은 확대되지 않는다. 과거 증거·G-097 차단·retry 이력은 보존하며 현재 정책과 작업 소유권을 확인하지 않은 일괄 차단 해제는 하지 않는다.

## Product Impact

- Codex 자동 실행은 직접 실행과 내장 sandbox를 기준으로 하고 중첩 sandbox 호환성을 요구하지 않는다.
- Agent의 workspace 쓰기 경계와 자동 승인 검토를 유지하면서 Codex의 정상 runtime 저장을 구분한다.
- Reviewer의 파일 쓰기는 제한하고 테스트 재실행 금지는 별도의 역할 지침으로 유지한다.

## Non-Goals

- sandbox나 자동 검토를 해제하거나 host 소켓을 노출하지 않는다.
- 새로운 컨테이너·VM·외부 실행 infrastructure를 도입하지 않는다.
- Codex 프로세스 전체의 파일시스템 쓰기를 별도 OS 경계로 제한·증명하지 않는다.
- 테스트 명령을 식별하는 새로운 실행 차단기를 도입하지 않는다.
- Claude의 실행 방식·지원 범위, Worker·Manager의 검증 책임 및 자동 Merge 정책은 변경하지 않는다.
- 기존 ExecPlan·SQLite 실행 상태·session·재시도 이력을 문서 작성 과정에서 수정하지 않는다.

## Open Questions

- 없음
