use serde::Serialize;

pub const DEFAULT_MAX_OUTPUT_CHARS: usize = 100_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TruncatedText {
    pub text: String,
    pub truncated: bool,
}

pub fn truncate_text(text: &str, max_chars: usize) -> TruncatedText {
    if text.chars().count() <= max_chars {
        return TruncatedText {
            text: text.to_owned(),
            truncated: false,
        };
    }

    let text = text.chars().take(max_chars).collect();
    TruncatedText {
        text,
        truncated: true,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BoundingBox {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TextBlock {
    pub text: String,
    pub confidence: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bounding_box: Option<BoundingBox>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OcrSuccess {
    pub ok: bool,
    pub text: String,
    pub blocks: Vec<TextBlock>,
    pub confidence: Option<f32>,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OcrError {
    pub ok: bool,
    pub error: ErrorInfo,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ErrorInfo {
    pub code: String,
    pub message: String,
}

pub fn success_response(blocks: Vec<TextBlock>, max_output_chars: usize) -> OcrSuccess {
    let text = blocks
        .iter()
        .map(|block| block.text.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    let truncated_text = truncate_text(&text, max_output_chars);
    let confidence = if blocks.is_empty() {
        None
    } else {
        Some(blocks.iter().map(|block| block.confidence).sum::<f32>() / blocks.len() as f32)
    };

    OcrSuccess {
        ok: true,
        text: truncated_text.text,
        blocks,
        confidence,
        truncated: truncated_text.truncated,
    }
}

pub fn error_response(code: impl Into<String>, message: impl Into<String>) -> OcrError {
    OcrError {
        ok: false,
        error: ErrorInfo {
            code: code.into(),
            message: message.into(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{error_response, success_response, BoundingBox, TextBlock};

    #[test]
    fn serializes_success_envelope() {
        let response = success_response(
            vec![TextBlock {
                text: "hello".to_owned(),
                confidence: 0.9,
                bounding_box: Some(BoundingBox {
                    x: 0.1,
                    y: 0.2,
                    width: 0.3,
                    height: 0.4,
                }),
            }],
            100,
        );
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["ok"], true);
        assert_eq!(json["text"], "hello");
        assert_eq!(json["blocks"][0]["bounding_box"]["x"], 0.1);
    }

    #[test]
    fn serializes_stable_error_envelope() {
        let response = error_response("no_text", "No text was recognized.");
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["ok"], false);
        assert_eq!(json["error"]["code"], "no_text");
    }
}
