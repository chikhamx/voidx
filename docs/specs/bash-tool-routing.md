# Bash 工具路由兜底 — 技术设计文档

## Context

bash 工具是万能后门：LLM 可以用 `bash -c "cat file"` 代替 `read`，用 `bash -c "sed -i ..."` 代替 `replace`，用 `bash -c "git status"` 代替 `git` 工具。这些场景下，专用工具的优化链路全部被绕过：

- **文件状态追踪失效**：staleness 检查、read coverage、mtime 追踪、版本快照全部跳过
- **权限分类粗化**：`bash git commit` 走 `BASH_WRITE` 而非 `GIT_WRITE`，sandbox 校验精度下降
- **结构化输出丢失**：git 工具返回 JSON，bash 返回纯文本；read 有行号分页，cat 没有
- **Runtime Guards 去重失效**：bash 归一化只压缩空格，无法像专用工具那样按 file_path 去重

根因：没有路由层。bash 工具的 description 和 LLM prompt 中均无"优先使用专用工具"的引导，也没有执行时拦截和重定向机制。

## Goals and Non-Goals

### Goals

- 在 bash 工具执行前检测可路由到专用工具的命令
- 自动路由执行并返回专用工具的结果（含结构化输出、文件状态追踪等全部优化）
- 在返回结果中附带提示信息，引导 LLM 后续直接使用专用工具
- 路由失败时静默 fallback 到原始 bash 执行，不阻断工作流

### Non-Goals

- 不拦截复杂管道、多命令组合（`git status && echo done`）
- 不拦截带 shell 特性的命令（变量替换、子 shell、重定向组合）
- 不修改 LLM prompt 或 bash 工具 description（路由兜底是运行时行为，prompt 引导是独立优化）
- 不替代 bash 工具本身——bash 仍用于无法被专用工具覆盖的场景

## Architecture

```
LLM 调用 bash 工具
       │
       ▼
  BashTool.execute()
       │
       ▼
  _check_command()  ── blocked? ──→ 返回 blocked 结果
       │
       ▼
  _sandbox_denial()  ── denied? ──→ 返回 denied 结果
       │
       ▼
  _try_route()  ── 可路由? ──→ 调用专用工具 ──→ 返回路由结果 + 提示
       │
       │ 不可路由
       ▼
  _try_hint()  ── 可提示? ──→ 附加 RouteHint 到 bash 输出
       │
       │ 不可提示
       ▼
  原始 bash 执行
       │
       ▼
  返回 ToolResult（可能含 RouteHint 后缀）
```

路由检测在 sandbox 检查之后、实际进程创建之前执行。这样保证：
1. 危险命令先被 blocklist 拦截
2. sandbox 违规先被拒绝
3. 只有"安全但应该用专用工具"的命令才进入路由

### 路由检测模块

新增 `src/voidx/tools/bash_router.py`，职责单一：解析 bash 命令，判断是否可路由，构造专用工具参数。

```python
# bash_router.py 核心接口

@dataclass
class RouteResult:
    tool_id: str          # 目标工具 id: "read", "git", "write", ...
    tool_args: dict       # 传给目标工具的参数
    hint: str             # 给 LLM 的提示信息
    confidence: float     # 路由置信度 0-1

def try_route(command: str) -> RouteResult | None:
    """尝试将 bash 命令路由到专用工具。返回 None 表示不可路由。"""
```

### 路由执行

在 `BashTool.execute()` 中调用路由：

```python
async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
    inp = BashInput.model_validate(args)

    blocked = _check_command(inp.command)
    if blocked:
        return ToolResult(output=blocked, ...)

    blocked = _sandbox_denial(inp.command, ctx)
    if blocked:
        return ToolResult(output=blocked, ...)

    # --- 新增：路由兜底 ---
    route = try_route(inp.command)
    if route is not None:
        routed = await _execute_routed(route, ctx)
        if routed is not None:
            return routed
        # 路由执行失败，静默 fallback 到原始 bash

    # 原始 bash 执行
    ...
```

`_execute_routed` 通过 `ToolContext` 上的工具注册表找到目标工具实例并执行：

```python
async def _execute_routed(route: RouteResult, ctx: ToolContext) -> ToolResult:
    tool = ctx.tool_registry.get(route.tool_id)
    if tool is None:
        return None  # fallback 到原始 bash
    result = await tool.execute(route.tool_args, ctx)
    # 在 output 前追加路由提示
    hint_prefix = f"[Routed from bash → {route.tool_id}] {route.hint}\n\n"
    return ToolResult(
        title=result.title,
        output=hint_prefix + result.output,
        summary=result.summary,
        metadata={**result.metadata, "routed_from": "bash", "routed_to": route.tool_id},
        diff=result.diff,
    )
```

## Data Model

### RouteResult

```
RouteResult
├── tool_id: str           # "read" | "git" | "write" | "replace" | "insert" | "glob" | "grep"
├── tool_args: dict        # 目标工具的参数字典
├── hint: str              # "Use the read tool instead of cat for line numbers and read tracking."
└── confidence: float      # 0.0-1.0，当前版本仅用于日志，不影响路由决策
```

## 路由规则

### 1. Git 命令路由 → `git` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `git status` | `{"command": "status", "args": {}}` |
| `git status -- <paths>` | `{"command": "status", "args": {"pathspec": [...]}}` |
| `git diff` | `{"command": "diff", "args": {}}` |
| `git diff --cached` | `{"command": "diff", "args": {"cached": true}}` |
| `git diff <ref>` | `{"command": "diff", "args": {"ref": "<ref>"}}` |
| `git diff -- <paths>` | `{"command": "diff", "args": {"pathspec": [...]}}` |
| `git log` | `{"command": "log", "args": {}}` |
| `git log -n <N>` | `{"command": "log", "args": {"limit": N}}` |
| `git log --author=<who>` | `{"command": "log", "args": {"author": "<who>"}}` |
| `git log --since=<date>` | `{"command": "log", "args": {"since": "<date>"}}` |
| `git log -- <path>` | `{"command": "log", "args": {"path": "<path>"}}` |
| `git log -n <N> -- <path>` | `{"command": "log", "args": {"limit": N, "path": "<path>"}}` |
| `git blame <path>` | `{"command": "blame", "args": {"path": "<path>"}}` |
| `git blame -L <start>,<end> <path>` | `{"command": "blame", "args": {"path": "<path>", "start": <start>, "end": <end>}}` |
| `git branch` | `{"command": "branch_list", "args": {}}` |
| `git branch -a` / `git branch --all` | `{"command": "branch_list", "args": {"all": true}}` |
| `git remote -v` | `{"command": "remote_list", "args": {}}` |
| `git add <paths>` | `{"command": "add", "args": {"paths": [...]}}` |
| `git commit -m "<msg>"` | `{"command": "commit", "args": {"message": "<msg>"}}` |
| `git commit -m "<msg>" -- <paths>` | `{"command": "commit", "args": {"message": "<msg>", "paths": [...]}}` |
| `git restore <paths>` | `{"command": "restore", "args": {"paths": [...]}}` |
| `git restore --staged <paths>` | `{"command": "restore", "args": {"paths": [...], "staged": true}}` |

**不可路由**：`git push`、`git pull`、`git merge`、`git rebase`、`git stash`、`git cherry-pick`、`git reset`、`git checkout`（分支切换）、`git switch`、`git fetch`、`git clone`、`git init`——这些没有对应的专用工具 command。

### 2. 文件读取路由 → `read` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `cat <path>` | `{"file_path": "<path>"}` |
| `head -n <N> <path>` | `{"file_path": "<path>", "limit": N}` |
| `head <path>` | `{"file_path": "<path>", "limit": 10}` |
| `tail -n +<N> <path>` | `{"file_path": "<path>", "offset": N}` |
| `tail -n <N> <path>` | `{"file_path": "<path>", "_tail_n": N}` |
| `tail <path>` | `{"file_path": "<path>", "_tail_n": 17}` |

**tail 路由策略**：
- `tail -n +<N>` 直接映射 `offset=N`，无需读文件
- `tail -n <N>` 使用内部参数 `_tail_n`，在 `_execute_routed` 中先读文件获取总行数再算 offset
- `tail <path>` 默认取最后 17 行（与系统 tail 默认行为一致）

**不可路由**：`cat a.py b.py`（多文件）、`cat <path> | grep`（管道）、`cat <path> > other`（重定向）、`head -f` / `tail -f`（follow 模式）。

### 3. 文件写入路由 → `write` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `cat > <path> << 'EOF'\n...\nEOF` | `{"file_path": "<path>", "content": "..."}` |
| `cat > <path> << EOF\n...\nEOF` | `{"file_path": "<path>", "content": "..."}` |
| `echo '<content>' > <path>` | `{"file_path": "<path>", "content": "<content>"}` |
| `echo "<content>" > <path>` | `{"file_path": "<path>", "content": "<content>"}` |
| `printf '<content>' > <path>` | `{"file_path": "<path>", "content": "<content>"}` |

**echo/printf 写入路由策略**：
- 单引号内容：原样写入，无转义
- 双引号内容：处理 `\n`、`\t`、`\\` 转义序列，忽略 `$VAR`（路由时 `$` 已被快速排除拦截）
- 无引号内容：不路由（空格分割歧义）

**不可路由**：`echo >> <path>`（追加模式，专用工具无追加语义）、`tee`（管道场景）、`echo $VAR > file`（变量替换）、多命令组合。

### 4. 文件搜索路由 → `glob` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `find . -name "<pattern>"` | `{"pattern": "**/<pattern>"}` |
| `find . -type f -name "<pattern>"` | `{"pattern": "**/<pattern>"}` |
| `find <dir> -name "<pattern>"` | `{"pattern": "<dir>/**/<pattern>"}` |
| `ls <dir>` | `{"pattern": "<dir>/*"}` |
| `ls` | `{"pattern": "*"}` |

**find 路由策略**：
- 只路由 `-name` 过滤的简单 find，忽略 `-type`、`-path` 等复杂谓词
- `find . -name "*.py"` 等价于 `glob **/*.py`，需要将 `-name` 的 glob 模式转换为 `**/<pattern>`
- `ls` 路由为单层 glob，`ls -la` 等带 flag 的不路由

**不可路由**：`find . -exec`（执行动作）、`find . -type d`（目录搜索，glob 不区分类型）、`find . -newer`（时间过滤）、`find . -size`（大小过滤）、`ls -la`（详细信息）、`ls -R`（递归，等价于 `glob **/*` 但输出格式不同）。

### 5. 内容搜索路由 → `grep` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `grep <pattern> <path>` | `{"pattern": "<pattern>", "path": "<path>"}` |
| `grep -r <pattern>` | `{"pattern": "<pattern>"}` |
| `grep -r <pattern> <dir>` | `{"pattern": "<pattern>", "path": "<dir>"}` |
| `grep --include="<glob>" <pattern>` | `{"pattern": "<pattern>", "include": "<glob>"}` |
| `grep -r --include="<glob>" <pattern> <dir>` | `{"pattern": "<pattern>", "path": "<dir>", "include": "<glob>"}` |
| `rg <pattern>` | `{"pattern": "<pattern>"}` |
| `rg <pattern> <path>` | `{"pattern": "<pattern>", "path": "<path>"}` |
| `rg -t <type> <pattern>` | `{"pattern": "<pattern>", "include": "<type_glob>"}` |
| `egrep <pattern> <path>` | `{"pattern": "<pattern>", "path": "<path>"}` |
| `fgrep <string> <path>` | `{"pattern": "<re_escaped_string>", "path": "<path>"}` |

**grep/rg 路由策略**：
- `grep` 无 `-r` 时默认搜索单文件，`path` 参数直接传入
- `grep -r` 搜索目录，等价于 `grep` 工具的默认行为（递归搜索）
- `rg` 默认递归，直接映射
- `fgrep` 需要对搜索字符串做 `re.escape()`
- `rg -t py` 映射为 `include: "*.py"`（需要类型映射表）

**rg 类型映射表**（常用）：

| rg -t | include |
|-------|---------|
| `py` | `*.py` |
| `js` | `*.js` |
| `ts` | `*.ts` |
| `rs` | `*.rs` |
| `go` | `*.go` |
| `java` | `*.java` |
| `rb` | `*.rb` |

**不可路由**：`grep -l`（只列文件名，输出格式不同）、`grep -c`（只计数）、`grep -v`（反向匹配，专用工具不支持）、`grep -A/-B/-C`（上下文行，专用工具不支持）、管道组合（`cat file | grep`）。

### 6. 文件编辑路由 → `replace` 工具

| bash 命令模式 | tool_args |
|--------------|-----------|
| `sed -i '<addr>s/<old>/<new>/' <path>` | `{"file_path": "<path>", "start_no": <addr>, "end_no": <addr>, "prefix": "<old_prefix>", "suffix": "<old_suffix>", "new_string": "<new>"}` |

**sed 路由策略**：
- 只路由最简单的 `sed -i '<line>s/<old>/<new>/'` 形式
- 行地址必须是单个行号，不支持范围（`1,5s`）或正则地址（`/pattern/s`）
- 替换内容中的 `&` 和 `\1` 等反向引用不路由
- `sed -i` 的 GNU/BSD 语法差异（BSD 需要 `sed -i ''`）都支持

**不可路由**：`sed -i '1,5d'`（删除行）、`sed -i '/pattern/d'`（按模式删除）、`sed -i 's/old/new/g'`（无行号的全局替换，无法映射到 replace 的 start_no/end_no）、`awk -i inplace`（复杂编辑）、`perl -i -pe`（复杂编辑）、多命令 sed（`sed -i -e '...' -e '...'`）。

> sed 路由覆盖面窄，因为 replace 工具需要精确的行号和 prefix/suffix，而 sed 的地址空间和 replace 的参数空间不对齐。首版只路由最简单的单行替换，后续根据实际使用频率决定是否扩展。


## 扩展路由策略

当前有大量命令因专用工具参数约束或语义差异而不可路由。两种扩展策略可以显著提高路由覆盖率：

### 策略 A：路由提示（Route Hint）

不执行路由，而是返回一条提示信息，告诉 LLM 应该调用哪个专用工具以及如何构造参数。

**数据模型扩展**：

```python
@dataclass
class RouteHint:
    tool_id: str          # 推荐的专用工具 id
    reason: str           # 为什么不能直接路由
    suggestion: str       # 给 LLM 的操作建议（含参数示例）
    confidence: float     # 置信度 0-1
```

**执行流程变更**：

```
_try_route()  ── 可路由? ──→ 调用专用工具 ──→ 返回路由结果 + 提示
       │
       │ 不可路由
       ▼
_try_hint()  ── 可提示? ──→ 返回 RouteHint ──→ 附加到 bash 输出末尾
       │
       │ 不可提示
       ▼
  原始 bash 执行
```

**提示输出格式**：

```
[Route Hint: 用 replace 工具替代]
  建议: 先 read <path> 定位行号，再调用 replace(file_path="<path>", start_no=<line>, end_no=<line>, prefix="<old>", suffix="<old>", new_string="<new>")
  原因: sed -i 's/old/new/g' 无行号地址，replace 工具需要精确行号
```

**可提示的不可路由场景**：

| 不可路由命令 | 推荐工具 | 提示建议 |
|-------------|---------|---------|
| `sed -i 's/old/new/g' <path>` | `replace` | 先 `read <path>` 定位匹配行号，再 `replace(file_path, start_no, end_no, prefix, suffix, new_string)` |
| `sed -i '1,5d' <path>` | `replace` | `replace(file_path="<path>", start_no=1, end_no=5, prefix="<line1>", suffix="<line5>", new_string="")` |
| `sed -i '/pattern/d' <path>` | `replace` | 先 `grep <pattern> <path>` 定位行号，再逐行 `replace(..., new_string="")` |
| `echo 'text' >> <path>` | `insert` | `insert(file_path="<path>", lineno=-1, new_string="text")` |
| `grep -l <pattern> <dir>` | `grep` | 用 `grep(pattern="<pattern>", path="<dir>")`，输出中每行的 `file:line:content` 格式已包含文件名 |
| `grep -c <pattern> <path>` | `grep` | 用 `grep(pattern="<pattern>", path="<path>")`，输出行数即为匹配数 |
| `find . -type d -name "<pat>"` | `glob` | 用 `glob(pattern="**/<pat>/")`（目录以 `/` 结尾） |
| `ls -la <dir>` | `bash` | 此命令需要文件元数据，保留用 bash 执行 |

**不可提示的场景**（无对应工具能力或过于复杂）：

| 不可路由命令 | 原因 |
|-------------|------|
| `grep -v <pattern>` | grep 工具不支持反向匹配 |
| `grep -A/-B/-C <pattern>` | grep 工具不支持上下文行 |
| `awk -i inplace` / `perl -i -pe` | 复杂编辑，无对应工具 |
| `git push/pull/merge/...` | 无对应 git 子命令工具 |
| 管道组合 | 跨工具组合，无法单工具提示 |

### 策略 B：兼容工具（Compatibility Tool）

新增轻量级专用工具，放宽参数约束，覆盖高频但当前无法路由的场景。

**B1. `sub` 工具 — 无需行号的全局替换**

```python
class SubInput(BaseModel):
    file_path: str = Field(description="Path to edit")
    old: str = Field(description="Text to find (literal string, not regex)")
    new: str = Field(description="Replacement text")

class SubTool(BaseTool):
    id = "sub"
    description = "Replace all occurrences of exact text in a file. No line numbers needed."
```

- 语义：在文件中查找 `old` 的所有出现，替换为 `new`
- 等价于 `sed -i 's/old/new/g'`，但用字面量匹配而非正则
- 实现：读文件 → `str.replace(old, new)` → 写回，记录变更行数
- 路由映射：`sed -i 's/<old>/<new>/g' <path>` → `sub(file_path="<path>", old="<old>", new="<new>")`
- 安全：`old` 不能为空（防止无限替换），单次替换上限 1000 处

**B2. `append` 工具 — 文件追加写入**

```python
class AppendInput(BaseModel):
    file_path: str = Field(description="Path to append to")
    content: str = Field(description="Content to append")

class AppendTool(BaseTool):
    id = "append"
    description = "Append content to the end of a file. Creates the file if it doesn't exist."
```

- 语义：在文件末尾追加内容，文件不存在则创建
- 等价于 `echo 'text' >> file`
- 路由映射：`echo '<content>' >> <path>` → `append(file_path="<path>", content="<content>")`
- 与 `insert(lineno=-1)` 的区别：`append` 不需要先读文件确认行数，语义更直接

### 策略选择指南

| 场景 | 推荐策略 | 理由 |
|------|---------|------|
| `sed -i 's/old/new/g'` | **B1 兼容工具** | 高频操作，`sub` 工具语义清晰，路由直接 |
| `echo >> file` | **B2 兼容工具** | 高频操作，`append` 比 `insert(lineno=-1)` 更直观 |
| `sed -i '1,5d'` | **A 路由提示** | 低频，提示用 `replace` 即可，不值得新增工具 |
| `sed -i '/pattern/d'` | **A 路由提示** | 需要先搜索定位，提示引导 LLM 两步操作 |
| `grep -l/-c` | **A 路由提示** | 输出格式差异，提示用现有 `grep` 即可 |
| `grep -v/-A/-B/-C` | 暂不处理 | 需要扩展 `grep` 工具能力，属于独立优化 |
| `find -type d` | **A 路由提示** | 低频，提示用 `glob` 近似替代 |
| `ls -la` | 暂不处理 | 需要文件元数据，bash 执行更合适 |
| `awk/perl` 复杂编辑 | 暂不处理 | 复杂度不值得覆盖 |
| `git push/pull/...` | 暂不处理 | 无专用工具，bash 执行更合适 |

### 实现优先级

1. **P0 — 路由提示框架**：`RouteHint` 数据模型 + `_try_hint()` + 输出格式，零风险，立即收益
2. **P1 — `sub` 兼容工具**：覆盖 `sed -i 's/old/new/g'`，高频场景
3. **P1 — `append` 兼容工具**：覆盖 `echo >> file`，高频场景
4. **P2 — 扩展路由规则**：将 `sub`/`append` 加入路由表，新增对应的 `_route_sed_global`、`_route_echo_append` 检测
5. **P3 — 扩展 `grep` 工具**：支持 `-v`（反向匹配）、`-A/-B/-C`（上下文行），属于独立优化

## 路由检测算法

```python
def try_route(command: str) -> RouteResult | None:
    stripped = command.strip()
    if not stripped:
        return None

    # 快速排除：管道、多命令、子 shell、变量替换
    if any(ch in stripped for ch in ("|", ";", "&", "$")):
        return None
    if "`" in stripped or "$(" in stripped:
        return None

    words = _shell_words(stripped)
    if not words:
        return None

    prog = words[0].lower()

    # Git 路由
    if prog == "git" and len(words) >= 2:
        return _route_git(words)

    # 文件读取路由
    if prog in ("cat", "head", "tail"):
        # cat 同时可能是 heredoc 写入，优先检查
        if prog == "cat" and "<<" in stripped:
            return _route_write_heredoc(stripped)
        return _route_read(words)

    # 文件写入路由
    if prog in ("echo", "printf") and ">" in stripped:
        return _route_write_echo(stripped, words)

    # 文件搜索路由
    if prog == "find":
        return _route_find(words)
    if prog == "ls":
        return _route_ls(words)

    # 内容搜索路由
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return _route_grep(words)

    # 文件编辑路由
    if prog == "sed":
        return _route_sed(words)

    return None
```

### 快速排除原则

以下情况**不路由**，直接走原始 bash：
- 包含管道 `|`、分号 `;`、`&&`/`||` 的多命令
- 包含命令替换 `$()` 或反引号
- 包含环境变量 `$VAR`
- 重定向与管道组合（`cat file | grep`）
- 多文件参数（`cat a.py b.py`）
- `head`/`tail` 带 `-f`（follow 模式）

### Git 路由细节

```python
_UNROUTABLE_GIT_SUBCOMMANDS = frozenset({
    "push", "pull", "merge", "rebase", "stash", "cherry-pick",
    "reset", "checkout", "switch", "fetch", "clone", "init",
    "submodule", "filter-branch", "bisect",
})

def _route_git(words: list[str]) -> RouteResult | None:
    subcommand = words[1]
    rest = words[2:]

    if subcommand in _UNROUTABLE_GIT_SUBCOMMANDS:
        return None

    mapping = {
        "status": _route_git_status,
        "diff": _route_git_diff,
        "log": _route_git_log,
        "blame": _route_git_blame,
        "branch": _route_git_branch,
        "remote": _route_git_remote,
        "add": _route_git_add,
        "commit": _route_git_commit,
        "restore": _route_git_restore,
    }

    router = mapping.get(subcommand)
    if router is None:
        return None
    return router(rest)
```

### 文件读取路由细节

```python
def _route_read(words: list[str]) -> RouteResult | None:
    prog = words[0].lower()
    args = words[1:]

    # 排除带 flag 的复杂用法
    if any(a.startswith("-") and a not in ("-n",) for a in args):
        return None

    if prog == "cat":
        if len(args) != 1:
            return None  # 多文件或无参数
        return RouteResult(
            tool_id="read",
            tool_args={"file_path": args[0]},
            hint="Use the read tool instead of cat for line numbers, pagination, and read tracking.",
            confidence=0.95,
        )

    if prog == "head":
        limit = 10
        path = None
        i = 0
        while i < len(args):
            if args[i] == "-n" and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    return None
                i += 2
            elif not args[i].startswith("-"):
                path = args[i]
                i += 1
            else:
                return None
        if path is None:
            return None
        return RouteResult(
            tool_id="read",
            tool_args={"file_path": path, "limit": limit},
            hint="Use the read tool instead of head for line numbers and read tracking.",
            confidence=0.9,
        )

    if prog == "tail":
        return _route_tail(args)

    return None


def _route_tail(args: list[str]) -> RouteResult | None:
    """Route tail commands to read tool with offset."""
    n_value = None
    plus_offset = False
    path = None
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            n_str = args[i + 1]
            if n_str.startswith("+"):
                plus_offset = True
                n_value = n_str[1:]
            else:
                n_value = n_str
            i += 2
        elif not args[i].startswith("-"):
            path = args[i]
            i += 1
        else:
            return None
    if path is None:
        return None

    if plus_offset and n_value:
        # tail -n +N -> offset=N
        try:
            offset = int(n_value)
        except ValueError:
            return None
        return RouteResult(
            tool_id="read",
            tool_args={"file_path": path, "offset": offset},
            hint="Use the read tool instead of tail for line numbers and read tracking.",
            confidence=0.85,
        )

    if n_value:
        # tail -n N -> need to compute offset from file line count
        try:
            n = int(n_value)
        except ValueError:
            return None
        return RouteResult(
            tool_id="read",
            tool_args={"file_path": path, "_tail_n": n},
            hint="Use the read tool instead of tail for line numbers and read tracking.",
            confidence=0.8,
        )

    # bare tail -> last 17 lines
    return RouteResult(
        tool_id="read",
        tool_args={"file_path": path, "_tail_n": 17},
        hint="Use the read tool instead of tail for line numbers and read tracking.",
        confidence=0.8,
    )
```

**tail -n N 的 offset 计算问题**：`_tail_n` 是路由内部参数，不是 `read` 工具的原生参数。`_execute_routed` 需要在执行前将其转换为 `offset`：

```python
async def _execute_routed(route: RouteResult, ctx: ToolContext) -> ToolResult | None:
    tool_args = dict(route.tool_args)

    # 处理 tail -n N 的 offset 计算
    tail_n = tool_args.pop("_tail_n", None)
    if tail_n is not None:
        file_path = tool_args.get("file_path", "")
        path = resolve_safe(ctx.workspace, file_path, ctx.sandbox_extra_paths)
        if path is None or not path.exists():
            return None  # fallback to bash
        total_lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        offset = max(1, total_lines - tail_n + 1)
        tool_args["offset"] = offset

    tool = ctx.tool_registry.get(route.tool_id)
    if tool is None:
        return None
    result = await tool.execute(tool_args, ctx)
    hint_prefix = f"[Routed from bash → {route.tool_id}] {route.hint}\n\n"
    return ToolResult(
        title=result.title,
        output=hint_prefix + result.output,
        summary=result.summary,
        metadata={**result.metadata, "routed_from": "bash", "routed_to": route.tool_id},
        diff=result.diff,
    )
```

> ⚠️ **关键约束**：`_tail_n` 必须在调用目标工具前从 `tool_args` 中移除。`FileReadInput.model_validate(args)` 会因 `additionalProperties: false` 拒绝未知字段，导致验证失败。`tool_args.pop("_tail_n")` 不是可选清理，而是硬性要求——任何内部参数（以 `_` 开头）都不能传递到目标工具的 `model_validate`。

**tail offset 计算的双重读取问题**：当前方案在 `_execute_routed` 中先 `path.read_text()` 算总行数，然后 `read` 工具执行时再读一次文件。大文件场景下存在双重 I/O 开销。可接受的 trade-off：路由本身是低频路径（LLM 通常直接调用 `read`），且 fallback 到 bash 时 `tail` 也会读文件。如果后续需要优化，可以在 `_execute_routed` 中直接构造 `ToolResult` 而不走 `read` 工具，但会丢失文件状态追踪等优化——因此当前方案优先保证正确性。

### 文件搜索路由细节

```python
def _route_find(words: list[str]) -> RouteResult | None:
    """Route simple find -name commands to glob."""
    if len(words) < 4:
        return None

    args = words[1:]
    name_pattern = None
    base_dir = "."

    i = 0
    while i < len(args):
        if args[i] == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
        elif args[i] == "-type" and i + 1 < len(args):
            if args[i + 1] != "f":
                return None
            i += 2
        elif not args[i].startswith("-") and i == 0:
            base_dir = args[i]
            i += 1
        else:
            return None

    if name_pattern is None:
        return None

    if base_dir == ".":
        glob_pattern = f"**/{name_pattern}"
    else:
        glob_pattern = f"{base_dir}/**/{name_pattern}"

    return RouteResult(
        tool_id="glob",
        tool_args={"pattern": glob_pattern},
        hint="Use the glob tool instead of find for file discovery with skip-dir filtering.",
        confidence=0.85,
    )


def _route_ls(words: list[str]) -> RouteResult | None:
    """Route simple ls to glob."""
    args = words[1:]

    if any(a.startswith("-") for a in args):
        return None

    if len(args) == 0:
        pattern = "*"
    elif len(args) == 1:
        pattern = f"{args[0]}/*"
    else:
        return None

    return RouteResult(
        tool_id="glob",
        tool_args={"pattern": pattern},
        hint="Use the glob tool instead of ls for file listing with skip-dir filtering.",
        confidence=0.7,
    )
```

### 内容搜索路由细节

```python
_RG_TYPE_MAP = {
    "py": "*.py", "js": "*.js", "ts": "*.ts",
    "rs": "*.rs", "go": "*.go", "java": "*.java", "rb": "*.rb",
}

def _route_grep(words: list[str]) -> RouteResult | None:
    """Route grep/rg/egrep/fgrep to the grep tool."""
    prog = words[0].lower()
    args = words[1:]

    recursive = prog == "rg"
    include = None
    pattern = None
    path = None
    i = 0

    while i < len(args):
        a = args[i]
        if a in ("-r", "-R"):
            recursive = True
            i += 1
        elif a == "--include" and i + 1 < len(args):
            include = args[i + 1]
            i += 2
        elif a.startswith("--include="):
            include = a.split("=", 1)[1]
            i += 1
        elif a == "-t" and i + 1 < len(args) and prog == "rg":
            type_name = args[i + 1]
            include = _RG_TYPE_MAP.get(type_name)
            if include is None:
                return None
            i += 2
        elif a.startswith("-") and a not in ("-e", "-i", "-w"):
            return None
        elif pattern is None:
            pattern = a
            i += 1
        elif path is None:
            path = a
            i += 1
        else:
            return None

    if pattern is None:
        return None

    if prog == "fgrep":
        import re
        pattern = re.escape(pattern)

    tool_args = {"pattern": pattern}
    if path:
        tool_args["path"] = path
    if include:
        tool_args["include"] = include

    return RouteResult(
        tool_id="grep",
        tool_args=tool_args,
        hint=f"Use the grep tool instead of {prog} for structured output with skip-dir filtering.",
        confidence=0.85 if prog == "rg" else 0.8,
    )
```

### 文件编辑路由细节

```python
import re

_SED_SIMPLE = re.compile(r"^(\d+)s/([^/]*)/([^/]*)/?$")

def _route_sed(words: list[str]) -> RouteResult | None:
    """Route simple sed -i '<line>s/<old>/<new>/' to replace."""
    if len(words) < 3:
        return None

    args = words[1:]
    has_inplace = False
    script = None
    path = None
    i = 0

    if args[i] == "-i":
        has_inplace = True
        i += 1
        if i < len(args) and args[i] == "":
            i += 1
    elif args[i].startswith("-i"):
        has_inplace = True
        i += 1
    else:
        return None

    if i >= len(args):
        return None

    script = args[i]
    i += 1

    if i < len(args):
        path = args[i]
        i += 1

    if i != len(args) or script is None or path is None:
        return None

    m = _SED_SIMPLE.match(script)
    if not m:
        return None

    line_no = int(m.group(1))
    old_text = m.group(2)
    new_text = m.group(3)

    if "&" in new_text or r"\1" in new_text:
        return None

    # replace 工具的 prefix/suffix 是首行/末行的子串匹配，不是整个 old 的前后截断。
    # 单行替换时 start_no == end_no，prefix 和 suffix 应该是同一行的子串。
    # 当 old_text 过长时，前后截断会产生重叠且语义错误，因此限制路由范围。
    if len(old_text) > 80:
        return None  # old_text 过长，prefix/suffix 推导不可靠，不路由
    prefix = old_text
    suffix = old_text

    return RouteResult(
        tool_id="replace",
        tool_args={
            "file_path": path,
            "start_no": line_no,
            "end_no": line_no,
            "prefix": prefix,
            "suffix": suffix,
            "new_string": new_text,
        },
        hint="Use the replace tool instead of sed -i for file edits with staleness checking and diff output.",
        confidence=0.7,
    )
```

## API Contract

### try_route

- **Signature**: `try_route(command: str) -> RouteResult | None`
- **Input**: bash 命令字符串
- **Output**: `RouteResult` 如果可路由，`None` 如果不可路由
- **Side effects**: 无

### _execute_routed

- **Signature**: `async _execute_routed(route: RouteResult, ctx: ToolContext) -> ToolResult | None`
- **Input**: 路由结果 + 工具上下文
- **Output**: 专用工具的执行结果（带路由提示前缀），或 `None` 表示路由执行失败需 fallback
- **Side effects**: 通过专用工具执行，会触发文件状态追踪、版本保存等全部副作用

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 路由检测解析失败（shlex 异常） | 返回 `None`，走原始 bash |
| 目标工具不在 registry 中 | 返回 `None`，走原始 bash |
| 目标工具执行抛异常 | 捕获异常，返回 `None`，走原始 bash |
| 路由参数构造不完整 | 返回 `None`，走原始 bash |
| 专用工具返回 error（如文件不存在） | 正常返回路由结果，LLM 看到错误信息后自行调整 |
| tail offset 计算时文件不存在 | 返回 `None`，走原始 bash |

**核心原则**：路由是"尽力而为"的优化，绝不能因为路由逻辑的 bug 导致原本可以执行的 bash 命令失败。

## ToolContext 扩展

当前 `ToolContext` 没有 `tool_registry` 引用。需要新增：

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.tools.registry import ToolRegistry

class ToolContext(BaseModel):
    ...
    tool_registry: ToolRegistry | None = Field(default=None, exclude=True)
```

> **类型安全**：使用 `TYPE_CHECKING` 避免循环导入（`tools.base` → `tools.registry` → `tools.bash` → `tools.base`）。运行时 `tool_registry` 的实际类型由 `executor.py` 注入保证。`_execute_routed` 中通过 `ctx.tool_registry.get(route.tool_id)` 访问，若 `tool_registry` 为 `None` 则 fallback 到原始 bash。

在 `executor.py` 的 `make_context()` 中注入：

```python
def make_context() -> ToolContext:
    return ToolContext(
        ...
        tool_registry=host.tools,  # 新增：ToolRegistry 实例
    )
```

**路由执行时的共享状态**：`_execute_routed` 通过 `ctx` 调用目标工具时，`ctx.file_mtimes` 和 `ctx.file_read_coverage` 是 host 上的共享引用（通过 `ToolContext.__init__` 中的 `_file_mtimes`/`_file_read_coverage` 私有属性实现）。路由执行的目标工具对 `ctx` 的修改（如 `record_mtime`、`record_read_range`）会正确传播到后续工具调用，与直接调用专用工具的行为一致。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 路由在 BashTool.execute 内部执行 | 在 executor 层拦截 | BashTool 内部更内聚，不需要修改 executor 通用逻辑 |
| 路由失败静默 fallback | 路由失败返回错误 | 不阻断工作流是硬性要求 |
| 通过 ToolContext 传递 registry | 直接 import 全局 registry | 避免循环依赖，保持可测试性 |
| 只路由简单命令 | 尝试解析管道和重定向 | 简单命令覆盖 90% 场景，复杂解析容易误路由 |
| 不修改 bash description | 在 description 中加"优先用专用工具" | description 修改是独立优化，路由是运行时兜底，两层互补 |
| 路由提示放在 output 前缀 | 放在 metadata 或 next_step_hint | LLM 最容易注意到 output 前缀；metadata 对 LLM 不可见 |
| tail -n N 用 _tail_n 内部参数 | 直接在路由时算 offset | 路由层不应有 IO 副作用，offset 计算放在执行层 |
| sed 只路由单行替换 | 尝试解析多行/范围替换 | replace 工具需要精确行号，sed 范围语义映射复杂且易错 |
| echo/printf 写入支持引号解析 | 只路由 heredoc | 引号解析是 bash 的核心语义，覆盖常见场景收益大 |
| ls 路由为单层 glob | 不路由 ls | ls 是高频命令，单层 glob 覆盖最常见用法 |
| 不可路由命令返回 Route Hint | 直接 fallback 到 bash | 提示引导 LLM 下次用专用工具，渐进改善路由覆盖率 |
| 新增 `sub` 兼容工具覆盖 sed 全局替换 | 只用 Route Hint | `sed -i 's/old/new/g'` 是高频操作，直接路由比提示更高效 |
| 新增 `append` 兼容工具覆盖 echo 追加 | 只用 Route Hint | `echo >> file` 是高频操作，`append` 比 `insert(lineno=-1)` 语义更直接 |
| Route Hint 附加在 bash 输出末尾 | 中断 bash 执行只返回提示 | 不阻断工作流，bash 仍然执行，提示是附加信息 |

## Open Questions

- [ ] 是否需要对路由行为做可观测性（日志/metrics），方便后续调优路由规则？
- [ ] heredoc 写入路由的解析复杂度是否可接受？是否需要限制 heredoc 长度？
- [ ] `rg -t` 类型映射表是否需要更完整？是否应该从 ripgrep 的配置文件读取？
- [ ] sed 路由的 prefix/suffix 推导是否足够准确？是否需要先读文件验证？
- [ ] Route Hint 是否应该统计 LLM 后续是否采纳了建议（即下次是否改用专用工具），用于评估提示效果？
- [ ] `sub` 工具的字面量匹配是否足够？是否需要支持正则模式（增加 `regex: bool` 参数）？
- [ ] `append` 工具与 `insert(lineno=-1)` 功能重叠，是否应该统一为 `insert` 的简化调用方式而非独立工具？
- [ ] Route Hint 附加在 bash 输出末尾，LLM 是否会注意到？是否需要放在更醒目的位置？

- [ ] `rg -t` 类型映射表是否应该从 `rg --type-list` 动态获取而非硬编码？
- [ ] `grep -i`（忽略大小写）和 `grep -w`（全词匹配）当前被放行但 `GrepInput` 无对应参数，路由后 flag 会丢失——是否应加入排除列表？
- [ ] 快速排除中 `&` 检查会误杀包含 `&` 字符的参数值（如 git commit message），是否可接受？

## Review Notes

> 评审日期：2026-06-19。以下问题已在文档正文中修正（标记为 ⚠️），此处记录评审发现的全貌。

### 已修正（3 项）

1. **`_tail_n` 内部参数导致 `model_validate` 失败**：`FileReadInput` 设置了 `additionalProperties: false`，`_tail_n` 字段会导致验证报错。已在 tail offset 计算段落补充关键约束说明，明确 `tool_args.pop("_tail_n")` 是硬性要求。同时补充了双重读取问题的 trade-off 分析。

2. **sed 路由的 prefix/suffix 推导逻辑错误**：原方案对 `old_text` 做前后 40 字符截断作为 prefix/suffix，但 `FileReplaceInput` 的 prefix/suffix 是首行/末行的子串匹配语义，截断会导致行定位失败。已修正为：`old_text` 长度 > 80 时不路由，≤ 80 时直接用 `old_text` 整体作为 prefix 和 suffix（单行场景下两者相同）。

3. **`ToolContext.tool_registry` 类型为 `Any` 缺乏类型安全**：已改为 `ToolRegistry | None`，使用 `TYPE_CHECKING` 避免循环导入。补充了路由执行时 `ctx` 共享状态（file_mtimes、file_read_coverage）正确传播的说明。

### 建议改进（5 项，未在正文中修改）

4. **快速排除 `&` 的粒度**：`any(ch in stripped for ch in ("|", ";", "&", "$"))` 中 `&` 会误杀 `git log --author="Tom & Jerry"` 等合法但罕见的场景。当前 trade-off 可接受，但建议在实现时加注释说明。

5. **echo/printf 双引号写入路由复杂度被低估**：bash 双引号的转义规则远不止 `\n`/`\t`/`\\`，还有 `\a`、`\x41`、`\u0041`、`\0nnn` 等，且 `echo` 的 `-e`/`-E` flag 影响转义行为。建议首版只路由单引号 echo 和 heredoc，双引号 echo 标记为不可路由。

6. **`ls` 路由为 glob 的输出格式差异**：`ls` 输出纯文件名，`glob` 输出相对路径。建议在 hint 中明确说明格式差异，如 "Note: glob returns relative paths, not bare filenames."

7. **`sed -i ''` BSD 语法处理**：`shlex.split("sed -i '' '3s/old/new/' file.py")` 和 `shlex.split("sed -i'' '3s/old/new/' file.py")` 两种写法的 token 分割路径不同，当前代码逻辑正确但建议加注释说明。

8. **`rg -t` 类型映射表硬编码**：当前仅 7 种类型，`rg --type-list` 支持几十种。建议在 Open Questions 中补充是否应动态获取（已补充）。

### 小建议（4 项，未在正文中修改）

9. **`RouteResult.confidence` 类型**：当前为 `float`，建议改为枚举级别（high/medium/low）或 `Literal[0.7, 0.8, 0.85, 0.9, 0.95]`，避免浮点精度问题，也便于后续做路由阈值过滤。

10. **路由提示位置一致性**：路由成功时提示在 output 前缀（`hint_prefix`），Route Hint 在 output 后缀。建议在文档中明确区分这两种场景的提示位置约定。

11. **`grep -i`/`grep -w` flag 丢失**：`_route_grep` 中 `-i` 和 `-w` 被放行（不在排除列表中），但 `GrepInput` 没有 `case_insensitive` 或 `whole_word` 参数，路由后这些 flag 会静默丢失。建议加入排除列表（已补充到 Open Questions）。

12. **文档状态**：文档位于 `docs/specs/`，按 AGENTS.md 的 Document Lifecycle 应为 "approved designs awaiting or in implementation"。建议确认状态标注。