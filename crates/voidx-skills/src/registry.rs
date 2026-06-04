//! Local skill discovery from SKILL.md files.
//!
//! Ported from `src/voidx/skills/registry.py`.
//!
//! Search order: bundled → global → project. Later sources override
//! earlier sources with the same skill name (case-insensitive).

use crate::schema::{SkillDefinition, SkillMeta, SkillScope};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const SKILL_FILENAME: &str = "SKILL.md";

/// Normalize a skill name for case-insensitive comparison.
pub fn normalize_skill_name(name: &str) -> String {
    name.trim().to_lowercase()
}

/// Error during skill file parsing.
#[derive(Debug, thiserror::Error)]
pub enum SkillParseError {
    #[error("Skill at {path} has no name")]
    NoName { path: PathBuf },
    #[error("Unclosed frontmatter in {path}")]
    UnclosedFrontmatter { path: PathBuf },
    #[error("IO error reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

/// Discovers bundled, global, and project skills.
pub struct SkillRegistry {
    workspace: PathBuf,
    bundled_dir: PathBuf,
    global_dir: PathBuf,
    project_dir: PathBuf,
    cache: Option<Vec<SkillDefinition>>,
}

impl SkillRegistry {
    pub fn new(workspace: &Path) -> Self {
        let workspace = workspace.to_path_buf().canonicalize().unwrap_or_else(|_| workspace.to_path_buf());
        let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/"));

        Self {
            workspace: workspace.clone(),
            bundled_dir: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("bundled")
                .join("superpowers"),
            global_dir: home.join(".voidx").join("skills"),
            project_dir: workspace.join(".voidx").join("skills"),
            cache: None,
        }
    }

    /// Create with custom directories (for testing).
    pub fn with_dirs(
        workspace: PathBuf,
        bundled_dir: PathBuf,
        global_dir: PathBuf,
        project_dir: PathBuf,
    ) -> Self {
        Self {
            workspace,
            bundled_dir,
            global_dir,
            project_dir,
            cache: None,
        }
    }

    /// Discover all skills. Results are cached until `invalidate()` is called.
    pub fn discover(&mut self) -> &[SkillDefinition] {
        if self.cache.is_none() {
            let mut skills: HashMap<String, SkillDefinition> = HashMap::new();

            for (scope, root) in [
                (SkillScope::Bundled, &self.bundled_dir),
                (SkillScope::Global, &self.global_dir),
                (SkillScope::Project, &self.project_dir),
            ] {
                for skill in discover_root(root, scope) {
                    skills.insert(normalize_skill_name(skill.name()), skill);
                }
            }

            let mut list: Vec<SkillDefinition> = skills.into_values().collect();
            list.sort_by(|a, b| a.name().cmp(b.name()));
            self.cache = Some(list);
        }
        self.cache.as_ref().unwrap()
    }

    /// Invalidate the cache, forcing re-discovery on next `discover()`.
    pub fn invalidate(&mut self) {
        self.cache = None;
    }

    /// Get a specific skill by name.
    pub fn get(&mut self, name: &str) -> Option<&SkillDefinition> {
        let target = normalize_skill_name(name);
        self.discover();
        self.cache
            .as_ref()
            .unwrap()
            .iter()
            .find(|s| normalize_skill_name(s.name()) == target)
    }
}

/// Discover skills under a root directory.
fn discover_root(root: &Path, scope: SkillScope) -> Vec<SkillDefinition> {
    if !root.exists() || !root.is_dir() {
        return Vec::new();
    }

    let mut skills = Vec::new();

    // Look for */SKILL.md pattern
    if let Ok(entries) = std::fs::read_dir(root) {
        let mut dirs: Vec<_> = entries.filter_map(|e| e.ok()).collect();
        dirs.sort_by_key(|e| e.file_name());

        for entry in dirs {
            if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let skill_file = entry.path().join(SKILL_FILENAME);
            if skill_file.exists() {
                match parse_skill_file(&skill_file, scope) {
                    Ok(skill) => skills.push(skill),
                    Err(e) => {
                        tracing::warn!("Failed to parse skill file {:?}: {}", skill_file, e);
                    }
                }
            }
        }
    }

    skills
}

/// Parse a SKILL.md file with optional YAML frontmatter.
pub fn parse_skill_file(path: &Path, scope: SkillScope) -> Result<SkillDefinition, SkillParseError> {
    let text = std::fs::read_to_string(path).map_err(|e| SkillParseError::Io {
        path: path.to_path_buf(),
        source: e,
    })?;

    let (fields, body) = split_frontmatter(&text);

    let name = fields
        .get("name")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| {
            path.parent()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default()
        });

    if name.is_empty() {
        return Err(SkillParseError::NoName {
            path: path.to_path_buf(),
        });
    }

    let description = fields
        .get("description")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let enabled = fields
        .get("enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);

    let triggers = fields
        .get("triggers")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.trim().to_string()))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();

    let meta = SkillMeta {
        name,
        description,
        enabled,
        triggers,
        scope,
    };

    Ok(SkillDefinition {
        meta,
        path: path.to_path_buf(),
        body: body.trim().to_string(),
    })
}

/// Split a Markdown file into YAML frontmatter fields and body text.
fn split_frontmatter(text: &str) -> (serde_json::Map<String, serde_json::Value>, String) {
    let lines: Vec<&str> = text.lines().collect();

    if lines.is_empty() || lines[0].trim() != "---" {
        return (serde_json::Map::new(), text.to_string());
    }

    // Find closing ---
    let end_index = lines[1..]
        .iter()
        .position(|line| line.trim() == "---");

    let (frontmatter_text, body) = match end_index {
        Some(i) => {
            let end = i + 1; // 1-based offset from the opening ---
            let fm = lines[1..end].join("\n");
            let body = lines[end + 1..].join("\n");
            (fm, body)
        }
        None => return (serde_json::Map::new(), text.to_string()),
    };

    // Parse YAML-like frontmatter as simple key: value pairs
    let fields = parse_simple_frontmatter(&frontmatter_text);
    (fields, body)
}

/// Parse simple YAML frontmatter (key: value pairs, lists).
/// We use a minimal parser rather than pulling in a full YAML crate.
fn parse_simple_frontmatter(text: &str) -> serde_json::Map<String, serde_json::Value> {
    let mut map = serde_json::Map::new();
    let mut current_list_key: Option<String> = None;
    let mut current_list: Vec<serde_json::Value> = Vec::new();

    for line in text.lines() {
        let trimmed = line.trim();

        // List item: "- value"
        if trimmed.starts_with("- ") && current_list_key.is_some() {
            let value = trimmed[2..].trim().trim_matches('"').to_string();
            current_list.push(serde_json::Value::String(value));
            continue;
        }

        // Flush any pending list
        if let Some(key) = current_list_key.take() {
            if !current_list.is_empty() {
                map.insert(key, serde_json::Value::Array(current_list));
            }
            current_list = Vec::new();
        }

        // Key: value
        if let Some(colon_pos) = trimmed.find(':') {
            let key = trimmed[..colon_pos].trim().to_string();
            let value = trimmed[colon_pos + 1..].trim().to_string();

            if value.is_empty() {
                // Start of a list
                current_list_key = Some(key);
                current_list = Vec::new();
            } else {
                // Simple key: value
                let json_val = if value == "true" {
                    serde_json::Value::Bool(true)
                } else if value == "false" {
                    serde_json::Value::Bool(false)
                } else {
                    serde_json::Value::String(value.trim_matches('"').to_string())
                };
                map.insert(key, json_val);
            }
        }
    }

    // Flush final list
    if let Some(key) = current_list_key.take() {
        if !current_list.is_empty() {
            map.insert(key, serde_json::Value::Array(current_list));
        }
    }

    map
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_skill_name() {
        assert_eq!(normalize_skill_name("  TDD  "), "tdd");
        assert_eq!(normalize_skill_name("Systematic-Debugging"), "systematic-debugging");
    }

    #[test]
    fn test_parse_simple_frontmatter() {
        let text = "name: test-skill\ndescription: A test\nenabled: true";
        let fields = parse_simple_frontmatter(text);
        assert_eq!(fields["name"], "test-skill");
        assert_eq!(fields["description"], "A test");
        assert_eq!(fields["enabled"], true);
    }

    #[test]
    fn test_parse_frontmatter_with_list() {
        let text = "name: test\ntriggers:\n- debug\n- fix";
        let fields = parse_simple_frontmatter(text);
        let triggers = fields["triggers"].as_array().unwrap();
        assert_eq!(triggers.len(), 2);
        assert_eq!(triggers[0], "debug");
        assert_eq!(triggers[1], "fix");
    }

    #[test]
    fn test_split_frontmatter_no_frontmatter() {
        let text = "Just a body\nNo frontmatter";
        let (fields, body) = split_frontmatter(text);
        assert!(fields.is_empty());
        assert_eq!(body, text);
    }

    #[test]
    fn test_split_frontmatter_with_frontmatter() {
        let text = "---\nname: my-skill\n---\nBody content here";
        let (fields, body) = split_frontmatter(text);
        assert_eq!(fields["name"], "my-skill");
        assert!(body.contains("Body content here"));
    }
}
