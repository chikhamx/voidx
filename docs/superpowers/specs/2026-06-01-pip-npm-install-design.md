# pip and npm install design

## Goal

Make voidx installable through both Python and Node package managers without
duplicating the application implementation.

Users should be able to run:

```bash
pip install voidx
voidx
```

or:

```bash
npm install -g @voidx/cli
voidx
```

The Python package remains the source of truth. The npm package is a thin
launcher that prepares and delegates to the Python CLI.

## Scope

In scope:

- Publishable Python package metadata for PyPI.
- Publishable npm wrapper package metadata.
- A Node-based `voidx` executable for npm installs.
- Python 3.11+ discovery and clear failure messages.
- Per-user isolated virtual environment bootstrap for npm installs.
- Version coupling between the npm package and the Python package.
- Packaging smoke tests.

Out of scope:

- Bundling Python into the npm package.
- Rewriting voidx in TypeScript or JavaScript.
- Auto-publishing to PyPI or npm from CI.
- Supporting Python versions below 3.11.

## Recommended Approach

Use PyPI as the canonical distribution and npm as a bootstrap wrapper.

The Python package already has the core pieces:

- `pyproject.toml` declares `name = "voidx"`.
- `pyproject.toml` declares `requires-python = ">=3.11"`.
- `[project.scripts]` exposes `voidx = "voidx.main:cli"`.
- `scripts/package.py` builds wheel and sdist artifacts.

The npm package should not contain application logic. It should contain:

- `package.json` with `bin.voidx`.
- A small executable script.
- Optional smoke-test fixtures.

When the npm-installed `voidx` command runs, it should:

1. Find a compatible Python executable.
2. Create a user-local venv if missing.
3. Install or upgrade `voidx==<npm package version>` into that venv.
4. Execute the venv's `voidx` command with all original CLI arguments.

This keeps install behavior familiar for Node users while preserving one
runtime implementation.

## Package Layout

Add a dedicated npm wrapper directory:

```text
npm/
  package.json
  bin/
    voidx.js
```

The repository root remains a Python project. The npm wrapper lives in `npm/`
so root-level Python packaging is not confused with Node packaging.

## Runtime Behavior

The npm launcher should use only Node built-ins:

- `child_process` for command probing and process forwarding.
- `fs` and `path` for venv paths.
- `os` for a stable user data directory.

Python discovery order:

1. `VOIDX_PYTHON` if set.
2. `python3`.
3. `python`.
4. Windows launcher `py -3.11` when available.

The selected interpreter must report `sys.version_info >= (3, 11)`.

The npm-managed venv path should be stable and user-local:

- macOS/Linux: `$XDG_DATA_HOME/voidx/npm-venv` or `~/.local/share/voidx/npm-venv`
- Windows: `%LOCALAPPDATA%\voidx\npm-venv`

The launcher should reinstall only when needed. A marker file can store the
installed Python package version. If it matches the npm package version, skip
the install step.

## User-Facing Errors

Failure messages should be short and actionable:

- No Python found:
  `voidx npm launcher requires Python 3.11+. Install Python or set VOIDX_PYTHON.`
- Python too old:
  `voidx requires Python 3.11+. Found Python X.Y at <path>.`
- pip/bootstrap failure:
  `Failed to install voidx==<version> into the npm-managed Python environment.`

Debug output should remain hidden by default and be enabled by an env var such
as `VOIDX_NPM_DEBUG=1`.

## Versioning

The npm package version should match `pyproject.toml` and `src/voidx/__init__.py`.

During packaging checks, fail if these versions differ:

- Python project version.
- Python runtime `__version__`.
- npm `package.json` version.

This avoids `npm install -g voidx@1.0.1` installing `voidx==1.0.0` from PyPI.

## Verification

Add focused checks:

- Python package builds wheel and sdist.
- npm package metadata is valid.
- npm launcher prints version through the Python CLI.
- npm launcher forwards CLI arguments.
- npm launcher fails clearly when a mocked Python is too old.

Manual smoke test:

```bash
python scripts/package.py --format all --clean
npm --prefix npm pack
```

After publishing the Python package to PyPI and the npm wrapper to npm:

```bash
npm install -g ./npm/voidx-cli-<version>.tgz
voidx version
```

## Release Flow

1. Update the Python version.
2. Update the npm package version to match.
3. Run tests and packaging checks.
4. Build Python wheel and sdist.
5. Publish Python package to PyPI.
6. Pack and publish npm wrapper.
7. Verify both install paths in a clean environment.
