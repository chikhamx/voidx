# voidx

voidx is a terminal AI coding agent built in Python.

## Install

Python users can install the canonical package from PyPI:

```bash
pip install voidx
voidx
```

Node users can install the npm launcher. The launcher requires Python 3.11+
on the machine and installs the matching Python package into an isolated
user-local virtual environment on first run:

```bash
npm install -g @voidx/cli
voidx
```

## Useful Commands

```bash
voidx version
voidx sessions
voidx -w /path/to/project
```

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python scripts/package.py --format all --clean
npm --prefix npm run check
```
