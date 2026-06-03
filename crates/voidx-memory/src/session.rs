//! Session management — create, list, update sessions.
//!
//! Ported from `src/voidx/memory/session.rs`.

use crate::error::MemoryError;
use crate::store::SessionStore;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInfo {
    pub id: String,
    pub workspace: String,
    pub model_provider: Option<String>,
    pub model_name: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionSummary {
    pub id: String,
    pub workspace: String,
    pub created_at: String,
    pub updated_at: String,
    pub message_count: u64,
}

impl SessionStore {
    pub fn create_session(
        &self,
        workspace: &Path,
        provider: Option<&str>,
        model: Option<&str>,
    ) -> Result<SessionInfo, MemoryError> {
        let id = uuid_v4();
        let now = Utc::now().to_rfc3339();

        self.db().execute(
            "INSERT INTO sessions (id, workspace, model_provider, model_name, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5)",
            rusqlite::params![id, workspace.to_string_lossy(), provider, model, now],
        )?;

        Ok(SessionInfo {
            id,
            workspace: workspace.to_string_lossy().into_owned(),
            model_provider: provider.map(|s| s.to_string()),
            model_name: model.map(|s| s.to_string()),
            created_at: now.clone(),
            updated_at: now,
        })
    }

    pub fn get_session(&self, session_id: &str) -> Result<SessionInfo, MemoryError> {
        self.db()
            .query_row(
                "SELECT id, workspace, model_provider, model_name, created_at, updated_at
                 FROM sessions WHERE id = ?1",
                rusqlite::params![session_id],
                |row| {
                    Ok(SessionInfo {
                        id: row.get(0)?,
                        workspace: row.get(1)?,
                        model_provider: row.get(2)?,
                        model_name: row.get(3)?,
                        created_at: row.get(4)?,
                        updated_at: row.get(5)?,
                    })
                },
            )
            .map_err(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => {
                    MemoryError::SessionNotFound(session_id.to_string())
                }
                other => MemoryError::Sqlite(other),
            })
    }

    pub fn list_sessions(&self) -> Result<Vec<SessionSummary>, MemoryError> {
        let mut stmt = self.db().prepare(
            "SELECT s.id, s.workspace, s.created_at, s.updated_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) as msg_count
             FROM sessions s ORDER BY s.updated_at DESC",
        )?;

        let rows = stmt.query_map([], |row| {
            Ok(SessionSummary {
                id: row.get(0)?,
                workspace: row.get(1)?,
                created_at: row.get(2)?,
                updated_at: row.get(3)?,
                message_count: row.get(4)?,
            })
        })?;

        let mut sessions = Vec::new();
        for row in rows {
            sessions.push(row?);
        }
        Ok(sessions)
    }

    pub fn touch_session(&self, session_id: &str) -> Result<(), MemoryError> {
        let now = Utc::now().to_rfc3339();
        self.db().execute(
            "UPDATE sessions SET updated_at = ?1 WHERE id = ?2",
            rusqlite::params![now, session_id],
        )?;
        Ok(())
    }
}

fn uuid_v4() -> String {
    // Simple UUID v4 generation without external crate
    let random_bytes: Vec<u8> = (0..16).map(|_| fast_random()).collect();
    let mut result = String::with_capacity(36);
    for (i, b) in random_bytes.iter().enumerate() {
        if i == 4 || i == 6 || i == 8 || i == 10 {
            result.push('-');
        }
        // Set version to 4 and variant bits
        let byte = match i {
            6 => (b & 0x0f) | 0x40,
            8 => (b & 0x3f) | 0x80,
            _ => *b,
        };
        result.push_str(&format!("{:02x}", byte));
    }
    result
}

fn fast_random() -> u8 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    ((now.as_nanos() ^ 0xDEAD_BEEF) & 0xFF) as u8
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn setup_store() -> SessionStore {
        let dir = tempdir().unwrap();
        let store = SessionStore::open(&dir.path().join("test.db")).unwrap();
        store.migrate().unwrap();
        store
    }

    #[test]
    fn test_create_and_get_session() {
        let store = setup_store();
        let session = store
            .create_session(Path::new("/tmp/test"), Some("anthropic"), Some("claude-haiku-4-5"))
            .unwrap();
        assert!(!session.id.is_empty());

        let fetched = store.get_session(&session.id).unwrap();
        assert_eq!(fetched.workspace, "/tmp/test");
        assert_eq!(fetched.model_provider.as_deref(), Some("anthropic"));
    }

    #[test]
    fn test_list_sessions() {
        let store = setup_store();
        store.create_session(Path::new("/tmp/a"), None, None).unwrap();
        store.create_session(Path::new("/tmp/b"), None, None).unwrap();

        let list = store.list_sessions().unwrap();
        assert_eq!(list.len(), 2);
    }

    #[test]
    fn test_session_not_found() {
        let store = setup_store();
        let err = store.get_session("nonexistent").unwrap_err();
        assert!(err.to_string().contains("not found"));
    }
}
