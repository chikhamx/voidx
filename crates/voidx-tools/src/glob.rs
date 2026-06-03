//! Glob tool — file pattern matching over workspace.
//!
//! Ported from `src/voidx/tools/search.py` (GlobTool).

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use crate::schema::model_to_json_schema;
use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use walkdir::WalkDir;

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct GlobInput {
    /// The glob pattern to match files against (e.g., "**/*.rs")
    pub pattern: String,
    /// The directory to search in (defaults to workspace root)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}

pub struct GlobTool;

#[async_trait]
impl Tool for GlobTool {
    fn id(&self) -> &'static str {
        "glob"
    }

    fn description(&self) -> &'static str {
        "Find files matching a glob pattern. Returns sorted file paths."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<GlobInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: GlobInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        let search_root = match &inp.path {
            Some(p) => ctx.workspace.join(p),
            None => ctx.workspace.clone(),
        };

        if !search_root.exists() {
            return Err(ToolError::Other(format!(
                "Path not found: {}",
                search_root.display()
            )));
        }

        let pattern = glob::Pattern::new(&inp.pattern)
            .map_err(|e| ToolError::InvalidArgs(format!("Invalid glob pattern: {e}")))?;

        let mut matches: Vec<String> = Vec::new();

        for entry in WalkDir::new(&search_root)
            .follow_links(false)
            .max_depth(100)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            if !entry.file_type().is_file() {
                continue;
            }

            let relative = entry.path().strip_prefix(&search_root).unwrap_or(entry.path());

            if pattern.matches_path(relative) {
                matches.push(relative.display().to_string());
            }
        }

        matches.sort();
        let count = matches.len();
        let output = if matches.is_empty() {
            format!("No files matched pattern: {}", inp.pattern)
        } else {
            matches.join("\n")
        };

        Ok(ToolResult::new(output).with_metadata(serde_json::json!({
            "pattern": inp.pattern,
            "matches": count,
        })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn test_glob_finds_rust_files() {
        let ws = tempfile::tempdir().unwrap();
        std::fs::write(ws.path().join("lib.rs"), "// lib").unwrap();
        std::fs::write(ws.path().join("main.rs"), "// main").unwrap();
        std::fs::write(ws.path().join("README.md"), "# readme").unwrap();

        let tool = GlobTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({"pattern": "**/*.rs"}),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("lib.rs"));
        assert!(result.output.contains("main.rs"));
        assert!(!result.output.contains("README.md"));
    }
}
