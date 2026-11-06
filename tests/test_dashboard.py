# -*- coding: utf-8 -*-
"""dashboard.py 回归: 推送卡片上的「已对齐 N/M P」。

用法: python tests/test_dashboard.py     (退出码 0 = 全过)

不碰真实数据: 只替换 dashboard._load_schedule 的返回值。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-collector"))

import dashboard                                            # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def test_calibration_info_reads_merged_view():
    """手动校准先写本地层, split 才进 align.json —— 摘要必须读合并视图。

    旧实现直接读 data/{date}/align.json 的 lists[date]: 手动校准完、还没 split 时
    卡片显示「还没有对齐记录」, 用户会以为校准没成功而重复点。
    """
    print("\n[对齐摘要] 读合并视图, 而不是只读 align.json")
    sch = {"date": "2026-11-06", "lists": {"2026-11-06": [
        {"title": "DK vs BLG", "bv": "BV1X",
         "pages": {"1": {}, "2": {}, "3": {}}, "total_pages": 5}]},
        "align": {}}
    old = dashboard._load_schedule
    dashboard._load_schedule = lambda day: sch
    try:
        got = dashboard._calibration_info("2026-11-06")
    finally:
        dashboard._load_schedule = old
    check("读到场次", got.get("ok") and len(got["items"]) == 1, str(got))
    check("分P 进度 3/5", got["items"][0]["aligned"] == 3
          and got["items"][0]["total"] == 5, str(got))


def test_calibration_info_follows_match_date():
    """列表键是比赛日、date 是数据目录日: 凌晨场次差一天, 不能只看 lists[date]。

    也不能把**别的晚上**的场次算进来 —— 卡片上会出现不属于这天的比赛。
    """
    print("\n[对齐摘要] 按比赛日取列表, 不串到别的日期")
    sch = {"date": "2026-10-25",
           "lists": {"2026-10-26": [{"title": "HLE vs IG", "bv": "BV1A",
                                     "pages": {"1": {}}, "total_pages": 3}],
                     "2026-10-27": [{"title": "G2 vs TES", "bv": "BV1B",
                                     "pages": {"1": {}, "2": {}}, "total_pages": 3}]},
           "align": {}}
    old = dashboard._load_schedule
    dashboard._load_schedule = lambda day: sch
    try:
        got = dashboard._calibration_info("2026-10-25")
    finally:
        dashboard._load_schedule = old
    titles = [i["title"] for i in got.get("items") or []]
    check("读到次日的列表(凌晨场次)", titles == ["HLE vs IG"], str(titles))
    check("没有把后一天的场次算进来", "G2 vs TES" not in titles, str(titles))


def main() -> int:
    print("dashboard 回归")
    test_calibration_info_reads_merged_view()
    test_calibration_info_follows_match_date()
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
