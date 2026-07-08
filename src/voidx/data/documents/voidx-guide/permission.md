## 权限与沙箱

分层控制 agent 的操作边界，从最严格到最宽松。

### 权限模式

| 命令 | 行为 |
|------|------|
| `/permission-mode read-only` | 阻止所有写操作 |
| `/permission-mode default` | 写/编辑/bash 前询问 |
| `/permission-mode accept-edits` | 允许文件编辑，bash 仍询问 |
| `/permission-mode auto-review` | reviewer 辅助审批 |
| `/permission-mode custom` | 用 `.voidx/settings.json` 配置 |
| `/permission-mode full-access` | 无沙箱无审批 |

### 沙箱

| 命令 | 行为 |
|------|------|
| `/sandbox read-only` | 只读沙箱 |
| `/sandbox workspace-write` | 允许工作区写 |
| `/sandbox danger-full-access` | 无沙箱限制 |

### 工具级控制

| 命令 | 作用 |
|------|------|
| `/allow <tool>` | 本会话允许某工具 |
| `/deny <tool>` | 本会话禁止某工具 |
| `/permissions` | 查看当前规则 |
| `/approval untrusted\|on-failure\|on-request\|never` | 审批策略 |

---
