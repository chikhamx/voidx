//! Python enum wrappers matching voidx_config enums.

use pyo3::prelude::*;
use voidx_config::{ApprovalPolicy, PermissionMode, SandboxMode};

#[pyclass(name = "SandboxMode", eq)]
#[derive(Debug, Clone, PartialEq)]
pub struct PySandboxMode(pub SandboxMode);

#[pymethods]
impl PySandboxMode {
    #[new]
    fn new(mode: &str) -> PyResult<Self> {
        let m = match mode.to_lowercase().as_str() {
            "read-only" | "readonly" => SandboxMode::ReadOnly,
            "workspace-write" | "workspacewrite" => SandboxMode::WorkspaceWrite,
            "danger-full-access" | "dangerfullaccess" => SandboxMode::DangerFullAccess,
            _ => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    format!("Unknown sandbox mode: {mode}"),
                ))
            }
        };
        Ok(PySandboxMode(m))
    }

    fn __str__(&self) -> String {
        format!("{:?}", self.0)
    }

    fn __repr__(&self) -> String {
        format!("SandboxMode.{:?}", self.0)
    }
}

impl From<SandboxMode> for PySandboxMode {
    fn from(m: SandboxMode) -> Self {
        PySandboxMode(m)
    }
}

#[pyclass(name = "ApprovalPolicy", eq)]
#[derive(Debug, Clone, PartialEq)]
pub struct PyApprovalPolicy(pub ApprovalPolicy);

#[pymethods]
impl PyApprovalPolicy {
    #[new]
    fn new(policy: &str) -> PyResult<Self> {
        let p = match policy.to_lowercase().as_str() {
            "untrusted" => ApprovalPolicy::Untrusted,
            "on-failure" | "onfailure" => ApprovalPolicy::OnFailure,
            "on-request" | "onrequest" => ApprovalPolicy::OnRequest,
            "never" => ApprovalPolicy::Never,
            _ => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    format!("Unknown approval policy: {policy}"),
                ))
            }
        };
        Ok(PyApprovalPolicy(p))
    }

    fn __str__(&self) -> String {
        format!("{:?}", self.0)
    }
}

#[pyclass(name = "PermissionMode", eq)]
#[derive(Debug, Clone, PartialEq)]
pub struct PyPermissionMode(pub PermissionMode);

#[pymethods]
impl PyPermissionMode {
    #[new]
    fn new(mode: &str) -> PyResult<Self> {
        let m = match mode.to_lowercase().as_str() {
            "default" => PermissionMode::Default,
            "read-only" | "readonly" => PermissionMode::ReadOnly,
            "accept-edits" | "acceptedits" => PermissionMode::AcceptEdits,
            "auto-review" | "autoreview" => PermissionMode::AutoReview,
            "full-access" | "fullaccess" => PermissionMode::FullAccess,
            "custom" => PermissionMode::Custom,
            _ => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    format!("Unknown permission mode: {mode}"),
                ))
            }
        };
        Ok(PyPermissionMode(m))
    }
}
