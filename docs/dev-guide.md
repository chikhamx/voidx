# voidx Developer Guide

## Exception Handling

- Avoid `except Exception: pass`.
- Use the narrowest practical exception type for fallback paths.
- Add debug logging for silent fallback paths unless the exception is expected at very high frequency.
- Preserve cancellation semantics: do not catch `BaseException`; re-raise `asyncio.CancelledError` when caught explicitly.
- Include enough context in log messages to identify the file, provider, backend, or subsystem involved.

## Runtime Environment

- Use `./python.py` as the Python entry point on Linux/macOS/Windows. It locates the voidx venv under `VOIDX_HOME` and forwards all arguments to the venv Python.
- Install directory (`VOIDX_HOME`) defaults:
  - Unix: `${XDG_DATA_HOME:-$HOME/.local/share}/voidx`
  - Windows: `$env:LOCALAPPDATA\voidx`
  - Override by setting the `VOIDX_HOME` environment variable.
- venv Python path inside `VOIDX_HOME`:
  - Unix: `venv/bin/python`
  - Windows: `venv\Scripts\python.exe`
- If the venv is missing, `./python.py` prints an error pointing to `scripts/install.sh` or `scripts/install.ps1`. Run the installer to create it.
