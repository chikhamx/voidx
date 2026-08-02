from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.slash import SlashHandler
from tests.test_slash.context import command_context


def _capture_output(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr("voidx.agent.slash.handler.ui.print", lambda text="": output.append(str(text)))
    monkeypatch.setattr("voidx.agent.slash.handler.ui.error", lambda text="": output.append(f"ERROR: {text}"))
    return output


class FakeLoopService:
    def __init__(self) -> None:
        self.started: tuple[str | None, object] | None = None
        self.stopped: str | None = None
        self._status = None

    async def start(self, parent_thread_id: str | None, spec):
        self.started = (parent_thread_id, spec)
        self._status = SimpleNamespace(
            active=True,
            mode=spec.mode.value,
            interval_seconds=spec.interval_seconds,
            loop_thread_id=spec.loop_thread_id(parent_thread_id),
        )
        return self._status

    async def status(self, parent_thread_id: str | None):
        return self._status

    async def stop(self, parent_thread_id: str | None):
        self.stopped = parent_thread_id
        self._status = None
        return True


def _host(tmp_path, service=None):
    return SimpleNamespace(
        workspace=str(tmp_path),
        loop_service=service,
        session=SimpleNamespace(id="session-1"),
    )


@pytest.mark.asyncio
async def test_loop_command_starts_fixed_loop(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeLoopService()

    assert await SlashHandler(_host(tmp_path, service)).dispatch("/loop 5m check build") is True

    assert service.started is not None
    parent_id, spec = service.started
    assert parent_id == "session-1"
    assert spec.prompt == "check build"
    assert spec.interval_seconds == 300
    assert any("every 300s" in line for line in output)


@pytest.mark.asyncio
async def test_loop_command_starts_dynamic_loop(tmp_path, monkeypatch) -> None:
    _capture_output(monkeypatch)
    service = FakeLoopService()

    assert await SlashHandler(_host(tmp_path, service)).dispatch("/loop check deploy") is True

    assert service.started is not None
    _, spec = service.started
    assert spec.prompt == "check deploy"
    assert spec.interval_seconds is None


@pytest.mark.asyncio
async def test_loop_stop_and_status(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeLoopService()
    handler = SlashHandler(_host(tmp_path, service))

    await handler.dispatch("/loop 5m check build")
    await handler.dispatch("/loop status")
    await handler.dispatch("/loop stop")

    assert service.stopped == "session-1"
    assert any("active" in line for line in output)
    assert any("stopped" in line for line in output)


@pytest.mark.asyncio
async def test_loop_unavailable_without_service(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    handler = SlashHandler(_host(tmp_path, service=None))

    assert await handler.dispatch("/loop check build") is True

    assert any("not available" in line for line in output)


@pytest.mark.asyncio
async def test_loop_requires_prompt(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    service = FakeLoopService()
    handler = SlashHandler(_host(tmp_path, service))

    await handler.dispatch("/loop 5m")

    assert service.started is None
    assert any("requires a prompt" in line for line in output)


@pytest.mark.asyncio
async def test_loop_start_value_error_is_reported_as_ui_error(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)

    class FailingService(FakeLoopService):
        async def start(self, parent_thread_id, spec):
            from voidx.agent.loop.prompt_materialize import PromptMaterializeError

            raise PromptMaterializeError("Referenced path not found: missing.md")

    handler = SlashHandler(_host(tmp_path, FailingService()))

    assert await handler.dispatch("/loop handle @missing.md") is True
    assert any(line.startswith("ERROR:") and "missing.md" in line for line in output)


@pytest.mark.asyncio
async def test_loop_start_runtime_error_is_reported_as_ui_error(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)

    class FailingService(FakeLoopService):
        async def start(self, parent_thread_id, spec):
            raise RuntimeError("loop failed during first iteration: provider_timeout")

    handler = SlashHandler(_host(tmp_path, FailingService()))

    assert await handler.dispatch("/loop check build") is True
    assert any(line.startswith("ERROR:") and "provider_timeout" in line for line in output)


@pytest.mark.asyncio
async def test_loop_resume_calls_service_and_prints_status(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)

    class ResumableService(FakeLoopService):
        async def resume(self, parent_thread_id):
            self._status = SimpleNamespace(
                active=True, mode="dynamic", interval_seconds=None,
                loop_thread_id="loop:session-1:20260728-01",
            )
            return self._status

    service = ResumableService()

    assert await SlashHandler(_host(tmp_path, service)).dispatch("/loop resume") is True

    assert any("resumed" in line for line in output)


@pytest.mark.asyncio
async def test_loop_resume_without_history_prints_hint(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)

    class EmptyService(FakeLoopService):
        async def resume(self, parent_thread_id):
            return None

    assert await SlashHandler(_host(tmp_path, EmptyService())).dispatch("/loop resume") is True

    assert any("No previous /loop" in line for line in output)


@pytest.mark.asyncio
async def test_loop_without_args_switches_profile(tmp_path, monkeypatch) -> None:
    switches: list[str] = []

    class Handler(SlashHandler):
        async def _switch_profile(self, profile: str) -> None:
            switches.append(profile)

    assert await Handler(_host(tmp_path)).dispatch("/loop") is True

    assert switches == ["loop"]


@pytest.mark.asyncio
async def test_loop_help_still_prints_usage(tmp_path, monkeypatch) -> None:
    output = _capture_output(monkeypatch)
    switches: list[str] = []

    class Handler(SlashHandler):
        async def _switch_profile(self, profile: str) -> None:
            switches.append(profile)

    assert await Handler(_host(tmp_path)).dispatch("/loop help") is True

    assert switches == []
    assert any("Usage:" in line for line in output)
