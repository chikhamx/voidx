//! Web fetch tool — retrieve and extract content from URLs.
//!
//! Ported from `src/voidx/tools/webfetch.py`.

use crate::base::{Tool, ToolContext, ToolResult};
use crate::error::ToolError;
use crate::schema::model_to_json_schema;
use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WebFetchInput {
    /// The URL to fetch content from
    pub url: String,
    /// What to extract or ask about the page content
    pub prompt: String,
}

pub struct WebFetchTool {
    client: reqwest::Client,
}

impl Default for WebFetchTool {
    fn default() -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .user_agent("voidx/1.0")
                .build()
                .unwrap(),
        }
    }
}

#[async_trait]
impl Tool for WebFetchTool {
    fn id(&self) -> &'static str {
        "webfetch"
    }

    fn description(&self) -> &'static str {
        "Fetch a URL and extract its content as text. Use prompt to specify what to extract."
    }

    fn parameters_schema(&self) -> serde_json::Value {
        model_to_json_schema::<WebFetchInput>()
    }

    async fn execute(
        &self,
        args: serde_json::Value,
        _ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        let inp: WebFetchInput = serde_json::from_value(args)
            .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;

        // Validate URL
        let url = if inp.url.starts_with("http://") || inp.url.starts_with("https://") {
            inp.url.clone()
        } else if inp.url.starts_with("//") {
            format!("https:{url}", url = inp.url)
        } else {
            return Err(ToolError::InvalidArgs(
                "URL must start with http:// or https://".to_string(),
            ));
        };

        let response = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| ToolError::Http(e))?;

        let status = response.status();
        if !status.is_success() {
            return Err(ToolError::Other(format!(
                "HTTP {status}: failed to fetch {url}"
            )));
        }

        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();

        let body = response.text().await.map_err(ToolError::Http)?;

        let text = if content_type.contains("text/html") || content_type.contains("text/plain") {
            strip_html_tags(&body)
        } else {
            body.chars().take(50_000).collect()
        };

        let truncated = if text.len() > 50_000 {
            let mut t: String = text.chars().take(50_000).collect();
            t.push_str("\n\n[Content truncated at 50,000 characters]");
            t
        } else {
            text
        };

        Ok(ToolResult::new(truncated).with_metadata(serde_json::json!({
            "url": url,
            "content_type": content_type,
            "status": status.as_u16(),
        })))
    }
}

fn strip_html_tags(html: &str) -> String {
    // Simple tag stripper — removes <script>, <style>, and all HTML tags
    let mut text = html.to_string();

    // Remove script and style blocks
    let script_re = regex::Regex::new(r"(?is)<script[^>]*>.*?</script>").unwrap();
    let style_re = regex::Regex::new(r"(?is)<style[^>]*>.*?</style>").unwrap();
    text = script_re.replace_all(&text, "").to_string();
    text = style_re.replace_all(&text, "").to_string();

    // Remove HTML tags
    let tag_re = regex::Regex::new(r"<[^>]+>").unwrap();
    text = tag_re.replace_all(&text, " ").to_string();

    // Decode common entities
    text = text.replace("&amp;", "&");
    text = text.replace("&lt;", "<");
    text = text.replace("&gt;", ">");
    text = text.replace("&quot;", "\"");
    text = text.replace("&#39;", "'");
    text = text.replace("&nbsp;", " ");

    // Collapse whitespace
    let ws_re = regex::Regex::new(r"\s+").unwrap();
    text = ws_re.replace_all(&text, " ").to_string();
    text.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_html() {
        let html = "<html><body><p>Hello</p><script>alert('x')</script><p>World</p></body></html>";
        let text = strip_html_tags(html);
        assert!(text.contains("Hello"));
        assert!(text.contains("World"));
        assert!(!text.contains("script"));
        assert!(!text.contains("alert"));
    }
}
