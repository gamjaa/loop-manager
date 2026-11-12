# Loop Manager 개발 지침

문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.

## 용어

- 계획: Requirement
- 작업: ExecPlan
- 설계: PRODUCT, ARCHITECTURE
- 베이스/기본/원본 브랜치: checkout 받은 브랜치
- 작업 브랜치: G 작업 등을 수행하기 위해 분리한 브랜치

## 저장소 구조

- 새 루트 항목은 사용자 진입, 실행·빌드·배포 또는 도구의 필수 탐색이라는 근거가 필요하다.
  그 근거가 없는 새 루트 항목은 Review에서 blocking issue로 보고한다.

## 작업 중 실행

- 작업 중에는 `loop_manager.py run`을 직접 실행하지 않는다.

## Claude 지원 개발

Claude는 공식 문서에 근거한 일반 호출 구성과 mock 테스트로 개발·완료를 판단한다.
실제 CLI·계정 부재나 실환경 미검증만으로 개발을 차단하거나 완전 지원을 요구하지 않는다.
실행 중 조직 managed 정책 변경 대응과 실환경 점검은
[후속 확인](docs/FUTURE_IDEAS.md#claude-실환경-후속-점검)에 남긴다.
문서/mock 확인을 실제 실행 성공으로 기록하지 않으며, 관측한 호출 오류는 보존·처리한다.

## 실행 지침 원본 수정

이 저장소의 공통·역할별 `docs/AGENTS.*.md` 원본 파일은 현재 역할·목표의 일반 문서 권한과
PR 보고 규칙에 따라 수정한다. 파일을 수정해도 현재 작업에 고정된 Loop Manager instruction과
실행 권한은 바뀌지 않는다. 공통·역할별 원본에는 등록 프로젝트 전반에 적용되는 자동 실행
규칙만 둔다.

## 검증

변경한 동작을 확인하는 최소 범위의 unit test를 실행한다. 문서만 변경했거나 실행 가능한
관련 테스트가 없으면 그 사유를 결과에 명시한다.

Windows 개발 환경에서 이 저장소의 전체 verify와 대규모 Git·CLI 통합 테스트는 WSL에서
실행한다. 이 기준은 Loop Manager 자체 개발에만 적용하며 다른 등록 프로젝트의 verify
실행 환경을 지정하지 않는다.

Windows 고유 동작은 변경과 관련된 최소 native 회귀로 별도 확인한다. 경로·명령 인자·CRLF·
ACL·Job 종료·SQLite 파일 잠금·중첩 Python guard와 필요한 실제 Git 파일 보존 효과를 포함한다.
WSL 전체 verify와 Windows 핵심 회귀 결과는 구분해서 보고하며, 이 조합을 Windows 전체
verify 통과로 표기하지 않는다. 이 저장소의 `scripts/verify.ps1`은 WSL 전체 검증 후
`scripts/verify-windows-tests.json`의 Windows 핵심 회귀를 자동 실행한다. 두 단계가 모두
성공해야 통과하며 WSL 부재·실패를 native 전체 실행이나 성공으로 우회하지 않는다.
복사·두 단계·정리는 하나의 전체 timeout을 공유한다. WSL 하위 runner는 독립 Linux 조율을
사용하고, Windows 핵심 회귀는 Manager 호출이면 기존 Windows 예약 조율을 사용한다.

WSL verify는 `/tmp/` 아래에 실행별 전용 디렉터리를 만들고,
그 안에 검증 대상 변경을 반영한 프로젝트 복사본과 임시 파일 폴더를 생성해 사용한다.
프로젝트 복사본의 Git 메타데이터는 WSL에서 유효해야 하며, 임시 파일은 `TMPDIR`, `TMP`, `TEMP`를
해당 임시 파일 폴더로 지정해 사용한다. 검증 종료 후에는 성공·실패 여부와 관계없이 이번 실행에서
생성한 전용 디렉터리와 그 안의 프로젝트 복사본·임시 파일을 삭제한다.

자체 테스트를 추가·변경할 때는 [실행 경계와 시나리오 분리 기준](docs/ARCHITECTURE.md#14-구현-이행과-검증)을 따른다.
실제 Git·CLI 실행에는 확인할 효과와 assertion의 근거가 있어야 하며, 조건 조합이나 확인 이후의 교정·게시를 불필요하게 반복하지 않는다.
