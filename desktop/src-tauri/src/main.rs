use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::json;
use tauri::{Emitter, State, WindowEvent};

struct AppState {
    gateway_url: Arc<Mutex<Option<String>>>,
    backend_status: Arc<Mutex<BackendStatus>>,
}

#[derive(Clone)]
enum BackendStatus {
    Starting,
    Ready { url: String },
    Failed { error: String },
}

impl BackendStatus {
    fn to_json(&self) -> serde_json::Value {
        match self {
            BackendStatus::Starting => json!({"status": "starting"}),
            BackendStatus::Ready { url } => json!({"status": "ready", "url": url}),
            BackendStatus::Failed { error } => json!({"status": "failed", "error": error}),
        }
    }
}

#[tauri::command]
fn get_gateway_url(state: State<'_, AppState>) -> Option<String> {
    state.gateway_url.lock().ok()?.clone()
}

#[tauri::command]
fn get_backend_status(state: State<'_, AppState>) -> serde_json::Value {
    state
        .backend_status
        .lock()
        .map(|s| s.to_json())
        .unwrap_or_else(|_| json!({"status": "failed", "error": "state lock poisoned"}))
}

fn resolve_python() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("VOIDX_PYTHON") {
        return Some(PathBuf::from(path));
    }
    let candidates: &[&str] = if cfg!(windows) {
        &[".venv/Scripts/python.exe", ".venv/bin/python"]
    } else {
        &[".venv/bin/python", ".venv/Scripts/python.exe"]
    };
    for candidate in candidates {
        let path = PathBuf::from(candidate);
        if path.exists() {
            return Some(path);
        }
    }
    if cfg!(windows) {
        if let Ok(output) = Command::new("py")
            .arg("-c")
            .arg("import sys; print(sys.executable)")
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
    None
}

fn resolve_workspace() -> PathBuf {
    if let Ok(path) = std::env::var("VOIDX_WORKSPACE") {
        return PathBuf::from(path);
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn spawn_backend(
    app: tauri::AppHandle,
    gateway_url: Arc<Mutex<Option<String>>>,
    backend_status: Arc<Mutex<BackendStatus>>,
    child_handle: Arc<Mutex<Option<Child>>>,
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

        let workspace = resolve_workspace();
        let child = Command::new(&python)
            .args([
                "-m",
                "voidx.main",
                "--web",
                "--web-headless",
                "-w",
                &workspace.to_string_lossy(),
            ])
            .stderr(Stdio::piped())
            .stdout(Stdio::null())
            .spawn();

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
        for line in reader.lines().map_while(Result::ok) {
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
            let error = "backend exited without publishing gateway url".to_string();
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
        if let Some(mut child) = slot.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn main() {
    let gateway_url: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let backend_status: Arc<Mutex<BackendStatus>> =
        Arc::new(Mutex::new(BackendStatus::Starting));
    let child_handle: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));

    let app_gateway_url = Arc::clone(&gateway_url);
    let app_backend_status = Arc::clone(&backend_status);
    let app_child_handle = Arc::clone(&child_handle);

    tauri::Builder::default()
        .manage(AppState {
            gateway_url,
            backend_status,
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
            );
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_gateway_url, get_backend_status])
        .run(tauri::generate_context!())
        .expect("failed to run voidx desktop app");
}
