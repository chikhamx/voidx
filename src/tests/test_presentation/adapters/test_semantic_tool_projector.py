"""Second-batch projection contracts, including transactional retries."""
import pytest

from voidx.agent.domain import semantic_events as s
from voidx.agent.domain.display_policy import ToolDisplayMode, ToolDisplayPolicy, ToolDisplayRule
from voidx.presentation.adapters.semantic_event_projector import SemanticEventProjector
from voidx.presentation.output.console.formatting import format_tool_args, format_tool_title
from tests.test_presentation.adapters.test_semantic_event_projector import event, start, stream


def tool(cls, seq, **kwargs):
    return event(cls, seq, tool_call_id="call", name="read", **kwargs)


def test_tool_defaults_result_failure_policy_and_file():
    p = SemanticEventProjector(display_policy=ToolDisplayPolicy(rules={
        "read": ToolDisplayRule(tool_name="read", mode=ToolDisplayMode.SUMMARY, summary_max_lines=2)}))
    start(p)
    args = {"file_path": "a.py"}
    begun, = p.project(tool(s.ToolStarted, 2, arguments=args))
    assert begun.model_dump(exclude={"kind"}) == dict(thread_id="t", agent_id=-1,
        tool_call_id="call", label=format_tool_title("read"), args=format_tool_args(args),
        tool_name="read", raw_args=args, display_mode=ToolDisplayMode.SUMMARY, summary_max_lines=2)
    ended, = p.project(tool(s.ToolFinished, 3, elapsed=0.25, ok=False))
    assert (ended.label, ended.elapsed, ended.ok, ended.detail) == (format_tool_title("read"), .25, False, "")
    result, = p.project(tool(s.ToolResult, 4, summary="failed\nreason\nmore"))
    assert (result.text, result.collapsed, result.display_mode, result.summary_max_lines) == ("failed\nreason\nmore", False, ToolDisplayMode.SHOW, 2)
    diff, = p.project(event(s.FileChanged, 5, tool_call_id="call", path="a.py", diff="diff"))
    assert (diff.tool_call_id, diff.diff_text) == ("call", "diff")
    p.project(event(s.TurnCompleted, 6, usage={}))
    assert p.active_turn_count == 0


@pytest.mark.parametrize("name,ok,expected", [("read", True, "summary"), ("read", False, "show"), ("agent", False, "show"), ("agent", True, "hidden"), ("todo", False, "hidden")])
def test_display_resolution(name, ok, expected):
    policy = ToolDisplayPolicy(rules={name: ToolDisplayRule(tool_name=name, mode="hidden" if name != "read" else "show", auto_summary_lines=1)})
    p = SemanticEventProjector(display_policy=policy, tool_label=lambda name, started: "Running" if started else "Done")
    start(p)
    begun, = p.project(event(s.ToolStarted, 2, tool_call_id="c", name=name, arguments={}))
    assert begun.label == "Running"
    assert p.project(event(s.ToolFinished, 3, tool_call_id="c", name=name, elapsed=0, ok=ok))[0].label == "Done"
    assert p.project(event(s.ToolResult, 4, tool_call_id="c", name=name, summary="a\nb"))[0].display_mode == expected


def test_status_policy_lifecycle_and_atomic_failure():
    p = SemanticEventProjector()
    start(p)
    body = dict(status_id="status", stage="working", description="work", parent_tool_call_id=None)
    for cls in (s.StatusUpdated, s.StatusFinished):
        with pytest.raises(ValueError):
            p.project(event(cls, 2, **body, **({"ok": True, "result": "done"} if cls is s.StatusFinished else {})))
    out, = p.project(event(s.StatusStarted, 2, **body))
    assert (out.label, out.detail, out.stage, out.display, out.parent_tool_call_id) == ("work", "", "working", "tree_node", "")
    with pytest.raises(ValueError):
        p.project(event(s.StatusUpdated, 3, **{**body, "stage": "custom"}))
    p.project(event(s.StatusUpdated, 3, **{**body, "description": "progress"}))
    end, = p.project(event(s.StatusFinished, 4, **body, ok=False, result="failure"))
    assert (end.label, end.detail, end.ok, end.remove) == ("work", "failure", False, True)
    custom = SemanticEventProjector(status_policy=lambda payload: dict(stage="working", display="record_only", remove=False, label="Mapped"))
    start(custom)
    body["stage"] = "custom"
    out, = custom.project(event(s.StatusStarted, 2, **body))
    assert (out.stage, out.display, out.label) == ("working", "record_only", "Mapped")
    assert custom.project(event(s.StatusFinished, 3, **body, ok=True, result=""))[0].remove is False


def test_association_references_isolation_and_cleanup():
    p = SemanticEventProjector()
    start(p)
    start(p, thread="other")
    p.project(tool(s.ToolStarted, 2, arguments={}))
    invalid = [tool(s.ToolStarted, 3, arguments={}),
        event(s.ToolFinished, 3, tool_call_id="call", name="wrong", elapsed=0, ok=True),
        tool(s.ToolResult, 3, summary="too early"),
        event(s.FileChanged, 3, tool_call_id="missing", path="x", diff="x"),
        event(s.StatusStarted, 3, status_id="st", stage="working", description="", parent_tool_call_id="missing"),
        event(s.TurnCompleted, 3, usage={})]
    for e in invalid:
        with pytest.raises(ValueError):
            p.project(e)
    with pytest.raises(ValueError):
        p.project(tool(s.ToolFinished, 2, thread="other", elapsed=0, ok=True))
    p.project(stream(s.AssistantStreamStarted, 3))
    p.project(tool(s.ToolFinished, 4, elapsed=0, ok=True))
    with pytest.raises(ValueError, match="result_ref"):
        p.project(tool(s.ToolResult, 5, summary="summary", result_ref="blob"))
    p.project(tool(s.ToolResult, 5, summary="resolved"))
    with pytest.raises(ValueError, match="result_ref"):
        p.project(event(s.FileChanged, 6, tool_call_id="call", path="x", result_ref="blob"))
    p.project(event(s.FileChanged, 6, tool_call_id="call", path="x", diff="", result_ref="blob"))
    p.project(stream(s.AssistantCommitted, 7, text="answer"))
    p.project(event(s.TurnCompleted, 8, usage={}))
    p.project(event(s.TurnCancelled, 2, thread="other", reason="cancel"))
    assert p.active_turn_count == 0
    start(p)
    p.project(tool(s.ToolStarted, 2, arguments={}))
    p.project(event(s.TurnCancelled, 3, reason="cancel"))
    assert p.active_turn_count == 0


def test_todo_boundaries_and_complete_payload():
    p = SemanticEventProjector()
    start(p)
    body = dict(items=[dict(id="1", content="task", status="pending")], operation="write", boundary_id="b")
    with pytest.raises(ValueError):
        p.project(event(s.TodoCommitted, 2, **body))
    preview, = p.project(event(s.TodoUpdated, 2, **body))
    assert preview.items[0].model_dump() == body["items"][0]
    assert (preview.summary, preview.todo_op) == ("", "write")
    for changes in ({"boundary_id": "wrong"}, {"items": []}):
        with pytest.raises(ValueError):
            p.project(event(s.TodoCommitted, 3, **{**body, **changes}))
    assert p.project(event(s.TodoCommitted, 3, **body))[0].kind == "todo.committed"
    with pytest.raises(ValueError):
        p.project(event(s.TodoCleared, 4, **{**body, "operation": "clear"}))
    assert p.project(event(s.TodoCleared, 4, items=[], operation="clear", boundary_id="clear"))[0].kind == "todo.cleared"


def test_policy_exception_does_not_claim_tool_or_sequence():
    def label(name, started):
        if broken[0]:
            raise RuntimeError("policy failed")
        return name
    broken = [True]
    p = SemanticEventProjector(tool_label=label)
    start(p)
    with pytest.raises(RuntimeError, match="policy failed"):
        p.project(tool(s.ToolStarted, 2, arguments={}))
    broken[0] = False
    assert p.project(tool(s.ToolStarted, 2, arguments={}))[0].tool_call_id == "call"


@pytest.mark.parametrize("family", ["status", "todo"])
def test_active_auxiliary_blocks_completion_and_cancel_retires_state(family):
    p = SemanticEventProjector()
    start(p)
    if family == "status":
        body = dict(status_id="st", stage="working", description="", parent_tool_call_id=None)
        p.project(event(s.StatusStarted, 2, **body))
        with pytest.raises(ValueError):
            p.project(event(s.StatusStarted, 3, **body))
    else:
        body = dict(items=[], operation="read", boundary_id="b")
        p.project(event(s.TodoUpdated, 2, **body))
        with pytest.raises(ValueError):
            p.project(event(s.TodoUpdated, 3, **{**body, "boundary_id": "different"}))
    with pytest.raises(ValueError):
        p.project(event(s.TurnCompleted, 3, usage={}))
    p.project(event(s.TurnCancelled, 3, reason="stop"))
    assert p.active_turn_count == 0
    start(p)
    cls = s.StatusStarted if family == "status" else s.TodoUpdated
    p.project(event(cls, 2, **body))


def test_duplicate_finish_result_and_status_parent_changes_are_atomic():
    p = SemanticEventProjector()
    start(p)
    p.project(tool(s.ToolStarted, 2, arguments={}))
    p.project(tool(s.ToolFinished, 3, elapsed=0, ok=True))
    with pytest.raises(ValueError):
        p.project(tool(s.ToolFinished, 4, elapsed=0, ok=True))
    p.project(tool(s.ToolResult, 4, summary=""))
    with pytest.raises(ValueError):
        p.project(tool(s.ToolResult, 5, summary="duplicate"))
    body = dict(status_id="st", stage="working", description="", parent_tool_call_id="call")
    p.project(event(s.StatusStarted, 5, **body))
    with pytest.raises(ValueError):
        p.project(event(s.StatusUpdated, 6, **{**body, "parent_tool_call_id": None}))
    p.project(event(s.StatusFinished, 6, **body, ok=True, result=""))
    with pytest.raises(ValueError):
        p.project(event(s.StatusUpdated, 7, **body))
    p.project(event(s.TurnCompleted, 7, usage={}))
