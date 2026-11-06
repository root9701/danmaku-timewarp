# -*- coding: utf-8 -*-
"""test_discover.py — 采集端把 Riot 草稿合进场次列表的规则。

用法: python tests/test_discover.py     (退出码 0 = 全过)

赛程发现是采集端 `tools/nightly.py` 的职责, 播放端只读分发下来的 `align.json`,
不再提供「刷新赛程」。这里覆盖 nightly 依赖的合并/兜底/搜索排序规则。

背景(实测缺陷): 2026-10-30 当晚先上线 KC vs CFO, 列表里就一条; 几小时后
BLG vs TES 的回放也上线了, 但新场次进不去 —— 旧判据只问"有没有要回填空 bv
的条目", 列表里那一条已经完整, 判据恒为 False。这里把合并规则作为纯函数覆盖。

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


def t_draft_without_bv():
    """回放还没上传时, 草稿里根本没有 bv 键 —— 不能因此崩掉整天的发现。

    2026-11-06 前的实现给每条草稿都赋了 bv(即使是空串); 改成"搜到才赋值"
    后, 空结果就意味着缺键, `_apply_discover` 里 m["bv"] 直接 KeyError。
    后果不是少一个 BV, 而是场次列表根本建不出来, 推送闸门永远不放行。
    """
    print("\n[合并] 草稿缺 bv 键时不崩")
    draft = [{"title": "DK vs BLG", "teams": ["DK", "BLG"],
              "start": datetime(2026, 11, 6, 6, 0)}]
    m = {"lists": {}, "align": {}}
    r = _result()
    server._apply_discover(m, "2026-11-06", draft, r)
    lst = m["lists"].get("2026-11-06") or []
    check("场次建出来了", len(lst) == 1, str(lst))
    check("bv 落成空串", bool(lst) and lst[0].get("bv") == "", str(lst))
    check("start_ms 有值", bool(lst) and "start_ms" in lst[0], str(lst))

    # 已有列表里追加一场"还没有 BV"的比赛, 同样不能崩
    m2 = {"lists": {"2026-11-06": [dict(lst[0])]}, "align": {}}
    r2 = _result()
    draft2 = [{"title": "GEN vs LYON", "teams": ["GEN", "LYON"],
               "start": datetime(2026, 11, 6, 8, 0)}]
    server._apply_discover(m2, "2026-11-06", draft2, r2)
    got = m2["lists"]["2026-11-06"]
    check("追加了第二场", len(got) == 2, str(got))
    check("第二场 bv 为空", got[1].get("bv") == "", str(got))


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


def t_replay_search_order():
    """单场回放搜索也要查最新排序。

    2026-11-06 实测: 官方 06:57 已上传 DK vs BLG, 但「全球总决赛 DK BLG」
    综合排序前 20 条没有官方条目; nightly 对 Riot 已列出的场次走的正是这条
    搜索路径, 只搜一种排序会让该场永远没有 BV、推送闸门永远不放行。
    """
    print("\n[兜底] 单场回放搜索也查最新排序")
    official = {"type": "video", "mid": server.BILI_OFFICIAL_MID,
                "title": "【2026全球总决赛】11月6日 DK vs BLG",
                "bvid": "BV_NEW"}
    calls = []

    def fake_search(kw, order=""):
        calls.append(order)
        return [official] if order == "pubdate" else []

    old = server._bili_search_videos
    server._bili_search_videos = fake_search
    try:
        got = server.bili_search_replay("DK", "BLG", "2026-11-06")
    finally:
        server._bili_search_videos = old
    check("综合与最新都搜过", calls == ["", "pubdate"], str(calls))
    check("找回了官方回放", got == "BV_NEW", got)


def t_replay_rejects_clips():
    """高光/切片不能被当成整场回放, 否则错误的 BV 会永久占位。

    `_apply_discover` 只回填空 BV、不覆盖已有 BV; 一旦短片段写进去, 后续每轮
    发现都会跳过该场, 校准永远失败, 推送闸门也永远不开。
    """
    print("\n[兜底] 单场搜索挡掉高光与短切片")
    official = {"type": "video", "mid": server.BILI_OFFICIAL_MID,
                "title": "【2026全球总决赛】11月6日 DK vs BLG",
                "bvid": "BV_FULL", "duration": "1:56:54"}
    clip = {"type": "video", "mid": server.BILI_OFFICIAL_MID,
            "title": "【2026全球总决赛】11月6日 DK vs BLG 高光",
            "bvid": "BV_CLIP", "duration": "3:20"}
    short = {"type": "video", "mid": server.BILI_OFFICIAL_MID,
             "title": "【2026全球总决赛】11月6日 DK vs BLG 速看",
             "bvid": "BV_SHORT", "duration": "3:20"}

    def fake_search(kw, order=""):
        return [clip, short, official]

    old = server._bili_search_videos
    server._bili_search_videos = fake_search
    try:
        got = server.bili_search_replay("DK", "BLG", "2026-11-06")
    finally:
        server._bili_search_videos = old
    check("选中的是整场回放", got == "BV_FULL", got)


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
              "bv": "BV_TEST_FULL", "start_is_publish": True,
              "start": datetime(2026, 11, 4, 6, 59, 53, tzinfo=TZ)}]
    server._apply_discover(m, "2026-11-04", draft, r)
    got = m["lists"].get("2026-11-04") or []
    check("创建了当天列表", len(got) == 1, str(got))
    if got:
        check("带 BV", got[0]["bv"] == "BV_TEST_FULL", str(got[0]))
        check("不把投稿时间当开赛时间", "start_ms" not in got[0], str(got[0]))


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


def t_buvid3_from_spi():
    """buvid3 必须能从官方指纹接口拿到 —— 首页已不再下发这条 Cookie。

    2026-11-07 实测: https://www.bilibili.com/ 返回 200 但 Set-Cookie 是 0 条,
    于是 buvid3 永远为空 -> 搜索接口一律 412 Precondition Failed -> 赛程能建出来
    但永远拿不到 BV -> 发布闸门永远不放行(当天数据卡在本地)。
    /x/frontend/finger/spi 的 data.b_3 就是 buvid3。
    """
    print("\n[buvid3] 从官方指纹接口取, 不依赖首页 Set-Cookie")

    class FakeResp:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def get_all(self, _name):
            return []            # 首页已不下发 buvid3

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        if "finger/spi" in u:
            return FakeResp(json.dumps({"code": 0,
                                        "data": {"b_3": "BVUID3-FROM-SPI",
                                                 "b_4": "x"}}).encode())
        return FakeResp(b"<html></html>")

    old_open = server.urllib.request.urlopen
    old_cache = dict(server._BUVID3_CACHE)
    server._BUVID3_CACHE.update(ts=0, value="")
    server.urllib.request.urlopen = fake_urlopen
    try:
        got = server._bili_buvid3()
    finally:
        server.urllib.request.urlopen = old_open
        server._BUVID3_CACHE.clear()
        server._BUVID3_CACHE.update(old_cache)
    check("从 spi 拿到 buvid3", got == "BVUID3-FROM-SPI", repr(got))


def t_search_headers_carry_buvid3():
    """搜索请求头必须带上 buvid3 —— 缺它就是 412。"""
    print("\n[buvid3] 搜索请求头带 Cookie")
    old = server._bili_buvid3
    server._bili_buvid3 = lambda: "BVUID3-X"
    try:
        h = server._bili_search_headers()
    finally:
        server._bili_buvid3 = old
    cookie = h.get("Cookie") or ""
    check("Cookie 里有 buvid3", "buvid3=BVUID3-X" in cookie, cookie)


def main() -> int:
    t_append()
    t_draft_without_bv()
    t_no_clobber()
    t_team_order()
    t_fill()
    t_sort()
    t_bili_date_fallback()
    t_bili_date_mismatch()
    t_search_order()
    t_replay_search_order()
    t_replay_rejects_clips()
    t_discover_fallback()
    t_apply_fallback()
    t_buvid3_from_spi()
    t_search_headers_carry_buvid3()
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
