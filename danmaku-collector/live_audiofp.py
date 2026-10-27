# -*- coding: utf-8 -*-
"""live_audiofp.py — 弹幕采集器音频指纹 sidecar（自包含模块）。

为夜间弹幕采集附带记录直播音频指纹(.afp.jsonl), 供次日把弹幕时间轴对齐到官方回放:
  - 指纹算法与播放端 danmaku-server/audiofp.py **逐 bit 等价**(两侧同名函数必须同步修改):
    分帧 -> 汉明窗 FFT -> 33 个对数频带能量 -> log10 -> 每帧 L2 归一化;
  - 拉流走 web_v2 路径(buvid3 预热 + 浏览器 UA/Referer, 两步取最低 qn 的 flv);
  - ffmpeg(imageio-ffmpeg 自带二进制) -vn 解码为 16kHz 单声道 s16le PCM, 增量算指纹。

afp 文件契约(UTF-8 JSONL, 与弹幕数据同目录同基础名):
  {"ty":"hdr","v":1,"sr":16000,"hop":0.2,"win":0.4,"bands":33}        文件首行
  (当前采集端写 hop=0.4; 旧文件是 0.2。读取方必须按各文件 hdr 里的值解释帧,
   不能假定当前默认值 —— 否则历史指纹会被整体拉伸或压缩。)
  {"ty":"conn","c":0,"t0_ms":<该段首样本墙钟>}                        每个拉流连接段
  {"ty":"fp","c":0,"ts":<本批首帧墙钟>,"n":<帧数>,"fp":"<base64>"}    每 60s 一批
  {"ty":"end","c":0,"reason":"reconnect|stop|error"}                  段结束
fp base64 解码为 uint8 (n x 33) row-major; 第 i 帧墙钟 = ts + i*200 (毫秒)。
量化 q = clamp(round((v+1)*127.5), 0, 255), 反量化 v = q/127.5 - 1。

纪律: 本模块任何异常自行吞掉(log.warning 降级), 绝不向 Recorder 抛——弹幕采集是主任务,
指纹是增强, 宁可没有指纹也不能影响弹幕。
"""
import asyncio
import base64
import contextlib
import json
import logging
import shutil
import time
from pathlib import Path

import aiohttp
import numpy as np

log = logging.getLogger("collector.afp")

# ======================= 指纹算法(逐行复制自 alignproto/audiofp.py, 勿改动数学定义) =======================
N_BANDS = 33
F_LO = 250.0   # 频带下界 Hz
F_HI = 5000.0  # 频带上界 Hz
_LOG_EPS = 1e-10  # log10 下限保护（防 log10(0)）
SR = 16000
# hop/win 是**指纹的数学定义**, 与 playback 端 audiofp.py 必须同步(见 tests/test_align.py)。
#
# 为什么 hop 是 0.4 而不是 0.2:
#   每帧看 0.4s 音频。hop=0.4 时相邻帧**首尾相接, 不漏任何音频**; hop 更大就会漏掉
#   中间的样本, 定位能力下降。实测(真实采集 + 真实回放, 4 个分P):
#     hop=0.2/0.4/0.6 → 4/4 对齐(偏差 <0.2s); 0.8 → 3/4; 1.0 → 0/4 全失败
#   win 必须保持 0.4 且**不能随 hop 放大**: 窗中心由 hop 决定, 放大 win 会让窗中心错位,
#   与播放端重算的结果不再等价(实测窗错位后 max 差 0.03, 而非 0)。
#   降频后 afp 从 7MB/晚降到 3.5MB/晚, 误报率不变(无关内容在每档 hop 都被正确拒绝)。
HOP_SEC, WIN_SEC = 0.4, 0.4


def band_bin_edges(sr, nfft, n_bands=N_BANDS, f_lo=F_LO, f_hi=F_HI):
    """返回 (bin_edges int 数组长度 n_bands+1)。对数分布频带的 FFT bin 边界。"""
    edges = np.logspace(np.log10(f_lo), np.log10(f_hi), n_bands + 1)
    bins = np.round(edges * nfft / float(sr)).astype(np.int64)
    bins = np.clip(bins, 0, nfft // 2)
    for i in range(1, len(bins)):  # 严格递增
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1
    if bins[-1] > nfft // 2:
        raise ValueError(f"频带上限超出奈奎斯特: f_hi={f_hi}, sr={sr}")
    return bins


def fingerprint(samples, sr, hop_sec=HOP_SEC, win_sec=WIN_SEC):
    """计算频带能量指纹。返回 (N, 33) float32, N = 1 + (len-win)//hop。"""
    x = np.ascontiguousarray(np.asarray(samples, dtype=np.float32).ravel())
    hop = int(round(hop_sec * sr))
    win = int(round(win_sec * sr))
    if hop < 1 or win < 8:
        raise ValueError("hop/win 太小")
    if len(x) < win:
        raise ValueError(f"音频太短: {len(x)} 采样 < 窗长 {win}")
    n_frames = 1 + (len(x) - win) // hop
    nfft = 1 << (win - 1).bit_length()
    bins = band_bin_edges(sr, nfft)
    starts = bins[:-1]
    w = np.hamming(win).astype(np.float32)
    band_e = np.empty((n_frames, len(starts)), dtype=np.float32)
    chunk = max(1, (1 << 22) // win)
    for i0 in range(0, n_frames, chunk):
        i1 = min(n_frames, i0 + chunk)
        frames = np.lib.stride_tricks.as_strided(
            x[i0 * hop:], shape=(i1 - i0, win),
            strides=(x.strides[0] * hop, x.strides[0])) * w
        spec = np.fft.rfft(frames, n=nfft, axis=1)
        power = spec.real * spec.real + spec.imag * spec.imag
        band_e[i0:i1] = np.add.reduceat(power[:, : bins[-1]], starts, axis=1)
        del frames, spec, power
    feat = np.log10(band_e + _LOG_EPS)
    nrm = np.sqrt((feat * feat).sum(axis=1, keepdims=True))
    nrm[nrm < 1e-12] = 1.0
    return (feat / nrm).astype(np.float32)


def quantize(fp):
    """float 指纹 -> uint8: q = clamp(round((v+1)*127.5), 0, 255)。"""
    return np.clip(np.round((np.asarray(fp, np.float32) + 1.0) * 127.5), 0, 255).astype(np.uint8)


def dequantize(q):
    """uint8 -> float: v = q/127.5 - 1（读端用, 量化误差 ~0.004 对对齐无影响）。"""
    return np.asarray(q, np.float32) / 127.5 - 1.0


class FingerprintAccumulator:
    """增量指纹引擎: 喂入 s16le PCM 字节, 吐出新就绪帧的 float32 指纹。

    帧对齐契约: 段首左边界补 hop 个零样本, 帧 i 覆盖样本 [i*hop-hop, i*hop+hop);
    边界样本不足时缓冲等待。计算路径与 audiofp.fingerprint 逐 bit 等价(同窗/同FFT/同频带/同L2)。
    """

    def __init__(self, sr=SR, hop_sec=HOP_SEC, win_sec=WIN_SEC):
        self.sr = sr
        self.hop = int(round(hop_sec * sr))
        self.win = int(round(win_sec * sr))
        self.frames_done = 0  # 已算完的帧数(下一帧索引)
        self._raw = bytearray()                       # 待转换的 PCM 字节(防奇数字节边界)
        self._buf = np.zeros(self.hop, np.float32)    # 样本缓冲, [0] 恒为下一帧窗起点
        self._w = np.hamming(self.win).astype(np.float32)
        self._nfft = 1 << (self.win - 1).bit_length()
        starts = band_bin_edges(sr, self._nfft)
        self._starts, self._bins_end = starts[:-1], starts[-1]

    def push_pcm(self, data: bytes) -> np.ndarray:
        """喂入 s16le PCM 字节, 返回新就绪的 (k, 33) float32 指纹(可能为空矩阵)。"""
        self._raw.extend(data)
        n = len(self._raw) - (len(self._raw) & 1)  # 只取整样本, 悬空字节留到下一批
        if n:
            take = bytes(self._raw[:n])
            del self._raw[:n]
            self._buf = np.concatenate(
                (self._buf, np.frombuffer(take, "<i2").astype(np.float32) / 32768.0))
        if len(self._buf) < self.win:
            return np.empty((0, N_BANDS), np.float32)
        n_new = (len(self._buf) - self.win) // self.hop + 1
        frames = np.lib.stride_tricks.as_strided(
            self._buf, shape=(n_new, self.win),
            strides=(self._buf.strides[0] * self.hop, self._buf.strides[0])) * self._w
        spec = np.fft.rfft(frames, n=self._nfft, axis=1)
        power = spec.real * spec.real + spec.imag * spec.imag
        band_e = np.add.reduceat(power[:, : self._bins_end], self._starts, axis=1)
        feat = np.log10(band_e + _LOG_EPS)
        nrm = np.sqrt((feat * feat).sum(axis=1, keepdims=True))
        nrm[nrm < 1e-12] = 1.0
        out = (feat / nrm).astype(np.float32)
        self.frames_done += n_new
        self._buf = np.ascontiguousarray(self._buf[n_new * self.hop:])
        return out


# ======================= 拉流地址(复刻 fetch_live_url.py 的 web_v2 路径) =======================
HOME_URL = "https://www.bilibili.com/"
WEB_V2_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
REQ_TIMEOUT = aiohttp.ClientTimeout(total=15)


def extract_v2_streams(playurl_info):
    """v2 结构: playurl.stream[].format[].codec[].url_info[]{host,extra_params} + base_url。"""
    playurl = (playurl_info or {}).get("playurl") or {}
    qn_desc = [q.get("qn") for q in (playurl.get("g_qn_desc") or []) if isinstance(q.get("qn"), int)]
    streams = []
    for st in (playurl.get("stream") or []):
        proto = st.get("protocol_name")
        for fmt in (st.get("format") or []):
            for c in (fmt.get("codec") or []):
                base = c.get("base_url") or ""
                # 实测(2026-10-25) url_info 的查询参数键为 "extra"(fetch_live_url.py 误用
                # "extra_params" 会拼出无签名 URL, CDN 403); 两个键名都兼容
                urls = [ui.get("host", "") + base + (ui.get("extra") or ui.get("extra_params") or "")
                        for ui in (c.get("url_info") or []) if ui.get("host") and base]
                if urls:
                    streams.append({"protocol": proto, "current_qn": c.get("current_qn"), "urls": urls})
    return streams, qn_desc


async def _pick_low_qn_flv(session, room_id):
    """web_v2 两步取流: 先 qn=0 拿 qn 列表, 再取最低 qn; 选 flv 优先的最低清晰度。"""
    async def call(qn):
        params = {"room_id": room_id, "protocol": "0,1", "format": "0,1,2",
                  "codec": "0,1", "qn": qn, "platform": "web"}
        headers = {"User-Agent": UA, "Referer": "https://live.bilibili.com/%d" % room_id,
                   "Origin": "https://live.bilibili.com"}
        async with session.get(WEB_V2_URL, params=params, headers=headers,
                               timeout=REQ_TIMEOUT) as r:
            data = await r.json(content_type=None)
        if data.get("code") != 0:
            return [], []
        return extract_v2_streams((data.get("data") or {}).get("playurl_info"))

    streams, qn_desc = await call(0)
    if streams and qn_desc:
        low, low_desc = await call(min(qn_desc))
        if low:
            streams = low
    if not streams:
        return None

    def key(s):
        qn = s.get("current_qn")
        return (99999 if qn is None else qn, 0 if "flv" in str(s.get("protocol")) else 1)

    best = min(streams, key=key)
    return best["urls"][0]


# ======================= AudioFingerprinter =======================
BATCH_FRAMES = 300      # 每批帧数 = 60s
RECV_TIMEOUT = 60.0     # 秒无 PCM 判死该段(断流自愈)
BACKOFF_BASE, BACKOFF_MAX = 10.0, 300.0  # 重连退避: 10s 起倍增, 上限 5 分钟
RESET_AFTER_SEC = 60.0  # 段存活超过该时长, 重连退避复位


class AudioFingerprinter:
    """音频指纹 sidecar: start() 后台自转(拉流->指纹->落盘->自愈重连), stop() 收尾。

    一切异常内部消化; 对 Recorder 只暴露 start/stop 两个入口, 均不抛出。
    """

    def __init__(self):
        self._room_id = 0
        self._session = None
        self._out = None
        self._out_path = None
        self._task = None
        self._proc = None
        self._stopped = False
        self._c = -1             # 当前段号(尝试开播即 +1; conn 行仅在首字节 PCM 到达时写)
        self._conn_written = False
        self._acc = None
        self._t0 = None          # 本段首样本墙钟 ms
        self._rows = []          # 待落盘的量化帧(凑满 BATCH_FRAMES 写一行)
        self._rows_i0 = 0
        self._n_pend = 0

    # ---------- 对外入口(均不抛出) ----------
    async def start(self, room_id, session, out_path):
        """启动指纹记录(后台任务)。out_path 为 .afp.jsonl 完整路径。"""
        try:
            self._room_id, self._session = int(room_id), session
            self._out_path = Path(out_path)
            self._open_output()
            self._task = asyncio.create_task(self._run())
            log.info("afp 启动: room=%s out=%s", self._room_id, self._out_path)
        except Exception as e:
            log.warning("afp 启动失败(禁用, 不影响弹幕): %s", e)
            self._close_output()

    async def stop(self):
        """停止并收尾(写 end 行/flush/关文件/杀子进程)。幂等, 不抛出。"""
        try:
            self._stopped = True
            await self._kill_proc()
            t, self._task = self._task, None
            if t is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.gather(t, return_exceptions=True), 15)
        except Exception as e:
            log.warning("afp stop 异常(忽略): %s", e)
        finally:
            with contextlib.suppress(Exception):
                self._finish_segment("stop")
            self._close_output()
        log.info("afp 已停止: %s", self._out_path)

    # ---------- 主循环: 段连接 / 自愈重连 ----------
    async def _run(self):
        backoff = BACKOFF_BASE
        try:
            while not self._stopped:
                url = None
                try:
                    url = await self._fetch_stream_url()
                except Exception as e:
                    log.warning("afp 取流地址异常: %s", e)
                if self._stopped:
                    break
                lived = await self._segment(url) if url else 0.0
                if self._stopped:
                    break
                if lived >= RESET_AFTER_SEC:
                    backoff = BACKOFF_BASE
                log.info("afp 段结束(存活 %.1fs), %.0fs 后重连", lived, backoff)
                await self._sleep_interruptible(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        finally:
            with contextlib.suppress(Exception):
                self._finish_segment("stop" if self._stopped else "error")
            self._close_output()

    async def _fetch_stream_url(self):
        session = self._session
        if not any(c.key == "buvid3" for c in session.cookie_jar):  # 风控预热(collector 通常已做过)
            with contextlib.suppress(Exception):
                async with session.get(HOME_URL, timeout=REQ_TIMEOUT) as r:
                    await r.read()
        url = await _pick_low_qn_flv(session, self._room_id)
        if url:
            log.info("afp 取到流地址(qn 最低 flv): %s...", url[:80])
        else:
            log.warning("afp 未取到流地址(未开播或风控)")
        return url

    async def _segment(self, url):
        """跑一段 ffmpeg 直到退出/断流/停止; 返回段存活秒数。"""
        self._begin_segment()
        t_start = time.monotonic()
        # Termux 等环境用系统 ffmpeg; Windows 无系统 ffmpeg 时回落到 imageio-ffmpeg 自带二进制
        exe = shutil.which("ffmpeg")
        if exe is None:
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as e:
                log.warning("afp 找不到 ffmpeg 二进制(%s)", e)
                return 0.0
        args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-user_agent", UA,
                "-headers", "Referer: https://live.bilibili.com/%d\r\n" % self._room_id,
                "-i", url, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            log.warning("afp ffmpeg 启动失败: %s", e)
            self._proc = None
            return 0.0
        err_tail = []
        drain = asyncio.create_task(self._drain(self._proc.stderr, err_tail))
        got_pcm = False
        try:
            while not self._stopped:
                try:
                    chunk = await asyncio.wait_for(self._proc.stdout.read(65536), RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("afp %ds 无 PCM, 判死本段", int(RECV_TIMEOUT))
                    break
                if not chunk:
                    break
                got_pcm = True
                self._feed(chunk)
        except Exception as e:
            log.warning("afp 读 PCM 异常(结束本段): %s", e)
        finally:
            await self._kill_proc()
            drain.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(drain, return_exceptions=True)
            if not got_pcm and err_tail:
                log.warning("afp 本段无 PCM, ffmpeg stderr 尾部: %s", "".join(err_tail)[-400:])
            self._finish_segment("stop" if self._stopped else "reconnect")
        return time.monotonic() - t_start

    async def _drain(self, stderr, buf):
        """排空 stderr(防管道写满阻塞 ffmpeg), 留尾部用于诊断。"""
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    return
                buf.append(chunk.decode("utf-8", "replace"))
                if sum(len(s) for s in buf) > 4000:
                    del buf[:-2]
        except Exception:
            pass

    async def _kill_proc(self):
        p, self._proc = self._proc, None
        if p is None or p.returncode is not None:
            return
        with contextlib.suppress(Exception):
            p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 3)
        except Exception:
            with contextlib.suppress(Exception):
                p.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.wait(), 3)

    async def _sleep_interruptible(self, sec):
        end = time.monotonic() + sec
        while not self._stopped:
            remain = end - time.monotonic()
            if remain <= 0:
                return
            await asyncio.sleep(min(1.0, remain))

    # ---------- 段内: 指纹增量计算与落盘 ----------
    def _begin_segment(self):
        self._c += 1
        self._conn_written = False
        self._t0 = None
        self._acc = FingerprintAccumulator()
        self._rows, self._rows_i0, self._n_pend = [], 0, 0

    def _feed(self, chunk):
        if self._t0 is None:  # 首字节 PCM 墙钟 = 段锚点 t0
            self._t0 = int(time.time() * 1000)
            self._conn_written = True
            self._write({"ty": "conn", "c": self._c, "t0_ms": self._t0})
            log.info("afp 段 %d 首样本 t0=%s", self._c,
                     time.strftime("%H:%M:%S", time.localtime(self._t0 / 1000)))
        rows = quantize(self._acc.push_pcm(chunk))
        if len(rows):
            if not self._rows:
                self._rows_i0 = self._acc.frames_done - len(rows)
            self._rows.append(rows)
            self._n_pend += len(rows)
            if self._n_pend >= BATCH_FRAMES:
                self._flush_rows()

    def _flush_rows(self):
        if not self._rows:
            return
        arr = np.concatenate(self._rows)
        self._write({"ty": "fp", "c": self._c, "ts": self._t0 + self._rows_i0 * 200,
                     "n": int(len(arr)), "fp": base64.b64encode(arr.tobytes()).decode("ascii")})
        self._rows, self._n_pend = [], 0

    def _finish_segment(self, reason):
        self._flush_rows()  # 尾批不足 60s 也照常落盘(n 为实际帧数)
        if self._conn_written:
            self._write({"ty": "end", "c": self._c, "reason": reason})
            self._conn_written = False

    # ---------- 输出文件 ----------
    def _write(self, obj):
        self._out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._out.flush()

    def _open_output(self):
        existed = self._out_path.exists() and self._out_path.stat().st_size > 0
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._out = open(self._out_path, "a", encoding="utf-8", newline="\n")
        if existed:  # 续写: 不重写 hdr, 段号接着已有最大 c
            self._c = self._scan_max_c()
            log.info("afp 续写 %s (已有最大段号 c=%d)", self._out_path.name, self._c)
        else:
            self._write({"ty": "hdr", "v": 1, "sr": SR, "hop": HOP_SEC, "win": WIN_SEC,
                         "bands": N_BANDS})
            self._c = -1

    def _scan_max_c(self):
        mx = -1
        with open(self._out_path, encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(Exception):
                    obj = json.loads(line)
                    if obj.get("ty") in ("conn", "fp", "end"):
                        mx = max(mx, int(obj.get("c", -1)))
        return mx

    def _close_output(self):
        f, self._out = self._out, None
        if f is not None:
            with contextlib.suppress(Exception):
                f.flush()
                f.close()
