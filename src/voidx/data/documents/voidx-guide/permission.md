## 权限与沙箱

分层控制 agent 的操作边界，从最严格到最宽松。

### 权限模式

| 命令 | 行为 |
|------|------|
| `/permission-preset read_only` | 阻止写入和高风险操作 |
| `/permission-preset safe` | 默认安全预设，风险操作会询问 |
| `/permission-preset project_trusted` | 信任项目内编辑，危险操作仍询问 |
| `/permission-preset full_access` | 最宽松预设，但极高风险仍需确认 |

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
| `/permission-preset <preset>` | 切换高层权限预设 |

---
