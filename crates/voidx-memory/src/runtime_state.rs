//! Runtime state snapshots — persist agent state for recovery.
//!
//! Ported from `src/voidx/memory/runtime_state.rs`.

use crate::error::MemoryError;
use crate::store::SessionStore;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeState {
    pub session_id: String,
    pub state: serde_json::Value,
    pub snapshot_at: String,
}

impl SessionStore {
    /// Save a snapshot of the current runtime state.
    pub fn save_runtime_state(
        &self,
        session_id: &str,
        state: &serde_json::Value,
    ) -> Result<(), MemoryError> {
        let state_json = serde_json::to_string(state)?;
        self.db().execute(
            "INSERT INTO runtime_states (session_id, state_json) VALUES (?1, ?2)",
            rusqlite::params![session_id, state_json],
        )?;
        Ok(())
    }

    /// Load the most recent runtime state snapshot.
    pub fn load_latest_runtime_state(
        &self,
        session_id: &str,
    ) -> Result<Option<RuntimeState>, MemoryError> {
        let result = self.db().query_row(
            "SELECT session_id, state_json, snapshot_at
             FROM runtime_states WHERE session_id = ?1
             ORDER BY snapshot_at DESC LIMIT 1",
            rusqlite::params![session_id],
            |row| {
                let state_str: String = row.get(1)?;
                Ok(RuntimeState {
                    session_id: row.get(0)?,
                    state: serde_json::from_str(&state_str).unwrap_or_default(),
                    snapshot_at: row.get(2)?,
                })
            },
        );

        match result {
            Ok(state) => Ok(Some(state)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(MemoryError::Sqlite(e)),
        }
    }

    /// Delete old runtime states, keeping only the most recent N.
    pub fn prune_runtime_states(
        &self,
        session_id: &str,
        keep: usize,
    ) -> Result<(), MemoryError> {
        self.db().execute(
            "DELETE FROM runtime_states WHERE id NOT IN (
                SELECT id FROM runtime_states WHERE session_id = ?1
                ORDER BY snapshot_at DESC LIMIT ?2
            ) AND session_id = ?1",
            rusqlite::params![session_id, keep as i64],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_save_and_load_state() {
        let dir = tempdir().unwrap();
        let store = SessionStore::open(&dir.path().join("test.db")).unwrap();
        store.migrate().unwrap();

        // Need a session row for FK constraint
        store.create_session(std::path::Path::new("/test"), None, None).unwrap();
        let sid = &store.list_sessions().unwrap()[0].id;

        let state = serde_json::json!({"step": 5, "mode": "auto"});
        store.save_runtime_state(sid, &state).unwrap();

        let loaded = store
            .load_latest_runtime_state(sid)
            .unwrap()
            .unwrap();
        assert_eq!(loaded.state["step"], 5);
    }
}
