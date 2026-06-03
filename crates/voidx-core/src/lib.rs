//! voidx-core — Python bindings for the voidx Rust engine.
//!
//! Each public module wraps one internal crate with #[pyclass] types.

pub mod agent;
pub mod config;
pub mod enums;
pub mod error;
pub mod memory;
pub mod tools;

use pyo3::prelude::*;

/// Python module entry point.
#[pymodule]
fn voidx_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Config types
    m.add_class::<config::PyModelConfig>()?;
    m.add_class::<config::PyConfig>()?;

    // Enums
    m.add_class::<enums::PySandboxMode>()?;
    m.add_class::<enums::PyApprovalPolicy>()?;
    m.add_class::<enums::PyPermissionMode>()?;

    // Tool types
    m.add_class::<tools::PyToolResult>()?;
    m.add_class::<tools::PyToolRegistry>()?;

    // Memory
    m.add_class::<memory::PySessionStore>()?;
    m.add_class::<memory::PySessionInfo>()?;

    // Agent
    m.add_class::<agent::PyRustAgent>()?;
    m.add_class::<agent::PyRunResult>()?;

    Ok(())
}
