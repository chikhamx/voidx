//! Configuration system — typed, JSON-backed, no .env restrictions.
//!
//! Ported from `src/voidx/config.py`.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use strum_macros::{Display, EnumString};

// ── Enums ──────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Display, EnumString)]
#[serde(rename_all = "kebab-case")]
#[strum(serialize_all = "kebab-case")]
pub enum SandboxMode {
    /// All write/edit/bash/lsp_format tools are denied.
    ReadOnly,
    /// Only writes inside the workspace (+ extra_paths) are allowed.
    WorkspaceWrite,
    /// No filesystem restrictions.
    DangerFullAccess,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Display, EnumString)]
#[serde(rename_all = "kebab-case")]
#[strum(serialize_all = "kebab-case")]
pub enum ApprovalPolicy {
    /// Write/edit/write-capable bash/implement agent tools ask.
    Untrusted,
    /// Auto-allow non-bash ask tools, then report failures.
    OnFailure,
    /// Auto-allow; only ask when the agent explicitly requests approval.
    OnRequest,
    /// Full auto — no human-in-the-loop.
    Never,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Display, EnumString, Default)]
#[serde(rename_all = "kebab-case")]
#[strum(serialize_all = "kebab-case")]
pub enum ApprovalReviewer {
    #[default]
    User,
    AutoReview,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Display, EnumString)]
#[serde(rename_all = "kebab-case")]
#[strum(serialize_all = "kebab-case")]
pub enum PermissionMode {
    Default,
    ReadOnly,
    AcceptEdits,
    AutoReview,
    FullAccess,
    Custom,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Display, EnumString, Default)]
#[serde(rename_all = "kebab-case")]
#[strum(serialize_all = "kebab-case")]
pub enum CodeIde {
    #[default]
    Auto,
    Trae,
    Cursor,
    Code,
    Windsurf,
    Zed,
    Sublime,
    Jetbrains,
    Ghostty,
    System,
}

// ── Config structs ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelConfig {
    pub provider: String,
    pub model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
    #[serde(default = "default_temperature")]
    pub temperature: f64,
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
}

fn default_temperature() -> f64 {
    0.7
}

fn default_max_tokens() -> u32 {
    8192
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub workspace: PathBuf,
    pub model: ModelConfig,
    #[serde(default = "default_sandbox_mode")]
    pub sandbox_mode: SandboxMode,
    #[serde(default)]
    pub sandbox_workspace_write: bool,
    #[serde(default = "default_approval_policy")]
    pub approval_policy: ApprovalPolicy,
    #[serde(default)]
    pub approval_reviewer: ApprovalReviewer,
    #[serde(default = "default_permission_mode")]
    pub permission_mode: PermissionMode,
    #[serde(default)]
    pub sandbox_extra_paths: Vec<PathBuf>,
    #[serde(default)]
    pub code_ide: CodeIde,
}

fn default_sandbox_mode() -> SandboxMode {
    SandboxMode::WorkspaceWrite
}

fn default_approval_policy() -> ApprovalPolicy {
    ApprovalPolicy::Untrusted
}

fn default_permission_mode() -> PermissionMode {
    PermissionMode::Default
}

impl Default for Config {
    fn default() -> Self {
        Self {
            workspace: PathBuf::from("."),
            model: ModelConfig::default(),
            sandbox_mode: SandboxMode::WorkspaceWrite,
            sandbox_workspace_write: false,
            approval_policy: ApprovalPolicy::Untrusted,
            approval_reviewer: ApprovalReviewer::User,
            permission_mode: PermissionMode::Default,
            sandbox_extra_paths: Vec::new(),
            code_ide: CodeIde::Auto,
        }
    }
}

impl Default for ModelConfig {
    fn default() -> Self {
        Self {
            provider: "anthropic".to_string(),
            model: "claude-haiku-4-5".to_string(),
            protocol: None,
            base_url: None,
            temperature: 0.7,
            max_tokens: 8192,
            reasoning_effort: None,
        }
    }
}

// ── Permission mode defaults ───────────────────────────────────────────────

impl PermissionMode {
    /// Return the (SandboxMode, ApprovalPolicy) defaults for this mode.
    pub fn defaults(&self) -> (SandboxMode, ApprovalPolicy) {
        match self {
            PermissionMode::Default => (SandboxMode::WorkspaceWrite, ApprovalPolicy::Untrusted),
            PermissionMode::ReadOnly => (SandboxMode::ReadOnly, ApprovalPolicy::Untrusted),
            PermissionMode::AcceptEdits => (SandboxMode::WorkspaceWrite, ApprovalPolicy::Untrusted),
            PermissionMode::AutoReview => (SandboxMode::WorkspaceWrite, ApprovalPolicy::Untrusted),
            PermissionMode::FullAccess => (SandboxMode::DangerFullAccess, ApprovalPolicy::Never),
            PermissionMode::Custom => (SandboxMode::WorkspaceWrite, ApprovalPolicy::Untrusted),
        }
    }
}

// ── Settings file paths ────────────────────────────────────────────────────

pub const SETTINGS_FILE: &str = ".voidx/settings.json";
pub const SKILLS_STATE_FILE: &str = ".voidx/skills.json";
pub const LEGACY_SETTINGS_FILE: &str = "voidx.json";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sandbox_mode_serde_roundtrip() {
        let json = r#""workspace-write""#;
        let mode: SandboxMode = serde_json::from_str(json).unwrap();
        assert_eq!(mode, SandboxMode::WorkspaceWrite);
        let out = serde_json::to_string(&mode).unwrap();
        assert_eq!(out, r#""workspace-write""#);
    }

    #[test]
    fn test_permission_mode_defaults() {
        let (sandbox, approval) = PermissionMode::FullAccess.defaults();
        assert_eq!(sandbox, SandboxMode::DangerFullAccess);
        assert_eq!(approval, ApprovalPolicy::Never);
    }

    #[test]
    fn test_model_config_defaults() {
        let json = r#"{"provider":"anthropic","model":"claude-haiku-4-5"}"#;
        let cfg: ModelConfig = serde_json::from_str(json).unwrap();
        assert_eq!(cfg.temperature, 0.7);
        assert_eq!(cfg.max_tokens, 8192);
        assert!(cfg.base_url.is_none());
    }
}
