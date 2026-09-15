# 外部路径在 Search、Find 与 Shell 工具下的授权与执行打通规范

> **Status: Done** — Archived on 2026-09-15.

- **日期**：2026-09-15
- **状态**：方案已修订完善（经过子代理独立审查优化，已解决兼容性与安全漏洞），待确认实施
- **读者**：架构维护者、后端核心开发者与实施代理
- **涉及范围**：
  - `src/voidx/tooling/application/execution.py`
  - `src/voidx/tooling/builtin/file/search.py`
  - `src/voidx/tooling/policy/filesystem/constants.py`
  - `src/voidx/tooling/policy/permission/rules.py`
  - `src/voidx/tooling/application/authorization.py`
  - `src/voidx/tooling/policy/shell/policy.py`
  - `src/voidx/tooling/builtin/shell/bash/tool.py`
  - `src/voidx/tooling/builtin/shell/powershell/tool.py`
  - 关联单元与集成测试套件

---

## 1. 背景与缺陷表现

在多工作区与外部代码库协同场景中，用户常通过跨路径操作（例如通过 `Read` 查看外部仓库的文件）。当用户在弹窗中选择批准外部文件/目录授权后，后续工具调用出现如下异常拦截现象：

1. **`Search` / `Find` 工具针对外部路径直接抛出异常或报 `Path traversal blocked`**：
   - 即使外部路径已获得用户批准，LLM 调用 `Search(query="...", path="/external/path")` 时，依然报错 `Path traversal blocked: /external/path`；
   - 若绕过路径校验直接执行，工具在内部计算相对路径时直接崩溃：`ValueError: '...' is not in the subpath of '...'`。
2. **`Bash` / `PowerShell` 工具执行针对已批准外部目录的只读/写命令时被拦截**：
   - 用户已授权某外部目录（如 `/Users/xxx/workspace/imcore/...`），但 LLM 执行 `git -C /Users/xxx/workspace/imcore/... log -n 5 --stat` 时，工具直接返回 `[blocked] shell policy deferred: external path requires access grant`；
   - 若命令在调度层触发权限审批（弹出选择窗口），用户无论选择单次允许（`allow`）还是本会话允许（`session`），命令仍然无法执行，底层沙箱门禁无条件拦截。

---

## 2. 根因剖析 (Root Causes)

经过完整调用链路排查，定位到以下五个关键断点：

### 断点 1：`AuthorizationRuntime.sandbox_paths()` 缺失动态 Grants 聚合
- **源码位置**：`src/voidx/tooling/application/execution.py:54-56`
- **机制**：
  ```python
  def sandbox_paths(self, *, write: bool) -> list[str]:
      writable = [*self.write_files, *self.write_dirs]
      return writable if write else [*self.read_files, *self.read_dirs, *writable]
  ```
  `self.read_files`/`self.read_dirs` 仅在进程启动时由静态配置（`config.sandbox_readable_dirs`）初始化，默认皆为空。用户在会话中交互批准的动态路径（通过 `add_grant` 写入的 `AccessGrant`）保存在 `access_grants_reader`（即 `permission.get_access_grants()`）中。
- **后果**：
  `search.py:142` 与 `lsp.py:37` 依赖 `_sandbox_paths_for_access(ctx, write=False)` 解析路径白名单。因 `sandbox_paths` 缺失动态 grant 路径，`resolve_tool_path` 将已授权的外部路径判定为沙箱越界（返回 `None`），直接报错 `Path traversal blocked`。

### 断点 2：`search.py` 内部假设所有检索候选必须隶属于工作区根目录
- **源码位置**：`src/voidx/tooling/builtin/file/search.py:92-94, 126, 130`
- **机制**：
  `_visible_files(base, scope)` 中固定传入 `base = Path(ctx.workspace)`。但在对文件条目排序和包装时，调用了：
  ```python
  def _relative(base: Path, path: Path) -> str:
      return str(path.relative_to(base)).replace("\\", "/")
  ```
  当 `scope` 位于工作区外部时，`candidates` 中的绝对路径调用 `path.relative_to(base)` 会直接抛出 Python 标准库异常：
  `ValueError: '/path/outside' is not in the subpath of '/workspace'`。
- **后果**：
  `Search` 与 `Find` 工具在外部目录下搜索时直接未捕获异常退出，ToolResult 被包装为 `Tool execution error: ...`，在 UI 上表现为执行红叉。

### 断点 3：`search` 与 `find` 工具未纳入调度层外部路径管控体系
- **源码位置**：
  - `src/voidx/tooling/policy/filesystem/constants.py:1`
  - `src/voidx/tooling/policy/permission/rules.py:106-115, 184-209`
  - `src/voidx/tooling/application/authorization.py:136-179`
- **机制**：
  1. `FILE_PATTERN_TOOLS` 常量中仅定义了 `{"read", "write", "replace", "lsp_format", "lsp"}`，未包含 `search` 与 `find`；且 `search`/`find` 的作用路径参数名为 `path`（而非 `file_path`）；
  2. `file_paths_for_tool` 针对 `search` 与 `find` 返回空列表 `[]`；
  3. `path_tool_names` 集合与 `read_tools` 集合在 `authorization.py` 中未包含 `search`、`find`；
  4. `BASIC_RULES` 中 `search` 与 `find` 默认 action 均为 `allow`。
- **后果**：
  当模型首次发起对外部路径的 `Search(query="...", path="/external/path")` 时，调度层将其判定为 `allow`（绕过任何权限检查与弹窗审批），但进入工具执行层后，因路径未被授权又无法通过沙箱检查，导致“调度层静默放行，执行层直接报错”。

### 断点 4：`Bash` / `PowerShell` 调度层预检丢失 `AccessIntent`
- **源码位置**：`src/voidx/tooling/application/authorization.py:102-122, 145-148`
- **机制**：
  在 `sandbox_precheck_action` 中，针对 `bash` 和 `powershell`：
  ```python
  if classified.name == "bash":
      return (*shell_sandbox_precheck(classified.args, context, shell="bash"), ())
  ```
  末尾第三个返回值 `access_intents` 硬编码为空元组 `()`。
  即使 `shell_sandbox_precheck` 检查到外部只读/可写路径未授权返回 `defer`，生成的 `PermissionDecision` 的 `access_intents` 仍然为空。
- **后果**：
  在 `PermissionFlow._ask_and_apply_permission` 中：
  - 因无 `access_intents`，审批弹窗退化为普通的命令确认（`Yes / Yes, always / No`），而非路径授权（`Allow once / Session dir / Persistent dir`）；
  - 无论用户选择 `y` 还是 `a`，均**不会调用 `add_grant` 写入路径 Grant**，仅写入命令 pattern 的 session rule；
  - 工具进入 `BashTool.execute` 后，再次执行 `shell_sandbox_precheck` 时依然缺少该路径的 `AccessGrant`，命中 `if "external path requires" in shell_reason: return build_blocked_result(...)`，遭到强行拦截。

### 断点 5：`shell_sandbox_precheck` 未能产出待审批路径意图
- **源码位置**：`src/voidx/tooling/policy/shell/policy.py:326-380`
- **机制**：
  `shell_sandbox_precheck` 对命令进行静态分析提取出 `policy.read_paths` 与 `policy.write_paths`，并通过 `resolve_access` 进行校验。当某个外部路径未授权时，它仅返回单一字符串原因 `_EXTERNAL_ACCESS_GRANT_REASON`，而没有把解析出的 `AccessIntent` 向上返回，阻断了向调度层传递路径意图的能力。

---

## 3. 架构设计与目标行为 (Architecture & Goals)

### 3.1 核心目标
1. **动态 Grant 贯通执行环境**：`AuthorizationRuntime.sandbox_paths()` 必须完整包含当前生效的所有读取/写入 Grants（包含 Session Grants 与 Persistent Grants）。
2. **Search / Find 外部路径解耦与绝对路径契约**：
   - 允许 `path` 指向工作区外的已授权外部路径；
   - **统一输出路径契约**：检索到的文件，若在 `workspace` 内，返回相对于工作区的相对路径；**若在 `workspace` 之外，统一返回规范化的 POSIX 绝对路径**（确保后续 LLM 传递给 `Read`、`Replace`、`Write` 时能无歧义精确定位）；
   - 消除 `_visible_files` 内部所有基于工作区的非法 `relative_to` 假设；
   - 将 `search` 与 `find` 纳入外部路径调度与门禁体系，未授权外部路径统一在前置调度层触发权限审批。
3. **Shell 外部路径意图闭环与向后兼容**：
   - 保留现有 `shell_sandbox_precheck` 的二元组 `(Action, str | None)` 签名不变，确保所有调用方与已有测试完全兼容；
   - 新增 `shell_sandbox_precheck_with_intents` 向调度层提供 `(Action, str | None, tuple[AccessIntent, ...])`；
   - `sandbox_precheck_action` 将该 `AccessIntent` 注入 `PermissionDecision`；
   - 用户审批后，将外部路径（文件或目录）作为 `AccessGrant` 正式写入权限服务，使底层 `BashTool`/`PowerShellTool` 门禁与后续工具调用无缝放行；
   - **会话规则防短路安全守卫**：在 `authorize_tool_call` 的 `defer` 分支中，增加对未授权外部路径的保护，防止先前放行的通用命令规则在缺失 Grant 时短路绕过弹窗审批。

### 3.2 禁则 (Invariants & Forbidden Changes)
- **禁止破坏既有接口签名**：不得直接更改已广泛被测试或工具调用的公开函数签名（如 `shell_sandbox_precheck`）。
- **禁止弱化安全沙箱**：未获得用户授权的外部路径，在任何情况下均不得直接读取或执行。
- **禁止工具内部二次弹窗**：遵循 2026-09-14 权限归一化规范，工具底层仅作为门禁（Gatekeeper），不发起 UI 请求。
- **禁止硬编码绝对路径规则**：跨平台适配（Linux/macOS POSIX 与 Windows 路径），路径比较必须基于规范化的 `Path.resolve()`。

---

## 4. 详细实施方案 (Implementation Plan)

### 4.1 阶段一：`AuthorizationRuntime.sandbox_paths` 动态聚合
**文件**：`src/voidx/tooling/application/execution.py`
- 修改 `sandbox_paths(self, *, write: bool) -> list[str]`：
  从 `self.access_grants()` 中获取当前的 `readable_files`、`readable_dirs`、`writable_files`、`writable_dirs`。
  合并逻辑：
  ```python
  def sandbox_paths(self, *, write: bool) -> list[str]:
      grants = self.access_grants()
      writable = [*self.write_files, *self.write_dirs, *grants.writable_files, *grants.writable_dirs]
      if write:
          return list(dict.fromkeys(writable))
      readable = [
          *self.read_files, *self.read_dirs, *grants.readable_files, *grants.readable_dirs,
          *writable,
      ]
      return list(dict.fromkeys(readable))
  ```

### 4.2 阶段二：`Search` 与 `Find` 工具外部路径适配与契约规范
**文件**：`src/voidx/tooling/builtin/file/search.py`
1. **相对/绝对路径显示契约**：
   - 实现安全路径显示转换函数 `_display_path(base: Path, path: Path) -> str`：
     ```python
     def _display_path(base: Path, path: Path) -> str:
         try:
             return str(path.relative_to(base)).replace("\\", "/")
         except ValueError:
             return str(path.resolve()).replace("\\", "/")
     ```
   - 在 `_visible_files(base: Path, scope: Path, ...)` 中：
     排序 key 改用 `_display_path(base, item)`；
     `_FileEntry.relative` 改用 `_display_path(base, path)`；
     彻底消除 `ValueError` 异常抛出。
2. **Gitignore 适配**：
   外部路径若自身是独立的 Git 仓库，且 `scope` 包含 `.gitignore`，加载该 `scope` 所在目录下的 `.gitignore` 并基于 `scope` 进行 ignore 匹配。

### 4.3 阶段三：将 `search` 与 `find` 纳入外部路径权限体系
**文件**：
- `src/voidx/tooling/policy/permission/rules.py`
- `src/voidx/tooling/application/authorization.py`

1. **更新参数提取与分类**：
   - 在 `file_paths_for_tool(tool: str, args: dict) -> list[str]` 中：
     对 `tool in {"search", "find"}`，如果 `args.get("path")` 存在且非空，返回 `[str(args["path"])]`。
   - `build_pattern` 中针对 `search`/`find`：若指定了 `path`，返回形如 `search:{query}:{path}` 或保留 `path`。
2. **调度层前置拦截与读工具集扩充**：
   - 在 `authorization.py` 中：
     `path_tool_names = {"read", "write", "replace", "manage", "lsp_format", "lsp", "search", "find"}`；
     `read_tools = {"read", "lsp", "search", "find"}`（确保对外部路径判定为 `access="read"` 且 `require_exists=True`）。
   - 当 LLM 指定了工作区外的 `path` 时，自动触发 `_collect_external_access_intents`，未获 Grant 时返回 `defer`，并在调度层向用户展示路径授权弹窗。

### 4.4 阶段四：Shell 外部路径意图提取与 Grant 注入闭环
**文件**：
- `src/voidx/tooling/policy/shell/policy.py`
- `src/voidx/tooling/application/authorization.py`

1. **`shell_sandbox_precheck_with_intents`**：
   - 在 `src/voidx/tooling/policy/shell/policy.py` 中新增：
     ```python
     def shell_sandbox_precheck_with_intents(
         args: dict,
         context: PermissionContext,
         *,
         shell: str = "bash",
     ) -> tuple[Action, str | None, tuple[AccessIntent, ...]]:
     ```
     在遍历 `policy.read_paths` 和 `policy.write_paths` 时，收集所有 `resolution.action != "allow"` 且存在 `resolution.intent` 的 `AccessIntent`；
     现有的 `shell_sandbox_precheck` 保持签名 `(Action, str | None)`，直接调用 `shell_sandbox_precheck_with_intents(...)[:2]`。
2. **`sandbox_precheck_action` 对接 intents**：
   - 在 `sandbox_precheck_action` 中针对 `bash` 与 `powershell`，调用 `shell_sandbox_precheck_with_intents` 并将返回的 `intents` 透传给调度决策。
3. **会话规则防短路安全守卫**：
   - 在 `authorize_tool_call` 的 `if sandbox_action == "defer":` 逻辑中：
     ```python
     if sandbox_action == "defer":
         if session_action == "deny":
             return _decision(classified, "deny", "session", _reason_for(classified, "deny"), context=context)
         if session_action == "allow" and context.sandbox_mode == "workspace-write" and not access_intents:
             return _decision(classified, "allow", "session", _reason_for(classified, "allow"), context=context)
         return _decision(classified, "ask", "sandbox", reason or _reason_for(classified, "ask"), context=context, access_intents=access_intents)
     ```
     当且仅当没有未授权外部路径（`not access_intents`）时才允许被 session 规则静默放行；存在外部路径时强制保留 `ask` 触发路径授权。

---

## 5. 验收标准与测试用例 (Acceptance Criteria & Test Commands)

### 5.1 验收标准 (Acceptance Criteria)
1. **测试通过**：
   - 检索单元测试：`./test.py --backend -- src/tests/test_tooling/file/test_search.py`
   - Shell 单元测试：`./test.py --backend -- src/tests/test_tooling/bash/test_bash_git_direct.py`
   - 权限核心测试：`./test.py --backend -- src/tests/test_tooling/permission/`
2. **场景覆盖**：
   - **用例 1 (Search 外部路径返回绝对路径)**：用户已批准外部目录 Grant，调用 `SearchTool` 搜索该外部目录中的关键字，返回以绝对路径标识的匹配项，无 `ValueError`，无 `Path traversal blocked`。
   - **用例 2 (Find 外部路径)**：用户已批准外部目录 Grant，调用 `FindTool` 搜索该外部目录中的文件，成功返回带有完整绝对路径的文件列表。
   - **用例 3 (Bash 外部 Git 命令执行成功)**：用户批准了外部目录 Grant，调用 `BashTool` 执行 `git -C /external/repo log`，底层沙箱门禁正常放行，命令执行成功。
   - **用例 4 (Bash 外部路径调度层前置意图收集与审批闭环)**：未预先授权外部路径时调用 `git -C /external/repo status`，调度层通过 `shell_sandbox_precheck_with_intents` 正确捕获外部路径 `AccessIntent`，用户批准后成功写入 Grant，命令顺利执行。
   - **用例 5 (未授权外部路径拒绝)**：用户拒绝授权时，`Search` 与 `Bash` 工具均被阻断，绝不发生越界读取或执行。
