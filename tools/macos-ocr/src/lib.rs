mod barcode;
mod cli;
mod input;
mod layout;
mod output;
mod pdf;
mod preprocess;
mod vision;

use cli::OutputFormat;
use serde::Serialize;

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum Response {
    Success(output::OcrSuccess),
    BatchSuccess(output::OcrBatchSuccess),
    Error(output::OcrError),
}

#[derive(Debug)]
pub struct RunResult {
    pub response: Response,
    pub exit_code: u8,
    pub diagnostic: Option<String>,
    pub stdout: String,
}

pub fn run<I, S>(args: I) -> RunResult
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let options = match cli::parse_args(args) {
        Ok(options) => options,
        Err(error) => {
            if error.code == "help" {
                let message = error.message;
                return RunResult {
                    response: Response::Error(output::error_response("help", message.clone())),
                    exit_code: 0,
                    diagnostic: None,
                    stdout: format!("{message}\n"),
                };
            }
            return failure(error.code, error.message, exit_code_for_error(error.code));
        }
    };

    let jobs = match input::jobs_for_options(&options) {
        Ok(jobs) => jobs,
        Err(error) => return failure(error.code, error.message, exit_code_for_error(error.code)),
    };

    let mut items = Vec::new();
    let mut has_content = false;
    for (job_index, job) in jobs.iter().enumerate() {
        let regions = match recognize_job(job, &options) {
            Ok(regions) => regions,
            Err(error) => {
                let code = error.code();
                let message = error.message();
                return failure(code, message, exit_code_for_error(code));
            }
        };

        for recognized in regions {
            has_content |= !recognized.blocks.is_empty()
                || recognized
                    .barcodes
                    .as_ref()
                    .is_some_and(|barcodes| !barcodes.is_empty());
            items.push(output::success_item(
                job_index + 1,
                job.source.to_string_lossy(),
                job.page,
                recognized.region.map(Into::into),
                recognized.blocks,
                recognized.barcodes,
                options.max_output_chars,
            ));
        }
    }

    if !has_content {
        return failure(
            "no_text",
            "No text or barcode was recognized in the input.".to_owned(),
            2,
        );
    }

    let legacy_single_image = jobs.len() == 1
        && jobs[0].page.is_none()
        && options.regions.is_empty()
        && matches!(options.output_format, OutputFormat::Json);
    let response = if legacy_single_image {
        let item = items
            .first()
            .expect("a successful single-image run produces one item");
        Response::Success(output::success_response_with_barcodes(
            item.blocks.clone(),
            item.barcodes.clone(),
            options.max_output_chars,
        ))
    } else {
        Response::BatchSuccess(output::batch_response(items.clone()))
    };
    let stdout = match render_success(&response, &items, options.output_format) {
        Ok(stdout) => stdout,
        Err(message) => return failure("internal_error", message, 70),
    };

    RunResult {
        response,
        exit_code: 0,
        diagnostic: None,
        stdout,
    }
}

#[derive(Debug)]
struct RecognizedRegion {
    region: Option<cli::NormalizedRegion>,
    blocks: Vec<output::TextBlock>,
    barcodes: Option<Vec<output::Barcode>>,
}

fn recognize_job(
    job: &input::InputJob,
    options: &cli::CliOptions,
) -> Result<Vec<RecognizedRegion>, PipelineError> {
    let data = input::read_job(job).map_err(PipelineError::Input)?;
    let regions = if options.regions.is_empty() {
        vec![None]
    } else {
        options.regions.iter().copied().map(Some).collect()
    };

    regions
        .into_iter()
        .map(|region| {
            let processed = preprocess::process(
                &data,
                preprocess::PreprocessOptions {
                    region,
                    scale: options.scale,
                    grayscale: options.grayscale,
                    contrast: options.contrast,
                    sharpen: options.sharpen,
                    rotation: options.rotation,
                },
            )
            .map_err(PipelineError::Preprocess)?;
            let mut blocks = vision::recognize(&processed.png, &processed.mapper, options)
                .map_err(PipelineError::Vision)?;
            layout::sort_blocks(&mut blocks, options.layout);
            let barcodes = if options.barcodes {
                Some(
                    barcode::detect(&processed.png, &processed.mapper, options)
                        .map_err(PipelineError::Barcode)?,
                )
            } else {
                None
            };
            Ok(RecognizedRegion {
                region,
                blocks,
                barcodes,
            })
        })
        .collect()
}

#[derive(Debug)]
enum PipelineError {
    Input(input::InputError),
    Preprocess(preprocess::PreprocessError),
    Vision(vision::VisionError),
    Barcode(barcode::BarcodeError),
}

impl PipelineError {
    fn code(&self) -> &'static str {
        match self {
            Self::Input(error) => error.code,
            Self::Preprocess(error) => error.code,
            Self::Vision(error) => error.code,
            Self::Barcode(error) => error.code,
        }
    }

    fn message(self) -> String {
        match self {
            Self::Input(error) => error.message,
            Self::Preprocess(error) => error.message,
            Self::Vision(error) => error.message,
            Self::Barcode(error) => error.message,
        }
    }
}

fn render_success(
    response: &Response,
    items: &[output::OcrItem],
    format: OutputFormat,
) -> Result<String, String> {
    match format {
        OutputFormat::Json => json_line(response),
        OutputFormat::Text => {
            let text = output::plain_text(items);
            if text.is_empty() {
                Ok(String::new())
            } else {
                Ok(format!("{text}\n"))
            }
        }
        OutputFormat::Jsonl => output::jsonl_items(items)
            .map_err(|error| format!("failed to serialize JSONL OCR response: {error}")),
    }
}

fn failure(code: &'static str, message: String, exit_code: u8) -> RunResult {
    let response = Response::Error(output::error_response(code, &message));
    let stdout = json_line(&response).unwrap_or_else(|_| {
        "{\"ok\":false,\"error\":{\"code\":\"internal_error\",\"message\":\"failed to serialize OCR response\"}}\n".to_owned()
    });
    RunResult {
        response,
        exit_code,
        diagnostic: Some(message),
        stdout,
    }
}

fn json_line<T: Serialize>(value: &T) -> Result<String, String> {
    serde_json::to_string(value)
        .map(|json| format!("{json}\n"))
        .map_err(|error| format!("failed to serialize OCR response: {error}"))
}

fn exit_code_for_error(code: &str) -> u8 {
    match code {
        "invalid_arguments" => 64,
        "invalid_input" => 66,
        "vision_unavailable" | "pdf_unavailable" => 69,
        "read_failed" => 74,
        _ => 70,
    }
}

#[cfg(test)]
mod tests {
    use super::{run, Response};
    use serde_json::Value;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_input() -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("voidx-macos-ocr-{unique}.png"));
        fs::write(&path, b"not a real image").unwrap();
        path
    }

    #[test]
    fn returns_json_error_for_missing_input_argument() {
        let result = run(["--regions"]);
        assert_eq!(result.exit_code, 64);
        let Response::Error(response) = result.response else {
            panic!("expected error response");
        };
        let value: Value = serde_json::to_value(response).unwrap();
        assert_eq!(value["error"]["code"], "invalid_arguments");
    }

    #[test]
    fn returns_json_error_for_unreadable_image_path() {
        let result = run(["--input", "/path/that/does/not/exist.png"]);
        assert_eq!(result.exit_code, 66);
        let Response::Error(response) = result.response else {
            panic!("expected error response");
        };
        let value: Value = serde_json::to_value(response).unwrap();
        assert_eq!(value["error"]["code"], "invalid_input");
    }

    #[test]
    fn accepts_a_regular_file_before_vision_processing() {
        let path = temp_input();
        let result = run(["--input", path.to_str().unwrap()]);
        assert!(matches!(result.response, Response::Error(_)));
        assert_ne!(result.exit_code, 64);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn preprocesses_input_before_invoking_vision() {
        let path = temp_input();
        let result = run(["--input", path.to_str().unwrap()]);
        let Response::Error(response) = result.response else {
            panic!("expected error response");
        };
        let value: Value = serde_json::to_value(response).unwrap();
        assert_eq!(value["error"]["code"], "decode_failed");
        fs::remove_file(path).unwrap();
    }
}

#[cfg(test)]
mod output_tests {
    use super::{output, render_success, Response};
    use crate::cli::OutputFormat;

    fn item(job: usize, text: &str) -> output::OcrItem {
        output::success_item(
            job,
            format!("source-{job}.png"),
            None,
            None,
            vec![output::TextBlock {
                text: text.to_owned(),
                confidence: 0.9,
                candidates: None,
                bounding_box: None,
            }],
            None,
            100,
        )
    }

    #[test]
    fn renders_batch_text_as_one_line_per_item() {
        let items = vec![item(1, "first"), item(2, "second")];
        let response = Response::BatchSuccess(output::batch_response(items.clone()));

        assert_eq!(
            render_success(&response, &items, OutputFormat::Text).unwrap(),
            "first\nsecond\n"
        );
    }

    #[test]
    fn renders_jsonl_as_complete_item_lines() {
        let items = vec![item(1, "first"), item(2, "second")];
        let response = Response::BatchSuccess(output::batch_response(items.clone()));
        let rendered = render_success(&response, &items, OutputFormat::Jsonl).unwrap();
        let lines = rendered.lines().collect::<Vec<_>>();

        assert_eq!(lines.len(), 2);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(lines[0]).unwrap()["job"],
            1
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(lines[1]).unwrap()["text"],
            "second"
        );
    }
}

#[cfg(test)]
mod batch_contract_tests {
    use super::{output, Response};
    use serde_json::Value;

    #[test]
    fn serializes_batch_success_as_items_envelope() {
        let item = output::success_item(
            1,
            "first.png",
            None,
            None,
            vec![output::TextBlock {
                text: "first".to_owned(),
                confidence: 0.9,
                candidates: None,
                bounding_box: None,
            }],
            None,
            100,
        );
        let response = Response::BatchSuccess(output::batch_response(vec![item]));
        let value: Value = serde_json::to_value(response).unwrap();

        assert_eq!(value["ok"], true);
        assert_eq!(value["items"][0]["job"], 1);
        assert_eq!(value["items"][0]["source"], "first.png");
        assert_eq!(value["items"][0]["text"], "first");
    }
}
