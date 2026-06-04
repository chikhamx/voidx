//! Token usage tracking — per-call and cumulative stats with cache hit rate.
//!
//! Ported from `src/voidx/llm/usage.py`.

use serde::{Deserialize, Serialize};

/// Per-call token counts extracted from LLM response.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub reasoning_tokens: u32,
    pub cache_read_tokens: u32,
    pub cache_write_tokens: u32,
    pub cache_tokens_reported: bool,
}

/// Cumulative usage tracker with context-limit awareness and cache tracking.
#[derive(Debug, Clone)]
pub struct UsageStats {
    context_tokens: u32,
    context_limit: u32,
    last_input_tokens: u32,
    last_output_tokens: u32,
    last_cache_read_tokens: u32,
    last_cache_write_tokens: u32,
    last_estimated_cache_read_tokens: u32,
    total_input_tokens: u64,
    total_output_tokens: u64,
    total_cache_read_tokens: u64,
    total_cache_write_tokens: u64,
    total_estimated_cache_read_tokens: u64,
    total_cache_metric_calls: u64,
    estimated_cache_calls: u64,
    call_count: u64,
}

impl UsageStats {
    pub fn new(context_limit: u32) -> Self {
        Self {
            context_tokens: 0,
            context_limit,
            last_input_tokens: 0,
            last_output_tokens: 0,
            last_cache_read_tokens: 0,
            last_cache_write_tokens: 0,
            last_estimated_cache_read_tokens: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_cache_read_tokens: 0,
            total_cache_write_tokens: 0,
            total_estimated_cache_read_tokens: 0,
            total_cache_metric_calls: 0,
            estimated_cache_calls: 0,
            call_count: 0,
        }
    }

    pub fn total_tokens(&self) -> u64 {
        self.total_input_tokens + self.total_output_tokens
    }

    pub fn cache_observed_tokens(&self) -> u64 {
        self.total_cache_read_tokens + self.total_cache_write_tokens
    }

    /// Cache hit rate — actual if available, otherwise estimated.
    pub fn cache_hit_rate(&self) -> Option<f64> {
        self.actual_cache_hit_rate()
            .or_else(|| self.estimated_cache_hit_rate())
    }

    pub fn actual_cache_hit_rate(&self) -> Option<f64> {
        if self.total_cache_metric_calls == 0 && self.cache_observed_tokens() == 0 {
            return None;
        }
        let denominator = self.total_input_tokens.max(self.cache_observed_tokens());
        if denominator == 0 {
            return None;
        }
        Some(self.total_cache_read_tokens as f64 / denominator as f64)
    }

    pub fn estimated_cache_hit_rate(&self) -> Option<f64> {
        if self.estimated_cache_calls == 0 || self.total_input_tokens == 0 {
            return None;
        }
        Some(self.total_estimated_cache_read_tokens as f64 / self.total_input_tokens as f64)
    }

    pub fn cache_hit_rate_is_estimated(&self) -> bool {
        self.actual_cache_hit_rate().is_none() && self.estimated_cache_hit_rate().is_some()
    }

    pub fn reset(&mut self) {
        self.context_tokens = 0;
        self.last_input_tokens = 0;
        self.last_output_tokens = 0;
        self.last_cache_read_tokens = 0;
        self.last_cache_write_tokens = 0;
        self.last_estimated_cache_read_tokens = 0;
        self.total_input_tokens = 0;
        self.total_output_tokens = 0;
        self.total_cache_read_tokens = 0;
        self.total_cache_write_tokens = 0;
        self.total_estimated_cache_read_tokens = 0;
        self.total_cache_metric_calls = 0;
        self.estimated_cache_calls = 0;
        self.call_count = 0;
    }

    pub fn update_context(&mut self, tokens: u32) {
        self.context_tokens = tokens;
    }

    pub fn update_context_limit(&mut self, limit: u32) {
        self.context_limit = limit;
    }

    pub fn record_call(
        &mut self,
        usage: Option<&TokenUsage>,
        fallback_input: u32,
        fallback_output: u32,
    ) {
        let input = usage.map(|u| u.input_tokens).unwrap_or(fallback_input);
        let output = usage.map(|u| u.output_tokens).unwrap_or(fallback_output);
        let cache_read = usage.map(|u| u.cache_read_tokens).unwrap_or(0);
        let cache_write = usage.map(|u| u.cache_write_tokens).unwrap_or(0);

        self.last_input_tokens = input;
        self.last_output_tokens = output;
        self.last_cache_read_tokens = cache_read;
        self.last_cache_write_tokens = cache_write;

        self.total_input_tokens += input as u64;
        self.total_output_tokens += output as u64;
        self.total_cache_read_tokens += cache_read as u64;
        self.total_cache_write_tokens += cache_write as u64;

        if cache_read > 0 || cache_write > 0 {
            self.total_cache_metric_calls += 1;
        }

        self.call_count += 1;
    }

    pub fn context_usage_pct(&self) -> f64 {
        if self.context_limit == 0 {
            return 0.0;
        }
        (self.context_tokens as f64 / self.context_limit as f64) * 100.0
    }

    // ── Accessors ────────────────────────────────────────────────────────

    pub fn context_tokens(&self) -> u32 {
        self.context_tokens
    }

    pub fn context_limit(&self) -> u32 {
        self.context_limit
    }

    pub fn total_input(&self) -> u64 {
        self.total_input_tokens
    }

    pub fn total_output(&self) -> u64 {
        self.total_output_tokens
    }

    pub fn call_count(&self) -> u64 {
        self.call_count
    }

    pub fn last_input(&self) -> u32 {
        self.last_input_tokens
    }

    pub fn last_output(&self) -> u32 {
        self.last_output_tokens
    }

    pub fn total_cache_read(&self) -> u64 {
        self.total_cache_read_tokens
    }

    pub fn total_cache_write(&self) -> u64 {
        self.total_cache_write_tokens
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

/// Format a token count for display.
pub fn format_token_count(tokens: u64) -> String {
    if tokens >= 1_000_000 {
        format!("{:.1}M", tokens as f64 / 1_000_000.0)
    } else if tokens >= 1_000 {
        format!("{:.1}k", tokens as f64 / 1_000.0)
    } else {
        tokens.to_string()
    }
}

/// Format cache hit rate for display.
pub fn format_cache_hit_rate(rate: Option<f64>, is_estimated: bool) -> String {
    match rate {
        Some(r) => {
            let suffix = if is_estimated { "~" } else { "" };
            format!("{}{:.0}%", suffix, r * 100.0)
        }
        None => "N/A".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_usage_stats_record_call() {
        let mut stats = UsageStats::new(128_000);
        let usage = TokenUsage {
            input_tokens: 1000,
            output_tokens: 500,
            cache_read_tokens: 800,
            cache_write_tokens: 200,
            ..Default::default()
        };
        stats.record_call(Some(&usage), 0, 0);
        assert_eq!(stats.total_input(), 1000);
        assert_eq!(stats.total_output(), 500);
        assert_eq!(stats.call_count(), 1);
    }

    #[test]
    fn test_usage_stats_cache_hit_rate() {
        let mut stats = UsageStats::new(128_000);
        let usage = TokenUsage {
            input_tokens: 1000,
            output_tokens: 500,
            cache_read_tokens: 800,
            cache_write_tokens: 200,
            ..Default::default()
        };
        stats.record_call(Some(&usage), 0, 0);
        let rate = stats.cache_hit_rate();
        assert!(rate.is_some());
        assert!(rate.unwrap() > 0.0);
    }

    #[test]
    fn test_usage_stats_no_cache() {
        let mut stats = UsageStats::new(128_000);
        stats.record_call(None, 1000, 500);
        assert_eq!(stats.total_input(), 1000);
        assert_eq!(stats.total_output(), 500);
        assert!(stats.cache_hit_rate().is_none());
    }

    #[test]
    fn test_estimate_tokens() {
        assert!(estimate_tokens("hello world") > 0);
        assert!(estimate_tokens("hello world") <= 10);
    }

    #[test]
    fn test_format_token_count() {
        assert_eq!(format_token_count(500), "500");
        assert_eq!(format_token_count(1500), "1.5k");
        assert_eq!(format_token_count(1_500_000), "1.5M");
    }

    #[test]
    fn test_context_usage_pct() {
        let mut stats = UsageStats::new(128_000);
        stats.update_context(64_000);
        assert!((stats.context_usage_pct() - 50.0).abs() < 0.01);
    }
}
