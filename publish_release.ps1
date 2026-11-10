#Requires -Version 5.1
<#
.SYNOPSIS
  Build CANkit.exe and publish a GitHub Release (asset + sha256).

.EXAMPLE
  .\publish_release.ps1
  .\publish_release.ps1 -Version 0.2.0 -Notes "Signal plot improvements"
#>
param(
    [string]$Version = "",
    [string]$Notes = "",
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
            [System.Environment]::GetEnvironmentVariable('Path','User')

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) not found. Install it and run: gh auth login"
}
gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Not logged in. Run: gh auth login"
}

if (-not $Repo) {
    $Repo = (gh repo view --json nameWithOwner -q .nameWithOwner 2>$null)
    if (-not $Repo) {
        throw "Pass -Repo owner/CANkit or run from a git checkout with origin set."
    }
}

if (-not $Version) {
    $pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
    if ($pyproject -match 'version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        throw "Could not read version from pyproject.toml; pass -Version"
    }
}

$Tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
$Version = $Tag.TrimStart("v")

Write-Host "==> Building exe" -ForegroundColor Cyan
& (Join-Path $Root "build_exe.ps1")
if ($LASTEXITCODE -ne 0) { throw "build_exe.ps1 failed" }

$Exe = Join-Path $Root "output\CANkit.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Missing $Exe"
}

$ShaFile = Join-Path $Root "output\CANkit.exe.sha256"
$hash = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ShaFile -Value "$hash  CANkit.exe" -Encoding ascii

if (-not $Notes) {
    $Notes = "CANkit $Version"
}

Write-Host "==> Publishing $Tag to $Repo" -ForegroundColor Cyan
# Create release if missing; upload/replace assets.
$exists = gh release view $Tag --repo $Repo 2>$null
if ($LASTEXITCODE -ne 0) {
    gh release create $Tag $Exe $ShaFile --repo $Repo --title "CANkit $Version" --notes $Notes
} else {
    gh release upload $Tag $Exe $ShaFile --repo $Repo --clobber
    gh release edit $Tag --repo $Repo --title "CANkit $Version" --notes $Notes
}

Write-Host ""
Write-Host "Published: https://github.com/$Repo/releases/tag/$Tag" -ForegroundColor Green
Write-Host "SHA256: $hash"
