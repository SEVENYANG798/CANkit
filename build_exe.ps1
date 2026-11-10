#Requires -Version 5.1
<#
.SYNOPSIS
  One-click PyInstaller build → output\CANkit.exe, then delete build junk.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$AppName = "CANkit"
$OutputDir = Join-Path $Root "output"
$WorkRoot = Join-Path $Root ".pyinstaller"
$DistDir = Join-Path $WorkRoot "dist"
$BuildDir = Join-Path $WorkRoot "build"
$Entry = Join-Path $Root "src\xcp_pcan\__main__.py"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Find-Python {
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) {
        return $venvPy
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "Python not found. Create .venv first: python -m venv .venv && .\.venv\Scripts\pip install -e `".[dev]`""
}

function Remove-TreeIfExists([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $Entry)) {
    throw "Entry not found: $Entry"
}

$Python = Find-Python
Write-Step "Using Python: $Python"

Write-Step "Ensuring PyInstaller is installed"
& $Python -m pip install -q "pyinstaller>=6.0"
if ($LASTEXITCODE -ne 0) {
    throw "pip install pyinstaller failed"
}

Write-Step "Cleaning previous pack folders"
Remove-TreeIfExists $WorkRoot
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null

Write-Step "Building $AppName.exe (this may take a few minutes)"
$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $AppName,
    "--paths", (Join-Path $Root "src"),
    "--workpath", $BuildDir,
    "--distpath", $DistDir,
    "--specpath", $WorkRoot,
    # Rely on PyInstaller PySide6 hooks (QtWidgets/Gui/Core) — avoid --collect-all bloat
    "--hidden-import", "PySide6.QtCore",
    "--hidden-import", "PySide6.QtGui",
    "--hidden-import", "PySide6.QtWidgets",
    "--hidden-import", "can.interfaces.pcan",
    "--hidden-import", "can.interfaces.vector",
    "--hidden-import", "can.interfaces.vector.canlib",
    "--hidden-import", "can.interfaces.vector.xldriver",
    # Trace Viewer offline readers (ASC/BLF/CSV)
    "--hidden-import", "can.io",
    "--hidden-import", "can.io.asc",
    "--hidden-import", "can.io.blf",
    "--hidden-import", "can.io.csv",
    "--hidden-import", "can.io.generic",
    "--hidden-import", "xcp_pcan.trace_log",
    "--hidden-import", "xcp_pcan.trace_log.readers",
    "--hidden-import", "elftools",
    "--hidden-import", "elftools.elf.elffile",
    "--hidden-import", "elftools.elf.sections",
    "--hidden-import", "xcp_pcan",
    "--hidden-import", "xcp_pcan.__main__",
    "--hidden-import", "xcp_pcan.elf2a2l",
    "--hidden-import", "xcp_pcan.elf2a2l.elf_extract",
    "--hidden-import", "cantools",
    "--hidden-import", "cantools.database",
    "--collect-submodules", "cantools",
    "--hidden-import", "elftools.dwarf.descriptions",
    "--hidden-import", "elftools.dwarf.die",
    "--hidden-import", "elftools.dwarf.locationlists",
    "--add-data", ((Join-Path $Root "src\xcp_pcan\elf2a2l\templates\Templete_FR.a2l") + ";xcp_pcan/elf2a2l/templates"),
    "--add-data", ((Join-Path $Root "src\xcp_pcan\elf2a2l\templates\Templete_FL.a2l") + ";xcp_pcan/elf2a2l/templates"),
    "--add-data", ((Join-Path $Root "src\xcp_pcan\elf2a2l\templates\xcp_fr.a2l") + ";xcp_pcan/elf2a2l/templates"),
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtWebEngine",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    $Entry
)

& $Python @pyArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$BuiltExe = Join-Path $DistDir "$AppName.exe"
if (-not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Expected exe not found: $BuiltExe"
}

$OutExe = Join-Path $OutputDir "$AppName.exe"
Write-Step "Copying exe → $OutExe"
Copy-Item -LiteralPath $BuiltExe -Destination $OutExe -Force

Write-Step "Removing pack intermediates (.pyinstaller)"
Remove-TreeIfExists $WorkRoot

# Drop accidental root-level leftovers if any
Remove-TreeIfExists (Join-Path $Root "build")
Remove-TreeIfExists (Join-Path $Root "dist")
$spec = Join-Path $Root "$AppName.spec"
if (Test-Path -LiteralPath $spec) {
    Remove-Item -LiteralPath $spec -Force
}

$sizeMb = [math]::Round((Get-Item -LiteralPath $OutExe).Length / 1MB, 1)
Write-Host ""
Write-Host "Done: $OutExe ($sizeMb MB)" -ForegroundColor Green
Write-Host "Double-click the exe on a PC without Python. PCAN/Vector drivers are still required."
