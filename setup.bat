@echo off
chcp 65001 >nul
title Live Subtitles - Setup

echo ============================================
echo  Live Subtitles - First-Time Setup
echo ============================================
echo.

:: -- Check Python -------------------------------------------------------
set PYCMD=
py --version >nul 2>&1 && set PYCMD=py
if not defined PYCMD (
    python --version >nul 2>&1 && set PYCMD=python
)
if not defined PYCMD (
    echo.
    echo Python not found. Please install Python 3.10 or newer:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: During install, check "Add Python to PATH".
    echo Then run this setup again.
    pause & exit /b 1
)
echo [OK] Python found (using: %PYCMD%).

:: -- Create virtual environment -----------------------------------------
if not exist venv (
    echo Creating virtual environment...
    %PYCMD% -m venv venv
    if %ERRORLEVEL% neq 0 (
        echo ERROR: Failed to create virtual environment.
        pause & exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)

:: -- Install dependencies -----------------------------------------------
echo Installing dependencies (this may take a few minutes)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Dependency install failed. See errors above.
    pause & exit /b 1
)
echo [OK] Dependencies installed.

echo.
echo ============================================
echo  Setup complete!
echo  Double-click run.bat to start the app.
echo ============================================
pause
