# Smart Session Title Design

> **Status: Done**

Date: 2026-06-06

## Problem

Session titles are currently derived from the first user message by truncating
raw text. Titles like "看看这个", "帮我改一下 bug", or pasted code snippets are
not useful in `/list` or resume flows.

The codebase already has an internal hidden `title` agent and `TITLE_PROMPT`,
but it is not wired into session persistence. This design connects that existing
agent to the first meaningful user turn while preserving responsiveness and
avoiding session races.

Empty sessions should also be cleaned up, but cleanup must be based on database
facts, not title text. A session with messages must not be deleted just because
its title is still `"New session"`.

## Goals

- Generate concise LLM-based titles after the first user message.
- Do not block the user turn or the next input.
- Preserve the temporary rule-based title if generation fails.
- Ensure manual `/title` changes always win over background generation.
- Avoid updating the wrong session after `/clear`, `/resume`, or a new session.
- Delete only truly empty sessions on exit.

## Existing Building Blocks

| File | Existing role |
|------|---------------|
| `src/voidx/agent/agents.py` | Hidden `title` agent and `TITLE_PROMPT` already exist |
| `src/voidx/config/models.py` | `agent_max_steps.title` already exists |
| `src/voidx/agent/graph/turn_mixin.py` | Current first-message rule-based title update |
| `src/voidx/agent/slash/session.py` | `/title`, `/clear`, `/resume` session commands |
| `src/voidx/agent/graph/run_loop.py` | Exit cleanup path |
| `src/voidx/memory/session.py` | `update_title()`, `delete_session()`, `count_messages()` |

## Trigger

After the first user message in a new session:

1. Save the user message as usual.
2. Set a temporary title from `payload.title_text`, truncated to 80 chars.
3. Start a background smart-title task for that exact session id.
4. On success, replace the temporary title if the task is still valid.
5. On failure, keep the temporary title.

The first-message check should be based on the loaded session history before the
current turn. It should only schedule title generation when there were no prior
persisted user messages for this session.

## Title Generation

Reuse the existing hidden title agent prompt:

```python
from voidx.agent.agents import TITLE_PROMPT
```

The title call is:

- non-streaming;
- no tools;
- no project context;
- bounded by a timeout;
- limited to the first user message, truncated to a small prompt budget.

Prompt shape:

```text
System: TITLE_PROMPT
User: First user message:

{first_user_message[:500]}
```

The title task may use the parent session model. It should record token usage if
usage metadata is available, but failure to record usage must not fail the
title update.

## Race Protection

Every title generation task must capture:

- `session_id`;
- `generation_id`;
- first user text;
- the temporary title that was current when the task started.

The graph owns:

```python
_title_generation: int
_title_task: asyncio.Task[None] | None
```

Starting a new title task increments `_title_generation`, cancels any previous
task, and stores the new task.

Before applying a generated title, the task must verify:

1. the generation id still matches;
2. `self._session` still exists;
3. `self._session.id == session_id`;
4. the current title is still the same temporary title generated for this turn.

If any check fails, the task returns without updating the database. The final
database write must also be conditional on `title == temporary_title`, so an
external/manual title update that wins the race cannot be overwritten by a
stale background task. This makes manual `/title`, `/clear`, `/resume`, and
new-session transitions safe.

## Manual Title Wins

`set_session_title()` and `/title <text>` must invalidate any in-flight smart
title task before writing the manual title.

`/clear` and `resume_session()` must also invalidate title tasks because the
current session identity changed.

## `/title auto`

Add `/title auto` to regenerate a title for the current session.

Behavior:

1. If there is no active session, print a dim message and return.
2. Load the first persisted user message for the current session.
3. If there is no user message, print a dim message and return.
4. Set a temporary title from that first user message.
5. Start the same guarded background smart-title task.

This command is useful when a title generation failed or the user wants to undo
a manual title.

## Title Sanitization

Generated title rules:

- trim whitespace;
- strip surrounding single/double quotes;
- collapse internal whitespace and newlines to single spaces;
- reject empty output;
- reject titles containing markdown syntax such as fenced/inline code,
  headings, emphasis, links, or obvious bullet prefixes;
- truncate to 60 chars, using `...` when truncation is needed.

Manual `/title` should keep current behavior except that it invalidates the
background title task.

## Empty Session Cleanup

On run-loop exit, delete only the current active session if it is truly empty:

```text
if self._session exists and count_messages(self._session.id) == 0:
    delete_session(self._session.id)
    self._session = None
```

Do not delete sessions based only on title text.

This avoids deleting a real session whose title generation failed or whose
manual title happens to be `"New session"`.

`/clear` already detaches from the old session and schedules old-session storage
cleanup separately. This exit cleanup should not try to clean detached old
sessions.

## Implementation Plan

### Step 1: Title Helper

Add a title helper module or mixin, for example:

```text
src/voidx/agent/graph/title_mixin.py
```

Methods:

- `_schedule_session_title_generation(session_id, first_user_text, temporary_title)`
- `_generate_session_title(session_id, generation_id, first_user_text, temporary_title)`
- `_run_title_agent(first_user_text) -> str | None`
- `_sanitize_generated_title(raw) -> str`
- `_invalidate_session_title_generation()`
- `regenerate_session_title() -> bool`

### Step 2: Graph State

Initialize:

```python
self._title_generation = 0
self._title_task = None
```

Cancel/invalidate title tasks in:

- `set_session_title()`;
- `clear_current_session()`;
- `resume_session()`.

### Step 3: First-Turn Auto Title

In `_run_once()`, replace the existing auto-title block with:

```python
if is_first_user_message:
    temporary_title = _temporary_session_title(payload.title_text)
    await update_title(self._session.id, temporary_title)
    self._session = self._session.model_copy(update={"title": temporary_title})
    self._schedule_session_title_generation(
        self._session.id,
        payload.title_text,
        temporary_title,
    )
```

The `is_first_user_message` check should use persisted user-message history
before appending the current user message.

### Step 4: `/title auto`

Extend `SlashSessionMixin._set_title()`:

- if argument is `auto`, call `regenerate_session_title()`;
- otherwise keep the manual title path.

Add `/title auto` to the command palette.

### Step 5: Empty Current Session Cleanup

Add a graph method such as `_delete_empty_current_session()` and call it from
`GraphRunLoopMixin.run()` before external managers and UI are stopped.

Cleanup failures should be swallowed or logged as dim UI output; exit should not
hang or fail because cleanup failed.

## Tests

| Test | Description |
|------|-------------|
| `test_smart_title_generation_updates_matching_session` | Generated title updates current matching session |
| `test_smart_title_generation_failure_keeps_temporary_title` | Failed title call leaves temp title |
| `test_smart_title_does_not_update_after_clear` | Clear invalidates old title task |
| `test_smart_title_does_not_override_manual_title` | Manual `/title` wins over pending generation |
| `test_smart_title_does_not_update_resumed_session` | Resume invalidates old title task |
| `test_smart_title_requires_database_title_to_remain_temporary` | Generated title cannot overwrite a changed DB title |
| `test_title_auto_uses_first_user_message` | `/title auto` reads first user message and schedules generation |
| `test_sanitize_generated_title_rejects_markdown` | Generated markdown-like titles are rejected |
| `test_delete_empty_current_session_only_deletes_sessions_without_messages` | Empty cleanup is based on message count |
| `test_exit_cleanup_deletes_empty_current_session` | Run-loop exit deletes empty active session |
| `test_exit_cleanup_keeps_session_with_messages_even_new_session_title` | Non-empty session is preserved despite default title |
| `test_title_auto_dispatches_regenerator` | `/title auto` dispatches to the graph regenerator |
| `test_title_auto_without_user_message_prints_notice` | `/title auto` reports missing user messages |
| `test_title_auto_command_is_in_palette` | Command palette exposes `/title auto` |

## Acceptance Criteria

- First user message gets a temporary title immediately.
- Smart title generation runs in the background and never blocks the turn.
- Generated title updates only the matching still-current session.
- Manual titles, `/clear`, and `/resume` invalidate pending title updates.
- Empty-session cleanup deletes only truly empty current sessions.
- `/title auto` can regenerate the title from the first persisted user message.
