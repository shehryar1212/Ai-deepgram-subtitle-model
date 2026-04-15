@echo off
chcp 65001 >nul
title Live Subtitles - Setup

echo ============================================
echo  Live Subtitles - First-Time Setup
echo ============================================
echo.

:: -- Enable Windows Long Path support (required for deepgram-sdk) ---------
for /f "tokens=3" %%v in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2^>nul') do set LONGPATH=%%v
if not "%LONGPATH%"=="0x1" (
    echo [INFO] Enabling Windows Long Path support...
    echo        This requires administrator privileges.
    echo.
    powershell -NoProfile -Command "Start-Process powershell -ArgumentList '-NoProfile -Command ""Set-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1; Write-Host Done""' -Verb RunAs -Wait" >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo.
        echo WARNING: Could not enable Long Path support automatically.
        echo          If installation fails, please run this as Administrator:
        echo          PowerShell: Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1
        echo.
    ) else (
        echo [OK] Long Path support enabled.
    )
)


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
venv\Scripts\python.exe -m pip install --upgrade pip -q
venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Dependency install failed. See errors above.
    echo.
    echo If you see a "Long Path" error, run PowerShell as Administrator and run:
    echo   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1
    echo Then re-run this setup.
    pause & exit /b 1
)
echo [OK] Dependencies installed.

echo.
echo ============================================
echo  Setup complete!
echo  Double-click run.bat to start the app.
echo ============================================
pause
