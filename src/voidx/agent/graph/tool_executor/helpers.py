from __future__ import annotations

import asyncio
import json
import inspect
import os

from langchain_core.messages import ToolMessage

from voidx.agent.tool_messages import sanitize_tool_message_content
from voidx.tools.service import ToolResult, UserInteraction, UserResponse

from .types import (
    AGENT_RESULT_PREVIEW_CHARS,
    AGENT_RESULT_PREVIEW_LINES,
    ToolResultOk,
    _ExecutedTool,
    _goal_for_state,
    _task_state_for_state,
    _todo_state_for_state,
    _workflow_runs_for_state,
)
from .guards import _split_runtime_guard_blocked_calls, _restore_runtime_guard_blocked_results


def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    if app is not None and callable(getattr(app, "invalidate", None)):
        app.invalidate()


_OTHER_VALUE_PREFIX = "__voidx_choice_prompt_other__"



class _FileRWLock:
    """Async per-file read-write lock.

    Multiple readers may hold the lock concurrently.
    Writers wait for all readers to finish and get exclusive access.
    Built on asyncio.Condition for correct cross-task coordination.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer_active = False

    async def acquire_read(self) -> None:
        async with self._condition:
            while self._writer_active:
                await self._condition.wait()
            self._readers += 1

    async def release_read(self) -> None:
        async with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    async def acquire_write(self) -> None:
        async with self._condition:
            while self._writer_active or self._readers > 0:
                await self._condition.wait()
            self._writer_active = True

    async def release_write(self) -> None:
        async with self._condition:
            self._writer_active = False
            self._condition.notify_all()


def _extract_file_paths(tool_call: dict) -> list[str]:
    """Extract file paths from a tool call for per-file locking."""
    name = tool_call.get("name", "")
    args = tool_call.get("args") or {}
    paths: list[str] = []

    if name in {"read", "write", "replace"}:
        fp = args.get("file_path")
        if isinstance(fp, str) and fp:
            paths.append(fp)
    elif name == "file":
        fp = args.get("file_path")
        if isinstance(fp, str) and fp:
            paths.append(fp)
        dp = args.get("dest_path")
        if isinstance(dp, str) and dp:
            paths.append(dp)
    elif name == "manage":
        op = args.get("op", "")
        if op in {"create", "delete"}:
            raw = args.get("paths")
            if isinstance(raw, str):
                paths.append(raw)
            elif isinstance(raw, list):
                paths.extend(p for p in raw if isinstance(p, str) and p)
        elif op == "move":
            for move in args.get("moves") or []:
                if not isinstance(move, dict):
                    continue
                src = move.get("src")
                dest = move.get("dest")
                if isinstance(src, str) and src:
                    paths.append(src)
                if isinstance(dest, str) and dest:
                    paths.append(dest)

    return [os.path.normpath(p) for p in paths]


def _dedupe_repeated_read_calls(tool_calls: list[dict]) -> tuple[list[dict], dict[str, str]]:
    unique: list[dict] = []
    duplicate_sources: dict[str, str] = {}
    seen_reads: dict[str, str] = {}
    for call in tool_calls:
        if call.get("name") != "read":
            seen_reads.clear()
            unique.append(call)
            continue

        key = _read_call_key(call)
        call_id = str(call.get("id") or "")
        if key in seen_reads and call_id:
            duplicate_sources[call_id] = seen_reads[key]
            continue

        seen_reads[key] = call_id or "earlier read"
        unique.append(call)
    return unique, duplicate_sources


def _read_call_key(tool_call: dict) -> str:
    args = tool_call.get("args") or {}
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _restore_deduped_read_results(
    original_calls: list[dict],
    unique_executed: list[_ExecutedTool],
    duplicate_sources: dict[str, str],
) -> list[_ExecutedTool]:
    if not duplicate_sources:
        return unique_executed

    restored: list[_ExecutedTool] = []
    unique_iter = iter(unique_executed)
    for call in original_calls:
        call_id = str(call.get("id") or "")
        source = duplicate_sources.get(call_id)
        if source is None:
            restored.append(next(unique_iter))
            continue
        output = f"Skipped duplicate read; same arguments already requested in tool call {source}."
        restored.append(_ExecutedTool(
            message=ToolMessage(content=output, tool_call_id=call_id),
            result=ToolResult(
                title="read: duplicate skipped",
                output=output,
                metadata={"deduplicated": True, "duplicate_of": source},
            ),
            tool_call=call,
        ))
    return restored


def _parallel_subagent_limit(config) -> int:
    parallel = getattr(config, "parallel_subagents", None)
    if not bool(getattr(parallel, "enabled", False)):
        return 1
    raw = getattr(parallel, "max_concurrent", 4)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 4


def _agent_result_preview(text: object) -> str:
    raw = str(text)
    stripped = raw.strip()
    if not stripped:
        return raw

    lines = stripped.splitlines()
    visible = lines[:AGENT_RESULT_PREVIEW_LINES]
    omitted_lines = max(0, len(lines) - len(visible))

    preview = "\n".join(visible)
    omitted_chars = max(0, len(preview) - AGENT_RESULT_PREVIEW_CHARS)
    if omitted_chars:
        preview = preview[:AGENT_RESULT_PREVIEW_CHARS].rstrip()

    suffixes = []
    if omitted_lines:
        suffixes.append(f"{omitted_lines} more lines")
    if omitted_chars:
        suffixes.append(f"{omitted_chars} more chars")
    if suffixes:
        preview = f"{preview}\n... ({'; '.join(suffixes)} omitted; full result passed to voidx)"
    return preview




_WORKSPACE_WRITE_LOCK_TOOLS = {"file", "write", "replace", "checkpoint"}
_WORKSPACE_WRITE_LOCK_CAPABILITIES = {"file_write", "bash_write", "git_write", "agent_implement"}


def _workspace_write_lock_manager(host: object):
    gateway_session = getattr(host, "_gateway_session", None)
    if gateway_session is None:
        return None
    return getattr(gateway_session, "_run_manager", None)


def _requires_workspace_write_lock(tool_call: dict) -> bool:
    name = str(tool_call.get("name") or "")
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    if name in _WORKSPACE_WRITE_LOCK_TOOLS:
        return True
    try:
        from voidx.permission.rules import classify_tool_call

        classified = classify_tool_call({**tool_call, "name": name, "args": args})
    except Exception:
        return name in {"bash", "powershell", "git", "agent"}
    return str(classified.capability.value) in _WORKSPACE_WRITE_LOCK_CAPABILITIES
def _is_barrier_tool(tool_call: dict) -> bool:
    return tool_call.get("name") in {"clarify", "checkpoint", "workflow", "compact"}


def _split_at_first_barrier(tool_calls: list[dict]) -> tuple[list[dict], dict | None, list[dict]]:
    for index, tool_call in enumerate(tool_calls):
        if _is_barrier_tool(tool_call):
            return tool_calls[:index], tool_call, tool_calls[index + 1:]
    return tool_calls, None, []


def _blocked_after_barrier_messages(
    tool_calls: list[dict],
    workspace: str,
    outcome: str,
) -> list[ToolMessage]:
    return [
        ToolMessage(
            content=sanitize_tool_message_content(
                f"Blocked because a prior runtime barrier was {outcome}.",
                workspace=workspace,
            ),
            tool_call_id=tc.get("id", ""),
            status="error",
        )
        for tc in tool_calls
    ]


async def _authorize_tool_calls(
    authorize,
    tool_calls: list[dict],
    *,
    runtime_persona: str = "coordinate",
    plan_mode: bool,
    session_id: str,
    interaction_mode: str | None,
    workflow_runs: object,
):
    kwargs = {
        "runtime_persona": runtime_persona,
        "plan_mode": plan_mode,
        "session_id": session_id,
        "interaction_mode": interaction_mode,
    }
    kwargs["workflow_runs"] = workflow_runs
    try:
        signature = inspect.signature(authorize)
    except (TypeError, ValueError):
        return await authorize(tool_calls, **kwargs)
    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return await authorize(tool_calls, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in params}
    return await authorize(tool_calls, **filtered)


def _make_interact_callback(app):
    if app is None:
        return None

    async def interact(request: UserInteraction) -> UserResponse:
        timeout = request.timeout
        if request.options and _is_tuple_options(request.options):
            other_value = _other_choice_value(request.options)
            choices = [*request.options, ("Other…", other_value, "Type a custom answer")]
            result = await app.ask_choice(request.prompt, choices, timeout=timeout)
            if result == other_value:
                result = await app.ask_text(request.prompt, timeout=timeout)
                if result is None:
                    return UserResponse(value="", cancelled=True)
                return UserResponse(value=result, free_text=True)
        elif request.options:
            suggestions = " / ".join(str(o) for o in request.options)
            prompt = f"{request.prompt} ({suggestions})"
            result = await app.ask_text(prompt, timeout=timeout)
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result, free_text=True)
        else:
            result = await app.ask_text(request.prompt, timeout=timeout)
            if result is None:
                return UserResponse(value="", cancelled=True)
            return UserResponse(value=result, free_text=True)
        if result is None:
            return UserResponse(value="", cancelled=True)
        return UserResponse(value=result)

    return interact


def _is_tuple_options(options: list[str | tuple[str, str, str]]) -> bool:
    # Mixed str/tuple lists are not allowed — routing is determined by the first element.
    return bool(options) and isinstance(options[0], tuple)


def _other_choice_value(options: list[str | tuple[str, str, str]]) -> str:
    used: set[str] = set()
    for opt in options:
        if isinstance(opt, tuple):
            used.add(opt[1])
        else:
            used.add(opt)
    value = _OTHER_VALUE_PREFIX
    index = 1
    while value in used:
        value = f"{_OTHER_VALUE_PREFIX}_{index}"
        index += 1
    return value


def _apply_state_update(
    update: dict,
    *,
    host,
    state_update: dict,
    runtime_task_state,
    runtime_persona_ref: list,
) -> None:
    """Apply a state update dict, mutating runtime variables in place.

    runtime_persona_ref is a mutable list [persona_str, intent_str] so we can update it.
    runtime_task_state is a mutable list [state, goal, workflow_runs_list].
    """
    from voidx.agent.todo_state import apply_todo_state_to_host
    from voidx.runtime.intent import TaskIntent
    from voidx.agent.task_state import WorkflowRoute

    if not update:
        return
    if "persona" in update:
        runtime_persona_ref[0] = update.get("persona") or runtime_persona_ref[0]
        state_update["persona"] = runtime_persona_ref[0]
    if "should_continue" in update:
        state_update["should_continue"] = bool(update.get("should_continue"))
    if "task_state" in update:
        runtime_task_state[0] = _task_state_for_state(update.get("task_state"))
        runtime_task_state[1] = runtime_task_state[0].current_goal
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    if "todo_state" in update:
        apply_todo_state_to_host(host, update.get("todo_state"))
        runtime_task_state[0].todo_state = _todo_state_for_state(update.get("todo_state"))
        state_update["todo_state"] = update.get("todo_state")
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    if "task_intent" in update:
        runtime_persona_ref[1] = update.get("task_intent") or "coding"
        runtime_task_state[0].current_intent = TaskIntent(runtime_persona_ref[1])
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    if "current_goal" in update:
        raw_goal = update.get("current_goal")
        runtime_task_state[1] = _goal_for_state(raw_goal)
        runtime_task_state[0].current_goal = runtime_task_state[1]
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    if "workflow_route" in update:
        raw_route = update.get("workflow_route")
        runtime_task_state[0].workflow_route = (
            WorkflowRoute.model_validate(raw_route)
            if raw_route
            else None
        )
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    if "workflow_runs" in update:
        runtime_task_state[2] = _workflow_runs_for_state(update.get("workflow_runs") or [])
        runtime_task_state[0].workflow_runs = {run.name: run for run in runtime_task_state[2]}
        state_update["task_state"] = runtime_task_state[0].model_dump(mode="json")
    # Sync runtime task state to host so status bar updates immediately
    if "task_state" in state_update:
        host._task_state = runtime_task_state[0].model_copy(deep=True)
        _invalidate_tui(host)


async def _execute_approved_batch(
    approved: list[dict],
    *,
    host,
    guard_state,
    execute_one_fn,
    serial: bool = False,
) -> list[_ExecutedTool]:
    """Execute a batch of approved tool calls with dedup and guard restoration."""
    if not approved:
        return []
    runnable, blocked = _split_runtime_guard_blocked_calls(approved, guard_state)
    unique_calls, duplicate_sources = _dedupe_repeated_read_calls(runnable)
    if serial:
        executed = []
        for tc in unique_calls:
            executed.append(await execute_one_fn(tc))
        restored = _restore_deduped_read_results(runnable, executed, duplicate_sources)
        return _restore_runtime_guard_blocked_results(approved, restored, blocked)

    agent_limit = _parallel_subagent_limit(host.config)
    agent_semaphore = __import__("asyncio").Semaphore(agent_limit)
    parallel_agent_count = sum(1 for tc in unique_calls if tc.get("name") == "agent")
    aggregate_status_id = ""
    show_parallel_status = agent_limit > 1 and parallel_agent_count > 1

    # --- file read-write lock manager (per-batch) ---
    file_lock_manager: dict[str, _FileRWLock] = {}

    def _get_rwlock(path: str) -> _FileRWLock:
        if path not in file_lock_manager:
            file_lock_manager[path] = _FileRWLock()
        return file_lock_manager[path]

    async def execute_one_file_locked(tc):
        paths = sorted(set(_extract_file_paths(tc)))
        is_write = tc.get("name") in ("write", "replace", "file")
        rw_locks: list[_FileRWLock] = []
        # Acquire locks in sorted order to avoid deadlock across tools
        try:
            for p in paths:
                lk = _get_rwlock(p)
                rw_locks.append(lk)
                if is_write:
                    await lk.acquire_write()
                else:
                    await lk.acquire_read()

            return await execute_one_fn(tc)
        finally:
            for lk, p in zip(rw_locks, paths):
                if is_write:
                    await lk.release_write()
                else:
                    await lk.release_read()

    async def execute_one_no_file_lock(tc):
        if tc.get("name") == "agent":
            async with agent_semaphore:
                return await execute_one_fn(tc)
        return await execute_one_fn(tc)

    # Split into file ops (rwlock) and non-file ops (bash, etc.).
    # Non-file ops must wait for all file ops to complete, so that
    # a compile/test bash never runs before pending file writes.
    file_calls: list[dict] = []
    other_calls: list[dict] = []
    for tc in unique_calls:
        if _extract_file_paths(tc):
            file_calls.append(tc)
        else:
            other_calls.append(tc)

    from voidx.runtime.ui import StatusFinished, StatusUpdated
    if show_parallel_status and host._ui.via_events():
        last = getattr(host, "_current_messages", [None])[-1]
        aggregate_status_id = f"parallel-subagents:{id(last)}:{id(unique_calls)}"
        await host._ui.events.emit(StatusUpdated(
            status_id=aggregate_status_id,
            label=f"Running {parallel_agent_count} child agents",
            stage="working",
        ))

    # Run file ops first (rwlock), then non-file ops (bash, etc.).
    # Results are collected back into original unique_calls order.
    call_index = {tc.get("id", id(tc)): i for i, tc in enumerate(unique_calls)}
    results: list = [None] * len(unique_calls)

    async def _run_and_place(file_group, executor_fn):
        if not file_group:
            return
        group_results = await __import__("asyncio").gather(
            *[executor_fn(tc) for tc in file_group]
        )
        for tc, result in zip(file_group, group_results):
            results[call_index[tc.get("id", id(tc))]] = result

    executed = []
    try:
        await _run_and_place(file_calls, execute_one_file_locked)
        await _run_and_place(other_calls, execute_one_no_file_lock)
        executed = [r for r in results if r is not None]
    finally:
        if aggregate_status_id:
            await host._ui.events.emit(StatusFinished(
                status_id=aggregate_status_id,
                label=f"Finished {parallel_agent_count} child agents",
            ))
    restored = _restore_deduped_read_results(runnable, executed, duplicate_sources)
    return _restore_runtime_guard_blocked_results(approved, restored, blocked)
