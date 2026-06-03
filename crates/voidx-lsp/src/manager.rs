//! LSP Manager — lazily starts and caches language servers for a workspace.
//!
//! Ported from `src/voidx/lsp/manager.py`.

use crate::client::LspClient;
use crate::detector::LspDetector;
use crate::error::LspError;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::Mutex;

/// Manages LSP server instances for a workspace.
pub struct LspManager {
    workspace: PathBuf,
    clients: Mutex<HashMap<String, Arc<Mutex<LspClient>>>>,
}

impl LspManager {
    pub fn new(workspace: &Path) -> Self {
        Self {
            workspace: workspace.to_path_buf(),
            clients: Mutex::new(HashMap::new()),
        }
    }

    /// Get or start an LSP client for a file type.
    pub async fn get_client(
        &self,
        file_path: &Path,
    ) -> Result<Option<Arc<Mutex<LspClient>>>, LspError> {
        let config = match LspDetector::detect(file_path) {
            Some(c) => c,
            None => return Ok(None),
        };

        let language = config.language.clone();
        let mut clients = self.clients.lock().await;

        if let Some(client) = clients.get(&language) {
            return Ok(Some(Arc::clone(client)));
        }

        // Start a new LSP server
        let args: Vec<&str> = config.args.iter().map(|s| s.as_str()).collect();
        let mut client = LspClient::start(&config.command, &args, &self.workspace).await?;
        client.initialize(&self.workspace).await?;

        let client = Arc::new(Mutex::new(client));
        clients.insert(language.clone(), Arc::clone(&client));

        tracing::info!("Started LSP server: {} ({})", config.command, language);
        Ok(Some(client))
    }

    /// Check whether an LSP server is available for a file type.
    pub async fn is_available(&self, file_path: &Path) -> bool {
        LspDetector::detect(file_path).is_some()
    }

    /// Get definition locations for a symbol at a position.
    pub async fn definition(
        &self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<crate::types::Location>, LspError> {
        let client = match self.get_client(file_path).await? {
            Some(c) => c,
            None => return Ok(Vec::new()),
        };
        let mut client = client.lock().await;
        client.definition(file_path, line, character).await
    }

    /// Get references for a symbol at a position.
    pub async fn references(
        &self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<crate::types::Location>, LspError> {
        let client = match self.get_client(file_path).await? {
            Some(c) => c,
            None => return Ok(Vec::new()),
        };
        let mut client = client.lock().await;
        client.references(file_path, line, character).await
    }

    /// Get symbols in a document.
    pub async fn symbols(
        &self,
        file_path: &Path,
    ) -> Result<Vec<crate::types::SymbolInformation>, LspError> {
        let client = match self.get_client(file_path).await? {
            Some(c) => c,
            None => return Ok(Vec::new()),
        };
        let mut client = client.lock().await;
        client.symbols(file_path).await
    }

    /// Format a document.
    pub async fn formatting(
        &self,
        file_path: &Path,
    ) -> Result<Vec<crate::types::TextEdit>, LspError> {
        let client = match self.get_client(file_path).await? {
            Some(c) => c,
            None => return Ok(Vec::new()),
        };
        let mut client = client.lock().await;
        client.formatting(file_path).await
    }
}
