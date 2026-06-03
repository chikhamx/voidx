//! Runtime context builder — assembles the system prompt for each turn.
//!
//! Ported from `src/voidx/agent/runtime_context.py`.

use crate::agents::{AgentDef, BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND};
use crate::state::InteractionMode;
use voidx_config::Config;

/// Built context ready to insert into the message list.
pub struct RuntimeContext {
    pub system_prompt: String,
    pub summary: Option<String>,
}

/// Build the runtime context (system prompt + optional compaction summary).
pub fn build_context(
    config: &Config,
    agent_def: &AgentDef,
    interaction_mode: InteractionMode,
    summary: Option<&str>,
    _instructions: Option<&str>,
) -> RuntimeContext {
    let mut parts: Vec<String> = Vec::new();

    // 1. Base system prompt
    parts.push(BASE_SYSTEM_PROMPT.to_string());

    // 2. Role prompt
    if !agent_def.role_prompt.is_empty() {
        parts.push(format!("\n## Your Role\n{}", agent_def.role_prompt));
    }

    // 3. Tool contract
    if !agent_def.tool_contract.is_empty() {
        parts.push(format!("\n## Available Tools\n{}", agent_def.tool_contract));
    }

    // 4. Plan mode
    if interaction_mode == InteractionMode::Plan {
        parts.push(PLAN_MODE_APPEND.to_string());
    }

    // 5. Workspace info
    parts.push(format!(
        "\n## Workspace\nPath: {}\nModel: {} ({})\n",
        config.workspace.display(),
        config.model.provider,
        config.model.model,
    ));

    let system_prompt = parts.join("\n");

    RuntimeContext {
        system_prompt,
        summary: summary.map(|s| s.to_string()),
    }
}
