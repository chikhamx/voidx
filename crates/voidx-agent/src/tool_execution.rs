//! Tool execution node — extract tool calls from LLM response, authorize,
//! execute in parallel, collect results, and sanitize output.
//!
//! Ported from `src/voidx/agent/graph/tool_execution.py`.
//! This module is the single authority for how tools get run inside the agent loop.

use crate::agents::get_agent;
use crate::error::AgentError;
use crate::tool_messages::sanitize_tool_message_content;
use crate::VoidXAgent;
use std::sync::Arc;
use tokio::sync::RwLock;
use voidx_config::Config;
use voidx_llm::{ChatClient, ChatMessage, ToolCall};
use voidx_permission::{PermissionEngine, PermissionVerdict};
use voidx_tools::base::ToolContext;
use voidx_tools::registry::ToolRegistry;

/// Default max chars for tool output in the main loop.
const TOOL_OUTPUT_MAX_CHARS: usize = 4_000;

/// Result of executing a batch of tool calls.
#[derive(Debug)]
pub struct ToolExecutionResult {
    /// Tool result messages to append to the conversation.
    pub messages: Vec<ChatMessage>,
    /// Number of tools that were executed successfully.
    pub succeeded: usize,
    /// Number of tools that were denied by permissions.
    pub denied: usize,
    /// Number of tools that errored.
    pub errored: usize,
}

/// Execute a batch of tool calls from an LLM response.
///
/// This handles:
/// 1. Permission checks (deny/ask/allow)
/// 2. Subagent delegation (the "agent" tool)
/// 3. Normal tool execution
/// 4. Output sanitization (secret redaction, truncation, path normalization)
pub async fn execute_tools(
    tool_calls: &[ToolCall],
    agent: &VoidXAgent,
    session_id: &str,
    agent_name: &str,
) -> Result<ToolExecutionResult, AgentError> {
    let mut messages: Vec<ChatMessage> = Vec::new();
    let mut succeeded = 0usize;
    let mut denied = 0usize;
    let mut errored = 0usize;

    let tool_ctx = ToolContext {
        workspace: agent.config.workspace.clone(),
        session_id: session_id.to_string(),
        agent: agent_name.to_string(),
        sandbox_extra_paths: agent.config.sandbox_extra_paths.clone(),
        ..Default::default()
    };

    // ── Phase 1: Authorize all tool calls ────────────────────────────────
    let authorized = authorize_tool_calls(
        tool_calls,
        &agent.permission,
        &agent.config.workspace,
        agent_name,
    );

    // ── Phase 2: Execute approved tools (sequential for now; can be parallelized) ──
    for (tc, allowed) in authorized {
        if !allowed {
            denied += 1;
            messages.push(ChatMessage::ToolResult {
                content: "[Permission denied] Tool not authorized for this agent or context."
                    .to_string(),
                tool_call_id: tc.id.clone(),
                name: tc.name.clone(),
            });
            continue;
        }

        // Handle subagent delegation
        if tc.name == "agent" {
            let result = execute_subagent(&tc, &agent.client, &agent.config, &agent.tools, &agent.permission).await;
            match result {
                Ok(output) => {
                    succeeded += 1;
                    let sanitized = sanitize_tool_message_content(
                        &output,
                        Some(agent.config.workspace.to_string_lossy().as_ref()),
                        TOOL_OUTPUT_MAX_CHARS,
                    );
                    messages.push(ChatMessage::ToolResult {
                        content: sanitized,
                        tool_call_id: tc.id.clone(),
                        name: "agent".to_string(),
                    });
                }
                Err(e) => {
                    errored += 1;
                    messages.push(ChatMessage::ToolResult {
                        content: format!("Subagent error: {e}"),
                        tool_call_id: tc.id.clone(),
                        name: "agent".to_string(),
                    });
                }
            }
            continue;
        }

        // Execute normal tool
        let result = agent
            .tools
            .read()
            .await
            .execute(&tc.name, tc.arguments.clone(), &tool_ctx)
            .await;

        match result {
            Ok(tool_result) => {
                succeeded += 1;
                let sanitized = sanitize_tool_message_content(
                    &tool_result.output,
                    Some(agent.config.workspace.to_string_lossy().as_ref()),
                    TOOL_OUTPUT_MAX_CHARS,
                );
                messages.push(ChatMessage::ToolResult {
                    content: sanitized,
                    tool_call_id: tc.id.clone(),
                    name: tc.name.clone(),
                });
            }
            Err(e) => {
                errored += 1;
                messages.push(ChatMessage::ToolResult {
                    content: format!("Tool error: {e}"),
                    tool_call_id: tc.id.clone(),
                    name: tc.name.clone(),
                });
            }
        }
    }

    Ok(ToolExecutionResult {
        messages,
        succeeded,
        denied,
        errored,
    })
}

/// Authorize a list of tool calls against the permission engine.
///
/// Returns a Vec of (ToolCall, bool) where bool indicates if the call is allowed.
fn authorize_tool_calls(
    tool_calls: &[ToolCall],
    permission: &PermissionEngine,
    workspace: &std::path::Path,
    _agent_name: &str,
) -> Vec<(ToolCall, bool)> {
    tool_calls
        .iter()
        .map(|tc| {
            let verdict = permission.evaluate(&tc.name, &tc.arguments, workspace);
            match verdict {
                Ok(PermissionVerdict::Allow) => (tc.clone(), true),
                Ok(PermissionVerdict::AllowWithFailureCheck { .. }) => (tc.clone(), true),
                Ok(PermissionVerdict::Deny(_reason)) => (tc.clone(), false),
                Ok(PermissionVerdict::AskUser(_reason)) => {
                    // In headless/non-interactive mode, auto-deny
                    (tc.clone(), false)
                }
                Err(_) => (tc.clone(), false),
            }
        })
        .collect()
}

/// Execute a subagent tool call.
async fn execute_subagent(
    tc: &ToolCall,
    client: &Arc<dyn ChatClient>,
    config: &Config,
    tools: &Arc<RwLock<ToolRegistry>>,
    permission: &PermissionEngine,
) -> Result<String, AgentError> {
    let subagent_name = tc
        .arguments
        .get("agent_type")
        .and_then(|v| v.as_str())
        .unwrap_or("explore");
    let subagent_desc = tc
        .arguments
        .get("prompt")
        .and_then(|v| v.as_str())
        .unwrap_or("Investigate and report.");

    let sub_def = get_agent(subagent_name).unwrap_or_else(|| crate::agents::explore());

    crate::subagent::run_subagent(&sub_def, subagent_desc, client, config, tools, permission).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_authorize_empty_calls() {
        let permission = PermissionEngine::new(
            voidx_permission::SandboxMode::Workspace,
            true,
            voidx_permission::ApprovalPolicy::Auto,
            vec![],
        );
        let workspace = std::path::Path::new("/tmp");
        let result = authorize_tool_calls(&[], &permission, workspace, "orchestrator");
        assert!(result.is_empty());
    }
}
