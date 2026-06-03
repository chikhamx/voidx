//! Model profiles — lightweight key-value store for user model configs.
//!
//! Ported from `src/voidx/memory/model_profiles.rs`.

use crate::error::MemoryError;
use crate::store::SessionStore;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelProfile {
    pub name: String,
    pub provider: String,
    pub model: String,
    pub protocol: Option<String>,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub temperature: Option<f64>,
    pub max_tokens: Option<u32>,
}

impl SessionStore {
    /// Ensure the profiles table exists (lightweight migration, idempotent).
    pub fn ensure_profiles_table(&self) -> Result<(), MemoryError> {
        self.db().execute_batch(
            "CREATE TABLE IF NOT EXISTS model_profiles (
                name TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                protocol TEXT,
                base_url TEXT,
                api_key TEXT,
                temperature REAL,
                max_tokens INTEGER
            );",
        )?;
        Ok(())
    }

    /// Save or update a model profile.
    pub fn save_profile(&self, profile: &ModelProfile) -> Result<(), MemoryError> {
        self.ensure_profiles_table()?;
        self.db().execute(
            "INSERT OR REPLACE INTO model_profiles
                (name, provider, model, protocol, base_url, api_key, temperature, max_tokens)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                profile.name,
                profile.provider,
                profile.model,
                profile.protocol,
                profile.base_url,
                profile.api_key,
                profile.temperature,
                profile.max_tokens,
            ],
        )?;
        Ok(())
    }

    /// Load a profile by name.
    pub fn load_profile(&self, name: &str) -> Result<Option<ModelProfile>, MemoryError> {
        self.ensure_profiles_table()?;
        let result = self.db().query_row(
            "SELECT name, provider, model, protocol, base_url, api_key, temperature, max_tokens
             FROM model_profiles WHERE name = ?1",
            rusqlite::params![name],
            |row| {
                Ok(ModelProfile {
                    name: row.get(0)?,
                    provider: row.get(1)?,
                    model: row.get(2)?,
                    protocol: row.get(3)?,
                    base_url: row.get(4)?,
                    api_key: row.get(5)?,
                    temperature: row.get(6)?,
                    max_tokens: row.get(7)?,
                })
            },
        );
        match result {
            Ok(profile) => Ok(Some(profile)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(MemoryError::Sqlite(e)),
        }
    }

    /// Delete a profile by name.
    pub fn delete_profile(&self, name: &str) -> Result<(), MemoryError> {
        self.ensure_profiles_table()?;
        self.db().execute(
            "DELETE FROM model_profiles WHERE name = ?1",
            rusqlite::params![name],
        )?;
        Ok(())
    }

    /// List all profile names.
    pub fn list_profiles(&self) -> Result<Vec<String>, MemoryError> {
        self.ensure_profiles_table()?;
        let mut stmt = self
            .db()
            .prepare("SELECT name FROM model_profiles ORDER BY name")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        let mut names = Vec::new();
        for row in rows {
            names.push(row?);
        }
        Ok(names)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_save_and_load_profile() {
        let dir = tempdir().unwrap();
        let store = SessionStore::open(&dir.path().join("test.db")).unwrap();

        let profile = ModelProfile {
            name: "default".to_string(),
            provider: "anthropic".to_string(),
            model: "claude-opus-4-8".to_string(),
            protocol: None,
            base_url: None,
            api_key: None,
            temperature: Some(0.7),
            max_tokens: Some(8192),
        };

        store.save_profile(&profile).unwrap();

        let loaded = store.load_profile("default").unwrap().unwrap();
        assert_eq!(loaded.provider, "anthropic");
        assert_eq!(loaded.model, "claude-opus-4-8");
        assert_eq!(loaded.temperature, Some(0.7));
    }

    #[test]
    fn test_delete_profile() {
        let dir = tempdir().unwrap();
        let store = SessionStore::open(&dir.path().join("test.db")).unwrap();

        let profile = ModelProfile {
            name: "temp".to_string(),
            provider: "openai".to_string(),
            model: "gpt-5.4-mini".to_string(),
            protocol: None,
            base_url: None,
            api_key: None,
            temperature: None,
            max_tokens: None,
        };

        store.save_profile(&profile).unwrap();
        assert_eq!(store.list_profiles().unwrap().len(), 1);

        store.delete_profile("temp").unwrap();
        assert_eq!(store.list_profiles().unwrap().len(), 0);
    }
}
