@echo off
setlocal enabledelayedexpansion

rem Electronic Cats
rem build_windows.bat - mirrors build-windows.yml step for step so the two
rem never drift apart the way catnip's did (see docs/PACKAGING_PLAN.md 1.8).
rem
rem Usage: build_windows.bat [version]
rem   version defaults to the contents of VERSION.
rem Distributed as-is; no warranty is given.

cd /d "%~dp0"

set VERSION=%1
if "%VERSION%"=="" (
    set /p VERSION=<VERSION
)

echo Building bombercat %VERSION% (.exe) ...

python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
pip install -r requirements.txt pyinstaller pywin32
if errorlevel 1 exit /b 1

pyinstaller bombercat_windows.spec
if errorlevel 1 exit /b 1

if not exist "dist\bombercat\bombercat.exe" (
    echo ERROR: dist\bombercat\bombercat.exe was not produced.
    exit /b 1
)
echo Built dist\bombercat\bombercat.exe

where ISCC.exe >nul 2>nul
if errorlevel 1 (
    echo Inno Setup [ISCC.exe] not found on PATH: skipping the installer step.
    echo Install it [choco install innosetup] to also produce bombercat-%VERSION%.exe.
    exit /b 0
)

powershell -NoProfile -Command "(Get-Content packaging\windows\bombercat_installer.iss) -replace '@VERSION@', '%VERSION%' | Set-Content packaging\windows\bombercat_installer.rendered.iss"
if errorlevel 1 exit /b 1

ISCC.exe packaging\windows\bombercat_installer.rendered.iss
if errorlevel 1 exit /b 1

move /Y dist\BomberCat-Setup.exe bombercat-%VERSION%.exe
echo Built bombercat-%VERSION%.exe
