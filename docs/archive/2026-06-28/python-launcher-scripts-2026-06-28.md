# 跨平台 Python 启动脚本 — 技术设计文档

Date: 2026-06-28

> **Status: Done** — `python.sh` 与 `python.ps1` 已实现并验证可用;`AGENTS.md`、`docs/dev-guide.md` 已改用这两个脚本作为 Python 入口。

## Context

voidx 通过 `scripts/install.sh`(Unix)与 `scripts/install.ps1`(Windows)安装,会在
`VOIDX_HOME` 下创建独立 venv:

- Unix: `${XDG_DATA_HOME:-$HOME/.local/share}/voidx/venv/bin/python`
- Windows: `%LOCALAPPDATA%\voidx\venv\Scripts\python.exe`

当前 `AGENTS.md` 的命令直接写死 `.venv/bin/python`,这在两个层面有问题:

1. **路径不对** — 项目根的 `.venv` 是开发 venv,不是 voidx 安装 venv。运行 voidx
   应该用安装目录里的 Python。
2. **平台差异** — Unix 是 `bin/python`,Windows 是 `Scripts\python.exe`,命令无法跨平台
   复用,每条都要标注两种写法,冗余且易错。

本设计用两个原生脚本封装这种差异,调用方只需 `./python.sh ...` 或 `.\python.ps1 ...`。

## Goals and Non-Goals

### Goals

- 提供 `python.sh`(Unix)与 `python.ps1`(Windows)两个脚本,各自定位 `VOIDX_HOME` 下的
  venv Python 并转发所有参数。
- 支持 `VOIDX_HOME` 环境变量覆盖,与 install 脚本的约定一致。
- venv 缺失时给出清晰错误提示,指向 install 脚本。
- `AGENTS.md` 与 `docs/dev-guide.md` 改为引用这两个脚本,命令不再写死平台路径。

### Non-Goals

- 不创建项目根 `.venv` 的封装(那是开发 venv,由开发者自行管理)。
- 不提供单文件跨平台方案(纯 Python helper 等)——用户明确要求 `.sh` + `.ps1` 双脚本,
  与现有 `install.sh` / `install.ps1` 范式一致。
- 不处理 venv 创建/重建——那是 install 脚本的职责。

## Architecture

两个脚本是对称的薄封装,职责单一:定位 → 校验 → 转发。

```
调用方                脚本                    venv Python
───────              ────                    ───────────
./python.sh -m pytest ... ──┐
                            ├─ 解析 VOIDX_HOME ──► $VOIDX_HOME/venv/bin/python
.\python.ps1 -m pytest ... ─┤   校验可执行文件存在
                            └─ exec / & 转发参数 ──► python -m pytest ...
```

### 路径解析约定

与 install 脚本完全对齐:

| 平台    | VOIDX_HOME 默认值                         | venv Python 路径                              |
|---------|------------------------------------------|-----------------------------------------------|
| Unix    | `${XDG_DATA_HOME:-$HOME/.local/share}/voidx` | `$VOIDX_HOME/venv/bin/python`                |
| Windows | `$env:LOCALAPPDATA\voidx`                 | `$VOIDXHome\venv\Scripts\python.exe`          |

`VOIDX_HOME` 环境变量在两个脚本里都优先读取,允许自定义安装位置。

### 放置位置

项目根目录(`D:\chikham\voidx\python.sh`、`python.ps1`),与 `pyproject.toml`、
`AGENTS.md` 同级。理由:

- 调用最短:`./python.sh ...` / `.\python.ps1 ...`,无需 `scripts/` 前缀。
- 作为项目级入口显眼,与 `pyproject.toml` 的 `[project.scripts] voidx = ...` 呼应。

## API Contract

### `python.sh`

- **Signature**: `./python.sh <args>...`
- **行为**:
  1. `ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"` — 定位脚本所在目录(项目根)。
  2. `VOIDX_HOME="${VOIDX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/voidx}"` — 解析安装目录。
  3. `PY="$VOIDX_HOME/venv/bin/python"` — 定位 venv Python。
  4. 若 `[ ! -x "$PY" ]`,打印红色错误(指向 `scripts/install.sh`),`exit 1`。
  5. `exec "$PY" "$@"` — 替换当前进程,转发所有参数。
- **退出码**: 透传 Python 进程退出码(`exec` 后由 Python 决定)。

### `python.ps1`

- **Signature**: `.\python.ps1 <args>...`
- **行为**:
  1. `$Root = $PSScriptRoot` — 定位脚本所在目录。
  2. `$VoidxHome = if ($env:VOIDX_HOME) { $env:VOIDX_HOME } else { Join-Path $env:LOCALAPPDATA "voidx" }`。
  3. `$Py = Join-Path $VoidxHome "venv\Scripts\python.exe"`。
  4. 若 `-not (Test-Path $Py)`,红色错误(指向 `scripts/install.ps1`),`exit 1`。
  5. `& $Py @args` — 调用并转发所有参数。
  6. `exit $LASTEXITCODE` — 透传退出码。
- **退出码**: 透传 `$LASTEXITCODE`。

## Error Handling

| 失败场景                | 处理策略                                                                 |
|-------------------------|--------------------------------------------------------------------------|
| `VOIDX_HOME` 下无 venv  | 红色 `❌` 提示 Python 未找到,指向对应 install 脚本,退出码 1              |
| `VOIDX_HOME` 未设置且默认路径不存在 | 同上(默认路径解析后仍指向同一 venv,校验失败逻辑一致)         |
| 参数为空                | 不特殊处理,直接转发给 Python(Python 自身会报错或进入 REPL)             |

错误提示风格对齐 `install.sh` / `install.ps1`:红色 `❌` + 简洁原因 + 修复指引。

## Decisions Log

| 决策                          | 备选方案                          | 选择理由                                                                 |
|-------------------------------|-----------------------------------|--------------------------------------------------------------------------|
| 双脚本 `.sh` + `.ps1`         | 纯 Python `scripts/run.py`        | 用户明确要求;与现有 `install.sh`/`install.ps1` 范式一致;调用更短       |
| 放项目根而非 `scripts/`       | 放 `scripts/python.sh`            | 调用更短(`./python.sh` vs `./scripts/python.sh`);作为项目级入口更显眼   |
| 指向 `VOIDX_HOME/venv`        | 指向项目根 `.venv`                | 运行 voidx 应用应用安装目录的 venv;项目根 `.venv` 是开发环境,职责不同   |
| `exec` 替换进程(Unix)        | `bash -c "$PY ..."` 子进程        | `exec` 让信号/退出码直接透传,无中间 shell 开销                          |
| 不创建 venv                   | 脚本内自动 `python -m venv`       | venv 创建是 install 脚本职责;helper 只负责定位与转发,职责单一          |

## Open Questions

- [ ] `python.sh` 是否需要 `chmod +x`?当前设计即便无执行位也能 `bash python.sh ...` 跑,
      但 `./python.sh` 需要执行位。建议在 install 脚本或文档里提示 `chmod +x python.sh`。
