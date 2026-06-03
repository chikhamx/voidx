use thiserror::Error;

#[derive(Debug, Error)]
pub enum PermissionError {
    #[error("Sandbox violation: {0}")]
    SandboxViolation(String),

    #[error("Approval required: {tool} — {reason}")]
    ApprovalRequired { tool: String, reason: String },

    #[error("Approval denied: {0}")]
    Denied(String),

    #[error("{0}")]
    Other(String),
}
