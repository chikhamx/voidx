#!/usr/bin/env bash
# voidx installer — downloads a standalone Python, creates an isolated venv,
# and installs voidx. No Python, pip, or npm required.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash
#   # or:
#   bash install.sh
#
# Environment variables:
#   VOIDX_VERSION         — version to install (default: 1.1.1)
#   VOIDX_HOME            — install directory (default: ~/.local/share/voidx)
#   VOIDX_PYTHON_MIRROR   — mirror for python-build-standalone downloads
#   VOIDX_PIP_INDEX       — custom PyPI index URL
#   VOIDX_BIN_DIR         — directory for the voidx symlink (default: ~/.local/bin)

set -euo pipefail

VERSION="${VOIDX_VERSION:-2.0.4}"
PBS_TAG="20260602"
PBS_CPYTHON="3.12.13"
PBS_PYTHON_MAJOR="3.12"
PBS_RELEASE_BASE="https://github.com/astral-sh/python-build-standalone/releases/download"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${CYAN}  ℹ${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}  ✅${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}  ⚠${NC} %s\n" "$*"; }
err()   { printf "${RED}  ❌${NC} %s\n" "$*" >&2; }
step()  { printf "\n${BOLD}  [%s]${NC} %s\n" "$1" "$2"; }

# ── Platform detection ──────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

case "${OS}-${ARCH}" in
    Darwin-arm64)  PBS_TARGET="aarch64-apple-darwin" ;;
    Darwin-x86_64) PBS_TARGET="x86_64-apple-darwin" ;;
    Linux-aarch64) PBS_TARGET="aarch64-unknown-linux-gnu" ;;
    Linux-x86_64)  PBS_TARGET="x86_64-unknown-linux-gnu" ;;
    *)
        err "Unsupported platform: ${OS}-${ARCH}"
        err "voidx supports: macOS (x64/arm64), Linux (x64/arm64)"
        exit 1
        ;;
esac

PBS_FILENAME="cpython-${PBS_CPYTHON}+${PBS_TAG}-${PBS_TARGET}-install_only_stripped.tar.gz"
PBS_URL="${VOIDX_PYTHON_MIRROR:-${PBS_RELEASE_BASE}}/${PBS_TAG}/${PBS_FILENAME}"

# ── Paths ───────────────────────────────────────────────────────────────────
VOIDX_HOME="${VOIDX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/voidx}"
PYTHON_DIR="${VOIDX_HOME}/python"
VENV_DIR="${VOIDX_HOME}/venv"
BIN_DIR="${VOIDX_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
MARKER_PATH="${VENV_DIR}/.voidx-install-version"
MARKER="${VERSION}\n${PBS_TAG}\n${PBS_CPYTHON}\n"

BUNDLED_PYTHON="${PYTHON_DIR}/python/bin/python3"
VENV_PYTHON="${VENV_DIR}/bin/python"
VOIDX_BIN="${VENV_DIR}/bin/voidx"
VOIDX_LINK="${BIN_DIR}/voidx"

# ── Check if already installed ──────────────────────────────────────────────
if [ -f "${VOIDX_BIN}" ] && [ -f "${MARKER_PATH}" ]; then
    EXISTING=$(cat "${MARKER_PATH}" 2>/dev/null || echo "")
    if [ "${EXISTING}" = "$(printf '%b' "${MARKER}")" ]; then
        ok "voidx ${VERSION} already installed at ${VENV_DIR}"
        # Still ensure the symlink exists
        if [ ! -L "${VOIDX_LINK}" ] && [ ! -f "${VOIDX_LINK}" ]; then
            mkdir -p "${BIN_DIR}"
            ln -sf "${VOIDX_BIN}" "${VOIDX_LINK}"
            info "Created symlink: ${VOIDX_LINK} → ${VOIDX_BIN}"
        fi
        exit 0
    fi
fi

printf "\n${BOLD}🐍 Installing voidx ${VERSION}…${NC}\n"

# ── Step 1: Download Python ─────────────────────────────────────────────────
step "1/3" "Setting up Python runtime"

if [ -f "${BUNDLED_PYTHON}" ]; then
    ok "Using cached Python runtime"
else
    ARCHIVE_PATH="${PYTHON_DIR}/${PBS_FILENAME}"
    mkdir -p "${PYTHON_DIR}"

    if [ -f "${ARCHIVE_PATH}" ]; then
        ok "Using cached archive"
    else
        info "Downloading ${PBS_FILENAME}…"

        # Try up to 3 times with retries
        RETRIES=3
        DOWNLOADED=false
        for i in $(seq 1 "${RETRIES}"); do
            if curl -fsSL --connect-timeout 30 --max-time 300 -o "${ARCHIVE_PATH}.tmp" "${PBS_URL}"; then
                mv "${ARCHIVE_PATH}.tmp" "${ARCHIVE_PATH}"
                DOWNLOADED=true
                break
            else
                rm -f "${ARCHIVE_PATH}.tmp"
                if [ "${i}" -lt "${RETRIES}" ]; then
                    DELAY=$((2 ** i))
                    warn "Download attempt ${i}/${RETRIES} failed, retrying in ${DELAY}s…"
                    sleep "${DELAY}"
                fi
            fi
        done

        if [ "${DOWNLOADED}" = false ]; then
            err "Failed to download Python runtime after ${RETRIES} attempts"
            err ""
            err "This is usually a network issue. Try:"
            err "  1. Use a mirror: VOIDX_PYTHON_MIRROR=https://npmmirror.com/mirrors/python-standalone bash install.sh"
            err "  2. Retry: bash install.sh"
            err "  3. If you're in China, also set: VOIDX_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
            exit 1
        fi
    fi

    info "Extracting Python runtime…"
    tar -xzf "${ARCHIVE_PATH}" -C "${PYTHON_DIR}"
    rm -f "${ARCHIVE_PATH}"
    ok "Python runtime ready"
fi

# ── Step 2: Create venv ────────────────────────────────────────────────────
step "2/3" "Creating virtual environment"

# If venv exists but is corrupted, rebuild
if [ -d "${VENV_DIR}" ] && [ ! -f "${VENV_PYTHON}" ]; then
    warn "Existing venv is corrupted, rebuilding…"
    rm -rf "${VENV_DIR}"
fi

if [ ! -f "${VENV_PYTHON}" ]; then
    "${BUNDLED_PYTHON}" -m venv "${VENV_DIR}"
    if [ $? -ne 0 ]; then
        err "Failed to create virtual environment"
        exit 1
    fi
fi

# Upgrade pip to avoid resolver bugs
if ! "${VENV_PYTHON}" -m pip install --upgrade pip --no-cache-dir >/dev/null 2>&1; then
    warn "Failed to upgrade pip, continuing with current version"
fi

ok "Virtual environment ready"

# ── Step 3: Install voidx ──────────────────────────────────────────────────
step "3/3" "Installing voidx ${VERSION}"

PIP_ARGS=("-m" "pip" "install" "--upgrade" "--no-cache-dir" "--progress-bar" "on")

if [ -n "${VOIDX_PIP_INDEX:-}" ]; then
    PIP_ARGS+=("-i" "${VOIDX_PIP_INDEX}")
    # Extract hostname for --trusted-host
    PIP_HOST=$(echo "${VOIDX_PIP_INDEX}" | sed -E 's|https?://([^/]+).*|\1|')
    PIP_ARGS+=("--trusted-host" "${PIP_HOST}")
fi

PIP_ARGS+=("voidx==${VERSION}")

export PIP_NO_INPUT=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

if "${VENV_PYTHON}" "${PIP_ARGS[@]}"; then
    ok "voidx ${VERSION} installed"
else
    err "pip install failed"
    err ""
    err "This is usually a network issue. Try:"
    err "  1. Use a PyPI mirror: VOIDX_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh"
    err "  2. Retry: bash install.sh"
    exit 1
fi

# ── Create symlink ─────────────────────────────────────────────────────────
mkdir -p "${BIN_DIR}"
ln -sf "${VOIDX_BIN}" "${VOIDX_LINK}"

# ── Write marker ────────────────────────────────────────────────────────────
printf '%b' "${MARKER}" > "${MARKER_PATH}"

# ── Done ────────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}✅ voidx ${VERSION} installed!${NC}\n\n"

# Check if BIN_DIR is in PATH
if ! echo ":${PATH}:" | grep -q ":${BIN_DIR}:"; then
    warn "${BIN_DIR} is not in your PATH"
    info "Add it by running:"
    printf "    echo 'export PATH=\"%s:\$PATH\"' >> ~/.bashrc\n" "${BIN_DIR}"
    printf "    source ~/.bashrc\n\n"
    info "Or just run voidx directly:"
    printf "    %s\n\n" "${VOIDX_BIN}"
fi

info "Run: voidx"
