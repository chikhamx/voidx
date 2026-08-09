from __future__ import annotations

import pytest

from voidx.agent.domain.subagent import (
    AgentGatewayError,
    AgentRun,
    ensure_control_route,
    ensure_open_send,
    ensure_send_route,
    finish_run,
)


def _run(run_id: str, *, session_id: str = "s1", parent_run_id: str = "", agent_type: str = "sub", status: str = "running") -> AgentRun:
    return AgentRun(
        run_id=run_id,
        session_id=session_id,
        parent_run_id=parent_run_id,
        agent_type=agent_type,
        agent_name=run_id,
        description=run_id,
        status=status,
        created_at=1.0,
        updated_at=1.0,
    )


def test_send_route_allows_parent_child_both_directions():
    root = _run("root", agent_type="root")
    child = _run("child", parent_run_id="root")

    ensure_send_route(root, child)
    ensure_send_route(child, root)


def test_send_route_rejects_siblings_and_cross_session():
    child = _run("child", parent_run_id="root")
    sibling = _run("sibling", parent_run_id="root")
    other = _run("other", session_id="s2", parent_run_id="root2")

    with pytest.raises(AgentGatewayError, match="Route not allowed"):
        ensure_send_route(child, sibling)
    with pytest.raises(AgentGatewayError, match="same session"):
        ensure_send_route(child, other)


def test_control_route_only_allows_root_to_direct_child():
    root = _run("root", agent_type="root")
    child = _run("child", parent_run_id="root")
    grandchild = _run("grandchild", parent_run_id="child")

    ensure_control_route(root, child)
    with pytest.raises(AgentGatewayError, match="Route not allowed"):
        ensure_control_route(root, grandchild)


def test_terminal_runs_cannot_send_or_receive():
    running = _run("running")
    terminal = _run("terminal", status="completed")

    with pytest.raises(AgentGatewayError, match="Source run is terminal"):
        ensure_open_send(terminal, running)
    with pytest.raises(AgentGatewayError, match="Target run is terminal"):
        ensure_open_send(running, terminal)


def test_finish_run_normalizes_string_result_and_is_idempotent():
    running = _run("child", parent_run_id="root")

    finished = finish_run(running, status="completed", result="done", now=2.0)
    unchanged = finish_run(finished, status="failed", error="late", now=3.0)

    assert finished.status == "completed"
    assert finished.result == {"result": "done"}
    assert finished.updated_at == 2.0
    assert unchanged == finished


def test_finish_run_copies_result_payload():
    running = _run("child", parent_run_id="root")
    payload = {"result": "before"}

    finished = finish_run(running, status="completed", result=payload, now=2.0)
    payload["result"] = "after"

    assert finished.result == {"result": "before"}


def test_gateway_error_preserves_message_and_defaults_reason():
    error = AgentGatewayError("Readable gateway failure")

    assert str(error) == "Readable gateway failure"
    assert error.reason == "gateway_error"


def test_route_errors_expose_stable_reasons():
    child = _run("child", parent_run_id="root")
    sibling = _run("sibling", parent_run_id="root")
    other = _run("other", session_id="s2", parent_run_id="root2")

    with pytest.raises(AgentGatewayError, match="Route not allowed") as route_error:
        ensure_control_route(child, sibling)
    assert route_error.value.reason == "route_not_allowed"

    with pytest.raises(AgentGatewayError, match="same session") as session_error:
        ensure_control_route(child, other)
    assert session_error.value.reason == "cross_session"
