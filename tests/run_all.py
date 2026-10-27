# -*- coding: utf-8 -*-
"""run_all.py — 全量回归。退出码说话: 0 = 全过, 1 = 有失败。

用法: python tests/run_all.py       (或用 danmaku-server/.venv 的解释器)

收集同目录全部 test_*.py, 逐个以子进程跑, 汇总退出码。
单文件也可直跑: python tests/test_<主题>.py
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main() -> int:
    tests = sorted(p for p in HERE.glob("test_*.py"))
    if not tests:
        print("没有找到 test_*.py")
        return 3
    failed = []
    for t in tests:
        print(f"\n{'=' * 60}\n{t.name}\n{'=' * 60}")
        r = subprocess.run([sys.executable, str(t)], cwd=HERE.parent)
        if r.returncode != 0:
            failed.append((t.name, r.returncode))
    print(f"\n{'=' * 60}")
    if failed:
        print(f"FAIL: {len(failed)}/{len(tests)} 个文件失败")
        for name, rc in failed:
            print(f"  {name} 退出码 {rc}")
        return 1
    print(f"OK: {len(tests)} 个文件全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
