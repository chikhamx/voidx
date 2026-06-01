#!/bin/bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'src'); from voidx.main import cli; cli()" "$@"
