## 交互模式

三种模式控制 agent 的自主程度。

| 命令 | 模式 | 行为 |
|------|------|------|
| `/mode auto` | Auto | 每轮自动推断意图（默认） |
| `/mode plan` | Plan | 只读规划，禁止写/编辑/bash |
| `/mode goal` | Goal | 多步工作限定在当前目标内 |
| `/plan` | — | 等价 `/mode plan` |
| `/unplan` | — | 回到 auto 模式 |
| `/goal` | — | 设置/查看当前目标，`/goal clear` 清除 |

---
