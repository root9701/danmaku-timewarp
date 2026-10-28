# -*- coding: utf-8 -*-
"""test_align.py — 对齐算法的回归用例。

用法: python tests/test_align.py     (退出码 0 = 全过)

覆盖的是"改了算法但我们不知道"的高风险点:
  - 互相关能否恢复已知偏移
  - 指纹算法在两端(采集端 live_audiofp / 播放端 audiofp)是否仍然逐 bit 等价
    —— 两份是复制关系(采集端要能单独部署到手机), 分歧不会报错, 只会永远对不上
  - 时间窗约束是否真的拒绝窗外候选
  - 音频时长下限是否真的拦得住

这些用例用合成数据, 不依赖真实采集产物, 因此任何机器可跑。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))
sys.path.insert(0, str(ROOT / "danmaku-collector"))

import numpy as np                                          # noqa: E402

import audiofp                                              # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def make_audio(seconds: float, sr: int = 16000, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(sr * seconds)) * 0.1).astype(np.float32)


def test_align_recovers_offset() -> None:
    """从参考指纹里截一段当查询, align 必须还原出原始偏移。"""
    rng = np.random.default_rng(7)
    ref = rng.standard_normal((1500, 33)).astype(np.float32)
    start = 400
    q = ref[start:start + 600].copy()
    r = audiofp.align(ref, q, 0.2)
    want = start * 0.2
    check("align 还原偏移", abs(r["offset_sec"] - want) < 0.05,
          f"得到 {r['offset_sec']}, 期望 {want}")
    check("align 高置信", r["confidence"] > 5.0,
          f"confidence={r['confidence']}")


def test_fingerprint_both_sides_identical() -> None:
    """采集端与播放端的指纹必须是同一份数学定义, 否则永远对不上。"""
    try:
        import live_audiofp
    except ImportError as e:                       # numpy 缺失等
        check("live_audiofp 可导入", False, str(e))
        return
    x = make_audio(5.0, seed=11)
    a = audiofp.fingerprint(x, 16000, 0.2, 0.4)
    b = live_audiofp.fingerprint(x, 16000, 0.2, 0.4)
    check("两端指纹 shape 一致", a.shape == b.shape, f"{a.shape} vs {b.shape}")
    check("两端指纹逐 bit 等价", np.array_equal(a, b),
          f"max|diff|={float(np.abs(a - b).max()) if a.shape == b.shape else 'N/A'}")
    check("两端量化等价", np.array_equal(audiofp.quantize(a),
                                        live_audiofp.quantize(b)))
    check("两端频带边界等价",
          np.array_equal(audiofp.band_bin_edges(16000, 8192),
                         live_audiofp.band_bin_edges(16000, 8192)))


def test_quantize_roundtrip() -> None:
    """量化误差必须小到不影响互相关。"""
    rng = np.random.default_rng(3)
    fp = rng.standard_normal((50, 33)).astype(np.float32)
    fp = fp / np.linalg.norm(fp, axis=1, keepdims=True)
    back = audiofp.dequantize(audiofp.quantize(fp))
    check("量化往返误差 < 0.01", float(np.abs(fp - back).max()) < 0.01,
          f"max|err|={float(np.abs(fp - back).max())}")


def test_valid_range_excludes_outside() -> None:
    """时间窗外的高峰必须不被采纳 —— 防"早上重播"被当成正赛。"""
    rng = np.random.default_rng(5)
    ref = rng.standard_normal((2000, 33)).astype(np.float32)
    # 两处完全相同的片段: 一处在 100 帧, 一处在 1500 帧
    q = ref[100:500].copy()
    ref[1500:1900] = q
    hop = 0.2
    # 只允许 1400~1900 帧区间(即 280~380 秒)
    r = audiofp.align(ref, q, hop, valid_range=(1400 * hop, 1900 * hop))
    got = r["offset_sec"]
    check("窗口约束只接受窗内位置", 280.0 <= got <= 380.0,
          f"得到 {got} 秒, 应在 280~380")


def test_min_match_seconds_guard() -> None:
    """音频时长下限: 证据不足时必须真的拒绝, 而不只是把数字带出去。"""
    import server
    rng = np.random.default_rng(13)
    # 回放指纹 30 秒; 直播侧给一段完全无关的噪声 -> 不该被采纳
    fp_replay = rng.standard_normal((150, 33)).astype(np.float32)
    conns = [{"c": 0, "t0_ms": 1_792_942_382_304,
              "frames": rng.standard_normal((1800, 33)).astype(np.float32),
              "n": 1800}]
    try:
        server._align_one_page(fp_replay, conns, 0.2,
                               server.AUTO_MIN_CONFIDENCE,
                               server.AUTO_MIN_MATCH_SEC)
        rejected = False
    except server.ApiError:
        rejected = True
    check("无关音频被拒绝", rejected, "居然采纳了随机噪声")

    # 同一段音频(有真实匹配)应当通过
    ok_conns = [{"c": 0, "t0_ms": 1_792_942_382_304,
                 "frames": fp_replay.copy(), "n": 150}]
    try:
        r = server._align_one_page(fp_replay, ok_conns, 0.2,
                                   server.AUTO_MIN_CONFIDENCE,
                                   server.AUTO_MIN_MATCH_SEC)
        passed = r.get("matched_seconds", 0) >= server.AUTO_MIN_MATCH_SEC
    except server.ApiError as e:
        passed = False
        print("    (自匹配也失败:", e, ")")
    check("自匹配被采纳且时长达标", passed)


def test_config_overrides_take_effect() -> None:
    """config.json 里写了的阈值必须真的被读到(而不是只写在文档里)。"""
    import server
    import json
    cfg_path = ROOT / "danmaku-server" / "config.json"
    if not cfg_path.is_file():
        check("config.json 存在", True, "跳过(本机无 config.json)")
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    al = cfg.get("align") or {}
    if "vote_segments" in al:
        check("vote_segments 来自配置",
              server.AUTO_VOTE_SEGMENTS == int(al["vote_segments"]),
              f"代码={server.AUTO_VOTE_SEGMENTS} 配置={al['vote_segments']}")
    if "vote_tolerance_sec" in al:
        check("vote_tolerance_sec 来自配置",
              server.AUTO_VOTE_TOLERANCE_SEC == float(al["vote_tolerance_sec"]),
              f"代码={server.AUTO_VOTE_TOLERANCE_SEC} 配置={al['vote_tolerance_sec']}")


def test_random_level_consensus_rejected() -> None:
    """全票"一致"但置信度只有随机水平时, 必须拒绝 —— 不能只看票数。

    real 场景: 某分P 的音频在直播里根本不存在(赛前评论), 8 段全都退化到
    同一个默认位置, 票数 8/8 看着完美, conf 却只有 1.07~1.21(随机水平)。
    只数票的实现会把这种"幻觉一致"当成高可信结果采纳。
    """
    import server
    rng = np.random.default_rng(23)

    def mk(n):
        m = rng.standard_normal((n, 33)).astype(np.float32)
        return m / np.linalg.norm(m, axis=1, keepdims=True)

    fp_replay = mk(900)          # 3 分钟回放
    # 直播侧是一条与本分P 无关的长噪声
    conns = [{"c": 0, "t0_ms": 1_792_942_382_304,
              "frames": mk(4000), "n": 4000}]
    try:
        r = server._align_one_page(fp_replay, conns, 0.2,
                                   server.AUTO_MIN_CONFIDENCE,
                                   server.AUTO_MIN_MATCH_SEC)
        rejected = False
        why = f"采纳了 conf={r['confidence']:.2f}"
    except server.ApiError as e:
        rejected = True
        why = str(e)[:60]
    check("随机水平的一致票被拒绝", rejected, why)


def test_segmented_mapping_no_drift() -> None:
    """分段映射: 用单标量会把第二局算错 200 秒以上, 分段映射必须逐段正确。

    这里只验算公式本身, 不依赖前端:
      虚拟位置 v 的墙钟 = anchor + offset_k*1000 + (v - S_k)*1000
    """
    anchor = 1_792_942_382_304
    # 两个分P: 第一局 offset=5272.5 时长 3031s; 第二局 offset=16072.5(局间休息被剪)
    segs = [{"offset_sec": 5272.5, "duration": 3031},
            {"offset_sec": 16072.5, "duration": 3000}]
    starts = [0, segs[0]["duration"]]

    def wall(v_sec):
        for i in range(len(segs) - 1, -1, -1):
            if v_sec >= starts[i]:
                k = i
                break
        return anchor + segs[k]["offset_sec"] * 1000 + (v_sec - starts[k]) * 1000

    # 第二局开头: 虚拟位置 = 3031s, 应是 wall = anchor + 16072.5s
    got = (wall(starts[1]) - anchor) / 1000.0
    check("第二局起点墙钟正确", abs(got - 16072.5) < 0.01, f"得到 {got}")

    # 若用单标量(第一局 offset) 会算出什么
    single = (anchor + segs[0]["offset_sec"] * 1000 + starts[1] * 1000
              - anchor) / 1000.0
    drift = single - 16072.5
    check("单标量确实会漂移(>100s)", abs(drift) > 100, f"漂移 {drift:.1f}s")


def test_hop_change_contract() -> None:
    """指纹 hop 的约定 —— 改动它会同时影响体积与对齐成功率, 必须显式约束。

    这几条不是"测试实现细节", 而是踩过的坑:
      - hop != win 就会有音频从未被任何帧覆盖 -> 定位能力下降(实测 hop>win 后
        0.8 档开始失败)。所以要求 hop <= win。
      - win 不能随 hop 放大: 窗中心由 hop 决定, 放大 win 会让两端算出的指纹
        不再逐 bit 等价(实测 max 差 0.03)。
      - 历史 afp(hop=0.2) 必须仍能被读出来并正确折算时长 —— 它不可再生。
    """
    import server
    import live_audiofp

    check("hop 不大于 win(否则有音频被跳过)",
          live_audiofp.HOP_SEC <= live_audiofp.WIN_SEC + 1e-9,
          f"hop={live_audiofp.HOP_SEC} win={live_audiofp.WIN_SEC}")
    check("win 固定 0.4(不随 hop 放大)", abs(live_audiofp.WIN_SEC - 0.4) < 1e-9,
          f"win={live_audiofp.WIN_SEC}")

    # 两端在**当前**参数下必须逐 bit 等价
    x = make_audio(4.0, seed=21)
    a = audiofp.fingerprint(x, 16000, live_audiofp.HOP_SEC, live_audiofp.WIN_SEC)
    b = live_audiofp.fingerprint(x, 16000, live_audiofp.HOP_SEC, live_audiofp.WIN_SEC)
    check("两端逐 bit 等价(当前 hop)", np.array_equal(a, b),
          f"max|diff|={float(np.abs(a - b).max()) if a.shape == b.shape else 'N/A'}")

    # 历史指纹仍可读, 且 hop 取自文件而不是代码默认值
    import tempfile
    import json
    import base64
    with tempfile.TemporaryDirectory(prefix="hop_") as td:
        p = Path(td) / "old.afp.jsonl"
        n = 120
        q = audiofp.quantize(np.tile(
            (np.arange(33, dtype=np.float32) / 33.0 - 0.5), (n, 1)))
        lines = [
            {"ty": "hdr", "v": 1, "sr": 16000, "hop": 0.2, "win": 0.4, "bands": 33},
            {"ty": "conn", "c": 0, "t0_ms": 1792942382304},
            {"ty": "fp", "c": 0, "ts": 1792942382304, "n": n,
             "fp": base64.b64encode(q.tobytes()).decode("ascii")},
        ]
        p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
        d = audiofp.read_afp(p)
        got_hop = float(d["params"]["hop"])
        check("旧文件 hop 读回 0.2(不是代码默认值)", got_hop == 0.2, f"得到 {got_hop}")
        check("帧数按文件 hop 折算时长", abs(d["conns"][0]["n"] * got_hop - 24.0) < 1e-6,
              f"{d['conns'][0]['n'] * got_hop}s")

        # 参数不一致的两份不得合并(混了会静默拉伸时间轴)
        p2 = Path(td) / "new.afp.jsonl"
        lines[0]["hop"] = 0.4
        p2.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
        mixed = server.read_afp_merged([p2, p])
        check("异参 afp 不合并(只留主文件)", len(mixed["conns"]) == 1,
              f"实际 {len(mixed['conns'])} 段")
        same = server.read_afp_merged([p2, p2])
        check("同参 afp 正常合并", len(same["conns"]) == 2,
              f"实际 {len(same['conns'])} 段")


def test_afp_written_ts_matches_hop() -> None:
    """写盘侧 fp 行的 ts 必须按 hdr 的 hop 推进 —— 这曾经是 hop=0.2 时代的残留。

    踩过的坑: live_audiofp._flush_rows 里写死 `self._rows_i0 * 200`, 而 HOP_SEC 早已
    改成 0.4。于是每批 300 帧(实际 120 秒音频)写出的 ts 只前进 60 秒, 行与行之间
    时间轴倒跳 60 秒。现有读端(read_afp)只用 conn.t0_ms + 帧序号, 所以对齐结果
    没坏 —— 但 fp.ts 是对外契约字段, 按 SCHEMA 实现的第三方读者会算出错时间轴,
    而且不报错。

    这条用例只用合成 PCM 跑一遍真实写盘路径, 断言 ts 差 == n * hop。
    """
    import io
    import json

    import live_audiofp

    af = live_audiofp.AudioFingerprinter()
    buf = io.StringIO()
    af._out = buf
    af._begin_segment()
    af._t0 = 1_800_000_000_000

    # 喂 3 批多一点 PCM, 按 ffmpeg 的真实块大小(几 KB)分批
    hop_samp = int(round(live_audiofp.HOP_SEC * live_audiofp.SR))
    rng = np.random.default_rng(101)
    total = hop_samp * (1 + live_audiofp.BATCH_FRAMES * 3 + 20)
    pcm = (rng.standard_normal(total) * 1000).astype("<i2").tobytes()
    for off in range(0, len(pcm), 8192):
        af._feed(pcm[off:off + 8192])
    af._finish_segment("test")

    rows = []
    for line in buf.getvalue().splitlines():
        with __import__("contextlib").suppress(ValueError):
            o = json.loads(line)
            if o.get("ty") == "fp":
                rows.append(o)

    check("写盘产生多行 fp(才能验行间 ts)", len(rows) >= 3, f"只有 {len(rows)} 行")
    if len(rows) < 2:
        return

    hop = live_audiofp.HOP_SEC
    ok = True
    detail = ""
    for i in range(1, len(rows)):
        want = rows[i - 1]["n"] * hop * 1000
        got = rows[i]["ts"] - rows[i - 1]["ts"]
        if abs(got - want) > 1:
            ok = False
            detail = f"行{i - 1}->{i}: ts 前进 {got}ms, 应为 {want:.0f}ms"
            break
    check("fp 行间 ts 差 == 帧数 x hop(不写死 200ms)", ok, detail)

    # 累计校验: 第 0 行 ts 就是 t0, 每行 ts 应等于 t0 + 累计帧数*hop
    acc = 0
    ok2 = True
    for r in rows:
        want = af._t0 + int(round(acc * hop * 1000))
        if abs(r["ts"] - want) > 1:
            ok2 = False
            break
        acc += r["n"]
    check("fp 行 ts 与 t0+帧序号xhop 一致", ok2)


def test_ffmpeg_timeout_guard() -> None:
    """ffmpeg 抽音频必须带超时 —— 否则上游停流会永久挂死, 而校准是全局互斥的。

    踩过的坑: `subprocess.run(cmd, capture_output=True)` 没 timeout。上游(尤其经
    /api/stream 反代的 B站 CDN)只 accept 不发送时, ffmpeg 永远等下去, 一个 worker
    挂死就挡住之后所有自动校准, 用户看到"一直转圈, 重启才好"。

    这里造一个"只 accept、不吐数据"的本地端口, 断言 ffmpeg_audio_fp 在预算内报错退出。
    """
    import socket
    import threading
    import time as _t

    import server

    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def hold():
            try:
                conn, _ = srv.accept()
                stop.wait(60)          # 挂着不发送任何字节
                conn.close()
            except OSError:
                pass
        threading.Thread(target=hold, daemon=True).start()

        t0 = _t.monotonic()
        try:
            server.ffmpeg_audio_fp(f"http://127.0.0.1:{port}/x", 0.4,
                                   timeout_sec=3)
            timed_out = False
            why = "居然正常返回了"
        except server.ApiError as e:
            timed_out = "超时" in str(e)
            why = str(e)[:70]
        elapsed = _t.monotonic() - t0
    finally:
        stop.set()
        srv.close()

    check("ffmpeg 挂起时按预算报错(ApiError 提到超时)", timed_out, why)
    check("超时在预算附近生效(<=15s, 不会挂死)", elapsed <= 15, f"耗时 {elapsed:.1f}s")

    # 正常本地文件仍然能用(不能因为加了超时就误伤)
    import tempfile
    import wave
    with tempfile.TemporaryDirectory(prefix="ffprobe_") as td:
        wav = Path(td) / "a.wav"
        sr = 16000
        x = (np.random.default_rng(5).standard_normal(sr * 2) * 2000).astype("<i2")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())
        fp = server.ffmpeg_audio_fp(str(wav), 0.4)
        check("本地短音频照常算出指纹", fp.shape[0] > 0 and fp.shape[1] == 33,
              str(fp.shape))


def test_anchored_cluster_rejects_chain_votes() -> None:
    """防链式聚类凑票: 100/130/160/190 这种每票只差 30 秒的散票不得判为一致。

    踩过的坑: 旧实现把每票与簇内**最新一票**比, 于是 100->130->160->190 一路串成
    同一簇, 簇内真实离散 90 秒仍算"一致"。实测随机噪声的 4 张烂票(conf≈1.2)就是
    这样凑够票数被判通过的。

    现在改成**锚定式**: 每票只与簇的**第一个**位置比。这里直接注入受控的票,
    验证 4 张递进散票不会聚成一个簇(而不是靠真实音频碰运气)。
    """
    import audiofp
    import server

    hop = 0.4
    t0_ms = 1_792_942_382_304
    # 回放要够长才会切成多段: seg_frames 上限 180 秒, 8 段
    ref_frames = int(round(180.0 * 8 / hop))          # 1440 帧 ≈ 576 秒
    fp_replay = np.random.default_rng(31).standard_normal(
        (ref_frames, 33)).astype(np.float32)
    conns = [{"c": 0, "t0_ms": t0_ms,
              "frames": np.random.default_rng(32).standard_normal(
                  (int(3600 / hop), 33)).astype(np.float32),
              "n": int(3600 / hop)}]

    # 注入: 每段都被"定位"到递进 30 秒的位置, conf 都高于随机水平
    # base 秒 = t0 + 100, 130, 160, 190, ... (每票只与前一票差 30 秒)
    tol = server.AUTO_VOTE_TOLERANCE_SEC
    chain = [100.0 + 30.0 * i for i in range(server.AUTO_VOTE_SEGMENTS)]

    def seg_start_of(piece):
        """piece 是 fp_replay 的视图, 反推出它的起始帧号(=_align_one_page 里的 st)。

        必须这么算: base = seg_t0 + offset - st*hop, 各段的 st 不同。想模拟"某段
        定位到 base=X", 就要给 offset = X - seg_t0 + st*hop, 不能直接返回 X。
        """
        p0 = piece.__array_interface__["data"][0]
        b0 = fp_replay.__array_interface__["data"][0]
        return (p0 - b0) // fp_replay.strides[0]

    def make_fake(base_for_call):
        def fake_align(seg_frames_arr, piece, hop_sec, exclude_sec=1.0, valid_range=None):
            st = seg_start_of(piece)
            base = base_for_call(st)
            off = base - t0_ms / 1000.0 + st * hop_sec
            return {"offset_sec": off, "confidence": 4.0, "best_score": 1.0}
        return fake_align

    old = audiofp.align
    # 按"第几段"（而不是按帧号取模）给位置：8 段各给 100/130/160/.../310 秒。
    # conf 用**随机水平**(1.2, 与真实噪声残票一致): 报告里的原始事故正是"4 张 conf≈1.2
    # 的烂票靠递进位置凑够票数"。用 4.0 会走"强票"救援分支, 那是另一条路径, 不该混进来。
    order = {"n": 0}

    def chain_align(seg_frames_arr, piece, hop_sec, exclude_sec=1.0, valid_range=None):
        st = seg_start_of(piece)
        i = order["n"]
        order["n"] += 1
        base = chain[i % len(chain)]
        off = base - t0_ms / 1000.0 + st * hop_sec
        return {"offset_sec": off, "confidence": 1.2, "best_score": 1.0}

    audiofp.align = chain_align
    try:
        try:
            r = server._align_one_page(fp_replay, conns, hop,
                                       server.AUTO_MIN_CONFIDENCE,
                                       server.AUTO_MIN_MATCH_SEC)
            accepted = True
            detail = f"采纳了 offset={r.get('offset_ms')} votes={r.get('votes')}"
        except server.ApiError as e:
            accepted = False
            detail = str(e)[:70]
    finally:
        audiofp.align = old

    check("每票差 30 秒的链式散票不被判为一致", not accepted, detail)
    # 这条不是实现细节, 是**两道防线缺一不可**的证据:
    # 容差恰好是 30 秒, 所以"每票差 30 秒"正好卡在锚定聚类的边界上, 会进同一个簇
    # (第一票与第四票已差 90 秒)。真正拦住它的是后面的"簇内离散 > 容差"硬上限。
    # 换句话说: 只靠容差比对(无论锚定还是链式)都挡不住这种票, 必须有离散上限兜底。
    check("容差 <= 30 秒(单靠容差挡不住 30 秒步进, 必须有离散上限)",
          tol <= 30.0, f"容差 {tol}s")

    # 反向对照: 真正紧贴的票(全部同一位置)必须被采纳 —— 别为了防凑票把正确的也拒了。
    audiofp.align = make_fake(lambda st: 500.0)
    try:
        r = server._align_one_page(fp_replay, conns, hop,
                                   server.AUTO_MIN_CONFIDENCE,
                                   server.AUTO_MIN_MATCH_SEC)
        # offset_ms 就是 base 的毫秒值(chosen[0]*1000), 不是墙钟。
        ok = abs(r["offset_ms"] - 500_000) < 1000
        why = f"offset={r['offset_ms']} 期望≈500000"
    except server.ApiError as e:
        ok, why = False, str(e)[:70]
    finally:
        audiofp.align = old
    check("全部票指向同一处时正常采纳(防过拒)", ok, why)


def main() -> int:
    print("对齐算法回归")
    for fn in (test_align_recovers_offset,
               test_fingerprint_both_sides_identical,
               test_quantize_roundtrip,
               test_valid_range_excludes_outside,
               test_min_match_seconds_guard,
               test_random_level_consensus_rejected,
               test_segmented_mapping_no_drift,
               test_config_overrides_take_effect,
               test_hop_change_contract,
               test_afp_written_ts_matches_hop,
               test_ffmpeg_timeout_guard,
               test_anchored_cluster_rejects_chain_votes):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
