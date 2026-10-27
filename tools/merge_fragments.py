#!/usr/bin/env python
"""把同一房间同一晚的多个碎片会话目录合并成一个 v3 会话目录。

背景(v1 代码的 bug, 已在新代码修掉):
  旧实现里每个整点切片都会重新调用 _session_dir_path(), 而它用"当前时刻"算日期,
  于是 8 小时录制散成了 8 个目录:
      data/2026-10-26/2026-10-26_0100_7734200/
      data/2026-10-26/2026-10-26_0200_7734200/
      ...
  每个目录只有 1 个弹幕分片, 服务端会把它们当成 8 个独立会话,
  总览里是 8 条碎轨道, 没法当一晚正常使用。

本脚本做的事:
  1. 找出同一 (赛事日, 房间) 下的多个碎片目录
  2. 按数据时间排序, 校验它们首尾相接(有间隔就警告, 不中断)
  3. 合并到 data/{赛事日}/{room}/, 弹幕分片保持原名(不重命名, 避免破坏引用)
  4. afp 多份时按段号重新编号后拼接(段号必须全局唯一)
  5. 写一份新的 manifest.json(列出全部真实存在的文件)
  6. 原碎片目录删除(用 --apply 才执行)

赛事日 = 全部碎片**最晚结束时刻**所在日期。
"""
import argparse
import gzip
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 本脚本住 tools/, 目标数据在**仓库根**的 data/ —— 从仓库根推, 不依赖 cwd
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
V2_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(\d+)$")          # v2 正式
V2T_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_test_(\d{6})_(\d+)$")     # v2 测试


def _open_text(p: Path):
    if p.name.endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8", errors="replace")
    return open(p, "rt", encoding="utf-8", errors="replace")


def dm_files(d: Path):
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        n = p.name
        if n.endswith(".meta.json") or ".afp.jsonl" in n or n == "manifest.json":
            continue
        if n.endswith(".jsonl.gz") or n.endswith(".jsonl"):
            out.append(p)
    return out


def afp_files(d: Path):
    return [p for p in sorted(d.iterdir()) if p.is_file() and ".afp.jsonl" in p.name]


def scan_range(files):
    lo = hi = None
    for p in files:
        try:
            head, tail = [], []
            with _open_text(p) as f:
                for i, line in enumerate(f):
                    if i > 100:
                        break
                    if line.strip():
                        head.append(line)
            with _open_text(p) as f:
                for line in f:
                    if line.strip():
                        tail.append(line)
                        if len(tail) > 100:
                            tail.pop(0)
        except Exception:
            continue
        for line in head + tail:
            try:
                t = int(json.loads(line).get("ts") or 0)
            except Exception:
                continue
            if t:
                lo = t if lo is None else min(lo, t)
                hi = t if hi is None else max(hi, t)
    return lo, hi


def count_dm(files):
    n = 0
    for p in files:
        try:
            with _open_text(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("ty") == "dm":
                            n += 1
                    except Exception:
                        pass
        except Exception:
            pass
    return n


def read_meta(d: Path):
    for p in sorted(d.glob("*.meta.json")):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def collect_fragments():
    """找出需要合并的碎片组: {(赛事日, room): [(dir, t0, t1, n_dm), ...]}"""
    groups = {}
    orphans = []      # 无法判断房间号的
    for top in sorted(DATA_DIR.iterdir()):
        if not top.is_dir() or not DATE_RE.fullmatch(top.name):
            continue
        for d in sorted(top.iterdir()):
            if not d.is_dir():
                continue
            m = V2_RE.fullmatch(d.name)
            if not m:
                continue
            room = m.group(3)
            files = dm_files(d)
            afps = afp_files(d)
            if not files and not afps:
                orphans.append(d)
                continue
            t0, t1 = scan_range(files)
            n = count_dm(files) if files else 0
            groups.setdefault(room, []).append({
                "dir": d, "t0": t0, "t1": t1, "n_dm": n,
                "files": files, "afps": afps,
            })
    return groups, orphans


def pick_match_date(items):
    """赛事日 = 全部碎片最晚结束时刻所在日期。"""
    ends = [x["t1"] for x in items if x["t1"]]
    if not ends:
        return None
    return datetime.fromtimestamp(max(ends) / 1000).strftime("%Y-%m-%d")


def to_v3(d: Path) -> str:
    return d.name


def merge_group(room, items, apply):
    items = sorted(items, key=lambda x: (x["t0"] or 0))
    day = pick_match_date(items)
    if not day:
        print("  [跳过] 房间 %s: 所有碎片都读不到时间" % room)
        return None
    dst = DATA_DIR / day / room
    print("  房间 %s -> 赛事日 %s, 合并 %d 个碎片到 %s" % (
        room, day, len(items), dst.relative_to(DATA_DIR)))
    # 时间连续性检查
    prev_end = None
    gaps = []
    for it in items:
        if prev_end is not None and it["t0"]:
            gap = (it["t0"] - prev_end) / 1000
            if gap > 120:
                gaps.append((prev_end, it["t0"], gap))
        prev_end = max(prev_end or 0, it["t1"] or 0)
    if gaps:
        print("    警告: 有 %d 处时间空档(>2分钟)" % len(gaps))
        for a, b, g in gaps[:3]:
            print("      %s -> %s 空 %.0f 分" % (
                datetime.fromtimestamp(a / 1000).strftime("%H:%M"),
                datetime.fromtimestamp(b / 1000).strftime("%H:%M"), g / 60))
    # 目标已存在时合并进去(而不是报错)
    existing = []
    if dst.is_dir():
        existing = [p for p in dst.iterdir() if p.is_file()]
        if existing:
            print("    目标目录已存在(%d 文件), 将并进去" % len(existing))
    plan = {
        "dst": dst, "day": day, "room": room, "items": items,
        "existing": existing,
    }
    return plan


def execute(plan, apply):
    dst = plan["dst"]
    day, room = plan["day"], plan["room"]
    items = plan["items"]

    # 统计总量
    total_dm = sum(it["n_dm"] for it in items)
    starts = [it["t0"] for it in items if it["t0"]]
    ends = [it["t1"] for it in items if it["t1"]]
    start_ms = min(starts) if starts else None
    end_ms = max(ends) if ends else None

    if not apply:
        print("    预演: 会合并 %d 条弹幕, %s ~ %s" % (
            total_dm,
            datetime.fromtimestamp(start_ms / 1000).strftime("%m-%d %H:%M") if start_ms else "-",
            datetime.fromtimestamp(end_ms / 1000).strftime("%m-%d %H:%M") if end_ms else "-"))
        return True

    dst.mkdir(parents=True, exist_ok=True)
    moved = []
    # ---- 1. 搬弹幕分片(保持原名) ----
    for it in items:
        for p in it["files"]:
            tgt = dst / p.name
            if tgt.exists():
                # 同名冲突: 加序号
                stem = p.name
                i = 2
                while tgt.exists():
                    if stem.endswith(".jsonl.gz"):
                        tgt = dst / ("%s_r%d.jsonl.gz" % (stem[:-len(".jsonl.gz")], i))
                    elif stem.endswith(".jsonl"):
                        tgt = dst / ("%s_r%d.jsonl" % (stem[:-len(".jsonl")], i))
                    i += 1
            p.replace(tgt)
            moved.append(tgt.name)
    # ---- 2. 合并 afp(段号重编) ----
    afp_list = []
    for it in items:
        afp_list.extend(it["afps"])
    afp_name = None
    if afp_list:
        afp_name = merge_afp(afp_list, dst, room)
    # ---- 3. meta: 取最完整的那份改名 ----
    meta_name = None
    metas = []
    for it in items:
        metas.extend(sorted(it["dir"].glob("*.meta.json")))
    if metas:
        best = max(metas, key=lambda p: p.stat().st_size)
        try:
            m = json.loads(best.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            m = {}
        # 覆写为合并后的真实统计
        m.update({
            "room": int(room),
            "dm_count": total_dm,
            "record_start": datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if start_ms else None,
            "record_end": datetime.fromtimestamp(end_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if end_ms else None,
            "merged_from": [it["dir"].name for it in items],
        })
        meta_name = "%s.meta.json" % room
        (dst / meta_name).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    # ---- 4. manifest ----
    manifest = {
        "schema_version": 1,
        "room": int(room),
        "match_date": day,
        "is_test": False,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "dm_count": total_dm,
        "sc_count": 0,
        "files": sorted(moved),
        "afp": ({"file": afp_name, "hop": 0.2, "win": 0.4} if afp_name else None),
        "meta": meta_name,
        "collector_version": "merged",
        "ended_by": "latest_end",
        "merged_from": [it["dir"].name for it in items],
    }
    tmp = dst / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dst / "manifest.json")
    # ---- 5. 删空的原目录 ----
    for it in items:
        d = it["dir"]
        if d == dst:
            continue
        leftover = [p for p in d.iterdir()]
        if leftover:
            print("    警告: %s 还有 %d 个文件未搬, 保留目录" % (d.name, len(leftover)))
        else:
            d.rmdir()
    print("    完成: %d 条弹幕, %d 个分片, afp=%s" % (total_dm, len(moved), afp_name))
    return True


def merge_afp(afps, dst, room):
    """把多份 afp 拼成一份, 段号全局重编。返回目标文件名。"""
    # 若有 .gz 和明文同源, 优先明文(内容更全/正在写)
    plains = [p for p in afps if p.name.endswith(".jsonl")]
    gzs = [p for p in afps if p.name.endswith(".jsonl.gz")]
    use = plains if plains else gzs
    if len(use) == 1 and use[0].name.endswith(".jsonl"):
        tgt = dst / ("%s.afp.jsonl" % room)
        if use[0].parent != dst:
            use[0].replace(tgt)
            return tgt.name
        return use[0].name
    conn_idx = 0
    out_lines = []
    wrote_hdr = False
    for p in sorted(use, key=lambda x: x.name):
        try:
            with _open_text(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    ty = o.get("ty")
                    if ty == "hdr":
                        if not wrote_hdr:
                            out_lines.append(line)
                            wrote_hdr = True
                    elif ty == "end":
                        continue
                    elif ty == "conn":
                        o["c"] = conn_idx
                        conn_idx += 1
                        out_lines.append(json.dumps(o, ensure_ascii=False))
                    elif ty == "fp":
                        o["c"] = conn_idx - 1
                        out_lines.append(json.dumps(o, ensure_ascii=False))
        except Exception as e:
            print("    警告: 读 afp %s 失败: %s" % (p.name, e))
    tgt = dst / ("%s.afp.jsonl" % room)
    tgt.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return tgt.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行(默认预演)")
    ap.add_argument("--room", default=None, help="只处理指定房间号")
    args = ap.parse_args()

    if not DATA_DIR.is_dir():
        print("没有 data 目录")
        return 1
    groups, orphans = collect_fragments()
    print("=== 发现 %d 个房间有碎片目录 ===" % len(groups))
    for room, items in sorted(groups.items()):
        if args.room and room != args.room:
            continue
        print()
        print("[房间 %s] %d 个碎片" % (room, len(items)))
        for it in sorted(items, key=lambda x: (x["t0"] or 0)):
            print("    %-32s %7d 条  %s ~ %s" % (
                it["dir"].name, it["n_dm"],
                datetime.fromtimestamp(it["t0"] / 1000).strftime("%m-%d %H:%M") if it["t0"] else "-",
                datetime.fromtimestamp(it["t1"] / 1000).strftime("%m-%d %H:%M") if it["t1"] else "-"))
        plan = merge_group(room, items, args.apply)
        if plan:
            execute(plan, args.apply)
    if orphans:
        print()
        print("=== 空目录(无弹幕也无 afp) %d 个 ===" % len(orphans))
        for d in orphans:
            print("    %s" % d.relative_to(DATA_DIR))
    print()
    if not args.apply:
        print("（预演模式，未改动；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
