//! Python error helpers.

use pyo3::PyErr;

/// Convert internal Rust errors to PyErr.
pub fn to_py_err<E: std::fmt::Display>(e: E) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("voidx: {e}"))
}
