//! Tool registry — every tool typed, all dispatch quantified.
//!
//! Ported from `src/voidx/tools/registry.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolDef {
    pub id: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

/// Manages all available tools.
pub struct ToolRegistry {
    tools: HashMap<String, Arc<dyn Tool>>,
    defs: HashMap<String, ToolDef>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        let mut reg = Self {
            tools: HashMap::new(),
            defs: HashMap::new(),
        };
        reg.register_builtins();
        reg
    }

    fn register_builtins(&mut self) {
        // Register all built-in tool implementations
        self.register(Arc::new(crate::bash::BashTool));
        self.register(Arc::new(crate::file_ops::FileReadTool));
        self.register(Arc::new(crate::file_ops::FileWriteTool));
        self.register(Arc::new(crate::file_ops::FileEditTool));
        self.register(Arc::new(crate::grep::GrepTool));
        self.register(Arc::new(crate::glob::GlobTool));
        self.register(Arc::new(crate::webfetch::WebFetchTool::default()));
        self.register(Arc::new(crate::websearch::WebSearchTool::default()));
        // RepoMap
        self.register(Arc::new(crate::repomap::RepoMapTool));
        // Todo & Task Status
        self.register(Arc::new(crate::todo::TodoWriteTool));
        self.register(Arc::new(crate::task_status::TaskStatusTool));
        // LSP tools
        self.register(Arc::new(crate::lsp::LspDiagnosticsTool));
        self.register(Arc::new(crate::lsp::LspSymbolsTool));
        self.register(Arc::new(crate::lsp::LspDefinitionTool));
        self.register(Arc::new(crate::lsp::LspReferencesTool));
        self.register(Arc::new(crate::lsp::LspFormatTool));
    }

    /// Register a tool dynamically.
    pub fn register(&mut self, tool: Arc<dyn Tool>) {
        let id = tool.id().to_string();
        let def = ToolDef {
            id: id.clone(),
            description: tool.description().to_string(),
            parameters: tool.parameters_schema(),
        };
        self.defs.insert(id.clone(), def);
        self.tools.insert(id, tool);
    }

    pub fn register_external(&mut self, tool: Arc<dyn Tool>) {
        self.register(tool);
    }

    /// Remove tools whose id starts with the given prefix.
    pub fn unregister_prefix(&mut self, prefix: &str) {
        let ids: Vec<String> = self
            .defs
            .keys()
            .filter(|id| id.starts_with(prefix))
            .cloned()
            .collect();
        for id in ids {
            self.defs.remove(&id);
            self.tools.remove(&id);
        }
    }

    /// List all tool definitions for LLM consumption.
    pub fn list(&self) -> Vec<&ToolDef> {
        self.defs.values().collect()
    }

    /// Generate OpenAI/Anthropic-compatible tool definitions.
    pub fn tools_for_llm(&self) -> Vec<serde_json::Value> {
        self.defs
            .values()
            .map(|t| {
                serde_json::json!({
                    "type": "function",
                    "function": {
                        "name": t.id,
                        "description": t.description,
                        "parameters": t.parameters,
                        "strict": true,
                    }
                })
            })
            .collect()
    }

    /// Look up a tool definition by id.
    pub fn get_def(&self, tool_id: &str) -> Option<&ToolDef> {
        self.defs.get(tool_id)
    }

    /// Return all tool ids.
    pub fn ids(&self) -> Vec<&str> {
        self.tools.keys().map(|s| s.as_str()).collect()
    }

    /// Retain only the tools listed in allowed_ids.
    pub fn filter_tools(&mut self, allowed_ids: &[&str]) {
        let allowed: std::collections::HashSet<&str> = allowed_ids.iter().copied().collect();
        let to_remove: Vec<String> = self
            .defs
            .keys()
            .filter(|id| !allowed.contains(id.as_str()))
            .cloned()
            .collect();
        for id in to_remove {
            self.defs.remove(&id);
            self.tools.remove(&id);
        }
    }

    /// Check if a tool is registered.
    pub fn has(&self, tool_id: &str) -> bool {
        self.tools.contains_key(tool_id)
    }

    /// Execute a tool by id.
    pub async fn execute(
        &self,
        tool_id: &str,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        match self.tools.get(tool_id) {
            Some(tool) => tool.execute(args, ctx).await,
            None => Err(ToolError::NotFound(tool_id.to_string())),
        }
    }
}

impl Default for ToolRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_registry_has_builtins() {
        let reg = ToolRegistry::new();
        let ids = reg.ids();
        assert!(ids.contains(&"bash"));
        assert!(ids.contains(&"file_read"));
        assert!(ids.contains(&"file_write"));
        assert!(ids.contains(&"file_edit"));
        assert!(ids.contains(&"grep"));
        assert!(ids.contains(&"glob"));
        assert!(ids.contains(&"repo_map"));
        assert!(ids.contains(&"todo"));
        assert!(ids.contains(&"task_status"));
        assert!(ids.contains(&"lsp_diagnostics"));
        assert!(ids.contains(&"lsp_symbols"));
        assert!(ids.contains(&"lsp_definition"));
        assert!(ids.contains(&"lsp_references"));
        assert!(ids.contains(&"lsp_format"));
    }

    #[test]
    fn test_tools_for_llm_produces_valid_schema() {
        let reg = ToolRegistry::new();
        let tools = reg.tools_for_llm();
        let bash = tools
            .iter()
            .find(|t| t["function"]["name"] == "bash")
            .unwrap();
        assert_eq!(bash["type"], "function");
        assert_eq!(bash["function"]["strict"], true);
        assert!(bash["function"]["parameters"]["properties"]["command"].is_object());
    }
}
