//! Runtime context builder — assembles the system prompt for each turn.
//!
//! Ported from `src/voidx/agent/runtime_context.py`.
//! Includes: base prompt, role, tool contract, plan mode, workspace facts,
//! current date, AGENTS.md instructions, active skills, runtime state.

use crate::agents::{AgentDef, BASE_SYSTEM_PROMPT, PLAN_MODE_APPEND};
use crate::state::{InteractionMode, TaskIntent, TaskPhase, TaskRunStatus};
use voidx_config::Config;

/// Built context ready to insert into the message list.
pub struct RuntimeContext {
    pub system_prompt: String,
    pub summary: Option<String>,
}

/// Options for building the runtime context.
pub struct ContextOptions<'a> {
    pub config: &'a Config,
    pub agent_def: &'a AgentDef,
    pub interaction_mode: InteractionMode,
    pub summary: Option<&'a str>,
    pub instructions: Option<&'a str>,
    pub skills: Option<&'a [SkillEntry]>,
    pub task_intent: Option<TaskIntent>,
    pub goal: Option<&'a str>,
    pub goal_phase: Option<TaskPhase>,
    pub goal_status: Option<TaskRunStatus>,
    pub approved_scope: Option<&'a str>,
}

/// A skill entry for injection into the system prompt.
#[derive(Debug, Clone)]
pub struct SkillEntry {
    pub name: String,
    pub description: String,
    pub instructions: String,
}

/// Build the runtime context (system prompt + optional compaction summary).
pub fn build_context(opts: &ContextOptions<'_>) -> RuntimeContext {
    let mut parts: Vec<String> = Vec::new();

    // 1. Base system prompt
    parts.push(BASE_SYSTEM_PROMPT.to_string());

    // 2. Role prompt
    if !opts.agent_def.role_prompt.is_empty() {
        parts.push(format!("\n## Your Role\n{}", opts.agent_def.role_prompt));
    }

    // 3. Tool contract
    if !opts.agent_def.tool_contract.is_empty() {
        parts.push(format!("\n## Available Tools\n{}", opts.agent_def.tool_contract));
    }

    // 4. Plan mode
    if opts.interaction_mode == InteractionMode::Plan {
        parts.push(PLAN_MODE_APPEND.to_string());
    }

    // 5. Workspace info
    parts.push(format!(
        "\n## Workspace\nPath: {}\nModel: {} ({})\n",
        opts.config.workspace.display(),
        opts.config.model.provider,
        opts.config.model.model,
    ));

    // 6. Current date
    let now = chrono::Local::now();
    parts.push(format!(
        "\n## Current Date\n{} CST\n",
        now.format("%Y-%m-%d %H:%M")
    ));

    // 7. Runtime state
    let mut runtime_parts: Vec<String> = Vec::new();

    // Interaction mode
    runtime_parts.push(format!("- Mode: {}", opts.interaction_mode));

    // Task intent
    if let Some(intent) = opts.task_intent {
        runtime_parts.push(format!("- Intent: {}", intent));
    }

    // Goal tracking
    if let Some(goal) = opts.goal {
        if !goal.is_empty() {
            runtime_parts.push(format!("- Goal: {}", goal));
        }
    }
    if let Some(phase) = opts.goal_phase {
        runtime_parts.push(format!("- Goal Phase: {}", phase));
    }
    if let Some(status) = opts.goal_status {
        runtime_parts.push(format!("- Goal Status: {}", status));
    }
    if let Some(scope) = opts.approved_scope {
        if !scope.is_empty() {
            runtime_parts.push(format!("- Approved Scope: {}", scope));
        }
    }

    if runtime_parts.len() > 1 {
        parts.push(format!("\n## Runtime State\n{}\n", runtime_parts.join("\n")));
    }

    // 8. AGENTS.md / Instructions
    if let Some(instructions) = opts.instructions {
        if !instructions.is_empty() {
            parts.push(format!(
                "\n## Project Instructions\n{}\n",
                instructions
            ));
        }
    }

    // 9. Active skills
    if let Some(skills) = opts.skills {
        if !skills.is_empty() {
            let mut skill_parts: Vec<String> = vec!["Active workflow skills:".to_string()];
            for skill in skills {
                skill_parts.push(format!("- **{}**: {}", skill.name, skill.description));
                if !skill.instructions.is_empty() {
                    skill_parts.push(format!("  {}", skill.instructions));
                }
            }
            parts.push(format!("\n## Active Skills\n{}\n", skill_parts.join("\n")));
        }
    }

    let system_prompt = parts.join("\n");

    RuntimeContext {
        system_prompt,
        summary: opts.summary.map(|s| s.to_string()),
    }
}

/// Convenience: build context with minimal args (backward compat).
pub fn build_context_simple(
    config: &Config,
    agent_def: &AgentDef,
    interaction_mode: InteractionMode,
    summary: Option<&str>,
    instructions: Option<&str>,
) -> RuntimeContext {
    build_context(&ContextOptions {
        config,
        agent_def,
        interaction_mode,
        summary,
        instructions,
        skills: None,
        task_intent: None,
        goal: None,
        goal_phase: None,
        goal_status: None,
        approved_scope: None,
    })
}

/// Load AGENTS.md instructions from the workspace.
/// Walks up from workspace to find AGENTS.md or CLAUDE.md.
/// Also checks ~/.voidx/AGENTS.md for global instructions.
pub fn load_instructions(workspace: &std::path::Path) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();

    // Global: ~/.voidx/AGENTS.md
    let global_path = dirs::home_dir()
        .map(|h| h.join(".voidx").join("AGENTS.md"))
        .unwrap_or_else(|| std::path::PathBuf::from("/dev/null"));

    if global_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&global_path) {
            if !content.trim().is_empty() {
                parts.push(format!("Instructions from: {}\n{}", global_path.display(), content.trim()));
            }
        }
    } else {
        // Fallback: ~/.claude/CLAUDE.md
        let claude_path = dirs::home_dir()
            .map(|h| h.join(".claude").join("CLAUDE.md"))
            .unwrap_or_else(|| std::path::PathBuf::from("/dev/null"));
        if claude_path.exists() {
            if let Ok(content) = std::fs::read_to_string(&claude_path) {
                if !content.trim().is_empty() {
                    parts.push(format!("Instructions from: {}\n{}", claude_path.display(), content.trim()));
                }
            }
        }
    }

    // Project: walk-up from workspace, first match wins
    let mut current = workspace.to_path_buf();
    let root = {
        let mut r = current.as_path();
        while let Some(parent) = r.parent() {
            r = parent;
        }
        r.to_path_buf()
    };
    while current != root {
        for filename in &["AGENTS.md", "CLAUDE.md"] {
            let candidate = current.join(filename);
            if candidate.exists() {
                if let Ok(content) = std::fs::read_to_string(&candidate) {
                    if !content.trim().is_empty() {
                        parts.push(format!("Instructions from: {}\n{}", candidate.display(), content.trim()));
                        // First match wins (opencode semantics)
                        if !parts.is_empty() {
                            let result = parts.join("\n\n");
                            return if result.trim().is_empty() { None } else { Some(result) };
                        }
                    }
                }
            }
        }
        current = match current.parent() {
            Some(p) => p.to_path_buf(),
            None => break,
        };
    }

    if parts.is_empty() {
        None
    } else {
        Some(parts.join("\n\n"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use voidx_config::{Config, ModelConfig};

    #[test]
    fn test_build_context_includes_date() {
        let config = Config::default();
        let agent_def = crate::agents::orchestrator();
        let ctx = build_context_simple(&config, &agent_def, InteractionMode::Auto, None, None);
        assert!(ctx.system_prompt.contains("Current Date"));
    }

    #[test]
    fn test_build_context_plan_mode() {
        let config = Config::default();
        let agent_def = crate::agents::orchestrator();
        let ctx = build_context_simple(&config, &agent_def, InteractionMode::Plan, None, None);
        assert!(ctx.system_prompt.contains("PLAN MODE"));
    }

    #[test]
    fn test_build_context_with_instructions() {
        let config = Config::default();
        let agent_def = crate::agents::orchestrator();
        let ctx = build_context_simple(
            &config,
            &agent_def,
            InteractionMode::Auto,
            None,
            Some("Always use TypeScript."),
        );
        assert!(ctx.system_prompt.contains("Project Instructions"));
        assert!(ctx.system_prompt.contains("Always use TypeScript"));
    }

    #[test]
    fn test_build_context_with_skills() {
        let config = Config::default();
        let agent_def = crate::agents::orchestrator();
        let skills = vec![
            SkillEntry {
                name: "test-driven-development".to_string(),
                description: "Write tests first".to_string(),
                instructions: "Red → Green → Refactor".to_string(),
            },
        ];
        let ctx = build_context(&ContextOptions {
            config: &config,
            agent_def: &agent_def,
            interaction_mode: InteractionMode::Auto,
            summary: None,
            instructions: None,
            skills: Some(&skills),
            task_intent: Some(TaskIntent::Implement),
            goal: Some("Fix auth bug"),
            goal_phase: Some(TaskPhase::Implement),
            goal_status: Some(TaskRunStatus::Active),
            approved_scope: Some("auth.py"),
        });
        assert!(ctx.system_prompt.contains("Active Skills"));
        assert!(ctx.system_prompt.contains("test-driven-development"));
        assert!(ctx.system_prompt.contains("Runtime State"));
        assert!(ctx.system_prompt.contains("Intent: implement"));
        assert!(ctx.system_prompt.contains("Goal: Fix auth bug"));
    }
}
