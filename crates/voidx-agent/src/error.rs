use thiserror::Error;

#[derive(Debug, Error)]
pub enum AgentError {
    #[error("LLM error: {0}")]
    Llm(#[from] voidx_llm::error::LlmError),

    #[error("Tool error: {0}")]
    Tool(#[from] voidx_tools::error::ToolError),

    #[error("Memory error: {0}")]
    Memory(#[from] voidx_memory::MemoryError),

    #[error("Permission error: {0}")]
    Permission(#[from] voidx_permission::error::PermissionError),

    #[error("Permission denied: {0}")]
    PermissionDenied(String),

    #[error("Max steps exceeded ({0})")]
    MaxSteps(u32),

    #[error("No model configured")]
    NoModel,

    #[error("Serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("{0}")]
    Other(String),
}
