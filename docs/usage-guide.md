# voidx 使用指南

voidx 是一个终端 AI 编码助手。本文档覆盖用户可操作的全部功能。

> 所有斜杠命令在对话输入框直接输入即可触发，输入 `/` 会弹出可选命令列表。

---

## 快速上手

```bash
# 进入项目目录启动
voidx -w /path/to/project

# 或直接在当前目录启动
voidx

# 恢复上次会话
voidx -r <session-id>

# 查看已保存会话
voidx sessions
```

启动后直接用自然语言描述任务即可。需要调整行为时用斜杠命令。

### 运行形态

| 形态 | 启动方式 | 适用场景 |
|------|----------|----------|
| 终端 TUI | `voidx` | 日常本地开发 |
| Web UI | `voidx --web` | 浏览器中使用 |
| 桌面应用 | Tauri 构建 | 原生桌面体验 |
| 无头后端 | `voidx --web --web-headless` | 远程服务器 |

### 常用 CLI 参数

| 参数 | 作用 |
|------|------|
| `-w / --workspace` | 指定工作目录 |
| `-m / --model` | 指定模型名 |
| `-p / --provider` | 指定 provider |
| `-r / --resume` | 按 ID 恢复会话 |
| `-n / --new` | 强制新会话 |
| `--web-host / --web-port` | Web 网关监听地址 |
| `--version` | 显示版本 |

---

## 会话管理

会话基于 SQLite 持久化，可跨终端恢复。

| 命令 | 作用 |
|------|------|
| `/clear` | 开新会话，清空上下文 |
| `/list` | 列出已保存会话 |
| `/session new` | 新建空会话 |
| `/session resume` | 恢复会话 |
| `/session list` | 列出会话 |
| `/session del` | 删除旧会话 |
| `/session del --dry-run` | 预览删除候选 |
| `/resume` | 从列表或指定 ID 恢复 |
| `/rollback` | 回滚当前轮次的文件改动 |
| `/title` | 设置会话标题 |
| `/title auto` | 自动生成标题 |

---

## 模型与 Provider

支持多 provider 配置，随时切换。

| 命令 | 作用 |
|------|------|
| `/model` | 切换模型 |
| `/model new` | 创建或更新模型 profile |
| `/model list` | 查看已配置模型详情 |
| `/model switch` | 切换 provider |
| `/model test` | 测试 provider 连通性 |
| `/model del` | 删除 profile |
| `/model reasoning` | 设置推理强度 |
| `/model ctx` | 设置上下文窗口大小 |

---

## 交互模式

三种模式控制 agent 的自主程度。

| 命令 | 模式 | 行为 |
|------|------|------|
| `/plan` | Plan | 只读规划，禁止写/编辑/bash |
| `/unplan` | Auto | 回到 auto 模式（默认） |
| `/goal` | Goal | 设置目标并进入 goal 模式，多步工作限定在当前目标内 |

默认是 Auto：每轮自动推断意图。`/goal` 无参数时显示当前目标。

---

## 权限与沙箱

分层控制 agent 的操作边界，从最严格到最宽松。

### 权限模式

| 命令 | 行为 |
|------|------|
| `/permission read_only` | 阻止写入和高风险操作 |
| `/permission safe` | 默认安全预设，写入和风险命令会询问 |
| `/permission project_trusted` | 信任项目内编辑和常规命令，危险操作仍询问 |
| `/permission full_access` | 最宽松预设，大多数操作自动允许；硬阻断风险仍直接拒绝 |

### 沙箱

沙箱不是独立命令；它由 `/permission` 预设派生。

| 权限模式 | 派生沙箱 | 审批策略 |
|------|------|------|
| `read_only` | `read-only` | 风险操作询问，写入默认受限 |
| `safe` | `workspace-write` | 写入和风险命令询问 |
| `project_trusted` | `workspace-write` | 项目内编辑/常规命令更宽松，危险操作询问 |
| `full_access` | `danger-full-access` | 大多数操作自动允许，硬阻断风险仍直接拒绝 |

### 工具级控制

| 命令 | 作用 |
|------|------|
| `/allow <tool>` | 本会话允许某工具 |
| `/deny <tool>` | 本会话禁止某工具 |
| `/permissions` | 查看当前规则 |
| `/permission <preset>` | 切换高层权限预设 |

---

## 工作流

内置结构化工作流，按任务场景自动路由。每个节点有进入条件（gate）和对应思维模式（persona）。

| 节点 | 触发场景 | 目标 |
|------|----------|------|
| `brainstorm` | 新功能/组件/行为变更前 | 确认需求与设计，获用户批准 |
| `design` | 写技术文档/PRD/RFC/API 文档 | 产出通过 reader test 的文档 |
| `plan` | 把规格转成实现计划 | 产出可执行计划，获批准 |
| `tdd` | 实现功能/修复/重构 | TDD 循环，测试全绿 |
| `verify` | 声明完成前 | 用可复现命令证明状态 |
| `review` | 实现完成后 | 结构化代码审查，收集 PASS/FAIL |
| `feedback` | 收到审查反馈 | 验证并实现有效反馈 |
| `debug` | 调试 bug/崩溃/失败 | 定位根因，确认修复方向 |

用户侧入口：

| 命令 | 作用 |
|------|------|
| `/guide` | 向运行中的 agent 注入指导 |
| `/parallel` | 切换并行子 agent 执行 |
| `/parallel status` | 查看并行配置 |

---

## 扩展能力

### MCP 服务器

连接外部 MCP 服务器扩展 agent 工具能力。

| 命令 | 作用 |
|------|------|
| `/mcp list` | 列出已配置服务器 |
| `/mcp new` | 配置新服务器 |
| `/mcp del` | 删除服务器 |
| `/mcp enable` / `/mcp disable` | 启用/禁用 |
| `/mcp restart` | 重启服务器 |
| `/mcp test` | 测试连接 |
| `/mcp tools` | 查看服务器工具 |

### LSP 语言服务

| 命令 | 作用 |
|------|------|
| `/lsp status` | 服务器状态 |
| `/lsp servers` | 已配置服务器 |
| `/lsp doctor` | 检查已安装语言服务器 |
| `/lsp restart` | 重启语言服务器 |

### 技能系统

可加载的指令包，定义特定场景行为。项目级存于 `.voidx/skills/`，全局存于 `~/.voidx/skills/`。

| 命令 | 作用 |
|------|------|
| `/skills` | 管理本地技能 |

---

## 上下文管理

长对话的上下文压缩与用量监控。

| 命令 | 作用 |
|------|------|
| `/compact` | 手动触发上下文压缩 |
| `/usage` | 查看本会话 token 用量 |
| `/diff` | 显示 git 工作树 diff |

达到阈值时自动压缩历史，无需手动干预。

---

## 用户偏好

| 命令 | 作用 |
|------|------|
| `/lang` | 设置响应语言偏好 |
| `/tone` | 设置响应语气偏好 |
| `/init` | 为当前项目生成 `AGENTS.md` |
| `/init force` | 强制重新生成 |
| `/code-ide` | 选择打开变更文件的 IDE |
| `/code-ide status` | 查看检测到的 IDE |

---

## Web 搜索

| 命令 | 作用 |
|------|------|
| `/tavily set` | 设置 Tavily API key |
| `/tavily show` | 查看 key 状态 |
| `/tavily delete` | 删除 key |

配置后 agent 可自动进行网页搜索和抓取。

---

## 调试与日志

| 命令 | 作用 |
|------|------|
| `/debug on` / `/debug off` | 详细步骤/工具输出开关 |
| `/log exchange` | 切换 exchange 日志 |
| `/log diagnostic` | 切换诊断日志 |

---

## 升级

| 命令 | 作用 |
|------|------|
| `/upgrade check` | 检查 PyPI 新版本 |
| `/upgrade now` | 同时升级并验证 `voidx` 与 `voidx-cli`，成功后重启 voidx |
| `/upgrade on` / `/upgrade off` | 启动时检查开关 |
| `/upgrade status` | 查看检查状态 |

通过 pip 安装时，手动升级也必须同时更新两个包：

```bash
python -m pip install --upgrade voidx voidx-cli
```

通过 npm 安装或由 npm 启动的 voidx 不能使用 `/upgrade now`，请退出当前进程后执行：

```bash
npm update -g @chikhamx/voidx
```

使用 `install.sh` 或 `install.ps1` 安装的用户可以重新运行原安装脚本；安装器会验证两个包的元数据版本、模块导入和 `voidx --version`，验证失败时只进行一次完整强制修复。

---

## 命令速查

| 命令 | 一句话 |
|------|--------|
| `/help` | 显示所有命令 |
| `/exit` `/quit` | 退出 |
| `/paste` | 从剪贴板粘贴图片 |
| `/clear` | 开新会话 |
| `/list` | 列出会话 |
| `/resume` | 恢复会话 |
| `/rollback` | 回滚当前轮次 |
| `/model` | 切换模型 |
| `/plan` `/unplan` | 进入/退出 plan 模式 |
| `/goal` | 设置/查看当前目标 |
| `/permissions` | 查看权限规则 |
| `/permission` | 选择权限预设 |
| `/compact` | 压缩上下文 |
| `/usage` | 查看 token 用量 |
| `/diff` | 查看 git diff |
| `/mcp` | 管理 MCP 服务器 |
| `/lsp` | 管理语言服务器 |
| `/skills` | 管理技能 |
| `/init` | 生成 AGENTS.md |
| `/lang` `/tone` | 语言/语气偏好 |
| `/upgrade` | 检查/执行升级 |
| `/debug` | 调试输出开关 |
| `/log` | 日志开关 |
