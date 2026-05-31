"""Pydantic models for Language Server Protocol data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field


LspStatus = Literal["disabled", "disconnected", "connected", "error"]


class LspPosition(BaseModel):
    line: int = Field(ge=0)
    character: int = Field(ge=0)


class LspRange(BaseModel):
    start: LspPosition
    end: LspPosition


class LspLocation(BaseModel):
    uri: str
    path: str
    range: LspRange | None = None


class LspDiagnostic(BaseModel):
    uri: str
    path: str
    range: LspRange
    severity: int | None = None
    source: str = ""
    code: str = ""
    message: str


class LspSymbol(BaseModel):
    name: str
    kind: int | None = None
    path: str = ""
    range: LspRange | None = None
    selection_range: LspRange | None = None
    container_name: str = ""


class LspServerConfig(BaseModel):
    language: str
    command: str
    args: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Set after auto-detection: actual binary path found
    resolved_command: str = ""
    # Human-readable source of detection, e.g. "CursorPyright (Cursor ext)"
    detected_source: str = ""


class LspRuntimeStatus(BaseModel):
    language: str
    command: str = ""
    status: LspStatus
    pid: int | None = None
    open_documents: int = 0
    error_message: str = ""


class LspDoctorCheck(BaseModel):
    language: str
    command: str
    enabled: bool = True
    available: bool = False
    resolved_path: str = ""
    install_hint: str = ""
    error_message: str = ""
    detected_source: str = ""


def file_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


def path_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    return unquote(parsed.path)


def parse_range(data: dict[str, Any] | None) -> LspRange | None:
    if not isinstance(data, dict):
        return None
    try:
        return LspRange.model_validate(data)
    except ValueError:
        return None


def parse_location(data: dict[str, Any]) -> LspLocation | None:
    uri = data.get("uri") or data.get("targetUri")
    if not isinstance(uri, str):
        return None
    range_data = data.get("range") or data.get("targetSelectionRange") or data.get("targetRange")
    return LspLocation(
        uri=uri,
        path=path_from_uri(uri),
        range=parse_range(range_data),
    )


def parse_locations(value: Any) -> list[LspLocation]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[LspLocation] = []
    for item in items:
        if isinstance(item, dict):
            location = parse_location(item)
            if location is not None:
                result.append(location)
    return result


def parse_diagnostics(uri: str, diagnostics: Any) -> list[LspDiagnostic]:
    if not isinstance(diagnostics, list):
        return []
    result: list[LspDiagnostic] = []
    for item in diagnostics:
        if not isinstance(item, dict) or not isinstance(item.get("message"), str):
            continue
        range_data = parse_range(item.get("range"))
        if range_data is None:
            continue
        code = item.get("code", "")
        result.append(LspDiagnostic(
            uri=uri,
            path=path_from_uri(uri),
            range=range_data,
            severity=item.get("severity") if isinstance(item.get("severity"), int) else None,
            source=str(item.get("source", "")),
            code=str(code) if code is not None else "",
            message=item["message"],
        ))
    return result


def parse_document_symbols(uri: str, value: Any) -> list[LspSymbol]:
    if not isinstance(value, list):
        return []
    result: list[LspSymbol] = []

    def visit(items: list[Any], container: str = "") -> None:
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            location = item.get("location")
            symbol_uri = uri
            range_data = item.get("range")
            selection_range = item.get("selectionRange")
            if isinstance(location, dict):
                symbol_uri = location.get("uri") or uri
                range_data = location.get("range") or range_data
            result.append(LspSymbol(
                name=item["name"],
                kind=item.get("kind") if isinstance(item.get("kind"), int) else None,
                path=path_from_uri(symbol_uri),
                range=parse_range(range_data),
                selection_range=parse_range(selection_range),
                container_name=str(item.get("containerName") or container or ""),
            ))
            children = item.get("children")
            if isinstance(children, list):
                visit(children, item["name"])

    visit(value)
    return result
