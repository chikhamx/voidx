> **Status: Done** — Archived on 2026-07-12.

---
name: external-path-permission-model-plan
display_name: Workspace 外路径权限模型分阶段执行计划
description: 将 external path permission model 拆成可交付阶段，每阶段定义范围、禁用边界、验收测试和进入下一阶段的 gate
doc_type: implementation-plan
audience: human+llm
---

# Workspace 外路径权限模型 — 分阶段执行计划

## 目标

在不兼容旧 `sandbox_workspace_write` 行为的前提下，分阶段落地 `docs/design/external-path-permission-model.md` 的目标架构。每个阶段都必须可独立验证；未完成安全前置能力的功能必须 fail closed，不能以兼容模式、best-effort 检查或旧白名单放行。

## 全局原则

- 不保留 `resolve_safe`、`add_extra_path`、`sandbox_extra_paths`、`sandbox_workspace_write` 兼容接口。
- 旧配置只允许一次性迁移或清理提示；迁移失败时不激活旧授权。
- workspace 内行为保持现有 `workspace-write` 语义。
- workspace 外 read/write/file/dir/session/persistent 必须显式建模。
- read grant 不隐含 write；write grant 隐含同路径 read。
- 新阶段只能放开已经完成安全执行链路的工具；其余工具在 workspace 外 fail closed。
- 每个阶段都要补齐 targeted tests，再进入下一阶段。

## 阶段总览

| 阶段 | 可交付目标 | 允许的 workspace 外能力 | 必须 fail closed 的范围 |
|---|---|---|---|
| Phase 0 | 删除旧模型并建立 canonical permission state | 无新增工具放行 | 所有未迁移外部路径访问 |
| Phase 1 | `resolve_access` + Engine 三态 + file read/write 基础审批 | read/write/replace 精确 file grant | manage move/delete、git、shell、powershell |
| Phase 2 | Settings 持久化事务、session/persistent grants、并发锁 | file tools 的 session/persistent file/dir grant | git、shell、powershell |
| Phase 3 | SafePathExecutor MVP 与 manage 工具 | manage create/delete/move，使用 capability 执行 | 跨文件系统 move copy/delete fallback |
| Phase 4 | 子代理 snapshot、epoch lease、LSP 输出过滤 | 子代理继承固定 snapshot；LSP 过滤授权外结果 | 子代理新增 grant；未授权 LSP path 泄露 |
| Phase 5 | Git 有限策略白名单 | 已登记 git read/write 策略及完整 runtime plan | 未登记 raw git、危险配置、计划外 metadata |
| Phase 6 | Shell/PowerShell 安全执行 | 受限 grammar + policy + OS sandbox 同时可用的平台 | 后端缺失、动态语法、未知命令 |

## Phase 0 — Canonical Permission Foundation

### Scope

- 新增 `permission/grants.py`，定义 `AccessGrants`、`EffectiveAccessGrants`、`GrantDelta`、`GrantTarget`、`ApprovalPrecondition`、结果类型和状态不变量。
- 配置层新增四个 canonical 字段：`sandbox_readable_files`、`sandbox_readable_dirs`、`sandbox_writable_files`、`sandbox_writable_dirs`。
- 删除运行时对 `sandbox_workspace_write` 的依赖，不保留兼容接口。
- `Settings(...)` 与 `Settings.create(...)` 使用同一个同步迁移入口。
- mixed legacy/canonical schema 中 canonical 整体优先，legacy 不合并。

### Gate

- 代码中没有旧接口的生产调用点。
- 旧配置迁移失败时 external grants 为空并报告 warning。
- 非 CUSTOM preset 清空 session/persistent path grants 和四个 canonical 字段。

### Tests

- `test_resolve_safe_removed`
- `test_legacy_config_migrated_on_load`
- `test_mixed_permission_schema_prefers_canonical`
- `test_legacy_migration_failure_fails_closed`
- `test_preset_clears_path_grants`

## Phase 1 — Engine Defer + File Tool Path Approval MVP

### Scope

- 新增 `resolve_access(...)`，只做规范化、grant 命中和 `AccessIntent` 生成，不执行文件系统副作用。
- Engine sandbox 预检查从二态改为 `ALLOW / DEFER_TO_TOOL / DENY`。
- `read`、`write`、`replace` 先迁移到 `resolve_access`。
- 外部 read/write 缺 grant 时由工具发起封闭选择审批。
- `write` 支持不存在的精确 file target；read 默认要求目标存在。

### Gate

- Engine 对已迁移 file tools 的可审批外部路径返回 defer，不 hard deny。
- Tool 收到 defer 后，用户拒绝或审批失败时无副作用。
- read grant 后同文件 write 仍需审批；file grant 不覆盖 sibling。

### Tests

- `test_engine_defers_approvable_read`
- `test_engine_denies_non_approvable_tool`
- `test_deferred_path_denied_by_user`
- `test_read_grant_does_not_allow_write`
- `test_file_grant_does_not_cover_sibling`
- `test_write_missing_external_target`

## Phase 2 — Persistent Grants, Revisions, and Grant Locks

### Scope

- `PermissionService` 拥有 runtime/session/persistent grants、`state_revision`、`permissions_revision`、`permission_state_ready` 和 `revocation_epoch`。
- 新增 `PathGrantLockManager`，实现 request/final 两阶段锁和目录祖先/后代冲突规则。
- 新增 Settings 完整权限事务与 additive persistent `GrantDelta` 事务。
- `ToolContext` 注入 callback：`get_access_grants`、`acquire_grant_targets`、`add_grant`。
- file tools 支持 session 与 persistent file/dir grants。

### Gate

- session grant 不落盘；persistent grant 以 Settings replace 为提交点。
- 并发 persistent grants 不丢失更新。
- `state_revision` 与 `permissions_revision` 不混用。
- `permission_state_ready=False` 时所有 workspace 外入口 fail closed。

### Tests

- `test_context_grants_are_refreshed`
- `test_grant_lock_serializes_same_file`
- `test_grant_lock_sibling_upgrade_to_parent`
- `test_concurrent_persistent_grants_merge_latest`
- `test_revision_domains_are_independent`
- `test_permission_not_ready_blocks_all_authorization_entries`
- `test_permission_transaction_postcommit_recovery`

## Phase 3 — SafePathExecutor MVP + Manage Tools

### Scope

- 新增 `tools/file/safe_path.py`，提供不可伪造 `AuthorizedPath` capability。
- file write/replace/read 副作用迁移到 SafePathExecutor。
- `manage create/delete/move` 迁移到 `resolve_access` 和 SafePathExecutor。
- move 的 src 与 dest 都要求 write grant。
- 初始版本仅支持同文件系统原子 rename；`EXDEV` 返回稳定错误，不做 copy/delete fallback。

### Gate

- 授权检查后不得退回 `Path.write_text`、`shutil.move` 等路径式副作用。
- symlink/reparse point 竞态必须 fail closed。
- forged/cross-executor/inactive capability 不能访问文件系统。

### Tests

- `test_move_source_requires_write`
- `test_move_cross_write_grants`
- `test_safe_path_read_rejects_symlink_swap`
- `test_safe_path_rejects_symlink_swap`
- `test_authorized_path_is_unforgeable`
- `test_safe_path_rename_uses_authorized_handles`
- `test_safe_path_rejects_cross_filesystem_move`

## Phase 4 — Epoch Lease, Subagents, and LSP Filtering

### Scope

- 新增 `PermissionEpochGate` 和 execution lease。
- 主代理工具从最终授权复查持有 lease 到副作用完成。
- 子代理创建时注入不可变 `SubagentPermissionSnapshot`。
- 子代理 ToolContext 固定返回创建时 grants，不允许新增 grant。
- LSP 输入按 read grant 检查，输出逐条过滤未授权路径。

### Gate

- 撤销、clear、mode 收紧不能插入授权检查与副作用之间。
- 父会话新增 grant 不传播到已创建子代理。
- LSP 不泄露未授权路径名称、数量或位置。

### Tests

- `test_main_tool_execution_lease_blocks_revocation`
- `test_pregranted_tool_holds_execution_lease`
- `test_execution_lease_token_is_unforgeable`
- `test_subagent_inherits_effective_grants`
- `test_subagent_cannot_add_grant`
- `test_subagent_grants_snapshot_fixed`
- `test_subagent_snapshot_invalidated_on_revocation`
- `test_lsp_filters_external_locations`

## Phase 5 — Git Limited Policy Rollout

### Scope

- 新增 `permission/git_policy.py`，作为 Engine 与 GitTool 的共享策略注册表。
- 首版只支持明确登记的 git 子命令和参数组合。
- workspace 外 repo path 必须直接指向 worktree root 或 bare git dir，不向上搜索父目录。
- runtime plan 必须包含 worktree、git dir、common dir、index、object dirs、config files 和 explicit paths。
- Git 子进程清理 `GIT_*` 环境并固定受控 config/hook 行为。

### Gate

- 未登记 raw git 在 workspace-write 下 fail closed。
- linked worktree 的 common dir、object alternates、config include 均先授权后读取。
- `git config --show-origin` 返回计划外来源时拒绝执行。
- 危险配置未被策略显式禁用时拒绝命令。

### Tests

- `test_git_external_path_must_be_repo_root`
- `test_git_requires_linked_worktree_common_dir`
- `test_git_requires_alternate_object_dirs`
- `test_git_sanitizes_path_environment`
- `test_git_config_include_requires_grant`
- `test_git_rejects_unplanned_config_origin`
- `test_git_denies_implicit_executable_config`
- `test_git_unknown_raw_policy_denied`

## Phase 6 — Shell and PowerShell Containment

### Scope

- 新增 `permission/shell_policy.py`，定义受限 grammar、命令策略注册表和静态访问计划。
- 新增 `permission/process_sandbox.py`，封装 Linux/macOS/Windows 文件系统沙箱能力。
- bash/powershell 在 read-only 和 workspace-write 下只允许已登记策略。
- workspace 外路径统一要求 writable grant。
- danger-full-access 保持通用 shell 语法，但仍保留 destructive deny、审批策略和超时。

### Gate

- 无可验证进程级 sandbox 后端的平台在 read-only/workspace-write 下 fail closed。
- 动态语法、未知命令、嵌套解释器默认拒绝。
- 子进程尝试访问未授权路径时由 OS sandbox 阻止，不依赖静态 parser。

### Tests

- `test_shell_closed_policy_denies_unknown_and_dynamic`
- `test_shell_sandbox_contains_child_process`
- `test_shell_requires_process_sandbox_backend`
- `test_shell_read_only_denies_write_capability`
- `test_shell_full_access_mode_matrix`
- `test_powershell_external_read_requires_write`

## Release Gates

每个 phase 合并前必须满足：

1. 本 phase targeted tests 全绿。
2. `./test.py --backend -- <targeted tests>` 通过。
3. 生产代码不存在已删除旧接口的新引用。
4. 未完成阶段对应工具在 workspace 外 fail closed。
5. 用户可见错误稳定、明确，不泄露未授权路径是否存在。

Phase 3、Phase 5、Phase 6 额外要求 security-focused review；Phase 6 还要求 Linux/macOS/Windows containment 能力矩阵通过后才能默认启用。

## 推荐执行顺序

1. Phase 0 先作为 breaking cleanup 单独合并。
2. Phase 1 和 Phase 2 可连续实现，但 Phase 1 不应开放 persistent grant。
3. Phase 3 完成后再开放 manage 外部路径能力。
4. Phase 4 在子代理和 LSP 输出过滤前必须完成 epoch lease。
5. Phase 5 与 Phase 6 独立推进；两者未完成前 Git/Shell 外部路径访问保持 fail closed。
