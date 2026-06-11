import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.agent.slash import SlashHandler
from voidx.agent.tool_filters import filter_unavailable_lsp_tools
from voidx.lsp.client import LSP_REQUEST_TIMEOUT_SECONDS, encode_lsp_message
from voidx.lsp.errors import LspConnectionError, LspServerUnavailable
from voidx.lsp.manager import LspManager, apply_text_edits
from voidx.lsp.schema import LspServerConfig
from voidx.tools.base import ToolContext
from voidx.tools.registry import ToolRegistry


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


def test_encode_lsp_message_uses_content_length():
    encoded = encode_lsp_message({"jsonrpc": "2.0", "method": "ping"})

    header, body = encoded.split(b"\r\n\r\n", 1)

    assert header.startswith(b"Content-Length: ")
    assert int(header.split(b": ")[1]) == len(body)


def test_apply_text_edits_replaces_ranges_from_back_to_front():
    text = "abc\ndef\n"
    edits = [
        {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}}, "newText": "xyz"},
        {"range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 2}}, "newText": "B"},
    ]

    assert apply_text_edits(text, edits) == "aBc\nxyz\n"


@pytest.mark.asyncio
async def test_lsp_manager_talks_to_stdio_server(tmp_path):
    _write_fake_lsp(tmp_path)
    (tmp_path / "sample.py").write_text("class Foo:\n def bar(self):\n  return 1\n", encoding="utf-8")
    manager = LspManager(str(tmp_path))

    try:
        assert manager.initialized is False
        diagnostics = await manager.diagnostics("sample.py", wait=0.05)
        symbols = await manager.document_symbols("sample.py")
        definition = await manager.definition("sample.py", 1, 8)
        references = await manager.references("sample.py", 2, 8)
        changed, old_text, new_text = await manager.format_document("sample.py")

        assert manager.initialized is True
        assert diagnostics[0].message == "fake warning"
        assert [symbol.name for symbol in symbols] == ["Foo", "bar"]
        assert definition[0].path.endswith("sample.py")
        assert references[0].range.start.line == 1
        assert changed is True
        assert "class Foo" in old_text
        assert new_text == "class Foo:\n    def bar(self):\n        return 1\n"
        assert (tmp_path / "sample.py").read_text(encoding="utf-8") == new_text
    finally:
        await manager.stop_all()


def test_lsp_manager_constructor_does_not_load_servers(monkeypatch, tmp_path):
    def fail_load(_workspace):
        raise AssertionError("load_lsp_servers should not run in constructor")

    monkeypatch.setattr("voidx.lsp.manager.load_lsp_servers", fail_load)

    manager = LspManager(str(tmp_path))

    assert manager.initialized is False
    assert manager.servers == {}


@pytest.mark.asyncio
async def test_lsp_initialize_runs_load_in_thread(monkeypatch, tmp_path):
    captured = SimpleNamespace(fn=None, args=None)

    async def fake_to_thread(fn, *args):
        captured.fn = fn
        captured.args = args
        return {
            "python": LspServerConfig(
                language="python",
                command=sys.executable,
                extensions=[".py"],
                resolved_command=sys.executable,
            )
        }

    monkeypatch.setattr("voidx.lsp.manager.asyncio.to_thread", fake_to_thread)

    manager = LspManager(str(tmp_path))
    await manager.initialize()

    assert captured.fn.__name__ == "load_lsp_servers"
    assert captured.args == (str(tmp_path.resolve()),)
    assert manager.initialized is True
    assert manager.has_available_server() is True


@pytest.mark.asyncio
async def test_lsp_tool_waits_for_initialization(tmp_path):
    _write_fake_lsp(tmp_path)
    (tmp_path / "sample.py").write_text("class Foo:\n def bar(self):\n  return 1\n", encoding="utf-8")
    manager = LspManager(str(tmp_path))

    try:
        assert manager.initialized is False
        symbols = await manager.document_symbols("sample.py")

        assert manager.initialized is True
        assert [symbol.name for symbol in symbols] == ["Foo", "bar"]
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_lsp_manager_warm_up_starts_available_servers(tmp_path):
    _write_fake_lsp(tmp_path)
    manager = LspManager(str(tmp_path))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command=sys.executable,
            args=[str(tmp_path / "fake_lsp.py")],
            extensions=[".py"],
            resolved_command=sys.executable,
        )
    }
    manager._initialized = True

    try:
        result = await manager.warm_up(timeout=1.0)
        statuses = {status.language: status for status in manager.statuses()}

        assert result == {"python": "ok"}
        assert statuses["python"].status == "connected"
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_lsp_manager_warm_up_skips_unavailable_servers(tmp_path):
    manager = LspManager(str(tmp_path))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command="missing-lsp-bin",
            extensions=[".py"],
        )
    }
    manager._initialized = True

    result = await manager.warm_up(timeout=0.01)

    assert result == {}
    status = manager.statuses()[0]
    assert status.language == "python"
    assert status.status == "disconnected"


@pytest.mark.asyncio
async def test_lsp_manager_warm_up_records_per_server_errors(tmp_path, monkeypatch):
    manager = LspManager(str(tmp_path))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command=sys.executable,
            extensions=[".py"],
            resolved_command=sys.executable,
        )
    }
    manager._initialized = True

    async def fake_ensure_client(language: str, *, timeout: float = LSP_REQUEST_TIMEOUT_SECONDS):
        raise LspConnectionError(f"{language} failed")

    monkeypatch.setattr(manager, "_ensure_client", fake_ensure_client)

    result = await manager.warm_up(timeout=1.0)

    assert result == {"python": "error: python failed"}
    assert any(
        status.language == "python" and status.status == "error" and status.error_message == "python failed"
        for status in manager.statuses()
    )


@pytest.mark.asyncio
async def test_lsp_manager_requests_use_extended_timeout(tmp_path):
    (tmp_path / "sample.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    manager = LspManager(str(tmp_path))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command=sys.executable,
            extensions=[".py"],
            resolved_command=sys.executable,
        )
    }
    manager._initialized = True
    requests: list[tuple[str, float]] = []

    class FakeClient:
        connected = True
        pid = 123
        error_message = ""

        async def request(self, method, params=None, *, timeout=0):
            requests.append((method, timeout))
            if method == "textDocument/documentSymbol":
                return []
            if method == "workspace/symbol":
                return []
            if method == "textDocument/definition":
                return None
            if method == "textDocument/references":
                return []
            if method == "textDocument/formatting":
                return []
            return None

        async def notify(self, method, params=None):
            return None

    manager._clients["python"] = FakeClient()

    await manager.document_symbols("sample.py")
    await manager.workspace_symbols("")
    await manager.definition("sample.py", 1, 0)
    await manager.references("sample.py", 1, 0)
    await manager.format_document("sample.py")

    assert requests == [
        ("textDocument/documentSymbol", LSP_REQUEST_TIMEOUT_SECONDS),
        ("workspace/symbol", LSP_REQUEST_TIMEOUT_SECONDS),
        ("textDocument/definition", LSP_REQUEST_TIMEOUT_SECONDS),
        ("textDocument/references", LSP_REQUEST_TIMEOUT_SECONDS),
        ("textDocument/formatting", LSP_REQUEST_TIMEOUT_SECONDS),
    ]


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
        {"function": {"name": "lsp_symbols"}},
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
        symbols = await registry.execute_tool("lsp_symbols", {"file_path": "sample.py"}, ctx)
        formatted = await registry.execute_tool("lsp_format", {"file_path": "sample.py"}, ctx)

        assert "Foo" in symbols.output
        assert "bar" in symbols.output
        assert "File formatted" in formatted.output
        assert formatted.diff
    finally:
        await manager.stop_all()


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
