# Smart Session Title Design

> **Status: Draft**

## Problem

Session titles are currently set to the raw text of the first user message (truncated to 80 chars). This produces poor titles — "看看这个", "帮我改一下 bug", or long pasted code snippets are not useful as session identifiers. There is no LLM-based title generation.

Additionally, empty or trivial sessions (those that never received a meaningful title) are persisted forever, cluttering the session list.

## Design

### Trigger

After the **first user message** in a session, automatically generate a concise title via LLM. Replace the current rule-based approach (`title_text[:80]`).

### Generation Method

Use the session's current LLM to generate the title. Send a minimal prompt with the user's first message and ask for a short summary title.

### Prompt

```
Generate a concise title (max 60 characters) for a coding session that starts with this user message. Output only the title, nothing else.

User message: {first_message}
```

This is a single-turn, non-streaming call — fast and cheap (typically < 50 output tokens).

### Flow

```
First user message arrives
  → Save message as usual
  → If session_msgs <= 1:
      1. Set temporary title from title_text[:80] (immediate, for UI responsiveness)
      2. Fire-and-forget: call LLM to generate smart title
      3. On success: update_title() with LLM result
      4. On failure: keep the temporary title
```

Key design decisions:

1. **Fire-and-forget** — the title generation runs as a background `asyncio.Task`. It does not block the agent turn or the user's next action. The temporary rule-based title provides immediate feedback.

2. **Non-streaming call** — use `model.ainvoke()` instead of `stream_llm()`. No need to stream a 60-char title.

3. **No tool binding** — the title generation call is a plain LLM call with no tools, no system prompt, no context. Minimal token cost.

4. **Graceful degradation** — if the LLM call fails (network error, no API key, timeout), the temporary rule-based title remains. No user-visible error.

5. **Title sanitization** — strip quotes, newlines, and leading/trailing whitespace from the LLM output. Truncate to 60 chars.

### Auto-Cleanup: Untitled Sessions

Sessions that still have the default title `"New session"` when the user exits are considered trivial and should be automatically deleted from the database.

**Rationale:**

- A session with title `"New session"` means the user never sent a message, or the smart title generation failed/hasn't completed yet
- These sessions add noise to `/list` and waste storage
- If the smart title generation succeeded, the session has a meaningful title and should be kept

**Logic:**

```
On exit (/exit, /quit, Ctrl+C):
  if session.title == "New session":
      delete_session(session.id)   ← cascade deletes all related data
  else:
      keep session as normal
```

The `sessions` table has `ON DELETE CASCADE` foreign keys on all related tables (`messages`, `turns`, `transcript_nodes`, `session_runtime_state`, `session_task_runs`, `message_runtime_snapshots`, `context_frames`), so `delete_session()` cleanly removes everything.

**Edge cases:**

| Case | Behavior |
|------|----------|
| Smart title generation still in-flight at exit | Title is still "New session" → delete |
| User ran `/clear` then exited | Title reset to "New session" → delete |
| User manually set `/title New session` | Deleted (edge case, acceptable) |
| User sent messages but LLM title failed | Title is the rule-based temp title (not "New session") → keep |

### Implementation

#### Step 1: Add `_generate_session_title` to `turn_mixin.py`

```python
async def _generate_session_title(self: GraphRunLoopHost, user_text: str) -> None:
    """Background task: generate a smart session title via LLM."""
    if self.model is None:
        return
    try:
        from langchain_core.messages import HumanMessage
        prompt = (
            "Generate a concise title (max 60 characters) for a coding session "
            "that starts with this user message. Output only the title, nothing else.\n\n"
            f"User message: {user_text[:500]}"
        )
        result = await asyncio.wait_for(
            self.model.ainvoke([HumanMessage(content=prompt)]),
            timeout=10.0,
        )
        title = str(result.content).strip().strip('"\'').strip()
        title = title.replace("\n", " ")
        if len(title) > 60:
            title = title[:57] + "..."
        if title and self._session is not None:
            await update_title(self._session.id, title)
            self._session = self._session.model_copy(update={"title": title})
    except Exception:
        pass  # keep temporary title
```

#### Step 2: Update auto-title logic in `_run_once`

File: `src/voidx/agent/graph/turn_mixin.py`

Replace the current auto-title block:

```python
# Before
if len(session_msgs) <= 1:
    title_source = payload.title_text
    title = title_source[:80] + ("..." if len(title_source) > 80 else "")
    await update_title(self._session.id, title)

# After
if len(session_msgs) <= 1:
    # Immediate temporary title
    title_source = payload.title_text
    title = title_source[:80] + ("..." if len(title_source) > 80 else "")
    await update_title(self._session.id, title)
    # Background smart title generation
    asyncio.create_task(self._generate_session_title(payload.title_text))
```

#### Step 3: Add auto-cleanup on exit

File: `src/voidx/agent/graph/run_loop.py`

In the `run()` method's `finally` block, before cleanup:

```python
# Before
finally:
    if gateway_server is not None:
        await gateway_server.stop()
    ...

# After
finally:
    # Auto-cleanup: delete sessions that never got a meaningful title
    if self._session is not None and self._session.title == "New session":
        from voidx.memory.session import delete_session
        await delete_session(self._session.id)
    if gateway_server is not None:
        await gateway_server.stop()
    ...
```

#### Step 4: Add `/title auto` command

Allow users to manually trigger smart title regeneration:

```python
# In SlashSessionMixin._set_title
async def _set_title(self, cmd: str) -> None:
    title = cmd.removeprefix("/title").strip()
    if title.lower() == "auto":
        # Trigger smart title generation from current conversation
        session = self._host_session()
        if session:
            # Read first user message and generate title
            ...
        return
    if title:
        # Manual title set (existing behavior)
        ...
```

This is a nice-to-have — the core value is the automatic generation.

#### Step 5: Tests

- Test smart title generation produces a valid title
- Test smart title generation failure keeps temporary title
- Test auto-cleanup: session with "New session" title is deleted on exit
- Test auto-cleanup: session with smart title is preserved on exit
- Test auto-cleanup: session with rule-based temp title (not "New session") is preserved

### Token Cost Estimate

| Item | Tokens |
|------|--------|
| System prompt | 0 (no system prompt) |
| User prompt template | ~30 |
| User message (truncated to 500 chars) | ~100-200 |
| LLM output (title) | ~10-20 |
| **Total per generation** | **~150-250** |

Negligible compared to a typical agent turn (thousands of tokens).

### Edge Cases

| Case | Behavior |
|------|----------|
| No API key / model not configured | Skip generation, keep temporary title |
| LLM call times out (10s) | Keep temporary title |
| LLM returns empty or garbage | Keep temporary title (sanitization rejects it) |
| User sends `/title` before LLM returns | Manual title wins, cancel background task |
| `/clear` resets session | New first message triggers generation again |
| User message is very long | Truncate to 500 chars in the prompt |

## Files Changed

| File | Change |
|------|--------|
| `src/voidx/agent/graph/turn_mixin.py` | Add `_generate_session_title`, update auto-title block |
| `src/voidx/agent/graph/run_loop.py` | Add auto-cleanup on exit |
| `src/voidx/agent/graph/contracts.py` | Add `_generate_session_title` to protocol |
| `src/voidx/ui/commands.py` | Add `/title auto` entry (optional) |
| `tests/` | Tests for title generation and auto-cleanup |

## Out of Scope

- Per-turn title updates (only first message triggers generation)
- Title generation for resumed sessions (could be a future enhancement)
- Caching/batching of title generation calls
- User preference to disable auto-cleanup (separate enhancement)
