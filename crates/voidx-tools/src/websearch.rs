//! Web search tool — search the web using Tavily API or free fallback.
//!
//! Ported from `src/voidx/tools/websearch.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use crate::schema::model_to_json_schema;
use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WebSearchInput {
    /// The search query string
    pub search_query: String,
    /// Domains to limit the search to
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allowed_domains: Option<Vec<String>>,
    /// Domains to exclude from results
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blocked_domains: Option<Vec<String>>,
}

pub struct WebSearchTool {
    client: reqwest::Client,
}

impl Default for WebSearchTool {
    fn default() -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(20))
                .user_agent("voidx/1.0")
                .build()
                .unwrap(),
        }
    }
}

#[async_trait]
impl Tool for WebSearchTool {
    fn id(&self) -> &'static str {
        "websearch"
    }

    fn description(&self) -> &'static str {
        "Search the web using DuckDuckGo (free, no API key). Returns titles and URLs."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<WebSearchInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: WebSearchInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        let results = search_duckduckgo(&self.client, &inp.search_query).await?;

        let filtered: Vec<&SearchResult> = results
            .iter()
            .filter(|r| {
                if let Some(ref allowed) = inp.allowed_domains {
                    return allowed.iter().any(|d| r.url.contains(d));
                }
                if let Some(ref blocked) = inp.blocked_domains {
                    return !blocked.iter().any(|d| r.url.contains(d));
                }
                true
            })
            .take(10)
            .collect();

        if filtered.is_empty() {
            return Ok(ToolResult::new(format!(
                "No results found for: {}",
                inp.search_query
            )));
        }

        let output = filtered
            .iter()
            .map(|r| format!("- **{}**\n  {}", r.title, r.url))
            .collect::<Vec<_>>()
            .join("\n\n");

        Ok(ToolResult::new(output).with_metadata(serde_json::json!({
            "query": inp.search_query,
            "results": filtered.len(),
        })))
    }
}

// ── DuckDuckGo HTML search (zero API key) ──────────────────────────────────

#[derive(Debug, Clone)]
struct SearchResult {
    title: String,
    url: String,
}

async fn search_duckduckgo(
    client: &reqwest::Client,
    query: &str,
) -> Result<Vec<SearchResult>, ToolError> {
    let url = format!(
        "https://html.duckduckgo.com/html/?q={}",
        urlencoding(query)
    );

    let response = client
        .get(&url)
        .header("Accept", "text/html")
        .send()
        .await?;

    if !response.status().is_success() {
        return Err(ToolError::Other(format!(
            "Search failed with status {}",
            response.status()
        )));
    }

    let html = response.text().await?;
    parse_duckduckgo_html(&html)
}

fn urlencoding(s: &str) -> String {
    let mut result = String::new();
    for byte in s.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                result.push(byte as char);
            }
            b' ' => result.push('+'),
            _ => {
                result.push_str(&format!("%{:02X}", byte));
            }
        }
    }
    result
}

fn parse_duckduckgo_html(html: &str) -> Result<Vec<SearchResult>, ToolError> {
    let mut results = Vec::new();

    // Parse DuckDuckGo's HTML result format: find result blocks
    let result_re = regex::Regex::new(
        r#"<div[^>]*class="[^"]*result[^"]*"[^>]*>.*?<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>"#
    ).unwrap();

    for cap in result_re.captures_iter(html).take(10) {
        let url = clean_duckduckgo_url(&cap[1]);
        let title = strip_html_simple(&cap[2]);
        if !url.is_empty() && !title.is_empty() {
            results.push(SearchResult { title, url });
        }
    }

    // Fallback: try generic link extraction
    if results.is_empty() {
        let fallback_re =
            regex::Regex::new(r#"<a[^>]*href="(https?://[^"]*)"[^>]*>([^<]*)</a>"#).unwrap();
        for cap in fallback_re.captures_iter(html).take(10) {
            let url = &cap[1];
            let title = strip_html_simple(&cap[2]);
            if !url.contains("duckduckgo.com") && !title.is_empty() {
                results.push(SearchResult {
                    title,
                    url: url.to_string(),
                });
            }
        }
    }

    Ok(results)
}

fn clean_duckduckgo_url(raw: &str) -> String {
    // DuckDuckGo wraps URLs through their redirect — extract the real URL
    if let Some(pos) = raw.find("uddg=") {
        let encoded = &raw[pos + 5..];
        let decoded = encoded.split('&').next().unwrap_or(encoded);
        return percent_decode(decoded).unwrap_or_else(|| decoded.to_string());
    }
    raw.to_string()
}

fn percent_decode(s: &str) -> Option<String> {
    let mut result = String::new();
    let mut chars = s.bytes();
    while let Some(b) = chars.next() {
        match b {
            b'%' => {
                let hi = chars.next()?;
                let lo = chars.next()?;
                let byte = u8::from_str_radix(
                    &format!("{}{}", hi as char, lo as char),
                    16,
                )
                .ok()?;
                result.push(byte as char);
            }
            b'+' => result.push(' '),
            _ => result.push(b as char),
        }
    }
    Some(result)
}

fn strip_html_simple(s: &str) -> String {
    let tag_re = regex::Regex::new(r"<[^>]+>").unwrap();
    let result = tag_re.replace_all(s, " ").to_string();
    let ws_re = regex::Regex::new(r"\s+").unwrap();
    ws_re.replace_all(&result, " ").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_urlencoding() {
        let encoded = urlencoding("hello world");
        assert_eq!(encoded, "hello+world");
    }

    #[test]
    fn test_clean_duckduckgo_url_no_redirect() {
        let url = "https://example.com/page";
        assert_eq!(clean_duckduckgo_url(url), url);
    }
}
