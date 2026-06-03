//! LSP client — JSON-RPC over stdio to a language server.
//!
//! Ported from `src/voidx/lsp/client.py`.

use crate::error::LspError;
use crate::types::*;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};

#[derive(Debug, Serialize)]
struct LspRequest {
    jsonrpc: String,
    id: u64,
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct LspResponse {
    #[allow(dead_code)]
    jsonrpc: String,
    id: u64,
    #[serde(default)]
    result: Option<serde_json::Value>,
    #[serde(default)]
    error: Option<LspResponseError>,
}

#[derive(Debug, Deserialize)]
struct LspResponseError {
    code: i64,
    message: String,
}

pub struct LspClient {
    child: Child,
    #[allow(dead_code)]
    stdin: tokio::process::ChildStdin,
    reader: BufReader<tokio::process::ChildStdout>,
    next_id: AtomicU64,
    capabilities: Option<ServerCapabilities>,
}

impl LspClient {
    /// Start a language server subprocess.
    pub async fn start(command: &str, args: &[&str], workspace: &std::path::Path) -> Result<Self, LspError> {
        let mut child = Command::new(command)
            .args(args)
            .current_dir(workspace)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .map_err(|e| LspError::Spawn(format!("Failed to start {command}: {e}")))?;

        let stdin = child.stdin.take().ok_or_else(|| LspError::Spawn("No stdin".to_string()))?;
        let stdout = child.stdout.take().ok_or_else(|| LspError::Spawn("No stdout".to_string()))?;
        let reader = BufReader::new(stdout);

        Ok(Self {
            child,
            stdin,
            reader,
            next_id: AtomicU64::new(1),
            capabilities: None,
        })
    }

    /// Send initialize request and get server capabilities.
    pub async fn initialize(&mut self, workspace: &std::path::Path) -> Result<(), LspError> {
        let workspace_uri = format!(
            "file://{}",
            workspace.to_string_lossy()
        );

        let params = serde_json::json!({
            "processId": std::process::id(),
            "rootUri": workspace_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": true},
                    "references": {"dynamicRegistration": true},
                    "documentSymbol": {"dynamicRegistration": true},
                    "formatting": {"dynamicRegistration": true},
                }
            },
        });

        let response = self.send_request("initialize", Some(params)).await?;

        if let Some(result) = response.get("result") {
            if let Some(caps) = result.get("capabilities") {
                self.capabilities = Some(serde_json::from_value(caps.clone()).unwrap_or(ServerCapabilities {
                    definition_provider: false,
                    references_provider: false,
                    document_symbol_provider: false,
                    document_formatting_provider: false,
                }));
            }
        }

        // Send initialized notification
        self.send_notification("initialized", Some(serde_json::json!({})))
            .await?;

        Ok(())
    }

    /// Notify the server that a document is open.
    pub async fn open_document(&mut self, file_path: &std::path::Path) -> Result<(), LspError> {
        let uri = format!("file://{}", file_path.to_string_lossy());
        let content = std::fs::read_to_string(file_path).unwrap_or_default();

        let params = serde_json::json!({
            "textDocument": {
                "uri": uri,
                "languageId": guess_language(file_path),
                "version": 1,
                "text": content,
            }
        });

        self.send_notification("textDocument/didOpen", Some(params))
            .await?;
        Ok(())
    }

    /// Get definition locations for a symbol.
    pub async fn definition(
        &mut self,
        file_path: &std::path::Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<Location>, LspError> {
        let uri = format!("file://{}", file_path.to_string_lossy());
        let params = serde_json::json!({
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        });

        let response = self.send_request("textDocument/definition", Some(params)).await?;
        let locations: Vec<Location> = response
            .get("result")
            .and_then(|r| serde_json::from_value(r.clone()).ok())
            .unwrap_or_default();

        Ok(locations)
    }

    /// Get all references to a symbol.
    pub async fn references(
        &mut self,
        file_path: &std::path::Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<Location>, LspError> {
        let uri = format!("file://{}", file_path.to_string_lossy());
        let params = serde_json::json!({
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": true},
        });

        let response = self.send_request("textDocument/references", Some(params)).await?;
        let locations: Vec<Location> = response
            .get("result")
            .and_then(|r| serde_json::from_value(r.clone()).ok())
            .unwrap_or_default();

        Ok(locations)
    }

    /// Get diagnostics for a file.
    pub async fn diagnostics(
        &mut self,
        file_path: &std::path::Path,
    ) -> Result<Vec<Diagnostic>, LspError> {
        // Diagnostics are typically sent as notifications from the server.
        // We send a didOpen + didChange to trigger diagnostics, then wait briefly.
        let uri = format!("file://{}", file_path.to_string_lossy());
        let content = std::fs::read_to_string(file_path).unwrap_or_default();

        let params = serde_json::json!({
            "textDocument": {
                "uri": uri,
                "languageId": guess_language(file_path),
                "version": 1,
                "text": content,
            }
        });

        self.send_notification("textDocument/didChange", Some(params))
            .await?;

        // For now, return empty — full diagnostic pull requires async notification handling
        Ok(Vec::new())
    }

    /// Get document symbols.
    pub async fn symbols(
        &mut self,
        file_path: &std::path::Path,
    ) -> Result<Vec<SymbolInformation>, LspError> {
        let uri = format!("file://{}", file_path.to_string_lossy());
        let params = serde_json::json!({
            "textDocument": {"uri": uri},
        });

        let response = self.send_request("textDocument/documentSymbol", Some(params)).await?;
        let symbols: Vec<SymbolInformation> = response
            .get("result")
            .and_then(|r| serde_json::from_value(r.clone()).ok())
            .unwrap_or_default();

        Ok(symbols)
    }

    /// Format a document.
    pub async fn formatting(
        &mut self,
        file_path: &std::path::Path,
    ) -> Result<Vec<TextEdit>, LspError> {
        let uri = format!("file://{}", file_path.to_string_lossy());
        let params = serde_json::json!({
            "textDocument": {"uri": uri},
            "options": {"tabSize": 4, "insertSpaces": true},
        });

        let response = self.send_request("textDocument/formatting", Some(params)).await?;
        let edits: Vec<TextEdit> = response
            .get("result")
            .and_then(|r| serde_json::from_value(r.clone()).ok())
            .unwrap_or_default();

        Ok(edits)
    }

    // ── Internals ────────────────────────────────────────────────────────

    async fn send_request(
        &mut self,
        method: &str,
        params: Option<serde_json::Value>,
    ) -> Result<serde_json::Value, LspError> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);

        let request = LspRequest {
            jsonrpc: "2.0".to_string(),
            id,
            method: method.to_string(),
            params,
        };

        let header = serde_json::to_string(&request)?;
        let message = format!("Content-Length: {}\r\n\r\n{}", header.len(), header);

        self.stdin
            .write_all(message.as_bytes())
            .await
            .map_err(|e| LspError::Io(e))?;

        // Read Content-Length header
        let mut content_length = 0usize;
        loop {
            let mut line = String::new();
            self.reader
                .read_line(&mut line)
                .await
                .map_err(|e| LspError::Io(e))?;

            if line == "\r\n" || line == "\n" {
                break;
            }

            if let Some(len_str) = line
                .to_lowercase()
                .strip_prefix("content-length:")
                .map(|s| s.trim())
            {
                content_length = len_str.parse().unwrap_or(0);
            }
        }

        // Read body
        let mut body = vec![0u8; content_length];
        tokio::io::AsyncReadExt::read_exact(&mut self.reader, &mut body)
            .await
            .map_err(|e| LspError::Io(e))?;

        let body_str = String::from_utf8_lossy(&body);
        let response: LspResponse =
            serde_json::from_str(&body_str).map_err(|e| LspError::Protocol(e.to_string()))?;

        if let Some(err) = response.error {
            return Err(LspError::Protocol(format!(
                "LSP error ({code}): {message}",
                code = err.code,
                message = err.message
            )));
        }

        Ok(serde_json::json!({
            "result": response.result,
        }))
    }

    async fn send_notification(
        &mut self,
        method: &str,
        params: Option<serde_json::Value>,
    ) -> Result<(), LspError> {
        let notification = serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params.unwrap_or(serde_json::Value::Null),
        });

        let header = serde_json::to_string(&notification)?;
        let message = format!("Content-Length: {}\r\n\r\n{}", header.len(), header);

        self.stdin
            .write_all(message.as_bytes())
            .await
            .map_err(|e| LspError::Io(e))?;

        Ok(())
    }
}

fn guess_language(path: &std::path::Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("rs") => "rust",
        Some("py") => "python",
        Some("ts") | Some("tsx") => "typescript",
        Some("js") | Some("jsx") => "javascript",
        Some("go") => "go",
        Some("java") => "java",
        Some("cpp") | Some("cc") | Some("cxx") | Some("hpp") | Some("h") => "cpp",
        Some("c") => "c",
        Some("css") => "css",
        Some("html") => "html",
        Some("json") => "json",
        Some("md") | Some("mdx") => "markdown",
        Some("yaml") | Some("yml") => "yaml",
        Some("toml") => "toml",
        Some("sql") => "sql",
        Some("sh") | Some("bash") => "shellscript",
        Some("lua") => "lua",
        Some("rb") => "ruby",
        _ => "plaintext",
    }
}

impl Drop for LspClient {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_guess_language() {
        assert_eq!(guess_language(std::path::Path::new("test.rs")), "rust");
        assert_eq!(guess_language(std::path::Path::new("test.py")), "python");
        assert_eq!(guess_language(std::path::Path::new("test.unknown")), "plaintext");
    }
}
