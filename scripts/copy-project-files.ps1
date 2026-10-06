[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$TargetPath
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if (-not (Test-Path -LiteralPath $TargetPath -PathType Container)) {
    throw "대상 디렉터리가 존재하지 않습니다: $TargetPath"
}

$target = (Resolve-Path -LiteralPath $TargetPath).Path
$conflicts = @(
    (Join-Path $target '.agents')
    (Join-Path $target 'docs')
) | Where-Object { $null -ne (Get-Item -LiteralPath $_ -Force -ErrorAction SilentlyContinue) }
$agentsPath = Join-Path $target 'AGENTS.md'
$existingAgents = Get-Item -LiteralPath $agentsPath -Force -ErrorAction SilentlyContinue
if ($existingAgents -and ($existingAgents.PSIsContainer -or
        ($existingAgents.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
    $conflicts += "$agentsPath (일반 파일이 아님)"
}

if ($conflicts) {
    throw "이미 존재하는 파일 또는 폴더가 있습니다: $($conflicts -join ', ')"
}

if (-not $existingAgents) {
    $minimalAgents = @'
# Project instructions

프로젝트 전용 언어·코딩·빌드 규칙은 이 파일에 기록한다.
새로운 기능이나 정책 변경은 Requirement에서 시작한다.
구현 전에 존재하는 관련 문서를 확인한다:
- docs/PRODUCT.md: 현재 제품 동작과 장기 규칙
- docs/requirements/*.md: 원하는 변경, 이유와 Acceptance Criteria
- docs/ARCHITECTURE.md: 시스템 설계
docs/PRODUCT.md 또는 활성 Requirement 중 하나는 반드시 존재해야 한다.
'@
    $stream = [IO.File]::Open($agentsPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($minimalAgents + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}
Copy-Item -LiteralPath (Join-Path $sourceRoot '.agents') -Destination (Join-Path $target '.agents') -Recurse

$directories = @(
    (Join-Path $target 'docs')
    (Join-Path $target 'docs/plans')
    (Join-Path $target 'docs/requirements')
    (Join-Path $target 'docs/requirements/archive')
)
New-Item -ItemType Directory -Path $directories | Out-Null

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PLANS.md') -Destination (Join-Path $target 'docs/PLANS.md')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/FUTURE_IDEAS.md') -Destination (Join-Path $target 'docs/FUTURE_IDEAS.md')
Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'docs') -Filter '*.template.md' |
    Copy-Item -Destination (Join-Path $target 'docs')

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PRODUCT.template.md') -Destination (Join-Path $target 'docs/PRODUCT.md')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/ARCHITECTURE.template.md') -Destination (Join-Path $target 'docs/ARCHITECTURE.md')

Write-Output "Loop Manager 프로젝트 파일을 복사했습니다: $target"
