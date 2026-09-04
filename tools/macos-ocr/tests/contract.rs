use serde_json::Value;
use voidx_macos_ocr::run;

#[test]
fn help_prints_usage_to_stdout_without_an_error_envelope() {
    let result = run(["--help"]);

    assert_eq!(result.exit_code, 0);
    assert!(result.stdout.starts_with("Usage: voidx-macos-ocr "));
    assert!(!result.stdout.contains("\"error\""));
    assert!(result.diagnostic.is_none());
}

#[test]
fn invalid_arguments_are_one_json_line_on_stdout() {
    let result = run(["--unknown"]);

    assert_eq!(result.exit_code, 64);
    let lines = result.stdout.lines().collect::<Vec<_>>();
    assert_eq!(lines.len(), 1);
    let value: Value = serde_json::from_str(lines[0]).unwrap();
    assert_eq!(value["ok"], false);
    assert_eq!(value["error"]["code"], "invalid_arguments");
}
