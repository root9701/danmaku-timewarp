"""datablocks.py — 采集控制台的“小时数据块”视图。

只读: 扫 data/{日期}/{房间}/ 下的切片, 再结合 git status / HEAD / origin/main
判断每一块处于什么状态。控制台只负责展示, 这里不写任何文件。

状态口径:
  采集中  当前正在写的 raw 片
  待提交  已封口, 但还没进本地 git
  待推送  已进本地 git, 但 origin/main 还没有(或内容不一致)
  推送失败 同上, 且最近一次推送失败
  已推送  origin/main 上的 blob 与本地文件一致

为什么按 blob 比而不是“文件在 origin 上就算推了”:
  manifest/切片会在同一路径被重写; 只看路径会把“旧版本在远端”误判成已推送。
"""
import gzip
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

SLICE_RE = re.compile(
    r"^(?P<base>.+)_s(?P<hh>\d{2})(?P<mm>\d{2})\.jsonl(?P<gz>\.gz)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNT_CACHE: dict = {}


def parse_slice(name: str):
    """切片名 -> {start_min, sealed}; 不是弹幕切片返回 None。"""
    m = SLICE_RE.match(name or "")
    if not m:
        return None
    return {"start_min": int(m.group("hh")) * 60 + int(m.group("mm")),
            "sealed": bool(m.group("gz"))}


def fmt_min(minutes: int) -> str:
    """一天内的分钟数 -> HH:MM(跨零点按 24 小时取模)。"""
    m = int(minutes) % (24 * 60)
    return "%02d:%02d" % (m // 60, m % 60)


def count_records(path: Path) -> tuple:
    """数弹幕与 SC(一行一条)。按 mtime+size 缓存, 避免每次轮询重读大文件。"""
    try:
        st = path.stat()
    except OSError:
        return 0, 0
    key = str(path)
    hit = _COUNT_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], hit[3]
    dm = sc = 0
    try:
        opener = (gzip.open(path, "rt", encoding="utf-8", errors="replace")
                  if path.suffix == ".gz"
                  else open(path, "r", encoding="utf-8", errors="replace"))
        with opener as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("ty") == "sc":
                    sc += 1
                else:
                    dm += 1
    except (OSError, EOFError):
        return 0, 0
    _COUNT_CACHE[key] = (st.st_mtime_ns, st.st_size, dm, sc)
    return dm, sc


def _git(root: Path, *args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _tree(root: Path, rev: str, rel_dir: str) -> dict:
    """git ls-tree -r <rev> -- <dir> -> {相对路径: blob sha}。"""
    out = _git(root, "ls-tree", "-r", rev, "--", rel_dir)
    res = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and path:
            res[path.replace("\\", "/")] = parts[2]
    return res


def _dirty(root: Path, rel_dir: str) -> set:
    """git status --porcelain -- <dir> -> 相对路径集合。"""
    out = _git(root, "status", "--porcelain", "--", rel_dir)
    res = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip().strip('"').replace("\\", "/")
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        res.add(p)
    return res


def _hash(root: Path, path: Path) -> str:
    return _git(root, "hash-object", "--", str(path)).strip()


def _last_commit(root: Path, rev: str, rel: str) -> str:
    return _git(root, "log", "-1", "--format=%h %ci", rev, "--", rel).strip()


def _block_state(rel: str, active: bool, dirty: set, head: dict,
                 origin: dict, root: Path, path: Path, push_failed: bool) -> str:
    if active:
        return "采集中"
    if rel in dirty:
        return "待提交"
    if rel in head:
        if origin.get(rel) == _hash(root, path):
            return "已推送"
        return "推送失败" if push_failed else "待推送"
    return "待提交"


def _room_blocks(root: Path, date: str, room: str, now: datetime,
                 head: dict, origin: dict, dirty: set, push_failed: bool,
                 running: bool) -> list:
    day_dir = root / "data" / date / room
    if not day_dir.is_dir():
        return []
    items = []
    for p in day_dir.iterdir():
        if not p.is_file() or "test_" in p.name:
            continue
        info = parse_slice(p.name)
        if info:
            items.append((info["start_min"], info["sealed"], p))
    items.sort(key=lambda x: (x[0], x[2].name))
    out = []
    for i, (start, sealed, path) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else start + 60
        if end <= start:
            end = start + 60
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            mtime = now
        active = (running and not sealed and i == len(items) - 1
                  and (now - mtime).total_seconds() < 7200)
        rel = "data/%s/%s/%s" % (date, room, path.name)
        state = _block_state(rel, active, dirty, head, origin, root, path,
                             push_failed)
        dm, sc = count_records(path)
        out.append({
            "room": room,
            "name": path.name,
            "start": fmt_min(start),
            "end": fmt_min(end),
            "label": "%s–%s" % (fmt_min(start), fmt_min(end)),
            "count": dm,
            "sc_count": sc,
            "state": state,
            "commit": _last_commit(root, "origin/main", rel)
                      if state == "已推送" else "",
            "active": active,
            "mtime": mtime.strftime("%H:%M"),
        })
    return out


def scan(root, date: str, rooms=None, now=None, push_failed: bool = False,
         running: bool = False) -> list:
    """扫某天的全部房间, 返回小时块列表(按房间、时间排序)。"""
    root = Path(root)
    now = now or datetime.now()
    if not DATE_RE.fullmatch(str(date or "")):
        return []
    rel_dir = "data/%s" % date
    head = _tree(root, "HEAD", rel_dir)
    origin = _tree(root, "origin/main", rel_dir)
    dirty = _dirty(root, rel_dir)
    if rooms is None:
        day = root / "data" / date
        rooms = sorted(p.name for p in day.iterdir()
                       if p.is_dir() and p.name.isdigit()) if day.is_dir() else []
    out = []
    for room in rooms:
        out.extend(_room_blocks(root, date, str(room), now, head, origin,
                                dirty, push_failed, running))
    return out
