@echo off
setlocal
cd /d "%~dp0"
echo Building XcpVariableReader.exe ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1"
set ERR=%ERRORLEVEL%
echo.
if %ERR% neq 0 (
  echo Build FAILED (exit %ERR%).
  pause
  exit /b %ERR%
)
echo Build OK. EXE is in the output folder.
pause
exit /b 0
