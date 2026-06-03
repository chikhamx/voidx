//! Memory layer — SQLite-backed sessions, transcripts, context frames.
//!
//! Ported from `src/voidx/memory/`.

pub mod context_frames;
pub mod error;
pub mod model_profiles;
pub mod runtime_state;
pub mod session;
pub mod store;
pub mod transcript;

pub use error::MemoryError;
pub use session::{SessionInfo, SessionSummary};
pub use store::SessionStore;

use std::sync::Arc;

/// Convenience: open a store and return it wrapped for sharing.
pub async fn open(path: &std::path::Path) -> Result<Arc<SessionStore>, MemoryError> {
    let store = SessionStore::open(path)?;
    store.migrate()?;
    Ok(Arc::new(store))
}
