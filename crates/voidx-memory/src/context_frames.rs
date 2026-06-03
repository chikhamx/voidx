//! Context frames — snapshot the full message list sent to the LLM.
//!
//! Ported from `src/voidx/memory/context_frames.rs`.

use crate::error::MemoryError;
use crate::store::SessionStore;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContextFrame {
    pub id: i64,
    pub session_id: String,
    pub user_message_id: Option<i64>,
    pub frame_kind: String,
    pub agent_role: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub messages: serde_json::Value,
    pub token_estimate: i64,
    pub created_at: String,
    pub metadata: serde_json::Value,
}

impl SessionStore {
    /// Save a snapshot of the messages sent to the LLM.
    pub fn save_context_frame(
        &self,
        session_id: &str,
        user_message_id: Option<i64>,
        frame_kind: &str,
        agent_role: Option<&str>,
        provider: Option<&str>,
        model: Option<&str>,
        messages: &serde_json::Value,
        token_estimate: i64,
        metadata: &serde_json::Value,
    ) -> Result<i64, MemoryError> {
        let messages_json = serde_json::to_string(messages)?;
        let metadata_str = serde_json::to_string(metadata)?;

        self.db().execute(
            "INSERT INTO context_frames
                (session_id, user_message_id, frame_kind, agent_role,
                 provider, model, messages_json, token_estimate, metadata)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                session_id,
                user_message_id,
                frame_kind,
                agent_role,
                provider,
                model,
                messages_json,
                token_estimate,
                metadata_str,
            ],
        )?;

        Ok(self.db().last_insert_rowid())
    }

    /// Get recent context frames for a session.
    pub fn get_context_frames(
        &self,
        session_id: &str,
        limit: usize,
    ) -> Result<Vec<ContextFrame>, MemoryError> {
        let mut stmt = self.db().prepare(
            "SELECT id, session_id, user_message_id, frame_kind, agent_role,
                    provider, model, messages_json, token_estimate, created_at, metadata
             FROM context_frames WHERE session_id = ?1
             ORDER BY id DESC LIMIT ?2",
        )?;

        let rows = stmt.query_map(rusqlite::params![session_id, limit as i64], |row| {
            let messages_str: String = row.get(7)?;
            let meta_str: String = row.get(10)?;
            Ok(ContextFrame {
                id: row.get(0)?,
                session_id: row.get(1)?,
                user_message_id: row.get(2)?,
                frame_kind: row.get(3)?,
                agent_role: row.get(4)?,
                provider: row.get(5)?,
                model: row.get(6)?,
                messages: serde_json::from_str(&messages_str).unwrap_or_default(),
                token_estimate: row.get(8)?,
                created_at: row.get(9)?,
                metadata: serde_json::from_str(&meta_str).unwrap_or_default(),
            })
        })?;

        let mut frames = Vec::new();
        for row in rows {
            frames.push(row?);
        }
        Ok(frames)
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
    fn test_save_and_retrieve_frame() {
        let store = setup_store();
        store.create_session(std::path::Path::new("/test"), None, None).unwrap();
        let sid = &store.list_sessions().unwrap()[0].id;

        let messages = serde_json::json!([{"role": "user", "content": "hi"}]);
        let meta = serde_json::json!({"step": 1});

        let id = store
            .save_context_frame(
                sid,
                None,
                "main",
                Some("orchestrator"),
                Some("anthropic"),
                Some("claude-haiku-4-5"),
                &messages,
                10,
                &meta,
            )
            .unwrap();

        assert!(id > 0);

        let frames = store.get_context_frames(sid, 10).unwrap();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].frame_kind, "main");
    }
}
