---
name: macos-ocr
description: 使用 macOS Apple Vision OCR CLI 识别图片文字
enabled: true
---

# macOS OCR

使用预编译 CLI：

```bash
OCR_BIN="${VOIDX_MACOS_OCR_BIN:-tools/macos-ocr/bin/voidx-macos-ocr}"
"$OCR_BIN" \
  --input "$IMAGE_PATH" \
  --recognition-level accurate \
  --regions
```

可选参数：

- `--language <id>`：可重复传入，例如 `--language zh-Hans --language en-US`
- `--recognition-level fast|accurate`
- `--regions`：返回文字区域
- `--max-output-chars <positive-integer>`：限制输出字符数

OCR 结果从 stdout 读取 JSON。
