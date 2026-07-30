# 能力规范：workspace 外路径审批

## Requirement: 单次审批

workspace 外路径访问（read/write/replace/manage/lsp_format）只触发一次用户审批，不在 permission_flow 层和工具执行层各弹一次。

### Scenario: workspace 外文件 read 只弹一次审批

- **GIVEN** sandbox 模式为 `workspace-write`
- **AND** LLM 返回一个 read 工具调用，file_path 在 workspace 外
- **WHEN** permission_flow 层完成审批（用户选择 Allow once）
- **THEN** approved tool call 包含 `metadata["access_approval"]`
- **AND** `metadata["access_approval"].access = "read"`
- **AND** 工具执行层不再弹出 `ctx.interact()` 审批
- **AND** 文件读取成功

### Scenario: workspace 外文件 write 只弹一次审批

- **GIVEN** sandbox 模式为 `workspace-write`
- **AND** LLM 返回一个 write 工具调用，file_path 在 workspace 外
- **WHEN** permission_flow 层完成审批（用户选择 Allow once）
- **THEN** approved tool call 包含 `metadata["access_approval"]`
- **AND** `metadata["access_approval"].access = "write"`
- **AND** 工具执行层不再弹出 `ctx.interact()` 审批
- **AND** 文件写入成功

### Scenario: workspace 外路径访问被拒绝后不弹第二次

- **GIVEN** sandbox 模式为 `workspace-write`
- **AND** LLM 返回一个 read 或 write 工具调用，file_path 在 workspace 外
- **WHEN** permission_flow 层用户选择 Deny
- **THEN** 该 tool call 进入 denied 列表
- **AND** 工具执行层不执行该 tool call
- **AND** 不再弹出 `ctx.interact()` 审批

### Scenario: workspace 内路径不触发审批

- **GIVEN** sandbox 模式为 `workspace-write`
- **AND** LLM 返回一个 read 或 write 工具调用，file_path 在 workspace 内
- **WHEN** authorize_tool_call 执行
- **THEN** 返回 action="allow"
- **AND** 不弹出任何审批

## Requirement: 路径级 approval token

路径审批通过后，系统 MUST 使用真实工具名、access 和 normalized path 生成专用路径 approval token，工具执行层 MUST 只在三者都与当前工具调用匹配时跳过二次审批。

### Scenario: Once approval 写入 access_approval

- **GIVEN** workspace 外文件 read 触发审批
- **WHEN** 用户选择 Allow once
- **THEN** approved tool call 包含 `metadata["access_approval"].tool_name = "read"`
- **AND** `metadata["access_approval"].access = "read"`
- **AND** `metadata["access_approval"].normalized_path` 是 `AccessIntent.normalized_path` 的字符串形式
- **AND** `metadata["access_approval"].approved_by = "user"`

### Scenario: access 不匹配不放行

- **GIVEN** workspace 外 read 审批生成了 `access="read"` 的 access approval
- **WHEN** 工具执行层处理同一路径的 write 操作
- **THEN** `ctx.has_access_approval()` 不匹配
- **AND** 工具执行层走 fallback 审批逻辑

### Scenario: 工具名不匹配不放行

- **GIVEN** workspace 外 write 审批生成了 `tool_name="write"` 的 access approval
- **WHEN** 工具执行层处理同一路径的 replace 或 manage 操作
- **THEN** `ctx.has_access_approval()` 不匹配
- **AND** 工具执行层走 fallback 审批逻辑

### Scenario: 多路径 tool call 写入 token 列表

- **GIVEN** manage move 同时涉及 workspace 外 src 和 dest
- **WHEN** 用户选择批量 Allow once
- **THEN** approved tool call 的 `metadata["access_approval"]` 是 token 列表
- **AND** token 列表包含 src 的 normalized_path
- **AND** token 列表包含 dest 的 normalized_path

### Scenario: AI approval 写入 access_approval

- **GIVEN** permission mode 为 AI approval
- **AND** workspace 外路径 decision 被 AI 审批允许
- **WHEN** permission_flow 返回 approved tool call
- **THEN** approved tool call 包含 `metadata["access_approval"].approved_by = "ai"`
- **AND** 工具执行层不再弹出 `ctx.interact()` 审批

### Scenario: 路径 approval 不使用 legacy approved_risk

- **GIVEN** workspace 外路径 decision 被审批允许
- **WHEN** permission_flow 返回 approved tool call
- **THEN** approved tool call 使用 `metadata["access_approval"]`
- **AND** 不使用 `metadata["approved_risk"]` 表示路径授权

## Requirement: 友好的 grant 级别选项

workspace 外**单路径 intent**审批时，用户可选择授权范围：单次、本 session 文件、本 session 文件夹、永久记住文件、永久记住文件夹。多路径审批 MUST 保持批量 Yes/No，避免一个 grant choice 错误表达多个路径。

### Scenario: 单路径审批显示友好文案

- **GIVEN** 当前审批批次只有一个 workspace 外 AccessIntent
- **WHEN** permission_flow 生成用户审批选项
- **THEN** 选项 label 包含 "Allow once"
- **AND** 选项 label 包含 "This file this session"
- **AND** 选项 label 包含 "This folder this session"
- **AND** 选项 label 包含 "Always allow this file"
- **AND** 选项 label 包含 "Always allow this folder"
- **AND** 选项 label 包含 "Deny"

### Scenario: 选择 This file this session 授权

- **GIVEN** 单个 workspace 外文件 read 触发审批
- **WHEN** 用户选择 "This file this session"
- **THEN** 调用 `add_grant` 写入 `AccessGrant(access="read", persistence="session", object_type="file")`
- **AND** 同一文件再次读取时不再审批

### Scenario: 选择 This folder this session 授权

- **GIVEN** 单个 workspace 外文件 write 触发审批
- **WHEN** 用户选择 "This folder this session"
- **THEN** 调用 `add_grant` 写入 `AccessGrant(access="write", persistence="session", object_type="dir")`
- **AND** 同目录下文件再次写入时不再审批

### Scenario: 选择 Always allow this file 授权

- **GIVEN** 单个 workspace 外文件 write 触发审批
- **WHEN** 用户选择 "Always allow this file"
- **THEN** 调用 `add_grant` 写入 `AccessGrant(access="write", persistence="persistent", object_type="file")`
- **AND** 重启后同一文件操作不再审批

### Scenario: 选择 Always allow this folder 授权

- **GIVEN** 单个 workspace 外文件 write 触发审批
- **WHEN** 用户选择 "Always allow this folder"
- **THEN** 调用 `add_grant` 写入 `AccessGrant(access="write", persistence="persistent", object_type="dir")`
- **AND** 重启后同目录下文件操作不再审批

### Scenario: 多 decision 审批不显示 grant 选项但写 once token

- **GIVEN** 同一审批批次包含多个 workspace 外路径 decision
- **WHEN** permission_flow 生成用户审批选项
- **THEN** 不显示 This file this session / This folder this session / Always allow this file / Always allow this folder
- **AND** 用户选择批量 Allow once 后，每个 approved path call 都包含 `access_approval`

### Scenario: 单 decision 多路径不显示 grant 选项

- **GIVEN** 单个 decision 包含多个 workspace 外 AccessIntent
- **WHEN** permission_flow 生成用户审批选项
- **THEN** 不显示路径级 grant 选项
- **AND** 用户选择批量 Allow once 后，该 tool call 包含 token 列表形式的 `access_approval`

## Requirement: manage 与 format 边界

### Scenario: manage move 多路径不显示 grant 选项

- **GIVEN** manage move 同时涉及 workspace 外 src 和 dest
- **WHEN** permission_flow 生成用户审批选项
- **THEN** 不显示路径级 grant 选项
- **AND** 用户仍可通过批量 Yes/No 允许或拒绝该 tool call

### Scenario: manage create/delete 单外部路径可显示 grant 选项

- **GIVEN** manage create 或 delete 只涉及一个 workspace 外路径
- **WHEN** permission_flow 生成用户审批选项
- **THEN** 显示单路径 grant 选项

### Scenario: lsp_format workspace 外路径使用 write access grant

- **GIVEN** lsp_format 的 file_path 在 workspace 外
- **WHEN** 触发 workspace 外路径审批
- **THEN** 该 decision 可使用单路径 grant 选项
- **AND** grant 使用 write access 语义

## Requirement: 未鉴权场景

### Scenario: permission_flow 层未授权时工具执行层 fallback

- **GIVEN** sandbox 模式为 `workspace-write`
- **AND** permission_flow 层因某种原因未写入 `access_approval` metadata（如兼容旧代码路径）
- **WHEN** 工具执行层 `_resolve_tool_path_for_access` 调用 `resolve_access` 返回 defer
- **AND** `ctx.has_access_approval()` 返回 False
- **THEN** 走原有 `ctx.interact()` 逻辑作为 fallback
- **AND** 用户仍可完成审批

### Scenario: path traversal 路径直接拒绝

- **GIVEN** LLM 返回一个 read 或 write 工具调用，file_path 含 `../` path traversal
- **WHEN** authorize_tool_call 执行
- **THEN** 返回 action="deny"
- **AND** 不弹出任何审批

## Requirement: 参数非法场景

### Scenario: file_path 为空

- **GIVEN** LLM 返回一个 read 或 write 工具调用，args 中 file_path 为空或缺失
- **WHEN** authorize_tool_call 执行
- **THEN** 不崩溃
- **AND** 返回合理的错误或默认行为

### Scenario: file_path 为相对路径

- **GIVEN** LLM 返回一个 read 或 write 工具调用，file_path 为相对路径如 `../../etc/passwd`
- **WHEN** resolve_access 解析路径
- **THEN** 正确解析为 workspace 外路径
- **AND** 触发审批或拒绝（取决于是否 path traversal）
