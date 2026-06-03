//! Token usage tracking — per-call and cumulative stats.
//!
//! Ported from `src/voidx/llm/usage.py`.

/// Per-call token counts extracted from LLM response.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cache_read_tokens: u32,
    pub cache_write_tokens: u32,
}

/// Cumulative usage tracker with context-limit awareness.
#[derive(Debug, Clone)]
pub struct UsageStats {
    context_limit: u32,
    total_input: u64,
    total_output: u64,
    call_count: u64,
    last_context_tokens: u32,
}

impl UsageStats {
    pub fn new(context_limit: u32) -> Self {
        Self {
            context_limit,
            total_input: 0,
            total_output: 0,
            call_count: 0,
            last_context_tokens: 0,
        }
    }

    pub fn record_call(&mut self, usage: Option<TokenUsage>, fallback_input: u32, fallback_output: u32) {
        let input = usage.as_ref().map(|u| u.input_tokens).unwrap_or(fallback_input);
        let output = usage.as_ref().map(|u| u.output_tokens).unwrap_or(fallback_output);
        self.total_input += input as u64;
        self.total_output += output as u64;
        self.call_count += 1;
    }

    pub fn update_context(&mut self, tokens: u32) {
        self.last_context_tokens = tokens;
    }

    pub fn context_usage_pct(&self) -> f64 {
        if self.context_limit == 0 {
            return 0.0;
        }
        (self.last_context_tokens as f64 / self.context_limit as f64) * 100.0
    }

    pub fn total_input(&self) -> u64 {
        self.total_input
    }

    pub fn total_output(&self) -> u64 {
        self.total_output
    }

    pub fn call_count(&self) -> u64 {
        self.call_count
    }

    pub fn context_limit(&self) -> u32 {
        self.context_limit
    }
}

/// Rough token estimate for a string (4 chars ≈ 1 token for English text).
pub fn estimate_tokens(text: &str) -> u32 {
    (text.chars().count() as f64 / 4.0).ceil() as u32
}

/// Estimate tokens for a list of chat messages.
pub fn estimate_message_tokens(messages: &[crate::ChatMessage]) -> u32 {
    messages
        .iter()
        .map(|m| {
            let content = match m {
                crate::ChatMessage::System { content } => content,
                crate::ChatMessage::User { content } => content,
                crate::ChatMessage::Assistant { content, .. } => content,
                crate::ChatMessage::ToolResult { content, .. } => content,
            };
            // 4 chars ≈ 1 token + 4 tokens of message overhead
            estimate_tokens(content) + 4
        })
        .sum()
}

use serde::{Deserialize, Serialize};
