@echo off
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); from voidx.main import cli; cli()" %*
