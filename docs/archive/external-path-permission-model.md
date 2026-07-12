> **Status: Done** — Archived on 2026-07-12.

---
name: external-path-permission-model
display_name: Workspace 外路径权限模型
description: workspace 内保持现有沙箱行为，workspace 外按访问类型、对象类型和生命周期精确授权
doc_type: tech-design
audience: human+llm
---

# Workspace 外路径权限模型 — 技术设计文档

## TL;DR

当前 read 审批 workspace 外文件后，会把父目录加入 `sandbox_workspace_write`，导致一次只读审批实际授予整个目录在当前会话内的读写权限。

本方案把外部路径授权拆为三个正交维度：

- 访问类型：read / write
- 对象类型：file / directory
- 生命周期：session / persistent

用户授权由 session 与 persistent 两层组成，另有仅由启动代码注入的 runtime 授权；三者合并为有效授权。workspace 内继续保持现有 workspace-write 行为；workspace 外必须命中对应授权，或由支持路径审批的工具发起精确审批。write 授权隐含 read，read 授权不隐含 write。

安全边界的关键修正：

- move 会删除源文件，因此 src 与 dest 都要求 write
- Engine 对可审批的外部路径返回“延迟到工具审批”，不能提前 deny 阻断审批链
- 并发审批使用规范化路径层级锁：相同目标、目录与其任意后代互斥；不相交文件或目录可以并发
- persistent grant 在层级锁内基于最新磁盘权限状态合并；`state_revision` 与 `permissions_revision` 分属内存和持久化版本域
- 外部文件副作用通过句柄式 `SafePathExecutor` 执行，不能在授权检查后退回 `Path.write_text`、`shutil.move` 等路径式调用
- shell 在 workspace-write 下使用封闭命令策略、受限 AST 与进程级文件系统沙箱；未知、动态或无法完整约束的命令直接拒绝
- 子代理的 Engine 路径检查与 ToolContext getter 必须绑定同一份创建时授权快照
- `resolve_safe` 直接删除，所有调用点迁移到 `resolve_access`
- 新文件可以按精确目标路径审批，不要求目标预先存在

## Context

### 当前行为

权限模型有两层路径检查：

1. **Permission Engine**：`authorize_tool_call` 调用 `sandbox_denial_reason`，在工具执行前检查 workspace 边界。
2. **Tool 内部**：文件工具执行时调用 `resolve_safe` 再次检查路径。

两层共用 `PermissionService.sandbox_workspace_write`：

- Engine 通过 `PermissionContext.sandbox_workspace_write` 获取快照
- Tool 通过 `ToolContext.sandbox_extra_paths` 获取允许路径

read 工具审批外部文件时，会调用 `ctx.add_extra_path(str(external.parent))`，把父目录加入写白名单。用户看到的是一次读取授权，实际得到的是目录级读写授权。

### 根因

| 层 | 问题 |
|---|---|
| 数据结构 | 单一列表不区分 read/write、file/directory、session/persistent |
| 审批回调 | `add_extra_path` 无差别写入目录级 write 白名单 |
| 路径解析 | `resolve_safe` 不知道调用者需要 read 还是 write |
| Engine | hard deny 会阻止工具进入路径审批流程 |
| 生命周期 | 当前授权直接修改 PermissionService 列表，没有 session/persistent 分层 |
| 上下文同步 | Pydantic 会复制 list，不能依赖 ToolContext 与 PermissionService 共享列表引用 |

## Goals / Non-Goals

### Goals

- workspace 外 read 与 write 权限严格分离
- 文件级授权不扩散到同目录其他文件
- 用户可选择文件级或目录级授权
- 用户可选择 session 或 persistent 生命周期
- write 授权隐含 read，read 授权不隐含 write
- workspace 内行为保持不变
- 所有调用点直接迁移到 `resolve_access`，不保留兼容包装
- 现有 `sandbox_workspace_write` 配置在加载时一次性迁移为 canonical 字段
- 主代理与子代理执行时使用一致的有效授权

### Non-Goals

- 不改变 workspace 内 workspace-write 行为
- 不改变 danger-full-access 与 read-only 的模式定义
- 不让 glob 搜索 workspace 外路径
- 不构建用户/角色 ACL
- 不在 workspace-write 模式下维持任意 shell 语法或任意可执行文件兼容性；命令不满足封闭策略或缺少可用的进程沙箱后端时拒绝，并提示使用专用工具或 danger-full-access

## Permission Model

### 授权集合

每个授权来源都包含四个集合：

| | 文件级 | 目录级 |
|---|---|---|
| **read** | `readable_files` | `readable_dirs` |
| **write** | `writable_files` | `writable_dirs` |

```python
@dataclass(frozen=True)
class AccessGrants:
    readable_files: frozenset[str] = frozenset()
    readable_dirs: frozenset[str] = frozenset()
    writable_files: frozenset[str] = frozenset()
    writable_dirs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EffectiveAccessGrants:
    runtime: AccessGrants
    session: AccessGrants
    persistent: AccessGrants
    permission_mode: PermissionMode
    permission_state_ready: bool
    state_revision: int
    permissions_revision: int
    revocation_epoch: int
```

检查时读取三个来源集合的并集，不在 ToolContext 内维护可变列表副本：

- `runtime`：应用内部预授权路径，例如当前自动注入的 `DATA_DIR`；只能由启动代码构建，不接受用户输入、不写入 settings
- `session`：当前会话内由用户审批的授权
- `persistent`：从 settings 加载或由用户明确持久化的授权

runtime grant 仅允许列入应用自身管理且已有明确用途的路径，不能成为绕过用户审批的通用注入口。

### 授权规则

1. workspace 内：保持现有 workspace-write 行为。
2. workspace 外 read：命中 readable 或 writable 的 file/dir 授权即可。
3. workspace 外 write：必须命中 writable 的 file/dir 授权。
4. 文件级授权仅精确匹配规范化后的目标路径。
5. 目录级授权通过 `Path.relative_to` 判断后代关系，禁止字符串前缀匹配。
6. write 隐含 read；read 不隐含 write。
7. read-only 模式始终阻止写操作，即使存在 writable grant。
8. danger-full-access 跳过本模型的路径限制。

### 路径规范化与安全落点

授权判断与文件系统副作用分为两层，二者不能合并为一次 `Path.resolve()`：

1. `normalize_grant_path(...)` 生成审批与授权集合使用的规范化路径：
   - 展开 `~`
   - 相对路径以 workspace 为基准
   - 已存在路径解析为 canonical 路径
   - 不存在的写目标解析最近的已存在祖先，再规范化剩余路径段
   - 拒绝 `.` / `..` 逃逸、非法路径段、无法规范化的路径和对象类型冲突
2. `SafePathExecutor` 在实际操作时以已打开的授权祖先目录句柄为根执行：
   - Unix 逐级使用 `dir_fd` + `O_DIRECTORY` + `O_NOFOLLOW` 打开路径组件，最终读写、创建、删除和 rename 均使用 `*at`/等价句柄 API
   - Windows 使用不跟随 reparse point 的句柄打开方式，并校验句柄解析出的最终路径仍位于授权根内
   - 缺少所需平台原语或校验失败时，workspace 外操作 fail closed；不得退回普通路径 API
   - 不存在目标只允许在已打开且已授权的祖先句柄下逐级创建；任何中间组件在操作期间变为 symlink/reparse point 都必须失败
   - move 优先使用同一安全执行器的原子 rename；跨文件系统移动只有在实现安全 copy+fsync+delete 流程时才允许，否则拒绝

授权命中仅表示“允许尝试操作”；`SafePathExecutor` 成功获得安全句柄才表示最终落点已被约束。副作用前再次调用 `resolve_access` 仍保留用于刷新授权状态，但不能作为 TOCTOU 防护的替代品。

`SafePathExecutor` 将 `AccessIntent` 转换为不可伪造的执行能力。`AuthorizedPath` 构造器不导出，实例只由 executor 在 execution lease 内打开授权根句柄、逐级 no-follow 校验后创建；能力绑定 executor 实例、根句柄、相对组件、访问类型和对象类型，不能序列化或跨 executor 使用：

```python
class SafePathExecutor(Protocol):
    def open_authorized(
        self,
        intent: AccessIntent,
        lease: ExecutionLeaseToken,
    ) -> AuthorizedPath: ...
    def read_bytes(self, target: AuthorizedPath) -> bytes: ...
    def write_bytes_atomic(self, target: AuthorizedPath, data: bytes, *, replace: bool) -> None: ...
    def create_directory(self, target: AuthorizedPath, *, parents: bool) -> None: ...
    def unlink_file(self, target: AuthorizedPath) -> None: ...
    def remove_tree(self, target: AuthorizedPath) -> RemoveTreeResult: ...
    def rename(self, src: AuthorizedPath, dest: AuthorizedPath, *, replace: bool) -> None: ...

@dataclass(frozen=True)
class RemoveTreeResult:
    deleted: tuple[str, ...]
    error: str = ""
```

- `ExecutionLeaseToken` 与 `AuthorizedPath` 都由内部私有实现签发，不暴露公共构造器、序列化方法或可复制的 issuer secret；退出 lease context 后 token 永久 inactive
- executor 持有私有 issuer identity；每个方法验证 capability 由同一 executor 签发、绑定的 lease token 仍 active、token epoch 与 intent/capability epoch 一致且 access 足够，调用方手工构造对象必须失败
- `open_authorized(intent, lease)` 拒绝 inactive、跨 gate 或 epoch 不匹配的 token；验证通过后从 grant root 开始逐级打开，不能信任 intent 中的字符串作为已打开路径
- `read_bytes` 从最终 no-follow 文件句柄读取；授权检查后不得重新按字符串路径打开
- `write_bytes_atomic` 在目标 parent handle 下创建随机临时文件，写入并 fsync，再用句柄相对 rename 提交；失败时清理临时项
- `remove_tree` 通过目录句柄递归枚举，不跟随 symlink/reparse point；任一步失败停止并在 `RemoveTreeResult` 中返回已删除项和错误，不宣称原子回滚
- `rename` 验证 src/dest 均为 write capability，同时持有两个 parent handles，并在同一文件系统执行原子 rename；初始版本遇到 `EXDEV` 返回 `cross_filesystem_move_unsupported`，不自动 copy+delete
- Windows 每次打开都拒绝 reparse point，并根据 handle final path 校验根；平台无法表达这些约束时 workspace 外操作 fail closed
- capability 与根/目标 handle 从最终校验到操作完成保持打开；工具不得在 SafePathExecutor 外自行创建父目录、删除或覆盖目标


## Runtime Data Model

### PermissionService

授权数据结构与结果类型放在独立模块 `permission/grants.py`，避免 PermissionService、ToolContext 与 Settings 之间形成循环依赖。

```text
PermissionService
├── runtime_grants: AccessGrants
├── session_grants: MutableAccessGrants
├── persistent_grants: MutableAccessGrants
├── state_revision: int
├── permissions_revision: int
├── permission_state_ready: bool
├── revocation_gate: PermissionEpochGate
├── grant_locks: PathGrantLockManager
├── commit_lock: asyncio.Lock
├── persist_grant_delta: PersistGrantDeltaCallback | None
├── get_effective_grants() -> EffectiveAccessGrants
├── create_permission_context() -> PermissionContext
├── acquire_grant_targets(targets) -> AsyncContextManager[None]
├── async add_grant(delta, precondition, persist) -> GrantUpdateResult
├── async update_permissions(mutation, expected_permissions_revision) -> PermissionUpdateResult
├── apply_committed_permissions(result) -> None
└── clear_session_permissions()
```

```python
@dataclass(frozen=True)
class GrantDelta:
    path: str
    access: Literal["read", "write"]
    is_dir: bool


@dataclass(frozen=True)
class ApprovalPrecondition:
    permission_mode: PermissionMode
    revocation_epoch: int


@dataclass(frozen=True)
class PersistentPermissionSnapshot:
    permission_mode: PermissionMode
    sandbox_mode: SandboxMode
    approval_policy: ApprovalPolicy
    approval_reviewer: ApprovalReviewer
    grants: AccessGrants
    permissions_revision: int


@dataclass(frozen=True)
class PermissionPatch:
    permission_mode: PermissionMode | None = None
    sandbox_mode: SandboxMode | None = None
    approval_policy: ApprovalPolicy | None = None
    approval_reviewer: ApprovalReviewer | None = None
    grants: AccessGrants | None = None


PermissionMutation = GrantDelta | PermissionPatch


@dataclass(frozen=True)
class PermissionCommitResult:
    committed: bool
    durable: bool
    conflict: bool
    snapshot: PersistentPermissionSnapshot | None
    latest_snapshot: PersistentPermissionSnapshot | None = None
    warning: str = ""
    error: str = ""


@dataclass(frozen=True)
class PermissionUpdateResult:
    ok: bool
    committed: bool
    durable: bool
    applied: bool
    conflict: bool
    restart_required: bool
    persistent_snapshot: PersistentPermissionSnapshot | None
    latest_snapshot: PersistentPermissionSnapshot | None
    state_revision: int
    permissions_revision: int
    warning: str = ""
    error: str = ""


PersistGrantDeltaCallback = Callable[
    [GrantDelta],
    Awaitable[PermissionCommitResult],
]
```

两个 revision 属于不同版本域，禁止相互比较或赋值：

- `state_revision`：仅用于当前 PermissionService 的内存快照；任一 session/persistent grant 发布、清除或模式切换后递增
- `permissions_revision`：仅镜像 settings 文件中的持久化权限版本；只由权限事务返回的新值更新
- `revocation_epoch`：由 `PermissionEpochGate` 持有；撤销、清除或切换模式时递增，用于使已打开审批和子代理快照失效

三个结果类型使用同一字段语义，但 `GrantUpdateResult` 额外声明 `persistent: bool`，从而区分 session 与 persistent grant；调用方不得自行重新解释状态。

结果类型遵守以下状态不变量：

- `PermissionCommitResult.committed=False` 时 `snapshot=None`；`conflict=True` 时 `latest_snapshot` 必须非空，表示未写磁盘并携带当前持久化版本
- `PermissionCommitResult.committed=True` 时 `snapshot` 必须是从目标文件确认的 committed snapshot，`latest_snapshot=None`；`durable=False` 只表示父目录 fsync 或等价耐久性未确认，不表示已回滚
- 完整权限 patch 成功时：`PermissionUpdateResult(ok=True, committed=True, applied=True, conflict=False, restart_required=False)`，且 `persistent_snapshot` 非空
- 完整权限 patch 冲突时：`committed=False, applied=False, conflict=True, restart_required=False`，且 `latest_snapshot` 非空
- 磁盘已提交但 runtime 发布与恢复均失败时：`ok=False, committed=True, applied=False, conflict=False, restart_required=True`，且 `permission_state_ready=False`
- session grant 成功时不涉及磁盘：`GrantUpdateResult(persistent=False, ok=True, committed=True, durable=None, applied=True, conflict=False, restart_required=False, persistent_snapshot=None)`；此处 `committed=True` 表示 session grant 已在 PermissionService 的 `commit_lock` 线性化点发布
- persistent grant 成功时：`persistent=True, committed=True, applied=True`，`durable` 为 `True/False`，且 `persistent_snapshot` 非空
- grant 的 precondition 失败时：`committed=False, applied=False, conflict=False`，错误码为 `permission_context_changed`
- `state_revision` 只来自 PermissionService 发布结果；`permissions_revision` 只来自 committed persistent snapshot；session grant 不改变 `permissions_revision`

`PermissionEpochGate` 是主代理、子代理和所有外部路径工具共享的撤销屏障。它提供 shared execution lease 与 exclusive revocation lease；外部路径工具必须从最终授权复查一直持有 execution lease 到文件系统副作用或子进程结束。

`permission_state_ready` 是 PermissionService 拥有的权威运行时状态，所有外部路径授权入口统一执行 readiness gate：

1. `PermissionService.create_permission_context()` 是 Engine 获取授权快照的唯一入口，签名始终为 `() -> PermissionContext`，不抛权限状态异常，也不直接返回 Engine 决策。`permission_state_ready=False` 时返回脱敏 context：`access_grants.permission_state_ready=False`，session/persistent user grants 为空，并设置 `denial_reason="permission_state_unavailable"`。Engine 看到该字段后统一映射为 `DENY(permission_state_unavailable)`。
2. `PermissionService.get_effective_grants()` 在 not-ready 状态返回 `permission_state_ready=False` 且 user session/persistent grants 为空的快照；ToolContext getter 不得缓存或绕过该状态。
3. `acquire_execution_lease(...)` 在 not-ready 状态拒绝创建 token；已有 token 在 PermissionService 进入 not-ready 恢复状态时按 revocation 流程取消或等待结束。
4. `resolve_access(...)` 必须首先检查 `grants.permission_state_ready`；为 False 时对所有 workspace 外路径返回拒绝，即使快照中意外残留 grant。
5. runtime/session/persistent 内置对象不得直接暴露给 Engine 或工具；只能经上述 snapshot、context 与 lease API 访问。

`clear_session_permissions()` 在 `commit_lock` 与 revocation gate 的独占阶段中：

- 清除工具级 session allow/deny
- 清除 `session_grants` 中的四类路径授权
- 增加 `state_revision` 与 `revocation_epoch`

`ApprovalPrecondition` 的唯一权威校验者是 PermissionService，Settings 不接收也不知道 runtime epoch：

- `add_grant(..., persist=False)` 在 `commit_lock` 内检查 permission mode、`revocation_gate.pending` 与 `revocation_epoch`；通过后基于最新 session grants 合并 delta并增加 `state_revision`
- `add_grant(..., persist=True)` 在 `commit_lock` 内执行相同校验；通过后调用只接收 `GrantDelta` 的持久化回调
- Settings 在自己的事务锁内只校验磁盘 JSON、持久化权限约束与 `permissions_revision`，并把 delta 合并到最新磁盘 snapshot；不得校验或缓存 runtime epoch
- PermissionService 持有 `commit_lock` 跨越 Settings 事务，因此 mode/epoch 的 runtime 发布不能在校验与磁盘提交之间插入；若 revocation 已标记 pending，提交前置条件直接失败
- Settings 返回 `PermissionCommitResult` 后，PermissionService 在同一 `commit_lock` 临界区通过 `apply_committed_permissions(result)` 发布 persistent grants、更新 `permissions_revision` 并增加 `state_revision`

若 revocation 在 PermissionService 已持有 `commit_lock` 后才标记 pending，则当前 grant 提交在线性化顺序上先发生；pending 会阻止新的 execution lease，随后的撤销事务取得 `commit_lock` 后必须移除或收紧该 grant。因此旧审批不能在已提交的模式切换之后重新授权，也不能在撤销 pending 期间被工具使用。

`build_permission_service` 必须接收 `Settings | None` 或等价的持久化回调。Graph 已持有 `self._settings`，创建和刷新 PermissionService 时都要传入；没有 Settings 的运行环境只能使用 session grants，选择持久化时返回明确错误。

### 授权并发与路径层级锁

路径审批不能使用单一全局锁，否则两个不相交文件的用户确认会被无谓串行化。锁定对象必须是**用户最终选择的授权范围**，而不只是原始操作路径：

```python
def grant_targets_conflict(a: GrantTarget, b: GrantTarget) -> bool:
    return (
        a.path == b.path
        or (a.is_dir and b.path.is_relative_to(a.path))
        or (b.is_dir and a.path.is_relative_to(b.path))
    )
```

因此：

- 同一文件、同一目录或文件/目录同路径互斥
- 目录与任意深度的子目录或子文件互斥
- 两个不同文件、兄弟目录、兄弟目录下的不同文件可以并发审批
- 文件目标不作为其他路径的祖先；对象类型与文件系统状态冲突时规范化阶段直接拒绝
- move 等多路径操作最终通过一次 `acquire_many` 原子获取整组授权范围锁；锁管理器对有冲突的请求保持 FIFO

由于用户可能从“此文件”改选“父目录”，审批采用两阶段协议，禁止持锁升级：

1. **请求阶段**：对原始操作目标获取窄 request lock；同一目标不会重复弹窗，不相交 sibling 文件可以并发交互。
2. 用户选择后释放全部 request locks，得到最终 `GrantTarget` 集合；目录选项必须转换为真实 parent directory target。
3. **提交阶段**：通过单次 `acquire_many(final_targets)` 获取最终授权范围锁。不得在持有子文件锁时升级为父目录锁，避免两个 sibling 同时升级造成死锁。
4. 获取 final locks 后重新验证路径、permission mode、`revocation_epoch` 和 effective grants。若另一审批已授予足够权限，则跳过提交；否则使用原用户选择提交 delta。
5. final locks 覆盖 grant 提交和提交后二次检查；释放 final locks 前必须先进入 execution lease context，并在 lease 内完成最后一次授权复查。

因此，两个 sibling 文件可以同时等待用户；若两者都选择同一父目录，最终目录授权提交严格串行，第二个请求复查后不会重复写入。用户选择到 final lock 之间没有授权或副作用，重新规范化失败或 precondition 失效时必须重新审批，不能沿用旧选择。

层级锁只控制授权范围冲突；`PermissionEpochGate` 控制授权检查到副作用之间的撤销竞态。唯一允许的锁序列是：

- grant 提交：`final grant locks -> PermissionService.commit_lock -> settings transaction lock`
- 普通工具执行：`final grant locks（如本次发生审批） -> execution lease`；获取 lease 并在 lease 内重新检查授权后释放 final locks，继续持有 lease 到副作用完成
- 已有授权的普通工具：直接获取 execution lease，在 lease 内读取最新 snapshot 并重新检查后执行
- 撤销/clear/模式收紧：先通过 `revocation_gate.begin_revocation()` 标记 pending，阻止新 execution lease；**不持有 commit/settings/grant lock** 地取消或等待活动 lease；归零后获取 exclusive revocation lease，再按 `commit_lock -> settings transaction lock` 提交变更并增加 epoch

禁止在等待活动 execution lease 时持有 `commit_lock`、settings lock 或 grant lock。审批提交在 `commit_lock` 内看到 revocation pending、mode 或 epoch 变化时必须返回 `permission_context_changed`。该顺序消除“撤销等待工具、工具等待 commit lock”的锁环。

切换到任一非 CUSTOM 权限模式时：

- 保留 runtime grants
- 清空 session grants 和工具级 session allow/deny
- 清空 live PermissionService 的 persistent grants
- 从 settings 删除四个 canonical grant 字段

这与现有切换 preset 时删除 `sandbox_workspace_write` 的行为一致，避免自定义路径授权在预设模式下静默残留。切回 CUSTOM 不自动恢复已删除的授权。

### ToolContext

ToolContext 不保存需要与 PermissionService 共享引用的四个 list。新增回调：

```python
@dataclass(frozen=True)
class GrantUpdateResult:
    persistent: bool
    ok: bool
    committed: bool = False
    durable: bool | None = None
    applied: bool = False
    conflict: bool = False
    restart_required: bool = False
    persistent_snapshot: PersistentPermissionSnapshot | None = None
    latest_snapshot: PersistentPermissionSnapshot | None = None
    state_revision: int = 0
    permissions_revision: int = 0
    grants: AccessGrants | None = None
    warning: str = ""
    error: str = ""


class ExecutionLeaseToken(Protocol):
    expected_epoch: int
    @property
    def active(self) -> bool: ...


GetAccessGrantsCallback = Callable[[], EffectiveAccessGrants]
AcquireGrantTargetsCallback = Callable[
    [tuple[GrantTarget, ...]],
    AbstractAsyncContextManager[None],
]
AcquireExecutionLeaseCallback = Callable[
    [int],
    AbstractAsyncContextManager[ExecutionLeaseToken],
]
AddGrantCallback = Callable[
    [GrantDelta, ApprovalPrecondition, bool],
    Awaitable[GrantUpdateResult],
]

class ToolContext(BaseModel):
    get_access_grants: GetAccessGrantsCallback | None
    acquire_grant_targets: AcquireGrantTargetsCallback | None
    acquire_execution_lease: AcquireExecutionLeaseCallback | None
    add_grant: AddGrantCallback | None
```

路径审批由共享 `ensure_path_access(...)` helper 执行。helper 实现 request-lock/交互/final-lock 两阶段协议，并在用户交互前捕获 `ApprovalPrecondition`。`ctx.add_grant(...)` 必须在提交点重新验证 mode 与 epoch；precondition 失效返回稳定错误 `permission_context_changed`，工具不得产生副作用，并可提示用户重新发起操作。

只有返回 `ok=True, committed=True, applied=True` 且 final lock 内二次授权检查成功才能继续；session grant 使用上述 runtime-commit 语义，persistent grant 同时要求磁盘 committed。随后工具使用 snapshot 的 `revocation_epoch` 获取 execution lease context manager；进入 context 后得到不可伪造且仅在该 context 内 active 的 `ExecutionLeaseToken`。工具在 token 存活期间再次读取 snapshot 并检查路径，成功后可释放 final locks，但 token/context 必须保持到 `SafePathExecutor` 或受控子进程完成。已有授权且不需要审批的调用也必须执行同样的 lease + recheck 流程。持久化 durability 警告必须返回用户，但不能把已提交磁盘状态伪装成回滚失败。

`add_extra_path` 与 `sandbox_extra_paths` 直接删除，所有调用点迁移到 `get_access_grants` / `acquire_grant_targets` / `add_grant` callbacks。

### PermissionContext

PermissionContext 使用不可变授权快照：

```text
PermissionContext
├── access_grants: EffectiveAccessGrants
└── denial_reason: str = ""
```

`create_permission_context()` 始终返回该类型。ready 状态下 `denial_reason` 为空；not-ready 状态下返回不含 user grants 的脱敏 snapshot 和 `denial_reason="permission_state_unavailable"`。只有 Engine 将该字段转换为 DENY，PermissionService 不返回 Engine 枚举，也不通过异常表达正常拒绝。

Engine 每轮授权时从 PermissionService 创建新快照，不依赖可变对象共享。

### 子代理

创建子代理时构造不可变 `SubagentPermissionSnapshot`，包含 effective grants、sandbox mode、工具级 session 决策和 expected `revocation_epoch`。主代理和子代理使用同一个 `PermissionEpochGate` 协议；区别仅在于子代理的 expected epoch 与 grants 固定在创建时：

```python
class PermissionEpochToken(Protocol):
    expected_epoch: int
    def is_current(self) -> bool: ...
    def acquire_execution_lease(self) -> AbstractAsyncContextManager[ExecutionLeaseToken]: ...
```

- 子代理 `authorize_tools` 使用 snapshot 内 grants，不回调父图 live PermissionService
- 子代理 `ToolContext.get_access_grants` 固定返回同一 snapshot；不注入 grant lock、add grant 或交互回调，但必须注入绑定 expected epoch 的 execution lease callback
- 每个子代理 tool call 在 Engine 授权前获取 execution lease，并持有到工具或子进程完成；获取时 epoch 不匹配则 fail closed
- 撤销、clear 或模式切换先阻止新 lease，再取消/等待活动 lease，最后在 exclusive revocation lease 内提交新权限状态；因此撤销不能发生在 Engine 检查与副作用之间
- 父会话新增 grant 不增加 revocation epoch，不传播到已创建子代理；撤销或模式收紧会使旧 snapshot 永久失效
- 长运行子进程在撤销时由 runtime guard 请求取消；若无法安全取消，撤销事务等待其 execution lease 释放后再提交

若未来支持子代理交互，必须由父会话创建新审批和新 snapshot，不能修改运行中的 snapshot。

## Access Resolution

### API

```python
def resolve_access(
    workspace: str,
    file_path: str,
    access: Literal["read", "write"],
    grants: EffectiveAccessGrants,
    *,
    kind: Literal["file", "dir", "unknown"],
    allow_missing: bool = False,
) -> AccessIntent | None:
    """规范化路径、选择授权根并返回不可执行的访问意图。"""
```

```python
@dataclass(frozen=True)
class AccessIntent:
    normalized_path: str
    grant_root: str
    relative_parts: tuple[str, ...]
    access: Literal["read", "write"]
    kind: Literal["file", "dir", "unknown"]
    allow_missing: bool
    revocation_epoch: int
```

- read 默认要求目标存在；工具仍负责检查文件/目录类型
- write 可传 `allow_missing=True`，允许创建尚不存在的精确目标
- `grant_root` 是实际命中的 workspace、file grant 或 directory grant 根；file grant 的 relative parts 为空
- 返回值只描述授权意图，不包含打开句柄，不能直接用于文件系统副作用
- `grants.permission_state_ready=False` 时，workspace 外路径无条件拒绝并返回稳定错误；不得读取其中的 user grants
- 函数只做规范化与授权判断，不发起 UI 交互

### 调用点迁移

`resolve_safe` 直接删除，不保留兼容包装。所有调用点（`tools/file/read.py`、`tools/file/write.py`、`tools/file/replace.py`、`tools/file/manage.py`、`tools/git.py`、`ui/session.py`）迁移到 `resolve_access`，显式传入 `access="read"` 或 `access="write"` 与 `ctx.get_access_grants()`。

## Engine and Tool Approval Flow

### Engine 检查结果

Engine 路径预检查从二态改为三态：

```text
ALLOW            路径在 workspace 内或已有授权
DEFER_TO_TOOL    外部路径缺少授权，但该工具已实现路径级审批
DENY             工具不支持路径审批、路径不可安全解析或模式禁止
```

`DEFER_TO_TOOL` 不等于授权，只允许工具执行到内部 `resolve_access` 与审批逻辑。工具在执行副作用前必须重新检查；未审批或审批失败时必须返回 error。

这样既保留 Engine 的前置防线，又不会让 hard deny 阻断工具内部审批。

### 路径访问描述

普通工具继续使用同步路径提取：

```python
@dataclass(frozen=True)
class PathAccess:
    path: str
    access: Literal["read", "write"]
    kind: Literal["file", "dir", "unknown"]
    allow_missing: bool = False


def path_accesses_for_tool(tool: str, args: dict) -> list[PathAccess]: ...
```

Engine 仅检查该函数明确返回的文件系统访问。没有路径参数的 webfetch、todo、document 等 READ_TOOLS 不参与文件路径检查。

Git 是两阶段例外，因为真实仓库根和元数据路径必须执行只读 discovery 后才能确定：

1. Engine 调用同步 `parse_git_command(args)`，从共享策略注册表得到 `GitStaticPlan`：read/write capability、用户输入 repo path、显式仓库外参数和是否需要运行时 discovery。
2. Engine 检查静态可见路径；若策略未知直接 DENY，若仍需发现 repo/metadata 路径则返回 DEFER_TO_TOOL，不能把静态检查结果当成完整授权。
3. Git 工具在无副作用的受控 discovery 环境中调用异步 `complete_git_access_plan(static_plan, ctx)`，生成完整 `GitRuntimePlan`。
4. 工具对 runtime plan 中所有路径完成授权与二次检查后才执行实际 Git 子命令。

共享类型、策略注册表和纯同步解析放在独立 `permission/git_policy.py`。`permission/rules.py` 与 `tools/git.py` 都依赖该模块；该模块不得反向导入 rules 或 GitTool，避免循环依赖。

### 工具审批与执行流程

1. Tool 将本次操作目标规范化为原始 `GrantTarget`；move 同时包含 src 与 dest，并捕获 `ApprovalPrecondition`。
2. 对缺失目标执行 request-lock/交互/final-lock 两阶段审批；并发审批已满足权限时跳过写入，precondition 失效时终止。
3. final locks 内进入基于当前 `revocation_epoch` 的 execution lease context，取得 `ExecutionLeaseToken`；随后重新读取 effective snapshot 并调用 `resolve_access` 生成 `AccessIntent`。无法获取 token、token inactive 或 epoch 改变则 fail closed。
4. 授权检查完成后释放 final locks，但继续保持 lease context 与 token active。对于调用开始时已有授权的路径，跳过审批并直接从本步骤开始。
5. 在 execution lease 内由 `SafePathExecutor.open_authorized(intent, lease_token)` 获取 capability，并执行读写、创建、删除或 rename；受控 Git/shell runner 必须接收同一 token，并在子进程退出前保持 context active。
6. 工具完成或失败后先关闭 capability/句柄，再释放 execution lease。撤销操作只有在所有活动 lease 结束后才能提交。

final grant locks 保证授权范围提交互斥；execution lease 保证授权检查到副作用完成期间不可撤销。二者职责不同，均不可省略。

### 审批选项

read：

```text
读取 workspace 外的文件: /path/to/file.txt
[本次会话读取此文件]
[本次会话读取此目录]
[持久化读取此文件]
[持久化读取此目录]
[拒绝]
```

write：

```text
写入 workspace 外的文件: /path/to/file.txt
[本次会话写入此文件]
[本次会话写入此目录]
[持久化写入此文件]
[持久化写入此目录]
[拒绝]
```

“本次会话”在当前 session 结束或清除 session permissions 后失效，不是单次工具调用。

## Tool-Specific Rules

| Tool / Operation | Path | Access | Approval Object |
|---|---|---|---|
| read | `file_path` | read | file 或 parent dir |
| grep | `path` | read | file/dir 按实际目标 |
| lsp | `file_path` | read | file 或 parent dir |
| write / replace | `file_path` | write | file 或 parent dir |
| manage create/delete | each path | write | file 或 parent dir |
| manage move | src | write | file 或 parent dir |
| manage move | dest | write | file 或 parent dir |
| git read operation | repository `path` | read | directory only |
| git write operation | repository `path` | write | directory only |
| bash / powershell | each external literal path | write | file/dir，见 shell 规则 |

### move

move 不等价于 read source + write destination。`shutil.move` 成功后源文件被删除，因此：

- src 要求 write
- dest 要求 write
- 两端分别审批
- 若任一端未授权，不得执行移动

未来若增加 copy 操作，copy 才使用 src=read、dest=write。

### Git

Git 的权限边界包括工作树、Git 元数据、对象库、索引和显式仓库外参数。规划拆为同步静态解析与异步运行时补全：

```python
@dataclass(frozen=True)
class GitStaticPlan:
    capability: Literal["read", "write"]
    input_repo_path: PathAccess
    explicit_paths: tuple[PathAccess, ...]
    policy_id: str
    needs_discovery: bool = True


@dataclass(frozen=True)
class GitRuntimePlan:
    static: GitStaticPlan
    worktree_root: PathAccess | None  # bare repo 为 None
    git_dir: PathAccess
    common_dir: PathAccess
    index_path: PathAccess | None
    object_dirs: tuple[PathAccess, ...]
    config_files: tuple[PathAccess, ...]
    explicit_paths: tuple[PathAccess, ...]
```

运行时补全不能先执行可能读取未授权元数据的 `git rev-parse`。采用授权感知的文件系统预检：

1. `parse_git_command(args)` 仅做纯同步解析，从策略注册表产生 `GitStaticPlan`；未知子命令或参数组合直接拒绝。
2. 先授权并规范化用户输入 repository path 和静态可见的 explicit paths。
3. workspace 内允许在 workspace 边界内向上定位仓库根；workspace 外要求 `path` 直接指向 worktree root 或 bare git dir，禁止为了搜索仓库根而读取未授权父目录。
4. 在已授权输入目录内检查 `.git`：目录形式直接作为 git dir；文件形式只读取该已授权文件并解析 `gitdir:` 指针。指针目标尚未授权时先审批，审批成功前不得读取目标目录。
5. 在已授权 git dir 内读取 `commondir`（若存在）。若其解析目标位于当前授权之外，先审批 common dir，再读取其中内容。
6. 从已授权 git dir/common dir 推导 index 与 objects 路径；读取 objects/info/alternates 前必须已授权该对象库。alternates 中的每个目标规范化后分别审批，无法可靠解析时直接拒绝。
7. 从已授权 `common_dir/config` 读取仓库级配置，但首次解析不跟随 include。`common_dir/config` 始终加入 `config_files`；linked worktree 不能错误地从 `git_dir/config` 代替它。
8. 从 `common_dir/config` 读取 `extensions.worktreeConfig`。仅当其为 true 时，将 `git_dir/config.worktree` 加入配置来源；文件存在时必须先获得 read grant 再读取，不存在则按 Git 的空 worktree 配置处理。未启用该扩展时不得读取 `config.worktree`。
9. 对 `common_dir/config`、可选的 `git_dir/config.worktree` 及其已授权 include 递归解析。先提取 `include.path` 与可静态求值的 `includeIf.*.path`，对每个新配置文件先审批、后读取；支持的条件和求值输入必须在策略中列举。动态条件、循环、深度超限或无法规范化的路径直接拒绝。
10. 对完整合并配置执行危险行为检查：shell alias、hooks、external diff/textconv、filter clean/smudge/process、credential helper、fsmonitor、editor、GPG/SSH command 等可能启动外部进程的配置必须由策略显式禁用，否则该命令组合 DENY。
11. 完整路径均授权后，在受控环境中先运行 `git config --show-origin --show-scope --null --list --includes`。只提取 `file:` origin 并规范化；每个实际来源都必须属于已授权的 `config_files` 或应用维护的空 global config。出现计划外来源、同一 origin 规范化到不一致目标或不可规范化的 origin 时立即拒绝。空文件、仅注释文件或未产生有效配置项的已授权文件可以不出现在 origin 输出中；缺失 origin 本身不构成失败。
12. 配置来源校验通过后运行 `rev-parse` 做元数据一致性验证；其 worktree/git-dir/common-dir/index/objects 输出必须与预检计划一致，否则拒绝执行。
13. 所有路径再次通过授权检查后，才使用同一受控环境执行实际 Git 子命令。

这样 discovery 只读取当前已授权范围内的入口文件；发现新的外部元数据路径时先审批、后访问。外部调用若传入仓库子目录，返回 `external_git_path_must_be_repo_root`，而不是越过授权边界搜索父目录。

授权规则：

- read-only Git 操作要求工作树、元数据、索引和对象库的 read grant；write grant 同样可满足
- write Git 操作要求可能被修改的工作树、元数据、索引、对象库和显式输出目标的 write grant
- linked worktree 的 `git_dir` 与 `common_dir` 必须分别检查；授权工作树或其子目录不能隐含授权位于其他位置的 Git 元数据
- 外部仓库的工作树与 Git 元数据目录只接受目录级授权；显式输出文件仍可使用文件级 write grant

Git 子进程不得继承影响路径解析或配置来源的任意 `GIT_*` 环境变量。`_run_process` 为 Git 构造最小环境：

- 从基础环境只保留运行 Git 所需的 PATH、locale 和平台必需变量
- 清除 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR`、`GIT_INDEX_FILE`、`GIT_OBJECT_DIRECTORY`、`GIT_ALTERNATE_OBJECT_DIRECTORIES`、`GIT_CONFIG_*` 及其他继承的 `GIT_*`
- 固定 `GIT_TERMINAL_PROMPT=0`；read discovery/operation 固定 `GIT_OPTIONAL_LOCKS=0`
- 固定 `GIT_CONFIG_NOSYSTEM=1`，并将 `GIT_CONFIG_GLOBAL` 指向 runtime grant 下由应用维护的空配置文件，禁止隐式读取用户或系统 Git 配置
- 所有 Git 调用固定 `core.hooksPath` 到 runtime grant 下的空目录；禁止执行仓库 hooks
- diff/show/log 等读取命令强制关闭 ext-diff 与 textconv；可能触发 filter、credential、editor、GPG、SSH 或其他 helper 的命令，只有在策略证明相关执行路径已禁用时才允许
- `config_files` 必须包含 `common_dir/config`、启用 `extensions.worktreeConfig` 时的 `git_dir/config.worktree`，以及全部递归 include；每个外部文件均先授权后读取
- 受控 `git config --show-origin --show-scope --null --list --includes` 返回的每个规范化 `file:` origin 都必须包含在已授权计划中；空或仅注释的计划文件无需出现在输出中，`rev-parse` 不能替代配置来源验证
- 不允许调用参数重新注入 `--git-dir`、`--work-tree`、`--namespace`、`--config-env` 或改变对象库/索引/配置来源的等价选项

raw Git 必须 fail closed。策略注册表同时定义 capability、参数校验、显式路径提取和 discovery 需求：

- `repo_only` 策略：只允许明确不会访问计划外路径的子命令与参数组合
- `extra_path` 策略：明确提取路径及 access，例如 `worktree add/move`、`archive --output`、`format-patch -o/--output-directory`、`bundle create/list/verify`、`config --file`
- `config --global`、`config --system`、改变 Git 目录/工作树/对象数据库/索引的选项，以及没有策略定义的组合，在 workspace-write 模式下直接拒绝
- pathspec 必须限制在发现后的真实 worktree root 内；`--` 后路径不能逃逸工作树
- danger-full-access 可绕过 workspace 外路径授权，但仍使用受控 Git 环境并保留 destructive deny 规则

Engine、capability 分类和 Git execute 都调用 `permission/git_policy.py` 的 `parse_git_command`；只有 Git execute 调用异步 `complete_git_access_plan`。这样共享静态策略而不要求同步 Engine 执行 `rev-parse`，也避免 rules 与 GitTool 的循环依赖。

### LSP

当前 LSP 操作仅包含 diagnostics/definition/references/symbols，输入文件统一按 read 检查。LSP 返回值也可能引用其他文件，因此输出必须进行二次授权过滤：

- diagnostics 无 `file_path` 时，只返回路径仍命中 effective read grants 的已打开文档结果
- definition/references 返回的每个 `LspLocation.path` 都调用 `resolve_access(access="read")`；未授权位置从结果中移除
- document symbols 只返回已授权输入文件的符号
- workspace symbols 若未来通过工具暴露，必须逐条过滤 symbol path；当前工具不提供无路径 workspace symbols
- 若过滤后为空，返回“没有可访问的结果”，不得泄露未授权路径名称、数量或位置

过滤逻辑放在 Tool/LspService 边界，并接收本次调用的 effective grants；不能只依赖 LspManager 的 workspace，因为语言服务器可能返回 workspace 外位置。

## Shell Rules

shell 不能仅提取重定向或少数命令参数；解释器展开、隐式配置文件和子进程都可能访问未授权路径。行为按 sandbox mode 明确定义：

| Mode | Parser / Policy | Process Filesystem Sandbox | Path Access |
|---|---|---|---|
| read-only | 受限 grammar + read-only command policies | 必须启用；workspace/runtime 只读 | 任何写能力命令拒绝；外部用户 grants 不生效 |
| workspace-write | 受限 grammar + closed `ShellPolicyRegistry` | 必须启用 | workspace 按既有规则；外部路径统一要求 writable grant |
| danger-full-access | 保持现有通用 shell 语法兼容性 | 不施加本模型的文件系统限制 | 跳过外部路径 grant；仍保留 destructive deny、审批策略和进程超时 |

read-only 与 workspace-write 共用以下强制约束：

1. `ShellPolicyRegistry` 是允许命令的单一来源。每个策略声明 executable、允许参数、路径操作数、能力、管道规则和可能子进程；未登记命令直接 DENY。
2. bash 只接受简单 argv、受控 pipeline、显式重定向和开头受控 `cd`；禁止变量展开、命令替换、glob、brace expansion、here-doc、process substitution、subshell、函数、alias、`eval`、动态 source 和控制流。
3. PowerShell 使用等价受限 AST allowlist；script block、变量插值、provider path、module/profile 自动加载、子表达式和动态 invocation 默认拒绝。
4. 嵌套解释器只有存在专用递归策略时允许；初始版本拒绝 `sh -c`、`bash -c`、`python -c`、`node -e`、`powershell -Command` 等。
5. 命令不通过 `shell=True` 执行。parser 生成 argv/pipeline plan，由 runner 使用 `create_subprocess_exec` 或平台等价 API 启动。
6. 每个进程进入 deny-by-default 文件系统沙箱，仅开放本模式允许的 workspace、grants 与最小 runtime 路径；子进程继承相同或更严格限制。
7. 后端必须提供可验证限制（Linux Landlock/broker、macOS sandbox profile、Windows 文件 broker 等）。后端缺失或无法表达精确授权时 fail closed。
8. 沙箱拒绝转换为稳定错误，不向模型泄露未授权路径是否存在。

静态计划用于审批，OS 沙箱负责最终强制执行。danger-full-access 明确绕过本节的 closed grammar 与 filesystem sandbox，以保持现有模式定义，但不得绕过独立 destructive deny 规则。

## Persistence and Config Migration

### Canonical 配置字段

```text
sandbox_readable_files
sandbox_readable_dirs
sandbox_writable_files
sandbox_writable_dirs
```

不保留 `sandbox_workspace_write` 字段。

### 加载与写回规则

权限迁移只有一个同步底层入口 `_load_and_migrate_permissions_sync()`：`Settings.__init__` 与 `Settings.create` 都必须在暴露 `_data` 或构建 PermissionService 前调用它。异步更新 API 通过 `asyncio.to_thread`/等价执行同一同步文件事务原语；禁止仅在 async `create()` 中迁移，避免同步构造路径加载旧授权。

- 仅存在旧 `sandbox_workspace_write` 时，在同步 settings 事务中迁移为 `sandbox_writable_dirs`，写入 canonical 字段、增加 `permissions_revision` 并删除旧字段。
- 任一 canonical grant 字段与 legacy 字段同时存在时，canonical 集合整体优先；缺失的 canonical 字段按空集合处理，legacy 值不得合并或重新激活。事务删除 legacy 字段并记录 mixed-schema 警告。
- 迁移写入失败时不启用 legacy grant：应用继续以空用户 external grants 启动并报告明确警告，避免无法持久化的旧权限静默生效。
- 正常写回只写四个 canonical grant 字段和内部 `permissions_revision`，路径排序、去重并执行语义保持的冗余消除。
- Settings snapshot 返回四个 canonical 字段及 `permissions_revision`，不返回 legacy 字段。

运行时完整权限更新只能调用 PermissionService 拥有的统一入口，Gateway、slash 命令和其他调用方不得直接调用 Settings 权限事务：

```python
async def update_permissions(
    self,
    mutation: PermissionMutation,
    *,
    expected_permissions_revision: int | None,
) -> PermissionUpdateResult: ...
```

`PermissionService.update_permissions(...)` 的固定流程：

1. 在无锁状态读取当前权限并构造、校验 candidate，判断是否删除 grant、收紧 mode/sandbox/approval 或以其他方式要求 revocation。
2. 若需要 revocation，调用 `begin_revocation()` 阻止新 execution lease；不持有其他锁地取消或等待活动 lease，归零后进入 exclusive revocation lease。
3. 获取 `commit_lock`，重新读取当前状态与 expected `permissions_revision`；并发变化时返回 conflict，不沿用旧 candidate。
4. 在 `commit_lock` 内调用 Settings 的纯持久化事务；锁序固定为 `exclusive revocation lease（如需） -> commit_lock -> settings transaction lock`。
5. Settings 返回 committed snapshot 后，调用唯一发布原语 `apply_committed_permissions(result)`；成功后更新 mode、grants、`permissions_revision`、`state_revision`，需要撤销时再增加 epoch。
6. 释放 `commit_lock` 与 exclusive lease，最后允许新 execution lease。

纯加权/放宽且不撤销现有权限的完整 patch 可跳过 exclusive revocation lease，但仍必须经过 `commit_lock -> settings transaction lock -> apply_committed_permissions`。单个交互式 persistent `GrantDelta` 继续走 `add_grant`，但使用同一 Settings 事务与发布原语。

Settings API 必须先解析并校验完整 `permissions` patch，再调用上述 PermissionService 入口：

- grant 字段集合包括四个 canonical 字段；修改 mode 或 grants 的请求必须携带 snapshot 中的 `expected_permissions_revision`
- 若同一 patch 将 `permission_mode` 设置为任一非 CUSTOM preset，同时包含任一 grant 字段，则整体返回参数错误
- 若 patch 只切换到非 CUSTOM preset，则在 PermissionService `commit_lock` 与 revocation gate 独占阶段执行一次事务：删除四个 grant 字段、增加 `permissions_revision`，提交后清空 live 用户 grants，并增加 `state_revision` 与 `revocation_epoch`
- 若 patch 保持或切换到 CUSTOM，可在同一事务中更新 mode、sandbox、approval 与全部 grant 字段
- revision 不匹配时返回 conflict 和最新 snapshot，不覆盖并发提交
- 校验失败或提交点前失败时，permission mode、grant 配置和 live PermissionService 保持请求前状态

### 持久化写入与提交点

Settings 新增完整权限事务，而不是逐字段 setter：

```python
async def update_permissions_transaction(
    self,
    mutation: PermissionMutation,
    *,
    expected_permissions_revision: int | None,
) -> PermissionCommitResult: ...
```

单个 persistent grant 只向 Settings 传入 additive `GrantDelta`；runtime mode、revocation pending 与 epoch 已由 PermissionService 在外层 `commit_lock` 内权威校验。Settings 在最新磁盘状态上合并 delta，不携带也不比较 `state_revision` 或 runtime epoch。完整替换传入 `PermissionPatch` 并要求 `expected_permissions_revision`。两者共享按 settings 绝对路径索引的提交协调器；同步迁移使用其同步锁，async API 通过线程桥接调用同一文件事务。

事务顺序：

1. 获取 settings 提交锁，重新读取目标文件并校验 JSON 与当前 `permissions_revision`。Settings 不读取、接收或校验 `ApprovalPrecondition`、`revocation_epoch` 或 PermissionService 状态。
2. 在最新持久化数据副本上应用 `GrantDelta` 或 `PermissionPatch`，校验完整 persisted permissions 状态，生成新的 `permissions_revision`；不得读取、比较或覆盖 PermissionService 的 `state_revision`。
3. 在目标同目录创建权限为 owner-only 的临时文件，写入完整 JSON，flush 并 fsync 文件。
4. 使用 `os.replace` 替换目标文件；**成功的 replace 是提交点**。
5. fsync 父目录以确认目录项耐久性；用已提交 candidate 更新 `Settings._data` 与 effective cache，并返回 `PermissionCommitResult`。Settings 层不得持有或调用 PermissionService。
6. 提交点前失败：删除临时文件并返回 `PermissionCommitResult(committed=False, durable=False, conflict=False, snapshot=None, error=...)`，磁盘和 Settings 内存保持旧状态。revision/CAS 不匹配使用 `conflict=True`，同样不得写磁盘。
7. 提交点后失败（例如目录 fsync 或 Settings 内存刷新异常）：不能声称回滚。Settings 重新读取目标文件并返回 `PermissionCommitResult(committed=True, durable=False, conflict=False, snapshot=<disk snapshot>, warning=...)`。
8. **PermissionService 是自身 runtime 状态的唯一发布者**：`update_permissions` / `add_grant` 在持有 `commit_lock` 的临界区调用 Settings 事务，收到结果后仅通过 `PermissionService.apply_committed_permissions(result)` 发布 persistent grants、mode、`permissions_revision` 与 `state_revision`。Settings 不更新 PermissionService，PermissionService 也不再次合并 delta。
9. `apply_committed_permissions` 必须是无 I/O、无用户回调的确定性状态替换。若故障注入或异常导致首次发布失败，PermissionService 立即标记 `permission_state_ready=False`，使所有 workspace 外访问 fail closed；仍在 `commit_lock` 内从 Settings 重读 committed snapshot 并执行一次 `reconcile_committed_permissions(snapshot)`。
10. 重读恢复成功后原子设置 `permission_state_ready=True`，返回 `committed=True, applied=True` 和恢复警告；恢复仍失败则保持 fail-closed 状态，返回 `committed=True, applied=False, restart_required=True`，不得报告回滚成功。任何调用方都不得在 `permission_state_ready=False` 时使用旧 grants。
11. `build_permission_service` 启动时先设置 `permission_state_ready=False`，从 Settings 读取并校验 committed snapshot，构造全部 runtime/persistent 状态后才一次性切换为 True。读取或校验失败时保持 False 并报告 restart/repair required；成功重启必须恢复访问，而不是继承上次进程内的 fail-closed 标记。

该模型保证并发提交不丢失、提交点前可回滚；提交点后优先使磁盘与 live state 收敛，无法收敛时进入显式 fail-closed/restart-required 状态。不承诺进程在 replace 与目录 fsync 之间遭遇机器断电时具备已确认耐久性。

session grant 不写入 settings，但其最终发布仍经过 PermissionService 的短时 `commit_lock`。Settings API 和 preset 切换必须复用完整权限事务，不能逐字段调用 setter。

## UI Changes

权限审批需要封闭的五选一。交互能力由 `UserInteraction` 明确声明，helper 不再猜测或无条件追加选项：

```python
class UserInteraction(BaseModel):
    prompt: str
    options: list[str | tuple[str, str, str]] = Field(default_factory=list)
    timeout: float | None = None
    allow_other: bool = False
    selected: int = 0
    anchor: str = ""
```

完整调用链：

1. 工具构造 `UserInteraction`。
2. `_make_interact_callback` 读取 `allow_other`；仅在其为 True 时追加唯一的 “Other…” value，并在选中后 fallback 到 `ask_text`。
3. helper 将最终 choices、`selected`、`anchor` 传给 `app.ask_choice`。
4. `InteractionFrontend.ask_choice`、Gateway frontend 和 TUI 继续接收 choices/selected/anchor，不再自行追加 “Other…”。
5. `UiChoiceRequest` 与 `UiPermissionRequest` 新增并保存 `selected`、`anchor`；`allow_other` 只存在于 `UserInteraction` 层，由 helper 消费，避免远端 UI 重复生成选项。

调用规则：

- 权限审批显式使用 `allow_other=False`，只能选择四种授权或 deny
- clarify 与 checkpoint 需要自由修改时显式使用 `allow_other=True`
- 普通封闭选择保持默认 `allow_other=False`
- `selected` 必须落在选项范围内，越界时 clamp 到有效索引
- Esc、timeout 或 cancel 等价于 deny
- 权限审批不接受 `free_text=True` 的响应；即使异常前端返回自由文本也按 deny 处理

## Implementation Scope

| Path | Expected Change |
|---|---|
| `src/voidx/permission/grants.py` | 新增 grants/target/delta/precondition、PersistentPermissionSnapshot、PermissionPatch/Mutation、Commit/Update 结果类型及状态不变量 |
| `src/voidx/permission/path_locks.py` | 新增 PathGrantLockManager；实现 request/final 两阶段锁、目录祖先后代冲突、整组原子获取和 FIFO 等待 |
| `src/voidx/tools/base.py` | 新增 AccessIntent、resolve_access、审批 helper、execution lease 与 async grant/lock callbacks；删除旧接口 |
| `src/voidx/tools/file/safe_path.py` | 新增不导出构造器的 AuthorizedPath capability、issuer 校验、句柄生命周期、原子写、递归删除与同盘 rename |
| `src/voidx/permission/service.py` | runtime/session/persistent grants、permission_state_ready gate、opaque execution lease、统一 update_permissions、启动/发布恢复及唯一 runtime 发布逻辑 |
| `src/voidx/permission/context.py` | 增加 readiness、revision、epoch 的 immutable snapshot；not-ready 时禁止携带 user grants |
| `src/voidx/permission/sandbox.py` | 三态路径预检查与 file/dir 精确匹配；删除 best-effort shell 放行逻辑 |
| `src/voidx/permission/shell_policy.py` | 受限 shell/PowerShell grammar、命令策略注册表和静态访问计划 |
| `src/voidx/permission/process_sandbox.py` | Linux/macOS/Windows 文件系统沙箱后端与能力检测；不可用时 fail closed |
| `src/voidx/permission/git_policy.py` | 共享 GitStaticPlan/GitRuntimePlan 类型、策略注册表、同步 parse_git_command 和异步计划补全辅助契约 |
| `src/voidx/permission/rules.py` | 新增 `path_accesses_for_tool`；使用 parse_git_command 做 Git 静态分类；覆盖 grep 与 manage 双路径 |
| `src/voidx/permission/engine.py` | 处理 ALLOW/DEFER_TO_TOOL/DENY，不按 READ_TOOLS 粗粒度检查 |
| `src/voidx/tools/file/read.py` | read 路径审批、封闭选择和二次检查 |
| `src/voidx/tools/file/write.py` | write 路径审批；支持不存在的精确目标 |
| `src/voidx/tools/file/replace.py` | write 路径审批与二次检查 |
| `src/voidx/tools/file/manage.py` | create/delete=write；move src/dest 均为 write |
| `src/voidx/tools/search.py` | grep path=read |
| `src/voidx/tools/lsp.py` | 输入路径 read 检查；将 effective grants 传给输出过滤层 |
| `src/voidx/lsp/service.py` | 过滤 diagnostics/symbols/definition/references 中未授权路径 |
| `src/voidx/tools/git.py` | 异步补全 GitRuntimePlan；发现 worktree/git-dir/common-dir/index/object dirs；隔离 GIT_* 环境；授权全部路径后执行 |
| `src/voidx/tools/bash/safety.py`、`src/voidx/tools/bash/*` | 受限 grammar 解析、策略计划、exec runner 与进程沙箱；外部路径统一按 write |
| `src/voidx/tools/powershell/sandbox.py`、`src/voidx/tools/powershell/*` | 与 bash 相同的保守外部路径规则 |
| `src/voidx/agent/graph/tool_executor/executor.py` | 注入 get/add grant callbacks，不共享 list 引用 |
| `src/voidx/agent/graph/tool_executor/helpers.py` | 按 UserInteraction.allow_other 条件追加 Other；透传 selected/anchor |
| `src/voidx/agent/graph/subagent.py` | 注入固定 snapshot 与 PermissionEpochToken；每个 tool call 持有 authorization-to-execution lease |
| `src/voidx/agent/graph/wiring.py` | 接收 Settings/持久化回调；构建 runtime 与 persistent grants |
| `src/voidx/agent/graph/core/voidx_graph.py` | 创建与刷新 PermissionService 时传入 `self._settings` |
| `src/voidx/config/models.py` | 新增四个 canonical 配置字段，删除 `sandbox_workspace_write` |
| `src/voidx/config/settings.py` | 新增同步统一迁移入口、线程桥接、按路径锁、advisory lock、fsync/replace/目录 fsync 与恢复原语 |
| `src/voidx/config/settings_permissions.py` | 完整 permissions 事务、delta 合并、revision/CAS、mixed-schema 迁移和 preset 清理规则 |
| `src/voidx/ui/gateway/session/method/settings.py` | 预校验 patch 后只调用 PermissionService.update_permissions；不得直接调用 Settings 权限 setter/事务 |
| `src/voidx/ui/protocol/requests.py` | UiChoiceRequest/UiPermissionRequest 新增 selected/anchor |
| `src/voidx/ui/output/types.py` | 保持 ask_choice 的 choices/selected/anchor 契约，不在前端生成 Other |
| `src/voidx/ui/gateway/frontend.py` | 将 selected/anchor 写入 request DTO |
| `tui/voidx_cli/choice_mixin.py` | 继续渲染 helper 已生成的最终 choices，并应用 selected/anchor |
| `src/voidx/tools/clarify.py`、`src/voidx/tools/checkpoint.py` | 需要自由输入的 tuple 选择显式设置 allow_other=True |
| `src/tests/test_tools/file/test_safe_path.py` | 新建 capability、句柄竞态、delete/rename/EXDEV 与 Windows reparse 安全测试 |
| `src/tests/test_permission/test_epoch_gate.py` | 新建主/子代理 execution lease、forged/cross-gate/inactive/epoch-mismatch token、撤销等待、锁序与 fail-closed 发布恢复测试 |
| `scripts/check_permission_backend.py` | 新增平台沙箱能力探测；`--require` 不满足时退出非零 |
| `.github/workflows/permission-security.yml` | 新增 Linux/macOS/Windows 安全矩阵，运行能力探测和平台 containment 集成测试 |

## Invariants

- workspace 内行为不变
- read-only 永远阻止写操作
- danger-full-access 跳过外部路径限制，但不跳过既有 destructive deny 规则
- glob 始终限制在 workspace 内
- read grant 不允许任何写工具产生副作用
- write grant 隐含同路径 read
- move 的 src 与 dest 都要求 write
- 最终授权范围相同或存在目录祖先/后代关系时互斥；不相交文件与目录可以并发；锁升级必须使用释放 request lock 后原子获取 final locks 的两阶段协议
- `state_revision` 与 `permissions_revision` 是独立版本域；persistent delta 与完整 Settings 权限事务共享提交协调器，不能丢失并发更新
- 所有主代理/子代理外部路径工具必须持有 execution lease，从最终授权复查覆盖到 SafePathExecutor 或子进程完成
- 工具内部路径审批必须在副作用前完成二次检查，workspace 外副作用必须由不可伪造 AuthorizedPath capability 约束最终落点
- Engine 的 DEFER_TO_TOOL 不能被解释为路径已授权
- 不存在 `resolve_safe`、`add_extra_path`、`sandbox_extra_paths`、`sandbox_workspace_write` 兼容接口
- Settings 事务只发布 Settings 状态；所有运行时完整权限更新由 PermissionService.update_permissions 编排，PermissionService 是 runtime 权限状态与 permission_state_ready 的唯一发布者
- session grant 不落盘，其 committed 表示 runtime 线性化发布；persistent grant 的 committed 表示磁盘提交，二者都必须 applied=True 才能继续工具执行
- 子代理 Engine 与 ToolContext 使用同一创建时快照；每个工具调用持有 epoch execution lease，撤销或模式切换不得与已授权副作用并发
- 非 CUSTOM 模式不存在用户 persistent/session path grants
- Git 必须校验 worktree root、git dir、common dir、index 和所有对象库；授权工作树不能隐含授权外置元数据
- Git 子进程使用清理后的受控环境，不能继承或由参数注入改变路径/配置来源的 `GIT_*` 语义
- Git 未登记的 raw 子命令或参数组合在 workspace-write 模式下 fail closed
- read-only/workspace-write shell 只有受限 grammar、已登记策略和进程沙箱三者同时满足才可执行；danger-full-access 保持通用语法但仍受 destructive deny
- LSP 输出不得包含未授权路径或可推断该路径存在的信息
- 权限审批不提供 Other/free-text 路径

## Failure Paths and Test Coverage

| Case | Expected Behavior | Test |
|---|---|---|
| read 文件后 write 同文件 | write 仍需审批 | `test_read_grant_does_not_allow_write` |
| read 文件后 read sibling | sibling 仍需审批 | `test_file_grant_does_not_cover_sibling` |
| read 目录后 read child | 放行 | `test_read_dir_grant_covers_child` |
| write 文件后 read 同文件 | 放行 | `test_write_grant_implies_read` |
| move src 只有 read grant | 阻止并要求 src write | `test_move_source_requires_write` |
| move src/dest 均有 write | 放行 | `test_move_cross_write_grants` |
| write 创建不存在文件 | 可对精确目标审批并创建 | `test_write_missing_external_target` |
| Engine 遇到可审批外部 read | DEFER_TO_TOOL，不 hard deny | `test_engine_defers_approvable_read` |
| Engine 遇到未迁移工具外部路径 | deny | `test_engine_denies_non_approvable_tool` |
| tool 收到 DEFER 但用户拒绝 | 无副作用 | `test_deferred_path_denied_by_user` |
| Pydantic 复制上下文字段 | 新 grant 仍可通过 callback 获取 | `test_context_grants_are_refreshed` |
| resolve_safe 已删除，调用点使用 resolve_access | 无兼容接口残留 | `test_resolve_safe_removed` |
| bash `cat /etc/hosts` | 无 writable grant 时阻止 | `test_bash_external_read_requires_write` |
| bash 动态外部路径 | 阻止并提示显式路径/full access | `test_bash_dynamic_external_path_denied` |
| PowerShell 外部只读路径 | 无 writable grant 时阻止 | `test_powershell_external_read_requires_write` |
| 外部 Git path 指向仓库子目录 | 不向上搜索，返回 `external_git_path_must_be_repo_root` | `test_git_external_path_must_be_repo_root` |
| linked worktree 的 common dir 位于未授权位置 | 阻止并要求授权 common dir | `test_git_requires_linked_worktree_common_dir` |
| linked worktree 仅读取 `common_dir/config` | 不以 `git_dir/config` 代替仓库级配置 | `test_git_reads_common_dir_config` |
| `extensions.worktreeConfig=true` | 授权并读取 `git_dir/config.worktree` | `test_git_reads_worktree_config_when_enabled` |
| 未启用 `extensions.worktreeConfig` | 不读取 `git_dir/config.worktree` | `test_git_ignores_worktree_config_when_disabled` |
| Git 对象库 alternates 指向未授权目录 | 阻止；无法解析 alternates 时 fail closed | `test_git_requires_alternate_object_dirs` |
| 进程环境含 `GIT_DIR`/`GIT_OBJECT_DIRECTORY` | discovery 与执行均清除继承值 | `test_git_sanitizes_path_environment` |
| 本地 config include 指向未授权外部文件 | 先审批配置文件，未授权不得读取 | `test_git_config_include_requires_grant` |
| config include 条件动态、循环或无法解析 | fail closed，不启动 Git | `test_git_rejects_unsafe_config_include` |
| `git config --show-origin` 返回计划外来源 | 严格集合比较失败并拒绝执行 | `test_git_rejects_unplanned_config_origin` |
| 空 global/config.worktree/include 文件不产生 origin | 允许缺失，但所有实际 origin 仍必须已授权 | `test_git_allows_empty_planned_config_without_origin` |
| 本地 config 含 hooks/helper/filter/ext-diff 等隐式执行项 | 策略未显式禁用时拒绝命令 | `test_git_denies_implicit_executable_config` |
| Git 参数含 `--git-dir`/`--work-tree`/`--config-env` | workspace-write 下直接拒绝 | `test_git_denies_path_override_options` |
| Git `config --global/--system` | workspace-write 下直接拒绝 | `test_git_denies_global_and_system_config` |
| Git `archive --output` 指向外部文件 | 单独要求目标 write grant | `test_git_archive_output_requires_grant` |
| Git `worktree add` 指向外部目录 | 单独要求目标 write grant | `test_git_worktree_target_requires_grant` |
| Git 未登记 raw 参数组合 | fail closed，不执行 Git | `test_git_unknown_raw_policy_denied` |
| 同一文件同时审批 | 后一个等待；锁内复查后不重复提示 | `test_grant_lock_serializes_same_file` |
| 父目录与子文件同时审批 | 层级互斥，按 FIFO 完成 | `test_grant_lock_serializes_directory_and_child` |
| 两个 sibling 文件同时选择同一父目录 | request 阶段可并行；final 目录锁串行且只提交一次 | `test_grant_lock_sibling_upgrade_to_parent` |
| 兄弟文件/目录同时审批 | 可并发等待用户响应 | `test_grant_lock_allows_disjoint_targets` |
| move 两端与另一目录审批重叠 | acquire_many 原子等待且无死锁 | `test_grant_lock_acquire_many_no_deadlock` |
| 两个不相交 persistent grant 同时提交 | 两项均保留；permissions/state revision 各自单调且不混用 | `test_concurrent_persistent_grants_merge_latest` |
| session grant 与 persistent grant 并发 | state revision 不回退，磁盘 revision 只反映 persistent 提交 | `test_revision_domains_are_independent` |
| 等待用户时切换非 CUSTOM mode | precondition 失效，旧审批不能重新添加 grant | `test_pending_approval_invalidated_by_mode_change` |
| Settings 收到 persistent delta | 不接收/读取 runtime epoch；PermissionService 在 commit_lock 内完成权威校验 | `test_settings_transaction_has_no_runtime_epoch_dependency` |
| 权限结果状态组合 | session/persistent/conflict/fail-closed 非法组合无法构造或被拒绝 | `test_permission_result_state_invariants` |
| 主代理最终检查后尝试撤销 | 撤销等待 execution lease，副作用结束后才提交 | `test_main_tool_execution_lease_blocks_revocation` |
| 已授权主代理工具与撤销并发 | lease 内 recheck 后执行；撤销不能插入检查与副作用之间 | `test_pregranted_tool_holds_execution_lease` |
| Settings patch 使用过期 revision | 原子返回 conflict，不覆盖最新 grants | `test_permission_patch_rejects_stale_revision` |
| replace 前写入/fsync 失败 | committed=False，文件与 live state 不变 | `test_permission_transaction_precommit_failure` |
| replace 后目录 fsync 失败 | committed=True/durable=False，live state 从磁盘收敛 | `test_permission_transaction_postcommit_recovery` |
| 磁盘提交后 PermissionService 首次发布失败 | 重读并重试；再次失败则 external access fail closed 且要求重启 | `test_permission_publish_failure_enters_fail_closed_recovery` |
| permission_state_ready=False | Engine、ToolContext、lease 与 resolve_access 全部拒绝外部路径 | `test_permission_not_ready_blocks_all_authorization_entries` |
| 发布失败后成功重启 | 从 committed snapshot 恢复并设置 ready=True，外部授权重新生效 | `test_permission_service_restart_recovers_committed_snapshot` |
| 读取时将中间目录交换为 symlink | 从 no-follow 句柄读取或 fail closed | `test_safe_path_read_rejects_symlink_swap` |
| 写入时将中间目录交换为 symlink | SafePathExecutor fail closed，授权外无副作用 | `test_safe_path_rejects_symlink_swap` |
| 不存在目标的祖先在创建期间变化 | 句柄式创建失败，不落到新目标 | `test_safe_path_missing_target_parent_race` |
| forged / cross-gate / inactive / epoch-mismatch ExecutionLeaseToken | lease 或 SafePathExecutor 拒绝，无文件系统访问 | `test_execution_lease_token_is_unforgeable` |
| 手工构造或跨 executor 使用 AuthorizedPath | issuer 校验失败，无文件系统访问 | `test_authorized_path_is_unforgeable` |
| delete/recursive delete 遇到 symlink | 不跟随链接；部分失败返回 RemoveTreeResult | `test_safe_path_delete_does_not_follow_links` |
| 同盘 move | 同时使用两个 parent handles 原子 rename | `test_safe_path_rename_uses_authorized_handles` |
| 跨文件系统 move | 无 copy/delete 副作用，返回稳定 EXDEV 错误 | `test_safe_path_rejects_cross_filesystem_move` |
| Windows reparse point | 拒绝打开且最终 handle path 不越过授权根 | `test_safe_path_rejects_windows_reparse_point` |
| shell 未登记命令或动态语法 | 在启动进程前拒绝 | `test_shell_closed_policy_denies_unknown_and_dynamic` |
| shell 子进程尝试访问未授权路径 | OS 沙箱阻止且不泄露存在性 | `test_shell_sandbox_contains_child_process` |
| shell 平台沙箱后端不可用 | read-only/workspace-write 下 fail closed | `test_shell_requires_process_sandbox_backend` |
| read-only shell 含写能力命令 | 启动前拒绝 | `test_shell_read_only_denies_write_capability` |
| danger-full-access 使用动态 shell 语法 | 不应用路径模型/closed grammar，但保留 destructive deny | `test_shell_full_access_mode_matrix` |
| LSP definition/reference 返回未授权位置 | 过滤该位置且不泄露路径 | `test_lsp_filters_external_locations` |
| 无路径 diagnostics 包含失效授权文件 | 过滤该文件诊断 | `test_lsp_filters_ungranted_open_documents` |
| session grant 后 clear | 授权消失 | `test_clear_session_grants` |
| persistent grant 在 replace 前保存失败 | 文件、Settings._data 和 PermissionService 都保持原值 | `test_persistent_grant_save_failure_rolls_back` |
| 原子替换成功 | 四个 canonical 字段同时更新 | `test_replace_access_grants_atomic_success` |
| sync `Settings(...)` 加载旧字段 | 在暴露配置前完成事务迁移 | `test_sync_settings_migrates_legacy_permissions` |
| async `Settings.create(...)` 加载旧字段 | 复用同一同步迁移入口 | `test_async_settings_uses_same_permission_migration` |
| 旧 `sandbox_workspace_write` 加载时迁移 | 事务迁移为 `sandbox_writable_dirs`、增加 revision 并删除旧字段 | `test_legacy_config_migrated_on_load` |
| legacy 与任一 canonical 字段并存 | canonical 整体优先，legacy 不合并并被事务删除 | `test_mixed_permission_schema_prefers_canonical` |
| legacy 迁移持久化失败 | 不激活旧 grant，启动时报告警告 | `test_legacy_migration_failure_fails_closed` |
| 切换非 CUSTOM preset | 删除用户 session/persistent grants 与四个配置字段 | `test_preset_clears_path_grants` |
| 同一 Settings patch 含非 CUSTOM preset 和 grant 字段 | 整体拒绝，模式与授权均不改变 | `test_settings_rejects_non_custom_with_grants` |
| 子代理使用父会话已有 grant | 放行 | `test_subagent_inherits_effective_grants` |
| 子代理尝试扩大 grant | 阻止 | `test_subagent_cannot_add_grant` |
| 父会话新增 grant 后子代理仍用旧快照 | Engine 与 ToolContext 均不感知新增授权 | `test_subagent_grants_snapshot_fixed` |
| 父会话撤销 grant 或切换模式 | 阻止新 lease，取消/等待活动 lease 后使旧 snapshot 失效 | `test_subagent_snapshot_invalidated_on_revocation` |
| 子代理工具持有活动 lease 时撤销 | 撤销等待或取消工具，不能与副作用并发提交 | `test_subagent_revocation_waits_for_active_lease` |
| 权限选择 allow_other=False | helper 不追加 Other | `test_permission_choice_has_no_other` |
| clarify/checkpoint allow_other=True | 追加 Other 并可 fallback 到文本 | `test_open_choice_supports_other` |
| selected/anchor 经过 gateway | request DTO 保留字段 | `test_gateway_choice_preserves_selected_anchor` |
| 权限选择返回 free_text | 按 deny 处理 | `test_permission_free_text_is_denied` |

## Test Plan

安全关键测试必须由新增 `.github/workflows/permission-security.yml` 在 Linux、macOS 和 Windows 三个独立 job 上运行，不能以单平台单元测试替代：

- `permission-linux`：GitHub-hosted Ubuntu 当前稳定镜像；先运行 `./python.py scripts/check_permission_backend.py --require linux`，再执行 no-follow `openat`、symlink race、同盘 rename、真实或受控模拟 EXDEV 及子进程 containment 测试
- `permission-macos`：GitHub-hosted macOS 当前稳定镜像；`--require macos` 必须证明所选 sandbox/broker 后端可用，再运行目录句柄、symlink race 与子进程继承测试
- `permission-windows`：GitHub-hosted Windows 当前稳定镜像；`--require windows` 必须证明 reparse/broker 后端可用，再运行 junction、handle final path、rename 与子进程 containment 测试
- 专用安全矩阵中能力探测失败必须使 job 失败，不能 skip；普通开发机/通用测试 job 可运行 `--probe` 并验证“后端不可用时 fail closed”单元测试
- workflow 固定上传能力探测结果与平台测试日志；平台专属测试使用 pytest marker `permission_security_platform`
- 故障注入覆盖文件 fsync、`os.replace`、目录 fsync、Settings 刷新、PermissionService 首次/二次发布和 lease 取消/等待
- execution lease 测试覆盖主代理、子代理、已有授权路径、审批后路径和长运行子进程

下列命令包含现有测试路径，以及在 Implementation Scope 中明确要求新建的两个测试模块。

| Area | Command |
|---|---|
| Access resolution / capability / file approvals | `./test.py --backend -- src/tests/test_tools/test_resolve_safe.py src/tests/test_tools/file/test_read.py src/tests/test_tools/file/test_read_write.py src/tests/test_tools/file/test_write_file.py src/tests/test_tools/file/test_safe_path.py` |
| Engine / grants / epoch leases / runtime DATA_DIR | `./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_append.py src/tests/test_permission/test_epoch_gate.py` |
| Tool interaction helper | `./test.py --backend -- src/tests/test_tools/test_make_interact_callback.py` |
| Config migration / atomic persistence | `./test.py --backend -- src/tests/test_config/test_config.py src/tests/test_config/test_config_advanced.py` |
| Settings API / choice DTO | `./test.py --backend -- src/tests/test_ui/gateway/test_gateway_v2_dispatch.py src/tests/test_ui/gateway/test_gateway_headless_frontend.py` |
| Bash external paths | `./test.py --backend -- src/tests/test_tools/bash/test_tool.py src/tests/test_tools/bash/test_router_safety.py` |
| PowerShell external paths | `./test.py --backend -- src/tests/test_tools/test_powershell_tool.py` |
| Git policy and external paths | `./test.py --backend -- src/tests/test_tools/test_git_tool_raw_permissions.py src/tests/test_tools/test_git_tool_destructive.py src/tests/test_tools/test_git_tool_structured.py` |
| LSP input/output filtering | `./test.py --backend -- src/tests/test_lsp/test_lsp.py src/tests/test_lsp/test_lsp_advanced.py` |
| Child-agent grants / active lease revocation | `./test.py --backend -- src/tests/test_agent/graph/test_subagent_runner.py src/tests/test_agent/graph/test_parallel_subagents.py` |
| Platform security integration | `./python.py scripts/check_permission_backend.py --require <linux|macos|windows>` then `./test.py --backend -- -m permission_security_platform` |
| Focused regression | `./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_tools/test_resolve_safe.py src/tests/test_tools/test_make_interact_callback.py src/tests/test_tools/file/ src/tests/test_tools/bash/ src/tests/test_tools/test_powershell_tool.py src/tests/test_tools/test_git_tool_raw_permissions.py src/tests/test_lsp/ src/tests/test_config/test_config_advanced.py src/tests/test_ui/gateway/test_gateway_v2_dispatch.py` |

## Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| runtime/session/persistent 三来源 grants | 仅 session/persistent | 隔离应用内置路径与用户授权，避免 DATA_DIR 被误写入持久化配置 |
| ToolContext 使用 getter callback | 共享可变 list | 避免 Pydantic 复制导致授权状态不同步 |
| Engine 三态结果 | Engine hard deny 或完全跳过 | 既保持前置检查，又允许工具路径审批可达 |
| move 两端均为 write | src=read, dest=write | move 会删除源文件 |
| shell 外部路径统一 write | 按命令 read/write 分类 | shell 静态分析不可靠，保守规则更安全 |
| shell 使用封闭策略与进程沙箱 | best-effort 文本路径提取 | 静态计划负责审批，OS 强制层防止隐式或子进程越界 |
| resolve_safe 直接删除，不保留兼容包装 | 包装为 effective writable dirs 只读视图 | 所有调用点一次性迁移到 resolve_access，避免兼容层长期存在 readable grant 泄漏风险 |
| request/final 两阶段路径锁 | 全局锁、锁升级或只锁原始目标 | sibling 文件可并发交互，最终父目录/后代授权严格互斥且无升级死锁 |
| 分离 state/permissions revision | 单一 revision | session 内存变化与磁盘 CAS 不相互回退或误冲突 |
| PermissionService 权威校验 precondition，Settings 只持久化 delta | Settings 校验 runtime epoch 或提交调用者完整 snapshot | 保持分层并在线性化 commit_lock 内阻止旧审批越过模式/撤销边界 |
| PermissionService.update_permissions 是完整 patch 唯一入口 | Gateway/Settings 直接更新或双方各自发布 | 统一 revocation、锁序、持久化、发布及失败恢复 |
| PermissionService readiness gate 覆盖全部授权入口 | 仅在某个 Engine/工具路径检查 | 发布恢复失败时不会从旁路继续使用旧 external grants |
| PermissionService 是唯一 runtime 发布者 | Settings 回调直接更新服务或双方各自发布 | 避免重复发布、锁内回调死锁与 revision 重复递增 |
| execution lease 覆盖所有外部工具 | 仅子代理检查 epoch 或只在副作用前比较一次 | 消除授权检查后、文件操作前的普通工具撤销竞态 |
| `os.replace` 是持久化提交点 | 承诺任意失败均完全回滚 | 区分提交前回滚与提交后恢复，诚实表达 durability 边界 |
| 非 CUSTOM preset 删除用户路径 grants | 暂时停用并保留配置 | 与现有 preset 清理语义一致，避免隐藏授权在切回 CUSTOM 后恢复 |
| 不保留 legacy alias，加载时一次性迁移 | 双字段共存 + 冲突拒绝 | 避免兼容期复杂性和双字段不一致风险 |
| Other 由 interaction helper 生成 | 每个前端各自生成 | 保证封闭权限选择不会被前端差异绕过，避免重复选项 |
| Git 使用同步静态策略 + 异步授权感知补全 | Engine 同步执行 Git discovery 或各层重复实现 | 共享分类与参数策略，同时避免在授权前访问 Git 元数据 |
| Git 外部仓库只允许目录授权 | 单文件授权 | Git 会访问工作树、元数据、索引和对象库 |
| 外部 Git path 必须直指 worktree root/bare git dir | 自动向上搜索仓库根 | 禁止 discovery 越过已授权入口读取父目录 |
| Git 元数据、对象库与配置 include 分别授权 | 只授权工作树 | linked worktree、alternates 和 config include 可位于工作树之外 |
| Git 使用受控环境并禁用隐式可执行扩展 | 继承用户环境和仓库执行配置 | 防止 hooks、helpers、filters 和环境变量绕过路径模型 |
| LSP 输入与输出都做 read 过滤 | 只检查输入文件 | definition/references/diagnostics 可能返回其他文件路径 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|---|---|---|
| 调用点迁移遗漏 | 权限行为不一致 | Engine 仅对明确标记为 approval-capable 的工具 DEFER；其余 fail closed |
| 五选一审批较复杂 | 用户认知负担 | 默认选择 session file，文案显示规范化路径 |
| shell 规则偏严格 | 合理外部只读命令也需 write grant | 使用 read/grep 工具，或显式授予 shell writable access |
| shell 对未知命令和隐式配置路径分析不完备 | 外部访问可能无法可靠识别 | 只允许已登记的静态路径策略；未知或动态组合 fail closed |
| Git 策略注册表维护成本 | 新子命令默认不可用 | 每个新增策略同时提供分类、路径提取和安全测试 |
| persistent 配置增加 | settings 体积增长 | 去重、排序，仅显式持久化时写入 |
| 事务写入的平台差异 | fsync/replace 在不同文件系统上行为不同 | 明确 replace 提交点、父目录 fsync 和提交后恢复；按平台增加失败注入测试 |
| 句柄式安全路径 API 的平台复杂度 | 文件工具实现成本与兼容性增加 | AccessIntent 与 opaque AuthorizedPath 分层；SafePathExecutor 集中签发和验证 capability；后端不足时 fail closed |
| 不保留兼容层，所有调用点必须同 PR 迁移 | 迁移遗漏导致权限行为不一致 | Engine 仅对明确标记为 approval-capable 的工具 DEFER；其余 fail closed；CI 全量测试覆盖 |
| LSP 过滤改变结果完整性 | definition/reference 结果可能减少 | 仅返回已授权位置；不暴露被过滤路径或数量 |

## Forbidden Changes

- 不把 move src 降级为 read
- 不把 Engine 的 DEFER_TO_TOOL 当作 allow
- 不让 shell 使用 readable grants
- 不让 read-only/workspace-write shell 回退到 best-effort 文本提取、`shell=True` 或缺少文件系统沙箱的执行路径
- 不让 workspace 外文件副作用回退到普通 Path/shutil 路径 API，也不允许调用方自行构造 AuthorizedPath capability
- 不允许主代理、子代理或已有授权路径绕过会产出 opaque ExecutionLeaseToken 的 execution lease；forged、cross-gate、inactive 或 epoch-mismatch token 必须拒绝，不得仅在副作用前瞬时检查 epoch
- 不用单一全局锁覆盖用户审批，不在持有子锁时升级父锁，也不允许最终目录授权与其后代提交并发
- 不让 shell 或 Git 的未知路径组合默认放行
- 不让外部 Git path 通过向上搜索越过已授权入口；外部路径必须直指 worktree root 或 bare git dir
- 不让 Git 只校验工作树而跳过 git dir、common dir、index、对象库和配置 include
- 不让 Git 继承影响路径/配置来源的环境变量，或执行未被策略禁用的 hooks/helpers/filters
- 不允许 Git capability 与静态路径策略使用不同逻辑源
- 不让 LSP 返回未授权位置或其路径信息
- 不保留 `resolve_safe`、`add_extra_path`、`sandbox_extra_paths`、`sandbox_workspace_write` 等兼容接口
- 不依赖 Pydantic 模型字段保持 list 对象引用
- 不把 `os.replace` 成功后的错误描述为完整回滚；必须从磁盘恢复 live state 并标记 durability 状态
- 不允许 persistent grant 用旧完整 snapshot 覆盖最新 grants，也不允许混用 state_revision 与 permissions_revision
- 不允许待审批请求在 mode/epoch 变化后继续提交授权；Settings 不得接收或自行校验 runtime ApprovalPrecondition
- 不允许 Gateway/slash/其他运行时入口绕过 PermissionService.update_permissions 直接提交完整权限 patch
- 不允许 Settings 事务直接更新 PermissionService 或在调用方发布后再次发布同一结果
- 不允许提交/更新/grant 结果缺少 persistent、committed、durable、applied、conflict、restart_required 与 snapshot 的明确状态语义；session commit 不得被误解为磁盘提交
- 不允许发布失败后继续使用旧 external grants；permission_state_ready=False 必须由 Engine、ToolContext、lease 与 resolve_access 共同强制，恢复失败进入 restart-required 状态
- 不允许撤销流程持有 commit/settings/grant lock 等待活动 execution lease
- 不允许同步 Settings 构造路径绕过 legacy 权限迁移
- 不在权限审批中启用 Other/free-text
- 不在 Gateway/TUI 重复生成 Other 选项
- 不在非 CUSTOM preset 下保留用户 session/persistent path grants
- 不修改 glob 的 workspace-only 范围
- 不修改 BASIC_RULES 的工具级策略
- 不改变 danger-full-access 和 read-only 的模式定义
