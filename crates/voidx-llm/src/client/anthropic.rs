//! Anthropic Messages API client.
//!
//! Covers: anthropic, deepseek, mimo, mimo-token-plan, qwen, zhipu, kimi
//! (all providers that speak the Anthropic wire protocol).

use crate::client::{ChatClient, ChatMessage, ToolCall, ToolDefinition};
use crate::error::LlmError;
use crate::protocol::{default_base_url, Protocol};
use crate::reasoning::reasoning_kwargs;
use crate::streaming::{SseStream, StreamEvent};
// TokenUsage extracted during streaming — used for stats
use async_trait::async_trait;
use futures::stream::Stream;
use std::pin::Pin;
use voidx_config::ModelConfig;

pub struct AnthropicClient {
    api_key: String,
    model: String,
    base_url: String,
    provider: String,
    http: reqwest::Client,
    max_tokens: u32,
    temperature: f64,
    reasoning_effort: Option<crate::reasoning::ReasoningEffort>,
}

impl AnthropicClient {
    pub fn new(config: &ModelConfig, api_key: &str, _protocol: Protocol) -> Self {
        let base_url = config
            .base_url
            .clone()
            .or_else(|| {
                default_base_url(&config.provider, Protocol::Anthropic).map(|s| s.to_string())
            })
            .unwrap_or_else(|| "https://api.anthropic.com".to_string());

        Self {
            api_key: api_key.to_string(),
            model: config.model.clone(),
            base_url,
            provider: config.provider.clone(),
            http: reqwest::Client::new(),
            max_tokens: config.max_tokens,
            temperature: config.temperature,
            reasoning_effort: config
                .reasoning_effort
                .as_deref()
                .and_then(crate::reasoning::ReasoningEffort::parse),
        }
    }

    fn messages_url(&self) -> String {
        format!("{}/messages", self.base_url.trim_end_matches('/'))
    }

    fn build_body(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
        stream: bool,
    ) -> serde_json::Value {
        let system_messages: Vec<&str> = messages
            .iter()
            .filter_map(|m| match m {
                ChatMessage::System { content } => Some(content.as_str()),
                _ => None,
            })
            .collect();

        let conversation: Vec<serde_json::Value> = messages
            .iter()
            .filter(|m| !matches!(m, ChatMessage::System { .. }))
            .map(|m| match m {
                ChatMessage::User { content } => {
                    serde_json::json!({"role": "user", "content": content})
                }
                ChatMessage::Assistant {
                    content,
                    tool_calls,
                } => {
                    let mut msg = serde_json::json!({"role": "assistant", "content": content});
                    if !tool_calls.is_empty() {
                        let calls: Vec<serde_json::Value> = tool_calls
                            .iter()
                            .map(|tc| {
                                serde_json::json!({
                                    "type": "tool_use",
                                    "id": tc.id,
                                    "name": tc.name,
                                    "input": tc.arguments,
                                })
                            })
                            .collect();
                        msg["content"] = serde_json::Value::Array(calls);
                    }
                    msg
                }
                ChatMessage::ToolResult {
                    content,
                    tool_call_id,
                    name: _,
                } => serde_json::json!({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content,
                    }]
                }),
                ChatMessage::System { .. } => unreachable!(),
            })
            .collect();

        let mut body = serde_json::json!({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": conversation,
            "stream": stream,
        });

        // System prompt — Anthropic uses top-level "system" field
        if !system_messages.is_empty() {
            body["system"] = serde_json::Value::String(system_messages.join("\n\n"));
        }

        // Tools
        if !tools.is_empty() {
            let tool_defs: Vec<serde_json::Value> = tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.parameters,
                    })
                })
                .collect();
            body["tools"] = serde_json::Value::Array(tool_defs);
        }

        // Reasoning / thinking kwargs
        let reasoning = reasoning_kwargs(
            &self.provider,
            Protocol::Anthropic,
            &self.model,
            self.reasoning_effort,
            self.max_tokens,
        );
        if !reasoning.is_null() {
            if let Some(obj) = reasoning.as_object() {
                for (k, v) in obj {
                    body[k] = v.clone();
                }
            }
        }

        body
    }

    fn parse_response(
        &self,
        response: serde_json::Value,
    ) -> Result<ChatMessage, LlmError> {
        // Check for API error
        if let Some(error) = response.get("error") {
            let msg = error
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("Unknown error");
            return Err(LlmError::Api {
                code: 400,
                message: msg.to_string(),
            });
        }

        let content = response.get("content").and_then(|v| v.as_array());
        let content_blocks = match content {
            Some(blocks) => blocks,
            None => {
                return Err(LlmError::Parse("No content in response".to_string()));
            }
        };

        let mut text_parts: Vec<String> = Vec::new();
        let mut tool_calls: Vec<ToolCall> = Vec::new();

        for block in content_blocks {
            match block.get("type").and_then(|v| v.as_str()) {
                Some("text") => {
                    if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                        text_parts.push(t.to_string());
                    }
                }
                Some("tool_use") => {
                    tool_calls.push(ToolCall {
                        id: block
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string(),
                        name: block
                            .get("name")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string(),
                        arguments: block.get("input").cloned().unwrap_or_default(),
                    });
                }
                _ => {}
            }
        }

        // Extract usage
        if let Some(usage) = response.get("usage") {
            let _input = usage.get("input_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
            let _output = usage.get("output_tokens").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
            // TODO: store usage for reporting
        }

        Ok(ChatMessage::Assistant {
            content: text_parts.join("\n"),
            tool_calls,
        })
    }

    fn parse_sse_stream_response(
        &self,
        events: Vec<StreamEvent>,
    ) -> Result<ChatMessage, LlmError> {
        let mut text = String::new();
        let mut tool_calls: std::collections::HashMap<String, (String, String)> =
            std::collections::HashMap::new(); // id -> (name, args_json)

        for event in &events {
            match event {
                StreamEvent::TextDelta(delta) => text.push_str(delta),
                StreamEvent::ToolCallStart { id, name } => {
                    tool_calls.insert(id.clone(), (name.clone(), String::new()));
                }
                StreamEvent::ToolCallDelta { id, args_delta } => {
                    if let Some((_, args)) = tool_calls.get_mut(id) {
                        args.push_str(args_delta);
                    } else {
                        // Anthropic SSE doesn't include id in deltas —
                        // find the most recent tool call
                        if let Some((_, (_, args))) = tool_calls.iter_mut().last() {
                            args.push_str(args_delta);
                        }
                    }
                }
                StreamEvent::Usage(_) => {}
                StreamEvent::MessageComplete => {}
                StreamEvent::Thinking(_) => {}
            }
        }

        let calls: Vec<ToolCall> = tool_calls
            .into_iter()
            .map(|(id, (name, args))| {
                let arguments: serde_json::Value =
                    serde_json::from_str(&args).unwrap_or(serde_json::Value::Null);
                ToolCall {
                    id,
                    name,
                    arguments,
                }
            })
            .collect();

        Ok(ChatMessage::Assistant {
            content: text,
            tool_calls: calls,
        })
    }
}

#[async_trait]
impl ChatClient for AnthropicClient {
    async fn invoke(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
    ) -> Result<ChatMessage, LlmError> {
        let body = self.build_body(messages, tools, false);

        let response = self
            .http
            .post(self.messages_url())
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json")
            .json(&body)
            .send()
            .await?;

        let status = response.status();
        if !status.is_success() {
            let error_text = response.text().await.unwrap_or_default();
            return Err(LlmError::Api {
                code: status.as_u16(),
                message: error_text,
            });
        }

        let json: serde_json::Value = response.json().await?;
        self.parse_response(json)
    }

    async fn stream(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
    ) -> Result<Pin<Box<dyn Stream<Item = Result<StreamEvent, LlmError>> + Send>>, LlmError> {
        let body = self.build_body(messages, tools, true);

        let response = self
            .http
            .post(self.messages_url())
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json")
            .json(&body)
            .send()
            .await?;

        let status = response.status();
        if !status.is_success() {
            let error_text = response.text().await.unwrap_or_default();
            return Err(LlmError::Api {
                code: status.as_u16(),
                message: error_text,
            });
        }

        let byte_stream = response.bytes_stream();
        let sse_stream = SseStream::new(byte_stream, Protocol::Anthropic);
        Ok(Box::pin(sse_stream))
    }

    fn provider(&self) -> &str {
        &self.provider
    }

    fn model(&self) -> &str {
        &self.model
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_simple_text_response() {
        let client = AnthropicClient {
            api_key: "test".into(),
            model: "claude-haiku-4-5".into(),
            base_url: "https://api.anthropic.com".into(),
            provider: "anthropic".into(),
            http: reqwest::Client::new(),
            max_tokens: 1024,
            temperature: 0.0,
            reasoning_effort: None,
        };

        let json = serde_json::json!({
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hello, world!"}
            ],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        });

        let msg = client.parse_response(json).unwrap();
        match msg {
            ChatMessage::Assistant { content, tool_calls } => {
                assert_eq!(content, "Hello, world!");
                assert!(tool_calls.is_empty());
            }
            _ => panic!("Expected assistant message"),
        }
    }

    #[test]
    fn test_parse_tool_use_response() {
        let client = AnthropicClient {
            api_key: "test".into(),
            model: "claude-haiku-4-5".into(),
            base_url: "https://api.anthropic.com".into(),
            provider: "anthropic".into(),
            http: reqwest::Client::new(),
            max_tokens: 1024,
            temperature: 0.0,
            reasoning_effort: None,
        };

        let json = serde_json::json!({
            "id": "msg_456",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_001", "name": "bash", "input": {"command": "ls"}}
            ],
            "model": "claude-haiku-4-5",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 15, "output_tokens": 8}
        });

        let msg = client.parse_response(json).unwrap();
        match msg {
            ChatMessage::Assistant { content, tool_calls } => {
                assert!(content.is_empty());
                assert_eq!(tool_calls.len(), 1);
                assert_eq!(tool_calls[0].name, "bash");
                assert_eq!(tool_calls[0].arguments["command"], "ls");
            }
            _ => panic!("Expected assistant message"),
        }
    }
}
