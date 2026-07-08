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
