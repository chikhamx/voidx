//! SQLite-backed session store — schema, migrations, low-level queries.
//!
//! Ported from `src/voidx/memory/store.py`.

use crate::error::MemoryError;
use rusqlite::Connection;
use std::path::Path;

pub struct SessionStore {
    db: Connection,
}

impl SessionStore {
    /// Open (or create) the session database at the given path.
    pub fn open(path: &Path) -> Result<Self, MemoryError> {
        let db = Connection::open(path)?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
        Ok(Self { db })
    }

    /// Run all migrations.
    pub fn migrate(&self) -> Result<(), MemoryError> {
        self.db.execute_batch(
            "CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                model_provider TEXT,
                model_name TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls TEXT,
                tool_call_id TEXT,
                usage_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id);

            CREATE TABLE IF NOT EXISTS context_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                user_message_id INTEGER,
                frame_kind TEXT NOT NULL DEFAULT 'main',
                agent_role TEXT,
                provider TEXT,
                model TEXT,
                messages_json TEXT NOT NULL,
                token_estimate INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_context_frames_session
                ON context_frames(session_id, id);

            CREATE TABLE IF NOT EXISTS runtime_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                state_json TEXT NOT NULL,
                snapshot_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_states_session
                ON runtime_states(session_id, snapshot_at);
            ",
        )?;
        Ok(())
    }

    /// Return a reference to the underlying SQLite connection.
    pub fn db(&self) -> &Connection {
        &self.db
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_open_and_migrate() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.db");
        let store = SessionStore::open(&path).unwrap();
        store.migrate().unwrap();

        // Verify tables exist
        let count: i64 = store
            .db()
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
