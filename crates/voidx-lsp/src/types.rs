//! LSP types — a minimal subset of the Language Server Protocol types.
//!
//! We define only what we need (definition, references, diagnostics, symbols, format)
//! rather than pulling in the full `lsp-types` crate.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub line: u32,
    pub character: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Range {
    pub start: Position,
    pub end: Position,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Location {
    pub uri: String,
    pub range: Range,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Diagnostic {
    pub range: Range,
    #[serde(default)]
    pub severity: Option<u32>,
    #[serde(default)]
    pub code: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SymbolInformation {
    pub name: String,
    pub kind: u32,
    pub location: Location,
    #[serde(default)]
    pub container_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextEdit {
    pub range: Range,
    pub new_text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerCapabilities {
    #[serde(default)]
    pub definition_provider: bool,
    #[serde(default)]
    pub references_provider: bool,
    #[serde(default)]
    pub document_symbol_provider: bool,
    #[serde(default)]
    pub document_formatting_provider: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InitializeResult {
    pub capabilities: ServerCapabilities,
}

/// Convert a file:// URI to a filesystem path.
pub fn uri_to_path(uri: &str) -> Option<String> {
    let path = uri.strip_prefix("file://")?;
    // Handle Windows paths like file:///C%3A/...
    if path.len() > 1 && path.as_bytes()[0] == b'/' && path.as_bytes()[1].is_ascii_alphabetic() {
        let decoded = url_decode(&path[1..]);
        return Some(decoded);
    }
    Some(url_decode(path))
}

fn url_decode(s: &str) -> String {
    let mut result = String::new();
    let mut chars = s.bytes();
    while let Some(b) = chars.next() {
        match b {
            b'%' => {
                let hi = chars.next().unwrap_or(b'0');
                let lo = chars.next().unwrap_or(b'0');
                let byte = u8::from_str_radix(
                    &format!("{}{}", hi as char, lo as char),
                    16,
                )
                .unwrap_or(b'?');
                result.push(byte as char);
            }
            _ => result.push(b as char),
        }
    }
    result
}
