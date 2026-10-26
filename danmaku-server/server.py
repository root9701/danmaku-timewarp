"""弹幕延时直播 · 本地回放服务器 (danmaku-server/server.py)

托管前端静态页 web/; 弹幕回放 API: /api/days /api/danmaku /api/match(GET/POST);
B站视频对接: /api/playurl(取 mp4 直链) /api/stream(直链 403 时的反代兜底)。

数据源(只读): ../danmaku-collector/data/{YYYY-MM-DD}/
  正式 {room}.jsonl.gz(旧格式) / {room}_s{HHMM}.jsonl[.gz](小时滚动切片, 活动 raw 未压缩)
  + {room}_runN.jsonl.gz(同日旧 session 归档) + {room}.meta.json(finalize 总账, 含 parts);
  测试 test_HHMMSS_{room}[_s{HHMM}].jsonl[.gz] + 同基础名 .meta.json。
  按房间主名聚合读取(边采边看), 显式带 _sHHMM/_runN 后缀只读该文件。
  弹幕行: {"ts":毫秒, "un":用户名, "t":文本, "ty":"dm"|"sc"(sc 另有 "price")}

运行: python server.py  (依赖 aiohttp>=3.9)。
配置: 环境变量 DANMAKU_DATA 覆盖数据目录; cookies.txt(可选)提供B站 Cookie,
      支持 Netscape 格式或每行 "k=v" 格式。
"""

import asyncio
import gzip
import zlib
import html
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile                     # npz 缓存是 zip 容器, 损坏时抛 BadZipFile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import web

# 音频指纹自动对齐依赖(缺库不致命, /api/autocalign 会返回明确错误)
try:
    import numpy as np          # noqa: F401
    import audiofp              # 本目录 audiofp.py
    HAS_AUDIOFP = True
    _AFP_IMPORT_ERR = ""
except ImportError as e:        # numpy/audiofp 任一缺失
    HAS_AUDIOFP = False
    _AFP_IMPORT_ERR = str(e)
try:
    import imageio_ffmpeg       # /api/autocalign 用它自带的可执行 ffmpeg
except ImportError:
    imageio_ffmpeg = None       # type: ignore

# ---- 常量 ----
# 8788 已被本机常驻的 node 服务占用(实测 bind 报 10048), 换 8789
PORT = 8789
BASE_DIR = Path(__file__).resolve().parent                  # danmaku-server/
WEB_DIR = BASE_DIR / "web"                                  # 前端静态目录
MATCHES_PATH = BASE_DIR / "matches.json"                    # 弹幕-视频匹配配置
COOKIES_PATH = BASE_DIR / "cookies.txt"                     # 可选: B站 Cookie
_env = os.environ.get("DANMAKU_DATA", "").strip()
DATA_DIR = (Path(_env).resolve() if _env
            else BASE_DIR.parent / "danmaku-collector" / "data")

BILI_REFERER = "https://www.bilibili.com/"
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- 自动对齐(/api/autocalign) ----
# 回放音频指纹缓存目录(可用环境变量 AFP_CACHE 覆盖; 默认放在本机临时目录,
# 不写死绝对路径, 换机器也能跑, 也避免缓存随仓库/备份被带走)
AFP_CACHE_DIR = Path(os.environ.get("AFP_CACHE", "").strip()
                     or (Path(tempfile.gettempdir()) / "danmaku_afp_cache"))
AUTO_MIN_CONFIDENCE = 2.0   # 原型实测真对齐置信度 2.2-10.8, 低于 2 视为未验证
AUTO_MIN_CONN_FRAMES = 50   # 连接段指纹至少 50 帧(约 10s @hop0.2)才参与对齐
# 参与对齐的音频时长下限: 测过纯随机噪声在"片段长度接近回放长度"时可达 conf 2.5~5.5,
# 光靠置信度挡不住。真实比赛对齐通常能匹配几十秒以上, 故再要求匹配时长足够。
AUTO_MIN_MATCH_SEC = 20.0

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")        # 日期目录名(同时杜绝 ../ 越界)
FILE_RE = re.compile(r"^[0-9A-Za-z_-]+$")           # 弹幕文件名(不含扩展名)
TEST_FILE_RE = re.compile(r"^test_(\d{6})_(\d+)$")  # test_HHMMSS_房间号
BV_RE = re.compile(r"^BV[0-9A-Za-z]+$")             # B站 BV 号

MIME_MAP = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
            ".woff": "font/woff", ".woff2": "font/woff2"}


# ---- 通用工具 ----
class ApiError(Exception):
    """业务错误: 统一转为 {"ok":false,"error":"中文原因"} 响应。"""
    def __init__(self, msg: str):
        super().__init__(msg)
        self.msg = str(msg)


def api_ok(**kw) -> web.Response:
    kw["ok"] = True
    return web.json_response(kw, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def api_err(msg: str) -> web.Response:
    return web.json_response({"ok": False, "error": str(msg)},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


def require_date_file(request: web.Request) -> tuple[str, str]:
    """校验并返回 date/file 两个参数, 非法即抛 ApiError。"""
    date = (request.query.get("date") or "").strip()
    file = (request.query.get("file") or "").strip()
    if not date or not file:
        raise ApiError("缺少参数 date 或 file")
    if not DATE_RE.fullmatch(date):
        raise ApiError("参数 date 非法(应为 YYYY-MM-DD, 且不得包含路径字符)")
    if not FILE_RE.fullmatch(file):
        raise ApiError("参数 file 非法(只允许字母/数字/下划线/连字符)")
    return date, file


def load_cookie_header() -> str:
    """读取 cookies.txt(可选): 支持 Netscape 制表符格式或每行 k=v 格式。"""
    if not COOKIES_PATH.is_file():
        return ""
    try:
        text = COOKIES_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    pairs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):      # Netscape 的 HttpOnly 行
            line = line[len("#HttpOnly_"):]
        if line.startswith("#"):               # 注释
            continue
        if "\t" in line:   # Netscape: domain flag path secure expiry name value
            fields = line.split("\t")
            if len(fields) >= 7 and fields[5].strip():
                pairs.append(f"{fields[5].strip()}={fields[6].strip()}")
        elif "=" in line:  # 每行 k=v
            k, _, v = line.partition("=")
            if k.strip():
                pairs.append(f"{k.strip()}={v.strip()}")
    return "; ".join(pairs)


# matches.json 的读-改-写全程串行化。
# 原实现用**固定** tmp 名("matches.json.tmp")且无锁, 而写入点有三处
# (POST /api/match、/api/discover、/api/autocalign)。日常操作就能撞车:
# 「自动校准」成功后前端 applyOffsetValue 会紧接着 POST 一次 /api/match,
# 两个请求同时 read-modify-write → tmp 互相覆盖 → replace 出交错 JSON,
# 而 load_matches 对解析失败是**静默返回空表**, 表现为"全部对齐记录凭空消失"。
_MATCHES_LOCK = threading.Lock()


def load_matches() -> dict:
    """结构: {"align": {"date|file": {...}}, "lists": {"date": [{title,bv,offset_sec}]}}
    兼容旧格式(顶层平铺 align 条目)自动迁移。"""
    try:
        data = json.loads(MATCHES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"align": {}, "lists": {}}
    if not isinstance(data, dict):
        return {"align": {}, "lists": {}}
    if "align" in data or "lists" in data:
        return {"align": data.get("align") or {}, "lists": data.get("lists") or {}}
    return {"align": data, "lists": {}}  # 旧格式迁移


def save_matches(matches: dict) -> bool:
    """原子写入(唯一临时名 + 替换), 避免写一半损坏与并发互相踩踏。

    每次调用生成独立的 tmp 文件名: 即使两个线程同时写, 也不会共用同一个
    临时文件而把彼此内容写坏(旧的固定 ".tmp" 名正是丢数据的根因)。"""
    tmp = MATCHES_PATH.with_name("%s.%d.%d.tmp" % (
        MATCHES_PATH.name, os.getpid(), threading.get_ident()))
    try:
        tmp.write_text(json.dumps(matches, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(MATCHES_PATH)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def update_matches(mutator) -> bool:
    """在锁内做 read-modify-write。mutator(matches) 的返回值忽略。

    所有写 matches.json 的地方都应走这里, 而不是各自 load→改→save。"""
    with _MATCHES_LOCK:
        matches = load_matches()
        mutator(matches)
        return save_matches(matches)


def load_meta(date: str, file: str) -> dict:
    """读会话 meta。先找新布局的会话子目录, 再回落旧平铺布局。"""
    meta_path = _meta_path_for(date, file)
    if meta_path is None or not meta_path.is_file():
        raise ApiError(f"找不到 meta 文件: {date}/{file}.meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ApiError(f"meta 文件读取失败: {e}")
    if not isinstance(meta, dict):
        raise ApiError("meta 文件格式不正确")
    return meta


def _meta_path_for(date: str, file: str):
    """定位 meta 文件: 会话目录布局优先, 其次旧平铺布局。找不到返回 None。"""
    m = SESSION_DIR_RE.fullmatch(file)
    if m:
        d = DATA_DIR / date / file
        if not d.is_dir():
            return None
        # 同 danmaku_paths: 会话目录里的 meta 名不一定等于目录推导名
        # (测试会话是 test_HHMMSS_{room}.meta.json), 直接找目录内唯一的 meta。
        cands = sorted(d.glob("*.meta.json"))
        return cands[0] if cands else None
    for d in _scope_dirs(date):
        cand = d / f"{file}.meta.json"
        if cand.is_file():
            return cand
    return None


def compute_anchor_ms(meta: dict) -> int:
    """锚点: stream_events 第一个 live_start 的 ts_ms; 否则 record_start 换算毫秒;
    再否则 first_ts_ms; 兜底 0。"""
    for ev in meta.get("stream_events") or []:
        if isinstance(ev, dict) and ev.get("type") == "live_start":
            ts = ev.get("ts_ms")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                return int(ts)
    rs = str(meta.get("record_start") or "")
    if rs:
        try:
            return int(datetime.strptime(rs, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except ValueError:
            pass
    ts = meta.get("first_ts_ms")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return int(ts)
    return 0


def read_danmaku_text(path: Path) -> str:
    """(gzip/明文按后缀)流式按行读取, 拼接为完整文本(避免一次性驻留压缩+解压双份数据)。"""
    lines = []
    with open_text(path) as f:
        for line in f:
            lines.append(line)
    return "".join(lines)


def open_text(path: Path):
    """按后缀选 opener: .gz 走 gzip 流式解压, 其余明文(采集正在写的活动 raw)。"""
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


RUN_RE = re.compile(r"_run\d+$")
SLICE_RE = re.compile(r"_s\d{4}$")   # 滚动切片后缀 _sHHMM(如 7734200_s0030.jsonl)

# ---- 会话目录(新布局) ----
# 旧布局: data/{比赛日}/{room}_sHHMM.jsonl.gz 平铺, 一个目录里混着多个会话
#         (晚间场 + 白天场 + 孤儿 afp), 靠文件名猜归属, 已多次踩坑。
# 新布局: data/{比赛日}/{session_id}/ 每个会话一个子目录, 内含它自己的切片/afp/meta。
#   session_id = "{比赛日}_{HHMM}_{room}"  (HHMM = 该会话 record_start 的时分)
# 读取层必须**同时**支持两种布局(旧数据不迁移也能读), 见 _session_dirs/_resolve_day_scope。
SESSION_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(\d+)(?:$|_test_(\d{6}))$")


def _is_session_dir(p: Path) -> bool:
    return p.is_dir() and bool(SESSION_DIR_RE.fullmatch(p.name))


def _day_dirs(date: str) -> list:
    """该日期下所有"会话目录"(新布局)。无则返回空列表。"""
    day_dir = DATA_DIR / date
    if not day_dir.is_dir():
        return []
    try:
        return sorted([p for p in day_dir.iterdir() if _is_session_dir(p)], key=lambda p: p.name)
    except OSError:
        return []


def _day_flat_has_files(date: str) -> bool:
    """该日目录的平铺层(旧布局)是否还有弹幕/afp 文件。"""
    day_dir = DATA_DIR / date
    if not day_dir.is_dir():
        return False
    for pat in ("*.jsonl", "*.jsonl.gz"):
        if any(day_dir.glob(pat)):
            return True
    return False


def _scope_dirs(date: str) -> list:
    """返回"该日期要从哪些目录读数据": 新布局的会话子目录 + (若有旧数据)日目录本身。
    顺序: 旧平铺层在前(历史数据), 会话目录按名字(含 HHMM)升序。"""
    dirs = []
    if _day_flat_has_files(date):
        dirs.append(DATA_DIR / date)
    dirs.extend(_day_dirs(date))
    return dirs



def danmaku_paths(date: str, file: str) -> list[Path]:
    """主名 file(纯房间号或 test_HHMMSS_{room}, 或会话目录名) 归并全部同前缀产物:
      {file}.jsonl.gz(旧格式) + {file}_runN.jsonl.gz + {file}_s????.jsonl.gz(滚动切片)
      + {file}_s????.jsonl(活动 raw 未压缩, 采集进行中, 排最后);
    file 显式带 _runN/_sHHMM 后缀时只读那一个文件(gz 优先, 活动 raw 兜底)。
    glob 后用正则 fullmatch 严格匹配主名边界, 防前缀撞号(7734200 误吞 77342000 的片)。

    **两种布局都支持**:
      * 新: file 形如 "2026-10-25_2332_7734200"(会话目录名) -> 只读该子目录;
      * 旧: file 是纯房间号 -> 在日目录平铺层(旧数据) + 各会话子目录里按主名找。"""
    # 新布局: file 就是会话目录名。目录里放什么文件名取决于采集那次会话的命名
    # (正式会话是 {room}_sHHMM, 测试会话是 test_HHMMSS_{room}_sHHMM, 历史迁移的还可能是
    #  {room}_day0719 之类), 所以**不能**用目录名推导基础名去匹配 —— 那样绝大多数会话
    # 都会匹配到 0 个文件(实测 4 个会话里 3 个读不出数据)。正确做法: 收集目录内所有
    # 弹幕数据片(排除 afp/meta), 按名字排序, 活动 raw 排在最后。
    m = SESSION_DIR_RE.fullmatch(file)
    if m:
        sess = DATA_DIR / date / file
        if not sess.is_dir():
            return []
        return _collect_all_in_session(sess)

    if RUN_RE.search(file) or SLICE_RE.search(file):  # 用户明确指定某个归档段/切片
        for d in _scope_dirs(date):
            for suffix in (".jsonl.gz", ".jsonl"):
                p = d / f"{file}{suffix}"
                if p.is_file():
                    return [p]
        return []

    paths = []
    for d in _scope_dirs(date):
        paths.extend(_collect_paths_in_dir(d, file, file))
    # 去重 + 保持稳定顺序
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _base_of_session_dir(name: str) -> str:
    """会话目录名 -> 里面文件的基础名(正式会话=房间号; 测试会话=test_HHMMSS_房间号)。"""
    m = SESSION_DIR_RE.fullmatch(name)
    if not m:
        return name
    room, tstamp = m.group(3), m.group(4)
    return f"test_{tstamp}_{room}" if tstamp else room


def _collect_paths_in_dir(dirp: Path, base_name: str, file: str) -> list[Path]:
    """从一个目录里收集 base_name 这一会话的全部数据片(该函数只处理单目录)。"""
    if not dirp.is_dir():
        return []
    base = re.escape(base_name)
    paths = []
    main = dirp / f"{base_name}.jsonl.gz"
    if main.is_file():
        paths.append(main)
    for gz in sorted(dirp.glob(f"{base_name}_run*.jsonl.gz")):
        if re.fullmatch(base + r"(?:_s\d{4})?_run\d+", gz.name[: -len(".jsonl.gz")]):
            paths.append(gz)   # _runN 归档 + 极罕见的 _sHHMM_runN(切片 gz 被归档)
    for gz in sorted(dirp.glob(f"{base_name}_s*.jsonl.gz")):
        if re.fullmatch(base + r"_s\d{4}", gz.name[: -len(".jsonl.gz")]):
            paths.append(gz)
    for raw in sorted(dirp.glob(f"{base_name}_s*.jsonl")):   # 活动切片, 未压缩, 排最后
        if re.fullmatch(base + r"_s\d{4}", raw.name[: -len(".jsonl")]):
            paths.append(raw)
    return paths


def _collect_all_in_session(sess_dir: Path) -> list[Path]:
    """收集一个会话目录里的**全部**弹幕数据片(不看命名规则)。

    会话目录里只会放本次会话的产物, 所以这里不需要再按主名过滤 —— 反倒必须宽松,
    因为历史会话的文件名千奇百怪({room}_sHHMM / test_HHMMSS_{room}_sHHMM /
    {room}_day0719 ...)。用名字推导前缀会漏掉大部分会话(已实测)。

    排除: afp 指纹、meta 摘要(它们不是弹幕)。
    排序: 压缩片按名字(含 _sHHMM, 即时间顺序)在前, 未压缩的活动 raw 排最后
          (活动片可能正被采集端写入, 放最后避免影响正常片)。
    """
    if not sess_dir.is_dir():
        return []
    gz, raw = [], []
    for p in sorted(sess_dir.iterdir(), key=lambda x: x.name):
        if not p.is_file():
            continue
        n = p.name
        if n.endswith(".meta.json") or ".afp.jsonl" in n:
            continue
        if n.endswith(".jsonl.gz"):
            gz.append(p)
        elif n.endswith(".jsonl"):
            raw.append(p)
    return gz + raw


def read_danmaku_merged(paths: list[Path]) -> str:
    """多文件合并: 逐行解析 ts 排序, 保证墙钟单调(不同时段采集的弹幕在时间轴上归位)。
    活动 raw 末尾半行(采集正在写)是正常现象: ts 解析失败的行排最前, 不抛错。"""
    if len(paths) == 1:
        return read_danmaku_text(paths[0])
    rows = []
    for p in paths:
        with open_text(p) as f:
            for line in f:
                if line.strip():
                    # 活动 raw 末尾半行无换行, 补上避免 join 后与相邻行粘连(吞掉真实弹幕)
                    rows.append(line if line.endswith("\n") else line + "\n")
    def ts_of(line: str) -> int:
        try:
            v = json.loads(line).get("ts") or 0
            return int(v)
        except (ValueError, TypeError, AttributeError):
            return 0
    return "".join(sorted(rows, key=ts_of))


def first_ts_from_data(date: str, file: str, max_lines: int = 64):
    """anchor 兜底: 聚合数据各文件头部若干行取最小 ts(只读头部, 不全量读)。
    各片头部都是完整行(采集从空文件顺序写); 半行/解析失败行跳过。无数据返回 None。"""
    best = None
    for p in danmaku_paths(date, file):
        try:
            with open_text(p) as f:
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    try:
                        ts = int(json.loads(line).get("ts") or 0)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if ts > 0 and (best is None or ts < best):
                        best = ts
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
    return best


# ---- /api/days: 扫描数据目录 ----
_DM_COUNT_CACHE: dict = {}   # {路径: (mtime, dm行数)} 切片行数估算缓存, 避免每次全量数行


def count_dm_lines(path: Path) -> int:
    """流式数 ty=="dm" 的行数: 滚动切片无独立 meta 时的 dm_count 估算(带 mtime 缓存)。"""
    try:
        key, mtime = str(path), path.stat().st_mtime
    except OSError:
        return 0
    hit = _DM_COUNT_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    n = 0
    try:
        with open_text(path) as f:
            for line in f:
                try:
                    if json.loads(line).get("ty") == "dm":
                        n += 1
                except (ValueError, TypeError, AttributeError):
                    continue   # 半行/坏行跳过
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
        pass
    _DM_COUNT_CACHE[key] = (mtime, n)
    return n


def _session_dir_match_date(sess_dir: Path) -> str:
    """会话目录 -> 比赛日。

    目录名前缀是"数据日"(record_start 的日历日, 凌晨归前一天), 不是比赛日。
    判定比赛日的依据是**数据本身**: 会话里首条 ts 落在 00:00~11:00 就说明这是
    "昨晚开打、凌晨结束"的比赛, 比赛日 = 那天(即 ts 的日期); 否则就是当天比赛。
    读不到数据时退回用目录前缀 + 时刻推断。"""
    base = _base_of_session_dir(sess_dir.name)
    for p in danmaku_paths(sess_dir.parent.name, sess_dir.name)[:1]:
        try:
            with open_text(p) as f:
                for i, line in enumerate(f):
                    if i > 200:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts = int(json.loads(line).get("ts") or 0)
                    except (ValueError, TypeError):
                        continue
                    if ts:
                        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
    # 无数据可读: 用目录名里的数据日 + 时刻推断(凌晨开录 -> 比赛日是数据日次日)
    m = SESSION_DIR_RE.fullmatch(sess_dir.name)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        hh = int(m.group(2)[:2])
        return (d + timedelta(days=1)).strftime("%Y-%m-%d") if hh < 11 else m.group(1)
    return sess_dir.parent.name


def _session_time_range(date: str, file: str, sess_dir=None):
    """会话真实起止时间(毫秒墙钟), 返回 (t0, t1); 无数据返回 (None, None)。

    为什么不用 meta.record_start/record_end: 采集端的 meta 只覆盖**最后一次**收尾的片段
    (实测主会话 meta 记的是 08:45~09:00 的 281 条, 而实际数据是 23:33~08:59 的 20 万条),
    用它当会话时间会严重错位。这里直接取弹幕数据里最早/最晚的 ts。

    精度做法: **每个数据片**都读头部若干行取最小 ts、读尾部若干行取最大 ts, 再取全片集合的
    最小/最大。不能只看第一个/最后一个片 —— 文件名 `_sHHMM` 跨零点后字典序并不等于时间序
    (_s2332 会排在 _s0845 之后), 只看首尾片会漏掉真实边界。
    """
    paths = (_collect_all_in_session(sess_dir) if sess_dir is not None
             else danmaku_paths(date, file))
    if not paths:
        return None, None
    lo = hi = None
    for p in paths:
        try:
            head = []
            with open_text(p) as f:
                for i, line in enumerate(f):
                    if i > 120:
                        break
                    if line.strip():
                        head.append(line)
            tail = deque(maxlen=120)
            with open_text(p) as f:
                for line in f:
                    if line.strip():
                        tail.append(line)
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
        for line in list(head) + list(tail):
            try:
                t = int(json.loads(line).get("ts") or 0)
            except (ValueError, TypeError):
                continue
            if not t:
                continue
            lo = t if lo is None else min(lo, t)
            hi = t if hi is None else max(hi, t)
    return lo, hi


def scan_days() -> list:
    days = []
    if not DATA_DIR.is_dir():
        return days
    for day_dir in sorted(DATA_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not (day_dir.is_dir() and DATE_RE.fullmatch(day_dir.name)):
            continue
        rooms = {}   # room_id -> {"entries": [条目], "agg": {主名: 切片dm估算和}}
        # 待扫描的"数据容器": 旧平铺层(日目录自身) + 各会话子目录。
        # 每个容器内的命名规则相同, 只是用 (容器, 条目名) 唯一标识一个可选会话。
        containers = []
        if _day_flat_has_files(day_dir.name):
            containers.append(day_dir)
        containers.extend(_day_dirs(day_dir.name))
        for cont in containers:
            is_session_dir = _is_session_dir(cont)
            paths = list(cont.glob("*.jsonl.gz")) + list(cont.glob("*.jsonl"))
            for p in sorted(paths, key=lambda x: x.name):
                if p.name.endswith(".jsonl.gz"):
                    name = p.name[: -len(".jsonl.gz")]
                else:
                    name = p.name[: -len(".jsonl")]   # 活动切片 raw
                if name.endswith(".afp"):             # 音频指纹文件, 不是弹幕数据
                    continue
                m = TEST_FILE_RE.fullmatch(name)
                if m:                            # test_HHMMSS_{room}
                    room_id, base = int(m.group(2)), name
                else:
                    base = SLICE_RE.sub("", RUN_RE.sub("", name))  # 剥 _runN/_sHHMM 归回主名
                    if base.isdigit():
                        room_id = int(base)
                    elif is_session_dir:
                        # 会话目录里的文件名不一定以房间号结尾(如迁移遗留的 {room}_day0719)。
                        # 房间号以**目录名**为准(目录名是 {比赛日}_{HHMM}_{room}, 权威来源)。
                        sm = SESSION_DIR_RE.fullmatch(cont.name)
                        if not sm:
                            continue
                        room_id = int(sm.group(3))
                    else:
                        continue
                room = rooms.setdefault(room_id, {"entries": [], "agg": {}})
                # 会话目录布局下, 可选"文件"用目录名标识(一个目录 == 一个会话);
                # 旧平铺布局仍用基础名(同一目录里其实只有一个房间的主会话)。
                entry_name = cont.name if is_session_dir else name
                if SLICE_RE.search(RUN_RE.sub("", name)):
                    # _s???? 滚动切片(含活动 raw / 被 _runN 归档的切片): 聚合进主条目, 不单列
                    key = cont.name if is_session_dir else base
                    room["agg"][key] = room["agg"].get(key, 0) + count_dm_lines(p)
                    continue
                dm_count = 0
                meta_path = cont / f"{name}.meta.json"
                if meta_path.is_file():
                    try:
                        dm_count = int(json.loads(
                            meta_path.read_text(encoding="utf-8")).get("dm_count") or 0)
                    except (OSError, TypeError, ValueError):
                        dm_count = 0
                room["entries"].append({
                    "name": entry_name,
                    "is_test": (base.startswith("test_") or
                                "test_" in (cont.name if is_session_dir else "")),
                    "dm_count": dm_count,
                    "_base": base,
                    "_dir": cont.name,
                    "_session": is_session_dir,
                })
        if not rooms:
            continue
        out_rooms = []
        for rid, room in sorted(rooms.items()):
            entry_names = {e["name"] for e in room["entries"]}
            # 新格式没有 {room}.jsonl.gz 主文件(第一片就是 _sHHMM): 造虚拟主条目承载切片
            for name in sorted(room["agg"].keys() - entry_names):
                m2 = SESSION_DIR_RE.fullmatch(name)
                is_sess = bool(m2)
                room["entries"].append({
                    "name": name,
                    "is_test": ("test_" in name) if is_sess else name.startswith("test_"),
                    "dm_count": 0, "_base": _base_of_session_dir(name) if is_sess else name,
                    "_dir": name if is_sess else "", "_session": is_sess})
            for e in room["entries"]:
                # 该条目若在聚合表里有切片行数, 且自身没拿到 meta 的准确总数, 就用估算值
                agg_key = e["name"] if e["_session"] else e["_base"]
                if agg_key in room["agg"] and not e["dm_count"]:
                    e["dm_count"] = room["agg"][agg_key]
            room["entries"].sort(key=lambda x: (x["is_test"], x["name"]))
            out_rooms.append({"room": rid, "files": [
                {"name": x["name"], "is_test": x["is_test"], "dm_count": x["dm_count"],
                 "layout": "session" if x["_session"] else "flat"}
                for x in room["entries"]]})
        days.append({"date": day_dir.name, "rooms": out_rooms})
    # 跨零点**旧数据**归并: 次日目录只有切片聚合(无主 meta/主文件=非独立会话)时,
    # 估算量并入前一日同名条目, 次日条目移除。
    # 新会话目录布局下每个会话自带 meta, 天然不会跨日混淆, 所以这里只处理旧平铺条目。
    by_date = {d["date"]: d for d in days}
    for d in list(days):
        prev = by_date.get((datetime.strptime(d["date"], "%Y-%m-%d").date() - timedelta(days=1)).isoformat())
        if not prev:
            continue
        for room in d["rooms"]:
            has_meta = any((DATA_DIR / d["date"] / f"{f['name']}.meta.json").is_file()
                           for f in room["files"]
                           if not f["is_test"] and f.get("layout") != "session")
            if has_meta:
                continue    # 次日有独立会话(正常情况), 不动
            prev_room = next((r for r in prev["rooms"] if r["room"] == room["room"]), None)
            if not prev_room:
                continue
            for f in room["files"]:
                if f["is_test"] or f.get("layout") == "session":
                    continue
                tgt = next((x for x in prev_room["files"] if x["name"] == f["name"]), None)
                if tgt:
                    tgt["dm_count"] += f["dm_count"]
            room["files"] = [f for f in room["files"]
                             if f["is_test"] or f.get("layout") == "session"]
        d["rooms"] = [r for r in d["rooms"] if r["files"]]
    days = [d for d in days if d["rooms"]]
    # 给每个会话目录补上"比赛日": 目录日期只是数据落盘日(凌晨内容属昨晚 -> 落在昨天目录),
    # 而用户认知与 Riot 赛程都按比赛日组织。前端据此显示"比赛日", 目录日期仍用于取数据。
    for d in days:
        d["match_date"] = _session_match_date(d["date"])
        d["data_date"] = d["date"]      # 显式给出"数据日", 供前端显示「比赛日 + 数据日」
        # 每个会话补真实起止时间(毫秒墙钟): 前端据此显示时段并排序。
        # 取不到(如只有 afp 没有弹幕)就留空, 前端会跳过该会话。
        for room in d["rooms"]:
            for f in room["files"]:
                if f.get("layout") == "session":
                    t0, t1 = _session_time_range(d["date"], f["name"],
                                                 DATA_DIR / d["date"] / f["name"])
                else:
                    t0, t1 = _session_time_range(d["date"], f["name"])
                f["start_ms"], f["end_ms"] = t0, t1
    # 场次列表按比赛日挂在 lists[比赛日], 而弹幕会话目录=比赛日(新归属)或起始日(旧数据):
    # 前端请求 matchlist 时若当日列表为空, 自动回落到前一日列表(旧数据兼容), 见 handle_matchlist_get
    return days


async def handle_days(request: web.Request) -> web.Response:
    return api_ok(days=await asyncio.to_thread(scan_days))


# ---- /api/danmaku: 返回解压后的 jsonl 文本 ----
async def handle_danmaku(request: web.Request) -> web.Response:
    date, file = require_date_file(request)
    gz_paths = await asyncio.to_thread(danmaku_paths, date, file)
    if not gz_paths:
        raise ApiError(f"找不到弹幕文件: {date}/{file}.jsonl.gz")

    def _read_with_retry():
        """采集端整点滚动时, gz 正被写出(先写最终名再改名), 此刻读到会是不完整 gzip。
        这属于**正常并发**, 不是数据损坏: 短暂退避重试即可, 不该直接报"文件被截断"。
        重试 4 次(约 1.4s)仍失败才认定真损坏。

        注意捕获范围: gzip 解压失败在不同损坏形态下抛的异常不同 ——
        头部/CRC 错误抛 gzip.BadGzipFile, 而流内数据错乱抛的是 zlib.error
        (实测截断的 gz 走的是 zlib.error), 截断还可能是 EOFError。
        三者都要重试, 否则重试形同虚设。"""
        last = None
        for attempt in range(4):
            try:
                return read_danmaku_merged(gz_paths)
            except (gzip.BadGzipFile, EOFError, zlib.error) as e:
                last = e
                if attempt < 3:
                    time.sleep(0.2 * (2 ** attempt))
        raise last

    try:
        text = await asyncio.to_thread(_read_with_retry)
    except gzip.BadGzipFile as e:
        raise ApiError(f"弹幕文件不是有效的 gzip: {e}")
    except zlib.error as e:
        raise ApiError(f"弹幕文件解压失败(数据损坏或正在写入): {e}")
    except EOFError:
        raise ApiError("弹幕文件不完整(gzip 数据被截断)")
    except OSError as e:
        raise ApiError(f"弹幕文件读取失败: {e}")
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


# ---- /api/match: 读取/保存 弹幕文件与视频的匹配信息 ----
async def handle_match_get(request: web.Request) -> web.Response:
    date, file = require_date_file(request)
    meta_missing = False
    try:
        meta = load_meta(date, file)
    except ApiError:
        meta, meta_missing = {}, True
    # anchor 一律以聚合数据首行 ts 为准(跨零点采集的 meta 可能落在次日目录/存在旧 meta);
    # meta 的 first_ts 仅在数据读不到时兜底
    anchor_ms = await asyncio.to_thread(first_ts_from_data, date, file) or 0
    if not anchor_ms and not meta_missing:
        anchor_ms = compute_anchor_ms(meta)
    entry = load_matches()["align"].get(f"{date}|{file}") or {}
    offset_sec = entry.get("offset_sec", 0)
    if isinstance(offset_sec, bool) or not isinstance(offset_sec, (int, float)):
        offset_sec = 0
    # title 只作**兜底**展示用: 优先 align 记录里用户选过的场次名(G1 TES vs AL),
    # 绝不能把 meta.room_title(直播间当时的标题, 如"【预告】27号1点CFO vs T1")当比赛名,
    # 那正是"左上角比赛信息和实际选中的比赛不一致"的根因之一。
    title = str(entry.get("title") or "")
    return api_ok(anchor_ms=anchor_ms, offset_sec=offset_sec,
                  bv=str(entry.get("bv") or ""), title=title,
                  room_title=str(meta.get("room_title") or "") if not meta_missing else "",
                  match_date=_session_match_date(date),
                  meta_missing=meta_missing)


async def handle_match_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        raise ApiError("请求体不是有效的 JSON")
    if not isinstance(body, dict):
        raise ApiError("请求体应为 JSON 对象")
    date = str(body.get("date") or "").strip()
    file = str(body.get("file") or "").strip()
    if not date or not file:
        raise ApiError("缺少字段 date 或 file")
    if not DATE_RE.fullmatch(date):
        raise ApiError("字段 date 非法(应为 YYYY-MM-DD)")
    if not FILE_RE.fullmatch(file):
        raise ApiError("字段 file 非法(只允许字母/数字/下划线/连字符)")
    bv = str(body.get("bv") or "").strip()
    raw = body.get("offset_sec", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        try:
            raw = float(str(raw).strip())
        except ValueError:
            raise ApiError("offset_sec 应为数字")
    if not math.isfinite(raw):
        raise ApiError("offset_sec 应为有限数字")
    key = f"{date}|{file}"
    lt = str(body.get("list_title") or "").strip()
    new_title = body.get("title")
    # manual=true 表示这是用户手动微调(±5/10/30 或"对齐到此刻")产生的 offset。
    # 此时必须清掉旧的 auto 记录(自动校准置信度), 否则条目里会长期挂着一个
    # "高置信度自动校准"的假象, 让人以为当前 offset 是算法给出的。
    # 自动校准自身写回(order 见 run_autocalign)不走 manual, 所以不会误删。
    is_manual = bool(body.get("manual"))

    def _mut(matches):
        entry = dict(matches["align"].get(key) or {})   # 保留旧字段(如 title)实现合并
        entry.update({"date": date, "file": file, "bv": bv, "offset_sec": raw})
        if new_title is not None:
            entry["title"] = str(new_title)
        if is_manual:
            entry.pop("auto", None)
        matches["align"][key] = entry
        # 若携带 list_title, 同步更新场次列表里对应条目的 offset/bv。
        # 双保险: bv 也必须一致才写入(不同日期可能存在同名标题, 但防 applyOffsetValue
        # 携带过期 bv/list_title 组合跨列表污染)。
        if lt:
            for day_list in matches["lists"].values():
                if not isinstance(day_list, list):
                    continue
                for m in day_list:
                    if str(m.get("title")) == lt and (not m.get("bv") or str(m.get("bv")) == bv):
                        m["offset_sec"] = raw
                        m["bv"] = bv
                        if is_manual:
                            m.pop("auto", None)
                        break

    if not update_matches(_mut):
        raise ApiError("matches.json 写入失败")
    return api_ok()


def _session_match_date(date: str) -> str:
    """数据目录日期 -> **比赛日**。

    目录语义 vs 比赛语义的差别(正是"播放台日期对不上"的根因):
      采集端按"会话日"归目录: 凌晨 00:00~11:00 的内容属前一晚比赛, 落昨天日期目录。
      所以 data/2026-10-25/ 里装的是 10-25 23:33 ~ 10-26 08:45 的内容,
      即**北京时间 10-26 凌晨**开打的比赛 -> 比赛日 = 2026-10-26。
      Riot 赛程也按这个日期组织(窗口 = 当日 01:00~09:00 北京时间), 所以
      场次列表/赛程发现的 key 是比赛日, 而弹幕文件/对齐记录挂在数据目录日。

    判定依据是数据本身(首片首条 ts 落在 0~11 点), 不靠人工记账。
    """
    try:
        d0 = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return date
    day_dir = DATA_DIR / date
    if not day_dir.is_dir():
        return date
    # 新布局: 目录名是 {数据日}_{HHMM}_{room}。注意前缀是**数据日**(record_start 的日历日,
    # 凌晨会归到前一天), 并不等于比赛日 —— 例: 2026-10-25_2332 开录, 比赛日其实是 10-26。
    # 所以这里不能直接取前缀当比赛日, 要按开录时刻重算:
    #   开录在 23:32(当晚) -> 比赛日 = 当天日期 + 1 天? 不对, 取决于"比赛窗口"。
    # 正确判定: 用会话目录里的数据实际最早 ts 的**日期**作为比赛日(凌晨内容属当晚比赛)。
    best = None
    for sd in _day_dirs(date):
        m = SESSION_DIR_RE.fullmatch(sd.name)
        if not m:
            continue
        if "test_" in sd.name:      # 测试会话不代表当晚正赛
            continue
        md = _session_dir_match_date(sd)
        if best is None or md > best:
            best = md
    if best:
        return best
    for gz in sorted(day_dir.glob("*_s*.jsonl*")):
        try:
            with open_text(gz) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts = int(json.loads(line).get("ts") or 0)
                    except (ValueError, TypeError):
                        continue
                    if not ts:
                        continue
                    hour = datetime.fromtimestamp(ts / 1000).hour
                    if 0 <= hour < 11:      # 首片即次日凌晨内容 -> 属前一晚比赛
                        return (d0 + timedelta(days=1)).isoformat()
                    break    # 该文件首条不在凌晨, 检查下一文件
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
    return date


# ---- /api/matchlist: 场次列表(点选谁和谁) ----
async def handle_matchlist_get(request: web.Request) -> web.Response:
    date = (request.query.get("date") or "").strip()
    if not DATE_RE.fullmatch(date):
        raise ApiError("参数 date 非法(应为 YYYY-MM-DD)")
    lists = load_matches()["lists"]
    match_date = _session_match_date(date)
    matches = _matchlist_for_session(date, lists)
    # match_date 一并返回: 前端据此显示"比赛日", 并作为 /api/discover 的日期
    return api_ok(matches=matches or [], match_date=match_date)


def _matchlist_for_session(date: str, lists: dict):
    """该弹幕会话目录对应的赛程列表(程序判定, 不依赖人工记哪晚是哪晚):
    目录数据首条落在凌晨 00:00-11:00 => 属"前夜会话", 赛程取次日列表(比赛日=次日);
    否则用当日列表; 当日无列表再回落次日(目录存在但无切片可判时)。"""
    md = _session_match_date(date)
    got = lists.get(md)
    if got:
        return got
    next_key = (datetime.strptime(date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    return lists.get(date) or lists.get(next_key)


# ---- /api/discover: 当天赛程自动发现 + 官方回放BV自动搜索 ----
# 数据链: ① Riot getSchedule(Worlds) 取"北京时间 date 当天 01:00-09:00"的比赛块生成
# 场次草稿(队名顺序按 Riot 返回顺序) -> ② B站 web 搜索(带 buvid3 预热+浏览器UA)
# 官方账号 哔哩哔哩英雄联盟赛事(mid=50329118) 的单场回放, 标题同时含两队名(词边界+
# 大小写/英文别名不敏感)取 BV -> ③ 写回 matches.json: 列表缺失/为空才整体写入,
# 已存在只回填空 bv 条目, 绝不覆盖已有 bv/offset_sec/title。
# 全部网络请求走阻塞 urllib + asyncio.to_thread, 8 秒超时, 不堵事件循环。
# Riot API key: 优先取环境变量 RIOT_API_KEY; 未设置时回落到下面这个公开社区 key
# (vickz84259 社区文档里公开的 key, 非私密凭据, 调研时实测可用)。
# 公开出来是为了开箱即用; 若该 key 失效或被限流, 设同名环境变量覆盖即可, 不必改代码。
_DEFAULT_RIOT_API_KEY = "0TvQnueqKa5mxJntVWt5w0w4LpLfEkrV1Ta8rQBb9Z"
RIOT_API_KEY = (os.environ.get("RIOT_API_KEY", "").strip() or _DEFAULT_RIOT_API_KEY)
RIOT_SCHEDULE_URL = ("https://esports-api.lolesports.com/persisted/gw/getSchedule"
                     "?hl=en-US&leagueId=98767975604431411")  # Worlds 正确 leagueId(getLeagues 实测)
BILI_OFFICIAL_MID = 50329118          # 官方账号: 哔哩哔哩英雄联盟赛事
NET_TIMEOUT = 8                       # 秒, 对 Riot/B站 的单次请求超时
DISCOVER_TZ = timezone(timedelta(hours=8))            # 北京时间(比赛块过滤窗口用)
_SCHEDULE_CACHE = {"loaded": False, "ts": 0.0, "events": []}   # Riot 赛程内存缓存(赛程当天不变)
_SCHEDULE_CACHE_TTL = 600             # 秒
_SCHEDULE_LOCK = threading.Lock()
_BUVID3_CACHE = {"ts": 0.0, "value": ""}

# 队伍别名(仅英文全称等安全别名; 不含中文别名——"哔哩哔哩"等词会与官方账号名撞车造成误匹配)
TEAM_ALIASES = {
    "TES": ["top esports", "滔搏"],
    "AL": ["anyone's legend"],
    "BLG": ["bilibili gaming"],
    "IG": ["invictus gaming"],
    "GEN": ["gen.g", "gen g"],
    "HLE": ["hanwha life"],
    "DK": ["dplus kia"],
    "KC": ["karmine corp"],
    "MKOI": ["movistar koi"],
    "CFO": ["ctbc flying oyster"],
    "G2": ["g2 esports"],
    "FNC": ["fnatic"],
}


def _http_get(url: str, headers: dict, timeout: float = NET_TIMEOUT) -> tuple[int, str]:
    """阻塞 GET(供 to_thread 调用), 返回 (status, 文本)。异常向上抛由调用方处理。"""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _site_schedule_fallback() -> list:
    """Riot API 网关被限流时的兜底: 解析 lolesports.com 赛程页内嵌的 RSC 赛程数据。
    官方站点自身的渲染数据(与 getSchedule 同源: EventMatch 块含 startTime/
    league.slug/matchTeams[].code), 无需 key、不走被管控的网关。失败返回 []。"""
    try:
        req = urllib.request.Request("https://lolesports.com/schedule?league=worlds",
                                     headers={"User-Agent": BILI_UA,
                                              "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[discover] 官网页兜底获取失败: {e.__class__.__name__}: {e}")
        return []
    text = raw.replace('\\"', '"')        # RSC flight 字符串里的转义引号还原
    out, seen = [], set()
    for block in text.split('"__typename":"EventMatch"')[1:]:
        m_start = re.search(r'"startTime":"(\d{4}-\d{2}-\d{2}T[^"]+)"', block)
        if not m_start or not re.search(r'"league":\{[^{}]*?"slug":"worlds"', block):
            continue
        codes = re.findall(r'"code":"([0-9A-Za-z]{2,6})"', block)
        if len(codes) < 2:
            continue
        try:
            start = datetime.fromisoformat(m_start.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        key = (m_start.group(1), codes[0], codes[1])
        if key in seen or not start.tzinfo:   # 官网页 RSC 载荷通常重复携带两份, 去重
            continue
        seen.add(key)
        out.append({"start": start, "teams": [codes[0], codes[1]]})
    if not out:
        print("[discover] 官网页兜底未解析出 worlds 比赛块")
    return out


def riot_fetch_schedule() -> list:
    """Riot 赛程全量比赛块(带内存缓存)。
    主路: getSchedule API; 实测(2026-10-25) 该 key 为社区文档公开 key, 网关对其
    有用量管控, 会间歇性 403 ForbiddenException, 故 4 次退避重试。
    主路全部失败时兜底走 lolesports.com 官网页内嵌赛程(同源数据, 无需 key)。
    返回 [{"start": aware-datetime(UTC), "teams": [code1, code2]}], 按开始时间升序。"""
    with _SCHEDULE_LOCK:
        if (_SCHEDULE_CACHE["loaded"]
                and time.monotonic() - _SCHEDULE_CACHE["ts"] < _SCHEDULE_CACHE_TTL):
            return _SCHEDULE_CACHE["events"]
    headers = {"x-api-key": RIOT_API_KEY, "User-Agent": BILI_UA,
               "Accept": "application/json, text/plain, */*"}
    last_err: Exception = RuntimeError("未发起请求")

    def _parse_and_cache(events: list) -> list:
        out = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            teams = [str((t or {}).get("code") or "").strip()
                     for t in (ev.get("match") or {}).get("teams") or []]
            try:
                start = datetime.fromisoformat(
                    str(ev.get("startTime") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if len(teams) == 2 and teams[0] and teams[1] and start.tzinfo:
                out.append({"start": start, "teams": teams})
        out.sort(key=lambda e: e["start"])
        if out:
            with _SCHEDULE_LOCK:
                _SCHEDULE_CACHE.update(loaded=True, ts=time.monotonic(), events=out)
        return out

    for attempt in range(4):
        try:
            _, body = _http_get(RIOT_SCHEDULE_URL, headers)
            events = (((json.loads(body).get("data") or {}).get("schedule")
                       or {}).get("events")) or []
            out = _parse_and_cache(events)
            if out:
                return out
            last_err = RuntimeError("getSchedule 返回空事件列表")
        except Exception as e:            # 网络/解析/403 管控异常统一退避重试(本地工具从宽)
            last_err = e
        if attempt < 3:
            time.sleep((1.5, 5.0, 12.0)[attempt])
    out = _site_schedule_fallback()       # 兜底: 官网页内嵌赛程(已是解析后的形态)
    if out:
        out.sort(key=lambda e: e["start"])
        with _SCHEDULE_LOCK:
            _SCHEDULE_CACHE.update(loaded=True, ts=time.monotonic(), events=out)
        return out
    raise ApiError(f"获取Riot赛程失败(API已退避重试4次且官方页兜底未取得数据; "
                   f"该社区公开key被网关间歇性限流, 稍后再试): "
                   f"{last_err.__class__.__name__}: {last_err}")


def discover_draft_for_date(date: str) -> list:
    """北京时间 date 当天 01:00-09:00(凌晨~上午)的比赛块 -> 场次草稿。
    双方均 TBD 的占位块跳过(无回放匹配价值)。标题形如 "G1 TES vs AL"。"""
    y, m, d = (int(x) for x in date.split("-"))
    win0 = datetime(y, m, d, 1, 0, tzinfo=DISCOVER_TZ)
    win1 = datetime(y, m, d, 9, 0, tzinfo=DISCOVER_TZ)
    draft = []
    for ev in riot_fetch_schedule():
        local = ev["start"].astimezone(DISCOVER_TZ)
        if not (win0 <= local < win1):
            continue
        c1, c2 = ev["teams"]
        if c1.upper() == "TBD" and c2.upper() == "TBD":
            continue
        draft.append({"title": f"G{len(draft) + 1} {c1} vs {c2}",
                      "teams": [c1, c2], "start": local})
    return draft


def _bili_buvid3() -> str:
    """访问B站首页预热拿 buvid3(Set-Cookie), 1 小时内存缓存; 拿不到返回旧值/空串。"""
    now = time.monotonic()
    if _BUVID3_CACHE["value"] and now - _BUVID3_CACHE["ts"] < 3600:
        return _BUVID3_CACHE["value"]
    try:
        req = urllib.request.Request("https://www.bilibili.com/",
                                     headers={"User-Agent": BILI_UA})
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            for sc in r.headers.get_all("Set-Cookie") or []:
                if sc.startswith("buvid3="):
                    val = sc.split(";", 1)[0].split("=", 1)[1]
                    _BUVID3_CACHE.update(ts=now, value=val)
                    return val
    except Exception as e:
        print(f"[discover] buvid3 预热失败(沿用旧值): {e.__class__.__name__}: {e}")
    _BUVID3_CACHE["ts"] = now
    return _BUVID3_CACHE["value"]


_BILI_TAG_RE = re.compile(r"<[^>]+>")


def _team_in_title(title: str, code: str) -> bool:
    """队名(或别名)出现在标题: 大小写不敏感 + ASCII 词边界(防 "AL" 误命中 FINAL/G2 误命中内部)。"""
    for pat in [code] + TEAM_ALIASES.get(code.upper(), []):
        if re.search(r"(?<![0-9A-Za-z])" + re.escape(pat) + r"(?![0-9A-Za-z])",
                     title, re.IGNORECASE):
            return True
    return False


def bili_search_replay(c1: str, c2: str, date: str) -> str:
    """搜官方账号单场回放, 返回 BV 号(搜不到/未上传/请求失败 -> "")。
    关键词 "全球总决赛 队1 队2"; 结果筛 mid=官方 且标题同时含两队(词边界+别名);
    官方往届回放(如 2025 同对阵)可能排在前面, 故优先取标题含北京日期"M月D日"与年份者。"""
    cookie_parts = []
    buvid3 = _bili_buvid3()
    if buvid3:
        cookie_parts.append(f"buvid3={buvid3}")
    saved = load_cookie_header()
    if saved:
        cookie_parts.append(saved)
    headers = {"User-Agent": BILI_UA, "Referer": BILI_REFERER,
               "Accept": "application/json, text/plain, */*"}
    if cookie_parts:
        headers["Cookie"] = "; ".join(cookie_parts)
    url = ("https://api.bilibili.com/x/web-interface/search/type"
           "?search_type=video&page=1&keyword=" + quote(f"全球总决赛 {c1} {c2}"))
    try:
        _, body = _http_get(url, headers)
        data = json.loads(body)
    except Exception as e:
        print(f"[discover] B站搜索请求失败 {c1}/{c2}: {e.__class__.__name__}: {e}")
        return ""
    if not isinstance(data, dict) or data.get("code") != 0:
        code = data.get("code") if isinstance(data, dict) else "?"
        print(f"[discover] B站搜索返回异常 {c1}/{c2}: code={code}")
        return ""
    y, m, d = (int(x) for x in date.split("-"))
    date_str, year_str = f"{m}月{d}日", str(y)
    best = None          # (无日期惩罚, 无年份惩罚, 结果序号) 元组最小者胜
    for i, r in enumerate((data.get("data") or {}).get("result") or []):
        if not isinstance(r, dict) or r.get("type") != "video":
            continue
        if r.get("mid") != BILI_OFFICIAL_MID:
            continue                     # 搬运号标题可做到与官方完全一致, 必须 mid 过滤
        title = html.unescape(_BILI_TAG_RE.sub("", str(r.get("title") or "")))
        if not (_team_in_title(title, c1) and _team_in_title(title, c2)):
            continue
        key = (0 if date_str in title else 1, 0 if year_str in title else 1, i)
        if best is None or key < best[0]:
            best = (key, str(r.get("bvid") or ""))
    return best[1] if best else ""


_TITLE_TEAMS_RE = re.compile(r"^\s*G\d+\s+(\S+)\s+vs\s+(\S+)", re.IGNORECASE)


def _title_teams(title) -> tuple | None:
    """从既有列表条目标题解析两队 code: "G3 LOS vs G2（待确认场次）" -> ("LOS","G2")。"""
    m = _TITLE_TEAMS_RE.match(str(title or ""))
    if not m:
        return None

    def clean(s: str) -> str:
        return re.sub(r"[（(].*$", "", s).strip()

    a, b = clean(m.group(1)), clean(m.group(2))
    return (a, b) if a and b else None


async def handle_discover(request: web.Request) -> web.Response:
    raw_date = (request.query.get("date") or "").strip()
    if not DATE_RE.fullmatch(raw_date):
        raise ApiError("参数 date 非法(应为 YYYY-MM-DD)")
    # 前端传的是**数据目录日**(取文件要用它), 但赛程/Riot 窗口是按**比赛日**组织的。
    # 直接用目录日会去抓前一天的对阵: 例如目录 2026-10-25 装的是 10-26 凌晨的比赛,
    # 若按 10-25 去 discover, 拿到的是"TES vs AL"那一晚的场次, 与正在看的比赛完全不符,
    # 且因为 lists["2026-10-25"] 已存在(只回填空 bv), 真正的次日场次永远补不上 BV。
    date = await asyncio.to_thread(_session_match_date, raw_date)
    # a) Riot 赛程 -> 草稿(阻塞网络全部在 worker 线程, 8s 超时, 带内存缓存)
    draft = await asyncio.to_thread(discover_draft_for_date, date)
    # b) 逐场搜B站官方回放(顺序请求, 避免并发触发风控); 失败按 found=false 降级不炸整体
    for m in draft:
        m["bv"] = await asyncio.to_thread(
            bili_search_replay, m["teams"][0], m["teams"][1], date)
    # c) 写回: 列表缺失/为空 -> 整体写入草稿; 已存在 -> 只回填空 bv, 其余字段不动
    result = {"created": 0, "backfilled": 0}

    def _mut(matches):
        lst = matches["lists"].get(date)
        if not lst:
            if draft:
                matches["lists"][date] = [{"title": m["title"], "bv": m["bv"],
                                           "offset_sec": 0} for m in draft]
                result["created"] = len(draft)
            return
        for entry in lst:
            if str(entry.get("bv") or "").strip():
                continue
            teams = _title_teams(entry.get("title"))
            if not teams:
                continue
            want = {t.lower() for t in teams}
            for m in draft:
                if {t.lower() for t in m["teams"]} == want and m["bv"]:
                    entry["bv"] = m["bv"]
                    result["backfilled"] += 1
                    break

    if draft:
        # 无变更就不落盘(省一次写); 有变更走锁内 read-modify-write
        probe = await asyncio.to_thread(load_matches)
        need = (not probe["lists"].get(date)) or any(
            not str(e.get("bv") or "").strip() and _title_teams(e.get("title"))
            for e in (probe["lists"].get(date) or []))
        if need:
            if not await asyncio.to_thread(update_matches, _mut):
                raise ApiError("matches.json 写入失败")
    out = [{"idx": i, "title": m["title"], "bv": m["bv"], "found": bool(m["bv"])}
           for i, m in enumerate(draft)]
    return api_ok(matches=out, riot_count=len(draft), date=date, data_date=raw_date,
                  created=result["created"], backfilled=result["backfilled"])


# ---- /api/playurl: 通过B站网页接口取 mp4 直链 ----
def _bili_headers() -> dict:
    headers = {"User-Agent": BILI_UA, "Referer": BILI_REFERER}
    cookie = load_cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    return headers


async def bili_view(session, bv: str) -> dict:
    """B站网页 view 接口。返回 {cid, title, duration, pages}。

    pages 是**全部分P**(官方回放按"第一局/第二局/第三局"拆成多个分P):
      [{"page":1,"cid":...,"duration":秒,"part":"第一局"}, ...]

    历史缺陷: 旧实现只取 data.cid(恒为 pages[0]), 而前端进度条长度却用
    data.duration(**多P合计**) → 只能播第一局, 进度条天花板按局数缩水
    (实测 G4 播到 36% 就卡死, 第二/三局完全无法访问)。
    现在把分P列表一并返回, 由 /api/playurl?page=N 精确取某一局。"""
    try:
        async with session.get("https://api.bilibili.com/x/web-interface/view",
                               params={"bvid": bv}, headers=_bili_headers()) as r:
            view = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ApiError(f"请求B站视频信息失败: {e}")
    if not isinstance(view, dict) or view.get("code") != 0:
        msg = view.get("message") if isinstance(view, dict) else None
        raise ApiError(f"获取视频信息失败: {msg or 'B站接口返回异常'}")
    vdata = view.get("data") or {}
    cid = vdata.get("cid")
    if cid is None:
        raise ApiError("视频信息中缺少 cid")
    try:
        duration = int(vdata.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    pages = []
    for p in vdata.get("pages") or []:
        if not isinstance(p, dict):
            continue
        try:
            p_cid = int(p.get("cid"))
            p_no = int(p.get("page") or 0)
            p_dur = int(p.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if p_cid <= 0 or p_no < 1:
            continue
        pages.append({"page": p_no, "cid": p_cid, "duration": p_dur,
                      "part": str(p.get("part") or "")})
    if not pages:   # 极老/异常视频无 pages 字段: 退化为单P
        try:
            pages = [{"page": 1, "cid": int(cid), "duration": duration, "part": ""}]
        except (TypeError, ValueError):
            raise ApiError("视频信息中缺少可用的 cid")
    pages.sort(key=lambda x: x["page"])
    return {"cid": cid, "title": str(vdata.get("title") or ""),
            "duration": duration, "pages": pages}


async def _bili_durl_segments(session, bv: str, cid, page_label: str = "") -> dict:
    """取某个 cid 的 html5 mp4 durl 分段。返回 {urls, seg_lengths}。失败抛 ApiError。"""
    try:
        async with session.get("https://api.bilibili.com/x/player/playurl",
                               params={"bvid": bv, "cid": cid, "qn": 64,
                                       "platform": "html5", "high_quality": 1},
                               headers=_bili_headers()) as r:
            play = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ApiError(f"请求B站播放地址失败{page_label}: {e}")
    if not isinstance(play, dict) or play.get("code") != 0:
        msg = play.get("message") if isinstance(play, dict) else None
        raise ApiError(f"获取播放地址失败{page_label}: {msg or 'B站接口返回异常'}")
    urls, seg_lens = [], []
    for item in (play.get("data") or {}).get("durl") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
            seg_lens.append(round((item.get("length") or 0) / 1000, 3))
    return {"urls": urls, "seg_lengths": seg_lens}


async def bili_playurl_mp4(session, bv: str, page: int = 0) -> dict:
    """html5 平台 mp4 直链。page=0(默认) 表示**全部分P连播**; page>=1 只取该分P。

    为什么有"全分P连播": 官方回放把每局拆成独立分P(第一局/第二局/第三局),
    旧实现只取 data.cid(= pages[0]) 且用 view.duration(多P合计) 当进度条长度,
    导致只能播第一局、进度条按局数缩水(实测 G4 播到 36% 就卡死, 二/三局完全不可达)。

    连播模式下把各分P的 durl 段**顺序拼成一条 url 列表**, 前端沿用已有的
    多分段虚拟时间轴(getV/seekV)即可无缝跨局播放, 且 offset 语义不变
    (整条虚拟时间轴 = 官方回放完整时间线, 与赛程自然对应)。

    返回:
      urls/seg_lengths  全部分段(按播放顺序)
      duration          整条虚拟时间轴总长(= 各段之和, 前端进度条用)
      pages             分P清单 [{page,cid,duration,part}]
      page_marks        [{page,part,start_sec,duration}] 每局在虚拟轴上的起点(供分集标签)
      mode              "all" | "page"
    """
    info = await bili_view(session, bv)
    pages = info["pages"]
    total_pages = len(pages)

    if page and page >= 1:
        sel = next((p for p in pages if p["page"] == page), None)
        if sel is None:
            raise ApiError(f"该视频没有第 {page} 个分P(共 {total_pages} 个)")
        seg = await _bili_durl_segments(session, bv, sel["cid"],
                                        f"(第 {page} 分P {sel['part']})")
        if not seg["urls"]:
            raise ApiError("未取得播放直链(可能需要 cookies.txt 登录 Cookie, 或视频不可播放)")
        seg_total = round(sum(seg["seg_lengths"]), 3)
        page_duration = int(round(seg_total)) if seg_total > 0 else int(sel["duration"] or 0)
        if page_duration <= 0:
            page_duration = int(info["duration"] or 0)
        return {"url": seg["urls"][0], "urls": seg["urls"],
                "seg_lengths": seg["seg_lengths"], "duration": page_duration,
                "title": info["title"], "pages": pages, "page": page,
                "page_part": sel["part"], "total_pages": total_pages,
                "page_marks": [{"page": page, "part": sel["part"],
                                "start_sec": 0, "duration": page_duration}],
                "mode": "page"}

    # --- 全分P连播 ---
    urls, seg_lens, page_marks, failed = [], [], [], []
    for p in pages:
        try:
            seg = await _bili_durl_segments(session, bv, p["cid"],
                                            f"(第 {p['page']} 分P {p['part']})")
        except ApiError as e:
            # 单个分P取不到不拖垮整场: 记下来继续, 前端提示缺哪一局
            failed.append({"page": p["page"], "part": p["part"], "error": str(e)})
            continue
        if not seg["urls"]:
            failed.append({"page": p["page"], "part": p["part"], "error": "无播放直链"})
            continue
        p_dur = round(sum(seg["seg_lengths"]), 3) or float(p["duration"] or 0)
        page_marks.append({"page": p["page"], "part": p["part"],
                           "start_sec": round(sum(seg_lens), 3), "duration": p_dur})
        urls.extend(seg["urls"])
        seg_lens.extend(seg["seg_lengths"])
    if not urls:
        raise ApiError("未取得任何播放直链(可能需要 cookies.txt 登录 Cookie, 或视频不可播放)")
    return {"url": urls[0], "urls": urls, "seg_lengths": seg_lens,
            "duration": round(sum(seg_lens), 3), "title": info["title"],
            "pages": pages, "page": 0, "page_part": "",
            "total_pages": total_pages, "page_marks": page_marks,
            "failed_pages": failed, "mode": "all"}


async def handle_playurl(request: web.Request) -> web.Response:
    bv = (request.query.get("bv") or "").strip()
    if not BV_RE.fullmatch(bv):
        raise ApiError("参数 bv 缺失或格式不正确(应形如 BV1xx411c7mD)")
    # page 缺省 = 0 = 全部分P连播(官方回放按局拆P, 必须全取才是一整场)
    raw_page = (request.query.get("page") or "0").strip()
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        raise ApiError("参数 page 应为整数")
    if page < 0:
        raise ApiError("参数 page 应 >= 0 (0=全部分P连播)")
    play = await bili_playurl_mp4(request.app["http"], bv, page)
    return api_ok(**play)


# ---- /api/overview: 一晚"总览时间轴"(剪辑软件式多轨) ----
# 设计意图(对应前端"总览"视图):
#   弹幕是一条**连续的真实时间轴**(一个直播间的整晚录制), 官方回放却是**切分的分段**
#   (每局一个分P, 各自独立视频)。校准就是求出每场比赛在弹幕主轴上的落点(offset)。
#   总览视图把"弹幕主轴"当时间基准, 把每场比赛的各分P按 offset 摆放成一条轨道,
#   于是"哪一局落在哪一刻""场次之间空档多久"一目了然, 也便于后续做连播/自动切场。
OVERVIEW_MAX_FILES = 40          # 单个会话目录最多统计多少个弹幕文件(防极端目录拖慢)
OVERVIEW_BIN_SEC = 30            # 密度直方图 bin 宽度(秒)


def _afp_duration_sec(date: str, file: str) -> float:
    """该会话音频指纹的覆盖时长(秒), 0 表示没有/读不到。用于给没有 meta 的会话兜底算主轴长度。"""
    try:
        paths = find_afp_paths(date, file)
        if not paths:
            return 0.0
        afp = read_afp_merged(paths)
    except Exception:
        return 0.0
    hop = float((afp.get("params") or {}).get("hop") or 0.2)
    total = 0
    for c in afp.get("conns") or []:
        try:
            total = max(total, int(c.get("n") or 0))
        except (TypeError, ValueError):
            continue
    return round(total * hop, 1)


def _session_density(date: str, file: str, bin_sec: int = OVERVIEW_BIN_SEC) -> dict:
    """统计一个会话文件的弹幕密度直方图 + 首末 ts。

    只读一遍文件, 直接产前端画总览用的紧凑数组; 不返回原始弹幕(几十万行太重)。"""
    paths = danmaku_paths(date, file)
    first_ts = last_ts = 0
    stamps = []
    total = 0
    for p in paths:
        try:
            with open_text(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue        # 半行/坏行跳过(采集进行中常见)
                    ts = obj.get("ts")
                    if not isinstance(ts, (int, float)) or ts <= 0:
                        continue
                    ts = int(ts)
                    if obj.get("ty") == "dm":
                        total += 1
                        stamps.append(ts)
                    if not first_ts or ts < first_ts:
                        first_ts = ts
                    if ts > last_ts:
                        last_ts = ts
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
    if not stamps:
        return {"first_ts": first_ts, "last_ts": last_ts, "dm_count": total,
                "bins": [], "bin_sec": bin_sec, "peak": 0}
    # 注意: 必须用 first_ts(全局最小 ts) 而不是 stamps[0] 作为 bin 原点 ——
    # danmaku_paths 返回的文件顺序不保证按时间升序(切片按文件名排序, 而 _s2332 这类
    # 跨零点片名会排在 s0000 之后), 用 stamps[0] 会让整条直方图错位。
    t0, t1 = first_ts, last_ts
    nb = max(1, int((t1 - t0) / (bin_sec * 1000)) + 1)
    if nb > 4000:                       # 极端长会话: 放大 bin, 保证前端能画
        bin_sec = int((t1 - t0) / 1000 / 4000) + 1
        nb = max(1, int((t1 - t0) / (bin_sec * 1000)) + 1)
    bins = [0] * nb
    for ts in stamps:
        i = int((ts - t0) / (bin_sec * 1000))
        if 0 <= i < nb:
            bins[i] += 1
    return {"first_ts": t0, "last_ts": t1, "dm_count": total,
            "bins": bins, "bin_sec": bin_sec, "peak": max(bins) if bins else 0}


async def handle_overview(request: web.Request) -> web.Response:
    """一晚总览: 弹幕主轴(连续真实时间) + 每场比赛的各分P轨道(按 offset 摆放)。

    参数 date = **数据目录日**; 返回里的 match_date 是比赛日(给用户看)。
    """
    raw_date = (request.query.get("date") or "").strip()
    if not DATE_RE.fullmatch(raw_date):
        raise ApiError("参数 date 非法(应为 YYYY-MM-DD)")

    def _build():
        day_dir = DATA_DIR / raw_date
        if not day_dir.is_dir():
            raise ApiError(f"数据目录不存在: {raw_date}")
        # 选主会话: 非 test 的、弹幕文件数最多的那一组。
        # 两种布局都要能选: 新会话目录(每个子目录是一个会话, 用目录名作 file),
        # 旧平铺层(用去 _sHHMM 的主名作 file)。取"文件最多"的那个 = 主会话。
        names = {}      # file_key -> 文件数
        # 新布局: 会话子目录
        for sd in _day_dirs(raw_date):
            if "test_" in sd.name:
                continue
            cnt = sum(1 for p in list(sd.glob("*.jsonl.gz")) + list(sd.glob("*.jsonl"))
                      if ".afp.jsonl" not in p.name)
            if cnt:
                names[sd.name] = cnt
        # 旧布局: 日目录平铺层
        if _day_flat_has_files(raw_date):
            for p in day_dir.iterdir():
                if not p.is_file():
                    continue
                name = p.name
                if name.startswith("test_"):
                    continue
                if ".afp.jsonl" in name or name.endswith(".meta.json"):
                    continue
                if ".jsonl" not in name:
                    continue
                stem = name
                for suf in (".jsonl.gz", ".jsonl"):
                    if stem.endswith(suf):
                        stem = stem[: -len(suf)]
                        break
                stem = re.sub(r"(_s\d{4}|_run\d+)$", "", stem)   # 归到主名
                if not FILE_RE.fullmatch(stem):
                    continue
                names[stem] = names.get(stem, 0) + 1
        if not names:
            raise ApiError(f"{raw_date} 没有可用的弹幕会话文件")
        file = max(names.items(), key=lambda kv: kv[1])[0]

        # 弹幕主轴(连续真实时间)
        dens = _session_density(raw_date, file)
        afp_sec = _afp_duration_sec(raw_date, file)   # 算一次复用, 别在下面重复解压 afp
        if not dens["dm_count"]:
            # 没有 dm 行也可能是空会话: 用 afp 时长兜底, 主轴仍可画
            if dens["first_ts"] and afp_sec:
                dens["last_ts"] = max(dens["last_ts"],
                                      dens["first_ts"] + int(afp_sec * 1000))
        t0 = dens["first_ts"]
        t1 = dens["last_ts"]
        span = max(1, t1 - t0)

        matches_all = load_matches()
        match_date = _session_match_date(raw_date)
        lst = matches_all["lists"].get(match_date) or []
        align = matches_all["align"]

        # 每场比赛: 取它的 BV + offset + 分P结构(分P信息来自 playurl, 需要网络;
        # 这里只用已缓存/已记录的元信息, 不主动打 B 站接口 —— 总览要秒开)
        tracks = []
        for idx, m in enumerate(lst):
            if not isinstance(m, dict):
                continue
            title = str(m.get("title") or "")
            bv = str(m.get("bv") or "")
            offset = m.get("offset_sec")
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                offset = 0
            # 若该 BV 有整场对齐记录, 用它的 offset 覆盖场次列表里可能过期的值
            for akey, aentry in align.items():
                if not isinstance(aentry, dict):
                    continue
                if str(aentry.get("bv") or "") != bv or not bv:
                    continue
                ao = aentry.get("offset_sec")
                if isinstance(ao, (int, float)) and not isinstance(ao, bool):
                    offset = float(ao)
                break
            tracks.append({
                "idx": idx, "title": title, "bv": bv,
                "offset_sec": round(float(offset), 3),
                "auto": m.get("auto"),
                # 该场在弹幕主轴上的落点(相对主轴起点, 秒): offset 是"回放 t=0 对应的
                # 真实墙钟", 主轴 0 点是首条弹幕, 故相对位置 = offset - (t0-anchor) 由前端算
                "has_bv": bool(bv),
            })
        return {
            "date": raw_date,
            "match_date": match_date,
            "file": file,
            "anchor_ts": t0,
            "end_ts": t1,
            "span_sec": round(span / 1000.0, 1),
            "density": {"bins": dens["bins"], "bin_sec": dens["bin_sec"],
                        "peak": dens["peak"], "t0": t0},
            "tracks": tracks,
            "dm_count": dens["dm_count"],
            "afp_duration_sec": afp_sec,
        }

    data = await asyncio.to_thread(_build)
    return api_ok(**data)


# ---- /api/stream: 反代B站 CDN 字节流(直链 403 时的兜底) ----
# /api/stream 只用于反代 B 站 CDN。这里的防护是"白名单域名 + 解析后 IP 校验":
# 旧实现只做字面 IP 判断, 审计实测 127.1 / 0x7f000001 / 2130706433 等写法能绕过字面
# 解析(ipaddress 判 ValueError 直接放行), 且域名解析到 127.0.0.1 也可穿透 —— 等于
# 把它当成了开放正向代理。既然本工具只反代 B 站, 直接白名单最省事也最安全。
ALLOWED_STREAM_SUFFIXES = (
    ".bilivideo.com", ".bilivideo.cn", ".bilibili.com", ".biliapi.net",
    ".hdslb.com", ".szbdyd.com", ".akamaized.net", ".mcdn.bilivideo.com",
)


def _host_allowed_for_stream(host: str) -> bool:
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return False
    return any(h == s.lstrip(".") or h.endswith(s) for s in ALLOWED_STREAM_SUFFIXES)


def is_private_host(host: str) -> bool:
    """阻断本机/内网目标。除字面 IP 外, 也对域名做 DNS 解析后逐 IP 校验。"""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return True
    if h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return True

    def _bad_ip(ip) -> bool:
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)

    try:
        return _bad_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    # 域名: 解析后逐个 IP 判断, 防"公网域名解析到 127.0.0.1"的绕过
    try:
        infos = socket.getaddrinfo(h, None)
    except (socket.gaierror, OSError, UnicodeError):
        return True     # 解析不了就不放行(宁可拒绝, 本地工具无需宽松)
    for info in infos:
        addr = info[4][0]
        try:
            if _bad_ip(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            return True
    return False


async def handle_stream(request: web.Request) -> web.StreamResponse:
    url = (request.query.get("url") or "").strip()
    if not url:
        raise ApiError("缺少参数 url")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError("参数 url 必须是合法的 http/https 地址")
    if not _host_allowed_for_stream(parsed.hostname):
        raise ApiError("只允许代理 B 站 CDN 地址")
    if is_private_host(parsed.hostname):
        raise ApiError("不允许代理本机/内网地址")
    session = request.app["http"]
    fwd = {"User-Agent": BILI_UA, "Referer": BILI_REFERER}
    if request.headers.get("Range"):           # 透传 Range, 支持视频拖动进度条
        fwd["Range"] = request.headers["Range"]
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10,
                                    sock_read=120)
    upstream = None
    try:
        upstream = await session.get(url, headers=fwd, timeout=timeout)
        if upstream.status >= 400:
            status = upstream.status
            upstream.close()
            upstream = None
            raise ApiError(f"上游返回 HTTP {status}, 无法代理")
        resp = web.StreamResponse(status=upstream.status)
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges"):
            v = upstream.headers.get(h)
            if v is not None:
                resp.headers[h] = v
        await resp.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
        except Exception as e:                 # 客户端断开/上游中断: 放弃本次代理
            print(f"[stream] 代理中断: {e.__class__.__name__}: {e}")
            upstream.close()
            try:
                await resp.write_eof()
            except Exception:
                pass
            return resp
        upstream.release()                     # 正常读完, 连接回池
        await resp.write_eof()
        return resp
    except aiohttp.ClientError as e:
        if upstream is not None:
            upstream.close()
        raise ApiError(f"代理请求失败: {e}")
    except asyncio.TimeoutError:
        if upstream is not None:
            upstream.close()
        raise ApiError("连接上游超时")


# ---- /api/autocalign: 音频指纹自动对齐 ----
def _afp_in_dir(day_dir: Path, file: str) -> list:
    """某日期目录里的 afp 文件: 主 {file}.afp.jsonl[.gz] + {file}_runN 归档。"""
    paths = []
    if not day_dir.is_dir():
        return paths
    for name in (f"{file}.afp.jsonl", f"{file}.afp.jsonl.gz"):
        p = day_dir / name
        if p.is_file():
            paths.append(p)
            break  # 同基础名明文/gzip 只取一份
    for gz in sorted(day_dir.glob(f"{file}_run*.afp.jsonl.gz")):
        if re.fullmatch(re.escape(file) + r"_run\d+" + re.escape(".afp.jsonl.gz"), gz.name):
            paths.append(gz)
    return paths


def find_afp_paths(date: str, file: str) -> list:
    """找采集端写的音频指纹文件: 主 {file}.afp.jsonl[.gz] + 全部 {file}_runN 归档
    (一晚多次下播 finalize 会产生多份 afp, 比赛内容可能主要在归档段里)。

    另有**次日兜底**: 旧版采集端的 afp 路径用的是 `record_start` 的日历日, 而弹幕
    用的是"会话日"(凌晨归前一天)。于是凌晨(世界赛主时段)开录的会话, afp 会落进
    次日目录、弹幕落进当日目录。只查单一日录会报"该日无音频指纹数据"。
    这里在当日目录找不到时, 再去次日目录找同名 afp(次日目录无独立主 meta 时才算
    同一会话残留, 避免误取第二天晚上的新指纹)。"""
    paths = []
    m = SESSION_DIR_RE.fullmatch(file)
    if m:   # 新布局: file 就是会话目录名。afp 名同样不一定等于目录推导名, 直接扫目录。
        sess = DATA_DIR / date / file
        if sess.is_dir():
            cands = sorted(p for p in sess.iterdir()
                           if ".afp.jsonl" in p.name and p.is_file())
            if cands:
                return cands
    # 旧平铺布局 + 各会话子目录
    for d in _scope_dirs(date):
        paths = _afp_in_dir(d, file)
        if paths:
            return paths
    try:
        next_day = (datetime.strptime(date, "%Y-%m-%d").date()
                    + timedelta(days=1)).isoformat()
    except ValueError:
        return []
    nd = DATA_DIR / next_day
    if not nd.is_dir():
        return []
    # 次日若已有自己的主 meta, 说明是独立会话, 不应把它的 afp 借给前一天
    if (nd / f"{file}.meta.json").is_file():
        return []
    return _afp_in_dir(nd, file)


def read_afp_merged(paths: list) -> dict:
    """多份 afp(主+归档)合并: 各文件段号都从 0 起, 跨文件统一重编全局段号。"""
    conns, params = [], None
    for p in paths:
        d = audiofp.read_afp(p)
        if params is None:
            params = d["params"]
        for seg in d["conns"]:
            seg = dict(seg)
            seg["c"] = len(conns)  # 全局重编, 避免跨文件撞号
            conns.append(seg)
    return {"params": params, "conns": conns}


def _ffmpeg_exe() -> str:
    if imageio_ffmpeg is None:
        raise ApiError("缺少 imageio-ffmpeg, 请在 venv 里 pip install imageio-ffmpeg 后重启服务")
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as e:      # 二进制不可用
        raise ApiError(f"ffmpeg 不可用: {e}")


def ffmpeg_audio_fp(src, hop_sec: float, win_sec: float = 0.4):
    """ffmpeg 把 src(本地文件或URL) 抽成 16k 单声道 s16le 原始流后直接算指纹。

    同步阻塞(CPU+子进程), 调用方需 asyncio.to_thread。返回 (N,33) float32。"""
    cmd = [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
           "-f", "s16le", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except OSError as e:
        raise ApiError(f"启动 ffmpeg 失败: {e}")
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise ApiError(f"ffmpeg 抽取音频失败: {err or '无输出'}")
    x = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    del proc
    if x.size < int(16000 * win_sec):
        raise ApiError(f"音频太短无法算指纹: {x.size / 16000:.1f}s")
    return audiofp.fingerprint(x, 16000, hop_sec=hop_sec, win_sec=win_sec)


async def bili_dash_audio_url(session, bv: str):
    """尝试 dash 纯音轨直链(fnval=16, 优先 30280/192kbps)。拿不到返回 None(不抛错)。"""
    try:
        info = await bili_view(session, bv)
        async with session.get("https://api.bilibili.com/x/player/playurl",
                               params={"bvid": bv, "cid": info["cid"],
                                       "qn": 64, "fnval": 16},
                               headers=_bili_headers()) as r:
            play = await r.json(content_type=None)
        if not isinstance(play, dict) or play.get("code") != 0:
            return None
        audios = ((play.get("data") or {}).get("dash") or {}).get("audio") or []
        cand = None
        for a in audios:
            if not isinstance(a, dict):
                continue
            u = a.get("baseUrl") or a.get("base_url")
            if not u:
                continue
            if a.get("id") == 30280:      # 192kbps 音轨优先
                return str(u)
            if cand is None:
                cand = str(u)
        return cand
    except (ApiError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        return None


async def download_dash_audio(session, bv: str, page: int, url: str) -> Path:
    """下载 dash 音轨到缓存目录(已存在则复用), 返回本地文件路径。"""
    dst = AFP_CACHE_DIR / f"{bv}_{page}.audio.m4s"
    if dst.is_file() and dst.stat().st_size > 1024:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".m4s.part")
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10,
                                    sock_read=120)
    try:
        async with session.get(url, headers=_bili_headers(), timeout=timeout) as r:
            if r.status >= 400:
                raise ApiError(f"dash 音轨下载失败: HTTP {r.status}")
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(256 * 1024):
                    f.write(chunk)
        if tmp.stat().st_size < 1024:
            raise ApiError("dash 音轨下载内容为空")
        tmp.replace(dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return dst


def _afp_cache_file(bv: str, page: int) -> Path:
    return AFP_CACHE_DIR / f"{bv}_{page}.fp.npz"


def _save_fp_cache(cache: Path, fp, hop: float, win: float) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    # 唯一临时名: 两个请求同时算同一 BV 的指纹时不会共用同一个 .tmp 而写坏缓存
    tmp = cache.with_name("%s.%d.%d.tmp" % (cache.name, os.getpid(), threading.get_ident()))
    try:
        with open(tmp, "wb") as f:
            np.savez_compressed(f, fp=fp, hop=hop, win=win)
        tmp.replace(cache)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


async def get_replay_fingerprint(session, bv: str, page: int, hop_sec: float = 0.2,
                                 win_sec: float = 0.4, local_audio=None):
    """取回放({bv} P{page})的音频指纹(带磁盘缓存, 避免 2636s 视频反复拉流)。

    优先级: local_audio 调试参数 > 磁盘缓存 > dash 纯音轨 > html5 mp4 整段(经
    本服务 /api/stream 反代; 多段按顺序拼接指纹)。
    返回 (fp (N,33) float32, 来源说明)。"""
    if not HAS_AUDIOFP:
        raise ApiError(f"音频指纹模块不可用: {_AFP_IMPORT_ERR or 'numpy/audiofp 导入失败'}")
    if local_audio:                      # 调试/测试: 本地音频直接算, 不碰网络与缓存
        fp = await asyncio.to_thread(ffmpeg_audio_fp, local_audio, hop_sec, win_sec)
        return fp, f"本地音频({Path(local_audio).name})"
    cache = _afp_cache_file(bv, page)
    if cache.is_file():
        try:
            z = np.load(cache)
            if (float(z["hop"]) == float(hop_sec) and float(z["win"]) == float(win_sec)
                    and int(z["fp"].ndim) == 2):
                return np.asarray(z["fp"], dtype=np.float32), "缓存"
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            # npz 是 zip 容器, 被写坏/截断时抛的是 zipfile.BadZipFile(不是 OSError),
            # 旧实现漏了它 → 一旦缓存损坏, 自动校准会永久报"服务器内部错误"。
            # 现在按"缓存不可用"处理: 删掉坏文件后走网络重算。
            print(f"[autocalign] 指纹缓存损坏, 丢弃重算: {cache.name}")
            try:
                cache.unlink(missing_ok=True)
            except OSError:
                pass
    # ① dash 纯音轨: 数据量比整段 mp4 小一个数量级
    audio_url = await bili_dash_audio_url(session, bv)
    if audio_url:
        try:
            m4s = await download_dash_audio(session, bv, page, audio_url)
            fp = await asyncio.to_thread(ffmpeg_audio_fp, str(m4s), hop_sec, win_sec)
            await asyncio.to_thread(_save_fp_cache, cache, fp, hop_sec, win_sec)
            return fp, f"dash音轨({m4s.stat().st_size // 1024 // 1024}MB)"
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError,
                OSError, ValueError) as e:
            print(f"[autocalign] dash 音轨路线失败, 转入 mp4 兜底: {e}")
    # ② 整段 mp4 兜底: html5 durl(可能多段), ffmpeg 走本服务 /api/stream 反代
    play = await bili_playurl_mp4(session, bv)
    urls = play["urls"]
    fps = []
    for i, u in enumerate(urls):
        src = f"http://127.0.0.1:{PORT}/api/stream?url={quote(u, safe='')}"
        fps.append(await asyncio.to_thread(ffmpeg_audio_fp, src, hop_sec, win_sec))
        print(f"[autocalign] 回放段 {i + 1}/{len(urls)} 指纹完成 "
              f"({fps[-1].shape[0]} 帧)")
    fp = np.concatenate(fps, axis=0)     # 段间相位差 < hop, 顺序拼接即可
    await asyncio.to_thread(_save_fp_cache, cache, fp, hop_sec, win_sec)
    return fp, f"mp4直链({len(urls)}段)"


async def run_autocalign(date: str, file: str, bv: str, page: int, session,
                         local_audio=None) -> dict:
    """自动对齐全流程(读 afp -> 回放指纹 -> 逐段互相关 -> 算 offset -> 写 matches)。

    offset 语义与前端完全一致(app.js: offset_sec = (wall_ms-anchor_ms)/1000
    - 回放内位置): anchor_ms 取 compute_anchor_ms(meta)。
    local_audio 仅供调试/测试注入回放音频, 正常流程传 None。"""
    if not HAS_AUDIOFP:
        raise ApiError(f"音频指纹模块不可用: {_AFP_IMPORT_ERR or 'numpy/audiofp 导入失败'}")
    t0 = time.monotonic()

    # a) 找 afp 文件(主文件 + 全部 _runN 归档, 跨文件合并段号)
    afp_paths = find_afp_paths(date, file)
    if not afp_paths:
        raise ApiError("该日无音频指纹数据(采集端未启用或版本旧)")
    afp = await asyncio.to_thread(read_afp_merged, afp_paths)
    params = afp["params"]
    hop = float(params.get("hop") or 0.2)
    win = float(params.get("win") or 0.4)
    conns = [c for c in afp["conns"] if c["n"] >= AUTO_MIN_CONN_FRAMES]
    if not conns:
        raise ApiError(f"音频指纹无有效连接段(每段至少 {AUTO_MIN_CONN_FRAMES} 帧"
                       f"≈{AUTO_MIN_CONN_FRAMES * hop:.0f}s, 全部段过短)")

    # b) 回放指纹(dash 音轨优先, mp4 兜底, 带缓存)
    fp_replay, src_desc = await get_replay_fingerprint(
        session, bv, page, hop, win, local_audio=local_audio)

    # c) 每个连接段与回放互相关(ref=回放, query=直播段)。
    # 整晚单段(数小时)远长于回放(几十分钟)时, 对段滑窗: 窗=回放的 75%, 步进=半窗,
    # 逐窗对齐取置信度最高的窗(该窗墙钟 = 段首 + 窗偏移*hop)
    results = []
    ref_frames = int(fp_replay.shape[0])
    for seg in conns:
        n = seg["n"]
        if n <= ref_frames:
            windows = [(0, n)]
        else:
            wlen = max(int(ref_frames * 0.75), AUTO_MIN_CONN_FRAMES)
            step = max(wlen // 2, 1)
            windows = [(s, min(s + wlen, n)) for s in range(0, n - wlen + 1, step)]
        for (a, b) in windows:
            sub = seg["frames"][a:b]
            try:
                r = await asyncio.to_thread(audiofp.align, fp_replay, sub, hop)
            except ValueError as e:           # 窗比回放长等
                print(f"[autocalign] 段 {seg['c']} 窗[{a}:{b}] 跳过: {e}", flush=True)
                continue
            if math.isfinite(r["offset_sec"]) and r["best_score"] > 0:
                results.append({"conn": seg["c"],
                                "t0_ms": seg["t0_ms"] + int(a * hop * 1000), "n": b - a,
                                "offset_in_replay": r["offset_sec"],
                                "confidence": r["confidence"]})
    if not results:
        raise ApiError("直播指纹与回放对齐失败(无有效互相关结果)")

    # d) 取置信度最高段算最终 offset(与前端手动校准同一 anchor 语义)
    results.sort(key=lambda r: r["confidence"], reverse=True)
    best = results[0]
    # conf 的唯一正确性闸门。注意 inf 必须被视为"不可判定"而非"极高置信":
    # align 在次峰<=0(整段只有一个峰, 常见于片段长度接近回放长度或大量静音)时返回 inf,
    # 旧实现 min(inf,999)=999 会直接放行 —— 实测纯随机噪声在接近等长时也能拿到 inf/5.5。
    # 因此: inf -> 记为不可判定并拒绝; 同时要求匹配时长足够长才采信。
    raw_conf = best["confidence"]
    if not math.isfinite(raw_conf):
        raise ApiError("自动校准无法给出可信结果(互相关只有单一峰, 无法判定置信度), "
                       "已拒绝写入(请用密度图人工校准)")
    conf = min(raw_conf, 999.0)
    if conf < AUTO_MIN_CONFIDENCE:
        raise ApiError(f"对齐置信度过低({conf:.2f} < {AUTO_MIN_CONFIDENCE}), "
                       f"已拒绝写入(建议人工校准)")
    matched_seconds = best["n"] * hop
    if matched_seconds < AUTO_MIN_MATCH_SEC:
        raise ApiError(f"参与对齐的音频过短({matched_seconds:.0f}s < {AUTO_MIN_MATCH_SEC:.0f}s), "
                       f"置信度不可靠, 已拒绝写入(建议人工校准)")
    # anchor 与 handle_match_get 同源: 聚合数据首行 ts 优先, meta 兜底(跨零点/未finalize 场景)
    anchor_ms = first_ts_from_data(date, file)
    if not anchor_ms:
        try:
            anchor_ms = compute_anchor_ms(load_meta(date, file))
        except ApiError:
            anchor_ms = 0
    offset_sec = (best["t0_ms"] - anchor_ms) / 1000.0 - best["offset_in_replay"]
    auto_meta = {"confidence": round(conf, 3), "conn": best["conn"],
                 "method": "audiofp",
                 "ts": datetime.now().isoformat(timespec="seconds")}
    key = f"{date}|{file}"

    # e) 写回 matches.json: 锁内 read-modify-write, 只更新该 key 的 offset_sec + auto
    def _mut(matches):
        entry = dict(matches["align"].get(key) or {})
        entry.update({"date": date, "file": file, "bv": bv,
                      "offset_sec": round(offset_sec, 3)})
        entry["auto"] = auto_meta
        matches["align"][key] = entry
        # 同步场次列表: 按 bv 全局匹配(lists 的日期键可能与会话日期不同, 如跨零点/赛程日)
        for day_list in matches.get("lists", {}).values():
            if not isinstance(day_list, list):
                continue
            for item in day_list:
                if isinstance(item, dict) and str(item.get("bv") or "") == bv:
                    item["offset_sec"] = round(offset_sec, 3)
                    item["auto"] = auto_meta

    if not await asyncio.to_thread(update_matches, _mut):
        raise ApiError("matches.json 写入失败")

    elapsed = time.monotonic() - t0
    message = (f"音频指纹对齐完成: 连接段 {best['conn']}/{len(conns)}, "
               f"匹配直播音频 {matched_seconds:.0f}s, 回放指纹来源 {src_desc}, "
               f"耗时 {elapsed:.1f}s")
    print(f"[autocalign] {date}/{file} bv={bv} P{page}: {message}")
    return {"offset_sec": round(offset_sec, 3), "confidence": round(conf, 3),
            "matched_seconds": round(matched_seconds, 1),
            "conn": f"{best['conn']}/{len(conns)}",
            "elapsed_sec": round(elapsed, 1), "message": message}


async def handle_autocalign(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        raise ApiError("请求体不是有效的 JSON")
    if not isinstance(body, dict):
        raise ApiError("请求体应为 JSON 对象")
    date = str(body.get("date") or "").strip()
    file = str(body.get("file") or "").strip()
    bv = str(body.get("bv") or "").strip()
    if not date or not file:
        raise ApiError("缺少字段 date 或 file")
    if not DATE_RE.fullmatch(date):
        raise ApiError("字段 date 非法(应为 YYYY-MM-DD)")
    if not FILE_RE.fullmatch(file):
        raise ApiError("字段 file 非法(只允许字母/数字/下划线/连字符)")
    if not BV_RE.fullmatch(bv):
        raise ApiError("字段 bv 非法(应形如 BV1xx411c7mD)")
    try:
        page = int(body.get("page") or 1)
    except (TypeError, ValueError):
        raise ApiError("字段 page 应为整数")
    if page < 1:
        raise ApiError("字段 page 应 >= 1")
    result = await run_autocalign(date, file, bv, page, request.app["http"])
    return api_ok(**result)


# ---- 静态托管 web/ ----
def resolve_web_file(rel: str):
    """把 URL 相对路径安全映射到 web/ 内的文件, 越界或不存在返回 None。"""
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    candidate = WEB_DIR.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(WEB_DIR.resolve())
    except (ValueError, OSError):
        return None
    return resolved if resolved.is_file() else None


async def serve_web_file(rel: str) -> web.Response:
    path = resolve_web_file(rel)
    if path is None:
        raise web.HTTPNotFound(text="404: 文件不存在")
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise web.HTTPNotFound(text="404: 文件读取失败")
    resp = web.Response(body=body)
    resp.headers["Content-Type"] = (MIME_MAP.get(path.suffix.lower())
                                    or mimetypes.guess_type(path.name)[0]
                                    or "application/octet-stream")
    resp.headers["Cache-Control"] = "no-cache"  # 本地开发页, 禁缓存避免改版不生效
    return resp


async def handle_index(request: web.Request) -> web.Response:
    return await serve_web_file("index.html")


async def handle_static(request: web.Request) -> web.Response:
    return await serve_web_file(request.match_info.get("tail", ""))


# ---- 中间件: API 出错一律返回 JSON, 不抛 500 ----
@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        return api_err(e.msg)
    except asyncio.CancelledError:
        raise
    except web.HTTPException:
        if request.path.startswith("/api/"):
            return api_err(f"接口不存在或方法不允许: {request.path}")
        raise
    except Exception as e:
        traceback.print_exc()
        if request.path.startswith("/api/"):
            return api_err(f"服务器内部错误: {e.__class__.__name__}: {e}")
        raise


# ---- 应用装配与入口 ----
async def _http_client_ctx(app: web.Application):
    async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)) as session:
        app["http"] = session
        yield


def _open_browser() -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:                     # 无浏览器等异常: 忽略, 用户可手动打开
        pass


async def _startup_open_browser(app: web.Application) -> None:
    # on_startup 早于端口绑定, 延迟 1 秒确保监听已就绪再打开浏览器
    if os.environ.get("NO_BROWSER") != "1":   # 测试环境不弹浏览器
        asyncio.get_running_loop().call_later(1.0, _open_browser)


def build_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app.cleanup_ctx.append(_http_client_ctx)
    app.on_startup.append(_startup_open_browser)
    r = app.router
    r.add_get("/", handle_index)
    r.add_get("/index.html", handle_index)
    r.add_get("/api/days", handle_days)
    r.add_get("/api/danmaku", handle_danmaku)
    r.add_get("/api/match", handle_match_get)
    r.add_post("/api/match", handle_match_post)
    r.add_get("/api/matchlist", handle_matchlist_get)
    r.add_get("/api/overview", handle_overview)
    r.add_get("/api/discover", handle_discover)
    r.add_get("/api/playurl", handle_playurl)
    r.add_get("/api/stream", handle_stream)
    r.add_post("/api/autocalign", handle_autocalign)
    r.add_get("/{tail:.*}", handle_static)     # 兜底静态路由, 必须最后注册
    return app


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print()
    print("=" * 60)
    print("  弹幕延时直播 · 本地服务器")
    print(f"  访问地址:     http://127.0.0.1:{PORT}")
    print(f"  采集数据目录: {DATA_DIR}")
    print(f"  B站Cookie:    {'已加载' if load_cookie_header() else '未配置(可选)'}")
    print("  停止: 按 Ctrl+C")
    print("=" * 60)
    web.run_app(build_app(), host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
