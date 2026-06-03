//! Protocol resolution — maps each provider to Anthropic or OpenAI wire protocol.
//!
//! Ported from `src/voidx/llm/provider.py`.

use std::collections::HashMap;
use std::sync::LazyLock;

/// Which wire protocol a provider uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Protocol {
    Anthropic,
    OpenAI,
}

static PROVIDER_PROTOCOLS: LazyLock<HashMap<&'static str, Protocol>> = LazyLock::new(|| {
    HashMap::from([
        ("anthropic", Protocol::Anthropic),
        ("deepseek", Protocol::Anthropic),
        ("openai", Protocol::OpenAI),
        ("openrouter", Protocol::OpenAI),
        ("mimo", Protocol::Anthropic),
        ("mimo-token-plan", Protocol::Anthropic),
        ("qwen", Protocol::Anthropic),
        ("zhipu", Protocol::Anthropic),
        ("kimi", Protocol::Anthropic),
        ("doubao", Protocol::OpenAI),
    ])
});

static DEFAULT_BASE_URLS: LazyLock<HashMap<(&'static str, Protocol), &'static str>> =
    LazyLock::new(|| {
        use Protocol::*;
        HashMap::from([
            (("anthropic", Anthropic), "https://api.anthropic.com"),
            (("openai", OpenAI), "https://api.openai.com/v1"),
            (("deepseek", Anthropic), "https://api.deepseek.com/anthropic"),
            (("deepseek", OpenAI), "https://api.deepseek.com/v1"),
            (("openrouter", OpenAI), "https://openrouter.ai/api/v1"),
            (("mimo", OpenAI), "https://api.xiaomimimo.com/v1"),
            (("mimo", Anthropic), "https://api.xiaomimimo.com/anthropic"),
            (
                ("mimo-token-plan", OpenAI),
                "https://token-plan-cn.xiaomimimo.com/v1",
            ),
            (
                ("mimo-token-plan", Anthropic),
                "https://token-plan-cn.xiaomimimo.com/anthropic",
            ),
            (
                ("qwen", OpenAI),
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            (
                ("qwen", Anthropic),
                "https://dashscope.aliyuncs.com/apps/anthropic",
            ),
            (("zhipu", OpenAI), "https://open.bigmodel.cn/api/paas/v4"),
            (("zhipu", Anthropic), "https://open.bigmodel.cn/api/anthropic"),
            (("kimi", OpenAI), "https://api.moonshot.cn/v1"),
            (("kimi", Anthropic), "https://api.moonshot.cn/anthropic"),
            (
                ("doubao", OpenAI),
                "https://ark.cn-beijing.volces.com/api/v3",
            ),
        ])
    });

/// Resolve which wire protocol a provider uses.
pub fn resolve_protocol(provider: &str, explicit_protocol: Option<&str>) -> Protocol {
    if let Some(p) = explicit_protocol {
        return match p {
            "anthropic" => Protocol::Anthropic,
            _ => Protocol::OpenAI,
        };
    }
    PROVIDER_PROTOCOLS
        .get(provider)
        .copied()
        .unwrap_or(Protocol::OpenAI)
}

/// Return the default base URL for a (provider, protocol) pair.
pub fn default_base_url(provider: &str, protocol: Protocol) -> Option<&'static str> {
    DEFAULT_BASE_URLS.get(&(provider, protocol)).copied()
}

/// Return context-window token limit for a provider.
pub fn context_limit(provider: &str) -> u32 {
    match provider {
        "deepseek" => 1_000_000,
        "anthropic" => 200_000,
        "openai" => 1_050_000,
        "openrouter" => 128_000,
        "mimo" | "mimo-token-plan" => 1_000_000,
        "qwen" => 1_000_000,
        "zhipu" => 200_000,
        "kimi" => 262_144,
        "doubao" => 256_000,
        _ => 128_000,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resolve_anthropic_native() {
        assert_eq!(resolve_protocol("anthropic", None), Protocol::Anthropic);
    }

    #[test]
    fn test_resolve_openai_native() {
        assert_eq!(resolve_protocol("openai", None), Protocol::OpenAI);
    }

    #[test]
    fn test_resolve_deepseek_uses_anthropic() {
        assert_eq!(resolve_protocol("deepseek", None), Protocol::Anthropic);
    }

    #[test]
    fn test_resolve_explicit_overrides() {
        assert_eq!(
            resolve_protocol("deepseek", Some("openai")),
            Protocol::OpenAI
        );
    }

    #[test]
    fn test_unknown_provider_defaults_to_openai() {
        assert_eq!(resolve_protocol("unknown-xyz", None), Protocol::OpenAI);
    }

    #[test]
    fn test_context_limits() {
        assert_eq!(context_limit("deepseek"), 1_000_000);
        assert_eq!(context_limit("anthropic"), 200_000);
        assert_eq!(context_limit("openai"), 1_050_000);
    }
}
