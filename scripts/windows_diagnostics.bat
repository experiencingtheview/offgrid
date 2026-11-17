@echo off
rem OffGrid Windows first-run diagnostics. Double-click this file after extracting
rem the whole zip. It needs no Python and writes a log zip under %TEMP%.
setlocal
title OffGrid diagnostics

set "SCRIPT=%~dp0windows_diagnostics.ps1"
if not exist "%SCRIPT%" (
  echo OffGrid: scripts\windows_diagnostics.ps1 is missing.
  echo Extract the whole zip and run this file again.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Diagnostic complete. Attach the log bundle or files folder printed above.
) else (
  echo Diagnostic finished with errors. Attach the log bundle or files folder printed above.
)
echo.
pause
exit /b %RC%
