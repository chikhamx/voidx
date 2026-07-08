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
