use std::fs;
use std::path::Path;

fn metadata_value(source: &str, marker: &str, label: &str) -> String {
    let start = source
        .find(marker)
        .map(|index| index + marker.len())
        .unwrap_or_else(|| panic!("src/voidx/platform/version.py must define {label}"));
    let end = source[start..]
        .find('"')
        .map(|index| start + index)
        .unwrap_or_else(|| panic!("voidx {label} must be quoted"));
    source[start..end].to_string()
}

fn main() {
    let version_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src/voidx/platform/version.py");
    let version_source = fs::read_to_string(&version_path)
        .unwrap_or_else(|error| panic!("failed to read {}: {error}", version_path.display()));
    let backend_version = metadata_value(&version_source, "VERSION = \"", "VERSION");
    let backend_api = metadata_value(&version_source, "BACKEND_API = \"", "BACKEND_API");
    println!("cargo:rerun-if-changed={}", version_path.display());
    println!("cargo:rustc-env=VOIDX_BACKEND_VERSION={backend_version}");
    println!("cargo:rustc-env=VOIDX_BACKEND_API={backend_api}");
    tauri_build::build();
}
