//! Todo tool — update the todo list so progress is visible.
//!
//! Ported from `src/voidx/tools/todo.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use async_trait::async_trait;

pub struct TodoWriteTool;

#[async_trait]
impl Tool for TodoWriteTool {
    fn id(&self) -> &'static str {
        "todo"
    }

    fn description(&self) -> &'static str {
        "Update the todo list so progress is visible. Each call REPLACES the entire list — pass the full updated list. Items: [{id, status, content}] Status: pending → in_progress → completed."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Full todo list — replaces the previous list. Items:[{id:string, status:pending|in_progress|completed, content:string}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": { "type": "string" },
                            "status": { "type": "string", "enum": ["pending", "in_progress", "completed"] },
                            "content": { "type": "string" }
                        },
                        "required": ["id", "status", "content"]
                    }
                }
            },
            "required": ["todos"]
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let todos = args.get("todos").and_then(|v| v.as_array()).cloned().unwrap_or_default();

        let mut output = String::new();
        let total = todos.len();
        let completed = todos.iter()
            .filter(|t| t.get("status").and_then(|s| s.as_str()) == Some("completed"))
            .count();

        output.push_str(&format!("Todo list updated ({} items, {} completed)\n\n", total, completed));

        for item in &todos {
            let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("?");
            let status = item.get("status").and_then(|v| v.as_str()).unwrap_or("pending");
            let content = item.get("content").and_then(|v| v.as_str()).unwrap_or("");

            let icon = match status {
                "completed" => "✓",
                "in_progress" => "◐",
                _ => "○",
            };
            output.push_str(&format!("{} [{}] {}\n", icon, id, content));
        }

        Ok(ToolResult::new(output))
    }
}
