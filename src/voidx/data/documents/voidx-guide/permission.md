## 权限与沙箱

分层控制 agent 的操作边界，从最严格到最宽松。

### 权限模式

| 命令 | 行为 |
|------|------|
| `/permission read_only` | 阻止写入和高风险操作 |
| `/permission safe` | 默认安全预设，风险操作会询问 |
| `/permission project_trusted` | 信任项目内编辑，危险操作仍询问 |
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
