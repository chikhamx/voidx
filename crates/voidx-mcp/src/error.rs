use thiserror::Error;

#[derive(Debug, Error)]
pub enum McpError {
    #[error("Process spawn failed: {0}")]
    Spawn(String),

    #[error("Process already exited: {0}")]
    ProcessExited(String),

    #[error("JSON-RPC error ({code}): {message}")]
    Rpc { code: i64, message: String },

    #[error("Protocol error: {0}")]
    Protocol(String),

    #[error("Timeout: {0}")]
    Timeout(String),

    #[error("Serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("{0}")]
    Other(String),
}
