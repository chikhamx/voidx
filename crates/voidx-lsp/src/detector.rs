//! LSP Detector — maps file extensions to LSP server commands.
//!
//! Ported from `src/voidx/lsp/detector.py`.

use std::collections::HashMap;
use std::path::Path;
use std::sync::LazyLock;

/// Configuration for starting a language server.
#[derive(Debug, Clone)]
pub struct LspServerConfig {
    pub language: String,
    pub command: String,
    pub args: Vec<String>,
    pub extensions: Vec<String>,
}

/// Mapping from file extension → LSP server config.
type DetectorMap = HashMap<String, LspServerConfig>;

static DETECTORS: LazyLock<DetectorMap> = LazyLock::new(|| {
    let mut map: DetectorMap = HashMap::new();

    // Rust
    let rust = LspServerConfig {
        language: "rust".to_string(),
        command: "rust-analyzer".to_string(),
        args: vec![],
        extensions: vec!["rs".to_string()],
    };
    map.insert("rs".to_string(), rust);

    // Python (Pyright)
    let pyright = LspServerConfig {
        language: "python".to_string(),
        command: "pyright-langserver".to_string(),
        args: vec!["--stdio".to_string()],
        extensions: vec!["py".to_string(), "pyi".to_string()],
    };
    map.insert("py".to_string(), pyright.clone());
    map.insert("pyi".to_string(), pyright);

    // TypeScript/JavaScript
    let ts = LspServerConfig {
        language: "typescript".to_string(),
        command: "typescript-language-server".to_string(),
        args: vec!["--stdio".to_string()],
        extensions: vec![
            "ts".to_string(),
            "tsx".to_string(),
            "js".to_string(),
            "jsx".to_string(),
            "mjs".to_string(),
            "cjs".to_string(),
        ],
    };
    for ext in &ts.extensions {
        map.insert(ext.clone(), ts.clone());
    }

    // Go (gopls)
    map.insert(
        "go".to_string(),
        LspServerConfig {
            language: "go".to_string(),
            command: "gopls".to_string(),
            args: vec![],
            extensions: vec!["go".to_string()],
        },
    );

    // C/C++ (clangd)
    let cpp = LspServerConfig {
        language: "cpp".to_string(),
        command: "clangd".to_string(),
        args: vec![],
        extensions: vec![
            "c".to_string(),
            "cpp".to_string(),
            "cc".to_string(),
            "cxx".to_string(),
            "h".to_string(),
            "hpp".to_string(),
            "hxx".to_string(),
        ],
    };
    for ext in &cpp.extensions {
        map.insert(ext.clone(), cpp.clone());
    }

    // Java (jdtls) — simplified, real jdtls needs more setup
    map.insert(
        "java".to_string(),
        LspServerConfig {
            language: "java".to_string(),
            command: "jdtls".to_string(),
            args: vec![],
            extensions: vec!["java".to_string()],
        },
    );

    // Lua (lua-language-server)
    map.insert(
        "lua".to_string(),
        LspServerConfig {
            language: "lua".to_string(),
            command: "lua-language-server".to_string(),
            args: vec![],
            extensions: vec!["lua".to_string()],
        },
    );

    // Ruby (solargraph)
    map.insert(
        "rb".to_string(),
        LspServerConfig {
            language: "ruby".to_string(),
            command: "solargraph".to_string(),
            args: vec!["stdio".to_string()],
            extensions: vec!["rb".to_string()],
        },
    );

    // Zig (zls)
    map.insert(
        "zig".to_string(),
        LspServerConfig {
            language: "zig".to_string(),
            command: "zls".to_string(),
            args: vec![],
            extensions: vec!["zig".to_string()],
        },
    );

    // CSS
    map.insert(
        "css".to_string(),
        LspServerConfig {
            language: "css".to_string(),
            command: "vscode-css-language-server".to_string(),
            args: vec!["--stdio".to_string()],
            extensions: vec!["css".to_string(), "scss".to_string(), "less".to_string()],
        },
    );

    // HTML
    map.insert(
        "html".to_string(),
        LspServerConfig {
            language: "html".to_string(),
            command: "vscode-html-language-server".to_string(),
            args: vec!["--stdio".to_string()],
            extensions: vec!["html".to_string(), "htm".to_string()],
        },
    );

    // JSON
    map.insert(
        "json".to_string(),
        LspServerConfig {
            language: "json".to_string(),
            command: "vscode-json-language-server".to_string(),
            args: vec!["--stdio".to_string()],
            extensions: vec!["json".to_string()],
        },
    );

    map
});

pub struct LspDetector;

impl LspDetector {
    /// Detect which LSP server to use for a file, based on its extension.
    pub fn detect(file_path: &Path) -> Option<&'static LspServerConfig> {
        let ext = file_path.extension()?.to_str()?.to_lowercase();
        DETECTORS.get(&ext)
    }

    /// Return all configured detectors.
    pub fn all_languages() -> Vec<String> {
        let mut langs: Vec<String> = DETECTORS
            .values()
            .map(|c| c.language.clone())
            .collect();
        langs.sort();
        langs.dedup();
        langs
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_rust() {
        let config = LspDetector::detect(Path::new("src/main.rs")).unwrap();
        assert_eq!(config.command, "rust-analyzer");
    }

    #[test]
    fn test_detect_python() {
        let config = LspDetector::detect(Path::new("src/main.py")).unwrap();
        assert_eq!(config.command, "pyright-langserver");
    }

    #[test]
    fn test_detect_typescript() {
        let config = LspDetector::detect(Path::new("src/App.tsx")).unwrap();
        assert_eq!(config.language, "typescript");
    }

    #[test]
    fn test_detect_unknown() {
        let config = LspDetector::detect(Path::new("README.md"));
        // .md is not in our detector map currently
        assert!(config.is_none());
    }

    #[test]
    fn test_all_languages() {
        let langs = LspDetector::all_languages();
        assert!(langs.contains(&"rust".to_string()));
        assert!(langs.contains(&"python".to_string()));
    }
}
