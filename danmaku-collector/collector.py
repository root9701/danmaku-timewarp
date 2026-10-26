# -*- coding: utf-8 -*-
"""Bilibili 直播弹幕采集器(单文件, asyncio 单事件循环)。
用法: python collector.py [--config config.json] [--test [秒]] [--room 房间号]
数据: data/{YYYY-MM-DD}/{room}_s{HHMM}.jsonl 按小时滚动切片(HHMM=该片起始墙钟),
      每行 {"ts","un","t","ty"[,"price"]}; 测试模式第一片 test_HHMMSS_{room}.jsonl,
      滚动片 test_HHMMSS_{room}_s{HHMM}.jsonl。
滚动: 到点后 close 当前 raw -> gzip 成同名 .jsonl.gz 并删 raw -> 开新片(append);
      收包循环与 _aux_loop(每 1s)都会检查, 断线等待期也照常切片刻盘;
      afp sidecar/meta/dm_count 等总账不受影响。切片周期 config slice_minutes(默认 60=整点)。
收尾: 最后活动片 gzip + 同目录 {room}.meta.json(含 "parts" 片清单); 已存在的同名账本
      一律先归档为 _runN 再写(meta 用 tmp+replace 原子落盘), 绝不覆盖。
      单实例锁 logs/collector.lock 防双开损坏同一数据片。
"""
import argparse
import asyncio
import contextlib
import errno
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import brotli

VERSION = "0.1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={}"
HOME_URL = "https://www.bilibili.com/"  # 主站首页 Set-Cookie 下发 buvid3/b_nut 指纹(直播页不下发)
APP_DANMU_URL = "https://api.live.bilibili.com/xlive/app-room/v1/index/getDanmuInfo"
APP_UA = ("Mozilla/5.0 BiliDroid/6.64.0 (bbcallen@gmail.com) os/android model/Unknown "
          "mobi_app/android build/6640400 channel/bili innerVer/6640400 osVer/6.0.1 network/2")
# 公开的安卓客户端 appkey/appsec(bilibili-API-collect), 仅用于游客身份取弹幕 token, 无账号语义
APPKEY, APPSEC = "1d8b6e7d45233436", "560c52ccd288fed045859ed18bffd973"
WS_HOST = "broadcastlv.chat.bilibili.com"  # 兜底 host, 优先用 getDanmuInfo 返回的就近 host
REQ_TIMEOUT = aiohttp.ClientTimeout(total=10)
PAGE_TIMEOUT = aiohttp.ClientTimeout(total=15)
WS_TIMEOUT = 15
# B站弹幕协议: 包 = 16 字节头 + body; 头字段大端: pack_len(u32) header_len(u16) protover(u16) op(u32) seq(u32), seq 恒 1
HEADER = struct.Struct("!IHHII")
HEADER_LEN = 16
# op: 2=心跳(发送,空body) 3=心跳响应(仅喂看门狗) 5=业务消息(可能压缩) 7=认证(发送) 8=认证响应(code!=0即失败)
OP_HEARTBEAT, OP_HEARTBEAT_REPLY, OP_MESSAGE, OP_AUTH, OP_AUTH_REPLY = 2, 3, 5, 7, 8
PROTOVER_ZLIB, PROTOVER_BROTLI = 2, 3
HEARTBEAT_SEC, WATCHDOG_SEC = 30, 60  # 心跳间隔 / 连续无任何包(含心跳响应)判死的时长
FLUSH_SEC, PROGRESS_SEC, RECHECK_SEC = 30, 60, 60
BACKOFF_BASE, BACKOFF_MAX, API_FAIL_LIMIT = 2, 60, 5
# 弹幕写盘失败限流: 每 60s 最多报一条 error(带累计数), 连续失败超阈值额外告警"数据正在丢失"
WRITE_ERR_LOG_SEC, WRITE_ERR_ALARM = 60, 50
# 复核下播需连续 N 次 live_status==0(间隔 RECHECK_SEC); 中间一次恢复就清零, 防接口抖动误判
LIVE_END_CONFIRM = 2
ROT_RETRY_SEC = 30  # 滚动切片失败后的重试间隔(避免 1s 辅助循环把 error 日志刷爆)
# 对齐标记关键词: 命中的弹幕时间戳额外记入 meta.kw_events, 供回放对齐校准(一血/GG等事件两侧同构)
EVENT_KWS = ("一血", "GG", "gg", "大龙", "水晶", "团灭", "五杀", "翻盘")
DEFAULT_CONFIG = {"rooms": [7734200], "earliest_start": "now", "latest_end": "09:00",
                  "poll_interval_sec": 60, "data_dir": "data", "log_dir": "logs",
                  "audio_fp": True, "slice_minutes": 60}
# 跨零点会话归属: 凌晨 00:00-10:59 的录制内容属"前一晚比赛", 目录/日期语义归到比赛日。
# 实现: 会话 record_start 在 [NIGHT_CUTOFF_START, NIGHT_CUTOFF_END) 时, 会话日 = 昨天日期。
NIGHT_CUTOFF_START, NIGHT_CUTOFF_END = 0, 11
# 滚动切片文件名后缀 _sHHMM(如 7734200_s0030.jsonl); meta 名派生时剥离
SLICE_SUFFIX_RE = re.compile(r"(_s\d{4}|_run\d+)$")
log = logging.getLogger("collector")


def archive_name(p, n):
    """旧产物 _runN 归档名。与"插在最后一个后缀前"的朴素写法不同, 这里保证服务端通配能认:
    {room}.afp.jsonl.gz -> {room}_run2.afp.jsonl.gz(必须是 _runN.afp.jsonl.gz, 否则
    server.find_afp_paths 的 {file}_run*.afp.jsonl.gz 通配匹配不到, 归档指纹等于丢);
    其余(切片 gz / meta)按原规则 {stem}_runN{exts}。"""
    name = p.name
    if name.endswith(".afp.jsonl.gz"):
        return p.with_name("%s_run%d.afp.jsonl.gz" % (name[: -len(".afp.jsonl.gz")], n))
    if name.endswith(".afp.jsonl"):
        return p.with_name("%s_run%d.afp.jsonl" % (name[: -len(".afp.jsonl")], n))
    stem, exts = name, "".join(p.suffixes)
    stem = stem[: -len(exts)] if exts else stem
    return p.with_name("%s_run%d%s" % (stem, n, exts))


class AuthError(Exception):
    """WS 认证被拒绝(op=8 且 body JSON code != 0)。"""


def now_ms():
    return int(time.time() * 1000)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def ts_str(ms):
    return fmt_dt(datetime.fromtimestamp(ms / 1000)) if ms else "-"


# 服务器时间与本机墙钟允许的最大偏差: 超过即认为该字段不可信(取到 0/秒级时间戳等), 回落墙钟
MAX_TS_SKEW_MS = 6 * 3600 * 1000


def msg_ts(info):
    """弹幕 ts 优先取 B 站消息体里的服务器时间 info[0][4](毫秒), 无效才回落本机墙钟。
    为什么: ts 是次日音视频对齐的唯一锚点; 重连补发时若用接收墙钟, 时间轴会被压缩/拉伸。
    SC 消息无此字段, 仍用 now_ms()。"""
    try:
        raw = info[0][4]
    except (IndexError, KeyError, TypeError):
        return now_ms()
    if not isinstance(raw, int) or isinstance(raw, bool):  # 兼容字符串/缺失的脏数据
        return now_ms()
    wall = now_ms()
    if raw <= 0 or abs(raw - wall) > MAX_TS_SKEW_MS:
        return wall
    return raw


def pack(op, body=b"", protover=1):
    return HEADER.pack(HEADER_LEN + len(body), HEADER_LEN, protover, op, 1) + body


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Bilibili 直播弹幕采集器")
    p.add_argument("--config", default=None, help="配置文件路径(相对路径基于脚本目录), 默认 config.json")
    p.add_argument("--test", nargs="?", const=60, type=int, default=None, metavar="秒",
                   help="测试模式: 跳过定时与轮询, 直接录制 N 秒(默认 60)后走完整收尾")
    p.add_argument("--room", type=int, default=None, help="房间号(覆盖配置)")
    return p.parse_args(argv)


def load_config(args):
    cfg = dict(DEFAULT_CONFIG)
    path = Path(args.config) if args.config else SCRIPT_DIR / "config.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k in DEFAULT_CONFIG:
            if user.get(k) is not None:
                cfg[k] = user[k]
    elif args.config:
        print("配置文件不存在: %s" % path, file=sys.stderr)
        sys.exit(2)
    else:
        log.warning("未找到 %s, 使用默认配置", path)
    cfg["rooms"] = [int(r) for r in cfg["rooms"]]
    cfg["poll_interval_sec"] = max(5, int(cfg["poll_interval_sec"]))
    cfg["slice_minutes"] = max(0, int(cfg["slice_minutes"]))  # 0=禁用滚动切片
    cfg["data_dir"] = (SCRIPT_DIR / str(cfg["data_dir"])).resolve()  # 相对路径基于脚本目录
    cfg["log_dir"] = (SCRIPT_DIR / str(cfg["log_dir"])).resolve()
    return cfg


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / ("collector_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)  # 控制台 INFO, 文件 DEBUG(忽略的 cmd 只进文件)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.setLevel(logging.DEBUG)
    log.addHandler(sh)
    log.addHandler(fh)
    log.info("日志文件: %s", path)


def next_occurrence(hhmm, base):
    """从 base 起下一次出现的 HH:MM 时刻(当日已过则取次日)。"""
    h, m = (int(x) for x in hhmm.split(":"))
    cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > base else cand + timedelta(days=1)


def parse_earliest(spec, base):
    """earliest_start: "now" 立即 / "+5m" 相对偏移(支持 s/m/h) / "HH:MM" 下一次出现。"""
    spec = str(spec).strip()
    if spec == "now":
        return base
    mm = re.fullmatch(r"\+(\d+)([smh])", spec)
    if mm:
        return base + timedelta(seconds=int(mm.group(1)) * {"s": 1, "m": 60, "h": 3600}[mm.group(2)])
    return next_occurrence(spec, base)


async def wait_until(target, label, stop_event):
    log.info("%s: %s", label, fmt_dt(target))
    next_log = 0.0
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0 or stop_event.is_set():
            return
        t = time.monotonic()
        if t >= next_log:  # 等待期间每分钟报一次状态
            next_log = t + 60
            log.info("等待中, 剩余约 %d 分 %d 秒", remain // 60, remain % 60)
        await asyncio.sleep(min(1.0, remain))


def set_keep_awake(enable):
    # ES_CONTINUOUS|ES_SYSTEM_REQUIRED 阻止系统睡眠; 退出时恢复为 ES_CONTINUOUS; 非 Windows 跳过
    if sys.platform == "win32":
        import ctypes
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (1 if enable else 0))


class SingleInstanceLock:
    """单实例锁: 双开会同时 append 同一数据片、afp 段号互撞, 数据直接损坏。
    Windows 用 msvcrt.locking 对锁文件加独占区锁(进程死掉操作系统自动释放, 不留僵尸锁);
    非 Windows 用 fcntl.flock(LOCK_EX|LOCK_NB)。--test 也加锁, 避免和正式任务抢同一目录。
    设计要点: 锁机制本身出任何问题都只降级为 warning 放行, 绝不因为它拒绝启动录制。"""

    def __init__(self, path):
        self.path, self._fh, self._ok = Path(path), None, False

    def acquire(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+", encoding="utf-8")
            if sys.platform == "win32":
                # Windows 不能对已打开文件 rename/unlink, 故复用一份固定锁文件 + 字节区锁:
                # 锁第 512 字节(文件长度之外合法), 前几字节留给 PID —— 若锁第 0 字节,
                # 其他进程连读 PID 都会被拒(PermissionError), 人工排查很别扭。
                # 进程异常退出时由操作系统自动解锁, 不留僵尸锁。
                import msvcrt
                self._fh.seek(512)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._close_fh()
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EPERM):
                return False  # 明确"已被占用", 由调用方 error + 退出码 2
            log.warning("单实例锁异常(降级放行, 继续启动): %s", e)
            return True
        except Exception as e:  # msvcrt/fcntl 导入失败等罕见情况: 绝不影响录制
            self._close_fh()
            log.warning("单实例锁不可用(降级放行, 继续启动): %s", e)
            return True
        self._ok = True  # 到这里锁确实拿到了
        with contextlib.suppress(Exception):  # 写 PID 只为人工排查, 失败不影响已持有的锁
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write("%d\n" % os.getpid())
            self._fh.flush()
        return True

    def _close_fh(self):
        with contextlib.suppress(Exception):
            if self._fh is not None:
                self._fh.close()
        self._fh = None

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        if self._ok:
            with contextlib.suppress(Exception):
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(512)  # 与 acquire() 同一区域
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            fh.close()
        if self._ok:  # 只删自己真正拿到的锁文件
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._ok = False


def install_signals(stop_event, reason):
    def on_sig(signum, frame):
        if not stop_event.is_set():
            reason["value"] = "interrupt"
            stop_event.set()

    for sig in {signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGTERM)}:
        with contextlib.suppress(Exception):
            signal.signal(sig, on_sig)


async def run_deadline_timer(delay_sec, label, stop_event, reason):
    try:
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        if not stop_event.is_set():
            reason["value"] = label
            stop_event.set()
            log.info("触发收尾: %s", label)
    except asyncio.CancelledError:
        pass


class Recorder:
    """单房间采集器: 轮询开播 -> WS 录制(含重连/复核) -> 收尾产物。"""

    def __init__(self, session, room_input, cfg, stop_event, reason, is_test, meta_name):
        self.session, self.room_input, self.room_id = session, int(room_input), int(room_input)
        self.title = None  # room_id 在首次 get_info 后更新为真实房间号
        self.poll_interval, self.data_dir = cfg["poll_interval_sec"], cfg["data_dir"]
        self.stop_event, self.reason = stop_event, reason
        self.is_test, self.meta_name = is_test, meta_name  # meta_name 为 None 时按数据文件名派生
        self.file = self.file_path = self.record_start = None
        # 会话结束时刻: 录制中为 None, finalize 时置值。赛事日目录名与 manifest 都依赖它
        self.record_end = None
        # None 才回落默认 60; 显式 0 = 禁用滚动切片
        _sm = cfg.get("slice_minutes")
        self.slice_minutes = max(0, int(_sm if _sm is not None else 60))
        self._base_prefix = None   # 本次 session 数据主名(测试 test_HHMMSS_{room}, 正式 {room})
        self._parts = []           # 全部数据片文件名清单(不含 .gz), finalize 写入 meta.parts
        self._next_rot_at = None   # 下一次滚动切片时刻(None=不滚动)
        self.dm_count = self.sc_count = self.other_cmd_count = self.parse_err_count = 0
        # 写盘失败计数: write_err_count=累计(写进 meta), _write_err_streak=连续失败次数
        self.write_err_count = self._write_err_streak = 0
        self._last_write_err_log = 0.0
        self._rotating = False     # 滚动切片重入保护(_ws_loop 与 _aux_loop 共用)
        self._rot_retry_at = 0.0   # 切片失败后的退避截止(monotonic), 防 1s 循环刷屏重试
        self.reconnect_count, self.first_ts, self.last_ts = 0, None, None
        self.gaps, self.stream_events = [], []
        self.buvid3, self.token, self.ws_hosts = None, None, []
        self._ws, self._last_packet, self._live_ended = None, 0.0, False
        self._last_sample_log = 0.0
        # 对齐辅助记录: 在线人数采样 / 关键词事件 / 标题变化
        self.watched, self.kw_events, self.title_events = [], [], []
        self._last_watch_log = 0.0
        # 音频指纹 sidecar(live_audiofp): 增强功能, 任何失败不得影响弹幕采集
        self._afp_enabled = bool(cfg.get("audio_fp", True))
        self.afp, self._afp_path, self._afp_task = None, None, None

    async def run(self):
        ended = None
        try:
            if self.is_test:  # 测试模式: 不看开播状态, 直接录制到时限
                await self._warmup()
                try:
                    status, title, rid = await self._get_info()
                    self._note_info(title, rid)
                    log.info("测试: live_status=%s 标题=%s", status, self.title)
                except Exception as e:
                    log.warning("测试: get_info 异常(忽略, 直接连 WS): %s", e)
                self._open_file()
                self._mark_event("live_start")
                await self._record_session()
            else:
                opened = False
                while not self.stop_event.is_set():
                    await self._wait_for_live()
                    if self.stop_event.is_set():
                        break
                    if not opened:  # 文件只开一次, 重新开播后继续 append 同一文件
                        self._open_file()
                        opened = True
                    self._mark_event("live_start")
                    if await self._record_session() != "live_end":
                        break
                    log.info("房间 %s 等待重新开播", self.room_id)
        except asyncio.CancelledError:
            ended = "interrupt"
        except Exception:
            log.exception("房间 %s 发生未预期异常", self.room_input)
            ended = "error"
        finally:
            try:
                await self.finalize(ended)
            except Exception:
                log.exception("房间 %s 收尾失败", self.room_input)

    def _slice_name(self):
        """当前时刻的新数据片文件名(不含目录)。
        正式: {room}_s{HHMM}.jsonl(含第一片, HHMM=该片起始墙钟);
        测试: 第一片 test_HHMMSS_{room}.jsonl(保持现状), 滚动片 test_HHMMSS_{room}_s{HHMM}.jsonl。"""
        hhmm = datetime.now().strftime("%H%M")
        if self.is_test:
            return "%s%s.jsonl" % (self._base_prefix, "_s%s" % hhmm if self._parts else "")
        return "%d_s%s.jsonl" % (self.room_id, hhmm)

    def _calc_next_rot(self, base):
        """下一次滚动时刻: 从当天 0 点起 slice_minutes 整数倍对齐(60 分=下一个整点,
        1 分=下一个整分)。slice_minutes<=0 返回 None(不滚动)。"""
        if self.slice_minutes <= 0:
            return None
        midnight = base.replace(hour=0, minute=0, second=0, microsecond=0)
        step = self.slice_minutes * 60
        k = int((base - midnight).total_seconds() // step) + 1
        return midnight + timedelta(seconds=k * step)

    def _session_dirname(self, now=None):
        """会话归属目录(比赛日): 凌晨(NIGHT_CUTOFF_START..END)的录制属前一晚, 目录用昨天日期;
        其余时刻用当天日期。跨零点滚动切片后, 后续片也落在同一会话目录(跟随会话日)。"""
        # 赛事日 = 会话**结束时刻**所在日期。旧规则"凌晨减一天"会把 25 号 23:33 开录、
        # 26 号 08:59 结束的正赛标成 25 号(用户反馈"明明是今天的却成了昨天")。
        # 以结束时刻为准: 跨零点大夜场归到"打完那天", 无魔法阈值, 符合直觉。
        # record_end 未知(录制中)时按当前时刻估算, finalize 会用真实结束时刻重算。
        when = now or self.record_end or datetime.now()
        return when.strftime("%Y-%m-%d")

    def _session_dir_path(self, when=None):
        """本次会话的数据目录: data/{比赛日}/{会话ID}/

        为什么要再套一层会话目录(用户反馈 "26 号只有一个文件, 分不清哪天"):
        旧布局把同一房间的所有会话平铺进 data/{日期}/, 于是
          - 晚间 23:33~08:59 的主会话, 和一个白天的独立会话(day0719)混在同一目录;
          - 被强杀留下的孤儿 afp 也堆在里面, 服务端分不清它属于哪个会话;
          - 跨零点切片 _s2332 与 _s0000 字典序颠倒, 前端曾据此算错时间轴。
        每个会话独立目录后, 一个目录 == 一个会话, 切片/afp/meta 不会串场。
        会话ID = {比赛日}_{HHMM}_{room} (正式) / {比赛日}_test_{HHMMSS}_{room} (测试)。
        服务端 _is_session_dir/SESSION_DIR_RE 与本方法必须保持一致。"""
        start = when or self.record_start or datetime.now()
        day = self._session_dirname(self.record_end or start)
        if self.is_test:
            return self.data_dir / "_test" / ("%s_%d" % (
                start.strftime("%Y-%m-%dT%H%M"), self.room_id))
        return self.data_dir / day / str(self.room_id)

    def _open_slice(self):
        """打开(append)一个新数据片并计算下次滚动时刻; _open_file 与滚动切片共用。"""
        now = datetime.now()
        date_dir = self._session_dir_path(now)  # 会话归属(比赛日)目录
        date_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = date_dir / self._slice_name()
        self.file = open(self.file_path, "a", encoding="utf-8", newline="\n")
        self._next_rot_at = self._calc_next_rot(now)
        log.info("数据片(append): %s", self.file_path)

    def _open_file(self):
        self.record_start = datetime.now()
        prefix = "test_%s_" % self.record_start.strftime("%H%M%S") if self.is_test else ""
        self._base_prefix = "%s%d" % (prefix, self.room_id)
        self._parts = []
        # 指纹 sidecar 一晚一份不切片, 与弹幕同一会话目录同基础名。路径先定, 才能在开片前
        # 归档同目录里的旧账本(P0-2)。
        self._afp_path = (self._session_dir_path() / ("%s.afp.jsonl" % self._base_prefix)
                          if self._afp_enabled else None)
        self._archive_stale_ledgers()  # 收尾未执行(被强制终止)的旧会话账本, 先归档再开写
        self._open_slice()
        if self._afp_path is not None:  # 异步启动, 失败自吞
            self._afp_task = asyncio.create_task(self._start_afp())

    def _archive_stale_ledgers(self):
        """新会话开始时归档目标目录里已存在的同名账本(meta / afp)。
        为什么: 旧逻辑只在 finalize 里检查"本进程即将写出的那几个路径", 等于只看自己;
        被强制终止(Windows 关闭控制台窗口 = CTRL_CLOSE_EVENT, Python 收不到信号, finally 不执行)
        的会话不写 meta, 下次启动发现 meta 不存在就直接覆盖, 旧账本彻底丢失(已真实发生)。
        另: afp 是 append 模式(live_audiofp._open_output), 不归档会把两次会话的指纹混进同一文件。
        切片 _sHHMM 不归档: 服务端靠文件名通配聚合, 改名会丢数据可见性。"""
        meta_path = self._meta_path_in(self._session_dir_path())
        if meta_path is not None and meta_path.exists() and not self._is_same_session_meta(meta_path):
            log.warning("发现上次会话残留的 meta(未正常收尾?), 先归档: %s", meta_path.name)
            self._archive_existing([meta_path])
        afp_path = self._afp_path
        if afp_path is None:
            return
        with contextlib.suppress(OSError):
            if afp_path.exists() and afp_path.stat().st_size > 0:
                log.warning("发现上次会话残留的 afp 指纹(未正常收尾?), 先归档再开写: %s", afp_path.name)
                self._archive_afp(afp_path)
        # 还要归档**已压缩**的同名 afp 副本(finalize 正常收尾会产出 {base}.afp.jsonl.gz)。
        # 为什么必须管它: 服务端 find_afp_paths 会同时匹配 {file}.afp.jsonl 与
        # {file}.afp.jsonl.gz 并**合并**读取。若上一场收尾留下 gz、本场又新写明文, 两份指纹
        # 描的是不同时段却被当成同一份拼接, 校准会拿到错位的指纹。
        # 另外, 本场的 finalize 结尾会用同名 gz 覆盖写, 不先归档就会把旧指纹冲掉。
        gz_path = afp_path.with_name(afp_path.name + ".gz")
        with contextlib.suppress(OSError):
            if gz_path.exists() and gz_path.stat().st_size > 0:
                log.warning("发现上次会话残留的 afp 压缩副本, 先归档: %s", gz_path.name)
                self._archive_afp(gz_path)

    def _archive_afp(self, afp_path):
        """归档一份 afp 账本, 落成 {stem}_runN.afp.jsonl.gz(**始终是 gz 形态**)。

        输入可能是明文(raw, 本场刚写的)或已是 gz(上一场正常收尾留下的副本), 分别处理:
          * 明文: 直接压进不冲突的 runN gz, 写完再删明文(中途失败则明文原样保留)。
          * gz  : **只改名, 绝不再压**。对 gz 再 gzip 一次会得到"解压后仍是 gz 字节"的双重
                  压缩文件, 服务端 open_text 解一层后拿到的是二进制, 解析全失败 —— 指纹等于丢。
        两者都不覆盖已存在的 runN(序号取不冲突的最小值)。
        服务端 find_afp_paths 只认 {file}.afp.jsonl[.gz] 与 {file}_runN.afp.jsonl.gz。"""
        dst = self._afp_archive_target(afp_path)
        if dst is None:
            return
        if afp_path.name.endswith(".gz"):
            try:
                afp_path.replace(dst)          # 同一目录内改名, 原子且无需重新压缩
                log.info("归档旧产物: %s -> %s", afp_path.name, dst.name)
            except OSError as e:
                log.error("归档 %s 失败: %s", afp_path.name, e)
            return
        if self._gzip_file(afp_path, dst):
            log.info("归档旧产物: %s -> %s", afp_path.name, dst.name)

    @staticmethod
    def _afp_archive_target(afp_path):
        """{stem}_runN.afp.jsonl.gz, N 取不冲突的最小值(与 server.find_afp_paths 通配一致)。"""
        name = afp_path.name
        for tail in (".afp.jsonl.gz", ".afp.jsonl"):
            if name.endswith(tail):
                base = name[: -len(tail)]
                break
        else:
            return None
        n = 2
        while True:
            dst = afp_path.with_name("%s_run%d.afp.jsonl.gz" % (base, n))
            if not dst.exists():
                return dst
            n += 1

    def _is_same_session_meta(self, path):
        """meta 的 record_start 与本会话一致(极端: 同一秒内重启)才算同一会话, 否则归档。"""
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("record_start")
        except (OSError, ValueError):
            return False
        return bool(old) and old == fmt_dt(self.record_start)

    def _maybe_rotate(self):
        """到点则滚动切片。两处调用: _ws_loop(收包间隙) 与 _aux_loop(每 1s 心跳)。
        为什么两处: 收包间隙在断线重连等待期/下播等待重开期根本不存在, 跨整点就不切片,
        片名失真且 _parts 缺片。 _rotating 防重入: 二者虽同属单事件循环串行执行, 但 _rotate
        内部若被将来引入 await 也会重入, 加个布尔最稳。
        失败后退避 ROT_RETRY_SEC: 开新片失败时 _next_rot_at 仍是过去时刻, 1s 循环会每秒重试刷屏。"""
        if self._next_rot_at is None or datetime.now() < self._next_rot_at:
            return
        if self._rotating or time.monotonic() < self._rot_retry_at:
            return
        self._rotating = True
        try:
            self._rotate()
            if self.file is None:  # _open_slice 失败: 退避, 别每秒打一条 error
                self._rot_retry_at = time.monotonic() + ROT_RETRY_SEC
        finally:
            self._rotating = False

    def _rotate(self):
        """滚动切片(仅弹幕文件): close 当前 raw -> gzip 成同名 .jsonl.gz + unlink raw
        (gz 已存在则先 _archive_existing 归档) -> 开新片(append)。
        在收包间隙同步执行(单事件循环, 无并发写); afp sidecar / meta / dm_count 等
        总账全部不受影响、不重启。任何失败不丢数据: gzip 失败则保留 raw 原地。"""
        old_path = self.file_path
        f, self.file = self.file, None
        if f is not None:
            with contextlib.suppress(Exception):
                f.flush()
                f.close()
        if old_path is not None and old_path.exists():
            gz_path = Path(str(old_path) + ".gz")
            try:
                if gz_path.exists():  # 同名 gz 已存在(同分钟重启等罕见场景), 先归档防覆盖
                    self._archive_existing([gz_path])
                with open(old_path, "rb") as src_f, gzip.open(gz_path, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                old_path.unlink()  # gz 完整写出后移除 raw, 保持目录整洁; 失败则保留
                self._parts.append(old_path.name)
                log.info("滚动切片: %s -> %s (已完成片数 %d)", old_path.name, gz_path.name,
                         len(self._parts))
            except Exception as e:
                log.error("滚动切片 gzip 失败(保留 raw 原地, 服务端仍可读): %s", e)
                self._parts.append(old_path.name)
        try:
            self._open_slice()
        except Exception as e:
            log.error("开新数据片失败(下一轮重试): %s", e)
            self.file, self.file_path = None, None

    async def _start_afp(self):
        """启动音频指纹 sidecar; 模块缺失/任何异常都只降级告警, 绝不影响弹幕采集。"""
        try:
            from live_audiofp import AudioFingerprinter
        except Exception as e:
            log.warning("音频指纹模块不可用(本次禁用, 不影响弹幕): %s", e)
            return
        try:
            afp = AudioFingerprinter()
            await afp.start(self.room_id, self.session, self._afp_path)
            self.afp = afp
        except Exception as e:
            log.warning("音频指纹启动失败(忽略, 不影响弹幕): %s", e)

    async def _interruptible_sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            remain = end - time.monotonic()
            if remain <= 0 or self.stop_event.is_set() or self._live_ended:
                return
            await asyncio.sleep(min(0.5, remain))

    async def _wait_for_live(self):
        self._live_ended = False
        fails = 0
        await self._warmup()
        while not self.stop_event.is_set():
            try:
                status, title, rid = await self._get_info()
                self._note_info(title, rid)
                fails = 0
                if status == 1:
                    log.info("房间 %s 已开播: %s", self.room_id, self.title)
                    return
                log.info("房间 %s 未开播(live_status=%s), %d 秒后再查", self.room_id, status, self.poll_interval)
            except Exception as e:
                fails += 1
                log.warning("get_info 异常(%d/%d): %s", fails, API_FAIL_LIMIT, e)
                if fails >= API_FAIL_LIMIT:  # 风控/网络持续异常: 直连 WS, 能收到弹幕即在播
                    log.warning("API 连续 %d 次异常, 跳过轮询直接尝试连接 WS", API_FAIL_LIMIT)
                    return
            await self._interruptible_sleep(self.poll_interval)

    async def _get_info(self):
        async with self.session.get(INFO_URL.format(self.room_input), timeout=REQ_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError("get_info code=%s msg=%s" % (data.get("code"), data.get("message")))
        d = data["data"]
        return int(d.get("live_status", 0)), d.get("title"), d.get("room_id")

    async def _warmup(self):
        # 访问主站首页让 cookie jar 获得 buvid3/b_nut 指纹(2025 起风控必需); 失败不阻断
        try:
            async with self.session.get(HOME_URL, timeout=PAGE_TIMEOUT) as r:
                await r.read()
            for c in self.session.cookie_jar:
                if c.key == "buvid3":
                    self.buvid3 = c.value
        except Exception as e:
            log.warning("指纹预热失败(继续): %s", e)

    async def _fetch_token(self):
        # APP 端签名接口不走网页风控(-352); token 即 WS 认证 key, 顺带取就近 WS host 列表
        params = {"actionKey": "appkey", "build": "6640400", "channel": "bili",
                  "device": "android", "mobi_app": "android", "platform": "android",
                  "room_id": self.room_id, "ts": int(time.time())}
        params = dict(sorted({**params, "appkey": APPKEY}.items()))
        query = urlencode(params)
        params["sign"] = hashlib.md5((query + APPSEC).encode()).hexdigest()
        async with self.session.get(APP_DANMU_URL, params=params,
                                    headers={"User-Agent": APP_UA}, timeout=REQ_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError("getDanmuInfo code=%s msg=%s" % (data.get("code"), data.get("message")))
        d = data["data"] or {}
        self.token = d.get("token")
        self.ws_hosts = [h.get("host") for h in (d.get("host_list") or []) if h.get("host")]

    def _note_info(self, title, rid):
        if rid and int(rid) != self.room_id:
            log.info("真实房间号: %s -> %s", self.room_input, rid)
            self.room_id = int(rid)
        if title and title != self.title:
            if self.title is not None:  # 运行中标题变化(常对应换场), 记为对齐标记
                self.title_events.append({"ts_ms": now_ms(), "title": title})
                log.info("房间标题变化: %s", title)
            self.title = title

    async def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _record_session(self):
        """录制阶段(含断线重连), 返回 'stopped'|'live_end'。"""
        self._live_ended = False
        backoff, disconnect_ms = BACKOFF_BASE, None
        aux = asyncio.create_task(self._aux_loop())
        try:
            while not self.stop_event.is_set():
                if await self._connect_auth():
                    if disconnect_ms is not None:
                        to = now_ms()
                        self.gaps.append({"from_ms": disconnect_ms, "to_ms": to})
                        self.reconnect_count += 1
                        disconnect_ms = None
                        log.info("重连成功(第 %d 次), 数据缺口 %d ms", self.reconnect_count, to - self.gaps[-1]["from_ms"])
                    backoff = BACKOFF_BASE
                    result = await self._ws_loop()
                    if result == "stopped":
                        return "stopped"
                    if result != "live_end":
                        disconnect_ms = now_ms()
                        log.warning("连接断开(%s), %d 秒后重连", result, backoff)
                elif disconnect_ms is None:
                    disconnect_ms = now_ms()
                if self._live_ended:
                    self._mark_event("live_end")
                    log.info("直播已结束, 关闭 WS, 转入等待重开")
                    return "live_end"
                await self._interruptible_sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            return "stopped"
        finally:
            aux.cancel()
            with contextlib.suppress(BaseException):
                await aux

    async def _connect_auth(self):
        await self._warmup()  # 重连同样重新取指纹 cookie
        try:
            await self._fetch_token()  # token 有时效, 每次连接前取新 token
        except Exception as e:
            log.warning("获取弹幕 token 失败(降级裸连): %s", e)
            self.token = None
        host = self.ws_hosts[0] if self.ws_hosts else WS_HOST
        try:
            ws = await asyncio.wait_for(self.session.ws_connect("wss://%s/sub" % host), WS_TIMEOUT)
        except Exception as e:
            log.warning("WS 连接失败(%s): %s", host, e)
            return False
        self._ws = ws
        self._last_packet = time.monotonic()
        auth = {"uid": 0, "roomid": self.room_id, "protover": PROTOVER_BROTLI,
                "buvid": self.buvid3 or "", "platform": "web", "type": 2}
        if self.token:
            auth["key"] = self.token
        body = json.dumps(auth, separators=(",", ":")).encode()
        try:
            await ws.send_bytes(pack(OP_AUTH, body))
        except Exception as e:
            log.warning("发送认证包失败: %s", e)
            await self._close_ws()
            return False
        log.info("WS 已连接(%s), 认证包已发送(房间 %s)", host, self.room_id)
        return True

    async def _ws_loop(self):
        """收包直到断开/停止/直播结束。返回 'stopped'|'live_end'|'dead'|'auth_fail'。"""
        ws, result = self._ws, "dead"
        hb = asyncio.create_task(self._heartbeat_loop(ws))
        wd = asyncio.create_task(self._watchdog_loop(ws))
        try:
            while not self.stop_event.is_set() and not self._live_ended:
                # 滚动切片检查: 收包间隙同步执行(receive 短超时兜底, 静默时每 2s 也有机会)
                self._maybe_rotate()
                # receive 带短超时: 服务器静默时优雅 close 唤不醒挂起的收包等待(实测拖 ~50s), 靠超时轮询退出
                try:
                    msg = await ws.receive(timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type is not aiohttp.WSMsgType.BINARY:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                self._last_packet = time.monotonic()  # 任意包(含心跳响应)都喂看门狗
                try:
                    self._handle_buffer(msg.data, 0)
                except AuthError as e:
                    log.warning("认证失败: %s", e)
                    result = "auth_fail"
                    break
                except Exception as e:
                    self.parse_err_count += 1
                    log.debug("包处理异常(跳过该包): %s", e)
        finally:
            for t in (hb, wd):
                t.cancel()
            try:
                await asyncio.gather(hb, wd, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            await self._close_ws()
        return "stopped" if self.stop_event.is_set() else ("live_end" if self._live_ended else result)

    async def _heartbeat_loop(self, ws):
        try:
            while True:
                await ws.send_bytes(pack(OP_HEARTBEAT, b""))
                await asyncio.sleep(HEARTBEAT_SEC)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("心跳发送失败(连接将由收包循环退出): %s", e)
            await self._close_ws()

    async def _watchdog_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(5)
                if time.monotonic() - self._last_packet > WATCHDOG_SEC:
                    log.warning("看门狗: 超过 %d 秒未收到任何包, 判定连接死亡", WATCHDOG_SEC)
                    await self._close_ws()
                    return
        except asyncio.CancelledError:
            pass

    async def _aux_loop(self):
        """会话级辅助: 定时 flush / 进度日志 / 滚动切片兜底 / live_status 复核 / 停止时关闭 WS。"""
        last_flush = last_prog = last_check = time.monotonic()
        live_zero_streak = 0  # 连续 live_status==0 次数(中间一次恢复即清零)
        try:
            while True:
                await asyncio.sleep(1)
                now = time.monotonic()
                if self.stop_event.is_set():
                    await self._close_ws()
                # 滚动切片兜底: 断线重连/等待重开期间收包循环不跑, 靠这里保证整点仍然切片刻盘
                self._maybe_rotate()
                if now - last_flush >= FLUSH_SEC:
                    last_flush = now
                    if self.file:
                        self.file.flush()
                if now - last_prog >= PROGRESS_SEC:
                    last_prog = now
                    log.info("房间 %s 进度: 弹幕 %d, SC %d, 其他cmd %d, 重连 %d, 解析失败 %d, 写入失败 %d",
                             self.room_id, self.dm_count, self.sc_count,
                             self.other_cmd_count, self.reconnect_count, self.parse_err_count,
                             self.write_err_count)
                if not self.is_test and now - last_check >= RECHECK_SEC:  # 测试模式不复核下播
                    last_check = now
                    try:
                        status, title, rid = await self._get_info()
                        self._note_info(title, rid)
                        if status == 0:
                            # 单次 status==0 就判下播会被接口抖动/瞬时错误提前结束录制,
                            # 故需连续 LIVE_END_CONFIRM 次(streak 在恢复时清零)
                            live_zero_streak += 1
                            if live_zero_streak >= LIVE_END_CONFIRM:
                                log.info("复核: live_status 连续 %d 次为 0, 判定下播",
                                         live_zero_streak)
                                self._live_ended = True
                                await self._close_ws()
                            else:
                                log.warning("复核: live_status=0(%d/%d), %d 秒后再确认",
                                            live_zero_streak, LIVE_END_CONFIRM, RECHECK_SEC)
                        else:
                            if live_zero_streak:
                                log.info("复核: live_status=%s 已恢复, 下播计数清零", status)
                            live_zero_streak = 0
                    except Exception as e:
                        log.warning("复核 get_info 异常(忽略, 不影响下播计数): %s", e)
        except asyncio.CancelledError:
            pass

    def _handle_buffer(self, buf, depth):
        """解析同格式包序列; op=5 且压缩时, 解压后仍是包序列(递归, 限深防嵌套炸弹)。"""
        off, total = 0, len(buf)
        while off + HEADER_LEN <= total:
            pack_len, header_len, protover, op, _seq = HEADER.unpack_from(buf, off)
            if pack_len < HEADER_LEN or header_len < HEADER_LEN or off + pack_len > total:
                raise ValueError("包长非法: pack_len=%d" % pack_len)
            body = buf[off + header_len: off + pack_len]
            off += pack_len
            if op == OP_MESSAGE and depth < 3:
                if protover == PROTOVER_BROTLI:
                    self._handle_buffer(brotli.decompress(body), depth + 1)
                elif protover == PROTOVER_ZLIB:
                    self._handle_buffer(zlib.decompress(body), depth + 1)
                else:
                    self._handle_message(body)
            elif op == OP_MESSAGE:
                self._handle_message(body)
            elif op == OP_AUTH_REPLY:
                self._handle_auth_reply(body)
            # op=3 心跳响应与未知 op: 忽略

    def _handle_auth_reply(self, body):
        try:
            code = json.loads(body.decode("utf-8", "replace")).get("code", 0)
        except Exception:
            self.parse_err_count += 1
            log.debug("认证响应解析失败, 视作通过, 由后续包/看门狗验证")
            return
        if code != 0:
            raise AuthError("code=%s" % code)
        log.info("WS 认证成功(房间 %s)", self.room_id)

    def _handle_message(self, body):
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            self.parse_err_count += 1
            log.debug("业务消息 JSON 解析失败(跳过)")
            return
        cmd = obj.get("cmd", "")
        if cmd == "SUPER_CHAT_MESSAGE":
            d = obj.get("data") or {}
            self._write_row(now_ms(), (d.get("user_info") or {}).get("uname"), d.get("message"), "sc", d.get("price"))
        elif cmd == "WATCHED_CHANGE":  # 在线看过人数: 每30秒采样一条, 人气曲线与弹幕密度互补
            d = obj.get("data") or {}
            num = d.get("num")
            if isinstance(num, int) and time.monotonic() - self._last_watch_log >= 30:
                self._last_watch_log = time.monotonic()
                self.watched.append({"ts_ms": now_ms(), "num": num})
        elif cmd.startswith("DANMU_MSG"):  # 兼容 "DANMU_MSG:4:0" 等带后缀变体
            info = obj.get("info") or []
            if len(info) > 2 and isinstance(info[2], list) and len(info[2]) > 1:
                # ts 用消息体服务器时间(msg_ts), 回落本机墙钟; SC 无该字段仍用 now_ms()
                self._write_row(msg_ts(info), info[2][1], info[1] if len(info) > 1 else None, "dm")
            else:
                self.parse_err_count += 1
        else:
            self.other_cmd_count += 1
            log.debug("忽略 cmd=%s", cmd)

    def _write_row(self, ts, un, text, ty, price=None):
        if text is None or self.file is None:
            self.parse_err_count += 1
            return
        row = {"ts": int(ts), "un": str(un or ""), "t": str(text), "ty": ty}
        if ty == "sc":
            row["price"] = int(price or 0)
        # 写盘单独 try: 磁盘满/句柄失效时若让异常冒到 _ws_loop 的 except, 会被计成 parse_err_count
        # 且只 log.debug, 表面"还在录"实则整晚弹幕静默丢光。这里显式计数 + 限流 error 告警。
        try:
            self.file.write(json.dumps(row, ensure_ascii=False) + "\n")  # 每条立即写入
        except Exception as e:
            self._note_write_error(e)
            return
        self._write_err_streak = 0
        if ty == "dm":
            for kw in EVENT_KWS:  # 事件关键词标记, 供对齐校准
                if kw in str(text):
                    self.kw_events.append({"ts_ms": int(ts), "kw": kw})
                    break
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = int(ts)
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = int(ts)
        if ty == "dm":
            self.dm_count += 1
            now = time.monotonic()
            if now - self._last_sample_log >= 10:  # 抽样打印一条, 供控制台/日志肉眼确认在抓
                self._last_sample_log = now
                log.info("弹幕样例[第%d条]: %s", self.dm_count, str(text)[:40])
        else:
            self.sc_count += 1

    def _note_write_error(self, exc):
        """记录一次写盘失败: 计数 + 限流 error(每 60s 最多一条, 带累计数);
        连续失败达 WRITE_ERR_ALARM 次额外明确提示数据正在丢失。"""
        self.write_err_count += 1
        self._write_err_streak += 1
        now = time.monotonic()
        if self._write_err_streak >= WRITE_ERR_ALARM:
            # 每满一个阈值都报(不跟限流走), 否则盘满时可能只看到一条轻描淡写的日志
            log.error("!! 弹幕连续写入失败 %d 次, 数据正在丢失(累计 %d 条), 请立即检查磁盘/句柄: %s",
                      self._write_err_streak, self.write_err_count, exc)
        elif now - self._last_write_err_log >= WRITE_ERR_LOG_SEC:
            self._last_write_err_log = now
            log.error("弹幕写盘失败(本次已丢 %d 条, 连续 %d 次): %s",
                      self.write_err_count, self._write_err_streak, exc)

    def _mark_event(self, etype):
        self.stream_events.append({"ts_ms": now_ms(), "type": etype})

    def _meta_path_in(self, base_dir):
        """给定会话目录, 本次会话 meta 的路径(开片前也能算, 供归档旧账本用)。"""
        if self.meta_name:
            return base_dir / self.meta_name
        return base_dir / ("%s.meta.json" % self._base_prefix)

    def _meta_target_path(self):
        """本次会话 meta 的目标路径; 尚未开片(file_path is None, 如整夜未开播)返回 None。
        旧逻辑 stem 兜底成 "meta" 会写出 data/meta.meta.json, 还会被 dashboard 当最近结果展示。"""
        if self.file_path is None:
            return None
        if self.meta_name:
            return self.file_path.parent / self.meta_name
        # 剥离滚动片 _sHHMM(及防御性 _runN)后缀取本次 session 主名:
        # 正式 -> {room}.meta.json(固定名, 不随切片变); 测试 -> test_HHMMSS_{room}.meta.json(维持现规则)
        stem = SLICE_SUFFIX_RE.sub("", self.file_path.stem)
        return self.file_path.parent / (stem + ".meta.json")

    def _archive_existing(self, paths):
        """同日旧 session 产物改名归档(多个产物共用同一 _runN 后缀, 便于按次对应),
        防止 finalize 无条件覆盖丢数据(2026-10-25 事故修复)。
        返回 [(原路径, 归档后路径)] 供调用方后续处理。"""
        stale = [p for p in paths if p is not None and p.exists()]
        if not stale:
            return []

        n = 2
        while any(archive_name(p, n).exists() for p in stale):  # 共用同一后缀序号
            n += 1
        moved = []
        for p in stale:
            try:
                dst = archive_name(p, n)
                p.rename(dst)
                moved.append((p, dst))
                log.info("归档旧产物: %s -> %s", p.name, dst.name)
            except Exception as e:
                log.error("归档 %s 失败: %s", p, e)
        return moved

    @staticmethod
    def _gzip_file(src, dst):
        """gz 写完整后才删 raw; 失败保留 raw 原地并返回 False。"""
        try:
            with open(src, "rb") as src_f, gzip.open(dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            src.unlink()
            return True
        except Exception as e:
            log.error("生成 %s 失败: %s", dst, e)
            return False

    async def finalize(self, ended_hint=None):
        record_end = datetime.now()
        self.record_end = record_end          # 赛事日/manifest 依赖它
        await self._close_ws()
        # 音频指纹 sidecar 先停(内部自吞异常), 再与其余产物一并归档/gzip
        afp_task, self._afp_task = self._afp_task, None
        if afp_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(afp_task, 10)
        afp, self.afp = self.afp, None
        if afp is not None:
            try:
                await afp.stop()
            except Exception as e:
                log.warning("音频指纹停止失败(忽略): %s", e)
        if self.file is not None:
            with contextlib.suppress(Exception):
                self.file.flush()
                self.file.close()
            self.file = None
        ended = ended_hint or self.reason.get("value") or "interrupt"
        gz_path = None
        meta_path = self._meta_target_path()
        if self.file_path is not None and self.file_path.exists():
            gz_path = Path(str(self.file_path) + ".gz")
        # 弹幕切片/meta 走统一 _runN 归档。afp 单独处理(见下): 它的归档名规则不同,
        # 且 gzip.open("wb") 会静默覆盖同名 gz, 先归档到明文再压有覆盖旧指纹的风险。
        self._archive_existing([gz_path, meta_path])  # 弹幕/meta 共用归档序号
        afp_gz = None
        if self._afp_path is not None and self._afp_path.exists():
            plain_gz = Path(str(self._afp_path) + ".gz")
            if plain_gz.exists():  # 上次会话留下的同名 gz: 先按 afp 规则归档, 绝不覆盖
                self._archive_afp(plain_gz)
            afp_gz = plain_gz
        if gz_path is not None and not self._gzip_file(self.file_path, gz_path):
            gz_path = None
        if afp_gz is not None and not self._gzip_file(self._afp_path, afp_gz):
            afp_gz = None
        if self.file_path is not None and self.file_path.name not in self._parts:
            self._parts.append(self.file_path.name)  # 最后活动片也计入 parts(gzip 失败保留 raw 同样计入)
        meta = {"room": self.room_id, "room_title": self.title,
                "record_start": fmt_dt(self.record_start), "record_end": fmt_dt(record_end),
                "first_ts_ms": self.first_ts, "last_ts_ms": self.last_ts,
                "dm_count": self.dm_count, "sc_count": self.sc_count,
                "reconnect_count": self.reconnect_count, "gaps": self.gaps,
                "stream_events": self.stream_events, "ended_by": ended, "version": VERSION,
                "watched": self.watched, "kw_events": self.kw_events,
                "title_events": self.title_events, "parts": list(self._parts),
                "write_err_count": self.write_err_count,  # 写盘失败丢条数, 供事后判断数据完整性
                "audio_fp_gz": afp_gz.name if afp_gz else None}
        meta_path = self._meta_target_path()
        if meta_path is None:
            # 整夜未开播: 从未开过数据片, 不写 meta(data/meta.meta.json 这类垃圾文件)
            log.warning("本会话未产生任何数据片(整夜未开播?), 跳过写 meta")
        else:
            self._write_meta_atomic(meta_path, meta)
        log.info("== 房间 %s 摘要: 弹幕 %d, SC %d, 重连 %d, gaps=%d, 首条=%s, 末条=%s, ended_by=%s",
                 self.room_id, self.dm_count, self.sc_count, self.reconnect_count,
                 len(self.gaps), ts_str(self.first_ts), ts_str(self.last_ts), ended)
        if self.write_err_count:
            log.warning("本次会话有 %d 条因写盘失败丢失(见 write_err_count)", self.write_err_count)
        if afp_gz:
            log.info("音频指纹 gzip 副本: %s", afp_gz)
        if gz_path:
            log.info("gzip 副本: %s", gz_path)
        if meta_path:
            log.info("meta 已写入: %s", meta_path)
        # manifest.json: 本会话的**权威索引**, 服务端优先读它(见 SCHEMA.md)。
        # 与 meta 的分工: meta 是给人和旧工具看的统计摘要(字段会演进);
        # manifest 是给程序读的稳定契约(带 schema_version, 路径/时间/计数都在里面)。
        self._write_manifest_atomic(meta_path, afp_gz, meta)

    def _manifest_target_path(self):
        """manifest 总是写在会话目录根(与 meta 同目录)。"""
        mp = self._meta_target_path()
        return mp.parent / "manifest.json" if mp is not None else None

    def _write_manifest_atomic(self, meta_path, afp_gz, meta):
        """写 manifest.json(schema_version 契约)。失败只告警, 不影响 other 产物。"""
        manifest_path = self._manifest_target_path()
        if manifest_path is None:
            return
        try:
            day = self._session_dirname(self.record_end or self.record_start)
            files = [p for p in self._parts]
            # start_ms/end_ms 优先用真实弹幕首末条(最可靠), 缺失时退回 record_start/end
            start_ms = self.first_ts or (
                int(self.record_start.timestamp() * 1000) if self.record_start else None)
            end_ms = self.last_ts or (
                int(self.record_end.timestamp() * 1000) if self.record_end else None)
            mf = {
                "schema_version": 1,
                "room": self.room_id,
                "match_date": day,
                "is_test": bool(self.is_test),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "dm_count": self.dm_count,
                "sc_count": self.sc_count,
                "files": files,
                "afp": ({"file": afp_gz.name if afp_gz else None,
                         "hop": 0.2, "win": 0.4} if self._afp_path is not None else None),
                "meta": meta_path.name if meta_path else None,
                "collector_version": VERSION,
                "ended_by": meta.get("ended_by"),
            }
            tmp = manifest_path.with_name(manifest_path.name + ".tmp")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(manifest_path)
            log.info("manifest 已写入: %s", manifest_path)
        except Exception as e:
            log.error("写 manifest.json 失败(不影响弹幕数据): %s", e)

    def _write_meta_atomic(self, meta_path, meta):
        """归档已存在的 meta(无论来源) -> tmp 原子写入 -> Path.replace 顶上。
        绝不用裸 write_text 直接覆盖: 写到一半被杀(关窗口)会留下半截 JSON, 服务端读 meta 直接抛错;
        旧的同名 meta 也可能来自上次未正常收尾的会话, 直接覆盖等于丢账本。"""
        with contextlib.suppress(Exception):
            if meta_path.exists():
                self._archive_existing([meta_path])
        tmp = meta_path.with_name(meta_path.name + ".tmp")
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(meta_path)  # 同盘 rename 原子; 服务端任何时刻读到的都是完整 JSON
        except Exception as e:
            log.error("写 meta.json 失败: %s", e)
            with contextlib.suppress(Exception):
                if tmp.exists():
                    tmp.unlink()  # 不留 .tmp 残留


async def amain(cfg, args, stop_event, reason):
    timers = []
    try:
        async with aiohttp.ClientSession(
                headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}) as session:
            if args.test is not None:
                room = args.room if args.room else cfg["rooms"][0]
                log.info("测试模式: 房间 %s, 时长 %s 秒", room, args.test)
                timers.append(asyncio.create_task(
                    run_deadline_timer(args.test, "test_done", stop_event, reason)))
                await Recorder(session, room, cfg, stop_event, reason,
                               is_test=True, meta_name=None).run()
                return
            rooms = [args.room] if args.room else cfg["rooms"]
            await wait_until(parse_earliest(cfg["earliest_start"], datetime.now()),
                             "等待最早开始时刻", stop_event)
            if stop_event.is_set():
                return
            if cfg["latest_end"]:
                end_at = next_occurrence(cfg["latest_end"], datetime.now())
                log.info("最晚结束时刻: %s", fmt_dt(end_at))
                timers.append(asyncio.create_task(
                    run_deadline_timer((end_at - datetime.now()).total_seconds(),
                                       "latest_end", stop_event, reason)))
            # 数据文件同名的 .meta.json: 正式 {room}.meta.json, 测试 test_HHMMSS_{room}.meta.json
            recs = [Recorder(session, r, cfg, stop_event, reason, is_test=False,
                             meta_name=None) for r in rooms]
            await asyncio.gather(*(asyncio.create_task(r.run()) for r in recs))
    finally:
        for t in timers:
            t.cancel()


def main():
    args = parse_args()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")
    cfg = load_config(args)
    setup_logging(cfg["log_dir"])
    log.info("collector v%s 启动", VERSION)
    log.info("配置: rooms=%s earliest_start=%s latest_end=%s poll=%ds data=%s log=%s",
             cfg["rooms"], cfg["earliest_start"], cfg["latest_end"],
             cfg["poll_interval_sec"], cfg["data_dir"], cfg["log_dir"])
    if args.test is not None:
        log.info("--test 模式: %s 秒", args.test)
    if not cfg["rooms"]:
        log.error("rooms 为空, 退出")
        return 2
    # 单实例锁: 双开会同时 append 同一数据片 / afp 段号互撞, 直接损坏整晚数据
    lock = SingleInstanceLock(cfg["log_dir"] / "collector.lock")
    if not lock.acquire():
        log.error("另一个 collector 实例正在运行(锁文件 %s 被占用), 本次退出", lock.path)
        lock.release()
        return 2
    try:
        return _run_main(cfg, args)
    finally:
        lock.release()


def _run_main(cfg, args):
    try:
        parse_earliest(cfg["earliest_start"], datetime.now())
        if cfg["latest_end"]:
            next_occurrence(cfg["latest_end"], datetime.now())
    except (ValueError, TypeError) as e:
        log.error("时间配置格式错误: %s", e)
        return 2
    set_keep_awake(True)
    stop_event = asyncio.Event()
    reason = {"value": None}
    install_signals(stop_event, reason)
    try:
        asyncio.run(amain(cfg, args, stop_event, reason))
    except KeyboardInterrupt:
        log.info("键盘中断, 退出")
    except Exception:
        log.exception("主流程异常")
        return 1
    finally:
        set_keep_awake(False)
    log.info("退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
