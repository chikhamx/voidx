use std::path::PathBuf;
use std::process::Command;

use serde_json::json;

/// Max parent dirs to walk up from the exe when searching for a `.venv`.
/// Covers typical bundle layouts, e.g. macOS:
///   voidx.app/Contents/MacOS/voidx-desktop -> ../../../../.venv
const EXE_VENV_SEARCH_DEPTH: usize = 6;

/// Max parent dirs to walk up from the exe when searching for a project root
/// (dir containing AGENTS.md or pyproject.toml). Slightly deeper than the
/// venv search to handle nested bundle + dev layouts.
const EXE_PROJECT_ROOT_SEARCH_DEPTH: usize = 8;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(not(windows))]
pub mod command_ext {
    use std::process::Command;
    pub trait CommandExt {
        fn creation_flags(&mut self, _flags: u32) -> &mut Self;
    }
    impl CommandExt for Command {
        fn creation_flags(&mut self, _flags: u32) -> &mut Self { self }
    }
}
#[cfg(not(windows))]
pub use command_ext::CommandExt;

pub const DESKTOP_RUNTIME_PROFILES: [&str; 4] = ["coding", "chat", "loop", "goal"];

pub fn is_supported_runtime_profile(profile: &str) -> bool {
    DESKTOP_RUNTIME_PROFILES.contains(&profile)
}

pub fn runtime_profile_label(profile: &str) -> &'static str {
    match profile {
        "coding" => "Coding",
        "chat" => "Chat",
        "loop" => "Loop",
        "goal" => "Goal",
        _ => "Unknown",
    }
}

pub enum BackendStatus {
    Starting,
    Ready { url: String },
    Failed { error: String },
}

impl BackendStatus {
    pub fn to_json(&self) -> serde_json::Value {
        match self {
            BackendStatus::Starting => json!({"status": "starting"}),
            BackendStatus::Ready { url } => json!({"status": "ready", "url": url}),
            BackendStatus::Failed { error } => json!({"status": "failed", "error": error}),
        }
    }
}

/// Resolve the default voidx install directory's venv python path.
/// Windows: %LOCALAPPDATA%/voidx/venv; Unix: ${XDG_DATA_HOME:-$HOME/.local/share}/voidx/venv.
/// Matches the logic in python.py. Returns the expected path
/// (does not check existence — caller verifies).
pub fn default_install_dir(venv_scripts: &str) -> Option<PathBuf> {
    if cfg!(windows) {
        let local_app_data = std::env::var("LOCALAPPDATA").ok()?;
        Some(PathBuf::from(local_app_data).join("voidx/venv").join(venv_scripts))
    } else {
        let base = std::env::var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|_| std::env::var("HOME").map(|h| PathBuf::from(h).join(".local/share")))
            .ok()?;
        Some(base.join("voidx/venv").join(venv_scripts))
    }
}

pub fn resolve_python() -> Option<PathBuf> {
    // 1. Explicit override
    if let Ok(path) = std::env::var("VOIDX_PYTHON") {
        return Some(PathBuf::from(path));
    }

    let venv_scripts = if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    };

    // 2. VOIDX_HOME — same install directory as python.py
    if let Ok(home) = std::env::var("VOIDX_HOME") {
        let p = PathBuf::from(home).join("venv").join(venv_scripts);
        if p.exists() {
            return Some(p);
        }
    }

    // 3. .venv relative to CWD (dev mode: launched from project root)
    if let Ok(cwd) = std::env::current_dir() {
        let p = cwd.join(".venv").join(venv_scripts);
        if p.exists() {
            return Some(p);
        }
    }

    // 4. .venv relative to the exe directory (bundled: exe next to project root)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            let mut dir = Some(exe_dir);
            for _ in 0..EXE_VENV_SEARCH_DEPTH {
                if let Some(d) = dir {
                    let p = d.join(".venv").join(venv_scripts);
                    if p.exists() {
                        return Some(p);
                    }
                    dir = d.parent();
                }
            }
        }
    }

    // 5. User-installed voidx venv
    if let Some(p) = default_install_dir(venv_scripts) {
        if p.exists() {
            return Some(p);
        }
    }

    // 6. py launcher (Windows)
    // SECURITY: This executes whatever `py` resolves to on PATH. Only reached
    // after all trusted-path lookups fail. A compromised PATH could run
    // arbitrary code here. Mitigation: prefer VOIDX_PYTHON or a bundled venv
    // in production deployments.
    if cfg!(windows) {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        if let Ok(output) = Command::new("py")
            .arg("-c")
            .arg("import sys; print(sys.executable)")
            .creation_flags(CREATE_NO_WINDOW)
            .output()
        {
            if output.status.success() {
                let resolved = String::from_utf8_lossy(&output.stdout);
                let trimmed = resolved.trim();
                if !trimmed.is_empty() {
                    return Some(PathBuf::from(trimmed));
                }
            }
        }
    }

    // 7. python on PATH
    let cmd = if cfg!(windows) { "python" } else { "python3" };
    let mut probe = Command::new(cmd);
    probe.arg("-c").arg("import sys; print(sys.executable)");
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        probe.creation_flags(CREATE_NO_WINDOW);
    }
    if let Ok(output) = probe.output() {
        if output.status.success() {
            let resolved = String::from_utf8_lossy(&output.stdout);
            let trimmed = resolved.trim();
            if !trimmed.is_empty() {
                return Some(PathBuf::from(trimmed));
            }
        }
    }

    None
}

pub fn resolve_workspace() -> PathBuf {
    if let Ok(path) = std::env::var("VOIDX_WORKSPACE") {
        let path = PathBuf::from(path);
        if is_usable_workspace(&path) {
            return path;
        }
    }
    // Walk up from exe directory to find project root (contains AGENTS.md or pyproject.toml)
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent();
        for _ in 0..EXE_PROJECT_ROOT_SEARCH_DEPTH {
            if let Some(d) = dir {
                if is_project_root(d) {
                    return d.to_path_buf();
                }
                dir = d.parent();
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        if is_usable_workspace(&cwd) {
            return cwd;
        }
    }
    // Fallback to user home dir. Windows uses USERPROFILE; Unix uses HOME.
    let home_env = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    if let Ok(home) = std::env::var(home_env) {
        let home_path = PathBuf::from(&home);
        for candidate in [
            home_path.join("workspace/voidx"),
            home_path.join("workspace"),
            home_path.clone(),
        ] {
            if is_usable_workspace(&candidate) {
                return candidate;
            }
        }
    }
    PathBuf::from(".")
}

pub fn is_project_root(path: &std::path::Path) -> bool {
    path.join("AGENTS.md").exists() || path.join("pyproject.toml").exists()
}

pub fn is_usable_workspace(path: &std::path::Path) -> bool {
    path.exists() && path.is_dir() && path.parent().is_some()
}

