# 统一测试入口 (scripts/test.py) — 技术设计文档

## Context

voidx 当前有三套独立测试，分散在不同目录、用不同工具链运行：

| 套件 | 工具 | 命令 | 目录 |
|------|------|------|------|
| 后端 Python | pytest | `./python.py -m pytest src/tests tui/tests` | `src/tests/`, `tui/tests/` |
| 前端 TS | vitest | `cd frontend && npm test` | `frontend/test/` |
| Desktop Rust | cargo test | `cd desktop/tauri && cargo test` | `desktop/tauri/tests/` |

没有统一入口，跑全套需手动执行 3 条命令，且无法一眼看出整体通过/失败状态。
本设计新增 `scripts/test.py`，与 `package.py`/`release.py` 风格一致，聚合三套测试。

## Goals and Non-Goals

### Goals
- 一条命令跑完三套测试，聚合结果
- 工具链缺失时优雅跳过（如环境无 cargo），不报错
- 支持选择性运行单套（`--backend` / `--frontend` / `--desktop`）
- 退出码反映整体结果（任一失败则非零）

### Non-Goals
- 打包发布流程（已有 `package.py` / `release.py`）
- CI pipeline 编排（本脚本只管本地测试）
- 测试覆盖率报告

## Architecture

```
scripts/test.py
├── main()                              # argparse 入口，调度 suites
├── _has_cmd(name)                      # shutil.which 封装，检查可执行文件是否在 PATH
├── _run_backend(extra_args, verbose)   # [sys.executable] -m pytest src/tests tui/tests [-v] [*extra]
├── _run_frontend(extra_args, verbose)  # [npm, test, --, *extra]  cwd=ROOT/"frontend"
├── _run_desktop(extra_args, verbose)   # [cargo, test, *extra]    cwd=ROOT/"desktop/tauri"
└── _summarize(results)                 # 打印汇总表，返回聚合退出码

注意：脚本由 ./python.py 执行，但内部不再调 python.py——
直接用 sys.executable（当前 venv 的 Python）运行 pytest，避免套娃。
各 _run_* 内部用 subprocess.run(cwd=...) 切到对应目录，调用方无需关心。
```

### 执行流程

```
main()
  ├── 解析参数 (--backend/--frontend/--desktop/--verbose/--keep-going)
  ├── 解析透传参数: -- 之后的参数按套件归属转发
  │     例: ./test.py backend -- -x --tb=short   → -x --tb=short 转给 pytest
  │         ./test.py frontend -- --reporter=dot → --reporter=dot 转给 vitest
  │         ./test.py -- -k fast                 → 全套件都收到 -k fast（各工具链自行解释）
  ├── 无选择参数 → 全跑
  ├── 依次执行选中的 suites
  │     ├── 检测工具链 → 缺失则 SKIP
  │     ├── 运行测试 → 收集 (name, status, exit_code)
  │     └── 失败且无 --keep-going → 立即返回
  └── _summarize() 打印汇总表，返回退出码
```

## API Contract

### CLI 接口

```
./python.py scripts/test.py [OPTIONS] [-- EXTRA_ARGS...]

Options:
  --backend       只跑后端 pytest
  --frontend      只跑前端 vitest
  --desktop       只跑 desktop cargo test
  --verbose, -v   详细输出（各套件等价：pytest -v / vitest 无额外参数 / cargo test -- --nocapture）
  --keep-going    失败后继续跑剩余套件（默认遇错即停）

透传参数（-- 之后）按套件归属原样转发：
  ./test.py backend -- -x --tb=short        # -x --tb=short → pytest
  ./test.py frontend -- --reporter=dot      # --reporter=dot → vitest
  ./test.py -- -k fast                      # 全套件都收到 -k fast（各工具链自行解释）

无 `--backend/--frontend/--desktop` 参数时默认全跑。

### 退出码

| 值 | 含义 |
|----|------|
| 0 | 所有运行的套件通过（含全部跳过的情况）|
| 1 | 至少一个套件失败 |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| cargo 不可用 | 跳过 desktop 套件，打印 `⏭ desktop: skipped (cargo not found)` |
| npm/node 不可用 | 跳过 frontend 套件，打印 `⏭ frontend: skipped (npm not found)` |
| 前端 node_modules 缺失 | npm test 会报错，exit code 非 0，正常计入失败 |
| 某套件失败 | 默认立即停止返回 1；`--keep-going` 时继续跑剩余套件 |
| 测试进程被信号杀死 | 视为失败，exit code 取负值转正 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| Python 脚本 | Makefile / shell 脚本 | 与 package.py/release.py 风格一致，跨平台 |
| 默认遇错即停 | 默认全跑完再汇总 | 快速定位失败，`--keep-going` 保留全跑选项 |
| 工具链缺失跳过而非报错 | 报错退出 | 开发者可能只装了部分工具链（如无 Rust）|
| 后端显式指定路径 | 依赖 pyproject testpaths | 避免歧义，确保 src/tests + tui/tests 都跑到 |
| 内部用 sys.executable 而非调 python.py | 套娃调 python.py | 脚本已在 venv Python 下运行，直接用 sys.executable，与 package.py 一致 |
| cwd 由 _run_* 内部管理 | 调用方传 cwd 参数 | 调用方只需选套件，目录切换是实现细节 |
| `--` 透传各套件独立参数 | 统一参数翻译 | 各工具链参数差异大，透传最灵活、无信息损失 |

## Open Questions

- [ ] 是否需要 `--fast` 模式（只跑受影响套件）？暂不实现，保持简单。
