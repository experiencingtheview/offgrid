@echo off
rem OffGrid launcher (Windows). Starts the bundled model server if needed,
rem waits for it to answer, then runs the agent with whatever arguments you gave.
setlocal enabledelayedexpansion

set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
if "%OFFGRID_PORT%"=="" (set "PORT=8089") else (set "PORT=%OFFGRID_PORT%")
set "BASE=http://127.0.0.1:%PORT%/v1"

if exist "%ROOT%\runtime\python\python.exe" (
  set "PY=%ROOT%\runtime\python\python.exe"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo OffGrid: no python found. Bundle one with scripts\setup_usb.py
    exit /b 1
  )
  set "PY=python"
)

set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\run" mkdir "%ROOT%\run"

set "SERVER="
for /r "%ROOT%\runtime\llama" %%F in (llama-server.exe) do if not defined SERVER set "SERVER=%%~fF"
set "MODEL="
for %%F in ("%ROOT%\models\*.gguf") do if not defined MODEL set "MODEL=%%~fF"

"%PY%" -c "import urllib.request,sys;urllib.request.urlopen(sys.argv[1]+'/models',timeout=2)" "%BASE%" >nul 2>&1
set "UP=%errorlevel%"

if "%UP%"=="0" goto run
if not defined MODEL goto run
if not exist "%SERVER%" goto run

echo OffGrid: loading %MODEL% on port %PORT% (first token takes a moment)...
start "offgrid-llama" /b "%SERVER%" -m "%MODEL%" --host 127.0.0.1 --port %PORT% -c 8192 --jinja >>"%ROOT%\logs\llama.log" 2>&1

set /a TRIES=0
:wait
"%PY%" -c "import urllib.request,sys;urllib.request.urlopen(sys.argv[1]+'/models',timeout=2)" "%BASE%" >nul 2>&1
if %errorlevel%==0 goto run
set /a TRIES+=1
if %TRIES% GEQ 180 goto slow
timeout /t 1 /nobreak >nul
goto wait

:slow
echo OffGrid: the model server did not come up yet. Continuing anyway.
echo Check "%ROOT%\logs\llama.log" or run: offgrid doctor

:run
"%PY%" -m offgrid %*
endlocal
