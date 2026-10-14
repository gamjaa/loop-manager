$ErrorActionPreference = "Stop"
if (-not $env:LOOP_MANAGER_VERIFY_RUNTIME_PYTHON -or -not $env:LOOP_MANAGER_VERIFY_RUNNER) {
    throw "Set the installed Manager Python and entrypoint.py absolute paths"
}
$previous = Get-Location
try {
    Set-Location (Join-Path $PSScriptRoot "..")
    $protocol = if ($env:LOOP_MANAGER_VERIFY_VERSION) { $env:LOOP_MANAGER_VERIFY_VERSION } else { "1" }
    $timeout = if ($env:LOOP_MANAGER_VERIFY_TIMEOUT) { $env:LOOP_MANAGER_VERIFY_TIMEOUT } else { "600" }
    $projectPython = if ($env:LOOP_MANAGER_EXAMPLE_PYTHON) { $env:LOOP_MANAGER_EXAMPLE_PYTHON } else { $env:LOOP_MANAGER_VERIFY_RUNTIME_PYTHON }
    & $env:LOOP_MANAGER_VERIFY_RUNTIME_PYTHON -u $env:LOOP_MANAGER_VERIFY_RUNNER --protocol-version $protocol --project-python $projectPython --timeout $timeout
    $result = $LASTEXITCODE
} finally {
    Set-Location $previous
}
exit $result
