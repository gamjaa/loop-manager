$ErrorActionPreference = "Stop"

# 이 스크립트 파일이 위치한 디렉터리
$ScriptDir = $PSScriptRoot

Set-Location $ScriptDir

$python = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$loopManager = Join-Path $ScriptDir "loop_manager.py"

& $python -u $loopManager run @args
exit $LASTEXITCODE