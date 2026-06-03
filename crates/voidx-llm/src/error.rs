use thiserror::Error;

#[derive(Debug, Error)]
pub enum LlmError {
    #[error("HTTP request failed: {0}")]
    Http(#[from] reqwest::Error),

    #[error("Invalid request: {0}")]
    InvalidRequest(String),

    #[error("API error ({code}): {message}")]
    Api { code: u16, message: String },

    #[error("Rate limited. Retry after {retry_after:?}")]
    RateLimited { retry_after: Option<u64> },

    #[error("Authentication failed: {0}")]
    Auth(String),

    #[error("Stream error: {0}")]
    Stream(String),

    #[error("Parse error: {0}")]
    Parse(String),

    #[error("Unsupported protocol: {0}")]
    UnsupportedProtocol(String),

    #[error("Model not found: {0}")]
    ModelNotFound(String),

    #[error("Serialization error: {0}")]
    Serde(#[from] serde_json::Error),
}
