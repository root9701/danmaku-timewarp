# -*- coding: utf-8 -*-
"""split_matches.py — 把 matches.json 拆成 align/{日期}.json + 本地覆盖层。

用法:
    python tools/split_matches.py            # 干跑, 只报告
    python tools/split_matches.py --apply    # 真正写入

拆分规则:
  - align/{日期}.json  一天一个文件, **入库分发**。只装该日的 align 记录与 lists 条目。
  - matches.json       本地覆盖层, **不入库**。拆完后只留"无法归日"的残留(通常为空)。

为什么要拆: matches.json 同时装"作者算好的对齐结果"和"用户自己的手动微调",
混在一个被 gitignore 的文件里, 既发不出去也合不回来。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRV = ROOT / "danmaku-server"
MATCHES = SRV / "matches.json"
ALIGN_DIR = SRV / "align"

DATE_KEYS = ("date", "data_date", "match_date")


def key_date(key: str):
    """从 "{日期}|{文件}[|{BV}]" 取日期; 取不到返回 None。"""
    head = str(key or "").split("|")[0].strip()
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入(默认只干跑)")
    args = ap.parse_args()

    if not MATCHES.is_file():
        print(f"没有 {MATCHES}, 无需拆分")
        return 0
    try:
        data = json.loads(MATCHES.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"matches.json 解析失败: {e}")
        return 2
    if not isinstance(data, dict):
        print("matches.json 顶层不是对象, 放弃")
        return 2

    # 兼容旧格式(顶层平铺 align 条目)
    if "align" in data or "lists" in data:
        align = data.get("align") or {}
        lists = data.get("lists") or {}
    else:
        align, lists = data, {}

    per_day = {}          # 日期 -> {"align": {...}, "lists": {...}}
    unassigned = {"align": {}, "lists": {}}

    for k, v in align.items():
        d = key_date(k)
        if d is None and isinstance(v, dict):
            d = str(v.get("date") or "").strip() or None
        if d:
            per_day.setdefault(d, {"align": {}, "lists": {}})["align"][k] = v
        else:
            unassigned["align"][k] = v

    for d, lst in lists.items():
        dd = str(d or "").strip()
        if len(dd) == 10 and dd[4] == "-" and dd[7] == "-":
            per_day.setdefault(dd, {"align": {}, "lists": {}})["lists"][dd] = lst
        else:
            unassigned["lists"][d] = lst

    print(f"matches.json: {len(align)} 条对齐记录, {len(lists)} 天场次列表")
    print(f"归入 {len(per_day)} 个日期文件:")
    for d in sorted(per_day):
        e = per_day[d]
        print(f"  align/{d}.json   {len(e['align'])} 条记录, "
              f"{len(e['lists'])} 天 lists")
    if unassigned["align"] or unassigned["lists"]:
        print(f"无法归日(留在本地层): {len(unassigned['align'])} 条记录, "
              f"{len(unassigned['lists'])} 天 lists")

    if not args.apply:
        print("\n[干跑] 加 --apply 才写入")
        return 0

    ALIGN_DIR.mkdir(parents=True, exist_ok=True)
    for d in sorted(per_day):
        body = per_day[d]
        p = ALIGN_DIR / f"{d}.json"
        if p.is_file():
            # 已存在: 合并(以新拆出来的为准, 但保留文件里已有的其他记录)
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            merged = {"align": dict((old.get("align") or {})),
                      "lists": dict((old.get("lists") or {}))}
            merged["align"].update(body["align"])
            merged["lists"].update(body["lists"])
            body = merged
        p.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
                     + "\n", encoding="utf-8")
        print(f"写入 {p.relative_to(ROOT)}")

    # 本地层只留无法归日的残留
    if unassigned["align"] or unassigned["lists"]:
        MATCHES.write_text(
            json.dumps(unassigned, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"本地层保留 {len(unassigned['align'])} 条记录")
    else:
        backup = MATCHES.with_suffix(".json.bak")
        MATCHES.replace(backup)
        print(f"本地层已清空, 原文件备份为 {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
