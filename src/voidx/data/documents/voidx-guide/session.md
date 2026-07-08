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
