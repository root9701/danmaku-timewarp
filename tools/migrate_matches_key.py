#!/usr/bin/env python
"""把 matches.json 里的对齐记录迁移到 v3 的会话标识(date|room)。

v3 结构下会话标识变成 (赛事日, 房间号), 而旧记录用的是 (数据日, 会话目录名)。
不迁移的话总览读不到逐分P 对齐结果(parts 为空)。

规则: 用 manifest 建立 "旧目录名 -> 新标识" 映射, 逐条改写 align 的 key。
只处理能唯一确定归属的; 有歧义(多个旧会话映射到同一新 key)时保留弹幕/置信度更高的那条。
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 从仓库根推, 不写死绝对路径(换机器/换目录仍可用)
PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data"
MATCHES = PROJECT / "danmaku-server" / "matches.json"


def load():
    return json.loads(MATCHES.read_text(encoding="utf-8"))


def build_map():
    """建立 旧会话名 -> (赛事日, room) 映射。

    关键: v3 目录名是纯房间号, 同一个 room 在不同日期下会重复出现,
    所以**不能**用 "room -> 某一天" 的字典(后写的会覆盖前面的)。
    正确做法: 同时记录每个会话的 start_ms, 迁移时用旧会话名里的 HHMM 去匹配。
    """
    by_name = {}      # 旧/新会话名 -> [(day, room, start_ms)]
    sessions = []     # [(day, room, start_ms, sess_name)]
    for mf in DATA.glob("*/*/manifest.json"):
        try:
            d = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sess = mf.parent
        day = d.get("match_date") or sess.parent.name
        room = d.get("room")
        if not room:
            continue
        sessions.append((day, str(room), d.get("start_ms") or 0, sess.name))
    return sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    data = load()
    align = data.get("align") or {}
    sessions = build_map()

    print("=== 现有会话 ===")
    for day, room, st, name in sorted(sessions):
        print("  %-28s -> %s|%s  start_ms=%s" % (name, day, room, st))

    def resolve(filename, bv=None):
        """把旧会话名解析成 (day, room)。

        三种来源:
          2026-10-25_2332_7734200   -> 名字里带 HHMM, 用 room + 时间就近匹配
          2026-10-24_0819_7734200   -> 同上
          7734200                   -> 有歧义(多天同 room), 用 BV 所属赛程日定夺
        """
        # 纯房间号且多天都有同 room 时, 用该 BV 出现在哪天的 lists 里来判定
        if bv:
            for day_key, lst in (data.get("lists") or {}).items():
                for it in (lst or []):
                    if isinstance(it, dict) and it.get("bv") == bv:
                        same = [s for s in sessions
                                if s[1] == filename and s[0] == day_key]
                        if same:
                            return (same[0][0], same[0][1])
        cands = [s for s in sessions if s[1] == filename and s[0] != "_test"]
        if cands:
            return (cands[0][0], cands[0][1])
        m = re.match(r"^\d{4}-\d{2}-\d{2}_(\d{4})_(\d+)$", filename)
        if not m:
            m = re.match(r"^\d{4}-\d{2}-\d{2}T(\d{4})_(\d+)$", filename)
        if m:
            hhmm, room = m.group(1), m.group(2)
            same_room = [s for s in sessions if s[1] == room and s[0] != "_test"]
            if not same_room:
                return None
            if len(same_room) == 1:
                return (same_room[0][0], same_room[0][1])
            import datetime
            target_min = int(hhmm[:2]) * 60 + int(hhmm[2:])

            def dist(s):
                if not s[2]:
                    return 9999
                dt = datetime.datetime.fromtimestamp(s[2] / 1000)
                d = abs((dt.hour * 60 + dt.minute) - target_min)
                return min(d, 1440 - d)
            best = min(same_room, key=dist)
            return (best[0], best[1])
        return None

    new_align = {}
    report = []
    for key, entry in align.items():
        date, _, fname = key.partition("|")
        tgt = resolve(fname, entry.get("bv"))
        if not tgt:
            report.append((key, None, "无对应会话"))
            continue
        newday, room = tgt
        newkey = "%s|%s" % (newday, room)
        e = dict(entry)
        e["date"], e["file"] = newday, room
        if newkey in new_align:
            old = new_align[newkey]

            def score(x):
                return (1 if x.get("pages") else 0,
                        (x.get("auto") or {}).get("confidence") or 0)
            if score(e) > score(old):
                new_align[newkey] = e
                report.append((key, newkey, "冲突, 用信息更全的"))
            else:
                report.append((key, newkey, "冲突, 保留原有"))
        else:
            new_align[newkey] = e
            report.append((key, newkey, "迁移"))

    print()
    print("=== 迁移明细 ===")
    for old, new, info in report:
        if new is None:
            print("  %-38s -> (无对应会话, 丢弃)  %s" % (old, info if isinstance(info, str) else ""))
        else:
            print("  %-38s -> %-18s %s" % (old, new, info))

    data["align"] = new_align
    # lists 里的 offset_sec 也按 BV 同步成 pages 里的第一分P(如果有)
    for day_list in (data.get("lists") or {}).values():
        if not isinstance(day_list, list):
            continue
        for it in day_list:
            if not isinstance(it, dict):
                continue
            bv = it.get("bv")
            for e in new_align.values():
                if bv and e.get("bv") == bv and e.get("pages"):
                    pgs = e["pages"]
                    first = str(min(int(k) for k in pgs))
                    it["pages"] = pgs
                    it["offset_sec"] = pgs[first].get("offset_sec")
                    break

    if not args.apply:
        print()
        print("(预演, 未写入; 加 --apply 执行)")
        return 0

    tmp = MATCHES.with_name("matches.json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MATCHES)
    print()
    print("已写入 %s" % MATCHES)
    return 0


if __name__ == "__main__":
    sys.exit(main())

