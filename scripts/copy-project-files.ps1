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
if ($target -eq $sourceRoot) {
    throw '복사 원본과 같은 디렉터리는 지정할 수 없습니다.'
}
$directories = @(
    (Join-Path $target '.agents')
    (Join-Path $target 'docs')
    (Join-Path $target 'docs/plans')
    (Join-Path $target 'docs/plans/archive')
    (Join-Path $target 'docs/requirements')
    (Join-Path $target 'docs/requirements/archive')
)
New-Item -ItemType Directory -Path $directories -Force | Out-Null

Get-ChildItem -LiteralPath (Join-Path $sourceRoot '.agents/skills') -Directory -Force | ForEach-Object {
    $skillTarget = [System.IO.Path]::GetFullPath((Join-Path $target ".agents/skills/$($_.Name)"))
    if (-not $skillTarget.StartsWith($target.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "대상 디렉터리 밖의 스킬 경로입니다: $skillTarget"
    }
    if (Test-Path -LiteralPath $skillTarget) {
        Remove-Item -LiteralPath $skillTarget -Recurse -Force
    }
}

Get-ChildItem -LiteralPath (Join-Path $sourceRoot '.agents') -Force |
    Copy-Item -Destination (Join-Path $target '.agents') -Recurse -Force

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PLANS.md') -Destination (Join-Path $target 'docs/PLANS.md') -Force
Set-Content -LiteralPath (Join-Path $target 'docs/FUTURE_IDEAS.md') -Value '# 후속 아이디어' -Encoding utf8
Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'docs') -Filter '*.template.md' |
    Copy-Item -Destination (Join-Path $target 'docs') -Force

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PRODUCT.template.md') -Destination (Join-Path $target 'docs/PRODUCT.md') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/ARCHITECTURE.template.md') -Destination (Join-Path $target 'docs/ARCHITECTURE.md') -Force

Write-Output "Loop Manager 프로젝트 파일을 복사했습니다: $target"
