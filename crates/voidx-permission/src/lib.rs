//! Permission engine — sandbox, approval policies, tool call authorization.
//!
//! Ported from `src/voidx/permission/`.

pub mod engine;
pub mod error;
pub mod evaluate;
pub mod sandbox;
pub mod schema;
pub mod wildcard;

pub use engine::{ClassifiedToolCall, PermissionContext, PermissionDecision, PermissionEngine, ToolCapability};
pub use error::PermissionError;
pub use evaluate::PermissionVerdict;
pub use schema::{Action, Rule, Ruleset, basic_rules};
pub use voidx_config::{ApprovalPolicy, SandboxMode};
