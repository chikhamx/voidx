# 静默异常可观测性规范化设计

> **Status: Done**

## Problem

当前代码里存在多处静默 fallback：

- `except ...: pass`
- `except ...: continue`
- `except ...: return []`

其中一些是合理的容错路径，但完全没有日志，导致真实解析失败、网络 fallback 失败、文件读取失败时难以排查。

这份设计不做“工具执行失败统一处理”。工具执行层已经会把工具异常转换成 `ToolResult(metadata={"error": True})` 或向上抛出取消信号。本期只处理低风险 fallback/parse/search 类静默异常的可观测性。

## Goals

1. 审计本期范围内的静默异常处理。
2. 对合理 fallback 添加 `debug` 日志，保留正常路径行为。
3. 避免新增用户可见 warning，除非后续有明确产品要求。
4. 建立开发规范，避免后续新增无理由的静默异常。

## Non-goals

- 不改变正常返回值、fallback 行为或错误传播语义。
- 不改变工具执行主链路的异常转换策略。
- 不处理所有仓库里的 broad `except Exception`。
- 不重构 browse/search/repomap 的控制流。
- 不修改 SQLite migration、配置读取 fallback、asyncio cancellation 等已明确合理的静默路径。

## Audit Result

### Keep As-Is

| File | Pattern | Reason |
|------|---------|--------|
| `ui/tui/parser.py` | `except OSError: pass` | 关闭 pipe 时忽略错误，标准清理路径 |
| `ui/tui/app.py` | `except AttributeError: pass` | `__setattr__` 状态代理初始化路径 |
| `ui/tui/app.py` | `except asyncio.CancelledError: pass` | 等待已取消任务结束 |
| `tools/bash.py` | `except asyncio.TimeoutError: pass` | terminate 超时后继续 kill |
| `tools/search.py` | `except PermissionError: pass` | 递归搜索跳过无权限目录 |
| `memory/store.py` | `except sqlite3.OperationalError: pass` | 迁移列已存在时跳过 |
| `config/settings.py` | `except (json.JSONDecodeError, OSError): pass` | 配置损坏/缺失时返回空配置 |

### Change In This Phase

| File | Current pattern | Problem | Change |
|------|-----------------|---------|--------|
| `tools/websearch.py` | `except Exception: pass` in DuckDuckGo HTML parser | 解析失败完全不可见 | 保留 fallback，增加 `debug(..., exc_info=True)` |
| `llm/catalog.py` | `except Exception: pass` around dynamic model fetcher | fetcher 失败不可见 | 预期异常和意外异常都 debug 记录，然后 fallback 到静态列表 |
| `ui/output/browse.py` | `except (ValueError, IndexError): pass` | 鼠标 row 解析失败不可见，Unix decode 失败未被局部容错 | 增加 debug 日志，并覆盖 `UnicodeDecodeError` |
| `tools/search.py` | `except Exception: continue` while reading grep files | 单文件读取失败不可见 | 增加 debug 日志，继续搜索其他文件 |
| `tools/repomap.py` | `except Exception: return []` while reading Python symbols | 单文件符号解析失败不可见 | 增加 debug 日志，继续返回空 symbols |

## Design

### Logging Strategy

Use module-local loggers:

```python
import logging

_logger = logging.getLogger(__name__)
```

Use `debug` for all changes in this phase:

```python
_logger.debug("DuckDuckGo HTML parse failed", exc_info=True)
```

Rationale:

- These paths are fallback/capability-degradation paths.
- Default logging should preserve current user experience.
- `exc_info=True` keeps stack traces available for debug sessions.
- `warning` would be user-visible in some logging configurations and violates the “normal behavior unchanged” goal.

### DuckDuckGo Parser

`HTMLParser.feed()` is intentionally tolerant and modern Python does not expose a useful `HTMLParseError` for this path. Keep broad catch around parser feed, but make failure observable:

```python
try:
    parser.feed(html)
except Exception:
    _logger.debug("DuckDuckGo HTML parse failed", exc_info=True)
return parser.results()
```

### Model Catalog Fetcher

Dynamic model listing should still fallback to `STATIC_MODELS`.

Expected failures:

- `httpx.HTTPError`
- `asyncio.TimeoutError`
- `ValueError`

Unexpected failures also fallback, but are logged at debug level with a distinct message.

### Browse Mouse Row Parsing

Invalid mouse escape rows should keep being ignored. Add debug logging around:

- invalid `int(parts[2])`;
- invalid row lookup if applicable;
- Unix `buf.decode()` failure.

`IndexError` is not expected from `line_map.get(row - 1)`, but can remain harmless if local parsing code changes later.

### Grep File Read

Grep should continue after one unreadable or malformed file. Add debug logging with the file path:

```python
except Exception:
    _logger.debug("Failed to read file during grep: %s", f, exc_info=True)
    continue
```

### Repo Map Python Symbol Extraction

Repo map should continue returning an empty symbol list when one file cannot be read or parsed. Add debug logging with the path:

```python
except Exception:
    _logger.debug("Failed to extract Python symbols from %s", f, exc_info=True)
    return []
```

## Developer Guidance

Add a short exception-handling section to `docs/dev-guide.md` instead of `AGENTS.md`.

`AGENTS.md` controls agent behavior and should stay focused on repository working rules. The broader Python exception policy belongs in developer documentation.

Guidance:

- Avoid `except Exception: pass`.
- If a fallback intentionally ignores an exception, use the narrowest practical exception type.
- Add debug logging for silent fallback paths unless the error is expected at very high frequency.
- Preserve cancellation semantics: do not catch `BaseException`; re-raise `asyncio.CancelledError` when caught explicitly.
- Include enough context in the log message to identify the file/provider/backend.

## Implementation Plan

1. Update `tools/websearch.py` with debug logging for DuckDuckGo parse failures.
2. Update `llm/catalog.py` with debug logging for dynamic fetcher failures.
3. Update `ui/output/browse.py` with debug logging for invalid mouse row parsing/decode failures.
4. Update `tools/search.py` with debug logging for unreadable grep files.
5. Update `tools/repomap.py` with debug logging for Python symbol extraction failures.
6. Add/update `docs/dev-guide.md` with exception-handling guidance.
7. Add focused tests for log output and unchanged fallback behavior.

## Testing Plan

### `tests/test_tools/test_web_mcp.py`

- `test_duckduckgo_parser_logs_parse_failures`
  - monkeypatch parser feed to raise;
  - assert returns partial/empty results;
  - assert debug log contains parser failure.

### `tests/test_llm_catalog.py`

- `test_list_models_logs_expected_fetcher_failure_and_falls_back`
  - fetcher raises `httpx.HTTPError`;
  - assert static models returned;
  - assert debug log.
- `test_list_models_logs_unexpected_fetcher_failure_and_falls_back`
  - fetcher raises `RuntimeError`;
  - assert static models returned;
  - assert debug log.

### `tests/test_output_browse.py`

- `test_browse_unix_logs_invalid_mouse_row`
  - exercise small helper for mouse row handling, or extract helper if needed.

### `tests/test_tools/test_basic.py`

- `test_grep_logs_unreadable_file_and_continues`
  - simulate one file read failure;
  - assert matching readable file still returns results;
  - assert debug log includes failed file.
- `test_repomap_logs_python_symbol_extraction_failure`
  - simulate read failure in `_extract_python_symbols`;
  - assert `[]`;
  - assert debug log includes path.

## Acceptance Criteria

- [x] `tools/websearch.py` parser failures are debug-logged and still return parser results.
- [x] `llm/catalog.py` dynamic fetcher failures are debug-logged and still fallback to static/custom models.
- [x] `ui/output/browse.py` invalid mouse row/decode errors are debug-logged and still ignored.
- [x] `tools/search.py` grep read failures are debug-logged and search continues.
- [x] `tools/repomap.py` Python symbol extraction failures are debug-logged and return `[]`.
- [x] `docs/dev-guide.md` contains exception-handling guidance.
- [x] Focused tests pass.
