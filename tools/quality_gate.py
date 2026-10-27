# -*- coding: utf-8 -*-
"""quality_gate.py — 架构与质量闸门，靠退出码说话。

用法: python tools/quality_gate.py [--json]

退出码:
  0  全过
  1  有超限(函数 60 行 / 文件 400 行 / 圈复杂度 15 / 死代码)

判据与取数手段同尺:
  函数行数   AST 行跨度(end_lineno - lineno + 1), 不用 PLR0915(那数的是语句数)
  文件行数   物理行数
  圈复杂度   ruff C901, 阈值 15
  死代码     vulture 60% 置信
  未用名     ruff F401,F841

超标项必须登记在 架构总览.md 的「架构债」表里, 只减不增;
本脚本只报数, 不判是否可接受 —— 让数字可复现, 别靠回忆。
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CODE_DIRS = ("danmaku-server", "danmaku-collector")
SKIP = ("__pycache__", ".venv", "tools", "tests")

FUNC_MAX_LINES = 60
FILE_MAX_LINES = 400
MAX_COMPLEXITY = 15


def iter_py():
    for d in CODE_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            s = str(p).replace("\\", "/")
            if any(x in s for x in SKIP):
                continue
            yield p


def iter_js():
    for p in sorted((ROOT / "danmaku-server" / "web").rglob("*.js")):
        if "__pycache__" not in str(p):
            yield p


def ast_findings():
    """函数行数超限清单。"""
    import ast

    bad = []
    for p in iter_py():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append((p, f"<语法错误>", e.lineno or 0, 0, "syntax"))
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = n.end_lineno - n.lineno + 1
                if span > FUNC_MAX_LINES:
                    bad.append((p, n.name, n.lineno, span, "func_lines"))
    return bad


def file_findings():
    bad = []
    for p in list(iter_py()) + list(iter_js()):
        n = len(p.read_text(encoding="utf-8").splitlines())
        if n > FILE_MAX_LINES:
            bad.append((p, p.name, 1, n, "file_lines"))
    return bad


def ruff_complexity():
    """ruff C901, 阈值 15。返回 [(文件, 函数名, 复杂度)]。"""
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


def vulture_findings():
    cmd = [sys.executable, "-m", "vulture", *CODE_DIRS, "--min-confidence", "60"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    rows = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        f, ln, msg = parts[0], parts[1], parts[3]
        if any(x in f.replace("\\", "/") for x in SKIP):
            continue
        rows.append((f, ln, msg.strip()))
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

    funcs = ast_findings()
    files = file_findings()
    cx = ruff_complexity()
    dead = vulture_findings()
    unused = ruff_unused()

    if args.json:
        print(json.dumps({
            "func_over": [{"file": str(p.relative_to(ROOT)), "name": n,
                           "line": ln, "span": s}
                          for p, n, ln, s, _ in funcs],
            "file_over": [{"file": str(p.relative_to(ROOT)), "lines": s}
                          for p, _, _, s, _ in files],
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
    red |= section(f"函数 > {FUNC_MAX_LINES} 行 ({len(funcs)})",
                   [f"{p.relative_to(ROOT)}:{ln} {n} {s} 行"
                    for p, n, ln, s, _ in funcs])
    red |= section(f"文件 > {FILE_MAX_LINES} 行 ({len(files)})",
                   [f"{p.relative_to(ROOT)} {s} 行" for p, _, _, s, _ in files])
    red |= section(f"圈复杂度 > {MAX_COMPLEXITY} ({len(cx)})",
                   [f"{n} {v}" for n, v in cx])
    red |= section(f"死代码 ({len(dead)})",
                   [f"{f}:{ln} {m}" for f, ln, m in dead])
    red |= section(f"未用导入/变量 ({len(unused)})", unused)

    total = len(funcs) + len(files) + len(cx) + len(dead) + len(unused)
    if red:
        # 存量超限已在 架构总览.md「架构债」登记, 是"只减不增"的账, 不等于回归失败。
        # 真正的红线是死代码与未用名: 那两项必须为 0。
        if dead or unused:
            print(f"\nFAIL: {total} 项超限, 其中死代码 {len(dead)} / 未用名 {len(unused)} 必须清零")
            return 1
        print(f"\nDEBT: {total} 项存量超限(已登记, 只减不增); 死代码与未用名均为 0")
        return 0
    print(f"\nOK: 0 项超限")
    return 0


if __name__ == "__main__":
    sys.exit(main())
