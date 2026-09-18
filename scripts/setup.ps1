$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts/python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv $venv
    } else {
        $launcher = Get-Command python -ErrorAction Stop
        & $launcher.Source -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $venvPython -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$requirements = Join-Path $root 'requirements.txt'
$hasRequirements = Get-Content -LiteralPath $requirements | Where-Object { $_ -match '^\s*[^#\s]' } | Select-Object -First 1
if ($hasRequirements) {
    & $venvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Output "준비 완료: $venvPython"
