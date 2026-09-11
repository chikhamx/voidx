# Task-state history cache experiment

Use `/taskstate strip off` to retain previously sent task-state messages while testing prompt-cache reuse. `/taskstate strip on` restores the default behavior: remove old runtime state and render only the latest state. `/taskstate` opens the selector when an interactive frontend is available, or prints the status and usage. `/taskstate status` prints the current setting.

The experiment changes task-state history handling without changing the provider protocol or cache key. Retention mode sends the current task state as a separate user message. After a successful model response, the runtime remembers the sent state snapshots and their positions in the semantic history. Subsequent requests restore them before newly appended tool exchanges and user messages, then append the current state. Rebuilding or retrying a failed request does not commit another snapshot.

Snapshots are runtime-only and are not written into the user's conversation transcript. Restarting defaults to strip on. Switching sessions discards the retained snapshots; enabling strip also clears them. If earlier semantic history changes, such as after compaction or tool-result trimming, snapshots beyond the unchanged prefix are discarded rather than restored into unrelated context. Other sources of prefix changes, such as tool definitions and system instructions, are outside this switch.

Retaining snapshots consumes additional context and exposes earlier, potentially outdated task states to the model. The switch is a diagnostic experiment, not a guarantee of cache hits or a confirmed fix for upstream cache behavior.

## Comparing behavior

1. Use a fresh conversation with the same provider, model, and reasoning effort.
2. Run `/taskstate strip off` before the first task.
3. Ask for a task requiring several tool calls. Compare cache reads across those calls, rather than only across user messages.
4. Compare against a separate fresh conversation with the default `/taskstate strip on`.
5. Record input tokens, cached tokens, request times, and any history compaction. Switching modes during a conversation can itself change the prefix.

Focused tests cover tool-loop and new-user-message prefix retention, failed retries, repeated context reconstruction, session isolation, re-enabling stripping, history replacement, and command dispatch.
