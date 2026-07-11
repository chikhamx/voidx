# Structured Test Results — Implementation Spec

> **Status: Done** — Archived on 2026-07-11.

## Objective

将 `test.py` 的裸字符串状态和元组结果重构为结构化数据，使 LLM 消费方能直接解析状态、定位失败、复现命令，无需猜测或正则解析。

## Source of Truth

| Source | Path / Link | Notes |
|--------|-------------|-------|
| Existing Code | `test.py` | 当前 22 处裸字符串散落 |
| Existing Tests | `src/tests/test_test_runner.py` | 分类函数单元测试 |
| Design Decision | 见对话记录 | SuiteStatus 枚举 + dataclass 方案 |

## Current Behavior

`test.py` 当前用 `(suite_name: str, status: str, code: int)` 三元组传递结果：

```python
results.append((suite, "FAIL", 0))     # 错误详情丢失
results.append((suite, "SKIP", 0))     # 跳过原因丢失
```

- 状态是裸字符串 `"PASS"/"FAIL"/"SKIP"/"ERROR"`，无类型保护
- 退出码逻辑隐式绑定字符串：`"ERROR"` → exit 2
- 摘要信息混在 status 字符串里，LLM 需正则解析

## Target Behavior

### 数据结构

```python
from enum import Enum
from dataclasses import dataclass, field

class SuiteStatus(str, Enum):
    """Suite 执行终态。str mixin 保持向后兼容：`"PASS" == SuiteStatus.PASS"`。"""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"

@dataclass
class TestFailure:
    """单个测试失败记录。"""
    test_id: str                    # 完整标识: "test_foo.py::test_bar"
    file_path: str                  # 文件路径: "src/tests/test_foo.py"
    test_name: str                  # 测试函数名: "test_bar"
    message: str                    # 失败信息（断言错误/异常堆栈）

@dataclass
class SkipRecord:
    """单条跳过记录。"""
    reason: str                     # 为什么跳过（如 "npm not found"）

@dataclass
class SuiteResult:
    """单个 suite 的完整结果。"""
    name: str                       # 套件名称: backend / frontend / desktop
    status: SuiteStatus             # 终态
    passed: int = 0                 # 通过数量
    failed: int = 0                 # 失败数量
    skipped: int = 0                # 跳过数量
    errors: list[TestFailure] = field(default_factory=list)
    skipped_details: list[SkipRecord] = field(default_factory=list)
    command: list[str] = field(default_factory=list)  # 实际执行的命令
    exit_code: int = 0              # 子进程原始退出码
    duration_seconds: float = 0.0   # 执行耗时（秒）
```

### 退出码映射

```python
# 显式声明：哪些状态表示 test.py 自身出错 → exit 2
_RUNNER_ERROR_STATES = {SuiteStatus.ERROR}

# main() 退出逻辑
runner_error = any(r.status in _RUNNER_ERROR_STATES for r in results)
return 2 if runner_error else 0
```

### 序列化

```python
def to_dict(result: SuiteResult) -> dict:
    return {
        "name": result.name,
        "status": result.status.value,
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "errors": [
            {
                "test_id": e.test_id,
                "file_path": e.file_path,
                "test_name": e.test_name,
                "message": e.message,
            }
            for e in result.errors
        ],
        "skipped_details": [{"reason": s.reason} for s in result.skipped_details],
        "command": result.command,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
    }
```

## Files to Change

| Path | Change Type | Required Change | Do Not Change |
|------|-------------|-----------------|---------------|
| `test.py` | modify | 新增 `SuiteStatus`/`TestFailure`/`SkipRecord`/`SuiteResult`；三个分类函数返回 `SuiteStatus`；`main()` 用 `_RUNNER_ERROR_STATES` 判断退出码；`_summarize` 消费 `SuiteResult` | 不改变命令行参数、不改变 suite 执行逻辑 |
| `src/tests/test_test_runner.py` | modify | 更新断言以匹配新类型 | 不删减测试覆盖范围 |

## Invariants

- `SuiteStatus` 是 `str` mixin 枚举——`"PASS" == SuiteStatus.PASS` 必须为 `True`
- 退出码只有 0 和 2：`0` = 正常（含测试失败），`2` = test.py 自身 bug
- `ERROR` 状态仅由 `OSError`（子进程启动失败）触发，用户代码错误一律为 `FAIL`
- `_summarize` 输出格式不变：单 suite 时 `✅ backend — passed`，多 suite 时 `✅ backend=PASS | ❌ frontend=FAIL`
- `to_dict()` 输出必须是 JSON-serializable 的纯字典

## Implementation Requirements

### Functional Requirements

- [ ] 新增 `SuiteStatus` 枚举，`str` mixin
- [ ] 新增 `TestFailure`、`SkipRecord`、`SuiteResult` dataclass
- [ ] `_classify_pytest_status` 返回 `SuiteStatus`
- [ ] `_classify_vitest_status` 返回 `SuiteStatus`
- [ ] `_classify_cargo_status` 返回 `SuiteStatus`
- [ ] `main()` 中 `results: list[SuiteResult]`，退出码用 `_RUNNER_ERROR_STATES` 判断
- [ ] `_summarize` 消费 `SuiteResult`，输出格式不变
- [ ] 新增 `to_dict()` 序列化函数

### Error Handling

- [ ] `OSError` 仍触发 `ERROR` 状态和 exit 2
- [ ] 用户代码错误（conftest.py、import 失败、编译错误）归类为 `FAIL`，exit 0

### Data / Migration Requirements

- [ ] N/A

### API / Compatibility Requirements

- [ ] `SuiteStatus.PASS == "PASS"` 必须成立（str mixin 保证）
- [ ] 不暴露 `SuiteResult` 为公共 API——仅内部使用

## Edge Cases

| Case | Required Behavior | Verification |
|------|-------------------|--------------|
| 所有测试通过 | `status=PASS, passed=N, failed=0, errors=[]` | 跑 `test_diffing.py` 确认 |
| 有测试失败 | `status=FAIL, failed>0, errors=[TestFailure(...)]` | 制造一个失败测试确认 |
| conftest.py 抛异常 | `status=FAIL`，exit 0 | 创建 broken conftest 确认 |
| 工具未安装 | `status=SKIP, skipped_details=[SkipRecord(...)]` | 确认 `_missing_reason` 填入 |
| 子进程无法启动 | `status=ERROR`，exit 2 | 确认 OSError 路径 |
| 多 suite 混合结果 | 每个 suite 独立 `SuiteResult`，退出码按最严重状态 | 同时跑 backend+frontend 确认 |

## Forbidden Changes

- Do not change CLI arguments or suite execution logic.
- Do not add new dependencies.
- Do not expose `SuiteResult` as a public API beyond `test.py`.
- Do not change the exit code contract: only `ERROR` → exit 2, everything else → exit 0.
- Do not remove or rename existing tests in `test_test_runner.py` — only update assertions.

## Tests

| Test Level | Command | Expected Result |
|------------|---------|-----------------|
| Focused | `./test.py --backend -- src/tests/test_test_runner.py` | 28 passed |
| Regression | `./test.py --backend` | 2900+ passed |
| Smoke | `./test.py --backend -- src/tests/test_tools/test_diffing.py` | exit 0, `status=PASS` |
| Edge | 创建 broken conftest.py 后跑 `--backend` | exit 0, `status=FAIL` |

## Definition of Done

- [ ] All functional requirements are implemented.
- [ ] Existing invariants still hold (str mixin, exit code contract, output format).
- [ ] Edge cases above are covered by tests or documented manual checks.
- [ ] Verification commands pass with captured output.
- [ ] No unrelated files were changed.
