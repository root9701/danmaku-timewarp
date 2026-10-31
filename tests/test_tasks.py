# -*- coding: utf-8 -*-
"""采集任务表回归: 跨零点、错过/立即开始、重叠校验。"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "danmaku-collector"))

import tasks  # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def test_window_and_data_date():
    t = {"date": "2026-10-31", "start": "23:00", "end": "03:00"}
    s, e = tasks.window(t)
    check("跨零点截止顺延到次日", e.date().isoformat() == "2026-11-01", str(e))
    check("数据日按结束日", tasks.data_date(t) == "2026-11-01", tasks.data_date(t))
    t2 = {"date": "2026-10-31", "start": "00:30", "end": "09:00"}
    check("同日任务数据日不变", tasks.data_date(t2) == "2026-10-31", tasks.data_date(t2))
    t3 = {"date": "2026-10-31", "start": "23:00"}
    s3, e3 = tasks.window(t3)
    check("无截止时结束为 None", e3 is None, str(e3))


def test_next_task():
    now = datetime(2026, 10, 31, 20, 0)
    future = {"date": "2026-11-01", "start": "00:30", "end": "09:00",
              "rooms": [7734200], "status": "pending"}
    r = tasks.next_task([future], now)
    check("未到开始 -> wait", r.get("action") == "wait", str(r))

    now = datetime(2026, 11, 1, 0, 45)
    r = tasks.next_task([future], now)
    check("窗口内 -> start", r.get("action") == "start", str(r))

    now = datetime(2026, 11, 1, 9, 10)
    r = tasks.next_task([future], now)
    check("已过结束 -> missed", r.get("action") == "missed", str(r))

    running = dict(future, status="running")
    check("有 running 任务时不排下一个", tasks.next_task([running, future], now) == {})

    early = dict(future, date="2026-10-31", start="23:00", end="03:00")
    r = tasks.next_task([future, early], datetime(2026, 10, 31, 22, 59))
    check("取最早开始的一场", r.get("date") == "2026-10-31", str(r))


def test_validate():
    good = [{"date": "2026-10-31", "start": "00:30", "end": "09:00",
             "rooms": [7734200], "status": "pending"},
            {"date": "2026-11-01", "start": "00:30", "end": "09:00",
             "rooms": [7734200], "status": "pending"}]
    check("两晚不相邻不报重叠", tasks.validate(good) == [], tasks.validate(good))

    bad_overlap = [
        {"date": "2026-10-31", "start": "23:00", "end": "03:00", "rooms": [7734200]},
        {"date": "2026-11-01", "start": "01:00", "end": "09:00", "rooms": [7734200]}]
    check("跨零点窗口重叠被拦住", any("重叠" in p for p in tasks.validate(bad_overlap)),
          tasks.validate(bad_overlap))

    bad_time = [{"date": "2026-10-31", "start": "25:00", "end": "09:00",
                 "rooms": [7734200]}]
    check("非法时刻被拦住", bool(tasks.validate(bad_time)))
    bad_room = [{"date": "2026-10-31", "start": "00:30", "end": "09:00", "rooms": []}]
    check("空房间被拦住", bool(tasks.validate(bad_room)))


def main() -> int:
    print("采集任务表回归")
    test_window_and_data_date()
    test_next_task()
    test_validate()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
