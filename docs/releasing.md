# Releasing voidx

This document describes the manual release flow for publishing voidx to PyPI
and npm.

## Package Names

- PyPI package: `voidx`
- npm package: `@chikhamx/voidx`
- Installed CLI command: `voidx`

The Python package is the canonical implementation. The npm package is a thin
launcher that installs and runs the matching Python package version.

## Version Files

Bump the version in **all 5 files** before building. Missing any one causes
breakage (see Common Pitfalls below).

| # | File | Field / Location | Notes |
|---|------|------------------|-------|
| 1 | `pyproject.toml` | `version = "X.Y.Z"` | Python package metadata, build entry point |
| 2 | `src/voidx/__init__.py` | `__version__ = "X.Y.Z"` | Runtime version, read by `voidx --version` |
| 3 | `npm/package.json` | `"version": "X.Y.Z"` | npm package metadata |
| 4 | `scripts/install.sh` | `VERSION="${VOIDX_VERSION:-X.Y.Z}"` | Bash installer default version |
| 5 | `scripts/install.ps1` | `$Version = ... else { "X.Y.Z" }` | PowerShell installer default version |

## Version Policy

- **Patch** (`X.Y.Z+1`): bug fixes, minor improvements
- **Minor** (`X.Y+1.0`): new features, skills, tools
- **Major** (`X+1.0.0`): architecture changes, breaking changes

## Prerequisites

- Python 3.11+
- Node.js 16+
- npm account with access to the `@chikhamx` scope
- PyPI account with access to the `voidx` project
- Clean working tree, except ignored build outputs

## Preflight

Run the full verification suite before publishing:

```bash
.venv/bin/python scripts/package.py --check-only
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/python -m pytest -q
npm --prefix npm run check
npm pack ./npm --dry-run
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
npm pack ./npm --dry-run
```

Publish:

```bash
npm publish ./npm --access public
```

Verify a clean global install:

```bash
npm install -g @chikhamx/voidx@<version>
voidx version
```

The first npm-launched run creates a user-local Python virtual environment and
installs the matching PyPI package. Set `VOIDX_NPM_DEBUG=1` if bootstrap
details are needed.

## Git Tag

After both packages are published:

```bash
git add -A
git commit -m "chore: bump version to <version>"
git tag v<version>
git push && git push origin v<version>
```

## Post-Release Checks

Confirm both package managers report the released version:

```bash
python3.11 -m pip index versions voidx
npm view @chikhamx/voidx version
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

## Common Pitfalls

| Mistake | Consequence |
|---------|-------------|
| Only bump `pyproject.toml`, not `__init__.py` | `voidx --version` shows old version |
| Only bump Python files, not `npm/package.json` | `scripts/package.py` build fails (version mismatch check) |
| Only bump package files, not install scripts | New users get the old version via `curl \| bash` |

## Notes

- Keep package tokens out of git, shell history, and logs.
- Do not publish npm before PyPI for the same version.
- The npm package name is scoped because the unscoped `voidx` npm name is not
  available.
