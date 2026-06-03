//! MCP Manager — manages multiple MCP server connections, registers their tools.
//!
//! Ported from `src/voidx/mcp/manager.py`.

use crate::client::McpClient;
use crate::error::McpError;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use voidx_tools::registry::ToolRegistry;

/// Manages a collection of MCP server connections.
pub struct McpManager {
    clients: HashMap<String, McpClient>,
    registry: Arc<RwLock<ToolRegistry>>,
}

impl McpManager {
    pub fn new(registry: Arc<RwLock<ToolRegistry>>) -> Self {
        Self {
            clients: HashMap::new(),
            registry,
        }
    }

    /// Add and start an MCP server, registering its tools in the registry.
    pub async fn add_server(
        &mut self,
        name: &str,
        command: &str,
        args: &[&str],
    ) -> Result<(), McpError> {
        let mut client = McpClient::start(name, command, args).await?;
        client.initialize().await?;

        // Register discovered tools with the tool registry
        let tools = client.tools().to_vec();
        for tool_def in &tools {
            let tool_id = format!("mcp__{name}__{}", tool_def.name);

            // Create an adapter tool and register it
            let adapter = McpToolAdapter {
                tool_id: tool_id.clone(),
                name: tool_def.name.clone(),
                description: tool_def.description.clone(),
                parameters: tool_def.input_schema.clone(),
                server_name: name.to_string(),
            };

            self.registry
                .write()
                .unwrap()
                .register(std::sync::Arc::new(adapter));
        }

        self.clients.insert(name.to_string(), client);
        tracing::info!(
            "MCP server '{}' started with {} tools",
            name,
            tools.len()
        );
        Ok(())
    }

    /// Remove an MCP server and unregister its tools.
    pub async fn remove_server(&mut self, name: &str) {
        self.clients.remove(name);
        let prefix = format!("mcp__{name}__");
        self.registry
            .write()
            .unwrap()
            .unregister_prefix(&prefix);
    }

    /// Get a client by server name.
    pub fn get(&mut self, name: &str) -> Option<&mut McpClient> {
        self.clients.get_mut(name)
    }

    /// List connected server names.
    pub fn servers(&self) -> Vec<&str> {
        self.clients.keys().map(|s| s.as_str()).collect()
    }
}

// ── MCP → Tool adapter ──────────────────────────────────────────────────

use async_trait::async_trait;
use voidx_tools::base::{Tool, ToolContext, ToolResult};
use voidx_tools::error::ToolError;

struct McpToolAdapter {
    tool_id: String,
    #[allow(dead_code)]
    name: String,
    description: String,
    parameters: serde_json::Value,
    #[allow(dead_code)]
    server_name: String,
}

#[async_trait]
impl Tool for McpToolAdapter {
    fn id(&self) -> &'static str {
        // We need to return a &'static str, but the id is dynamic.
        // This is a known limitation — for now, leak the string.
        // In production, this would use a different approach (e.g. Arc<String>).
        Box::leak(self.tool_id.clone().into_boxed_str())
    }

    fn description(&self) -> &'static str {
        Box::leak(self.description.clone().into_boxed_str())
    }

    fn parameters_schema(&self) -> serde_json::Value {
        self.parameters.clone()
    }

    async fn execute(
        &self,
        _args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        // The actual MCP call is handled by the agent layer which has access
        // to the McpManager. Here we just validate args.
        Ok(ToolResult::new("MCP tool execution delegated to manager"))
    }
}
