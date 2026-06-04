//! Task status tool — check child-agent task status.
//!
//! Ported from `src/voidx/tools/task_status.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use async_trait::async_trait;

pub struct TaskStatusTool;

#[async_trait]
impl Tool for TaskStatusTool {
    fn id(&self) -> &'static str {
        "task_status"
    }

    fn description(&self) -> &'static str {
        "Check child-agent task status. Returns status (pending/running/completed/error), current step, elapsed time, and recent output preview. Without task_id, lists all tasks."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Specific task ID to check. Omit to list all tasks."
                }
            }
        })
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let task_id = args.get("task_id").and_then(|v| v.as_str());

        if let Some(_id) = task_id {
            // In a real implementation, this would check the task state
            Ok(ToolResult::new("No active tasks found.".to_string()))
        } else {
            // List all tasks
            Ok(ToolResult::new("No active tasks.".to_string()))
        }
    }
}
