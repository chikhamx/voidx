import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.frontend import UiController, UiFrontend
from voidx.ui.output.events.schema import (
    AssistantStreamUpdated,
    CheckpointDecisionSubmitted,
    CheckpointChoicePayload,
    CheckpointPlanPayload,
    CheckpointPromptShown,
)
from voidx.ui.protocol import (
    ProtocolEnvelope,
    TranscriptSnapshot,
    UiCommandEnvelope,
    UiEventEnvelope,
    UiChoiceRequest,
    UiHello,
    UiHelloEnvelope,
    UiPermissionRequest,
    UiRequestEnvelope,
    UiResponse,
    UiSubmitCommand,
    export_protocol_schema,
    parse_protocol_envelope,
    parse_ui_command,
    UiTextRequest,
    tree_to_snapshot,
    parse_ui_request,
)
from voidx.ui.output.tree import OutputTree


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


def test_frontend_protocol_surface_is_tui_independent():
    assert getattr(UiFrontend, "_is_protocol", False)
    assert getattr(UiController, "_is_protocol", False)
    assert hasattr(UiFrontend, "emit")
    assert hasattr(UiFrontend, "request")
    assert hasattr(UiFrontend, "run")
    assert hasattr(UiController, "submit_text")
    assert hasattr(UiController, "cancel")


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


def test_checkpoint_events_round_trip_through_protocol_envelope():
    shown = CheckpointPromptShown(
        checkpoint_id="cp_1",
        plan=CheckpointPlanPayload(
            plan_summary="Add checkpoint node",
            steps=["Add event", "Render node"],
            affected_files=["src/voidx/tools/plan_checkpoint.py"],
            risks=["Avoid duplicate JSON"],
        ),
        choices=[
            CheckpointChoicePayload(
                label="Implement directly",
                value="approved",
                description="Start implementing",
            )
        ],
    )
    submitted = CheckpointDecisionSubmitted(
        checkpoint_id="cp_1",
        decision="approved",
        label="Implement directly",
        response="Implement directly",
    )

    shown_envelope = UiEventEnvelope(seq=11, payload=shown)
    submitted_envelope = UiEventEnvelope(seq=12, payload=submitted)

    assert parse_protocol_envelope(shown_envelope.model_dump()) == shown_envelope
    assert parse_protocol_envelope(submitted_envelope.model_dump()) == submitted_envelope


def test_protocol_envelopes_parse_by_type_and_round_trip_payloads():
    event = UiEventEnvelope(
        seq=7,
        payload=AssistantStreamUpdated(text="hello"),
    )
    request = UiRequestEnvelope(
        seq=8,
        payload=UiChoiceRequest(
            request_id="req_1",
            prompt="Mode",
            choices=[("Auto", "auto", "")],
        ),
    )
    hello = UiHelloEnvelope(seq=0, payload=UiHello(client="web", last_seq=6))
    command = UiCommandEnvelope(seq=9, payload=UiSubmitCommand(text="hello"))

    assert parse_protocol_envelope(event.model_dump()) == event
    assert parse_protocol_envelope(request.model_dump()) == request
    assert parse_protocol_envelope(hello.model_dump()) == hello
    assert parse_protocol_envelope(command.model_dump()) == command


def test_protocol_schema_exports_contract_definitions():
    schema = export_protocol_schema()

    assert schema["title"] == "VoidxUiProtocol"
    assert "ProtocolEnvelope" in schema["$defs"]
    assert "TranscriptSnapshot" in schema["$defs"]
    assert "CheckpointPromptShown" in schema["$defs"]
    assert "CheckpointDecisionSubmitted" in schema["$defs"]
    assert "UiChoiceRequest" in schema["$defs"]
    assert "UiSubmitCommand" in schema["$defs"]
