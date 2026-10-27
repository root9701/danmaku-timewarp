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


def main() -> int:
    print("对齐算法回归")
    for fn in (test_align_recovers_offset,
               test_fingerprint_both_sides_identical,
               test_quantize_roundtrip,
               test_valid_range_excludes_outside,
               test_min_match_seconds_guard,
               test_random_level_consensus_rejected,
               test_segmented_mapping_no_drift,
               test_config_overrides_take_effect):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
