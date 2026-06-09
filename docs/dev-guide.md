# voidx Developer Guide

## Exception Handling

- Avoid `except Exception: pass`.
- Use the narrowest practical exception type for fallback paths.
- Add debug logging for silent fallback paths unless the exception is expected at very high frequency.
- Preserve cancellation semantics: do not catch `BaseException`; re-raise `asyncio.CancelledError` when caught explicitly.
- Include enough context in log messages to identify the file, provider, backend, or subsystem involved.
