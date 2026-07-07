@echo off
"%~dp0python.py" -c "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, 'tui'); from voidx.main import cli; cli()" %*
