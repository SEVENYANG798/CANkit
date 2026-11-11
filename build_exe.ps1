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
$AppIcon = Join-Path $Root "src\xcp_pcan\resources\cankit.ico"

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
if (-not (Test-Path -LiteralPath $AppIcon)) {
    throw "App icon not found: $AppIcon"
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
    "--icon", $AppIcon,
    "--add-data", ($AppIcon + ";xcp_pcan/resources"),
    "--add-data", ((Join-Path $Root "src\xcp_pcan\resources\cankit.png") + ";xcp_pcan/resources"),
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
# Overwriting the same path keeps Explorer's cached (often default) icon.
# Write to a new file identity, then rename into place and notify the shell.
Write-Step "Installing exe → $OutExe (cache-busting replace)"
$StagingExe = Join-Path $OutputDir ("{0}.__new__.exe" -f $AppName)
Copy-Item -LiteralPath $BuiltExe -Destination $StagingExe -Force
if (Test-Path -LiteralPath $OutExe) {
    Remove-Item -LiteralPath $OutExe -Force
}
Rename-Item -LiteralPath $StagingExe -NewName "$AppName.exe"

# Ask Explorer/Q-Dir to drop the stale icon for this path.
$shellNotify = @"
using System;
using System.Runtime.InteropServices;
public static class ShellNotify {
  [DllImport("shell32.dll")] public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
  public const int SHCNE_ASSOCCHANGED = 0x08000000;
  public const int SHCNE_UPDATEITEM = 0x00002000;
  public const uint SHCNF_PATHW = 0x0005;
  public const uint SHCNF_FLUSH = 0x1000;
}
"@
try {
    Add-Type -TypeDefinition $shellNotify -ErrorAction SilentlyContinue | Out-Null
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($OutExe + [char]0)
    $ptr = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    [System.Runtime.InteropServices.Marshal]::Copy($bytes, 0, $ptr, $bytes.Length)
    [ShellNotify]::SHChangeNotify([ShellNotify]::SHCNE_UPDATEITEM, [ShellNotify]::SHCNF_PATHW -bor [ShellNotify]::SHCNF_FLUSH, $ptr, [IntPtr]::Zero)
    [ShellNotify]::SHChangeNotify([ShellNotify]::SHCNE_ASSOCCHANGED, [ShellNotify]::SHCNF_FLUSH, [IntPtr]::Zero, [IntPtr]::Zero)
    [System.Runtime.InteropServices.Marshal]::FreeHGlobal($ptr)
} catch {
    Write-Host "Shell icon notify skipped: $($_.Exception.Message)" -ForegroundColor Yellow
}

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
Write-Host "If Explorer still shows the old icon, press F5 or open CANkit_v2.exe once (same binary)."
Write-Host "Double-click the exe on a PC without Python. PCAN/Vector drivers are still required."
