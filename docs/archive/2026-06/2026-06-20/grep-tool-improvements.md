> **Status: Done** — P0 and P1 implemented and verified.

# voidx grep 工具改进 — 技术设计文档

## Context

voidx 内置 grep 工具（`src/voidx/tools/search.py`）是 LLM agent 搜索代码的主要手段。当前实现是纯 Python 逐文件逐行扫描，功能上对标 ripgrep (rg) 存在明显差距：

- 搜索表达力不足：不支持大小写不敏感、全词匹配、上下文行
- 过滤能力弱：不支持排除模式、不感知 .gitignore
- 输出不可结构化：匹配结果为纯文本，无法程序化消费
- 性能瓶颈：单线程、全量读入、硬编码上限

本设计分两期实施，P0 补齐高频缺失功能，P1 补齐过滤和结构化输出。

## Goals and Non-Goals

### Goals

- P0：补齐搜索表达力（ignore_case、whole_word、context_lines、exclude）
- P1：.gitignore 感知、结构化匹配结果、二进制检测、可配置上限
- 保持向后兼容：所有新参数有默认值，现有调用行为不变
- bash_router 同步更新，拦截 `-i`/`-w` 等标志

### Non-Goals

- 不替换为 rg 子进程调用（保持纯 Python、跨平台、沙箱可控）
- 不实现 PCRE2 / 多行匹配 / 压缩文件搜索（低频需求）
- 不实现并行搜索（P2 范畴，需评估 asyncio 线程池开销）

## Architecture

改动集中在 `GrepInput` → `GrepTool.execute` → `_hint_grep` 三处，无新模块。

```
GrepInput (Pydantic model)
  ├── pattern: str              # 现有
  ├── path: str | None          # 现有
  ├── include: str | None       # 现有
  ├── ignore_case: bool = False # P0 新增
  ├── whole_word: bool = False  # P0 新增
  ├── context_lines: int = 0    # P0 新增
  └── exclude: str | None       # P0 新增

GrepTool.execute
  ├── 正则编译：根据 ignore_case/whole_word 调整 flags 和 pattern
  ├── 文件遍历：iter_files 增加 exclude 过滤
  ├── 行匹配：记录匹配行号，context_lines 时输出前后行
  └── 结果格式：context_lines 时用分隔符标注上下文

_hint_grep (bash_router)
  └── 解析 -i/-w/-C 等标志，映射到新参数
```

## Data Model

### GrepInput 变更

```
GrepInput
├── pattern: str                          # 正则表达式
├── path: str | None = None               # 搜索路径
├── include: str | None = None            # 包含 glob（如 *.py）
├── ignore_case: bool = False             # P0: 大小写不敏感
├── whole_word: bool = False              # P0: 全词匹配 \b 包裹
├── context_lines: int = 0                # P0: 上下文行数（0=无）
└── exclude: str | None = None            # P0: 排除 glob（如 *.min.js）
```

### ToolResult.metadata 扩展（P1）

```
metadata
├── pattern: str
├── matches: int
├── match_details: list[dict]  # P1: [{file, line, column, content}]
└── truncated: bool
```

## API Contract

### GrepTool.execute

- **Signature**: `async def execute(self, args: dict, ctx: ToolContext) -> ToolResult`
- **Behavior Changes**:

#### ignore_case

```python
flags = re.IGNORECASE if inp.ignore_case else 0
regex = re.compile(inp.pattern, flags)
```

#### whole_word

```python
pattern = rf"\b{inp.pattern}\b" if inp.whole_word else inp.pattern
```

注意：whole_word 在正则编译前包裹，用户传入的 pattern 中的 `\b` 不受影响。若用户同时写了 `\b`，会变成 `\b\b...\b\b`，功能上等价但冗余。文档中说明 whole_word 自动添加词边界即可。

#### context_lines

匹配阶段先收集所有匹配行号，然后二次遍历输出上下文：

```python
# 第一遍：收集匹配
hits: list[tuple[str, int, str]] = []  # (file, line_no, content)
for f in files:
    lines = f.read_text(...).split("\n")
    for i, line in enumerate(lines, 1):
        if regex.search(line):
            hits.append((rel_path, i, line.strip()[:200]))
            count += 1
            if count >= 100:
                break

# 第二遍：格式化输出（含上下文）
if inp.context_lines > 0:
    for rel, line_no, content in hits:
        results.append(f"{rel}:{line_no}:{content}")
        # 输出上下文行
        start = max(1, line_no - inp.context_lines)
        end = min(len(lines), line_no + inp.context_lines)
        for ctx_no in range(start, end + 1):
            if ctx_no != line_no:
                results.append(f"{rel}-{ctx_no}-{lines[ctx_no - 1].strip()[:200]}")
else:
    for rel, line_no, content in hits:
        results.append(f"{rel}:{line_no}:{content}")
```

上下文行格式：`file-line_no-content`（用 `-` 代替 `:` 区分匹配行与上下文行，与 rg `-C` 输出格式一致）。

#### exclude

```python
def iter_files(dir_path: Path):
    for entry in dir_path.iterdir():
        if should_skip_dir(entry):
            continue
        if entry.is_dir():
            yield from iter_files(entry)
        elif entry.is_file():
            if entry.suffix in SKIP_SUFFIXES:
                continue
            if inp.include and not entry.match(inp.include):
                continue
            if inp.exclude and entry.match(inp.exclude):  # P0 新增
                continue
            scanned += 1
            yield entry
```

### _hint_grep 变更

解析新增标志：

| bash 标志 | 映射参数 |
|-----------|----------|
| `-i` | `ignore_case=True` |
| `-w` | `whole_word=True` |
| `-C N` / `-A N` / `-B N` | `context_lines=N`（简化为对称上下文） |
| `--exclude GLOB` | `exclude=GLOB` |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| whole_word + 无效正则 | 先包裹 `\b`，再编译；编译失败返回 `Invalid regex` |
| context_lines 导致输出超限 | 上下文行不计入 100 条匹配上限，但总输出行数硬限 500 行 |
| exclude glob 匹配异常 | `entry.match()` 已处理异常，无需额外处理 |
| .gitignore 解析失败（P1） | 静默降级，回退到 SKIP_DIRS 硬编码过滤 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 上下文行用 `-` 分隔符 | 用缩进或 `>` 前缀 | 与 rg 输出格式一致，LLM 已熟悉此格式 |
| context_lines 简化为对称上下文 | 分别支持 -A/-B | 单参数更简单，LLM 不需要区分前后 |
| exclude 单 glob | 支持多 exclude 列表 | P0 保持简单，P2 可扩展为 list |
| whole_word 用 `\b` 包裹 | 用 `re.IGNORECASE` + 单词边界正则 | `\b` 是标准做法，Python re 原生支持 |
| 不引入 rg 子进程 | 用 rg 替代纯 Python | 保持沙箱可控、无外部依赖、跨平台一致 |

## P1 扩展（后续实施）

### .gitignore 感知

- 引入 `pathspec` 库（Python gitignore glob 实现）
- 在工作区根目录查找 `.gitignore`，解析为 pathspec 规则
- `iter_files` 中额外过滤匹配 .gitignore 的文件
- 降级策略：无 .gitignore 或解析失败时回退到 SKIP_DIRS

### 结构化匹配结果

- `metadata["match_details"]` 返回 `[{file, line, column, content}]`
- column 通过 `regex.search(line).start()` 获取
- 纯文本输出不变，结构化数据仅存 metadata

### 二进制文件内容检测

- 替代 `SKIP_SUFFIXES` 硬编码
- 读取文件前 8192 字节，检测 NUL 字节
- 保留 SKIP_SUFFIXES 作为快速路径（避免读入已知二进制文件）

### 可配置上限

- `GrepInput` 新增 `max_matches: int = 100`、`max_scanned: int = 5000`
- LLM 可按需调大，避免大项目搜索被截断

## Open Questions

- [ ] context_lines 输出中上下文行是否应计入 100 条匹配上限？（倾向：不计入，但总行数限 500）
- [ ] 是否需要 `literal` 参数支持纯字面量搜索？（P2，可用 `re.escape` 实现）
- [ ] pathspec 是否已是项目依赖？需确认 pyproject.toml
