//! Sandbox — filesystem boundary enforcement.
//!
//! Ported from `src/voidx/permission/sandbox.rs`.

use crate::error::PermissionError;
use voidx_config::SandboxMode;

/// Filesystem sandbox that restricts file-based tool operations.
#[derive(Debug, Clone)]
pub struct Sandbox {
    pub mode: SandboxMode,
    pub workspace_write: bool,
    pub extra_paths: Vec<std::path::PathBuf>,
}

impl Sandbox {
    pub fn new(mode: SandboxMode) -> Self {
        Self {
            mode,
            workspace_write: false,
            extra_paths: Vec::new(),
        }
    }

    pub fn with_workspace_write(mut self, enabled: bool) -> Self {
        self.workspace_write = enabled;
        self
    }

    pub fn with_extra_paths(mut self, paths: Vec<std::path::PathBuf>) -> Self {
        self.extra_paths = paths;
        self
    }

    /// Check if a write operation is allowed at the given path.
    pub fn check_write(
        &self,
        path: &std::path::Path,
        workspace: &std::path::Path,
    ) -> Result<(), PermissionError> {
        match self.mode {
            SandboxMode::DangerFullAccess => Ok(()),
            SandboxMode::ReadOnly => Err(PermissionError::SandboxViolation(
                "Write operations are blocked in read-only mode".to_string(),
            )),
            SandboxMode::WorkspaceWrite => {
                if self.workspace_write || is_within(path, workspace) {
                    return Ok(());
                }
                if self.extra_paths.iter().any(|ep| is_within(path, ep)) {
                    return Ok(());
                }
                Err(PermissionError::SandboxViolation(format!(
                    "Write outside workspace: {}",
                    path.display()
                )))
            }
        }
    }

    /// Check if a read operation is allowed at the given path.
    pub fn check_read(
        &self,
        _path: &std::path::Path,
        _workspace: &std::path::Path,
    ) -> Result<(), PermissionError> {
        // All sandbox modes allow reads within workspace
        // even ReadOnly mode allows reading
        Ok(())
    }

    /// Check if a bash command is allowed under the current sandbox.
    pub fn check_bash(&self) -> Result<(), PermissionError> {
        match self.mode {
            SandboxMode::ReadOnly => Err(PermissionError::SandboxViolation(
                "Bash is blocked in read-only mode".to_string(),
            )),
            _ => Ok(()),
        }
    }
}

fn is_within(path: &std::path::Path, base: &std::path::Path) -> bool {
    let canonical_base = std::fs::canonicalize(base).unwrap_or_else(|_| base.to_path_buf());
    let canonical_path = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    canonical_path.starts_with(&canonical_base)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_read_only_blocks_writes() {
        let sandbox = Sandbox::new(SandboxMode::ReadOnly);
        let ws = tempdir().unwrap();
        let file = ws.path().join("test.txt");

        assert!(sandbox.check_write(&file, ws.path()).is_err());
    }

    #[test]
    fn test_workspace_write_allows_inside() {
        let sandbox = Sandbox::new(SandboxMode::WorkspaceWrite).with_workspace_write(true);
        let ws = tempdir().unwrap();
        let file = ws.path().join("inside.txt");

        assert!(sandbox.check_write(&file, ws.path()).is_ok());
    }

    #[test]
    fn test_danger_full_access_allows_all() {
        let sandbox = Sandbox::new(SandboxMode::DangerFullAccess);
        let ws = tempdir().unwrap();
        let file = std::path::PathBuf::from("/etc/passwd");

        assert!(sandbox.check_write(&file, ws.path()).is_ok());
    }
}
