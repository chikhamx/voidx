//! MCP (Model Context Protocol) — JSON-RPC client for external tool servers.
//!
//! Ported from `src/voidx/mcp/`.

pub mod client;
pub mod error;
pub mod manager;

pub use client::McpClient;
pub use error::McpError;
pub use manager::{McpManager, McpRuntimeStatus, McpServerConfig};
