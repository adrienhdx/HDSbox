@echo off
REM =============================================================================
REM S3Sync - Windows Build Script
REM =============================================================================
REM Builds the S3Sync application as a Windows executable
REM The resulting app runs as a system tray application
REM
REM Usage:
REM   build_windows.bat          - Production build
REM   build_windows.bat --clean  - Clean build directories
REM =============================================================================

setlocal EnableDelayedExpansion

REM Get the directory where this script is located
set SCRIPT_DIR=%~dp0
REM Project root is the parent directory
for %%I in ("%SCRIPT_DIR%\..") do set PROJECT_ROOT=%%~fI

REM Configuration
set APP_NAME=S3Sync
set VERSION=1.0.0
set BUILD_DIR=%PROJECT_ROOT%\build
set DIST_DIR=%PROJECT_ROOT%\dist

REM Parse arguments
set CLEAN_ONLY=false
if "%1"=="--clean" set CLEAN_ONLY=true
if "%1"=="-c" set CLEAN_ONLY=true
if "%1"=="--help" goto :help
if "%1"=="-h" goto :help

REM Clean function
if "%CLEAN_ONLY%"=="true" (
    echo [INFO] Cleaning build directories...
    if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
    if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
    if exist "*.egg-info" rmdir /s /q "*.egg-info"
    echo [SUCCESS] Build directories cleaned.
    goto :end
)

REM Header
echo.
echo ==============================================
echo   %APP_NAME% v%VERSION% - Windows Build
echo ==============================================
echo.

REM Check Python version
echo [INFO] Checking Python version...
for /f "tokens=2 delims= " %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [SUCCESS] Python %PYTHON_VERSION% detected.

REM Check for required files
echo [INFO] Checking required files...

if not exist "%PROJECT_ROOT%\main.py" (
    echo [ERROR] main.py not found in project root.
    exit /b 1
)

if not exist "%SCRIPT_DIR%s3sync.spec" (
    echo [ERROR] s3sync.spec not found.
    exit /b 1
)

REM Check for icon
if not exist "%PROJECT_ROOT%\icon.ico" (
    echo [WARNING] icon.ico not found. App will use default icon.
    echo [INFO] To add a custom icon, place icon.ico in the project root.
)

REM Clean previous builds
echo [INFO] Cleaning previous builds...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"

REM Install build dependencies
echo [INFO] Installing build dependencies...
pip install -q pyinstaller>=6.0.0

REM Change to project root for building
cd /d "%PROJECT_ROOT%"

REM Run PyInstaller
echo [INFO] Building production version...
echo [INFO] This may take a few minutes...
python -m PyInstaller "%SCRIPT_DIR%s3sync.spec" --noconfirm

REM Check if build succeeded
if not exist "%DIST_DIR%\%APP_NAME%\%APP_NAME%.exe" (
    echo [ERROR] Build failed. Check the output above for errors.
    exit /b 1
)

REM Success!
echo.
echo [SUCCESS] Build completed successfully!
echo.
echo ==============================================
echo   Build Summary
echo ==============================================
echo   Executable: %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe
echo ==============================================
echo.
echo [INFO] To run the app:
echo   %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe
echo.
echo [INFO] To create an installer, consider using NSIS or Inno Setup.
echo.
goto :end

:help
echo Usage: build_windows.bat [OPTIONS]
echo.
echo Options:
echo   --clean, -c   Clean build directories only
echo   --help, -h    Show this help message
goto :end

:end
endlocal
