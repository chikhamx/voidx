//! LLM Provider layer — typed abstraction over multi-vendor chat APIs.
//!
//! Ported from `src/voidx/llm/provider.py`.

pub mod catalog;
pub mod error;
pub mod protocol;
pub mod reasoning;
pub mod streaming;
pub mod usage;

mod client;
pub use client::{anthropic::AnthropicClient, openai::OpenAIClient, ChatClient, ChatMessage, ToolCall, ToolDefinition};
pub use protocol::Protocol;
pub use streaming::StreamEvent;
pub use voidx_config::ModelConfig;

use std::sync::Arc;

/// Factory: create the right client for a model config.
pub fn create_client(config: &ModelConfig, api_key: &str) -> Result<Arc<dyn ChatClient>, error::LlmError> {
    let protocol = protocol::resolve_protocol(&config.provider, config.protocol.as_deref());
    match protocol {
        Protocol::Anthropic => Ok(Arc::new(AnthropicClient::new(config, api_key, protocol))),
        Protocol::OpenAI => Ok(Arc::new(OpenAIClient::new(config, api_key, protocol))),
    }
}
