# 版本号单一源头 — 技术设计文档

Date: 2026-06-28

> **Status: Design** — 将版本号源头收敛到 `src/voidx/__init__.py` 一处,其余文件动态引用或由 bump 脚本同步。

## Context

当前版本号 `3.3.1` 硬编码在 5 个文件里(`docs/releasing.md` 的 Version Files 清单):

| # | 文件 | 当前写法 |
|---|------|----------|
| 1 | `pyproject.toml` | `version = "3.3.1"` |
| 2 | `src/voidx/__init__.py` | `__version__ = "3.3.1"` |
| 3 | `npm/package.json` | `"version": "3.3.1"` |
| 4 | `scripts/install.sh` | `VERSION="${VOIDX_VERSION:-3.3.1}"` |
| 5 | `scripts/install.ps1` | `$Version = ... else { "3.3.1" }` |

每次发版要手动改 5 处,漏一个就出问题(`docs/releasing.md` 的 Common Pitfalls 专门记录了这类事故)。
目标是让版本号只需在 `__init__.py` 一处修改,其余文件自动引用或由脚本同步。

## Goals and Non-Goals

### Goals

- `pyproject.toml` 改为动态读 `src/voidx/__init__.py` 的 `__version__`,消除文件 1 与 2 的重复。
- 新增 `scripts/bump_version.py`:输入新版本号,自动同步 `__init__.py`(源头)、`npm/package.json`、
  `scripts/install.sh`、`scripts/install.ps1`,并跑一致性校验。
- `scripts/package.py` 的版本校验逻辑适配动态版本(源头改为 `__init__.py`)。
- `voidx_publish.sh` 的版本号读取适配动态版本(从 `pyproject.toml` 改为读 `__init__.py`)。
- `docs/releasing.md` 的 Version Files 清单更新:源头改为 `__init__.py`,发版流程改为"改一处 + 跑 bump 脚本"。

### Non-Goals

- 不改 npm 工具链——`npm/package.json` 的 `version` 必须是静态字符串,无法动态引用,只能由 bump 脚本同步。
- 不改 install 脚本的运行时行为——它们的默认版本号是用户安装时的 fallback,必须静态,只能由 bump 脚本同步。
- 不自动 bump 版本号——bump 脚本只负责同步,不决定版本号怎么变(由人按 Version Policy 决定)。

## Architecture

### 版本号流向

```
src/voidx/__init__.py  ← 唯一源头(人工修改)
        │
        ├─ pyproject.toml          ← 动态引用(setuptools attr)
        ├─ npm/package.json        ← bump 脚本同步
        ├─ scripts/install.sh      ← bump 脚本同步
        └─ scripts/install.ps1     ← bump 脚本同步
```

### pyproject.toml 动态版本

按 PEP 621 标准改为:

```toml
[project]
name = "voidx"
dynamic = ["version"]          # 替换原 version = "3.3.1"

[tool.setuptools.dynamic]
version = {attr = "voidx.__version__"}
```

setuptools 构建时会 import `voidx.__version__`,无需在 `pyproject.toml` 里写死版本号。

### bump_version.py 职责

```
输入: ./python.sh scripts/bump_version.py 3.4.0
  │
  ├─ 1. 校验版本号格式(semver: X.Y.Z)
  ├─ 2. 改 src/voidx/__init__.py 的 __version__
  ├─ 3. 同步 npm/package.json 的 version
  ├─ 4. 同步 scripts/install.sh 的 VERSION 默认值
  ├─ 5. 同步 scripts/install.ps1 的 $Version 默认值
  ├─ 6. 跑一致性校验(读回 4 个文件,确认版本号一致)
  └─ 7. 打印变更摘要
```

## API Contract

### `scripts/bump_version.py`

- **Signature**: `./python.sh scripts/bump_version.py <version>`
- **参数**: `<version>` — 目标版本号,必须匹配 `^\d+\.\d+\.\d+$`
- **行为**:
  1. 解析 `<version>`,校验 semver 格式,不通过则退出码 1。
  2. 读取 `src/voidx/__init__.py`,正则替换 `__version__ = "..."`。
  3. 读取 `npm/package.json`,JSON 解析后改 `version` 字段,写回(保持 2 空格缩进)。
  4. 读取 `scripts/install.sh`,正则替换 `VERSION="${VOIDX_VERSION:-X.Y.Z}"` 里的版本号。
  5. 读取 `scripts/install.ps1`,正则替换 `$Version = ... else { "X.Y.Z" }` 里的版本号。
  6. 读回 4 个文件,确认版本号都等于目标值,不一致则退出码 1。
  7. 打印每个文件的变更摘要。
- **退出码**: 0 成功,1 失败(格式错误或校验不一致)。

### `scripts/package.py` 适配

`_check_release_metadata()`(第 88-118 行)当前从 `pyproject.toml` 读 `project_version` 作为
校验基准。动态版本后 `pyproject["project"]["version"]` 不存在,改为:

- 基准版本改为从 `src/voidx/__init__.py` 读 `__version__`(即 `init_version`)。
- 校验 `npm/package.json` 的版本号是否等于 `__init__.py` 的版本号。
- 不再校验 `pyproject.toml`(它动态引用 `__init__.py`,无需静态校验)。

### `voidx_publish.sh` 适配

第 11-16 行当前从 `pyproject.toml` 读版本号:

```python
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
```

动态版本后 `pyproject["project"]["version"]` 不存在,改为从 `__init__.py` 读:

```python
import re
text = open("src/voidx/__init__.py").read()
print(re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1))
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 版本号格式不合法(非 X.Y.Z) | 打印错误,退出码 1,不修改任何文件 |
| `__init__.py` 里找不到 `__version__` | 打印错误,退出码 1 |
| `npm/package.json` JSON 解析失败 | 打印错误,退出码 1 |
| install 脚本里找不到版本号模式 | 打印错误,退出码 1(说明脚本格式变了,bump 脚本需更新) |
| 同步后读回校验不一致 | 打印哪个文件不一致,退出码 1 |

所有修改在内存中完成,校验通过后才写盘——避免改了一半失败导致状态不一致。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `__init__.py` 作为唯一源头 | `pyproject.toml` 作为源头 | `__version__` 是运行时 import 的值,被 `main.py`/`selfupdate.py`/`ui/session.py` 引用;`pyproject.toml` 只在构建时读,运行时不可访问 |
| `pyproject.toml` 动态引用 | bump 脚本同步 `pyproject.toml` | PEP 621 标准做法,彻底消除该文件的手动同步;setuptools 原生支持 |
| npm/install 脚本由 bump 脚本同步 | 运行时动态读取 | npm 要求静态 `version`;install 脚本在用户机器运行,无源码可读,必须静态 |
| bump 脚本先内存修改再写盘 | 边读边写 | 避免改了一半失败导致文件状态不一致 |
| `package.py` 校验基准改为 `__init__.py` | 保留校验 `pyproject.toml` | 动态版本后 `pyproject.toml` 无静态版本号,无法作为基准;`__init__.py` 是新源头 |

## Open Questions

- [ ] `pyproject.toml` 动态版本是否影响 `scripts/package.py` 的构建流程?需在实现后跑一次
      `./python.sh scripts/package.py --check-only` 确认 setuptools 能正确解析 `attr = "voidx.__version__"`。
