//! Subagent runner — spawn child agents with restricted tools.
//!
//! Ported from `src/voidx/agent/graph_components/subagent.py`.

use crate::agents::AgentDef;
use crate::error::AgentError;
use crate::prompt;
use crate::state::{AgentState, InteractionMode};
use voidx_config::Config;
use voidx_llm::{ChatClient, ChatMessage};
use voidx_permission::{PermissionEngine, PermissionVerdict};
use voidx_tools::base::ToolContext;
use std::sync::Arc;
use tokio::sync::RwLock;
use voidx_tools::registry::ToolRegistry;

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
    let context = prompt::build_context(
        config,
        agent_def,
        InteractionMode::Auto,
        None,
        None,
    );

    // Insert system prompt
    state.messages.insert(0, ChatMessage::system(&context.system_prompt));
    if let Some(summary) = &context.summary {
        state.messages.insert(1, ChatMessage::system(summary));
    }

    // ── Run loop (simplified, no streaming) ──────────────────────────
    while state.should_continue && state.step_count < state.max_steps {
        state.prepare_step();

        // Get tool definitions for this agent
        let tool_defs = if agent_def.tools.is_empty() {
            tools.read().await.tools_for_llm()
        } else {
            let all = tools.read().await.tools_for_llm();
            all.into_iter()
                .filter(|t| {
                    agent_def
                        .tools
                        .contains(&t["function"]["name"].as_str().unwrap_or("").to_string())
                })
                .collect()
        };

        // Call LLM
        let response = client.invoke(&state.messages, &[]).await?;

        match &response {
            ChatMessage::Assistant {
                content,
                tool_calls,
            } => {
                if tool_calls.is_empty() {
                    state.should_continue = false;
                    return Ok(content.clone());
                }

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
                        PermissionVerdict::Allow => {}
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
                                content: format!("Approval required: {reason}"),
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
                            voidx_tools::base::ToolResult::new(format!("Error: {e}"))
                        });

                    tool_results.push(ChatMessage::ToolResult {
                        content: result.output,
                        tool_call_id: tc.id.clone(),
                        name: tc.name.clone(),
                    });
                }

                // Append assistant msg + tool results
                state.messages.push(response.clone());
                state.messages.extend(tool_results);
            }
            _ => {
                state.should_continue = false;
                return Ok("Subagent returned unexpected response".to_string());
            }
        }
    }

    // Get the last assistant message as the result
    let last = state
        .messages
        .iter()
        .rev()
        .find(|m| matches!(m, ChatMessage::Assistant { .. }));

    match last {
        Some(ChatMessage::Assistant { content, .. }) => Ok(content.clone()),
        _ => Ok("Subagent completed without output.".to_string()),
    }
}
