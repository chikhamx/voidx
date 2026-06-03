//! File operation tools — read, write, edit.
//!
//! Ported from `src/voidx/tools/file_ops.py`.

use crate::base::{resolve_safe, Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use crate::schema::model_to_json_schema;
use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

// ── Shared helpers ─────────────────────────────────────────────────────────

fn read_file_content(path: &std::path::Path) -> Result<String, ToolError> {
    if !path.exists() {
        return Err(ToolError::Other(format!("File not found: {}", path.display())));
    }
    if !path.is_file() {
        return Err(ToolError::Other(format!("Not a file: {}", path.display())));
    }
    Ok(std::fs::read_to_string(path)?)
}

fn check_in_workspace(
    workspace: &std::path::Path,
    file_path: &str,
    extra_paths: &[PathBuf],
) -> Result<PathBuf, ToolError> {
    resolve_safe(workspace, file_path, extra_paths)
        .ok_or_else(|| ToolError::SandboxViolation(file_path.to_string()))
}

// ── FileReadTool ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct FileReadInput {
    /// The absolute path to the file to read
    pub file_path: String,
    /// The line number to start reading from (0-indexed, optional)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offset: Option<usize>,
    /// The number of lines to read (optional, max 2000)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limit: Option<usize>,
}

pub struct FileReadTool;

#[async_trait]
impl Tool for FileReadTool {
    fn id(&self) -> &'static str {
        "file_read"
    }

    fn description(&self) -> &'static str {
        "Read a file from the workspace. Returns content with line numbers."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<FileReadInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: FileReadInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        let path = check_in_workspace(&ctx.workspace, &inp.file_path, &ctx.sandbox_extra_paths)?;
        let content = read_file_content(&path)?;

        let offset = inp.offset.unwrap_or(0);
        let limit = inp.limit.unwrap_or(2000).min(2000);

        let lines: Vec<&str> = content.lines().skip(offset).take(limit).collect();
        let output = lines
            .iter()
            .enumerate()
            .map(|(i, line)| format!("{:>6}\t{}", offset + i + 1, line))
            .collect::<Vec<_>>()
            .join("\n");

        Ok(ToolResult::new(output)
            .with_title(format!("File: {}", inp.file_path))
            .with_metadata(serde_json::json!({
                "file_path": inp.file_path,
                "total_lines": content.lines().count(),
                "offset": offset,
                "limit": limit,
            })))
    }
}

// ── FileWriteTool ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct FileWriteInput {
    /// The absolute path to the file to write
    pub file_path: String,
    /// The content to write to the file
    pub content: String,
}

pub struct FileWriteTool;

#[async_trait]
impl Tool for FileWriteTool {
    fn id(&self) -> &'static str {
        "file_write"
    }

    fn description(&self) -> &'static str {
        "Write a file to the workspace. Overwrites existing files."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<FileWriteInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: FileWriteInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        let path = check_in_workspace(&ctx.workspace, &inp.file_path, &ctx.sandbox_extra_paths)?;

        // Ensure parent directory exists
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }

        let exists = path.exists();
        std::fs::write(&path, &inp.content)?;

        let line_count = inp.content.lines().count();
        let action = if exists { "Updated" } else { "Created" };

        Ok(ToolResult::new(format!(
            "{action} {} ({} lines)",
            inp.file_path, line_count
        ))
        .with_title(format!("Write: {}", inp.file_path))
        .with_metadata(serde_json::json!({
            "file_path": inp.file_path,
            "lines": line_count,
            "existed": exists,
        })))
    }
}

// ── FileEditTool ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct FileEditInput {
    /// The absolute path to the file to modify
    pub file_path: String,
    /// The text to replace
    pub old_string: String,
    /// The text to replace it with (must differ from old_string)
    pub new_string: String,
    /// Replace all occurrences (default: false)
    #[serde(default)]
    pub replace_all: bool,
}

pub struct FileEditTool;

#[async_trait]
impl Tool for FileEditTool {
    fn id(&self) -> &'static str {
        "file_edit"
    }

    fn description(&self) -> &'static str {
        "Perform exact string replacement in a file."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<FileEditInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: FileEditInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        if inp.old_string == inp.new_string {
            return Err(ToolError::InvalidArgs(
                "old_string and new_string must differ".to_string(),
            ));
        }

        let path = check_in_workspace(&ctx.workspace, &inp.file_path, &ctx.sandbox_extra_paths)?;
        let original = read_file_content(&path)?;

        if inp.old_string.is_empty() {
            return Err(ToolError::InvalidArgs("old_string must not be empty".to_string()));
        }

        let occurrences = original.matches(&inp.old_string).count();
        if occurrences == 0 {
            return Err(ToolError::Other(format!(
                "old_string not found in {}",
                inp.file_path
            )));
        }

        if occurrences > 1 && !inp.replace_all {
            return Err(ToolError::Other(format!(
                "old_string found {occurrences} times; set replace_all=true to replace all, or make old_string more specific"
            )));
        }

        let modified = if inp.replace_all {
            original.replace(&inp.old_string, &inp.new_string)
        } else {
            original.replacen(&inp.old_string, &inp.new_string, 1)
        };

        std::fs::write(&path, &modified)?;

        // Generate unified diff
        let diff = similar::TextDiff::from_lines(&original, &modified);
        let diff_str = diff
            .unified_diff()
            .context_radius(3)
            .header(&inp.file_path, &inp.file_path)
            .to_string();

        let replaced = if inp.replace_all { occurrences } else { 1 };

        Ok(ToolResult::new(format!(
            "Applied edit to {}: {} replacement(s)",
            inp.file_path, replaced
        ))
        .with_title(format!("Edit: {}", inp.file_path))
        .with_diff(diff_str)
        .with_metadata(serde_json::json!({
            "file_path": inp.file_path,
            "replacements": replaced,
        })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn temp_workspace() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    fn create_file(dir: &std::path::Path, name: &str, content: &str) {
        let path = dir.join(name);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        let mut f = std::fs::File::create(path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
    }

    #[tokio::test]
    async fn test_file_read() {
        let ws = temp_workspace();
        create_file(ws.path(), "test.txt", "line1\nline2\nline3\n");

        let tool = FileReadTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({"file_path": "test.txt"}),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("line1"));
        assert!(result.output.contains("line2"));
    }

    #[tokio::test]
    async fn test_file_write() {
        let ws = temp_workspace();

        let tool = FileWriteTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({"file_path": "new.txt", "content": "hello world"}),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("Created"));
        let content = std::fs::read_to_string(ws.path().join("new.txt")).unwrap();
        assert_eq!(content, "hello world");
    }

    #[tokio::test]
    async fn test_file_edit() {
        let ws = temp_workspace();
        create_file(ws.path(), "edit.txt", "Hello Alice\nHello Bob\n");

        let tool = FileEditTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({
                    "file_path": "edit.txt",
                    "old_string": "Alice",
                    "new_string": "Charlie",
                    "replace_all": false,
                }),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("1 replacement"));
        let content = std::fs::read_to_string(ws.path().join("edit.txt")).unwrap();
        assert!(content.contains("Charlie"));
        assert!(result.diff.is_some());
    }

    #[tokio::test]
    async fn test_file_read_outside_workspace_blocked() {
        let ws = temp_workspace();

        let tool = FileReadTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({"file_path": "../outside.txt"}),
                &ctx,
            )
            .await;

        assert!(result.is_err());
    }
}
