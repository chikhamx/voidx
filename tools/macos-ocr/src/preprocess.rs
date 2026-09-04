use crate::cli::{NormalizedRegion, Rotation};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreprocessError {
    pub code: &'static str,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CoordinateMapper {
    source_width: u32,
    source_height: u32,
    crop_x: u32,
    crop_y: u32,
    crop_width: u32,
    crop_height: u32,
    rotation: Rotation,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProcessedImage {
    pub png: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub mapper: CoordinateMapper,
}

impl CoordinateMapper {
    pub fn map_rect(&self, x: f64, y: f64, width: f64, height: f64) -> (f64, f64, f64, f64) {
        let corners = [
            (x, y),
            (x + width, y),
            (x, y + height),
            (x + width, y + height),
        ];
        let mapped = corners.map(|(x, y)| match self.rotation {
            Rotation::Auto | Rotation::Degrees(0) => (x, y),
            Rotation::Degrees(90) => (y, 1.0 - x),
            Rotation::Degrees(180) => (1.0 - x, 1.0 - y),
            Rotation::Degrees(270) => (1.0 - y, x),
            Rotation::Degrees(_) => (x, y),
        });
        let min_x = mapped
            .iter()
            .map(|point| point.0)
            .fold(f64::INFINITY, f64::min);
        let min_y = mapped
            .iter()
            .map(|point| point.1)
            .fold(f64::INFINITY, f64::min);
        let max_x = mapped
            .iter()
            .map(|point| point.0)
            .fold(f64::NEG_INFINITY, f64::max);
        let max_y = mapped
            .iter()
            .map(|point| point.1)
            .fold(f64::NEG_INFINITY, f64::max);

        let crop_x = self.crop_x as f64 / self.source_width as f64;
        let crop_y = self.crop_y as f64 / self.source_height as f64;
        let crop_width = self.crop_width as f64 / self.source_width as f64;
        let crop_height = self.crop_height as f64 / self.source_height as f64;
        (
            crop_x + min_x * crop_width,
            crop_y + min_y * crop_height,
            (max_x - min_x) * crop_width,
            (max_y - min_y) * crop_height,
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PreprocessOptions {
    pub region: Option<NormalizedRegion>,
    pub scale: u32,
    pub grayscale: bool,
    pub contrast: Option<f32>,
    pub sharpen: bool,
    pub rotation: Rotation,
}

pub fn process(data: &[u8], options: PreprocessOptions) -> Result<ProcessedImage, PreprocessError> {
    if options.scale == 0 || options.scale > 8 {
        return Err(PreprocessError {
            code: "invalid_input",
            message: "image scale must be between 1 and 8".to_owned(),
        });
    }
    if let Some(contrast) = options.contrast {
        if !contrast.is_finite() || !(-2.0..=2.0).contains(&contrast) {
            return Err(PreprocessError {
                code: "invalid_input",
                message: "image contrast must be finite and between -2 and 2".to_owned(),
            });
        }
    }

    let reader = image::ImageReader::new(std::io::Cursor::new(data))
        .with_guessed_format()
        .map_err(|error| decode_error(error.to_string()))?;
    let mut decoder = reader
        .into_decoder()
        .map_err(|error| decode_error(error.to_string()))?;
    let orientation = image::ImageDecoder::orientation(&mut decoder)
        .map_err(|error| decode_error(error.to_string()))?;
    let mut image = image::DynamicImage::from_decoder(decoder)
        .map_err(|error| decode_error(error.to_string()))?;
    image.apply_orientation(orientation);

    let source_width = image.width();
    let source_height = image.height();
    if source_width == 0 || source_height == 0 {
        return Err(PreprocessError {
            code: "decode_failed",
            message: "decoded image has no pixels".to_owned(),
        });
    }

    let (crop_x, crop_y, crop_width, crop_height) =
        pixel_crop(options.region, source_width, source_height)?;
    let mapper = CoordinateMapper {
        source_width,
        source_height,
        crop_x,
        crop_y,
        crop_width,
        crop_height,
        rotation: normalize_rotation(options.rotation),
    };

    image = image.crop_imm(crop_x, crop_y, crop_width, crop_height);
    if options.scale > 1 {
        let width = crop_width
            .checked_mul(options.scale)
            .ok_or_else(|| PreprocessError {
                code: "invalid_input",
                message: "scaled image dimensions are too large".to_owned(),
            })?;
        let height = crop_height
            .checked_mul(options.scale)
            .ok_or_else(|| PreprocessError {
                code: "invalid_input",
                message: "scaled image dimensions are too large".to_owned(),
            })?;
        image = image.resize_exact(width, height, image::imageops::FilterType::Lanczos3);
    }
    if options.grayscale {
        image = image.grayscale();
    }
    if let Some(contrast) = options.contrast {
        image = image.adjust_contrast(contrast * 100.0);
    }
    if options.sharpen {
        image = image.unsharpen(1.0, 1);
    }
    image = match normalize_rotation(options.rotation) {
        Rotation::Auto | Rotation::Degrees(0) => image,
        Rotation::Degrees(90) => image.rotate90(),
        Rotation::Degrees(180) => image.rotate180(),
        Rotation::Degrees(270) => image.rotate270(),
        Rotation::Degrees(_) => image,
    };

    let width = image.width();
    let height = image.height();
    let mut output = std::io::Cursor::new(Vec::new());
    image
        .write_to(&mut output, image::ImageFormat::Png)
        .map_err(|error| PreprocessError {
            code: "encode_failed",
            message: format!("failed to encode preprocessed image: {error}"),
        })?;

    Ok(ProcessedImage {
        png: output.into_inner(),
        width,
        height,
        mapper,
    })
}

fn decode_error(message: String) -> PreprocessError {
    PreprocessError {
        code: "decode_failed",
        message: format!("failed to decode image: {message}"),
    }
}

fn normalize_rotation(rotation: Rotation) -> Rotation {
    match rotation {
        Rotation::Auto => Rotation::Degrees(0),
        value => value,
    }
}

fn pixel_crop(
    region: Option<NormalizedRegion>,
    source_width: u32,
    source_height: u32,
) -> Result<(u32, u32, u32, u32), PreprocessError> {
    let Some(region) = region else {
        return Ok((0, 0, source_width, source_height));
    };

    let x = (region.x * source_width as f64).round();
    let y = (region.y * source_height as f64).round();
    let right = ((region.x + region.width) * source_width as f64).round();
    let bottom = ((region.y + region.height) * source_height as f64).round();
    if ![x, y, right, bottom]
        .iter()
        .all(|value| value.is_finite() && *value >= 0.0)
    {
        return Err(PreprocessError {
            code: "invalid_input",
            message: "image region contains non-finite coordinates".to_owned(),
        });
    }

    let x = x.min(source_width as f64) as u32;
    let y = y.min(source_height as f64) as u32;
    let right = right.min(source_width as f64) as u32;
    let bottom = bottom.min(source_height as f64) as u32;
    if right <= x || bottom <= y {
        return Err(PreprocessError {
            code: "invalid_input",
            message: "image region is empty after pixel rounding".to_owned(),
        });
    }
    Ok((x, y, right - x, bottom - y))
}

#[cfg(test)]
mod tests {
    use super::{process, PreprocessOptions};
    use crate::cli::{NormalizedRegion, Rotation};
    use image::{DynamicImage, ImageFormat, Rgb, RgbImage};
    use std::io::Cursor;

    fn png(width: u32, height: u32) -> Vec<u8> {
        let image = RgbImage::from_pixel(width, height, Rgb([10, 20, 30]));
        let mut output = Cursor::new(Vec::new());
        DynamicImage::ImageRgb8(image)
            .write_to(&mut output, ImageFormat::Png)
            .unwrap();
        output.into_inner()
    }

    fn options() -> PreprocessOptions {
        PreprocessOptions {
            region: None,
            scale: 1,
            grayscale: false,
            contrast: None,
            sharpen: false,
            rotation: Rotation::Auto,
        }
    }

    #[test]
    fn decodes_the_full_image_without_transforming_dimensions() {
        let result = process(&png(4, 3), options()).unwrap();
        assert_eq!((result.width, result.height), (4, 3));
        assert_eq!(
            result.mapper.map_rect(0.0, 0.0, 1.0, 1.0),
            (0.0, 0.0, 1.0, 1.0)
        );
    }

    #[test]
    fn crops_top_left_normalized_region_before_scaling() {
        let mut options = options();
        options.region = Some(NormalizedRegion {
            x: 0.25,
            y: 0.25,
            width: 0.5,
            height: 0.5,
        });
        options.scale = 2;

        let result = process(&png(8, 8), options).unwrap();

        assert_eq!((result.width, result.height), (8, 8));
        assert_eq!(
            result.mapper.map_rect(0.0, 0.0, 1.0, 1.0),
            (0.25, 0.25, 0.5, 0.5)
        );
    }

    #[test]
    fn applies_grayscale_contrast_sharpen_and_rotation_as_one_pipeline() {
        let mut options = options();
        options.grayscale = true;
        options.contrast = Some(1.0);
        options.sharpen = true;
        options.rotation = Rotation::Degrees(90);

        let result = process(&png(4, 3), options).unwrap();

        assert_eq!((result.width, result.height), (3, 4));
        assert_eq!(
            result.mapper.map_rect(0.0, 0.0, 1.0, 1.0),
            (0.0, 0.0, 1.0, 1.0)
        );
    }

    #[test]
    fn maps_a_rotated_subregion_back_to_the_original_image() {
        let mut options = options();
        options.region = Some(NormalizedRegion {
            x: 0.25,
            y: 0.25,
            width: 0.5,
            height: 0.5,
        });
        options.rotation = Rotation::Degrees(90);
        let result = process(&png(8, 8), options).unwrap();

        let (x, y, width, height) = result.mapper.map_rect(0.1, 0.2, 0.3, 0.4);
        for (actual, expected) in [x, y, width, height]
            .into_iter()
            .zip([0.35, 0.55, 0.2, 0.15])
        {
            assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
        }
    }

    #[test]
    fn rejects_invalid_image_bytes() {
        let error = process(b"not an image", options()).unwrap_err();
        assert_eq!(error.code, "decode_failed");
    }
}
