//! Parse and materialize user file attachments.
//!
//! Ported from `src/voidx/agent/attachments.py`.
//! Handles: @path resolution, image base64 encoding, directory tree expansion.

use base64::Engine;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

// pathdiff is used for computing relative paths between directories.
// If unavailable, we fall back to strip_prefix.

/// Image extensions that get base64-encoded as image parts.
pub const IMAGE_EXTENSIONS: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".webp"];

/// Max size for text attachments (bytes).
pub const MAX_TEXT_ATTACHMENT_BYTES: u64 = 200_000;
/// Max size for image attachments (bytes).
pub const MAX_IMAGE_ATTACHMENT_BYTES: u64 = 5_000_000;
/// Max items in a directory listing.
pub const MAX_DIR_LISTING_ITEMS: usize = 500;

/// Directories to skip when building directory trees.
pub const DIR_TREE_SKIP: &[&str] = &[
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "target",
];

/// An attachment parsed from user input.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Attachment {
    pub path: PathBuf,
    pub rel_path: String,
    pub kind: AttachmentKind,
    pub mime_type: String,
    pub size: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AttachmentKind {
    File,
    Image,
    Dir,
}

/// The processed user message payload.
#[derive(Debug, Clone)]
pub struct UserMessagePayload {
    /// Original text with @path tokens.
    pub raw_text: String,
    /// Text with @path tokens replaced by section markers.
    pub clean_text: String,
    /// Text suitable for display (with [image-x] markers).
    pub display_text: String,
    /// Short title for UI.
    pub title_text: String,
    /// Final content — either a plain string or structured content with image parts.
    pub content: MessageContent,
    /// Parsed attachments.
    pub attachments: Vec<Attachment>,
    /// Warnings generated during parsing.
    pub warnings: Vec<String>,
}

/// Message content can be plain text or structured (with image parts).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum MessageContent {
    Text(String),
    Structured(Vec<ContentPart>),
}

/// A single part of structured content.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ContentPart {
    #[serde(rename = "text")]
    Text { text: String },
    #[serde(rename = "image_url")]
    Image { image_url: ImageUrl },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageUrl {
    pub url: String,
}

/// Parse user input and resolve all @path attachments.
pub fn build_user_message_payload(
    user_text: &str,
    workspace: &Path,
) -> UserMessagePayload {
    let workspace = match workspace.canonicalize() {
        Ok(p) => p,
        Err(_) => workspace.to_path_buf(),
    };

    let tokens = extract_attachment_tokens(user_text);
    let mut clean_parts: Vec<String> = Vec::new();
    let mut display_parts: Vec<String> = Vec::new();
    let mut attachments: Vec<Attachment> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    let mut image_parts: Vec<ContentPart> = Vec::new();
    let mut text_sections: Vec<ContentPart> = Vec::new();

    let mut last_end = 0usize;

    for token in &tokens {
        // Add text before this token
        if token.start > last_end {
            let before = &user_text[last_end..token.start];
            if !before.is_empty() {
                clean_parts.push(before.to_string());
                display_parts.push(before.to_string());
                text_sections.push(ContentPart::Text {
                    text: before.to_string(),
                });
            }
        }

        let raw_path = &token.raw_path;
        let resolved = resolve_workspace_path(&workspace, raw_path);

        match resolved {
            None => {
                warnings.push(format!("Attachment skipped outside workspace: {raw_path}"));
                clean_parts.push(raw_path.clone());
                display_parts.push(raw_path.clone());
                text_sections.push(ContentPart::Text {
                    text: raw_path.clone(),
                });
            }
            Some(path) if !path.exists() => {
                warnings.push(format!("Attachment not found: {raw_path}"));
                clean_parts.push(raw_path.clone());
                display_parts.push(raw_path.clone());
                text_sections.push(ContentPart::Text {
                    text: raw_path.clone(),
                });
            }
            Some(path) => {
                let is_dir = path.is_dir();
                let rel_path = pathdiff::diff_paths(&path, &workspace)
                    .unwrap_or_else(|| path.clone())
                    .to_string_lossy()
                    .to_string();

                let mime_type = guess_mime_type(&path);
                let size = path.metadata().map(|m| m.len()).unwrap_or(0);

                let kind = if is_dir {
                    AttachmentKind::Dir
                } else if is_image_path(&path) {
                    AttachmentKind::Image
                } else {
                    AttachmentKind::File
                };

                let attachment = Attachment {
                    path: path.clone(),
                    rel_path: rel_path.clone(),
                    kind,
                    mime_type: mime_type.clone(),
                    size,
                };
                attachments.push(attachment);

                match kind {
                    AttachmentKind::Image => {
                        if size > MAX_IMAGE_ATTACHMENT_BYTES {
                            warnings.push(format!(
                                "Image skipped because it is too large: {rel_path}"
                            ));
                        } else {
                            let section = format!("\nAttached image: {rel_path}\n");
                            clean_parts.push(section.clone());
                            display_parts.push(format!("[image-{rel_path}]"));
                            if let Some(image_part) = encode_image(&path, &mime_type) {
                                image_parts.push(image_part);
                            }
                        }
                    }
                    AttachmentKind::Dir => {
                        let (section, warning) = directory_section(&path, &workspace);
                        if let Some(w) = warning {
                            warnings.push(w);
                        }
                        clean_parts.push(section.clone());
                        display_parts.push(format!("[dir-{rel_path}]"));
                        text_sections.push(ContentPart::Text { text: section });
                    }
                    AttachmentKind::File => {
                        if size > MAX_TEXT_ATTACHMENT_BYTES {
                            warnings.push(format!(
                                "File skipped because it is too large: {rel_path} ({size} bytes)"
                            ));
                        } else {
                            let content = std::fs::read_to_string(&path).unwrap_or_default();
                            let section = format!(
                                "\nAttached file: {rel_path}\n```\n{content}\n```\n"
                            );
                            clean_parts.push(section.clone());
                            display_parts.push(format!("[file-{rel_path}]"));
                            text_sections.push(ContentPart::Text { text: section });
                        }
                    }
                }
            }
        }

        last_end = token.end;
    }

    // Add remaining text after last token
    if last_end < user_text.len() {
        let remaining = &user_text[last_end..];
        if !remaining.is_empty() {
            clean_parts.push(remaining.to_string());
            display_parts.push(remaining.to_string());
            text_sections.push(ContentPart::Text {
                text: remaining.to_string(),
            });
        }
    }

    let clean_text = clean_parts.join("");
    let display_text = display_parts.join("");
    let title_text = make_title(&display_text);

    // Build final content
    let content = if image_parts.is_empty() {
        MessageContent::Text(clean_text.clone())
    } else {
        let mut parts: Vec<ContentPart> = text_sections;
        parts.extend(image_parts);
        MessageContent::Structured(parts)
    };

    UserMessagePayload {
        raw_text: user_text.to_string(),
        clean_text,
        display_text,
        title_text,
        content,
        attachments,
        warnings,
    }
}

// ── Attachment token extraction ──────────────────────────────────────────

/// A parsed @path token from user text.
#[derive(Debug)]
struct AttachmentToken {
    start: usize,
    end: usize,
    raw_path: String,
}

/// Extract all @path tokens from user text.
///
/// Supports: @path/to/file, @"path with spaces", [image-name]
///
/// All indices are byte offsets into `text`, safe for slicing.
fn extract_attachment_tokens(text: &str) -> Vec<AttachmentToken> {
    let mut tokens = Vec::new();
    let bytes = text.as_bytes();
    let mut i = 0usize; // byte index

    while i < bytes.len() {
        // @path or @"quoted path"
        if bytes[i] == b'@' {
            let start = i;
            i += 1;

            if i < bytes.len() && bytes[i] == b'"' {
                // Quoted path
                i += 1;
                let path_start = i;
                while i < bytes.len() && bytes[i] != b'"' {
                    i += 1;
                }
                let raw_path = text[path_start..i].to_string();
                if i < bytes.len() {
                    i += 1; // skip closing quote
                }
                tokens.push(AttachmentToken {
                    start,
                    end: i,
                    raw_path,
                });
            } else {
                // Unquoted path
                let path_start = i;
                while i < bytes.len() && !bytes[i].is_ascii_whitespace() {
                    i += 1;
                }
                let raw_path = text[path_start..i].to_string();
                if !raw_path.is_empty() {
                    tokens.push(AttachmentToken {
                        start,
                        end: i,
                        raw_path,
                    });
                }
            }
            continue;
        }

        // [image-name]
        if bytes[i] == b'[' && i + 1 < bytes.len() {
            let bracket_start = i;
            i += 1;
            if text[i..].starts_with("image-") {
                i += 6; // skip "image-"
                let name_start = i;
                while i < bytes.len() && bytes[i] != b']' {
                    i += 1;
                }
                if i < bytes.len() {
                    let name = text[name_start..i].to_string();
                    i += 1; // skip ]
                    tokens.push(AttachmentToken {
                        start: bracket_start,
                        end: i,
                        raw_path: format!(":image:{name}"),
                    });
                    continue;
                }
            }
            i = bracket_start + 1;
            continue;
        }

        i += 1;
    }

    tokens
}

// ── Path resolution ──────────────────────────────────────────────────────

/// Resolve a path relative to the workspace, ensuring it stays inside.
fn resolve_workspace_path(workspace: &Path, raw_path: &str) -> Option<PathBuf> {
    // Handle :image: prefix
    if let Some(stem) = raw_path.strip_prefix(":image:") {
        // Try to find the image in the workspace
        for ext in IMAGE_EXTENSIONS {
            let candidate = workspace.join(format!("{stem}{ext}"));
            if candidate.exists() {
                return Some(candidate);
            }
        }
        // Try the stem as a full path
        let candidate = workspace.join(stem);
        if candidate.exists() {
            return Some(candidate);
        }
        return None;
    }

    let resolved = workspace.join(raw_path);

    // Verify it's inside workspace
    match resolved.canonicalize() {
        Ok(canonical) => {
            if let Ok(ws_canonical) = workspace.canonicalize() {
                if canonical.starts_with(&ws_canonical) {
                    return Some(canonical);
                }
            }
            // Fallback: if canonicalize fails for workspace, allow it
            Some(canonical)
        }
        Err(_) => {
            // Path doesn't exist yet — still allow if it's under workspace
            if resolved.starts_with(workspace) {
                Some(resolved)
            } else {
                None
            }
        }
    }
}

/// Check if a path has an image extension.
fn is_image_path(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| IMAGE_EXTENSIONS.contains(&e.to_lowercase().as_str()))
        .unwrap_or(false)
}

/// Guess MIME type from file extension.
fn guess_mime_type(path: &Path) -> String {
    match path.extension().and_then(|e| e.to_str()).map(|e| e.to_lowercase()) {
        Some(e) => match e.as_str() {
            "png" => "image/png".to_string(),
            "jpg" | "jpeg" => "image/jpeg".to_string(),
            "gif" => "image/gif".to_string(),
            "webp" => "image/webp".to_string(),
            "svg" => "image/svg+xml".to_string(),
            "json" => "application/json".to_string(),
            "xml" => "application/xml".to_string(),
            "html" => "text/html".to_string(),
            "md" => "text/markdown".to_string(),
            "txt" => "text/plain".to_string(),
            "rs" => "text/rust".to_string(),
            "py" => "text/x-python".to_string(),
            "ts" | "tsx" => "text/typescript".to_string(),
            "js" | "jsx" => "text/javascript".to_string(),
            _ => "application/octet-stream".to_string(),
        },
        None => "application/octet-stream".to_string(),
    }
}

/// Encode an image file as a base64 data URL.
fn encode_image(path: &Path, mime_type: &str) -> Option<ContentPart> {
    let data = std::fs::read(path).ok()?;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&data);
    Some(ContentPart::Image {
        image_url: ImageUrl {
            url: format!("data:{mime_type};base64,{b64}"),
        },
    })
}

/// Build a directory tree section for an attached directory.
fn directory_section(dir_path: &Path, workspace: &Path) -> (String, Option<String>) {
    let skip: HashSet<&str> = DIR_TREE_SKIP.iter().copied().collect();
    let mut entries = Vec::new();
    let mut warning = None;

    // Simple directory listing
    collect_dir_entries(dir_path, &skip, &mut entries, 0, &mut warning);

    let rel = pathdiff::diff_paths(dir_path, workspace)
        .unwrap_or_else(|| dir_path.to_path_buf());
    let rel_str = rel.to_string_lossy();

    let mut section = format!("\nAttached directory: {rel_str}\n```\n");
    for entry in &entries {
        section.push_str(entry);
        section.push('\n');
    }
    section.push_str("```\n");

    (section, warning)
}

/// Recursively collect directory entries for display.
fn collect_dir_entries(
    dir: &Path,
    skip: &HashSet<&str>,
    entries: &mut Vec<String>,
    depth: usize,
    warning: &mut Option<String>,
) {
    if entries.len() >= MAX_DIR_LISTING_ITEMS {
        if warning.is_none() {
            *warning = Some(format!(
                "Directory listing truncated at {} items",
                MAX_DIR_LISTING_ITEMS
            ));
        }
        return;
    }

    let mut subdirs: Vec<PathBuf> = Vec::new();
    let mut files: Vec<String> = Vec::new();

    if let Ok(rd) = std::fs::read_dir(dir) {
        for entry in rd.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if skip.contains(name.as_str()) || name.starts_with('.') {
                continue;
            }

            let path = entry.path();
            if path.is_dir() {
                subdirs.push(path);
            } else {
                files.push(name);
            }
        }
    }

    // Sort for deterministic output
    subdirs.sort();
    files.sort();

    let indent = "  ".repeat(depth);

    for subdir in &subdirs {
        let name = subdir.file_name().unwrap_or_default().to_string_lossy();
        entries.push(format!("{indent}{name}/"));
        collect_dir_entries(subdir, skip, entries, depth + 1, warning);
    }

    for file in &files {
        entries.push(format!("{indent}{file}"));
    }
}

/// Make a short title from display text.
fn make_title(text: &str) -> String {
    let first_line = text.lines().next().unwrap_or(text);
    if first_line.len() > 80 {
        format!("{}...", &first_line[..77])
    } else {
        first_line.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_tokens_simple() {
        let tokens = extract_attachment_tokens("look at @src/main.rs please");
        assert_eq!(tokens.len(), 1);
        assert_eq!(tokens[0].raw_path, "src/main.rs");
    }

    #[test]
    fn test_extract_tokens_quoted() {
        let tokens = extract_attachment_tokens("look at @\"path with spaces.rs\"");
        assert_eq!(tokens.len(), 1);
        assert_eq!(tokens[0].raw_path, "path with spaces.rs");
    }

    #[test]
    fn test_extract_tokens_image() {
        let tokens = extract_attachment_tokens("see [image-photo]");
        assert_eq!(tokens.len(), 1);
        assert_eq!(tokens[0].raw_path, ":image:photo");
    }

    #[test]
    fn test_extract_tokens_multiple() {
        let tokens = extract_attachment_tokens("@a.txt and @b.rs");
        assert_eq!(tokens.len(), 2);
        assert_eq!(tokens[0].raw_path, "a.txt");
        assert_eq!(tokens[1].raw_path, "b.rs");
    }

    #[test]
    fn test_is_image_path() {
        assert!(is_image_path(Path::new("photo.png")));
        assert!(is_image_path(Path::new("photo.JPG")));
        assert!(!is_image_path(Path::new("code.rs")));
    }

    #[test]
    fn test_guess_mime_type() {
        assert_eq!(guess_mime_type(Path::new("f.png")), "image/png");
        assert_eq!(guess_mime_type(Path::new("f.rs")), "text/rust");
        assert_eq!(guess_mime_type(Path::new("f.json")), "application/json");
    }

    #[test]
    fn test_sanitize_empty_input() {
        let payload = build_user_message_payload("hello world", Path::new("/tmp"));
        assert_eq!(payload.raw_text, "hello world");
        assert!(payload.attachments.is_empty());
        assert!(payload.warnings.is_empty());
    }

    #[test]
    fn test_make_title() {
        assert_eq!(make_title("short"), "short");
        let long = "x".repeat(100);
        let title = make_title(&long);
        assert!(title.ends_with("..."));
        assert_eq!(title.len(), 80);
    }
}
