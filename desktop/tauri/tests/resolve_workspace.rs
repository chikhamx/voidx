use std::fs;
use std::sync::Mutex;

use voidx_desktop::{is_usable_workspace, resolve_workspace};

/// Serialize tests that mutate process-global env vars (`HOME`,
/// `VOIDX_WORKSPACE`); cargo test runs threads in parallel.
static ENV_LOCK: Mutex<()> = Mutex::new(());

/// Redirect HOME to a temp dir and clear VOIDX_WORKSPACE so workspace
/// resolution is exercised without env shortcuts.
struct TempEnv {
    original_home: Option<String>,
    original_ws: Option<String>,
    // Kept alive so the temp dir outlives the test.
    #[allow(dead_code)]
    dir: tempfile::TempDir,
}

impl TempEnv {
    fn new() -> Self {
        let original_home = std::env::var("HOME").ok();
        let original_ws = std::env::var("VOIDX_WORKSPACE").ok();
        let dir = tempfile::tempdir().unwrap();
        // SAFETY: guarded by ENV_LOCK; restored on drop.
        unsafe {
            std::env::set_var("HOME", dir.path());
            std::env::remove_var("VOIDX_WORKSPACE");
        }
        Self {
            original_home,
            original_ws,
            dir,
        }
    }
}

impl Drop for TempEnv {
    fn drop(&mut self) {
        // SAFETY: guarded by ENV_LOCK.
        unsafe {
            match &self.original_home {
                Some(h) => std::env::set_var("HOME", h),
                None => std::env::remove_var("HOME"),
            }
            match &self.original_ws {
                Some(w) => std::env::set_var("VOIDX_WORKSPACE", w),
                None => std::env::remove_var("VOIDX_WORKSPACE"),
            }
        }
    }
}

#[test]
fn resolve_workspace_ignores_persisted_state_file() {
    let _lock = ENV_LOCK.lock().unwrap();
    let guard = TempEnv::new();

    // Simulate a leftover ~/.voidx/desktop-workspace pointing at a real dir.
    let persisted = tempfile::tempdir().unwrap();
    let state_dir = guard.dir.path().join(".voidx");
    fs::create_dir_all(&state_dir).unwrap();
    fs::write(
        state_dir.join("desktop-workspace"),
        persisted.path().to_string_lossy().as_bytes(),
    )
    .unwrap();

    // Workspace lives only in memory now; resolution must not consult the file.
    assert_ne!(resolve_workspace(), persisted.path());
}

#[test]
fn app_bundle_directories_are_not_workspaces() {
    let root = tempfile::tempdir().unwrap();
    let app_dir = root.path().join("voidx.app");
    let executable_dir = app_dir.join("Contents/MacOS");
    fs::create_dir_all(&executable_dir).unwrap();

    assert!(!is_usable_workspace(&app_dir));
    assert!(!is_usable_workspace(&executable_dir));
}
