//! Context compaction — three-layer management aligned with opencode.
//!
//! Layer 1 — prune:    Truncate old tool outputs. Zero API calls.
//! Layer 2 — overflow:  Check if total_tokens >= usable_window.
//! Layer 3 — compact:   LLM-generated structured summary. Preserve tail.
//!
//! Ported from `src/voidx/llm/compaction.py`.

use voidx_llm::{ChatClient, ChatMessage};

// ── Constants ───────────────────────────────────────────────────────────────

/// Minimum tokens to trigger prune.
pub const PRUNE_MINIMUM: u32 = 20_000;
/// Keep this many tokens of tool output.
pub const PRUNE_PROTECT: u32 = 40_000;
/// Reserved for output.
pub const COMPACTION_BUFFER: u32 = 20_000;
/// Keep this many recent turns.
pub const DEFAULT_TAIL_TURNS: usize = 3;
/// Minimum tokens to preserve as tail.
pub const MIN_PRESERVE_RECENT: u32 = 2_000;
/// Maximum tokens to preserve as tail.
pub const MAX_PRESERVE_RECENT: u32 = 8_000;
/// Max chars for tool output after pruning.
pub const TOOL_OUTPUT_MAX_CHARS: usize = 2_000;
/// Tools whose output is protected from pruning.
pub const PRUNE_PROTECTED_TOOLS: &[&str] = &["agent"];
/// Max retries for compaction.
pub const COMPACTION_MAX_RETRIES: u32 = 2;
/// Max chars per message in fallback summary.
pub const FALLBACK_SUMMARY_MAX_PER_MSG: usize = 200;
/// Trigger when used >= 90% of context_limit.
pub const COMPACTION_THRESHOLD: f64 = 0.90;

/// Structured summary template — mirrors Python's SUMMARY_TEMPLATE.
pub const SUMMARY_TEMPLATE: &str = "\
Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or \"(none)\"]

## Progress
### Done
- [completed work or \"(none)\"]

### In Progress
- [current work or \"(none)\"]

### Blocked
- [blockers or \"(none)\"]

## Key Decisions
- [decision and why, or \"(none)\"]

## Next Steps
- [ordered next actions or \"(none)\"]

## Critical Context
- [important technical facts, errors, open questions, or \"(none)\"]

## Relevant Files
- [file or directory path: why it matters, or \"(none)\"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted.";

// ── CompactionService ───────────────────────────────────────────────────────

/// Manages context window across the agent lifecycle.
pub struct CompactionService {
    pub context_limit: u32,
    pub output_token_max: u32,
    pub compaction_count: u32,
}

impl CompactionService {
    pub fn new(context_limit: u32, output_token_max: u32) -> Self {
        Self {
            context_limit,
            output_token_max,
            compaction_count: 0,
        }
    }

    /// How many tokens we can safely use before needing compaction.
    pub fn usable_window(&self) -> u32 {
        let reserved = COMPACTION_BUFFER;
        self.context_limit.saturating_sub(reserved + self.output_token_max)
    }

    /// How many tokens worth of messages to preserve as 'tail'.
    pub fn preserve_recent_budget(&self) -> u32 {
        let usable = self.usable_window();
        (MAX_PRESERVE_RECENT).min(MIN_PRESERVE_RECENT.max((usable as f64 * 0.25) as u32))
    }

    /// Check if token usage exceeds the compaction threshold.
    pub fn is_overflow(&self, total_tokens: u32) -> bool {
        let threshold = (self.context_limit as f64 * COMPACTION_THRESHOLD) as u32;
        total_tokens >= threshold
    }
}

// ── Layer 1: Prune ──────────────────────────────────────────────────────────

/// Prune tool outputs in messages to reduce token count.
/// Truncates old tool outputs to TOOL_OUTPUT_MAX_CHARS.
/// Protected tools (agent) are not pruned.
/// Returns the number of messages modified.
pub fn prune_messages(messages: &mut [ChatMessage]) -> usize {
    let mut modified = 0;
    for msg in messages.iter_mut() {
        if let ChatMessage::ToolResult { content, name, .. } = msg {
            if content.len() > TOOL_OUTPUT_MAX_CHARS {
                // Check if this tool is protected
                let is_protected = PRUNE_PROTECTED_TOOLS.contains(&name.as_str());
                if !is_protected {
                    *content = format!(
                        "{}\n\n[... output truncated ({} chars omitted) ...]",
                        &content[..TOOL_OUTPUT_MAX_CHARS.min(content.len())],
                        content.len().saturating_sub(TOOL_OUTPUT_MAX_CHARS)
                    );
                    modified += 1;
                }
            }
        }
    }
    modified
}

// ── Layer 2: Overflow check ─────────────────────────────────────────────────

/// Determine if compaction is needed based on message count and context usage.
pub fn should_compact(messages: &[ChatMessage], context_usage_pct: f64) -> bool {
    // Compact if context is >85% full or message count exceeds 40
    context_usage_pct > 85.0 || messages.len() > 40
}

/// More precise overflow check using token counts.
pub fn should_compact_tokens(
    total_tokens: u32,
    context_limit: u32,
    message_count: usize,
) -> bool {
    let threshold = (context_limit as f64 * COMPACTION_THRESHOLD) as u32;
    total_tokens >= threshold || message_count > 40
}

// ── Layer 3: Compact ────────────────────────────────────────────────────────

/// Extract old messages for summarization, keeping the most recent ones.
pub fn messages_to_compact(messages: &[ChatMessage], keep_last: usize) -> Vec<ChatMessage> {
    if messages.len() <= keep_last {
        return vec![];
    }
    // Keep system messages + last N messages, compact the middle
    let system_count = messages
        .iter()
        .take_while(|m| matches!(m, ChatMessage::System { .. }))
        .count();

    let compact_start = system_count;
    let compact_end = messages.len().saturating_sub(keep_last);
    if compact_start >= compact_end {
        return vec![];
    }

    messages[compact_start..compact_end].to_vec()
}

/// Build a compaction prompt asking the LLM to summarize.
pub fn compaction_prompt(messages: &[ChatMessage]) -> String {
    let conversation = messages
        .iter()
        .map(|m| match m {
            ChatMessage::System { content } => format!("[System]\n{content}"),
            ChatMessage::User { content } => format!("[User]\n{content}"),
            ChatMessage::Assistant { content, .. } => format!("[Assistant]\n{content}"),
            ChatMessage::ToolResult { content, .. } => format!("[Tool Result]\n{content}"),
        })
        .collect::<Vec<_>>()
        .join("\n\n");

    format!(
        "{SUMMARY_TEMPLATE}\n\n---\nConversation to summarize:\n\n{conversation}"
    )
}

/// Run the full compaction flow: prune → check → compact.
/// Returns the summary if compaction was applied, or None.
pub async fn compact(
    client: &dyn ChatClient,
    messages: &[ChatMessage],
    context_usage_pct: f64,
) -> Result<String, crate::error::AgentError> {
    // Layer 1: Prune is done in-place before calling this

    // Layer 2: Check
    if !should_compact(messages, context_usage_pct) {
        return Ok(String::new());
    }

    // Layer 3: Compact
    let to_compact = messages_to_compact(messages, 10);
    if to_compact.is_empty() {
        return Ok(String::new());
    }

    let prompt = compaction_prompt(&to_compact);
    let compact_messages = vec![ChatMessage::user(&prompt)];

    match client.invoke(&compact_messages, &[]).await {
        Ok(response) => {
            let summary = match &response {
                ChatMessage::Assistant { content, .. } => content.clone(),
                _ => "Context was compacted but summary was unavailable.".to_string(),
            };
            Ok(summary)
        }
        Err(_) => {
            // Fallback: create a simple summary from message snippets
            Ok(fallback_summary(&to_compact))
        }
    }
}

/// Create a fallback summary when LLM compaction fails.
fn fallback_summary(messages: &[ChatMessage]) -> String {
    let mut parts: Vec<String> = vec!["## Compacted Context (fallback summary)".to_string()];

    for msg in messages {
        let (role, content) = match msg {
            ChatMessage::User { content } => ("User", content.as_str()),
            ChatMessage::Assistant { content, .. } => ("Assistant", content.as_str()),
            ChatMessage::ToolResult { content, name, .. } => {
                (name.as_str(), content.as_str())
            }
            ChatMessage::System { content } => ("System", content.as_str()),
        };

        let truncated = if content.len() > FALLBACK_SUMMARY_MAX_PER_MSG {
            format!("{}...", &content[..FALLBACK_SUMMARY_MAX_PER_MSG])
        } else {
            content.to_string()
        };
        parts.push(format!("- [{}] {}", role, truncated));
    }

    parts.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_prune_messages() {
        let long_output = "x".repeat(5000);
        let mut messages = vec![
            ChatMessage::user("hello"),
            ChatMessage::ToolResult {
                content: long_output,
                tool_call_id: "1".to_string(),
                name: "bash".to_string(),
            },
        ];
        let modified = prune_messages(&mut messages);
        assert_eq!(modified, 1);
        if let ChatMessage::ToolResult { content, .. } = &messages[1] {
            assert!(content.contains("truncated"));
        }
    }

    #[test]
    fn test_prune_protected_tools() {
        let long_output = "x".repeat(5000);
        let mut messages = vec![
            ChatMessage::ToolResult {
                content: long_output,
                tool_call_id: "1".to_string(),
                name: "agent".to_string(),
            },
        ];
        let modified = prune_messages(&mut messages);
        assert_eq!(modified, 0);
    }

    #[test]
    fn test_should_compact() {
        assert!(should_compact(&[], 90.0));
        assert!(!should_compact(&[], 50.0));
    }

    #[test]
    fn test_should_compact_tokens() {
        assert!(should_compact_tokens(115_000, 128_000, 0));
        assert!(!should_compact_tokens(50_000, 128_000, 0));
        assert!(should_compact_tokens(0, 128_000, 50));
    }

    #[test]
    fn test_messages_to_compact() {
        let messages: Vec<ChatMessage> = (0..20)
            .map(|i| ChatMessage::user(format!("msg {i}")))
            .collect();
        let to_compact = messages_to_compact(&messages, 10);
        assert_eq!(to_compact.len(), 10);
    }

    #[test]
    fn test_compaction_service_usable_window() {
        let service = CompactionService::new(128_000, 8_192);
        assert_eq!(service.usable_window(), 128_000 - 20_000 - 8_192);
    }

    #[test]
    fn test_compaction_service_overflow() {
        let service = CompactionService::new(128_000, 8_192);
        assert!(service.is_overflow(120_000));
        assert!(!service.is_overflow(50_000));
    }

    #[test]
    fn test_fallback_summary() {
        let messages = vec![
            ChatMessage::user("hello"),
            ChatMessage::assistant("hi there"),
        ];
        let summary = fallback_summary(&messages);
        assert!(summary.contains("Compacted Context"));
        assert!(summary.contains("hello"));
        assert!(summary.contains("hi there"));
    }
}
