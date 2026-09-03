use std::process::ExitCode;

use serde::Serialize;
use voidx_macos_ocr::run;

fn main() -> ExitCode {
    let result = run(std::env::args().skip(1));
    let exit_code = result.exit_code;
    emit_json(&result.response);
    if let Some(diagnostic) = result.diagnostic {
        eprintln!("{diagnostic}");
    }
    ExitCode::from(exit_code)
}

fn emit_json<T: Serialize>(value: &T) {
    match serde_json::to_string(value) {
        Ok(json) => println!("{json}"),
        Err(error) => {
            eprintln!("failed to serialize OCR response: {error}");
            std::process::exit(70);
        }
    }
}
