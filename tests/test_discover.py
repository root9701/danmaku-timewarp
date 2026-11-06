# -*- coding: utf-8 -*-
"""test_discover.py — 「刷新赛程」把 Riot 草稿合进场次列表的规则。

用法: python tests/test_discover.py     (退出码 0 = 全过)

背景(实测缺陷): 2026-10-30 当晚先上线 KC vs CFO, 列表里就一条; 几小时后
BLG vs TES 的回放也上线了, 但点「刷新赛程」刷不出来 —— 旧判据只问
"有没有要回填空 bv 的条目", 列表里那一条已经完整, 判据恒为 False, 新场次
永远进不去。这里把判据与合并都作为纯函数覆盖。

不联网: draft 由 _mk 造, 只测写回规则。
"""
import json
import sys
import time
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


def t_bili_date_fallback():
    print("\n[兜底] Riot 当天无场次时, 从官方账号回放标题发现对阵")
    body = {"code": 0, "data": {"result": [
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月4日 TES vs IG",
         "bvid": "BV_TEST_SHORT", "pubdate": 1793746793, "duration": "1:00:00"},
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月4日 TES vs IG",
         "bvid": "BV_TEST_FULL", "pubdate": 1793746793, "duration": "2:59:30"},
        {"type": "video", "mid": 123,
         "title": "【2026全球总决赛】11月4日 TES vs IG",
         "bvid": "BV_TEST_COPY", "pubdate": 1793746793, "duration": "2:59:30"},
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月4日 淘汰赛宣传片",
         "bvid": "BV_TEST_TRAILER", "pubdate": 1793746793, "duration": "0:30"},
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月4日 TES vs IG 解说合集",
         "bvid": "BV_TEST_COLL", "pubdate": 1793746793, "duration": "2:00:00"},
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月1日 HLE vs BLG",
         "bvid": "BV_TEST_OLD", "pubdate": 1793487388, "duration": "1:00:00"},
    ]}}
    old_get = server._http_get
    old_cache = dict(server._BUVID3_CACHE)
    server._BUVID3_CACHE.update(ts=time.monotonic(), value="test")
    server._http_get = lambda url, headers, timeout=8: (200, json.dumps(body))
    try:
        got = server.bili_discover_date("2026-11-04")
    finally:
        server._http_get = old_get
        server._BUVID3_CACHE.clear()
        server._BUVID3_CACHE.update(old_cache)
    check("只认官方正片, 同场去重", len(got) == 1, str(got))
    if got:
        m = got[0]
        check("标题解析为对阵", m["title"] == "TES vs IG", str(m))
        check("同场取时长最长的官方正片", m["bv"] == "BV_TEST_FULL", str(m))
        check("开始时间用回放发布时间",
              m["start"].strftime("%Y-%m-%d %H:%M") == "2026-11-04 06:59",
              str(m["start"]))


def t_discover_fallback():
    print("\n[兜底] 只在 Riot 当天无场次时启用")
    old_riot, old_bili = server.discover_draft_for_date, server.bili_discover_date
    riot = [dict(D_KCCFO)]
    fallback = [dict(D_BLGTES)]
    try:
        server.discover_draft_for_date = lambda d: riot
        server.bili_discover_date = lambda d: fallback
        check("Riot 有场次 -> 不用兜底", server.discover_draft("2026-10-30") == riot)
        server.discover_draft_for_date = lambda d: []
        check("Riot 无场次 -> 用兜底", server.discover_draft("2026-10-30") == fallback)
    finally:
        server.discover_draft_for_date = old_riot
        server.bili_discover_date = old_bili


def t_bili_date_mismatch():
    """官方把 11-05 的比赛标成"11月4日" —— 必须按上传时间归属, 不能只看标题。

    实测 2026-11-05: 官方 07:02 上传 "【2026全球总决赛】11月4日 GEN vs LYON",
    按标题搜"11月5日"永远 0 场, 那一晚就没有场次列表。
    """
    print("\n[兜底] 标题日期写错时按上传时间归属")
    body = {"code": 0, "data": {"result": [
        {"type": "video", "mid": server.BILI_OFFICIAL_MID,
         "title": "【2026全球总决赛】11月4日 GEN vs LYON",
         "bvid": "BV_MISLABELED", "pubdate": 1793833323, "duration": "1:50:04"},
    ]}}
    old_get = server._http_get
    old_cache = dict(server._BUVID3_CACHE)
    server._BUVID3_CACHE.update(ts=time.monotonic(), value="test")
    server._http_get = lambda url, headers, timeout=8: (200, json.dumps(body))
    try:
        got = server.bili_discover_date("2026-11-05")
        # 同一条不能被前一天重复认领
        got_prev = server.bili_discover_date("2026-11-04")
    finally:
        server._http_get = old_get
        server._BUVID3_CACHE.clear()
        server._BUVID3_CACHE.update(old_cache)
    check("标题写错日期也能归到上传日", len(got) == 1 and got[0]["bv"] == "BV_MISLABELED",
          str(got))
    check("不会被前一天重复认领", not got_prev, str(got_prev))


def t_apply_fallback():
    print("\n[兜底] 兜底草稿能落进列表")
    m = {"lists": {}, "align": {}}
    r = _result()
    draft = [{"title": "TES vs IG", "teams": ["TES", "IG"],
              "bv": "BV_TEST_FULL",
              "start": datetime(2026, 11, 4, 6, 59, 53, tzinfo=TZ)}]
    server._apply_discover(m, "2026-11-04", draft, r)
    got = m["lists"].get("2026-11-04") or []
    check("创建了当天列表", len(got) == 1, str(got))
    if got:
        check("带 BV", got[0]["bv"] == "BV_TEST_FULL", str(got[0]))
        check("带 start_ms", got[0]["start_ms"] == 1793746793000, str(got[0]))


def t_search_order():
    """官方回放刚上传时，综合排序前 20 条看不到，只有最新排序能找到。

    2026-11-06 实测: 官方 06:57 已上传 DK vs BLG（BV1Zj4v6aEuj），但
    "2026全球总决赛" 综合排序第 1 页没有它（最新排序在第 17 位），
    日期关键词下前 20 条直接没有 —— 于是 nightly 连续两轮报"没有场次"。
    """
    print("\n[兜底] 综合排序漏掉的官方回放，用最新排序补上")
    official = {"type": "video", "mid": server.BILI_OFFICIAL_MID,
                "title": "【2026全球总决赛】11月6日 DK vs BLG",
                "bvid": "BV_NEW", "duration": "1:56:54",
                "pubdate": int(datetime(2026, 11, 6, 6, 57, tzinfo=TZ).timestamp())}
    urls = []
    old_get = server._http_get
    old_cache = dict(server._BUVID3_CACHE)
    server._BUVID3_CACHE.update(ts=time.monotonic(), value="test")

    def fake_get(url, headers, timeout=8):
        urls.append(url)
        # 模拟线上: 综合排序没有官方这条, 最新排序才有
        rs = [official] if "order=pubdate" in url else []
        return 200, json.dumps({"code": 0, "data": {"result": rs}})

    server._http_get = fake_get
    try:
        got = server.bili_discover_date("2026-11-06")
    finally:
        server._http_get = old_get
        server._BUVID3_CACHE.clear()
        server._BUVID3_CACHE.update(old_cache)
    check("按最新排序查过", any("order=pubdate" in u for u in urls), str(len(urls)))
    check("找回了官方回放", len(got) == 1 and got[0]["bv"] == "BV_NEW", str(got))


def main() -> int:
    t_need()
    t_append()
    t_no_clobber()
    t_team_order()
    t_fill()
    t_sort()
    t_bili_date_fallback()
    t_bili_date_mismatch()
    t_search_order()
    t_discover_fallback()
    t_apply_fallback()
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
