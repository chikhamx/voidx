//! Agent state — the data flowing through the state machine.
//!
//! Ported from `src/voidx/agent/state.py` + `runtime_context.py` + `task_state.py`.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use voidx_llm::ChatMessage;

// ── InteractionMode ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InteractionMode {
    Auto,
    Plan,
    Goal,
}

impl InteractionMode {
    pub fn parse(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "plan" => Self::Plan,
            "goal" => Self::Goal,
            _ => Self::Auto,
        }
    }

    /// Whether this mode denies write operations.
    pub fn denies_writes(&self) -> bool {
        matches!(self, Self::Plan)
    }
}

impl std::fmt::Display for InteractionMode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Auto => write!(f, "auto"),
            Self::Plan => write!(f, "plan"),
            Self::Goal => write!(f, "goal"),
        }
    }
}

// ── TaskIntent ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskIntent {
    Chat,
    Inspect,
    Design,
    Review,
    Implement,
    Debug,
    Ambiguous,
}

impl TaskIntent {
    pub fn parse(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "inspect" => Self::Inspect,
            "design" => Self::Design,
            "review" => Self::Review,
            "implement" => Self::Implement,
            "debug" => Self::Debug,
            "ambiguous" => Self::Ambiguous,
            _ => Self::Chat,
        }
    }

    /// Whether this intent implies implementation is allowed.
    pub fn implementation_allowed(&self) -> bool {
        matches!(self, Self::Implement)
    }
}

impl std::fmt::Display for TaskIntent {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Chat => write!(f, "chat"),
            Self::Inspect => write!(f, "inspect"),
            Self::Design => write!(f, "design"),
            Self::Review => write!(f, "review"),
            Self::Implement => write!(f, "implement"),
            Self::Debug => write!(f, "debug"),
            Self::Ambiguous => write!(f, "ambiguous"),
        }
    }
}

// ── TaskPhase ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskPhase {
    Clarify,
    Inspect,
    Design,
    Implement,
    Verify,
    Review,
    Done,
}

impl TaskPhase {
    pub fn from_intent(intent: TaskIntent) -> Self {
        match intent {
            TaskIntent::Inspect => Self::Inspect,
            TaskIntent::Design => Self::Design,
            TaskIntent::Implement => Self::Implement,
            TaskIntent::Review => Self::Review,
            TaskIntent::Debug => Self::Implement,
            TaskIntent::Ambiguous => Self::Clarify,
            TaskIntent::Chat => Self::Clarify,
        }
    }
}

impl std::fmt::Display for TaskPhase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Clarify => write!(f, "clarify"),
            Self::Inspect => write!(f, "inspect"),
            Self::Design => write!(f, "design"),
            Self::Implement => write!(f, "implement"),
            Self::Verify => write!(f, "verify"),
            Self::Review => write!(f, "review"),
            Self::Done => write!(f, "done"),
        }
    }
}

// ── TaskRun ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskRun {
    pub goal: String,
    pub phase: TaskPhase,
    pub status: TaskRunStatus,
    pub approved_scope: String,
    pub awaiting_implementation_approval: bool,
    pub turn_count: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskRunStatus {
    Idle,
    Active,
    Done,
}

impl std::fmt::Display for TaskRunStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TaskRunStatus::Idle => write!(f, "idle"),
            TaskRunStatus::Active => write!(f, "active"),
            TaskRunStatus::Done => write!(f, "done"),
        }
    }
}

impl Default for TaskRun {
    fn default() -> Self {
        Self {
            goal: String::new(),
            phase: TaskPhase::Clarify,
            status: TaskRunStatus::Idle,
            approved_scope: String::new(),
            awaiting_implementation_approval: false,
            turn_count: 0,
        }
    }
}

impl TaskRun {
    pub fn active(&self) -> bool {
        self.status == TaskRunStatus::Active && !self.goal.is_empty()
    }

    pub fn set_goal(&mut self, goal: &str) {
        self.goal = goal.to_string();
        self.phase = TaskPhase::Clarify;
        self.status = if goal.is_empty() {
            TaskRunStatus::Idle
        } else {
            TaskRunStatus::Active
        };
        self.approved_scope = String::new();
        self.awaiting_implementation_approval = false;
        self.turn_count = 0;
    }

    pub fn clear(&mut self) {
        self.goal = String::new();
        self.phase = TaskPhase::Clarify;
        self.status = TaskRunStatus::Idle;
        self.approved_scope = String::new();
        self.awaiting_implementation_approval = false;
        self.turn_count = 0;
    }
}

// ── TaskState ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskState {
    pub current_intent: TaskIntent,
    pub previous_intent: Option<TaskIntent>,
    pub current_goal: String,
    pub awaiting_implementation_approval: bool,
    pub approved_scope: String,
    pub last_plan_summary: String,
}

impl Default for TaskState {
    fn default() -> Self {
        Self {
            current_intent: TaskIntent::Chat,
            previous_intent: None,
            current_goal: String::new(),
            awaiting_implementation_approval: false,
            approved_scope: String::new(),
            last_plan_summary: String::new(),
        }
    }
}

// ── AgentState ──────────────────────────────────────────────────────────────

/// The state carried through the agent run loop.
/// Full port of Python's AgentState TypedDict.
#[derive(Debug, Clone)]
pub struct AgentState {
    // Messages
    pub messages: Vec<ChatMessage>,

    // Agent identity
    pub agent: String,
    pub workspace: String,

    // Mode & intent
    pub plan_mode: bool,
    pub interaction_mode: InteractionMode,
    pub task_intent: TaskIntent,
    pub implementation_allowed: bool,
    pub intent_resolution_reason: String,

    // Task tracking
    pub awaiting_implementation_approval: bool,
    pub approved_scope: String,
    pub goal: String,
    pub goal_phase: TaskPhase,
    pub goal_status: TaskRunStatus,
    pub goal_turn_count: u32,
    pub previous_intent: Option<TaskIntent>,

    // Step budget
    pub step_count: u32,
    pub max_steps: u32,
    pub should_continue: bool,

    // Tool results (tool_call_id → result text)
    pub tool_results: HashMap<String, String>,

    // Compaction
    pub compaction_summary: String,
}

impl Default for AgentState {
    fn default() -> Self {
        Self {
            messages: Vec::new(),
            agent: "orchestrator".to_string(),
            workspace: ".".to_string(),
            plan_mode: false,
            interaction_mode: InteractionMode::Auto,
            task_intent: TaskIntent::Chat,
            implementation_allowed: false,
            intent_resolution_reason: String::new(),
            awaiting_implementation_approval: false,
            approved_scope: String::new(),
            goal: String::new(),
            goal_phase: TaskPhase::Clarify,
            goal_status: TaskRunStatus::Idle,
            goal_turn_count: 0,
            previous_intent: None,
            step_count: 0,
            max_steps: 50,
            should_continue: true,
            tool_results: HashMap::new(),
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

    pub fn with_workspace(mut self, workspace: &str) -> Self {
        self.workspace = workspace.to_string();
        self
    }

    /// Advance step count before LLM call.
    pub fn prepare_step(&mut self) {
        self.step_count += 1;
    }

    /// Check if we've exceeded the step budget.
    pub fn has_tool_budget(&self) -> bool {
        self.step_count < self.max_steps - 1
    }

    /// Update task intent based on user text and current interaction mode.
    pub fn resolve_intent(&mut self, user_text: &str) {
        self.previous_intent = Some(self.task_intent);
        self.task_intent = infer_task_intent(user_text, self.interaction_mode);
        self.implementation_allowed = self.task_intent.implementation_allowed();

        // Update goal tracking
        if self.goal.is_empty() && self.task_intent != TaskIntent::Chat {
            self.goal = summarize_scope(user_text);
            self.goal_phase = TaskPhase::from_intent(self.task_intent);
            self.goal_status = TaskRunStatus::Active;
        }
        self.goal_turn_count += 1;

        // Approval transitions
        if self.task_intent == TaskIntent::Implement && !self.approved_scope.is_empty() {
            self.awaiting_implementation_approval = false;
        } else if self.task_intent == TaskIntent::Implement {
            self.awaiting_implementation_approval = true;
            self.approved_scope = summarize_scope(user_text);
        }
    }
}

// ── Intent inference ────────────────────────────────────────────────────────

/// Infer task intent from user text and interaction mode.
/// Ported from Python's `infer_task_intent`.
pub fn infer_task_intent(text: &str, mode: InteractionMode) -> TaskIntent {
    if mode == InteractionMode::Plan {
        return TaskIntent::Design;
    }

    let lower = text.to_lowercase();

    // Implementation hints
    const IMPLEMENT_HINTS: &[&str] = &[
        "fix", "implement", "change", "edit", "write", "refactor", "patch",
        "apply", "do it", "go ahead", "start coding",
        "修复", "实现", "修改", "改一下", "直接改", "开始干", "开始做",
        "动手", "落地", "继续改", "继续做", "继续实现", "继续修复",
        "可以改", "可以开始",
    ];
    if contains_any(&lower, IMPLEMENT_HINTS) {
        return TaskIntent::Implement;
    }

    const REVIEW_HINTS: &[&str] = &["review", "审查", "复核", "评审"];
    if contains_any(&lower, REVIEW_HINTS) {
        return TaskIntent::Review;
    }

    const DEBUG_HINTS: &[&str] = &["debug", "bug", "error", "traceback", "报错", "排查", "问题"];
    if contains_any(&lower, DEBUG_HINTS) {
        return TaskIntent::Debug;
    }

    const DESIGN_HINTS: &[&str] = &[
        "design", "plan", "proposal", "approach", "architecture", "suggest",
        "设计", "方案", "建议", "怎么改", "如何改", "讨论", "规划",
    ];
    if contains_any(&lower, DESIGN_HINTS) {
        return TaskIntent::Design;
    }

    const INSPECT_HINTS: &[&str] = &[
        "look at", "inspect", "analyze", "explain", "understand", "check",
        "what is", "why", "how does",
        "看看", "看一下", "分析", "梳理", "了解", "检查", "现状", "是什么", "为什么",
    ];
    if contains_any(&lower, INSPECT_HINTS) {
        return TaskIntent::Inspect;
    }

    TaskIntent::Chat
}

fn contains_any(text: &str, hints: &[&str]) -> bool {
    hints.iter().any(|h| text.contains(h))
}

/// Summarize a scope description to a reasonable length.
fn summarize_scope(text: &str) -> String {
    let trimmed = text.trim();
    if trimmed.len() <= 120 {
        return trimmed.to_string();
    }
    // Take first sentence or first 120 chars
    if let Some(pos) = trimmed.find('.') {
        if pos < 120 {
            return trimmed[..=pos].to_string();
        }
    }
    format!("{}...", &trimmed[..117])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_interaction_mode_parse() {
        assert_eq!(InteractionMode::parse("plan"), InteractionMode::Plan);
        assert_eq!(InteractionMode::parse("goal"), InteractionMode::Goal);
        assert_eq!(InteractionMode::parse("auto"), InteractionMode::Auto);
        assert_eq!(InteractionMode::parse("unknown"), InteractionMode::Auto);
    }

    #[test]
    fn test_plan_mode_denies_writes() {
        assert!(InteractionMode::Plan.denies_writes());
        assert!(!InteractionMode::Auto.denies_writes());
        assert!(!InteractionMode::Goal.denies_writes());
    }

    #[test]
    fn test_infer_intent_implement() {
        assert_eq!(
            infer_task_intent("fix the bug", InteractionMode::Auto),
            TaskIntent::Implement
        );
        assert_eq!(
            infer_task_intent("修复这个问题", InteractionMode::Auto),
            TaskIntent::Implement
        );
    }

    #[test]
    fn test_infer_intent_design() {
        assert_eq!(
            infer_task_intent("design the API", InteractionMode::Auto),
            TaskIntent::Design
        );
    }

    #[test]
    fn test_infer_intent_plan_mode() {
        assert_eq!(
            infer_task_intent("fix the bug", InteractionMode::Plan),
            TaskIntent::Design
        );
    }

    #[test]
    fn test_infer_intent_chat() {
        assert_eq!(
            infer_task_intent("hello there", InteractionMode::Auto),
            TaskIntent::Chat
        );
    }

    #[test]
    fn test_task_run_lifecycle() {
        let mut run = TaskRun::default();
        assert!(!run.active());

        run.set_goal("Implement auth module");
        assert!(run.active());
        assert_eq!(run.phase, TaskPhase::Clarify);

        run.clear();
        assert!(!run.active());
    }

    #[test]
    fn test_agent_state_resolve_intent() {
        let mut state = AgentState::new("fix the login bug");
        state.resolve_intent("fix the login bug");
        assert_eq!(state.task_intent, TaskIntent::Implement);
        assert!(state.implementation_allowed);
        assert!(state.awaiting_implementation_approval);
    }

    #[test]
    fn test_agent_state_default() {
        let state = AgentState::default();
        assert_eq!(state.agent, "orchestrator");
        assert_eq!(state.workspace, ".");
        assert_eq!(state.interaction_mode, InteractionMode::Auto);
        assert_eq!(state.task_intent, TaskIntent::Chat);
        assert!(!state.implementation_allowed);
        assert!(state.tool_results.is_empty());
    }
}
