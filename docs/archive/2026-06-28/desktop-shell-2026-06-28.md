# 桌面端壳侧完善 — 技术设计文档

> **Status: Done** — 壳侧已实现。`desktop/tauri/src/main.rs` 已含
> `BackendStatus` enum、`backend_status` 命令、`resolve_python` 路径解析
> （支持 `.venv/Scripts/python.exe` 回退）。

## Context

桌面端（`desktop/`）当前是一个 Tauri 2 壳应用，仅 97 行 Rust 代码，只做了"拉起 Python 后端 + 暴露 gateway URL"两件事。后端协议层（`src/voidx/ui/gateway/`、`src/voidx/ui/protocol/`）已完整支持流式输出、工具调用树、权限弹窗、todo、clarify、diff 等能力，前端（`frontend/src/main.js`）也已实现 WebSocket 协议消费和 Tauri 轮询逻辑。

但壳侧存在多个阻断性问题：Windows 上 Python 路径失效（`.venv/bin/python` 不存在）、后端进程在窗口关闭时不会被清理、缺少 Tauri 2 必需的 capabilities 配置、缺少图标资源、后端启动失败前端无感知。本设计解决这些问题，让桌面端在 Windows 上能稳定启动并连接后端。

## Goals and Non-Goals

### Goals

- 修复 Windows 上 Python 解释器路径解析，支持 `.venv/Scripts/python.exe` 及回退
- 托管后端子进程生命周期，窗口关闭时可靠清理
- 后端就绪/失败状态及时通知前端，替代不可靠的 `sleep(100ms)`
- 补齐 Tauri 2 必需配置：capabilities、图标
- `cargo check` 通过，`npm run dev` 能拉起后端并连接

### Non-Goals

- 不改动前端 `frontend/src/main.js` 的协议消费逻辑（已有 Tauri 轮询，够用）
- 不增加原生菜单、托盘、系统通知、文件拖拽等增强能力（后续迭代）
- 不改动后端 Python 代码
- 不处理 macOS/Linux 打包（本次聚焦 Windows 可用性，但路径解析保持跨平台）

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Tauri 主进程 (main.rs)                          │
│                                                  │
│  AppState                                       │
│  ├─ gateway_url: Arc<Mutex<Option<String>>>     │
│  ├─ backend_status: Arc<Mutex<BackendStatus>>   │  ← 新增
│  └─ child_handle: Arc<Mutex<Option<Child>>>     │  ← 新增
│                                                  │
│  Commands (invoke_handler)                      │
│  ├─ gateway_url() → Option<String>             │
│  ├─ backend_status() → {status, error?}        │  ← 新增
│                                                  │
│  setup                                          │
│  └─ spawn_backend()                             │
│     ├─ resolve_python() → PathBuf              │  ← 重写，跨平台
│     ├─ spawn child                              │
│     ├─ 读 stderr 解析 VOIDX_WEB_GATEWAY         │
│     ├─ 就绪 → emit("backend_ready", url)       │  ← 新增
│     └─ 失败 → emit("backend_failed", error)    │  ← 新增
│                                                  │
│  on_window_event(CloseRequested)                │  ← 新增
│  └─ kill child process                          │
└─────────────────────────────────────────────────┘
          │ invoke("gateway_url") / invoke("backend_status")
          │ listen("backend_ready" / "backend_failed")
          ▼
┌─────────────────────────────────────────────────┐
│  前端 (frontend/src/main.js) — 不改动            │
│  已有 60 次 × 500ms 轮询 gateway_url            │
└─────────────────────────────────────────────────┘
```

### 数据流

1. Tauri 启动 → `setup` 调 `spawn_backend`
2. `spawn_backend` 在独立线程拉起 Python，读 stderr
3. 解析到 `VOIDX_WEB_GATEWAY{...}` → 写 `gateway_url` + `backend_status=Ready` + emit `backend_ready`
4. spawn 失败或 stderr 异常 → 写 `backend_status=Failed` + emit `backend_failed`
5. 前端轮询 `gateway_url`（已有逻辑），或监听 `backend_ready` 事件（可选增强，本次不改前端）
6. 窗口关闭 → `on_window_event` → `child.kill()`

## Data Model

### BackendStatus（新增，Rust enum）

```rust
enum BackendStatus {
    Starting,
    Ready { url: String },
    Failed { error: String },
}
```

序列化为前端可消费的 JSON：
```json
{"status": "starting"}
{"status": "ready", "url": "ws://127.0.0.1:54321/?token=xxx"}
{"status": "failed", "error": "failed to spawn voidx backend: ..."}
```

## API Contract

### `gateway_url`（已有，不变）

- **Signature**: `fn gateway_url(state: State<'_, AppState>) -> Option<String>`
- **Returns**: `String | null`

### `backend_status`（新增）

- **Signature**: `fn backend_status(state: State<'_, AppState>) -> serde_json::Value`
- **Returns**:
  ```json
  {"status": "starting"}
  {"status": "ready", "url": "ws://127.0.0.1:54321/?token=xxx"}
  {"status": "failed", "error": "..."}
  ```

### Tauri 事件（新增，前端可选监听）

- `backend_ready` — payload: `{ url: String }`
- `backend_failed` — payload: `{ error: String }`

### `resolve_python`（重写）

```rust
fn resolve_python() -> Option<PathBuf> {
    // 1. 环境变量覆盖
    if let Ok(path) = std::env::var("VOIDX_PYTHON") {
        return Some(PathBuf::from(path));
    }
    // 2. 平台相关 venv 路径
    let candidates = if cfg!(windows) {
        [".venv/Scripts/python.exe", ".venv/bin/python"]
    } else {
        [".venv/bin/python", ".venv/Scripts/python.exe"]
    };
    for c in candidates {
        let p = PathBuf::from(c);
        if p.exists() { return Some(p); }
    }
    // 3. Windows py launcher 回退
    if cfg!(windows) {
        if let Ok(output) = Command::new("py").arg("-c").arg("import sys; print(sys.executable)").output() {
            if output.status.success() {
                let s = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !s.is_empty() { return Some(PathBuf::from(s)); }
            }
        }
    }
    None
}
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `resolve_python` 返回 None | `backend_status=Failed`，emit `backend_failed`，前端显示错误 |
| `Command::new(python).spawn()` 失败 | 同上，error 含 python 路径 |
| stderr 读到 EOF 未解析到 gateway URL | `backend_status=Failed`，error="backend exited without publishing gateway url" |
| 后端进程启动后崩溃 | stderr 线程结束，检测 child exit code，设 Failed |
| 窗口关闭时 kill 失败 | 记录 eprintln，不阻塞关闭（best-effort） |

### Windows 进程清理

Windows 上 `std::process::Child::kill()` 等价于 `TerminateProcess`，只杀主进程，不杀子进程树。Python 后端本身不 spawn 子进程，所以 `child.kill()` 足够。若后续后端有子进程，再引入 `taskkill /T /F`。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 Tauri emit 事件通知就绪 | 仅靠前端轮询 `gateway_url` | 事件更及时，但前端已有轮询逻辑且工作正常，事件作为补充而非替代，本次不改前端 |
| `backend_status` 命令返回 JSON | 定义 Rust struct + serde | 简单，避免引入额外类型定义 |
| 路径回退用 `py` launcher | 仅查 `.venv` | Windows 用户可能未创建 venv，`py` launcher 是官方推荐入口 |
| capabilities 放单个 default.json | 按窗口/功能拆分 | 当前只有单窗口，无需拆分 |
| 图标用占位 PNG 转 ico | 用官方 tauri icon 命令生成 | 当前环境可能无 `tauri icon` CLI，用脚本生成最小可用图标 |

## Open Questions

- [ ] 是否需要支持后端进程崩溃后自动重启？（本次不做，标记为后续）
- [ ] macOS/Linux 的 Python 路径是否需要额外回退？（当前 `python3` 系统命令回退可后续加）
