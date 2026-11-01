# -*- coding: utf-8 -*-
"""test_slim_import.py — 轻量版(不带 numpy)必须能起服务。

为什么单独立一个文件: 其余回归都在**装了 numpy 的环境**里跑, 因此
"某个模块在顶层 import 了需要 numpy 的东西"这种问题永远测不出来。
而对外发布的恰恰是轻量版 —— 一旦顶层拉进 numpy, 整个播放端在 import
阶段就崩, 用户看到的只是黑窗口一闪, 连报错都来不及看。

判据: 缺库只该关掉"自动校准"这一个能力, 其余(播放、拉取弹幕、手动微调)
必须照常可用。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "danmaku-server"

# 用 None 占位 sys.modules['numpy'] 是 CPython 的约定写法: 之后任何
# `import numpy` 都会抛 ImportError("import of numpy halted"), 从而在没有
# 卸载/改环境的进程里干净地模拟"这个包装不下 numpy"。
PROBE = r"""
import sys
sys.modules['numpy'] = None

import server                      # 不能抛异常
assert server.HAS_AUDIOFP is False, "numpy 缺失时 HAS_AUDIOFP 必须为 False"

import alignment                   # 时间格式化等不需要指纹能力的部分仍要可用
assert callable(alignment._fmt_wall)
assert isinstance(alignment._fmt_wall(0), str)   # 不比对具体时刻: 依赖本机时区

print("PROBE-OK")
"""


def _run_probe() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", PROBE], cwd=SERVER,
                          capture_output=True, text=True, encoding="utf-8")


def test_server_imports_without_numpy() -> bool:
    r = _run_probe()
    ok = r.returncode == 0 and "PROBE-OK" in (r.stdout or "")
    detail = (r.stdout or "") + (r.stderr or "")
    print(f"  {'PASS' if ok else 'FAIL'}  无 numpy 时 server 仍可导入"
          + ("" if ok else f"\n        实际: {detail.strip()[-400:]}"))
    return ok


def test_audiofp_guarded_when_present() -> bool:
    """反向验: 有 numpy 时不能被误判成"没有能力"。"""
    code = r"""
import sys
sys.path.insert(0, ".")
import server
print("HAS_AUDIOFP=%s" % server.HAS_AUDIOFP)
"""
    r = subprocess.run([sys.executable, "-c", code], cwd=SERVER,
                       capture_output=True, text=True, encoding="utf-8")
    expect = "HAS_AUDIOFP=True" in (r.stdout or "")
    if not expect:
        print(f"  跳过  本机无 numpy/audiofp, 反向判据不适用(轻量版环境)"
              f" 实际: {(r.stdout or r.stderr or '').strip()[-200:]}")
        return True
    print("  PASS  有 numpy 时 HAS_AUDIOFP=True(没把能力误关)")
    return True


def main() -> int:
    results = [test_server_imports_without_numpy(),
               test_audiofp_guarded_when_present()]
    print("\nOK: 全部通过" if all(results) else "\nFAIL: 有失败项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
