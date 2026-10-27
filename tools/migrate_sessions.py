#!/usr/bin/env python
"""把旧平铺布局迁移成"每会话一个子目录"的新布局。

旧: data/{日期}/{room}_sHHMM.jsonl.gz + {room}.afp.jsonl + {room}.meta.json
新: data/{日期}/{日期}_{HHMM}_{room}/  内含该会话的全部切片/afp/meta

安全原则(重要):
  * 默认**只预演不落盘**, 加 --apply 才真的移动。
  * 先移动 meta(它是会话边界的唯一可靠依据), 再移 afp, 最后移切片。
  * 同目录若有多个会话(如 10-25 里的晚间主会话 + day0719), 按 meta 的
    record_start 分桶; 没有 meta 归属的碎片(孤儿 afp)单独归到 orphan 目录, 不删。
  * 任何一步失败都记录并继续, 最后打印汇总, 不静默吞掉。
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 本脚本住 tools/, 目标数据在采集端目录下 —— 从仓库根推, 不依赖 cwd
ROOT = Path(__file__).resolve().parent.parent / "danmaku-collector"
DATA_DIR = ROOT / "data"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLICE_RE = re.compile(r"_s\d{4}$")
RUN_RE = re.compile(r"_run\d+$")
TEST_FILE_RE = re.compile(r"^test_(\d{6})_(\d+)$")
AFTER_TAGS = (".meta.json",)


def base_name(filename: str) -> str:
    """文件主名: 剥掉 .jsonl[.gz] / .afp.jsonl[.gz] / _sHHMM / _runN / .meta.json"""
    n = filename
    for suf in (".jsonl.gz", ".jsonl", ".meta.json"):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    if n.endswith(".afp"):
        n = n[: -len(".afp")]
    n = RUN_RE.sub("", n)
    n = SLICE_RE.sub("", n)
    return n


def parse_start(meta_path: Path):
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, None
    rs = str(m.get("record_start") or "")
    try:
        start = datetime.strptime(rs, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, m, None
    return start, m, m.get("room")


def session_day(start: datetime) -> str:
    """与采集端 _session_dirname 一致: 00:00~11:00 归前一天(比赛日)。"""
    if 0 <= start.hour < 11:
        return (start - timedelta(days=1)).strftime("%Y-%m-%d")
    return start.strftime("%Y-%m-%d")


def earliest_ts(path: Path):
    """读文件里最早的有效 ts(毫秒)。只读前若干行, 采集按时间顺序写, 够用。"""
    import gzip
    ts = None
    try:
        opener = (gzip.open(path, "rt", encoding="utf-8", errors="replace")
                  if path.name.endswith(".gz")
                  else open(path, "rt", encoding="utf-8", errors="replace"))
        with opener as f:
            for i, line in enumerate(f):
                if i > 500:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("ts")
                if isinstance(t, (int, float)) and t > 0:
                    ts = int(t) if ts is None else min(ts, int(t))
    except (OSError, EOFError, Exception):
        return None
    return ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的移动文件(默认只预演)")
    ap.add_argument("--date", help="只处理某一天, 如 2026-10-25")
    args = ap.parse_args()

    if not DATA_DIR.is_dir():
        print("没有 data 目录: %s" % DATA_DIR)
        return 1

    plan = []       # [(src, dst_dir, 说明)]
    problems = []

    for day_dir in sorted(DATA_DIR.iterdir()):
        if not (day_dir.is_dir() and DATE_RE.fullmatch(day_dir.name)):
            continue
        if args.date and day_dir.name != args.date:
            continue

        files = [p for p in day_dir.iterdir() if p.is_file() and not p.name.endswith(".afp.jsonl") \
                 and not p.name.endswith(".afp.jsonl.gz")] or \
                [p for p in day_dir.iterdir() if p.is_file()]
        all_files = [p for p in day_dir.iterdir() if p.is_file()]
        if not all_files:
            print("[%s] 空目录, 跳过" % day_dir.name)
            continue

        # 按"会话主名"分组: base_name 相同即同一次会话的所有产物
        # (切片 _sHHMM / 归档 _runN / afp / meta 都归到同一主名)
        groups = {}         # base -> [files]
        for p in all_files:
            if p.name.endswith(".afp.jsonl") or p.name.endswith(".afp.jsonl.gz"):
                b = p.name
                for suf in (".afp.jsonl.gz", ".afp.jsonl"):
                    if b.endswith(suf):
                        b = b[: -len(suf)]
                        break
                b = SLICE_RE.sub("", RUN_RE.sub("", b))
            elif p.name.endswith(".meta.json"):
                b = p.name[: -len(".meta.json")]
                b = SLICE_RE.sub("", RUN_RE.sub("", b))
            else:
                b = base_name(p.name)
            groups.setdefault(b, []).append(p)

        print("[%s] %d 个文件, %d 个会话组" % (day_dir.name, len(all_files), len(groups)))

        for b, gfiles in sorted(groups.items()):
            # 真正的开始时间: 优先用数据里最早的 ts(最可靠), 退而用 meta.record_start
            tss = [earliest_ts(p) for p in gfiles
                   if not p.name.endswith(".meta.json")]
            tss = [t for t in tss if t]
            # 房间号: 从 meta 里取最可靠(主名尾部数字在 _day0719 这类后缀下会取错)
            room = 0
            mp0 = next((p for p in gfiles if p.name.endswith(".meta.json")), None)
            if mp0 is not None:
                _st, mm0, mroom0 = parse_start(mp0)
                if isinstance(mroom0, int) and mroom0 > 0:
                    room = mroom0
            if not room:
                mm = re.search(r"(?:^|_)(\d{6,})$", b)   # 房间号通常是 6 位以上
                if mm:
                    room = int(mm.group(1))
            if tss:
                start = datetime.fromtimestamp(min(tss) / 1000)
                src_desc = "数据最早 ts"
            else:
                mp = next((p for p in gfiles if p.name.endswith(".meta.json")), None)
                st, _m, mroom = parse_start(mp) if mp else (None, None, None)
                if st is None:
                    problems.append("%s 组无任何可用时间, 归 orphan" % b)
                    for p in gfiles:
                        plan.append((p, day_dir / "_orphan", "无法判定时间"))
                    continue
                start = st
                room = room or (mroom or 0)
                src_desc = "meta.record_start"
            sday = session_day(start)
            dirname = "%s_%s_%d" % (sday, start.strftime("%H%M"), room)
            dst = day_dir / dirname
            print("   组 %-34s -> %-28s (%s, %d 个文件)"
                  % (b, dirname, src_desc, len(gfiles)))
            for p in gfiles:
                plan.append((p, dst, dirname))

    print()
    print("=== 计划: %d 个文件待移动 ===" % len(plan))
    for src, dst, note in plan:
        print("  %-46s -> %-30s  %s" % (src.name, dst.relative_to(DATA_DIR), note))

    if problems:
        print()
        print("=== 注意 ===")
        for x in problems:
            print("  ! " + x)

    if not args.apply:
        print()
        print("(预演模式, 没有移动任何文件。确认无误后加 --apply 执行)")
        return 0

    print()
    print("=== 开始移动 ===")
    moved = failed = 0
    for src, dst, note in plan:
        try:
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / src.name
            if target.exists():
                print("  跳过(目标已存在): %s -> %s" % (src.name, dst.name))
                continue
            shutil.move(str(src), str(target))
            print("  移动 %-46s -> %s/" % (src.name, dst.name))
            moved += 1
        except OSError as e:
            print("  失败 %s: %s" % (src.name, e))
            failed += 1

    # 清理空掉的平铺目录(只删空目录, 不碰子目录)
    for day_dir in sorted(DATA_DIR.iterdir()):
        if not (day_dir.is_dir() and DATE_RE.fullmatch(day_dir.name)):
            continue
        leftover = [p for p in day_dir.iterdir() if p.is_file()]
        if not leftover:
            print("  %s 平铺层已空(子目录保留)" % day_dir.name)

    print()
    print("完成: 移动 %d, 失败 %d" % (moved, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
