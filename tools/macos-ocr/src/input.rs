use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::cli::{CliOptions, PageSelection};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InputJob {
    pub source: PathBuf,
    pub page: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InputError {
    pub code: &'static str,
    pub message: String,
}

impl InputError {
    fn invalid_input(message: impl Into<String>) -> Self {
        Self {
            code: "invalid_input",
            message: message.into(),
        }
    }

    fn read_failed(message: impl Into<String>) -> Self {
        Self {
            code: "read_failed",
            message: message.into(),
        }
    }
}

pub fn enumerate_directory(directory: &Path) -> Result<Vec<PathBuf>, InputError> {
    let entries = fs::read_dir(directory).map_err(|error| {
        InputError::read_failed(format!(
            "failed to read input directory {}: {error}",
            directory.display()
        ))
    })?;

    let mut files = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| {
            InputError::read_failed(format!(
                "failed to inspect input directory {}: {error}",
                directory.display()
            ))
        })?;
        let path = entry.path();
        if path.is_file() && is_supported_input(&path) {
            files.push(path);
        }
    }

    files.sort_by(|left, right| {
        let left_key = left.to_string_lossy().to_ascii_lowercase();
        let right_key = right.to_string_lossy().to_ascii_lowercase();
        left_key.cmp(&right_key).then_with(|| left.cmp(right))
    });
    Ok(files)
}

pub fn expand_pages(selections: &[PageSelection], page_count: u32) -> Result<Vec<u32>, InputError> {
    if page_count == 0 {
        return Err(InputError::invalid_input(
            "PDF input does not contain any pages",
        ));
    }

    let mut pages = BTreeSet::new();
    if selections.is_empty() {
        pages.extend(1..=page_count);
    } else {
        for selection in selections {
            if selection.start == 0 || selection.end < selection.start || selection.end > page_count
            {
                return Err(InputError::invalid_input(format!(
                    "PDF page range {}-{} is outside the document with {} pages",
                    selection.start, selection.end, page_count
                )));
            }
            pages.extend(selection.start..=selection.end);
        }
    }

    Ok(pages.into_iter().collect())
}

pub fn jobs_for_options(options: &CliOptions) -> Result<Vec<InputJob>, InputError> {
    let sources = if let Some(directory) = &options.input_dir {
        enumerate_directory(directory)?
    } else {
        options.inputs.clone()
    };

    let mut jobs = Vec::new();
    for source in sources {
        if is_pdf(&source) {
            let page_count = crate::pdf::page_count(&source).map_err(|error| InputError {
                code: error.code,
                message: error.message,
            })?;
            jobs.extend(
                expand_pages(&options.pages, page_count)?
                    .into_iter()
                    .map(|page| InputJob {
                        source: source.clone(),
                        page: Some(page),
                    }),
            );
        } else {
            jobs.push(InputJob { source, page: None });
        }
    }
    Ok(jobs)
}

pub fn read_job(job: &InputJob) -> Result<Vec<u8>, InputError> {
    match job.page {
        Some(page) => crate::pdf::render_page(&job.source, page, 1.0).map_err(|error| InputError {
            code: error.code,
            message: error.message,
        }),
        None => fs::read(&job.source).map_err(|error| {
            InputError::read_failed(format!(
                "failed to read input {}: {error}",
                job.source.display()
            ))
        }),
    }
}

pub fn is_pdf(path: &Path) -> bool {
    has_extension(path, &["pdf"])
}

pub fn is_supported_input(path: &Path) -> bool {
    has_extension(
        path,
        &[
            "bmp", "gif", "jpeg", "jpg", "pdf", "png", "tif", "tiff", "webp",
        ],
    )
}

fn has_extension(path: &Path, extensions: &[&str]) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| {
            extensions
                .iter()
                .any(|candidate| extension.eq_ignore_ascii_case(candidate))
        })
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::{enumerate_directory, expand_pages, is_pdf, is_supported_input, jobs_for_options};
    use crate::cli::{parse_args, PageSelection};
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_directory() -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!("voidx-macos-ocr-input-{unique}"));
        fs::create_dir(&directory).unwrap();
        directory
    }

    #[test]
    fn enumerates_supported_files_in_stable_order() {
        let directory = temp_directory();
        fs::write(directory.join("b.PNG"), b"b").unwrap();
        fs::write(directory.join("a.jpg"), b"a").unwrap();
        fs::write(directory.join("document.PDF"), b"pdf").unwrap();
        fs::write(directory.join("ignore.txt"), b"ignore").unwrap();
        fs::create_dir(directory.join("nested.png")).unwrap();

        let files = enumerate_directory(&directory).unwrap();
        assert_eq!(
            files,
            vec![
                directory.join("a.jpg"),
                directory.join("b.PNG"),
                directory.join("document.PDF")
            ]
        );

        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn expands_and_deduplicates_page_ranges() {
        let pages = expand_pages(
            &[
                PageSelection { start: 2, end: 4 },
                PageSelection { start: 3, end: 5 },
                PageSelection { start: 1, end: 1 },
            ],
            5,
        )
        .unwrap();
        assert_eq!(pages, vec![1, 2, 3, 4, 5]);
    }

    #[test]
    fn selects_all_pages_when_no_ranges_are_given() {
        assert_eq!(expand_pages(&[], 3).unwrap(), vec![1, 2, 3]);
    }

    #[test]
    fn rejects_page_ranges_outside_document() {
        let error = expand_pages(&[PageSelection { start: 4, end: 6 }], 5).unwrap_err();
        assert_eq!(error.code, "invalid_input");
    }

    #[test]
    fn creates_jobs_for_directory_options() {
        let directory = temp_directory();
        fs::write(directory.join("one.png"), b"one").unwrap();
        let options = parse_args(["--input-dir", directory.to_str().unwrap()]).unwrap();
        let jobs = jobs_for_options(&options).unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].source, directory.join("one.png"));
        assert_eq!(jobs[0].page, None);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn excludes_formats_without_enabled_decoders() {
        assert!(!is_supported_input(std::path::Path::new("scan.heic")));
        assert!(!is_supported_input(std::path::Path::new("scan.HEIF")));
    }

    #[test]
    fn recognizes_pdf_extension_case_insensitively() {
        assert!(is_pdf(std::path::Path::new("scan.PDF")));
        assert!(!is_pdf(std::path::Path::new("scan.png")));
    }
}

#[cfg(all(test, target_os = "macos"))]
mod pdf_job_tests {
    use super::jobs_for_options;
    use crate::cli::parse_args;
    use std::fs;
    use std::path::Path;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn two_page_pdf() -> Vec<u8> {
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 5 0 R >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 5 0 R >>",
            "<< /Length 0 >>\nstream\n\nendstream",
        ];
        let mut pdf = b"%PDF-1.4\n".to_vec();
        let mut offsets = Vec::with_capacity(objects.len() + 1);
        offsets.push(0);
        for (index, object) in objects.iter().enumerate() {
            offsets.push(pdf.len());
            pdf.extend_from_slice(format!("{} 0 obj\n{}\nendobj\n", index + 1, object).as_bytes());
        }
        let xref_offset = pdf.len();
        pdf.extend_from_slice(format!("xref\n0 {}\n", objects.len() + 1).as_bytes());
        pdf.extend_from_slice(b"0000000000 65535 f \n");
        for offset in offsets.iter().skip(1) {
            pdf.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        pdf.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );
        pdf
    }

    fn temp_pdf() -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("voidx-macos-ocr-input-{unique}.pdf"))
    }

    #[test]
    fn expands_pdf_into_one_job_per_page() {
        let path = temp_pdf();
        fs::write(&path, two_page_pdf()).unwrap();
        let options = parse_args(["--input", path.to_str().unwrap()]).unwrap();

        let jobs = jobs_for_options(&options).unwrap();

        assert_eq!(
            jobs.iter()
                .map(|job| (job.source.as_path(), job.page))
                .collect::<Vec<(&Path, Option<u32>)>>()
                .as_slice(),
            &[(path.as_path(), Some(1)), (path.as_path(), Some(2))]
        );
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn expands_only_selected_pdf_pages() {
        let path = temp_pdf();
        fs::write(&path, two_page_pdf()).unwrap();
        let options = parse_args(["--input", path.to_str().unwrap(), "--page", "2"]).unwrap();

        let jobs = jobs_for_options(&options).unwrap();

        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].page, Some(2));
        fs::remove_file(path).unwrap();
    }
}
