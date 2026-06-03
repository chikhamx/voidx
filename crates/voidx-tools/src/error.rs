use thiserror::Error;

#[derive(Debug, Error)]
pub enum ToolError {
    #[error("Tool not found: {0}")]
    NotFound(String),

    #[error("Invalid arguments: {0}")]
    InvalidArgs(String),

    #[error("File not in workspace: {0}")]
    SandboxViolation(String),

    #[error("Command blocked: {0}")]
    CommandBlocked(String),

    #[error("Command timed out after {timeout}s: {command}")]
    Timeout { command: String, timeout: u64 },

    #[error("Command failed (exit {exit_code}): {message}")]
    CommandFailed { exit_code: i32, message: String },

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("{0}")]
    Other(String),
}
