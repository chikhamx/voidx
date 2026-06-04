//! Streaming renderer — processes StreamEvents into user-visible output,
//! and sanitizes messages for replay to the LLM.
//!
//! Ported from `src/voidx/agent/graph/streaming.py`.
//! Includes: StreamAccumulator, DSML parsing, replay sanitization,
//! tool result adjacency repair.

use voidx_llm::streaming::StreamEvent;
use voidx_llm::ChatMessage;

// ── Replay-unsafe block types ────────────────────────────────────────────

/// Block types that should be stripped before replaying assistant history.
const REPLAY_UNSAFE_BLOCK_TYPES: &[&str] = &[
    "thinking",
    "redacted_thinking",
    "reasoning",
    "reasoning_content",
];

// ── DSML markers ─────────────────────────────────────────────────────────

/// DSML (DeepSeek Markup Language) markers for tool calls in text content.
const DSML_MARKER: &str = "||DSML||";

/// Check if text contains DSML tool call markers.
pub fn has_dsml_tool_calls(text: &str) -> bool {
    text.contains(&format!("<{DSML_MARKER}tool_calls"))
}

/// Extract DSML tool calls from text content.
///
/// Returns (cleaned_text, tool_calls). DSML is DeepSeek's way of encoding
/// tool calls as XML-like tags in the text content.
pub fn extract_dsml_tool_calls(text: &str) -> (String, Vec<DSMLToolCall>) {
    let mut cleaned = text.to_string();
    let mut tool_calls = Vec::new();

    // Remove <||DSML||tool_calls>...</||DSML||tool_calls> blocks
    // and parse individual invoke elements
    while let Some(start) = cleaned.find(&format!("<{DSML_MARKER}tool_calls")) {
        let end_tag = format!("</{DSML_MARKER}tool_calls>");
        let end = cleaned.find(&end_tag).map(|i| i + end_tag.len()).unwrap_or(cleaned.len());

        let block = cleaned[start..end].to_string();
        cleaned = format!("{}{}", &cleaned[..start], &cleaned[end..]);

        // Parse invoke elements from the block
        let invoke_start_tag = format!("<{DSML_MARKER}invoke ");
        let invoke_end_tag = format!("</{DSML_MARKER}invoke>");

        let mut search_from = 0;
        while let Some(inv_start) = block[search_from..].find(&invoke_start_tag) {
            let abs_start = search_from + inv_start;
            if let Some(inv_end) = block[abs_start..].find(&invoke_end_tag) {
                let abs_end = abs_start + inv_end + invoke_end_tag.len();
                let invoke_block = &block[abs_start..abs_end];

                if let Some(tc) = parse_dsml_invoke(invoke_block) {
                    tool_calls.push(tc);
                }

                search_from = abs_end;
            } else {
                break;
            }
        }
    }

    (cleaned.trim().to_string(), tool_calls)
}

/// A parsed DSML tool call.
#[derive(Debug, Clone)]
pub struct DSMLToolCall {
    pub name: String,
    pub id: String,
    pub arguments: serde_json::Value,
}

/// Parse a single <||DSML||invoke> block.
fn parse_dsml_invoke(block: &str) -> Option<DSMLToolCall> {
    let invoke_start_tag = format!("<{DSML_MARKER}invoke ");
    let invoke_end_tag = format!("</{DSML_MARKER}invoke>");

    let inner = block
        .strip_prefix(&invoke_start_tag)?
        .strip_suffix(&invoke_end_tag)?;

    // Extract name and id from attributes
    let mut name = String::new();
    let mut id = String::new();

    // Simple attribute parsing: name="value"
    for attr in inner.splitn(2, '>').first()?.split_whitespace() {
        if let Some((key, value)) = attr.split_once('=') {
            let value = value.trim_matches('"');
            match key {
                "name" => name = value.to_string(),
                "id" => id = value.to_string(),
                _ => {}
            }
        }
    }

    // Extract parameters
    let param_start_tag = format!("<{DSML_MARKER}parameter ");
    let param_end_tag = format!("</{DSML_MARKER}parameter>");

    let mut params = serde_json::Map::new();
    let mut search_from = 0;

    while let Some(p_start) = inner[search_from..].find(&param_start_tag) {
        let abs_start = search_from + p_start;
        if let Some(p_end) = inner[abs_start..].find(&param_end_tag) {
            let abs_end = abs_start + p_end + param_end_tag.len();
            let param_block = &inner[abs_start..abs_end];

            // Parse parameter name and value
            if let Some((param_name, param_value)) = parse_dsml_parameter(param_block) {
                params.insert(param_name, param_value);
            }

            search_from = abs_end;
        } else {
            break;
        }
    }

    Some(DSMLToolCall {
        name,
        id,
        arguments: serde_json::Value::Object(params),
    })
}

/// Parse a single <||DSML||parameter> block.
fn parse_dsml_parameter(block: &str) -> Option<(String, serde_json::Value)> {
    let param_start_tag = format!("<{DSML_MARKER}parameter ");
    let param_end_tag = format!("</{DSML_MARKER}parameter>");

    let inner = block
        .strip_prefix(&param_start_tag)?
        .strip_suffix(&param_end_tag)?;

    // Split at > to separate attributes from value
    let (attrs, value) = inner.split_once('>')?;
    let value = value.trim();

    // Extract name from attributes
    let mut param_name = String::new();
    for attr in attrs.split_whitespace() {
        if let Some((key, val)) = attr.split_once('=') {
            if key == "name" {
                param_name = val.trim_matches('"').to_string();
            }
        }
    }

    if param_name.is_empty() {
        return None;
    }

    // Try to parse value as JSON, fall back to string
    let param_value = serde_json::from_str(value)
        .unwrap_or(serde_json::Value::String(value.to_string()));

    Some((param_name, param_value))
}

// ── StreamAccumulator ────────────────────────────────────────────────────

/// Accumulates stream events into a structured result.
#[derive(Debug, Default)]
pub struct StreamAccumulator {
    pub text: String,
    pub tool_call_ids: Vec<String>,
    pub tool_call_names: Vec<String>,
    pub tool_call_args: Vec<String>,
    pub thinking: String,
}

impl StreamAccumulator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Process a single stream event.
    pub fn feed(&mut self, event: &StreamEvent) {
        match event {
            StreamEvent::TextDelta(delta) => {
                self.text.push_str(delta);
            }
            StreamEvent::ToolCallStart { id, name } => {
                self.tool_call_ids.push(id.clone());
                self.tool_call_names.push(name.clone());
                self.tool_call_args.push(String::new());
            }
            StreamEvent::ToolCallDelta { args_delta, .. } => {
                if let Some(last) = self.tool_call_args.last_mut() {
                    last.push_str(args_delta);
                }
            }
            StreamEvent::Thinking(thought) => {
                self.thinking.push_str(thought);
            }
            StreamEvent::MessageComplete => {}
            StreamEvent::Usage(_) => {}
        }
    }

    /// Check if the response includes any tool calls.
    pub fn has_tool_calls(&self) -> bool {
        !self.tool_call_names.is_empty()
    }

    /// Convert accumulated tool calls into ChatMessage ToolCall objects.
    pub fn into_tool_calls(self) -> Vec<voidx_llm::ToolCall> {
        self.tool_call_names
            .iter()
            .enumerate()
            .map(|(i, name)| {
                let id = self.tool_call_ids.get(i).cloned().unwrap_or_default();
                let args: serde_json::Value = self
                    .tool_call_args
                    .get(i)
                    .and_then(|s| serde_json::from_str(s).ok())
                    .unwrap_or_default();
                voidx_llm::ToolCall {
                    id,
                    name: name.clone(),
                    arguments: args,
                }
            })
            .collect()
    }
}

// ── Replay sanitization ──────────────────────────────────────────────────

/// Sanitize messages before replaying them to the LLM.
///
/// This removes:
/// - Reasoning/thinking blocks from assistant messages
/// - DSML tool call markup from text content
/// - Empty assistant messages (no content and no tool calls)
/// - Orphaned tool results (whose parent tool call was removed)
/// - Fixes tool result adjacency (tool results must follow assistant messages)
pub fn sanitize_messages_for_replay(messages: &[ChatMessage]) -> Vec<ChatMessage> {
    let mut sanitized: Vec<ChatMessage> = Vec::new();

    for message in messages {
        match message {
            ChatMessage::Assistant { content, tool_calls } => {
                let clean_content = sanitize_ai_content_for_replay(content);

                // Skip empty assistant messages with no tool calls
                if is_empty_content(&clean_content) && tool_calls.is_empty() {
                    continue;
                }

                // Check for DSML tool calls in content
                let (stripped_content, dsml_calls) = extract_dsml_tool_calls(&clean_content);

                // Merge DSML tool calls with explicit tool calls
                let merged_tool_calls = if dsml_calls.is_empty() {
                    tool_calls.clone()
                } else {
                    let mut merged = tool_calls.clone();
                    for dc in dsml_calls {
                        merged.push(voidx_llm::ToolCall {
                            id: dc.id,
                            name: dc.name,
                            arguments: dc.arguments,
                        });
                    }
                    merged
                };

                sanitized.push(ChatMessage::Assistant {
                    content: stripped_content,
                    tool_calls: merged_tool_calls,
                });
            }
            ChatMessage::System { content } => {
                sanitized.push(ChatMessage::System {
                    content: content.clone(),
                });
            }
            ChatMessage::User { content } => {
                sanitized.push(ChatMessage::User {
                    content: content.clone(),
                });
            }
            ChatMessage::ToolResult {
                content,
                tool_call_id,
                name,
            } => {
                sanitized.push(ChatMessage::ToolResult {
                    content: content.clone(),
                    tool_call_id: tool_call_id.clone(),
                    name: name.clone(),
                });
            }
        }
    }

    repair_tool_result_adjacency(&sanitized)
}

/// Sanitize assistant message content for replay.
///
/// Strips reasoning/thinking blocks that some providers include in content.
fn sanitize_ai_content_for_replay(content: &str) -> String {
    let mut result = content.to_string();

    // Strip thinking blocks: <thinking>...</thinking>
    while let Some(start) = result.find("<thinking>") {
        let end_tag = "</thinking>";
        if let Some(end) = result[start..].find(end_tag) {
            let abs_end = start + end + end_tag.len();
            result = format!("{}{}", &result[..start], &result[abs_end..]);
        } else {
            // Unclosed tag — remove from start to end
            result.truncate(start);
        }
    }

    // Strip reasoning_content blocks
    while let Some(start) = result.find("<reasoning_content>") {
        let end_tag = "</reasoning_content>";
        if let Some(end) = result[start..].find(end_tag) {
            let abs_end = start + end + end_tag.len();
            result = format!("{}{}", &result[..start], &result[abs_end..]);
        } else {
            result.truncate(start);
        }
    }

    // Strip redacted_thinking blocks
    while let Some(start) = result.find("<redacted_thinking>") {
        let end_tag = "</redacted_thinking>";
        if let Some(end) = result[start..].find(end_tag) {
            let abs_end = start + end + end_tag.len();
            result = format!("{}{}", &result[..start], &result[abs_end..]);
        } else {
            result.truncate(start);
        }
    }

    result.trim().to_string()
}

/// Check if content is effectively empty after sanitization.
fn is_empty_content(content: &str) -> bool {
    content.trim().is_empty()
}

/// Repair tool result adjacency.
///
/// Tool results must immediately follow an assistant message with tool calls.
/// If we find a tool result that doesn't have a preceding assistant message
/// with matching tool_call_ids, we need to fix it.
fn repair_tool_result_adjacency(messages: &[ChatMessage]) -> Vec<ChatMessage> {
    let mut repaired: Vec<ChatMessage> = Vec::new();
    let mut valid_tool_call_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

    for message in messages {
        match message {
            ChatMessage::Assistant { content, tool_calls } => {
                // Track which tool call IDs are valid from this assistant message
                valid_tool_call_ids.clear();
                for tc in tool_calls {
                    valid_tool_call_ids.insert(tc.id.clone());
                }
                repaired.push(ChatMessage::Assistant {
                    content: content.clone(),
                    tool_calls: tool_calls.clone(),
                });
            }
            ChatMessage::ToolResult {
                content,
                tool_call_id,
                name,
            } => {
                // Only include tool results whose parent tool call exists
                if valid_tool_call_ids.contains(tool_call_id) {
                    repaired.push(ChatMessage::ToolResult {
                        content: content.clone(),
                        tool_call_id: tool_call_id.clone(),
                        name: name.clone(),
                    });
                }
                // Otherwise, skip the orphaned tool result
            }
            _ => {
                // System and User messages reset the tool call context
                valid_tool_call_ids.clear();
                repaired.push(message.clone());
            }
        }
    }

    repaired
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stream_accumulator_basic() {
        let mut acc = StreamAccumulator::new();
        acc.feed(&StreamEvent::TextDelta("Hello ".to_string()));
        acc.feed(&StreamEvent::TextDelta("World".to_string()));
        acc.feed(&StreamEvent::ToolCallStart {
            id: "t1".to_string(),
            name: "bash".to_string(),
        });
        acc.feed(&StreamEvent::ToolCallDelta {
            id: "".to_string(),
            args_delta: r#"{"command":"#.to_string(),
        });
        acc.feed(&StreamEvent::ToolCallDelta {
            id: "".to_string(),
            args_delta: r#""ls"}"#.to_string(),
        });

        assert_eq!(acc.text, "Hello World");
        assert!(acc.has_tool_calls());
        assert_eq!(acc.tool_call_names, vec!["bash"]);
    }

    #[test]
    fn test_sanitize_ai_content_removes_thinking() {
        let input = "Let me think...\n<thinking>I should check the file</thinking>\nHere's what I found.";
        let result = sanitize_ai_content_for_replay(input);
        assert!(!result.contains("<thinking>"));
        assert!(!result.contains("I should check the file"));
        assert!(result.contains("Let me think"));
        assert!(result.contains("Here's what I found"));
    }

    #[test]
    fn test_sanitize_ai_content_removes_reasoning() {
        let input = "<reasoning_content>step by step</reasoning_content>Final answer";
        let result = sanitize_ai_content_for_replay(input);
        assert!(!result.contains("reasoning_content"));
        assert!(result.contains("Final answer"));
    }

    #[test]
    fn test_sanitize_messages_removes_empty_assistant() {
        let messages = vec![
            ChatMessage::user("hello"),
            ChatMessage::assistant(""),
            ChatMessage::assistant("world"),
        ];
        let result = sanitize_messages_for_replay(&messages);
        assert_eq!(result.len(), 2);
        assert!(matches!(&result[0], ChatMessage::User { .. }));
        assert!(matches!(&result[1], ChatMessage::Assistant { content, tool_calls } if content == "world"));
    }

    #[test]
    fn test_repair_adjacency_removes_orphaned_tool_results() {
        let messages = vec![
            ChatMessage::user("hello"),
            ChatMessage::ToolResult {
                content: "orphaned".to_string(),
                tool_call_id: "tc1".to_string(),
                name: "bash".to_string(),
            },
        ];
        let result = repair_tool_result_adjacency(&messages);
        assert_eq!(result.len(), 1); // orphaned tool result removed
    }

    #[test]
    fn test_repair_adjacency_keeps_valid_tool_results() {
        let messages = vec![
            ChatMessage::assistant_with_tools(
                "",
                vec![voidx_llm::ToolCall {
                    id: "tc1".to_string(),
                    name: "bash".to_string(),
                    arguments: serde_json::json!({}),
                }],
            ),
            ChatMessage::ToolResult {
                content: "output".to_string(),
                tool_call_id: "tc1".to_string(),
                name: "bash".to_string(),
            },
        ];
        let result = repair_tool_result_adjacency(&messages);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn test_into_tool_calls() {
        let mut acc = StreamAccumulator::new();
        acc.feed(&StreamEvent::ToolCallStart {
            id: "tc1".to_string(),
            name: "read".to_string(),
        });
        acc.feed(&StreamEvent::ToolCallDelta {
            id: "".to_string(),
            args_delta: r#"{"path":"/tmp/f.rs"}"#.to_string(),
        });

        let calls = acc.into_tool_calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "read");
        assert_eq!(calls[0].id, "tc1");
    }
}
