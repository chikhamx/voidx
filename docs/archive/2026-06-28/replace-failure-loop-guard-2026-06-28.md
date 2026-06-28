# Replace 工具失败循环 Guard 归并逻辑优化

Date: 2026-06-28

> **Status: Done** — 实现已完成，`normalize_tool_args` 的 replace 分支已拆分，测试全部通过。

## Context

### 问题现象

`replace` 工具在连续失败 3 次后，第 4 次起会被 runtime guard 直接拦截，
工具根本不会执行，返回 "Runtime guard blocked repeated failed tool call"。
一旦进入拦截状态，对该文件的后续 replace 调用全部被挡，且无法自动解除，
形成"被拦 → 无法成功 → 无法解除 → 继续被拦"的死锁。

### 根因

拦截机制位于 `src/voidx/agent/graph/runtime_guards.py` 的 `ToolFailureLoopState`：

1. 每次工具失败，用 `FailureKey = (tool_name, normalized_args, error_kind)` 作为归并键。
2. 同一 key 连续失败时 `count` 累加。
3. 第 2 次失败返回 light 提示（不拦截）。
4. 第 3 次失败返回 stern 警告，并把 `call_key` 加入 `blocked_call_keys`。
5. 第 4 次起，`should_block` 命中黑名单，调用在执行前被拦截。

关键缺陷在 `normalize_tool_args`（`runtime_guards.py:286-288`）：

```python
if tool_name in {"read", "file", "write", "replace", "lsp_format"}:
    return str(args.get("file_path") or "")
```

对 `replace`，归并键**只取 `file_path`**，忽略 `start_no` / `end_no` /
`start_anchor` / `end_anchor` / `new_string`。后果：

- 对同一文件的不同行范围、不同内容的 replace 失败，只要 `error_kind` 归类相同
  （例如都落到 `unknown_error`），`FailureKey` 完全相同，被误判为"同一调用的重复失败"。
- 3 次失败后，**该文件上所有后续 replace 调用**都被拉黑，因为
  `call_key = "replace\x1f<file_path>"`，只要 file_path 相同就命中。

### 为什么 read/write/file 不受影响

- `read` / `file`（delete）/ `write`（create/move）对同一 path 的重复失败，
  语义上确实是"同一操作的重复失败"，归并合理。
- `replace` 的语义是"修改特定行范围"，同文件不同行范围是**不同操作**，
  当前归并粒度过粗，导致误伤。

## Goals and Non-Goals

### Goals

- 让 `replace` 的失败归并键反映"具体修改哪段"，而非仅"改哪个文件"。
- 同文件不同行范围的 replace 失败不再被合并计数、不再互相拉黑。
- 保留对"真正同一 replace 调用反复失败"的拦截能力（防 agent 死循环）。
- 不破坏现有 `read` / `file` / `write` / `lsp_format` 的归并行为。

### Non-Goals

- 不改动 `ToolFailureLoopState` 的计数阈值（2 次提示、3 次拉黑）。
- 不改动 `error_kind_from_result` 的分类逻辑。
- 不改动 `record_success` 的解除逻辑（仍按 tool_name 前缀清黑名单）。
- 不重构 `normalize_tool_args` 的整体结构，只调整 replace 分支。

## Architecture

### 当前数据流

```
replace 失败
  → build_failure_key(tool_call, result)
      → FailureKey(tool_name="replace",
                   normalized_args=normalize_tool_args("replace", args)
                                  = args["file_path"],          ← 过度归并
                   error_kind=error_kind_from_result(result))
  → ToolFailureLoopState.record_failure(key, ...)
      → 若 stable_key 与 last_key 相同 → count += 1
      → count == 3 → blocked_call_keys.add(key.call_key)
  → 下一次 replace(file_path=同一路径)
      → should_block(call)
          → tool_call_key(call) = "replace\x1f<file_path>"
          → 命中 blocked_call_keys → 拦截
```

### 目标数据流

```
replace 失败
  → build_failure_key(tool_call, result)
      → FailureKey(tool_name="replace",
                   normalized_args=normalize_tool_args("replace", args)
                                  = stable_json({
                                      "file_path": args["file_path"],
                                      "start_no": args["start_no"],
                                      "end_no": args["end_no"],
                                    }),                            ← 精确归并
                   error_kind=...)
  → record_failure / should_block 使用同一 normalize_tool_args
      → 不同行范围 → 不同 call_key → 不互相拉黑
      → 同一行范围反复失败 → 仍被计数、仍被拉黑（保留防死循环）
```

### 关键设计决策

归并键包含哪些字段：

| 字段 | 是否纳入 | 理由 |
|------|---------|------|
| `file_path` | ✅ | 必需，区分不同文件 |
| `start_no` | ✅ | 定位修改起点，不同起点是不同操作 |
| `end_no` | ✅ | 定位修改终点，不同范围是不同操作 |
| `start_anchor` | ❌ | 内容校验项，非定位项；同位置不同 anchor 的失败应合并（都是"没对上"） |
| `end_anchor` | ❌ | 同上 |
| `new_string` | ❌ | 替换内容不影响"是否同一操作"的判定 |

不纳入 anchor / new_string 的原因：agent 重试时常常微调 anchor 片段或 new_string，
若纳入会导致每次微调都算"新调用"，绕过防死循环保护。归并键应稳定反映"改哪里"，
而非"改成什么"。

## Data Model

### `normalize_tool_args` replace 分支变更

```python
# 变更前
if tool_name in {"read", "file", "write", "replace", "lsp_format"}:
    return str(args.get("file_path") or "")

# 变更后
if tool_name in {"read", "file", "write", "lsp_format"}:
    return str(args.get("file_path") or "")
if tool_name == "replace":
    return stable_json({
        "file_path": args.get("file_path"),
        "start_no": args.get("start_no"),
        "end_no": args.get("end_no"),
    })
```

`stable_json` 已存在于同文件（`runtime_guards.py:348`），输出排序后的紧凑 JSON，
保证相同输入产生相同字符串，满足归并键的稳定性要求。

### 影响的函数

- `normalize_tool_args` — 唯一改动点。
- `tool_call_key` — 自动受益（调用 `normalize_tool_args`），无需改动。
- `build_failure_key` — 自动受益，无需改动。
- `FailureKey.call_key` / `FailureKey.stable_key` — 自动受益，无需改动。

## API Contract

无新增 API。本次改动是 `normalize_tool_args` 内部行为调整，对外签名不变。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `args` 缺少 `start_no` / `end_no`（理论上不会发生，replace 入参有校验） | `stable_json` 中对应值为 `None`，归并键仍可生成，不影响拦截逻辑 |
| `args` 为空 dict | `stable_json({"file_path": None, "start_no": None, "end_no": None})`，所有空调用归并为一组，行为合理 |
| 历史已存在的 `blocked_call_keys`（含旧格式 file_path-only key） | 进程内状态，不持久化，重启后自然清空；运行中不迁移 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 归并键纳入 `start_no` + `end_no` | 纳入全部 5 个字段 | anchor/new_string 的微调不应绕过防死循环；归并键应反映"改哪里"而非"改成什么" |
| 只改 `normalize_tool_args`，不动计数阈值 | 调高 replace 的失败阈值 | 阈值是全局防死循环策略，不应按工具特例化；根因是归并粒度而非阈值 |
| 不迁移运行中的旧格式黑名单 | 运行时检测并迁移 | 状态不持久化、重启即清；迁移增加复杂度且收益低 |
| replace 单独分支，不并入通用 file 类 | 把 replace 留在 file 类统一处理 | replace 的语义（改特定行范围）与 read/file/write（整文件操作）不同，必须区分 |

## Verification

### 现有测试基线

测试文件：`tests/test_agent/test_runtime_guards.py`，已覆盖以下场景，本次改动需保证它们继续通过：

- `test_failure_loop_guidance_escalates_and_blocks_same_call` — read 同参失败 3 次被 block。
- `test_failure_loop_does_not_block_materially_different_args` — grep 不同 pattern 不互相 block。
- `test_failure_loop_success_clears_tool_blocks` — 成功调用按 tool_name 清黑名单。
- `test_failure_loop_new_key_clears_old_blocks` — 新 key 出现时清旧黑名单。

### 新增单元测试（加到 `tests/test_agent/test_runtime_guards.py`）

1. **同文件不同行范围失败不互相拉黑**
   - 对 `replace(file_path="a.py", start_no=1, end_no=1, ...)` 失败 3 次。
   - 对 `replace(file_path="a.py", start_no=10, end_no=10, ...)` 调用 `should_block`。
   - 断言：返回 `False`。

2. **同文件同行范围反复失败仍被拉黑**
   - 对 `replace(file_path="a.py", start_no=1, end_no=1, ...)` 失败 3 次。
   - 对相同参数调用 `should_block`。
   - 断言：返回 `True`。

3. **同行范围不同 anchor 仍归并为同一 key**
   - `replace(file_path="a.py", start_no=1, end_no=1, start_anchor="x", ...)` 失败。
   - `replace(file_path="a.py", start_no=1, end_no=1, start_anchor="y", ...)` 失败。
   - 断言：两次 `build_failure_key` 产生的 `FailureKey.stable_key` 相同。

4. **read 归并行为不变（回归保护）**
   - 对 `read(file_path="a.py")` 失败 3 次后，`should_block` 返回 `True`。
   - 确保拆分 replace 分支未影响 file 类工具的归并。

5. **成功调用解除同 tool_name 黑名单**
   - `replace(file_path="a.py", start_no=1, end_no=1)` 失败 3 次被拉黑。
   - `replace(file_path="a.py", start_no=5, end_no=5)` 成功（`record_success`）。
   - 断言：之后 `replace(file_path="a.py", start_no=1, end_no=1)` 的 `should_block` 返回 `False`。

### 验证命令

```bash
.venv/bin/python -m pytest tests/test_agent/test_runtime_guards.py -v
```

预期：所有现有测试 + 新增测试全部通过。

## Open Questions

- 无。测试路径已确认为 `tests/test_agent/test_runtime_guards.py`，现有测试已覆盖 read/grep 的归并行为，本次改动在此基础上新增 replace 专项测试即可。
