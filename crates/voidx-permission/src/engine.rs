//! Permission engine — combines sandbox + approval policy to decide on tool calls.
//!
//! Ported from `src/voidx/permission/engine.py` + `service.py`.

use crate::error::PermissionError;
use crate::evaluate::PermissionVerdict;
use crate::sandbox::Sandbox;
use voidx_config::{ApprovalPolicy, SandboxMode};

/// The permission engine: sandbox + approval policy combined.
#[derive(Debug, Clone)]
pub struct PermissionEngine {
    sandbox: Sandbox,
    approval: ApprovalPolicy,
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
        }
    }

    /// Evaluate a tool call and return a verdict.
    pub fn evaluate(
        &self,
        tool_id: &str,
        tool_args: &serde_json::Value,
        workspace: &std::path::Path,
    ) -> Result<PermissionVerdict, PermissionError> {
        // ── Sandbox check ──────────────────────────────────────────
        match tool_id {
            "bash" => {
                self.sandbox.check_bash()?;
            }
            "file_write" | "file_edit" => {
                if let Some(path) = tool_args.get("file_path").and_then(|v| v.as_str()) {
                    self.sandbox
                        .check_write(&std::path::PathBuf::from(path), workspace)?;
                }
            }
            _ => {}
        }

        // ── Approval check ─────────────────────────────────────────
        match self.approval {
            ApprovalPolicy::Never => Ok(PermissionVerdict::Allow),

            ApprovalPolicy::Untrusted => {
                if is_dangerous_tool(tool_id) {
                    Ok(PermissionVerdict::AskUser(format!(
                        "Untrusted tool '{tool_id}' requires approval"
                    )))
                } else {
                    Ok(PermissionVerdict::Allow)
                }
            }

            ApprovalPolicy::OnFailure => {
                // Auto-allow non-bash tools; we report failures after
                if tool_id == "bash" {
                    Ok(PermissionVerdict::AskUser(
                        "Bash in on-failure mode requires approval".to_string(),
                    ))
                } else {
                    Ok(PermissionVerdict::Allow)
                }
            }

            ApprovalPolicy::OnRequest => {
                // Auto-allow everything; agent explicitly requests if needed
                Ok(PermissionVerdict::Allow)
            }
        }
    }

    /// Get the sandbox mode for reporting.
    pub fn sandbox_mode(&self) -> SandboxMode {
        self.sandbox.mode
    }

    /// Get the approval policy for reporting.
    pub fn approval_policy(&self) -> ApprovalPolicy {
        self.approval
    }
}

/// Tools that are considered "dangerous" and should prompt in untrusted mode.
fn is_dangerous_tool(tool_id: &str) -> bool {
    matches!(
        tool_id,
        "bash" | "file_write" | "file_edit"
    )
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

        let verdict = engine.evaluate("file_read", &args, ws.path()).unwrap();
        assert_eq!(verdict, PermissionVerdict::Allow);
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
        let args = serde_json::json!({"file_path": ws.path().join("test.txt").to_str()});

        let verdict = engine.evaluate("file_write", &args, ws.path()).unwrap();
        assert!(matches!(verdict, PermissionVerdict::AskUser(_)));
    }

    #[test]
    fn test_bash_blocked_in_readonly() {
        let engine = PermissionEngine::new(
            SandboxMode::ReadOnly,
            false,
            ApprovalPolicy::Untrusted,
            vec![],
        );
        let ws = tempdir().unwrap();

        let result = engine.evaluate("bash", &serde_json::json!({"command": "ls"}), ws.path());
        assert!(result.is_err());
    }

    #[test]
    fn test_never_approval_allows_everything() {
        let engine = PermissionEngine::new(
            SandboxMode::DangerFullAccess,
            false,
            ApprovalPolicy::Never,
            vec![],
        );
        let ws = tempdir().unwrap();

        let verdict = engine
            .evaluate("bash", &serde_json::json!({"command": "rm -rf /"}), ws.path())
            .unwrap();
        assert_eq!(verdict, PermissionVerdict::Allow);
    }

    #[test]
    fn test_write_outside_workspace_blocked() {
        let engine = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            true,
            ApprovalPolicy::Never,
            vec![],
        );
        let ws = tempdir().unwrap();

        let result = engine.evaluate(
            "file_write",
            &serde_json::json!({"file_path": "/tmp/outside.txt", "content": "x"}),
            ws.path(),
        );
        assert!(result.is_ok(), "workspace_write=true allows writes anywhere");
        // Now test the restrictive case:
        let engine2 = PermissionEngine::new(
            SandboxMode::WorkspaceWrite,
            false,
            ApprovalPolicy::Never,
            vec![],
        );
        let result = engine2.evaluate(
            "file_write",
            &serde_json::json!({"file_path": "/tmp/outside.txt", "content": "x"}),
            ws.path(),
        );
        assert!(result.is_err());
    }
}
