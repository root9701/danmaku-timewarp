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
BASE_DIR = Path(__file__).resolve().parent                  # danmaku-server/
WEB_DIR = BASE_DIR / "web"                                  # 前端静态目录
MATCHES_PATH = BASE_DIR / "matches.json"                    # 弹幕-视频匹配配置
COOKIES_PATH = BASE_DIR / "cookies.txt"                     # 可选: B站 Cookie
CONFIG_PATH = Path(os.environ.get("DANMAKU_CONFIG", "").strip()
                   or BASE_DIR / "config.json")             # 可选的用户配置(不存在则全用默认)


def _load_config(path: Path) -> dict:
    """读 config.json。不存在/损坏都返回 {} —— 配置是**可选**的, 缺了必须能正常启动。"""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[config] 读取失败(用默认值继续): {e}", flush=True)
        return {}
    return raw if isinstance(raw, dict) else {}


CFG = _load_config(CONFIG_PATH)


def _cfg(*keys, default=None):
    """按路径取配置: _cfg("discover", "league_id", default="...")。任一层缺失即返回默认值。"""
    cur = CFG
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else cur


# 8788 已被本机常驻的 node 服务占用(实测 bind 报 10048), 默认换 8789
PORT = int(_cfg("port", default=8789))
# 监听地址。默认只绑本机; 想让手机/iPad 访问就把 config.json 的 host 改成 "0.0.0.0"
# (或设环境变量 DANMAKU_HOST=0.0.0.0)。注意本服务无登录鉴权, 只在可信网络这样用。
HOST = (os.environ.get("DANMAKU_HOST", "").strip()
        or str(_cfg("host", default="127.0.0.1")))
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
AUTO_MIN_CONFIDENCE = float(_cfg("align", "min_confidence", default=2.0))
AUTO_MIN_CONN_FRAMES = 50   # 连接段指纹至少 50 帧(约 10s @hop0.2)才参与对齐
AUTO_MAX_OFFSET_SPREAD_SEC = 0.5  # 多个时间窗对齐出的 offset 允许的最大离散(秒)
#   实测正常对齐时各窗离散为 0.0s(G3 三窗全 0.0), 因此 0.5s 已经非常宽松;
#   超过它说明各窗匹配到了不同位置(伪峰/该时段根本不是这一局), 必须拒绝。
AUTO_SEQUENCE_WINDOW_SEC = 1800   # 同一场次相邻分P 的最大合理间隔(30 分钟)
#   赛后采访/赛前评论这类配件紧贴正赛, 不会隔几小时。用它把"紧邻候选"和其他时段的
#   伪峰分开(实测 P2 赛后采访: 正确候选在 P1 结束后 0 分钟, 伪峰在 5~6 小时后)。
AUTO_VOTE_SEGMENTS = 8            # 对齐时把回放切成几段去做投票
#   切成短段比整段对齐可靠: 长篇里混进一段直播没播的内容, 整窗得分就被拉平。
#   实测同一局比赛, 全长对齐 conf=1.01(判为不匹配), 3 分钟短段逐段对齐 conf 高到 5~9。
AUTO_VOTE_TOLERANCE_SEC = 30      # 投票时"指向同一处"的容差(秒)
#   30 秒足够吸收不同片段算出的亚秒级偏差, 又远小于相邻两场比赛的间隔(几十~上百分钟)。
# 参与对齐的音频时长下限: 测过纯随机噪声在"片段长度接近回放长度"时可达 conf 2.5~5.5,
# 光靠置信度挡不住。真实比赛对齐通常能匹配几十秒以上, 故再要求匹配时长足够。
AUTO_MIN_MATCH_SEC = 20.0
# 赛程开始时间与实际开播的容差: 用 Riot/Riot 官方赛程的 startTime 作为"这场应该在哪个时段"
# 的锚点, 前后各放开一段, 用来排除直播间早上的重播/回顾(音频与官方回放完全一致, 投票也会
# 一致命中)。实测 Bo3 前一场提前结束后, 后一场比赛程早开 70 分钟, 故前向要放宽;
# 后向按"整场时长 + 30 分钟收尾"算, 不需要额外放宽。
AUTO_SCHEDULE_LEAD_SEC = 5400      # 允许比赛程提前 90 分钟开播
AUTO_SCHEDULE_TAIL_SEC = 1800      # 整场时长之后再多给 30 分钟(赛后采访/抽签等)

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
    sess = resolve_session_dir(date, file)
    if sess is not None:
        # 会话目录里的 meta 名不一定等于目录推导名(测试会话是 test_HHMMSS_{room}.meta.json),
        # 直接找目录内唯一的 meta; 优先用 manifest 里登记的 meta 名。
        mf = read_manifest(sess)
        if mf and mf.get("meta"):
            cand = sess / mf["meta"]
            if cand.is_file():
                return cand
        cands = sorted(sess.glob("*.meta.json"))
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

# ---- 目录布局(三代共存, 读取层全部兼容) ----
# v1 平铺:   data/{日期}/{room}_sHHMM.jsonl.gz           一个目录混多个会话(最早)
# v2 会话目录: data/{日期}/{日期}_{HHMM}_{room}/         一个目录一个会话(过渡)
# v3 现行:   data/{赛事日}/{room}/ + manifest.json        时间由 manifest 表达, 目录最简
#            测试数据: data/_test/{ts}_{room}/
# 服务端一律"优先读 manifest", 读不到再退化为目录扫描, 保证按 v1/v2 写的老数据仍可播。
SESSION_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(\d+)(?:$|_test_(\d{6}))$")
# v3 会话目录名: 纯房间号(正式) 或 {日期}T{HHMM}_{room}(测试)
V3_SESSION_RE = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})T(\d{4})_)?(\d+)$")
TEST_ROOT = "_test"
MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


def _looks_like_session_dir(p: Path) -> bool:
    """是否为"会话目录"(v2 或 v3)。目录里必须有 manifest 或弹幕/afp 文件。"""
    if not p.is_dir() or p.name.startswith("."):
        return False
    if not (SESSION_DIR_RE.fullmatch(p.name) or V3_SESSION_RE.fullmatch(p.name)):
        return False
    try:
        return (p / MANIFEST_NAME).is_file() or any(
            q.is_file() and (".jsonl" in q.name) for q in p.iterdir())
    except OSError:
        return False


def read_manifest(sess_dir: Path):
    """读会话目录里的 manifest.json。不存在或损坏返回 None(调用方退化为目录扫描)。"""
    p = sess_dir / MANIFEST_NAME
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    ver = m.get("schema_version")
    if not isinstance(ver, int) or ver < 1:
        return None
    return m


def _day_dirs(date: str) -> list:
    """该日期下所有会话目录。兼容 v2(名字带日期)/v3(纯房间号)两种命名。"""
    day_dir = DATA_DIR / date
    if not day_dir.is_dir():
        return []
    try:
        return sorted([p for p in day_dir.iterdir() if _looks_like_session_dir(p)],
                      key=lambda p: p.name)
    except OSError:
        return []


def _test_dirs() -> list:
    """测试会话目录(data/_test/ 下)。"""
    root = DATA_DIR / TEST_ROOT
    if not root.is_dir():
        return []
    try:
        return sorted([p for p in root.iterdir() if _looks_like_session_dir(p)],
                      key=lambda p: p.name)
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



def resolve_session_dir(date: str, file: str):
    """把 (date, file) 解析成会话目录。找不到返回 None(绝不做模糊兜底)。

    file 的取值可能是:
      * v3 纯房间号          "7734200"                 -> {date}/7734200 或 _test/ 下
      * v3 测试会话名        "2026-10-26T2023_7734200" -> data/_test/{file}
      * v2 会话目录名        "2026-10-25_2332_7734200" -> {date}/{file}
      * v1 旧平铺主名        "7734200"(散文件在日目录根) -> 不是目录, 返回 None

    为什么只在指定日期下找, 不做"任意日期兜底":
    当一个房间多天都有数据时(2026-10-25/7734200 与 2026-10-26/7734200 同时存在),
    遍历所有日期会**静默返回错误那天**的数据 —— 用户以为在看 24 日的比赛, 实际放的是
    25 日的。这种错误不报错、只是内容不对, 比直接找不到更难发现。
    """
    # v2: 名字里带日期 + 时分
    if SESSION_DIR_RE.fullmatch(file):
        p = DATA_DIR / date / file
        return p if p.is_dir() else None
    # v3: 纯房间号(正式) / {日期}T{HHMM}_{room}(测试)
    m = V3_SESSION_RE.fullmatch(file)
    if m:
        if m.group(1):        # 测试会话: 固定在 _test/ 下
            p = DATA_DIR / TEST_ROOT / file
            return p if p.is_dir() else None
        p = DATA_DIR / date / file
        return p if p.is_dir() else None
    return None


def danmaku_paths(date: str, file: str) -> list[Path]:
    """主名 file(纯房间号或 test_HHMMSS_{room}, 或会话目录名) 归并全部同前缀产物:
      {file}.jsonl.gz(旧格式) + {file}_runN.jsonl.gz + {file}_s????.jsonl.gz(滚动切片)
      + {file}_s????.jsonl(活动 raw 未压缩, 采集进行中, 排最后);
    file 显式带 _runN/_sHHMM 后缀时只读那一个文件(gz 优先, 活动 raw 兜底)。
    glob 后用正则 fullmatch 严格匹配主名边界, 防前缀撞号(7734200 误吞 77342000 的片)。

    **两种布局都支持**:
      * 新: file 形如 "2026-10-25_2332_7734200"(会话目录名) -> 只读该子目录;
      * 旧: file 是纯房间号 -> 在日目录平铺层(旧数据) + 各会话子目录里按主名找。"""
    # 会话目录布局(v2/v3): file 直接指向一个会话目录。目录里放什么文件名取决于采集那次
    # 会话的命名(正式 {room}_sHHMM / 测试 test_HHMMSS_{room} / 历史迁移的 {room}_day0719),
    # 所以**不能**用目录名推导基础名去匹配 —— 那样多数会话会匹配到 0 个文件(实测 3/4 读不出)。
    # 正确做法: 收集目录内所有弹幕数据片(排除 afp/meta)。
    sess = resolve_session_dir(date, file)
    if sess is not None:
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
    """会话目录名 -> 里面文件的基础名。

    v2 正式: {日期}_{HHMM}_{room}         -> room
    v2 测试: {日期}_test_{HHMMSS}_{room}  -> test_{HHMMSS}_{room}
    v3 正式: {room}                       -> room
    v3 测试: {日期}T{HHMM}_{room}         -> test_{HHMM}_{room}(仅作展示, 读文件不依赖它)
    """
    m = SESSION_DIR_RE.fullmatch(name)
    if m:
        room, tstamp = m.group(3), m.group(4)
        return f"test_{tstamp}_{room}" if tstamp else room
    m = V3_SESSION_RE.fullmatch(name)
    if m:
        stamp, room = m.group(2), m.group(3)
        return f"test_{stamp}_{room}" if stamp else room
    return name


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
    """会话目录 -> 比赛日(赛事日)。

    v3 布局: manifest 里直接写了 match_date(采集器用"结束时刻所在日期"算的), 优先用它。
    v2 布局: 目录名前缀是"数据日", 不是比赛日; 用**数据本身**判定 —— 首条 ts 落在
    00:00~11:00 说明这是"昨晚开打、凌晨结束"的比赛, 比赛日 = ts 的日期。
    都读不到时退回目录名推断。
    """
    mf = read_manifest(sess_dir)
    if mf and mf.get("match_date"):
        return str(mf["match_date"])
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
    # 无数据可读: 退化为目录名推断。v3 目录名里没有日期, 用父目录名(赛事日);
    # v2 目录名带 {日期}_{HHMM}, 凌晨开录的会话比赛日是次日。
    m = SESSION_DIR_RE.fullmatch(sess_dir.name)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        hh = int(m.group(2)[:2])
        return (d + timedelta(days=1)).strftime("%Y-%m-%d") if hh < 11 else m.group(1)
    return sess_dir.parent.name


def _session_time_range(date: str, file: str, sess_dir=None):
    """会话真实起止时间(毫秒墙钟), 返回 (t0, t1); 无数据返回 (None, None)。

    优先用 manifest 里采集器写好的 start_ms/end_ms(权威, 不需要扫盘);
    没有 manifest 时才扫数据片。**不要**用 meta.record_start/record_end:
    实测主会话的 meta 只覆盖最后一次收尾的 15 分钟(281 条), 而目录里有 20 万条、
    9.4 小时的数据 —— 拿 meta 当会话时间会严重错位。

    扫盘时的精度做法: **每个数据片**都读头部若干行取最小 ts、尾部若干行取最大 ts,
    再取全片集合的最小/最大。不能只看首尾片 —— 文件名 `_sHHMM` 跨零点后字典序
    不等于时间序(_s2332 排在 _s0845 之后), 只看首尾会漏掉真实边界。
    """
    if sess_dir is not None:
        mf = read_manifest(sess_dir)
        if mf:
            s, e = mf.get("start_ms"), mf.get("end_ms")
            if isinstance(s, int) and isinstance(e, int) and s and e:
                return s, e
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


def _room_from_dirname(name: str):
    """从会话目录名里解析房间号。兼容 v2({日期}_{HHMM}_{room}) 与 v3({room} / {ts}_{room})。"""
    m = SESSION_DIR_RE.fullmatch(name)
    if m:
        return int(m.group(3))
    m = V3_SESSION_RE.fullmatch(name)
    if m:
        return int(m.group(3))
    return None


def _entry_from_manifest(sess: Path, mf: dict, is_test: bool) -> dict:
    """用 manifest 构造一个会话条目(权威信息, 不必再扫文件)。"""
    dm_count = int(mf.get("dm_count") or 0)
    return {
        "name": sess.name,
        "room": int(mf.get("room") or 0),
        "is_test": bool(is_test),
        "dm_count": dm_count,
        "layout": "session",
        "schema_version": mf.get("schema_version"),
        "start_ms": mf.get("start_ms"),
        "end_ms": mf.get("end_ms"),
        "part_names": [f for f in (mf.get("files") or [])],
        "has_manifest": True,
    }


def scan_days() -> list:
    """列出数据目录下的会话。**优先读 manifest**, 读不到退化为目录扫描(兼容老数据)。

    返回结构(前端只依赖这里, 不解析路径):
      {days: [{date, match_date, data_date, rooms: [{room, files: [条目]}]}]}
    条目: {name, room, is_test, dm_count, start_ms, end_ms, layout, ...}
    """
    days = []
    if not DATA_DIR.is_dir():
        return days
    for day_dir in sorted(DATA_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not (day_dir.is_dir() and DATE_RE.fullmatch(day_dir.name)):
            continue
        rooms = {}   # room_id -> {"entries": [条目], "agg": {主名: 切片dm估算和}}
        # 待扫描容器: 旧平铺层(日目录自身) + 各会话子目录(v2/v3)
        containers = []
        if _day_flat_has_files(day_dir.name):
            containers.append(day_dir)
        containers.extend(_day_dirs(day_dir.name))
        for cont in containers:
            is_session_dir = cont is not day_dir
            mf = read_manifest(cont) if is_session_dir else None
            if mf is not None:
                # ---- manifest 路径: 直接用权威数据, 不再扫描 ----
                e = _entry_from_manifest(cont, mf, bool(mf.get("is_test")))
                rid = e["room"]
                if not rid:
                    continue
                rooms.setdefault(rid, {"entries": [], "agg": {}})["entries"].append({
                    "name": e["name"], "is_test": e["is_test"],
                    "dm_count": e["dm_count"], "_base": e["name"],
                    "_dir": cont.name, "_session": True,
                    "_start_ms": e["start_ms"], "_end_ms": e["end_ms"],
                    "_schema": e["schema_version"],
                })
                continue
            # ---- 无 manifest: 目录扫描(老数据) ----
            paths = list(cont.glob("*.jsonl.gz")) + list(cont.glob("*.jsonl"))
            for p in sorted(paths, key=lambda x: x.name):
                if p.name.endswith(".jsonl.gz"):
                    name = p.name[: -len(".jsonl.gz")]
                else:
                    name = p.name[: -len(".jsonl")]   # 活动切片 raw
                if name.endswith(".afp"):             # 音频指纹, 不是弹幕
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
                        # 房间号以**目录名**为准。
                        room_id = _room_from_dirname(cont.name)
                        if not room_id:
                            continue
                    else:
                        continue
                room = rooms.setdefault(room_id, {"entries": [], "agg": {}})
                entry_name = cont.name if is_session_dir else name
                if SLICE_RE.search(RUN_RE.sub("", name)):
                    # _s???? 滚动切片: 聚合进主条目, 不单列
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
            for name in sorted(room["agg"].keys() - entry_names):
                # 注意: 不能光靠"名字像会话目录"来判断 —— v3 的正式会话名就是纯房间号,
                # 而旧平铺数据的主名同样是纯房间号, 正则无法区分。
                # 只有**目录真实存在**才算会话目录, 否则是旧平铺条目。
                is_sess = resolve_session_dir(day_dir.name, name) is not None
                room["entries"].append({
                    "name": name,
                    "is_test": ("test_" in name) if is_sess else name.startswith("test_"),
                    "dm_count": 0, "_base": _base_of_session_dir(name) if is_sess else name,
                    "_dir": name if is_sess else "", "_session": is_sess})
            for e in room["entries"]:
                agg_key = e["name"] if e["_session"] else e["_base"]
                if agg_key in room["agg"] and not e["dm_count"]:
                    e["dm_count"] = room["agg"][agg_key]
            room["entries"].sort(key=lambda x: (x["is_test"], x["name"]))
            files = []
            for x in room["entries"]:
                item = {"name": x["name"], "is_test": x["is_test"],
                        "dm_count": x["dm_count"],
                        "layout": "session" if x["_session"] else "flat"}
                if x.get("_schema") is not None:
                    item["schema_version"] = x["_schema"]
                files.append(item)
            out_rooms.append({"room": rid, "files": files})
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
    # ---- 测试会话(data/_test/) ----
    # 单独归到 date="_test" 的一组里。默认不混进正式日期列表(避免误选测数据当正赛),
    # 但必须**可见** —— 否则测试期间录的东西永远找不到, 也没法回放验证。
    test_entries = []
    for td in _test_dirs():
        mf = read_manifest(td)
        if mf is not None:
            room_id = int(mf.get("room") or 0) or _room_from_dirname(td.name) or 0
            dm_count = int(mf.get("dm_count") or 0)
        else:
            room_id = _room_from_dirname(td.name) or 0
            dm_count = sum(count_dm_lines(p) for p in _collect_all_in_session(td))
        if not room_id:
            continue
        t0, t1 = _session_time_range("", td.name, td)
        test_entries.append({
            "name": td.name, "room": room_id, "is_test": True,
            "dm_count": dm_count, "layout": "session",
            "schema_version": (mf or {}).get("schema_version"),
            "start_ms": t0, "end_ms": t1,
        })
    if test_entries:
        rooms = {}
        for e in test_entries:
            rooms.setdefault(e["room"], {"room": e["room"], "files": []})["files"].append({
                "name": e["name"], "is_test": True, "dm_count": e["dm_count"],
                "layout": "session", "schema_version": e["schema_version"],
                "start_ms": e["start_ms"], "end_ms": e["end_ms"],
            })
        days.append({"date": TEST_ROOT, "match_date": "", "data_date": TEST_ROOT,
                     "is_test_group": True,
                     "rooms": [r for _, r in sorted(rooms.items())]})
    # 给每个会话目录补上"比赛日/赛事日"。v3 布局下目录名本身就是赛事日(采集器按
    # "会话结束时刻"命名), 直接采用; 老布局才需要用数据推断。
    for d in days:
        if d.get("is_test_group"):
            continue          # 测试组没有"比赛日"概念
        d["match_date"] = _session_match_date(d["date"])
        d["data_date"] = d["date"]      # 显式给出"数据日", 供前端显示「比赛日 + 数据日」
        # 每个会话补真实起止时间(毫秒墙钟): 前端据此显示时段并排序。
        # 取不到(如只有 afp 没有弹幕)就留空, 前端会跳过该会话。
        for room in d["rooms"]:
            for f in room["files"]:
                sess = resolve_session_dir(d["date"], f["name"])
                t0, t1 = _session_time_range(d["date"], f["name"], sess)
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
    # 可选 bv: 带上时返回该场比赛的**逐分P 对齐结果**, 供前端做分段映射。
    # 前端播放时每个分P 有各自的 offset(局间休息长度直播侧与回放侧不同), 只靠标量
    # offset 会让第二局起的弹幕整体错位(实测 AL 第二局偏差 -213.9s)。
    want_bv = str(request.query.get("bv") or "").strip()
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
    align = load_matches()["align"]
    entry = align.get(f"{date}|{file}") or {}
    offset_sec = entry.get("offset_sec", 0)
    if isinstance(offset_sec, bool) or not isinstance(offset_sec, (int, float)):
        offset_sec = 0
    # title 只作**兜底**展示用: 优先 align 记录里用户选过的场次名(G1 TES vs AL),
    # 绝不能把 meta.room_title(直播间当时的标题, 如"【预告】27号1点CFO vs T1")当比赛名,
    # 那正是"左上角比赛信息和实际选中的比赛不一致"的根因之一。
    title = str(entry.get("title") or "")

    # 逐分P 对齐结果: 精确 key 优先, 再回退按 bv 扫(兼容只存过一场的旧记录)
    pages_out, pages_bv = {}, ""
    target_bv = want_bv
    if not target_bv:
        # 没传 bv: 用会话级汇总里记录的第一个 bv
        bvs = entry.get("bvs")
        if isinstance(bvs, list) and bvs:
            target_bv = str(bvs[0] or "")
    if target_bv:
        rec = align.get(f"{date}|{file}|{target_bv}")
        if not (isinstance(rec, dict) and isinstance(rec.get("pages"), dict)):
            rec = None
            for k, v in align.items():
                if (isinstance(v, dict) and str(v.get("bv") or "") == target_bv
                        and isinstance(v.get("pages"), dict) and v["pages"]):
                    if k.count("|") >= 2:
                        rec = v
                        break
                    if rec is None:
                        rec = v
        if isinstance(rec, dict) and isinstance(rec.get("pages"), dict):
            pages_out = rec["pages"]
            pages_bv = target_bv

    return api_ok(anchor_ms=anchor_ms, offset_sec=offset_sec,
                  bv=str(entry.get("bv") or "") or pages_bv, title=title,
                  pages=pages_out, pages_bv=pages_bv,
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
    # v3 布局: 每个会话目录带 manifest, 直接读 match_date(采集器按"结束时刻"算的)。
    # 一个日期目录下若有多个正式会话, 取最晚的那个作该日的比赛日。
    best = None
    for sd in _day_dirs(date):
        if "test_" in sd.name:      # 测试会话不代表当晚正赛
            continue
        md = _session_dir_match_date(sd)
        if md and (best is None or md > best):
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
def _pick_exact_align_rec(align: dict, date: str, bv: str):
    """取某场比赛的精确对齐记录(key = `{date}|{file}|{bv}`, 该日可能有多个 session)。

    matchlist 只有 date 没有 file(文件名要到 /api/days 之后才定), 因此按 `{date}|*|{bv}`
    匹配。同一 BV 同一天理论上只有一条; 若有多条(目录被合并过), 取 pages 最多的那条 ——
    那是信息最完整的记录, 也就是真正跑过全部分P 对齐的那次。
    """
    prefix = f"{date}|"
    suffix = f"|{bv}"
    best = None
    best_n = -1
    for k, v in align.items():
        if not k.startswith(prefix) or not k.endswith(suffix):
            continue
        if k.count("|") < 2 or not isinstance(v, dict):
            continue
        pages = v.get("pages")
        if not isinstance(pages, dict) or not pages:
            continue
        n = len(pages)
        if n > best_n:
            best, best_n = v, n
    return best


async def handle_matchlist_get(request: web.Request) -> web.Response:
    date = (request.query.get("date") or "").strip()
    if not DATE_RE.fullmatch(date):
        raise ApiError("参数 date 非法(应为 YYYY-MM-DD)")
    lists = load_matches()["lists"]
    align = load_matches()["align"]
    match_date = _session_match_date(date)
    matches = _matchlist_for_session(date, lists)
    # 旧数据的标题带 "G1 " 前缀, 展示前统一剥掉(不改存储, 保留原始数据)
    fixed = []
    for it in (matches or []):
        if not isinstance(it, dict):
            fixed.append(it)
            continue
        it = dict(it)
        it["title"] = _strip_game_prefix(it.get("title"))
        # 合并该场自己的对齐状态: 前端据此显示「已对齐 N 个分P / 未对齐」。
        # **精确 key 优先**: {date}|{file}|{bv}。旧实现直接遍历 align 字典取第一条
        # bv 命中的记录, 而同一 BV 可能残留多条(例如数据目录被合并改名后, 旧目录名的
        # 记录还在), 字典顺序一旦先撞上残留记录就会读到错的 pages 数 ——
        # 实测 TSW vs LOS 实际对齐了 2 个分P, 下拉框却显示「已对齐」(=1 个)。
        bv = str(it.get("bv") or "")
        rec = None
        if bv:
            exact = _pick_exact_align_rec(align, date, bv)
            if exact is not None:
                rec = exact
            if rec is None:
                # 回退: 先找带 bv 维度的记录, 再退回任意 bv 命中的旧记录
                for k, v in align.items():
                    if not isinstance(v, dict) or str(v.get("bv") or "") != bv:
                        continue
                    if k.count("|") >= 2 and v.get("pages"):
                        rec = v
                        break
                    if rec is None:
                        rec = v
        if rec:
            if rec.get("pages"):
                it["pages"] = rec["pages"]
            if rec.get("page_errors"):
                it["page_errors"] = rec["page_errors"]
            # 对齐时 B 站实际有几个分P(用于显示"已对齐 1/2")。
            # 不在这里实时请求 bili_view: matchlist 必须毫秒级返回(前端下拉框依赖它),
            # 而分P 总数在对齐时就已经知道了, 直接落盘复用即可。
            if isinstance(rec.get("total_pages"), int):
                it["total_pages"] = rec["total_pages"]
            elif it.get("pages"):
                it["total_pages"] = len(it["pages"])
            # 失败标记只在**完全没有对齐结果**时才对外报。
            # 历史缺陷: 一旦某次全失败写过 align_failed, 之后即便对齐成功、pages 已有内容,
            # 下拉框仍显示「未对齐·直播可能未转播」—— 用户会以为功能坏了。
            if rec.get("align_failed") and not rec.get("pages"):
                it["align_failed"] = True
                it["align_error"] = rec.get("align_error") or ""
        fixed.append(it)
    # match_date 一并返回: 前端据此显示"比赛日", 并作为 /api/discover 的日期
    return api_ok(matches=fixed, match_date=match_date)


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

# 赛事与时区/时间窗口都可配置(见 config.json 的 discover 段)。
# 默认值对应"世界赛 + 北京时间凌晨到上午", 换赛区/常规赛改配置即可, 不必改代码。
RIOT_LEAGUE_ID = str(_cfg("discover", "league_id", default="98767975604431411"))
RIOT_SCHEDULE_URL = ("https://esports-api.lolesports.com/persisted/gw/getSchedule"
                     f"?hl=en-US&leagueId={RIOT_LEAGUE_ID}")
DISCOVER_ENABLED = bool(_cfg("discover", "enabled", default=True))
BILI_OFFICIAL_MID = int(_cfg("bili", "official_mid", default=50329118))
NET_TIMEOUT = 8                       # 秒, 对 Riot/B站 的单次请求超时


def _parse_hhmm(s, default):
    """把 "01:00" 解析成 (1, 0); 非法则用默认值。"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else default


def _load_tz(name):
    """按 IANA 名称加载时区。

    Windows 上 zoneinfo 需要 tzdata 包, 没装会抛 ZoneInfoNotFoundError。
    这里**必须打警告而不是静默退回**: 时区错了会让赛程窗口整体偏移几小时,
    表现是"明明有比赛却一场都没发现", 很难排查。宁可吵一点。
    """
    want = str(name or "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(want)
    except Exception as e:
        print(f"[config] 时区 {want!r} 加载失败({e.__class__.__name__}), "
              f"退回固定 UTC+08:00。若配置的不是东八区, 赛程窗口会整体偏移; "
              f"Windows 请先 `pip install tzdata`", flush=True)
        return timezone(timedelta(hours=8))


DISCOVER_TZ = _load_tz(_cfg("discover", "timezone", default="Asia/Shanghai"))
DISC_W0 = _parse_hhmm(_cfg("discover", "window_start", default="01:00"), (1, 0))
DISC_W1 = _parse_hhmm(_cfg("discover", "window_end", default="09:00"), (9, 0))
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
    双方均 TBD 的占位块跳过(无回放匹配价值)。

    标题直接用**战队名**(如 "HLE vs IG"), 不加 "G1/G2" 前缀 —— 序号是赛程里的位置,
    既可能因改期/加赛变化, 对用户也没有信息量; 对阵双方才是识别一场比赛的依据。
    旧数据里的 "G1 HLE vs IG" 由 _strip_game_prefix() 在展示时剥掉。
    """
    y, m, d = (int(x) for x in date.split("-"))
    # 窗口可配置(见 config.json discover.window_start/window_end)。
    # 默认 01:00-09:00 对应世界赛的北京时间凌晨档; 换赛区改配置即可。
    # 窗口结束早于开始视为跨零点(如 23:00-06:00), 结束日顺延一天。
    h0, m0 = DISC_W0
    h1, m1 = DISC_W1
    win0 = datetime(y, m, d, h0, m0, tzinfo=DISCOVER_TZ)
    win1 = datetime(y, m, d, h1, m1, tzinfo=DISCOVER_TZ)
    if win1 <= win0:
        win1 += timedelta(days=1)
    draft = []
    for ev in riot_fetch_schedule():
        local = ev["start"].astimezone(DISCOVER_TZ)
        if not (win0 <= local < win1):
            continue
        c1, c2 = ev["teams"]
        if c1.upper() == "TBD" and c2.upper() == "TBD":
            continue
        draft.append({"title": f"{c1} vs {c2}",
                      "teams": [c1, c2], "start": local})
    return draft


GAME_PREFIX_RE = re.compile(r"^\s*G\d+\s+")
# 已知的"看起来像序号、其实是战队名"的队名(G2 是最典型的例子)。
# 剥 "G1 " 前缀时必须避开它们, 否则 "G2 vs TES"(G2 战队对 TES) 会被剥成 "vs TES"。
_TEAM_NAMES_LOOKING_LIKE_GAME_NO = {"G2", "G3", "G4", "G5"}


def _strip_game_prefix(title: str) -> str:
    """剥掉旧数据标题里的 "G1/G2 " 序号前缀(展示用, 不修改存储)。

    只在**剥完仍是完整对阵**"A vs B"时才剥:
      "G1 HLE vs IG" -> "HLE vs IG"   剥完第一词是 HLE(队名) ✓
      "G4 GEN vs LYON" -> "GEN vs LYON"                        ✓
      "G2 G2 vs TES" -> "G2 vs TES"   剥完第一词是 G2(队名)    ✓
      "G2 vs TES"    -> 原样不动        剥完第一词是 vs -> 说明 G2 本身就是队名, 不能剥

    判据: 剥完后的**第一个词不能是 vs**(那样说明队名被剥掉了)。
    旧写法用 rest 里"有没有 vs"判断, 而 "vs TES" 里也有 vs, 会把 G2 战队
    整场标题剥成 "vs TES"(实测踩过)。
    """
    t = str(title or "").strip()
    m = GAME_PREFIX_RE.match(t)
    if not m:
        return t
    rest = t[m.end():].strip()
    if not rest:
        return t
    first = rest.split()[0]
    if first.lower() == "vs":
        return t          # 剥完就剩 "vs XXX", 说明被剥的那个 G\d+ 是队名
    return rest


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


# 从标题解析两队 code。**不能**要求 "G1 " 前缀: 新数据标题就是 "HLE vs IG",
# 强行要求前缀会让新格式全部解析失败(实测 G2 vs TES / MKOI vs DK 都返回 None),
# 而 "G2 vs TES" 里的 G2 本身就是队名, 去掉前缀逻辑反而更安全。
# 括号备注(旧数据里的"（待确认场次）")由 clean() 剥掉。
_TITLE_TEAMS_RE = re.compile(r"^\s*(?:G\d+\s+)?(\S+)\s+vs\s+(\S+)", re.IGNORECASE)


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
    # c) 写回: 列表缺失/为空 -> 整体写入草稿; 已存在 -> 只回填空 bv/开始时间, 其余字段不动
    result = {"created": 0, "backfilled": 0, "time_filled": 0}

    def _mut(matches):
        lst = matches["lists"].get(date)
        if not lst:
            if draft:
                matches["lists"][date] = [
                    {"title": m["title"], "bv": m["bv"], "offset_sec": 0,
                     "start_ms": int(m["start"].timestamp() * 1000)} for m in draft]
                result["created"] = len(draft)
            return
        for entry in lst:
            # 回填赛程开始时间(按队名匹配)。这是"该场次应该发生在直播的哪一段"的
            # 唯一权威来源: 对齐算法用它排除早上重播/回顾里的伪峰。
            if not isinstance(entry.get("start_ms"), (int, float)):
                teams = _title_teams(entry.get("title"))
                if teams:
                    want0 = {t.lower() for t in teams}
                    for m in draft:
                        if {t.lower() for t in m["teams"]} == want0:
                            entry["start_ms"] = int(m["start"].timestamp() * 1000)
                            result["time_filled"] += 1
                            break
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
            (not str(e.get("bv") or "").strip() and _title_teams(e.get("title")))
            or (not isinstance(e.get("start_ms"), (int, float)) and _title_teams(e.get("title")))
            for e in (probe["lists"].get(date) or []))
        if need:
            if not await asyncio.to_thread(update_matches, _mut):
                raise ApiError("matches.json 写入失败")
    out = [{"idx": i, "title": m["title"], "bv": m["bv"], "found": bool(m["bv"])}
           for i, m in enumerate(draft)]
    return api_ok(matches=out, riot_count=len(draft), date=date, data_date=raw_date,
                  created=result["created"], backfilled=result["backfilled"],
                  time_filled=result["time_filled"])


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

        # 每场比赛: 取它的 BV + 分P 级 offset。分P 的**名称与时长**来自 matches.json
        # 里对齐时记录的信息(pages), 不主动打 B 站接口 —— 总览要秒开。
        tracks = []
        for idx, m in enumerate(lst):
            if not isinstance(m, dict):
                continue
            title = _strip_game_prefix(m.get("title"))
            bv = str(m.get("bv") or "")
            offset = m.get("offset_sec")
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                offset = 0
            pages = {}
            # 按 (date|file|bv) 精确找该场自己的对齐记录(多场共用一个会话, key 必须带 bv)
            precise = align.get(f"{raw_date}|{file}|{bv}") if bv else None
            if isinstance(precise, dict) and isinstance(precise.get("pages"), dict):
                pages = precise["pages"]
                ao = precise.get("offset_sec")
                if isinstance(ao, (int, float)) and not isinstance(ao, bool):
                    offset = float(ao)
            else:
                # 兼容旧记录: 会话级 key 里只存过一场, 或老格式没有 bv 维度
                for akey, aentry in align.items():
                    if not isinstance(aentry, dict):
                        continue
                    if not bv or str(aentry.get("bv") or "") != bv:
                        continue
                    pg = aentry.get("pages")
                    if isinstance(pg, dict) and pg:
                        pages = pg
                        ao = aentry.get("offset_sec")
                        if isinstance(ao, (int, float)) and not isinstance(ao, bool):
                            offset = float(ao)
                    break
            # 分P 段: 每个分P 一个独立 offset(同一场次内各分P 落点不同)
            segs = []
            for pg_no, pv in sorted(pages.items(), key=lambda kv: int(kv[0])):
                if not isinstance(pv, dict):
                    continue
                po = pv.get("offset_sec")
                if isinstance(po, bool) or not isinstance(po, (int, float)):
                    continue
                segs.append({
                    "page": int(pg_no),
                    "part": _strip_game_prefix(pv.get("part") or ("P%s" % pg_no)),
                    "offset_sec": round(float(po), 3),
                    "duration": pv.get("duration"),
                    "confidence": pv.get("confidence"),
                })
            segs.sort(key=lambda s: s["offset_sec"])   # 按真实时间先后排列, 不按 P 序号
            tracks.append({
                "idx": idx, "title": title, "bv": bv,
                "offset_sec": round(float(offset), 3),
                "auto": m.get("auto"),
                "has_bv": bool(bv),
                "parts": segs,
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
    sess = resolve_session_dir(date, file)
    if sess is not None:   # 会话目录布局: afp 名不一定等于目录推导名, 直接扫目录
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


async def bili_dash_audio_url(session, bv: str, cid=None):
    """尝试 dash 纯音轨直链(fnval=16, 优先 30280/192kbps)。拿不到返回 None(不抛错)。

    **必须按 cid 取**: 官方回放把一场比赛拆成多个分P(第一局/赛后采访/赛前评论),
    每个分P 有自己的 cid 和音轨。旧实现只用 info["cid"](恒为第一分P)取音轨,
    导致 P2/P3 拿到的是 P1 的音频, 对齐全错位(实测三个"分P"指纹长度全是 50.5 分钟)。
    cid 为 None 时才回落到第一分P(兼容旧调用)。"""
    try:
        if cid is None:
            info = await bili_view(session, bv)
            cid = info["cid"]
        async with session.get("https://api.bilibili.com/x/player/playurl",
                               params={"bvid": bv, "cid": cid,
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


async def download_dash_audio(session, bv: str, cid, url: str) -> Path:
    """下载 dash 音轨到缓存目录(已存在则复用), 返回本地文件路径。

    文件名用 cid 而非 page: 分P 的顺序会变(官方可调整), cid 才是分P 的稳定标识。
    旧缓存用 page 命名, 会和新缓存混淆, 这里统一改用 cid。"""
    dst = AFP_CACHE_DIR / f"{bv}_{cid}.audio.m4s"
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


def _afp_cache_file(bv: str, cid) -> Path:
    """回放指纹缓存路径。用 cid 而非 page: 分P 顺序可能变, cid 才是稳定标识。"""
    return AFP_CACHE_DIR / f"{bv}_{cid}.fp.npz"


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
    """取回放({bv} P{page})的音频指纹(带磁盘缓存, 避免长视频反复拉流)。

    **按分P 精确取**: 先 bili_view 拿 pages, 用 page 找到该分P 的 cid, 再用 cid
    取音轨。旧实现直接取第一分P 的 cid, 导致 P2/P3 指纹全是 P1 的(实测复现),
    且缓存键 {bv}_{page} 会把错误结果缓存下来反复复用。

    优先级: local_audio 调试参数 > 磁盘缓存 > dash 纯音轨 > html5 mp4 整段(经
    本服务 /api/stream 反代; 多段按顺序拼接指纹)。
    返回 (fp (N,33) float32, 来源说明)。"""
    if not HAS_AUDIOFP:
        raise ApiError(f"音频指纹模块不可用: {_AFP_IMPORT_ERR or 'numpy/audiofp 导入失败'}")
    if local_audio:                      # 调试/测试: 本地音频直接算, 不碰网络与缓存
        fp = await asyncio.to_thread(ffmpeg_audio_fp, local_audio, hop_sec, win_sec)
        return fp, f"本地音频({Path(local_audio).name})"

    # 解析该分P 的 cid(拿不到 pages 时回落 info["cid"])
    info = await bili_view(session, bv)
    cid = info.get("cid")
    for p in (info.get("pages") or []):
        if int(p.get("page") or 0) == int(page):
            cid = p.get("cid")
            break
    if not cid:
        raise ApiError(f"取不到 {bv} P{page} 的 cid")

    cache = _afp_cache_file(bv, cid)
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
    audio_url = await bili_dash_audio_url(session, bv, cid)
    if audio_url:
        try:
            m4s = await download_dash_audio(session, bv, cid, audio_url)
            fp = await asyncio.to_thread(ffmpeg_audio_fp, str(m4s), hop_sec, win_sec)
            await asyncio.to_thread(_save_fp_cache, cache, fp, hop_sec, win_sec)
            return fp, f"dash音轨({m4s.stat().st_size // 1024 // 1024}MB)"
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError,
                OSError, ValueError) as e:
            print(f"[autocalign] dash 音轨路线失败, 转入 mp4 兜底: {e}")
    # ② 整段 mp4 兜底: html5 durl(可能多段), ffmpeg 走本服务 /api/stream 反代
    play = await bili_playurl_mp4(session, bv, page)
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


class DanmakuDensity:
    """把某会话的弹幕按分钟分桶, 供对齐时判断"哪一段才是真比赛"。

    比赛进行时弹幕密度远高于赛后闲聊/重播; 当音频对齐给出多个自洽候选时,
    密度是有效的第二信号(实测能把真场次和伪峰区分开: 6.8 万 vs 1.4 万条)。
    """

    def __init__(self, bins: dict, bin_sec: int = 60, anchor_ms: int = 0):
        self.bins = bins            # {相对 anchor 的桶序号: 条数}
        self.bin_sec = bin_sec
        self.anchor_ms = int(anchor_ms or 0)

    @classmethod
    def build(cls, date: str, file: str, anchor_ms: int, bin_sec: int = 60):
        """扫该会话全部弹幕, 以 anchor 为零点按 bin_sec 分桶。"""
        buckets = {}
        try:
            paths = danmaku_paths(date, file)
        except Exception:
            return None
        if not paths or not anchor_ms:
            return None
        for p in paths:
            try:
                with open_text(p) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        t = o.get("ts")
                        if not isinstance(t, (int, float)) or t <= 0:
                            continue
                        k = int((int(t) - anchor_ms) / 1000 // bin_sec)
                        buckets[k] = buckets.get(k, 0) + 1
            except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
                continue
        return cls(buckets, bin_sec, anchor_ms) if buckets else None

    def density_for(self, offset_ms: int, duration_sec: float) -> float:
        """某个起点(offset_ms = "回放 0 秒"对应的墙钟毫秒)对应区间的弹幕总量。

        注意桶号是**相对 anchor** 的: 必须先把墙钟减去 anchor 再除以 bin_sec,
        直接拿绝对时间戳去除会落到不存在的桶上, 结果恒为 0(已踩过)。
        """
        start_sec = (int(offset_ms) - self.anchor_ms) / 1000.0
        start_bin = int(start_sec // self.bin_sec)
        n_bins = max(1, int(duration_sec // self.bin_sec))
        return float(sum(self.bins.get(start_bin + i, 0) for i in range(n_bins)))

    def density_per_min(self, offset_ms: int, duration_sec: float) -> float:
        """区间内的**每分钟平均弹幕数**, 用于判断"这段像不像真比赛"。

        实测参考值(2026-10-26 那晚):
          真实比赛时段  5000~11000 条/10分钟 = 500~1100 条/分
          赛后闲聊/重播  200~250 条/分
        """
        dur_min = max(1.0, duration_sec / 60.0)
        return self.density_for(offset_ms, duration_sec) / dur_min


def _align_one_page(fp_replay, conns, hop, min_conf, min_match_sec,
                    density=None, anchor_ms=None, later_than_ms=None,
                    min_density=None, window_ms=None):
    """把一个分P 的回放指纹对齐到直播 afp 上, 返回它在直播时间轴中的起始位置。

    返回 dict:
      offset_ms        回放 t=0 对应的直播墙钟(毫秒)
      confidence       支持该位置的各段置信度中位数
      matched_seconds  参与投票的片段总时长
      votes            一致票数 / 总段落数
      candidates       找到的自洽位置个数(>1 表示该内容在直播里出现多次)
      all_candidates   全部自洽位置(毫秒, 升序), 便于排查
      suspect          该分P 只在窗口外找到位置(早上重播/回顾), 已拒绝
      suspect_reason   拒绝原因(给用户看)

    **为什么是「多段投票」而不是「一个长窗互相关」**

    旧实现把整段回放(或它的 75% 长窗)一次性拿去和直播滑动互相关。问题是长窗里只要
    混进一段直播没播的内容, 整窗得分就被拉平, 主峰/次峰比失去意义 —— 实测同一局
    比赛, 全长对齐 conf=1.01(判为不匹配), 而换成 3 分钟短片段逐段对齐, 落在同一位置
    的各段 conf 高达 5~9。**短片段比长片段更能定位**, 因为局部特征不会被全局噪声稀释。

    做法: 把回放均匀切成 N 段(默认 8 段, 每段约 3 分钟), 每段单独去直播里找最像的位置,
    得到 N 个候选起点。若某位置被 >=ceil(N/3) 段同时指向(容差 AUTO_VOTE_TOLERANCE_SEC),
    就算「自洽」并采信。

    **多候选时取最早的那个**

    同一段音频在直播里可能出现多次(首播 + 之后的回顾/重播)。实测 IG vs HLE 第一局
    在前 17 分钟匹配到 01:00(conf 5.5~8.3), 后半段在全晚各处只能拿到 conf≈1.1(随机水平),
    而在 07:15 有一个假的一致位置。我们要的是**当时正在直播的那次**, 即最早的真实位置。
    因此候选按时间升序, 取第一个通过的。
    """
    ref_frames = int(fp_replay.shape[0])
    if ref_frames < AUTO_MIN_CONN_FRAMES:
        raise ApiError("该分P 指纹过短, 无法对齐")

    # 片长: 目标 3 分钟, 但不短于 min_match_sec, 也不超过回放的 1/3
    seg_secs = min(180.0, fp_replay.shape[0] * hop / 3.0)
    seg_frames = max(int(seg_secs / hop), AUTO_MIN_CONN_FRAMES)
    if seg_frames >= ref_frames:
        # 回放太短(本身不到 3 分钟): 整段当一个样本
        starts = [0]
        seg_frames = ref_frames
    else:
        k = AUTO_VOTE_SEGMENTS
        spans = ref_frames - seg_frames
        starts = [int(round(spans * i / (k - 1))) for i in range(k)]
        # 去重(回放很短时可能算出重复起点)
        seen = set()
        starts = [s for s in starts if not (s in seen or seen.add(s))]

    n_seg = len(starts)
    need = max(2, (n_seg + 2) // 3)      # ceil(n/3), 至少 2 票
    votes = []                            # [(主播内起点秒, 段序号, 置信度)]
    detail = []
    # 窗口(墙钟秒): 给了就**优先在窗口内找**, 而不是先全局最优再筛。
    # 差别很关键: 早上的重播与首播音频完全一致, 甚至可能更清晰(码率更高), 于是
    # "全局最强位置"常常是重播那一段。若先取全局最优再按窗口过滤, 窗口内的正确位置
    # 根本没进入候选, 那些页会被判成"只在窗口外找到"而整页作废 ——
    # 实测 26 日 HLE vs IG 的赛后采访/赛前评论就这样被误杀了。
    # 现在: 每段先在窗口内挑最强的, 窗口内没有才退回全局最强(留着给后面报"疑似重播")。
    win_lo_s = win_hi_s = None
    if window_ms:
        win_lo_s, win_hi_s = window_ms[0] / 1000.0, window_ms[1] / 1000.0

    for i, st in enumerate(starts):
        piece = fp_replay[st:st + seg_frames]
        best = None            # 全局最强 (base秒, conf)
        best_in = None         # 窗口内最强
        for seg in conns:
            if seg["n"] < seg_frames:
                continue
            # 转成"该片段在**这一段连接**内允许的起始偏移"(秒), 传给 align 限制搜索。
            # 必须在 align 内部限制: 它只返回全局最大峰, 而重播峰可能更高 ——
            # 光在返回后过滤, 窗口内的正确峰根本不会出现(实测 26 日 HLE vs IG P2:
            # 全局峰在 08:05、被窗口排除, 而窗口内 01:51 也有一个峰, 只是矮一点)。
            rng = None
            if win_lo_s is not None:
                seg_t0_s = seg["t0_ms"] / 1000.0
                # base = seg_t0 + off - st*hop  =>  off = base - seg_t0 + st*hop
                rng = (win_lo_s - seg_t0_s + st * hop,
                       win_hi_s - seg_t0_s + st * hop)
            try:
                # 注意方向: **直播整段是 ref, 回放片段是 query**。
                # align() 要求 query 不长于 ref, 且返回 query 在 ref 里的起始位置。
                r = audiofp.align(seg["frames"], piece, hop, valid_range=rng)
            except ValueError:
                continue
            if not math.isfinite(r["offset_sec"]):
                continue                     # inf = 只有单峰, 不可判定
            pos_wall = seg["t0_ms"] / 1000.0 + r["offset_sec"]
            base = pos_wall - st * hop       # 回放 t=0 对应的墙钟
            if best is None or r["confidence"] > best[1]:
                best = (base, r["confidence"])
            if win_lo_s is not None and win_lo_s <= base <= win_hi_s:
                if best_in is None or r["confidence"] > best_in[1]:
                    best_in = (base, r["confidence"])
        pick = best_in if best_in is not None else best
        if pick is None:
            detail.append(f"段{i}无可判定位置")
            continue
        base, conf = pick
        votes.append((base, i, conf))
        detail.append(f"段{i}(回放{st*hop/60:.0f}分)->{base/3600:.2f}h conf={conf:.2f}"
                      + ("(窗口内)" if best_in is not None else "(窗口外)"))

    if not votes:
        raise ApiError("该分P 的音频在直播里找不到可判定的位置"
                       "(各片段都只有单峰, 可能这场直播没有音频或指纹损坏)")

    # ---- 按容差聚类投票, 得到「自洽位置」 ----
    votes.sort()
    clusters = []           # [[中心秒, [置信度...], [段序号...], [各票位置...]]]
    for base, i, conf in votes:
        if clusters and base - clusters[-1][0] <= AUTO_VOTE_TOLERANCE_SEC:
            c = clusters[-1]
            c[1].append(conf)
            c[2].append(i)
            c[3].append(base)
            # 中心取簇内各票的中位数(避免被单个离群票拉走)
            c[0] = c[3][len(c[3]) // 2]
        else:
            clusters.append([base, [conf], [i], [base]])

    # 自洽位置的判定: **票数够 或 票的质量够高**。
    # 单纯数票会误杀"直播只播了一部分"的情况: 实测 HLE vs IG 第一局只有 2/8 段能匹配,
    # 但这 2 段的置信度是 7.00 / 7.16(随机水平只有 1.1), 且指向完全相同的位置 ——
    # ---- 自洽位置的判定 ----
    # 判据是**投票一致性**, 而不是单段置信度。
    #
    # 为什么不能用单段置信度当门票: 单段 conf 是"该 3 分钟片段内部主峰/次峰比",
    # 它反映的是**这段音频本身有多独特**, 不是"这个位置对不对"。实测 CFO vs T1 第一局:
    # 8 段全部指向完全相同的位置(+47.4s, 离散 0.0 秒), 但只有 1 段 conf=8.28,
    # 其余 7 段是 1.08~2.12(那段是 BP/解说, 音频特征不独特)。旧实现先用 min_conf
    # 把低分票**丢掉**, 只剩 1 票, 达不到 need=3, 于是判"不可靠, 已拒绝" ——
    # 一场普通 Bo1 的第一局都对齐不了, 这就是"自动校准永远失败"的直接原因。
    #
    # 反面对照(同样 8 段): 拿 26 日的回放去对 27 日的直播, 8 段分别散落在 8 个毫无关系的
    # 位置(conf 全是 1.0~1.35), 最大簇只有 1 票。所以**"有多少段指向同一处"**是可靠判据,
    # "每段自己多有把握"不是。
    #
    # 采纳条件(任一成立即采信):
    #   ① 一致票数 >= ceil(N/3)      —— 多数段自洽
    #   ② 2 张强票(conf>=6)          —— 票少但都很确定(直播只缺了一小部分)
    #   ③ 1 张很强票(conf>=7) 且 总票数>=2 —— 部分段落极确定, 其余同向但特征弱
    #   ④ 全票一致(票数==N) 且最强票 >= min_conf —— 8 段无一例外指向同一处,
    #      这种离散 0.0s 的一致性不可能是巧合(随机散票不可能全撞一处)
    def cluster_score(c):
        """票数与票质的综合分: 各票置信度之和(低分票几乎不加分)。"""
        return sum(c[1])

    strong_conf = max(6.0, min_conf * 3)
    solid = []
    why = {}
    for c in clusters:
        n_vote = len(c[1])
        confs = sorted(c[1])
        max_conf = confs[-1]
        n_strong = len([x for x in confs if x >= strong_conf])
        reasons = []
        if n_vote >= need:
            reasons.append("多段一致")
        if n_strong >= 2:
            reasons.append("%d 张强票" % n_strong)
        if max_conf >= 7.0 and n_vote >= 2:
            reasons.append("强票+同向")
        if n_vote == n_seg and max_conf >= min_conf:
            reasons.append("全票一致")
        if reasons:
            solid.append(c)
            why[id(c)] = "、".join(reasons)
    if not solid:
        best_c = max(clusters, key=cluster_score)
        raise ApiError(
            "该分P 的音频在直播里找不到自洽位置(最多 %d/%d 段指向同一处, 最强票 conf=%.2f); "
            "可能是这场直播没有转播, 或采集期间音频断流"
            % (len(best_c[1]), n_seg, max(best_c[1])))

    solid.sort(key=lambda c: c[0])          # 按时间升序 -> 取最早

    # ---- 窗口约束: 排除"早上重播/回顾"里的伪峰 ----
    # 直播间整晚常把前几场重播一遍, 音频与官方回放一模一样, 投票也会一致命中。
    # 实测 CFO vs T1 的 P2「赛前评论」(01:00 那场的内容) 被对齐到了 07:44 ——
    # 那是早上重播, 且当时弹幕全是「抽签」, 一旦采信, 那一局的弹幕就整体错了 6 小时。
    # 因此: 给了 window_ms=(最早可接受墙钟, 最晚可接受墙钟) 时, 只接受落在窗内的候选;
    # 全部候选都在窗外 -> 该页标 suspect 并**拒绝采纳**(宁可报未对齐, 也不能错位)。
    suspect = False
    suspect_reason = ""
    all_pos = [int(round(c[0] * 1000)) for c in solid]
    if window_ms:
        w_lo, w_hi = window_ms
        in_win = [c for c in solid if w_lo <= c[0] * 1000 <= w_hi]
        if not in_win:
            suspect = True
            near = min(all_pos, key=lambda m: min(abs(m - w_lo), abs(m - w_hi)))
            # 报错文案要能直接看懂"偏了多少": 优先按赛程窗口给相对差, 没有 anchor 时给绝对时刻。
            ref = anchor_ms if anchor_ms else w_lo
            suspect_reason = (
                "该分P 只在窗口外找到位置(%s, 距赛程窗口 %+.0f 分钟), 判为早上重播/回顾, 未采纳"
                % (_fmt_wall(near), (near - ref) / 60000.0))
            raise ApiError(suspect_reason)
        solid = in_win

    chosen = solid[0]

    # ---- 一致性: 选中的位置内部各票离散不能太大 ----
    same = [v for v in votes if abs(v[0] - chosen[0]) <= AUTO_VOTE_TOLERANCE_SEC]
    spread = max(v[0] for v in same) - min(v[0] for v in same)

    offset_ms = int(round(chosen[0] * 1000))
    return {
        "offset_ms": offset_ms,
        # 置信度给**最强票**(不是中位数): 低分票也有定位价值, 但它们会把中位数拉到
        # 随机水平(~1.1), 让"已对齐"看起来像没对齐。最强票代表这次定位的证据强度。
        "confidence": min(float(max(chosen[1])), 999.0),
        "matched_seconds": len(chosen[1]) * seg_frames * hop,
        "conn": 0,
        "t0_ms": int(round(chosen[0] * 1000)),
        "votes": len(chosen[1]),
        "total_segments": n_seg,
        "candidates": len(solid),
        "all_candidates": [int(round(c[0] * 1000)) for c in solid],
        "spread_sec": round(spread, 2),
        "picked_by": ("多段投票 %d/%d 段一致(%s)" % (len(chosen[1]), n_seg,
                                                  why.get(id(chosen), "一致")) +
                      ("(取最早, 共 %d 处候选)" % len(solid) if len(solid) > 1 else "")),
        "suspect": suspect,
        "suspect_reason": suspect_reason,
        "debug": detail,
    }


def _fmt_wall(ms) -> str:
    """毫秒墙钟 -> 'MM-DD HH:MM' 便于日志阅读。"""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return str(ms)


def _schedule_window_for_bv(date: str, bv: str):
    """取该场比赛的赛程开始时间(毫秒), 用于给对齐加时间窗约束。

    数据来源: matches.json 的 lists[比赛日][].start_ms(由 /api/discover 从 Riot 赛程落盘)。
    匹配方式: 该 BV 所在列表条目的 start_ms; 没有 bv 时按 bv 反查不到, 返回 None。

    返回 (start_ms|None, 说明)。
    """
    try:
        matches = load_matches()
    except Exception:
        return None, "读取赛程失败"
    target_bv = str(bv or "").strip()
    for day, lst in (matches.get("lists") or {}).items():
        if not isinstance(lst, list):
            continue
        for it in lst:
            if not isinstance(it, dict):
                continue
            if str(it.get("bv") or "") != target_bv:
                continue
            sm = it.get("start_ms")
            if isinstance(sm, (int, float)) and not isinstance(sm, bool) and sm > 0:
                return int(sm), f"{day} {it.get('title') or ''} 赛程"
            # 该条目存在但没有赛程时间: 期间限内也别去别的天里找同 BV(同 BV 只会属一天)
            return None, f"{day} {it.get('title') or ''} 无赛程时间"
    return None, "赛程列表里没有该 BV"


async def run_autocalign(date: str, file: str, bv: str, page: int, session,
                         local_audio=None) -> dict:
    """自动对齐全流程(读 afp -> 逐分P 回放指纹 -> 互相关 -> 写 matches)。

    **逐分P 独立对齐**: 官方回放把一场比赛拆成多个分P(第一局/赛后采访/赛前评论),
    每个分P 都是独立的直播切片, 因此各自有独立的 offset。整体对齐只能让第一个
    分P 命中(已实测), 后两个就永远错位。

    page 传 0 表示"这场比赛的**所有**分P 都各自对齐一遍"。

    offset 语义与前端一致: replaySec = (wallMs - anchorMs)/1000 - offset_sec。
    """
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

    # anchor 与 handle_match_get 同源: 聚合数据首行 ts 优先, meta 兜底
    anchor_ms = first_ts_from_data(date, file)
    if not anchor_ms:
        try:
            anchor_ms = compute_anchor_ms(load_meta(date, file))
        except ApiError:
            anchor_ms = 0

    # 弹幕密度(第二信号): 多候选时用它区分"真比赛"与"重播/伪峰"
    density = await asyncio.to_thread(DanmakuDensity.build, date, file, anchor_ms)
    if density:
        print(f"[autocalign] 弹幕密度直方图已就绪({len(density.bins)} 个分钟桶)",
              flush=True)

    # b) 确定要对齐哪些分P
    info = await bili_view(session, bv)
    pages_meta = info.get("pages") or []
    if page and page > 0:
        targets = [{"page": int(page),
                    "part": next((p.get("part") for p in pages_meta
                                  if int(p.get("page") or 0) == int(page)), f"P{page}"),
                    "duration": next((p.get("duration") for p in pages_meta
                                      if int(p.get("page") or 0) == int(page)), 0)}]
    else:
        targets = [{"page": int(p["page"]), "part": p.get("part") or f"P{p['page']}",
                    "duration": p.get("duration") or 0} for p in pages_meta]
    if not targets:
        raise ApiError(f"{bv} 没有可用的分P 信息")

    # b2) 由赛程开始时间算出"该分P 允许落在哪个墙钟区间"。
    # 没有赛程时间(旧数据/未刷新赛程)时 window 为 None -> 不做约束, 行为与旧版一致。
    # 有了它才能挡住"早上重播"这类伪峰(见 _align_one_page 里的说明)。
    win_lo = win_hi = None
    sched_ms, win_reason = _schedule_window_for_bv(date, bv)
    if sched_ms:
        total_dur_sec = sum(int(p.get("duration") or 0) for p in pages_meta)
        win_lo = sched_ms - AUTO_SCHEDULE_LEAD_SEC * 1000
        win_hi = sched_ms + (total_dur_sec + AUTO_SCHEDULE_TAIL_SEC) * 1000
        print(f"[autocalign] 赛程窗口: {_fmt_wall(win_lo)} ~ {_fmt_wall(win_hi)} "
              f"({win_reason})", flush=True)
    else:
        print("[autocalign] 无赛程开始时间, 不做窗口约束"
              "(点「刷新赛程」可补全, 能挡掉早上重播的误匹配)", flush=True)
    window_ms = (win_lo, win_hi) if win_lo is not None else None

    # c) 逐分P 独立对齐。每个分P 各自投票定位, 互不影响。
    #    新算法(多段投票 + 取最早自洽位置)不需要密度/时序辅助判断:
    #      - 它自己会用 8 段投票过滤掉不稳定位置;
    #      - 多个自洽位置时取**最早**的(那是"当时正在直播"的那次, 后面的多半是回顾)。
    #    因此这里按分P 顺序直接对齐即可, 失败的分P 单独记录原因。
    page_results, page_errors = {}, {}
    for tp in targets:
        pg = tp["page"]
        try:
            fp_replay, src_desc = await get_replay_fingerprint(
                session, bv, pg, hop, win, local_audio=local_audio)
            r = await asyncio.to_thread(_align_one_page, fp_replay, conns, hop,
                                        AUTO_MIN_CONFIDENCE, AUTO_MIN_MATCH_SEC,
                                        anchor_ms=anchor_ms, window_ms=window_ms)
            r["offset_sec"] = round((r["offset_ms"] - anchor_ms) / 1000.0, 3)
            r["part"] = tp["part"]
            r["duration"] = tp["duration"]
            r["src"] = src_desc
            page_results[pg] = r
            print(f"[autocalign] {date}/{file} P{pg}({tp['part']}): "
                  f"offset={r['offset_sec']} conf={r['confidence']:.2f} "
                  f"投票{r['votes']}/{r['total_segments']} 候选{r['candidates']}个 "
                  f"依据={r['picked_by']}", flush=True)
        except (ApiError, Exception) as e:     # noqa: B014 - 单页失败要隔离
            page_errors[pg] = str(e)
            print(f"[autocalign] {date}/{file} P{pg}({tp['part']}) 失败: {e}", flush=True)

    if not page_results:
        # 全部失败: 记录失败原因(供前端显示"为什么这场没对齐"), 但**不动**已有的
        # offset —— 不能因为一次失败就把用户之前手工校准的结果清掉。
        fail_key = f"{date}|{file}|{bv}"
        err_text = "; ".join(f"P{k}: {v}" for k, v in page_errors.items())

        def _mark_fail(matches):
            e = dict(matches["align"].get(fail_key) or {})
            e.update({"date": date, "file": file, "bv": bv,
                      "title": e.get("title") or info.get("title") or "",
                      "align_failed": True,
                      "align_error": err_text[:300],
                      "align_failed_at": datetime.now().isoformat(timespec="seconds")})
            matches["align"][fail_key] = e

        await asyncio.to_thread(update_matches, _mark_fail)
        raise ApiError("所有分P 都无法对齐: " + err_text)

    # d) 写回 matches.json。
    # key 必须**按比赛(BV)区分**, 不能只用 {date}|{file}:
    # 一个会话里有多场比赛(一晚 4 场), 若共用 key 会互相覆盖 —— 实测对齐完第 4 场后,
    # 前 3 场的记录全没了。现在: 会话级汇总放在 {date}|{file}, 每场比赛的详细对齐
    # 按 {date}|{file}|{bv} 各存一条。
    sess_key = f"{date}|{file}"
    match_key = f"{date}|{file}|{bv}"
    now_iso = datetime.now().isoformat(timespec="seconds")

    def _mut(matches):
        entry = dict(matches["align"].get(match_key) or {})
        entry.update({"date": date, "file": file, "bv": bv,
                      "title": info.get("title") or entry.get("title") or ""})
        # 记下 B 站当时实际有几个分P。前端据此显示「已对齐 1/2」并把缺的那页原因
        # 挂到悬停提示上 —— 否则用户看到"已对齐"却少一局, 会以为程序漏了。
        entry["total_pages"] = len(pages_meta) or len(page_results)
        # 本次有分P 成功 -> 清掉历史失败标记。否则一旦某次全失败写入过 align_failed,
        # 之后即使对齐成功, 下拉框也会永远显示「未对齐·直播可能未转播」。
        for k in ("align_failed", "align_error", "align_failed_at"):
            entry.pop(k, None)
        # 部分分P 失败仍记下来(如赛前评论在开播前没录到), 供前端说明"哪几页没对齐"
        if page_errors:
            entry["page_errors"] = {str(k): str(v)[:300] for k, v in page_errors.items()}
        else:
            entry.pop("page_errors", None)
        pages_map = dict(entry.get("pages") or {})
        # 本次判失败的分P 必须**删掉旧 offset**, 不能留着上次的值。
        # 历史缺陷: 只写成功页、不清理失败页, 于是上一次(可能是按错误规则算出的)错位
        # offset 会一直生效 —— 实测 CFO vs T1 的 P2 残留在 07:44 那个重播位置(24297.9s),
        # 本次已正确判定"找不到", 但前端仍会拿旧值去映射, 弹幕整体错 6 小时。
        for pg in page_errors:
            pages_map.pop(str(pg), None)
        for pg, r in page_results.items():
            pages_map[str(pg)] = {
                "part": r["part"],
                "duration": r["duration"],
                "offset_sec": r["offset_sec"],
                "confidence": round(r["confidence"], 3),
                "matched_seconds": round(r["matched_seconds"], 1),
                "votes": r.get("votes"),
                "total_segments": r.get("total_segments"),
                "candidates": r["candidates"],
                "picked_by": r["picked_by"],
                "method": "audiofp",
                "ts": now_iso,
            }
        entry["pages"] = pages_map
        # 兼容字段: 顶层 offset_sec 取**第一分P**(旧前端/旧代码读它)
        first_pg = str(min(page_results.keys()))
        entry["offset_sec"] = page_results[int(first_pg)]["offset_sec"]
        entry["auto"] = {"confidence": round(page_results[int(first_pg)]["confidence"], 3),
                         "method": "audiofp", "ts": now_iso}
        matches["align"][match_key] = entry
        # 会话级汇总: 记录该会话对齐过哪些 bv(总览按 bv 查详情)
        sess = dict(matches["align"].get(sess_key) or {})
        sess.update({"date": date, "file": file})
        bvs = list(sess.get("bvs") or [])
        if bv not in bvs:
            bvs.append(bv)
        sess["bvs"] = bvs
        # 兼容: 会话级 offset_sec 取第一场的第一分P(仅当还没设过)
        if not isinstance(sess.get("offset_sec"), (int, float)):
            sess["offset_sec"] = entry["offset_sec"]
        matches["align"][sess_key] = sess
        # 同步场次列表: **按 bv + page 精确匹配**
        for day_list in matches.get("lists", {}).values():
            if not isinstance(day_list, list):
                continue
            for item in day_list:
                if not isinstance(item, dict) or str(item.get("bv") or "") != bv:
                    continue
                item_pages = item.get("pages")
                if not isinstance(item_pages, dict):
                    item_pages = {}
                # 列表里的 pages 同样要清掉本次失败的分P(理由同上)
                for pg in page_errors:
                    item_pages.pop(str(pg), None)
                for pg, r in page_results.items():
                    item_pages[str(pg)] = {
                        "part": r["part"], "offset_sec": r["offset_sec"],
                        "confidence": round(r["confidence"], 3)}
                item["pages"] = item_pages
                item["offset_sec"] = page_results[int(first_pg)]["offset_sec"]
                item["total_pages"] = len(pages_meta) or len(page_results)

    if not await asyncio.to_thread(update_matches, _mut):
        raise ApiError("matches.json 写入失败")

    elapsed = time.monotonic() - t0
    ok_pages = ", ".join(f"P{p}({r['part']})={r['offset_sec']:.1f}s"
                         for p, r in sorted(page_results.items()))
    message = (f"分P 对齐完成: {len(page_results)}/{len(targets)} 个分P 成功; "
               f"{ok_pages}; 耗时 {elapsed:.1f}s")
    if page_errors:
        message += "; 失败: " + ", ".join(f"P{p}" for p in page_errors)
    print(f"[autocalign] {date}/{file} bv={bv}: {message}")

    # 顶层 offset_sec/confidence 取**第一分P**: 多分P 各有一个 offset, 顶层无法表达,
    # 但前端/旧脚本都在读顶层字段来判断"这次校准成功了吗"。
    # 历史缺陷: 只返回 pages 字典而没有顶层 offset_sec, 前端条件
    # `j.ok && typeof j.offset_sec === 'number'` 恒为假 —— 后端算成功、界面却一律显示
    # 「自动校准失败: 未知错误」, 实测每一场每一天都这样。故必须补上这两个兼容字段。
    first_pg = min(page_results.keys())
    return {"date": date, "file": file, "bv": bv,
            "offset_sec": page_results[first_pg]["offset_sec"],
            "confidence": round(page_results[first_pg]["confidence"], 3),
            "page": first_pg,
            "total_pages": len(targets),
            "aligned_pages": len(page_results),
            "all_ok": not page_errors,
            "pages": {str(p): {"part": r["part"], "offset_sec": r["offset_sec"],
                               "confidence": round(r["confidence"], 3),
                               "matched_seconds": round(r["matched_seconds"], 1),
                               "votes": r.get("votes"),
                               "total_segments": r.get("total_segments"),
                               "candidates": r["candidates"],
                               "picked_by": r["picked_by"],
                               "suspect": r.get("suspect", False),
                               "suspect_reason": r.get("suspect_reason", "")}
                      for p, r in sorted(page_results.items())},
            "errors": page_errors,
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
        # page=0(默认) 表示"这场比赛的所有分P 各对齐一遍"; page>=1 只对齐指定分P
        page = int(body.get("page") or 0)
    except (TypeError, ValueError):
        raise ApiError("字段 page 应为整数")
    if page < 0:
        raise ApiError("字段 page 应 >= 0 (0=全部分P)")
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


def _lan_ips() -> list:
    """本机局域网 IPv4 地址(启动时打印, 方便手机/iPad 直接输入)。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))      # 只为让内核选路由, 不真发包
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("169.254.")]


def _print_qr(url: str) -> None:
    """在终端打印二维码(局域网模式下给手机扫)。

    两个关键点(之前都踩过):
      1. **反色**: 终端里 '█' 显示的是前景色, 深色主题下前景是白, 直接画会把
         黑白反掉, 手机扫不出来。所以用 '  '(空格, 背景色=深)表示二维码的黑模块,
         '██'(实心, 前景色=白)表示白模块 —— 视觉上就是白底黑码。
      2. **静区 4 格**: 标准要求四周留 4 个模块的空白, 少了扫描器找不到边界。
    """
    try:
        import qrcode
    except ImportError:
        print("  (未安装 qrcode 库, 无法显示二维码; 手动输入上面的网址即可)")
        print("   安装: .venv\\Scripts\\python.exe -m pip install qrcode")
        return
    qr = qrcode.QRCode(border=4, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    dark, light = "  ", "██"
    for row in qr.get_matrix():
        print("  " + "".join(dark if c else light for c in row))
    print()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    # 监听地址来源: 环境变量 DANMAKU_HOST > config.json 的 host > 默认 127.0.0.1。
    # 这里把**实际生效值**和来源都打出来 —— 曾经出现过: config.json 存了 0.0.0.0,
    # 而「启动回放台.bat」标题写"仅本机", 结果实际绑定 0.0.0.0 并打印二维码,
    # 用户看到的标题和实际行为自相矛盾, 排查时无从下手。
    host_src = ("环境变量 DANMAKU_HOST" if os.environ.get("DANMAKU_HOST", "").strip()
                else ("config.json 的 host" if str(_cfg("host", default="")).strip()
                      else "默认值"))
    lan = HOST not in ("127.0.0.1", "localhost")
    ips = _lan_ips() if lan else []
    print()
    print("=" * 60)
    print("  弹幕延时直播 · 本地服务器")
    print(f"  本机访问:     http://127.0.0.1:{PORT}")
    for ip in ips:
        print(f"  局域网访问:   http://{ip}:{PORT}")
    print(f"  监听地址:     {HOST}:{PORT}  (来自{host_src})")
    print(f"  采集数据目录: {DATA_DIR}")
    print(f"  B站Cookie:    {'已加载' if load_cookie_header() else '未配置(可选)'}")
    print("  停止: 关闭本窗口, 或按 Ctrl+C")
    print("=" * 60)
    if not lan:
        print("  仅本机可访问。要让手机/iPad 看, 请用「启动回放台_局域网.bat」。")
    elif not ips:
        print("  [注意] 已绑定 0.0.0.0, 但没探测到局域网 IP(未连 WiFi?), 手机可能连不上。")
    if lan and ips:
        print()
        print("  手机/iPad 扫码访问（和电脑连同一个 WiFi）:")
        print()
        _print_qr(f"http://{ips[0]}:{PORT}")
    try:
        web.run_app(build_app(), host=HOST, port=PORT, print=None)
    except OSError as e:
        # 端口被占用是最常见的启动失败(比如上次的窗口没关、或另一个 bat 还开着)。
        # 默认的 aiohttp 异常是 20 行 traceback, 用户看不出该怎么办, 这里给可执行的指引。
        if getattr(e, "errno", None) in (48, 10048) or "10048" in str(e):
            print()
            print("=" * 60)
            print(f"  [启动失败] 端口 {PORT} 已被占用")
            print("=" * 60)
            print("  多半是已经有一个回放台在运行了。可以:")
            print(f"    1. 回去找那个旧窗口, 直接关掉它; 或")
            print(f"    2. 双击「退出回放台.bat」一键清理; 或")
            print(f"    3. 命令行查是谁占的: netstat -ano | findstr :{PORT}")
            print()
            print("  如果你就想同时开两个, 改 config.json 的 port 再启动。")
            print("=" * 60)
        else:
            print(f"\n  [启动失败] {e}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

