//! Agent run loop — the main state machine that replaces LangGraph.
//!
//! Ported from `src/voidx/agent/graph.py` + `run_loop.py`.

use crate::agents::{get_agent, AgentDef};
use crate::compaction::{compact, should_compact};
use crate::error::AgentError;
use crate::prompt;
use crate::state::AgentState;
use crate::streaming::StreamAccumulator;
use crate::VoidXAgent;
use futures::StreamExt;
use voidx_llm::{ChatClient, ChatMessage, ToolCall};
use voidx_permission::PermissionVerdict;
use voidx_tools::base::ToolContext;
use std::sync::Arc;
use tokio::sync::RwLock;
use voidx_tools::registry::ToolRegistry;

/// Outcome of a single agent run.
#[derive(Debug)]
pub struct RunResult {
    pub messages: Vec<ChatMessage>,
    pub steps: u32,
    pub compaction_applied: bool,
}

/// Run the agent loop for a single user request.
pub async fn run(
    agent: &VoidXAgent,
    state: &mut AgentState,
    session_id: &str,
) -> Result<RunResult, AgentError> {
    let agent_def = get_agent(&state.agent).unwrap_or_else(|| crate::agents::orchestrator());

    // Build initial context with full runtime state
    let instructions = prompt::load_instructions(&agent.config.workspace);
    let context = prompt::build_context(&prompt::ContextOptions {
        config: &agent.config,
        agent_def: &agent_def,
        interaction_mode: state.interaction_mode,
        summary: if state.compaction_summary.is_empty() { None } else { Some(state.compaction_summary.as_str()) },
        instructions: instructions.as_deref(),
        skills: None,
        task_intent: Some(state.task_intent),
        goal: if state.goal.is_empty() { None } else { Some(state.goal.as_str()) },
        goal_phase: Some(state.goal_phase),
        goal_status: Some(state.goal_status),
        approved_scope: if state.approved_scope.is_empty() { None } else { Some(state.approved_scope.as_str()) },
    });

    // Insert system prompt at the front
    state.messages.insert(0, ChatMessage::system(&context.system_prompt));
    if let Some(summary) = &context.summary {
        if !summary.is_empty() {
            state.messages.insert(1, ChatMessage::system(summary));
        }
    }

    let mut compaction_applied = false;

    // ── MAIN LOOP ──────────────────────────────────────────────────────
    loop {
        state.prepare_step();

        // Step budget check
        if state.step_count > state.max_steps {
            state.messages.push(ChatMessage::assistant(
                format!("[Max steps ({}) reached]", state.max_steps),
            ));
            state.should_continue = false;
            break;
        }

        // Compaction check
        if should_compact(&state.messages, 0.0) {
            if let Ok(summary) = compact(
                agent.client.as_ref(),
                &state.messages,
                85.0,
            )
            .await
            {
                state.compaction_summary = summary.clone();
                // Remove compacted messages, keep system + last 10
                let keep = state.messages.split_off(
                    state.messages.len().saturating_sub(10),
                );
                state.messages = keep;
                state.messages.insert(0, ChatMessage::system(&summary));
                compaction_applied = true;
            }
        }

        // Get tool definitions for this agent
        let tool_defs = get_agent_tools(&agent_def, &agent.tools).await;

        // ── Call LLM ──────────────────────────────────────────────────
        let (response, tool_calls) = if agent.debug {
            // Streaming mode
            call_llm_stream(agent.client.as_ref(), &state.messages, &tool_defs).await?
        } else {
            // Non-streaming mode
            call_llm(agent.client.as_ref(), &state.messages, &tool_defs).await?
        };

        // No tool calls → done
        if tool_calls.is_empty() {
            state.messages.push(response);
            state.should_continue = false;
            break;
        }

        // Has tool calls but no budget → done
        if !state.has_tool_budget() {
            state.messages.push(ChatMessage::assistant(
                "Cannot execute more tools: step budget exhausted.",
            ));
            state.should_continue = false;
            break;
        }

        // Append assistant message with tool calls
        state.messages.push(response);

        // ── Execute tools ─────────────────────────────────────────────
        let tool_ctx = ToolContext {
            workspace: agent.config.workspace.clone(),
            session_id: session_id.to_string(),
            agent: state.agent.clone(),
            sandbox_extra_paths: agent.config.sandbox_extra_paths.clone(),
            ..Default::default()
        };

        let mut tool_results: Vec<ChatMessage> = Vec::new();

        for tc in &tool_calls {
            // Permission check
            let verdict = agent
                .permission
                .evaluate(&tc.name, &tc.arguments, &agent.config.workspace)?;

            match verdict {
                PermissionVerdict::Deny(reason) => {
                    tool_results.push(ChatMessage::ToolResult {
                        content: format!("[Permission denied] {reason}"),
                        tool_call_id: tc.id.clone(),
                        name: tc.name.clone(),
                    });
                    continue;
                }
                PermissionVerdict::AskUser(reason) => {
                    // In headless mode, deny. In interactive mode, would prompt.
                    tool_results.push(ChatMessage::ToolResult {
                        content: format!(
                            "[Approval required] {reason}. Use the interactive mode to approve."
                        ),
                        tool_call_id: tc.id.clone(),
                        name: tc.name.clone(),
                    });
                    continue;
                }
                PermissionVerdict::Allow | PermissionVerdict::AllowWithFailureCheck { .. } => {}
            }

            // Handle subagent delegation
            if tc.name == "agent" {
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

                if let Some(sub_def) = get_agent(subagent_name) {
                    let result = crate::subagent::run_subagent(
                        &sub_def,
                        subagent_desc,
                        &agent.client,
                        &agent.config,
                        &agent.tools,
                        &agent.permission,
                    )
                    .await;

                    match result {
                        Ok(output) => {
                            tool_results.push(ChatMessage::ToolResult {
                                content: output,
                                tool_call_id: tc.id.clone(),
                                name: "agent".to_string(),
                            });
                        }
                        Err(e) => {
                            tool_results.push(ChatMessage::ToolResult {
                                content: format!("Subagent error: {e}"),
                                tool_call_id: tc.id.clone(),
                                name: "agent".to_string(),
                            });
                        }
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
                .await
                .unwrap_or_else(|e| {
                    voidx_tools::base::ToolResult::new(format!("Tool error: {e}"))
                });

            tool_results.push(ChatMessage::ToolResult {
                content: result.output,
                tool_call_id: tc.id.clone(),
                name: tc.name.clone(),
            });
        }

        // Append all tool results
        state.messages.extend(tool_results);
    }

    Ok(RunResult {
        messages: state.messages.clone(),
        steps: state.step_count,
        compaction_applied,
    })
}

// ── LLM call helpers ───────────────────────────────────────────────────────

async fn get_agent_tools(
    agent_def: &AgentDef,
    registry: &Arc<RwLock<ToolRegistry>>,
) -> Vec<serde_json::Value> {
    let all = registry.read().await.tools_for_llm();
    if agent_def.tools.is_empty() {
        all
    } else {
        all.into_iter()
            .filter(|t| {
                agent_def
                    .tools
                    .contains(&t["function"]["name"].as_str().unwrap_or("").to_string())
            })
            .collect()
    }
}

async fn call_llm(
    client: &dyn ChatClient,
    messages: &[ChatMessage],
    _tool_defs: &[serde_json::Value],
) -> Result<(ChatMessage, Vec<ToolCall>), AgentError> {
    let response = client.invoke(messages, &[]).await?;

    match &response {
        ChatMessage::Assistant {
            content: _,
            tool_calls,
        } => Ok((response.clone(), tool_calls.clone())),
        _ => Ok((response, vec![])),
    }
}

async fn call_llm_stream(
    client: &dyn ChatClient,
    messages: &[ChatMessage],
    _tool_defs: &[serde_json::Value],
) -> Result<(ChatMessage, Vec<ToolCall>), AgentError> {
    let mut stream = client.stream(messages, &[]).await?;
    let mut acc = StreamAccumulator::new();

    while let Some(event) = stream.next().await {
        let event = event.map_err(|e| AgentError::Llm(e))?;
        if matches!(event, voidx_llm::streaming::StreamEvent::MessageComplete) {
            break;
        }
        acc.feed(&event);
    }

    let tool_calls: Vec<ToolCall> = acc
        .tool_call_names
        .iter()
        .enumerate()
        .map(|(i, name)| {
            let id = acc.tool_call_ids.get(i).cloned().unwrap_or_default();
            let args: serde_json::Value = acc
                .tool_call_args
                .get(i)
                .and_then(|s| serde_json::from_str(s).ok())
                .unwrap_or_default();
            ToolCall {
                id,
                name: name.clone(),
                arguments: args,
            }
        })
        .collect();

    let msg = ChatMessage::Assistant {
        content: acc.text,
        tool_calls: tool_calls.clone(),
    };

    Ok((msg, tool_calls))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::orchestrator;
    use crate::state::AgentState;

    #[test]
    fn test_agent_state_default() {
        let state = AgentState::default();
        assert_eq!(state.agent, "orchestrator");
        assert_eq!(state.max_steps, 50);
        assert!(state.should_continue);
    }

    #[test]
    fn test_agent_state_has_tool_budget() {
        let mut state = AgentState::default();
        state.step_count = 48;
        assert!(state.has_tool_budget()); // 48 < 49

        state.step_count = 49;
        assert!(!state.has_tool_budget()); // 49 >= 49
    }

    #[test]
    fn test_agent_defs_exist() {
        let orch = orchestrator();
        assert_eq!(orch.name, "orchestrator");
        assert_eq!(orch.max_steps, 50);

        let explore = get_agent("explore").unwrap();
        assert!(explore.tools.contains(&"file_read".to_string()));
        assert!(!explore.tools.contains(&"file_write".to_string()));
    }

    #[test]
    fn test_stream_accumulator() {
        use voidx_llm::streaming::StreamEvent;
        let mut acc = StreamAccumulator::new();
        acc.feed(&StreamEvent::TextDelta("Hello ".to_string()));
        acc.feed(&StreamEvent::TextDelta("World".to_string()));
        acc.feed(&StreamEvent::ToolCallStart {
            id: "t1".to_string(),
            name: "bash".to_string(),
        });
        acc.feed(&StreamEvent::ToolCallDelta {
            id: "".to_string(),
            args_delta: r#"{"command":"#.to_string(),
        });
        acc.feed(&StreamEvent::ToolCallDelta {
            id: "".to_string(),
            args_delta: r#""ls"}"#.to_string(),
        });

        assert_eq!(acc.text, "Hello World");
        assert!(acc.has_tool_calls());
        assert_eq!(acc.tool_call_names, vec!["bash"]);
    }
}
