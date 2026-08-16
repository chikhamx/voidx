use voidx_desktop::{is_supported_runtime_profile, runtime_profile_label, BackendStatus};
use serde_json::json;

#[test]
fn starting_status_serializes() {
    assert_eq!(BackendStatus::Starting.to_json(), json!({"status": "starting"}));
}

#[test]
fn ready_status_serializes_with_url() {
    let status = BackendStatus::Ready { url: "ws://127.0.0.1:8080".into() };
    assert_eq!(
        status.to_json(),
        json!({"status": "ready", "url": "ws://127.0.0.1:8080"})
    );
}

#[test]
fn failed_status_serializes_with_error() {
    let status = BackendStatus::Failed { error: "boom".into() };
    assert_eq!(
        status.to_json(),
        json!({"status": "failed", "error": "boom"})
    );
}

#[test]
fn desktop_supports_all_backend_runtime_profiles() {
    for profile in ["coding", "chat", "loop", "goal"] {
        assert!(is_supported_runtime_profile(profile));
        assert_ne!(runtime_profile_label(profile), "Unknown");
    }
    assert!(!is_supported_runtime_profile("unknown"));
    assert_eq!(runtime_profile_label("unknown"), "Unknown");
}


#[test]
fn desktop_frontend_runtime_profile_contract_is_embedded() {
    let index = include_str!("../../../frontend/index.html");
    let main = include_str!("../../../frontend/src/main.ts");
    let connection = include_str!("../../../frontend/src/services/connection.ts");
    let mode = include_str!("../../../frontend/src/ui/mode.ts");

    assert!(index.contains("id=\"btn-new-chat\""));
    for profile in ["coding", "chat", "loop", "goal"] {
        assert!(mode.contains(&format!("\"{profile}\"")));
    }
    assert!(main.contains("rpcCall(\"session.create\", params)"));
    assert!(main.contains("rpcCall(\"session.switch\", {"));
    assert!(main.contains("turn_limit: TRANSCRIPT_PAGE_SIZE"));
    assert!(main.contains("runtime_profile"));
    assert!(connection.contains("wait_gateway_url"));
}
