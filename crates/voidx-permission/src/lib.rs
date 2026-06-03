//! Permission engine — sandbox, approval policies, tool call authorization.
//!
//! Ported from `src/voidx/permission/`.

pub mod engine;
pub mod error;
pub mod evaluate;
pub mod sandbox;
pub mod wildcard;

pub use engine::PermissionEngine;
pub use error::PermissionError;
pub use evaluate::PermissionVerdict;
pub use voidx_config::{ApprovalPolicy, SandboxMode};
