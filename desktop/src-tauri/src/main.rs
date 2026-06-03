use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{Manager, State};

struct AppState {
    gateway_url: Arc<Mutex<Option<String>>>,
}

#[tauri::command]
fn gateway_url(state: State<'_, AppState>) -> Option<String> {
    state.gateway_url.lock().ok()?.clone()
}

fn resolve_python() -> PathBuf {
    if let Ok(path) = std::env::var("VOIDX_PYTHON") {
        return PathBuf::from(path);
    }
    PathBuf::from(".venv/bin/python")
}

fn resolve_workspace() -> PathBuf {
    if let Ok(path) = std::env::var("VOIDX_WORKSPACE") {
        return PathBuf::from(path);
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn spawn_backend(state: AppState) {
    std::thread::spawn(move || {
        let python = resolve_python();
        let workspace = resolve_workspace();
        let mut child = match Command::new(&python)
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
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                eprintln!("failed to spawn voidx backend with {}: {error}", python.display());
                return;
            }
        };

        let Some(stderr) = child.stderr.take() else {
            return;
        };

        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            let Some(json) = line.strip_prefix("VOIDX_WEB_GATEWAY") else {
                continue;
            };
            let Ok(payload) = serde_json::from_str::<serde_json::Value>(json) else {
                continue;
            };
            let Some(url) = payload.get("url").and_then(|value| value.as_str()) else {
                continue;
            };
            if let Ok(mut slot) = state.gateway_url.lock() {
                *slot = Some(url.to_string());
            }
            break;
        }
    });
}

fn main() {
    let state = AppState {
        gateway_url: Arc::new(Mutex::new(None)),
    };
    let backend_state = AppState {
        gateway_url: Arc::clone(&state.gateway_url),
    };

    tauri::Builder::default()
        .manage(state)
        .setup(move |_app| {
            spawn_backend(backend_state);
            std::thread::sleep(Duration::from_millis(100));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![gateway_url])
        .run(tauri::generate_context!())
        .expect("failed to run voidx desktop app");
}
