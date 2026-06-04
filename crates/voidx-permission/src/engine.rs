//! Permission engine — combines sandbox + approval policy + rules + session whitelist.
//!
//! Ported from `src/voidx/permission/engine.py` + `service.py`.

use crate::error::PermissionError;
use crate::evaluate::PermissionVerdict;
use crate::sandbox::Sandbox;
use crate::schema::{self, Action, Ruleset};
use voidx_config::{ApprovalPolicy, SandboxMode};

/// Tool capability classification — mirrors Python's PermissionCapability.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolCapability {
    ReadTools,
    FileWrite,
    FileFormat,
    BashRead,
    BashWrite,
    AgentReadonly,
    AgentImplement,
    McpTools,
    Other,
}

impl ToolCapability {
    pub fn classify(tool_id: &str, tool_args: &serde_json::Value) -> Self {
        match tool_id {
            "read" | "glob" | "grep" | "webfetch" | "websearch" | "repo_map"
            | "lsp_diagnostics" | "lsp_symbols" | "lsp_definition" | "lsp_references"
            | "todo" | "task_status" => Self::ReadTools,
            "write" => Self::FileWrite,
            "edit" => Self::FileWrite,
            "lsp_format" => Self::FileFormat,
            "bash" => {
                // Check if the bash command is read-only
                if is_bash_readonly(tool_args) {
                    Self::BashRead
                } else {
                    Self::BashWrite
                }
            }
            "agent" => {
                let agent_type = tool_args
                    .get("agent_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if agent_type == "implement" {
                    Self::AgentImplement
                } else {
                    Self::AgentReadonly
                }
            }
            id if id.starts_with("mcp__") || id.starts_with("mcp/") => Self::McpTools,
            _ => Self::Other,
        }
    }
}

/// Check if a bash command appears to be read-only.
fn is_bash_readonly(args: &serde_json::Value) -> bool {
    let command = args
        .get("command")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let lower = command.trim().to_lowercase();

    // Common read-only commands
    const READONLY_PREFIXES: &[&str] = &[
        "ls", "cat", "head", "tail", "find", "grep", "rg", "ag", "ack",
        "wc", "sort", "uniq", "diff", "which", "where", "type",
        "echo", "pwd", "whoami", "id", "uname", "date", "env",
        "git status", "git log", "git diff", "git show", "git branch",
        "cargo check", "cargo test", "cargo build", "cargo clippy",
        "npm test", "npm run", "npm list",
        "python -c", "python3 -c",
    ];

    for prefix in READONLY_PREFIXES {
        if lower.starts_with(prefix) {
            return true;
        }
    }

    // If it contains pipes or redirects to read, consider read-only
    if lower.contains("|") && !lower.contains(">") && !lower.contains(">>") {
        return true;
    }

    false
}

/// Classified tool call — mirrors Python's ClassifiedToolCall.
#[derive(Debug, Clone)]
pub struct ClassifiedToolCall {
    pub tool_call_id: String,
    pub name: String,
    pub args: serde_json::Value,
    pub capability: ToolCapability,
}

impl ClassifiedToolCall {
    pub fn classify(tool_call_id: &str, name: &str, args: &serde_json::Value) -> Self {
        Self {
            tool_call_id: tool_call_id.to_string(),
            name: name.to_string(),
            args: args.clone(),
            capability: ToolCapability::classify(name, args),
        }
    }
}

/// Permission decision — mirrors Python's PermissionDecision.
#[derive(Debug, Clone)]
pub struct PermissionDecision {
    pub action: Action,
    pub tool_call: ClassifiedToolCall,
    pub reason: String,
    pub failure_check: bool,
}

/// Permission context — mirrors Python's PermissionContext.
#[derive(Debug, Clone)]
pub struct PermissionContext {
    pub workspace: String,
    pub interaction_mode: String,
    pub permission_mode: String,
    pub sandbox_mode: SandboxMode,
    pub sandbox_workspace_write: Vec<String>,
    pub approval_policy: ApprovalPolicy,
    pub session_allow: Vec<String>,
    pub session_deny: Vec<String>,
}

/// The permission engine: sandbox + rules + session whitelist + approval policy.
#[derive(Debug, Clone)]
pub struct PermissionEngine {
    sandbox: Sandbox,
    approval: ApprovalPolicy,
    rules: Ruleset,
    session_allow: Vec<String>,
    session_deny: Vec<String>,
}

impl PermissionEngine {
    pub fn new(
        mode: SandboxMode,
        workspace_write: bool,
        approval: ApprovalPolicy,
        extra_paths: Vec<std::path::PathBuf>,
    ) -> Self {
        Self {
            sandbox: Sandbox {
                mode,
                workspace_write,
                extra_paths,
            },
            approval,
            rules: schema::basic_rules(),
            session_allow: Vec::new(),
            session_deny: Vec::new(),
        }
    }

    /// Pre-approve a tool for the entire session.
    pub fn allow(&mut self, tool: &str) {
        if !self.session_allow.contains(&tool.to_string()) {
            self.session_allow.push(tool.to_string());
        }
        self.session_deny.retain(|t| t != tool);
    }

    /// Pre-deny a tool for the entire session.
    pub fn deny(&mut self, tool: &str) {
        if !self.session_deny.contains(&tool.to_string()) {
            self.session_deny.push(tool.to_string());
        }
        self.session_allow.retain(|t| t != tool);
    }

    /// Check if a tool is in the session allow list.
    pub fn is_allowed(&self, tool: &str) -> bool {
        self.session_allow.contains(&tool.to_string())
    }

    /// Check if a tool is in the session deny list.
    pub fn is_denied(&self, tool: &str) -> bool {
        self.session_deny.contains(&tool.to_string())
    }

    /// Get the sandbox mode for reporting.
    pub fn sandbox_mode(&self) -> SandboxMode {
        self.sandbox.mode
    }

    /// Get the approval policy for reporting.
    pub fn approval_policy(&self) -> ApprovalPolicy {
        self.approval
    }

    /// Classify a tool call.
    pub fn classify(&self, tool_id: &str, args: &serde_json::Value) -> ClassifiedToolCall {
        ClassifiedToolCall::classify("", tool_id, args)
    }

    /// Full authorization flow — mirrors Python's authorize_tool_call.
    pub fn authorize(
        &self,
        tool_id: &str,
        args: &serde_json::Value,
        workspace: &std::path::Path,
        interaction_mode: &str,
        plan_mode: bool,
    ) -> PermissionDecision {
        let classified = ClassifiedToolCall::classify("", tool_id, args);

        // 1. Session deny list
        if self.session_deny.contains(&tool_id.to_string()) {
            return PermissionDecision {
                action: Action::Deny,
                tool_call: classified,
                reason: format!("Tool '{}' is denied for this session", tool_id),
                failure_check: false,
            };
        }

        // 2. Session allow list
        if self.session_allow.contains(&tool_id.to_string()) {
            return PermissionDecision {
                action: Action::Allow,
                tool_call: classified,
                reason: String::new(),
                failure_check: false,
            };
        }

        // 3. Sandbox check
        if let Err(e) = self.sandbox_check(tool_id, args, workspace) {
            return PermissionDecision {
                action: Action::Deny,
                tool_call: classified,
                reason: e.to_string(),
                failure_check: false,
            };
        }

        // 4. Plan mode check — block writes in plan mode
        if plan_mode || interaction_mode == "plan" {
            match classified.capability {
                ToolCapability::FileWrite
                | ToolCapability::FileFormat
                | ToolCapability::BashWrite
                | ToolCapability::AgentImplement => {
                    return PermissionDecision {
                        action: Action::Deny,
                        tool_call: classified,
                        reason: format!("Tool '{}' is blocked in plan mode", tool_id),
                        failure_check: false,
                    };
                }
                _ => {}
            }
        }

        // 5. Rule evaluation
        let tool_pattern = extract_tool_pattern(tool_id, args);
        let rule_action = schema::evaluate_rules(&self.rules, tool_id, &tool_pattern);

        // 6. Apply approval policy overlay
        let final_action = match self.approval {
            ApprovalPolicy::Never => Action::Allow,
            ApprovalPolicy::Untrusted => {
                // In untrusted mode, ask for dangerous tools
                match rule_action {
                    Action::Ask => {
                        match classified.capability {
                            ToolCapability::FileWrite | ToolCapability::BashWrite
                            | ToolCapability::AgentImplement | ToolCapability::McpTools => Action::Ask,
                            _ => Action::Allow,
                        }
                    }
                    other => other,
                }
            }
            ApprovalPolicy::OnFailure => {
                // Auto-allow non-bash tools; ask for bash
                match classified.capability {
                    ToolCapability::BashWrite => Action::Ask,
                    _ => rule_action,
                }
            }
            ApprovalPolicy::OnRequest => {
                // Auto-allow everything; agent explicitly requests if needed
                Action::Allow
            }
        };

        let failure_check = matches!(final_action, Action::Allow)
            && matches!(
                classified.capability,
                ToolCapability::FileWrite
                    | ToolCapability::BashWrite
                    | ToolCapability::AgentImplement
            );

        PermissionDecision {
            action: final_action,
            tool_call: classified,
            reason: String::new(),
            failure_check,
        }
    }

    /// Evaluate a tool call and return a verdict (backward compat).
    pub fn evaluate(
        &self,
        tool_id: &str,
        tool_args: &serde_json::Value,
        workspace: &std::path::Path,
    ) -> Result<PermissionVerdict, PermissionError> {
        let decision = self.authorize(tool_id, tool_args, workspace, "auto", false);
        match decision.action {
            Action::Allow => {
                if decision.failure_check {
                    Ok(PermissionVerdict::AllowWithFailureCheck {
                        reason: decision.reason,
                    })
                } else {
                    Ok(PermissionVerdict::Allow)
                }
            }
            Action::Ask => Ok(PermissionVerdict::AskUser(format!(
                "Tool '{}' requires approval",
                tool_id
            ))),
            Action::Deny => Ok(PermissionVerdict::Deny(
                decision.reason.or_default(format!("Tool '{}' denied", tool_id)),
            )),
        }
    }

    /// Sandbox check for a tool call.
    fn sandbox_check(
        &self,
        tool_id: &str,
        args: &serde_json::Value,
        workspace: &std::path::Path,
    ) -> Result<(), PermissionError> {
        match tool_id {
            "bash" => self.sandbox.check_bash(),
            "write" | "edit" => {
                if let Some(path) = args.get("file_path").and_then(|v| v.as_str()) {
                    self.sandbox
                        .check_write(&std::path::PathBuf::from(path), workspace)
                } else {
                    Ok(())
                }
            }
            _ => Ok(()),
        }
    }
}

/// Extract the tool pattern from args (e.g., agent_type for agent tool).
fn extract_tool_pattern(tool_id: &str, args: &serde_json::Value) -> String {
    if tool_id == "agent" {
        args.get("agent_type")
            .and_then(|v| v.as_str())
            .unwrap_or("*")
            .to_string()
    } else {
        "*".to_string()
    }
}

/// Helper trait for providing default reason.
trait OrDefault {
    fn or_default(self, default: String) -> String;
}

impl OrDefault for String {
    fn or_default(self, default: String) -> String {
        if self.is_empty() { default } else { self }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_read_tool_allowed_in_untrusted() {
        let engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"file_path": "test.rs"});

        let verdict = engine.evaluate("read", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::Allow));
    }

    #[test]
    fn test_write_tool_asks_in_untrusted() {
        let engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"file_path": "test.rs"});

        let verdict = engine.evaluate("write", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::AskUser(_)));
    }

    #[test]
    fn test_bash_asks_in_untrusted() {
        let engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"command": "rm -rf /"});

        let verdict = engine.evaluate("bash", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::AskUser(_)));
    }

    #[test]
    fn test_session_allow_overrides() {
        let mut engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        engine.allow("bash");
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"command": "ls"});

        let verdict = engine.evaluate("bash", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::Allow));
    }

    #[test]
    fn test_session_deny_overrides() {
        let mut engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        engine.deny("read");
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"file_path": "test.rs"});

        let verdict = engine.evaluate("read", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::Deny(_)));
    }

    #[test]
    fn test_plan_mode_blocks_writes() {
        let engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"file_path": "test.rs"});

        let decision = engine.authorize("write", &args, ws.path(), "plan", true);
        assert_eq!(decision.action, Action::Deny);
    }

    #[test]
    fn test_never_policy_auto_allows() {
        let engine = PermissionEngine::new(
            SandboxMode::DangerFullAccess,
            true,
            ApprovalPolicy::Never,
            vec![],
        );
        let ws = tempdir().unwrap();
        let args = serde_json::json!({"command": "rm -rf /"});

        let verdict = engine.evaluate("bash", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::Allow));
    }

    #[test]
    fn test_tool_capability_classification() {
        assert_eq!(
            ToolCapability::classify("read", &serde_json::json!({})),
            ToolCapability::ReadTools
        );
        assert_eq!(
            ToolCapability::classify("write", &serde_json::json!({})),
            ToolCapability::FileWrite
        );
        assert_eq!(
            ToolCapability::classify("agent", &serde_json::json!({"agent_type": "implement"})),
            ToolCapability::AgentImplement
        );
        assert_eq!(
            ToolCapability::classify("agent", &serde_json::json!({"agent_type": "explore"})),
            ToolCapability::AgentReadonly
        );
        assert_eq!(
            ToolCapability::classify("mcp__server__tool", &serde_json::json!({})),
            ToolCapability::McpTools
        );
    }
}
