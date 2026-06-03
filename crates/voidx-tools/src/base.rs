//! Tool base — abstract contract, result types, context.
//!
//! Ported from `src/voidx/tools/base.py`.

use crate::error::ToolError;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Resolve a file path and verify it stays inside workspace (+ optional extra paths).
pub fn resolve_safe(
    workspace: &Path,
    file_path: &str,
    extra_paths: &[PathBuf],
) -> Option<PathBuf> {
    let ws = workspace.canonicalize().unwrap_or_else(|_| workspace.to_path_buf());
    let resolved = ws.join(file_path).canonicalize().unwrap_or_else(|_| ws.join(file_path));

    let mut allowed = vec![ws];
    for ep in extra_paths {
        if let Ok(p) = std::fs::canonicalize(ep) {
            allowed.push(p);
        }
    }

    for base in &allowed {
        if let Ok(relative) = resolved.strip_prefix(base) {
            // Ensure no path traversal via ..
            if relative.components().all(|c| c != std::path::Component::ParentDir) {
                return Some(resolved);
            }
        }
    }
    None
}

/// Context passed to every tool execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolContext {
    pub workspace: PathBuf,
    pub session_id: String,
    pub agent: String,
    pub file_mtimes: HashMap<PathBuf, u64>,
    pub sandbox_extra_paths: Vec<PathBuf>,
}

impl Default for ToolContext {
    fn default() -> Self {
        Self {
            workspace: PathBuf::from("."),
            session_id: "default".to_string(),
            agent: "orchestrator".to_string(),
            file_mtimes: HashMap::new(),
            sandbox_extra_paths: vec![],
        }
    }
}

/// Result from tool execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    #[serde(default)]
    pub title: String,
    pub output: String,
    #[serde(default)]
    pub metadata: serde_json::Value,
    /// Unified diff for edit/write tools.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diff: Option<String>,
}

impl ToolResult {
    pub fn new(output: impl Into<String>) -> Self {
        Self {
            title: String::new(),
            output: output.into(),
            metadata: serde_json::Value::Null,
            diff: None,
        }
    }

    pub fn with_title(mut self, title: impl Into<String>) -> Self {
        self.title = title.into();
        self
    }

    pub fn with_metadata(mut self, meta: serde_json::Value) -> Self {
        self.metadata = meta;
        self
    }

    pub fn with_diff(mut self, diff: impl Into<String>) -> Self {
        self.diff = Some(diff.into());
        self
    }
}

/// Every tool has: id, description, typed parameters, deterministic execute.
#[async_trait]
pub trait Tool: Send + Sync {
    /// Unique tool identifier (snake_case).
    fn id(&self) -> &'static str;

    /// Description for the LLM.
    fn description(&self) -> &'static str;

    /// JSON Schema for the tool's parameters.
    fn parameters_schema(&self) -> serde_json::Value;

    /// Execute the tool with typed inputs. Returns typed result.
    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError>;
}
