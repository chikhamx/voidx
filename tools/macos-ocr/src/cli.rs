use std::path::PathBuf;

use crate::output::DEFAULT_MAX_OUTPUT_CHARS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecognitionLevel {
    Fast,
    Accurate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreprocessMode {
    None,
    Auto,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rotation {
    Auto,
    Degrees(u16),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LayoutMode {
    None,
    Lines,
    Paragraphs,
    Columns,
    ReadingOrder,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputFormat {
    Json,
    Text,
    Jsonl,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct NormalizedRegion {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PageSelection {
    pub start: u32,
    pub end: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CliOptions {
    pub inputs: Vec<PathBuf>,
    pub input_dir: Option<PathBuf>,
    pub pages: Vec<PageSelection>,
    pub regions: Vec<NormalizedRegion>,
    pub preprocess: PreprocessMode,
    pub scale: u32,
    pub grayscale: bool,
    pub contrast: Option<f32>,
    pub sharpen: bool,
    pub rotation: Rotation,
    pub languages: Vec<String>,
    pub recognition_level: RecognitionLevel,
    pub candidates: usize,
    pub barcodes: bool,
    pub barcode_symbologies: Vec<String>,
    pub layout: LayoutMode,
    pub output_format: OutputFormat,
    pub include_regions: bool,
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
    let mut inputs = Vec::new();
    let mut input_dir = None;
    let mut pages = Vec::new();
    let mut regions = Vec::new();
    let mut preprocess = PreprocessMode::None;
    let mut scale = 1;
    let mut grayscale = false;
    let mut contrast = None;
    let mut sharpen = false;
    let mut rotation = Rotation::Auto;
    let mut languages = Vec::new();
    let mut recognition_level = RecognitionLevel::Accurate;
    let mut candidates = 1;
    let mut barcodes = false;
    let mut barcode_symbologies = Vec::new();
    let mut layout = LayoutMode::None;
    let mut output_format = OutputFormat::Json;
    let mut include_regions = false;
    let mut max_output_chars = DEFAULT_MAX_OUTPUT_CHARS;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--input" => inputs.push(PathBuf::from(next_value(
                &mut args,
                "--input",
                "a non-empty input path",
            )?)),
            "--input-dir" => {
                let value =
                    PathBuf::from(next_value(&mut args, "--input-dir", "a directory path")?);
                if input_dir.replace(value).is_some() {
                    return Err(CliError::usage("--input-dir may be provided only once"));
                }
            }
            "--page" => pages.push(parse_page_selection(&next_value(
                &mut args,
                "--page",
                "a page number or range",
            )?)?),
            "--region" => regions.push(parse_region(&next_value(
                &mut args,
                "--region",
                "x,y,width,height with values from 0 to 1",
            )?)?),
            "--preprocess" => {
                preprocess = match next_value(&mut args, "--preprocess", "auto")?.as_str() {
                    "auto" => PreprocessMode::Auto,
                    value => {
                        return Err(CliError::usage(format!(
                            "--preprocess must be auto, got {value}"
                        )))
                    }
                };
            }
            "--scale" => {
                scale = parse_positive_u32(
                    &next_value(&mut args, "--scale", "a positive integer")?,
                    "--scale",
                )?;
                if scale > 8 {
                    return Err(CliError::usage("--scale must be between 1 and 8"));
                }
            }
            "--grayscale" => grayscale = true,
            "--contrast" => {
                contrast = Some(parse_contrast(&next_numeric_value(
                    &mut args,
                    "--contrast",
                    "a value from -2 to 2",
                )?)?);
            }
            "--sharpen" => sharpen = true,
            "--rotate" => {
                rotation =
                    parse_rotation(&next_value(&mut args, "--rotate", "auto or 0,90,180,270")?)?;
            }
            "--language" => languages.push(next_value(
                &mut args,
                "--language",
                "an ISO language identifier",
            )?),
            "--recognition-level" => {
                recognition_level = match next_value(
                    &mut args,
                    "--recognition-level",
                    "fast or accurate",
                )?
                .as_str()
                {
                    "fast" => RecognitionLevel::Fast,
                    "accurate" => RecognitionLevel::Accurate,
                    _ => {
                        return Err(CliError::usage(
                            "--recognition-level must be fast or accurate",
                        ));
                    }
                };
            }
            "--candidates" => {
                candidates = parse_positive_u32(
                    &next_value(&mut args, "--candidates", "a positive integer")?,
                    "--candidates",
                )? as usize;
                if candidates > 10 {
                    return Err(CliError::usage("--candidates must be between 1 and 10"));
                }
            }
            "--barcodes" => barcodes = true,
            "--barcode-symbology" => barcode_symbologies.push(next_value(
                &mut args,
                "--barcode-symbology",
                "a barcode symbology",
            )?),
            "--layout" => {
                layout = parse_layout(&next_value(
                    &mut args,
                    "--layout",
                    "lines, paragraphs, columns, or reading-order",
                )?)?;
            }
            "--format" => {
                output_format =
                    parse_format(&next_value(&mut args, "--format", "json, text, or jsonl")?)?;
            }
            "--regions" => include_regions = true,
            "--max-output-chars" => {
                max_output_chars = parse_positive_u32(
                    &next_value(&mut args, "--max-output-chars", "a positive integer")?,
                    "--max-output-chars",
                )? as usize;
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

    if inputs.is_empty() == input_dir.is_none() {
        return Err(CliError::usage(
            "provide one or more --input paths or exactly one --input-dir",
        ));
    }
    if let Some(path) = inputs.iter().find(|path| !path.is_file()) {
        return Err(CliError::input(format!(
            "input does not exist or is not a regular file: {}",
            path.display()
        )));
    }
    if let Some(path) = &input_dir {
        if !path.is_dir() {
            return Err(CliError::input(format!(
                "input directory does not exist or is not a directory: {}",
                path.display()
            )));
        }
    }
    if !pages.is_empty() {
        if input_dir.is_some() || inputs.iter().any(|path| !is_pdf(path)) {
            return Err(CliError::usage(
                "--page can only be used with PDF files passed through --input",
            ));
        }
    }
    if !barcode_symbologies.is_empty() && !barcodes {
        return Err(CliError::usage("--barcode-symbology requires --barcodes"));
    }

    Ok(CliOptions {
        inputs,
        input_dir,
        pages,
        regions,
        preprocess,
        scale,
        grayscale,
        contrast,
        sharpen,
        rotation,
        languages,
        recognition_level,
        candidates,
        barcodes,
        barcode_symbologies,
        layout,
        output_format,
        include_regions,
        max_output_chars,
    })
}

fn next_value<I>(
    args: &mut std::iter::Peekable<I>,
    option: &str,
    description: &str,
) -> Result<String, CliError>
where
    I: Iterator<Item = String>,
{
    let value = args
        .next()
        .ok_or_else(|| CliError::usage(format!("{option} requires {description}")))?;
    if value.is_empty() || value.starts_with('-') {
        return Err(CliError::usage(format!("{option} requires {description}")));
    }
    Ok(value)
}

fn next_numeric_value<I>(
    args: &mut std::iter::Peekable<I>,
    option: &str,
    description: &str,
) -> Result<String, CliError>
where
    I: Iterator<Item = String>,
{
    let value = args
        .next()
        .ok_or_else(|| CliError::usage(format!("{option} requires {description}")))?;
    if value.is_empty() || (value.starts_with('-') && value.parse::<f32>().is_err()) {
        return Err(CliError::usage(format!("{option} requires {description}")));
    }
    Ok(value)
}

fn parse_positive_u32(value: &str, option: &str) -> Result<u32, CliError> {
    let parsed = value
        .parse::<u32>()
        .map_err(|_| CliError::usage(format!("{option} requires a positive integer")))?;
    if parsed == 0 {
        return Err(CliError::usage(format!(
            "{option} must be greater than zero"
        )));
    }
    Ok(parsed)
}

fn parse_contrast(value: &str) -> Result<f32, CliError> {
    let parsed = value
        .parse::<f32>()
        .map_err(|_| CliError::usage("--contrast requires a value from -2 to 2"))?;
    if !parsed.is_finite() || !(-2.0..=2.0).contains(&parsed) {
        return Err(CliError::usage("--contrast must be between -2 and 2"));
    }
    Ok(parsed)
}

fn parse_rotation(value: &str) -> Result<Rotation, CliError> {
    match value {
        "auto" => Ok(Rotation::Auto),
        "0" => Ok(Rotation::Degrees(0)),
        "90" => Ok(Rotation::Degrees(90)),
        "180" => Ok(Rotation::Degrees(180)),
        "270" => Ok(Rotation::Degrees(270)),
        _ => Err(CliError::usage(
            "--rotate must be auto or one of 0, 90, 180, 270",
        )),
    }
}

fn parse_layout(value: &str) -> Result<LayoutMode, CliError> {
    match value {
        "lines" => Ok(LayoutMode::Lines),
        "paragraphs" => Ok(LayoutMode::Paragraphs),
        "columns" => Ok(LayoutMode::Columns),
        "reading-order" => Ok(LayoutMode::ReadingOrder),
        _ => Err(CliError::usage(
            "--layout must be lines, paragraphs, columns, or reading-order",
        )),
    }
}

fn parse_format(value: &str) -> Result<OutputFormat, CliError> {
    match value {
        "json" => Ok(OutputFormat::Json),
        "text" => Ok(OutputFormat::Text),
        "jsonl" => Ok(OutputFormat::Jsonl),
        _ => Err(CliError::usage("--format must be json, text, or jsonl")),
    }
}

fn parse_page_selection(value: &str) -> Result<PageSelection, CliError> {
    let mut parts = value.split('-');
    let start = parts
        .next()
        .ok_or_else(|| CliError::usage("--page requires a page number or range"))?
        .parse::<u32>()
        .map_err(|_| CliError::usage("--page requires a page number or range"))?;
    let end = match parts.next() {
        Some(value) => value
            .parse::<u32>()
            .map_err(|_| CliError::usage("--page requires a page number or range"))?,
        None => start,
    };
    if parts.next().is_some() || start == 0 || end < start {
        return Err(CliError::usage(
            "--page requires a valid 1-based page range",
        ));
    }
    Ok(PageSelection { start, end })
}

fn parse_region(value: &str) -> Result<NormalizedRegion, CliError> {
    let values = value
        .split(',')
        .map(|part| part.parse::<f64>())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| CliError::usage("--region requires x,y,width,height from 0 to 1"))?;
    if values.len() != 4
        || values
            .iter()
            .any(|value| !value.is_finite() || !(0.0..=1.0).contains(value))
        || values[2] == 0.0
        || values[3] == 0.0
        || values[0] + values[2] > 1.0
        || values[1] + values[3] > 1.0
    {
        return Err(CliError::usage(
            "--region requires a non-empty rectangle within 0..1",
        ));
    }
    Ok(NormalizedRegion {
        x: values[0],
        y: values[1],
        width: values[2],
        height: values[3],
    })
}

fn is_pdf(path: &PathBuf) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| extension.eq_ignore_ascii_case("pdf"))
        .unwrap_or(false)
}

pub fn usage() -> &'static str {
    "Usage: voidx-macos-ocr (--input <path>... | --input-dir <directory>) [--page <n|start-end>]... [--region <x,y,width,height>]... [--preprocess auto] [--scale <n>] [--grayscale] [--contrast <-2..2>] [--sharpen] [--rotate auto|0|90|180|270] [--language <id>]... [--recognition-level fast|accurate] [--candidates <n>] [--barcodes] [--barcode-symbology <name>]... [--layout lines|paragraphs|columns|reading-order] [--format json|text|jsonl] [--regions] [--max-output-chars <n>]"
}

#[cfg(test)]
mod tests {
    use super::{parse_args, LayoutMode, OutputFormat, PreprocessMode, RecognitionLevel, Rotation};
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_input(extension: &str) -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("voidx-macos-ocr-{unique}.{extension}"));
        fs::write(&path, b"not a real image").unwrap();
        path
    }

    #[test]
    fn parses_repeated_languages_and_options() {
        let path = temp_input("png");
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
        assert_eq!(options.inputs, [path.clone()]);
        assert_eq!(options.languages, ["en-US", "zh-Hans"]);
        assert_eq!(options.recognition_level, RecognitionLevel::Fast);
        assert!(options.include_regions);
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
        let path = temp_input("png");
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

    #[test]
    fn parses_orthogonal_options() {
        let path = temp_input("png");
        let options = parse_args([
            "--input",
            path.to_str().unwrap(),
            "--region",
            "0.1,0.2,0.5,0.6",
            "--preprocess",
            "auto",
            "--scale",
            "2",
            "--grayscale",
            "--contrast",
            "1.2",
            "--sharpen",
            "--rotate",
            "90",
            "--language",
            "en-US",
            "--candidates",
            "3",
            "--barcodes",
            "--barcode-symbology",
            "QR",
            "--layout",
            "reading-order",
            "--format",
            "json",
            "--regions",
            "--max-output-chars",
            "42",
        ])
        .unwrap();
        assert_eq!(options.inputs, [path.clone()]);
        assert_eq!(options.regions.len(), 1);
        assert_eq!(options.candidates, 3);
        assert_eq!(options.preprocess, PreprocessMode::Auto);
        assert_eq!(options.scale, 2);
        assert!(options.grayscale && options.sharpen && options.include_regions);
        assert_eq!(options.contrast, Some(1.2));
        assert_eq!(options.rotation, Rotation::Degrees(90));
        assert!(options.barcodes);
        assert_eq!(options.layout, LayoutMode::ReadingOrder);
        assert_eq!(options.output_format, OutputFormat::Json);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn accepts_multiple_inputs_and_jsonl() {
        let first = temp_input("png");
        let second = temp_input("jpg");
        let options = parse_args([
            "--input",
            first.to_str().unwrap(),
            "--input",
            second.to_str().unwrap(),
            "--format",
            "jsonl",
        ])
        .unwrap();
        assert_eq!(options.inputs, [first.clone(), second.clone()]);
        assert_eq!(options.output_format, OutputFormat::Jsonl);
        fs::remove_file(first).unwrap();
        fs::remove_file(second).unwrap();
    }

    #[test]
    fn accepts_input_directory() {
        let directory = std::env::temp_dir();
        let options = parse_args(["--input-dir", directory.to_str().unwrap()]).unwrap();
        assert_eq!(options.input_dir, Some(directory));
    }

    #[test]
    fn rejects_input_and_input_dir_together() {
        let path = temp_input("png");
        let directory = std::env::temp_dir();
        let error = parse_args([
            "--input",
            path.to_str().unwrap(),
            "--input-dir",
            directory.to_str().unwrap(),
        ])
        .unwrap_err();
        assert_eq!(error.code, "invalid_arguments");
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn rejects_invalid_region() {
        let path = temp_input("png");
        let error = parse_args([
            "--input",
            path.to_str().unwrap(),
            "--region",
            "0.8,0,0.5,0.5",
        ])
        .unwrap_err();
        assert_eq!(error.code, "invalid_arguments");
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn rejects_page_for_non_pdf() {
        let path = temp_input("png");
        let error = parse_args(["--input", path.to_str().unwrap(), "--page", "1"]).unwrap_err();
        assert_eq!(error.code, "invalid_arguments");
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn accepts_negative_contrast_values() {
        let path = temp_input("png");
        let options = parse_args(["--input", path.to_str().unwrap(), "--contrast", "-1"]).unwrap();
        assert_eq!(options.contrast, Some(-1.0));
        fs::remove_file(path).unwrap();
    }
}
