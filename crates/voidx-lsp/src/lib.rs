//! LSP (Language Server Protocol) — client and auto-detection.
//!
//! Ported from `src/voidx/lsp/`.

pub mod client;
pub mod detector;
pub mod error;
pub mod manager;
pub mod types;

pub use client::LspClient;
pub use detector::LspDetector;
pub use error::LspError;
pub use manager::LspManager;
