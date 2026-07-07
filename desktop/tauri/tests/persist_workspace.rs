use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use voidx_desktop::{is_usable_workspace, load_persisted_workspace, persist_workspace};

/// Serialize tests that mutate the `HOME` env var. cargo test runs threads
/// in parallel by default; `set_var("HOME")` is process-global and not
/// thread-safe, so we guard all HOME-mutating tests with this lock.
static HOME_LOCK: Mutex<()> = Mutex::new(());

/// Isolate the persisted-workspace file under a temporary HOME so the test
/// never touches the real `~/.voidx/desktop-workspace`.
struct TempHome {
    original_home: Option<String>,
    // Kept alive so the temp dir is only cleaned up when TempHome drops.
    #[allow(dead_code)]
    dir: tempfile::TempDir,
}

impl TempHome {
    fn new() -> Self {
        let original_home = std::env::var("HOME").ok();
        let dir = tempfile::tempdir().unwrap();
        // SAFETY: tests are single-threaded; we restore HOME on drop.
        // Setting HOME is the only reliable way to redirect workspace_state_path.
        unsafe {
            std::env::set_var("HOME", dir.path());
        }
        Self { original_home, dir }
    }
}

impl Drop for TempHome {
    fn drop(&mut self) {
        // SAFETY: single-threaded test context.
        unsafe {
            match &self.original_home {
                Some(h) => std::env::set_var("HOME", h),
                None => std::env::remove_var("HOME"),
            }
        }
    }
}

#[test]
fn persist_then_load_roundtrips() {
    let _lock = HOME_LOCK.lock().unwrap();
    let _guard = TempHome::new();

    let parent = tempfile::tempdir().unwrap();
    let workspace = parent.path().join("my-ws");
    fs::create_dir(&workspace).unwrap();

    persist_workspace(&workspace);

    let loaded = load_persisted_workspace();
    assert_eq!(loaded, Some(workspace));
}

#[test]
fn load_returns_none_when_no_state_file() {
    let _lock = HOME_LOCK.lock().unwrap();
    let _guard = TempHome::new();
    assert_eq!(load_persisted_workspace(), None);
}

#[test]
fn load_returns_none_when_workspace_missing() {
    let _lock = HOME_LOCK.lock().unwrap();
    let _guard = TempHome::new();

    // Persist a path that does not exist on disk.
    let ghost = PathBuf::from("/nonexistent/voidx-ghost-67890");
    persist_workspace(&ghost);

    // load_persisted_workspace validates via is_usable_workspace, so it should
    // reject the persisted ghost path.
    assert_eq!(load_persisted_workspace(), None);
    // Sanity: the ghost path is indeed not usable.
    assert!(!is_usable_workspace(&ghost));
}
