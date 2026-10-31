# -*- coding: utf-8 -*-
"""quality_gate.py — 复杂度 / 死代码 / 未用名闸门，靠退出码说话。

用法: python tools/quality_gate.py [--json]

退出码:
  0  死代码、未用名、语法错误均为 0（圈复杂度超限只报数，属已登记存量债）
  1  有语法错误 / 死代码 / 未用名

分工（避免同一件事两处各数一遍）:
  文件 400 行、函数 60 行、链接与文档总量由 规范/gate.py + tools/gate.json 判;
  本脚本只管需要工具链取数的三项。发布时 release.py 优先走 规范/gate.py
  （本机有镜像），没有镜像的机器回退到本脚本，仍能查语法、死代码与复杂度。

判据与取数手段同尺:
  语法       ast.parse 每个 .py
  圈复杂度   ruff C901, 阈值 15
  死代码     vulture 60% 置信（框架回调按名忽略：它们由基类分发，静态分析看不到调用点）
  未用名     ruff F401,F841

超标项必须登记在 架构总览.md 的「架构债」表里, 只减不增;
本脚本只报数, 不判是否可接受 —— 让数字可复现, 别靠回忆。
"""
import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# tools/ 也要扫: release.py 曾经藏着 `tag = "" if kind == "slim" else ""` 这种死代码,
# 而它当时不在扫描范围内 —— 闸门"看不见"的地方就会长草。tools/ 是发布链路的唯一入口,
# 恰恰是最不该免检的部分。tests/ 仍不扫(测试文件的结构性超限没有改进价值)。
CODE_DIRS = ("danmaku-server", "danmaku-collector", "tools")
SKIP = ("__pycache__", ".venv", "tests")

MAX_COMPLEXITY = 15
# BaseHTTPRequestHandler 按方法名分发 do_GET/do_POST/log_message, 静态分析看不见调用点。
VULTURE_IGNORE_NAMES = "do_GET,do_POST,log_message"


def iter_py():
    for d in CODE_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            s = str(p).replace("\\", "/")
            if any(x in s for x in SKIP):
                continue
            yield p


def syntax_findings():
    """语法错误清单。它必须让闸门变红 —— 语法错时行数/死代码都不可信。"""
    bad = []
    for p in iter_py():
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{p.relative_to(ROOT)}:{e.lineno or 0} 语法错误: {e.msg}")
    return bad


def ruff_complexity():
    """ruff C901, 阈值 15。返回 [(函数名, 复杂度)]。"""
    cmd = [sys.executable, "-m", "ruff", "check", "--no-cache",
           "--select", "C901",
           "--config", f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
           *CODE_DIRS]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = []
    for line in (r.stdout or "").splitlines():
        if line.startswith("C901"):
            # C901 `name` is too complex (N > 15)
            try:
                name = line.split("`")[1]
                val = int(line.split("(")[1].split(">")[0].strip())
            except (IndexError, ValueError):
                continue
            out.append((name, val))
    return out


def parse_vulture_line(line):
    """vulture 输出是 `路径:行: 消息`（只有两个冒号），不是 columns 四段。

    返回 (路径, 行号, 消息)；不是结果行返回 None。
    单独抽出来是因为这里曾把三段切成四段、全部静默丢弃，让"死代码 0"变成假绿灯。
    """
    line = line.strip()
    if not line or ":" not in line:
        return None
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    f, ln, msg = parts[0], parts[1], parts[2]
    if not ln.isdigit():
        return None
    return f, ln, msg.strip()


def vulture_findings():
    cmd = [sys.executable, "-m", "vulture", *CODE_DIRS,
           "--min-confidence", "60",
           "--ignore-names", VULTURE_IGNORE_NAMES,
           "--exclude", ".venv,__pycache__"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    rows = []
    for line in (r.stdout or "").splitlines():
        parsed = parse_vulture_line(line)
        if not parsed:
            continue
        f, ln, msg = parsed
        if any(x in f.replace("\\", "/") for x in SKIP):
            continue
        rows.append((f, ln, msg))
    return rows


def ruff_unused():
    cmd = [sys.executable, "-m", "ruff", "check", "--no-cache",
           "--select", "F401,F841", "--output-format", "concise", *CODE_DIRS]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    # 只看 "路径:行:列: 规则码 ..." 形态的行; "All checks passed!" 之类的汇总行不算
    out = []
    for ln in (r.stdout or "").splitlines():
        if re.match(r"^[^:]+:\d+:\d+: [A-Z]\d+", ln.strip()):
            out.append(ln.strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syntax = syntax_findings()
    cx = ruff_complexity()
    dead = vulture_findings()
    unused = ruff_unused()

    if args.json:
        print(json.dumps({
            "syntax": syntax,
            "complexity_over": [{"name": n, "value": v} for n, v in cx],
            "dead_code": [{"file": f, "line": ln, "msg": m} for f, ln, m in dead],
            "unused": unused,
        }, ensure_ascii=False, indent=2))
        return 0

    def section(title, rows):
        print(f"\n== {title} ==")
        if not rows:
            print("  无")
            return False
        for r in rows:
            print("  " + r)
        return True

    red = False
    red |= section(f"语法错误 ({len(syntax)})", syntax)
    red |= section(f"圈复杂度 > {MAX_COMPLEXITY} ({len(cx)})",
                   [f"{n} {v}" for n, v in cx])
    red |= section(f"死代码 ({len(dead)})",
                   [f"{f}:{ln} {m}" for f, ln, m in dead])
    red |= section(f"未用导入/变量 ({len(unused)})", unused)

    total = len(syntax) + len(cx) + len(dead) + len(unused)
    if red:
        # 存量超限已在 架构总览.md「架构债」登记, 是"只减不增"的账, 不等于回归失败。
        # 真正的红线是语法错误、死代码与未用名: 那三项必须为 0。
        if syntax or dead or unused:
            print(f"\nFAIL: {total} 项超限, 其中语法错误 {len(syntax)} / "
                  f"死代码 {len(dead)} / 未用名 {len(unused)} 必须清零")
            return 1
        print(f"\nDEBT: {total} 项存量超限(已登记, 只减不增); 死代码与未用名均为 0")
        return 0
    print(f"\nOK: 0 项超限")
    return 0


if __name__ == "__main__":
    sys.exit(main())
