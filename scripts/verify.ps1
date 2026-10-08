$ErrorActionPreference = 'Stop'

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousTmpDir = $env:TMPDIR
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'
    $tempDirectory = Join-Path $env:USERPROFILE '.cache/loop-manager/verify/tmp'
    New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
    $env:TEMP = $tempDirectory
    $env:TMP = $tempDirectory
    $env:TMPDIR = $tempDirectory
    $venvPython = Join-Path $root '.venv/Scripts/python.exe'
    $pythonArguments = @()
    if (Test-Path -LiteralPath $venvPython) {
        $pythonCommand = $venvPython
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonCommand = 'py'
        $pythonArguments = @('-3')
    } else {
        $pythonCommand = 'python'
    }
    if ($env:LOOP_MANAGER_VERIFY_RUNNER) {
        if (!(Test-Path -LiteralPath $env:LOOP_MANAGER_VERIFY_RUNNER) -or !$env:LOOP_MANAGER_VERIFY_RUNTIME_PYTHON -or !$env:LOOP_MANAGER_VERIFY_VERSION) {
            throw 'Manager verify runner, runtime Python or version is missing'
        }
        $projectPython = & $pythonCommand @pythonArguments -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'Project Python lookup failed' }
        & $env:LOOP_MANAGER_VERIFY_RUNTIME_PYTHON -u $env:LOOP_MANAGER_VERIFY_RUNNER --protocol-version $env:LOOP_MANAGER_VERIFY_VERSION --project-python $projectPython --timeout $env:LOOP_MANAGER_VERIFY_TIMEOUT
    } else {
        & $pythonCommand @pythonArguments -u -m loop_manager_internal.verification.runner --protocol-version 1
    }
    $verifyExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:TMPDIR = $previousTmpDir
    Pop-Location
}
exit $verifyExitCode
