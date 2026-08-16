use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use voidx_desktop::{
    bundled_backend_command_args,
    default_install_dir,
    resolve_python,
    validate_installed_runtime,
};

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


#[test]
fn runtime_paths_are_nested_under_voidx_data_root() {
    let data_root = PathBuf::from("/tmp/voidx-data");
    assert_eq!(
        voidx_desktop::runtime_root(&data_root),
        data_root.join("runtime")
    );
    assert_eq!(
        voidx_desktop::runtime_current_manifest_path(&data_root),
        data_root.join("runtime/current.json")
    );
    assert_eq!(
        voidx_desktop::runtime_version_dir(&data_root, "sha256-test"),
        data_root.join("runtime/versions/sha256-test")
    );
}

#[test]
fn backend_manifest_requires_version_api_target_and_fingerprint() {
    let manifest = serde_json::json!({
        "schema_version": 1,
        "backend_version": "3.8.0",
        "backend_api": "gateway-v2",
        "target": "aarch64-apple-darwin",
        "image_fingerprint": "a".repeat(64),
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages"
    });

    assert!(voidx_desktop::backend_manifest_is_compatible(
        &manifest,
        "3.8.0",
        "gateway-v2",
        "aarch64-apple-darwin",
    ));
    assert!(!voidx_desktop::backend_manifest_is_compatible(
        &manifest,
        "3.8.1",
        "gateway-v2",
        "aarch64-apple-darwin",
    ));
    assert!(!voidx_desktop::backend_manifest_is_compatible(
        &manifest,
        "3.8.0",
        "gateway-v1",
        "aarch64-apple-darwin",
    ));
    assert!(!voidx_desktop::backend_manifest_is_compatible(
        &manifest,
        "3.8.0",
        "x",
        "x86_64-apple-darwin",
    ));
}


#[test]
fn runtime_target_and_relative_paths_are_strict() {
    assert_eq!(
        voidx_desktop::runtime_target_triple("macos", "aarch64"),
        Some("aarch64-apple-darwin".to_string())
    );
    assert_eq!(
        voidx_desktop::runtime_target_triple("windows", "x86_64"),
        Some("x86_64-pc-windows-msvc".to_string())
    );
    assert_eq!(
        voidx_desktop::runtime_target_triple("linux", "aarch64"),
        Some("aarch64-unknown-linux-gnu".to_string())
    );
    assert_eq!(
        voidx_desktop::runtime_python_path(
            PathBuf::from("/tmp/voidx/runtime/versions/fingerprint").as_path(),
            "python/bin/python",
        ),
        Some(PathBuf::from("/tmp/voidx/runtime/versions/fingerprint/python/bin/python"))
    );
    assert!(voidx_desktop::runtime_python_path(
        PathBuf::from("/tmp/voidx/runtime/versions/fingerprint").as_path(),
        "../python/bin/python",
    )
    .is_none());
    assert!(voidx_desktop::runtime_python_path(
        PathBuf::from("/tmp/voidx/runtime/versions/fingerprint").as_path(),
        "/tmp/python",
    )
    .is_none());
}


#[test]
fn bundled_backend_command_uses_isolated_python_and_runtime_site_packages() {
    let args = bundled_backend_command_args(PathBuf::from("/tmp/runtime/site-packages").as_path());

    assert_eq!(args.first().map(String::as_str), Some("-I"));
    assert_eq!(args.get(1).map(String::as_str), Some("-c"));
    assert!(args
        .get(2)
        .is_some_and(|code| code.contains("sys.path.insert(0, sys.argv.pop(1))")));
    assert_eq!(args.get(3).map(String::as_str), Some("/tmp/runtime/site-packages"));
}


#[test]
fn install_backend_image_uses_shared_data_root_and_preserves_user_data() {
    let data_root = tempfile::tempdir().unwrap();
    let image = tempfile::tempdir().unwrap();
    let site_packages = image.path().join("site-packages");
    std::fs::create_dir_all(site_packages.join("voidx/presentation/gateway/session/method"))
        .unwrap();
    std::fs::create_dir_all(image.path().join("python/bin")).unwrap();
    std::fs::write(image.path().join("python/bin/python"), b"python").unwrap();
    std::fs::write(
        site_packages.join("voidx/presentation/gateway/session/method/sessions.py"),
        b"new_temporary_thread_id runtime_profile=profile",
    )
    .unwrap();

    let fingerprint = voidx_desktop::hash_image_tree(image.path()).unwrap();
    let manifest = serde_json::json!({
        "schema_version": 1,
        "backend_version": "3.8.0",
        "backend_api": "gateway-v2",
        "target": voidx_desktop::current_runtime_target_triple(),
        "image_fingerprint": fingerprint,
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages",
        "source_revision": "test-revision"
    });
    std::fs::write(
        image.path().join("manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();

    let store = data_root.path().join("store");
    std::fs::create_dir_all(&store).unwrap();
    std::fs::write(store.join("voidx.db"), b"existing database").unwrap();

    let installed = voidx_desktop::install_backend_image(
        data_root.path(),
        image.path(),
        "3.8.0",
        "gateway-v2",
        voidx_desktop::current_runtime_target_triple(),
    )
    .unwrap();
    voidx_desktop::activate_runtime(data_root.path(), &manifest).unwrap();

    assert_eq!(
        installed,
        data_root.path().join("runtime/versions").join(fingerprint)
    );
    assert!(installed.join("python/bin/python").exists());
    assert_eq!(
        std::fs::read(store.join("voidx.db")).unwrap(),
        b"existing database"
    );
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(
            &std::fs::read(data_root.path().join("runtime/current.json")).unwrap()
        )
        .unwrap(),
        manifest
    );
}

#[test]
fn install_backend_image_does_not_activate_incomplete_source() {
    let data_root = tempfile::tempdir().unwrap();
    let image = tempfile::tempdir().unwrap();
    std::fs::write(image.path().join("manifest.json"), b"{}").unwrap();

    assert!(voidx_desktop::install_backend_image(
        data_root.path(),
        image.path(),
        "3.8.0",
        "gateway-v2",
        voidx_desktop::current_runtime_target_triple(),
    )
    .is_err());
    assert!(!data_root.path().join("runtime/current.json").exists());
}

#[test]
fn runtime_install_lock_reclaims_a_dead_owner() {
    let data_root = tempfile::tempdir().unwrap();
    let runtime = voidx_desktop::runtime_root(data_root.path());
    std::fs::create_dir_all(&runtime).unwrap();
    std::fs::write(runtime.join("install.lock"), b"4294967295\n").unwrap();

    let lock = voidx_desktop::acquire_runtime_install_lock(data_root.path()).unwrap();

    assert!(!runtime.join("install.lock").metadata().unwrap().len().eq(&0));
    drop(lock);
    assert!(!runtime.join("install.lock").exists());
}


#[test]
fn validate_installed_runtime_checks_paths_without_rehashing_contents() {
    let data_root = tempfile::tempdir().unwrap();
    let fingerprint = "a".repeat(64);
    let runtime = data_root
        .path()
        .join("runtime/versions")
        .join(&fingerprint);
    let python = runtime.join("python/bin/python");
    let site_packages = runtime.join("site-packages");
    std::fs::create_dir_all(python.parent().unwrap()).unwrap();
    std::fs::create_dir_all(&site_packages).unwrap();
    std::fs::write(&python, b"changed after install").unwrap();

    let manifest = serde_json::json!({
        "image_fingerprint": fingerprint,
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages"
    });

    let paths = validate_installed_runtime(data_root.path(), &manifest).unwrap();
    assert_eq!(paths.0, python);
    assert_eq!(paths.1, site_packages);
}


#[test]
fn validate_installed_runtime_rejects_invalid_fingerprint_before_path_lookup() {
    let data_root = tempfile::tempdir().unwrap();
    let manifest = serde_json::json!({
        "image_fingerprint": "../escape",
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages"
    });

    let error = validate_installed_runtime(data_root.path(), &manifest).unwrap_err();
    assert_eq!(error, "backend manifest has an invalid image fingerprint");
}
