use std::path::PathBuf;

use crate::output::DEFAULT_MAX_OUTPUT_CHARS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecognitionLevel {
    Fast,
    Accurate,
}


#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliOptions {
    pub input: PathBuf,
    pub languages: Vec<String>,
    pub recognition_level: RecognitionLevel,
    pub regions: bool,
    pub max_output_chars: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliError {
    pub code: &'static str,
    pub message: String,
    pub usage: bool,
}

impl CliError {
    fn usage(message: impl Into<String>) -> Self {
        Self {
            code: "invalid_arguments",
            message: message.into(),
            usage: true,
        }
    }

    fn input(message: impl Into<String>) -> Self {
        Self {
            code: "invalid_input",
            message: message.into(),
            usage: false,
        }
    }
}

pub fn parse_args<I, S>(args: I) -> Result<CliOptions, CliError>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let mut args = args.into_iter().map(Into::into).peekable();
    let mut input = None;
    let mut languages = Vec::new();
    let mut recognition_level = RecognitionLevel::Accurate;
    let mut regions = false;
    let mut max_output_chars = DEFAULT_MAX_OUTPUT_CHARS;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--input" => {
                let value = args.next().ok_or_else(|| {
                    CliError::usage("--input requires a non-empty image path")
                })?;
                if value.is_empty() || value.starts_with('-') {
                    return Err(CliError::usage("--input requires a non-empty image path"));
                }
                input = Some(PathBuf::from(value));
            }
            "--language" => {
                let value = args
                    .next()
                    .ok_or_else(|| CliError::usage("--language requires an ISO language identifier"))?;
                if value.is_empty() || value.starts_with('-') {
                    return Err(CliError::usage(
                        "--language requires an ISO language identifier",
                    ));
                }
                languages.push(value);
            }
            "--recognition-level" => {
                let value = args
                    .next()
                    .ok_or_else(|| CliError::usage("--recognition-level requires fast or accurate"))?;
                recognition_level = match value.as_str() {
                    "fast" => RecognitionLevel::Fast,
                    "accurate" => RecognitionLevel::Accurate,
                    _ => {
                        return Err(CliError::usage(
                            "--recognition-level must be fast or accurate",
                        ));
                    }
                };
            }
            "--regions" => regions = true,
            "--max-output-chars" => {
                let value = args
                    .next()
                    .ok_or_else(|| CliError::usage("--max-output-chars requires a positive integer"))?;
                max_output_chars = value.parse::<usize>().map_err(|_| {
                    CliError::usage("--max-output-chars requires a positive integer")
                })?;
                if max_output_chars == 0 {
                    return Err(CliError::usage("--max-output-chars must be greater than zero"));
                }
            }
            "--help" | "-h" => {
                return Err(CliError {
                    code: "help",
                    message: usage().to_owned(),
                    usage: true,
                });
            }
            value if value.starts_with('-') => {
                return Err(CliError::usage(format!("unknown option: {value}")));
            }
            value => {
                return Err(CliError::usage(format!(
                    "unexpected positional argument: {value}"
                )));
            }
        }
    }

    let input = input.ok_or_else(|| CliError::usage("--input is required"))?;
    if !input.is_file() {
        return Err(CliError::input(format!(
            "input image does not exist or is not a regular file: {}",
            input.display()
        )));
    }

    Ok(CliOptions {
        input,
        languages,
        recognition_level,
        regions,
        max_output_chars,
    })
}

pub fn usage() -> &'static str {
    "Usage: voidx-macos-ocr --input <path> [--language <id>]... [--recognition-level fast|accurate] [--regions] [--max-output-chars <n>]"
}

#[cfg(test)]
mod tests {
    use super::{parse_args, RecognitionLevel};
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
    fn parses_repeated_languages_and_options() {
        let path = temp_input();
        let options = parse_args([
            "--input",
            path.to_str().unwrap(),
            "--language",
            "en-US",
            "--language",
            "zh-Hans",
            "--recognition-level",
            "fast",
            "--regions",
            "--max-output-chars",
            "42",
        ])
        .unwrap();
        assert_eq!(options.languages, ["en-US", "zh-Hans"]);
        assert_eq!(options.recognition_level, RecognitionLevel::Fast);
        assert!(options.regions);
        assert_eq!(options.max_output_chars, 42);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn rejects_missing_input() {
        let error = parse_args(["--recognition-level", "fast"]).unwrap_err();
        assert_eq!(error.code, "invalid_arguments");
    }

    #[test]
    fn rejects_unknown_recognition_level() {
        let path = temp_input();
        let error = parse_args([
            "--input",
            path.to_str().unwrap(),
            "--recognition-level",
            "balanced",
        ])
        .unwrap_err();
        assert_eq!(error.code, "invalid_arguments");
        fs::remove_file(path).unwrap();
    }
}
