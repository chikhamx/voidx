//! OpenAI Chat Completions API client.
//!
//! Covers: openai, openrouter, doubao
//! (all providers that speak the OpenAI wire protocol).

use crate::client::{ChatClient, ChatMessage, ToolCall, ToolDefinition};
use crate::error::LlmError;
use crate::protocol::{default_base_url, Protocol};
use crate::reasoning::reasoning_kwargs;
use crate::streaming::{SseStream, StreamEvent};
use async_trait::async_trait;
use futures::stream::Stream;
use std::pin::Pin;
use voidx_config::ModelConfig;

pub struct OpenAIClient {
    api_key: String,
    model: String,
    base_url: String,
    provider: String,
    http: reqwest::Client,
    max_tokens: u32,
    temperature: f64,
    reasoning_effort: Option<crate::reasoning::ReasoningEffort>,
}

impl OpenAIClient {
    pub fn new(config: &ModelConfig, api_key: &str, _protocol: Protocol) -> Self {
        let base_url = config
            .base_url
            .clone()
            .or_else(|| {
                default_base_url(&config.provider, Protocol::OpenAI).map(|s| s.to_string())
            })
            .unwrap_or_else(|| "https://api.openai.com/v1".to_string());

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

    fn chat_url(&self) -> String {
        format!(
            "{}/chat/completions",
            self.base_url.trim_end_matches('/')
        )
    }

    fn build_body(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
        stream: bool,
    ) -> serde_json::Value {
        let openai_messages: Vec<serde_json::Value> = messages
            .iter()
            .map(|m| match m {
                ChatMessage::System { content } => {
                    serde_json::json!({"role": "system", "content": content})
                }
                ChatMessage::User { content } => {
                    serde_json::json!({"role": "user", "content": content})
                }
                ChatMessage::Assistant {
                    content,
                    tool_calls,
                } => {
                    let mut msg =
                        serde_json::json!({"role": "assistant", "content": content});
                    if !tool_calls.is_empty() {
                        let calls: Vec<serde_json::Value> = tool_calls
                            .iter()
                            .map(|tc| {
                                serde_json::json!({
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": tc.arguments.to_string(),
                                    }
                                })
                            })
                            .collect();
                        msg["tool_calls"] = serde_json::Value::Array(calls);
                    }
                    msg
                }
                ChatMessage::ToolResult {
                    content,
                    tool_call_id,
                    name,
                } => serde_json::json!({
                    "role": "tool",
                    "content": content,
                    "tool_call_id": tool_call_id,
                    "name": name,
                }),
            })
            .collect();

        let mut body = serde_json::json!({
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": stream,
        });

        // Tools
        if !tools.is_empty() {
            let tool_defs: Vec<serde_json::Value> = tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                            "strict": true,
                        }
                    })
                })
                .collect();
            body["tools"] = serde_json::Value::Array(tool_defs);
        }

        // Reasoning kwargs
        let reasoning = reasoning_kwargs(
            &self.provider,
            Protocol::OpenAI,
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
                code: error.get("code").and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(400),
                message: msg.to_string(),
            });
        }

        let choices = response
            .get("choices")
            .and_then(|v| v.as_array())
            .ok_or_else(|| LlmError::Parse("No choices in response".to_string()))?;

        let choice = choices.first().ok_or_else(|| {
            LlmError::Parse("Empty choices array".to_string())
        })?;

        let message = choice
            .get("message")
            .ok_or_else(|| LlmError::Parse("No message in choice".to_string()))?;

        let content = message
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let tool_calls: Vec<ToolCall> = message
            .get("tool_calls")
            .and_then(|v| v.as_array())
            .map(|calls| {
                calls
                    .iter()
                    .filter_map(|tc| {
                        let function = tc.get("function")?;
                        Some(ToolCall {
                            id: tc
                                .get("id")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string(),
                            name: function
                                .get("name")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string(),
                            arguments: function
                                .get("arguments")
                                .and_then(|v| v.as_str())
                                .and_then(|s| serde_json::from_str(s).ok())
                                .unwrap_or_default(),
                        })
                    })
                    .collect()
                })
            .unwrap_or_default();

        Ok(ChatMessage::Assistant {
            content,
            tool_calls,
        })
    }
}

#[async_trait]
impl ChatClient for OpenAIClient {
    async fn invoke(
        &self,
        messages: &[ChatMessage],
        tools: &[ToolDefinition],
    ) -> Result<ChatMessage, LlmError> {
        let body = self.build_body(messages, tools, false);

        let response = self
            .http
            .post(self.chat_url())
            .header("Authorization", format!("Bearer {}", self.api_key))
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
            .post(self.chat_url())
            .header("Authorization", format!("Bearer {}", self.api_key))
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
        let sse_stream = SseStream::new(byte_stream, Protocol::OpenAI);
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
        let client = OpenAIClient {
            api_key: "test".into(),
            model: "gpt-5.4-mini".into(),
            base_url: "https://api.openai.com/v1".into(),
            provider: "openai".into(),
            http: reqwest::Client::new(),
            max_tokens: 1024,
            temperature: 0.0,
            reasoning_effort: None,
        };

        let json = serde_json::json!({
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "model": "gpt-5.4-mini",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello! How can I help?"
                },
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        });

        let msg = client.parse_response(json).unwrap();
        match msg {
            ChatMessage::Assistant { content, tool_calls } => {
                assert_eq!(content, "Hello! How can I help?");
                assert!(tool_calls.is_empty());
            }
            _ => panic!("Expected assistant message"),
        }
    }

    #[test]
    fn test_parse_tool_call_response() {
        let client = OpenAIClient {
            api_key: "test".into(),
            model: "gpt-5.4-mini".into(),
            base_url: "https://api.openai.com/v1".into(),
            provider: "openai".into(),
            http: reqwest::Client::new(),
            max_tokens: 1024,
            temperature: 0.0,
            reasoning_effort: None,
        };

        let json = serde_json::json!({
            "id": "chatcmpl-456",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": "{\"command\":\"ls\"}"
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        });

        let msg = client.parse_response(json).unwrap();
        match msg {
            ChatMessage::Assistant { tool_calls, .. } => {
                assert_eq!(tool_calls.len(), 1);
                assert_eq!(tool_calls[0].name, "bash");
                assert_eq!(tool_calls[0].arguments["command"], "ls");
            }
            _ => panic!("Expected assistant message"),
        }
    }
}
