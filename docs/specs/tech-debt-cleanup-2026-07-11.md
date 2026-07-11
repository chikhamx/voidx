---
name: tech-debt-cleanup
display_name: src/voidx 技术债清理
description: 基于 2026-07-11 全量扫描结果，分批清理 src/voidx 模块的技术债、冗余代码、死代码和代码规范问题
doc_type: implementation-spec
audience: llm
---

# src/voidx 技术债清理 — Implementation Spec

## Objective

修复 `src/voidx` 模块全量扫描发现的 34 项技术债问题（1 项误报已排除），按严重度分 3 批执行，每批独立可验证。

## Source of Truth

| Source | Path / Link | Notes |
|--------|-------------|-------|
| Scan Report | 本文档 | 2026-07-11 对 299 个文件、5.2 万行代码的全量扫描 |
| Existing Code | `src/voidx/` | 所有路径均基于实际文件确认 |
| Tests | `src/tests/` | 现有测试覆盖见各批次 Tests 节 |

## Current Behavior

- ~~`settings.py:149` 存在语法级 bug~~ — **误报**：`[redacted]` 是工具显示层脱敏，AST 确认实际代码为 `api_key = secrets_patch.get("api_key", "")`，无 bug
- `diffing.py` 和 `agent/attachments.py` 各自维护一份 `language_from_path`，映射表不同步
- tool 显示名/值提取逻辑散落在 `nodes.py`、`consumers.py`、`console/app.py` 三处，内容高度重叠但各有差异
- `status.py` 有 8 个结构完全相同的 `active_*_text` 函数
- `consumers.py:138-155` 的 `ResetRequested` 和 `TurnStarted` 两个 case 分支有完全相同的 6 行清理代码
- 6 处 `except Exception: pass/return None/return ""` 静默吞异常，无日志记录
- `diff.py` 有 2 个未使用 import（`StructuredDiff`、`diff_stat`）
- `server.py:232` 函数内重复 `import json`（顶部第 7 行已有）
- `console/app.py:62` 硬编码 `self._debug = True`
- `main.py:128` 用裸 `print()` 而非 `vconsole`
- `mcp/client/stdio_transport.py:116` 和 `mcp/client/base.py:152` 有多余的 `pass` 语句

## Target Behavior

- ~~settings.py 的 api_key 提取逻辑恢复正常~~ — 误报，无需修复
- 全项目只有一份 `language_from_path`，映射表合并所有后缀
- tool 显示名/值提取逻辑统一为公共函数
- `status.py` 的重复函数泛化为高阶函数
- 所有静默异常至少记录 warning 日志
- 无未使用 import、无重复 import、无多余 pass
- `console/app.py` 的 `_debug` 默认为 False
- `main.py` 的 version 输出统一用 `vconsole`

## Files to Change

### ~~Batch 1 — Bug 修复（阻断性）~~ — 误报，已移除

> `settings.py:149` 的 `[redacted]` 是工具显示层脱敏，AST 确认实际代码为 `secrets_patch.get("api_key", "")`，无 bug。

### Batch 2 — 冗余代码消除

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `src/voidx/diffing.py` | modify | 扩展 `language_from_path` 映射表，合并 `attachments.py` 的后缀（sh/tsx/jsx），保留原有后缀 | 不改变函数签名 `language_from_path(path: str) -> str`；不改变 `return mapping.get(suffix, "")` 的默认返回值 |
| `src/voidx/agent/attachments.py` | modify | 删除 `_language_from_path`（第 324-340 行），改为 `from voidx.diffing import language_from_path`，更新第 280 行调用点 | 不修改 `attachments.py` 的其他函数 |
| `src/voidx/ui/output/dock/nodes.py` | modify | 删除 `_tool_display_value`（409-438）、`_strip_rich_markup`（441-448），改为从新公共模块导入 | 不修改 `_tool_display_name`（376-406）——它有额外的 label_mapping 逻辑，暂不合并 |
| `src/voidx/ui/output/events/consumers.py` | modify | 删除 `_subagent_tool_detail`（611-634）、`_subagent_args_value`（637-645），改为从新公共模块导入；提取 `ResetRequested`/`TurnStarted` 的重复清理代码为 `_reset_turn_state()` 方法 | 不修改 `_subagent_tool_action`（586-608）——它的映射表与 `_tool_display_name` 语义不同（gerund vs noun），暂不合并 |
| `src/voidx/ui/output/tool_display.py` | create | 新建公共模块，包含 `extract_tool_display_value(tool_name, raw_args, args, *, short_path_limit=None)` 和 `strip_rich_markup(text)` | — |
| `src/voidx/ui/output/dock/status.py` | modify | 将 8 个 `active_*_text` / `active_*_detail_text` 函数泛化为 `_active_text(status_id, field)` 高阶函数，保留原函数名作为薄包装以维持向后兼容 | 不修改 `active_agent_step_text` 和 `active_guidance_preview_text`——它们有特殊逻辑 |

### Batch 3 — 静默异常 & 垃圾代码清理

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `src/voidx/selfupdate.py` | modify | 第 508-509 行和 518-519 行的 `except Exception: pass` 改为 `except Exception as exc: log_internal_error("selfupdate_marker", exc)` | 不修改 marker 文件的写入/删除逻辑 |
| `src/voidx/diffing.py` | modify | `git_diff`（294-309）和 `git_diff_stat`（312-324）的 `except Exception: return ""` 改为记录 warning 日志后返回空字符串 | 不改变返回值类型；不改变 timeout=10 |
| `src/voidx/llm/catalog.py` | modify | `_resolve_base_url`（203-204）和 `_resolve_api_key`（216-217）的静默 except 改为记录 warning 日志 | 不改变 fallback 逻辑 |
| `src/voidx/llm/instruction.py` | modify | 第 252-254 行的 `except Exception: return ""` 改为记录 warning 日志后清空缓存并返回空字符串 | 不改变缓存清理行为 |
| `src/voidx/ui/output/diff.py` | modify | 删除未使用的 `StructuredDiff` 和 `diff_stat` import（第 14-15 行） | 不删除其他 import |
| `src/voidx/ui/gateway/server.py` | modify | 删除第 232 行函数内重复的 `import json`；为 `parse_jsonrpc_message_str` 添加返回类型标注 `-> JsonRpcRequest | JsonRpcNotification | JsonRpcResult | JsonRpcError`（与 `parse_jsonrpc_message` 一致，需从 `envelope.py` 导入类型） | 不修改函数逻辑 |
| `src/voidx/mcp/client/stdio_transport.py` | modify | 删除第 116 行多余的 `pass`（已有 `log_tool_event` 调用） | 不修改 except 逻辑 |
| `src/voidx/mcp/client/base.py` | modify | 删除第 152 行多余的 `pass`（已有 `log_tool_event` 调用） | 不修改 except 逻辑 |

### Batch 4 — 代码规范小修

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `src/voidx/ui/output/console/app.py` | modify | 第 62 行 `self._debug = True` 改为 `self._debug = False` | 不修改 `set_debug` 方法和其他 `_debug` 使用点 |
| `src/voidx/main.py` | modify | 第 128 行 `print(f"voidx v{__version__}")` 改为 `vconsole = _vconsole(); vconsole.print(f"voidx v{__version__}")`；提取 `_print_version()` 公共函数供 `--version` flag 和 `version` 子命令共用 | 不修改其他 typer 选项 |
| `src/voidx/main.py` | modify | `_select_start_session`（第 24 行）移除未使用参数 `workspace`、`provider`、`model`、`new_session`，或添加下划线前缀；同步更新第 87-94 行的调用点 | 不修改函数的 resume 逻辑 |

## Invariants

- **不改变任何公共 API 的签名或返回值类型**
- **不改变任何工具的执行行为**——tool display 逻辑的统一只影响 UI 显示，不影响工具调用
- **不改变任何异常的返回值**——静默异常添加日志后，仍返回原来的 `None`/`""`/`False`
- **不引入新依赖**——日志使用已有的 `log_internal_error` 或 `log_tool_event`
- **language_from_path 的默认返回值保持为 `""`**（diffing.py 的语义），不改为 `suffix`（attachments.py 的旧语义）——因为 diff 渲染依赖空字符串来跳过语法高亮
- **status.py 的原函数名必须保留**作为薄包装，因为外部代码直接调用 `active_permission_request_text()` 等
- **_debug 改为 False 后**，确保现有测试不依赖 `_debug=True` 的默认行为——如有，在测试中显式 `set_debug(True)`

## Implementation Requirements

### Functional Requirements

- [ ] ~~Batch 1: settings.py:149 的 api_key 提取逻辑恢复~~ — 误报，无需修复
- [ ] Batch 2: 新建 `ui/output/tool_display.py`，提取 `extract_tool_display_value` 和 `strip_rich_markup`
- [ ] Batch 2: `nodes.py` 和 `consumers.py` 改为从 `tool_display.py` 导入
- [ ] Batch 2: `language_from_path` 合并所有后缀，`attachments.py` 复用 `diffing.language_from_path`
- [ ] Batch 2: `status.py` 泛化为高阶函数 + 薄包装
- [ ] Batch 2: `consumers.py` 提取 `_reset_turn_state()` 方法
- [ ] Batch 3: 所有静默异常添加日志记录
- [ ] Batch 3: 删除未使用 import、重复 import、多余 pass
- [ ] Batch 4: `_debug` 默认 False、version 输出统一、移除未使用参数

### Error Handling

- [ ] 静默异常添加日志时，使用 `log_internal_error(exc, context="category")`（签名：`log_internal_error(exc: BaseException, *, context: str)`）或 `log_tool_event("category", tool_name=..., message=str(exc))`，不使用 print
- [ ] 日志 context/category 字符串应具有可搜索性，如 `"selfupdate_marker_write"`、`"diffing_git_diff"`、`"llm_resolve_base_url"`、`"llm_resolve_api_key"`、`"instruction_read_file"`

### Data / Migration Requirements

- [ ] N/A — 无数据迁移

### API / Compatibility Requirements

- [ ] `language_from_path` 的调用方（`ui/output/diff.py:38,59` 和 `agent/attachments.py:280`）在合并后行为一致
- [ ] `status.py` 的所有 `active_*_text` 函数名保持不变，外部调用无需修改
- [ ] `tool_display.py` 的 `extract_tool_display_value` 必须支持 `short_path_limit` 参数——当传入时，对结果调用 `short_path(..., limit=limit)`（consumers.py 的行为）；当不传入时，直接返回字符串（nodes.py 的行为）
- [ ] `extract_tool_display_value` 的 tool 分支必须合并两个函数的所有分支：`read`/`write`/`replace`/`edit`/`lsp`→file_path（consumers.py 有 `edit`，nodes.py 没有）；`grep`→`f"{pattern} in {include}"` 格式（nodes.py 独有，consumers.py 把 grep/glob 合并为 pattern/query）；`glob`→pattern（nodes.py 独有）；`agent`→agent/description（nodes.py 独有）；`checkpoint`→goal（nodes.py 独有）；`manage`→manage_display（两者共有）；`bash`/`powershell`→command（两者共有）；`git`→args（两者共有）；`webfetch`/`websearch`→url/query（两者共有）
- [ ] `strip_rich_markup` 使用 nodes.py 的 regex 实现（`re.sub(r"\[/?[A-Za-z0-9_#= .:-]+\]", "", text)`），因为 consumers.py 的 `_subagent_args_value` 不做 rich 标记去除——但 consumers.py 的调用点传入的 args 不含 rich 标记，所以 regex 实现对两者都安全

## Edge Cases

| Case | Required Behavior | Verification |
|------|-------------------|--------------|
| `language_from_path("file.sh")` | 返回 `"bash"`（来自 attachments.py 的映射） | `./test.py --backend -- src/tests/test_tools/test_diffing.py -k language` |
| `language_from_path("file.cpp")` | 返回 `"cpp"`（来自 diffing.py 的映射） | 同上 |
| `language_from_path("file.unknown")` | 返回 `""`（diffing.py 语义） | 同上 |
| `extract_tool_display_value("edit", raw_args, args)` | 返回 `raw_args.get("file_path")` 或 fallback（consumers.py 的 edit 支持） | 新增测试 |
| `extract_tool_display_value("grep", raw_args, args)` | 返回 `f"{pattern} in {include}"`（nodes.py 的 grep 特殊格式） | 新增测试 |
| `extract_tool_display_value("glob", raw_args, args)` | 返回 `raw_args.get("pattern")`（nodes.py 独有分支） | 新增测试 |
| `extract_tool_display_value("agent", raw_args, args)` | 返回 `raw_args.get("agent") or raw_args.get("description")`（nodes.py 独有分支） | 新增测试 |
| `extract_tool_display_value("checkpoint", raw_args, args)` | 返回 `raw_args.get("goal")`（nodes.py 独有分支） | 新增测试 |
| `extract_tool_display_value("grep", raw_args, args, short_path_limit=72)` | 返回 `short_path(pattern, limit=72)`（consumers.py 行为，grep/glob 合并为 pattern/query） | 新增测试 |
| `extract_tool_display_value("unknown_tool", raw_args, args)` | fallback 到 `strip_rich_markup(args)` 或 `short_path(args, limit=72)` | 新增测试 |
| `_debug = False` 时工具调用 | 不显示 debug 信息 | `./test.py --backend -- src/tests/test_agent/graph/` |
| ~~settings.py `action == "set"`~~ | 误报，现有代码已正确 | N/A |
| git 未安装时 `git_diff()` | 记录 warning 日志，返回 `""` | `./test.py --backend -- src/tests/test_tools/test_diffing.py` |

## Forbidden Changes

- Do not modify unrelated files.
- Do not change public API behavior unless listed above.
- Do not replace existing patterns when a local extension is sufficient.
- Do not add new dependencies.
- Do not refactor the long functions (>100 lines) in this spec — that is a separate future task. This spec only covers redundancy elimination, silent exception logging, and small code hygiene fixes.
- Do not merge `_TOOL_GERUND` (console/app.py), `_tool_display_name` (nodes.py), and `_subagent_tool_action` (consumers.py) into a single mapping — they serve different display contexts (gerund for console status, noun for dock node title, gerund phrase for subagent status). Merging them risks subtle UI regressions and is out of scope.
- Do not remove `_LEGACY_WORKING_HEADER_PREFIX` in `agent_placeholder.py` — the "Remove after 2026-09" marker has not expired yet.

## Tests

| Test Level | Command | Expected Result |
|------------|---------|-----------------|
| ~~Batch 1 Focused~~ | 误报，无需测试 | N/A |
| Batch 2 Focused | `./test.py --backend -- src/tests/test_tools/test_diffing.py src/tests/test_ui/output/ src/tests/test_ui/gateway/test_ui_events_dock_status.py src/tests/test_ui/gateway/test_ui_events_subagent.py -v` | All pass |
| Batch 3 Focused | `./test.py --backend -- src/tests/test_selfupdate/ src/tests/test_tools/test_diffing.py src/tests/test_llm/ src/tests/test_mcp/ -v` | All pass |
| Batch 4 Focused | `./test.py --backend -- src/tests/test_voidx_entrypoint.py src/tests/test_ui/ -v` | All pass |
| Regression (full backend) | `./test.py --backend` | All pass |
| Regression (full suite) | `./test.py` | All pass |

## Definition of Done

- [ ] All functional requirements are implemented.
- [ ] Existing invariants still hold.
- [ ] Edge cases above are covered by tests or documented manual checks.
- [ ] Verification commands pass with captured output.
- [ ] No unrelated files were changed.
- [ ] Each batch can be committed independently.

## Appendix — Out of Scope (Future Work)

以下问题在扫描中发现但不在本 spec 范围内，留作后续处理：

### 过长函数拆分（22 个函数 >100 行）

| File | Function | Lines |
|------|----------|-------|
| `agent/graph/core/llm.py:177` | `_call_llm` | 439 |
| `agent/graph/tool_executor/executor.py:73` | `execute_tools` | 401 |
| `agent/graph/subagent.py:56` | `run_subagent` | 379 |
| `agent/graph/turn_runner.py:98` | `run_once` | 355 |
| `ui/output/events/consumers.py:130` | `handle` | 334 |
| `agent/graph/run_loop.py:132` | `run` | 233 |
| `agent/graph/compaction_coordinator.py:149` | `compact_for_live_state` | 224 |
| `agent/message_trimming.py:299` | `trim_superseded_file_tools` | 213 |
| `ui/output/tree.py:445` | `_walk_render` | 197 |
| `ui/gateway/session/method/settings.py:18` | `_method_settings_update` | 172 |
| `agent/graph/tool_executor/helpers.py:520` | `_execute_approved_batch` | 156 |
| `tools/search.py:139` | `execute` | 154 |
| `tools/file/replace.py:288` | `_execute_text_replace` | 147 |
| `agent/graph/tool_executor/executor.py:173` | `execute_one` | 138 |
| `agent/slash/model.py:12` | `_model_new` | 133 |
| `tools/bash/hint/search.py:31` | `_hint_grep` | 121 |
| `agent/graph/core/voidx_graph.py:536` | `_subagent_runner` | 114 |
| `agent/goal_resolver.py:83` | `resolve_goal_for_turn` | 113 |
| `ui/output/tree.py:299` | `_incremental_render` | 105 |
| `selfupdate.py:179` | `perform_upgrade` | 105 |
| `tools/file/read.py:189` | `execute` | 102 |
| `agent/attachments.py:69` | `build_user_message_payload` | 102 |

### 过深嵌套

| File | Function | Nesting Depth |
|------|----------|---------------|
| `ui/output/dock/nodes.py:409` | `_tool_display_value` | 12 层（if/elif 链 + 内部 for 循环） |
| `ui/output/browse.py:81` | `_browse_windows` | 8 层 |
| `ui/output/browse.py:123` | `_browse_unix` | 9 层 |

### 缺少类型标注（约 20 处公共函数）

`ui/tools/clipboard_image.py:71`、`ui/tools/skill_picker.py:43`、`ui/tools/clipboard_text.py:25`、`ui/tools/code_ide.py:116,167`、`ui/session.py:26,46`、`ui/protocol/transcript.py:45`、`tools/file/replace_resolve.py:300`、`tools/file/state.py:218,332`、`tools/shell/common.py:85`、`tools/web/content.py:162,201`、`tools/powershell/sandbox.py:114`、`llm/provider.py:635`、`selfupdate.py:66`、`memory/context_frames.py:34`、`agent/tool_filters.py:28`

### 过期标记

`ui/output/dock/agent_placeholder.py:8` — "Remove after 2026-09"，当前 2026-07-11 尚未到期。2026-09 后移除 `_LEGACY_WORKING_HEADER_PREFIX` 及 `is_agent_placeholder_header` 中的兼容分支。

### 嵌入字符串脚本

`selfupdate.py:50` — `_VERIFICATION_PROBE` 是 66 行嵌入字符串形式的 Python 脚本，无法被 IDE/lint 工具分析。可考虑提取为独立 .py 文件并用 importlib 加载。
