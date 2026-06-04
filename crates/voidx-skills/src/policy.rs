//! Workflow skill activation policy.
//!
//! Ported from `src/voidx/skills/policy.py`.
//!
//! Determines which workflow skills should be activated based on
//! user text, agent role, task intent, and interaction mode.

/// A workflow skill that should be activated, with a reason.
#[derive(Debug, Clone)]
pub struct WorkflowSkillActivation {
    pub name: String,
    pub reason: String,
}

/// Priority ordering for workflow skills (lower = higher priority).
const WORKFLOW_SKILL_PRIORITY: &[(&str, u32)] = &[
    ("systematic-debugging", 10),
    ("receiving-code-review", 20),
    ("writing-plans", 30),
    ("test-driven-development", 40),
    ("verification-before-completion", 50),
    ("requesting-code-review", 60),
];

fn skill_priority(name: &str) -> u32 {
    WORKFLOW_SKILL_PRIORITY
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, p)| *p)
        .unwrap_or(999)
}

/// Sort key for workflow skills: (priority, name).
pub fn workflow_skill_sort_key(name: &str) -> (u32, String) {
    (skill_priority(name), name.to_string())
}

/// Determine which workflow skills should be activated for the given context.
pub fn workflow_skill_activations(
    user_text: &str,
    agent: &str,
    task_intent: &str,
    interaction_mode: &str,
) -> Vec<WorkflowSkillActivation> {
    let text = user_text.to_lowercase();
    let agent_name = agent.to_lowercase();
    let intent = task_intent.to_lowercase();
    let mode = interaction_mode.to_lowercase();

    let mut activations: Vec<WorkflowSkillActivation> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

    let mut add = |name: &str, reason: &str| {
        if seen.contains(name) {
            return;
        }
        seen.insert(name.to_string());
        activations.push(WorkflowSkillActivation {
            name: name.to_string(),
            reason: reason.to_string(),
        });
    };

    // Debug intent → systematic-debugging + verification
    if intent == "debug" {
        add("systematic-debugging", "debug intent");
        add("verification-before-completion", "debug lifecycle");
    }

    // Implement agent/intent → TDD + verification
    if agent_name == "implement" {
        add("test-driven-development", "implement role");
        add("verification-before-completion", "implement lifecycle");
    } else if intent == "implement" {
        add("test-driven-development", "implement intent");
        add("verification-before-completion", "implement lifecycle");
    }

    // Plan agent → writing-plans
    if agent_name == "plan" {
        add("writing-plans", "plan role");
    }

    // Review feedback → receiving-code-review
    if intent == "review" && contains_any(&text, REVIEW_FEEDBACK_TERMS) {
        add("receiving-code-review", "review feedback");
    }

    // Design + planning terms → writing-plans
    if intent == "design" && contains_any(&text, PLAN_TERMS) {
        add("writing-plans", "planning intent");
    }

    // Plan mode → writing-plans
    if mode == "plan" {
        add("writing-plans", "plan mode");
    }

    // Sort by priority
    activations.sort_by_key(|a| workflow_skill_sort_key(&a.name));
    activations
}

fn contains_any(text: &str, terms: &[&str]) -> bool {
    terms.iter().any(|t| text.contains(t))
}

const REVIEW_FEEDBACK_TERMS: &[&str] = &[
    "review feedback",
    "code review feedback",
    "review comment",
    "reviewer says",
    "feedback says",
    "优化点",
    "审查意见",
    "评审意见",
];

const PLAN_TERMS: &[&str] = &[
    "implementation plan",
    "write a plan",
    "planning",
    "spec",
    "requirements",
    "计划",
    "实施方案",
    "需求",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_debug_activates_systematic_debugging() {
        let activations = workflow_skill_activations("fix the bug", "", "debug", "");
        assert!(activations.iter().any(|a| a.name == "systematic-debugging"));
    }

    #[test]
    fn test_implement_agent_activates_tdd() {
        let activations = workflow_skill_activations("", "implement", "", "");
        assert!(activations.iter().any(|a| a.name == "test-driven-development"));
    }

    #[test]
    fn test_plan_mode_activates_writing_plans() {
        let activations = workflow_skill_activations("", "", "", "plan");
        assert!(activations.iter().any(|a| a.name == "writing-plans"));
    }

    #[test]
    fn test_no_activations_for_chat() {
        let activations = workflow_skill_activations("hello", "", "chat", "auto");
        assert!(activations.is_empty());
    }
}
