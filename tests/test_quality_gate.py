# -*- coding: utf-8 -*-
"""test_quality_gate.py — 闸门取数解析的回归。

为什么单测这一行: vulture 输出是 `路径:行: 消息`(两个冒号), 旧实现按四段切,
`len(parts) < 4` 把所有结果静默丢掉, 于是"死代码 0"一直是假绿灯。
解析是纯函数, 用字符串直接测, 不依赖本机装没装 vulture。
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "quality_gate", ROOT / "tools" / "quality_gate.py")
QG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QG)


def test_vulture_line_parsed():
    row = "danmaku-collector\\collector.py:295: unused variable 'frame' (100% confidence)"
    assert QG.parse_vulture_line(row) == (
        "danmaku-collector\\collector.py", "295",
        "unused variable 'frame' (100% confidence)")


def test_summary_lines_ignored():
    assert QG.parse_vulture_line("") is None
    assert QG.parse_vulture_line("All checks passed!") is None


def test_non_numeric_line_ignored():
    assert QG.parse_vulture_line("http://example.com:8080: not a finding") is None


if __name__ == "__main__":
    test_vulture_line_parsed()
    test_summary_lines_ignored()
    test_non_numeric_line_ignored()
    print("test_quality_gate: 3/3 通过")
