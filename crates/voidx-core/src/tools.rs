//! Python-wrapped tool registry and tool result.

use crate::error::to_py_err;
use pyo3::prelude::*;
use std::sync::{Arc, Mutex};
use voidx_tools::base::ToolContext;
use voidx_tools::registry::ToolRegistry;

#[pyclass(name = "ToolResult")]
#[derive(Debug, Clone)]
pub struct PyToolResult {
    #[pyo3(get)]
    pub title: String,
    #[pyo3(get)]
    pub output: String,
    #[pyo3(get)]
    pub metadata: Option<String>,
    #[pyo3(get)]
    pub diff: Option<String>,
}

#[pymethods]
impl PyToolResult {
    fn __repr__(&self) -> String {
        format!(
            "ToolResult(title='{}', output='{}')",
            self.title,
            &self.output[..self.output.len().min(60)]
        )
    }
}

impl From<voidx_tools::base::ToolResult> for PyToolResult {
    fn from(r: voidx_tools::base::ToolResult) -> Self {
        PyToolResult {
            title: r.title,
            output: r.output,
            metadata: Some(serde_json::to_string(&r.metadata).unwrap_or_default()),
            diff: r.diff,
        }
    }
}

#[pyclass(name = "ToolRegistry")]
pub struct PyToolRegistry {
    inner: Arc<Mutex<ToolRegistry>>,
}

#[pymethods]
impl PyToolRegistry {
    #[new]
    fn new() -> Self {
        PyToolRegistry {
            inner: Arc::new(Mutex::new(ToolRegistry::new())),
        }
    }

    /// List all tool ids.
    fn ids(&self) -> PyResult<Vec<String>> {
        let reg = self.inner.lock().map_err(|e| to_py_err(e))?;
        Ok(reg.ids().iter().map(|s| s.to_string()).collect())
    }

    /// Execute a tool by id.
    fn execute(&self, tool_id: &str, args_json: &str, workspace: &str) -> PyResult<PyToolResult> {
        let args: serde_json::Value =
            serde_json::from_str(args_json).map_err(|e| to_py_err(e))?;

        let ctx = ToolContext {
            workspace: std::path::PathBuf::from(workspace),
            session_id: "python-bridge".to_string(),
            agent: "orchestrator".to_string(),
            ..Default::default()
        };

        let reg = self.inner.lock().map_err(|e| to_py_err(e))?;
        let rt = tokio::runtime::Runtime::new().map_err(|e| to_py_err(e))?;
        let result = rt
            .block_on(reg.execute(tool_id, args, &ctx))
            .map_err(|e| to_py_err(e))?;

        Ok(PyToolResult::from(result))
    }
}
