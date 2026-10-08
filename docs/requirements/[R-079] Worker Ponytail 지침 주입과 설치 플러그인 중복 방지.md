# [R-079] Worker Ponytail 지침 주입과 설치 플러그인 중복 방지

## Context

Worker가 불필요한 코드·추상화·의존성을 추가하지 않고 기존 구현을 재사용하도록 Ponytail의 코딩 지침을 제공한다. CLI에 Ponytail 플러그인이 이미 설치된 경우에는 해당 CLI에 적용을 맡겨 Manager의 추가 주입을 생략한다. 범용 Skill 관리 기능을 추가하지 않고 기존 Worker 지침 전달 흐름을 확장한다.

[Ponytail 저장소](https://github.com/DietrichGebert/ponytail)의 [AGENTS.md](https://github.com/DietrichGebert/ponytail/blob/main/AGENTS.md)는 축약 지침이며 플러그인의 전체 Skill·Hook과 동일한 구성은 아니다. 설치 여부만으로 실제 규칙 로딩을 보증할 수는 없지만, 이번 요구사항에서는 사용자 결정에 따라 설치 여부를 주입 생략의 기준으로 삼는다. 비활성 플러그인도 설치되어 있으면 Manager가 지침을 대신 주입하지 않는다.

## Requirement

- Ponytail 플러그인 저장소의 루트 `AGENTS.md` 원문을 Loop Manager의 `docs/AGENTS.ponytail.md`로 저장한다. 가져온 출처와 기준 revision을 변경 PR에서 확인할 수 있어야 한다. 실행 시에는 선택된 runtime에 저장된 파일을 사용하며 원격 원문을 매번 다운로드하지 않는다.
- Ponytail 적용 작업에서 `README.md` 하단에 Loop Manager가 Ponytail 코딩 지침을 사용한다는 안내와 `docs/AGENTS.ponytail.md`로 연결되는 상대 링크를 추가한다.
- Worker 실행 지침을 생성할 때 기존 공통·Worker 지침에 `docs/AGENTS.ponytail.md` 내용을 조건부로 추가한다. 기존 instruction 전달 경로를 사용하며 작업별 사용자 prompt에 같은 내용을 반복하지 않는다. Planner·Reviewer 등 다른 역할에는 이 파일을 추가하지 않는다.
- Manager 실행 후 첫 Worker 호출 전에 Codex와 Claude Code 각각의 설치 플러그인 목록을 CLI가 제공하는 조회 기능으로 확인한다. Worker 호출이 없는 실행에서는 이 조회를 수행하지 않는다.
- 조회 결과는 provider별로 구분하여 같은 Manager 실행에서 재사용한다. 재시도·후속 Worker 호출·동시 호출 때문에 목록을 반복 조회하지 않으며, Manager를 다시 실행하면 첫 Worker 호출 전에 새로 조회한다. 실행 중 설치 상태의 변경은 다음 Manager 실행부터 반영한다.

Worker에 사용할 CLI의 조회 결과에 따라 다음 정책을 적용한다. 다른 CLI의 설치 여부는 해당 Worker의 주입 판단에 영향을 주지 않는다.

| 해당 CLI의 조회 결과 | Manager의 Ponytail 지침 추가 |
|---|---|
| Ponytail 설치 확인, 활성 상태 | 생략 |
| Ponytail 설치 확인, 비활성 상태 | 생략 |
| 정상 조회 결과에 Ponytail이 설치되어 있지 않음 | 추가 |
| 조회 실패·시간 초과·명령 미지원·출력 해석 실패·설치 여부 불명확 | 추가 |

설치 판정은 실제 설치된 Ponytail 플러그인을 식별한 결과로 한정한다. 설치 가능한 marketplace 목록에 이름이 나타나거나 비슷한 이름의 플러그인이 있다는 이유로 생략하지 않는다. 조회 실패는 미설치 확정과 구분해 원인과 주입 판단을 진단에 남기며, 이 실패만으로 Worker 호출을 중단하지 않는다. 한 CLI의 조회 실패는 다른 CLI의 정상 조회 결과를 무효화하지 않는다.

Ponytail 원본도 [R-064](%5BR-064%5D%20모델%20호출을%20통한%20공통%20Agent%20지침%20전달.md)의 지침 버전 보존 규칙을 따른다. 포함 여부는 이번 Manager 실행의 provider별 조회 결과로 결정하되, 같은 작업은 고정된 Ponytail 원본 버전을 사용한다. 실제 전달 내용과 해시는 포함·생략 결과를 반영하며, 기존 snapshot에 없는 새 원본을 과거 작업에 소급해 덧붙이지 않는다. 지침 원본·snapshot 오류는 플러그인 조회 실패와 구분하여 기존 호출 거부 기준을 유지한다.

Ponytail은 코딩 스타일 지침으로 적용하며 명시된 요구사항·필수 기능·보안·입력 검증·데이터 무결성·필수 테스트와 Loop Manager의 역할·권한·검증·게시 규칙을 생략하거나 변경하는 근거로 사용하지 않는다.

## Acceptance Criteria

- `docs/AGENTS.ponytail.md`에 upstream 루트 `AGENTS.md` 원문이 저장되고 출처와 기준 revision을 확인할 수 있다.
- `README.md` 하단에 Ponytail 사용 안내가 있고, 함께 제공된 `AGENTS.ponytail.md` 링크가 저장소의 `docs/AGENTS.ponytail.md` 파일로 연결된다.
- 최초 Worker가 실행되기 전에 두 CLI의 설치 플러그인 목록 조회가 수행된다. 같은 Manager 실행의 후속·재시도·동시 Worker 호출에서는 provider별 조회가 중복되지 않는다. Worker가 없으면 조회하지 않으며 다음 Manager 실행에서는 다시 조회한다.
- Ponytail 미설치가 확인된 provider의 Worker 지침에는 고정된 `AGENTS.ponytail.md` 내용이 정확히 한 번 추가된다. 기존 공통·Worker 지침과 provider의 기본·추가 지침 및 프로젝트 규칙은 보존된다.
- Ponytail이 설치된 provider의 Worker에는 활성·비활성 여부와 관계없이 Manager가 Ponytail 원문을 추가하지 않는다. Codex에만 설치된 경우 Claude Worker에는 추가하고, Claude에만 설치된 경우 Codex Worker에는 추가한다.
- marketplace의 미설치 Ponytail 항목이나 유사한 이름의 다른 플러그인만 존재하면 Ponytail 설치로 판단하지 않는다.
- 조회 실패·시간 초과·명령 미지원·출력 해석 실패와 설치 여부 불명확 상황에서는 해당 provider에 저장된 지침을 추가한다. 진단에서 실패 원인과 추가 판단을 확인할 수 있으며 다른 provider의 결과와 Worker 실행을 이 실패만으로 막지 않는다.
- Planner·Reviewer 등 다른 역할과 작업별 사용자 prompt에는 이번 Ponytail 원문이 추가되지 않는다.
- 재시도·재개·provider 변경에서도 같은 작업의 원본 버전은 유지한다. 호출 기록의 실제 내용·해시는 해당 provider의 포함·생략 판단과 일치하고, 다른 지침으로 얻은 성공 결과를 동일 지침의 결과로 재사용하지 않는다. 과거 snapshot의 보존과 원본·snapshot 오류에 대한 호출 거부는 유지된다.
- 단순화 지침이 명시된 요구사항과 필수 검증·안전·역할·게시 조건을 생략하도록 허용하지 않는다.

## Product Impact

- Ponytail 미설치 또는 설치 확인 불가 환경의 Worker도 저장된 코딩 지침을 전달받는다.
- Ponytail이 설치된 환경에서는 CLI에 적용을 맡기며 비활성 상태도 Manager가 보완하거나 변경하지 않는다.
- 설치 상태는 Manager 실행마다 한 번 확인하고 같은 실행에서 재사용한다.

## Non-Goals

- 범용 전역·프로젝트 Skill 목록 관리, Skill 디렉터리 탐색과 Vercel Skills CLI 연동.
- 전역·프로젝트 `AGENTS.md` 또는 Skill 본문을 비교하는 일반적인 중복 규칙 탐지.
- Hook 신뢰·실행 여부, 실제 prompt 로딩 또는 Ponytail 모드를 확인하는 런타임 검증 기능.
- 플러그인의 설치·활성화·제거, Hook 설정 또는 `lite`·`full`·`ultra`·`off` 모드의 자동 변경.
- 리팩터링 전용 추가 규칙, 다른 역할로의 Ponytail 확대 또는 실행 중 원격 원문 자동 갱신.

## Open Questions

- 없음
