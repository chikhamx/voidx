# Edit-Window Adjacent Duplicate Collapse

> **Status: Done** — Archived on 2026-08-03.

## 来源

2026-08-03 对 `replace` 重叠去重的分析。现有 L1（head/tail boundary overlap）能处理“上下文粘连重写”，但截图类“编辑后区间内出现相邻重复块”不会触发。

## 背景与问题

### L1 已覆盖（boundary overlap）

`resolve_overlap` 只比较：

- `new_string` 前缀 ↔ 替换/插入点**之前**的行（head）
- `new_string` 后缀 ↔ 替换/插入点**之后**的行（tail）

条件：整行精确匹配、非空、每侧最多 3 行；head 优先于 tail。

### L1 不覆盖（截图类）

典型结果：

```text
type_str = "single";
int ct = feed.value(...);   // 本次写入
type_str = "single";        // 原 after 保留
int ct = feed.value(...);   // 原 after 保留
```

常见触发：

1. bounds 选在相邻行，`new_string` 只带了新增片段，after 仍是整段旧块；
2. `new_string` 自身已写了重复块，L1 只吃掉 after 边界，内部重复留下；
3. 空行/空格打断 tail 精确匹配。

此时 after 首行往往不是 `new_string` 尾行，**tail=0**，重复块落在编辑结果中间，L1 无能为力。

### 目标语义

兼容两种情况，且不把工具做成全文去重器：

| 层 | 职责 | 典型 case |
|----|------|-----------|
| L1 boundary overlap | 上下文粘连重写 | decorator/签名后缀、前后缀上下文 |
| L2 adjacent collapse | 编辑影响区间内相邻重复块 | 截图类整段重复 |

## Goals

- 在 **编辑影响区间** 内折叠相邻重复块，兼容截图类重复。
- 保留现有 L1 head/tail 语义与测试，不改 matching limit=3。
- L1 → L2 固定顺序：先 boundary，再 collapse。
- `replace` 与 `write(op="insert")` 共用同一 L2 纯函数。
- 行为可观测：metadata + 简短 hint。
- 读覆盖仍以 L1 扩展后的 effective range 为准（L2 只消费已在结果中的重复，不额外要求新 coverage）。

## Non-goals

- 不做全文 / 跨函数 / 语义去重。
- 不做模糊匹配、空白归一化、跨空行匹配。
- 不折叠 **单行** 相邻重复（`min_block=2`），避免误伤连续 `import` / `}` / 空语句。
- 不改 `write(op="append")` / `write(op="write")` / `manage`。
- 不引入调用方开关；L2 默认开启（与 L1 一致）。
- 不让工具猜测 insert vs replace 意图。

## Architecture

扩展 `src/voidx/tools/file/overlap.py` 为重叠/去重纯逻辑的唯一归属：

```python
@dataclass(frozen=True)
class CollapsedBlock:
    index: int          # 0-based start of the kept block (pre-collapse coords)
    size: int           # k, lines in one block
    gap: int            # 0 = strictly adjacent; 1 = one blank line between blocks


@dataclass(frozen=True)
class CollapseResult:
    lines: list[str]    # full file lines after collapse
    collapsed: list[CollapsedBlock]


def collapse_adjacent_duplicate_blocks(
    lines: Sequence[str],
    *,
    boundaries: Sequence[int],   # edit boundary positions (0-based): head_pos, tail_pos
    margin: int = 3,
    min_block: int = 2,
    max_block: int = 3,
    max_gap_blanks: int = 1,
) -> CollapseResult:
    ...
```

约定：

- 函数 **纯**：不碰 IO / ToolContext / coverage。
- 输入是 **整文件 line list** + 编辑边界位置；函数内部构造双窗  
  `W = ⋃ [b - margin, b + margin]`（裁剪到 `[0, len(lines)]`，重叠合并），只改窗内内容。
- 返回的 `lines` 为完整结果列表，便于调用方直接写入。
- 已验证的覆盖矩阵见文末「验证记录」。

调用方（`replace` / `write.insert` 共用路径）负责：

1. L1 `resolve_overlap`
2. 拼出候选 `lines`
3. 计算 `window_start/window_end`
4. L2 `collapse_adjacent_duplicate_blocks`
5. 写文件、diff、metadata

## L2 Semantics

在每个窗口内从左到右扫描，对每个位置 `i`：

1. 尝试 `k` 从 `max_block` 降到 `min_block`（默认 3→2）。块内**无空行**才继续。
2. **严格相邻**：`lines[i:i+k] == lines[i+k:i+2k]` → 删后一块，`gap=0`。
3. **近似相邻**：两块之间仅隔 `g ∈ [1, max_gap_blanks]` 个空行（默认 ≤1）且块相同 → 删「空行 + 后一块」，`gap=g`。**截图类靠这条命中**：真实截图里重复块 `type_str+ct` 之间隔了一个空行，严格相邻抓不到。
4. 命中后窗口右界随删除收缩，**同一 `i` 继续**（三次重复可连续折叠）。
5. 无匹配则 `i += 1`。不回扫窗口外。

### 为何 min_block=2

单行相邻重复在真实代码里合法且常见（连续 `import`、`}`）。截图类失败几乎都是 **≥2 行整块** 重复。用 `k≥2` 换安全性。**k=1 的单行粘连仍由 L1 负责**（见覆盖矩阵），L2 不碰。

### 为何 max_block=3 且 max_gap_blanks=1

与 L1 limit 对齐；gap 放宽到 ≥2 会显著提高误删合法「空行分隔的相似模板」的概率，已验证 gap=2 不折叠。

已知边界：窗口为边界 ±3，因此贴近边界的近似相邻折叠最多可见 `k=2, gap=1`（`k=3, gap=1` 需要边界外 4 行可见性）；严格相邻 k=3 不受影响（无 gap 时 L1 本就先行消费）。此取舍接受。

实现备注（review 后修正）：多窗口按序处理时必须跟踪 `shift`（前面窗口删除行数）来平移后续窗口坐标，否则第二个编辑边界的重复会被漏折叠。`CollapsedBlock.index` 为折叠时刻的 post-collapse 坐标，仅用于观测调试，勿当作原文件行号。

### 空行

块内任一行 `== ""` 则该块不成立（与 L1 一致）；空行只允许出现在两块**之间**且 ≤1 个。

## Edit Window（双窗）

L1 应用后，设：

```text
kept_before = before[:-head] if head else before
# lines = kept_before + new_lines + after[tail:]
head_pos = len(kept_before)              # new 起点（0-based）
tail_pos = len(kept_before) + len(new_lines)   # new 终点 / after 起点
```

调用 `collapse_adjacent_duplicate_blocks(lines, boundaries=[head_pos, tail_pos])`，函数内部构造：

```text
W = [head_pos - 3, head_pos + 3] ∪ [tail_pos - 3, tail_pos + 3]
  （裁剪到 [0, len(lines)]，两窗重叠时合并）
```

### 为何是双窗而不是「整段 new ± margin」

出错集中在 **new 与 before/after 的交界附近**（粘连、漏粘、隔空行的旧块残留）。双窗只扫两端 ±3：

- 窗口更小 → 误伤面更小；
- 长 `new_string` 中段的合法重复（如配置模板）**天然豁免**；
- 当 new 很短（≤6 行）时两窗自然粘成一段，等价于整段扫描，行为一致。

### 截图窗口直觉

```text
106  type_str          ← before 尾
107  int ct (新增)      ← new
     ── tail_pos 在这 ──
108  (空行)
109  type_str           ┐ 旧块残留
110  int ct             ┘
```

`head_pos=1, tail_pos=2`，双窗合并后覆盖 106–110；`k=2, gap=1` 近似相邻命中，删「空行+旧块」→ 留下 `type_str, int ct` 一份。

### insert 路径

`write.insert` 经 `_apply_resolved_edits(..., overlap=...)` 拼行后，用同一公式算 `head_pos/tail_pos`（insert 的 before/after 切分与 replace 一致）。


## Integration Points

### `replace._execute_text_replace`

现有顺序：

```text
resolve_overlap → kept_before + new_lines + after[tail:] → join → write
```

改为：

```text
resolve_overlap
→ lines = kept_before + new_lines + after[tail:]
→ collapse_adjacent_duplicate_blocks(lines, window_...)
→ join(collapsed.lines) → write
```

`actual_start_line` / `actual_end_line` **仍只反映 L1**（coverage 语义不变）。  
L2 删除量只进 `collapsed_blocks` metadata，不回写 coverage 要求。

### `replace._apply_resolved_edits`（insert + overlap）

在 for-edit 拼完 `lines` 之后、`content == original` 判断之前，若本路径启用了 overlap（insert），同样跑 L2。  
无 overlap 的纯 insert/append 字面拼接：**也建议跑 L2**（仅当本次 edit 改写了内容时），以便 insert 截图类命中；append/full-write 不走此函数的 insert 分支则不受影响。

更稳妥的落地：

- **replace 主路径**：必跑 L2  
- **insert + 任意**（含 overlap=0）：必跑 L2，窗口按插入点+new_len+margin  
- **append / full write**：不跑  

### Metadata / Hint

```python
metadata["overlap"] = {"head": h, "tail": t}   # 现有 L1
metadata["collapsed_blocks"] = [2]             # 每次折叠的 k；多次则为 [2, 2] 等
```

Hint 示例：

```text
[Adjacent collapse: removed 1 duplicate block(s) of 2 lines inside the edit window.]
```

与 `[Boundary overlap: ...]` 可同时出现。

## 算法伪代码

```python
def collapse_adjacent_duplicate_blocks(lines, *, boundaries, margin=3,
                                       min_block=2, max_block=3, max_gap_blanks=1):
    out = list(lines)
    n = len(out)
    windows = merge([(max(0, b - margin), min(n, b + margin)) for b in boundaries])
    collapsed = []
    for w0, w1 in windows:
        i, end = w0, w1
        while i < end:
            matched = False
            for k in range(min(max_block, (end - i) // 2), min_block - 1, -1):
                block = out[i:i + k]
                if "" in block:
                    continue
                if block == out[i + k:i + 2 * k]:            # 严格相邻
                    del out[i + k:i + 2 * k]
                    end -= k
                    collapsed.append(CollapsedBlock(index=i, size=k, gap=0))
                    matched = True
                    break  # 同一 i 下一轮 while 再试（三次重复）
                for g in range(1, max_gap_blanks + 1):        # 近似相邻：隔 g 个空行
                    j = i + k + g
                    if j + k > end:
                        break
                    if all(x == "" for x in out[i + k:j]) and out[j:j + k] == block:
                        del out[i + k:j + k]                  # 删空行 + 后一块，保留前一块
                        end -= g + k
                        collapsed.append(CollapsedBlock(index=i, size=k, gap=g))
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                i += 1
    return CollapseResult(lines=out, collapsed=collapsed)
```

## 测试计划

### Unit（`test_overlap.py`）

- 无重复 → 不变  
- `k=2` / `k=3` 命中，删后一块  
- `A A A`（k=2）→ 折叠到单份  
- 空行在块内 → 不折叠  
- `k=1` 即使两行相同 → 不折叠  
- 窗口外的相邻重复 → 不动  
- 窗口边界截断导致不足 `2k` → 不折叠  
- 精确空白敏感  

### Integration（`test_edit_dedup.py`）

- **截图形态 replace**：替换/邻行写入后出现 `type_str+ct` 双份 → 结果一份，`collapsed_blocks` 非空  
- **L1 仍优先**：decorator 后缀 overlap 回归，不依赖 L2  
- **L1+L2 叠加**：new 自带重复且 after 也有 tail 可吃 → 最终一份，metadata 两边都有  
- **write insert 截图类**：insert 一段后与下方块重复 → 折叠  
- **单行重复不折叠**：`import os\nimport os` 保持  
- **coverage**：L2 不改变“未读 effective L1 range 则失败”  

### 验证命令

```bash
./test.py --backend -- src/tests/test_tools/file/test_overlap.py src/tests/test_tools/file/test_edit_dedup.py -v
```

## Risks

| 风险 | 缓解 |
|------|------|
| 合法相邻重复模板被删（如两段相同 config） | 窗口限制 + min_block=2 + max_block=3 |
| 与 L1 双重处理语义不清 | 固定 L1→L2；测试叠加 case |
| 静默掩盖模型写错 | metadata + hint 必暴露 |
| 窗口公式 off-by-one | 集成测钉死 prefix_len/new_len/margin |
| insert 与 replace 行为分叉 | 共用纯函数 + 两侧集成测 |

## 文件结构（实现时）

| 文件 | 职责 |
|------|------|
| `src/voidx/tools/file/overlap.py` | 新增 collapse 纯函数与结果类型 |
| `src/voidx/tools/file/replace.py` | L1 后接 L2；metadata/hint |
| `src/voidx/tools/file/write.py` | insert 路径确保进入带 L2 的 apply（若逻辑全在 replace apply 则可能仅改 replace） |
| `src/tests/test_tools/file/test_overlap.py` | unit |
| `src/tests/test_tools/file/test_edit_dedup.py` | replace/insert 集成 |

## 决策摘要

1. **是编辑边界双窗内去重**，不是全文去重，也不是整段 new 扫描。  
2. **L1 head/tail 保留**（k=1 单行粘连只能靠它）；L2 专打 k≥2 相邻/近似相邻重复块。  
3. **min_block=2, max_block=3, max_gap_blanks=1**，块内精确匹配、禁空行；块间容忍 ≤1 空行。  
4. **默认开启**，可观测，不改 coverage 合同。  

## 验证记录（2026-08-03，纯函数模拟）

| 场景 | L1 | 双窗 L2 | 结论 |
|------|-----|--------|------|
| decorator 后缀粘连（k=2） | tail=2 消费 | 无需动作 | L1 覆盖 ✅ |
| 单行粘连 `import os`（k=1） | tail=1 消费 | 不碰（min_block=2） | **必须保留 L1** ✅ |
| 截图：隔 1 空行的 `type_str+ct` 重复 | head=tail=0 | near k=2 gap=1 折叠 ✅ | **L2 主目标** |
| new 自带 ABAB + L1 tail=1 | 吃掉尾 | 严格相邻折叠剩余 ✅ | 两层互补 |
| 隔 2 空行的相似块 | — | 不折叠 ✅ | gap≤1 约束生效 |
| 长 new 中段的合法重复 | — | 双窗外，不碰 ✅ | 双窗意义 |
| 合法单行重复 `x=1; x=1` | — | 不碰 ✅ | min_block=2 生效 |

## 开放问题（若实现前需拍板）

1. `collapsed_blocks` metadata 用逐条 `{size, gap}` 列表即可，足够调试。  
2. `A A A` 折叠到 1 份（同一位置循环）。  
3. L2 对所有 replace 都跑（含 L1 无 overlap 的），窗口按双窗公式。
