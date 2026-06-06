"""Static LSP detector metadata."""

from __future__ import annotations

from voidx.lsp.schema import LspServerConfig


# ---------------------------------------------------------------------------
# Per-language default server identities
# ---------------------------------------------------------------------------

LANGUAGE_DEFAULTS: dict[str, LspServerConfig] = {
    "python": LspServerConfig(
        language="python",
        command="pyright-langserver",
        args=["--stdio"],
        extensions=[".py"],
    ),
    "typescript": LspServerConfig(
        language="typescript",
        command="typescript-language-server",
        args=["--stdio"],
        extensions=[".ts", ".tsx"],
    ),
    "javascript": LspServerConfig(
        language="javascript",
        command="typescript-language-server",
        args=["--stdio"],
        extensions=[".js", ".jsx"],
    ),
    "go": LspServerConfig(
        language="go",
        command="gopls",
        extensions=[".go"],
    ),
    "rust": LspServerConfig(
        language="rust",
        command="rust-analyzer",
        extensions=[".rs"],
    ),
    "c": LspServerConfig(
        language="c",
        command="clangd",
        extensions=[".c", ".h"],
    ),
    "cpp": LspServerConfig(
        language="cpp",
        command="clangd",
        extensions=[".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"],
    ),
    "lua": LspServerConfig(
        language="lua",
        command="lua-language-server",
        extensions=[".lua"],
    ),
}

# ---------------------------------------------------------------------------
# IDE extension identifiers → language + server entry point relative to ext dir
#
# Each entry: (extension_id_prefix, language, server_relative_path, args, source_label)
# server_relative_path is relative to the extension root.
# ---------------------------------------------------------------------------

_EXTENSION_MAP: list[tuple[str, str, str, list[str], str]] = [
    # Python
    ("ms-python.vscode-pylance", "python", "dist/server.bundle.js", ["--stdio"], "Pylance (VS Code ext)"),
    ("anysphere.cursorpyright", "python", "dist/server.js", ["--stdio"], "CursorPyright (Cursor ext)"),
    ("ms-pyright.pyright", "python", "dist/server.js", ["--stdio"], "Pyright (VS Code ext)"),
    # C/C++ — clangd extension doesn't bundle the binary, skip.
    # TypeScript — VS Code bundles tsserver, but it's not a stdio LSP, skip.
    # Rust
    ("rust-lang.rust-analyzer", "rust", "server/rust-analyzer", [], "rust-analyzer (VS Code ext)"),
    # Go
    ("golang.go", "go", "extension/gopls", [], "gopls (VS Code ext)"),
    # Lua
    ("sumneko.lua", "lua", "server/bin/lua-language-server", [], "lua-language-server (VS Code ext)"),
]

# ---------------------------------------------------------------------------
# npm global package → language + command mapping
# ---------------------------------------------------------------------------

_NPM_PACKAGE_MAP: dict[str, tuple[str, str, list[str]]] = {
    # package name  → (language, command, args)
    "pyright": ("python", "pyright-langserver", ["--stdio"]),
    "typescript-language-server": ("typescript", "typescript-language-server", ["--stdio"]),
    "bash-language-server": ("bash", "bash-language-server", ["start"]),
    "vscode-langservers-extracted": ("css", "vscode-html-language-server", ["--stdio"]),
    "dockerfile-language-server-nodejs": ("dockerfile", "docker-langserver", ["--stdio"]),
    "yaml-language-server": ("yaml", "yaml-language-server", ["--stdio"]),
}

# ---------------------------------------------------------------------------
# pip package → language + command mapping
# ---------------------------------------------------------------------------

_PIP_PACKAGE_MAP: dict[str, tuple[str, str, list[str]]] = {
    "python-lsp-server": ("python", "pylsp", []),
    "pyright": ("python", "pyright-langserver", ["--stdio"]),
    "basedpyright": ("python", "basedpyright-langserver", ["--stdio"]),
    "ruff-lsp": ("python", "ruff-lsp", []),
    "jedi-language-server": ("python", "jedi-language-server", []),
}

# ---------------------------------------------------------------------------
# Mason package → language + binary relative to package dir
# ---------------------------------------------------------------------------

_MASON_MAP: dict[str, tuple[str, str, list[str]]] = {
    "pyright": ("python", "pyright-langserver", ["--stdio"]),
    "ruff-lsp": ("python", "ruff-lsp", []),
    "lua-language-server": ("lua", "lua-language-server", []),
    "rust-analyzer": ("rust", "rust-analyzer", []),
    "gopls": ("go", "gopls", []),
    "clangd": ("cpp", "clangd", []),
    "bash-language-server": ("bash", "bash-language-server", ["start"]),
    "typescript-language-server": ("typescript", "typescript-language-server", ["--stdio"]),
}

# ---------------------------------------------------------------------------
# Extra PATH locations to search for specific binaries
# ---------------------------------------------------------------------------

_EXTRA_PATH_GLOBS: list[tuple[list[str], str, str, list[str]]] = [
    # (path_globs, binary_name, language, args)
    (["/usr/bin"], "clangd", "cpp", []),
    (["/opt/homebrew/bin", "/usr/local/bin"], "clangd", "cpp", []),
    (["/opt/homebrew/opt/llvm/bin"], "clangd", "cpp", []),
]
