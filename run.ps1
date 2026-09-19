$ErrorActionPreference = "Stop"

# 이 스크립트 파일이 위치한 디렉터리
$ScriptDir = $PSScriptRoot

Set-Location $ScriptDir

Write-Output "===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="

$gitOutput = @(& git pull --ff-only 2>&1)
$gitExitCode = $LASTEXITCODE

if ($gitExitCode -eq 0) {
    if (-not ($gitOutput.Count -eq 1 -and $gitOutput[0].ToString() -eq "Already up to date.")) {
        $gitOutput | ForEach-Object { Write-Output $_ }
    }
}
else {
    $gitOutput | ForEach-Object { Write-Output $_ }
    exit $gitExitCode
}

$python = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$loopManager = Join-Path $ScriptDir "loop_manager.py"

& $python -u $loopManager run @args
exit $LASTEXITCODE