//! SSE streaming parser — extracts text deltas and tool_call deltas from
//! Anthropic and OpenAI streaming responses.
//!
//! Ported from `src/voidx/agent/graph_components/streaming.py`.

use crate::error::LlmError;
use crate::Protocol;
use futures::stream::Stream;
use serde_json::Value;
use std::pin::Pin;
use std::task::{Context, Poll};

/// Events emitted during streaming.
#[derive(Debug, Clone)]
pub enum StreamEvent {
    /// A chunk of text content.
    TextDelta(String),
    /// Start of a tool call.
    ToolCallStart { id: String, name: String },
    /// A chunk of tool call arguments (JSON fragment).
    ToolCallDelta { id: String, args_delta: String },
    /// End of a completed message (all text + tool calls populated).
    MessageComplete,
    /// Thinking/reasoning block (collapsed in terminal by default).
    Thinking(String),
    /// Usage info from stream end.
    Usage(super::usage::TokenUsage),
}

/// SSE line parser that translates raw SSE events into StreamEvents.
pub struct SseStream<S>
where
    S: Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Unpin,
{
    inner: S,
    protocol: Protocol,
    buffer: String,
    done: bool,
}

impl<S> SseStream<S>
where
    S: Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Unpin,
{
    pub fn new(inner: S, protocol: Protocol) -> Self {
        Self {
            inner,
            protocol,
            buffer: String::new(),
            done: false,
        }
    }

    fn parse_line(&self, line: &str) -> Option<StreamEvent> {
        let line = line.trim();
        if line.is_empty() || line.starts_with(':') {
            return None;
        }
        let data = line.strip_prefix("data: ")?;
        if data == "[DONE]" {
            return Some(StreamEvent::MessageComplete);
        }
        let value: Value = serde_json::from_str(data).ok()?;

        match self.protocol {
            Protocol::Anthropic => self.parse_anthropic_event(&value),
            Protocol::OpenAI => self.parse_openai_event(&value),
        }
    }

    fn parse_anthropic_event(&self, value: &Value) -> Option<StreamEvent> {
        let event_type = value.get("type")?.as_str()?;

        match event_type {
            "content_block_start" => {
                let block = value.get("content_block")?;
                if block.get("type")?.as_str()? == "tool_use" {
                    Some(StreamEvent::ToolCallStart {
                        id: block.get("id")?.as_str()?.to_string(),
                        name: block.get("name")?.as_str()?.to_string(),
                    })
                } else {
                    None
                }
            }
            "content_block_delta" => {
                let delta = value.get("delta")?;
                let delta_type = delta.get("type")?.as_str()?;
                match delta_type {
                    "text_delta" => {
                        let text = delta.get("text")?.as_str()?;
                        if text.is_empty() {
                            None
                        } else {
                            Some(StreamEvent::TextDelta(text.to_string()))
                        }
                    }
                    "input_json_delta" => {
                        let partial = delta.get("partial_json")?.as_str()?;
                        Some(StreamEvent::ToolCallDelta {
                            id: String::new(), // Anthropic SSE doesn't include id in deltas
                            args_delta: partial.to_string(),
                        })
                    }
                    _ => None,
                }
            }
            "message_delta" => {
                // usage info at stream end
                if let Some(usage) = value.get("usage") {
                    let input = usage.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                    let output = usage.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                    return Some(StreamEvent::Usage(super::usage::TokenUsage {
                        input_tokens: input,
                        output_tokens: output,
                        cache_read_tokens: usage.get("cache_read_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
                        cache_write_tokens: usage.get("cache_creation_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
                        reasoning_tokens: 0,
                        cache_tokens_reported: usage.get("cache_read_input_tokens").and_then(|v| v.as_u64()).unwrap_or(0) > 0,
                    }));
                }
                None
            }
            "message_stop" => Some(StreamEvent::MessageComplete),
            _ => None,
        }
    }

    fn parse_openai_event(&self, value: &Value) -> Option<StreamEvent> {
        let choices = value.get("choices")?.as_array()?;
        let choice = choices.first()?;
        let delta = choice.get("delta")?;

        // Check for tool calls
        if let Some(tool_calls) = delta.get("tool_calls").and_then(|v| v.as_array()) {
            for tc in tool_calls {
                let _index = tc.get("index").and_then(|v| v.as_u64()).unwrap_or(0);
                if let Some(function) = tc.get("function") {
                    if let Some(name) = function.get("name").and_then(|v| v.as_str()) {
                        return Some(StreamEvent::ToolCallStart {
                            id: tc.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            name: name.to_string(),
                        });
                    }
                    if let Some(args) = function.get("arguments").and_then(|v| v.as_str()) {
                        return Some(StreamEvent::ToolCallDelta {
                            id: tc.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            args_delta: args.to_string(),
                        });
                    }
                }
            }
        }

        // Check for text content
        if let Some(content) = delta.get("content").and_then(|v| v.as_str()) {
            if !content.is_empty() {
                return Some(StreamEvent::TextDelta(content.to_string()));
            }
        }

        // Check for reasoning content
        if let Some(reasoning) = delta.get("reasoning_content").and_then(|v| v.as_str()) {
            if !reasoning.is_empty() {
                return Some(StreamEvent::Thinking(reasoning.to_string()));
            }
        }

        // Finish reason
        if choice.get("finish_reason").and_then(|v| v.as_str()).is_some() {
            // usage may be in the top-level value
            if let Some(usage) = value.get("usage") {
                let input = usage.get("prompt_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let output = usage.get("completion_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                return Some(StreamEvent::Usage(super::usage::TokenUsage {
                    input_tokens: input,
                    output_tokens: output,
                    cache_read_tokens: 0,
                    cache_write_tokens: 0,
                    reasoning_tokens: 0,
                    cache_tokens_reported: false,
                }));
            }
            return Some(StreamEvent::MessageComplete);
        }

        None
    }
}

impl<S> Stream for SseStream<S>
where
    S: Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Unpin,
{
    type Item = Result<StreamEvent, LlmError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        if this.done {
            return Poll::Ready(None);
        }

        loop {
            match Pin::new(&mut this.inner).poll_next(cx) {
                Poll::Ready(Some(Ok(bytes))) => {
                    let text = String::from_utf8_lossy(&bytes);
                    this.buffer.push_str(&text);

                    // Process complete lines from buffer
                    while let Some(newline_pos) = this.buffer.find('\n') {
                        let line = this.buffer[..newline_pos].to_string();
                        this.buffer = this.buffer[newline_pos + 1..].to_string();

                        if let Some(event) = this.parse_line(&line) {
                            if matches!(event, StreamEvent::MessageComplete) {
                                this.done = true;
                            }
                            return Poll::Ready(Some(Ok(event)));
                        }
                    }
                    // Continue loop to get more bytes
                }
                Poll::Ready(Some(Err(e))) => {
                    this.done = true;
                    return Poll::Ready(Some(Err(e.into())));
                }
                Poll::Ready(None) => {
                    this.done = true;
                    // Flush remaining buffer
                    if !this.buffer.trim().is_empty() {
                        let line = this.buffer.clone();
                        this.buffer.clear();
                        if let Some(event) = this.parse_line(&line) {
                            return Poll::Ready(Some(Ok(event)));
                        }
                    }
                    return Poll::Ready(None);
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}
