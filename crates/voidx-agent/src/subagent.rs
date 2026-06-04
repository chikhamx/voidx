//! Subagent runner — spawn child agents with restricted tools.
//!
//! Ported from `src/voidx/agent/graph/subagent.py`.
//! Includes: parent context filtering, tool filtering, subagent run loop.

use crate::agents::AgentDef;
use crate::error::AgentError;
use crate::prompt;
use crate::state::{AgentState, InteractionMode};
use crate::streaming::StreamAccumulator;
use crate::tool_messages::sanitize_tool_message_content;
use futures::StreamExt;
use voidx_config::Config;
use voidx_llm::{ChatClient, ChatMessage, ToolCall};
use voidx_permission::{PermissionEngine, PermissionVerdict};
use voidx_tools::base::ToolContext;
use std::sync::Arc;
use tokio::sync::RwLock;
use voidx_tools::registry::ToolRegistry;

/// Default max chars for tool output in subagent context.
const SUBAGENT_TOOL_OUTPUT_MAX_CHARS: usize = 4_000;

/// Run a subagent and return its final result.
pub async fn run_subagent(
    agent_def: &AgentDef,
    description: &str,
    client: &Arc<dyn ChatClient>,
    config: &Config,
    tools: &Arc<RwLock<ToolRegistry>>,
    permission: &PermissionEngine,
) -> Result<String, AgentError> {
    let mut state = AgentState::new(description);
    state.agent = agent_def.name.clone();
    state.max_steps = agent_def.max_steps;

    // Build context
    let context = prompt::build_context(&prompt::ContextOptions {
        config,
        agent_def,
        interaction_mode: InteractionMode::Auto,
        summary: None,
        instructions: None,
        skills: None,
        task_intent: None,
        goal: None,
        goal_phase: None,
        goal_status: None,
        approved_scope: None,
    });

    // Insert system prompt
    state.messages.insert(0, ChatMessage::system(&context.system_prompt));
    if let Some(summary) = &context.summary {
        if !summary.is_empty() {
            state.messages.insert(1, ChatMessage::system(summary));
        }
    }

    // ── Run loop (simplified, no streaming) ──────────────────────────
    while state.should_continue && state.step_count < state.max_steps {
        state.prepare_step();

        // Get tool definitions for this agent (exclude agent/task_status to prevent nesting)
        let tool_defs = get_subagent_tools(agent_def, tools).await;

        // Call LLM
        let response = client.invoke(&state.messages, &tool_defs).await?;

        match &response {
            ChatMessage::Assistant {
                content,
                tool_calls,
            } => {
                if tool_calls.is_empty() {
                    state.should_continue = false;
                    return Ok(content.clone());
                }

                // Append assistant message
                state.messages.push(response);

                // Execute tools
                let tool_ctx = ToolContext {
                    workspace: config.workspace.clone(),
                    session_id: format!("subagent-{}", agent_def.name),
                    agent: agent_def.name.clone(),
                    ..Default::default()
                };

                // Check each tool call
                let mut tool_results: Vec<ChatMessage> = Vec::new();
                for tc in tool_calls {
                    let verdict = permission.evaluate(
                        &tc.name,
                        &tc.arguments,
                        &config.workspace,
                    )?;

                    match verdict {
                        PermissionVerdict::Allow | PermissionVerdict::AllowWithFailureCheck { .. } => {}
                        PermissionVerdict::Deny(reason) => {
                            tool_results.push(ChatMessage::ToolResult {
                                content: format!("Permission denied: {reason}"),
                                tool_call_id: tc.id.clone(),
                                name: tc.name.clone(),
                            });
                            continue;
                        }
                        PermissionVerdict::AskUser(reason) => {
                            // In subagent mode, auto-deny AskUser
                            tool_results.push(ChatMessage::ToolResult {
                                content: format!(
                                    "Approval required (auto-denied in subagent): {reason}"
                                ),
                                tool_call_id: tc.id.clone(),
                                name: tc.name.clone(),
                            });
                            continue;
                        }
                    }

                    // Execute the tool
                    let result = tools
                        .read()
                        .await
                        .execute(&tc.name, tc.arguments.clone(), &tool_ctx)
                        .await
                        .unwrap_or_else(|e| {
                            voidx_tools::base::ToolResult::new(format!("Tool error: {e}"))
                        });

                    // Sanitize tool output
                    let sanitized = sanitize_tool_message_content(
                        &result.output,
                        Some(config.workspace.to_string_lossy().as_ref()),
                        SUBAGENT_TOOL_OUTPUT_MAX_CHARS,
                    );

                    tool_results.push(ChatMessage::ToolResult {
                        content: sanitized,
                        tool_call_id: tc.id.clone(),
                        name: tc.name.clone(),
                    });
                }

                state.messages.extend(tool_results);
            }
            ChatMessage::System { content } => {
                // Shouldn't happen from LLM, but handle gracefully
                state.should_continue = false;
                return Ok(content.clone());
            }
            _ => {
                state.should_continue = false;
                return Ok(String::new());
            }
        }
    }

    // If we exited the loop without a final response, return the last assistant content
    let last_assistant = state.messages.iter().rev().find_map(|m| {
        if let ChatMessage::Assistant { content, tool_calls } = m {
            if tool_calls.is_empty() && !content.is_empty() {
                return Some(content.clone());
            }
        }
        None
    });

    Ok(last_assistant.unwrap_or_else(|| "[Subagent completed without text response]".to_string()))
}

/// Run a subagent with parent context (messages from the parent conversation).
///
/// This filters the parent messages to exclude:
/// - System prompts (the subagent gets its own)
/// - AIMessages that contain agent-spawning tool calls
/// - ToolResults that belong to agent-spawning tool calls
pub async fn run_subagent_with_parent_context(
    agent_def: &AgentDef,
    description: &str,
    client: &Arc<dyn ChatClient>,
    config: &Config,
    tools: &Arc<RwLock<ToolRegistry>>,
    permission: &PermissionEngine,
    parent_messages: &[ChatMessage],
) -> Result<String, AgentError> {
    // Filter parent context
    let filtered = filter_parent_context(parent_messages);

    let mut state = AgentState::new(description);
    state.agent = agent_def.name.clone();
    state.max_steps = agent_def.max_steps;

    // Build context
    let context = prompt::build_context(&prompt::ContextOptions {
        config,
        agent_def,
        interaction_mode: InteractionMode::Auto,
        summary: None,
        instructions: None,
        skills: None,
        task_intent: None,
        goal: None,
        goal_phase: None,
        goal_status: None,
        approved_scope: None,
    });

    // Insert system prompt
    state.messages.insert(0, ChatMessage::system(&context.system_prompt));
    if let Some(summary) = &context.summary {
        if !summary.is_empty() {
            state.messages.insert(1, ChatMessage::system(summary));
        }
    }

    // Add filtered parent context
    state.messages.extend(filtered);

    // Add the task description as a user message
    state.messages.push(ChatMessage::user(description));

    // ── Run loop ─────────────────────────────────────────────────────
    while state.should_continue && state.step_count < state.max_steps {
        state.prepare_step();

        let tool_defs = get_subagent_tools(agent_def, tools).await;
        let response = client.invoke(&state.messages, &tool_defs).await?;

        match &response {
            ChatMessage::Assistant { content, tool_calls } => {
                if tool_calls.is_empty() {
                    state.should_continue = false;
                    return Ok(content.clone());
                }

                state.messages.push(response);

                let tool_ctx = ToolContext {
                    workspace: config.workspace.clone(),
                    session_id: format!("subagent-{}", agent_def.name),
                    agent: agent_def.name.clone(),
                    ..Default::default()
                };

                let mut tool_results: Vec<ChatMessage> = Vec::new();
                for tc in tool_calls {
                    let verdict = permission.evaluate(&tc.name, &tc.arguments, &config.workspace)?;

                    match verdict {
                        PermissionVerdict::Allow | PermissionVerdict::AllowWithFailureCheck { .. } => {}
                        PermissionVerdict::Deny(reason) => {
                            tool_results.push(ChatMessage::ToolResult {
                                content: format!("Permission denied: {reason}"),
                                tool_call_id: tc.id.clone(),
                                name: tc.name.clone(),
                            });
                            continue;
                        }
                        PermissionVerdict::AskUser(reason) => {
                            tool_results.push(ChatMessage::ToolResult {
                                content: format!(
                                    "Approval required (auto-denied in subagent): {reason}"
                                ),
                                tool_call_id: tc.id.clone(),
                                name: tc.name.clone(),
                            });
                            continue;
                        }
                    }

                    let result = tools
                        .read()
                        .await
                        .execute(&tc.name, tc.arguments.clone(), &tool_ctx)
                        .await
                        .unwrap_or_else(|e| {
                            voidx_tools::base::ToolResult::new(format!("Tool error: {e}"))
                        });

                    let sanitized = sanitize_tool_message_content(
                        &result.output,
                        Some(config.workspace.to_string_lossy().as_ref()),
                        SUBAGENT_TOOL_OUTPUT_MAX_CHARS,
                    );

                    tool_results.push(ChatMessage::ToolResult {
                        content: sanitized,
                        tool_call_id: tc.id.clone(),
                        name: tc.name.clone(),
                    });
                }

                state.messages.extend(tool_results);
            }
            _ => {
                state.should_continue = false;
                return Ok(String::new());
            }
        }
    }

    let last_assistant = state.messages.iter().rev().find_map(|m| {
        if let ChatMessage::Assistant { content, tool_calls } = m {
            if tool_calls.is_empty() && !content.is_empty() {
                return Some(content.clone());
            }
        }
        None
    });

    Ok(last_assistant.unwrap_or_else(|| "[Subagent completed without text response]".to_string()))
}

/// Filter parent messages for subagent context.
///
/// Rules:
/// 1. Skip SystemMessages (subagent gets its own system prompt)
/// 2. Skip AIMessages that contain agent-spawning tool calls
/// 3. Skip ToolResults whose tool_call_id belongs to a skipped agent call
pub fn filter_parent_context(messages: &[ChatMessage]) -> Vec<ChatMessage> {
    // Phase 1: Collect IDs of agent-spawning tool calls
    let mut skipped_tool_call_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

    for msg in messages {
        if let ChatMessage::Assistant { tool_calls, .. } = msg {
            for tc in tool_calls {
                if tc.name == "agent" {
                    skipped_tool_call_ids.insert(tc.id.clone());
                }
            }
        }
    }

    // Phase 2: Filter messages
    let mut filtered: Vec<ChatMessage> = Vec::new();

    for msg in messages {
        match msg {
            // Skip system messages
            ChatMessage::System { .. } => continue,

            // Skip assistant messages that contain agent-spawning tool calls
            ChatMessage::Assistant { tool_calls, .. } => {
                if tool_calls.iter().any(|tc| tc.name == "agent") {
                    continue;
                }
                filtered.push(msg.clone());
            }

            // Skip tool results that belong to skipped agent calls
            ChatMessage::ToolResult { tool_call_id, .. } => {
                if skipped_tool_call_ids.contains(tool_call_id) {
                    continue;
                }
                filtered.push(msg.clone());
            }

            // Keep user messages
            ChatMessage::User { .. } => {
                filtered.push(msg.clone());
            }
        }
    }

    filtered
}

/// Get tool definitions for a subagent (excluding agent/task_status to prevent nesting).
async fn get_subagent_tools(
    agent_def: &AgentDef,
    registry: &Arc<RwLock<ToolRegistry>>,
) -> Vec<voidx_llm::ToolDefinition> {
    let all = registry.read().await.tools_for_llm();

    let allowed: std::collections::HashSet<&str> = agent_def
        .tools
        .iter()
        .map(|s| s.as_str())
        .collect();

    let blocked: std::collections::HashSet<&str> = ["agent", "task_status"].into_iter().collect();

    all.iter()
        .filter(|t| {
            let name = t["function"]["name"].as_str().unwrap_or("");
            !blocked.contains(name) && (allowed.is_empty() || allowed.contains(name))
        })
        .map(|t| voidx_llm::ToolDefinition {
            name: t["function"]["name"].as_str().unwrap_or("").to_string(),
            description: t["function"]["description"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            parameters: t["function"]["parameters"].clone(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_filter_parent_context_removes_system() {
        let messages = vec![
            ChatMessage::system("system prompt"),
            ChatMessage::user("hello"),
            ChatMessage::assistant("hi"),
        ];
        let filtered = filter_parent_context(&messages);
        assert_eq!(filtered.len(), 2);
        assert!(matches!(filtered[0], ChatMessage::User { .. }));
        assert!(matches!(filtered[1], ChatMessage::Assistant { .. }));
    }

    #[test]
    fn test_filter_parent_context_removes_agent_calls() {
        let messages = vec![
            ChatMessage::user("do something"),
            ChatMessage::assistant_with_tools(
                "delegating",
                vec![ToolCall {
                    id: "tc1".to_string(),
                    name: "agent".to_string(),
                    arguments: serde_json::json!({"agent_type": "explore"}),
                }],
            ),
            ChatMessage::ToolResult {
                content: "result".to_string(),
                tool_call_id: "tc1".to_string(),
                name: "agent".to_string(),
            },
            ChatMessage::assistant("done"),
        ];
        let filtered = filter_parent_context(&messages);
        assert_eq!(filtered.len(), 2); // user + final assistant
        assert!(matches!(filtered[0], ChatMessage::User { .. }));
        assert!(matches!(filtered[1], ChatMessage::Assistant { .. }));
    }

    #[test]
    fn test_filter_parent_context_keeps_normal_tool_calls() {
        let messages = vec![
            ChatMessage::user("read file"),
            ChatMessage::assistant_with_tools(
                "",
                vec![ToolCall {
                    id: "tc2".to_string(),
                    name: "read".to_string(),
                    arguments: serde_json::json!({"path": "/tmp/f.rs"}),
                }],
            ),
            ChatMessage::ToolResult {
                content: "file contents".to_string(),
                tool_call_id: "tc2".to_string(),
                name: "read".to_string(),
            },
        ];
        let filtered = filter_parent_context(&messages);
        assert_eq!(filtered.len(), 3); // all kept
    }
}
