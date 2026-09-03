#[derive(Debug, Clone, Copy, PartialEq)]
pub struct NormalizedRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

pub fn normalized_rect_to_top_left(rect: NormalizedRect) -> NormalizedRect {
    NormalizedRect {
        x: rect.x,
        y: 1.0 - rect.y - rect.height,
        width: rect.width,
        height: rect.height,
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use super::{normalized_rect_to_top_left, NormalizedRect};
    use crate::cli::{CliOptions, RecognitionLevel};
    use crate::output::{BoundingBox, TextBlock};
    use objc2::rc::autoreleasepool;
    use objc2::AnyThread;
    use objc2_foundation::{NSArray, NSData, NSDictionary, NSError, NSString};
    use objc2_vision::{
        VNImageRequestHandler, VNRecognizeTextRequest, VNRequest, VNRequestTextRecognitionLevel,
    };
    use std::fs;

    pub fn recognize(options: &CliOptions) -> Result<Vec<TextBlock>, VisionError> {
        let image_data = fs::read(&options.input).map_err(|error| VisionError {
            code: "read_failed",
            message: format!("failed to read input image {}: {error}", options.input.display()),
        })?;

        autoreleasepool(|_| {
            let image_data = NSData::with_bytes(&image_data);
            let request = VNRecognizeTextRequest::new();
            request.setRecognitionLevel(match options.recognition_level {
                RecognitionLevel::Fast => VNRequestTextRecognitionLevel::Fast,
                RecognitionLevel::Accurate => VNRequestTextRecognitionLevel::Accurate,
            });
            request.setUsesLanguageCorrection(true);
            request.setAutomaticallyDetectsLanguage(options.languages.is_empty());

            if !options.languages.is_empty() {
                let language_objects = options
                    .languages
                    .iter()
                    .map(|language| NSString::from_str(language))
                    .collect::<Vec<_>>();
                let language_refs = language_objects
                    .iter()
                    .map(|language| &**language)
                    .collect::<Vec<_>>();
                let languages = NSArray::from_slice(&language_refs);
                request.setRecognitionLanguages(&languages);
            }

            let request_ref: &VNRequest = &request;
            let requests = NSArray::from_slice(&[request_ref]);
            let handler_options = NSDictionary::new();
            let handler = VNImageRequestHandler::initWithData_options(
                VNImageRequestHandler::alloc(),
                &image_data,
                &handler_options,
            );
            handler
                .performRequests_error(&requests)
                .map_err(|error| VisionError {
                    code: "vision_failed",
                    message: error_description(&error),
                })?;

            let observations = request.results().unwrap_or_else(|| NSArray::new());
            let mut blocks = Vec::with_capacity(observations.len());
            for observation in observations.to_vec() {
                let candidates = observation.topCandidates(1);
                let Some(candidate) = candidates.to_vec().into_iter().next() else {
                    continue;
                };
                let text = candidate.string().to_string();
                if text.is_empty() {
                    continue;
                }
                let box_value = unsafe { observation.boundingBox() };
                let box_value = normalized_rect_to_top_left(NormalizedRect {
                    x: box_value.origin.x,
                    y: box_value.origin.y,
                    width: box_value.size.width,
                    height: box_value.size.height,
                });
                blocks.push(TextBlock {
                    text,
                    confidence: candidate.confidence(),
                    bounding_box: options_for_block(options, box_value),
                });
            }
            Ok(blocks)
        })
    }

    fn options_for_block(
        options: &CliOptions,
        box_value: NormalizedRect,
    ) -> Option<BoundingBox> {
        options.regions.then_some(BoundingBox {
            x: box_value.x,
            y: box_value.y,
            width: box_value.width,
            height: box_value.height,
        })
    }

    fn error_description(error: &NSError) -> String {
        error.localizedDescription().to_string()
    }

    #[derive(Debug)]
    pub struct VisionError {
        pub code: &'static str,
        pub message: String,
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use crate::output::TextBlock;
    use crate::cli::CliOptions;

    pub fn recognize(_options: &CliOptions) -> Result<Vec<TextBlock>, VisionError> {
        Err(VisionError {
            code: "vision_unavailable",
            message: "Apple Vision OCR is available only on macOS".to_owned(),
        })
    }

    #[derive(Debug)]
    pub struct VisionError {
        pub code: &'static str,
        pub message: String,
    }
}

pub use platform::recognize;

#[cfg(test)]
mod tests {
    use super::{normalized_rect_to_top_left, NormalizedRect};

    #[test]
    fn converts_the_full_image_without_moving_it() {
        let result = normalized_rect_to_top_left(NormalizedRect {
            x: 0.0,
            y: 0.0,
            width: 1.0,
            height: 1.0,
        });
        assert_eq!(result.y, 0.0);
    }
}
