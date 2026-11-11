#Requires -Version 5.1
<#
.SYNOPSIS
  Interactive one-click publish: version + notes -> bump -> build exe -> GitHub Release.

.EXAMPLE
  .\publish_release.ps1
  .\publish_release.ps1 -Version 0.2.0 -Notes "fix Signal plot"
  Double-click publish_release.bat
#>
param(
    [string]$Version = "",
    [string]$Notes = "",
    [string]$Repo = "",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

function Get-ProjectVersion {
    $pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
    if ($pyproject -match 'version\s*=\s*"([^"]+)"') {
        return $Matches[1]
    }
    throw "Cannot read version from pyproject.toml"
}

function Get-NextPatchVersion([string]$Current) {
    $parts = $Current.TrimStart("vV").Split(".")
    while ($parts.Count -lt 3) { $parts += "0" }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    $patch = [int]$parts[2] + 1
    return "$major.$minor.$patch"
}

function Set-ProjectVersion([string]$NewVersion) {
    $pyPath = Join-Path $Root "pyproject.toml"
    $initPath = Join-Path $Root "src\xcp_pcan\__init__.py"
    $py = Get-Content -LiteralPath $pyPath -Raw
    $py2 = [regex]::Replace($py, 'version\s*=\s*"[^"]+"', "version = `"$NewVersion`"", 1)
    if ($py2 -eq $py) { throw "Failed to update version in pyproject.toml" }
    Set-Content -LiteralPath $pyPath -Value $py2 -Encoding utf8 -NoNewline

    $init = Get-Content -LiteralPath $initPath -Raw
    if ($init -notmatch '__version__\s*=\s*"[^"]+"') {
        throw "Cannot find __version__ in __init__.py"
    }
    $init2 = [regex]::Replace($init, '__version__\s*=\s*"[^"]+"', "__version__ = `"$NewVersion`"", 1)
    Set-Content -LiteralPath $initPath -Value $init2 -Encoding utf8 -NoNewline
}

function Read-MultilineNotes {
    Write-Host "Release notes (shown in update dialog; empty line to finish):" -ForegroundColor Yellow
    $lines = New-Object System.Collections.Generic.List[string]
    while ($true) {
        $line = Read-Host
        if ([string]::IsNullOrWhiteSpace($line)) { break }
        $lines.Add($line) | Out-Null
    }
    if ($lines.Count -eq 0) { return "" }
    return ($lines -join "`n")
}

Write-Host ""
Write-Host "======== CANkit one-click publish ========" -ForegroundColor Cyan
Write-Host ""

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) not found. Install it and run: gh auth login"
}
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Not logged in to GitHub. Run: gh auth login"
}

if (-not $Repo) {
    $Repo = (gh repo view --json nameWithOwner -q .nameWithOwner 2>$null)
    if (-not $Repo) {
        $Repo = "SEVENYANG798/CANkit"
    }
}

$current = Get-ProjectVersion
$suggested = Get-NextPatchVersion $current

if (-not $Version) {
    Write-Host "Current version : $current" -ForegroundColor Gray
    Write-Host "Suggested next  : $suggested" -ForegroundColor Gray
    $inputVer = Read-Host "1) New version (Enter = $suggested)"
    if ([string]::IsNullOrWhiteSpace($inputVer)) {
        $Version = $suggested
    } else {
        $Version = $inputVer.Trim().TrimStart("vV")
    }
} else {
    $Version = $Version.Trim().TrimStart("vV")
}

if (-not $Notes) {
    Write-Host ""
    $Notes = Read-MultilineNotes
    if ([string]::IsNullOrWhiteSpace($Notes)) {
        $Notes = "CANkit $Version"
    }
}

$Tag = "v$Version"

Write-Host ""
Write-Host "About to publish:" -ForegroundColor Cyan
Write-Host "  Repo    : $Repo"
Write-Host "  Version : $current  ->  $Version  (tag $Tag)"
Write-Host "  Notes   :"
foreach ($line in ($Notes -split "`n")) {
    Write-Host "    $line"
}
Write-Host "  Steps   : bump version files -> build CANkit.exe -> upload GitHub Release"
Write-Host ""

if (-not $Yes) {
    $ok = Read-Host "Confirm publish? (Y/n)"
    if ($ok -and $ok.Trim().ToLowerInvariant() -eq "n") {
        Write-Host "Cancelled." -ForegroundColor Yellow
        exit 0
    }
}

Write-Host "==> Bump project version" -ForegroundColor Cyan
Set-ProjectVersion $Version

Write-Host "==> Building exe (may take a few minutes)" -ForegroundColor Cyan
& (Join-Path $Root "build_exe.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "build_exe.ps1 failed, exit $LASTEXITCODE"
}

$Exe = Join-Path $Root "output\CANkit.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Missing $Exe"
}

$ShaFile = Join-Path $Root "output\CANkit.exe.sha256"
$hash = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ShaFile -Value "$hash  CANkit.exe" -Encoding ascii

Write-Host "==> Upload Release $Tag -> $Repo" -ForegroundColor Cyan
# gh writes "release not found" to stderr; with $ErrorActionPreference=Stop that
# becomes a terminating error. Probe with Continue so missing tags are OK.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
gh release view $Tag --repo $Repo 1>$null 2>$null
$releaseExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap

if (-not $releaseExists) {
    gh release create $Tag $Exe $ShaFile --repo $Repo --title "CANkit $Version" --notes $Notes
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed, exit $LASTEXITCODE" }
} else {
    Write-Host "Tag $Tag already exists; replacing assets." -ForegroundColor Yellow
    gh release upload $Tag $Exe $ShaFile --repo $Repo --clobber
    if ($LASTEXITCODE -ne 0) { throw "gh release upload failed, exit $LASTEXITCODE" }
    gh release edit $Tag --repo $Repo --title "CANkit $Version" --notes $Notes
    if ($LASTEXITCODE -ne 0) { throw "gh release edit failed, exit $LASTEXITCODE" }
}

# Ensure not left as Draft (interrupted uploads can stay draft).
$prevEap2 = $ErrorActionPreference
$ErrorActionPreference = "Continue"
gh release edit $Tag --repo $Repo --draft=false 1>$null 2>$null
$draftRc = $LASTEXITCODE
$ErrorActionPreference = $prevEap2
if ($draftRc -ne 0) {
    throw "Failed to publish draft release, exit $draftRc"
}

$url = "https://github.com/$Repo/releases/tag/$Tag"
Write-Host ""
Write-Host "Published OK." -ForegroundColor Green
Write-Host "  Release : $url"
Write-Host "  EXE     : $Exe"
Write-Host "  SHA256  : $hash"
Write-Host ""
Write-Host "Tip: version files changed; remember to commit and push source later." -ForegroundColor Gray
Write-Host "Others will see $Version when they open the old exe online." -ForegroundColor Gray
exit 0