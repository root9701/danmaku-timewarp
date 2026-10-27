#!/usr/bin/env python
"""把数据目录迁移到 v3 结构:

    data/{赛事日}/{room}/manifest.json + 切片 + afp     正式会话
    data/_test/{日期}T{HHMM}_{room}/                    测试会话

赛事日 = 会话**结束时刻**所在日期。示例: 25 号 23:33 开录、26 号 08:59 结束的正赛
-> data/2026-10-26/7734200/ (旧结构里它躺在 2026-10-25/2026-10-25_2332_7734200/)。

同时为没有 manifest 的老目录补写 manifest.json(采集器新版本会自动写, 老数据没有)。

安全: 默认只预演, 加 --apply 才真的移动。移动前先校验目标不存在。
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
V3T_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{4})_(\d+)$")          # v3 测试
SLICE_RE = re.compile(r"_s\d{4}$")
RUN_RE = re.compile(r"_run\d+$")


def _open_text(p: Path):
    if p.name.endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8", errors="replace")
    return open(p, "rt", encoding="utf-8", errors="replace")


def dm_files(d: Path):
    """目录内的弹幕数据片(排除 afp/meta)。"""
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
    return [p for p in sorted(d.iterdir())
            if p.is_file() and ".afp.jsonl" in p.name]


def scan_range(files):
    """扫数据片的首尾 ts, 返回 (start_ms, end_ms)。"""
    lo = hi = None
    for p in files:
        try:
            head, tail = [], []
            with _open_text(p) as f:
                for i, line in enumerate(f):
                    if i > 80:
                        break
                    if line.strip():
                        head.append(line)
            with _open_text(p) as f:
                for line in f:
                    if line.strip():
                        tail.append(line)
                        if len(tail) > 80:
                            tail.pop(0)
        except (OSError, EOFError, Exception):
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
        except (OSError, EOFError, Exception):
            pass
    return n


def read_meta(d: Path):
    for p in sorted(d.glob("*.meta.json")):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def read_manifest_file(d: Path):
    p = d / "manifest.json"
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def session_day(end_ms, fallback_date):
    """赛事日 = 结束时刻所在日期。"""
    if end_ms:
        return datetime.fromtimestamp(end_ms / 1000).strftime("%Y-%m-%d")
    return fallback_date


def _room_of(src: Path, files: list) -> str:
    """解析房间号。三种来源:
       v2/v3 会话目录名里带房间号  -> 从目录名取
       v1 平铺(目录名是日期)       -> 从文件名取 {room}_sHHMM / test_HHMMSS_{room}
    """
    for pat in (V2_RE, V3T_RE):
        m = pat.fullmatch(src.name)
        if m:
            return m.group(3)
    # 目录名是纯房间号(v3 正式)
    if src.name.isdigit():
        return src.name
    # 从文件名推断
    for p in files:
        n = p.name
        for suf in (".jsonl.gz", ".jsonl"):
            if n.endswith(suf):
                n = n[: -len(suf)]
                break
        m = re.match(r"^test_\d{6}_(\d+)(?:_s\d{4}|_run\d+)?$", n)
        if m:
            return m.group(1)
        m = re.match(r"^(\d+)(?:_s\d{4}|_run\d+)?$", n)
        if m:
            return m.group(1)
    mm = re.search(r"(\d+)$", src.name)
    return mm.group(1) if mm else "0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的移动文件(默认预演)")
    args = ap.parse_args()

    if not DATA_DIR.is_dir():
        print("没有 data 目录")
        return 1

    plan = []      # (src_dir, dst_dir, is_test, info)

    # ---- 收集所有候选会话目录 ----
    for top in sorted(DATA_DIR.iterdir()):
        if not top.is_dir():
            continue
        # v3 测试根
        if top.name == "_test":
            for d in sorted(top.iterdir()):
                if d.is_dir():
                    plan.append((d, d, True, "已是 v3 测试目录"))
            continue
        if not DATE_RE.fullmatch(top.name):
            continue
        # v1 平铺层: 日目录下直接是数据文件(没有会话子目录)。
        # 也要能直接迁到 v3 —— 否则得先跑 migrate_sessions.py 再跑本脚本, 两步容易出错。
        subdirs = [p for p in top.iterdir() if p.is_dir()]
        flat_files = [p for p in top.iterdir()
                      if p.is_file() and not p.name.endswith(".meta.json")
                      and ".jsonl" in p.name]
        if flat_files and not subdirs:
            is_test = "test_" in flat_files[0].name
            plan.append((top, None, is_test, "v1 平铺层"))
            continue
        for d in sorted(top.iterdir()):
            if not d.is_dir():
                continue
            is_test = "test_" in d.name
            m2 = V2_RE.fullmatch(d.name)
            m2t = V2T_RE.fullmatch(d.name)
            m3t = V3T_RE.fullmatch(d.name)
            if not (m2 or m2t or m3t):
                continue
            plan.append((d, None, is_test, "待计算"))

    # ---- 计算目标 ----
    final = []
    for (src, forced_dst, is_test, note) in plan:
        if forced_dst is not None:      # v3 测试目录原地
            mf = read_manifest_file(src)
            final.append((src, src, True, "v3 测试, 原地", mf, 0))
            continue
        files = dm_files(src)
        afps = afp_files(src)
        meta = read_meta(src)
        start_ms, end_ms = scan_range(files)
        n_dm = count_dm(files) if files else 0
        # 赛事日: 有数据用结束时刻, 否则退回 meta.record_end / 目录名里的日期
        if end_ms:
            day = session_day(end_ms, src.parent.name)
        elif meta and meta.get("record_end"):
            try:
                day = datetime.strptime(meta["record_end"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
            except ValueError:
                day = src.parent.name
        else:
            day = src.parent.name
        if is_test:
            # 测试会话: 用其开始时刻做区分名
            if start_ms:
                ts = datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%dT%H%M")
            else:
                m = V2T_RE.fullmatch(src.name)
                ts = ("%sT%s" % (m.group(1), m.group(2)[:4])) if m else src.name
            room = _room_of(src, files)
            dst = DATA_DIR / "_test" / ("%s_%s" % (ts, room))
        else:
            room = _room_of(src, files)
            dst = DATA_DIR / day / room
        final.append((src, dst, is_test, note, None, n_dm if files else 0))

    # ---- 冲突消解: 同一目标目录只能放一个会话 ----
    # v3 结构是"一个房间一天一个目录"; 但历史上同一天可能有多个会话(测试/白天场/正赛)。
    # 冲突时把**弹幕最多**的那个当作该日主会话占用 data/{日}/{room}/,
    # 其余(较少的)转为测试区 data/_test/{日期}T{HHMM}_{room}/, 避免把不同会话混进同一目录。
    from collections import defaultdict
    groups = defaultdict(list)
    for item in final:
        groups[str(item[1])].append(item)
    resolved = []
    for dst_key, items in groups.items():
        if len(items) == 1:
            resolved.append(items[0])
            continue
        items_sorted = sorted(items, key=lambda x: -x[5])   # 弹幕多的优先保留正式位置
        keep = items_sorted[0]
        resolved.append(keep)
        print("  [冲突] %d 个会话都想进 %s; 保留弹幕最多的 %s(dm=%d), 其余转 _test/" % (
            len(items), dst_key, keep[0].name, keep[5]))
        for it in items_sorted[1:]:
            src, _dst, is_test, note, _mf, n_dm = it
            files = dm_files(src)
            start_ms, _end = scan_range(files)
            ts = (datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%dT%H%M")
                  if start_ms else src.name)
            mm = re.search(r"(\d+)$", src.name)
            room = mm.group(1) if mm else "0"
            alt = DATA_DIR / "_test" / ("%s_%s" % (ts, room))
            resolved.append((src, alt, True, "冲突转测试区", None, n_dm))
            print("        %s(dm=%d) -> %s" % (src.name, n_dm, alt.relative_to(DATA_DIR)))
    final = resolved

    # ---- 报告 / 执行 ----
    print("=== 迁移计划 ===")
    moves = 0
    for (src, dst, is_test, note, _mf, _ndm) in final:
        rel_src = src.relative_to(DATA_DIR)
        rel_dst = dst.relative_to(DATA_DIR)
        same = (src.resolve() == dst.resolve())
        files = dm_files(src)
        n_dm = count_dm(files) if files else 0
        start_ms, end_ms = scan_range(files)
        span = ""
        if start_ms and end_ms:
            a = datetime.fromtimestamp(start_ms / 1000)
            b = datetime.fromtimestamp(end_ms / 1000)
            span = "%s ~ %s" % (a.strftime("%m-%d %H:%M"), b.strftime("%m-%d %H:%M"))
        tag = "(原地)" if same else ""
        print("  %-42s -> %-28s %s  %s  dm=%d  %s" % (
            str(rel_src), str(rel_dst), tag, "测试" if is_test else "正式", n_dm, span))
        if not same:
            moves += 1

    if not args.apply:
        print()
        print("共 %d 个目录需要移动。(预演模式, 未改动; 加 --apply 执行)" % moves)
        return 0

    print()
    print("=== 开始迁移 ===")
    ok = fail = 0
    for (src, dst, is_test, note, _mf, _ndm) in final:
        try:
            if src.resolve() == dst.resolve():
                _write_manifest(dst, is_test)
                continue
            if dst.exists():
                print("  跳过(目标已存在): %s -> %s" % (src, dst))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            _write_manifest(dst, is_test)
            print("  移动 %s -> %s" % (src.relative_to(DATA_DIR), dst.relative_to(DATA_DIR)))
            ok += 1
        except OSError as e:
            print("  失败 %s: %s" % (src, e))
            fail += 1

    # 清理空掉的旧日期目录(只删空目录)
    for top in sorted(DATA_DIR.iterdir()):
        if top.is_dir() and DATE_RE.fullmatch(top.name):
            try:
                if not any(top.iterdir()):
                    top.rmdir()
                    print("  删除空目录 %s" % top.name)
            except OSError:
                pass

    print()
    print("完成: 移动 %d, 失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


def _write_manifest(d: Path, is_test: bool):
    """给没有 manifest 的目录补写一份(采集器新版本会自动写)。"""
    mp = d / "manifest.json"
    if mp.is_file():
        return
    files = dm_files(d)
    afps = afp_files(d)
    meta = read_meta(d)
    start_ms, end_ms = scan_range(files)
    n_dm = count_dm(files) if files else 0
    room = None
    for pat in (V2_RE, V3T_RE):
        m = pat.fullmatch(d.name)
        if m:
            room = int(m.group(3))
            break
    if room is None:
        mm = re.search(r"(\d+)$", d.name)
        room = int(mm.group(1)) if mm else 0
    day = session_day(end_ms, d.parent.name)
    mf = {
        "schema_version": 1,
        "room": room,
        "match_date": day if not is_test else d.parent.name,
        "is_test": bool(is_test),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "dm_count": n_dm,
        "sc_count": (meta or {}).get("sc_count", 0),
        "files": [p.name for p in files],
        "afp": ({"file": afps[0].name, "hop": 0.2, "win": 0.4} if afps else None),
        "meta": (sorted(d.glob("*.meta.json"))[0].name
                 if list(d.glob("*.meta.json")) else None),
        "collector_version": "migrated",
        "ended_by": (meta or {}).get("ended_by"),
    }
    try:
        tmp = mp.with_name("manifest.json.tmp")
        tmp.write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(mp)
        print("      补写 manifest: %s" % d.name)
    except OSError as e:
        print("      manifest 写入失败 %s: %s" % (d.name, e))


if __name__ == "__main__":
    sys.exit(main())

