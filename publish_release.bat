@echo off
setlocal
cd /d "%~dp0"
echo.
echo CANkit one-click publish
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_release.ps1"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo Publish FAILED ^(exit %ERR%^).
  pause
  exit /b %ERR%
)
echo Done
pause
exit /b 0
