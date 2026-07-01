#!/usr/bin/env bash
# voidx Python launcher — locates the venv Python under VOIDX_HOME and forwards
# all arguments. Resolves the same install directory as scripts/install.sh.
#
# Usage:
#   ./python.sh -m pytest tests/ -v
#   ./python.sh scripts/package.py
#
# Environment:
#   VOIDX_HOME — install directory (default: ${XDG_DATA_HOME:-$HOME/.local/share}/voidx)

set -euo pipefail

VOIDX_HOME="${VOIDX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/voidx}"
PY="${VOIDX_HOME}/venv/bin/python"

if [ ! -x "$PY" ]; then
  printf '\033[0;31m  ❌\033[0m voidx venv Python not found at %s\n' "$PY" >&2
  printf '     Run scripts/install.sh to create it, or set VOIDX_HOME to your install directory.\n' >&2
  exit 1
fi

exec "$PY" "$@"
