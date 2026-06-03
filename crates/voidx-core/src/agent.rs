//! Python-wrapped voidx agent — the main entry point for Python callers.
//!
//! This wraps `VoidXAgent` and exposes it as a Python class that
//! replaces `VoidXGraph` in Python's main.py.

use crate::config::PyConfig;
use crate::error::to_py_err;
use crate::tools::PyToolResult;
use pyo3::prelude::*;
use std::sync::{Arc, Mutex};
use voidx_agent::run_loop;
use voidx_agent::VoidXAgent;
use voidx_llm::create_client;
use voidx_permission::PermissionEngine;

/// Python-callable agent that replaces VoidXGraph.
#[pyclass(name = "RustAgent")]
pub struct PyRustAgent {
    config: voidx_config::Config,
    api_key: String,
    client: Option<Arc<dyn voidx_llm::ChatClient>>,
    memory: Option<Arc<Mutex<voidx_memory::SessionStore>>>,
}

#[pymethods]
impl PyRustAgent {
    #[new]
    fn new(config: PyConfig, api_key: String) -> PyResult<Self> {
        Ok(PyRustAgent {
            config: voidx_config::Config::from(config),
            api_key,
            client: None,
            memory: None,
        })
    }

    /// Initialize the agent: build LLM client, open session store.
    fn initialize(&mut self) -> PyResult<()> {
        let client = create_client(&self.config.model, &self.api_key)
            .map_err(|e| to_py_err(e))?;

        let store = voidx_memory::SessionStore::open(
            &self.config.workspace.join(".voidx/sessions.db"),
        )
        .map_err(|e| to_py_err(e))?;
        store.migrate().map_err(|e| to_py_err(e))?;

        self.client = Some(client);
        self.memory = Some(Arc::new(Mutex::new(store)));
        Ok(())
    }

    /// Run the agent on a user message. Returns the assistant's response.
    fn run(&self, user_message: &str, session_id: &str) -> PyResult<PyRunResult> {
        let client = self.require_client()?;
        let memory = self.require_memory()?;

        let permission = PermissionEngine::new(
            self.config.sandbox_mode,
            self.config.sandbox_workspace_write,
            self.config.approval_policy,
            vec![],
        );

        let agent = VoidXAgent::new(
            self.config.clone(),
            Arc::clone(client),
            Arc::clone(memory),
            permission,
        );

        let mut state = voidx_agent::state::AgentState::new(user_message);

        let rt = tokio::runtime::Runtime::new().map_err(|e| to_py_err(e))?;
        let result = rt.block_on(run_loop::run(&agent, &mut state, session_id))
            .map_err(|e| to_py_err(e))?;

        let last_message = result
            .messages
            .iter()
            .rev()
            .find_map(|m| match m {
                voidx_llm::ChatMessage::Assistant { content, .. } => Some(content.clone()),
                _ => None,
            })
            .unwrap_or_default();

        Ok(PyRunResult {
            output: last_message,
            steps: result.steps,
            compaction_applied: result.compaction_applied,
            message_count: result.messages.len() as u32,
        })
    }

    /// Quick text-only run.
    fn run_text(&self, user_message: &str) -> PyResult<String> {
        let result = self.run(user_message, "default")?;
        Ok(result.output)
    }

    // ── Static helpers ──────────────────────────────────────────────────

    /// List available models for a provider.
    #[staticmethod]
    fn list_models(provider: &str) -> Vec<String> {
        voidx_llm::catalog::list_models(provider)
    }

    /// Return a list of known providers.
    #[staticmethod]
    fn providers() -> Vec<String> {
        voidx_llm::catalog::providers()
            .iter()
            .map(|s| s.to_string())
            .collect()
    }

    /// Execute a single tool call (for testing/debugging from Python).
    fn execute_tool(
        &self,
        tool_id: &str,
        args_json: &str,
        workspace: &str,
    ) -> PyResult<PyToolResult> {
        let args: serde_json::Value =
            serde_json::from_str(args_json).map_err(|e| to_py_err(e))?;

        let ctx = voidx_tools::base::ToolContext {
            workspace: std::path::PathBuf::from(workspace),
            session_id: "python-debug".to_string(),
            agent: "orchestrator".to_string(),
            ..Default::default()
        };

        let registry = voidx_tools::registry::ToolRegistry::new();
        let rt = tokio::runtime::Runtime::new().map_err(|e| to_py_err(e))?;
        let result = rt
            .block_on(registry.execute(tool_id, args, &ctx))
            .map_err(|e| to_py_err(e))?;

        Ok(PyToolResult::from(result))
    }

}

// ── Private helpers (not exposed to Python) ─────────────────────────────

impl PyRustAgent {
    fn require_client(&self) -> PyResult<&Arc<dyn voidx_llm::ChatClient>> {
        self.client
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Agent not initialized — call initialize() first"))
    }

    fn require_memory(&self) -> PyResult<&Arc<Mutex<voidx_memory::SessionStore>>> {
        self.memory
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Agent not initialized — call initialize() first"))
    }
}

/// Result from a single agent run.
#[pyclass(name = "RunResult")]
#[derive(Debug, Clone)]
pub struct PyRunResult {
    #[pyo3(get)]
    pub output: String,
    #[pyo3(get)]
    pub steps: u32,
    #[pyo3(get)]
    pub compaction_applied: bool,
    #[pyo3(get)]
    pub message_count: u32,
}

#[pymethods]
impl PyRunResult {
    fn __repr__(&self) -> String {
        format!(
            "RunResult(steps={}, messages={}, compaction={})",
            self.steps, self.message_count, self.compaction_applied
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_list_models() {
        let models = PyRustAgent::list_models("anthropic");
        assert!(models.contains(&"claude-haiku-4-5".to_string()));
    }

    #[test]
    fn test_providers() {
        let providers = PyRustAgent::providers();
        assert!(providers.contains(&"anthropic".to_string()));
    }
}
