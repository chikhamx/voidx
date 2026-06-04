//! RepoMap tool — structural map of the codebase.
//!
//! Ported from `src/voidx/tools/repomap.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use async_trait::async_trait;
use std::path::Path;

pub struct RepoMapTool;

#[async_trait]
impl Tool for RepoMapTool {
    fn id(&self) -> &'static str {
        "repo_map"
    }

    fn description(&self) -> &'static str {
        "Structural map of the codebase: file tree with function/class signatures. Use 'detail=overview' for top-level symbols only, 'detail=signatures' for all symbols including methods. Narrow with path or pattern."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Subdirectory to focus on. Defaults to workspace root."
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files, e.g. '*.py' or 'src/**/*.ts'"
                },
                "detail": {
                    "type": "string",
                    "enum": ["overview", "signatures"],
                    "description": "'overview' = top-level symbols only, 'signatures' = all function/class signatures. Default: 'overview'"
                }
            }
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let path = args.get("path").and_then(|v| v.as_str()).unwrap_or(".");
        let pattern = args.get("pattern").and_then(|v| v.as_str()).unwrap_or("**/*");
        let detail = args.get("detail").and_then(|v| v.as_str()).unwrap_or("overview");

        let workspace = &ctx.workspace;
        let target = workspace.join(path);

        if !target.exists() {
            return Ok(ToolResult::new(format!("Path not found: {}", target.display())));
        }

        let mut output = String::new();
        output.push_str(&format!("Codebase map: {} (detail={})\n\n", target.display(), detail));

        // Walk the directory tree
        if let Ok(entries) = walk_dir(&target, pattern, detail) {
            output.push_str(&entries);
        } else {
            output.push_str("(could not read directory)");
        }

        Ok(ToolResult::new(output))
    }
}

fn walk_dir(root: &Path, pattern: &str, detail: &str) -> Result<String, std::io::Error> {
    let mut output = String::new();
    let mut file_count = 0u32;
    let mut dir_count = 0u32;

    fn walk(
        dir: &Path,
        root: &Path,
        pattern: &str,
        detail: &str,
        output: &mut String,
        file_count: &mut u32,
        dir_count: &mut u32,
        depth: usize,
    ) -> Result<(), std::io::Error> {
        let entries = std::fs::read_dir(dir)?;
        let mut entries: Vec<_> = entries.filter_map(|e| e.ok()).collect();
        entries.sort_by_key(|e| {
            let is_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
            (!is_dir, e.file_name())
        });

        for entry in entries {
            let name = entry.file_name().to_string_lossy().to_string();
            // Skip hidden and build directories
            if name.starts_with('.') || name == "node_modules" || name == "target"
                || name == "__pycache__" || name == ".git" || name == "build"
                || name == "dist" || name == ".venv"
            {
                continue;
            }

            let path = entry.path();
            let indent = "  ".repeat(depth);

            if entry.file_type()?.is_dir() {
                *dir_count += 1;
                output.push_str(&format!("{}{}/\n", indent, name));
                walk(&path, root, pattern, detail, output, file_count, dir_count, depth + 1)?;
            } else {
                // Check pattern match
                let rel = path.strip_prefix(root).unwrap_or(&path);
                if matches_pattern(rel.to_string_lossy().as_ref(), pattern) {
                    *file_count += 1;
                    if detail == "signatures" {
                        // Try to extract signatures for code files
                        let sigs = extract_signatures(&path);
                        if sigs.is_empty() {
                            output.push_str(&format!("{}{}\n", indent, name));
                        } else {
                            output.push_str(&format!("{}{}\n", indent, name));
                            for sig in &sigs {
                                output.push_str(&format!("{}  {}\n", indent, sig));
                            }
                        }
                    } else {
                        output.push_str(&format!("{}{}\n", indent, name));
                    }
                }
            }
        }
        Ok(())
    }

    walk(
        root, root, pattern, detail, &mut output, &mut file_count, &mut dir_count, 0,
    )?;

    output.push_str(&format!(
        "\n{} directories, {} files",
        dir_count, file_count
    ));

    Ok(output)
}

fn matches_pattern(path: &str, pattern: &str) -> bool {
    if pattern == "**/*" || pattern == "*" {
        return true;
    }
    // Simple glob matching
    if pattern.starts_with("**/*.") {
        let ext = &pattern[5..];
        return path.ends_with(ext);
    }
    if pattern.starts_with("*.") {
        let ext = &pattern[2..];
        return path.rsplit('/').next().map(|n| n.ends_with(ext)).unwrap_or(false);
    }
    path.contains(pattern)
}

/// Extract function/class signatures from a source file.
fn extract_signatures(path: &Path) -> Vec<String> {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
    let content = std::fs::read_to_string(path).unwrap_or_default();

    match ext {
        "py" => extract_python_signatures(&content),
        "rs" => extract_rust_signatures(&content),
        "ts" | "tsx" | "js" | "jsx" => extract_js_signatures(&content),
        _ => Vec::new(),
    }
}

fn extract_python_signatures(content: &str) -> Vec<String> {
    let mut sigs = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("def ") || trimmed.starts_with("class ") || trimmed.starts_with("async def ") {
            if let Some(end) = trimmed.find(':') {
                sigs.push(trimmed[..end].to_string());
            }
        }
    }
    sigs
}

fn extract_rust_signatures(content: &str) -> Vec<String> {
    let mut sigs = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if (trimmed.starts_with("pub fn ") || trimmed.starts_with("fn ")
            || trimmed.starts_with("pub async fn ") || trimmed.starts_with("async fn ")
            || trimmed.starts_with("pub struct ") || trimmed.starts_with("struct ")
            || trimmed.starts_with("pub enum ") || trimmed.starts_with("enum ")
            || trimmed.starts_with("pub trait ") || trimmed.starts_with("trait ")
            || trimmed.starts_with("impl "))
            && !trimmed.starts_with("//")
        {
            // Take up to the opening brace or semicolon
            let sig = if let Some(pos) = trimmed.find('{') {
                trimmed[..pos].trim_end().to_string()
            } else if let Some(pos) = trimmed.find(';') {
                trimmed[..pos].trim_end().to_string()
            } else {
                trimmed.to_string()
            };
            sigs.push(sig);
        }
    }
    sigs
}

fn extract_js_signatures(content: &str) -> Vec<String> {
    let mut sigs = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if (trimmed.starts_with("export function ") || trimmed.starts_with("function ")
            || trimmed.starts_with("export async function ") || trimmed.starts_with("async function ")
            || trimmed.starts_with("export const ") || trimmed.starts_with("const ")
            || trimmed.starts_with("export class ") || trimmed.starts_with("class "))
            && !trimmed.starts_with("//")
        {
            let sig = if let Some(pos) = trimmed.find('{') {
                trimmed[..pos].trim_end().to_string()
            } else {
                trimmed.to_string()
            };
            sigs.push(sig);
        }
    }
    sigs
}
