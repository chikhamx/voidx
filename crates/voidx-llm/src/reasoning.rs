//! Reasoning effort mapping — per-provider thinking/reasoning configuration.
//!
//! Ported from `src/voidx/llm/provider.py` (_reasoning_kwargs, _anthropic_reasoning_kwargs, etc.)

/// Normalized reasoning effort level.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReasoningEffort {
    Minimal,
    Low,
    Medium,
    High,
    XHigh,
    Max,
}

impl ReasoningEffort {
    pub fn parse(input: &str) -> Option<Self> {
        match input.trim().to_lowercase().as_str() {
            "off" | "none" | "" => None,
            "minimal" => Some(Self::Minimal),
            "low" => Some(Self::Low),
            "medium" => Some(Self::Medium),
            "high" => Some(Self::High),
            "xhigh" => Some(Self::XHigh),
            "max" => Some(Self::Max),
            _ => Some(Self::Medium), // default for unknown values
        }
    }
}

/// Anthropic thinking budget in tokens.
const ANTHROPIC_BUDGETS: &[(ReasoningEffort, u32)] = &[
    (ReasoningEffort::Low, 1_024),
    (ReasoningEffort::Medium, 4_096),
    (ReasoningEffort::High, 8_192),
    (ReasoningEffort::XHigh, 16_384),
    (ReasoningEffort::Max, 32_000),
];

fn budget_for(effort: ReasoningEffort) -> u32 {
    ANTHROPIC_BUDGETS
        .iter()
        .find(|(e, _)| *e == effort)
        .map(|(_, b)| *b)
        .unwrap_or(8_192)
}

/// Whether a model name appears to support Anthropic native thinking.
pub fn supports_anthropic_effort(model: &str) -> bool {
    model.to_lowercase().contains("claude-opus-4-")
}

/// Build Anthropic-protocol reasoning kwargs.
pub fn anthropic_reasoning_kwargs(
    model: &str,
    effort: Option<ReasoningEffort>,
    max_tokens: u32,
) -> serde_json::Value {
    let effort = match effort {
        Some(e) => e,
        None => return serde_json::Value::Null,
    };

    if supports_anthropic_effort(model) {
        let level = match effort {
            ReasoningEffort::Minimal => "low",
            ReasoningEffort::Low => "low",
            ReasoningEffort::Medium => "medium",
            ReasoningEffort::High => "high",
            ReasoningEffort::XHigh => "xhigh",
            ReasoningEffort::Max => "max",
        };
        return serde_json::json!({
            "thinking": {"type": "adaptive"},
            "effort": level,
        });
    }

    let budget = budget_for(effort).min(max_tokens.saturating_sub(1));
    if budget < 1_024 {
        return serde_json::Value::Null;
    }
    serde_json::json!({
        "thinking": {"type": "enabled", "budget_tokens": budget},
    })
}

/// Build OpenAI-protocol reasoning kwargs.
pub fn openai_reasoning_kwargs(
    provider: &str,
    model: &str,
    effort: Option<ReasoningEffort>,
) -> serde_json::Value {
    let effort = match effort {
        Some(e) => e,
        None => return serde_json::Value::Null,
    };

    let reasoning_strings: &[(&str, &str)] = &[
        ("none", "none"),
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "high"),
    ];

    let effort_str = reasoning_strings
        .iter()
        .find(|(e, _)| *e == effort_name(effort))
        .map(|(_, s)| *s)
        .unwrap_or("medium");

    match provider {
        "openai" => {
            if !supports_openai_reasoning(model) {
                return serde_json::Value::Null;
            }
            if effort_name(effort) == "none" {
                if model.to_lowercase().starts_with("gpt-5") {
                    return serde_json::json!({"reasoning_effort": "none"});
                }
                return serde_json::json!({"reasoning_effort": "low"});
            }
            serde_json::json!({"reasoning_effort": effort_str})
        }
        "openrouter" => serde_json::json!({
            "extra_body": {"reasoning": {"effort": effort_str}}
        }),
        "doubao" => doubao_reasoning_kwargs(model, effort),
        _ => serde_json::Value::Null,
    }
}

fn effort_name(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Minimal => "minimal",
        ReasoningEffort::Low => "low",
        ReasoningEffort::Medium => "medium",
        ReasoningEffort::High => "high",
        ReasoningEffort::XHigh => "xhigh",
        ReasoningEffort::Max => "max",
    }
}

fn supports_openai_reasoning(model: &str) -> bool {
    let lower = model.to_lowercase();
    lower.starts_with("gpt-5")
        || lower.starts_with("o1")
        || lower.starts_with("o3")
        || lower.starts_with("o4")
}

fn doubao_reasoning_kwargs(model: &str, effort: ReasoningEffort) -> serde_json::Value {
    let lower = model.to_lowercase();
    if !lower.contains("thinking") && !lower.contains("seed-1.6") {
        return serde_json::Value::Null;
    }
    let thinking_type = match effort {
        ReasoningEffort::Minimal | ReasoningEffort::Low => "disabled",
        _ => "enabled",
    };
    serde_json::json!({
        "extra_body": {"thinking": {"type": thinking_type}}
    })
}

/// Build the full reasoning kwargs for a (provider, protocol, model) combo.
pub fn reasoning_kwargs(
    provider: &str,
    protocol: crate::Protocol,
    model: &str,
    effort: Option<ReasoningEffort>,
    max_tokens: u32,
) -> serde_json::Value {
    use crate::Protocol;
    match protocol {
        Protocol::Anthropic => {
            if provider == "anthropic" {
                return anthropic_reasoning_kwargs(model, effort, max_tokens);
            }
            if matches!(provider, "mimo" | "deepseek" | "mimo-token-plan") {
                return mimo_reasoning_kwargs(effort);
            }
            serde_json::Value::Null
        }
        Protocol::OpenAI => openai_reasoning_kwargs(provider, model, effort),
    }
}

fn mimo_reasoning_kwargs(effort: Option<ReasoningEffort>) -> serde_json::Value {
    match effort {
        None => serde_json::Value::Null,
        Some(e) if effort_name(e) == "none" => {
            serde_json::json!({"thinking": {"type": "disabled"}})
        }
        Some(_) => serde_json::json!({"thinking": {"type": "enabled"}}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_effort_medium() {
        assert_eq!(ReasoningEffort::parse("medium"), Some(ReasoningEffort::Medium));
    }

    #[test]
    fn test_parse_effort_off_returns_none() {
        assert_eq!(ReasoningEffort::parse("off"), None);
    }

    #[test]
    fn test_anthropic_opus_thinking() {
        let kwargs = anthropic_reasoning_kwargs(
            "claude-opus-4-8",
            Some(ReasoningEffort::High),
            8192,
        );
        assert_eq!(kwargs["thinking"]["type"], "adaptive");
        assert_eq!(kwargs["effort"], "high");
    }

    #[test]
    fn test_anthropic_non_opus_budget() {
        let kwargs = anthropic_reasoning_kwargs(
            "claude-sonnet-4-6",
            Some(ReasoningEffort::High),
            8192,
        );
        assert_eq!(kwargs["thinking"]["type"], "enabled");
        assert!(kwargs["thinking"]["budget_tokens"].as_u64().unwrap() > 0);
    }
}
