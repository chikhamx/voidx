# Releasing voidx

This document describes the manual release flow for publishing voidx to PyPI
and npm.

## Package Names

- PyPI package: `voidx`
- npm package: `@voidx/cli`
- Installed CLI command: `voidx`

The Python package is the canonical implementation. The npm package is a thin
launcher that installs and runs the matching Python package version.

## Prerequisites

- Python 3.11+
- Node.js 16+
- npm account with access to the `@voidx` scope
- PyPI account with access to the `voidx` project
- Clean working tree, except ignored build outputs
- Version values aligned in:
  - `pyproject.toml`
  - `src/voidx/__init__.py`
  - `npm/package.json`

## Preflight

Run the full verification suite before publishing:

```bash
.venv/bin/python scripts/package.py --check-only
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/python -m pytest -q
npm --prefix npm run check
npm --prefix npm pack --dry-run
```

If `uv` needs a writable cache outside the home directory, run package builds
with:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache .venv/bin/python scripts/package.py --format all --clean
```

The build should produce:

```text
dist/voidx-<version>.tar.gz
dist/voidx-<version>-py3-none-any.whl
```

## Publish to PyPI

Build fresh artifacts:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache .venv/bin/python scripts/package.py --format all --clean
```

Upload with `twine` or an equivalent PyPI publishing tool:

```bash
.venv/bin/python -m twine upload dist/voidx-<version>.tar.gz dist/voidx-<version>-py3-none-any.whl
```

Verify a clean install:

```bash
python3.11 -m venv /tmp/voidx-pypi-smoke
/tmp/voidx-pypi-smoke/bin/python -m pip install --upgrade pip
/tmp/voidx-pypi-smoke/bin/python -m pip install voidx==<version>
/tmp/voidx-pypi-smoke/bin/voidx version
```

## Publish to npm

The npm package must be published after the matching Python package is
available on PyPI, because the npm launcher installs `voidx==<version>` on
first run.

Pack and inspect the npm package:

```bash
npm --prefix npm pack --dry-run
```

Publish:

```bash
npm publish npm --access public
```

Verify a clean global install:

```bash
npm install -g @voidx/cli@<version>
voidx version
```

The first npm-launched run creates a user-local Python virtual environment and
installs the matching PyPI package. Set `VOIDX_NPM_DEBUG=1` if bootstrap
details are needed.

## Post-Release Checks

Confirm both package managers report the released version:

```bash
python3.11 -m pip index versions voidx
npm view @voidx/cli version
```

Confirm the CLI starts from both install paths:

```bash
voidx version
voidx --help
```

## Failure Handling

- If PyPI publish fails before upload completes, fix the issue and rebuild.
- If npm publish fails before upload completes, fix the npm wrapper and retry.
- If PyPI succeeds but npm fails, do not change the Python version just for the
  npm retry. Fix npm packaging and publish the same version.
- If a bad release is published, publish a new patch version. Do not overwrite
  published artifacts.

## Notes

- Keep package tokens out of git, shell history, and logs.
- Do not publish npm before PyPI for the same version.
- The npm package name is scoped because the unscoped `voidx` npm name is not
  available.
