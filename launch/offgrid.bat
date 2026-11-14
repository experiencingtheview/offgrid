@echo off
rem OffGrid launcher (Windows). With no arguments it opens the one-click browser
rem setup. Explicit commands start the bundled model server first. When something is
rem missing it says why and keeps the window open, because a double-click
rem closes the console the moment the script exits.
setlocal enabledelayedexpansion
title OffGrid

set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
cd /d "%ROOT%" 2>nul
if "%OFFGRID_PORT%"=="" (set "PORT=8089") else (set "PORT=%OFFGRID_PORT%")
set "BASE=http://127.0.0.1:%PORT%/v1"

rem -- find a Python that actually runs --------------------------------------
set "PY="
set "PYARGS="
if exist "%ROOT%\runtime\python\python.exe" (
  set "PY=%ROOT%\runtime\python\python.exe"
  goto check_python
)
rem The Microsoft Store alias answers "where python" but is not an interpreter,
rem so every candidate is executed once instead of trusted.
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  set "PY=py"
  set "PYARGS=-3"
  goto check_python
)
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
  set "PY=python"
  goto check_python
)
python3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  set "PY=python3"
  goto check_python
)
goto no_python

:check_python
"%PY%" %PYARGS% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 (
  if defined BOOTSTRAPPED goto old_python
  rem A system Python can be old or broken. Download the private one
  rem instead of sending a non-programmer to python.org.
  set "PY="
  set "PYARGS="
  goto no_python
)
goto python_ok

:old_python
echo OffGrid: the private Python runtime is older than 3.9.
echo Check the internet connection and run OffGrid again.
goto fail

:python_ok
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\run" mkdir "%ROOT%\run"

rem The setup command starts and owns the model server itself. Starting a second
rem copy here would make a double-click wait for the same model twice.
if "%~1"=="" goto run
if /I "%~1"=="easy" goto run

set "SERVER="
for /r "%ROOT%\runtime\llama" %%F in (llama-server.exe) do if not defined SERVER set "SERVER=%%~fF"
set "MODEL="
for %%F in ("%ROOT%\models\*.gguf") do if not defined MODEL set "MODEL=%%~fF"

"%PY%" %PYARGS% -c "import urllib.request,sys;urllib.request.urlopen(sys.argv[1]+'/models',timeout=2)" "%BASE%" >nul 2>&1
set "UP=%errorlevel%"

if "%UP%"=="0" goto run
if not defined MODEL goto run
if not exist "%SERVER%" goto run

echo OffGrid: loading %MODEL% on port %PORT% (first token takes a moment)...
start "offgrid-llama" /b "%SERVER%" -m "%MODEL%" --host 127.0.0.1 --port %PORT% -c 8192 --jinja >>"%ROOT%\logs\llama.log" 2>&1

set /a TRIES=0
:wait
"%PY%" %PYARGS% -c "import urllib.request,sys;urllib.request.urlopen(sys.argv[1]+'/models',timeout=2)" "%BASE%" >nul 2>&1
if %errorlevel%==0 goto run
set /a TRIES+=1
if %TRIES% GEQ 180 goto slow
timeout /t 1 /nobreak >nul
goto wait

:slow
echo OffGrid: the model server did not come up yet. Continuing anyway.
echo Check "%ROOT%\logs\llama.log" or run: offgrid doctor

:run
if "%~1"=="" (
  "%PY%" %PYARGS% -m offgrid easy
) else (
  "%PY%" %PYARGS% -m offgrid %*
)
set "RC=%errorlevel%"
rem A double-click has no arguments: keep the window open so the result and any
rem error remain readable instead of vanishing with the console.
if not "%~1"=="" goto end
echo.
if not "%RC%"=="0" echo OffGrid exited with code %RC%.
echo Press any key to close this window.
pause >nul
:end
endlocal & exit /b %RC%

:no_python
if defined BOOTSTRAPPED goto no_python_help
set "BOOTSTRAPPED=1"
echo OffGrid: first run needs a private Python runtime.
echo It will be downloaded into this folder, not installed system-wide.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\launch\bootstrap.ps1"
if exist "%ROOT%\runtime\python\python.exe" (
  set "PY=%ROOT%\runtime\python\python.exe"
  set "PYARGS="
  goto check_python
)

:no_python_help
echo.
echo OffGrid could not set up its private Python runtime.
echo Check the internet connection and try again.
echo If this computer blocks downloaded programs, right-click the .zip,
echo choose Properties, tick Unblock, extract it again, and retry.
echo.
echo Advanced alternative: install Python from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH". The Microsoft Store alias is not enough.

:fail
echo.
echo Press any key to close this window.
pause >nul
endlocal & exit /b 1
