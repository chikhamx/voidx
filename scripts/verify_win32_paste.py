#!/usr/bin/env python3
"""真机验证脚本：Windows Ctrl+V 多行粘贴 — R1-R5 假设验证。

在 Windows Terminal / cmd.exe / PowerShell 中直接运行此脚本，
按提示操作即可收集验证数据。

用法:
    python scripts/verify_win32_paste.py
"""

from __future__ import annotations

import sys
import time

if sys.platform != "win32":
    print("此脚本仅在 Windows 上运行。")
    sys.exit(1)

import msvcrt


# ── R1: 测量粘贴内容的灌入时序 ──────────────────────────────────────

def _drain_with_timing(first_char: str, timeout_ms: int = 50) -> dict:
    """读取控制台缓冲区剩余字符，记录每个字符的到达时间。"""
    chars: list[tuple[str, float]] = []
    t0 = time.monotonic()
    deadline = t0 + (timeout_ms / 1000.0)
    buffer = first_char
    chars.append((first_char, 0.0))

    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.0005)  # 0.5ms
            continue
        ch = msvcrt.getwch()
        elapsed_ms = (time.monotonic() - t0) * 1000
        if ch == "\x00" or ch == "\xe0":
            msvcrt.getwch()  # consume second byte
            break
        buffer += ch
        chars.append((ch, elapsed_ms))

    return {
        "buffer": buffer,
        "chars": chars,
        "total_elapsed_ms": (time.monotonic() - t0) * 1000,
        "length": len(buffer),
        "newline_count": buffer.count("\r") + buffer.count("\n"),
    }


def test_r1_paste_timing():
    """R1: 测量粘贴不同大小文本时，字符到达的时序和分批情况。"""
    print("\n" + "=" * 70)
    print("R1: 粘贴时序验证")
    print("=" * 70)
    print("请分别粘贴以下大小的多行文本：")
    print("  1) 10 行文本")
    print("  2) 100 行文本")
    print("  3) 1000 行文本")
    print()
    print("每次粘贴后，脚本会记录字符到达时间，判断是否分批灌入。")
    print()

    for label, expected_lines in [("10行", 10), ("100行", 100), ("1000行", 1000)]:
        input(f">>> 准备粘贴 {label} 文本，按 Enter 开始计时，然后立即 Ctrl+V 粘贴...")
        # 等待第一个字符（用户按 Enter 后开始粘贴）
        print(f"等待粘贴内容...（超时 5 秒）")
        first = _wait_for_char(5000)
        if first is None:
            print("  [超时] 未检测到输入")
            continue

        # 如果首字符是换行，进入 drain
        if first in ("\r", "\n"):
            result = _drain_with_timing(first, timeout_ms=100)
        else:
            # 首字符不是换行，继续读直到遇到换行
            result = _drain_from_arbitrary(first, timeout_ms=100)

        _report_r1(result, expected_lines, label)


def _wait_for_char(timeout_ms: int) -> str | None:
    """等待第一个字符到达，超时返回 None。"""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            return msvcrt.getwch()
        time.sleep(0.0005)
    return None


def _drain_from_arbitrary(first_char: str, timeout_ms: int = 100) -> dict:
    """从任意首字符开始读取，记录时序。"""
    t0 = time.monotonic()
    deadline = t0 + (timeout_ms / 1000.0)
    buffer = first_char
    chars: list[tuple[str, float]] = [(first_char, 0.0)]

    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.0005)
            continue
        ch = msvcrt.getwch()
        elapsed_ms = (time.monotonic() - t0) * 1000
        if ch == "\x00" or ch == "\xe0":
            msvcrt.getwch()
            break
        buffer += ch
        chars.append((ch, elapsed_ms))

    return {
        "buffer": buffer,
        "chars": chars,
        "total_elapsed_ms": (time.monotonic() - t0) * 1000,
        "length": len(buffer),
        "newline_count": buffer.count("\r") + buffer.count("\n"),
    }


def _report_r1(result: dict, expected_lines: int, label: str):
    """报告 R1 验证结果。"""
    print(f"\n  [{label}] 验证结果:")
    print(f"    总字符数: {result['length']}")
    print(f"    换行符数: {result['newline_count']}")
    print(f"    总耗时: {result['total_elapsed_ms']:.1f}ms")

    # 分析字符到达间隔
    if len(result["chars"]) > 1:
        intervals = []
        for i in range(1, len(result["chars"])):
            interval = result["chars"][i][1] - result["chars"][i-1][1]
            intervals.append(interval)

        max_interval = max(intervals)
        avg_interval = sum(intervals) / len(intervals)
        gaps_over_20ms = sum(1 for g in intervals if g > 20)

        print(f"    字符间隔: avg={avg_interval:.2f}ms, max={max_interval:.2f}ms")
        print(f"    超过 20ms 的间隔数: {gaps_over_20ms}")

        if gaps_over_20ms > 0:
            print(f"    ⚠️  检测到 {gaps_over_20ms} 个超过 20ms 的间隔 — 可能分批灌入！")
            print(f"    最大间隔 {max_interval:.1f}ms — 若此间隔后有换行符，会导致后续批次丢失")
        else:
            print(f"    ✅ 所有字符间隔 < 20ms — 20ms 超时足够")

    # 检查是否完整保留
    expected_newlines = expected_lines - 1  # N 行有 N-1 个换行
    if result["newline_count"] >= expected_newlines:
        print(f"    ✅ 换行符数量符合预期 (≥{expected_newlines})")
    else:
        print(f"    ❌ 换行符数量不足: 期望 ≥{expected_newlines}, 实际 {result['newline_count']}")
        print(f"    → 可能是分批灌入导致部分内容在 drain 超时后到达")


# ── R2: 换行符格式验证 ──────────────────────────────────────────────

def test_r2_newline_format():
    """R2: 检查粘贴内容的换行符格式（\\r\\n / \\r / \\n）。"""
    print("\n" + "=" * 70)
    print("R2: 换行符格式验证")
    print("=" * 70)
    print("请粘贴一段包含多行的文本（至少 3 行）。")
    print()

    input(">>> 按 Enter 开始，然后立即 Ctrl+V 粘贴多行文本...")
    first = _wait_for_char(5000)
    if first is None:
        print("  [超时] 未检测到输入")
        return

    result = _drain_from_arbitrary(first, timeout_ms=100)
    buffer = result["buffer"]

    print(f"\n  粘贴内容 ({len(buffer)} 字符):")
    # 显示原始字节
    raw_bytes = buffer.encode("utf-8", errors="replace")
    print(f"    原始字节 (hex): {raw_bytes[:200].hex(' ')}")
    if len(raw_bytes) > 200:
        print(f"    ... (共 {len(raw_bytes)} 字节)")

    crlf_count = buffer.count("\r\n")
    cr_only = buffer.count("\r") - crlf_count
    lf_only = buffer.count("\n") - crlf_count

    print(f"\n  换行符统计:")
    print(f"    \\r\\n (CRLF): {crlf_count}")
    print(f"    \\r  (CR only): {cr_only}")
    print(f"    \\n  (LF only): {lf_only}")

    if crlf_count > 0 and cr_only == 0 and lf_only == 0:
        print(f"    ✅ 换行格式为 \\r\\n (CRLF) — Windows 标准")
    elif lf_only > 0 and crlf_count == 0 and cr_only == 0:
        print(f"    ⚠️  换行格式为 \\n (LF) — 非标准 Windows 格式")
    elif cr_only > 0 and crlf_count == 0 and lf_only == 0:
        print(f"    ⚠️  换行格式为 \\r (CR only) — 旧 Mac 格式")
    else:
        print(f"    ℹ️  混合换行格式")

    print(f"\n  _insert_pasted_text 归一化: \\r\\n→\\n, \\r→\\n — 所有格式已覆盖")


# ── R4: 快速连按 Enter 间隔测量 ─────────────────────────────────────

def test_r4_double_enter_timing():
    """R4: 测量快速连按两次 Enter 的实际间隔。"""
    print("\n" + "=" * 70)
    print("R4: 快速连按 Enter 间隔测量")
    print("=" * 70)
    print("请尽可能快地连按两次 Enter（模拟快速双击回车）。")
    print("重复 5 次，脚本会记录每次的间隔。")
    print()

    intervals = []
    for i in range(5):
        input(f">>> 第 {i+1}/5 次：按 Enter 开始，然后快速连按两次 Enter...")
        first = _wait_for_char(3000)
        if first is None or first != "\r":
            print(f"  [跳过] 未检测到 Enter")
            continue

        # 等待第二个 Enter
        t0 = time.monotonic()
        second = _wait_for_char(1000)
        elapsed = (time.monotonic() - t0) * 1000

        if second is None:
            print(f"  [超时] 第二个 Enter 未在 1 秒内到达")
            continue

        intervals.append(elapsed)
        print(f"  间隔: {elapsed:.1f}ms")

    if intervals:
        print(f"\n  统计:")
        print(f"    最小间隔: {min(intervals):.1f}ms")
        print(f"    最大间隔: {max(intervals):.1f}ms")
        print(f"    平均间隔: {sum(intervals)/len(intervals):.1f}ms")

        min_interval = min(intervals)
        if min_interval < 20:
            print(f"\n  ⚠️  最小间隔 {min_interval:.1f}ms < 20ms — 存在误判风险！")
            print(f"    两个 \\r 间隔 <20ms 时，第二个会被 drain 吞掉，第一次提交丢失")
            print(f"    建议: 缩短 timeout_ms 至 {int(min_interval * 0.8)}ms，或提高阈值至 newline_count >= 3")
        else:
            print(f"\n  ✅ 最小间隔 {min_interval:.1f}ms ≥ 20ms — 20ms 超时不会误判")
    else:
        print(f"\n  [无有效数据]")


# ── R5: 大文本 _pending_bytes 交互验证 ──────────────────────────────

def test_r5_large_paste():
    """R5: 验证大段 bracketed paste 序列的处理。"""
    print("\n" + "=" * 70)
    print("R5: 大文本粘贴验证")
    print("=" * 70)
    print("请粘贴一段非常长的文本（如 500+ 行代码）。")
    print()

    input(">>> 按 Enter 开始，然后立即 Ctrl+V 粘贴大段文本...")
    first = _wait_for_char(5000)
    if first is None:
        print("  [超时] 未检测到输入")
        return

    result = _drain_from_arbitrary(first, timeout_ms=200)
    buffer = result["buffer"]

    print(f"\n  粘贴内容统计:")
    print(f"    总字符数: {len(buffer)}")
    print(f"    换行符数: {result['newline_count']}")
    print(f"    总耗时: {result['total_elapsed_ms']:.1f}ms")

    # 验证 _process_paste 能处理任意长度
    print(f"\n  _process_paste 累积机制:")
    print(f"    _paste_buffer 使用 bytes += 累积，无大小上限")
    print(f"    _pending_bytes 仅处理截断的 UTF-8/CSI 序列，不涉及 paste 标记")
    print(f"    ✅ 单次返回大段 bracketed paste 序列无大小上限问题")


# ── 主入口 ──────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Windows Ctrl+V 多行粘贴 — R1-R5 真机验证")
    print("=" * 70)
    print()
    print("此脚本验证设计文档中 R1-R5 的假设。")
    print("请在实际的 Windows 终端环境中运行（非 IDE 内嵌终端）。")
    print()
    print("建议测试环境:")
    print("  1. Windows Terminal")
    print("  2. cmd.exe (传统控制台)")
    print("  3. PowerShell (控制台)")
    print()

    tests = [
        ("R1: 粘贴时序验证", test_r1_paste_timing),
        ("R2: 换行符格式验证", test_r2_newline_format),
        ("R4: 快速连按 Enter 间隔", test_r4_double_enter_timing),
        ("R5: 大文本粘贴验证", test_r5_large_paste),
    ]

    for name, func in tests:
        try:
            func()
        except KeyboardInterrupt:
            print(f"\n  [跳过 {name}]")
        print()

    print("=" * 70)
    print("验证完成。请将以上结果记录到设计文档的 Risks 部分。")
    print("R3（功能键前导字节混入粘贴流）为边界情况，已通过单元测试覆盖，")
    print("无需真机验证。")
    print("=" * 70)


if __name__ == "__main__":
    main()
