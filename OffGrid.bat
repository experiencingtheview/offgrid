@echo off
rem Clear Windows launcher at the top of an OffGrid folder.
rem Double-click this file to start interactive chat. Arguments work like the CLI.
title OffGrid

rem Running the .bat straight from a zip viewer extracts only this file, so the
rem real launcher is missing. Say that instead of closing without a word.
if not exist "%~dp0launch\offgrid.bat" (
  echo OffGrid: launch\offgrid.bat is missing next to this file.
  echo Extract the whole zip first, then run OffGrid.bat from the extracted folder.
  echo.
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)

call "%~dp0launch\offgrid.bat" %*
exit /b %errorlevel%
