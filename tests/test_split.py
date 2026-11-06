# -*- coding: utf-8 -*-
"""split_matches.py：换源记录不能随本地层清空而丢失。

用法: python tests/test_split.py     (退出码 0 = 全过)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import split_matches  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        FAILED.append(name)


def test_source_swap_kept_local():
    print("\n[换源] 只保留在本地层, 不写进 data/")
    with tempfile.TemporaryDirectory(prefix="dmk_split_") as td:
        base = Path(td)
        data = base / "data"
        (data / "2026-11-06").mkdir(parents=True)
        matches = base / "matches.json"
        rec = {"date": "2026-11-06", "file": "7734200", "title": "DK vs BLG",
               "source_swap": True, "offset_sec": 12.3}
        matches.write_text(json.dumps({"align": {"2026-11-06|7734200": rec},
                                       "lists": {}}, ensure_ascii=False),
                           encoding="utf-8")
        old_root, old_data, old_matches = (split_matches.ROOT, split_matches.DATA,
                                           split_matches.MATCHES)
        old_argv = sys.argv
        split_matches.ROOT, split_matches.DATA, split_matches.MATCHES = base, data, matches
        sys.argv = ["split_matches.py", "--apply"]
        try:
            rc = split_matches.main()
        finally:
            split_matches.ROOT, split_matches.DATA, split_matches.MATCHES = (
                old_root, old_data, old_matches)
            sys.argv = old_argv
        check("拆分成功", rc == 0, str(rc))
        left = json.loads(matches.read_text(encoding="utf-8"))
        check("本地层保留换源记录", "2026-11-06|7734200" in (left.get("align") or {}),
              str(left))
        align = json.loads((data / "2026-11-06" / "align.json").read_text(encoding="utf-8"))
        check("分发层不含换源记录", "2026-11-06|7734200" not in (align.get("align") or {}),
              str(align))


def test_missing_data_dir_keeps_local():
    print("\n[缺目录] data/{日期}/ 不存在时记录保留在本地层")
    with tempfile.TemporaryDirectory(prefix="dmk_split_") as td:
        base = Path(td)
        data = base / "data"
        (data / "2026-11-06").mkdir(parents=True)
        matches = base / "matches.json"
        rec = {"date": "2026-11-07", "file": "7734200", "bv": "BV1X",
               "offset_sec": 12.3}
        lst = [{"title": "TES vs IG", "bv": "BV1X", "offset_sec": 12.3}]
        matches.write_text(json.dumps(
            {"align": {"2026-11-07|7734200|BV1X": rec},
             "lists": {"2026-11-07": lst}}, ensure_ascii=False),
            encoding="utf-8")
        old_root, old_data, old_matches = (split_matches.ROOT, split_matches.DATA,
                                           split_matches.MATCHES)
        old_argv = sys.argv
        split_matches.ROOT, split_matches.DATA, split_matches.MATCHES = base, data, matches
        sys.argv = ["split_matches.py", "--apply"]
        try:
            rc = split_matches.main()
        finally:
            split_matches.ROOT, split_matches.DATA, split_matches.MATCHES = (
                old_root, old_data, old_matches)
            sys.argv = old_argv
        check("拆分成功", rc == 0, str(rc))
        left = json.loads(matches.read_text(encoding="utf-8"))
        check("对齐记录仍在本地层", "2026-11-07|7734200|BV1X" in (left.get("align") or {}),
              str(left))
        check("场次列表仍在本地层", "2026-11-07" in (left.get("lists") or {}), str(left))
        check("没有凭空造出日期目录", not (data / "2026-11-07").exists())


def test_pending_days():
    print("\n[待分发] 本地层有待分发记录的日期")
    with tempfile.TemporaryDirectory(prefix="dmk_split_") as td:
        base = Path(td)
        data = base / "data"
        (data / "2026-11-06").mkdir(parents=True)
        matches = base / "matches.json"
        matches.write_text(json.dumps({
            "align": {
                "2026-11-06|7734200|BV1A": {"date": "2026-11-06", "offset_sec": 1.0},
                "2026-11-07|7734200|BV1B": {"date": "2026-11-07", "offset_sec": 2.0},
                "2026-11-06|7734200|BV1C": {"date": "2026-11-06", "source_swap": True},
            },
            "lists": {"2026-11-06": [], "2026-11-07": []},
        }, ensure_ascii=False), encoding="utf-8")
        old_data, old_matches = split_matches.DATA, split_matches.MATCHES
        split_matches.DATA, split_matches.MATCHES = data, matches
        try:
            got = split_matches.pending_days()
        finally:
            split_matches.DATA, split_matches.MATCHES = old_data, old_matches
        check("只返回有 data 目录的日期", got == ["2026-11-06"], str(got))
        check("换源记录不算待分发", "2026-11-06" in got and len(got) == 1, str(got))


def main() -> int:
    test_source_swap_kept_local()
    test_missing_data_dir_keeps_local()
    test_pending_days()
    print()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项未过")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
