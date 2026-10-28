# -*- coding: utf-8 -*-
"""audiofp.py — 纯 numpy 音频指纹与对齐库

用途：把"直播侧"短片段（未知偏移）对齐到"官方回放"参考时间轴（同源音频互相关求偏移）。

fingerprint(samples, sr, hop_sec, win_sec) -> (N, 33) float32
    分帧 -> 汉明窗 FFT -> 33 个对数分布频带（约 250Hz-5kHz）能量
    -> log10 -> 每帧 L2 归一化

align(fp_ref, fp_query, hop_sec) -> dict{offset_sec, confidence, best_score}
    指纹矩阵沿时间轴滑动互相关（FFT 卷积实现，全向量化，无逐位置 Python 循环），
    主峰抛物线插值取亚帧精度，confidence = 主峰/次峰比（次峰排除主峰邻域）。

wav 读写：read_wav / write_wav（wave 模块 + numpy frombuffer）。

afp 文件读取（服务端用，与采集端契约一致）：
read_afp(path) -> {"conns": [{c, t0_ms, frames, n}], "params": {...}}
    path 为 .afp.jsonl 或其 gzip 压缩 .afp.jsonl.gz（按后缀/魔数透明识别）。
    JSONL 每行一个对象，四种行：
      {"ty":"hdr","v":1,"sr":16000,"hop":0.4,"win":0.4,"bands":33}
      (旧数据是 hop=0.2, 读取方必须用本行的值, 不能套当前默认值)
      {"ty":"conn","c":0,"t0_ms":<毫秒墙钟,该连接段首样本对应的墙钟>}
      {"ty":"fp","c":0,"ts":<毫秒墙钟,本批首帧>,"n":<帧数>,"fp":"<base64>"}
      {"ty":"end","c":0,"reason":"reconnect|stop|error"}
    fp 的 base64 解码为 uint8 数组，row-major n 行 x bands 列；
    第 i 帧墙钟 = ts + i*hop*1000 毫秒。
    注意: read_afp 返回的 conn 里 **不含 ts**, 时间轴一律用 t0_ms + 帧序号 x hop 重建
    (server._align_one_page 就是这么用的)。fp.ts 只是验收冗余, 读端不依赖它。
量化约定（与 fingerprint() 输出一致，指纹值域 [-1,1]）：
    量化   q = clamp(round((v+1)*127.5), 0, 255)
    反量化 v = q/127.5 - 1
    （对齐前 align() 逐带去均值，量化误差 ~0.004 对互相关无影响）
"""
import base64
import gzip
import json
import math
import wave

import numpy as np

__all__ = [
    "read_wav",
    "write_wav",
    "fingerprint",
    "align",
    "band_bin_edges",
    "quantize",
    "dequantize",
    "read_afp",
]

N_BANDS = 33
F_LO = 250.0   # 频带下界 Hz
F_HI = 5000.0  # 频带上界 Hz
_LOG_EPS = 1e-10  # log10 下限保护（防 log10(0)）


# ----------------------------------------------------------------------------
# wav 读写
# ----------------------------------------------------------------------------
def read_wav(path):
    """读取 PCM wav，返回 (samples float32 [-1,1] 单声道, sr int)。"""
    with wave.open(str(path), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError(f"不支持压缩 wav: {path}")
        nch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw == 1:  # 8-bit 无符号
        x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        x = (x - 128.0) / 128.0
    elif sw == 2:  # 16-bit
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 3:  # 24-bit（小端，手工拼 int32 符号扩展）
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (a[:, 0] << 8) | (a[:, 1] << 16) | (a[:, 2] << 24)
        x = v.astype(np.float32) / 2147483648.0
    elif sw == 4:  # 32-bit int
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的采样宽度 {sw} 字节: {path}")
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), int(sr)


def write_wav(path, x, sr):
    """把 float [-1,1] 数组写成 16-bit PCM 单声道 wav。"""
    y = np.clip(np.asarray(x, dtype=np.float32).ravel(), -1.0, 1.0)
    yi = np.round(y * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(yi.tobytes())


# ----------------------------------------------------------------------------
# 频带设计
# ----------------------------------------------------------------------------
def band_bin_edges(sr, nfft, n_bands=N_BANDS, f_lo=F_LO, f_hi=F_HI):
    """返回 (bin_edges int 数组长度 n_bands+1)。对数分布频带的 FFT bin 边界。

    保证严格递增（窄带处去重），并裁剪到 [0, nfft//2]。
    """
    edges = np.logspace(np.log10(f_lo), np.log10(f_hi), n_bands + 1)
    bins = np.round(edges * nfft / float(sr)).astype(np.int64)
    bins = np.clip(bins, 0, nfft // 2)
    for i in range(1, len(bins)):  # 严格递增
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1
    if bins[-1] > nfft // 2:
        raise ValueError(f"频带上限超出奈奎斯特: f_hi={f_hi}, sr={sr}")
    return bins


# ----------------------------------------------------------------------------
# 指纹
# ----------------------------------------------------------------------------
def fingerprint(samples, sr, hop_sec=0.4, win_sec=0.4):
    """计算频带能量指纹。

    返回 (N, 33) float32，N = 1 + (len-win)//hop。
    每帧：汉明窗 FFT -> 33 频带能量 -> log10 -> L2 归一化。
    """
    x = np.ascontiguousarray(np.asarray(samples, dtype=np.float32).ravel())
    hop = int(round(hop_sec * sr))
    win = int(round(win_sec * sr))
    if hop < 1 or win < 8:
        raise ValueError("hop/win 太小")
    if len(x) < win:
        raise ValueError(f"音频太短: {len(x)} 采样 < 窗长 {win}")

    n_frames = 1 + (len(x) - win) // hop
    nfft = 1 << (win - 1).bit_length()  # 下一个 2 的幂，补零加速 FFT
    bins = band_bin_edges(sr, nfft)
    starts = bins[:-1]

    # 索引矩阵（分块处理控制内存）
    w = np.hamming(win).astype(np.float32)
    band_e = np.empty((n_frames, len(starts)), dtype=np.float32)

    chunk = max(1, (1 << 22) // win)  # 每块约 4M 采样
    for i0 in range(0, n_frames, chunk):
        i1 = min(n_frames, i0 + chunk)
        # (B, win) 帧视图（stride 技巧，免拷贝），乘汉明窗
        frames = np.lib.stride_tricks.as_strided(
            x[i0 * hop :], shape=(i1 - i0, win),
            strides=(x.strides[0] * hop, x.strides[0]),
        ) * w
        spec = np.fft.rfft(frames, n=nfft, axis=1)
        power = spec.real * spec.real + spec.imag * spec.imag  # (B, nfft/2+1) float32
        # 频带能量求和（reduceat 分段求和；截到 bins[-1] 保证最后一段边界正确）
        band_e[i0:i1] = np.add.reduceat(power[:, : bins[-1]], starts, axis=1)
        del frames, spec, power

    feat = np.log10(band_e + _LOG_EPS)  # 对数能量
    # 每帧 L2 归一化
    nrm = np.sqrt((feat * feat).sum(axis=1, keepdims=True))
    nrm[nrm < 1e-12] = 1.0
    feat = feat / nrm
    return feat.astype(np.float32)


# ----------------------------------------------------------------------------
# 对齐
# ----------------------------------------------------------------------------
def align(fp_ref, fp_query, hop_sec, exclude_sec=1.0, valid_range=None):
    """把查询指纹滑动互相关到参考指纹上，求最优偏移。

    返回 dict:
      offset_sec   查询在参考中的起始时间（亚帧精度，抛物线插值）
      confidence   主峰/次峰比（次峰为排除主峰 ±exclude_sec 邻域后的最大值）
      best_score   主峰互相关得分

    valid_range: (min_sec, max_sec) 可选，把搜索范围限制在偏移的这段时间内。
      为什么需要：同一段音频在参考里可能出现多次（直播首播 + 之后的重播），
      而**全局最大峰未必是想要的那次** —— 实测重播因码率更高，峰比首播还高。
      若只在外面过滤结果，正确的峰根本不会出现在返回值里；必须在求 argmax 之前
      就裁掉范围外的位置。给 None 时行为与旧版完全一致。

    实现说明（无逐位置双重循环）：
      得分曲线 s(m) = sum_k sum_d q[k,d] * r[m+k,d]，对每个频带 d 是一条
      互相关曲线；用卷积定理逐带 FFT 求出后按带求和：
        conv = irfft( rfft(ref) * rfft(query[::-1]) )
      即 conv[i] 对应偏移 m = i - (n_q - 1)。
    """
    A = np.asarray(fp_ref, dtype=np.float64)
    B = np.asarray(fp_query, dtype=np.float64)
    if A.ndim != 2 or B.ndim != 2 or A.shape[1] != B.shape[1]:
        raise ValueError("指纹矩阵形状不匹配")
    n_a, n_b = A.shape[0], B.shape[0]
    if n_b > n_a:
        raise ValueError("查询指纹比参考还长，无法完全对齐")
    if n_b < 2:
        raise ValueError("查询指纹太短")

    # 逐带去均值（去掉静态频谱形状的直流分量，抑制"能量大就分高"的伪峰）
    A = A - A.mean(axis=0, keepdims=True)
    B = B - B.mean(axis=0, keepdims=True)

    n = n_a + n_b - 1
    nfft = 1 << (n - 1).bit_length()
    FA = np.fft.rfft(A, n=nfft, axis=0)          # (nfft, D)
    FB = np.fft.rfft(B[::-1], n=nfft, axis=0)    # 时间反转 -> 卷积即相关
    conv = np.fft.irfft(FA * FB, n=nfft, axis=0)[:n]  # (n, D)
    score = conv.sum(axis=1)  # score[i] 对应偏移 m = i - (n_b - 1)

    # 有效偏移范围：查询完全落在参考内，m ∈ [0, n_a - n_b]
    valid = score[n_b - 1 : n_a]  # valid[j] 对应偏移 m = j
    nv = valid.shape[0]

    # 限制搜索范围：只在 [k_lo, k_hi] 内取 argmax / 次峰。
    # 抛物插值要用 j-1/j+1，故内层留出 1 帧余量；越界处退化为不插值。
    k_lo, k_hi, trim = 0, nv - 1, False
    if valid_range is not None:
        try:
            r_lo, r_hi = float(valid_range[0]), float(valid_range[1])
        except (TypeError, ValueError, IndexError):
            r_lo, r_hi = -float("inf"), float("inf")
        if r_hi < r_lo:
            r_lo, r_hi = r_hi, r_lo
        k_lo = max(0, int(math.floor(r_lo / hop_sec)))
        k_hi = min(nv - 1, int(math.ceil(r_hi / hop_sec)))
        if k_hi < k_lo:
            # 范围完全落在有效区间外：退化为全局搜索，不返回垃圾
            k_lo, k_hi = 0, nv - 1
        else:
            trim = True

    view = valid[k_lo : k_hi + 1]
    j = k_lo + int(np.argmax(view))
    y1 = float(valid[j])

    # 次峰：排除主峰邻域（避免主峰肩部被当成次峰）
    ex = max(1, int(round(exclude_sec / hop_sec)))
    mask = np.ones(nv, dtype=bool)
    if trim:
        # 只在搜索范围内比较次峰，否则范围外的重播峰会把 confidence 压低，
        # 明明是唯一合理的窗口内位置却被判成"不显著"
        mask[:k_lo] = False
        mask[k_hi + 1 :] = False
    lo, hi = max(0, j - ex), min(nv, j + ex + 1)
    mask[lo:hi] = False
    second = float(valid[mask].max()) if mask.any() else 0.0

    # 抛物线插值（亚帧精度）
    if 0 < j < nv - 1:
        y0, y2 = float(valid[j - 1]), float(valid[j + 1])
        denom = y0 - 2.0 * y1 + y2
        if denom != 0.0:
            delta = 0.5 * (y0 - y2) / denom
            delta = max(-0.5, min(0.5, delta))
        else:
            delta = 0.0
    else:
        delta = 0.0

    offset_sec = (j + delta) * hop_sec
    confidence = (y1 / second) if second > 0.0 else float("inf")
    return {
        "offset_sec": float(offset_sec),
        "confidence": float(confidence),
        "best_score": y1,
    }


# ----------------------------------------------------------------------------
# 指纹量化 / 反量化（afp 文件契约）
# ----------------------------------------------------------------------------
def quantize(fp):
    """float 指纹 -> uint8 (N, 33)。q = clamp(round((v+1)*127.5), 0, 255)。"""
    v = np.asarray(fp, dtype=np.float32)
    return np.clip(np.rint((v + 1.0) * 127.5), 0, 255).astype(np.uint8)


def dequantize(q):
    """uint8 (N, 33) -> float32 指纹。v = q/127.5 - 1（逐带去均值在 align 内做）。"""
    q = np.asarray(q, dtype=np.uint8)
    return (q.astype(np.float32) / np.float32(127.5)) - np.float32(1.0)


# ----------------------------------------------------------------------------
# afp 文件读取（.afp.jsonl / .afp.jsonl.gz）
# ----------------------------------------------------------------------------
def _open_text(path):
    """按后缀/魔数决定是否 gzip 解压，返回文本文件对象。"""
    path = str(path)
    with open(path, "rb") as f:
        magic = f.read(2)
    if path.endswith(".gz") or magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def read_afp(path):
    """解析 afp 文件（见模块 docstring 的行格式契约）。

    返回:
      {
        "conns": [ {"c": 段号, "t0_ms": 该段首样本墙钟毫秒,
                    "frames": (n, bands) float32 反量化指纹, "n": 帧数}, ... ]
        "params": {"v","sr","hop","win","bands"}  # hdr 行, 缺省为空 dict
      }
    conn 行按出现顺序生成段；fp 行附加到该 c 最近一次出现的段（正常一个 c 一段）。
    """
    segs = []                      # [{c, t0_ms, parts:[ndarray...]}]
    last_by_c = {}                 # c -> segs 下标（fp 行挂到这里）
    params = {}
    with _open_text(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                # 采集进行中/被强杀时末行常是半行(写到一半), 与弹幕读取的容错策略保持一致:
                # 半行只丢这一行, 不能让整晚的指纹作废。非末行的坏行仍视为文件损坏。
                if not line.endswith(("}", "]")):
                    continue
                raise ValueError(f"{path}:{lineno} 不是合法 JSON: {e}")
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{lineno} 行不是 JSON 对象")
            ty = obj.get("ty")
            if ty == "hdr":
                params = {k: obj.get(k) for k in ("v", "sr", "hop", "win", "bands")}
            elif ty == "conn":
                try:
                    c, t0 = int(obj["c"]), int(obj["t0_ms"])
                except (KeyError, TypeError, ValueError) as e:
                    raise ValueError(f"{path}:{lineno} conn 行字段非法: {e}")
                last_by_c[c] = len(segs)
                segs.append({"c": c, "t0_ms": t0, "parts": []})
            elif ty == "fp":
                try:
                    c, n = int(obj["c"]), int(obj["n"])
                    raw = base64.b64decode(str(obj["fp"]), validate=False)
                except (KeyError, TypeError, ValueError) as e:
                    raise ValueError(f"{path}:{lineno} fp 行字段非法: {e}")
                if c not in last_by_c:
                    raise ValueError(f"{path}:{lineno} fp 行(c={c})出现在 conn 行之前")
                bands = int(params.get("bands") or N_BANDS)
                if len(raw) != n * bands:
                    raise ValueError(
                        f"{path}:{lineno} fp 行长度不符: base64 解码 {len(raw)} 字节"
                        f" != n({n}) x bands({bands})")
                segs[last_by_c[c]]["parts"].append(
                    dequantize(np.frombuffer(raw, dtype=np.uint8).reshape(n, bands)))
            elif ty == "end":
                continue           # reason 只影响采集端语义, 读端无需处理
            else:
                raise ValueError(f"{path}:{lineno} 未知行类型 ty={ty!r}")

    conns = []
    for s in segs:
        if s["parts"]:
            frames = np.concatenate(s["parts"], axis=0)
        else:
            frames = np.empty((0, int(params.get("bands") or N_BANDS)), dtype=np.float32)
        conns.append({"c": s["c"], "t0_ms": s["t0_ms"],
                      "frames": frames, "n": int(frames.shape[0])})
    return {"conns": conns, "params": params}
