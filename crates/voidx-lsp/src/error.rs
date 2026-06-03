use thiserror::Error;

#[derive(Debug, Error)]
pub enum LspError {
    #[error("No LSP server found for: {0}")]
    NoServer(String),

    #[error("Spawn failed: {0}")]
    Spawn(String),

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
