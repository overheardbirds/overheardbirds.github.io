@echo off
title Overheard Birds  (leave this window open while you use the site)
cd /d "%~dp0OverheardBirds"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo.
    echo  Python was not found on this computer.
    echo  Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
    echo.
    pause
    exit /b 1
)

%PY% birdapp.py %*
if errorlevel 1 (
    echo.
    echo  Overheard Birds stopped with an error - see the message above.
    pause
)
