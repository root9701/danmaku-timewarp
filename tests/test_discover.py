# -*- coding: utf-8 -*-
"""test_discover.py — 「刷新赛程」把 Riot 草稿合进场次列表的规则。

用法: python tests/test_discover.py     (退出码 0 = 全过)

背景(实测缺陷): 2026-10-30 当晚先上线 KC vs CFO, 列表里就一条; 几小时后
BLG vs TES 的回放也上线了, 但点「刷新赛程」刷不出来 —— 旧判据只问
"有没有要回填空 bv 的条目", 列表里那一条已经完整, 判据恒为 False, 新场次
永远进不去。这里把判据与合并都作为纯函数覆盖。

不联网: draft 由 _mk 造, 只测写回规则。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import server                                              # noqa: E402

FAILED: list[str] = []

TZ = timezone(timedelta(hours=8))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def _mk(title: str, hh: int, bv: str = "") -> dict:
    """造一条 discover 草稿(与 discover_draft_for_date 同形)。"""
    c1, c2 = title.split(" vs ")
    return {"title": title, "teams": [c1, c2], "bv": bv,
            "start": datetime(2026, 10, 30, hh, 0, tzinfo=TZ)}


def _result() -> dict:
    return {"created": 0, "added": 0, "backfilled": 0, "time_filled": 0}


D_KCCFO = _mk("KC vs CFO", 4, "BV148xw6DEKP")
D_BLGTES = _mk("BLG vs TES", 7, "BV17XxP6CE7n")

OLD_ONLY_KC = [{"title": "KC vs CFO", "bv": "BV148xw6DEKP", "offset_sec": 43.1,
                "start_ms": 1793304000000}]


def t_need():
    print("\n[判据] 该不该落盘")
    check("列表为空 + 有草稿 -> 落盘", server._discover_need([], [D_KCCFO]))
    check("列表为空 + 无草稿 -> 不落盘", not server._discover_need([], []))
    check("已有条目完整, 草稿无新场 -> 不落盘",
          not server._discover_need(OLD_ONLY_KC, [D_KCCFO]))
    # 这就是线上那个 bug: 第一场完整, 第二场还没进列表
    check("已有条目完整, 草稿多一场 -> 落盘",
          server._discover_need(OLD_ONLY_KC, [D_KCCFO, D_BLGTES]))
    check("已有条目缺 bv -> 落盘",
          server._discover_need([{"title": "KC vs CFO", "bv": "",
                                  "start_ms": 1793304000000}], [D_KCCFO]))
    check("已有条目缺 start_ms -> 落盘",
          server._discover_need([{"title": "KC vs CFO", "bv": "BV148xw6DEKP"}],
                                [D_KCCFO]))
    # 解析不出对阵的占位条目不算"这场已经有了", 草稿里的场次仍需追加
    check("占位条目 + 草稿有对阵 -> 落盘",
          server._discover_need([{"title": "待定", "bv": ""}], [D_KCCFO]))
    # 占位条目 + 草稿为空: 没有可追加的, 也不该为了它写盘
    check("占位条目 + 草稿为空 -> 不落盘",
          not server._discover_need([{"title": "待定", "bv": ""}], []))


def t_append():
    print("\n[合并] 草稿里的新场次要能追加进来")
    m = {"lists": {"2026-10-30": [dict(e) for e in OLD_ONLY_KC]}, "align": {}}
    r = _result()
    server._apply_discover(m, "2026-10-30", [D_KCCFO, D_BLGTES], r)
    got = [e["title"] for e in m["lists"]["2026-10-30"]]
    check("追加了新场次", got == ["KC vs CFO", "BLG vs TES"], f"实际 {got}")
    check("added 计数 = 1", r["added"] == 1, f"实际 {r['added']}")
    check("新场次带 bv", m["lists"]["2026-10-30"][1]["bv"] == "BV17XxP6CE7n")
    check("新场次带 start_ms",
          m["lists"]["2026-10-30"][1]["start_ms"] == 1793314800000)
    check("已有条目的 offset_sec 未被改动",
          m["lists"]["2026-10-30"][0]["offset_sec"] == 43.1)


def t_no_clobber():
    print("\n[合并] 不覆盖已有值")
    m = {"lists": {"2026-10-30": [{"title": "KC vs CFO", "bv": "BV_MANUAL",
                                   "offset_sec": 99.0,
                                   "start_ms": 1793304000000}]}, "align": {}}
    r = _result()
    server._apply_discover(m, "2026-10-30", [D_KCCFO], r)
    e = m["lists"]["2026-10-30"][0]
    check("已有 bv 不被草稿覆盖", e["bv"] == "BV_MANUAL", f"实际 {e['bv']}")
    check("已有 offset_sec 不变", e["offset_sec"] == 99.0)
    check("backfilled = 0", r["backfilled"] == 0, f"实际 {r['backfilled']}")
    check("没有重复追加同一场", len(m["lists"]["2026-10-30"]) == 1)


def t_team_order():
    print("\n[合并] 对阵顺序相反也算同一场")
    # 列表写 "KC vs CFO", 草稿是 "CFO vs KC" —— 不能当成两场各加一条
    flipped = _mk("CFO vs KC", 4, "BV148xw6DEKP")
    m = {"lists": {"2026-10-30": [dict(e) for e in OLD_ONLY_KC]}, "align": {}}
    r = _result()
    server._apply_discover(m, "2026-10-30", [flipped], r)
    check("未因顺序相反而重复追加", len(m["lists"]["2026-10-30"]) == 1,
          f"实际 {[e['title'] for e in m['lists']['2026-10-30']]}")


def t_fill():
    print("\n[合并] 回填空 bv 与缺失 start_ms")
    m = {"lists": {"2026-10-30": [{"title": "KC vs CFO", "bv": "",
                                   "offset_sec": 0}]}, "align": {}}
    r = _result()
    server._apply_discover(m, "2026-10-30", [D_KCCFO], r)
    e = m["lists"]["2026-10-30"][0]
    check("补上 bv", e["bv"] == "BV148xw6DEKP", f"实际 {e['bv']!r}")
    check("补上 start_ms", e["start_ms"] == 1793304000000)
    check("backfilled = 1", r["backfilled"] == 1)
    check("time_filled = 1", r["time_filled"] == 1)


def t_sort():
    print("\n[合并] 追加后按赛程时间排序")
    # 旧列表缺 start_ms 的条目应沉到末尾, 不被排到凌晨场之前
    m = {"lists": {"2026-10-30": [{"title": "GEN vs LYON", "bv": "BV_OLD"}]},
         "align": {}}
    r = _result()
    server._apply_discover(m, "2026-10-30", [D_KCCFO, D_BLGTES], r)
    got = [e["title"] for e in m["lists"]["2026-10-30"]]
    check("有 start_ms 的按时间在前, 缺的在后",
          got == ["KC vs CFO", "BLG vs TES", "GEN vs LYON"], f"实际 {got}")


def main() -> int:
    t_need()
    t_append()
    t_no_clobber()
    t_team_order()
    t_fill()
    t_sort()
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
