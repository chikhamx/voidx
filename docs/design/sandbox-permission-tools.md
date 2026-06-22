# 沙箱、权限与工具 — 架构梳理

## 整体架构

voidx 的工具调用安全由三层防线依次拦截：

```
工具调用请求
  │
  ▼
┌─────────────────────┐
│  1. 沙箱 (Sandbox)  │  文件系统边界检查
└─────────┬───────────┘
          │ 通过
          ▼
┌─────────────────────┐
│  2. 模式覆盖 (Mode) │  plan 模式写操作拦截
└─────────┬───────────┘
          │ 通过
          ▼
┌─────────────────────┐
│  3. 权限引擎 (Auth)  │  规则匹配 → 会话白名单 → 审批策略
└─────────┬───────────┘
          │
          ▼
    allow / deny / ask
```

核心入口：`authorize_tool_call()` (`permission/engine.py:24`)


## 第一层：沙箱 (Sandbox)

沙箱负责**文件系统边界**，确保工具不会在工作区之外写入。

### 三种沙箱模式

| 模式 | 值 | 行为 |
|------|-----|------|
| 只读 | `read-only` | 所有写操作（文件写入、bash 写命令、git 写操作、implement 委托）全部拒绝 |
| 工作区写入 | `workspace-write` | 只允许在 workspace + extra_paths 内写入 |
| 完全访问 | `danger-full-access` | 不做任何文件系统边界检查 |

### 沙箱检查逻辑 (`engine.py:55` `sandbox_denial_reason`)

1. `danger-full-access` → 直接放行
2. `read-only` → 按 `PermissionCapability` 拒绝写能力
3. `workspace-write` → 对文件工具检查路径，对 bash 检查写入目标


### 文件路径检查 (`sandbox.py:40` `check_sandbox_filepath`)

- 将路径 resolve 为绝对路径，检查是否在 `workspace` 或 `extra_paths` 下
- 无法 resolve 的路径不阻止（交给工具自身报错）

### Bash 命令写入目标检查 (`sandbox.py:94` `check_sandbox_bash`)

从 bash 命令中提取写入目标：
- **重定向**：`>`, `>>`, `| tee`, `dd of=`
- **文件系统命令**：`rm`, `cp`, `mv`, `ln`, `mkdir`, `touch`, `install`
- 每个提取出的目标路径都做 `_allowed()` 检查

> 注意：这是尽力而为的检查，设计目标是拦截"诚实的错误"而非对抗性攻击。硬性黑名单在 `bash.py` 的 `_BLOCKED` 中。


### Bash 硬性黑名单 (`bash.py:20` `_BLOCKED`)

无论权限如何，以下命令**始终被阻止**：

| 模式 | 原因 |
|------|------|
| `sudo` | 提权 |
| `chmod 7xx` | 世界可写权限 |
| `chown` / `chgrp` | 所有权变更 |
| `mkfs` | 文件系统格式化 |
| `dd of=/dev/` | 原始磁盘写入 |
| `reboot` / `shutdown` / `poweroff` | 系统关停 |
| `fork bomb :(){ ... }` | fork 炸弹 |
| `git push --force main/master` | 强制推送主分支 |
| `curl/wget | bash` | 远程脚本执行 |

这是工具执行层的最后一道硬防线，在权限引擎之外。


## 第二层：模式覆盖 (Mode Overlay)

当交互模式为 `plan` 时，所有写操作被拦截，无论沙箱和权限规则如何。

### 拦截的能力 (`engine.py:91` `mode_overlay_denial_reason`)

- `FILE_WRITE` / `FILE_FORMAT` — 文件编辑工具
- `BASH_WRITE` — 写入型 bash 命令
- `GIT_WRITE` — 写入型 git 操作
- `AGENT_IMPLEMENT` — 委托给 implement persona

这确保 plan 模式下 agent 只能观察，不能修改。


## 第三层：权限引擎 (Permission Engine)

通过沙箱和模式覆盖后，进入权限引擎的决策流程：

```
会话白名单 (session_allow/deny)
  │
  ▼
策略规则 (BASIC_RULES + evaluate)
  │
  ▼
审批策略 (approval_policy + approval_reviewer)
```

### 3a. 会话白名单 (`engine.py:106` `session_action_for_tool`)

用户在运行时通过 `/allow` `/deny` 命令动态添加的规则，优先级最高：
- `session_deny` 中匹配 → deny
- `session_allow` 中匹配 → allow
- 都不匹配 → 继续下一层


### 3b. 策略规则 (`engine.py:114` `strategy_action_for_tool`)

基于 `BASIC_RULES` (`rules.py:27`) 和 `evaluate()` (`evaluate.py:18`)：

**默认规则表：**

| 工具 | pattern | action |
|------|---------|--------|
| read, glob, grep, webfetch, websearch | `*` | allow |
| todo, clarify, checkpoint, workflow, compact | `*` | allow |
| task_status, skill, repo_map, lsp | `*` | allow |
| agent (voidx) | `voidx` | allow |
| edit (file/line/replace) | `*` | ask |
| git | `write` | ask |
| bash | `*` | ask |
| agent | `implement` | ask |
| mcp__* / mcp/* | `*` | ask |

**规则匹配算法** (`evaluate.py:18`)：从后向前查找最后一条匹配的规则（later overrides earlier），无匹配时默认 `ask`。

**特殊：accept-edits 模式**下，`FILE_WRITE` / `FILE_FORMAT` 能力直接 allow，跳过规则表。


### 3c. 审批策略 (`engine.py:126` `resolve_approval`)

当策略规则返回 `ask` 时，由审批策略决定最终行为：

| 策略 | 值 | 行为 |
|------|-----|------|
| 不信任 | `untrusted` | 所有 ask 都弹确认（默认） |
| 失败时 | `on-failure` | bash_write / git_write 仍 ask，其余自动 allow 并在失败时通知 |
| 请求时 | `on-request` | 全部自动 allow，仅 agent 主动请求时才 ask |
| 从不 | `never` | 全部自动 allow，无人工确认 |

**审批人** (`approval_reviewer`)：
- `user` — 弹给用户确认
- `auto_review` — 自动审核，仅 bash_write / git_write 仍 ask


## 工具能力分类 (PermissionCapability)

每个工具调用在进入权限引擎前，先被分类为一种能力 (`rules.py:353` `capability_for_tool`)：

| 能力 | 工具 | 说明 |
|------|------|------|
| `READ_TOOLS` | read, glob, grep, webfetch, websearch, todo, task_status, skill, workflow, compact, repo_map, lsp | 纯读取，始终安全 |
| `FILE_WRITE` | file, line, replace | 文件写入/编辑 |
| `FILE_FORMAT` | (格式化类工具) | 代码格式化 |
| `BASH_READ` | bash (安全命令) | 只读 shell 命令 |
| `BASH_WRITE` | bash (非安全命令) | 有副作用的 shell 命令 |
| `GIT_READ` | git (只读子命令) | status, log, diff 等 |
| `GIT_WRITE` | git (写入子命令) | add, commit, push 等 |
| `AGENT_READONLY` | agent (非 implement) | 只读型子 agent |
| `AGENT_IMPLEMENT` | agent (implement) | 实现型子 agent |
| `MCP_TOOLS` | mcp__* / mcp/* | MCP 外部工具 |
| `OTHER` | 其他 | 未分类工具 |

### Bash 安全判定 (`rules.py:147` `is_safe_bash`)

bash 命令被分类为 `BASH_READ` 还是 `BASH_WRITE` 取决于：
- 含 `$()` 或反引号 → 不安全
- 含写重定向 (`>`, `>>`) → 不安全
- 每个管道段检查程序是否为已知只读命令（ls, cat, grep, git status 等）
- git 子命令有详细的只读/写入分类


## 权限模式预设 (PermissionMode)

用户通过预设模式一次性设定沙箱 + 审批策略组合 (`config/enums.py:53`)：

| 预设 | 沙箱 | 审批策略 | 审批人 | 说明 |
|------|------|---------|--------|------|
| `default` | workspace-write | untrusted | user | 默认安全模式 |
| `read-only` | read-only | untrusted | user | 只读观察 |
| `accept-edits` | workspace-write | untrusted | user | 自动接受文件编辑 |
| `auto-review` | workspace-write | on-failure | auto_review | 自动审核，失败通知 |
| `full-access` | danger-full-access | never | user | 完全自动，无限制 |
| `custom` | 自定义 | 自定义 | 自定义 | 手动配置各参数 |

可通过 `/permission-mode` 命令切换，或启动参数指定。


## 工具执行时的权限集成

### 调用链

```
LLM 输出 tool_calls
  │
  ▼
GraphPermissionMixin._authorize_tool_calls()    ← 权限入口
  │
  ├─ classify_tool_call()        → ClassifiedToolCall (name, args, pattern, capability)
  ├─ _workflow_gate_requires_approval()  → 工作流门控检查
  ├─ authorize_tool_call()       → PermissionDecision (三层防线)
  │
  ├─ action=allow → approved 列表
  ├─ action=deny  → denied 列表
  └─ action=ask   → need_ask → _ask_and_apply_permission()
                      │
                      ├─ "a" (always) → session_allow + approved
                      ├─ "y" (yes)    → approved
                      └─ "n" (no)     → denied
```

### 工作流门控 (`permissions.py:152`)

活跃工作流可以定义 `denied_tools` 门控，即使权限引擎返回 allow，仍需用户确认。
例如 `debug` 节点拒绝 `file`, `line`, `replace` 等写入工具。

### Persona 检查 (`permissions.py:177`)

当 runtime persona 不是 `implement` 时，`FILE_WRITE` / `FILE_FORMAT` 能力即使被 allow 也要 ask。
这防止非 implement persona 在未经确认时写入文件。


## 工具自身的沙箱检查

除了权限引擎的预检查，部分工具在执行时还会做二次沙箱验证：

### Bash 工具 (`bash.py:60` `_sandbox_denial`)

在 `execute()` 内部再次检查 `ctx.sandbox_mode`：
- `danger-full-access` → 跳过
- `read-only` → 只允许 `is_safe_bash_command()` 返回 true 的命令
- `workspace-write` → 调用 `bash_sandbox_denial()` 检查写入目标路径

### 文件工具

`ToolContext` 携带 `sandbox_mode` 和 `sandbox_extra_paths`，文件工具在执行时可通过 ctx 获取沙箱信息做路径校验。

> 这形成了**双重检查**：权限引擎在调用前拦截，工具自身在执行时再验证。


## 关键文件索引

| 文件 | 职责 |
|------|------|
| `permission/engine.py` | 权限决策主流程：沙箱 → 模式覆盖 → 会话白名单 → 策略规则 → 审批策略 |
| `permission/sandbox.py` | 文件路径和 bash 命令的沙箱边界检查 |
| `permission/rules.py` | 工具能力分类、BASIC_RULES、bash 安全判定、git 只读判定 |
| `permission/evaluate.py` | 规则匹配算法（findLast）、配置解析、规则合并 |
| `permission/schema.py` | Rule / Ruleset / Action 类型定义 |
| `permission/context.py` | PermissionContext / PermissionDecision 数据模型 |
| `permission/service.py` | PermissionService：用户交互、会话白名单管理、预设模式 |
| `agent/graph/permissions.py` | GraphPermissionMixin：工具调用的权限 UI 适配层 |
| `config/enums.py` | SandboxMode / ApprovalPolicy / ApprovalReviewer / PermissionMode 枚举 |
| `tools/bash.py` | Bash 工具：`_BLOCKED` 硬性黑名单 + `_sandbox_denial` 二次检查 |
| `tools/base.py` | ToolContext：携带 sandbox_mode / sandbox_extra_paths |