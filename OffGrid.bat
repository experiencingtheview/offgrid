@echo off
rem Clear Windows launcher at the top of an OffGrid folder.
rem Double-click this file to start interactive chat. Arguments work like the CLI.
call "%~dp0launch\offgrid.bat" %*
exit /b %errorlevel%
