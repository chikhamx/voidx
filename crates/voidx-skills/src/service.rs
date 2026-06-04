//! Runtime skill selection and instruction rendering.
//!
//! Ported from `src/voidx/skills/service.py`.
//!
//! Selects relevant skills based on user text, agent role, task intent,
//! and interaction mode. Renders skill instructions for injection into
//! the system prompt.

use crate::policy::{workflow_skill_activations, workflow_skill_sort_key};
use crate::registry::{normalize_skill_name, SkillRegistry};
use crate::schema::{SkillDefinition, SkillMatch, SkillSelectionConfig};

/// Regex for explicit skill references like $skill-name.
const EXPLICIT_REF_RE: &str = r"(?<![\w.-])\$([A-Za-z0-9_.-]+)";

/// Runtime skill selection and instruction rendering.
pub struct SkillService {
    registry: SkillRegistry,
    selection: SkillSelectionConfig,
}

impl SkillService {
    pub fn new(registry: SkillRegistry, selection: SkillSelectionConfig) -> Self {
        Self { registry, selection }
    }

    /// Create with default (empty) selection config.
    pub fn with_defaults(registry: SkillRegistry) -> Self {
        Self {
            registry,
            selection: SkillSelectionConfig::default(),
        }
    }

    /// List all discovered skills.
    pub fn list_skills(&mut self) -> &[SkillDefinition] {
        self.registry.discover()
    }

    /// Get a specific skill by name.
    pub fn get(&mut self, name: &str) -> Option<&SkillDefinition> {
        self.registry.get(name)
    }

    /// List only enabled skills.
    pub fn enabled_skills(&mut self) -> Vec<&SkillDefinition> {
        self.list_skills();
        self.registry
            .discover()
            .iter()
            .filter(|s| self.is_enabled(s))
            .collect()
    }

    /// Check if a skill is enabled according to selection config.
    pub fn is_enabled(&self, skill: &SkillDefinition) -> bool {
        let name = normalize_skill_name(skill.name());
        let disabled: Vec<String> = self
            .selection
            .disabled
            .iter()
            .map(|s| normalize_skill_name(s))
            .collect();
        let enabled: Vec<String> = self
            .selection
            .enabled
            .iter()
            .map(|s| normalize_skill_name(s))
            .collect();

        if disabled.contains(&name) {
            return false;
        }
        if enabled.contains(&name) {
            return true;
        }
        skill.meta.enabled
    }

    /// Select relevant skills for the current context.
    pub fn select(
        &mut self,
        user_text: &str,
        agent: &str,
        task_intent: &str,
        interaction_mode: &str,
        limit: usize,
    ) -> Vec<SkillMatch> {
        let text = user_text.trim();
        let has_context = !agent.is_empty() || !task_intent.is_empty() || !interaction_mode.is_empty();
        if text.is_empty() && !has_context {
            return Vec::new();
        }

        let skills = self.enabled_skills();
        let skills_by_name: std::collections::HashMap<String, &SkillDefinition> = skills
            .iter()
            .map(|s| (normalize_skill_name(s.name()), *s))
            .collect();

        let explicit = explicit_refs(text);
        let mut matches: Vec<SkillMatch> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

        let mut add_match = |skill: Option<&SkillDefinition>, reason: &str, matches: &mut Vec<SkillMatch>, seen: &mut std::collections::HashSet<String>| {
            if let Some(skill) = skill {
                let name = normalize_skill_name(skill.name());
                if !seen.contains(&name) {
                    seen.insert(name);
                    matches.push(SkillMatch {
                        skill: skill.clone(),
                        reason: reason.to_string(),
                    });
                }
            }
        };

        // If user explicitly referenced skills ($name), prioritize those
        if !explicit.is_empty() {
            for name in &explicit {
                let sorted_name = name.clone();
                if let Some(skill) = skills_by_name.get(&sorted_name) {
                    add_match(Some(skill), "explicit", &mut matches, &mut seen);
                }
            }
            for activation in workflow_skill_activations(text, agent, task_intent, interaction_mode) {
                if let Some(skill) = skills_by_name.get(&normalize_skill_name(&activation.name)) {
                    add_match(Some(skill), &activation.reason, &mut matches, &mut seen);
                }
            }
            matches.truncate(limit);
            return matches;
        }

        // Workflow skill activations
        for activation in workflow_skill_activations(text, agent, task_intent, interaction_mode) {
            if let Some(skill) = skills_by_name.get(&normalize_skill_name(&activation.name)) {
                add_match(Some(skill), &activation.reason, &mut matches, &mut seen);
            }
        }

        // Text-based matching against triggers
        let lowered = text.to_lowercase();
        let mut text_matches: Vec<SkillMatch> = Vec::new();
        for skill in &skills {
            if seen.contains(&normalize_skill_name(skill.name())) {
                continue;
            }
            if let Some(reason) = match_reason(skill, &lowered) {
                text_matches.push(SkillMatch {
                    skill: (*skill).clone(),
                    reason,
                });
            }
        }
        text_matches.sort_by_key(|m| workflow_skill_sort_key(m.name()));
        matches.extend(text_matches);

        matches.truncate(limit);
        matches
    }

    /// Generate activation summaries for system prompt injection.
    pub fn activation_summaries(
        &mut self,
        user_text: &str,
        agent: &str,
        task_intent: &str,
        interaction_mode: &str,
    ) -> Vec<String> {
        let matches = self.select(user_text, agent, task_intent, interaction_mode, 5);
        matches
            .iter()
            .map(|m| format!("- **{}**: {}", m.skill.name(), m.reason))
            .collect()
    }

    /// Render full skill instructions for system prompt injection.
    pub fn render_instructions(
        &mut self,
        user_text: &str,
        agent: &str,
        task_intent: &str,
        interaction_mode: &str,
    ) -> String {
        let matches = self.select(user_text, agent, task_intent, interaction_mode, 5);
        if matches.is_empty() {
            return String::new();
        }

        let mut parts: Vec<String> = Vec::new();
        for m in &matches {
            if !m.skill.body.is_empty() {
                parts.push(format!("## Skill: {}\n\n{}", m.skill.name(), m.skill.body));
            }
        }

        if parts.is_empty() {
            return String::new();
        }

        format!("## Active Skills\n\n{}", parts.join("\n\n"))
    }
}

/// Extract explicit skill references ($name) from user text.
fn explicit_refs(text: &str) -> Vec<String> {
    let re = regex::Regex::new(EXPLICIT_REF_RE).unwrap();
    let mut refs: Vec<String> = re
        .captures_iter(text)
        .filter_map(|cap| cap.get(1).map(|m| m.as_str().to_string()))
        .collect();
    refs.sort_by_key(|name| workflow_skill_sort_key(name));
    refs.dedup();
    refs
}

/// Check if a skill matches the user text via triggers.
fn match_reason(skill: &SkillDefinition, lowered_text: &str) -> Option<String> {
    // Check triggers
    for trigger in &skill.meta.triggers {
        if lowered_text.contains(&trigger.to_lowercase()) {
            return Some(format!("trigger: {trigger}"));
        }
    }

    // Check if skill name appears in text
    let name_lower = skill.name().to_lowercase();
    if lowered_text.contains(&name_lower) {
        return Some("name match".to_string());
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{SkillMeta, SkillScope};
    use std::path::PathBuf;

    fn make_skill(name: &str, triggers: Vec<&str>) -> SkillDefinition {
        SkillDefinition {
            meta: SkillMeta {
                name: name.to_string(),
                description: String::new(),
                enabled: true,
                triggers: triggers.into_iter().map(|s| s.to_string()).collect(),
                scope: SkillScope::Bundled,
            },
            path: PathBuf::from(format!("/tmp/skills/{name}/SKILL.md")),
            body: format!("Instructions for {name}"),
        }
    }

    #[test]
    fn test_explicit_refs() {
        let refs = explicit_refs("use $systematic-debugging and $writing-plans");
        assert_eq!(refs.len(), 2);
        assert!(refs.contains(&"systematic-debugging".to_string()));
        assert!(refs.contains(&"writing-plans".to_string()));
    }

    #[test]
    fn test_match_reason_trigger() {
        let skill = make_skill("tdd", vec!["test-driven", "tdd"]);
        let reason = match_reason(&skill, "let's use test-driven development");
        assert!(reason.is_some());
        assert!(reason.unwrap().contains("trigger"));
    }

    #[test]
    fn test_match_reason_name() {
        let skill = make_skill("systematic-debugging", vec![]);
        let reason = match_reason(&skill, "use systematic-debugging");
        assert!(reason.is_some());
        assert_eq!(reason.unwrap(), "name match");
    }
}
