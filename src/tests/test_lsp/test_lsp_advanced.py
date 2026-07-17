import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.agent.slash import SlashHandler
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.lsp.client import LSP_REQUEST_TIMEOUT_SECONDS, encode_lsp_message
from voidx.lsp.errors import LspConnectionError, LspServerUnavailable
from voidx.lsp.manager import LspManager, apply_text_edits
from voidx.lsp.schema import LspServerConfig
from voidx.tools.base import ToolContext
from voidx.tools.lsp import LspFormatTool
from voidx.tools.registry import ToolRegistry
import voidx.memory.store as store
import voidx.tools.lsp as lsp_module


FAKE_LSP_SERVER = r'''
import json
import sys

docs = {}

def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {}}})
    elif method == "initialized":
        continue
    elif method == "textDocument/didOpen":
        doc = params["textDocument"]
        docs[doc["uri"]] = doc["text"]
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": doc["uri"],
                "diagnostics": [{
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                    "severity": 2,
                    "source": "fake",
                    "message": "fake warning"
                }]
            }
        })
    elif method == "textDocument/didChange":
        doc = params["textDocument"]
        docs[doc["uri"]] = params["contentChanges"][0]["text"]
    elif method == "textDocument/documentSymbol":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": msg_id, "result": [{
            "name": "Foo",
            "kind": 5,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 0}},
            "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}},
            "children": [{
                "name": "bar",
                "kind": 6,
                "range": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 20}},
                "selectionRange": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 11}}
            }]
        }]})
    elif method == "textDocument/definition":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"uri": uri, "range": {
            "start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}
        }}})
    elif method == "textDocument/references":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": msg_id, "result": [{"uri": uri, "range": {
            "start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 11}
        }}]})
    elif method == "textDocument/formatting":
        send({"jsonrpc": "2.0", "id": msg_id, "result": [{
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 999, "character": 0}},
            "newText": "class Foo:\n    def bar(self):\n        return 1\n"
        }]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg_id, "result": None})
    elif method == "exit":
        break
'''


def _write_fake_lsp(workspace: Path) -> None:
    server = workspace / "fake_lsp.py"
    server.write_text(FAKE_LSP_SERVER, encoding="utf-8")
    config_dir = workspace / ".voidx"
    config_dir.mkdir()
    (config_dir / "lsp.json").write_text(
        json.dumps({
            "version": 1,
            "servers": {
                "python": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "extensions": [".py"],
                    "enabled": True,
                }
            }
        }),
        encoding="utf-8",
    )


def test_lsp_doctor_reports_initializing_without_io(monkeypatch, tmp_path):
    def fail_load(_workspace):
        raise AssertionError("load_lsp_servers should not run from doctor/statuses")

    monkeypatch.setattr("voidx.lsp.manager.load_lsp_servers", fail_load)
    manager = LspManager(str(tmp_path))

    checks = manager.doctor()
    statuses = manager.statuses()

    assert checks[0].language == "*"
    assert "initializing" in checks[0].error_message
    assert statuses[0].status == "initializing"


def test_lsp_warmup_languages_does_not_mutate_resolved_command(monkeypatch, tmp_path):
    manager = LspManager(str(tmp_path))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command="pyright-langserver",
            extensions=[".py"],
        )
    }
    monkeypatch.setattr("voidx.lsp.manager._resolve_command", lambda command: "/bin/pyright-langserver")

    assert manager._warmup_languages() == ["python"]
    assert manager._servers["python"].resolved_command == ""


def test_tool_filter_uses_cached_lsp_availability():
    tool_defs = [
        {"function": {"name": "lsp"}},
        {"function": {"name": "read_file"}},
    ]

    class FakeLspManager:
        def __init__(self) -> None:
            self.checked = False

        def has_available_server(self):
            self.checked = True
            return True

        def doctor(self):
            raise AssertionError("doctor should not be called by tool filtering")

    manager = FakeLspManager()

    assert filter_unavailable_lsp_tools(tool_defs, manager) == tool_defs
    assert manager.checked is True


@pytest.mark.asyncio
async def test_lsp_tools_use_context_manager(tmp_path):
    _write_fake_lsp(tmp_path)
    (tmp_path / "sample.py").write_text("class Foo:\n def bar(self):\n  return 1\n", encoding="utf-8")
    manager = LspManager(str(tmp_path))
    registry = ToolRegistry()
    ctx = ToolContext(workspace=str(tmp_path), lsp_manager=manager)

    try:
        symbols = await registry.execute_tool("lsp", {"operation": "symbols", "file_path": "sample.py"}, ctx)

        assert "Foo" in symbols.output
        assert "bar" in symbols.output
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_lsp_format_tool_saves_file_version_before_format(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / ".voidx")
    target = tmp_path / "sample.py"
    target.write_text("print( 1 )\n", encoding="utf-8")

    class FakeService:
        async def format_range(self, file_path, range_):
            assert range_.start.line == 0
            assert range_.start.character == 0
            assert range_.end.line == 0
            assert range_.end.character == 10
            path = tmp_path / file_path
            old_text = path.read_text(encoding="utf-8")
            return True, old_text, "print(1)\n"

    monkeypatch.setattr(lsp_module, "_service", lambda _ctx: FakeService())

    ctx = ToolContext(workspace=str(tmp_path), session_id="sid-1", lsp_manager=object())
    key = str(target.resolve())
    ctx.file_read_coverage[key] = {"ranges": [{"start_line": 1, "end_line": 1}]}
    result = await LspFormatTool().execute(
        {
            "file_path": "sample.py",
            "start_line": 1,
            "start_character": 0,
            "end_line": 1,
            "end_character": 10,
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["formatted"] is True
    assert target.read_text(encoding="utf-8") == "print(1)\n"
    assert result.diff is not None
    history_dir = store.DATA_DIR / "sessions" / "sid-1" / "file-history"
    assert key in ctx.file_mtimes
    assert key not in ctx.file_read_coverage
    rows = [
        json.loads(line)
        for line in (history_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["path"] == "sample.py"
    assert rows[0]["tool"] == "lsp_format"
    assert (history_dir / rows[0]["snapshot"]).read_text(encoding="utf-8") == "print( 1 )\n"


@pytest.mark.asyncio
async def test_lsp_manager_reports_missing_server(tmp_path):
    (tmp_path / ".voidx").mkdir()
    (tmp_path / ".voidx" / "lsp.json").write_text(
        json.dumps({"servers": {"python": {"command": "missing-lsp-bin", "args": [], "extensions": [".py"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "sample.py").write_text("print('x')\n", encoding="utf-8")
    manager = LspManager(str(tmp_path))

    with pytest.raises(LspServerUnavailable, match="Command not found"):
        await manager.diagnostics("sample.py", wait=0)

    statuses = manager.statuses()

    assert any(status.language == "python" and status.status == "error" for status in statuses)


@pytest.mark.asyncio
async def test_lsp_doctor_reports_available_missing_and_disabled_servers(tmp_path):
    (tmp_path / ".voidx").mkdir()
    (tmp_path / ".voidx" / "lsp.json").write_text(
        json.dumps({
            "servers": {
                "python": {
                    "command": sys.executable,
                    "args": ["-m", "fake"],
                    "extensions": [".py"],
                },
                "go": {
                    "command": "missing-gopls",
                    "extensions": [".go"],
                },
                "rust": {
                    "command": "rust-analyzer",
                    "extensions": [".rs"],
                    "enabled": False,
                },
            }
        }),
        encoding="utf-8",
    )
    manager = LspManager(str(tmp_path))
    await manager.initialize()

    checks = {check.language: check for check in manager.doctor()}

    assert checks["python"].available is True
    assert checks["python"].resolved_path == sys.executable
    assert checks["go"].available is False
    assert checks["go"].install_hint == "Install with: go install golang.org/x/tools/gopls@latest"
    assert checks["rust"].enabled is False
    assert checks["rust"].error_message == "Server disabled in config."


@pytest.mark.asyncio
async def test_slash_lsp_dispatches_status_and_restart(tmp_path):
    class FakeLspManager:
        def __init__(self) -> None:
            self.restart_target = "unset"
            self.servers = {}

        def statuses(self):
            return [SimpleNamespace(
                language="python",
                command="pyright-langserver --stdio",
                status="disconnected",
                pid=None,
                open_documents=0,
                error_message="",
            )]

        async def restart(self, language=None):
            self.restart_target = language

        def doctor(self):
            return [
                SimpleNamespace(
                    language="python",
                    command="pyright-langserver --stdio",
                    enabled=True,
                    available=False,
                    resolved_path="",
                    install_hint="Install with: npm install -g pyright",
                    error_message="Command not found: pyright-langserver",
                    detected_source="",
                )
            ]

    manager = FakeLspManager()
    graph = SimpleNamespace(_lsp_manager=manager, _workspace=str(tmp_path))
    handler = SlashHandler(graph)

    assert await handler.dispatch("/lsp status") is True
    assert await handler.dispatch("/lsp doctor") is True
    assert await handler.dispatch("/lsp restart python") is True
    assert manager.restart_target == "python"


@pytest.mark.asyncio
async def test_lsp_format_tool_accepts_eof_position_after_trailing_newline(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("print( 1 )\n", encoding="utf-8")

    class FakeService:
        async def format_range(self, file_path, range_):
            old_text = target.read_text(encoding="utf-8")
            return False, old_text, old_text

    monkeypatch.setattr(lsp_module, "_service", lambda _ctx: FakeService())

    ctx = ToolContext(workspace=str(tmp_path), lsp_manager=object())
    key = str(target.resolve())
    ctx.file_read_coverage[key] = {"ranges": [{"start_line": 1, "end_line": 1}]}
    result = await LspFormatTool().execute(
        {
            "file_path": "sample.py",
            "start_line": 1,
            "start_character": 0,
            "end_line": 2,
            "end_character": 0,
        },
        ctx,
    )

    assert result.metadata.get("error") is not True
    assert result.metadata["formatted"] is False

    assert key in ctx.file_mtimes
    assert key in ctx.file_read_coverage

@pytest.mark.asyncio
async def test_lsp_format_tool_does_not_overwrite_concurrent_change(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    original = "print( 1 )\n"
    target.write_text(original, encoding="utf-8")

    class FakeService:
        async def format_range(self, file_path, range_):
            target.write_text("concurrent = True\n", encoding="utf-8")
            return True, original, "print(1)\n"

    monkeypatch.setattr(lsp_module, "_service", lambda _ctx: FakeService())

    result = await LspFormatTool().execute(
        {
            "file_path": "sample.py",
            "start_line": 1,
            "start_character": 0,
            "end_line": 2,
            "end_character": 0,
        },
        ToolContext(workspace=str(tmp_path), lsp_manager=object()),
    )

    assert result.metadata["error"] is True
    assert "changed" in result.output.lower()
    assert target.read_text(encoding="utf-8") == "concurrent = True\n"
