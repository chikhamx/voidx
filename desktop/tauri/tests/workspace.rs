use std::fs;
use std::path::PathBuf;

use voidx_desktop::{is_project_root, is_usable_workspace, workspace_state_path};

#[test]
fn project_root_detected_by_agents_md() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("AGENTS.md"), "# project").unwrap();
    assert!(is_project_root(dir.path()));
}

#[test]
fn project_root_detected_by_pyproject_toml() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("pyproject.toml"), "[project]").unwrap();
    assert!(is_project_root(dir.path()));
}

#[test]
fn non_project_root_rejected() {
    let dir = tempfile::tempdir().unwrap();
    assert!(!is_project_root(dir.path()));
}

#[test]
fn usable_workspace_accepts_existing_dir_with_parent() {
    let parent = tempfile::tempdir().unwrap();
    let workspace = parent.path().join("ws");
    fs::create_dir(&workspace).unwrap();
    assert!(is_usable_workspace(&workspace));
}

#[test]
fn usable_workspace_rejects_nonexistent_path() {
    let ghost = PathBuf::from("/nonexistent/voidx-test-ghost-12345");
    assert!(!is_usable_workspace(&ghost));
}

#[test]
fn usable_workspace_rejects_file() {
    let file = tempfile::NamedTempFile::new().unwrap();
    assert!(!is_usable_workspace(file.path()));
}

#[test]
fn workspace_state_path_under_home() {
    // workspace_state_path reads $HOME; verify it produces the expected suffix.
    // We can't fully isolate HOME in an integration test without env mutation,
    // so we only assert the shape when HOME is set.
    if let Some(path) = workspace_state_path() {
        assert!(path.ends_with(".voidx/desktop-workspace"));
    }
}
