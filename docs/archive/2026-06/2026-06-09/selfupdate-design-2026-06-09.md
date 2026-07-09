# voidx 自升级检查机制 — 技术设计文档

> **Status: Done** — V1 已实现：启动检查并提示，不强制自动升级；只有用户显式执行 `/upgrade now` 时才尝试安装。

## Context

voidx 目前没有内置版本检查能力。用户需要自行执行 `pip install --upgrade voidx`、`npm update -g @chikhamx/voidx` 或重新运行 `install.sh` 才能知道并获取新版本。

V1 不做强制自动升级。启动时只异步检查 PyPI 最新版本并提示用户；真正安装新版本只在用户显式执行 `/upgrade now` 时发生。

## Goals and Non-Goals

### Goals

- 启动时异步检查 PyPI 最新版本，不阻塞 REPL
- 默认启用检查和提示，但不自动安装
- 提供 `/upgrade` 命令，支持手动检查、手动升级、开启/关闭检查
- 升级完成后提示用户重启，下次启动生效
- 不破坏 npm launcher / install.sh 的现有 marker 机制

### Non-Goals

- 不做后台自动安装
- 不做热重载，升级后需重启
- 不升级 npm launcher 自身
- 不修改 `.voidx-install-version` marker
- 不做回滚机制
- 不做预发布 / canary channel
- 不做 changelog 展示

## Architecture

```
启动流程
  │
  ▼
run_loop.run()
  │
  ├─ _show_startup()
  │
  ├─ _show_update_check_if_needed()  ← asyncio.create_task，不阻塞 REPL
  │     │
  │     ▼
  │   selfupdate.check_for_update()
  │     │
  │     ├─ TTL 未到 → 跳过
  │     ├─ fetch_latest_version() → PyPI JSON API
  │     ├─ is_newer(latest, current)
  │     └─ 有新版 → dock 提示用户执行 /upgrade now 或包管理器命令
  │
  ▼
REPL 主循环
```

### 模块边界

| 模块 | 职责 |
|------|------|
| `src/voidx/selfupdate.py` | 版本检查、版本比较、显式升级执行；纯逻辑，无 UI |
| `src/voidx/config/settings_update.py` | `update_check` 配置读写、TTL 状态 |
| `src/voidx/agent/graph/run_loop.py` | 启动时调度异步检查，把结果渲染到 dock |
| `src/voidx/agent/slash/upgrade.py` | `/upgrade` 命令实现 |
| `src/voidx/agent/slash/handler.py` | `/upgrade` 命令分发 |
| `src/voidx/ui/commands.py` | 命令面板条目 |

### 关键接口

```python
# selfupdate.py

@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str | None
    update_available: bool
    message: str
    error: str | None = None

@dataclass(frozen=True)
class UpgradeResult:
    ok: bool
    version: str | None
    message: str

async def fetch_latest_version(package: str = "voidx", timeout: float = 5.0) -> str | None:
    """查 PyPI JSON API，返回 info.version。"""

def is_newer(latest: str, current: str = __version__) -> bool:
    """比较稳定版本号；预发布版本不作为升级目标。"""

async def check_for_update(current: str = __version__) -> UpdateCheckResult:
    """检查 PyPI 是否有新版。网络失败时返回 error，不抛给 UI 层。"""

async def perform_upgrade(version: str | None = None, timeout: float = 120.0) -> UpgradeResult:
    """用户显式触发时，用当前 Python 解释器执行升级。"""
```

`perform_upgrade()` 使用：

```python
[sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir", f"voidx=={version}"]
```

不使用 `venv/bin/pip`，避免 Windows、pipx、uv tool 等环境路径不一致。

## Data Model

### 配置

在 `.voidx/settings.json` 中新增：

```json
{
  "update_check": {
    "enabled": true,
    "last_checked_at": 1780000000,
    "last_latest_version": "2.2.2"
  }
}
```

字段：

- `enabled`: 是否启动时自动检查，默认 `true`
- `last_checked_at`: 最近一次启动检查时间，Unix seconds
- `last_latest_version`: 最近一次检查到的 PyPI 最新版本，仅用于调试/展示

### 检查频率

- 启动检查默认 24 小时最多一次
- `/upgrade check` 忽略 TTL，强制检查
- 网络失败也会更新 `last_checked_at`，避免每次启动重复打 PyPI

### Marker 文件

V1 不修改 `.voidx-install-version`。

原因：

- npm launcher 要求 marker 完全等于 npm 包版本、PBS tag、Python 版本；如果 Python 包自升级后改 marker，下次 npm 启动可能把用户降级回 npm 包版本
- Bash installer marker 是三行，PowerShell installer marker 是四行，不能假设固定格式
- install.sh 直接运行 venv 内的 `voidx`，升级 Python 包后已经能持久生效，不需要 marker 参与

因此 marker 只由 npm launcher / install.sh / install.ps1 自己维护。

## API Contract

### `/upgrade` 命令

| 子命令 | 行为 |
|--------|------|
| `/upgrade` | 等同 `/upgrade check` |
| `/upgrade check` | 强制检查 PyPI 最新版并显示结果 |
| `/upgrade now` | 显式执行 Python 包升级；成功后提示重启 |
| `/upgrade on` | 开启启动时检查 |
| `/upgrade off` | 关闭启动时检查 |
| `/upgrade status` | 显示当前检查开关和最近检查版本 |

如果 `.voidx/settings.json` 中有 24 小时内检查到的稳定新版，`/upgrade now` 会直接使用该版本，避免用户刚执行 `/upgrade check` 后又重复请求 PyPI。没有可用缓存时才重新检查。

当检测到 npm launcher 环境时，`/upgrade now` 不执行 pip 升级，提示用户运行：

```bash
npm update -g @chikhamx/voidx
```

### 启动时检查

- 在 startup UI 显示后，用 `asyncio.create_task()` 启动
- 不阻塞 REPL 进入
- TTL 未到时不请求网络
- 有新版时通过 dock 显示提示
- 无新版或网络失败时静默
- 退出时取消未完成的检查任务

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| PyPI 不可达 | 启动检查静默；`/upgrade check` 显示失败原因 |
| PyPI 返回异常 JSON | 同上，log.debug 记录 |
| latest 是预发布版本 | 当作无可用稳定升级 |
| pip install 失败 | 显示失败消息，不影响当前会话 |
| 不在虚拟环境内运行 | `/upgrade now` 提示使用包管理器手动升级 |
| npm launcher 环境 | `/upgrade now` 提示使用 `npm update -g @chikhamx/voidx` |
| 升级超时 | 终止子进程，显示超时提示 |

## Test Plan

| 测试 | 覆盖 |
|------|------|
| `test_is_newer_*` | 版本比较、预发布过滤 |
| `test_check_for_update_*` | PyPI 响应、网络失败、无新版 |
| `test_perform_upgrade_*` | pip 命令、npm 环境拒绝、非 venv 拒绝、超时 |
| `test_update_check_settings_round_trip` | 配置默认值、开关、TTL 状态 |
| `test_slash_upgrade_*` | `/upgrade check/now/on/off/status` 分发 |
| `test_startup_update_check_*` | 启动检查非阻塞、TTL 跳过、新版提示 |
| `test_npm_launcher_marks_environment` | npm launcher 启动 Python 时注入 npm 环境变量 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 默认检查并提示，不自动安装 | 默认自动升级 | 避免启动时隐式联网改环境；用户显式 `/upgrade now` 才安装 |
| 用 PyPI JSON API 查版本 | `pip index versions` / GitHub releases API | 纯 HTTP，无需 pip 子进程，延迟低 |
| 24 小时 TTL | 每次启动都查 | 减少启动噪声和网络请求 |
| 不修改 marker | 更新 marker 第一行 | 避免 npm launcher 降级/重装；保留 installer 对 marker 的所有权 |
| 用 `sys.executable -m pip` | `venv/bin/pip` | 跨平台，适配 pipx/uv tool/Windows |
| npm launcher 环境拒绝 `/upgrade now` | 直接 pip 升级 | npm 下次启动可能降级，必须用 npm 更新 launcher 和 Python 包 |

## Open Questions

- [ ] 后续是否增加 `/upgrade changelog`
- [ ] 后续是否支持显式 opt-in 的后台自动安装
