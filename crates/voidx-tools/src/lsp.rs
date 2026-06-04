//! LSP tools — diagnostics, symbols, definition, references, format.
//!
//! Ported from `src/voidx/tools/lsp.py`.
//! These tools delegate to the LSP manager for actual operations.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use async_trait::async_trait;

// ── LspDiagnosticsTool ──────────────────────────────────────────────────────

pub struct LspDiagnosticsTool;

#[async_trait]
impl Tool for LspDiagnosticsTool {
    fn id(&self) -> &'static str {
        "lsp_diagnostics"
    }

    fn description(&self) -> &'static str {
        "Get LSP diagnostics (errors, warnings) for a file. Returns a list of issues with severity, message, and location."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file"
                }
            },
            "required": ["file_path"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let file_path = args.get("file_path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("file_path required".into()))?;

        let resolved = crate::base::resolve_safe(
            &ctx.workspace,
            file_path,
            &ctx.sandbox_extra_paths,
        );

        match resolved {
            Some(path) => {
                // In a full implementation, this would call LspManager::diagnostics
                // For now, return a placeholder
                Ok(ToolResult::new(format!(
                    "LSP diagnostics for {} (LSP integration pending)",
                    path.display()
                )))
            }
            None => Ok(ToolResult::new(format!(
                "File outside workspace: {}",
                file_path
            ))),
        }
    }
}

// ── LspSymbolsTool ──────────────────────────────────────────────────────────

pub struct LspSymbolsTool;

#[async_trait]
impl Tool for LspSymbolsTool {
    fn id(&self) -> &'static str {
        "lsp_symbols"
    }

    fn description(&self) -> &'static str {
        "Get document symbols (functions, classes, variables) for a file. Returns symbol names, kinds, and locations."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file"
                }
            },
            "required": ["file_path"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let file_path = args.get("file_path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("file_path required".into()))?;

        let resolved = crate::base::resolve_safe(
            &ctx.workspace,
            file_path,
            &ctx.sandbox_extra_paths,
        );

        match resolved {
            Some(path) => {
                Ok(ToolResult::new(format!(
                    "LSP symbols for {} (LSP integration pending)",
                    path.display()
                )))
            }
            None => Ok(ToolResult::new(format!(
                "File outside workspace: {}",
                file_path
            ))),
        }
    }
}

// ── LspDefinitionTool ───────────────────────────────────────────────────────

pub struct LspDefinitionTool;

#[async_trait]
impl Tool for LspDefinitionTool {
    fn id(&self) -> &'static str {
        "lsp_definition"
    }

    fn description(&self) -> &'static str {
        "Go to definition of a symbol at a position in a file. Returns the location(s) of the definition."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file"
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-based)"
                },
                "character": {
                    "type": "integer",
                    "description": "Character offset (1-based)"
                }
            },
            "required": ["file_path", "line", "character"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let file_path = args.get("file_path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("file_path required".into()))?;
        let line = args.get("line").and_then(|v| v.as_u64()).unwrap_or(1);
        let character = args.get("character").and_then(|v| v.as_u64()).unwrap_or(1);

        let resolved = crate::base::resolve_safe(
            &ctx.workspace,
            file_path,
            &ctx.sandbox_extra_paths,
        );

        match resolved {
            Some(path) => {
                Ok(ToolResult::new(format!(
                    "LSP definition at {}:{}:{} (LSP integration pending)",
                    path.display(), line, character
                )))
            }
            None => Ok(ToolResult::new(format!(
                "File outside workspace: {}",
                file_path
            ))),
        }
    }
}

// ── LspReferencesTool ───────────────────────────────────────────────────────

pub struct LspReferencesTool;

#[async_trait]
impl Tool for LspReferencesTool {
    fn id(&self) -> &'static str {
        "lsp_references"
    }

    fn description(&self) -> &'static str {
        "Find all references to a symbol at a position in a file. Returns locations of all references."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file"
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-based)"
                },
                "character": {
                    "type": "integer",
                    "description": "Character offset (1-based)"
                }
            },
            "required": ["file_path", "line", "character"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let file_path = args.get("file_path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("file_path required".into()))?;
        let line = args.get("line").and_then(|v| v.as_u64()).unwrap_or(1);
        let character = args.get("character").and_then(|v| v.as_u64()).unwrap_or(1);

        let resolved = crate::base::resolve_safe(
            &ctx.workspace,
            file_path,
            &ctx.sandbox_extra_paths,
        );

        match resolved {
            Some(path) => {
                Ok(ToolResult::new(format!(
                    "LSP references at {}:{}:{} (LSP integration pending)",
                    path.display(), line, character
                )))
            }
            None => Ok(ToolResult::new(format!(
                "File outside workspace: {}",
                file_path
            ))),
        }
    }
}

// ── LspFormatTool ───────────────────────────────────────────────────────────

pub struct LspFormatTool;

#[async_trait]
impl Tool for LspFormatTool {
    fn id(&self) -> &'static str {
        "lsp_format"
    }

    fn description(&self) -> &'static str {
        "Format a file using the LSP server. Returns the formatted content or a diff."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file"
                }
            },
            "required": ["file_path"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let file_path = args.get("file_path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("file_path required".into()))?;

        let resolved = crate::base::resolve_safe(
            &ctx.workspace,
            file_path,
            &ctx.sandbox_extra_paths,
        );

        match resolved {
            Some(path) => {
                Ok(ToolResult::new(format!(
                    "LSP format for {} (LSP integration pending)",
                    path.display()
                )))
            }
            None => Ok(ToolResult::new(format!(
                "File outside workspace: {}",
                file_path
            ))),
        }
    }
}
