#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
VERSION="$("$PYTHON" - <<'PY'
import tomllib
print(tomllib.loads(open("pyproject.toml", "rb").read())["project"]["version"])
PY
)"
NPM_PACKAGE="$("$PYTHON" - <<'PY'
import json
print(json.load(open("npm/package.json"))["name"])
PY
)"

echo "==> Releasing voidx ${VERSION}"

if [[ -n "$(git status --short)" ]]; then
  echo "Working tree is not clean. Commit or stash changes before publishing." >&2
  git status --short >&2
  exit 1
fi

if [[ -z "${TWINE_USERNAME:-}" || -z "${TWINE_PASSWORD:-}" ]]; then
  echo "Set PyPI credentials first:" >&2
  echo "  export TWINE_USERNAME=__token__" >&2
  echo "  export TWINE_PASSWORD='pypi-...'" >&2
  exit 1
fi

if ! "$PYTHON" -m twine --version >/dev/null 2>&1; then
  echo "twine is not installed in ${PYTHON}. Install with: uv pip install twine" >&2
  exit 1
fi

echo "==> Checking registry versions"
if VERSION="$VERSION" "$PYTHON" - <<'PY'
import json, os, sys, urllib.request
version = os.environ["VERSION"]
data = json.load(urllib.request.urlopen("https://pypi.org/pypi/voidx/json", timeout=20))
sys.exit(0 if version in data.get("releases", {}) else 1)
PY
then
  echo "PyPI voidx ${VERSION} already exists; refusing to overwrite." >&2
  exit 1
fi

if npm view "${NPM_PACKAGE}@${VERSION}" version >/dev/null 2>&1; then
  echo "npm ${NPM_PACKAGE}@${VERSION} already exists; refusing to overwrite." >&2
  exit 1
fi

echo "==> Running preflight"
"$PYTHON" scripts/package.py --check-only
"$PYTHON" -m compileall -q src scripts tests
"$PYTHON" -m pytest -q
npm --prefix npm run check
npm pack ./npm --dry-run

echo "==> Building Python artifacts"
UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/voidx-uv-cache}" \
  "$PYTHON" scripts/package.py --format all --clean

echo "==> Checking Python artifacts"
test -f "dist/voidx-${VERSION}.tar.gz"
test -f "dist/voidx-${VERSION}-py3-none-any.whl"
if "$PYTHON" -m zipfile -l "dist/voidx-${VERSION}-py3-none-any.whl" \
  | grep -E 'voidx/ui/(app.py|app_components)' >/dev/null; then
  echo "Wheel contains removed prompt TUI files; aborting." >&2
  exit 1
fi
if tar -tzf "dist/voidx-${VERSION}.tar.gz" \
  | grep -E "voidx-${VERSION}/src/voidx/ui/(app.py|app_components)" >/dev/null; then
  echo "sdist contains removed prompt TUI files; aborting." >&2
  exit 1
fi

echo "==> Uploading to PyPI"
"$PYTHON" -m twine upload --non-interactive \
  "dist/voidx-${VERSION}.tar.gz" \
  "dist/voidx-${VERSION}-py3-none-any.whl"

echo "==> Publishing to npm"
npm publish ./npm --access public

echo "==> Verifying published versions"
"$PYTHON" - <<PY
import json, urllib.request
data = json.load(urllib.request.urlopen("https://pypi.org/pypi/voidx/json", timeout=20))
print("PyPI:", data["info"]["version"])
PY
npm view "${NPM_PACKAGE}" version

echo "Release ${VERSION} complete."
