//! Permission verdict — the result of evaluating a tool call.

/// What to do with a tool call after permission evaluation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PermissionVerdict {
    /// Tool call is allowed; execute it.
    Allow,
    /// Tool call is denied; return a rejection message.
    Deny(String),
    /// Ask the user for approval before executing.
    AskUser(String),
    /// Tool call is allowed, but check for failures after execution.
    AllowWithFailureCheck { reason: String },
}
