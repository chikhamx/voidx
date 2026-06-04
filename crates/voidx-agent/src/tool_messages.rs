//! Sanitize tool results before replaying them to the LLM.
//!
//! Ported from `src/voidx/agent/tool_messages.py`.
//! Handles: workspace path normalization, home path normalization,
//! secret redaction, and output truncation.

use regex::Regex;
use std::path::PathBuf;

/// Default max chars for tool output before truncation.
pub const DEFAULT_TOOL_MESSAGE_MAX_CHARS: usize = 4_000;

/// Regex for key=value style secrets: api_key=xxx, token: xxx, etc.
static KEY_VALUE_SECRET_RE: &str =
    r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([^\s,;]+)";

/// Regex for Bearer token secrets: Bearer xxxxx
static BEARER_SECRET_RE: &str = r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]+)";

/// Sanitize tool message content before sending to the LLM.
///
/// Steps:
/// 1. Replace absolute workspace path with `<workspace>`
/// 2. Replace home directory with `~`
/// 3. Redact key=value secrets (api_key=xxx → api_key=[redacted])
/// 4. Redact Bearer tokens
/// 5. Truncate if longer than `max_chars`
pub fn sanitize_tool_message_content(
    content: &str,
    workspace: Option<&str>,
    max_chars: usize,
) -> String {
    let mut text = content.to_string();

    // 1. Replace workspace path
    if let Some(ws) = workspace {
        let workspace_path = PathBuf::from(ws)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(ws));
        let ws_str = workspace_path.to_string_lossy();
        if !ws_str.is_empty() {
            text = text.replace(&*ws_str, "<workspace>");
        }
    }

    // 2. Replace home directory
    if let Some(home) = dirs::home_dir() {
        let home_str = home.to_string_lossy();
        if !home_str.is_empty() {
            text = text.replace(&*home_str, "~");
        }
    }

    // 3. Redact key=value secrets
    if let Ok(re) = Regex::new(KEY_VALUE_SECRET_RE) {
        text = re.replace_all(&text, "${1}${2}[redacted]").to_string();
    }

    // 4. Redact Bearer tokens
    if let Ok(re) = Regex::new(BEARER_SECRET_RE) {
        text = re.replace_all(&text, "${1}[redacted]").to_string();
    }

    // 5. Truncate if needed
    if max_chars > 0 && text.len() > max_chars {
        let omitted = text.len() - max_chars;
        text.truncate(max_chars);
        text.push_str(&format!("\n\n[Tool output truncated: omitted {omitted} chars]"));
    }

    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sanitize_basic() {
        let result = sanitize_tool_message_content("hello world", None, 0);
        assert_eq!(result, "hello world");
    }

    #[test]
    fn test_sanitize_secret_redaction() {
        let result = sanitize_tool_message_content(
            "api_key=sk-12345 token: abc123",
            None,
            0,
        );
        assert!(result.contains("api_key=[redacted]"));
        assert!(result.contains("token: [redacted]"));
        assert!(!result.contains("sk-12345"));
        assert!(!result.contains("abc123"));
    }

    #[test]
    fn test_sanitize_bearer_redaction() {
        let result = sanitize_tool_message_content(
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
            None,
            0,
        );
        assert!(result.contains("Bearer [redacted]"));
        assert!(!result.contains("eyJhbGciOiJIUzI1NiJ9"));
    }

    #[test]
    fn test_sanitize_workspace_path() {
        let result = sanitize_tool_message_content(
            "File written to /home/user/project/src/main.rs",
            Some("/home/user/project"),
            0,
        );
        assert!(result.contains("<workspace>"));
        assert!(!result.contains("/home/user/project"));
    }

    #[test]
    fn test_sanitize_truncation() {
        let long = "x".repeat(5000);
        let result = sanitize_tool_message_content(&long, None, 100);
        assert!(result.len() > 100);
        assert!(result.contains("[Tool output truncated: omitted 4900 chars]"));
        assert!(result.starts_with(&"x".repeat(100)));
    }

    #[test]
    fn test_sanitize_password_redaction() {
        let result = sanitize_tool_message_content(
            "password=supersecret123",
            None,
            0,
        );
        assert!(result.contains("password=[redacted]"));
        assert!(!result.contains("supersecret123"));
    }
}
