> **Status: Done** — Implemented in `src/voidx/tools/bash_router.py`, integrated in `src/voidx/tools/bash.py`. Tests: `tests/test_tools/test_bash_router.py`, `tests/test_tools/test_bash_tool.py`.

# Bash 工具提示兜底 — 技术设计文档

## Context

bash 工具是万能后门：LLM 可以用 `bash -c "cat file"` 代替 `read`，用 `bash -c "sed -i ..."` 代替 `replace`，用 `bash -c "git status"` 代替 `git` 工具。这些场景下，专用工具的优化链路全部被绕过：

- **文件状态追踪失效**：staleness 检查、read coverage、mtime 追踪、版本快照全部跳过
- **权限分类粗化**：`bash git commit` 走 `BASH_WRITE` 而非 `GIT_WRITE`，sandbox 校验精度下降
- **结构化输出丢失**：git 工具返回 JSON，bash 返回纯文本；read 有行号分页，cat 没有
- **Runtime Guards 去重失效**：bash 归一化只压缩空格，无法像专用工具那样按 file_path 去重

根因：没有提示层。bash 工具的 description 和 LLM prompt 中均无"优先使用专用工具"的引导，也没有执行时的提示机制。

## Goals and Non-Goals

### Goals

- 在 bash 工具执行后检测可由专用工具替代的命令
- 在返回结果中附带提示信息，引导 LLM 后续直接使用专用工具
- 提示信息包含推荐工具和操作建议，降低 LLM 的迁移成本

### Non-Goals

- 不自动路由执行专用工具——提示是纯信息性的，LLM 自行决定是否采纳
- 不拦截或阻断 bash 执行——bash 照常运行，提示只是附加信息
- 不拦截复杂管道、多命令组合（`git status && echo done`）
- 不拦截带 shell 特性的命令（变量替换、子 shell、重定向组合）
- 不修改 LLM prompt 或 bash 工具 description（提示兜底是运行时行为，prompt 引导是独立优化）
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
  原始 bash 执行（照常运行）
       │
       ▼
  try_hint()  ── 可提示? ──→ 追加 ui_label 到 output，llm_hint 到 next_step_hint
       │
       │ 不可提示
       ▼
  返回 ToolResult
```

提示检测在 bash 执行**之后**运行。这样保证：
1. bash 命令照常执行，不因提示逻辑的 bug 而失败
2. 提示是附加信息，不影响 bash 的输出和退出码
3. LLM 看到完整的 bash 输出后，再看到提示，可以自行判断是否采纳

### 提示的两个通道

`ToolResult` 有两个消费者，提示需要分别投递：

| 通道 | 字段 | 消费者 | 内容 |
|------|------|--------|------|
| UI 标记 | `output` 末尾追加 | TUI/Web 用户 + LLM | `[→ read]` 短标记 |
| LLM 引导 | `next_step_hint` | 仅 LLM（executor 追加到 ToolMessage） | `Prefer read(file_path="...") for line numbers and file tracking.` |

`next_step_hint` 不进 UI 渲染——executor 只把它追加到 `ToolMessage.content`，`notify_tool_text_output` 只读 `result.output`。

### 提示检测模块

新增 `src/voidx/tools/bash_router.py`，职责单一：解析 bash 命令，判断是否可提示，构造提示信息。

```python
from typing import Literal
@dataclass
class RouteHint:
    tool_id: Literal["read", "git", "write", "replace", "insert", "glob", "grep"]
    ui_label: str         # "→ read"
    llm_hint: str         # 'Prefer read(file_path="...") for line numbers and file tracking.'

def try_hint(command: str) -> RouteHint | None:
    """尝试为 bash 命令生成专用工具提示。返回 None 表示不可提示。

    内部捕获所有异常并返回 None，保证提示逻辑的 bug 不影响 bash 执行结果。
    """
    try:
        return _try_hint_impl(command)
    except Exception:
        return None

def _try_hint_impl(command: str) -> RouteHint | None:
```

### 提示注入

在 `BashTool.execute()` 中，bash 执行完成后调用提示检测：

```python
async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
    inp = BashInput.model_validate(args)
    blocked = _check_command(inp.command)
    if blocked:
        return ToolResult(output=blocked, ...)
    blocked = _sandbox_denial(inp.command, ctx)
    if blocked:
        return ToolResult(output=blocked, ...)

    result = await _run_bash(inp, ctx)

    # --- 新增：提示兜底 ---
    hint = try_hint(inp.command)
    if hint is not None:
        result = ToolResult(
            title=result.title,
            output=result.output + f"\n[{hint.ui_label}]",
            summary=result.summary,
            metadata={**result.metadata, "route_hint": {"tool_id": hint.tool_id, "command": inp.command}},
            diff=result.diff,
            next_step_hint=hint.llm_hint,
        )
    return result
```

## Data Model

```
RouteHint
├── tool_id: Literal["read", "git", "write", "replace", "insert", "glob", "grep"]
├── ui_label: str      # "→ read" — 追加到 output 末尾
└── llm_hint: str      # "Prefer read(file_path=\"...\") for line numbers and file tracking." — 通过 next_step_hint 传递
```

## 提示规则

### 1. Git 命令 → `git` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `git status` | → git | Prefer git(command="status") for structured JSON output. |
| `git status -- <paths>` | → git | Prefer git(command="status", args={"pathspec": ["<paths>"]}) for structured JSON output. |
| `git diff` | → git | Prefer git(command="diff") for structured JSON output. |
| `git diff --cached` | → git | Prefer git(command="diff", args={"cached": true}) for structured JSON output. |
| `git diff <ref>` | → git | Prefer git(command="diff", args={"ref": "<ref>"}) for structured JSON output. |
| `git diff -- <paths>` | → git | Prefer git(command="diff", args={"pathspec": ["<paths>"]}) for structured JSON output. |
| `git log` | → git | Prefer git(command="log") for structured JSON output. |
| `git log -n <N>` | → git | Prefer git(command="log", args={"limit": N}) for structured JSON output. |
| `git log --author=<who>` | → git | Prefer git(command="log", args={"author": "<who>"}) for structured JSON output. |
| `git log --since=<date>` | → git | Prefer git(command="log", args={"since": "<date>"}) for structured JSON output. |
| `git log -- <path>` | → git | Prefer git(command="log", args={"path": "<path>"}) for structured JSON output. |
| `git log -n <N> -- <path>` | → git | Prefer git(command="log", args={"limit": N, "path": "<path>"}) for structured JSON output. |
| `git blame <path>` | → git | Prefer git(command="blame", args={"path": "<path>"}) for structured JSON output. |
| `git blame -L <start>,<end> <path>` | → git | Prefer git(command="blame", args={"path": "<path>", "start": <start>, "end": <end>}) for structured JSON output. |
| `git branch` | → git | Prefer git(command="branch_list") for structured JSON output. |
| `git branch -a` / `git branch --all` | → git | Prefer git(command="branch_list", args={"all": true}) for structured JSON output. |
| `git remote -v` | → git | Prefer git(command="remote_list") for structured JSON output. |
| `git add <paths>` | → git | Prefer git(command="add", args={"paths": ["<paths>"]}) for permission-scoped git operations. |
| `git commit -m "<msg>"` | → git | Prefer git(command="commit", args={"message": "<msg>"}) for permission-scoped git operations. |
| `git commit -m "<msg>" -- <paths>` | → git | Prefer git(command="commit", args={"message": "<msg>", "paths": ["<paths>"]}) for permission-scoped git operations. |
| `git restore <paths>` | → git | Prefer git(command="restore", args={"paths": ["<paths>"]}) for permission-scoped git operations. |
| `git restore --staged <paths>` | → git | Prefer git(command="restore", args={"paths": ["<paths>"], "staged": true}) for permission-scoped git operations. |

**不提示**：`git push`、`git pull`、`git merge`、`git rebase`、`git stash`、`git cherry-pick`、`git reset`、`git checkout`（分支切换）、`git switch`、`git fetch`、`git clone`、`git init`——这些没有对应的专用工具 command。

### 2. 文件读取 → `read` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `cat <path>` | → read | Prefer read(file_path="<path>") for line numbers and file tracking. |
| `head -n <N> <path>` | → read | Prefer read(file_path="<path>", limit=N) for line numbers and file tracking. |
| `head <path>` | → read | Prefer read(file_path="<path>", limit=10) for line numbers and file tracking. |
| `tail -n +<N> <path>` | → read | Prefer read(file_path="<path>", offset=N) for line numbers and file tracking. |

**不提示**：`tail -n <N> <path>`（最后 N 行，无法精确映射到 read 的 offset+limit）、`tail <path>`（语义模糊）、`cat a.py b.py`（多文件）、`cat <path> | grep`（管道）、`cat <path> > other`（重定向）、`head -f` / `tail -f`（follow 模式）。

### 3. 文件写入 → `write` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `cat > <path> << 'EOF'\n...\nEOF` | → write | Prefer write(file_path="<path>", content="...") for file tracking and diff output. |
| `echo '<content>' > <path>` | → write | Prefer write(file_path="<path>", content="<content>") for file tracking and diff output. |
| `printf '<content>' > <path>` | → write | Prefer write(file_path="<path>", content="<content>") for file tracking and diff output. |

**echo/printf 写入提示策略**：
- 单引号内容：提示中直接引用原始内容
- 双引号内容：不提示（bash 双引号转义规则复杂，提示中无法准确表达语义）
- 无引号内容：不提示（空格分割歧义）

**不提示**：`echo >> <path>`（追加模式，见 `insert` 提示）、`echo "<content>" > <path>`（双引号转义歧义）、`tee`（管道场景）、`echo $VAR > file`（变量替换）、多命令组合。

### 4. 文件追加 → `insert` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `echo '<content>' >> <path>` | → insert | Prefer insert(file_path="<path>", lineno=-1, new_string="<content>") for file tracking and diff output. |

**不提示**：`echo "<content>" >> <path>`（双引号转义歧义）、`echo $VAR >> file`（变量替换）。

### 5. 文件搜索 → `glob` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `find . -name "<pattern>"` | → glob | Prefer glob(pattern="**/<pattern>") — skips .git, node_modules, and build dirs automatically. |
| `find . -type f -name "<pattern>"` | → glob | Prefer glob(pattern="**/<pattern>") — skips .git, node_modules, and build dirs automatically. |
| `find <dir> -name "<pattern>"` | → glob | Prefer glob(pattern="<dir>/**/<pattern>") — skips .git, node_modules, and build dirs automatically. |

**find 提示策略**：只提示 `-name` 过滤的简单 find，忽略 `-type`、`-path` 等复杂谓词。`find . -name "*.py"` 等价于 `glob **/*.py`，需要将 `-name` 的 glob 模式转换为 `**/<pattern>`。

**不提示**：`find . -exec`（执行动作）、`find . -type d`（目录搜索，glob 不区分类型）、`find . -newer`（时间过滤）、`find . -size`（大小过滤）、`ls`（输出格式差异大）、`ls -la`（详细信息）、`ls -R`（递归）。

### 6. 内容搜索 → `grep` 工具

| bash 命令模式 | ui_label | llm_hint |
|--------------|----------|----------|
| `grep <pattern> <path>` | → grep | Prefer grep(pattern="<pattern>", path="<path>") — skips .git, node_modules, and binary files automatically. |
| `grep -r <pattern>` | → grep | Prefer grep(pattern="<pattern>") — searches recursively by default. |
| `grep -r <pattern> <dir>` | → grep | Prefer grep(pattern="<pattern>", path="<dir>") — searches recursively by default. |
| `grep --include="<glob>" <pattern>` | → grep | Prefer grep(pattern="<pattern>", include="<glob>") — skips .git, node_modules, and binary files automatically. |
| `grep -r --include="<glob>" <pattern> <dir>` | → grep | Prefer grep(pattern="<pattern>", path="<dir>", include="<glob>") — searches recursively by default. |
| `rg <pattern>` | → grep | Prefer grep(pattern="<pattern>") for structured output with skip-dir filtering. |
| `rg <pattern> <path>` | → grep | Prefer grep(pattern="<pattern>", path="<path>") for structured output with skip-dir filtering. |
| `rg -t <type> <pattern>` | → grep | Prefer grep(pattern="<pattern>", include="<type_glob>") for structured output with skip-dir filtering. |
| `egrep <pattern> <path>` | → grep | Prefer grep(pattern="<pattern>", path="<path>") — supports regex by default. |
| `fgrep <string> <path>` | → grep | Prefer grep(pattern="<escaped>", path="<path>") — fgrep is literal string match; pattern is re.escape'd to preserve literal semantics. |

**不提示**：`grep -l`（只列文件名）、`grep -c`（只计数）、`grep -v`（反向匹配）、`grep -A/-B/-C`（上下文行）、`grep -i`（忽略大小写）、`grep -w`（全词匹配）、管道组合（`cat file | grep`）。

> ⚠️ `grep -i` 和 `grep -w` 被排除，因为 `GrepInput` 没有 `case_insensitive` 或 `whole_word` 参数。提示后 LLM 会丢失这些 flag，导致搜索结果不同——这是语义差异，不是格式差异。

## 提示检测算法

```python
def _try_hint_impl(command: str) -> RouteHint | None:
    stripped = command.strip()
    if not stripped:
        return None

    # 快速排除：管道、多命令、子 shell、变量替换
    if any(ch in stripped for ch in ("|", ";", "$")):
        return None
    # & 检查：只排除命令分隔位置的 &（&&、 & 、&|），
    # 不排除参数值中的 &（如 git commit -m "A & B"）
    if re.search(r'&&|\s&$', stripped):
        return None
    if "`" in stripped or "$(" in stripped:
        return None

    words = _shell_words(stripped)
    if not words:
        return None

    prog = words[0].lower()

    if prog == "git" and len(words) >= 2:
        return _hint_git(words)
    if prog in ("cat", "head", "tail"):
        if prog == "cat" and "<<" in stripped:
            return _hint_write_heredoc(stripped)
        return _hint_read(words)
    if prog in ("echo", "printf") and ">" in stripped:
        return _hint_write_echo(stripped, words)
    if prog == "find":
        return _hint_find(words)
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return _hint_grep(words)
    if prog == "sed":
        return _hint_sed(words)

    return None
```

### `_shell_words`

```python
import shlex

def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []
```

### 快速排除原则

以下情况**不提示**，bash 照常执行：
- 包含管道 `|`、分号 `;`、`&&`/`||` 的多命令
- 包含命令替换 `$()` 或反引号
- 包含环境变量 `$VAR`
- 重定向与管道组合（`cat file | grep`）
- 多文件参数（`cat a.py b.py`）
- `head`/`tail` 带 `-f`（follow 模式）

> ⚠️ `&` 的检查使用正则 `r'&&|\s&$'`。`&&` 匹配命令链（如 `git status && echo done`），`\s&$` 匹配后台运行语法（如 `git status &`）。不检查孤立的 `\s&\s`，因为无法区分引号内外的 `&`（如 `git commit -m "feat: add A & B support"` 中的 `&` 在引号内，不应被排除）。

### Git 提示

```python
_UNHINTABLE_GIT_SUBCOMMANDS = frozenset({
    "push", "pull", "merge", "rebase", "stash", "cherry-pick",
    "reset", "checkout", "switch", "fetch", "clone", "init",
    "submodule", "filter-branch", "bisect",
})

def _hint_git(words: list[str]) -> RouteHint | None:
    subcommand = words[1]
    rest = words[2:]
    if subcommand in _UNHINTABLE_GIT_SUBCOMMANDS:
        return None
    mapping = {
        "status": _hint_git_status,
        "diff": _hint_git_diff,
        "log": _hint_git_log,
        "blame": _hint_git_blame,
        "branch": _hint_git_branch,
        "remote": _hint_git_remote,
        "add": _hint_git_add,
        "commit": _hint_git_commit,
        "restore": _hint_git_restore,
    }
    hinter = mapping.get(subcommand)
    if hinter is None:
        return None
    return hinter(rest)
```

### 文件读取提示

```python
def _hint_read(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    if any(a.startswith("-") and a not in ("-n",) for a in args):
        return None

    if prog == "cat":
        if len(args) != 1:
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{args[0]}") for line numbers and file tracking.',
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
            elif re.match(r"^-\d+$", args[i]):
                # 老式写法：head -5 file
                try:
                    limit = int(args[i][1:])
                except ValueError:
                    return None
                i += 1
            else:
                return None
        if path is None:
            return None
        return RouteHint(
            tool_id="read", ui_label="→ read",
            llm_hint=f'Prefer read(file_path="{path}", limit={limit}) for line numbers and file tracking.',
        )

    if prog == "tail":
        return _hint_tail(args)
    return None
```

### tail 提示

```python
def _hint_tail(args: list[str]) -> RouteHint | None:
    """tail 提示策略：
    - tail -n +N: 从第 N 行到末尾 → read(offset=N)
    - tail -n N: 最后 N 行 → 不提示（无法精确映射到 read 的 offset+limit）
    - tail: 不提示（语义模糊）
    - tail -f: 不提示（follow 模式，已在快速排除中处理）
    """
    if not args:
        return None
    path = None
    offset = None
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            val = args[i + 1]
            if val.startswith("+"):
                try:
                    offset = int(val[1:])
                except ValueError:
                    return None
            else:
                # tail -n N (最后 N 行) — 无法精确映射，不提示
                return None
            i += 2
        elif not args[i].startswith("-"):
            path = args[i]
            i += 1
        else:
            return None
    if path is None or offset is None:
        return None
    return RouteHint(
        tool_id="read", ui_label="→ read",
        llm_hint=f'Prefer read(file_path="{path}", offset={offset}) for line numbers and file tracking.',
    )
```

### echo/printf 写入提示

```python
def _hint_write_echo(stripped: str, words: list[str]) -> RouteHint | None:
    """echo/printf 写入提示策略：
    - 单引号内容：提示中直接引用原始内容
    - 双引号内容：不提示（bash 双引号转义规则复杂）
    - 无引号内容：不提示（空格分割歧义）
    - content 中包含双引号 "：不提示（llm_hint 中 content 用双引号包裹，转义歧义）
    - >> 追加：提示用 insert 工具
    - > 覆盖：提示用 write 工具
    """
    # 判断追加 vs 覆盖
    is_append = ">>" in stripped
    redirect = ">>" if is_append else ">"

    # 从原始命令中提取重定向后的路径
    # 使用原始字符串而非 shlex 结果，因为需要判断引号类型
    parts = stripped.split(redirect, 1)
    if len(parts) != 2:
        return None
    after_redirect = parts[1].strip()
    path = after_redirect.strip().strip("'\"")
    if not path:
        return None

    # 提取内容：words[0] 是 echo/printf，words[1] 是内容（shlex 已剥离引号）
    prog = words[0].lower()
    if len(words) < 2:
        return None

    # 判断原始引号类型：检查 stripped 中 echo/printf 后的第一个非空字符
    rest_after_prog = stripped[len(prog):].lstrip()
    if not rest_after_prog:
        return None
    first_char = rest_after_prog[0]

    if first_char == "'":
        # 单引号内容 — shlex 已剥离引号，words[1] 是原始内容
        content = words[1]
    elif first_char == '"':
        # 双引号内容 — 不提示（转义歧义）
        return None
    else:
        # 无引号内容 — 不提示（空格分割歧义）
        return None

    # content 中包含双引号时不提示（llm_hint 中 content 用双引号包裹，转义歧义）
    if '"' in content:
        return None

    if is_append:
        return RouteHint(
            tool_id="insert", ui_label="→ insert",
            llm_hint=f'Prefer insert(file_path="{path}", lineno=-1, new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="write", ui_label="→ write",
        llm_hint=f'Prefer write(file_path="{path}", content="{content}") for file tracking and diff output.',
    )
```

### heredoc 写入提示

```python
def _hint_write_heredoc(stripped: str) -> RouteHint | None:
    """heredoc 写入提示策略：
    - cat > path << 'EOF' 和 cat << 'EOF' > path 两种顺序
    - cat >> path << 'EOF' 追加模式提示用 insert
    - heredoc 内容中包含双引号时不提示（llm_hint 中 content 用双引号包裹，转义歧义）
    - heredoc 内容超过 200 字符时不提示（提示过长影响 LLM 上下文）
    """
    # 判断追加 vs 覆盖
    is_append = ">>" in stripped

    # 提取路径：处理 cat > path << 'EOF' 和 cat << 'EOF' > path 两种顺序
    path = None
    redirect_op = ">>" if is_append else ">"

    if redirect_op in stripped and "<<" in stripped:
        redirect_idx = stripped.index(redirect_op)
        heredoc_idx = stripped.index("<<")
        if redirect_idx < heredoc_idx:
            # cat > path << 'EOF'
            between = stripped[redirect_idx + len(redirect_op):heredoc_idx].strip()
            path = between.strip("'\"")
        else:
            # cat << 'EOF' > path — 路径在 heredoc 结束标记之后
            # 这种格式解析复杂，不提示
            return None

    if not path:
        return None

    # 提取 heredoc 内容
    heredoc_marker_match = re.search(r"<<\s*['\"]?(\w+)['\"]?", stripped)
    if not heredoc_marker_match:
        return None
    marker = heredoc_marker_match.group(1)
    # 内容在 marker 后的换行到结束 marker 之间
    marker_start = stripped.find(marker)
    if marker_start == -1:
        return None
    # 找到第一个换行后的内容开始位置
    content_start = stripped.find("\n", marker_start)
    if content_start == -1:
        return None
    content_end = stripped.rfind(marker)
    if content_end <= content_start:
        return None
    content = stripped[content_start + 1:content_end].rstrip("\n")

    # 内容过长不提示
    if len(content) > 200:
        return None

    # 内容中包含双引号时不提示
    if '"' in content:
        return None

    if is_append:
        return RouteHint(
            tool_id="insert", ui_label="→ insert",
            llm_hint=f'Prefer insert(file_path="{path}", lineno=-1, new_string="{content}") for file tracking and diff output.',
        )
    return RouteHint(
        tool_id="write", ui_label="→ write",
        llm_hint=f'Prefer write(file_path="{path}", content="{content}") for file tracking and diff output.',
    )
```

### Git 子命令提示

```python
def _hint_git_status(rest: list[str]) -> RouteHint | None:
    pathspec = []
    i = 0
    while i < len(rest):
        if rest[i] == "--" and i + 1 < len(rest):
            # git status -- <paths>
            pathspec = rest[i + 1:]
            break
        elif rest[i].startswith("-"):
            # 其他 flag（如 -s, --short, --porcelain）— 不提示
            return None
        else:
            # 位置参数可能是 pathspec
            pathspec.append(rest[i])
        i += 1
    if pathspec:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="status", args={{"pathspec": {pathspec}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="status") for structured JSON output.',
    )


def _hint_git_diff(rest: list[str]) -> RouteHint | None:
    cached = False
    ref = ""
    pathspec = []
    i = 0
    while i < len(rest):
        if rest[i] == "--cached":
            cached = True
            i += 1
        elif rest[i] == "--" and i + 1 < len(rest):
            pathspec = rest[i + 1:]
            break
        elif rest[i].startswith("-"):
            return None
        elif not ref:
            ref = rest[i]
            i += 1
        else:
            return None
    args_parts = []
    if cached:
        args_parts.append('"cached": true')
    if ref:
        args_parts.append(f'"ref": "{ref}"')
    if pathspec:
        args_parts.append(f'"pathspec": {pathspec}')
    args_str = ", ".join(args_parts)
    if args_str:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="diff", args={{{args_str}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="diff") for structured JSON output.',
    )


def _hint_git_log(rest: list[str]) -> RouteHint | None:
    limit = None
    author = ""
    since = ""
    path = ""
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "-n" and i + 1 < len(rest):
            try:
                limit = int(rest[i + 1])
            except ValueError:
                return None
            i += 2
        elif a.startswith("-"):
            # --author=, --since=, 其他 flag
            if a.startswith("--author="):
                author = a.split("=", 1)[1]
                i += 1
            elif a.startswith("--since="):
                since = a.split("=", 1)[1]
                i += 1
            else:
                return None
        elif a == "--" and i + 1 < len(rest):
            path = rest[i + 1]
            break
        elif not path:
            path = a
            i += 1
        else:
            return None
    args_parts = []
    if limit is not None:
        args_parts.append(f'"limit": {limit}')
    if author:
        args_parts.append(f'"author": "{author}"')
    if since:
        args_parts.append(f'"since": "{since}"')
    if path:
        args_parts.append(f'"path": "{path}"')
    args_str = ", ".join(args_parts)
    if args_str:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint=f'Prefer git(command="log", args={{{args_str}}}) for structured JSON output.',
        )
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint='Prefer git(command="log") for structured JSON output.',
    )


def _hint_git_blame(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    path = None
    start = None
    end = None
    i = 0
    while i < len(rest):
        if rest[i] == "-L" and i + 1 < len(rest):
            # -L start,end
            parts = rest[i + 1].split(",", 1)
            if len(parts) != 2:
                return None
            try:
                start = int(parts[0])
                end = int(parts[1])
            except ValueError:
                return None
            i += 2
        elif rest[i].startswith("-"):
            return None
        elif path is None:
            path = rest[i]
            i += 1
        else:
            return None
    if path is None:
        return None
    args_parts = [f'"path": "{path}"']
    if start is not None and end is not None:
        args_parts.append(f'"start": {start}')
        args_parts.append(f'"end": {end}')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="blame", args={{{args_str}}}) for structured JSON output.',
    )


def _hint_git_branch(rest: list[str]) -> RouteHint | None:
    if not rest:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="branch_list") for structured JSON output.',
        )
    if rest == ["-a"] or rest == ["--all"]:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="branch_list", args={"all": true}) for structured JSON output.',
        )
    return None


def _hint_git_remote(rest: list[str]) -> RouteHint | None:
    if rest == ["-v"]:
        return RouteHint(
            tool_id="git", ui_label="→ git",
            llm_hint='Prefer git(command="remote_list") for structured JSON output.',
        )
    return None


def _hint_git_add(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    # 跳过常见 flag
    paths = []
    for a in rest:
        if a.startswith("-"):
            return None
        paths.append(a)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="add", args={{"paths": {paths}}}) for permission-scoped git operations.',
    )


def _hint_git_commit(rest: list[str]) -> RouteHint | None:
    message = ""
    paths = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "-m" and i + 1 < len(rest):
            message = rest[i + 1]
            i += 2
        elif a.startswith("-m"):
            # -m"msg" 紧凑写法（shlex.split 后变为 -mmsg）
            message = a[2:]
            i += 1
        elif a.startswith("--message="):
            message = a.split("=", 1)[1]
            i += 1
        elif a == "--" and i + 1 < len(rest):
            paths = rest[i + 1:]
            break
        elif a.startswith("-"):
            return None
        else:
            paths.append(a)
            i += 1
    if not message:
        return None
    # message 中包含双引号时不提示（llm_hint 中用双引号包裹，转义歧义）
    if '"' in message:
        return None
    args_parts = [f'"message": "{message}"']
    if paths:
        args_parts.append(f'"paths": {paths}')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="commit", args={{{args_str}}}) for permission-scoped git operations.',
    )


def _hint_git_restore(rest: list[str]) -> RouteHint | None:
    if not rest:
        return None
    staged = False
    paths = []
    i = 0
    while i < len(rest):
        if rest[i] == "--staged":
            staged = True
            i += 1
        elif rest[i].startswith("-"):
            return None
        else:
            paths.append(rest[i])
            i += 1
    if not paths:
        return None
    args_parts = [f'"paths": {paths}']
    if staged:
        args_parts.append('"staged": true')
    args_str = ", ".join(args_parts)
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=f'Prefer git(command="restore", args={{{args_str}}}) for permission-scoped git operations.',
    )
```

### 文件搜索提示

```python
def _hint_find(words: list[str]) -> RouteHint | None:
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
    glob_pattern = f"**/{name_pattern}" if base_dir == "." else f"{base_dir}/**/{name_pattern}"
    return RouteHint(
        tool_id="glob", ui_label="→ glob",
        llm_hint=f'Prefer glob(pattern="{glob_pattern}") — skips .git, node_modules, and build dirs automatically.',
    )
```

### 内容搜索提示

```python
_RG_TYPE_MAP = {
    "py": "*.py", "js": "*.js", "ts": "*.ts",
    "rs": "*.rs", "go": "*.go", "java": "*.java", "rb": "*.rb",
}

# rg -t 支持几十种语言类型，此处只映射常见类型。
# 未知类型时 _RG_TYPE_MAP.get(type_name) 返回 None，_hint_grep 不提示。

def _hint_grep(words: list[str]) -> RouteHint | None:
    prog = words[0].lower()
    args = words[1:]
    include = None
    pattern = None
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "-R"):
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
        elif a.startswith("-") and a not in ("-e",):
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
    parts = [f'pattern="{pattern}"']
    if path:
        parts.append(f'path="{path}"')
    if include:
        parts.append(f'include="{include}"')
    return RouteHint(
        tool_id="grep", ui_label="→ grep",
        llm_hint=f'Prefer grep({", ".join(parts)}) — skips .git, node_modules, and binary files automatically.',
    )
```

### 文件编辑提示

```python
_SED_SIMPLE = re.compile(r"^(\d+)s/([^/]*)/([^/]*)/?$")
_SED_GLOBAL = re.compile(r"^s/([^/]*)/([^/]*)/g?$")
_SED_RANGE_DELETE = re.compile(r"^(\d+),(\d+)d$")
_SED_PATTERN_DELETE = re.compile(r"^/(.+)/d$")

def _hint_sed(words: list[str]) -> RouteHint | None:
    if len(words) < 3:
        return None
    args = words[1:]
    i = 0
    if args[i] == "-i":
        i += 1
        if i < len(args) and args[i] == "":
            i += 1
    elif args[i].startswith("-i"):
        i += 1
    else:
        return None
    if i >= len(args):
        return None
    script = args[i]; i += 1
    path = args[i] if i < len(args) else None; i += 1
    if i != len(args) or script is None or path is None:
        return None

    m = _SED_SIMPLE.match(script)
    if m:
        line_no, old_text, new_text = int(m.group(1)), m.group(2), m.group(3)
        if "&" not in new_text and r"\1" not in new_text:
            return RouteHint(
                tool_id="replace", ui_label="→ replace",
                llm_hint=f'Prefer replace(file_path="{path}", start_no={line_no}, end_no={line_no}, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}") — prefix/suffix are line content anchors for locating the edit, new_string is the replacement. Enables staleness checking and diff output.',
            )

    m = _SED_GLOBAL.match(script)
    if m:
        old_text, new_text = m.group(1), m.group(2)
        if "&" not in new_text and r"\1" not in new_text:
            return RouteHint(
                tool_id="replace", ui_label="→ replace",
                llm_hint=f'For global substitution: first read {path} to locate lines, then use replace(file_path, start_no, end_no, prefix="{old_text}", suffix="{old_text}", new_string="{new_text}") — prefix/suffix are line content anchors for locating the edit.',
            )

    m = _SED_RANGE_DELETE.match(script)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'Prefer replace(file_path="{path}", start_no={start}, end_no={end}, prefix="<line{start}>", suffix="<line{end}>", new_string="").',
        )

    m = _SED_PATTERN_DELETE.match(script)
    if m:
        pat = m.group(1)
        return RouteHint(
            tool_id="replace", ui_label="→ replace",
            llm_hint=f'For pattern-based deletion: first grep "{pat}" {path} to locate lines, then use replace(..., new_string="").',
        )

    return None
```
## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 提示检测解析失败（shlex 异常） | \`_shell_words\` 返回 \`[]\`，\`try_hint\` 返回 \`None\`，bash 照常执行 |
| 提示构造异常 | \`try_hint\` 内部捕获异常，返回 \`None\`，bash 照常执行 |

**核心原则**：提示是尽力而为的附加信息，绝不能因为提示逻辑的 bug 导致 bash 执行结果异常。提示检测在 bash 执行之后运行，即使提示逻辑崩溃，bash 的输出已经生成。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 纯提示策略，不自动路由执行 | 自动路由执行到专用工具 | 零风险：不改变执行路径，不引入复杂依赖，不因路由 bug 导致命令失败 |
| 提示在 bash 执行之后追加 | 在 bash 执行之前拦截 | 不阻断工作流，bash 照常执行，提示只是附加信息 |
| UI 标记用 \`[→ tool]\` 短格式 | 长文本追加到 output | 用户扫一眼就知道有更好的工具，不干扰 bash 输出阅读 |
| LLM 引导用 \`next_step_hint\` | 追加到 output 末尾 | \`next_step_hint\` 只进 ToolMessage 不进 UI，避免 UI 重复显示引导文本 |
| 不修改 bash description | 在 description 中加优先用专用工具 | description 修改是独立优化，提示是运行时兜底，两层互补 |
| 只提示简单命令 | 尝试解析管道和重定向 | 简单命令覆盖 90% 场景，复杂解析容易误提示 |
| \`grep -i\`/\`-w\`/\`-v\`/\`-A/-B/-C\` 不提示 | 提示但忽略 flag | 提示后 LLM 会用 grep 工具替代，但 grep 工具不支持这些 flag，导致语义丢失 |
| \`ls\` 不提示 | 提示用 glob 替代 | ls 输出纯文件名且显示隐藏文件，glob 输出相对路径且过滤隐藏目录内容，结果集和格式差异大 |
| echo/printf 双引号写入不提示 | 尝试解析双引号转义 | bash 双引号转义规则复杂，提示中无法准确表达语义 |
| \`&\` 检查用正则 \`r'&&\|\\s&$'\` | \`any(ch in stripped for ch in ("\&",))\` 或 \`r'\\b&&\\b\|\\s&\\s\|\\s&$'\` | \`\\b\` 在 \`&\` 旁不触发（非 word char），\`\\s&\\s\` 会误杀引号内的 \`&\`；只检查 \`&&\`（命令链）和行尾 \`&\`（后台运行） |
| sed 全局替换提示两步操作 | 不提示 | 全局替换是高频操作，提示引导 LLM 先 read 再 replace |
| \`echo >> file\` 提示用 insert | 不提示 | 追加写入是高频操作，insert(lineno=-1) 语义清晰 |
| 提示语言用英文 | 中文 | LLM 对英文工具调用引导理解更准确，与工具参数名一致 |

## Open Questions

- [ ] 是否需要对提示行为做可观测性（日志/metrics），方便后续调优提示规则？
- [x] heredoc 写入提示的解析复杂度是否可接受？是否需要限制 heredoc 长度？→ 已解决：限制 200 字符，超出不提示。
- [ ] \`rg -t\` 类型映射表是否需要更完整？是否应该从 ripgrep 的配置文件读取？
- [ ] 提示是否应该统计 LLM 后续是否采纳了建议（即下次是否改用专用工具），用于评估提示效果？

## Review Notes

> 评审日期：2026-06-19。本文档从路由执行策略重写为纯提示策略，以下记录重写中吸收的评审意见。

### 从路由执行策略中消除的问题

1. **\`_execute_routed\` 调用路径与 registry 不一致**：纯提示策略下不存在路由执行，此问题消除。
2. **\`ToolContext.tool_registry\` 注入**：纯提示策略不需要 \`tool_registry\`，无需修改 \`ToolContext\`。
3. **\`_tail_n\` offset 计算**：纯提示策略不执行路由，不需要计算 read 工具的 offset，此问题消除。
4. **路由执行失败回退**：纯提示策略不执行路由，不存在回退问题。

### 保留的设计决策

- **提示在 bash 执行之后**：与路由执行策略一致，保证 bash 照常运行。
- **快速排除管道和多命令**：与路由执行策略一致，简单命令覆盖 90% 场景。
- **\`grep -i\`/\`-w\`/\`-v\`/\`-A/-B/-C\` 不提示**：与路由执行策略一致，语义差异不可忽略。
- **\`ls\` 不提示**：与路由执行策略一致，输出格式差异大。
- **\`&\` 检查用正则**：与路由执行策略一致，避免误杀参数值中的 \`&\`。

### 2026-06-19 第二轮评审吸收的修改

1. **`try_hint` 加顶层 try/except**：评审指出伪代码缺少异常捕获，与 Error Handling 规格矛盾。拆分为 `try_hint`（捕获异常）+ `_try_hint_impl`（实际逻辑）。
2. **sed 提示 `prefix`/`suffix` 文案改进**：评审指出 `prefix`/`suffix` 是行内容锚点而非替换内容，原文案容易误导 LLM。在 llm_hint 中补充说明 "prefix/suffix are line content anchors for locating the edit"。
3. **echo/printf 写入提示中 content 含双引号时不提示**：评审指出 llm_hint 中 content 用双引号包裹，若 content 本身含双引号会产生转义歧义。新增 `if '"' in content: return None` 检查。
4. **`head -<digits>` 老式写法支持**：评审指出 `head -5 file` 这种写法被 `startswith("-")` 拦截。新增 `re.match(r"^-\d+$", args[i])` 分支。
5. **`tail -n <N>` 和 `tail <path>` 不提示**：评审指出 `tail -n N`（最后 N 行）无法精确映射到 read 的 offset+limit，`tail` 无参数语义模糊。从提示规则表格移除，`_hint_tail` 中返回 None。
6. **`RouteHint.tool_id` 改为 Literal 类型**：评审指出 `str` 太宽泛。改为 `Literal["read", "git", "write", "replace", "insert", "glob", "grep"]`。
7. **metadata `route_hint` 增加 `command` 字段**：评审建议增加原始命令，方便可观测性和效果评估。
8. **补充缺失的伪代码**：`_hint_tail`、`_hint_write_echo`、`_hint_write_heredoc`、各 `_hint_git_*` 子函数。评审指出这些函数在文档中被引用但未给出实现，边界情况无法审查。
9. **`git commit -m"msg"` 紧凑写法**：评审指出 `shlex.split('git commit -m"msg"')` → `['git', 'commit', '-mmsg']`，`-m` 和 msg 粘在一起。`_hint_git_commit` 新增 `elif a.startswith("-m"):` 分支处理。
10. **heredoc 追加模式提示用 insert**：评审指出 `cat >> path << 'EOF'` 应提示 insert 而非 write。`_hint_write_heredoc` 中 `is_append` 逻辑已覆盖。
11. **heredoc 内容长度限制**：评审指出长 heredoc 提示会污染 LLM 上下文。新增 `if len(content) > 200: return None`。
12. **`&` 排除正则文档补充**：评审指出 `\s&$` 匹配后台运行语法，应在文档中说明理由。
13. **fgrep 提示文案改进**：评审指出 fgrep 是固定字符串匹配，re.escape 后语义一致，但文案应说明是 literal match。
14. **`rg -t` 未知类型 fallback**：评审指出映射表不完整。确认 `_RG_TYPE_MAP.get(type_name)` 返回 None 时不提示，行为正确，补充注释说明。

### 2026-06-19 第三轮评审修复

1. **`_hint_write_echo` 引号内 `>` 错误分割**：原实现用 `stripped.split('>', 1)` 定位重定向操作符，不感知 shell 引号。当单引号内容含 `>` 时（如 `echo 'x > y' > file.txt`），分割位置错误，导致 path 和 content 都错。修复：改用 shlex 输出定位 `>` / `>>` token——shlex 正确区分重定向操作符和引号内的 `>`。
2. **sed 范围删除 hint 使用占位符 `<lineN>` 作为 prefix/suffix**：`<line10>` 不是真实行内容，LLM 照搬会导致 replace 失败。修复：改为两步引导 `For line range deletion: first read {path} to see lines {start}-{end}, then use replace(...)`。
3. **`git commit -m"msg"` 紧凑双引号形式绕过双引号检测**：原正则 `r'-m\s+"'` 只匹配有空格的 `-m "msg"`，不匹配紧凑 `-m"msg"`。修复：正则改为 `r'-m\s*"'`，统一处理两种形式。紧凑双引号形式现在也返回 None。
4. **`git log -5` 短格式未识别**：`git log -5` 是 `git log -n 5` 的常见简写，但被当作未知 flag 返回 None。修复：在 `_hint_git_log` 中增加 `-\d+` 模式匹配（类似 `head -5` 的处理）。