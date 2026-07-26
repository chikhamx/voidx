# 语义化搜索工具与 Shell 路由设计

> 以 LLM 的检索意图为中心，将 `glob`/`grep` 替换为 `find`/`search`，并使 Bash、PowerShell 的简单搜索命令在语义明确时路由到专用工具。

## 1. 背景

现有工具的能力已覆盖文件枚举、内容搜索、`.gitignore`、二进制跳过和结构化结果，但接口暴露了底层实现决策：

- `glob` 要求调用者构造 glob，并判断 `**`、大小写与目录深度。
- `grep` 默认 Python 正则，并要求调用者组合 `include`、`exclude`、`whole_word` 和扫描预算。
- 搜索结果分别使用 `files` 与扁平 `results`，两个工具的结果层级不一致。
- Bash 路由可解析部分 `grep`/`rg`/`find` 选项；PowerShell 路由仅产生泛化提示，未完整传递 `Select-String` 与 `Get-ChildItem` 的语义。

LLM 的主要意图只有两类：按文件名找文件，或按内容找位置。新接口应以这两类意图表达，默认安全、结果可直接定位，并将资源约束收敛到工具内部。

## 2. 目标与非目标

### 目标

1. 用 `find` 表达文件发现，用 `search` 表达文件内容搜索。
2. 将常见过滤收敛为 `path`、`extensions` 和结果预算，不要求 LLM 编写 glob 或正则。
3. 让 `search` 默认进行字面量、智能大小写匹配；仅在明确要求时启用正则或全词匹配。
4. 为两个工具提供一致、结构化、可截断的 JSON 输出。
5. 对 Bash/PowerShell 搜索命令做保守路由：能映射就自动执行，无法精确映射则保留 Shell 执行或仅提示。
6. 不增加旧工具兼容层或迁移层；本次变更直接替换内部工具定义与调用。

### 非目标

- 不做历史版本兼容或旧调用迁移。
- 不实现模糊搜索、全文索引、语义向量搜索或跨文件 AST 查询。
- 不替换当前 Python 文件遍历实现为 `rg` 子进程。
- 不让路由器解释管道、变量、命令替换、通配符展开或多阶段 Shell 脚本。
- 不承诺与 GNU grep、ripgrep 或 PowerShell 所有选项完全兼容。

## 3. 总体设计

`src/voidx/tools/search.py` 保持为搜索实现模块，但导出 `FindTool` 与 `SearchTool`。两者共享安全的文件枚举器：限制在可读沙箱路径内，跳过 `SKIP_DIRS`、隐藏目录内容、`.gitignore` 命中项、符号链接和二进制文件。

- `find` 只对文件名做子串匹配；目录范围由 `path` 表达，不把路径片段混进查询语义。
- `search` 在共享枚举器的文本文件上搜索；其匹配模式和大小写策略由显式枚举字段表示。
- 每次调用都使用固定的内部扫描预算与输出预算。发生截断时返回已收集内容及 `truncated: true`，而非将预算作为 LLM 的常规决策参数。
- Shell 路由与直接工具调用使用同一份参数模型；路由只负责把已证明等价的命令翻译成工具参数。

## 4. 工具契约

### 4.1 `find`

用途：在工作区或指定范围内查找文件。

输入：

```json
{
  "query": "search",
  "path": "src/voidx/tools",
  "extensions": ["py"],
  "case": "auto",
  "max_results": 50
}
```

| 字段 | 必填 | 默认 | 语义 |
|---|---:|---|---|
| `query` | 否 | 无 | 文件名片段，不包含目录分隔符。与 `extensions` 至少提供一个。 |
| `path` | 否 | 工作区根目录 | 文件或目录范围，遵守工具路径沙箱规则。 |
| `extensions` | 否 | 无过滤 | 扩展名列表，如 `["py", "pyi"]`；接受带或不带前导 `.` 的值，按不区分大小写比较。 |
| `case` | 否 | `"auto"` | `"auto"`：查询不含大写字母时忽略大小写，否则区分大小写；也可为 `"sensitive"` 或 `"insensitive"`。仅作用于 `query`。 |
| `max_results` | 否 | `50` | 返回的最大文件数，范围 `1..200`。 |

输出：

```json
{
  "query": "search",
  "files": [
    {"path": "src/voidx/tools/search.py", "name": "search.py"}
  ],
  "truncated": false
}
```

排序规则依次为：文件名完全相等、文件名以查询开头、文件名包含查询；同一等级按相对路径字典序排序。未提供 `query` 时直接按相对路径字典序排序。`truncated` 表示仍有符合条件的结果未返回。

### 4.2 `search`

用途：在文本文件内容中查找字符串或明确指定的正则表达式。

输入：

```json
{
  "query": "record_read_range",
  "path": "src",
  "extensions": ["py"],
  "match": "text",
  "case": "auto",
  "context": 2,
  "max_results": 30
}
```

| 字段 | 必填 | 默认 | 语义 |
|---|---:|---|---|
| `query` | 是 | — | 待搜索文本或正则；不得为空。 |
| `path` | 否 | 工作区根目录 | 文件或目录范围，遵守工具路径沙箱规则。 |
| `extensions` | 否 | 无过滤 | 扩展名列表，语义与 `find` 一致。 |
| `match` | 否 | `"text"` | `"text"` 为字面量匹配；`"word"` 为字面量完整词匹配；`"regex"` 为 Python 正则匹配。 |
| `case` | 否 | `"auto"` | `"auto"`：查询不含大写字母时忽略大小写，否则区分大小写；也可为 `"sensitive"` 或 `"insensitive"`。 |
| `context` | 否 | `0` | 每一命中前后的上下文行数，范围 `0..10`。重叠上下文在同一文件中合并。 |
| `max_results` | 否 | `30` | 返回的最大匹配数，范围 `1..100`。按稳定文件遍历顺序和行号截断。 |

输出按文件分组：

```json
{
  "query": "record_read_range",
  "match": "text",
  "case": "auto",
  "matches": [
    {
      "path": "src/voidx/tools/search.py",
      "hits": [
        {
          "line": 269,
          "column": 33,
          "text": "                        record_read_range(ctx, f, line_no, line_no)",
          "before": [],
          "after": []
        }
      ]
    }
  ],
  "truncated": false
}
```

`line` 与 `column` 均从 1 开始；`column` 指向行内首个匹配开始位置。`text` 保留原始行内容（含缩进），超过既定显示上限时截断。`before`、`after` 为 `{ "line": number, "text": string }` 数组。同一行多个匹配只记为一个 hit；正则无效时返回工具参数错误，不降级为字面量搜索。

### 4.3 资源与过滤不变量

- 两个工具都返回工作区相对 POSIX 路径；当显式范围位于允许的工作区外时，按既有沙箱规则处理。
- `find` 与 `search` 的可见文件集合必须一致；例外仅为 `search` 额外跳过二进制文件。
- `.gitignore` 规则、跳过目录和符号链接策略只在共享枚举器中维护。
- 文件遍历必须按相对路径稳定排序后再处理，保证截断结果可复现。
- 内部扫描、单文件大小和输出行预算应是实现常量或配置，不出现在工具 schema。预算触发后，保留有效 JSON 并将 `truncated` 置为 `true`。
- `max_results` 限制返回结果而不表示扫描上限；若扫描预算先触发，结果同样标记截断。
- 上下文行与命中行可能重叠；输出时同一文件内每个行号最多出现一次，优先作为命中行。

## 5. Shell 路由

当前运行时中，携带 `tool_args` 的 `RouteHint` 会通过工具注册表直接执行目标工具；没有 `tool_args` 的提示只会阻断命令并返回建议。因此本设计将路由分为两类：

- **自动执行**：命令语义可完整映射到新工具参数，生成 `tool_args`。
- **仅提示或不处理**：命令语义存在差异、包含复杂选项，或只适合建议改用专用工具。

### 5.1 Bash

Bash 入口 `src/voidx/tools/bash/router.py` 继续分派 `find` 至文件提示模块，`grep`、`egrep`、`fgrep`、`rg` 至搜索提示模块。提示的工具 ID 与展示标签更新为 `find` 或 `search`。

| 命令形式 | 路由 | 参数映射 |
|---|---|---|
| `find [PATH] -type f -name PATTERN` | `find` | 自动执行；`case="sensitive"`。 |
| `find [PATH] -type f -iname PATTERN` | `find` | 自动执行；`case="insensitive"`。 |
| `grep PATTERN PATH`、`egrep PATTERN PATH`、`rg PATTERN [PATH]` | `search` | 自动执行；`match="regex"`，`case="sensitive"`。 |
| `grep -F PATTERN PATH`、`fgrep PATTERN PATH`、`rg -F PATTERN [PATH]` | `search` | 自动执行；`match="text"`，`case="sensitive"`。 |
| `-w` / `--word-regexp` | `search` | 自动执行；`match="word"`。 |
| `-i` / `--ignore-case` | `search` | 自动执行；`case="insensitive"`。 |
| `-s` / `--case-sensitive`（rg） | `search` | 自动执行；`case="sensitive"`。 |
| `-S` / `--smart-case`（rg） | `search` | 自动执行；`case="auto"`。仅 `rg` 允许映射 `auto`，`grep`/`egrep`/`fgrep` 无智能大小写语义，永不映射 `auto`。 |
| `-C N`，或相等的 `-A N -B N` | `search` | 自动执行；`context=N`。 |
| `rg -t TYPE` | `search` | 自动执行；映射已知类型到 `extensions`。 |

`find -name/-iname` 的 PATTERN 只允许以下可精确映射形式：

- `literal`：不自动执行。`find -name literal`（无通配符）在 GNU find 中为文件名精确匹配，而 `find.query` 是子串匹配（"完全相等"仅为排序优先级，见 4.1），二者语义不等价。
- `*literal*`、`literal*`、`*literal`：映射到 `query="literal"`。
- `*.ext`、`*literal.ext`：映射到 `query` 与 `extensions`；例如 `find . -type f -name '*.py'` → `{"path": ".", "extensions": ["py"], "case": "sensitive"}`。

其他 glob 元字符、目录分隔符、多路径和 `find -maxdepth` 不自动执行。

正则路由采用方言白名单：

- `grep` 使用 BRE 可兼容子集。
- `egrep` 使用 ERE 可兼容子集。
- `rg` 使用 Rust regex 可兼容子集。
- 兼容性不能确认时不自动执行，保留 Bash。

### 5.2 PowerShell

PowerShell 入口 `src/voidx/tools/powershell/router.py` 继续将 `Select-String`/`sls` 和 `Get-ChildItem`/`dir`/`ls`/`gci` 分派到搜索提示模块。别名先解析为 cmdlet，再使用相同规则。

| 命令形式 | 路由 | 参数映射 |
|---|---|---|
| `Get-ChildItem PATH -File -Recurse -Filter PATTERN` | `find` | 自动执行；归约 `query`/`extensions`，`case` 固定为 `"sensitive"`（不依据 Filter 内容推断大小写，PowerShell `-Filter` 匹配语义无法由 `auto` 精确表达）。 |
| `Select-String PATTERN PATH` | `search` | 自动执行；`match="regex"`，`case="insensitive"`。 |
| `Select-String -SimpleMatch PATTERN ...` | `search` | 自动执行；`match="text"`，`case="insensitive"`。 |
| `Select-String -CaseSensitive PATTERN ...` | `search` | 自动执行；`case="sensitive"`。 |
| `Select-String -Context N PATTERN ...` | `search` | 自动执行；`context=N`。 |

PowerShell 路由还需遵守：

- `Select-String` 的 PATH 仅支持单个明确文件路径，或明确的单目录递归搜索；`*.py` 这类仅当前目录 glob 不自动执行，避免递归语义偏差。
- `-Context N,M` 不对称时不自动执行。
- `-Include`、`-Exclude`、复杂 `-Path` 数组、`-LiteralPath`、输入对象或管道输入不自动执行。
- 没有 `-Recurse` 的 `Get-ChildItem` 不自动执行。

## 6. 变更范围

本次不做迁移层；以下为直接替换与更新点。

| 路径 | 改动职责 |
|---|---|
| `src/voidx/tools/search.py` | 实现 `FindInput`/`FindTool`、`SearchInput`/`SearchTool`、共享枚举与统一 JSON 输出。 |
| `src/voidx/tools/registry.py` | 注册新工具，移除 `GlobTool`、`GrepTool`。 |
| `src/voidx/tools/bash/hint/file.py` | 重写 `_hint_find`：移除 glob 构造与 `-maxdepth` 支持，按 PATTERN 白名单归约 `query`/`extensions`，不支持的命令返回 `None`。 |
| `src/voidx/tools/bash/hint/search.py` | 重写 `_hint_grep`：输出 `tool_id="search"` 与新字段（`query`/`match`/`case`/`context`/`extensions`）；`fgrep`/`-F` 改为 `match="text"`（不再 `re.escape`）；`-w` 改为 `match="word"`；`grep`/`egrep`/`fgrep` 永不映射 `case="auto"`。 |
| `src/voidx/tools/powershell/hint/search.py` | 新增参数解析逻辑（现有仅生成无 `tool_args` 提示）：`Select-String`/`Get-ChildItem` 按白名单生成 `tool_args` 并自动执行，不支持的命令返回 `None`。 |
| `src/voidx/tools/shell/common.py` | 更新 `_HintableTool` Literal：移除 `"glob"`/`"grep"`，加入 `"find"`/`"search"`。`bash/core.py` 的 `RouteHint` 同步更新。 |
| `src/voidx/ui/output/display_policy.py`、`tool_display.py`、`events/consumers.py`、`dock/nodes.py` | 更新工具展示策略、标签与进行中状态。 |
| `src/voidx/permission/rules.py`、`service.py`、`runtime_guards.py`、`chat_policy.py`、`prompt_policy.py` | 更新工具分类、权限规则、提示词和自动路由策略。 |
| `frontend/src/utils/render.ts` 与相关前端测试 | 更新工具名称展示。 |
| `src/voidx/agent/prompts.py`、`agent/slash/handler.py`、`agent/domain/chat_policy.py`、`agent/domain/prompt_policy.py` | 更新工具 ID 字面量与 `requires` 集合（如 `prompts.py:237` 的 `requires={"read","glob","grep"}`）。 |
| `src/voidx/permission/shell_policy.py`、`permission/rules.py`、`permission/service.py` | 更新允许命令集与工具分类（`shell_policy.py:267`、`rules.py:294,397`、`service.py:387`）。 |
| `src/voidx/ui/output/console/formatting.py`、`ui/output/console/app.py` | 更新工具名称展示（`formatting.py:88,91`、`app.py:44`）。 |
| `src/tests/test_tools/test_search.py` 及路由测试 | 用新契约替换旧测试，补齐自动执行和回退边界。 |

同时必须搜索并更新所有对 `glob`、`grep`、`GlobInput`、`GrepInput`、`GlobTool`、`GrepTool` 的生产代码、提示词、快照及协议 schema 引用。旧工具 ID 不注册、不别名、不保留旧参数。

## 7. 测试与验收

实现按测试先行进行，至少覆盖：

1. `find`：文件名子串查询、纯 `extensions` 查询、路径范围、大小写策略、相关性排序、跳过目录、`.gitignore`、符号链接和截断。
2. `search`：默认字面量（包含正则元字符）、`word`、`regex`、三种大小写策略、扩展名、上下文合并、行列定位、无效正则、二进制跳过和截断。
3. 注册与 schema：新工具存在，旧工具不存在；schema 不含旧参数。
4. Bash：每条允许映射生成准确的 `tool_id`/`tool_args`；无法精确映射的命令不自动执行。
5. PowerShell：别名与 cmdlet 的允许映射生成准确参数；对象/管道/复杂路径和过滤组合不自动执行。
6. UI、权限和协议相关测试：新 ID 显示为搜索操作，且只读权限分类正确。

建议验证命令：

```bash
./test.py --backend -- src/tests/test_tools/test_search.py
./test.py --backend -- src/tests/test_tools/bash/
./test.py --backend -- src/tests/test_tools/test_powershell_tool.py
./test.py --backend -- src/tests/test_tools/test_powershell_tool_phase6.py
./test.py --backend -- src/tests/test_ui/
./test.py --backend
```

合并前还应运行 `./python.py scripts/export_ui_protocol_schema.py`（若 schema 受注册工具影响）并检查生成变更。

## 8. 风险与决策

- **工具名称替换风险：** 不做迁移层意味着所有内置调用和前端协议消费者必须同一变更集更新。通过全仓库引用搜索和注册测试防止遗漏。
- **语义差异风险：** Shell 正则、glob、递归和过滤语义无法完全由新接口表达。因此路由采取保守策略：不能证明等价就不自动执行。
- **`case: auto` 的适用范围：** `auto` 保留为工具默认值，符合 LLM 搜索习惯；Shell 路由必须显式传递 `sensitive` 或 `insensitive`，避免默认语义偏差。唯一例外是 `rg --smart-case`，其智能大小写语义与 `auto` 等价，可映射为 `case="auto"`；`grep`/`egrep`/`fgrep` 无智能大小写语义，永不映射 `auto`。
- **默认字面量的行为变化：** 这是有意设计。LLM 直接调用 `search` 时，代码文本中的 `.`、`[]`、`?` 不再意外作为正则解释；需要正则时显式设置 `match="regex"`。
- **结果预算风险：** 内部预算可能隐藏未扫描部分，故所有预算截断均必须可见地设置 `truncated: true`；LLM 可据此缩小 `path` 或 `extensions` 重试。
- **PowerShell 行为变化：** 对可精确映射的命令，路由可能从“仅提示”变为“自动执行”；相关测试需显式覆盖该决策。

## 9. 契约补充与精确映射规则

本节是对前述契约的约束性补充；实现与测试应以本节为准。

### 9.1 输入字段与预算

- `find.query` 与 `find.extensions` 至少提供一个；二者都省略时返回参数错误，不执行“列出全部文件”。
- `find.query` 只接受不含 `/` 或 `\\` 的文件名片段，不接受 glob 元字符。Shell 的 `-name`/`-iname` 只有在可证明等价时才可转换为片段查询。
- `search` 的字段映射固定为 `query`、`extensions`、`match`、`case`、`context`、`max_results`；旧字段 `pattern`、`include`、`ignore_case`、`whole_word`、`context_lines`、`max_matches`、`max_scanned` 不进入新工具 schema。
- `max_results` 只限制返回的命中数（`find` 为文件数），不限制扫描量；内部扫描预算、单文件大小和输出行预算由实现常量或配置控制。任一预算触发都必须设置 `truncated: true`，且不得在 JSON 结果中加入伪造的命中。
- `search` 的上下文行不消耗 `max_results`；同一文件同一行最多输出一次，命中行优先于上下文行。无命中时返回与有命中时相同的顶层字段，并使用空的 `matches`。
- `ToolResult.output` 是唯一面向 LLM 的 JSON 契约；`display` 可为人类可读文本，`metadata.match_details` 必须与输出中的命中保持一致，不能继续使用旧的 `results`/`content` 字段作为内部兼容格式。`metadata.match_details` 的字段名与 `output.matches` 一致：`path`/`line`/`column`/`text`/`before`/`after`，不使用旧实现的 `file`/`content`。

### 9.2 共享枚举器与路径规则

- 共享枚举器接收已通过 `_resolve_tool_path` 和当前沙箱权限检查的范围，并返回按工作区相对 POSIX 路径排序的文件项；不得在 `find` 和 `search` 中各自递归实现。
- 枚举器统一跳过 `SKIP_DIRS`、隐藏目录内容、符号链接和 `.gitignore` 命中项；`search` 在此基础上跳过二进制文件。显式传入单个文件时仍须应用符号链接、`.gitignore`、二进制和沙箱规则。
- `.gitignore` 始终以工作区根目录为规则基准，显式子目录范围不得改变匹配语义；遍历子目录也必须使用相对工作区根的路径进行匹配。
- `find` 与 `search` 的文件顺序必须来自同一个稳定排序枚举器。`find` 的相关性排序只在枚举完成后对候选文件应用，相关性相同则按相对路径字典序排序。
- 所有错误（路径不存在、越界、无效正则、参数范围错误）均返回结构化工具参数/执行错误，不回退到旧工具字段或文本格式。

### 9.3 Bash 精确映射与回退

路由器只有在以下条件全部满足时才生成 `tool_args` 并自动执行；否则保留 Bash 执行（若已有专用工具提示机制，则仅生成无 `tool_args` 的提示）：

- 命令是单一命令，没有管道、重定向、变量、命令替换、未引用通配符、多路径或其他 Shell 展开。
- `find [PATH] -type f -name/-iname PATTERN` 的 `PATTERN` 仅允许 `*literal*`、`literal*`、`*literal`、`*.ext`、`*literal.ext`。其中 `*.ext` 和 `*literal.ext` 映射为 `extensions` 加可选 `query`。无通配符的 `literal` 不自动执行（GNU find 精确匹配与新 `find.query` 子串匹配语义不等价，见 5.1）。
- `find -maxdepth` 不映射到新工具；新 `find` 没有深度字段，因此含该选项的命令必须回退。不得用工作区深度加法模拟旧的递归深度语义。
- `grep`/`egrep`/`rg` 的模式映射为 `search.query`，`-F`/`fgrep` 映射为 `match="text"`，`-w` 映射为 `match="word"`，其他允许的正则命令映射为 `match="regex"`。大小写映射为 `case="sensitive"` 或 `"insensitive"`；唯一例外是 `rg --smart-case` 映射为 `case="auto"`，`grep`/`egrep`/`fgrep` 无智能大小写语义，永不映射 `auto`。
- `--include`、`rg -t TYPE` 映射为 `extensions` 时，只接受能唯一归约为扩展名列表的值；任意 glob、`--exclude`、多文件路径、递归深度和不对称上下文均不得自动执行，除非新 schema 增加了等价字段。
- `-C N` 或同时提供相同 N 的 `-A N -B N` 才映射为 `context=N`。`-A`/`-B` 单独存在或数值不相等时回退。
- 正则路由必须先通过命令方言白名单校验：BRE、ERE、Rust regex 各自只允许实现明确列出的语法；无法证明 Python `re` 等价时回退，不得把不兼容模式静默改写或降级成字面量。

### 9.4 PowerShell 精确映射与回退

- 别名必须先解析为 cmdlet；`Select-String`/`sls` 和 `Get-ChildItem`/`dir`/`ls`/`gci` 仅在参数解析完成且没有管道、对象输入、变量、数组或复杂路径时考虑自动执行。
- `Select-String` 自动映射要求 PATH 是单个明确文件，或单个明确目录并显式带 `-Path`/`-Recurse` 的递归范围；`*.py` 当前目录 glob、`-Include`、`-Exclude`、`-LiteralPath`、多个 PATH 和管道输入一律不自动执行。
- `Select-String` 默认映射为 `match="regex"`；`-SimpleMatch` 映射为 `match="text"`；`-CaseSensitive` 映射为 `case="sensitive"`。未指定大小写时不得凭模式内容推断，必须采用方案定义的 PowerShell 默认语义并在映射表中固定；若该语义无法由新字段精确表达则回退。
- `-Context N,N` 只有前后数值相等时映射为 `context=N`；不对称值回退。`-Pattern`、`-Path` 的命名和位置参数必须归一化后再校验，不能只取第一个非选项词。
- `Get-ChildItem` 只有 `-File -Recurse` 且路径为单个明确文件/目录时才可映射为 `find`。`-Filter` 只允许可精确归约为 `query`/`extensions` 的文件名模式；不能依据模式是否含大写字母推断大小写，无法表达 PowerShell 匹配语义时回退。未带 `-Recurse` 的命令不自动执行。
- 自动路由失败时必须保留原 PowerShell 命令执行；仅提示不得伪装成已执行专用工具。

### 9.5 验收补充

除第 7 节测试外，必须增加以下反例和契约测试：

1. `find` 缺少 `query`/`extensions`、包含路径分隔符或 glob 元字符时失败。
2. `search` 默认把 `.`, `[]`, `?` 等按字面量处理；`match="regex"` 的无效表达式返回参数错误。
3. 同一工作区根规则下，从根目录和显式子目录搜索得到一致的 `.gitignore` 过滤结果；输出路径始终为工作区相对 POSIX 路径。
4. Bash 的 `find -maxdepth`、不支持的 glob、BRE/ERE/Rust 不兼容模式、`-A/-B` 不对称上下文均不自动执行。
5. PowerShell 的 `Select-String *.py`、管道输入、多个路径、`-Context 1,2`、`Get-ChildItem` 无 `-Recurse` 均不自动执行；精确命令生成完整 `tool_args`。
6. 注册表只包含 `find`/`search`，所有 UI、权限、协议 schema、提示词和测试中的旧 ID/字段引用均已删除；运行全仓库引用搜索时不得出现生产代码中的旧工具契约。
7. `search` 显式传入单个文件时，仍须应用 `.gitignore`、二进制和符号链接跳过规则（现有实现 `search.py:235-238` 当 `is_file()` 时直接 `files=[search_dir]` 绕过检查，新实现必须修正）。
8. `metadata.match_details` 的字段名为 `path`/`line`/`column`/`text`/`before`/`after`，不出现旧实现的 `file`/`content`。
9. `permission/shell_policy.py` 的 `_bounded_find_policy`/`_bounded_search_policy` 允许命令集与本节路由白名单一致；回退保留 Bash 执行时不得被权限策略误判为禁止。
