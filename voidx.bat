@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0python.ps1" -c "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, 'tui'); from voidx.main import cli; cli()" %*
