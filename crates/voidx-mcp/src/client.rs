//! MCP client — JSON-RPC over stdio to a subprocess server.
//!
//! Ported from `src/voidx/mcp/client.py`.

use crate::error::McpError;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};

// ── JSON-RPC types ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JsonRpcRequest {
    jsonrpc: String,
    id: u64,
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JsonRpcResponse {
    jsonrpc: String,
    id: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<JsonRpcError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JsonRpcError {
    code: i64,
    message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpToolDef {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub input_schema: serde_json::Value,
}

// ── MCP Client ─────────────────────────────────────────────────────────────

pub struct McpClient {
    server_name: String,
    child: Child,
    #[allow(dead_code)]
    stdin: tokio::process::ChildStdin,
    reader: BufReader<tokio::process::ChildStdout>,
    next_id: AtomicU64,
    tools: Vec<McpToolDef>,
    initialized: bool,
}

impl McpClient {
    /// Start an MCP server subprocess and connect via stdio.
    pub async fn start(server_name: &str, command: &str, args: &[&str]) -> Result<Self, McpError> {
        let mut child = Command::new(command)
            .args(args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .map_err(|e| McpError::Spawn(format!("Failed to start {server_name}: {e}")))?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| McpError::Spawn("No stdin".to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| McpError::Spawn("No stdout".to_string()))?;
        let reader = BufReader::new(stdout);

        Ok(Self {
            server_name: server_name.to_string(),
            child,
            stdin,
            reader,
            next_id: AtomicU64::new(1),
            tools: Vec::new(),
            initialized: false,
        })
    }

    /// Send initialize and list tools.
    pub async fn initialize(&mut self) -> Result<(), McpError> {
        // Send initialize request
        let init_params = serde_json::json!({
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "voidx",
                "version": "0.1.0"
            }
        });

        let response = self.send_request("initialize", Some(init_params)).await?;

        if let Some(result) = response.get("result") {
            tracing::debug!(
                "MCP server {} initialized: {:?}",
                self.server_name,
                result
            );
        }

        // Send initialized notification
        self.send_notification("notifications/initialized", None)
            .await?;

        // List tools
        let response = self.send_request("tools/list", None).await?;

        if let Some(tools) = response
            .get("result")
            .and_then(|r| r.get("tools"))
            .and_then(|t| t.as_array())
        {
            self.tools = tools
                .iter()
                .map(|t| McpToolDef {
                    name: t.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    description: t
                        .get("description")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    input_schema: t.get("inputSchema").cloned().unwrap_or_default(),
                })
                .collect();
        }

        self.initialized = true;
        Ok(())
    }

    /// Get the tools discovered from this MCP server.
    pub fn tools(&self) -> &[McpToolDef] {
        &self.tools
    }

    /// Call a tool on the MCP server.
    pub async fn call_tool(
        &mut self,
        tool_name: &str,
        arguments: &serde_json::Value,
    ) -> Result<serde_json::Value, McpError> {
        let params = serde_json::json!({
            "name": tool_name,
            "arguments": arguments,
        });

        let response = self.send_request("tools/call", Some(params)).await?;
        Ok(response.get("result").cloned().unwrap_or_default())
    }

    /// The server name for identification.
    pub fn server_name(&self) -> &str {
        &self.server_name
    }

    // ── Internals ────────────────────────────────────────────────────────

    async fn send_request(
        &mut self,
        method: &str,
        params: Option<serde_json::Value>,
    ) -> Result<serde_json::Value, McpError> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);

        let request = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id,
            method: method.to_string(),
            params,
        };

        let mut line = serde_json::to_string(&request)?;
        line.push('\n');

        self.stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| McpError::Io(e))?;

        // Read response — MCP sends one JSON-RPC response per request
        let mut response_line = String::new();
        self.reader
            .read_line(&mut response_line)
            .await
            .map_err(|e| McpError::Io(e))?;

        if response_line.trim().is_empty() {
            return Err(McpError::Protocol("Empty response from server".to_string()));
        }

        let response: JsonRpcResponse = serde_json::from_str(&response_line)?;

        if let Some(err) = response.error {
            return Err(McpError::Rpc {
                code: err.code,
                message: err.message,
            });
        }

        Ok(serde_json::json!({
            "result": response.result,
            "id": response.id,
        }))
    }

    async fn send_notification(
        &mut self,
        method: &str,
        params: Option<serde_json::Value>,
    ) -> Result<(), McpError> {
        let notification = serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params.unwrap_or(serde_json::Value::Null),
        });

        let mut line = serde_json::to_string(&notification)?;
        line.push('\n');

        self.stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| McpError::Io(e))?;

        Ok(())
    }
}

impl Drop for McpClient {
    fn drop(&mut self) {
        // Try to kill the child process
        let _ = self.child.start_kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_json_rpc_serialization() {
        let req = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: 1,
            method: "tools/list".to_string(),
            params: None,
        };
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("tools/list"));
        assert!(json.contains("2.0"));
    }
}
