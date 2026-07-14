# Implementation Plan: LLM Error Message Extraction

> **Status: Done** — Archived on 2026-07-14.

## Goal
Extract the core error message from LLM exceptions to avoid displaying raw JSON/Dict in UI and logs.

## Architecture
We will introduce a helper function `_clean_error_message(exc: Exception) -> str` in `src/voidx/agent/graph/core/helpers.py`. This helper will parse the exception string to find any JSON/Dict structure, extract the `message` field, and format it with the original prefix. We will then apply this helper to format exceptions in `llm.py` and `subagent.py`.

## Tech Stack
- Python 3.11+
- `ast.literal_eval` and `json.loads` for parsing
- `pytest` for testing

## File Structure
- `src/voidx/agent/graph/core/helpers.py`: Implement `_clean_error_message`.
- `src/voidx/agent/graph/core/llm.py`: Apply `_clean_error_message` to LLM exception formatting.
- `src/voidx/agent/graph/subagent.py`: Apply `_clean_error_message` to subagent LLM exception formatting.
- `src/tests/test_agent/graph/test_call_llm_compaction.py`: Add unit tests for `_clean_error_message`.

## Tasks
- [ ] Task 1: Implement `_clean_error_message` in `src/voidx/agent/graph/core/helpers.py`.
- [ ] Task 2: Add unit tests for `_clean_error_message` in `src/tests/test_agent/graph/test_call_llm_compaction.py`.
- [ ] Task 3: Apply `_clean_error_message` in `src/voidx/agent/graph/core/llm.py`.
- [ ] Task 4: Apply `_clean_error_message` in `src/voidx/agent/graph/subagent.py`.
- [ ] Task 5: Run all tests to verify correctness.

## Tests
- Run unit tests: `./test.py --backend -- src/tests/test_agent/graph/test_call_llm_compaction.py`
- Run advanced tests: `./test.py --backend -- src/tests/test_agent/graph/test_call_llm_compaction_advanced.py`

## Risks
- Parsing failure: If the exception string is not valid JSON or Python dict, we fallback to `str(exc)`.
- Performance: `ast.literal_eval` and `json.loads` are fast and only run on exception paths, so performance impact is negligible.
