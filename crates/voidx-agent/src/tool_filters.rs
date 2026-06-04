//! Tool definition filters shared by primary and worker-role loops.
//!
//! Ported from `src/voidx/agent/tool_filters.py`.
//!
//! Filters out LSP tools when no LSP server is available, preventing
//! the LLM from attempting calls that will always fail.

/// Filter out LSP tools when no LSP server is available.
///
/// `tool_defs` is a list of tool definitions (JSON objects with
/// `function.name` fields). `lsp_available` indicates whether at least
/// one LSP server is connected and healthy.
pub fn filter_unavailable_lsp_tools(
    tool_defs: Vec<serde_json::Value>,
    lsp_available: bool,
) -> Vec<serde_json::Value> {
    if lsp_available {
        return tool_defs;
    }

    tool_defs
        .into_iter()
        .filter(|tool| {
            let name = tool
                .get("function")
                .and_then(|f| f.get("name"))
                .and_then(|n| n.as_str())
                .unwrap_or("");
            !name.starts_with("lsp_")
        })
        .collect()
}

/// Check if any LSP server is available.
///
/// This is a simplified check — the full version would query the LspManager.
/// For now, we accept a boolean flag from the caller.
pub fn has_available_lsp_server(lsp_available: bool) -> bool {
    lsp_available
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_filter_removes_lsp_tools_when_unavailable() {
        let tools = vec![
            json!({"function": {"name": "read"}}),
            json!({"function": {"name": "lsp_diagnostics"}}),
            json!({"function": {"name": "write"}}),
            json!({"function": {"name": "lsp_symbols"}}),
        ];

        let filtered = filter_unavailable_lsp_tools(tools, false);
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0]["function"]["name"], "read");
        assert_eq!(filtered[1]["function"]["name"], "write");
    }

    #[test]
    fn test_filter_keeps_all_when_lsp_available() {
        let tools = vec![
            json!({"function": {"name": "read"}}),
            json!({"function": {"name": "lsp_diagnostics"}}),
        ];

        let filtered = filter_unavailable_lsp_tools(tools, true);
        assert_eq!(filtered.len(), 2);
    }

    #[test]
    fn test_filter_empty_list() {
        let filtered = filter_unavailable_lsp_tools(vec![], false);
        assert!(filtered.is_empty());
    }
}
