//! Smoke test for Anthropic client — requires ANTHROPIC_API_KEY env var.
//!
//! Run with: cargo test -p voidx-llm -- --ignored --nocapture

use voidx_config::ModelConfig;
use voidx_llm::{AnthropicClient, ChatClient, ChatMessage, Protocol, StreamEvent};

fn test_config() -> ModelConfig {
    ModelConfig {
        provider: "anthropic".to_string(),
        model: "claude-haiku-4-5".to_string(),
        protocol: None,
        base_url: None,
        temperature: 0.0,
        max_tokens: 1024,
        reasoning_effort: None,
    }
}

fn api_key() -> String {
    std::env::var("ANTHROPIC_API_KEY").expect("ANTHROPIC_API_KEY not set")
}

#[tokio::test]
#[ignore]
async fn test_anthropic_simple_message() {
    let client = AnthropicClient::new(&test_config(), &api_key(), Protocol::Anthropic);

    let messages = vec![ChatMessage::user(
        "Reply with exactly the word 'ok' and nothing else.",
    )];

    let result = client.invoke(&messages, &[]).await.unwrap();

    match result {
        ChatMessage::Assistant { content, .. } => {
            assert!(content.to_lowercase().contains("ok"), "Got: {content}");
        }
        _ => panic!("Expected assistant message"),
    }
}

#[tokio::test]
#[ignore]
async fn test_anthropic_streaming() {
    use futures::StreamExt;

    let client = AnthropicClient::new(&test_config(), &api_key(), Protocol::Anthropic);

    let messages = vec![ChatMessage::user("Say hello in exactly 5 words.")];

    let mut stream = client.stream(&messages, &[]).await.unwrap();

    let mut text = String::new();
    while let Some(event) = stream.next().await {
        match event.unwrap() {
            StreamEvent::TextDelta(delta) => {
                text.push_str(&delta);
            }
            StreamEvent::MessageComplete => break,
            _ => {}
        }
    }

    assert!(!text.is_empty(), "Should have received text");
    println!("Streamed text: {text}");
}
