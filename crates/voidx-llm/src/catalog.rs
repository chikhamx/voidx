//! Model catalog — typed abstraction over provider model discovery.
//!
//! Ported from `src/voidx/llm/catalog.py`.

use std::collections::HashMap;
use std::sync::LazyLock;

/// Static fallback model lists for each provider.
static STATIC_MODELS: LazyLock<HashMap<&'static str, &'static [&'static str]>> =
    LazyLock::new(|| {
        HashMap::from([
            (
                "anthropic",
                &[
                    "claude-opus-4-8",
                    "claude-sonnet-4-6",
                    "claude-opus-4-7",
                    "claude-haiku-4-5",
                ][..],
            ),
            (
                "openai",
                &[
                    "gpt-5.5",
                    "gpt-5.4-mini",
                    "gpt-5.4-nano",
                    "o3",
                    "o4-mini",
                ][..],
            ),
            (
                "deepseek",
                &["deepseek-v4-pro", "deepseek-v4-flash"][..],
            ),
            (
                "mimo",
                &["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2.5-tts"][..],
            ),
            (
                "mimo-token-plan",
                &["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2.5-tts"][..],
            ),
            (
                "qwen",
                &[
                    "qwen3.7-max",
                    "qwen3-max",
                    "qwen3.6-plus",
                    "qwen-plus",
                    "qwen-turbo",
                ][..],
            ),
            (
                "zhipu",
                &["glm-5.1", "glm-5", "glm-4.7", "glm-4.7-flash"][..],
            ),
            (
                "kimi",
                &["kimi-k2.6", "kimi-k2.5", "kimi-k2"][..],
            ),
            (
                "doubao",
                &[
                    "doubao-seed-1.6-thinking",
                    "doubao-seed-1.6",
                    "doubao-seed-1.6-flash",
                ][..],
            ),
        ])
    });

/// Return available model names for a provider (static list only for now).
/// Dynamic fetchers (e.g. OpenRouter) will be added later.
pub fn list_models(provider: &str) -> Vec<String> {
    STATIC_MODELS
        .get(provider)
        .map(|models| models.iter().map(|s| s.to_string()).collect())
        .unwrap_or_default()
}

/// Return the known providers.
pub fn providers() -> Vec<&'static str> {
    STATIC_MODELS.keys().copied().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_list_anthropic_models() {
        let models = list_models("anthropic");
        assert!(models.contains(&"claude-opus-4-8".to_string()));
        assert!(models.contains(&"claude-haiku-4-5".to_string()));
    }

    #[test]
    fn test_list_unknown_provider() {
        assert!(list_models("nonexistent").is_empty());
    }

    #[test]
    fn test_all_providers_have_models() {
        for provider in providers() {
            let models = list_models(provider);
            assert!(
                !models.is_empty(),
                "provider '{provider}' has no models"
            );
        }
    }
}
