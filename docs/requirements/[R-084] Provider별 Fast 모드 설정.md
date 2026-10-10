# [R-084] Provider별 Fast 모드 설정

## Context

현재 `config/config.ini`는 provider별 Model alias와 역할·Worker level의 Effort를 지정하지만, CLI가 제공하는 Fast 모드를 선택할 수 없다. `[codex.models]`와 `[claude.models]`의 `fast`는 모델 alias이며 Fast 모드 on/off와 별개다.

2026-10-10 확인한 [OpenAI Fast 모드 문서](https://learn.chatgpt.com/docs/agent-configuration/speed#fast-mode)는 Fast service tier와 `service_tier`, `features.fast_mode` 설정을 안내한다. [Claude Code Fast 모드 문서](https://code.claude.com/docs/en/fast-mode)는 `fastMode` 설정과 비대화형 호출의 `--settings` 전달을 안내한다. 지원 여부와 실제 적용·대체 실행은 provider CLI가 처리하며 Manager가 별도의 모델별 지원 목록이나 사전 판정을 관리하지 않는다.

Fast 모드는 Effort를 낮추거나 더 작은 모델을 선택하는 기능이 아니다. Manager 설정은 명시적 on/off 또는 CLI 기본 설정 상속을 선택한다. Fast 사용에는 추가 비용 또는 구독 사용량 증가가 발생할 수 있으며, 상속한 CLI 기본 설정이 on이면 Manager 설정이 공란이어도 Fast가 사용될 수 있다. CLI가 일반 속도로 대체하면 그 실행을 그대로 사용하되, 모든 미지원 상황에서 정상 실행으로 대체된다고 보장하지 않는다.

## Requirement

### 설정과 적용 범위

- `config/config.ini`에 provider별 `fast_mode` 설정을 제공한다. 명시적 boolean 값 또는 CLI 기본 설정 상속을 선택하며, 해당 provider를 사용하는 모든 등록 프로젝트와 Agent 역할·Worker level에 공통 적용한다.

  ```ini
  ; true: Fast 요청, false: Fast 끄기, 공란: CLI 기본 설정 상속
  [codex]
  fast_mode =

  [claude]
  fast_mode =
  ```

- section·항목 생략 또는 공란은 CLI 기본 설정 상속이다. 공백만 있는 값도 공란으로 취급하며 `false`로 바꾸지 않는다. 명시적 값은 `true`/`false`와 표준 INI boolean 표현인 `on`/`off`, `yes`/`no`, `1`/`0`을 허용하며 대소문자를 구분하지 않는다. 그 밖의 값은 설정 오류다. 선택 provider의 오류는 Agent 실행 전에 항목 위치와 함께 보고하고, 미선택 provider의 설정 오류가 독립 프로젝트 실행을 막지 않는다.
- Fast 설정은 기존 `Model alias,Effort` 형식, 모델 alias `fast`, Worker level 선택과 승격 규칙을 변경하지 않는다. Fast를 적용하려고 Model·Effort·provider를 자동 교체하지 않는다.
- 실행 시작 시 읽은 Manager의 Fast 선택을 해당 실행의 신규 호출·session 재개·재시도·새 session fallback에 동일하게 적용한다. Manager 설정 파일 변경은 다음 실행부터 반영하며 진행 중인 CLI 호출의 속도를 바꾸지 않는다. 상속을 선택한 호출의 기본 설정 해석은 CLI에 맡긴다. Fast 선택만 달라졌다는 이유로 기존 session·작업물·검증 이력을 폐기하거나 Worker level을 초기화하지 않는다.

### CLI 전달과 결과 처리

| 설정 | 호출 동작 |
|---|---|
| 공란·공백만 있는 값·section 또는 항목 생략 | Fast 관련 CLI override를 전달하지 않는다. 사용자·프로젝트·재개 session 등 CLI가 해석하는 기존 설정을 그대로 따른다. |
| `false` | 해당 호출의 Fast 모드를 명시적으로 끈다. CLI 기본 설정이나 저장된 session에 Fast 선택이 남아 있어도 off가 우선하도록 전달한다. |
| `true` | 설정된 Model·Effort와 함께 provider의 Fast 모드를 요청한다. 모델별 지원 여부를 사전 판정해 요청을 생략하거나 끄지 않는다. |

- Codex와 Claude의 공식 비대화형 설정 전달 경로로 on/off를 전달한다. 기존 Model·Effort 인자는 그대로 전달하고 Fast 적용을 위해 Manager가 모델이나 profile을 바꾸지 않는다. 재개 호출에도 동일한 기준을 적용한다.

  Codex의 명시적 on은 사용자가 제공한 다음 명령 구문을 사용한다.

  ```sh
  codex exec --json -c 'service_tier="fast"' -c 'features.fast_mode=true' "작업 내용"
  ```

  실제 호출에는 기존 인자를 보존하며 `service_tier="fast"`와 `features.fast_mode=true` override를 추가한다. 출력 형식·prompt·권한 인자는 기존 호출 경로를 따른다. 명시적 off는 `features.fast_mode=false`와 함께 기존 Fast service tier 선택도 해제해 일반 속도를 선택하도록 전달한다. 상속은 두 Fast 관련 override 모두 생략한다. Claude는 기존 `--settings` JSON에 명시한 경우에만 `fastMode: true` 또는 `fastMode: false`를 합치며, 상속이면 `fastMode` key만 생략하고 기존 권한·sandbox 설정은 유지한다.

- 이번 기능을 위한 모델별 지원 목록·catalog 판정·계정 권한 조회·별도 사전 호출을 추가하지 않는다. 기존 Model·Effort 검증은 유지한다.
- 호출에 필요한 설정만 전달하고 사용자의 Codex·Claude 설정 파일이나 실제 `config/config.ini`를 자동 수정하지 않는다. 기존 추가 지침·sandbox·자동 승인 검토·읽기 전용 역할의 제한을 유지한다. 조직의 관리 정책이나 계정 제한을 우회하지 않는다.
- CLI가 자체적으로 일반 속도로 대체하거나 cooldown 후 Fast를 다시 사용하는 경우에는 그 동작을 따른다. Fast 설정 미지원·계정 제한 등을 오류로 반환하면 기존 실패 분류·재시도·보고 규칙을 따른다. Manager가 별도의 일반 속도 재호출, Fast 전용 실패 분류, 무제한 재시도 또는 다른 모델·provider 전환을 추가하지 않는다. 기존 작업물·session·실패 기록과 독립 프로젝트 실행을 보존한다.
- 기존 호출 로그에서 명시적 on/off와 CLI 기본 설정 상속을 구분할 수 있어야 한다. CLI가 반환한 진단을 보존하며 일반 속도 대체를 성공적인 Fast 적용으로 표시하지 않는다. 실제 적용 여부나 처리 속도를 확인하기 위한 추가 호출은 하지 않는다.
- Fast 관련 인자를 올바르게 전달했지만 CLI 버그로 실제 Fast 적용이나 해제가 되지 않는 문제는 이번 변경에서 별도 대응하지 않는다. 우회 옵션·대체 호출·CLI patch·강제 업데이트·적용 확인 gate를 추가하지 않고 CLI 결과를 기존 규칙대로 처리한다.
- 구현 시 `config/config.ini.example`과 운영 설정 안내에 두 항목·상속 기본값·적용 범위·명시적 on/off 명령 구문·CLI에 맡기는 지원 및 대체 실행 처리·추가 비용 및 사용량 영향을 설명한다. 예시는 두 provider 모두 공란을 사용하고, 실제 비밀 설정 파일을 commit하지 않는다.

## Acceptance Criteria

- 두 provider의 `fast_mode`에서 on·off·CLI 기본 설정 상속을 독립적으로 선택할 수 있다. 공란·공백·section 또는 항목 생략은 상속이고, 유효하지 않은 명시적 값은 오류다. 허용한 boolean 표현과 미선택 provider 오류의 격리를 확인한다.
- 모델별 Fast 지원 판정 없이 선택한 설정과 기존 Model·Effort 인자를 전달한다. Codex on에는 예시의 두 override가 있고, off에는 Fast feature와 Fast service tier를 끄는 override가 있으며, 상속에는 Fast 관련 override가 없다. Claude의 기존 `--settings`에는 명시한 boolean만 합치고 상속이면 `fastMode` key가 없다.
- CLI 기본 설정이 on인 경우 명시적 `false`가 off를 요청하는 인자를 전달하고, CLI 기본 설정이 off인 경우 명시적 `true`가 on을 요청하는 인자를 전달한다. 공란·생략은 어느 기본값도 덮어쓰지 않는다. 이 구분은 모의 호출의 전달 인자로 검증하며 Fast 설정 때문에 모델·profile을 교체하거나 지원 판정을 위한 추가 CLI·외부 호출을 하지 않는다.
- 일반 Planner·빠른 점검 Planner·유지보수 Planner·Reviewer·Worker의 공통 호출 경로와 신규·재개·재시도·session fallback에서 동일한 설정을 적용한다. 실행 중 설정 변경은 다음 실행에 반영되고 Fast 변경만으로 작업 이력이나 session을 폐기하지 않는다.
- CLI가 Fast 요청으로 성공하는 경우, 자체적으로 일반 속도 대체 후 성공하는 경우와 오류를 반환하는 경우를 모의 호출로 확인한다. 일반 속도 대체는 CLI의 성공 결과를 사용하고, 오류는 기존 실패 처리로 전달한다. Fast 성공을 합성하거나 Manager의 일반 속도 재호출·정책 우회·설정 파일 수정을 하지 않는다.
- Fast 설정을 추가해도 기존 instruction·sandbox·자동 승인 검토·읽기 전용 제한과 Model·Effort 검증을 보존한다.
- 설정 예시와 운영 안내가 전달 계약과 일치한다. 문서·모의 호출의 인자 전달 검증과 실계정 Fast 적용 검증을 구분한다. CLI 버그로 인한 실제 미적용은 이 기능의 완료를 막는 별도 gate로 삼거나 우회하지 않으며, 실계정 검증을 수행하지 않았다면 그 범위를 명시한다.

## Product Impact

- 사용자는 provider별로 Fast on/off를 명시하거나 CLI 기본 설정을 상속한다. 공란·생략의 기본 동작은 상속이다.
- Fast 설정과 모델·Effort 선택은 독립적이며 지원 여부와 대체 실행 처리는 CLI가 소유한다.
- 호출에 전달한 선택과 CLI 진단을 확인할 수 있으며 실제 Fast 적용을 증거 없이 확정하지 않는다.

## Non-Goals

- 프로젝트별·역할별·Worker level별 Fast override, 실행 중 전환 명령 또는 별도 설정 편집 CLI.
- 모델별 Fast 지원 목록·사전 판정·계정 가용성 조회와 Fast 적용 여부 확인용 추가 호출.
- 새 모델·Effort 기본값, 자동 모델 교체·튜닝, Ultrafast 등 다른 service tier 선택.
- 계정 과금·usage credits 활성화, 조직 정책 변경, 사용자 CLI 설정 파일 수정.
- 별도 비용 원장·속도 benchmark·Fast 전용 재시도나 대체 실행 체계.
- provider CLI 버그 수정·우회 또는 실제 Fast 적용·해제 확인을 위한 실행 gate.

## Open Questions

- 없음
