# -*- coding: utf-8 -*-
"""pipelinelock.py 回归: 发布进程的单实例、心跳与停止请求。

用法: python tests/test_pipelinelock.py     (退出码 0 = 全过)

背景(实测事故 2026-11-05): 11-03 启的旧 nightly 与 11-04 控制台派生的 nightly
同时在跑, 两边各自 git commit, 撞 index.lock, 同一晚的对齐也算了两遍。
根因是"只认自己的 Popen 句柄": 手动启动的进程控制台看不见, 于是再点一次就双开。
这里覆盖修好之后的三条保证:
  1. 全机只允许一个常驻发布进程(生命周期锁)
  2. 外部启动的进程也能被看见(心跳文件, 不靠进程树猜)
  3. 停止是写请求文件让它自己退, 不强杀(避免停在 commit 中间)

不联网、不碰真实 data/; 锁文件与心跳都指到临时目录。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pipelinelock                                       # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


class Sandbox:
    """把锁/心跳/停止文件指到临时目录, 不动真实的 run/。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dmk_pl_"))
        self._old = (pipelinelock.RUN_DIR, pipelinelock.PID_PATH,
                     pipelinelock.LOOP_LOCK, pipelinelock.TICK_LOCK,
                     pipelinelock.STOP_PATH)
        pipelinelock.RUN_DIR = self.tmp
        pipelinelock.PID_PATH = self.tmp / "pipeline.pid"
        pipelinelock.LOOP_LOCK = self.tmp / "pipeline.loop.lock"
        pipelinelock.TICK_LOCK = self.tmp / "pipeline.tick.lock"
        pipelinelock.STOP_PATH = self.tmp / "pipeline.stop"

    def close(self):
        (pipelinelock.RUN_DIR, pipelinelock.PID_PATH, pipelinelock.LOOP_LOCK,
         pipelinelock.TICK_LOCK, pipelinelock.STOP_PATH) = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_loop_lock():
    print("\n[单实例] 常驻锁: 第二个进程必须拿不到")
    sb = Sandbox()
    try:
        a = pipelinelock.FileLock(pipelinelock.LOOP_LOCK)
        check("第一个拿到锁", a.acquire())
        b = pipelinelock.FileLock(pipelinelock.LOOP_LOCK)
        check("第二个拿不到", not b.acquire(), "同一进程内同文件两次加锁必须失败")
        a.release()
        c = pipelinelock.FileLock(pipelinelock.LOOP_LOCK)
        check("释放后可以再拿", c.acquire())
        c.release()
    finally:
        sb.close()


def test_tick_lock_wait():
    print("\n[单轮] 单轮锁: 等待超时返回 False, 不阻塞调用方")
    sb = Sandbox()
    try:
        a = pipelinelock.FileLock(pipelinelock.TICK_LOCK)
        a.acquire()
        b = pipelinelock.FileLock(pipelinelock.TICK_LOCK)
        t0 = time.time()
        got = b.acquire(wait_sec=0)
        cost = time.time() - t0
        check("已被占用时立刻失败", not got)
        check("wait_sec=0 不空转", cost < 1.0, f"{cost:.2f}s")
        a.release()
        check("释放后能拿到", pipelinelock.FileLock(pipelinelock.TICK_LOCK).acquire())
    finally:
        sb.close()


def test_heartbeat_visibility():
    print("\n[可见性] 心跳文件让外部启动的进程也能被看见")
    sb = Sandbox()
    try:
        check("没有心跳 -> 不在跑", not pipelinelock.is_alive().get("alive"))
        pipelinelock.beat(3600)
        info = pipelinelock.is_alive()
        check("刚写心跳 -> 在跑", info.get("alive"), str(info))
        check("心跳带 PID", info.get("pid"), str(info))
        check("心跳带间隔", info.get("interval") == 3600, str(info))

        # 陈旧心跳: 超过 2 个间隔 + 60 秒就算死了, 否则崩溃的进程会永远显示"运行中"
        d = json.loads(pipelinelock.PID_PATH.read_text(encoding="utf-8"))
        d["heartbeat"] = time.time() - (3600 * 2 + 120)
        pipelinelock.PID_PATH.write_text(json.dumps(d), encoding="utf-8")
        info = pipelinelock.is_alive()
        check("陈旧心跳 -> 判定已停止", not info.get("alive"), str(info))
        check("陈旧时仍报出 PID 便于排查", info.get("pid"), str(info))

        pipelinelock.clear_pid()
        check("清理后不再可见", not pipelinelock.is_alive().get("alive"))
    finally:
        sb.close()


def test_stop_request():
    print("\n[停止] 用请求文件, 不强杀")
    sb = Sandbox()
    try:
        check("默认没有停止请求", not pipelinelock.stop_requested())
        pipelinelock.request_stop()
        check("写入后能读到", pipelinelock.stop_requested())
        pipelinelock.clear_stop()
        check("清理后回到 False", not pipelinelock.stop_requested())
    finally:
        sb.close()


def test_bad_pid_file():
    print("\n[健壮] 坏心跳文件不该让控制台报错")
    sb = Sandbox()
    try:
        pipelinelock.PID_PATH.write_text("{ not json", encoding="utf-8")
        check("坏 JSON -> 当作不在跑", not pipelinelock.is_alive().get("alive"))
        pipelinelock.PID_PATH.write_text("[]", encoding="utf-8")
        check("顶层不是对象 -> 当作不在跑", not pipelinelock.is_alive().get("alive"))
        pipelinelock.PID_PATH.write_text(
            json.dumps({"pid": 1, "interval": "x", "heartbeat": "y"}), encoding="utf-8")
        check("字段类型不对 -> 当作不在跑", not pipelinelock.is_alive().get("alive"))
    finally:
        sb.close()


def main() -> int:
    print("发布进程锁与心跳回归")
    test_loop_lock()
    test_tick_lock_wait()
    test_heartbeat_visibility()
    test_stop_request()
    test_bad_pid_file()
    print()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项未过")
        for n in FAILED:
            print(f"  {n}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
