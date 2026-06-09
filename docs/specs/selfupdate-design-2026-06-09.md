# voidx 自升级机制 — 技术设计文档

## Context

voidx 目前没有自升级能力。用户升级需要手动执行 `pip install --upgrade voidx` 或 `npm update -g @chikhamx/voidx`，体验割裂。作为 CLI 工具，启动时自动检查并升级是用户期望的基线行为。

## Goals and Non-Goals

### Goals

- 启动时异步检查 PyPI 最新版本，默认自动升级
- 提供 `/upgrade` 命令，支持手动触发和配置开关
- 升级完成后提示用户重启，下次启动即生效
- 兼容三种安装方式（pip / npm / install.sh），共享同一升级路径

### Non-Goals

- 不做热重载——升级后需重启才能使用新版本
- 不做 npm launcher 自身的升级（npm 包升级仍需 `npm update`）
- 不做回滚机制
- 不做预发布 / canary channel

## Architecture

```
启动流程
  │
  ▼
run_loop.run()
  │
  ├─ _show_startup()
  │
  ├─ _start_upgrade_check()  ← 异步，不阻塞 REPL
  │     │
  │     ▼
  │   selfupdate.check_and_upgrade(auto=配置值)
  │     │
  │     ├─ fetch_latest_version()  → PyPI JSON API
  │     │
  │     ├─ is_newer(latest, current)
  │     │
  │     ├─ auto=true → perform_upgrade(latest)
  │     │     └─ venv/bin/pip install --upgrade voidx==X.Y.Z
  │     │     └─ 更新 .voidx-install-version marker
  │     │
  │     └─ auto=false → 只在 dock 显示提示
  │
  ▼
REPL 主循环
```

### 模块边界

| 模块 | 职责 |
|------|------|
| `src/voidx/selfupdate.py` | 版本检查、升级执行、marker 更新（纯逻辑，无 UI） |
| `src/voidx/agent/graph/run_loop.py` | 启动时调度异步升级检查 |
| `src/voidx/agent/slash/handler.py` | `/upgrade` 命令分发 |
| `src/voidx/config/settings.py` | `auto_upgrade` 配置读写 |

### 关键接口

```python
# selfupdate.py

async def fetch_latest_version() -> str | None
    """查 PyPI JSON API，返回最新版本号。结果进程内缓存。"""

def is_newer(latest: str, current: str = __version__) -> bool
    """比较版本号。"""

async def perform_upgrade(version: str | None = None) -> tuple[bool, str]
    """在当前 venv 内执行 pip install --upgrade voidx==version。
    返回 (成功, 消息)。"""

async def check_and_upgrade(*, auto: bool = True) -> tuple[bool, str]
    """检查 + 可选升级。返回 (是否升级了, 消息)。"""
```

## Data Model

### 配置

在 `settings.json` 中新增：

```
auto_upgrade
├── enabled: bool (default: true)
```

存储位置：`.voidx/settings.json` → `auto_upgrade.enabled`

### Marker 文件

现有 marker `.voidx-install-version` 格式：

```
2.2.0
20260602
3.12.13
```

自升级只更新第一行（版本号），保留 PBS_TAG 和 PBS_CPYTHON 不变。这样下次 npm launcher 启动时 marker 仍匹配，不会重复安装。

## API Contract

### `/upgrade` 命令

| 子命令 | 行为 |
|--------|------|
| `/upgrade` | 等同 `/upgrade check` |
| `/upgrade check` | 检查 PyPI 最新版，显示结果 |
| `/upgrade now` | 立即执行升级 |
| `/upgrade on` | 开启自动升级 |
| `/upgrade off` | 关闭自动升级（仅提示） |

### 启动时升级检查

- 在 `_show_startup()` 之后、REPL 主循环之前，用 `asyncio.create_task()` 启动
- 不阻塞 REPL 进入
- 检查完成后通过 dock 显示结果

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| PyPI 不可达 | 静默跳过，不显示任何提示 |
| PyPI 返回异常 JSON | 同上，log.debug 记录 |
| pip install 失败 | dock 显示升级失败提示，不影响当前会话 |
| 不在 venv 内运行 | `/upgrade now` 提示无法自升级，建议手动 pip install |
| 升级超时（120s） | 视为失败，显示超时提示 |
| marker 文件不存在 | 升级后不写 marker（非 npm/install.sh 安装，不影响） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 PyPI JSON API 查版本 | `pip index versions` / GitHub releases API | 纯 HTTP，无需 pip 子进程，延迟低 |
| 升级后需重启 | 热重载模块 | 热重载复杂且不可靠，CLI 重启成本低 |
| 只升级 Python 包 | 同时升级 npm launcher | npm launcher 版本与 Python 包解耦，且 npm 升级需不同机制 |
| 默认自动升级 | 默认只提示 | CLI 工具用户期望自动更新，类似 brew auto-update |
| marker 只改版本行 | 重写整个 marker | 保留 PBS 信息，避免 npm launcher 误判需重装 |

## Open Questions

- [ ] 升级检查频率：是否需要节流（如每天最多检查一次）？还是每次启动都查？
- [ ] 是否需要 `/upgrade changelog` 显示新版本变更？
