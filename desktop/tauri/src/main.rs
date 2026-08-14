use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(not(windows))]
use voidx_desktop::CommandExt;
use tauri::{Emitter, Manager, State, WindowEvent};

use voidx_desktop::{
    activate_runtime, acquire_runtime_install_lock, backend_manifest_is_compatible,
    backend_manifests_match, current_runtime_target_triple, default_data_root,
    bundled_backend_command_args, install_backend_image, probe_backend, resolve_python,
    resolve_workspace,
    validate_installed_runtime, BackendStatus, BACKEND_API, BACKEND_VERSION,
};

struct AppState {
    gateway_url: Arc<Mutex<Option<String>>>,
    backend_status: Arc<Mutex<BackendStatus>>,
    child_handle: Arc<Mutex<Option<Child>>>,
    workspace: Arc<Mutex<PathBuf>>,
}

#[tauri::command]
fn get_gateway_url(state: State<'_, AppState>) -> Option<String> {
    match state.gateway_url.lock() {
        Ok(slot) => slot.clone(),
        Err(_) => {
            eprintln!("gateway_url lock poisoned");
            None
        }
    }
}

#[tauri::command]
fn get_backend_status(state: State<'_, AppState>) -> serde_json::Value {
    state
        .backend_status
        .lock()
        .map(|s| s.to_json())
        .unwrap_or_else(|_| json!({"status": "failed", "error": "state lock poisoned"}))
}

#[tauri::command]
fn restart_backend(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    workspace: Option<String>,
) -> serde_json::Value {
    kill_backend(&state.child_handle);

    if let Ok(mut slot) = state.backend_status.lock() {
        *slot = BackendStatus::Starting;
    }
    if let Ok(mut slot) = state.gateway_url.lock() {
        *slot = None;
    }
    if let Some(path) = workspace {
        if !path.trim().is_empty() {
            let workspace_path = PathBuf::from(path);
            if let Ok(mut slot) = state.workspace.lock() {
                *slot = workspace_path;
            }
        }
    }

    let gateway_url = Arc::clone(&state.gateway_url);
    let backend_status = Arc::clone(&state.backend_status);
    let child_handle = Arc::clone(&state.child_handle);
    let workspace = Arc::clone(&state.workspace);

    spawn_backend(app, gateway_url, backend_status, child_handle, workspace);

    json!({"status": "restarting"})
}

struct PreparedBackend {
    python: PathBuf,
    site_packages: Option<PathBuf>,
    data_root: PathBuf,
}

fn read_manifest(path: &std::path::Path) -> Result<Value, String> {
    serde_json::from_slice(
        &std::fs::read(path).map_err(|error| format!("read backend manifest {}: {error}", path.display()))?,
    )
    .map_err(|error| format!("parse backend manifest {}: {error}", path.display()))
}

fn prepare_backend(app: &tauri::AppHandle) -> Result<PreparedBackend, String> {
    let data_root = default_data_root()
        .ok_or_else(|| "cannot resolve the user home for ~/.voidx".to_string())?;
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("resolve bundled backend resources: {error}"))?;
    let image_dir = resource_dir.join("backend");
    let bundled_manifest_path = image_dir.join("manifest.json");

    if !bundled_manifest_path.is_file() {
        if cfg!(debug_assertions) {
            let python = resolve_python()
                .ok_or_else(|| "failed to resolve development Python interpreter".to_string())?;
            return Ok(PreparedBackend {
                python,
                site_packages: None,
                data_root,
            });
        }
        return Err(format!(
            "bundled backend image is missing: {}",
            bundled_manifest_path.display()
        ));
    }

    let bundled_manifest = read_manifest(&bundled_manifest_path)?;
    let target = current_runtime_target_triple();
    if !backend_manifest_is_compatible(
        &bundled_manifest,
        BACKEND_VERSION,
        BACKEND_API,
        target,
    ) {
        return Err(format!(
            "bundled backend manifest is incompatible with this desktop build (target {target})"
        ));
    }

    let _install_lock = acquire_runtime_install_lock(&data_root)?;
    let current_manifest_path = voidx_desktop::runtime_current_manifest_path(&data_root);
    if let Ok(current_manifest) = read_manifest(&current_manifest_path) {
        if backend_manifests_match(&current_manifest, &bundled_manifest) {
            if let Ok((python, site_packages)) =
                validate_installed_runtime(&data_root, &bundled_manifest)
            {
                if probe_backend(&python, &site_packages, BACKEND_VERSION).is_ok() {
                    return Ok(PreparedBackend {
                        python,
                        site_packages: Some(site_packages),
                        data_root,
                    });
                }
            }
        }
    }

    let runtime_dir = install_backend_image(
        &data_root,
        &image_dir,
        BACKEND_VERSION,
        BACKEND_API,
        target,
    )?;
    activate_runtime(&data_root, &bundled_manifest)?;
    let (python, site_packages) = validate_installed_runtime(&data_root, &bundled_manifest)?;
    probe_backend(&python, &site_packages, BACKEND_VERSION)?;
    eprintln!("voidx backend runtime installed at {}", runtime_dir.display());
    Ok(PreparedBackend {
        python,
        site_packages: Some(site_packages),
        data_root,
    })
}

fn spawn_backend(
    app: tauri::AppHandle,
    gateway_url: Arc<Mutex<Option<String>>>,
    backend_status: Arc<Mutex<BackendStatus>>,
    child_handle: Arc<Mutex<Option<Child>>>,
    workspace: Arc<Mutex<PathBuf>>,
) {
    std::thread::spawn(move || {
        let prepared = match prepare_backend(&app) {
            Ok(prepared) => prepared,
            Err(error) => {
                eprintln!("{error}");
                set_failed(&app, &backend_status, &gateway_url, error);
                return;
            }
        };
        let python = prepared.python;

        if std::env::var("VOIDX_DEBUG").is_ok() {
            let log_path = std::env::temp_dir().join("voidx_spawn_diag.log");
            if let Ok(mut f) = std::fs::File::create(&log_path) {
                use std::io::Write;
                let _ = writeln!(f, "exe: {:?}", std::env::current_exe());
                let _ = writeln!(f, "cwd: {:?}", std::env::current_dir());
                let _ = writeln!(f, "resolved python: {:?}", python);
                let _ = writeln!(f, "bundled site-packages: {:?}", prepared.site_packages);
                let _ = writeln!(f, "data root: {:?}", prepared.data_root);
                let resolved_workspace = workspace
                    .lock()
                    .map(|slot| slot.clone())
                    .unwrap_or_else(|_| resolve_workspace());
                let _ = writeln!(f, "resolved workspace: {:?}", resolved_workspace);
            }
        }

        let workspace = workspace
            .lock()
            .map(|slot| slot.clone())
            .unwrap_or_else(|_| resolve_workspace());
        let mut command = Command::new(&python);
        if let Some(site_packages) = &prepared.site_packages {
            command.args(bundled_backend_command_args(site_packages));
        } else {
            command.args(["-m", "voidx.main"]);
        }
        command
            .args([
                "--web",
                "--web-headless",
                "-w",
                &workspace.to_string_lossy(),
            ])
            .env("VOIDX_HOME", &prepared.data_root)
            .env("PYTHONNOUSERSITE", "1")
            .stderr(Stdio::piped())
            .stdout(Stdio::null());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let child = command.spawn();

        let mut child = match child {
            Ok(child) => child,
            Err(error) => {
                let msg = format!(
                    "failed to spawn voidx backend with {}: {error}",
                    python.display()
                );
                eprintln!("{msg}");
                set_failed(&app, &backend_status, &gateway_url, msg);
                return;
            }
        };

        let stderr = child.stderr.take();
        if let Ok(mut slot) = child_handle.lock() {
            *slot = Some(child);
        }

        let Some(stderr) = stderr else {
            let error = "backend stderr unavailable".to_string();
            eprintln!("{error}");
            set_failed(&app, &backend_status, &gateway_url, error);
            return;
        };

        let reader = BufReader::new(stderr);
        let mut found_url = false;
        let mut io_error: Option<std::io::Error> = None;
        for line in reader.lines() {
            let line = match line {
                Ok(line) => line,
                Err(error) => {
                    io_error = Some(error);
                    break;
                }
            };
            let Some(json_payload) = line.strip_prefix("VOIDX_WEB_GATEWAY") else {
                continue;
            };
            let Ok(payload) = serde_json::from_str::<serde_json::Value>(json_payload) else {
                continue;
            };
            let Some(url) = payload.get("url").and_then(|value| value.as_str()) else {
                continue;
            };
            let url = url.to_string();
            if let Ok(mut slot) = gateway_url.lock() {
                *slot = Some(url.clone());
            }
            if let Ok(mut slot) = backend_status.lock() {
                *slot = BackendStatus::Ready { url: url.clone() };
            }
            let _ = app.emit("backend_ready", json!({ "url": url }));
            found_url = true;
            break;
        }

        if !found_url {
            let error = match io_error {
                Some(error) => format!("backend stderr read failed: {error}"),
                None => "backend exited without publishing gateway url".to_string(),
            };
            eprintln!("{error}");
            set_failed(&app, &backend_status, &gateway_url, error);
        }
    });
}

fn set_failed(
    app: &tauri::AppHandle,
    backend_status: &Arc<Mutex<BackendStatus>>,
    gateway_url: &Arc<Mutex<Option<String>>>,
    error: String,
) {
    if let Ok(mut slot) = backend_status.lock() {
        *slot = BackendStatus::Failed {
            error: error.clone(),
        };
    }
    if let Ok(mut slot) = gateway_url.lock() {
        *slot = None;
    }
    let _ = app.emit("backend_failed", json!({ "error": error }));
}

fn kill_backend(child_handle: &Arc<Mutex<Option<Child>>>) {
    if let Ok(mut slot) = child_handle.lock() {
        if let Some(child) = slot.take() {
            let pid = child.id();
            // Kill the entire process tree so MCP/LSP subprocesses spawned by
            // the Python backend are not orphaned. `child.kill()` only signals
            // the direct child.
            if cfg!(windows) {
                // taskkill /T /F walks and terminates the process tree.
                const CREATE_NO_WINDOW: u32 = 0x08000000;
                let _ = Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .creation_flags(CREATE_NO_WINDOW)
                    .status();
            }
            #[cfg(unix)]
            {
                // Send SIGKILL to the process group. The backend is spawned in
                // its own process group via process_group(0) in spawn_backend.
                unsafe {
                    libc::killpg(pid as i32, libc::SIGKILL);
                }
            }
            // Reap the direct child to avoid zombies.
            let mut child = child;
            let _ = child.wait();
        }
    }
}

fn main() {
    let gateway_url: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let backend_status: Arc<Mutex<BackendStatus>> =
        Arc::new(Mutex::new(BackendStatus::Starting));
    let child_handle: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let workspace: Arc<Mutex<PathBuf>> = Arc::new(Mutex::new(resolve_workspace()));

    let app_gateway_url = Arc::clone(&gateway_url);
    let app_backend_status = Arc::clone(&backend_status);
    let app_child_handle = Arc::clone(&child_handle);
    let app_workspace = Arc::clone(&workspace);
    let exit_child_handle = Arc::clone(&child_handle);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            gateway_url,
            backend_status,
            child_handle: Arc::clone(&app_child_handle),
            workspace,
        })
        .on_window_event({
            let child_handle = Arc::clone(&app_child_handle);
            move |_window, event| {
                if let WindowEvent::CloseRequested { .. } = event {
                    kill_backend(&child_handle);
                }
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            spawn_backend(
                handle,
                app_gateway_url,
                app_backend_status,
                Arc::clone(&app_child_handle),
                app_workspace,
            );
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_gateway_url,
            get_backend_status,
            restart_backend
        ])
        .build(tauri::generate_context!())
        .expect("failed to build voidx desktop app");

    app.run(move |_app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            kill_backend(&exit_child_handle);
        }
    });
}
