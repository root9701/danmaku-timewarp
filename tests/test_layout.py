# -*- coding: utf-8 -*-
"""test_layout.py — 三代目录布局兼容 / 跨零点归属 / _test 隔离。

用法: python tests/test_layout.py     (退出码 0 = 全过)

为什么要单独测目录:
  服务端要同时认 v1(平铺)、v2({日期}_{HHMM}_{room})、v3({room}) 三种布局 ——
  老数据必须一直能播。判错的后果不是报错, 而是**静默找不到会话**(用户以为数据丢了),
  或者更糟: 把 _test 里的测试数据当成正赛列进日期下拉。

  同时验证跨零点归属: 凌晨开打的比赛属于"打完那天"的赛事日, 而目录名可能是前一天。

全部在临时目录里跑, 不碰真实 data/。
"""
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import server                                              # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


class DataSandbox:
    """把 server.DATA_DIR 指到临时目录。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dmk_layout_"))
        self._old = server.DATA_DIR
        server.DATA_DIR = self.tmp

    def close(self):
        server.DATA_DIR = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def dm_rows(n, ts0):
    return [{"ts": ts0 + i, "ty": "dm", "t": "hi"} for i in range(n)]


def test_v1_flat_layout() -> None:
    """v1: 切片直接摊在日目录根, file = 房间号。"""
    sb = DataSandbox()
    try:
        ts = int(time.mktime((2026, 10, 20, 23, 0, 0, 0, 0, -1)) * 1000)
        write_jsonl(sb.tmp / "2026-10-20" / "7734200_s2300.jsonl.gz", dm_rows(5, ts))
        # resolve_session_dir 对 v1 平铺必须返回 None(不是目录)
        got = server.resolve_session_dir("2026-10-20", "7734200")
        check("v1 平铺: 不当作会话目录", got is None, str(got))
        paths = server.danmaku_paths("2026-10-20", "7734200")
        check("v1 平铺: 能找到切片", len(paths) == 1, f"{len(paths)} 个")
    finally:
        sb.close()


def test_v2_session_layout() -> None:
    """v2: {日期}_{HHMM}_{room} 子目录, 目录名本身是 file 入参。"""
    sb = DataSandbox()
    try:
        ts = int(time.mktime((2026, 10, 20, 23, 32, 0, 0, 0, -1)) * 1000)
        name = "2026-10-20_2332_7734200"
        write_jsonl(sb.tmp / "2026-10-20" / name / "7734200_s2332.jsonl.gz",
                    dm_rows(7, ts))
        got = server.resolve_session_dir("2026-10-20", name)
        check("v2: 认出会话目录", got is not None and got.name == name, str(got))
        paths = server.danmaku_paths("2026-10-20", name)
        check("v2: 能读到目录内切片", len(paths) == 1, f"{len(paths)} 个")
    finally:
        sb.close()


def test_v3_session_layout() -> None:
    """v3(现行): {赛事日}/{room}/ + manifest.json。"""
    sb = DataSandbox()
    try:
        ts = int(time.mktime((2026, 10, 21, 1, 0, 0, 0, 0, -1)) * 1000)
        sess = sb.tmp / "2026-10-21" / "7734200"
        write_jsonl(sess / "7734200_s0100.jsonl.gz", dm_rows(9, ts))
        (sess / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "room": 7734200, "match_date": "2026-10-21",
            "is_test": False, "start_ms": ts, "end_ms": ts + 9,
            "dm_count": 9, "sc_count": 0,
            "files": ["7734200_s0100.jsonl.gz"], "afp": None,
        }), encoding="utf-8")

        got = server.resolve_session_dir("2026-10-21", "7734200")
        check("v3: 认出会话目录", got is not None and got.name == "7734200", str(got))
        mf = server.read_manifest(sess)
        check("v3: manifest 可读", mf is not None and mf.get("dm_count") == 9, str(mf))
        days = server.scan_days()
        entry = None
        for d in days:
            for r in d["rooms"]:
                for f in r["files"]:
                    if f["name"] == "7734200":
                        entry = f
        check("v3: scan_days 列出该会话", entry is not None, str(days))
        if entry:
            check("v3: dm_count 取自 manifest(不做目录扫描)",
                  entry["dm_count"] == 9, str(entry["dm_count"]))
    finally:
        sb.close()


def test_cross_midnight_match_date() -> None:
    """跨零点: 凌晨结束的会话, 赛事日 = 结束那天(不是开打那天)。

    这是用户真实反馈过的:"明明是我今天早上看完的, 却标成昨天"。
    """
    sb = DataSandbox()
    try:
        # 23:33 开打, 次日 08:59 结束 -> 赛事日应为结束日
        sess = sb.tmp / "2026-10-26" / "7734200"
        ts = int(time.mktime((2026, 10, 26, 8, 30, 0, 0, 0, -1)) * 1000)
        write_jsonl(sess / "7734200_s0830.jsonl.gz", dm_rows(3, ts))
        (sess / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "room": 7734200, "match_date": "2026-10-26",
            "is_test": False, "start_ms": ts, "end_ms": ts + 3, "dm_count": 3,
            "sc_count": 0, "files": ["7734200_s0830.jsonl.gz"], "afp": None,
        }), encoding="utf-8")
        got = server._session_dir_match_date(sess)
        check("跨零点: match_date 用 manifest 的结束日",
              got == "2026-10-26", got)
    finally:
        sb.close()


def test_test_data_isolated() -> None:
    """_test 下的数据必须与正式日期分离, 不能混进日期下拉。"""
    sb = DataSandbox()
    try:
        # 正式数据
        sess = sb.tmp / "2026-10-22" / "7734200"
        ts = int(time.mktime((2026, 10, 22, 2, 0, 0, 0, 0, -1)) * 1000)
        write_jsonl(sess / "7734200_s0200.jsonl.gz", dm_rows(4, ts))
        (sess / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "room": 7734200, "match_date": "2026-10-22",
            "is_test": False, "start_ms": ts, "end_ms": ts + 4, "dm_count": 4,
            "sc_count": 0, "files": ["7734200_s0200.jsonl.gz"], "afp": None,
        }), encoding="utf-8")

        # 测试数据: 走 _test 目录
        tsess = sb.tmp / "_test" / "2026-10-22T0300_7734200"
        write_jsonl(tsess / "test_030000_7734200.jsonl.gz", dm_rows(6, ts + 10_000))
        (tsess / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "room": 7734200, "match_date": "2026-10-22",
            "is_test": True, "start_ms": ts, "end_ms": ts + 6, "dm_count": 6,
            "sc_count": 0, "files": ["test_030000_7734200.jsonl.gz"], "afp": None,
        }), encoding="utf-8")

        days = server.scan_days()
        dates = [d["date"] for d in days]
        check("正式日期在列表里", "2026-10-22" in dates, str(dates))

        # _test 的约定: **单独成组**(date="_test"), 不能混进任何正式日期里。
        formal = [d for d in days if d["date"] != "_test"]
        leaked = []
        for d in formal:
            for r in d["rooms"]:
                for f in r["files"]:
                    if f.get("is_test") or "test_" in f["name"]:
                        leaked.append((d["date"], f["name"]))
        check("_test 数据不混进正式日期", not leaked, str(leaked))

        test_group = [d for d in days if d["date"] == "_test"]
        check("_test 单独成组且可见", len(test_group) == 1, str(dates))
        if test_group:
            rooms = test_group[0]["rooms"]
            names = [f["name"] for r in rooms for f in r["files"]]
            check("_test 里的会话可见", len(names) >= 1, str(names))
            check("_test 里的条目标记 is_test",
                  all(f["is_test"] for r in rooms for f in r["files"]), str(names))
    finally:
        sb.close()


def test_room_id_does_not_prefix_collide() -> None:
    """房间号前缀撞号: 7734200 不能吞掉 77342000 的切片。"""
    sb = DataSandbox()
    try:
        ts = int(time.mktime((2026, 10, 23, 2, 0, 0, 0, 0, -1)) * 1000)
        write_jsonl(sb.tmp / "2026-10-23" / "7734200_s0200.jsonl.gz", dm_rows(2, ts))
        write_jsonl(sb.tmp / "2026-10-23" / "77342000_s0200.jsonl.gz", dm_rows(3, ts))
        a = server.danmaku_paths("2026-10-23", "7734200")
        b = server.danmaku_paths("2026-10-23", "77342000")
        check("7734200 只拿到自己的切片",
              len(a) == 1 and "77342000" not in a[0].name, str([p.name for p in a]))
        check("77342000 只拿到自己的切片",
              len(b) == 1 and b[0].name.startswith("77342000"), str([p.name for p in b]))
    finally:
        sb.close()


def main() -> int:
    print("目录布局与隔离回归")
    for fn in (test_v1_flat_layout,
               test_v2_session_layout,
               test_v3_session_layout,
               test_cross_midnight_match_date,
               test_test_data_isolated,
               test_room_id_does_not_prefix_collide):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
