import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


from voidx.presentation.slash import SlashHandler
from voidx.agent.application.tool_filters import filter_unavailable_lsp_tools
from voidx.lsp.adapters.client import LSP_REQUEST_TIMEOUT_SECONDS, LspClient, encode_lsp_message, create_lsp_client
from voidx.lsp.domain.errors import LspConnectionError, LspError, LspFormattingUnsupported, LspServerUnavailable, LspTimeoutError
from voidx.lsp.application.manager import LspManager, apply_text_edits
from voidx.lsp.domain.schema import LspPosition, LspRange, LspServerConfig
from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.adapters.lsp import LspFormatTool, LspTool
from voidx.tooling.application.registry import ToolRegistry
import voidx.persistence.sqlite as store
import voidx.tooling.adapters.lsp as lsp_module


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
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {
            "documentRangeFormattingProvider": True
        }}})
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
    elif method == "textDocument/rangeFormatting":
        send({"jsonrpc": "2.0", "id": msg_id, "result": [{
            "range": params["range"],
            "newText": "    def bar(self):\n        return 1\n"
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


def test_apply_text_edits_uses_utf16_character_offsets():
    text = "a😀b\n"
    edits = [{
        "range": {
            "start": {"line": 0, "character": 3},
            "end": {"line": 0, "character": 4},
        },
        "newText": "B",
    }]

    assert apply_text_edits(text, edits) == "a😀B\n"


@pytest.mark.asyncio
async def test_lsp_manager_talks_to_stdio_server(tmp_path):
    _write_fake_lsp(tmp_path)
    (tmp_path / "sample.py").write_text("class Foo:\n def bar(self):\n  return 1\n", encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)

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

    monkeypatch.setattr("voidx.lsp.application.manager.load_lsp_servers", fail_load)

    manager = LspManager(str(tmp_path), create_lsp_client)

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

    monkeypatch.setattr("voidx.lsp.application.manager.asyncio.to_thread", fake_to_thread)

    manager = LspManager(str(tmp_path), create_lsp_client)
    await manager.initialize()

    assert captured.fn.__name__ == "load_lsp_servers"
    assert captured.args == (str(tmp_path.resolve()),)
    assert manager.initialized is True
    assert manager.has_available_server() is True


@pytest.mark.asyncio
async def test_lsp_tool_waits_for_initialization(tmp_path):
    _write_fake_lsp(tmp_path)
    (tmp_path / "sample.py").write_text("class Foo:\n def bar(self):\n  return 1\n", encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)

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
    manager = LspManager(str(tmp_path), create_lsp_client)
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
    manager = LspManager(str(tmp_path), create_lsp_client)
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
    manager = LspManager(str(tmp_path), create_lsp_client)
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
    manager = LspManager(str(tmp_path), create_lsp_client)
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




@pytest.mark.asyncio
async def test_lsp_request_timeout_raises_lsp_timeout_error(tmp_path):
    class FakeStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

    client = LspClient(
        LspServerConfig(language="python", command=sys.executable, extensions=[".py"]),
        cwd=str(tmp_path),
    )
    client._process = SimpleNamespace(stdin=FakeStdin(), returncode=None)

    with pytest.raises(LspTimeoutError):
        await client.request("textDocument/hover", {}, timeout=0.001)


@pytest.mark.asyncio
async def test_lsp_tool_timeout_uses_unified_metadata(tmp_path, monkeypatch):
    class TimeoutService:
        workspace = str(tmp_path)

        async def diagnostics(self, file_path=None):
            raise LspTimeoutError("diagnostics timed out")

    ctx = ToolContext(workspace=str(tmp_path))

    result = await LspTool(TimeoutService()).execute(
        {"operation": "diagnostics"},
        ctx,
    )

    assert result.metadata["error"] is True
    assert result.metadata["timeout"] is True
    assert result.metadata["error_kind"] == "tool_timeout"
    assert result.metadata["timeout_source"] == "lsp"


@pytest.mark.asyncio
async def test_lsp_start_cancellation_rolls_back_owned_process_and_tasks(tmp_path, monkeypatch):
    import voidx.lsp.adapters.client.stdio as client_module

    class FakeProcess:
        def __init__(self):
            self.pid = 1234
            self.returncode = None
            self.stdin = object()
            self.stdout = object()
            self.stderr = object()

    process = FakeProcess()
    initialize_started = asyncio.Event()
    reader_cancelled = asyncio.Event()
    stderr_cancelled = asyncio.Event()
    finalized = asyncio.Event()

    async def create_owned_subprocess_exec(*args, **kwargs):
        return process

    async def finalize(owned):
        assert owned is process
        process.returncode = -15
        finalized.set()

    async def block_reader():
        try:
            await asyncio.Event().wait()
        finally:
            reader_cancelled.set()

    async def block_stderr():
        try:
            await asyncio.Event().wait()
        finally:
            stderr_cancelled.set()

    client = LspClient(
        LspServerConfig(language="python", command=sys.executable, extensions=[".py"]),
        cwd=str(tmp_path),
    )

    async def block_request(method, params=None, *, timeout=0):
        initialize_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client_module, "create_owned_subprocess_exec", create_owned_subprocess_exec, raising=False)
    monkeypatch.setattr(client_module, "finalize_process_tree", finalize, raising=False)
    monkeypatch.setattr(client, "_read_loop", block_reader)
    monkeypatch.setattr(client, "_drain_stderr", block_stderr)
    monkeypatch.setattr(client, "request", block_request)

    task = asyncio.create_task(client.start(root_uri=tmp_path.as_uri()))
    await initialize_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert finalized.is_set()
    assert reader_cancelled.is_set()
    assert stderr_cancelled.is_set()
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert client._pending == {}


@pytest.mark.asyncio
async def test_lsp_manager_cancellation_stops_client_before_propagating(tmp_path, monkeypatch):
    import voidx.lsp.application.manager as manager_module

    start_entered = asyncio.Event()
    stop_finished = asyncio.Event()

    class FakeClient:
        connected = False

        def __init__(self, config, *, cwd):
            self.config = config
            self.cwd = cwd

        async def start(self, *, root_uri, timeout):
            start_entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            await asyncio.sleep(0)
            stop_finished.set()

    manager = LspManager(str(tmp_path), lambda config, cwd: FakeClient(config, cwd=cwd))
    manager._servers = {
        "python": LspServerConfig(
            language="python",
            command=sys.executable,
            extensions=[".py"],
            resolved_command=sys.executable,
        ),
    }
    manager._initialized = True

    task = asyncio.create_task(manager._ensure_client("python"))
    await start_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stop_finished.is_set()
    assert manager._clients == {}


@pytest.mark.asyncio
async def test_lsp_manager_formats_only_requested_range_without_writing(tmp_path):
    _write_fake_lsp(tmp_path)
    target = tmp_path / "sample.py"
    original = "class Foo:\n def bar(self):\n  return 1\n"
    target.write_text(original, encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)
    range_ = LspRange(
        start=LspPosition(line=1, character=0),
        end=LspPosition(line=3, character=0),
    )

    try:
        changed, old_text, new_text = await manager.formatted_range_text("sample.py", range_)

        assert changed is True
        assert old_text == original
        assert new_text == "class Foo:\n    def bar(self):\n        return 1\n"
        assert target.read_text(encoding="utf-8") == original
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_lsp_manager_rejects_range_formatting_without_capability(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("print( 1 )\n", encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)

    class FakeClient:
        capabilities = {"documentFormattingProvider": True}

        async def request(self, method, params, timeout):
            raise AssertionError(f"unexpected request: {method}")

    async def fake_open_document(_file_path):
        return FakeClient(), target.as_uri()

    monkeypatch.setattr(manager, "open_document", fake_open_document)

    with pytest.raises(LspFormattingUnsupported):
        await manager.formatted_range_text(
            "sample.py",
            LspRange(
                start=LspPosition(line=0, character=0),
                end=LspPosition(line=0, character=10),
            ),
        )


@pytest.mark.asyncio
async def test_lsp_manager_rejects_range_formatting_edits_outside_request(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("first\nsecond\n", encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)

    class FakeClient:
        capabilities = {"documentRangeFormattingProvider": True}

        async def request(self, method, params, timeout):
            assert method == "textDocument/rangeFormatting"
            return [{
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0},
                },
                "newText": "changed\n",
            }]

    async def fake_open_document(_file_path):
        return FakeClient(), target.as_uri()

    monkeypatch.setattr(manager, "open_document", fake_open_document)

    with pytest.raises(LspError, match="outside requested range"):
        await manager.formatted_range_text(
            "sample.py",
            LspRange(
                start=LspPosition(line=1, character=0),
                end=LspPosition(line=1, character=6),
            ),
        )


@pytest.mark.asyncio
async def test_lsp_manager_rejects_range_formatting_edit_with_invalid_document_position(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("first\nsecond\n", encoding="utf-8")
    manager = LspManager(str(tmp_path), create_lsp_client)

    class FakeClient:
        capabilities = {"documentRangeFormattingProvider": True}

        async def request(self, method, params, timeout):
            return [{
                "range": {
                    "start": {"line": 1, "character": 99},
                    "end": {"line": 1, "character": 99},
                },
                "newText": "changed",
            }]

    async def fake_open_document(_file_path):
        return FakeClient(), target.as_uri()

    monkeypatch.setattr(manager, "open_document", fake_open_document)

    with pytest.raises(LspError, match="invalid document position"):
        await manager.formatted_range_text(
            "sample.py",
            LspRange(
                start=LspPosition(line=0, character=0),
                end=LspPosition(line=2, character=0),
            ),
        )
