# -*- coding: utf-8 -*-
"""daygate.py — 发布闸门与重试窗口。

只管两件与日期有关、且只读数据的事:
- `calibrated(day)`: 这一天是否已有可播放的对齐结果(推送闸门)。
- `too_old(day)`: 是否已超过自动重试窗口(避免每小时无休止重搜)。

为什么单独成文件: 它是"哪些数据允许出去"的策略, 与 nightly 的编排分开;
也避免 tools/nightly.py 继续长过 400 行。只依赖 server 的只读接口, 不写任何文件。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SERVER_DIR = str(ROOT / "danmaku-server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import server  # noqa: E402

# 数据目录日超过这么多天仍未校准, 就不再自动重搜; 需要时用 --date 手动跑。
RETRY_DAYS = 3


def too_old(day: str) -> bool:
    """数据目录日超过 RETRY_DAYS 天: 不再自动重搜赛程。

    推送闸门不允许未校准的数据出仓, 而 dirty_days() 会一直返回未提交的日期。
    若某天永远校准不了(官方一直没传回放, 或当天其实没有比赛), 不设上限就会
    每小时对同一天重搜 B站、重跑校准; 日期越积越多, 最终触发风控, 也拖慢
    正在采集的那天。超过窗口后只保留本地数据, 需要时用 `--date` 手动跑一次。
    """
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now().date() - d).days > RETRY_DAYS


def calibrated(day: str) -> bool:
    """当天是否已有可播放的对齐结果(至少一个分P)。

    推送闸门: 没有场次、或场次一个分P 都没对齐的原始数据不发布。
    用户拉到的每一天都必须自带赛程 —— 否则播放端只有一堆弹幕, 不知道播哪场。
    2026-11-06 的事故就是校准还没成, 数据先推上去了。

    数据不会丢: 未过闸门的日子留在本地工作区; 最近 RETRY_DAYS 天继续重试,
    超过后不再自动重搜(避免每小时无休止请求 B站), 需要时手动 --date 跑一次。
    """
    try:
        match_date = server._session_match_date(day)
        lists = (server.load_matches() or {}).get("lists") or {}
    except Exception:
        return False
    for m in lists.get(match_date) or []:
        if not isinstance(m, dict) or not str(m.get("bv") or "").strip():
            continue
        if isinstance(m.get("pages"), dict) and m["pages"]:
            return True
    return False
