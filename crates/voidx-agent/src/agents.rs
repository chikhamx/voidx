//! Agent definitions — the 5-agent system: orchestrator, explore, plan, implement, review.
//!
//! Ported from `src/voidx/agent/agents.py`.

/// Definition of an agent role.
#[derive(Debug, Clone)]
pub struct AgentDef {
    pub name: String,
    pub role: String,
    pub role_prompt: String,
    pub tool_contract: String,
    pub tools: Vec<String>,
    pub max_steps: u32,
}

/// Built-in agents that form the 5-agent system.
pub fn builtin_agents() -> Vec<AgentDef> {
    vec![orchestrator(), explore(), plan(), implement(), review()]
}

pub fn orchestrator() -> AgentDef {
    AgentDef {
        name: "orchestrator".to_string(),
        role: "Primary coding agent".to_string(),
        role_prompt: BASE_SYSTEM_PROMPT.to_string(),
        tool_contract: "".to_string(),
        tools: vec![], // empty = all tools available
        max_steps: 50,
    }
}

pub fn explore() -> AgentDef {
    AgentDef {
        name: "explore".to_string(),
        role: "Read-only codebase search agent".to_string(),
        role_prompt: "You are a code exploration agent. Search and read files to understand the codebase. Never write or edit files.".to_string(),
        tool_contract: "Only read/search tools are available. No writes, no bash.".to_string(),
        tools: vec![
            "file_read".to_string(),
            "grep".to_string(),
            "glob".to_string(),
            "webfetch".to_string(),
            "websearch".to_string(),
        ],
        max_steps: 20,
    }
}

pub fn plan() -> AgentDef {
    AgentDef {
        name: "plan".to_string(),
        role: "Read-only architecture design agent".to_string(),
        role_prompt: "You are a software architect. Analyze the codebase and design implementation plans. Never write code.".to_string(),
        tool_contract: "Only read/search tools are available.".to_string(),
        tools: vec![
            "file_read".to_string(),
            "grep".to_string(),
            "glob".to_string(),
        ],
        max_steps: 30,
    }
}

pub fn implement() -> AgentDef {
    AgentDef {
        name: "implement".to_string(),
        role: "Delegated coding agent for broad or isolated changes".to_string(),
        role_prompt: "You are an implementation agent. Write and edit code to accomplish the assigned task. Be thorough and precise.".to_string(),
        tool_contract: "All tools available except starting sub-agents (depth limit = 1).".to_string(),
        tools: vec![
            "file_read".to_string(),
            "file_write".to_string(),
            "file_edit".to_string(),
            "bash".to_string(),
            "grep".to_string(),
            "glob".to_string(),
        ],
        max_steps: 30,
    }
}

pub fn review() -> AgentDef {
    AgentDef {
        name: "review".to_string(),
        role: "Read-only code review agent (PASS/FAIL/NEEDS_CHANGE)".to_string(),
        role_prompt: "You are a code reviewer. Read and analyze code for correctness bugs, simplifications, and efficiency improvements. Output PASS, FAIL, or NEEDS_CHANGE with detailed reasoning.".to_string(),
        tool_contract: "Only read/search tools are available.".to_string(),
        tools: vec![
            "file_read".to_string(),
            "grep".to_string(),
            "glob".to_string(),
        ],
        max_steps: 15,
    }
}

pub fn get_agent(name: &str) -> Option<AgentDef> {
    match name {
        "orchestrator" => Some(orchestrator()),
        "explore" => Some(explore()),
        "plan" => Some(plan()),
        "implement" => Some(implement()),
        "review" => Some(review()),
        _ => None,
    }
}

/// Base system prompt for the orchestrator agent.
pub const BASE_SYSTEM_PROMPT: &str = "\
You are an interactive coding agent. You solve software engineering tasks \
by reasoning step-by-step and using tools to read files, search code, \
run shell commands, write and edit code, and delegate to specialized sub-agents.

## Core Rules
- Read files before editing them; never guess content.
- Use exact string replacement for edits (the Edit tool requires it).
- Prefer dedicated search tools over shell `grep`/`find`.
- When a task is complex, delegate to the explore/plan/implement/review agents.
- Depth limit = 1: sub-agents cannot start further sub-agents.
- Keep tool calls precise and minimal.
- Report outcomes truthfully.

## Modes
- Auto mode: you decide what to do and execute.
- Plan mode: you design an implementation plan first, then ask for approval.
";

pub const PLAN_MODE_APPEND: &str = "\n\
You are in PLAN MODE. Do not execute any writes or modifications. \
Design an implementation plan, present it, and wait for approval before proceeding.\n\
";
