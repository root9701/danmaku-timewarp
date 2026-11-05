# -*- coding: utf-8 -*-
"""pipelinelock.py — 发布进程的锁、心跳与停止请求。

为什么需要:
  1. 双开会撞车。2026-11-05 实测: 11-03 启的旧进程与 11-04 控制台派生的进程
     同时在跑, 两边各自 git commit, 撞 index.lock, 还会把同一晚的对齐算两遍。
  2. 控制台要能"看见"不是它派生的发布进程。作者可能在终端手动启动过一个,
     那时控制台手里没有 Popen 句柄 —— 只看心跳才看得见, 猜进程树是猜不准的。
  3. 停要停得干净。被强杀的 nightly 可能停在 git commit 中间, 留下 index.lock。
     所以"停"是写一个请求文件, 由 nightly 自己在一轮结束时退出。

三个文件各有各的活, 不合并:
  pipeline.loop.lock  常驻进程的生命周期锁 —— 全机只允许一个 --loop
  pipeline.tick.lock  单轮执行锁 —— 常驻与手动的一次性运行互斥, 避免并发 commit
  pipeline.pid        PID + 心跳 + 巡检间隔 —— 给控制台显示"在不在跑"
  pipeline.stop       停止请求 —— 优雅退出, 不强杀

只依赖标准库: nightly 与 dashboard 各自独立部署, 不能因为这个模块多出依赖。
"""
import contextlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "danmaku-collector" / "run"
PID_PATH = RUN_DIR / "pipeline.pid"
LOOP_LOCK = RUN_DIR / "pipeline.loop.lock"
TICK_LOCK = RUN_DIR / "pipeline.tick.lock"
STOP_PATH = RUN_DIR / "pipeline.stop"


class FileLock:
    """锁文件上的独占区锁(进程死掉由操作系统释放, 不留僵尸锁)。

    与 collector.SingleInstanceLock 同一手法: Windows 锁第 512 字节而不是第 0 字节,
    这样别的进程仍能读文件开头的 PID, 排查"谁占着"时不会被 PermissionError 挡住。
    """

    def __init__(self, path):
        self.path, self._fh, self._ok = Path(path), None, False

    def _try(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+", encoding="utf-8")
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(512)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._close()
            return False
        except Exception:
            # 锁机制本身出问题只降级放行: 绝不能因为锁坏了就让发布完全停摆
            self._close()
            self._ok = True
            return True
        self._ok = True
        return True

    def acquire(self, wait_sec: float = 0) -> bool:
        """拿锁。wait_sec > 0 时轮询等待(手动校准要等常驻进程跑完当前轮)。"""
        end = time.time() + max(0.0, wait_sec)
        while True:
            if self._try():
                return True
            if time.time() >= end:
                return False
            time.sleep(1)

    def _close(self):
        with contextlib.suppress(Exception):
            if self._fh is not None:
                self._fh.close()
        self._fh = None

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        if self._ok:
            with contextlib.suppress(Exception):
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(512)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            fh.close()
        self._ok = False


def beat(interval: int) -> None:
    """写心跳(原子替换, 别让控制台读到半截 JSON)。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "interval": int(interval),
               "heartbeat": time.time()}
    tmp = PID_PATH.with_name(PID_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, PID_PATH)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def read_pid() -> dict:
    try:
        d = json.loads(PID_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def is_alive() -> dict:
    """发布进程在不在跑。心跳超过 2 个巡检间隔 + 60 秒没更新就当已死。"""
    d = read_pid()
    if not d:
        return {"alive": False}
    try:
        interval = max(60, int(d.get("interval") or 0))
        age = time.time() - float(d.get("heartbeat") or 0)
    except (TypeError, ValueError):
        return {"alive": False}
    if age > interval * 2 + 60:
        return {"alive": False, "stale": True, "pid": d.get("pid")}
    return {"alive": True, "pid": d.get("pid"), "interval": interval,
            "age_sec": int(age)}


def clear_pid() -> None:
    with contextlib.suppress(OSError):
        PID_PATH.unlink(missing_ok=True)


def request_stop() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text("%d\n" % int(time.time()), encoding="utf-8")


def stop_requested() -> bool:
    return STOP_PATH.is_file()


def clear_stop() -> None:
    with contextlib.suppress(OSError):
        STOP_PATH.unlink(missing_ok=True)
