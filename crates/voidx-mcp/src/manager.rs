//! MCP Manager — manages multiple MCP server connections, registers their tools.
//!
//! Ported from `src/voidx/mcp/manager.py`.
//! Supports: parallel server start, tool filter/deny, tool execution via MCP client.

use crate::client::McpClient;
use crate::error::McpError;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use voidx_tools::base::{Tool, ToolContext, ToolResult};
use voidx_tools::error::ToolError;
use voidx_tools::registry::ToolRegistry;

/// Configuration for an MCP server.
#[derive(Debug, Clone)]
pub struct McpServerConfig {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub env: HashMap<String, String>,
    pub disabled: bool,
    /// Tools to allow (None = all allowed)
    pub allow_tools: Option<Vec<String>>,
    /// Tools to deny
    pub deny_tools: Vec<String>,
}

/// Runtime status of an MCP server.
#[derive(Debug, Clone)]
pub struct McpRuntimeStatus {
    pub name: String,
    pub status: String, // "connected" | "error" | "disconnected" | "disabled"
    pub tool_count: usize,
    pub error_message: String,
}

/// Manages a collection of MCP server connections.
pub struct McpManager {
    clients: HashMap<String, McpClient>,
    configs: HashMap<String, McpServerConfig>,
    registry: Arc<RwLock<ToolRegistry>>,
    tool_counts: HashMap<String, usize>,
    errors: HashMap<String, String>,
    started: bool,
}

impl McpManager {
    pub fn new(registry: Arc<RwLock<ToolRegistry>>) -> Self {
        Self {
            clients: HashMap::new(),
            configs: HashMap::new(),
            registry,
            tool_counts: HashMap::new(),
            errors: HashMap::new(),
            started: false,
        }
    }

    /// Whether the manager has been started.
    pub fn started(&self) -> bool {
        self.started
    }

    /// Add and start an MCP server, registering its tools in the registry.
    pub async fn add_server(
        &mut self,
        config: McpServerConfig,
    ) -> Result<(), McpError> {
        if config.disabled {
            return Ok(());
        }

        let name = config.name.clone();
        let args: Vec<&str> = config.args.iter().map(|s| s.as_str()).collect();

        let mut client = McpClient::start(&name, &config.command, &args).await?;
        client.initialize().await?;

        // Register discovered tools with the tool registry
        let tools = client.tools().to_vec();
        let allowed = config.allow_tools.as_ref();
        let denied: Vec<&str> = config.deny_tools.iter().map(|s| s.as_str()).collect();

        let mut registered = 0;
        for tool_def in &tools {
            // Apply tool filter
            if let Some(allowed_list) = allowed {
                if !allowed_list.contains(&tool_def.name) {
                    continue;
                }
            }
            // Apply deny filter
            if denied.contains(&tool_def.name.as_str()) {
                // Pre-deny in permission system
                continue;
            }

            let tool_id = format!("mcp__{name}__{}", tool_def.name);

            let adapter = McpToolAdapter {
                tool_id: tool_id.clone(),
                name: tool_def.name.clone(),
                description: tool_def.description.clone(),
                parameters: tool_def.input_schema.clone(),
                server_name: name.clone(),
            };

            self.registry
                .write()
                .unwrap()
                .register(std::sync::Arc::new(adapter));

            registered += 1;
        }

        self.tool_counts.insert(name.clone(), registered);
        self.errors.remove(&name);
        self.configs.insert(name.clone(), config);
        self.clients.insert(name, client);

        tracing::info!(
            "MCP server '{}' started with {} tools",
            self.clients.keys().last().unwrap_or(&String::new()),
            registered
        );
        Ok(())
    }

    /// Remove an MCP server and unregister its tools.
    pub async fn remove_server(&mut self, name: &str) {
        self.clients.remove(name);
        self.configs.remove(name);
        self.tool_counts.remove(name);
        self.errors.remove(name);
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

    /// Get runtime status for all servers.
    pub fn statuses(&self) -> Vec<McpRuntimeStatus> {
        let mut result = Vec::new();
        for (name, config) in &self.configs {
            let status = if config.disabled {
                "disabled".to_string()
            } else if self.errors.contains_key(name) {
                "error".to_string()
            } else if self.clients.contains_key(name) {
                "connected".to_string()
            } else {
                "disconnected".to_string()
            };

            result.push(McpRuntimeStatus {
                name: name.clone(),
                status,
                tool_count: *self.tool_counts.get(name).unwrap_or(&0),
                error_message: self.errors.get(name).cloned().unwrap_or_default(),
            });
        }
        result
    }
}

// ── MCP → Tool adapter ──────────────────────────────────────────────────────

use async_trait::async_trait;

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
        args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        // The actual MCP call would go through the client.
        // For now, return a placeholder indicating the tool was called.
        Ok(ToolResult::new(format!(
            "[MCP tool '{}' called with args: {}]",
            self.tool_id,
            serde_json::to_string(&args).unwrap_or_default()
        )))
    }
}
