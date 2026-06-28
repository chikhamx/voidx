# Releasing voidx

This document describes the manual release flow for publishing voidx to PyPI
and npm. The script `voidx_publish.sh` automates the build, validation, and
publish steps below; this document additionally covers version bumps, git tags,
and failure handling that the script does not perform.

## Package Names

- PyPI package: `voidx`
- npm package: `@chikhamx/voidx`
- Installed CLI command: `voidx`

The Python package is the canonical implementation. The npm package is a thin
launcher that installs and runs the matching Python package version.

## Version Files

The canonical version source is `src/voidx/__init__.py` (`__version__`).
`pyproject.toml` reads it dynamically at build time; the remaining files hold
static copies that must stay in sync. Run the bump script to update all of them
from the single source:

```bash
./python.sh scripts/bump_version.py <version>
```

| # | File | Field / Location | How it stays in sync |
|---|------|------------------|----------------------|
| 1 | `src/voidx/__init__.py` | `__version__ = "X.Y.Z"` | **Canonical source** — edit this (or let the bump script do it) |
| 2 | `pyproject.toml` | `dynamic = ["version"]` | Dynamic via `[tool.setuptools.dynamic] version = {attr = "voidx.__version__"}` |
| 3 | `npm/package.json` | `"version": "X.Y.Z"` | Bump script |
| 4 | `scripts/install.sh` | `VERSION="${VOIDX_VERSION:-X.Y.Z}"` | Bump script |
| 5 | `scripts/install.ps1` | `$Version = ... else { "X.Y.Z" }` | Bump script |

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
./python.sh scripts/package.py --check-only
./python.sh -m compileall -q src scripts tests
./python.sh -m pytest -q
npm --prefix npm run check
npm pack ./npm --dry-run
```

If `uv` needs a writable cache outside the home directory, run package builds
with:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache ./python.sh scripts/package.py --format all --clean
```

The build should produce:

```text
dist/voidx-<version>.tar.gz
dist/voidx-<version>-py3-none-any.whl
```

## Publish to PyPI

Build fresh artifacts:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache ./python.sh scripts/package.py --format all --clean
```

Upload with `twine` or an equivalent PyPI publishing tool:

```bash
./python.sh -m twine upload dist/voidx-<version>.tar.gz dist/voidx-<version>-py3-none-any.whl
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
| Edit `__init__.py` but forget to run `bump_version.py` | `npm/package.json` and install scripts keep the old version |
| Only bump Python files, not `npm/package.json` | `scripts/package.py` build fails (version mismatch check) |
| Only bump package files, not install scripts | New users get the old version via `curl \| bash` |

## Notes

- Keep package tokens out of git, shell history, and logs.
- Do not publish npm before PyPI for the same version.
- The npm package name is scoped because the unscoped `voidx` npm name is not
  available.
