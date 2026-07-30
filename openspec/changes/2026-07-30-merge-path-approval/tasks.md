# 任务清单：合并 workspace 外路径审批到单层

按 TDD 顺序编排，每个实现步骤前先写测试。

## 阶段一：engine 层传递 access intents

- [ ] T1 [unit] 编写测试：`sandbox_precheck_action` 对 workspace 外 write 路径返回一个 intent（`intent.access="write"`, `intent.is_workspace_path=False`）
- [ ] T2 [unit] 编写测试：`sandbox_precheck_action` 对 workspace 外 read 路径返回一个 intent（`intent.access="read"`, `intent.is_workspace_path=False`）
- [ ] T3 [unit] 编写测试：`sandbox_precheck_action` 对 workspace 外 manage move src/dest 返回两个 write intents
- [ ] T4 [unit] 编写测试：`sandbox_precheck_action` 对 workspace 外 lsp_format 返回 write intent
- [ ] T5 [unit] 编写测试：`sandbox_precheck_action` 对 workspace 内路径返回 `(action, reason, ())`（无 intents）
- [ ] T6 [unit] 编写测试：`authorize_tool_call` 返回的 `PermissionDecision.access_intents` 非空（defer→ask 路径）
- [ ] T7 [unit] 编写测试：`sandbox_denial_reason` 适配三元组返回值，不因解包失败崩溃
- [ ] T8 [实现] 修改 `sandbox_precheck_action` 返回 `tuple[Action, str | None, tuple[AccessIntent, ...]]`，所有分支补齐第三个元素
- [ ] T9 [实现] 修改 workspace-write 路径预检：read/write/replace/manage/lsp_format 统一用 `resolve_access()` 产出 intents
- [ ] T10 [实现] 修改 `authorize_tool_call` 将 intents 传入 `_decision`
- [ ] T11 [实现] 修改 `sandbox_denial_reason` 解包三元组
- [ ] T12 [实现] `PermissionDecision` 增加 `access_intents: tuple[AccessIntent, ...] = ()` 字段和可选 `primary_access_intent` helper
- [ ] T13 [验证] 运行 T1-T7 测试通过

## 阶段二：permission_flow 层增加单路径友好 grant 选项

- [ ] T14 [unit] 编写测试：单个 workspace 外 read intent 生成友好选项（Allow once / This file this session / Always allow this folder / Deny）
- [ ] T15 [unit] 编写测试：单个 workspace 外 write/replace/manage/lsp_format intent 生成友好选项，description 使用 edit 语义
- [ ] T16 [unit] 编写测试：单个 decision 但多个 external intents（manage move src/dest）不生成 grant 选项，保持批量 Yes/No
- [ ] T17 [unit] 编写测试：多个 workspace 外路径 decision 不生成 grant 选项，保持批量 Yes/No
- [ ] T18 [unit] 编写测试：workspace 内路径 decision（无 external intents）不生成 grant 选项
- [ ] T19 [实现] 修改 `_permission_choices`：按审批批次 external intent 总数判断；仅总数为 1 时显示 grant 选项
- [ ] T20 [实现] 将选项 label 从内部术语改为用户友好文案：Allow once / This file this session / This folder this session / Always allow this file / Always allow this folder / Deny
- [ ] T21 [验证] 运行 T14-T18 测试通过

## 阶段三：permission_flow 层处理 grant choice 与 once token

- [ ] T22 [unit] 编写测试：read choice="session_file" 时调用 `host._permission.add_grant`，access="read", persistence="session", object_type="file"
- [ ] T23 [unit] 编写测试：write choice="persistent_dir" 时调用 `host._permission.add_grant`，access="write", persistence="persistent", object_type="dir"
- [ ] T24 [unit] 编写测试：choice="once" 时不调用 `add_grant`，但写入 `access_approval` metadata
- [ ] T25 [unit] 编写测试：批量 allow once 为每个路径 decision 写入 `access_approval`，但不写 session/persistent grant
- [ ] T26 [unit] 编写测试：manage move 批量 allow once 写入 list `access_approval`，包含 src/dest 两个 token
- [ ] T27 [unit] 编写测试：choice="deny" 时 tool_call 进入 denied 列表且不进入 approved
- [ ] T28 [实现] 新增 `_tool_call_with_access_approval(decision, approved_by=...)`，支持单 token dict 和多 token list
- [ ] T29 [实现] 修改 `_ask_and_apply_permission`：处理 `once/session_file/session_dir/persistent_file/persistent_dir/deny`，并兼容旧 `n/no`
- [ ] T30 [实现] 增加 grant lock：调用 permission service 的 grant target lock，并在锁内重新 resolve 后再 add_grant
- [ ] T31 [验证] 运行 T22-T27 测试通过

## 阶段四：AI approval 与 cached 策略

- [ ] T32 [unit] 编写测试：AI allow 路径 decision 时写入 `metadata["access_approval"]` 且 `approved_by="ai"`
- [ ] T33 [unit] 编写测试：AI allow bash/powershell 时仍写入 legacy `approved_risk`
- [ ] T34 [unit] 编写测试：AI 失败 fallback 到人工 grant choice 时可正常写入 grant/access_approval
- [ ] T35 [unit] 编写测试：dangerous shell cached approval 仍使用 `approved_risk` 且不影响路径 approval
- [ ] T36 [实现] 修改 AI approval 分支：路径 decision 使用 `_tool_call_with_access_approval`，shell 风险 decision 继续使用 `_tool_call_with_approval_risk`
- [ ] T37 [实现] 明确路径 approval 不支持 cached；移除路径 metadata 中 `approved_by="cached"` 的写入路径
- [ ] T38 [验证] 运行 T32-T35 测试通过

## 阶段五：executor 读取 access_approval

- [ ] T39 [unit] 编写测试：`_approved_access_for_call` 读取 `metadata["access_approval"]` dict 并转为 `ApprovedAccess(tool_name, normalized_path, access)`
- [ ] T40 [unit] 编写测试：`_approved_access_for_call` 读取 `metadata["access_approval"]` list 并转为多个 `ApprovedAccess`
- [ ] T41 [unit] 编写测试：legacy `approved_risk` 仍被 `_approved_tool_risks_for_call` 读取，bash/powershell 回归不变
- [ ] T42 [实现] 新增 `ApprovedAccess` 模型和 `ToolContext.approved_access`
- [ ] T43 [实现] 新增 executor `_approved_access_for_call` 并在执行前设置 `ctx.approved_access`
- [ ] T44 [实现] 保持 `_approved_tool_risks_for_call` 仅处理 legacy `approved_risk`
- [ ] T45 [验证] 运行 T39-T41 测试通过

## 阶段六：工具执行层跳过已授权路径

- [ ] T46 [unit] 编写测试：`_resolve_tool_path_for_access(tool_name="read")` 在 path/access 匹配时不调用 `ctx.interact`，直接返回路径
- [ ] T47 [unit] 编写测试：`_resolve_tool_path_for_access(tool_name="write")` 在 path/access 匹配时不调用 `ctx.interact`，直接返回路径
- [ ] T48 [unit] 编写测试：access 不匹配时不跳过（approved read 不能放行 write）
- [ ] T49 [unit] 编写测试：tool_name 不匹配时不跳过（approved write 不能放行 replace/manage）
- [ ] T50 [unit] 编写测试：`has_access_approval=False` 时走原 `ctx.interact` fallback
- [ ] T51 [unit] 编写测试：workspace 内路径（resolve_access 返回 allow）不受影响
- [ ] T52 [实现] 修改 `_resolve_tool_path_for_access`：增加 `tool_name` 参数，在 defer 分支用 `actual_tool_name + str(intent.normalized_path) + access` 检查
- [ ] T53 [实现] 修改 read/write/replace/manage/lsp_format 调用方，传入真实工具名
- [ ] T54 [验证] 运行 T46-T51 测试通过

## 阶段七：manage/lsp_format 边界验证

- [ ] T55 [unit] 编写测试：manage move 涉及多个外部路径时不显示 grant 选项
- [ ] T56 [unit] 编写测试：manage create/delete 单外部路径可显示 grant 选项
- [ ] T57 [unit] 编写测试：lsp_format workspace 外路径按 `FILE_FORMAT` 可显示单路径 grant 选项
- [ ] T58 [验证] 运行 T55-T57 测试通过

## 阶段八：集成验证

- [ ] T59 [integration] 端到端测试：workspace 外 read 只弹一次审批（`_ask_tool_permission` 调用 1 次，`ctx.interact` 调用 0 次）
- [ ] T60 [integration] 端到端测试：workspace 外 write 只弹一次审批（`_ask_tool_permission` 调用 1 次，`ctx.interact` 调用 0 次）
- [ ] T61 [integration] 端到端测试：session_file 授权后同文件再次操作不再审批
- [ ] T62 [integration] 端到端测试：AI approval 允许 workspace 外路径后工具执行层不再二次审批
- [ ] T63 [integration] 端到端测试：manage move 批量 allow 不显示 grant，且 src/dest 执行层都不二次审批
- [ ] T64 [regression] 回归测试：workspace 内路径访问行为不变
- [ ] T65 [regression] 回归测试：bash/powershell 审批行为不变
