---
id: G-253
title: 자체 Windows verify의 명시적 임시 경로 선택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: low
priority: normal
---

## Purpose

자체 Windows verify에서 단독/Manager 실행 모두 테스트 임시 파일시스템을 명시적으로 선택하게 한다. 현재 사용자 프로필 기본값은 유지하고 실제 Python 시작 전에 절대 경로를 적용해 진단과 비교 조건을 일치시킨다.

## Acceptance Criteria

- 자체 Windows의 단독·Manager launcher와 부분/전체 실행에서 동일한 명시 입력으로 임시 경로를 선택한다. 미지정은 현재 %USERPROFILE%\.cache\loop-manager\verify\tmp이며 상대 경로는 실행 checkout 기준으로 해석한다. 공백·비ASCII 경로도 실제 Python의 tempfile 선택에 적용된다.
- Python 조회/실행을 포함한 자식 시작 전에 최종 절대 경로를 TEMP·TMP·TMPDIR에 적용한다. 실제 선택 경로를 부분/전체 mode와 함께 기존 run 진단/summary에 남기고 입력 문자열이나 checkout 위치만으로 선택 성공을 표시하지 않는다.
- 지정 경로 생성·사용 실패는 경로 오류와 실패 종료 코드다. 기본값이나 다른 디스크로 조용히 대체하지 않으며 검증 누락을 성공으로 처리하지 않는다.
- 정상·검증 실패·예외 뒤 호출자의 TEMP/TMP/TMPDIR·Python 인코딩·console 인코딩·cwd를 원래 존재/값으로 복원한다. 공유 임시 루트의 기존 파일과 다른 실행의 자료는 보존하며 전체 폴더 삭제·보안 제품 설정 변경을 수행하지 않는다.
- 다른 등록 프로젝트의 launcher/env를 일괄 변경하지 않는다. ARCHITECTURE의 Windows 경로 설명·docs/VERIFY.md와 OPERATIONS의 바로 필요한 선택 방법/오류/복구 안내를 실제 입력 계약에 맞춘다. 기존 interpreter·protocol/timeout·exit code·version 1 수집 범위를 유지한다.

## Implementation

scripts/verify.ps1의 기존 초기 환경 저장·checkout 이동·temp 생성·finally 복원을 확장한다. 자체 launcher에 한 가지 문서화된 선택 입력을 제공하고 Manager 호출에서도 같은 입력을 전달할 수 있게 한다. 운영 설정 전체나 공통 adapter 선언에 무관한 설정을 추가하지 않는다. 경로 유효/사용 가능 확인과 실제 프로젝트 Python 문맥의 temp 진단은 기존 launcher·runner/metadata 책임에서 처리하고 별도 임시 경로 관리 서비스를 만들지 않는다.

## Validation

- Worker는 test_platform_compatibility.py·verify launcher/manager adapter fixture의 최소 targeted case로 미지정/절대/상대·공백 경로, 생성/사용 실패, 원래 env 부재/값·인코딩/cwd 및 공유 marker 보존을 검사한다. 부분/전체 실행과 부분 선정 FALLBACK에서도 같은 경로 적용·환경 복원을 확인하며 경로 오류를 선정 fallback으로 숨기지 않는다. 정책 조합은 명령/OS 관측 mock으로 확인한다.
- 기존 launcher의 실제 interpreter 연결 회귀를 확장해 Windows 단독/Manager에서 Python tempfile.gettempdir와 진단의 최종 절대 경로가 같은지 확인한다. 실제 PowerShell/Windows가 없는 환경의 mock을 플랫폼 검증 성공으로 기록하지 않는다.
- Manager 전체 verify는 venv 선택·프로토콜/timeout·실패 exit code·guard/자식 환경·전체 필수 범위를 유지한다. Windows의 실제 선택 파일시스템 자료를 남기고 R-065의 같은 PC·코드·범위 Windows/WSL 순차 비교 조건을 따른다. 경로 변경 자체로 절감량이나 환경 원인을 확정하지 않는다.

## Progress

- [x] 2026-10-10 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-067, 고정 profile temp를 설정하는 verify.ps1·runner metadata와 기존 launcher 회귀를 정적으로 읽었다. 구현·테스트·Windows 측정·verify는 실행하지 않았다.
- [ ] 경로 선택/실패/복원·안내·targeted unit 검증
- [ ] Manager 전체 verify·실제 Windows 연결·읽기 전용 Review·사용자 Merge

## Decision Log

- 현재 verify.ps1은 기존 TEMP와 무관하게 사용자 프로필 기본 경로로 덮어쓴다. 환경 복원·공유 루트 보존은 이미 있으므로 재구현하지 않고 선택 입력·실효 경로 진단과 실패 조건만 보완한다.
- complexity medium: launcher의 경로/예외·환경 복원과 실제 Python/Manager 진단 연결이 명확한 책임이다. risk low: 선택 오류는 자체 검증 실패/임시 위치 설명에 한정하며 원본 env·공유 파일을 보존하고 입력 수정·재실행으로 복구한다. priority normal이다.
- G-248은 결과/자식/배정 진단을, 이 작업은 선택 입력과 실효 임시 경로를 담당한다. 두 작업은 기존 metadata를 재사용해 독립 검증하며 R-065의 우선순위를 필수 dependency로 만들지 않는다.
