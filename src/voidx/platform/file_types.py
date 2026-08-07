"""File type classification."""

from __future__ import annotations


def language_from_path(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mapping = {
        "py": "python", "js": "javascript", "jsx": "jsx", "ts": "typescript",
        "tsx": "tsx", "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "c": "c",
        "h": "cpp", "hpp": "cpp", "rs": "rust", "go": "go", "java": "java",
        "json": "json", "toml": "toml", "yaml": "yaml", "yml": "yaml",
        "md": "markdown", "css": "css", "html": "html", "sh": "bash",
    }
    return mapping.get(suffix, "")
