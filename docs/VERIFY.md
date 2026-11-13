# 검증

## 등록 프로젝트의 필수 Verify

Manager는 프로젝트가 지정한 명령을 하나의 필수 검증으로 실행한다. 언어별 테스트 러너·공통 분산 엔진을 등록 프로젝트에 강제하지 않는다. Worker의 관련 테스트 다음에 Verify, 그 성공 근거를 받아 Reviewer를 실행한다. 실패·중단·timeout·자식 종료 불명은 PASS가 아니다.

기본값은 프로젝트의 `scripts/verify.ps1`(Windows) 또는 `scripts/verify.sh`(POSIX)다. 다른 명령·cwd·환경이 필요하면 `scripts/verify.json` version 2를 사용한다.

```json
{
  "version": 2,
  "commands": {
    "windows": ["pwsh", "-NoProfile", "-File", "scripts/check.ps1"],
    "posix": ["bash", "scripts/check.sh"]
  },
  "cwd": ".",
  "environment": {"PROJECT_MODE": "test"},
  "inputs": ["src", "tests", "scripts", "package.json", "package-lock.json"]
}
```

명령은 문자열 argv 배열이며 shell 문자열로 재해석하지 않는다. `{python}`은 현재 Python 실행 파일이다. cwd는 프로젝트 내부 디렉터리, environment는 문자열 key/value다. 실행 제한은 Manager의 `verify_timeout_seconds`를 사용하고 자원 대기는 포함하지 않는다.

`inputs`는 검증에 영향을 주는 비 Git 입력을 포함해 안전하게 열거할 상대 경로·glob 목록이다. 존재하지 않는 패턴이나 입력 목록 부재는 성공 재사용을 허용하지 않는다. 필수 입력을 빠짐없이 열거할 수 없는 프로젝트는 생략한다. 후보 HEAD·선택 G·활성 R Blob OID·검증 명령/환경/timeout·파일 내용·Worker/Reviewer 지침을 비교하고 일치할 때만 이전 성공을 재사용한다. 프로젝트 밖을 가리키는 경로는 거부한다.

version 1은 이 저장소 자체의 검증 도구 선언이며 Manager의 범용 프로젝트 manifest가 아니다. 다른 프로젝트도 OS 기본 스크립트 안에서 자체 러너를 자유롭게 사용할 수 있다.

## Loop Manager 자체 검증

변경한 동작의 최소 unit 회귀를 먼저 실행한다. 정책 조합·SQLite·외부 API는 mock 중심으로 확인하고 실제 Git·CLI는 Merge·미게시 파일 보존·게시 후 재시작·ignored-only 비차단 등 실제 효과가 필요한 대표 경로만 실행한다.

Windows에서 전체 검증은 다음 명령이다.

```powershell
pwsh -NoProfile -File scripts/verify.ps1
```

WSL 전체 검증 뒤 `scripts/verify-windows-tests.json`의 Windows 핵심 회귀를 실행한다. 두 단계가 모두 성공해야 통과한다. WSL 부재·실패를 native 전체 실행으로 우회하지 않으며 결과를 Windows 전체 verify 통과로 표현하지 않는다.

WSL은 `/tmp/`의 실행별 전용 디렉터리에 현재 변경을 포함한 프로젝트 복사본과 임시 폴더를 만든다. Git 메타데이터를 WSL에서 유효하게 준비하고 TMPDIR/TMP/TEMP를 해당 임시 폴더로 지정한다. 성공·실패와 관계없이 이번 전용 디렉터리를 정리한다. 복사·두 단계 실행·정리는 하나의 전체 timeout을 공유한다.

POSIX 자체 검증은 `bash scripts/verify.sh`다. Python unittest와 compile, 격리 Python/Perl 검증 러너의 대표 통합 경로를 실행한다. 이 내부 도구는 Loop Manager 개발용이며 등록 프로젝트에는 강제하지 않는다. 부분 검증·선행 Reviewer 경로는 사용하지 않는다.

## 시간·병렬도·진단

- 전체 실행 제한: `LOOP_MANAGER_VERIFY_TIMEOUT`(초, 기본 600).
- 자체 도구 내부 병렬도: `LOOP_MANAGER_VERIFY_PARALLELISM` 또는 `config/config.ini`의 `verify_parallelism`. `auto`는 CPU·메모리 부하와 예약을 확인하고 관측 불확실 시 직렬로 축소한다.
- Windows 임시 폴더: `LOOP_MANAGER_VERIFY_TEMP_DIR`. 기본값은 사용자 홈 아래 `.cache/loop-manager/verify/tmp`다. Defender 제외를 설정한다면 이 전용 임시 경로만 사용자가 직접 판단해 지정한다.
- 실행 보고서: `.cache/verify/<run-id>/`의 `result.json`, 각 단계의 `summary.json`·manifest·진단. 테스트 결과와 WSL/native 상태를 구분한다. 운영 G의 장기 결과 저장소가 아니다.

중첩 Python 호출 guard·프로세스 종료·예약 조율을 제거해 검증을 우회하지 않는다. 종료 불확실성이 있으면 성공 표식을 남기지 않으며 실제 프로세스와 임시 폴더를 확인한다.
