# Ask-First Permission Model — 技术设计文档

> **Status: Done** — Archived on 2026-07-14.
> Date: 2026-07-13

## Context

voidx 当前权限系统同时暴露了多组概念：permission mode、sandbox mode、approval policy、session allow/deny、path grants、shell policy、tool 层安全检查等。它们各自有合理出发点，但组合后形成了两个问题：

1. **用户心智负担偏重**：用户需要理解“读写沙箱”和“审批策略”之间的差别，才能预测某个 tool call 会自动执行、弹窗还是失败。
2. **审批语义不一致**：权限引擎会把部分风险判为 `ask`，但 tool 执行层又会重新执行 policy precheck，并把同一个风险阻断。例如复杂 Bash 命令先通过审批，随后在 BashTool 内部因 `shell policy deferred` 被标记失败。

期望模型是：除明显灾难性命令外，voidx 不替用户做最终否决；系统负责展示风险等级和原因，用户决定是否允许。本设计将权限系统重构为“预设模式 + 风险分级 + ask-first 审批”的模型。

## Goals

- 降低用户可见权限模型数量和解释成本。
- 将默认行为调整为 ask-first：除灾难级命令外，不直接拒绝高风险操作。
- Read Only 模式也通过无状态审批处理风险操作，而不是直接拒绝。
- 对所有被阻断的灾难级操作也展示给用户，但只提供拒绝/不运行选项。
- 统一权限层和工具执行层语义，避免“用户已批准但工具层再次阻断”。
- 保留安全底线：灾难级命令不可通过普通审批执行。
- 提供清晰、可测试的风险分级与默认模式矩阵。

## Non-Goals

- 不在本设计中实现完整 UI 改版；仅定义需要的交互语义和数据字段。
- 不移除所有底层安全检查；tool 层仍可保留 hard block 防线。
- 不把所有高级配置暴露给普通用户。
- 不要求一次性重写所有工具权限逻辑；可先从 Bash、PowerShell、file、git 迁移。

## User Model

用户只需要理解一句话：

```text
权限模式决定默认信任范围；弹窗只在超出当前信任范围时出现。
```

主界面不再要求用户理解 sandbox / approval_policy / reviewer / defer / grant 等内部概念。高级配置可以保留在开发者设置或配置文件中，但常规使用只暴露少数预设模式。

## Permission Presets

### Read Only

用于查看陌生项目、代码审查、只读探索。

| 行为 | 默认处理 |
| --- | --- |
| 本地文件读取、搜索、列目录、只读 git | Allow |
| 写入、复杂 shell、外部路径、联网、安装依赖、git 写操作 | Ask once |
| 灾难级命令 | Blocked acknowledgement |

Read Only 的审批是**无状态**的：只有 `Yes once` / `No`，不提供 session、workspace 或 global 记忆选项。

### Safe

默认模式。适合大多数日常使用。

| 行为 | 默认处理 |
| --- | --- |
| 本地文件读取、搜索、列目录、只读 git | Allow |
| workspace 内文件修改、Bash、PowerShell、依赖安装、联网、外部路径、git 写操作 | Ask |
| 极度危险操作 | Ask once only |
| 灾难级命令 | Blocked acknowledgement |

Safe 模式可提供 `Yes once`、`Allow for session`、`No`。极度危险操作不提供持久保存选项。

### Project Trusted

用于用户信任当前项目并希望高频开发更流畅。

| 行为 | 默认处理 |
| --- | --- |
| 读操作、workspace 内文件编辑、测试、build、formatter、只读 git | Allow |
| 复杂 shell、外部路径、联网、安装依赖、git push、大量删除 | Ask |
| 极度危险操作 | Ask once only |
| 灾难级命令 | Blocked acknowledgement |

Project Trusted 可提供 `Yes once`、`Allow for session`、`Allow for project`、`No`。

### Full Access

用于用户明确希望减少中断的场景。

| 行为 | 默认处理 |
| --- | --- |
| 大多数 workspace 内操作、测试、build、常规 shell、常规 git | Allow |
| 外部路径写入、系统目录、联网执行脚本、git push、极度危险操作 | Ask / Ask once only |
| 灾难级命令 | Blocked acknowledgement |

Full Access 不等于无底线执行；灾难级命令仍不可批准。

## Risk Levels

权限系统先识别风险，再由当前 preset 决定 allow / ask / blocked acknowledgement。

### Normal

低风险操作，通常自动执行。

- 本地只读文件访问
- 搜索和列目录
- 只读 git，如 `status`、`diff`、`log`
- 无副作用的状态查询

### Dangerous

可能修改项目、访问外部资源或执行动态逻辑。默认进入审批。

- workspace 内文件写入
- Bash / PowerShell 命令
- 外部路径读取或写入
- 依赖安装
- 网络访问
- git commit、branch、stash 等本地写操作

### Extremely Dangerous

可能造成大范围副作用、难以静态分析、跨出项目边界或影响远端状态。仍可审批执行，但默认只允许本次，不允许持久保存。

- 动态 shell：`$()`、反引号、复杂管道、重定向、`&&`、多行脚本
- 解释器执行：`python`、`python3`、`node`、`ruby`、`perl`、`bash -c`
- `curl | bash` / `wget | sh` 等联网执行脚本
- git push
- 大范围删除 workspace 内容
- 修改权限或 owner 的非系统命令
- 写入 workspace 外路径

### Blocked

不可通过审批执行，但仍需要向用户展示。UI 只提供 `Do not run` / `Deny`。

- `rm -rf /`
- `rm -rf /home`、`rm -rf ~`、`rm -rf $HOME`
- `mkfs`
- fork bomb
- `dd` 写裸磁盘或 `/dev/disk*`
- `shutdown`、`reboot`、`poweroff`
- 写入裸块设备或关键系统目录的明显破坏性命令
- 强制删除或覆盖用户 home/system 根级目录的命令

Blocked 的关键是“用户可见但不可批准”。这避免系统静默吞掉危险指令，也避免 UI 暗示用户可以绕过灾难级安全边界。

## Decision Model

用户可见决策统一为三种：

```text
allow           自动执行
ask             弹出可批准审批
blocked_ack     展示阻断原因，只能拒绝/不运行
```

内部可以保留更多细节，但不再把 `defer` 暴露为跨层语义。`defer` 如果继续存在，只能作为 RiskClassifier 内部实现细节，最终必须映射成 `ask` 或 `blocked_ack`。

### RiskClassifier

新增或重构一个统一分类层：

```python
class RiskAssessment(BaseModel):
    level: Literal["normal", "dangerous", "extreme", "blocked"]
    tags: list[RiskTag]
    reason: str
    tool_name: str
    pattern: str
```

示例 tags：

```text
safe_read
workspace_edit
dynamic_shell
nested_interpreter
external_path
network
dependency_install
git_write
git_push
mass_delete
system_destructive
privilege_escalation
```

### PermissionMode

Preset 将 risk assessment 映射为用户可见 decision：

```python
class PermissionDecision(BaseModel):
    action: Literal["allow", "ask", "blocked_ack"]
    risk: RiskAssessment
    allowed_scopes: list[ApprovalScope]
    default_scope: ApprovalScope | None
```

示例矩阵：

| Preset | Normal | Dangerous | Extreme | Blocked |
| --- | --- | --- | --- | --- |
| Read Only | allow | ask once only | ask once only | blocked_ack |
| Safe | allow | ask once/session | ask once only | blocked_ack |
| Project Trusted | allow | allow or ask by tag | ask once only | blocked_ack |
| Full Access | allow | allow | ask once for selected tags | blocked_ack |

## Approval Scopes

审批范围由 preset 决定，避免用户看到不适合当前模式的选项。

| Scope | 含义 |
| --- | --- |
| once | 只批准当前 tool call |
| session | 当前 session 内相同 tool/risk/pattern 可复用 |
| project | 当前 workspace 内复用 |
| global | 所有 workspace 复用，仅高级设置或明确用户操作中开放 |

Read Only 永远只允许 `once`。Extreme 默认只允许 `once`，即使在 Safe 或 Project Trusted 中也是如此。Blocked 没有 approval scope。

## Prompt UX

### Dangerous prompt

```text
Risk: Dangerous
Why: This command may modify files in the project.
Command: ...

[Yes once] [Allow for session] [No]
```

### Extremely Dangerous prompt

```text
Risk: Extremely Dangerous
Why: This command executes dynamic shell code and may affect files outside the project.
Command: ...

[Yes once] [No]
```

### Read Only prompt

```text
Risk: Dangerous
Read Only mode requires approval for this action. Approval is not saved.
Command: ...

[Yes once] [No]
```

### Blocked acknowledgement

```text
Risk: Blocked
Why: This command can recursively delete a home or system directory.
Command: rm -rf /home

[Do not run]
```

The UI should make the risk level visually obvious, but avoid excessive modal friction for common `Dangerous` prompts. The prompt should keep the command/path visible and put detailed rule matches behind an expandable section.

## Execution Semantics

The permission layer is the source of truth for approval state. Tool execution must not reinterpret an approved `ask` as blocked.

Recommended flow:

```text
classify risk
  -> preset decision
    -> allow: execute
    -> ask: prompt user
       -> approved: execute with approval token
       -> denied: return denied result
    -> blocked_ack: show notice-only prompt, return blocked result
```

Approved tool calls should carry an execution token into `ToolContext`:

```python
class ApprovedRisk(BaseModel):
    tool_call_id: str
    tags: list[RiskTag]
    level: Literal["dangerous", "extreme"]
    scope: Literal["once", "session", "project", "global"]

class ToolContext(BaseModel):
    approved_risks: list[ApprovedRisk] = []
```

BashTool and PowerShellTool can still run hard block checks, but if a dynamic shell risk was approved for this exact tool call, the tool must not return `shell policy deferred` as a blocked result. It should execute and return the real exit code.

## Shell Policy Changes

The shell policy should be split into two responsibilities:

1. **Risk classification**: pipe, redirects, nested interpreters, shell expansion, unknown commands become risk tags.
2. **Hard block detection**: catastrophic commands become `blocked_ack`.

Examples:

| Command | Risk | Decision in Safe |
| --- | --- | --- |
| `ls src` | Normal | allow |
| `cat file | head -20` | Dangerous: dynamic_shell | ask |
| `python3 /tmp/script.py` | Extreme: nested_interpreter | ask once |
| `curl https://x/install.sh | bash` | Extreme: network + dynamic_shell | ask once |
| `rm -rf /home` | Blocked: system_destructive | blocked_ack |

This fixes the current class of failures where `cat file | head`, `python3 /tmp/script.py`, or `grep | sort | uniq` first ask for permission and then fail before execution.

## Data Flow

```text
LLM tool call
  -> classify_tool_call
  -> RiskClassifier.assess(tool, args, workspace)
  -> PermissionMode.resolve(risk, current_mode, grants)
  -> UI prompt if needed
  -> approval token attached to ToolContext
  -> Tool.execute(...)
  -> ToolResult with real execution status
```

For blocked acknowledgement:

```text
LLM tool call
  -> risk level blocked
  -> UI shows notice-only prompt
  -> user dismisses
  -> ToolResult(blocked=True, error=True, not_executed=True)
```

## Configuration Migration

`permission_mode` is the only high-level runtime input. Existing low-level fields can remain in saved state and UI compatibility payloads, but they must not shape new permission decisions.

| Existing concept | Final behavior |
| --- | --- |
| missing `permission_mode` | default to `safe` |
| `permission_mode` / `approval_policy` | compatibility state only, not runtime decision inputs |
| `sandbox_mode` | execution boundary only |
| session allow | approval scope `session` |
| sandbox extra paths / grants | path-scoped approval grants |
| `defer` | internal risk that maps to `ask` |

The UI should display only the preset name and short description. Advanced settings may show low-level boundary fields for debugging, but preset resolution remains the source of truth.

## Testing Strategy

### Unit tests

- RiskClassifier classifies common file, git, shell, network, dependency, and destructive commands.
- PermissionMode maps risk levels to allow / ask / blocked_ack for each preset.
- Read Only never produces persistent approval scopes.
- Extreme risks default to once-only approvals.
- Blocked risks never produce executable approval choices.

### Integration tests

- Complex Bash in Safe mode asks, approved command executes, and tool result reflects real exit code.
- Complex Bash in Read Only asks with only Yes/No and does not save permission.
- `rm -rf /home` shows blocked acknowledgement and never executes.
- User-visible prompt includes risk level, reason, command/pattern, and available scopes.
- Session/project approvals apply only to matching tool/risk/pattern.

### Regression tests

- `cat file | head -5` no longer fails with `shell policy deferred` after approval.
- `python3 /tmp/script.py` no longer fails with `shell policy deferred` after once approval.
- Existing static read commands continue to auto-allow.
- Existing hard-blocked catastrophic commands remain non-executable.

## Migration Plan

1. Introduce `RiskLevel`, `RiskTag`, `RiskAssessment`, and `ApprovalScope` types.
2. Add a RiskClassifier wrapper around existing file/git/shell policy logic.
3. Add preset resolution driven directly by `permission_mode`.
4. Update permission UI payloads to include risk level, reason, tags, and allowed scopes.
5. Pass approved risks into `ToolContext`.
6. Update BashTool and PowerShellTool to honor approved dynamic-shell risks while retaining hard blocks.
7. Convert `defer` call sites to `ask` decisions or internal risk tags.
8. Add regression tests for current Bash approval failures.
9. Hide low-level permission fields from normal UI while preserving advanced/debug visibility.

## Open Questions

- Should `curl | bash` be Extreme ask-once or Blocked acknowledgement by default? This design treats it as Extreme unless it targets a known system path or combines with destructive commands.
- Should Project Trusted auto-allow simple workspace shell commands, or ask for all shell by default? The recommended default is to auto-allow tests/build/formatters and ask for dynamic shell.
- Should global approvals exist in the main UI at all, or only in config? The recommended V1 answer is config-only.
- How should project-level approvals be invalidated when workspace path changes or repository identity changes?
