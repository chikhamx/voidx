//! Python-wrapped session store.

use crate::error::to_py_err;
use pyo3::prelude::*;
use std::sync::Mutex;
use voidx_memory::SessionStore;

#[pyclass(name = "SessionInfo")]
#[derive(Debug, Clone)]
pub struct PySessionInfo {
    #[pyo3(get)]
    pub id: String,
    #[pyo3(get)]
    pub workspace: String,
    #[pyo3(get)]
    pub created_at: String,
    #[pyo3(get)]
    pub updated_at: String,
}

#[pyclass(name = "SessionStore")]
pub struct PySessionStore {
    inner: Mutex<SessionStore>,
}

#[pymethods]
impl PySessionStore {
    /// Open a session store at the given path.
    #[staticmethod]
    fn open(path: &str) -> PyResult<Self> {
        let store = SessionStore::open(std::path::Path::new(path)).map_err(|e| to_py_err(e))?;
        store.migrate().map_err(|e| to_py_err(e))?;
        Ok(PySessionStore {
            inner: Mutex::new(store),
        })
    }

    /// List all sessions.
    fn list_sessions(&self) -> PyResult<Vec<PySessionInfo>> {
        let store = self.inner.lock().map_err(|e| to_py_err(e))?;
        let sessions = store.list_sessions().map_err(|e| to_py_err(e))?;
        Ok(sessions
            .into_iter()
            .map(|s| PySessionInfo {
                id: s.id,
                workspace: s.workspace,
                created_at: s.created_at,
                updated_at: s.updated_at,
            })
            .collect())
    }

    /// Create a new session.
    #[pyo3(signature = (workspace, provider=None, model=None))]
    fn create_session(
        &self,
        workspace: &str,
        provider: Option<String>,
        model: Option<String>,
    ) -> PyResult<PySessionInfo> {
        let store = self.inner.lock().map_err(|e| to_py_err(e))?;
        let session = store
            .create_session(
                std::path::Path::new(workspace),
                provider.as_deref(),
                model.as_deref(),
            )
            .map_err(|e| to_py_err(e))?;

        Ok(PySessionInfo {
            id: session.id,
            workspace: session.workspace,
            created_at: session.created_at,
            updated_at: session.updated_at,
        })
    }

    /// Count messages in a session.
    fn message_count(&self, session_id: &str) -> PyResult<u64> {
        let store = self.inner.lock().map_err(|e| to_py_err(e))?;
        store.message_count(session_id).map_err(|e| to_py_err(e))
    }

    /// Add a message to a session.
    fn append_message(&self, session_id: &str, role: &str, content: &str) -> PyResult<i64> {
        let store = self.inner.lock().map_err(|e| to_py_err(e))?;
        store
            .append_message(session_id, role, content, None, None)
            .map_err(|e| to_py_err(e))
    }
}
