@echo off
chcp 65001 >nul
title Live Subtitles

pushd "%~dp0"

:: -- Check setup has been run -------------------------------------------
if not exist venv\Scripts\activate.bat (
    echo.
    echo [ERROR] Virtual environment not found.
    echo Please run setup.bat first, then try again.
    echo.
    pause & exit /b 1
)

call venv\Scripts\activate.bat

python main.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] App exited with an error. See above for details.
    pause
)

popd
