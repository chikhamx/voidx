# 无头自治 SDK：S5 正式验收矩阵

> **Status: Done** — Archived on 2026-09-16.

- 日期：2026-09-15；读者：human+llm；验收对象：当前集成脏树中的**显式 SDK → SemanticGatewayBridge → Gateway/client** 链路。
- 合同：[自治会话补充规范](pure-event-driven-sdk-architecture-2026-09-13-autonomous-session.md) §8 S5，沿用其真实身份、审批、取消和恢复约束；格式承接 [S3/S4 验收记录](pure-event-driven-sdk-architecture-2026-09-13-s34-acceptance.md)。root 指 intake 根会话，child 指真实 work/evaluator/loop 执行会话；HITL 指需人确认的交互。
- **当前结论：S5 scoped PASS。** 下表合同场景已在最终集成树核验；父级独立浏览器组合 23 passed、0 skipped，1166 个后端源码哈希及关键前端哈希匹配。结论仅适用于显式 SDK/Gateway/client 链路，不授权 S6 生产入口迁移或主规范 Stage D。
- 本次只新增本报告，不改主规范/源码，不归档，不启用生产入口。没有将“仅 `[completed]`、实际报告缺失”的独立审查算作通过；本报告是源码与运行产物交叉核验，不声称完成独立审查。

## 1. 证据基准与可信范围

运行 cwd 为 `/Users/chikham/workspace/voidx`，macOS arm64；通过 `./python.py test.py` 使用项目 venv。被测为已有修改及未跟踪文件组成的集成树，不能仅用 HEAD 表示。以下结果来自实际读取的本地报告/日志，文档代理未重跑功能测试；父级在最终审阅期间单独重跑浏览器组合。

| 证据 | 实际结果及用途 |
| --- | --- |
| `/tmp/voidx-s5-continuation-parent-backend-J2Hnk3` | 最新父级全 backend：`results[0].status=PASS`、passed **6978**、failed 0、skipped **32**、suite/top-level exit_code 均 0；5 warnings，runner 248.43 s。覆盖 backend，不把 skip 当通过或推导 desktop/front-end 全绿。 |
| `/tmp/voidx-sdk-s5-continuation-process-summary.json` | 最终 target 3 passed；related 1866 passed、0 skip、3 warnings。报告内 `sourcehash.files` 有 **1166** 个 `src/**/*.py` 哈希；父级已核对，本次又逐文件比对，1166 匹配、0 差异。用于关联最新 backend 与当前源码，不代替浏览器最终运行。 |
| `/tmp/voidx-sdk-s5-autonomous-browser-summary.json` | 历史真实浏览器 target 2 passed、0 skip；相关 browser 30 passed；frontend focused 99 passed、相关 1020 passed。`frontend/src/main.ts` 与当前文件哈希相符；运行早于后续 recovery 集成，故仅是业务覆盖证据。 |
| `/tmp/voidx-s5-final-browser-azzPXg` | 父级最终独立浏览器组合：既有 `test_gateway_browser.py` + `test_autonomous_gateway_browser.py`，**23 passed、0 failed、0 skipped**，nested status=PASS、exit_code=0；42.17 s，输出未列 warnings。指定三环境变量、`./python.py test.py --backend -- <两文件>`、timeout 600；关键源码哈希见 §5。 |

证据合并按合同而非报告 status：`gateway-projection` 的 PASS_FIRST_SLICE 与 `gateway-continuation` 的 PARTIAL 只说明当时覆盖边界；后续 reconnect、restart-schema-compatible、discovery-corruption、needs-resume、continuation-process 已分别提供补充。早期 restart 的 INCOMPLETE_RED 及 fixture 错误不作通过证据，也不作为当前缺陷保留。

已读取 `/tmp/voidx-sdk-s5-*` 相关 JSON 报告：`gateway-projection-summary`、`gateway-continuation-summary`、`reconnect-summary`、`restart-summary`、`restart-implementation-summary`、`restart-schema-compatible-summary`、`discovery-corruption-summary`、`needs-resume-summary`、`autonomous-browser-summary`、`continuation-process-summary`，以及 `first-integration-backend.json` 和 `restart-schema-compatible-sourcehash.json`。这些前缀统一为 `/tmp/voidx-sdk-s5-`，summary/manifest 后缀为 `.json`。历史 first-integration 的 6946 passed 不覆盖后续修改，已由最新 6978 结果取代。

## 2. 合同—实现—业务断言矩阵

下面 `P` 表示 `src/tests/test_sdk/`；每项给出真实 test function，可用 pytest node id 直接定位。模型 provider 为确定性脚本；SDK、LangGraph、工具、service/store、桥接与客户端路径实际执行，不是向投影器喂伪造语义事件。PASS 只针对列出的场景，不是所有调度交错的证明。

| S5 合同 / 判定 | 实现位置（仓库相对路径） | 测试函数与业务观察 |
| --- | --- | --- |
| root intake 审批与跨 child HITL 所有权：**PASS** | `src/voidx/bootstrap/semantic_gateway.py`；`src/voidx/presentation/gateway/semantic_interactions.py`；`frontend/src/main.ts` | `P/test_autonomous_gateway_projection.py::test_real_autonomous_gateway_projection[goal]`：root 使用 approved，child 使用 allow；错误 root thread 回答 child 请求被拒，pending 数不变；resolved 与 required 的真实三元身份一致。恢复取消场景不重新要求 root intake。浏览器的 DOM 路径另见下项。 |
| 多真实 work/evaluator、连续 loop、唯一终态、工具结果：**PASS** | `src/voidx/bootstrap/semantic_gateway.py`；`src/voidx/presentation/adapters/semantic_event_projector.py` | 同函数 `[goal]`、`[loop]`：goal 共 5 个 starts（root + 两次 work + 两次 evaluator），loop 共 3 个（root + 两轮）；每个真实 `(session_id, thread_id, turn_id)` 恰好一个终态，客户端 display turn 的 start/end 一一对应且唯一；child raw_text 不复制 root prompt；文件实际为 `second evidence\n`，树含 tool_call/tool_result。不能把 display turn ID 当 SDK UUID。 |
| root cancel 与停止收敛：**PASS** | `src/voidx/bootstrap/semantic_gateway.py`；`src/voidx/presentation/gateway/semantic_interactions.py` | 同函数 `[cancel]`、`[recovery_cancel]`：child thread cancel 抛出身份错误，root cancel 接受；真实 cancelled 终态、工具文件未产生、pending/outbox 清空、bridge 不再 active。`[loop]` 第二轮后取消，未开始第三轮。 |
| loop waiting → guardrail needs_user：**PASS** | `src/voidx/agent/application/automation/loop/loop_service.py`；`src/voidx/agent/application/runtime/scheduler_events.py` | 同函数 `[loop_needs_user]`：五轮无进展，4 次 waiting 后 `status.finished/idle`，description.outcome 为 needs_user、ok=false，模型停在 step 12；确认最终状态投影后 root cancel 清理。不是把任意 loop 完成当作 guardrail 覆盖。 |
| goal needs_resume 投影与暂停：**PASS（受控故障）** | `src/voidx/agent/application/automation/goal/goal_service.py`；`src/voidx/agent/application/automation/goal/recovery.py` | `P/test_autonomous_gateway_needs_resume.py::test_real_goal_needs_resume_gateway[False]`、`[True]`：新建/恢复 INIT 后，真实 evaluator commit 处注入 fenced needs_resume；重开 store 查 lifecycle=needs_user、phase_status/decision=needs_resume；work/evaluator 状态携带实际 child identity，模型不多执行，root cancel 后释放锁。投影 label 是 work/evaluator 阶段，不是 needs_resume 标签；不声称自然存储异常转换覆盖。 |
| 同进程断连重连与磁盘 transcript：**PASS** | `src/voidx/presentation/gateway/session/core.py`；`src/voidx/bootstrap/semantic_gateway.py` | `P/test_autonomous_gateway_reconnect.py::test_real_autonomous_child_durable_reconnect[goal]`、`[loop]`：真实 WebSocket，清空 tree/snapshot cache 后重连，逐 child 检查磁盘结果、page/snapshot、epoch，模型调用数不增加；goal work/evaluator 各有两轮，不合并到 root。此部分保持同一 bridge，不能单独证明进程重启。 |
| 独立 PID 的历史发现与显示 ID 连续性：**PASS** | `src/voidx/agent/adapters/persistence/session_repository.py`；`src/voidx/presentation/gateway/session/core.py`；`src/voidx/bootstrap/semantic_gateway.py` | 上述 reconnect 函数关闭旧 bridge 后调用 `P/test_autonomous_gateway_restart.py::read_restarted_gateway`，仅给 thread IDs，不给绑定表；独立 reader PID 从持久化身份发现映射，page/snapshot 文本与 epoch 保持。producer pytest 尚存活。另 `test_fresh_bridge_continuation_preserves_durable_turns` 使用两个顺序退出的 coding producer PID，旧 history 不变、新 tree ID 唯一；**它单独不证明自治续跑**。 |
| 跨进程真实自治续跑与持久化暂停：**PASS** | `src/voidx/agent/application/automation/goal/recovery.py`；`src/voidx/bootstrap/autonomous_headless.py`；`src/voidx/bootstrap/semantic_gateway.py` | `P/test_autonomous_gateway_continuation.py::test_real_autonomous_process_continuation[goal]`、`[loop]`、`[needs_resume]`：第一进程在指定 durable 边界 exit 73，第二进程 exit 0，连同父进程三 PID 不同；generation、child session/thread 绑定不变，无重复审批/根启动；goal 接续 evaluator 到 completed，loop 接续第二轮后 root cancel；历史逐行不变、新 ID 不冲突、epoch 保持、终态匹配。needs_resume 重启保持暂停，无新模型轮/事件/历史，随后 root cancel；最终 lease/outbox/interaction 清理、锁可重新获取。 |
| 浏览器可见身份、完整工具输出与 reconnect：**PASS** | `frontend/src/main.ts`；`src/voidx/presentation/gateway/server.py`；`src/voidx/bootstrap/semantic_gateway.py` | `P/test_autonomous_gateway_browser.py::test_real_autonomous_gateway_browser[goal]`、`[loop]`：真实 Vite main.ts/worker/dialog/transcript + Gateway WebSocket；root 与六次 child 工具审批由 DOM 操作，错误 root 回答由真实 RPC 验证拒绝；六个工具结果、provider 读到的文件内容、两轮 work/evaluator marker 滚动后可见。loop 两轮取消；清树/缓存后 transport reconnect，逐 child 文本/epoch 保留且模型无增加。**cancel 和 transcript switch 通过 browser evaluate 调用现有 rpcCall，不是 DOM 点击**。父级最终组合 23 passed、0 skipped，包含这两个自治场景。 |
| UI protocol/schema 等价与持久化身份：**PASS（限定范围）** | `src/voidx/presentation/protocol/transcript.py`；`src/voidx/agent/adapters/persistence/session_repository.py`；`src/voidx/presentation/adapters/persistence/transcript_snapshot.py` | `src/tests/test_presentation/protocol/test_dto.py::test_checked_in_frontend_protocol_schema_matches_backend_export` 在最新 backend 中通过；restart 的 `test_metadata_discovery_ignores_reset_and_uncommitted_turns` 检查未提交 turn 不发现、完成后发现、reset 后清除。身份存于既有 node metadata，不增加独立 binding record/schema；corruption 专项验证不完整尾部与真正损坏的不同处理。schema 修正 target 27 passed；后续集成以最新 backend 为准。 |

## 3. 明确不包含的承诺

1. needs_resume 恢复合同是**恢复暂停，不自动执行模型**；不是 pending-interaction replay。进程边界测试验证已持久化数据及指定退出点，不覆盖每条指令/fsync 的任意崩溃。
2. JSONL transcript 与 SQLite 状态**不具备跨存储原子提交**；身份发现依赖已完成 transcript turn。旧历史缺实际身份时不猜测、不进行生产数据迁移；大历史扫描性能未验收。
3. display ID 是投影/历史定位标识，不是 SDK turn UUID；身份由真实 session/thread/turn 事件及持久化 metadata 建立，不从显示编号推算。
4. 浏览器 run 由 host 显式启动 SDK bridge，不是生产 `session.submit` 默认路由。未切默认入口、未迁移生产数据/schema、未删除 legacy/UI ports；未声称 frontend 完整最终回归、desktop 或主规范 Stage D 通过。S6/Stage D 仍需独立批准和验收。
5. `/tmp` 是易失本地证据，不是长期归档；本次不归档任何规范。长期复现应另行保全引用报告。

## 4. 最小复现与结果判读

在仓库根目录执行。safe3env 是下列三个环境变量的约定，**不是假定存在的可执行文件**；Chromium 必须已安装在该缓存，浏览器 skip 不能验收。下面 Python subprocess 给每次运行明确 `timeout=600`，适用于 macOS，不依赖 GNU timeout。

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.voidx/browser-cache" \
PYTHONSAFEPATH=1 PYTHONPATH="$PWD/src" ./python.py - <<'PY'
import json, subprocess, tempfile
from pathlib import Path
evidence = Path(tempfile.mkdtemp(prefix='voidx-s5-acceptance-'))
prefix = 'src/tests/test_sdk/test_autonomous_gateway_'
scopes = [
    ('s5-focused', [prefix + x + '.py' for x in
        ('projection', 'reconnect', 'restart', 'needs_resume', 'continuation')]),
    ('s5-browser-finalrun', ['src/tests/test_sdk/test_gateway_browser.py', prefix + 'browser.py']),
    ('s5-backend', []),
]
for label, files in scopes:
    command = ['./python.py', 'test.py', '--backend']
    if files:
        command += ['--', *files]
    run = subprocess.run(command, capture_output=True, text=True, timeout=600)
    (evidence / (label + '.json')).write_text(run.stdout)
    (evidence / (label + '.stderr')).write_text(run.stderr)
    data = json.loads(run.stdout)
    assert run.returncode == 0 and data['exit_code'] == 0, data
    assert data['results'], data
    for suite in data['results']:
        assert suite['status'] == 'PASS' and suite['exit_code'] == 0, suite
        assert suite['passed'] > 0 and suite['failed'] == 0, suite
        if files:
            assert suite['skipped'] == 0, suite
        print(label, {k: suite[k] for k in
              ('status', 'passed', 'failed', 'skipped', 'exit_code')})
PY
```

先 focused，再单独 browser，最后全 backend。上述命令是复现配方，不表示本次已执行。`test.py` 外层进程返回 0 曾包裹 suite FAIL，必须检查 nested `results` 及计数；timeout/JSON 解析失败也是失败。记录 warnings、skip 原因和实际代码哈希，不用历史计数硬制造通过。父级若已持有相同树的全 backend 证据，可只补单独 browser 并再次确认哈希。

## 5. 关键源码与证据指纹

算法 SHA-256；完整源码清单引用 `/tmp/voidx-sdk-s5-continuation-process-summary.json` 的 `sourcehash.files`（1166 项），其 aggregate 为 `c3424125ccf65e0b39dbfb7212d9d4d6ad98a51ba765b39b8392169cf07d74fc`。只列关键文件，不复制大段测试实现。

| 文件 | SHA-256 |
| --- | --- |
| `src/voidx/bootstrap/semantic_gateway.py` | `73efcd8a8d67ee1b1e656465796f76baf84284835e59524d8bfd92b3f076926d` |
| `src/voidx/presentation/gateway/session/core.py` | `42cda6e6c4bbb96c20e080ce0374c9145c4d8c19c0d212f8766dd057bd70a095` |
| `src/voidx/agent/adapters/persistence/session_repository.py` | `463879451fec29c5d27f9d118648c1f739c5a43bad8fd01594ecb977e44ccffa` |
| `src/voidx/agent/application/automation/goal/recovery.py` | `3bf14e19d3d4fc938fec1b8313e86d0a7479d7e1df764c2b0ab453ba9d353816` |
| `frontend/src/main.ts` | `0037fe033a5215ab8862cda975d06421584aadfe11a8c8109eedb7638d1a3014` |
| `/tmp/voidx-sdk-s5-continuation-process-summary.json` | `f86d2d1fd768cbc01a8b0b72de26d48cc7b3bccc7f3da8a373ec77c21749d15a` |
| `/tmp/voidx-s5-continuation-parent-backend-J2Hnk3` | `cd215c25beb463c576e0debcdceff725fa55d302f8b477bdc463eefd9ffefc55` |
| `/tmp/voidx-s5-final-browser-azzPXg` | `9bb88ab41cf1df5c992e4ab1a02272e337e656b72273af3f2c872b387d38f434` |

## 6. 读者与执行就绪自检

- **Fresh-reader：PASS。** 已定义对象、root/child/HITL、合同、各项判定、真实测试函数、最终证据及排除范围；不要求读者知道历史代理对话，也不靠子报告标题理解通过理由。
- **Execution-readiness：PASS。** 路径/测试函数真实存在，命令使用项目入口、safe3env、600 秒 timeout，检查 nested status/counts；区分本次检查、既有执行与父级最终 browser finalrun。没有将 planned 文件作为现存实现。
- **证据一致性：PASS。** 最新 backend 实际 nested 结果已读，1166 源码哈希重新匹配；关键前端哈希单独列出。纯文档交付适用 TDD 文档例外，无新增测试或实现。
- **父级最终审阅：PASS。** 已直接阅读报告、业务测试和实际日志，重新核对源码清单与最终浏览器结果；未将缺失的独立审查报告算作通过。S6/Stage D 和归档仍保持独立门槛。
