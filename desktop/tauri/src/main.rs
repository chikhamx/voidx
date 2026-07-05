use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::json;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(not(windows))]
use voidx_desktop::CommandExt;
use tauri::{Emitter, State, WindowEvent};

use voidx_desktop::{BackendStatus, persist_workspace, resolve_python, resolve_workspace};

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
            persist_workspace(&workspace_path);
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

fn spawn_backend(
    app: tauri::AppHandle,
    gateway_url: Arc<Mutex<Option<String>>>,
    backend_status: Arc<Mutex<BackendStatus>>,
    child_handle: Arc<Mutex<Option<Child>>>,
    workspace: Arc<Mutex<PathBuf>>,
) {
    std::thread::spawn(move || {
        let python = match resolve_python() {
            Some(path) => path,
            None => {
                let error = "failed to resolve python interpreter".to_string();
                eprintln!("{error}");
                set_failed(&app, &backend_status, &gateway_url, error);
                return;
            }
        };

        // DIAGNOSTIC: log resolve results to temp file for debugging msi startup
        {
            let log_path = std::env::temp_dir().join("voidx_spawn_diag.log");
            if let Ok(mut f) = std::fs::File::create(&log_path) {
                use std::io::Write;
                let _ = writeln!(f, "exe: {:?}", std::env::current_exe());
                let _ = writeln!(f, "cwd: {:?}", std::env::current_dir());
                let _ = writeln!(f, "LOCALAPPDATA: {:?}", std::env::var("LOCALAPPDATA"));
                let _ = writeln!(f, "VOIDX_PYTHON: {:?}", std::env::var("VOIDX_PYTHON"));
                let _ = writeln!(f, "resolved python: {:?}", python);
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
        command
            .args([
                "-m",
                "voidx.main",
                "--web",
                "--web-headless",
                "-w",
                &workspace.to_string_lossy(),
            ])
            .stderr(Stdio::piped())
            .stdout(Stdio::null());
        // Start the backend in its own process group so kill_backend can
        // terminate the whole tree (Python + any MCP/LSP subprocesses).
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        // Windows: suppress the console window that spawn() allocates for the
        // child process, so only the Tauri window is visible to the user.
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

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            gateway_url,
            backend_status,
            child_handle,
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
        .run(tauri::generate_context!())
        .expect("failed to run voidx desktop app");
}
