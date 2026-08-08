import io
import json
import logging


def test_external_log_bridge_routes_warning_and_restores_logging(tmp_path, monkeypatch):
    from voidx.observability.external import install_external_log_bridge

    namespace = "langchain_openai"
    namespace_logger = logging.getLogger(namespace)
    child_logger = logging.getLogger(f"{namespace}.chat_models._client_utils")
    root_logger = logging.getLogger()
    original_handlers = list(namespace_logger.handlers)
    original_propagate = namespace_logger.propagate
    original_root_handlers = list(root_logger.handlers)
    fallback_output = io.StringIO()
    fallback_handler = logging.StreamHandler(fallback_output)
    log_path = tmp_path / "agent_events.jsonl"

    monkeypatch.setattr(logging, "lastResort", fallback_handler)
    root_logger.handlers.clear()
    namespace_logger.handlers.clear()
    namespace_logger.propagate = True

    restore = install_external_log_bridge(namespace, log_path=log_path)
    try:
        child_logger.warning("langchain_openai.stream_chunk_timeout fired")
        assert fallback_output.getvalue() == ""

        entry = json.loads(log_path.read_text(encoding="utf-8"))
        assert entry["event"] == "python_warning"
        assert entry["tool_name"] == child_logger.name
        assert entry["message"] == "langchain_openai.stream_chunk_timeout fired"
    finally:
        restore()
        root_logger.handlers[:] = original_root_handlers
        namespace_logger.handlers[:] = original_handlers
        namespace_logger.propagate = original_propagate

    assert namespace_logger.handlers == original_handlers
    assert namespace_logger.propagate is original_propagate
