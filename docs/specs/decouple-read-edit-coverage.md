# Spec: 修正 Edit/Write 后的 Read Coverage

## 问题

当前 `file_read_coverage` 同时承担两个职责：

1. **防重复读**：read 工具检查 LLM 是否已看过某些行，避免重复返回内容
2. **编辑许可**：edit/write 工具检查 LLM 是否已读过要编辑的行，防止盲目编辑

edit/write 成功后调用 `record_read_range(ctx, path, 1, new_total)`，将整个文件标记为"已读"。这导致 LLM 从未实际见过的行也被防重复读逻辑拦截，无法获取编辑后的真实内容。

### 受影响场景

| 场景 | 操作链 | 问题 |
|------|--------|------|
| 部分读→编辑→读未读行 | read 1-2 → edit line 2 → read line 3 | line 3 从未被 LLM 读过，但被标记为已读 |
| 插入/删除→行号偏移 | read 全文 → insert_before line 2 → read 全文 | 新增行 LLM 从未见过，但 coverage 扩展覆盖 |
| write→读新内容 | read line 1 → write 全新内容 → read 全文 | 文件内容完全变了，read 被拦截 |

## 方案

核心思路：**edit/write 返回的 diff 就是 LLM "已读"的内容**。diff 中出现的行（add + context）的 `new_lineno` 就是 LLM 看到的新文件行号，没出现在 diff 里的行 LLM 没看到。

不需要引入新的 `file_edit_coverage` 字典——read coverage 本身就能同时服务"防重复读"和"编辑许可"两个职责，只要记录的范围是精确的。

### 编辑后 coverage 的更新规则

edit 后不是简单地清空旧 coverage 或整体标记失效，而是根据 diff 精确重映射：

**hunk 之前的行**：行号和内容都没变，旧 coverage 保留。

**hunk 范围内的行**：内容可能变了，丢弃旧 coverage，由 diff 的 add/context 行重新覆盖。

**hunk 之后的行**：内容没变但行号偏移了，旧 coverage 按偏移量调整行号。

#### 偏移量计算

每个 hunk 的偏移量 = `new_count - old_count`（hunk 新旧行数之差）。hunk 之后的行，累计之前所有 hunk 的偏移量。

#### 单次编辑示例

**delete line 50，旧 coverage [1-100]**：
- hunk: old 47-53, new 47-52, offset = -1
- hunk 之前: [1-46] → 保留
- hunk 范围: [47-53] → 丢弃，由 diff 覆盖 [47-52]
- hunk 之后: [54-100] → 偏移 -1 → [53-99]
- 合并: [1-99] ✓

**expand line 5 (1→2 行)，旧 coverage [1-30]**：
- hunk: old 2-8, new 2-9, offset = +1
- hunk 之前: [1-1] → 保留
- hunk 范围: [2-8] → 丢弃，由 diff 覆盖 [2-9]
- hunk 之后: [9-30] → 偏移 +1 → [10-31]
- 合并: [1-31]，line 32-101 未读 ✓

**replace 行数不变，旧 coverage [1-100]**：
- hunk: old 2-8, new 2-8, offset = 0
- hunk 之前: [1-1] → 保留
- hunk 范围: [2-8] → 丢弃，由 diff 覆盖 [2-8]
- hunk 之后: [9-100] → 偏移 0 → [9-100]
- 合并: [1-100] ✓

### 行号重映射算法

```
输入: 旧 coverage ranges + diff hunks
输出: 新 coverage ranges

1. 从 diff 提取 hunk 信息列表:
   [(old_start, old_end, new_start, new_end, offset), ...]
   offset = new_count - old_count

2. 对旧 coverage 的每个 range [s, e]:
   遍历 hunks，将 range 按 hunk 边界切分为三段:
     a) hunk 之前: [s, min(e, hunk.old_start-1)] → 行号不变，保留
     b) hunk 范围: [max(s, hunk.old_start), min(e, hunk.old_end)] → 丢弃
     c) hunk 之后: [max(s, hunk.old_end+1), e] → 行号 + 累计偏移

3. 累计偏移 = 之前所有 hunk 的 offset 之和

4. 合并: 重映射后的 ranges + diff 中 add/context 的 new_lineno ranges
```

### 连续编辑

每次 edit 后 coverage 已重映射为新文件行号。下一次 edit 的 diff 的 old 行号也是基于当前文件，与 coverage 行号体系一致。**因此连续编辑不会累积误差**，每次重映射都是独立的、正确的。

#### 连续编辑示例

**场景 A：连续 replace，行数不变**

```
read 1-10, coverage [1-10]

Edit 1: replace line 5
  hunk: old 2-8, new 2-8, offset=0
  重映射: [1-1]保留 + diff[2-8] + [9-10]偏移0=[9-10]
  coverage → [1-10]

Edit 2: replace line 7
  check_read_coverage(7,7) → [1-10]覆盖 → 允许 ✅
  hunk: old 4-10, new 4-10, offset=0
  重映射: [1-3]保留 + diff[4-10] + 无
  coverage → [1-10]
```

**场景 B：先 expand 再 edit 偏移后的行**

```
read 1-10, coverage [1-10]

Edit 1: expand line 5 (1→2行)
  hunk: old 2-8, new 2-9, offset=+1
  重映射: [1-1]保留 + diff[2-9] + [9-10]偏移+1=[10-11]
  coverage → [1-11], 文件 11 行

Edit 2: replace 新 line 10 (原 line 9)
  check_read_coverage(10,10) → [1-11]覆盖 → 允许 ✅
  hunk: old 7-11, new 7-11, offset=0
  重映射: [1-6]保留 + diff[7-11] + 无
  coverage → [1-11]
```

**场景 C：先 delete 再 edit 偏移后的行**

```
read 1-10, coverage [1-10]

Edit 1: delete line 5
  hunk: old 2-8, new 2-7, offset=-1
  重映射: [1-1]保留 + diff[2-7] + [9-10]偏移-1=[8-9]
  coverage → [1-9], 文件 9 行

Edit 2: replace 新 line 7 (原 line 8)
  check_read_coverage(7,7) → [1-9]覆盖 → 允许 ✅
  hunk: old 4-9, new 4-9, offset=0
  重映射: [1-3]保留 + diff[4-9] + 无
  coverage → [1-9]
```

**场景 D：先 expand 再 delete，偏移量抵消**

```
read 1-10, coverage [1-10]

Edit 1: expand line 5 (1→2行)
  hunk: old 2-8, new 2-9, offset=+1
  重映射: [1-1]保留 + diff[2-9] + [9-10]偏移+1=[10-11]
  coverage → [1-11], 文件 11 行

Edit 2: delete 新 line 8
  check_read_coverage(8,8) → [1-11]覆盖 → 允许 ✅
  hunk: old 5-11, new 5-10, offset=-1
  重映射: [1-4]保留 + diff[5-10] + [12-11]→空
  coverage → [1-10], 文件 10 行
```

**场景 E：部分读 + 连续编辑，coverage 不覆盖**

```
read 1-5, coverage [1-5]

Edit 1: replace line 3
  hunk: old 1-5, new 1-5, offset=0
  重映射: 无hunk之前 + diff[1-5] + 无hunk之后
  coverage → [1-5]

Edit 2: replace line 8
  check_read_coverage(8,8) → [1-5]不覆盖 → 拒绝 ❌
  需要先 read 6-10
```

### 重映射算法的边界条件

**旧 coverage range 被多个 hunk 切分**：

```
read 1-100, coverage [1-100]
Edit: 同时 edit line 10 (expand +1) 和 line 50 (delete -1)

Hunk 1: old 7-13, new 7-14, offset=+1
Hunk 2: old 47-53, new 48-53, offset=-1

对 coverage [1-100] 切分:
  Hunk 1 之前: [1-6] → 保留
  Hunk 1 范围: [7-13] → 丢弃
  Hunk 1 之后 + Hunk 2 之前: [14-46] → 偏移+1=[15-47]
  Hunk 2 范围: [47-53] → 丢弃
  Hunk 2 之后: [54-100] → 偏移+1+(-1)=[54-100]

diff 覆盖: Hunk1 [7-14] + Hunk2 [48-53]
合并: [1-6] + [7-14] + [15-47] + [48-53] + [54-100] = [1-100]
文件 100 行 (expand+1, delete-1)
```

**coverage range 完全在 hunk 范围内**：

```
read 5-8, coverage [5-8]
Edit: replace line 5-8
  Hunk: old 2-10, new 2-10, offset=0
  [5-8] 完全在 hunk 范围内 → 丢弃
  diff 覆盖: [2-10]
  coverage → [2-10]
```

**coverage range 完全在 hunk 之前**：

```
read 1-3, coverage [1-3]
Edit: replace line 8
  Hunk: old 5-10, new 5-10, offset=0
  [1-3] 完全在 hunk 之前 → 保留
  diff 覆盖: [5-10]
  coverage → [1-3] + [5-10]
```

**coverage range 完全在 hunk 之后**：

```
read 8-10, coverage [8-10]
Edit: expand line 3 (1→2行, +1)
  Hunk: old 1-6, new 1-7, offset=+1
  [8-10] 完全在 hunk 之后 → 偏移+1=[9-11]
  diff 覆盖: [1-7]
  coverage → [1-7] + [9-11]
```

**write 全新文件**：

```
read 1-5, coverage [1-5]
Write: 完全不同的内容, 10 行
  diff: old /dev/null → new 1-10, 单个 hunk 覆盖全文件
  旧 coverage [1-5] 全部在 hunk 范围内 → 丢弃
  diff 覆盖: [1-10]
  coverage → [1-10] ✓
```

### 各工具行为变化

#### read 工具（无变化）

- 写入 `file_read_coverage`
- 读取 `file_read_coverage` 做防重复读判断

#### edit 工具

| 阶段 | 变化前 | 变化后 |
|------|--------|--------|
| 前置检查 | `check_read_coverage()` | 不变 |
| 编辑成功后 | `record_read_range(1, new_total)` | `remap_read_coverage_from_diff(ctx, path, diff)` |

#### write 工具

| 阶段 | 变化前 | 变化后 |
|------|--------|--------|
| 写入成功后 | `record_read_range(1, line_count)` | `remap_read_coverage_from_diff(ctx, path, diff)` |

write 的 diff 覆盖整个文件，旧 coverage 全部在 hunk 范围内被丢弃，由 diff 重新覆盖。效果等同于标记整个文件为已读。

#### LSP format（本次不改动）

LSP format 的 coverage 处理保持现状（`clear_read_coverage`），后续单独优化。

### 行号偏移提示

edit 导致行号偏移时，LLM 脑中仍持有旧行号，容易用旧行号发起下一次编辑导致错误。虽然 diff 的 hunk header 包含行号信息，但依赖 LLM 自行推理不可靠。

**方案**：edit 返回结果中，当有行号偏移时追加明确提示，让 LLM 感知行号变更。

**提示格式**：

```
Line shift: lines after {old_end} shifted by {offset:+d} (old line {old_end+1} → new line {old_end+1+offset}, ...)
```

**示例**：

delete line 5：
```
File edited: test.txt (1 operations)
--- a/test.txt
+++ b/test.txt
@@ -2,7 +2,6 @@
...
Line shift: lines after 5 shifted by -1 (old line 6 → new line 5, old line 7 → new line 6, ...)
```

insert_before line 5：
```
File edited: test.txt (1 operations)
--- a/test.txt
+++ b/test.txt
@@ -2,6 +2,7 @@
...
Line shift: lines from 5 onward shifted by +1 (old line 5 → new line 6, old line 6 → new line 7, ...)
```

replace 行数不变：无偏移，不追加提示。

多 hunk 时，每个有偏移的 hunk 各追加一条提示。

**实现**：从 diff 的 `FileDiff.hunks` 中提取 `old_start + old_count - 1`（old_end）和 `new_count - old_count`（offset），offset != 0 时追加提示。此逻辑与 `remap_read_coverage_from_diff` 共用 hunk 信息。

**对 write 工具**：write 的 diff 覆盖整个文件，旧行号全部失效，不需要偏移提示（LLM 知道文件被完全重写）。

## 新增函数

```python
# file_state.py

def remap_read_coverage_from_diff(ctx: ToolContext, resolved: Path, diff: FileDiff) -> None:
    """根据 diff 重映射 file_read_coverage。
    
    1. 从 diff 提取 hunk 信息（old_start, old_end, offset）
    2. 对旧 coverage 按 hunk 边界切分，重映射行号
    3. 叠加 diff 中 add/context 行的 new_lineno 范围
    4. 用新 fingerprint 写入 file_read_coverage
    """

# file_ops.py (辅助函数)

def line_shift_hints(diff: FileDiff) -> str:
    """从 diff 中提取行号偏移提示文本，offset 为 0 的 hunk 不生成提示。"""
```

## 修改文件清单

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/file_state.py` | 新增 `remap_read_coverage_from_diff` |
| `src/voidx/tools/file_ops.py` | FileEditTool: 成功后 `remap_read_coverage_from_diff` 替代 `record_read_range(1, new_total)`，追加行号偏移提示；FileWriteTool: 同理（无偏移提示） |

## 测试用例

1. **replace 行数不变**：read 1-10 → edit line 5 → coverage 应为 [1-10]
2. **expand (行数增加)**：read 1-30 → expand line 5 → coverage 应为 [1-31]
3. **delete**：read 1-100 → delete line 50 → coverage 应为 [1-99]
4. **部分读→编辑→读未读行**：read 1-2 → edit line 2 → read line 5，应正常返回
5. **编辑后 read 被拦截（diff 覆盖行）**：read 1-2 → edit line 2 → read line 2，应被拦截
6. **连续编辑（coverage 覆盖内）**：read 1-10 → edit line 5 → edit line 8，应允许
7. **连续编辑（coverage 覆盖外）**：read 1-2 → edit line 2 → edit line 5，应拒绝
8. **insert 后行号偏移**：read 全文 → insert_before line 3 → read 偏移后的行，应正常返回
9. **write 全新内容**：read line 1 → write 全新内容 → read 全文，diff 覆盖全文件，应标记为已读
10. **多 hunk 场景**：read 全文 → 同时 edit line 5 和 line 50 → coverage 正确重映射
11. **连续 expand+delete 偏移抵消**：read 1-10 → expand line 5 → delete line 8 → coverage 正确
12. **coverage range 完全在 hunk 之前/之内/之后**：三种边界条件
13. **行号偏移提示**：delete line 5 → 输出包含 "lines after 5 shifted by -1"；replace 行数不变 → 无提示
14. **多 hunk 偏移提示**：多个 hunk 各有偏移 → 各追加一条提示
15. **现有测试适配**：`test_edit_preserves_read_coverage_after_success` 语义变化——编辑后 coverage 由 diff 精确重映射

---

## 后续增强：锚点校验（独立 PR）

行号偏移提示是"通知"机制——告诉 LLM 行号变了，依赖 LLM 正确处理。但 LLM 可能忽略提示，仍用旧行号发起 edit，导致**静默错误**（edit 成功但改错位置）。

锚点校验是"验证"机制——edit 时检查 LLM 认为的行内容是否和实际一致，将静默错误变为显式错误。两者互补：提示预防，锚点兜底。

### 问题场景

```
LLM read 文件，看到 line 5 是 "def bar():"
然后 edit line 2 (expand)，收到提示：
  "Line shift: lines after 4 shifted by +1 (old line 5 → new line 6)"

LLM 下次要 edit bar 函数时：
  - 注意到提示 → 用 start_line=6 ✅
  - 忽略提示 → 用 start_line=5 ❌ (静默错误：改到 foo 函数的 return 行)
```

当前 edit 工具只校验行号是否在 `[1, total_lines]` 范围内，不校验行内容是否和 LLM 预期一致。

### 方案：scope + anchor 双层定位

EditEntry 新增两个可选字段：

```python
class EditEntry(BaseModel):
    operation: Literal["replace", "insert_before", "insert_after"]
    start_line: int
    end_line: int | None = None
    new_string: str
    scope: str | None = Field(
        default=None,
        description=(
            "Optional scope anchor — a function/class definition line content. "
            "Used to locate the enclosing scope before applying the edit. "
            "Copy the definition line from your prior read output."
        ),
    )
    anchor: str | None = Field(
        default=None,
        description=(
            "Optional content anchor — expected content at start_line. "
            "If provided, the tool verifies start_line contains this text. "
            "On mismatch, searches within scope for the anchor to auto-correct line numbers."
        ),
    )
```

**scope**：函数/类定义行内容，用于粗定位。LLM 从 read 输出中复制定义行（如 `def bar():`、`class Foo:`），不需要理解 AST。

**anchor**：start_line 的预期内容片段，用于精确定位。LLM 从 read 输出中复制行内容或关键片段。

### 定位流程

```
1. 无 scope、无 anchor → 当前行为，纯行号定位

2. 有 scope → 在文件中搜索包含 scope 内容的行
   - 找到唯一匹配 → scope_line = 匹配行号
   - 找到多个匹配 → 返回错误，列出所有匹配位置
   - 未找到 → 返回错误，scope 内容可能已被修改

3. 有 anchor → 校验 start_line 的内容是否包含 anchor
   - 匹配 → 行号正确，正常编辑
   - 不匹配 + 有 scope → 在 scope_line 附近搜索 anchor
     - 找到唯一匹配 → 自动修正 start_line，继续编辑
     - 找到多个匹配 → 返回错误，列出匹配位置
     - 未找到 → 返回错误，anchor 内容可能已被修改
   - 不匹配 + 无 scope → 在全文件搜索 anchor
     - 找到唯一匹配 → 自动修正 start_line
     - 找到多个/未找到 → 返回错误
```

### 示例

**场景：行号偏移后 LLM 用了旧行号**

```
文件当前状态（line 5 已偏移到 line 6）：
  5: (空行)
  6: def bar():
  7:     y = 2
  8:     return y

LLM 发起 edit（用了旧行号 5）：
  edit: {
    start_line: 5,
    end_line: 5,
    scope: "def bar():",
    anchor: "def bar():",
    new_string: "def baz():"
  }

定位流程：
  1. 搜索 scope "def bar()" → 找到 line 6
  2. 校验 start_line=5 是否包含 anchor "def bar()" → 不匹配
  3. 在 scope_line=6 附近搜索 anchor → line 6 匹配
  4. 自动修正 start_line=5 → 6
  5. 正常编辑

返回结果包含修正信息：
  "Line corrected: start_line 5 → 6 (matched anchor 'def bar():')"
```

**场景：scope 内容已被修改**

```
LLM 发起 edit：
  edit: {
    start_line: 6,
    scope: "def bar():",
    anchor: "y = 2",
    new_string: "y = 3"
  }

定位流程：
  1. 搜索 scope "def bar()" → 未找到（函数已被重命名为 baz）
  2. 返回错误："Scope 'def bar():' not found in file. The function may have been renamed. Read the file to get current content."
```

### scope 匹配策略

纯文本包含匹配（`scope_text in line_content`），不做 AST 解析。理由：

1. LLM 从 read 输出中复制定义行，不需要理解代码结构
2. 不依赖语言特定的 AST 解析器
3. 足够可靠——函数/类定义行在文件中通常唯一
4. 简单实现，低风险

### anchor 匹配策略

纯文本包含匹配（`anchor_text in line_content`），不做 trim 或模糊匹配。理由：

1. LLM 从 read 输出中复制行内容，应该精确匹配
2. 如果需要模糊匹配，说明 LLM 输出了错误的内容，应该失败
3. 保持简单，避免引入模糊匹配的歧义

### 对 prompt 的影响

edit 工具的 description 中鼓励 LLM 提供 scope 和 anchor：

```
For edits after a line shift, provide scope (function/class definition line) and
anchor (expected content at start_line) to help locate the correct position.
```

### 实现范围

本增强独立于 coverage 重映射，作为后续 PR 实现。涉及文件：

| 文件 | 变更 |
|------|------|
| `src/voidx/tools/file_ops.py` | EditEntry 新增 scope/anchor 字段，定位逻辑 |
| `src/voidx/tools/file_ops.py` | edit 工具 description 更新 |

### 测试用例（锚点校验）

1. **anchor 匹配**：start_line 内容包含 anchor → 正常编辑
2. **anchor 不匹配 + scope 定位**：scope 找到正确位置，anchor 在 scope 附近找到 → 自动修正
3. **anchor 不匹配 + 无 scope**：全文件搜索 anchor，唯一匹配 → 自动修正
4. **anchor 不匹配 + 多个匹配**：返回错误，列出匹配位置
5. **anchor 不匹配 + 未找到**：返回错误
6. **scope 未找到**：返回错误
7. **scope 多个匹配**：返回错误
8. **无 scope 无 anchor**：当前行为，纯行号
9. **insert_before/insert_after + anchor**：anchor 校验 start_line 内容
10. **自动修正后返回修正信息**：输出包含 "Line corrected: start_line X → Y"