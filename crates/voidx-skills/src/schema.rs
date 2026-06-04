//! Types for local skill discovery and selection.
//!
//! Ported from `src/voidx/skills/schema.py`.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Where a skill was discovered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum SkillScope {
    #[default]
    Bundled,
    Global,
    Project,
}

impl std::fmt::Display for SkillScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SkillScope::Bundled => write!(f, "bundled"),
            SkillScope::Global => write!(f, "global"),
            SkillScope::Project => write!(f, "project"),
        }
    }
}

/// Metadata parsed from SKILL.md frontmatter.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillMeta {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub triggers: Vec<String>,
    #[serde(default)]
    pub scope: SkillScope,
}

fn default_true() -> bool {
    true
}

/// A fully parsed skill definition.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillDefinition {
    pub meta: SkillMeta,
    pub path: PathBuf,
    pub body: String,
}

impl SkillDefinition {
    pub fn name(&self) -> &str {
        &self.meta.name
    }

    pub fn source_dir(&self) -> PathBuf {
        self.path.parent().unwrap_or(&self.path).to_path_buf()
    }
}

/// User configuration for which skills are enabled/disabled.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SkillSelectionConfig {
    #[serde(default)]
    pub enabled: Vec<String>,
    #[serde(default)]
    pub disabled: Vec<String>,
}

/// A skill matched for the current context, with a reason.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillMatch {
    pub skill: SkillDefinition,
    pub reason: String,
}

impl SkillMatch {
    pub fn name(&self) -> &str {
        self.skill.name()
    }
}
