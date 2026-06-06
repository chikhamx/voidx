# Intent Classifier — 技术设计文档

> 在关键字匹配和 LLM on_intent 之间增加 FastText 轻量分类层，拦截高置信度意图识别请求，减少 LLM token 消耗。

## 1. 背景与动机

### 1.1 现有意图识别管线

```
用户输入
  │
  ▼
Layer 0: 关键字匹配 (infer_task_intent)
  │  _IMPLEMENT_HINTS / _DESIGN_HINTS / _INSPECT_HINTS / ...
  │  纯字符串 contains_any，中英文关键词
  │  命中 → 直接返回 intent
  │  未命中 → 返回 CHAT
  │
  ▼
Layer 2: LLM 调用 on_intent 工具
  │  LLM 分析上下文，输出 intent + confidence + reason
  │  runtime 侧 refine_intent() 做置信度校验和权限控制
  │  每次调用消耗 500-2000ms + token
  │
  ▼
最终 intent → 决定可用工具集 + 激活 skill
```

### 1.2 问题

| 问题 | 影响 |
|------|------|
| Layer 0 太粗糙 | 只看关键词是否出现，"帮我看看这个bug" 会命中 inspect 而非 debug |
| Layer 0 无法区分模糊意图 | "分析一下" 可能是 inspect 也可能是 design |
| Layer 2 太重 | 每次未命中关键词都要走 LLM，即使意图很明显 |
| 中英文混合 | 关键词列表需要手动维护两套，覆盖不全 |

### 1.3 目标

在 Layer 0 和 Layer 2 之间插入一个轻量分类层：

- **拦截 70%+ 的 LLM on_intent 调用**（高置信度场景直接返回）
- **推理延迟 < 1ms**（对比 LLM 的 500-2000ms）
- **模型 < 1.5MB**，打包进 pip wheel
- **中英文统一处理**，无需维护两套关键词
- **LLM 兜底不变**，分类器错了 LLM 仍可修正

## 2. 三层意图识别架构

```
用户输入
  │
  ▼
Layer 0: 关键字匹配 (现有，不变)
  │  硬规则：approval / direct command / 关键词
  │  命中 → 直接返回（最快，0ms）
  │  未命中 ↓
  │
  ▼
Layer 1: FastText 分类器 (新增)
  │  本地模型推理，< 0.5ms
  │  置信度 ≥ 0.85 → 直接返回 intent
  │  置信度 < 0.85 → 回退到 LLM
  │
  ▼
Layer 2: LLM on_intent (现有，不变)
  │  最精确但最贵，兜底
  │  分类器低置信度时仍走此路径
  │
  ▼
最终 intent → 决定可用工具集 + 激活 skill
```

## 3. 分类器设计

### 3.1 分类类别

7 个 intent，与 `TaskIntent` 枚举一一对应：

| Intent | 语义 | 典型输入 |
|--------|------|---------|
| `implement` | 修改/实现代码 | "修复这个bug"、"改成异步的" |
| `inspect` | 查看/理解代码 | "看看这个函数"、"分析性能" |
| `design` | 设计/讨论方案 | "如何设计缓存层"、"架构建议" |
| `review` | 审查代码 | "审查这个PR"、"代码规范检查" |
| `debug` | 排查问题 | "为什么报TypeError"、"接口返回500" |
| `chat` | 闲聊/解释概念 | "你好"、"什么是依赖注入" |
| `ambiguous` | 意图不明确 | "帮我看看这个"、"这个怎么办" |

### 3.2 模型选型

| 方案 | 模型大小 | 推理延迟 | 准确率 | 依赖 |
|------|---------|---------|--------|------|
| **FastText（选用）** | ~1 MB | < 0.5ms | 88-92% | numpy（已有） |
| ONNX 小模型 | 20-50 MB | 5-10ms | 90-95% | onnxruntime |
| sentence-transformers | ~90 MB | 10-20ms | 92-96% | torch |
| 规则 + TF-IDF | ~1 MB | < 1ms | 80-85% | sklearn |

**选择 FastText 的理由**：
1. 模型极小（1 MB），可打包进 wheel
2. 推理极快（< 0.5ms），用户无感知
3. subword 机制天然处理中英文混合，无需分词
4. 训练数据格式极简，训练速度快（秒级）
5. voidx 已依赖 numpy（langchain 生态），无新增依赖

### 3.3 置信度阈值设计

```python
def classify_with_threshold(text: str) -> tuple[TaskIntent, float, str]:
    intent, confidence = classifier.classify(text)

    if confidence >= 0.85:
        # 高置信度：直接采用，不走 LLM
        return intent, confidence, "accept"
    elif confidence >= 0.5:
        # 中置信度：回退 LLM，但分类结果可作为参考
        return intent, confidence, "suggest"
    else:
        # 低置信度：完全回退 LLM
        return TaskIntent.AMBIGUOUS, confidence, "fallback"
```

## 4. 实现要点

- 分类器模型文件放在 `src/voidx/data/intent_classifier.bin`
- 通过 `importlib.resources` 加载，兼容 wheel 打包
- 训练脚本放在 `scripts/train_intent_classifier.py`
- 分类器调用路径：`infer_task_intent()` → `classifier.classify()` → `on_intent` (LLM fallback)

## 5. 测试覆盖

| 测试 | 描述 |
|------|------|
| `test_classifier_high_confidence` | 高置信度直接返回，不走 LLM |
| `test_classifier_low_confidence_fallback` | 低置信度回退到 LLM |
| `test_classifier_chinese_input` | 中文输入正确分类 |
| `test_classifier_mixed_input` | 中英混合输入正确分类 |
| `test_classifier_model_size` | 模型文件 < 1.5MB |
| `test_classifier_latency` | 单次推理 < 1ms |
