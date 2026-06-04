//! Instruction service — AGENTS.md / CLAUDE.md project memory.
//!
//! Ported from `src/voidx/llm/instruction.py`.
//!
//! Resolution order:
//!   1. ~/.voidx/AGENTS.md    (global)
//!   2. ~/.claude/CLAUDE.md   (compat, only if ~/.voidx/ not found)
//!   3. Workspace walk-up AGENTS.md (first match wins, like opencode)
//!   4. Config URL instructions

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

const INSTRUCTION_FILES: &[&str] = &["AGENTS.md", "CLAUDE.md"];

/// Runtime context for skill activation.
#[derive(Debug, Clone)]
pub struct SkillRuntimeContext {
    pub instructions: Vec<String>,
    pub active: Vec<String>,
}

/// Manages project instructions injection into system prompt.
///
/// Tracks per-message claims to avoid duplicate instruction injection.
pub struct InstructionService {
    workspace: PathBuf,
    global_dir: PathBuf,
    claude_dir: PathBuf,
    /// Per-message claims: which instruction files have been attached
    /// to which assistant message to avoid duplicates.
    claims: HashMap<String, HashSet<String>>,
    /// Cached system paths (refreshed each turn).
    system_paths: Vec<String>,
}

impl InstructionService {
    pub fn new(workspace: &Path) -> Self {
        let workspace = workspace
            .canonicalize()
            .unwrap_or_else(|_| workspace.to_path_buf());
        let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/"));

        Self {
            workspace,
            global_dir: home.join(".voidx"),
            claude_dir: home.join(".claude"),
            claims: HashMap::new(),
            system_paths: Vec::new(),
        }
    }

    /// Clear claims for a message (called when message is removed).
    pub fn clear(&mut self, message_id: &str) {
        self.claims.remove(message_id);
    }

    /// Discover all instruction file paths. Refreshed each call.
    pub fn system_paths(&mut self) -> Vec<String> {
        let mut paths: Vec<String> = Vec::new();

        // 1. Global: ~/.voidx/AGENTS.md first, then ~/.claude/CLAUDE.md
        let global_voidx = self.global_dir.join("AGENTS.md");
        if global_voidx.exists() {
            if let Ok(canonical) = global_voidx.canonicalize() {
                paths.push(canonical.to_string_lossy().to_string());
            }
        } else {
            let claude_global = self.claude_dir.join("CLAUDE.md");
            if claude_global.exists() {
                if let Ok(canonical) = claude_global.canonicalize() {
                    paths.push(canonical.to_string_lossy().to_string());
                }
            }
        }

        // 2. Project: walk-up from workspace, first match wins
        let mut current = self.workspace.clone();
        let root = {
            let mut r = current.as_path();
            while let Some(parent) = r.parent() {
                r = parent;
            }
            r.to_path_buf()
        };
        while current != root {
            for filename in INSTRUCTION_FILES {
                let candidate = current.join(filename);
                if candidate.exists() {
                    if let Ok(canonical) = candidate.canonicalize() {
                        let path_str = canonical.to_string_lossy().to_string();
                        if !paths.contains(&path_str) {
                            paths.push(path_str);
                            break;
                        }
                    }
                }
            }
            // Move up one level
            match current.parent() {
                Some(parent) => current = parent.to_path_buf(),
                None => break,
            }
        }

        self.system_paths = paths.clone();
        paths
    }

    /// Read all system instruction files.
    /// Returns list of "Instructions from: <path>\n<content>" strings.
    pub fn system(&mut self) -> Vec<String> {
        let paths = self.system_paths();
        read_all(&paths)
    }

    /// Get skill runtime context for the current turn.
    pub fn skill_context_for(
        &mut self,
        _user_text: &str,
        _agent: &str,
        _task_intent: &str,
        _interaction_mode: &str,
    ) -> SkillRuntimeContext {
        // Note: This is a simplified version without the full SkillService
        // integration. The full version would use voidx-skills crate.
        let mut instructions: Vec<String> = Vec::new();
        let active: Vec<String> = Vec::new();

        // Add system instructions
        let system = self.system();
        instructions.extend(system);

        SkillRuntimeContext {
            instructions,
            active,
        }
    }

    /// Build the full system instruction block for injection.
    pub fn build_instruction_block(&mut self) -> String {
        let system = self.system();
        if system.is_empty() {
            return String::new();
        }
        system.join("\n\n")
    }

    /// Check if a path has already been claimed for a message.
    pub fn is_claimed(&self, message_id: &str, path: &str) -> bool {
        self.claims
            .get(message_id)
            .map(|set| set.contains(path))
            .unwrap_or(false)
    }

    /// Claim a path for a message.
    pub fn claim(&mut self, message_id: &str, path: &str) {
        self.claims
            .entry(message_id.to_string())
            .or_default()
            .insert(path.to_string());
    }

    /// Remove claims for messages that no longer exist.
    pub fn prune_claims(&mut self, active_message_ids: &[&str]) {
        let active: HashSet<String> = active_message_ids
            .iter()
            .map(|s| s.to_string())
            .collect();
        self.claims.retain(|id, _| active.contains(id));
    }
}

/// Read all instruction files and format them.
fn read_all(paths: &[String]) -> Vec<String> {
    let mut results = Vec::new();
    for path_str in paths {
        let path = PathBuf::from(path_str);
        match std::fs::read_to_string(&path) {
            Ok(content) => {
                if !content.trim().is_empty() {
                    results.push(format!("Instructions from: {path_str}\n{content}"));
                }
            }
            Err(e) => {
                tracing::warn!("Failed to read instruction file {path_str}: {e}");
            }
        }
    }
    results
}

/// Fetch instructions from a URL (async, for config URL instructions).
pub async fn fetch_url_instructions(url: &str) -> Result<String, String> {
    let client = reqwest::Client::new();
    let response = client
        .get(url)
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("Failed to fetch {url}: {e}"))?;

    if !response.status().is_success() {
        return Err(format!("HTTP {} from {url}", response.status()));
    }

    response
        .text()
        .await
        .map_err(|e| format!("Failed to read response from {url}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_system_paths_empty_when_no_files() {
        let dir = tempfile::tempdir().unwrap();
        let mut service = InstructionService::new(dir.path());
        let paths = service.system_paths();
        // Should not panic, may find global files if they exist
        assert!(paths.iter().all(|p| p.contains("AGENTS.md") || p.contains("CLAUDE.md")));
    }

    #[test]
    fn test_claim_tracking() {
        let dir = tempfile::tempdir().unwrap();
        let mut service = InstructionService::new(dir.path());

        assert!(!service.is_claimed("msg1", "/path/to/AGENTS.md"));
        service.claim("msg1", "/path/to/AGENTS.md");
        assert!(service.is_claimed("msg1", "/path/to/AGENTS.md"));
        assert!(!service.is_claimed("msg2", "/path/to/AGENTS.md"));
    }

    #[test]
    fn test_prune_claims() {
        let dir = tempfile::tempdir().unwrap();
        let mut service = InstructionService::new(dir.path());

        service.claim("msg1", "/path/to/AGENTS.md");
        service.claim("msg2", "/path/to/AGENTS.md");
        service.prune_claims(&["msg1"]);
        assert!(service.is_claimed("msg1", "/path/to/AGENTS.md"));
        assert!(!service.is_claimed("msg2", "/path/to/AGENTS.md"));
    }
}
