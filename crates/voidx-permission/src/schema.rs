//! Permission schema — rules, actions, and rule sets.
//!
//! Ported from `src/voidx/permission/schema.py`.

use serde::{Deserialize, Serialize};

/// What action to take for a tool call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Allow,
    Ask,
    Deny,
}

/// A single permission rule.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Rule {
    pub permission: String,
    pub pattern: String,
    pub action: Action,
}

/// A set of permission rules evaluated in order.
pub type Ruleset = Vec<Rule>;

/// The basic permission ruleset — mirrors Python's BASIC_RULES.
pub fn basic_rules() -> Ruleset {
    vec![
        // Read-only tools: auto-allow
        Rule { permission: "read".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "glob".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "grep".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "webfetch".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "websearch".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "todo".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "task_status".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "repo_map".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "lsp_diagnostics".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "lsp_symbols".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "lsp_definition".into(), pattern: "*".into(), action: Action::Allow },
        Rule { permission: "lsp_references".into(), pattern: "*".into(), action: Action::Allow },
        // Agent delegation: allow by default
        Rule { permission: "agent".into(), pattern: "*".into(), action: Action::Allow },
        // Write tools: ask
        Rule { permission: "write".into(), pattern: "*".into(), action: Action::Ask },
        Rule { permission: "edit".into(), pattern: "*".into(), action: Action::Ask },
        Rule { permission: "bash".into(), pattern: "*".into(), action: Action::Ask },
        Rule { permission: "lsp_format".into(), pattern: "*".into(), action: Action::Ask },
        // Agent implement: ask
        Rule { permission: "agent".into(), pattern: "implement".into(), action: Action::Ask },
        // MCP tools: ask by default
        Rule { permission: "mcp__*".into(), pattern: "*".into(), action: Action::Ask },
        Rule { permission: "mcp/*".into(), pattern: "*".into(), action: Action::Ask },
    ]
}

/// Evaluate a ruleset against a tool call.
/// Returns the action from the first matching rule, or Deny if no rule matches.
pub fn evaluate_rules(rules: &Ruleset, tool_name: &str, tool_pattern: &str) -> Action {
    for rule in rules {
        if wildcard_match_tool(&rule.permission, tool_name)
            && wildcard_match_pattern(&rule.pattern, tool_pattern)
        {
            return rule.action;
        }
    }
    // Default: deny unknown tools
    Action::Deny
}

/// Match a tool name against a permission pattern.
/// Supports wildcard: "mcp__*" matches "mcp__server__tool".
fn wildcard_match_tool(pattern: &str, tool_name: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if pattern == tool_name {
        return true;
    }
    // Handle "mcp__*" style patterns
    if pattern.ends_with('*') {
        let prefix = &pattern[..pattern.len() - 1];
        return tool_name.starts_with(prefix);
    }
    false
}

/// Match a tool pattern (the specific sub-action) against a rule pattern.
fn wildcard_match_pattern(rule_pattern: &str, tool_pattern: &str) -> bool {
    rule_pattern == "*" || rule_pattern == tool_pattern
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_rules_allow_read() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "read", "*"), Action::Allow);
    }

    #[test]
    fn test_basic_rules_ask_write() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "write", "*"), Action::Ask);
    }

    #[test]
    fn test_basic_rules_ask_bash() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "bash", "*"), Action::Ask);
    }

    #[test]
    fn test_basic_rules_allow_agent() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "agent", "explore"), Action::Allow);
    }

    #[test]
    fn test_basic_rules_ask_agent_implement() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "agent", "implement"), Action::Ask);
    }

    #[test]
    fn test_basic_rules_ask_mcp() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "mcp__server__tool", "*"), Action::Ask);
    }

    #[test]
    fn test_unknown_tool_denied() {
        let rules = basic_rules();
        assert_eq!(evaluate_rules(&rules, "unknown_tool", "*"), Action::Deny);
    }
}
