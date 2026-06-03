//! Python-wrapped config types.

use crate::enums::PySandboxMode;
use pyo3::prelude::*;
use voidx_config::{Config, ModelConfig};

#[pyclass(name = "ModelConfig")]
#[derive(Debug, Clone)]
pub struct PyModelConfig {
    #[pyo3(get, set)]
    pub provider: String,
    #[pyo3(get, set)]
    pub model: String,
    #[pyo3(get, set)]
    pub protocol: Option<String>,
    #[pyo3(get, set)]
    pub base_url: Option<String>,
    #[pyo3(get, set)]
    pub temperature: f64,
    #[pyo3(get, set)]
    pub max_tokens: u32,
    #[pyo3(get, set)]
    pub reasoning_effort: Option<String>,
}

#[pymethods]
impl PyModelConfig {
    #[new]
    #[pyo3(signature = (provider, model, protocol=None, base_url=None, temperature=0.7, max_tokens=8192, reasoning_effort=None))]
    fn new(
        provider: String,
        model: String,
        protocol: Option<String>,
        base_url: Option<String>,
        temperature: f64,
        max_tokens: u32,
        reasoning_effort: Option<String>,
    ) -> Self {
        PyModelConfig {
            provider,
            model,
            protocol,
            base_url,
            temperature,
            max_tokens,
            reasoning_effort,
        }
    }
}

impl From<PyModelConfig> for ModelConfig {
    fn from(py: PyModelConfig) -> Self {
        ModelConfig {
            provider: py.provider,
            model: py.model,
            protocol: py.protocol,
            base_url: py.base_url,
            temperature: py.temperature,
            max_tokens: py.max_tokens,
            reasoning_effort: py.reasoning_effort,
        }
    }
}

#[pyclass(name = "Config")]
#[derive(Debug, Clone)]
pub struct PyConfig {
    #[pyo3(get, set)]
    pub workspace: String,
    pub model: PyModelConfig,
    #[pyo3(get, set)]
    pub sandbox_mode: PySandboxMode,
    #[pyo3(get, set)]
    pub sandbox_workspace_write: bool,
    #[pyo3(get, set)]
    pub approval_policy: String,
    #[pyo3(get, set)]
    pub permission_mode: String,
}

#[pymethods]
impl PyConfig {
    #[new]
    #[pyo3(signature = (workspace, model, sandbox_mode=None, sandbox_workspace_write=false, approval_policy="untrusted", permission_mode="default"))]
    fn new(
        workspace: String,
        model: PyModelConfig,
        sandbox_mode: Option<PySandboxMode>,
        sandbox_workspace_write: bool,
        approval_policy: &str,
        permission_mode: &str,
    ) -> Self {
        PyConfig {
            workspace,
            model,
            sandbox_mode: sandbox_mode.unwrap_or(PySandboxMode(
                voidx_config::SandboxMode::WorkspaceWrite,
            )),
            sandbox_workspace_write,
            approval_policy: approval_policy.to_string(),
            permission_mode: permission_mode.to_string(),
        }
    }
}

impl From<PyConfig> for Config {
    fn from(py: PyConfig) -> Self {
        Config {
            workspace: std::path::PathBuf::from(&py.workspace),
            model: ModelConfig::from(py.model),
            sandbox_mode: py.sandbox_mode.0,
            sandbox_workspace_write: py.sandbox_workspace_write,
            approval_policy: voidx_config::ApprovalPolicy::Untrusted,
            approval_reviewer: voidx_config::ApprovalReviewer::User,
            permission_mode: voidx_config::PermissionMode::Default,
        }
    }
}
