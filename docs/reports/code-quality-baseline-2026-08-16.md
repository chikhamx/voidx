# 代码质量基线报告：src/

日期：2026-08-16
范围：`src/` 目录（Python 后端核心）
用途：作为后续持续优化的基线，每次优化后更新本报告中的状态。

## 结论

总体质量良好。测试投入（测试代码量超过源码 1.3 倍）、分层架构约束和自动化架构违规检查是突出优点；主要短板是缺少 lint/静态类型检查工具链、架构违规尚未清零、少数文件过大。

## 规模指标

| 指标 | 数值 |
|---|---|
| src 全部 .py 文件 | 1012 |
| src 总行数 | 178,003 行 |
| 非空行 | 147,012 行（纯注释 1,302 行） |
| 源码（不含 tests） | 574 文件 / 76,406 行 |
| 测试代码（src/tests） | 438 文件 / 101,597 行 |
| 测试/源码行数比 | 1.33 |

## 测试质量

| 指标 | 数值 |
|---|---|
| 测试结果 | 4426 passed / 30 skipped / 0 failed |
| 运行时长 | ~118 秒 |
| 覆盖率 | 未测（pytest-cov 未安装） |

## 架构质量

- 采用 domain / ports / application / adapters 分层，`src/AGENTS.md` 定义依赖方向，bootstrap 为唯一组合根。
- 存在自动化架构违规检查（`src/tests/test_architecture`），违规报告输出到 `.voidx/architecture-violations*.txt`。
- 当前违规：**15 处 source、11 个文件**（相比更早基线 24 处已在收敛）。
- 主要违规模式：application 层直接依赖 `agent.adapters.persistence`（`agent_service`、`goal_service`、`scheduler`、`dispatcher` 等），属分层倒置。

## 代码风格与类型

| 指标 | 数值 |
|---|---|
| 函数总数 | 4402 |
| 有返回类型标注 | 3520（80%） |
| 无返回类型标注 | 882（20%） |
| TODO/FIXME/XXX | 15 处 |
| lint 工具配置 | 无（ruff/pylint/flake8 均未配置） |
| 类型检查工具 | 无（mypy/pyright 未配置） |

## 最大文件（Top 5）

| 文件 | 行数 |
|---|---|
| `agent/adapters/langgraph/execution.py` | 1411 |
| `agent/adapters/langgraph/runtime/subagent.py` | 1166 |
| `presentation/output/tree.py` | 1047 |
| `agent/adapters/langgraph/runtime/compaction_coordinator.py` | 890 |
| `presentation/adapters/persistence/transcript_snapshot.py` | 844 |

## 问题清单

| 优先级 | 问题 | 证据 |
|---|---|---|
| P1 | 架构违规 15 处未清零 | `.voidx/architecture-violations-now.txt` |
| P1 | 无 lint / 静态类型检查工具链 | `pyproject.toml` 无 ruff/mypy 配置 |
| P2 | 大文件：Top5 均超 800 行，最大 1411 行 | 见上表 |
| P2 | 882 个函数缺返回类型标注 | 20% 无 `->` 标注 |
| P3 | 覆盖率数据缺失 | pytest-cov 未安装 |

## 后续优化路线图

- [ ] 接入 ruff + mypy，跑通并清零（或建立基线豁免清单）
- [ ] 清理 15 处架构违规（application 层通过 ports 依赖 persistence）
- [ ] 拆分 Top5 大文件（优先 `execution.py`）
- [ ] 补齐无类型标注函数的返回类型
- [ ] 安装 pytest-cov，纳入覆盖率基线
- [ ] 清理 15 处 TODO/FIXME

## 复现命令（更新基线用）

```bash
# 规模
find src -name "*.py" -exec cat {} + | wc -l
find src -name "*.py" ! -path "*/tests/*" -exec cat {} + | wc -l
find src/tests -name "*.py" -exec cat {} + | wc -l

# 测试
./python.py -m pytest src/tests -q

# 架构违规
cat .voidx/architecture-violations-now.txt

# 类型标注缺口
grep -rn "def " src/voidx --include="*.py" | grep -v __pycache__ | grep -vE "def .*->" | wc -l
```
