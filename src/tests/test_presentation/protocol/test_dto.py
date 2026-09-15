from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidx.presentation.output.tree import OutputTree
from voidx.presentation.protocol import (
    TranscriptSnapshot,
    UiChoiceRequest,
    UiPermissionRequest,
    UiResponse,
    UiSubmitCommand,
    UiTextRequest,
    export_protocol_schema,
    parse_ui_command,
    parse_ui_request,
    tree_to_snapshot,
)


def test_ui_choice_request_serializes_with_stable_kind():
    request = UiChoiceRequest(
        request_id="req_1",
        prompt="Interaction mode",
        choices=[
            ("Auto", "auto", "Let voidx decide"),
            ("Plan", "plan", "Design first"),
        ],
    )

    data = request.model_dump()

    assert data["kind"] == "choice"
    assert data["request_id"] == "req_1"
    assert data["choices"][1] == ("Plan", "plan", "Design first")
    assert parse_ui_request(data) == request


def test_ui_text_and_permission_requests_round_trip():
    text_request = UiTextRequest(
        request_id="req_text",
        prompt="API key",
        default="",
        secret=True,
    )
    permission_request = UiPermissionRequest(
        request_id="req_perm",
        prompt="Allow tool use?",
        choices=[("Yes", "y", "Allow once")],
        tools=[{"name": "bash", "pattern": "pytest", "args": {"command": "pytest"}}],
    )

    assert parse_ui_request(text_request.model_dump()) == text_request
    assert parse_ui_request(permission_request.model_dump()) == permission_request
    assert permission_request.tools[0].name == "bash"


def test_ui_response_round_trips_through_validation():
    response = UiResponse.model_validate({"request_id": "req_1", "value": "y"})

    assert response.model_dump() == {"request_id": "req_1", "value": "y"}


def test_ui_submit_command_serializes_with_stable_kind():
    command = UiSubmitCommand(text="hello web")
    data = command.model_dump()

    assert data == {"kind": "submit", "text": "hello web"}
    assert parse_ui_command(data) == command


def test_tree_to_snapshot_preserves_hierarchy_and_semantic_metadata():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="❯ hello")
    tool = tree.new_node(
        turn,
        node_type="tool_call",
        header="Reading",
        body_lines=["file_path=\"src/app.py\""],
        collapsed=True,
        status="done",
        elapsed=0.2,
        tool_call_id="call_1",
        payload={"tool_name": "read"},
    )
    tree.new_node(tool, node_type="tool_result", header="result", body_lines=["line 2"])

    snapshot = tree_to_snapshot(tree, session_id="session_1", revision=3)
    data = snapshot.model_dump()
    restored = TranscriptSnapshot.model_validate(data)

    assert restored.session_id == "session_1"
    assert restored.revision == 3
    assert [node.id for node in restored.nodes] == [turn.id, tool.id, tool.children[0].id]
    assert restored.nodes[1].parent_id == turn.id
    assert restored.nodes[1].title == "Reading"
    assert restored.nodes[1].tool_call_id == "call_1"
    assert restored.nodes[1].payload == {"tool_name": "read"}
    assert restored.nodes[2].body_lines == ["line 2"]


def test_tree_to_snapshot_accepts_checkpoint_nodes():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="❯ hello")
    checkpoint = tree.new_node(
        turn,
        node_type="checkpoint",
        header="● voidx plan",
        body_lines=["Plan: Add checkpoint node"],
        collapsed=False,
        status="running",
        payload={"interaction": "checkpoint", "checkpoint_id": "cp_1"},
    )

    snapshot = tree_to_snapshot(tree, session_id="session_1", revision=3)
    restored = TranscriptSnapshot.model_validate(snapshot.model_dump())

    assert restored.nodes[1].id == checkpoint.id
    assert restored.nodes[1].node_type == "checkpoint"
    assert restored.nodes[1].payload["checkpoint_id"] == "cp_1"


def test_v1_envelope_symbols_are_not_public_protocol_exports():
    import voidx.presentation.protocol as protocol

    assert not hasattr(protocol, "ProtocolEnvelope")
    assert not hasattr(protocol, "parse_protocol_envelope")

    with pytest.raises(ImportError):
        exec("from voidx.presentation.protocol import ProtocolEnvelope")
    with pytest.raises(ImportError):
        exec("from voidx.presentation.protocol import parse_protocol_envelope")


def test_protocol_schema_exports_v2_and_common_dto_definitions():
    schema = export_protocol_schema()

    assert schema["title"] == "VoidxUiProtocol"
    assert "ProtocolEnvelope" not in schema["$defs"]
    assert "JsonRpcRequest" in schema["$defs"]
    assert "JsonRpcNotification" in schema["$defs"]
    assert "JsonRpcResult" in schema["$defs"]
    assert "JsonRpcError" in schema["$defs"]
    assert "ErrorPayload" in schema["$defs"]
    assert "WorkspaceSnapshot" in schema["$defs"]
    assert "ThreadSnapshot" in schema["$defs"]
    assert "ThreadInfo" in schema["$defs"]
    assert "TurnInfo" in schema["$defs"]
    assert "Item" in schema["$defs"]
    assert "TranscriptSnapshot" in schema["$defs"]
    assert "UiChoiceRequest" in schema["$defs"]
    assert "UiSubmitCommand" in schema["$defs"]


def test_checked_in_frontend_protocol_schema_matches_backend_export():
    repo_root = Path(__file__).resolve().parents[4]
    schema_path = repo_root / "frontend" / "src" / "rpc" / "protocol.schema.json"

    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))

    assert checked_in == export_protocol_schema()


@pytest.mark.parametrize("node_type", ["clarify", "goal_spec", "loop_spec"])
def test_transcript_maps_hitl_transport_only_and_preserves_all_fields(node_type):
    from copy import deepcopy
    from voidx.presentation.protocol.transcript import TranscriptNode

    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="Question")
    card = tree.new_node(
        turn, node_type=node_type, header="Card", header_style="bold",
        body_lines=["First line", "Second line"], collapsed=True, status="done",
        elapsed=1.5, agent_name="agent", step_info="step", meta="meta",
        tool_call_id="call", agent_run_id="run", message_id=7,
        payload={"interaction": node_type, "nested": {"value": [1, 2]}},
    )
    answer = tree.new_node(card, node_type="message", header="Answer", body_lines=["yes"], status="done")
    before = deepcopy({key: value for key, value in vars(card).items() if key not in {"parent", "children"}})
    expected = {key: getattr(card, key) for key in TranscriptNode.model_fields
                if key not in {"node_type", "parent_id", "title", "child_ids"}}
    expected.update(node_type="checkpoint", parent_id=turn.id, title=card.header, child_ids=[answer.id])
    snapshot = tree_to_snapshot(tree, session_id="session", revision=9)
    assert snapshot.model_dump() == TranscriptSnapshot.model_validate(snapshot.model_dump()).model_dump()
    assert snapshot.session_id == "session" and snapshot.revision == 9
    assert snapshot.nodes[1].model_dump() == expected
    assert snapshot.nodes[2].parent_id == card.id
    assert snapshot.nodes[2].id == answer.id
    assert snapshot.nodes[2].body_lines == ["yes"]
    assert {key: value for key, value in vars(card).items() if key not in {"parent", "children"}} == before
    assert tree.root.children == [turn] and turn.children == [card]
    assert card.parent is turn and card.children == [answer] and answer.parent is card


def test_transcript_still_rejects_unknown_node_types():
    from pydantic import ValidationError

    tree = OutputTree()
    tree.new_node(tree.root, node_type="unknown_hitl", payload={"interaction": "clarify"})
    with pytest.raises(ValidationError, match="unknown_hitl"):
        tree_to_snapshot(tree)


def hitl_render_snapshot():
    tree = OutputTree()
    turn = tree.new_node(tree.root, node_type="turn", header="❯ HITL", status="done")
    for kind, prompt in [("clarify", "Question: Which option?"),
                         ("goal_spec", "Goal: Ship safely"),
                         ("loop_spec", "Loop: Check every 12 seconds")]:
        card = tree.new_node(
            turn, node_type=kind, header="", status="done",
            body_lines=[f"[bold]{prompt}[/bold]", "Details: Keep all content", ""],
            payload={"interaction": kind, "decision": "approved"},
        )
        tree.new_node(
            card, node_type="message", header=f"[bold]Answer:[/bold] Accepted {kind}",
            status="done", payload={"full_width_user_row": True, "align_full_width_user_row": True},
        )
    return tree_to_snapshot(tree, session_id="hitl-render", revision=3).model_dump()


def test_transcript_frontend_hitl_fixture_matches_backend_snapshot():
    fixture = Path(__file__).resolve().parents[4] / "frontend/test/fixtures/hitl-transcript.json"
    assert json.loads(fixture.read_text()) == hitl_render_snapshot()
