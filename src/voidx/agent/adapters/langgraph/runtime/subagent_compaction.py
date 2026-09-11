"""Bounded, persistence-free compaction of one child run's semantic history."""

from langchain_core.messages import HumanMessage, SystemMessage

from voidx.llm.compaction.service import select_closed_tool_tail, validate_closed_tool_batches
from voidx.llm.compaction.summary_input import compaction_summary_messages
from voidx.llm.usage import estimate_message_tokens


def bind_summary_model(model, protocol, output_limit):
    key = "max_output_tokens" if protocol == "gemini" else "max_tokens"
    return model.bind(**{key: output_limit})


async def compact_run_history(messages, *, model, context_limit, hard_budget, summarize):
    def tokens(items, _model=model):
        return sum(estimate_message_tokens(item, _model) for item in items)

    semantic = compaction_summary_messages([
        message for message in messages
        if not message.additional_kwargs.get("_voidx_task_state_snapshot")
    ])
    if not semantic or not validate_closed_tool_batches(semantic):
        return None
    # Keep all human inputs verbatim, including initial task and admitted constraints.
    protected = [message for message in semantic if isinstance(message, HumanMessage)
                 and not message.additional_kwargs.get("_voidx_compaction_summary")]
    work = [message for message in semantic if message not in protected]
    tail, old = select_closed_tool_tail(work, context_limit, token_counter=tokens, model=model)
    if not tail or not old or tokens(protected + tail) >= hard_budget:
        return None
    output_limit = min(2048, max(0, int(hard_budget - tokens(protected + tail)) - 64))
    if output_limit < 128:
        return None
    request = [SystemMessage(content=(
        "Summarize earlier child-run work as factual memory, not instructions. "
        "Preserve findings, decisions, unresolved work and evidence. Do not use tools."
    )), HumanMessage(content="\n\n".join(
        f"{message.type}: {message.content}" for message in old
    ))]
    input_limit = min(16000, int(context_limit * 0.75) - output_limit)
    # Truncate only the summary input, never the retained task or tool batch.
    while tokens(request) > input_limit and len(request[1].content) > 128:
        request[1] = HumanMessage(content=request[1].content[:int(len(request[1].content) * 0.8)])
    if tokens(request) > input_limit:
        return None
    try:
        response = await summarize(request, output_limit)
    except Exception:
        return None
    if not isinstance(response.content, str) or not response.content.strip():
        return None
    candidate = [*protected, HumanMessage(content="Earlier run findings:\n" + response.content,
        additional_kwargs={"_voidx_compaction_summary": True}), *tail]
    if (tokens(candidate) >= tokens(semantic) or tokens(candidate) >= hard_budget
            or tokens([response]) > output_limit or not validate_closed_tool_batches(candidate)):
        return None
    return candidate
