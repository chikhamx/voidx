use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PdfError {
    pub code: &'static str,
    pub message: String,
}

impl PdfError {
    fn invalid_input(message: impl Into<String>) -> Self {
        Self {
            code: "invalid_input",
            message: message.into(),
        }
    }
}

pub fn page_count(path: &Path) -> Result<u32, PdfError> {
    if !path.is_file() {
        return Err(PdfError::invalid_input(format!(
            "PDF input does not exist or is not a regular file: {}",
            path.display()
        )));
    }
    platform::page_count(path)
}

pub fn render_page(path: &Path, page: u32, scale: f64) -> Result<Vec<u8>, PdfError> {
    if !path.is_file() {
        return Err(PdfError::invalid_input(format!(
            "PDF input does not exist or is not a regular file: {}",
            path.display()
        )));
    }
    if page == 0 {
        return Err(PdfError::invalid_input(
            "PDF page numbers are 1-based and must be greater than zero",
        ));
    }
    if !scale.is_finite() || !(0.1..=8.0).contains(&scale) {
        return Err(PdfError::invalid_input(
            "PDF render scale must be finite and between 0.1 and 8",
        ));
    }
    platform::render_page(path, page, scale)
}

#[cfg(target_os = "macos")]
mod platform {
    use super::PdfError;
    use image::{DynamicImage, ImageFormat, RgbaImage};
    use objc2_core_foundation::{CFData, CGPoint, CGRect, CGSize};
    use objc2_core_graphics::{
        CGBitmapContextCreate, CGBitmapContextGetData, CGColorSpace, CGContext, CGDataProvider,
        CGImageAlphaInfo, CGPDFBox, CGPDFDocument, CGPDFPage,
    };
    use std::ffi::c_void;
    use std::fs;
    use std::io::Cursor;
    use std::path::Path;
    use std::slice;

    pub fn page_count(path: &Path) -> Result<u32, PdfError> {
        let data = fs::read(path).map_err(|error| PdfError {
            code: "read_failed",
            message: format!("failed to read PDF {}: {error}", path.display()),
        })?;
        let document = open_document(&data, path)?;
        let count = CGPDFDocument::number_of_pages(Some(&document));
        if count == 0 {
            return Err(PdfError {
                code: "invalid_input",
                message: format!("PDF contains no pages: {}", path.display()),
            });
        }
        u32::try_from(count).map_err(|_| PdfError {
            code: "invalid_input",
            message: format!("PDF has too many pages: {}", path.display()),
        })
    }

    pub fn render_page(path: &Path, page_number: u32, scale: f64) -> Result<Vec<u8>, PdfError> {
        let data = fs::read(path).map_err(|error| PdfError {
            code: "read_failed",
            message: format!("failed to read PDF {}: {error}", path.display()),
        })?;
        let document = open_document(&data, path)?;
        let page =
            CGPDFDocument::page(Some(&document), page_number as usize).ok_or_else(|| PdfError {
                code: "invalid_input",
                message: format!(
                    "PDF page {page_number} is not available: {}",
                    path.display()
                ),
            })?;
        let crop_box = CGPDFPage::box_rect(Some(&page), CGPDFBox::CropBox);
        let width = checked_dimension(crop_box.size.width, scale, "width")?;
        let height = checked_dimension(crop_box.size.height, scale, "height")?;
        let color_space = CGColorSpace::new_device_rgb().ok_or_else(|| PdfError {
            code: "pdf_failed",
            message: "failed to create PDF render color space".to_owned(),
        })?;
        let bytes_per_row = width.checked_mul(4).ok_or_else(|| PdfError {
            code: "invalid_input",
            message: "PDF render dimensions are too large".to_owned(),
        })?;
        let context = unsafe {
            CGBitmapContextCreate(
                std::ptr::null_mut::<c_void>(),
                width,
                height,
                8,
                bytes_per_row,
                Some(&color_space),
                CGImageAlphaInfo::PremultipliedLast.0,
            )
        }
        .ok_or_else(|| PdfError {
            code: "pdf_failed",
            message: "failed to create PDF render bitmap context".to_owned(),
        })?;

        CGContext::set_rgb_fill_color(Some(&context), 1.0, 1.0, 1.0, 1.0);
        CGContext::fill_rect(
            Some(&context),
            CGRect::new(
                CGPoint::new(0.0, 0.0),
                CGSize::new(width as f64, height as f64),
            ),
        );
        let render_rect = CGRect::new(
            CGPoint::new(0.0, 0.0),
            CGSize::new(width as f64, height as f64),
        );
        let transform = CGPDFPage::drawing_transform(
            Some(&page),
            CGPDFBox::CropBox,
            render_rect,
            CGPDFPage::rotation_angle(Some(&page)),
            true,
        );
        CGContext::concat_ctm(Some(&context), transform);
        CGContext::draw_pdf_page(Some(&context), Some(&page));
        CGContext::flush(Some(&context));

        let raw = CGBitmapContextGetData(Some(&context));
        if raw.is_null() {
            return Err(PdfError {
                code: "pdf_failed",
                message: "PDF render bitmap context returned no pixel data".to_owned(),
            });
        }
        let byte_len = height.checked_mul(bytes_per_row).ok_or_else(|| PdfError {
            code: "invalid_input",
            message: "PDF render dimensions are too large".to_owned(),
        })?;
        let pixels = unsafe { slice::from_raw_parts(raw.cast::<u8>(), byte_len) };
        let mut packed = Vec::with_capacity(width * height * 4);
        for row in pixels.chunks(bytes_per_row) {
            packed.extend_from_slice(&row[..width * 4]);
        }
        let image =
            RgbaImage::from_raw(width as u32, height as u32, packed).ok_or_else(|| PdfError {
                code: "pdf_failed",
                message: "failed to construct PDF render image".to_owned(),
            })?;
        let mut output = Cursor::new(Vec::new());
        DynamicImage::ImageRgba8(image)
            .write_to(&mut output, ImageFormat::Png)
            .map_err(|error| PdfError {
                code: "pdf_failed",
                message: format!("failed to encode rendered PDF page: {error}"),
            })?;
        Ok(output.into_inner())
    }

    fn open_document(
        data: &[u8],
        path: &Path,
    ) -> Result<objc2_core_foundation::CFRetained<CGPDFDocument>, PdfError> {
        let data = CFData::from_bytes(data);
        let provider = CGDataProvider::with_cf_data(Some(&data)).ok_or_else(|| PdfError {
            code: "invalid_input",
            message: format!("failed to create PDF data provider: {}", path.display()),
        })?;
        CGPDFDocument::with_provider(Some(&provider)).ok_or_else(|| PdfError {
            code: "invalid_input",
            message: format!("invalid or unreadable PDF: {}", path.display()),
        })
    }

    fn checked_dimension(value: f64, scale: f64, name: &str) -> Result<usize, PdfError> {
        let dimension = (value * scale).ceil();
        if !dimension.is_finite() || dimension <= 0.0 || dimension > 32_768.0 {
            return Err(PdfError {
                code: "invalid_input",
                message: format!("PDF page {name} is outside supported render dimensions"),
            });
        }
        Ok(dimension as usize)
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::PdfError;
    use std::path::Path;

    pub fn page_count(_path: &Path) -> Result<u32, PdfError> {
        Err(PdfError {
            code: "pdf_unavailable",
            message: "PDF input is available only on macOS".to_owned(),
        })
    }

    pub fn render_page(_path: &Path, _page: u32, _scale: f64) -> Result<Vec<u8>, PdfError> {
        Err(PdfError {
            code: "pdf_unavailable",
            message: "PDF input is available only on macOS".to_owned(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{page_count, render_page};
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static NEXT_TEST_FILE_ID: AtomicU64 = AtomicU64::new(0);

    fn temp_file() -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let sequence = NEXT_TEST_FILE_ID.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("voidx-macos-ocr-pdf-{unique}-{sequence}.pdf"));
        fs::write(&path, b"not a pdf").unwrap();
        path
    }

    #[test]
    fn rejects_invalid_pdf_data() {
        let path = temp_file();
        let error = page_count(&path).unwrap_err();
        assert!(matches!(error.code, "invalid_input" | "pdf_unavailable"));
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn rejects_page_zero() {
        let path = temp_file();
        let error = render_page(&path, 0, 2.0).unwrap_err();
        assert_eq!(error.code, "invalid_input");
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn rejects_invalid_scale() {
        let path = temp_file();
        let error = render_page(&path, 1, 0.0).unwrap_err();
        assert_eq!(error.code, "invalid_input");
        fs::remove_file(path).unwrap();
    }
}
