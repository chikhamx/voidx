//! Tool system — typed, deterministic tool execution.
//!
//! Ported from `src/voidx/tools/`.

pub mod base;
pub mod error;
pub mod registry;
pub mod schema;

mod bash;
mod file_ops;
mod glob;
mod grep;
mod webfetch;
mod websearch;

pub use base::{Tool, ToolContext, ToolResult};
pub use error::ToolError;
pub use registry::ToolRegistry;
pub use schema::model_to_json_schema;
