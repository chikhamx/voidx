use voidx_desktop::BackendStatus;
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
