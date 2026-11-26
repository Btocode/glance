//! Local OCR for Glance Shot: the `ocrs` engine over a PNG held in memory.
//!
//! The two models (text detection and recognition, ONNX) are files Glance fetched into its
//! data dir and checked against SHA-256 pins before this is ever called (Python side,
//! `glance.shot.ocr`); nothing here touches the network. The image never reaches disk:
//! it comes in as PNG bytes and is decoded here.

use std::path::Path;

use ocrs::{ImageSource, OcrEngine, OcrEngineParams};
use rten::Model;

/// An OCR engine with both models loaded (loading takes ~100 ms: keep one around).
pub struct Ocr {
    engine: OcrEngine,
}

/// Why OCR failed, as text (the Python side raises it as a ValueError/OSError).
#[derive(Debug)]
pub enum OcrFailure {
    Model(String),
    Image(String),
    Engine(String),
}

impl std::fmt::Display for OcrFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OcrFailure::Model(m) => write!(f, "model: {m}"),
            OcrFailure::Image(m) => write!(f, "image: {m}"),
            OcrFailure::Engine(m) => write!(f, "ocr: {m}"),
        }
    }
}

/// An 8-bit image decoded from PNG: width, height, channels (1, 3 or 4), pixels in HWC.
pub struct Pixels {
    pub width: u32,
    pub height: u32,
    pub channels: usize,
    pub data: Vec<u8>,
}

/// Decode a PNG to 8-bit grey, RGB or RGBA (palettes expanded, 16 bit stripped).
pub fn decode_png(bytes: &[u8]) -> Result<Pixels, OcrFailure> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::normalize_to_color8());
    let mut reader = decoder
        .read_info()
        .map_err(|e| OcrFailure::Image(e.to_string()))?;
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| OcrFailure::Image("too large".into()))?;
    let mut buf = vec![0; size];
    let info = reader
        .next_frame(&mut buf)
        .map_err(|e| OcrFailure::Image(e.to_string()))?;
    buf.truncate(info.buffer_size());
    let (channels, data) = match info.color_type {
        png::ColorType::Grayscale => (1, buf),
        png::ColorType::Rgb => (3, buf),
        png::ColorType::Rgba => (4, buf),
        // grey + alpha: keep the grey
        png::ColorType::GrayscaleAlpha => (1, buf.chunks_exact(2).map(|p| p[0]).collect()),
        png::ColorType::Indexed => return Err(OcrFailure::Image("palette not expanded".into())),
    };
    Ok(Pixels {
        width: info.width,
        height: info.height,
        channels,
        data,
    })
}

impl Ocr {
    pub fn load(detection: &Path, recognition: &Path) -> Result<Ocr, OcrFailure> {
        let det = Model::load_file(detection).map_err(|e| OcrFailure::Model(e.to_string()))?;
        let rec = Model::load_file(recognition).map_err(|e| OcrFailure::Model(e.to_string()))?;
        let engine = OcrEngine::new(OcrEngineParams {
            detection_model: Some(det),
            recognition_model: Some(rec),
            ..Default::default()
        })
        .map_err(|e| OcrFailure::Engine(e.to_string()))?;
        Ok(Ocr { engine })
    }

    /// The text of the image, one line of text per line; spurious one-character detections
    /// are dropped.
    pub fn text_of(&self, px: &Pixels) -> Result<String, OcrFailure> {
        if px.width == 0 || px.height == 0 {
            return Ok(String::new());
        }
        let source = ImageSource::from_bytes(&px.data, (px.width, px.height))
            .map_err(|e| OcrFailure::Image(e.to_string()))?;
        let input = self
            .engine
            .prepare_input(source)
            .map_err(|e| OcrFailure::Engine(e.to_string()))?;
        let words = self
            .engine
            .detect_words(&input)
            .map_err(|e| OcrFailure::Engine(e.to_string()))?;
        let lines = self.engine.find_text_lines(&input, &words);
        let texts = self
            .engine
            .recognize_text(&input, &lines)
            .map_err(|e| OcrFailure::Engine(e.to_string()))?;
        Ok(texts
            .into_iter()
            .flatten()
            .map(|l| l.to_string())
            .filter(|l| l.trim().chars().count() > 1)
            .collect::<Vec<_>>()
            .join("\n"))
    }

    pub fn png_text(&self, png_bytes: &[u8]) -> Result<String, OcrFailure> {
        self.text_of(&decode_png(png_bytes)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tiny_png(w: u32, h: u32, color: png::ColorType, data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        {
            let mut enc = png::Encoder::new(&mut out, w, h);
            enc.set_color(color);
            enc.set_depth(png::BitDepth::Eight);
            let mut wr = enc.write_header().unwrap();
            wr.write_image_data(data).unwrap();
        }
        out
    }

    #[test]
    fn decodes_rgb_and_grey_alpha() {
        let rgb = tiny_png(2, 1, png::ColorType::Rgb, &[1, 2, 3, 4, 5, 6]);
        let px = decode_png(&rgb).unwrap();
        assert_eq!((px.width, px.height, px.channels), (2, 1, 3));
        assert_eq!(px.data, vec![1, 2, 3, 4, 5, 6]);
        let ga = tiny_png(2, 1, png::ColorType::GrayscaleAlpha, &[9, 255, 7, 0]);
        let px = decode_png(&ga).unwrap();
        assert_eq!((px.channels, px.data.clone()), (1, vec![9, 7]));
    }

    #[test]
    fn rejects_garbage() {
        assert!(matches!(
            decode_png(b"not a png"),
            Err(OcrFailure::Image(_))
        ));
    }

    #[test]
    fn missing_models_are_a_model_error() {
        let r = Ocr::load(
            Path::new("/nonexistent/det.onnx"),
            Path::new("/nonexistent/rec.onnx"),
        );
        assert!(matches!(r, Err(OcrFailure::Model(_))));
    }

    /// With GLANCE_OCR_MODELS=<dir holding text-detection.onnx and text-recognition.onnx>
    /// and GLANCE_OCR_IMAGE=<png>, prints what it reads (a manual accuracy check).
    #[test]
    fn reads_an_image_when_models_are_given() {
        let (Ok(dir), Ok(img)) = (
            std::env::var("GLANCE_OCR_MODELS"),
            std::env::var("GLANCE_OCR_IMAGE"),
        ) else {
            return;
        };
        let dir = Path::new(&dir);
        let ocr = Ocr::load(
            &dir.join("text-detection.onnx"),
            &dir.join("text-recognition.onnx"),
        )
        .unwrap();
        let text = ocr.png_text(&std::fs::read(img).unwrap()).unwrap();
        println!("OCR: {text}");
        assert!(!text.is_empty());
    }
}
