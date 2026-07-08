# File 工具组改造 — 技术设计文档

Date: 2026-07-08

> **Status: Spec** — 等待实现。

## 背景

### 问题

当前 `file` 工具组有四个工具（`read` / `file` / `write` / `replace`），LLM 使用存在以下摩擦：

1. **`file` 工具有误导性** — 工具名暗示"文件操作"，实际只管理生命周期（create/delete/move），不做内容编辑。LLM 经常试图用它写内容。
2. **批量操作缺失** — 创建或删除多个文件时 LLM 必须重复调用，浪费 token、响应慢。
3. **`write` 的行号基不一致** — `lineno` 为 0-based，而 `read` 输出和 `replace` 都是 1-based。LLM 每次需要心算转换，是高频错误源。
4. **全量覆写缺失** — LLM 想完整替换一个文件时，必须走 delete+create+append 三步或 replace 全部行，缺少原子操作。
5. **`file`（管理）功能边界薄弱** — 命名不足以表达职责，改名后还能容纳更多文件生命周期操作。

### 目标

- 消除工具命名误导
- 支持批量文件创建、删除、移动
- 统一所有工具的行号语义为 1-based
- 增加全量覆写能力
- 保持工具总数和 LLM 选择路径的简单性

### 非目标

- 不合并 `write` 和 `replace` 为一个工具（保留了自然的分界线）
- 不改变 `read` 的行为
- 不引入 glob/pattern 通配符批量（路径列表是显式传递，避免通配符的意外匹配）

## 设计概览

### 变更一览

| 工具 | 变更类型 | 变化 |
|------|---------|------|
| `read` | **不变** | — |
| `file` | **改名 + 批量** | → `manage`，新增批量创建/删除/移动 |
| `write` | **改造** | `lineno` 改为 1-based，新增 `op="write"` |
| `replace` | **不变** | — |

工具数量 4 → 4（不变），但每个工具的职责边界更清晰。

### LLM 选择路径（决策树）

```
我要操作文件
├─ 只是看看内容                    → read
├─ 新建/删除/移动文件             → manage
│   ├─ 创建一个或一批文件          → manage(op="create", paths=...)
│   ├─ 删除一个或一批文件          → manage(op="delete", paths=...)
│   └─ 移动一个或一批文件          → manage(op="move", moves=...)
└─ 要改内容
    ├─ 我知道行号，要加内容        → write
    │   ├─ 插在某行前面           → write(op="insert", lineno=N, ...)
    │   ├─ 追加到末尾             → write(op="append", ...)
    │   └─ 完全重写文件           → write(op="write", ...)
    └─ 我知道内容的文本，要替换    → replace
```

## Manage 工具设计

### 参数模型

```python
class MoveSpec(BaseModel):
    src: str = Field(description="源文件路径")
    dest: str = Field(description="目标文件路径")
    overwrite: bool = Field(
        default=False,
        description="目标已存在时是否覆盖。False 时报错/跳过。",
    )


class ManageInput(BaseModel):
    op: Literal["create", "delete", "move"] = Field(
        description="操作类型：create（创建空文件+父目录）、delete（删除文件）、move（移动/重命名文件）"
    )
    paths: str | list[str] | None = Field(
        default=None,
        description=(
            "文件路径。op=create/delete 时必填。"
            "单个文件传字符串（如 paths='a.py'），"
            "多个文件传字符串数组（如 paths=['a.py', 'b.py']）。"
            "op=move 时忽略此字段。"
        ),
    )
    moves: list[MoveSpec] | None = Field(
        default=None,
        description=(
            "移动操作的目标映射数组。op=move 时必填。"
            "每项包含 src（源路径）、dest（目标路径）、可选的 overwrite。"
            "op=create/delete 时忽略此字段。"
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "目标已存在时是否覆盖。op=create 时有效，对所有 paths 统一生效。"
            "op=delete 时忽略；删除不存在文件仍返回 skipped。"
            "op=move 时由每项的 overwrite 单独控制，顶层此字段无效。"
        ),
    )

```

### 参数校验逻辑

```python
@model_validator(mode="after")
def _validate_op_params(self):
    if self.op in ("create", "delete"):
        if not self.paths:
            raise ValueError("paths is required when op=create or op=delete")
        if self.moves:
            raise ValueError("moves is ignored when op=create or op=delete; use paths instead")
    if self.op == "move":
        if not self.moves:
            raise ValueError("moves is required when op=move")
        if self.paths:
            raise ValueError("paths is ignored when op=move; use moves instead")
    return self
```

### 各操作行为

#### create

对 `paths` 中的每个路径：
- 检查路径穿越 → 阻塞并标记 error
- 检查是否为已有目录 → 阻塞并标记 error
- 文件已存在且 `overwrite=False` → 跳过并标记 skipped
- 文件已存在且 `overwrite=True` → 检查 staleness（mtime 指纹）→ 备份版本记录 → truncate 为 0 字节
- 文件不存在 → 创建父目录 + 写入空文件

#### delete

对 `paths` 中的每个路径：
- 检查路径穿越 → 阻塞并标记 error
- 文件不存在 → 跳过并标记 skipped
- 是目录 → 阻塞并标记 error
- 检查 staleness（mtime 指纹） → 提示重读
- 备份版本记录 → 删除文件

#### move

对 `moves` 中的每个 `MoveSpec`：
- 检查路径穿越（src/dest 均检查）→ 阻塞并标记 error
- src 不存在 → 跳过并标记 skipped
- src 是目录 → 阻塞并标记 error
- 检查 `move.src` staleness（mtime 指纹）→ 不匹配则标记 error 并提示重读
- dest 已存在且 `overwrite=False` → 跳过并标记 skipped
- dest 已存在且 `overwrite=True` → 检查 `move.dest` staleness（mtime 指纹）→ 备份 dest 版本记录
- 备份 src 版本记录 → 执行移动

### 批量结果返回格式

所有批量操作返回统一结构，逐文件报告状态。

```json
{
  "title": "Created 3/4 files",
  "output": "Created 3/4 files. 1 skipped.",
  "summary": "Created 3 files, 1 skipped",
  "metadata": {
    "operation": "create",
    "total": 4,
    "succeeded": 3,
    "skipped": 1,
    "failed": 0,
    "results": [
      {"file": "a.py", "status": "created"},
      {"file": "b.py", "status": "created"},
      {"file": "c.py", "status": "created"},
      {"file": "d.py", "status": "skipped", "reason": "already exists, set overwrite=True to replace"}
    ]
  }
}
```

| status 值 | 含义 |
|-----------|------|
| `created` / `deleted` / `moved` | 成功 |
| `skipped` | 跳过（不致命，如文件不存在、已存在但不覆盖） |
| `error` | 失败（该项不可执行，如路径穿越、路径是目录、staleness 不匹配） |

LLM 看到 results 列表后可以精确修复失败/跳过的项。

### 批量执行语义

批量操作采用 **逐项执行、部分成功、无自动回滚** 策略：

- 参数结构错误（例如 `op=move` 但缺少 `moves`）会让整个工具调用失败。
- schema validation / 参数结构错误返回工具级错误，不进入 `metadata.results`；只有逐项业务错误进入 results。
- 单个路径的业务错误只影响该项，其他项继续执行。
- 返回结果顺序必须与输入顺序一致，方便 LLM 精确定位失败项。
- `metadata.failed` 统计 `status="error"` 的项；`metadata.skipped` 单独统计跳过项。
- `move` 不做跨项事务回滚；如果第 1 项成功、第 2 项失败，第 1 项保持已移动状态。
- 如调用方需要原子语义，应拆成单文件操作并自行控制回滚策略。

### 单文件向后兼容

单文件操作仍然直观：

```python
manage(op="create", paths="app.py")
manage(op="delete", paths="app.py")
manage(op="move", moves=[{"src": "old.py", "dest": "new.py"}])
```

不强制列表写法。

### 错误消息风格（LLM 友好）

错误消息针对误用场景给出具体修正指导，而非仅抛出异常：
- 收到 `paths="app.py"`（字符串），`create` 或 `delete` → 走单文件逻辑
- 收到 `paths=["a.py", "b.py"]`（数组）→ 走批量逻辑
- 如果 LLM 在 `create` 时传了 `moves` 而非 `paths`：

> "op=create 时请使用 paths 参数指定文件路径，而非 moves。例如：manage(op='create', paths=['a.py', 'b.py'])"

- 如果 LLM 在 `move` 时传了 `paths` 而非 `moves`：

> "op=move 时请使用 moves 参数指定源/目标映射数组，而非 paths。每项包含 src 和 dest 字段。例如：manage(op='move', moves=[{'src': 'old.py', 'dest': 'new.py'}])"

## Write 工具设计

### 参数模型

```python
class WriteInput(BaseModel):
    file_path: str = Field(description="文件路径")
    op: Literal["insert", "append", "write"] = Field(
        description=(
            "写入模式：insert（在 lineno 行前插入）、"
            "append（追加到末尾）、"
            "write（用 new_string 完全替换文件内容）"
        )
    )
    lineno: int | None = Field(
        default=None,
        ge=1,
        description=(
            "行号（1-based）。op=insert 时必填，含义：在 lineno 行的前面插入新行。"
            "例如 read 输出显示 '5\\txxx'，在它前面插内容则 lineno=5。"
            "op=append 和 op=write 时忽略。"
        ),
    )
    new_string: str = Field(
        default="",
        description="要写入的内容。op=write 时覆盖整个文件。op=insert/append 时追加。",
    )
```

### 关键变更点

#### 1. `lineno` 改为 1-based

| 位置 | 当前 0-based | 改为 1-based |
|------|-------------|-------------|
| 文件开头前插 | `lineno=0` | `lineno=1` |
| read 输出 "line 5: xxx" 前插 | `lineno=4` | `lineno=5` |
| 第 10 行前插（文件共 10 行） | `lineno=9` | `lineno=10` |

**校验**：`lineno` 范围是 1 到 `total_lines + 1`（`total_lines + 1` 等价于 append 位置，但 append 应使用 `op="append"`）。

#### 2. 新增 `op="write"`

行为：`path.write_text(new_string, encoding="utf-8")`

- 文件不存在则自动创建（含父目录）
- 文件存在则直接覆盖，走版本备份 + staleness 检查
- `lineno` 参数忽略
- `new_string` 为空字符串则清空文件

适用于 LLM 需要完整替换一个文件内容的场景，如：
- 生成一个新模块
- 重建配置、文档或测试快照文件
- 将已有文件替换为已生成的完整内容

不适用于只修改某个函数或类的局部编辑；局部替换仍应使用 `replace`。

### 覆盖度检查调整

- `insert`：对 lineno 行检查读覆盖度（同当前行为，但 lineno 改为 1-based）
- `append`：不检查覆盖度（同当前行为）
- `write`：不对行号检查覆盖度，但检查 staleness（文件 mtime 指纹）

### Staleness / mtime 指纹契约

`read` 的用户可见行为保持不变，但运行时需要在覆盖度记录中保存文件指纹：

```python
class FileFingerprint(BaseModel):
    path: str
    mtime_ns: int
    size: int
```

检查规则：

- `read` 成功后记录 `mtime_ns + size`，作为后续编辑的基准。
- `insert` / `replace` / `delete` / `move.src` 必须命中最近一次读取的指纹；不匹配则返回 `error`，提示重新读取。
- `write(op="write")` 覆盖已存在文件时必须通过 staleness 检查；文件不存在时允许直接创建。
- `manage(op="create", overwrite=True)` 覆盖已存在文件时必须通过 staleness 检查；`overwrite=False` 时仍返回 skipped。
- `move.dest` 已存在且 `overwrite=True` 时，也需要对 dest 做 staleness 检查，避免覆盖用户刚修改的文件。
- 批量操作按项检查 staleness；某项不匹配只让该项 `status="error"`，不影响其他项继续执行。

错误消息示例：

> "文件自上次 read 后已变化，请重新 read 该文件后再重试。"

## 向后兼容

### Manage（原 File）

`manage` 是新主工具；`file` 保留一个迁移期 deprecation wrapper，建议保留 **1 个 minor release 或 30 天**（以先到者为准）。wrapper 不实现新逻辑，只做旧 schema 到新 schema 的转换。

旧 `file` 参数映射：

| 旧调用 | 新调用 | 备注 |
|--------|--------|------|
| `file(op="create", file_path="a.py", overwrite=False)` | `manage(op="create", paths="a.py", overwrite=False)` | 单文件创建 |
| `file(op="delete", file_path="a.py")` | `manage(op="delete", paths="a.py")` | `overwrite` 忽略 |
| `file(op="move", file_path="old.py", dest_path="new.py", overwrite=False)` | `manage(op="move", moves=[{"src": "old.py", "dest": "new.py", "overwrite": False}])` | 单文件移动 |

wrapper 行为：

- 只支持旧 `file` 的单文件 schema，不支持批量。批量必须使用 `manage`。
- `create/delete` 收到 `dest_path` 时返回参数错误，并提示使用 `file_path`。
- `move` 缺少 `dest_path` 时返回参数错误，并提示迁移到 `manage(..., moves=[...])`。
- 返回结果沿用 `manage` 的统一结构，并在 `metadata` 中追加：

```json
{
  "deprecated_tool": "file",
  "replacement_tool": "manage",
  "remove_after": "one minor release or 30 days"
}
```

过渡期结束后移除 `file` registry alias 和 wrapper 测试。

### Write

- `lineno` 从 0-based 改为 1-based：**这是一个 breaking change**。现有 agent 对话中的记忆可能按旧基调用。
- 不建议同时支持 0-based 和 1-based 灰度，因为 `lineno=4` 这类旧调用仍是合法 1-based 值，无法可靠判断调用意图。
- 明确可检测场景：`lineno=0` 直接报错，并提示："write.insert 的 lineno 已改为 1-based；如需插在文件开头，请使用 lineno=1。"
- 对 `lineno=total_lines + 1` 给出非阻塞提示：该位置等价于追加，优先使用 `op="append"`。
- 迁移动作：更新工具 schema、系统提示示例、设计文档中的所有调用样例，并在 release note 标记 breaking change。

### Replace

完全不变。没有向后兼容问题。

## 决策日志

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `file` → `manage` | 保留原名 | `file` 误导 LLM 以为能写内容；`manage` 准确传达"管理文件生命周期" |
| 批量用 `paths`/`moves` 分参 | 统一 `files` 参数做多态 | 参数名本身是语义提示，LLM 不需要跨操作推断格式；`str \| list[str]` 的 union 简单稳定 |
| `moves` 使用对象数组 | 平行数组 `sources` + `dests` 配对 | 对象数组可读性强，不依赖索引对齐；容易扩展字段（如 overwrite 每项单独控制） |
| 批量部分成功策略 | 全有或全无 | 部分成功允许 LLM 只修复失败项，不用重试整个操作 |
| `write` 不改名 | 合并到 `edit` | 保持工具简单扁平，不把选择压力移到参数级 |
| `write` 行号 1-based | 保留 0-based | 与 `read` 输出、`replace` 的行号基一致，消除 LLM 高频心算错误 |
| `write` 新增 `op="write"` | 保持现状（delete+create+append） | 原子化全量覆写，避免三步调用和版本历史碎片 |
| `paths: str \| list[str]` | 仅 `list[str]` | 单文件创建写 `paths=["a.py"]` 可接受，但字符串更简洁；简单的 union LLM 能稳定处理 |

## 实现任务

1. 新增/改造 `ManageInput` 和 `MoveSpec`，将原 `file` 生命周期逻辑迁移到 `manage`。
2. 增加 legacy `file` wrapper，只做旧参数到 `manage` 参数的转换和 deprecation metadata。
3. 改造 `WriteInput`：`lineno` 改为 1-based，新增 `op="write"` 全量覆写。
4. 将覆盖度记录扩展为 `mtime_ns + size` 指纹，并接入 `delete` / `move` / `write` 覆盖场景。
5. 统一批量返回结构，保证 results 顺序、计数字段和 status 值一致。
6. 更新工具 schema snapshot、系统提示示例和 release note。

## 测试矩阵

| 模块 | 场景 | 预期 |
|------|------|------|
| manage.create | 单路径字符串 | 创建文件和父目录 |
| manage.create | 多路径数组，部分已存在 | 已创建项为 `created`，已存在项为 `skipped` |
| manage.create | `overwrite=True` 覆盖已存在文件 | 通过 staleness 后清空文件并记录备份 |
| manage.delete | staleness 不匹配 | 返回 `error`，提示重新 read，不删除文件 |
| manage.move | src staleness 不匹配 | 对应项返回 `error`，不执行移动 |
| manage.move | dest 已存在且 overwrite=True 但 dest staleness 不匹配 | 对应项返回 `error`，不覆盖 dest |
| manage.delete | 文件不存在 | 返回 `skipped`，不抛致命异常 |
| manage.delete | 目标是目录 | 返回 `error`，不删除目录 |
| manage.move | 多项移动，第二项失败 | 第一项保持 `moved`，第二项 `error/skipped`，不回滚 |
| manage.move | dest 已存在且 overwrite=False | 返回 `skipped` |
| manage.move | src/dest 路径穿越 | 对应项返回 `error` |
| legacy file | create/delete/move 旧 schema | 正确转发到 `manage`，返回 deprecation metadata |
| write.insert | `lineno=1` | 插入到文件开头前 |
| write.insert | `lineno=0` | 参数错误，提示 1-based 迁移 |
| write.insert | `lineno=total_lines+1` | 允许但提示应优先使用 append |
| write.write | 文件不存在 | 创建父目录并写入完整内容 |
| write.write | 文件存在且 staleness 匹配 | 备份后全量覆盖 |
| write.write | 文件存在且 staleness 不匹配 | 返回错误，提示重新 read |
| replace | 原有 anchor 替换行为 | 行号语义和覆盖度行为保持不变 |

## 决策收敛

- [x] `file` → `manage`：保留 legacy wrapper 一个迁移期，不直接硬删除。
- [x] `write` 1-based：不支持双行号基灰度，只对 `lineno=0` 做明确迁移提示。
- [x] `overwrite`：`create` 使用顶层统一参数；`delete` 忽略；`move` 使用每项 `overwrite`。

