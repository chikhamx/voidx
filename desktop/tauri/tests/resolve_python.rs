use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use voidx_desktop::{default_install_dir, resolve_python};

/// Serialize tests that mutate process-global env vars. cargo test runs
/// threads in parallel; `set_var` is not thread-safe.
static ENV_LOCK: Mutex<()> = Mutex::new(());

/// Guard that saves and restores a set of env vars on drop.
struct EnvGuard {
    vars: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn new(vars: &'static [&'static str]) -> Self {
        let saved = vars
            .iter()
            .map(|&name| {
                let val = std::env::var(name).ok();
                // SAFETY: single-threaded via ENV_LOCK
                unsafe { std::env::remove_var(name) };
                (name, val)
            })
            .collect();
        Self { vars: saved }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, val) in &self.vars {
            // SAFETY: single-threaded via ENV_LOCK
            unsafe {
                match val {
                    Some(v) => std::env::set_var(name, v),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

/// Create a fake venv layout under `dir` matching the platform's venv path.
fn fake_venv(dir: &std::path::Path) -> PathBuf {
    let venv = dir.join("venv");
    let bin = if cfg!(windows) {
        venv.join("Scripts")
    } else {
        venv.join("bin")
    };
    fs::create_dir_all(&bin).unwrap();
    let exe = bin.join(if cfg!(windows) { "python.exe" } else { "python" });
    fs::write(&exe, b"#!/bin/sh\n").unwrap();
    exe
}

#[test]
fn voidx_python_override_takes_precedence() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["VOIDX_PYTHON", "VOIDX_HOME"]);

    let tmp = tempfile::tempdir().unwrap();
    fake_venv(tmp.path());

    // VOIDX_HOME points to a valid venv, but VOIDX_PYTHON should win.
    unsafe {
        std::env::set_var("VOIDX_HOME", tmp.path());
        std::env::set_var("VOIDX_PYTHON", "/custom/python/from/override");
    }

    assert_eq!(
        resolve_python(),
        Some(PathBuf::from("/custom/python/from/override"))
    );
}

#[test]
fn voidx_home_resolves_venv() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["VOIDX_PYTHON", "VOIDX_HOME"]);

    let tmp = tempfile::tempdir().unwrap();
    let exe = fake_venv(tmp.path());

    unsafe {
        std::env::set_var("VOIDX_HOME", tmp.path());
    }

    assert_eq!(resolve_python(), Some(exe));
}

#[test]
fn voidx_home_with_custom_venv_subpath() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["VOIDX_PYTHON", "VOIDX_HOME"]);

    // VOIDX_HOME can point anywhere; resolve_python should look for
    // $VOIDX_HOME/venv/{bin|Scripts}/python{.exe}
    let tmp = tempfile::tempdir().unwrap();
    let exe = fake_venv(tmp.path());

    unsafe {
        std::env::set_var("VOIDX_HOME", tmp.path());
    }

    let result = resolve_python();
    assert!(result.is_some());
    assert!(result.unwrap().ends_with(&exe));
}

#[cfg(unix)]
#[test]
fn default_install_dir_uses_xdg_data_home() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["XDG_DATA_HOME", "HOME"]);

    unsafe {
        std::env::set_var("XDG_DATA_HOME", "/custom/xdg");
        std::env::set_var("HOME", "/nonexistent/voidx-home-98765");
    }

    let path = default_install_dir("bin/python").unwrap();
    assert_eq!(path, PathBuf::from("/custom/xdg/voidx/venv/bin/python"));
}

#[cfg(unix)]
#[test]
fn default_install_dir_falls_back_to_home() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["XDG_DATA_HOME", "HOME"]);

    unsafe {
        std::env::remove_var("XDG_DATA_HOME");
        std::env::set_var("HOME", "/custom/home");
    }

    let path = default_install_dir("bin/python").unwrap();
    assert_eq!(
        path,
        PathBuf::from("/custom/home/.local/share/voidx/venv/bin/python")
    );
}

#[cfg(windows)]
#[test]
fn default_install_dir_uses_local_appdata() {
    let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _guard = EnvGuard::new(&["LOCALAPPDATA"]);

    unsafe {
        std::env::set_var("LOCALAPPDATA", "C:\\custom\\appdata");
    }

    let path = default_install_dir("Scripts/python.exe").unwrap();
    assert_eq!(
        path,
        PathBuf::from("C:\\custom\\appdata\\voidx\\venv\\Scripts\\python.exe")
    );
}
