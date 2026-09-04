use serde::Serialize;

use crate::cli::NormalizedRegion;

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
pub struct TextCandidate {
    pub text: String,
    pub confidence: f32,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Barcode {
    pub payload: String,
    pub symbology: String,
    pub confidence: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bounding_box: Option<BoundingBox>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TextBlock {
    pub text: String,
    pub confidence: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidates: Option<Vec<TextCandidate>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bounding_box: Option<BoundingBox>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Region {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl From<NormalizedRegion> for Region {
    fn from(region: NormalizedRegion) -> Self {
        Self {
            x: region.x,
            y: region.y,
            width: region.width,
            height: region.height,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OcrSuccess {
    pub ok: bool,
    pub text: String,
    pub blocks: Vec<TextBlock>,
    pub confidence: Option<f32>,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub barcodes: Option<Vec<Barcode>>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OcrItem {
    pub job: usize,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub region: Option<Region>,
    pub text: String,
    pub blocks: Vec<TextBlock>,
    pub confidence: Option<f32>,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub barcodes: Option<Vec<Barcode>>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OcrBatchSuccess {
    pub ok: bool,
    pub items: Vec<OcrItem>,
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

pub fn success_response_with_barcodes(
    blocks: Vec<TextBlock>,
    barcodes: Option<Vec<Barcode>>,
    max_output_chars: usize,
) -> OcrSuccess {
    let text = text_with_barcodes(&blocks, barcodes.as_deref().unwrap_or_default());
    let truncated_text = truncate_text(&text, max_output_chars);
    let confidence = confidence_for_blocks(&blocks);

    OcrSuccess {
        ok: true,
        text: truncated_text.text,
        blocks,
        confidence,
        truncated: truncated_text.truncated,
        barcodes,
    }
}

pub fn success_item(
    job: usize,
    source: impl Into<String>,
    page: Option<u32>,
    region: Option<Region>,
    blocks: Vec<TextBlock>,
    barcodes: Option<Vec<Barcode>>,
    max_output_chars: usize,
) -> OcrItem {
    let text = text_with_barcodes(&blocks, barcodes.as_deref().unwrap_or_default());
    let truncated_text = truncate_text(&text, max_output_chars);

    OcrItem {
        job,
        source: source.into(),
        page,
        region,
        text: truncated_text.text,
        blocks: blocks.clone(),
        confidence: confidence_for_blocks(&blocks),
        truncated: truncated_text.truncated,
        barcodes,
    }
}

pub fn batch_response(items: Vec<OcrItem>) -> OcrBatchSuccess {
    OcrBatchSuccess { ok: true, items }
}

pub fn plain_text(items: &[OcrItem]) -> String {
    items
        .iter()
        .map(|item| item.text.as_str())
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn jsonl_items(items: &[OcrItem]) -> Result<String, serde_json::Error> {
    let mut output = String::new();
    for item in items {
        output.push_str(&serde_json::to_string(item)?);
        output.push('\n');
    }
    Ok(output)
}

fn text_with_barcodes(blocks: &[TextBlock], barcodes: &[Barcode]) -> String {
    let mut parts = blocks
        .iter()
        .map(|block| block.text.as_str())
        .collect::<Vec<_>>();
    parts.extend(barcodes.iter().map(|barcode| barcode.payload.as_str()));
    parts.join("\n")
}

fn confidence_for_blocks(blocks: &[TextBlock]) -> Option<f32> {
    if blocks.is_empty() {
        None
    } else {
        Some(blocks.iter().map(|block| block.confidence).sum::<f32>() / blocks.len() as f32)
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
    use super::{
        batch_response, error_response, jsonl_items, plain_text, success_item,
        success_response_with_barcodes, Barcode, BoundingBox, Region, TextBlock,
    };

    fn block(text: &str) -> TextBlock {
        TextBlock {
            text: text.to_owned(),
            confidence: 0.9,
            candidates: None,
            bounding_box: None,
        }
    }

    fn barcode(payload: &str) -> Barcode {
        Barcode {
            payload: payload.to_owned(),
            symbology: "QR".to_owned(),
            confidence: 0.95,
            bounding_box: Some(BoundingBox {
                x: 0.1,
                y: 0.2,
                width: 0.3,
                height: 0.4,
            }),
        }
    }

    #[test]
    fn serializes_success_envelope() {
        let response = success_response_with_barcodes(
            vec![TextBlock {
                text: "hello".to_owned(),
                confidence: 0.9,
                candidates: None,
                bounding_box: Some(BoundingBox {
                    x: 0.1,
                    y: 0.2,
                    width: 0.3,
                    height: 0.4,
                }),
            }],
            None,
            100,
        );
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["ok"], true);
        assert_eq!(json["text"], "hello");
        assert_eq!(json["blocks"][0]["bounding_box"]["x"], 0.1);
        assert!(json.get("barcodes").is_none());
    }

    #[test]
    fn serializes_stable_error_envelope() {
        let response = error_response("no_text", "No text was recognized.");
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["ok"], false);
        assert_eq!(json["error"]["code"], "no_text");
    }

    #[test]
    fn serializes_requested_text_candidates() {
        let response = success_response_with_barcodes(
            vec![TextBlock {
                text: "primary".to_owned(),
                confidence: 0.9,
                candidates: Some(vec![
                    super::TextCandidate {
                        text: "primary".to_owned(),
                        confidence: 0.9,
                    },
                    super::TextCandidate {
                        text: "alternate".to_owned(),
                        confidence: 0.7,
                    },
                ]),
                bounding_box: None,
            }],
            None,
            100,
        );
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["blocks"][0]["candidates"][1]["text"], "alternate");
        let confidence = json["blocks"][0]["candidates"][1]["confidence"]
            .as_f64()
            .unwrap();
        assert!((confidence - 0.7).abs() < 1e-6);
    }

    #[test]
    fn includes_barcode_payloads_in_single_image_json() {
        let response = success_response_with_barcodes(
            vec![block("recognized")],
            Some(vec![barcode("payload")]),
            100,
        );
        let json = serde_json::to_value(response).unwrap();
        assert_eq!(json["text"], "recognized\npayload");
        assert_eq!(json["barcodes"][0]["payload"], "payload");
    }

    #[test]
    fn truncates_text_without_splitting_unicode_scalars() {
        let response = success_response_with_barcodes(vec![block("前缀🙂后缀")], None, 3);
        assert_eq!(response.text, "前缀🙂");
        assert!(response.truncated);
    }

    #[test]
    fn batch_items_keep_job_page_and_region_metadata() {
        let item = success_item(
            2,
            "scan.pdf",
            Some(3),
            Some(Region {
                x: 0.1,
                y: 0.2,
                width: 0.5,
                height: 0.6,
            }),
            vec![block("page three")],
            Some(vec![barcode("payload")]),
            100,
        );

        let json = serde_json::to_value(batch_response(vec![item])).unwrap();
        assert_eq!(json["ok"], true);
        assert_eq!(json["items"][0]["job"], 2);
        assert_eq!(json["items"][0]["source"], "scan.pdf");
        assert_eq!(json["items"][0]["page"], 3);
        assert_eq!(json["items"][0]["region"]["x"], 0.1);
        assert_eq!(json["items"][0]["barcodes"][0]["payload"], "payload");
    }

    #[test]
    fn plain_text_includes_text_and_barcode_payloads() {
        let item = success_item(
            1,
            "scan.png",
            None,
            None,
            vec![block("recognized")],
            Some(vec![barcode("payload")]),
            100,
        );

        assert_eq!(plain_text(&[item]), "recognized\npayload");
    }

    #[test]
    fn jsonl_emits_one_complete_item_per_line() {
        let items = vec![
            success_item(1, "first.png", None, None, vec![block("first")], None, 100),
            success_item(
                2,
                "second.png",
                None,
                None,
                vec![block("second")],
                None,
                100,
            ),
        ];

        let rendered = jsonl_items(&items).unwrap();
        let lines = rendered.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), 2);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(lines[0]).unwrap()["text"],
            "first"
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(lines[1]).unwrap()["job"],
            2
        );
    }
}
