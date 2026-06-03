//! Transcript storage — append messages, retrieve full history.
//!
//! Ported from `src/voidx/memory/transcript.rs`.

use crate::error::MemoryError;
use crate::store::SessionStore;
use serde::{Deserialize, Serialize};

/// A single message in the transcript.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptMessage {
    pub id: i64,
    pub role: String,
    pub content: String,
    pub tool_calls: Option<serde_json::Value>,
    pub tool_call_id: Option<String>,
    pub created_at: String,
}

impl SessionStore {
    /// Append a message to the session transcript.
    pub fn append_message(
        &self,
        session_id: &str,
        role: &str,
        content: &str,
        tool_calls: Option<&serde_json::Value>,
        tool_call_id: Option<&str>,
    ) -> Result<i64, MemoryError> {
        let tool_calls_str = tool_calls
            .map(|tc| serde_json::to_string(tc))
            .transpose()?;

        self.db().execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![session_id, role, content, tool_calls_str, tool_call_id],
        )?;

        Ok(self.db().last_insert_rowid())
    }

    /// Get all messages for a session, ordered by id.
    pub fn get_transcript(
        &self,
        session_id: &str,
    ) -> Result<Vec<TranscriptMessage>, MemoryError> {
        let mut stmt = self.db().prepare(
            "SELECT id, role, content, tool_calls, tool_call_id, created_at
             FROM messages WHERE session_id = ?1 ORDER BY id ASC",
        )?;

        let rows = stmt.query_map(rusqlite::params![session_id], |row| {
            let tool_calls_str: Option<String> = row.get(3)?;
            let tool_calls: Option<serde_json::Value> = tool_calls_str
                .and_then(|s| serde_json::from_str(&s).ok());

            Ok(TranscriptMessage {
                id: row.get(0)?,
                role: row.get(1)?,
                content: row.get(2)?,
                tool_calls,
                tool_call_id: row.get(4)?,
                created_at: row.get(5)?,
            })
        })?;

        let mut messages = Vec::new();
        for row in rows {
            messages.push(row?);
        }
        Ok(messages)
    }

    /// Count messages in a session.
    pub fn message_count(&self, session_id: &str) -> Result<u64, MemoryError> {
        let count: u64 = self.db().query_row(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?1",
            rusqlite::params![session_id],
            |row| row.get(0),
        )?;
        Ok(count)
    }
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
    fn test_append_and_retrieve() {
        let store = setup_store();
        store.create_session(std::path::Path::new("/test"), None, None).unwrap();

        let sessions = store.list_sessions().unwrap();
        let sid = &sessions[0].id;

        store.append_message(sid, "user", "Hello", None, None).unwrap();
        store.append_message(sid, "assistant", "Hi there!", None, None).unwrap();

        let transcript = store.get_transcript(sid).unwrap();
        assert_eq!(transcript.len(), 2);
        assert_eq!(transcript[0].role, "user");
        assert_eq!(transcript[1].role, "assistant");
    }
}
