//! Grep tool — regex content search over workspace files.
//!
//! Ported from `src/voidx/tools/search.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use crate::schema::model_to_json_schema;
use async_trait::async_trait;
use regex::Regex;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use walkdir::WalkDir;

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct GrepInput {
    /// The regular expression pattern to search for
    pub pattern: String,
    /// File or directory to search in (defaults to workspace root)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    /// Glob pattern to filter files (e.g., "*.rs")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub glob: Option<String>,
    /// Output mode: "content" (matching lines), "files_with_matches", or "count"
    #[serde(default = "default_output_mode")]
    pub output_mode: String,
    /// Max results (default: 250)
    #[serde(default = "default_head_limit")]
    pub head_limit: usize,
    /// Case insensitive search
    #[serde(default)]
    #[serde(rename = "-i")]
    pub case_insensitive: bool,
}

fn default_output_mode() -> String {
    "files_with_matches".to_string()
}

fn default_head_limit() -> usize {
    250
}

pub struct GrepTool;

#[async_trait]
impl Tool for GrepTool {
    fn id(&self) -> &'static str {
        "grep"
    }

    fn description(&self) -> &'static str {
        "Search file contents using regex. Returns matching lines, file paths, or counts."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<GrepInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: GrepInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        let regex = Regex::new(&inp.pattern)
            .map_err(|e| ToolError::InvalidArgs(format!("Invalid regex: {e}")))?;

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

        let glob_pattern = inp.glob.as_deref().map(|g| {
            glob::Pattern::new(g).map_err(|e| ToolError::InvalidArgs(format!("Invalid glob: {e}")))
        }).transpose()?;

        let mut results: Vec<String> = Vec::new();
        let mut file_count = 0u64;

        for entry in WalkDir::new(&search_root)
            .follow_links(false)
            .max_depth(50)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            if !entry.file_type().is_file() {
                continue;
            }

            // Skip binary files by extension
            let path = entry.path();
            if is_binary(path) {
                continue;
            }

            // Glob filter
            if let Some(ref glob) = glob_pattern {
                let relative = path.strip_prefix(&search_root).unwrap_or(path);
                if !glob.matches_path(relative) {
                    continue;
                }
            }

            // Skip hidden files and common ignores
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.starts_with('.') && name != ".gitignore" {
                    continue;
                }
            }

            let content = match std::fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue,
            };

            let matches: Vec<(usize, &str)> = content
                .lines()
                .enumerate()
                .filter(|(_, line)| regex.is_match(line))
                .collect();

            if matches.is_empty() {
                continue;
            }

            file_count += 1;
            let relative = path.strip_prefix(&search_root).unwrap_or(path);

            match inp.output_mode.as_str() {
                "count" => {
                    results.push(format!(
                        "{}: {} matches",
                        relative.display(),
                        matches.len()
                    ));
                }
                "files_with_matches" => {
                    results.push(relative.display().to_string());
                }
                _ => {
                    // content mode
                    for (line_num, line) in &matches {
                        results.push(format!(
                            "{}:{}:{}",
                            relative.display(),
                            line_num + 1,
                            line
                        ));
                    }
                }
            }

            if results.len() >= inp.head_limit {
                break;
            }
        }

        let output = if results.is_empty() {
            format!("No matches found for pattern: {}", inp.pattern)
        } else {
            results.join("\n")
        };

        Ok(ToolResult::new(output).with_metadata(serde_json::json!({
            "pattern": inp.pattern,
            "files_matched": file_count,
            "matches_shown": results.len(),
            "output_mode": inp.output_mode,
        })))
    }
}

fn is_binary(path: &std::path::Path) -> bool {
    let binary_extensions = [
        "exe", "dll", "so", "dylib", "obj", "o", "a", "lib",
        "png", "jpg", "jpeg", "gif", "ico", "bmp", "webp",
        "mp3", "mp4", "avi", "mov", "mkv",
        "zip", "tar", "gz", "bz2", "xz", "7z", "rar",
        "pdf", "doc", "docx", "xls", "xlsx",
        "pyc", "pyo", "class", "wasm",
        "ttf", "otf", "woff", "woff2",
    ];
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| binary_extensions.contains(&e.to_lowercase().as_str()))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn create_workspace() -> tempfile::TempDir {
        let ws = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(ws.path().join("test.rs")).unwrap();
        f.write_all(b"fn main() {\n    println!(\"hello\");\n}\n").unwrap();
        let mut f2 = std::fs::File::create(ws.path().join("lib.rs")).unwrap();
        f2.write_all(b"pub fn greet() {}\n").unwrap();
        ws
    }

    #[tokio::test]
    async fn test_grep_find_function() {
        let ws = create_workspace();
        let tool = GrepTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({
                    "pattern": "fn",
                    "output_mode": "content",
                }),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("fn main()"));
        assert!(result.output.contains("pub fn greet()"));
    }

    #[tokio::test]
    async fn test_grep_files_with_matches() {
        let ws = create_workspace();
        let tool = GrepTool;
        let ctx = ToolContext {
            workspace: ws.path().to_path_buf(),
            ..Default::default()
        };

        let result = tool
            .execute(
                serde_json::json!({
                    "pattern": "fn",
                    "output_mode": "files_with_matches",
                }),
                &ctx,
            )
            .await
            .unwrap();

        assert!(result.output.contains("test.rs"));
        assert!(result.output.contains("lib.rs"));
    }
}
