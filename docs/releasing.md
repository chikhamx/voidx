# Releasing voidx

This document describes the manual release flow for publishing voidx to PyPI
and npm.

## Package Names

- PyPI packages: `voidx` and `voidx-cli`
- npm package: `@chikhamx/voidx`
- Installed CLI command: `voidx`

The Python packages are the canonical implementation: `voidx` contains the core
runtime, while `voidx-cli` is an optional frontend for terminal-interactive mode.
The npm package is a thin launcher that installs and runs the matching Python
package version.

## Version Files

The canonical version source is `src/voidx/__init__.py` (`__version__`).
`pyproject.toml` reads it dynamically at build time; the remaining files hold
static copies that must stay in sync. Run the bump script to update all of them
from the single source:

```bash
./python.py scripts/bump_version.py <version>
```

| # | File | Field / Location | How it stays in sync |
|---|------|------------------|----------------------|
| 1 | `src/voidx/__init__.py` | `__version__ = "X.Y.Z"` | **Canonical source** — edit this (or let the bump script do it) |
| 2 | `tui/voidx_cli/__init__.py` | `__version__ = "X.Y.Z"` | Bump script, must match `voidx` |
| 3 | `tui/pyproject.toml` | `voidx==X.Y.Z` dependency pin | Bump script |
| 4 | `npm/package.json` | `"version": "X.Y.Z"` | Bump script |
| 5 | `scripts/install.sh` | `VERSION="${VOIDX_VERSION:-X.Y.Z}"` | Bump script |
| 6 | `scripts/install.ps1` | `$Version = ... else { "X.Y.Z" }` | Bump script |

### License

Both packages declare MIT license. The `LICENSE` file at repo root is
included in the PyPI sdist automatically; npm includes it via the default
file list.

| File | Field |
|------|-------|
| `LICENSE` | MIT full text (copyright chikhamx) |
| `npm/LICENSE` | Copy of root LICENSE — npm `files` field is relative to `npm/`, so a copy is needed |
| `pyproject.toml` | `license = {text = "MIT"}` |
| `npm/package.json` | `"license": "MIT"` |

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

## Recommended: Use `scripts/release.py`

The end-to-end release script handles the full flow — version sync, build,
PyPI upload, npm wheel bundling, and npm publish — in one command:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache ./python.py scripts/release.py
```

It ensures the freshly built `voidx_cli` wheel is copied into `npm/` before
publishing. **Always prefer this over manual step-by-step publishing** —
skipping it leaves a stale wheel in `npm/` and ships a broken npm package
(see Common Pitfalls below).

Use `--pypi-only` or `--npm-only` for partial releases, `--dry-run` to build
without publishing, `--skip-checks` to bypass metadata validation.

## Preflight

Run the full verification suite before publishing:

```bash
./python.py scripts/package.py --check-only
./python.py -m compileall -q src scripts tui
./python.py -m pytest -q
npm --prefix npm run check
npm pack ./npm --dry-run
```

If `uv` needs a writable cache outside the home directory, run package builds
with:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache ./python.py scripts/package.py --format all --clean --verify
```

The build produces both wheels:

```text
dist/voidx-<version>.tar.gz
dist/voidx-<version>-py3-none-any.whl
dist/voidx_cli-<version>.tar.gz
dist/voidx_cli-<version>-py3-none-any.whl
```

## Publish to PyPI

Build fresh artifacts and verify they install correctly:

```bash
UV_CACHE_DIR=/private/tmp/voidx-uv-cache ./python.py scripts/package.py --format all --clean --verify
```

The `--verify` flag creates a temporary venv, pip-installs both wheels, and
runs import verification. This proves the wheels are structurally sound before
upload.

Upload both wheels with `twine` or an equivalent publishing tool:

```bash
./python.py -m twine upload \
  dist/voidx-<version>.tar.gz \
  dist/voidx-<version>-py3-none-any.whl \
  dist/voidx_cli-<version>.tar.gz \
  dist/voidx_cli-<version>-py3-none-any.whl
```

Verify a clean install:

```bash
python3.11 -m venv /tmp/voidx-pypi-smoke
/tmp/voidx-pypi-smoke/bin/python -m pip install --upgrade pip
/tmp/voidx-pypi-smoke/bin/python -m pip install voidx==<version>
/tmp/voidx-pypi-smoke/bin/python -m pip install voidx-cli==<version>
/tmp/voidx-pypi-smoke/bin/voidx version
```

## Publish to npm

The npm package must be published after the matching Python packages are
available on PyPI, because the npm launcher installs `voidx-cli==<version>`
on first run.

> **⚠️ Do not run `npm publish` manually.** The npm `files` field includes
> `*.whl`, which means `npm pack` bundles whatever `voidx_cli-*.whl` exists
> in `npm/` at publish time. If you skip `scripts/release.py`, the stale
> wheel from the previous version stays in `npm/` and gets shipped — the
> postinstall then fails with `Bundled voidx_cli-<version>-py3-none-any.whl
> not found`. Always use `scripts/release.py`, which removes stale wheels
> and copies the freshly built one from `dist/`.

Pack and inspect the npm package:

The npm package must be published after the matching Python packages are
available on PyPI, because the npm launcher installs `voidx-cli==<version>`
on first run.

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
| Edit `__init__.py` but forget to run `bump_version.py` | `voidx_cli`, `npm/package.json`, and install scripts keep the old version |
| Forget to publish `voidx-cli` | Terminal UI (`voidx --web`) users fail with \"No frontend registered\" |
| Only bump package files, not install scripts | New users get the old version via `curl \| bash` |
| Run `npm publish` manually instead of `scripts/release.py` | `npm/` keeps the stale `voidx_cli-*.whl` from the previous version; postinstall fails with `Bundled ... not found` |

## Notes

- Keep package tokens out of git, shell history, and logs.
- Do not publish npm before PyPI for the same version.
- The npm package name is scoped because the unscoped `voidx` npm name is not
  available.
