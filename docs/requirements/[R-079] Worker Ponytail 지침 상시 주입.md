# [R-079] Worker Ponytail 지침 상시 주입

## Context

Worker가 불필요한 코드·추상화·의존성을 추가하지 않고 기존 구현을 재사용하도록 Ponytail의 코딩 지침을 제공한다. Codex 자동 실행은 현재 파일 쓰기 경계를 유지하기 위해 Plugin·App을 비활성화한다. [Codex 0.161.0의 command Hook](https://github.com/openai/codex/blob/rust-v0.161.0/codex-rs/hooks/src/engine/command_runner.rs)은 별도 셸 프로세스로 직접 실행되며, 외부 MCP 도구에도 Worker의 내장 파일 sandbox가 동일하게 적용된다고 보장할 수 없다.

따라서 기존 실행 경계를 유지하고 Ponytail 축약 지침을 Worker에 항상 제공한다. Plugin 설치 여부에 따른 생략은 하지 않으며 설치 목록 조회·캐시·프로젝트별 설치 판정 기능을 제거한다. 이 결정으로 설치 여부와 실제 적용 여부의 불일치 및 목록 조회의 원격 통신 timeout을 없앤다. 사용자 Plugin 설정은 수정하지 않는다.

[Ponytail 저장소](https://github.com/DietrichGebert/ponytail)의 [AGENTS.md](https://github.com/DietrichGebert/ponytail/blob/main/AGENTS.md)는 축약 지침이며 전체 Skill·Hook과 동일한 구성이나 효과를 보장하지 않는다.

## Requirement

- Ponytail 플러그인 저장소의 루트 `AGENTS.md` 원문을 `docs/AGENTS.ponytail.md`로 저장한다. 출처·기준 revision·라이선스를 확인할 수 있어야 한다. 실행 시에는 선택 runtime에 저장된 원문을 사용한다.
- Worker 지침은 기존 공통·Worker 지침에 Ponytail 원문을 정확히 한 번 합친다. provider·설치 여부·활성 상태와 관계없이 기존 instruction 전달 경로를 사용하며 사용자 prompt에 반복하지 않는다. Planner·Reviewer 등 다른 역할에는 추가하지 않는다.
- Ponytail 적용을 위해 CLI Plugin 목록을 조회하거나 설치 여부를 캐시·판정하지 않는다. Skill·Hook·설정 디렉터리 탐색과 실행 중 원격 원문 다운로드도 하지 않는다.
- Codex 호출의 기존 Plugin·App 비활성화와 내장 쓰기 제한·자동 승인 검토를 유지한다. 사용자 Plugin 설정이나 설치 상태는 변경하지 않는다. 다른 provider의 기존 실행 경계도 유지한다.
- 원본과 전달 내용은 [R-064](%5BR-064%5D%20모델%20호출을%20통한%20공통%20Agent%20지침%20전달.md)의 버전 보존·호출 기록·결과 재사용 규칙을 따른다. 같은 작업의 재시도·재개·provider 변경은 고정된 원문을 사용한다. version 2 묶음의 Worker 호출은 Ponytail을 항상 포함하되 Ponytail이 없는 과거 version 1 묶음·단일 Markdown snapshot에는 새 원문을 소급 추가하지 않는다. 원본·snapshot 오류의 기존 호출 거부 기준을 유지한다.
- Ponytail은 코딩 스타일 지침이다. 명시된 요구사항·필수 기능·보안·입력 검증·데이터 무결성·필수 테스트 및 Manager의 역할·권한·검증·게시 규칙을 생략하거나 변경하는 근거로 사용하지 않는다.
- README 하단에 Ponytail 사용 안내와 `docs/AGENTS.ponytail.md` 상대 링크를 제공하고 실제 적용 정책을 설명한다.

## Acceptance Criteria

- 저장된 Ponytail 원문·라이선스가 upstream과 일치하고 출처·기준 revision을 확인할 수 있다.
- Codex·Claude Worker는 고정된 Ponytail 원문을 정확히 한 번 전달받고, 기존 공통·역할·기본·추가 지침과 프로젝트 규칙을 보존한다. Plugin 설치·활성 상태에 따른 생략은 없다.
- Worker 호출·재시도·재개·동시 실행 및 Worker가 없는 실행에서 이번 기능에 의한 Plugin 목록 조회가 없다. 설치 목록 조회 실패·timeout·미발견 진단도 발생하지 않는다.
- 다른 역할과 작업별 사용자 prompt에는 Ponytail 원문을 추가하지 않는다.
- 같은 작업의 원본 버전은 provider 변경에도 유지되고 실제 전달 내용·해시는 호출 기록과 일치한다. 다른 지침으로 얻은 성공 결과를 동일 지침의 결과로 재사용하지 않는다. 과거 snapshot의 내용 보존과 손상 원본·snapshot에 대한 호출 거부를 유지한다.
- Codex 직접 실행·재개·fallback의 기존 Plugin·App 비활성화와 파일 쓰기 경계·자동 승인 검토를 유지하고 사용자 설정을 변경하지 않는다.
- README의 사용 안내와 상대 링크가 유효하고 요구사항·안전·검증·역할·게시 조건을 유지한다.

## Product Impact

- Plugin 설치 여부를 확인하지 않아도 Worker가 저장된 Ponytail 지침을 전달받는다.
- CLI Plugin 목록 조회의 지연·실패가 Worker 준비에 추가되지 않는다.
- 자동 실행의 기존 확장 도구 경계와 사용자 설치·설정 상태를 유지한다.

## Non-Goals

- Plugin·App·MCP·Hook 활성화 정책 변경 또는 Plugin별 실행 경계 검증 기능.
- 범용 Skill·Plugin 설치 목록 관리·설치·제거·활성화와 디렉터리 탐색.
- 전체 Ponytail Skill·Hook과 축약 지침의 동일한 효과 보장, 모드 자동 변경.
- 리팩터링 전용 추가 규칙·다른 역할로의 확대·원격 원문 자동 갱신.

## Open Questions

- 없음
