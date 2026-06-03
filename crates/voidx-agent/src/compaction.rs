//! Context compaction — summarizes old messages when context window fills.
//!
//! Ported from `src/voidx/agent/graph_components/compaction.rs`.

use voidx_llm::{ChatClient, ChatMessage};

/// Determine if compaction is needed based on message count and context usage.
pub fn should_compact(messages: &[ChatMessage], context_usage_pct: f64) -> bool {
    // Compact if context is >85% full or message count exceeds 40
    context_usage_pct > 85.0 || messages.len() > 40
}

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
        "Summarize the following conversation segment concisely, \
         preserving key decisions, tool outputs, and open questions:\n\n{conversation}"
    )
}

/// Run compaction: ask the LLM to summarize old messages.
pub async fn compact(
    client: &dyn ChatClient,
    messages: &[ChatMessage],
    _context_usage_pct: f64,
) -> Result<String, voidx_llm::error::LlmError> {
    let to_compact = messages_to_compact(messages, 10);
    if to_compact.is_empty() {
        return Ok(String::new());
    }

    let prompt = compaction_prompt(&to_compact);
    let request = vec![ChatMessage::user(prompt)];

    let response = client.invoke(&request, &[]).await?;
    match response {
        ChatMessage::Assistant { content, .. } => Ok(content),
        _ => Ok(String::new()),
    }
}
