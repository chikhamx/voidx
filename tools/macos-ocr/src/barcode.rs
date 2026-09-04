use crate::output::{Barcode, BoundingBox};

#[derive(Debug, Clone, PartialEq)]
pub struct BarcodeObservation {
    pub payload: Option<String>,
    pub symbology: String,
    pub confidence: f32,
    pub bounding_box: Option<BoundingBox>,
}

pub fn canonical_symbology(value: &str) -> Option<&'static str> {
    let value = compact(value);
    const ALIASES: &[(&str, &str)] = &[
        ("code39fullasciichecksum", "Code39FullASCIIChecksum"),
        ("code39fullascii", "Code39FullASCII"),
        ("code39checksum", "Code39Checksum"),
        ("interleaved2of5checksum", "I2of5Checksum"),
        ("code93i", "Code93i"),
        ("code93", "Code93"),
        ("code128", "Code128"),
        ("code39", "Code39"),
        ("databarexpanded", "GS1DataBarExpanded"),
        ("databarlimited", "GS1DataBarLimited"),
        ("databar", "GS1DataBar"),
        ("micropdf417", "MicroPDF417"),
        ("microqrcode", "MicroQR"),
        ("microqr", "MicroQR"),
        ("datamatrix", "DataMatrix"),
        ("ean13", "EAN13"),
        ("ean8", "EAN8"),
        ("interleaved2of5", "I2of5"),
        ("i2of5checksum", "I2of5Checksum"),
        ("i2of5", "I2of5"),
        ("itf14", "ITF14"),
        ("msiplessey", "MSIPlessey"),
        ("pdf417", "PDF417"),
        ("qrcode", "QR"),
        ("qr", "QR"),
        ("upce", "UPCE"),
        ("aztec", "Aztec"),
        ("codabar", "Codabar"),
    ];

    ALIASES.iter().find_map(|(alias, canonical)| {
        (value == *alias || value.ends_with(alias)).then_some(*canonical)
    })
}

pub fn display_symbology(value: &str) -> String {
    canonical_symbology(value).unwrap_or(value).to_owned()
}

pub fn matches_symbology(observation: &BarcodeObservation, filters: &[String]) -> bool {
    filters.is_empty()
        || filters.iter().any(|filter| {
            match (
                canonical_symbology(&observation.symbology),
                canonical_symbology(filter),
            ) {
                (Some(observation), Some(filter)) => observation == filter,
                _ => observation.symbology.eq_ignore_ascii_case(filter),
            }
        })
}

fn matches_symbology_name(value: &str, filters: &[String]) -> bool {
    matches_symbology(
        &BarcodeObservation {
            payload: None,
            symbology: value.to_owned(),
            confidence: 0.0,
            bounding_box: None,
        },
        filters,
    )
}

pub fn to_output(observation: BarcodeObservation) -> Option<Barcode> {
    let payload = observation.payload.filter(|payload| !payload.is_empty())?;
    Some(Barcode {
        payload,
        symbology: display_symbology(&observation.symbology),
        confidence: observation.confidence,
        bounding_box: observation.bounding_box,
    })
}

fn compact(value: &str) -> String {
    value
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

#[cfg(target_os = "macos")]
mod platform {
    use super::{matches_symbology_name, to_output, BarcodeObservation};
    use crate::cli::CliOptions;
    use crate::output::{Barcode, BoundingBox};
    use crate::preprocess::CoordinateMapper;
    use crate::vision::{normalized_rect_to_top_left, NormalizedRect};
    use objc2::rc::autoreleasepool;
    use objc2::AnyThread;
    use objc2_foundation::{NSArray, NSData, NSDictionary, NSError};
    use objc2_vision::{VNDetectBarcodesRequest, VNImageRequestHandler, VNRequest};

    pub fn detect(
        image_data: &[u8],
        mapper: &CoordinateMapper,
        options: &CliOptions,
    ) -> Result<Vec<Barcode>, BarcodeError> {
        autoreleasepool(|_| {
            let image_data = NSData::with_bytes(image_data);
            let request = unsafe { VNDetectBarcodesRequest::new() };

            if !options.barcode_symbologies.is_empty() {
                let supported =
                    unsafe { request.supportedSymbologiesAndReturnError() }.map_err(|error| {
                        BarcodeError {
                            code: "vision_failed",
                            message: error_description(&error),
                        }
                    })?;
                let selected = supported
                    .to_vec()
                    .into_iter()
                    .filter(|symbology| {
                        matches_symbology_name(&symbology.to_string(), &options.barcode_symbologies)
                    })
                    .collect::<Vec<_>>();
                if selected.is_empty() {
                    return Err(BarcodeError {
                        code: "invalid_arguments",
                        message: format!(
                            "none of the requested barcode symbologies are supported: {}",
                            options.barcode_symbologies.join(", ")
                        ),
                    });
                }
                let selected_refs = selected
                    .iter()
                    .map(|symbology| &**symbology)
                    .collect::<Vec<_>>();
                let selected = NSArray::from_slice(&selected_refs);
                unsafe { request.setSymbologies(&selected) };
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
                .map_err(|error| BarcodeError {
                    code: "vision_failed",
                    message: error_description(&error),
                })?;

            let observations = unsafe { request.results() }.unwrap_or_else(|| NSArray::new());
            let mut barcodes = Vec::with_capacity(observations.len());
            for observation in observations.to_vec() {
                let symbology = unsafe { observation.symbology() }.to_string();
                if !matches_symbology_name(&symbology, &options.barcode_symbologies) {
                    continue;
                }
                let payload =
                    unsafe { observation.payloadStringValue() }.map(|value| value.to_string());
                let box_value = unsafe { observation.boundingBox() };
                let box_value = normalized_rect_to_top_left(NormalizedRect {
                    x: box_value.origin.x,
                    y: box_value.origin.y,
                    width: box_value.size.width,
                    height: box_value.size.height,
                });
                let observation = BarcodeObservation {
                    payload,
                    symbology,
                    confidence: unsafe { observation.confidence() },
                    bounding_box: options.include_regions.then_some({
                        let (x, y, width, height) = mapper.map_rect(
                            box_value.x,
                            box_value.y,
                            box_value.width,
                            box_value.height,
                        );
                        BoundingBox {
                            x,
                            y,
                            width,
                            height,
                        }
                    }),
                };
                if let Some(barcode) = to_output(observation) {
                    barcodes.push(barcode);
                }
            }
            Ok(barcodes)
        })
    }

    fn error_description(error: &NSError) -> String {
        error.localizedDescription().to_string()
    }

    #[derive(Debug)]
    pub struct BarcodeError {
        pub code: &'static str,
        pub message: String,
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use crate::cli::CliOptions;
    use crate::output::Barcode;
    use crate::preprocess::CoordinateMapper;

    pub fn detect(
        _image_data: &[u8],
        _mapper: &CoordinateMapper,
        _options: &CliOptions,
    ) -> Result<Vec<Barcode>, BarcodeError> {
        Err(BarcodeError {
            code: "vision_unavailable",
            message: "Apple Vision barcode detection is available only on macOS".to_owned(),
        })
    }

    #[derive(Debug)]
    pub struct BarcodeError {
        pub code: &'static str,
        pub message: String,
    }
}

pub use platform::{detect, BarcodeError};

#[cfg(test)]
mod tests {
    use super::{
        canonical_symbology, display_symbology, matches_symbology, to_output, BarcodeObservation,
    };
    use crate::output::BoundingBox;

    fn observation(symbology: &str, payload: Option<&str>) -> BarcodeObservation {
        BarcodeObservation {
            payload: payload.map(str::to_owned),
            symbology: symbology.to_owned(),
            confidence: 0.91,
            bounding_box: Some(BoundingBox {
                x: 0.1,
                y: 0.2,
                width: 0.3,
                height: 0.4,
            }),
        }
    }

    #[test]
    fn normalizes_cli_aliases_and_vision_identifiers() {
        assert_eq!(canonical_symbology("qr"), Some("QR"));
        assert_eq!(canonical_symbology("org.iso.QRCode"), Some("QR"));
        assert_eq!(canonical_symbology("org.gs1.EAN-13"), Some("EAN13"));
        assert_eq!(
            canonical_symbology("org.iso.Interleaved2of5"),
            Some("I2of5")
        );
        assert_eq!(display_symbology("org.iso.Code128"), "Code128");
        assert_eq!(canonical_symbology("unknown"), None);
    }

    #[test]
    fn matches_requested_symbology_case_insensitively() {
        assert!(matches_symbology(
            &observation("org.iso.QRCode", Some("https://example.com")),
            &["qr".to_owned()]
        ));
        assert!(!matches_symbology(
            &observation("QR", Some("https://example.com")),
            &["ean13".to_owned()]
        ));
    }

    #[test]
    fn accepts_all_symbologies_without_a_filter() {
        assert!(matches_symbology(
            &observation("PDF417", Some("payload")),
            &[]
        ));
    }

    #[test]
    fn drops_observations_without_a_string_payload() {
        assert_eq!(to_output(observation("QR", None)), None);
        assert_eq!(to_output(observation("QR", Some(""))), None);
    }
}
