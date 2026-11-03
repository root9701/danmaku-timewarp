# -*- coding: utf-8 -*-
"""gitpush 回归: 失败分类与重试次数。

不碰网络: 全部用注入的 runner 跑, 只验"该重试的重试、不该重试的立刻停"。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import gitpush                                            # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def test_classify():
    check("连接超时算瞬时",
          gitpush.is_transient("fatal: unable to access ... Connection timed out"))
    check("DNS 失败算瞬时",
          gitpush.is_transient("fatal: Could not resolve host: github.com"))
    check("非快进不重试",
          not gitpush.is_transient("! [rejected] main -> main (non-fast-forward)"))
    check("认证失败不重试",
          not gitpush.is_transient("fatal: Authentication failed for 'https://...'"))


def test_retry():
    calls = []

    def flaky(cmd, env, timeout):
        calls.append(cmd)
        if len(calls) < 3:
            return False, "Connection reset by peer"
        return True, ""

    ok, _ = gitpush.push("origin", "main", attempts=3, delays=(0, 0), runner=flaky)
    check("瞬时失败重试到成功", ok and len(calls) == 3, f"calls={len(calls)}")

    calls.clear()

    def permanent(cmd, env, timeout):
        calls.append(cmd)
        return False, "! [rejected] main -> main (non-fast-forward)"

    ok, _ = gitpush.push("origin", "main", attempts=3, delays=(0, 0), runner=permanent)
    check("永久失败只试一次", not ok and len(calls) == 1, f"calls={len(calls)}")

    def no_prompt(cmd, env, timeout):
        check("无人值守不弹账号密码", env.get("GIT_TERMINAL_PROMPT") == "0")
        check("凭据过期不弹 GUI", env.get("GCM_INTERACTIVE") == "never")
        check("显式推当前 HEAD", cmd[-1] == "HEAD:refs/heads/main", str(cmd))
        return True, ""

    gitpush.push("origin", "main", attempts=1, runner=no_prompt)


def main() -> int:
    print("gitpush 回归")
    test_classify()
    test_retry()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
