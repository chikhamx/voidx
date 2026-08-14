use std::path::PathBuf;
use std::process::Command;

use serde_json::{json, Value};

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

pub fn runtime_root(data_root: &std::path::Path) -> PathBuf {
    data_root.join("runtime")
}

pub fn runtime_current_manifest_path(data_root: &std::path::Path) -> PathBuf {
    runtime_root(data_root).join("current.json")
}

pub fn runtime_version_dir(data_root: &std::path::Path, fingerprint: &str) -> PathBuf {
    runtime_root(data_root).join("versions").join(fingerprint)
}

pub fn backend_manifest_is_compatible(
    manifest: &Value,
    expected_version: &str,
    expected_api: &str,
    expected_target: &str,
) -> bool {
    manifest.get("schema_version").and_then(Value::as_u64) == Some(1)
        && manifest.get("backend_version").and_then(Value::as_str) == Some(expected_version)
        && manifest.get("backend_api").and_then(Value::as_str) == Some(expected_api)
        && manifest.get("target").and_then(Value::as_str) == Some(expected_target)
        && manifest
            .get("image_fingerprint")
            .and_then(Value::as_str)
            .is_some_and(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        && manifest
            .get("python_relative")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
        && manifest
            .get("site_packages_relative")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
}

pub fn runtime_target_triple(system: &str, architecture: &str) -> Option<String> {
    let system = system.trim().to_ascii_lowercase();
    let architecture = architecture.trim().to_ascii_lowercase();
    let arch = match architecture.as_str() {
        "arm64" | "aarch64" => "aarch64",
        "amd64" | "x86_64" | "x64" => "x86_64",
        _ => return None,
    };
    match system.as_str() {
        "macos" | "darwin" => Some(format!("{arch}-apple-darwin")),
        "windows" => Some(format!("{arch}-pc-windows-msvc")),
        "linux" => Some(format!("{arch}-unknown-linux-gnu")),
        _ => None,
    }
}

pub fn current_runtime_target_triple() -> &'static str {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "aarch64-apple-darwin"
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
        "x86_64-apple-darwin"
    } else if cfg!(all(target_os = "windows", target_arch = "aarch64")) {
        "aarch64-pc-windows-msvc"
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        "x86_64-pc-windows-msvc"
    } else if cfg!(all(target_os = "linux", target_arch = "aarch64")) {
        "aarch64-unknown-linux-gnu"
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        "x86_64-unknown-linux-gnu"
    } else {
        "unsupported"
    }
}

pub fn runtime_python_path(base: &std::path::Path, relative: &str) -> Option<PathBuf> {
    use std::path::Component;

    if relative.is_empty() || relative.contains('\\') {
        return None;
    }
    let path = std::path::Path::new(relative);
    if path.is_absolute() {
        return None;
    }
    for component in path.components() {
        if !matches!(component, Component::Normal(_)) {
            return None;
        }
    }
    Some(base.join(path))
}

pub fn bundled_backend_command_args(site_packages: &std::path::Path) -> Vec<String> {
    vec![
        "-I".to_string(),
        "-c".to_string(),
        "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(\"voidx.main\", run_name=\"__main__\")".to_string(),
        site_packages.to_string_lossy().into_owned(),
    ]
}

pub fn hash_image_tree(root: &std::path::Path) -> std::io::Result<String> {
    use sha2::{Digest, Sha256};
    use std::fs;

    fn collect_files(root: &std::path::Path, dir: &std::path::Path, files: &mut Vec<PathBuf>) -> std::io::Result<()> {
        for entry in fs::read_dir(dir)? {
            let path = entry?.path();
            let metadata = fs::metadata(&path)?;
            if metadata.is_dir() {
                collect_files(root, &path, files)?;
            } else if metadata.is_file()
                && !matches!(
                    path.file_name().and_then(|name| name.to_str()),
                    Some("manifest.json") | Some(".gitkeep")
                )
            {
                let _ = root;
                files.push(path);
            }
        }
        Ok(())
    }

    let root = root.canonicalize()?;
    let mut files = Vec::new();
    collect_files(&root, &root, &mut files)?;
    files.sort_by(|left, right| left.cmp(right));

    let mut digest = Sha256::new();
    for path in files {
        let relative = path
            .strip_prefix(&root)
            .expect("collected image path must be under image root")
            .to_string_lossy()
            .replace('\\', "/");
        let relative = relative.as_bytes();
        let data = fs::read(&path)?;
        digest.update((relative.len() as u64).to_be_bytes());
        digest.update(relative);
        digest.update((data.len() as u64).to_be_bytes());
        digest.update(data);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn copy_image_tree(source: &std::path::Path, destination: &std::path::Path) -> std::io::Result<()> {
    use std::fs;
    use std::path::Component;

    fn copy_entry(source: &std::path::Path, destination: &std::path::Path) -> std::io::Result<()> {
        let metadata = fs::symlink_metadata(source)?;
        if metadata.file_type().is_symlink() {
            let target = fs::read_link(source)?;
            #[cfg(unix)]
            std::os::unix::fs::symlink(target, destination)?;
            #[cfg(windows)]
            {
                let target_metadata = fs::metadata(source);
                if target_metadata.as_ref().is_ok_and(|value| value.is_dir()) {
                    std::os::windows::fs::symlink_dir(target, destination)?;
                } else {
                    std::os::windows::fs::symlink_file(target, destination)?;
                }
            }
        } else if metadata.is_dir() {
            fs::create_dir_all(destination)?;
            for entry in fs::read_dir(source)? {
                let entry = entry?;
                copy_entry(&entry.path(), &destination.join(entry.file_name()))?;
            }
        } else if metadata.is_file() {
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::copy(source, destination)?;
        } else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("unsupported backend image entry: {}", source.display()),
            ));
        }
        Ok(())
    }

    if source.components().any(|component| matches!(component, Component::Prefix(_))) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "unsupported backend image path prefix",
        ));
    }
    copy_entry(source, destination)
}

pub fn install_backend_image(
    data_root: &std::path::Path,
    image_root: &std::path::Path,
    expected_version: &str,
    expected_api: &str,
    expected_target: &str,
) -> Result<PathBuf, String> {
    use std::fs;

    let manifest_path = image_root.join("manifest.json");
    let manifest: Value = serde_json::from_slice(
        &fs::read(&manifest_path).map_err(|error| format!("read backend manifest: {error}"))?,
    )
    .map_err(|error| format!("parse backend manifest: {error}"))?;
    if !backend_manifest_is_compatible(
        &manifest,
        expected_version,
        expected_api,
        expected_target,
    ) {
        return Err("bundled backend manifest is incompatible".to_string());
    }
    let fingerprint = manifest
        .get("image_fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no image fingerprint".to_string())?;
    if fingerprint.len() != 64 || !fingerprint.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("backend manifest has an invalid image fingerprint".to_string());
    }
    let python_relative = manifest
        .get("python_relative")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no Python path".to_string())?;
    let python_path = runtime_python_path(image_root, python_relative)
        .ok_or_else(|| "backend manifest has an unsafe Python path".to_string())?;
    if !python_path.is_file() {
        return Err(format!("bundled backend Python not found: {}", python_path.display()));
    }
    let site_packages_relative = manifest
        .get("site_packages_relative")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no site-packages path".to_string())?;
    let site_packages = runtime_python_path(image_root, site_packages_relative)
        .ok_or_else(|| "backend manifest has an unsafe site-packages path".to_string())?;
    if !site_packages.is_dir() {
        return Err(format!("bundled site-packages not found: {}", site_packages.display()));
    }
    let actual_fingerprint = hash_image_tree(image_root)
        .map_err(|error| format!("hash bundled backend image: {error}"))?;
    if actual_fingerprint != fingerprint {
        return Err("bundled backend image fingerprint mismatch".to_string());
    }

    let runtime = runtime_root(data_root);
    let versions = runtime.join("versions");
    fs::create_dir_all(&versions).map_err(|error| format!("create runtime directory: {error}"))?;
    let destination = runtime_version_dir(data_root, fingerprint);
    if destination.is_dir() {
        match hash_image_tree(&destination) {
            Ok(actual) if actual == fingerprint => return Ok(destination),
            Ok(_) | Err(_) => {
                fs::remove_dir_all(&destination)
                    .map_err(|error| format!("remove damaged backend runtime: {error}"))?;
            }
        }
    }
    let staging = versions.join(format!(".{fingerprint}.install-{}", std::process::id()));
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|error| format!("remove stale staging runtime: {error}"))?;
    }
    copy_image_tree(image_root, &staging)
        .map_err(|error| format!("copy backend runtime: {error}"))?;
    let copied_fingerprint = hash_image_tree(&staging)
        .map_err(|error| format!("verify installed backend runtime: {error}"))?;
    if copied_fingerprint != fingerprint {
        let _ = fs::remove_dir_all(&staging);
        return Err("installed backend runtime fingerprint mismatch".to_string());
    }
    fs::rename(&staging, &destination)
        .map_err(|error| format!("activate installed backend runtime: {error}"))?;
    Ok(destination)
}

pub fn activate_runtime(data_root: &std::path::Path, manifest: &Value) -> Result<(), String> {
    use std::fs;

    let fingerprint = manifest
        .get("image_fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| "cannot activate backend without fingerprint".to_string())?;
    let version_dir = runtime_version_dir(data_root, fingerprint);
    if !version_dir.is_dir() {
        return Err("cannot activate backend runtime before installation".to_string());
    }
    let current_path = runtime_current_manifest_path(data_root);
    let temporary_path = current_path.with_extension(format!("json.tmp-{}", std::process::id()));
    fs::create_dir_all(runtime_root(data_root))
        .map_err(|error| format!("create runtime root: {error}"))?;
    fs::write(
        &temporary_path,
        serde_json::to_vec_pretty(manifest).map_err(|error| format!("serialize runtime manifest: {error}"))?,
    )
    .map_err(|error| format!("write runtime manifest: {error}"))?;
    fs::rename(&temporary_path, &current_path)
        .map_err(|error| format!("activate current backend runtime: {error}"))?;
    Ok(())
}

pub const BACKEND_VERSION: &str = env!("VOIDX_BACKEND_VERSION");
pub const BACKEND_API: &str = env!("VOIDX_BACKEND_API");

pub fn backend_manifests_match(actual: &Value, expected: &Value) -> bool {
    [
        "schema_version",
        "backend_version",
        "backend_api",
        "target",
        "image_fingerprint",
        "python_relative",
        "site_packages_relative",
        "source_revision",
    ]
    .iter()
    .all(|key| actual.get(*key) == expected.get(*key))
}

pub fn runtime_paths_for_manifest(
    runtime_dir: &std::path::Path,
    manifest: &Value,
) -> Result<(PathBuf, PathBuf), String> {
    let python_relative = manifest
        .get("python_relative")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no Python path".to_string())?;
    let site_packages_relative = manifest
        .get("site_packages_relative")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no site-packages path".to_string())?;
    let python = runtime_python_path(runtime_dir, python_relative)
        .ok_or_else(|| "backend manifest has an unsafe Python path".to_string())?;
    let site_packages = runtime_python_path(runtime_dir, site_packages_relative)
        .ok_or_else(|| "backend manifest has an unsafe site-packages path".to_string())?;
    Ok((python, site_packages))
}

pub fn validate_installed_runtime(
    data_root: &std::path::Path,
    manifest: &Value,
) -> Result<(PathBuf, PathBuf), String> {
    let fingerprint = manifest
        .get("image_fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| "backend manifest has no image fingerprint".to_string())?;
    let runtime_dir = runtime_version_dir(data_root, fingerprint);
    if !runtime_dir.is_dir() {
        return Err(format!("backend runtime is missing: {}", runtime_dir.display()));
    }
    let actual_fingerprint = hash_image_tree(&runtime_dir)
        .map_err(|error| format!("hash installed backend runtime: {error}"))?;
    if actual_fingerprint != fingerprint {
        return Err("installed backend runtime fingerprint mismatch".to_string());
    }
    let (python, site_packages) = runtime_paths_for_manifest(&runtime_dir, manifest)?;
    if !python.is_file() {
        return Err(format!("installed backend Python is missing: {}", python.display()));
    }
    if !site_packages.is_dir() {
        return Err(format!(
            "installed backend site-packages is missing: {}",
            site_packages.display()
        ));
    }
    Ok((python, site_packages))
}

pub fn probe_backend(
    python: &std::path::Path,
    site_packages: &std::path::Path,
    expected_version: &str,
) -> Result<(), String> {
    let probe = r#"
import importlib
import inspect
import pathlib
import sys

site_packages = pathlib.Path(sys.argv[1]).resolve()
expected_version = sys.argv[2]
sys.path.insert(0, str(site_packages))
import voidx

package_path = pathlib.Path(voidx.__file__).resolve()
if site_packages not in package_path.parents:
    raise RuntimeError(f"voidx imported outside bundled site-packages: {package_path}")
if getattr(voidx, "__version__", None) != expected_version:
    raise RuntimeError(f"voidx version mismatch: {getattr(voidx, '__version__', None)!r}")
module = importlib.import_module("voidx.presentation.gateway.session.method.sessions")
source = inspect.getsource(module.SessionMethods._method_session_create)
if "new_temporary_thread_id" not in source:
    raise RuntimeError("Gateway does not expose the temporary-thread API")
if "runtime_profile=profile" not in source:
    raise RuntimeError("Gateway does not expose the runtime-profile API")
print(package_path)
"#;
    let output = Command::new(python)
        .arg("-I")
        .arg("-c")
        .arg(probe)
        .arg(site_packages)
        .arg(expected_version)
        .env("PYTHONNOUSERSITE", "1")
        .output()
        .map_err(|error| format!("run backend probe: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        return Err(format!(
            "backend probe failed: {}{}",
            stderr,
            if stdout.is_empty() {
                String::new()
            } else {
                format!(" ({stdout})")
            }
        ));
    }
    Ok(())
}

pub struct RuntimeInstallLock {
    path: PathBuf,
    _file: std::fs::File,
}

impl Drop for RuntimeInstallLock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

pub fn acquire_runtime_install_lock(data_root: &std::path::Path) -> Result<RuntimeInstallLock, String> {
    use std::fs::OpenOptions;
    use std::io::ErrorKind;
    use std::thread;
    use std::time::{Duration, Instant};

    let runtime = runtime_root(data_root);
    std::fs::create_dir_all(&runtime)
        .map_err(|error| format!("create runtime root for lock: {error}"))?;
    let path = runtime.join("install.lock");
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok(RuntimeInstallLock { path, _file: file }),
            Err(error) if error.kind() == ErrorKind::AlreadyExists && Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(100));
            }
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {
                return Err("timed out waiting for backend runtime installation lock".to_string())
            }
            Err(error) => return Err(format!("create backend runtime installation lock: {error}")),
        }
    }
}

pub fn default_data_root() -> Option<PathBuf> {
    let home_var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(home_var).map(|home| PathBuf::from(home).join(".voidx"))
}


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

