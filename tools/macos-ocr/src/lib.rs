mod cli;
mod output;
mod vision;

use serde::Serialize;

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum Response {
    Success(output::OcrSuccess),
    Error(output::OcrError),
}

#[derive(Debug)]
pub struct RunResult {
    pub response: Response,
    pub exit_code: u8,
    pub diagnostic: Option<String>,
}

pub fn run<I, S>(args: I) -> RunResult
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let options = match cli::parse_args(args) {
        Ok(options) => options,
        Err(error) => {
            let exit_code = if error.code == "help" {
                0
            } else if error.code == "invalid_input" {
                66
            } else {
                64
            };
            return RunResult {
                response: Response::Error(output::error_response(error.code, &error.message)),
                exit_code,
                diagnostic: Some(error.message),
            };
        }
    };

    match vision::recognize(&options) {
        Ok(blocks) if blocks.is_empty() => RunResult {
            response: Response::Error(output::error_response(
                "no_text",
                "No text was recognized in the input image.",
            )),
            exit_code: 2,
            diagnostic: Some("no text was recognized".to_owned()),
        },
        Ok(blocks) => RunResult {
            response: Response::Success(output::success_response(
                blocks,
                options.max_output_chars,
            )),
            exit_code: 0,
            diagnostic: None,
        },
        Err(error) => RunResult {
            response: Response::Error(output::error_response(error.code, &error.message)),
            exit_code: exit_code_for_vision_error(error.code),
            diagnostic: Some(error.message),
        },
    }
}

fn exit_code_for_vision_error(code: &str) -> u8 {
    match code {
        "invalid_input" => 66,
        "vision_unavailable" => 69,
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
}

#[cfg(test)]
mod output_tests {
    use super::output::truncate_text;

    #[test]
    fn truncates_text_without_splitting_unicode_scalars() {
        let result = truncate_text("前缀🙂后缀", 3);
        assert_eq!(result.text, "前缀🙂");
        assert!(result.truncated);
    }
}
