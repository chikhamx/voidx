//! Agent state — the data flowing through the state machine.
//!
//! Ported from `src/voidx/agent/state.py`.

use serde::{Deserialize, Serialize};
use voidx_llm::ChatMessage;

/// The state carried through the agent run loop.
#[derive(Debug, Clone)]
pub struct AgentState {
    pub messages: Vec<ChatMessage>,
    pub agent: String,
    pub step_count: u32,
    pub max_steps: u32,
    pub should_continue: bool,
    pub interaction_mode: InteractionMode,
    pub plan_mode: bool,
    pub task_intent: Option<String>,
    pub compaction_summary: String,
}

impl Default for AgentState {
    fn default() -> Self {
        Self {
            messages: Vec::new(),
            agent: "orchestrator".to_string(),
            step_count: 0,
            max_steps: 50,
            should_continue: true,
            interaction_mode: InteractionMode::Auto,
            plan_mode: false,
            task_intent: None,
            compaction_summary: String::new(),
        }
    }
}

impl AgentState {
    pub fn new(user_message: &str) -> Self {
        Self {
            messages: vec![ChatMessage::user(user_message)],
            ..Default::default()
        }
    }

    /// Advance step count before LLM call.
    pub fn prepare_step(&mut self) {
        self.step_count += 1;
    }

    /// Check if we've exceeded the step budget.
    pub fn has_tool_budget(&self) -> bool {
        self.step_count < self.max_steps - 1
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InteractionMode {
    Auto,
    Plan,
}

impl InteractionMode {
    pub fn parse(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "plan" => Self::Plan,
            _ => Self::Auto,
        }
    }
}
