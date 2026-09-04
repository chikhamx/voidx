use std::process::ExitCode;

use voidx_macos_ocr::run;

fn main() -> ExitCode {
    let result = run(std::env::args().skip(1));
    let exit_code = result.exit_code;
    print!("{}", result.stdout);
    if let Some(diagnostic) = result.diagnostic {
        eprintln!("{diagnostic}");
    }
    ExitCode::from(exit_code)
}
