import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.application.runtime_context import ContextCompiler, RuntimeContext
from voidx.agent.domain.prompt_contracts import ContextSection


def test_snapshot_data_normalizes_the_rendered_sections():
    context = RuntimeContext(sections=[], task_sections=[
        ContextSection(name="Task", content="  full goal\nconstraint  "),
        ContextSection(name="Empty", content="  "),
    ])
    assert context.snapshot_data == [{"name": "Task", "content": "full goal\nconstraint"}]
    assert context.render_task_context() == "VOIDX_RUNTIME_CONTEXT\n\n## Task\nfull goal\nconstraint"
    context.snapshot_data[0]["content"] = "changed"
    assert "changed" not in context.render_task_context()


@pytest.mark.parametrize("sections", [[], [ContextSection(name="Empty", content="  ")]])
def test_empty_snapshot_has_no_marker(sections):
    assert RuntimeContext(sections=[], task_sections=sections).render_task_context() == ""


@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize("messages", [[], [HumanMessage(content="task")], [AIMessage(content="answer")], [ToolMessage(content="result", tool_call_id="t")]])
def test_restore_only_compilation_never_injects_task_state(append, messages):
    context = RuntimeContext(sections=[], task_sections=[ContextSection(name="Task", content="goal")])
    compiled = ContextCompiler(context).compile_messages(messages, append_task_state=append, inject_task_state=False)
    assert compiled[1:] == messages
