@echo off
setlocal
cd /d "%~dp0"

set "VOIDX_PYTHON=%LOCALAPPDATA%\voidx\venv\Scripts\python.exe"
set "VOIDX_WORKSPACE=%~dp0"
set "PYTHONPATH="

if not exist "%VOIDX_PYTHON%" (
    echo [error] Python not found: %VOIDX_PYTHON%
    echo         Please create a venv first: python -m venv .venv
    exit /b 1
)

echo Starting voidx desktop...
cd desktop
npm run dev
