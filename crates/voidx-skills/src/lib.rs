//! Skill discovery, selection, and instruction rendering.
//!
//! Ported from `src/voidx/skills/` (schema.py, policy.py, registry.py, service.py).
//!
//! Skills are Markdown files (SKILL.md) that provide workflow instructions
//! to the agent. They are discovered from bundled, global, and project
//! directories, then selected based on user text, agent role, and task intent.

pub mod policy;
pub mod registry;
pub mod schema;
pub mod service;

pub use schema::{SkillDefinition, SkillMatch, SkillMeta, SkillSelectionConfig};
