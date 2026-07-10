# voidx

voidx is a terminal AI coding agent built in Python.

## Install

### One-line install (no Python or npm required)

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/install.ps1 | iex
```

The installer downloads a standalone Python runtime and sets up voidx in an
isolated environment — nothing else is needed on your machine.

### pip

```bash
python -m pip install voidx voidx-cli
voidx
```

### npm

```bash
npm install -g @chikhamx/voidx
voidx
```

## Upgrade

Upgrade both Python packages together so the core and terminal UI stay on the
same version:

```bash
python -m pip install --upgrade voidx voidx-cli
```

For npm installations, upgrade the npm package instead:

```bash
npm update -g @chikhamx/voidx
```

The one-line installers can be rerun to repair or upgrade their managed
environment.

### From source

```bash
git clone https://github.com/chikhamx/voidx.git
cd voidx
pip install -e .
voidx
```

### China / slow network

Set mirror environment variables before running any install method:

```bash
export VOIDX_PYTHON_MIRROR=https://npmmirror.com/mirrors/python-standalone
export VOIDX_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
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
