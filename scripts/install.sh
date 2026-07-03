#!/usr/bin/env bash
# voidx installer — prefers npm when available, falls back to PBS+venv+pip.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash
#   # or:
#   bash install.sh
#
# Environment variables:
#   VOIDX_VERSION         — version to install (default: see VERSION below)
#   VOIDX_HOME            — install directory (default: ~/.local/share/voidx)
#   VOIDX_PYTHON_MIRROR   — mirror for python-build-standalone downloads (fallback only)
#   VOIDX_PIP_INDEX       — custom PyPI index URL (fallback only)
#   VOIDX_BIN_DIR         — directory for the voidx symlink (default: ~/.local/bin)
#   VOIDX_SKIP_NPM        — set to 1 to skip npm and force PBS+venv+pip fallback

set -euo pipefail

VERSION="${VOIDX_VERSION:-3.4.4}"
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

# ── Legacy cleanup ──────────────────────────────────────────────────────────
# Remove voidx installed via system Python (pip/pipx) from v1.x era.
# Only runs if system Python is present — if no Python, there's nothing to clean.
_cleanup_legacy() {
    # Skip entirely if no system Python exists
    if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
        return
    fi

    # pip
    for cmd in pip3 pip; do
        if command -v "$cmd" &>/dev/null; then
            local result
            result=$("$cmd" show voidx 2>/dev/null || true)
            if [ -n "$result" ]; then
                local version
                version=$(echo "$result" | grep "^Version:" | awk '{print $2}')
                if [ -n "$version" ]; then
                    warn "发现 pip 安装的旧版 voidx ${version}（${cmd}），正在卸载…"
                    if "$cmd" uninstall voidx -y 2>/dev/null || true; then
                        ok "已卸载 pip 安装的 voidx（${cmd}）"
                    else
                        err "卸载失败，请手动执行: ${cmd} uninstall voidx"
                    fi
                fi
            fi
        fi
    done

    # pipx
    if command -v pipx &>/dev/null; then
        if pipx list 2>/dev/null | grep -q "voidx"; then
            warn "发现 pipx 安装的旧版 voidx，正在卸载…"
            if pipx uninstall voidx 2>/dev/null || true; then
                ok "已卸载 pipx 安装的 voidx"
            else
                err "卸载失败，请手动执行: pipx uninstall voidx"
            fi
        fi
    fi

    # Old npm-venv directory (hardcoded default path — npm also defaults to ~/.local/share)
    local old_npm_venv="${HOME}/.local/share/voidx/npm-venv"
    if [ -d "${old_npm_venv}" ]; then
        warn "发现旧版 npm-venv 目录，正在删除…"
        rm -rf "${old_npm_venv}"
        ok "已删除旧版 npm-venv 目录"
    fi

    # Symlinks pointing to system Python
    for link_path in "${HOME}/.local/bin/voidx" "/usr/local/bin/voidx"; do
        if [ ! -e "$link_path" ]; then
            continue
        fi
        # Symlink pointing to a previous voidx venv install
        if [ -L "$link_path" ]; then
            local target
            target=$(readlink "$link_path" 2>/dev/null || echo "")
            case "$target" in
                */site-packages/*|*/dist-packages/*|*/.local/pipx/*|*/pipx/venvs/*)
                    warn "发现旧版符号链接: ${link_path} → ${target}，正在删除…"
                    rm -f "$link_path"
                    ok "已删除旧版符号链接: ${link_path}"
                    ;;
                */share/voidx/npm-venv/bin/voidx)
                    warn "发现旧版安装脚本创建的符号链接: ${link_path} → ${target}，正在删除…"
                    rm -f "$link_path"
                    ok "已删除旧版符号链接: ${link_path}"
                    ;;
            esac
        fi
        # Dangling symlink
        if [ -L "$link_path" ] && [ ! -e "$link_path" ]; then
            rm -f "$link_path"
            ok "已删除悬空符号链接: ${link_path}"
        fi
    done
}

_cleanup_legacy

# ── Prerequisites ────────────────────────────────────────────────────────────
if ! command -v curl &>/dev/null; then
    err "curl is required but not found. Please install curl first."
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════════════
# npm installation path
# ══════════════════════════════════════════════════════════════════════════════
_install_via_npm() {
    step "npm" "正在通过 npm 安装 voidx ${VERSION}…"

    # Remove old symlink-based install at ~/.local/bin/voidx that points to venv
    # (npm uses its own launcher, not a symlink to the venv)
    local old_link="${HOME}/.local/bin/voidx"
    if [ -L "${old_link}" ]; then
        local target
        target=$(readlink "${old_link}" 2>/dev/null || echo "")
        case "${target}" in
            */share/voidx/venv/bin/voidx)
                warn "正在删除旧版符号链接: ${old_link} → ${target}"
                rm -f "${old_link}"
                ok "已删除旧版符号链接"
                ;;
        esac
    fi

    # Run npm install
    if ! npm install -g "@chikhamx/voidx@${VERSION}"; then
        err "npm install 失败，正在回退到直接安装…"
        return 1
    fi

    # Find the npm-installed voidx binary
    local npm_bin_dir
    npm_bin_dir="$(npm prefix -g 2>/dev/null)/bin"

    if [ ! -x "${npm_bin_dir}/voidx" ]; then
        # Try alternative detection
        npm_bin_dir="$(npm bin -g 2>/dev/null || true)"
        if [ ! -x "${npm_bin_dir}/voidx" ]; then
            err "npm 安装成功但未找到 voidx 二进制文件，正在回退到直接安装…"
            return 1
        fi
    fi

    ok "npm 安装完成: ${npm_bin_dir}/voidx"

    # Ensure npm global bin dir is in PATH
    if ! echo ":${PATH}:" | grep -q ":${npm_bin_dir}:"; then
        export PATH="${npm_bin_dir}:${PATH}"

        SHELL_NAME=$(basename "${SHELL:-/bin/bash}")
        PROFILE_FILE=""
        case "${SHELL_NAME}" in
            zsh)  PROFILE_FILE="${HOME}/.zshrc" ;;
            bash) PROFILE_FILE="${HOME}/.bashrc" ;;
            *)    PROFILE_FILE="${HOME}/.profile" ;;
        esac

        EXPORT_LINE="export PATH=\"${npm_bin_dir}:\$PATH\""
        if [ -f "${PROFILE_FILE}" ] && grep -qF "${EXPORT_LINE}" "${PROFILE_FILE}" 2>/dev/null; then
            : # already present
        else
            printf '\n%s\n' "${EXPORT_LINE}" >> "${PROFILE_FILE}"
            info "已将 ${npm_bin_dir} 添加到 ${PROFILE_FILE}"
        fi
    fi

    # Verify installation
    local actual_version
    actual_version=$(voidx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [ -n "${actual_version}" ]; then
        ok "版本验证通过: voidx ${actual_version}"
    else
        warn "无法验证 voidx 版本，请确认 PATH 中包含 ${npm_bin_dir}"
    fi

    printf "\n${GREEN}${BOLD}✅ voidx ${VERSION} installed via npm!${NC}\n\n"
    info "Run: voidx"
    exit 0
}

# ── Check if npm installation is available ──────────────────────────────────
if [ "${VOIDX_SKIP_NPM:-0}" != "1" ] && command -v npm &>/dev/null; then
    # Check if npm-installed voidx already reports the correct version
    EXISTING_NPM_VERSION=$(voidx --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [ "${EXISTING_NPM_VERSION}" = "${VERSION}" ]; then
        ok "voidx ${VERSION} 已通过 npm 安装"
        exit 0
    fi

    # Try npm installation; fall back to PBS on failure
    if ! _install_via_npm; then
        warn "npm 安装失败，正在使用直接安装方式…"
    else
        # npm install succeeded and already exited 0
        # This line is unreachable but clarifies intent
        exit 0
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# Fallback: PBS + venv + pip direct installation
# ══════════════════════════════════════════════════════════════════════════════

# ── Ensure PATH and remove conflicting voidx ──────────────────────────────
_ensure_path_and_cleanup() {
    # Add BIN_DIR to PATH if missing
    if ! echo ":${PATH}:" | grep -q ":${BIN_DIR}:"; then
        export PATH="${BIN_DIR}:${PATH}"

        SHELL_NAME=$(basename "${SHELL:-/bin/bash}")
        PROFILE_FILE=""
        case "${SHELL_NAME}" in
            zsh)  PROFILE_FILE="${HOME}/.zshrc" ;;
            bash) PROFILE_FILE="${HOME}/.bashrc" ;;
            *)    PROFILE_FILE="${HOME}/.profile" ;;
        esac

        EXPORT_LINE="export PATH=\"${BIN_DIR}:\$PATH\""
        if [ -f "${PROFILE_FILE}" ] && grep -qF "${EXPORT_LINE}" "${PROFILE_FILE}" 2>/dev/null; then
            : # already present
        else
            printf '\n%s\n' "${EXPORT_LINE}" >> "${PROFILE_FILE}"
            info "已将 ${BIN_DIR} 添加到 ${PROFILE_FILE}"
        fi
    fi

    # Remove conflicting voidx from PATH
    FIRST_VOIDX=$(which voidx 2>/dev/null || true)
    if [ -n "${FIRST_VOIDX}" ] && [ "${FIRST_VOIDX}" != "${VOIDX_LINK}" ]; then
        FIRST_REAL=$(readlink -f "${FIRST_VOIDX}" 2>/dev/null || echo "${FIRST_VOIDX}")
        warn "发现旧版 voidx: ${FIRST_VOIDX} → ${FIRST_REAL}"

        # Homebrew
        if command -v brew &>/dev/null; then
            BREW_VOIDX=$(brew list voidx 2>/dev/null || true)
            if [ -n "${BREW_VOIDX}" ]; then
                warn "正在卸载 Homebrew 安装的 voidx…"
                if brew uninstall voidx 2>/dev/null; then
                    ok "已卸载 Homebrew 安装的 voidx"
                else
                    err "卸载失败，请手动执行: brew uninstall voidx"
                fi
            fi
        fi

        # npm global
        if command -v npm &>/dev/null; then
            NPM_VOIDX=$(npm list -g @chikhamx/voidx 2>/dev/null | grep '@chikhamx/voidx@' || true)
            if [ -n "${NPM_VOIDX}" ]; then
                warn "正在卸载 npm 安装的 voidx…"
                if npm uninstall -g @chikhamx/voidx 2>/dev/null; then
                    ok "已卸载 npm 安装的 voidx"
                else
                    err "卸载失败，请手动执行: npm uninstall -g @chikhamx/voidx"
                fi
            fi
        fi

        # Other locations
        case "${FIRST_VOIDX}" in
            /usr/local/bin/voidx|/opt/homebrew/bin/voidx)
                if [ -w "${FIRST_VOIDX}" ] || [ -w "$(dirname "${FIRST_VOIDX}")" ]; then
                    warn "正在删除旧版: ${FIRST_VOIDX}"
                    rm -f "${FIRST_VOIDX}"
                    ok "已删除 ${FIRST_VOIDX}"
                else
                    warn "无权限删除 ${FIRST_VOIDX}，请手动执行: sudo rm ${FIRST_VOIDX}"
                fi
                ;;
        esac
    fi

    # Verify installation
    ACTUAL_VERSION=$("${VOIDX_BIN}" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [ -n "${ACTUAL_VERSION}" ] && [ "${ACTUAL_VERSION}" != "${VERSION}" ]; then
        warn "安装的版本 (${ACTUAL_VERSION}) 与预期版本 (${VERSION}) 不一致"
        warn "可能存在其他 voidx 安装，请检查:"
        FOUND_OTHER=false
        while IFS= read -r p; do
            REAL=$(readlink -f "$p" 2>/dev/null || echo "$p")
            if [ "$REAL" != "${VOIDX_BIN}" ]; then
                warn "  ${p} → ${REAL}"
                FOUND_OTHER=true
            fi
        done < <(which -a voidx 2>/dev/null || true)
        if [ "${FOUND_OTHER}" = true ]; then
            info "请删除上述旧版 voidx，或确保 ${BIN_DIR} 在 PATH 最前面"
        fi
    elif [ -n "${ACTUAL_VERSION}" ]; then
        ok "版本验证通过: voidx ${ACTUAL_VERSION}"
    fi
}

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
        # Ensure the symlink points to the right place
        if [ ! -L "${VOIDX_LINK}" ] || [ "$(readlink "${VOIDX_LINK}")" != "${VOIDX_BIN}" ]; then
            mkdir -p "${BIN_DIR}"
            ln -sf "${VOIDX_BIN}" "${VOIDX_LINK}"
            info "Updated symlink: ${VOIDX_LINK} → ${VOIDX_BIN}"
        fi
        # Even on reinstall, ensure PATH and no conflicting versions
        _ensure_path_and_cleanup
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
            if curl -fsSL --connect-timeout 30 --max-time 600 -o "${ARCHIVE_PATH}.tmp" "${PBS_URL}"; then
                DOWNLOAD_SIZE=$(stat -f%z "${ARCHIVE_PATH}.tmp" 2>/dev/null || stat -c%s "${ARCHIVE_PATH}.tmp" 2>/dev/null || echo 0)
                if [ "${DOWNLOAD_SIZE}" -lt 1048576 ]; then
                    rm -f "${ARCHIVE_PATH}.tmp"
                    warn "Downloaded file is only ${DOWNLOAD_SIZE} bytes — likely incomplete, retrying…"
                    continue
                fi
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
    if ! tar -xzf "${ARCHIVE_PATH}" -C "${PYTHON_DIR}"; then
        rm -f "${ARCHIVE_PATH}"
        err "Failed to extract Python runtime — the downloaded archive may be incomplete."
        err "Re-run the installer to retry."
        exit 1
    fi
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

# cd to VENV_DIR so pip doesn't discover a local pyproject.toml in the
# current directory and install from source instead of PyPI.
cd "${VENV_DIR}"

# Clean pip leftover directories (~-prefixed) from interrupted installs.
# pip's AdjacentTempDirectory leaves folders like ~oidx.dist-info if an
# install is interrupted. On the next run pip prints
# "Ignoring invalid distribution" warnings for each leftover.
SITE_PACKAGES="${VENV_DIR}/lib/python${PBS_PYTHON_MAJOR}/site-packages"
if [ -d "${SITE_PACKAGES}" ]; then
    find "${SITE_PACKAGES}" -maxdepth 1 -name '~*' -exec rm -rf {} + 2>/dev/null || true
fi

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

_ensure_path_and_cleanup

info "Run: voidx"
