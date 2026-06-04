//! Hydrate persisted message rows into ChatMessage objects.
//!
//! Ported from `src/voidx/agent/message_rows.py`.
//!
//! Converts `TranscriptMessage` rows from the database into `ChatMessage`
//! enum variants suitable for feeding into the LLM client.

use voidx_llm::{ChatMessage, ToolCall};
use voidx_memory::transcript::TranscriptMessage;

/// Convert transcript rows into ChatMessage objects.
pub fn messages_from_rows(rows: &[TranscriptMessage]) -> Vec<ChatMessage> {
    let mut messages: Vec<ChatMessage> = Vec::new();

    for row in rows {
        let _msg_id = if row.id > 0 {
            Some(row.id.to_string())
        } else {
            None
        };

        match row.role.as_str() {
            "system" => {
                messages.push(ChatMessage::System {
                    content: row.content.clone(),
                });
            }
            "user" => {
                // Try to parse structured content
                let content = parse_structured_content(&row.content);
                messages.push(ChatMessage::User { content });
            }
            "assistant" => {
                let tool_calls = parse_tool_calls(&row.tool_calls);
                let content = parse_structured_content(&row.content);
                messages.push(ChatMessage::Assistant {
                    content,
                    tool_calls,
                });
            }
            "tool" => {
                let tool_call_id = row.tool_call_id.clone().unwrap_or_default();
                messages.push(ChatMessage::ToolResult {
                    content: row.content.clone(),
                    tool_call_id,
                    name: String::new(),
                });
            }
            _ => {
                // Unknown role — skip
                tracing::warn!("Skipping message with unknown role: {}", row.role);
            }
        }
    }

    messages
}

/// Parse tool_calls from JSON value.
fn parse_tool_calls(value: &Option<serde_json::Value>) -> Vec<ToolCall> {
    match value {
        Some(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|item| {
                let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let name = item
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let arguments = item
                    .get("arguments")
                    .cloned()
                    .unwrap_or(serde_json::Value::Object(Default::default()));

                if id.is_empty() && name.is_empty() {
                    None
                } else {
                    Some(ToolCall {
                        id,
                        name,
                        arguments,
                    })
                }
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// Try to parse structured content from a JSON string.
/// If the content is a valid JSON array (structured content format),
/// return it as a formatted string. Otherwise return as-is.
fn parse_structured_content(content: &str) -> String {
    // For now, return content as-is.
    // Structured content (multi-part messages with images) is handled
    // at a higher level when building the LLM request.
    content.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_row(id: i64, role: &str, content: &str) -> TranscriptMessage {
        TranscriptMessage {
            id,
            role: role.to_string(),
            content: content.to_string(),
            tool_calls: None,
            tool_call_id: None,
            created_at: String::new(),
        }
    }

    fn make_row_with_tool_calls(
        id: i64,
        role: &str,
        content: &str,
        tool_calls: serde_json::Value,
        tool_call_id: Option<&str>,
    ) -> TranscriptMessage {
        TranscriptMessage {
            id,
            role: role.to_string(),
            content: content.to_string(),
            tool_calls: Some(tool_calls),
            tool_call_id: tool_call_id.map(|s| s.to_string()),
            created_at: String::new(),
        }
    }

    #[test]
    fn test_messages_from_rows_basic() {
        let rows = vec![
            make_row(1, "system", "You are helpful."),
            make_row(2, "user", "Hello"),
            make_row(3, "assistant", "Hi there!"),
        ];

        let messages = messages_from_rows(&rows);
        assert_eq!(messages.len(), 3);

        assert!(matches!(&messages[0], ChatMessage::System { content } if content == "You are helpful."));
        assert!(matches!(&messages[1], ChatMessage::User { content } if content == "Hello"));
        assert!(matches!(&messages[2], ChatMessage::Assistant { content, tool_calls } if content == "Hi there!" && tool_calls.is_empty()));
    }

    #[test]
    fn test_messages_from_rows_with_tool_calls() {
        let tool_calls = serde_json::json!([
            {"id": "call_1", "name": "read", "arguments": {"path": "/tmp/test.rs"}}
        ]);

        let rows = vec![
            make_row_with_tool_calls(1, "assistant", "", tool_calls, None),
            make_row_with_tool_calls(2, "tool", "file content", None, Some("call_1")),
        ];

        let messages = messages_from_rows(&rows);
        assert_eq!(messages.len(), 2);

        if let ChatMessage::Assistant { tool_calls, .. } = &messages[0] {
            assert_eq!(tool_calls.len(), 1);
            assert_eq!(tool_calls[0].name, "read");
        } else {
            panic!("Expected Assistant message");
        }

        if let ChatMessage::ToolResult { tool_call_id, .. } = &messages[1] {
            assert_eq!(tool_call_id, "call_1");
        } else {
            panic!("Expected ToolResult message");
        }
    }

    #[test]
    fn test_messages_from_rows_skips_unknown_role() {
        let rows = vec![
            make_row(1, "user", "Hello"),
            make_row(2, "unknown", "???"),
            make_row(3, "assistant", "Hi"),
        ];

        let messages = messages_from_rows(&rows);
        assert_eq!(messages.len(), 2);
    }
}
