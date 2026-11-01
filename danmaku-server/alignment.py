"""逐分P 对齐核心: 把回放指纹定位到直播墙钟上。

从 server.py 抽出: 只依赖 audiofp 与 align_vote, 不读配置、不碰 HTTP 与数据目录,
因此播放端和无头 CLI 用的是同一份实现。

关键算法约束(改动前必读):
- 用**多段投票**而不是一个长窗互相关: 长窗混进直播没播的内容会把整窗得分拉平。
- 多个自洽位置取**最早**: 那是当时正在直播的那次, 其余是回顾/早间重播。
- 时间窗必须传给 align 内部限制搜索: 重播峰可能更高, 事后再筛会漏掉正确峰。
"""
from datetime import datetime

from errors import ApiError


def _fmt_wall(ms) -> str:
    """毫秒墙钟 -> 'MM-DD HH:MM' 便于日志与报错阅读。"""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return str(ms)


def _window_bounds(window_ms):
    """(最早, 最晚) 墙钟毫秒 -> 秒; 未给窗口返回 (None, None)。"""
    if not window_ms:
        return None, None
    return window_ms[0] / 1000.0, window_ms[1] / 1000.0


def _apply_window(solid, window_ms, anchor_ms):
    """排除窗口外候选(早上重播/回顾)。全部在窗外时报错拒绝, 宁可不对也不错位。"""
    if not window_ms:
        return solid
    w_lo, w_hi = window_ms
    in_win = [c for c in solid if w_lo <= c[0] * 1000 <= w_hi]
    if in_win:
        return in_win
    all_pos = [int(round(c[0] * 1000)) for c in solid]
    near = min(all_pos, key=lambda m: min(abs(m - w_lo), abs(m - w_hi)))
    # 优先按赛程窗口给相对差, 没有 anchor 时给绝对时刻
    ref = anchor_ms if anchor_ms else w_lo
    raise ApiError(
        "该分P 只在窗口外找到位置(%s, 距赛程窗口 %+.0f 分钟), 判为早上重播/回顾, 未采纳"
        % (_fmt_wall(near), (near - ref) / 60000.0))


def _spread(votes, chosen, tolerance):
    """选中的位置内部各票离散(秒)。"""
    same = [v for v in votes if abs(v[0] - chosen[0]) <= tolerance]
    return max(v[0] for v in same) - min(v[0] for v in same)


def _build_result(chosen, n_seg, seg_frames, hop, solid, why, spread, detail):
    """组装返回结构。suspect 只会在 _apply_window 里抛错, 走到这里必为 False。"""
    offset_ms = int(round(chosen[0] * 1000))
    return {
        "offset_ms": offset_ms,
        # 置信度给**最强票**而不是中位数: 低分票有定位价值, 但会把中位数拉到
        # 随机水平(~1.1), 让"已对齐"看起来像没对齐。
        "confidence": min(float(max(chosen[1])), 999.0),
        "matched_seconds": len(chosen[1]) * seg_frames * hop,
        "conn": 0,
        "t0_ms": offset_ms,
        "votes": len(chosen[1]),
        "total_segments": n_seg,
        "candidates": len(solid),
        "all_candidates": [int(round(c[0] * 1000)) for c in solid],
        "spread_sec": round(spread, 2),
        "picked_by": ("多段投票 %d/%d 段一致(%s)"
                      % (len(chosen[1]), n_seg, why.get(id(chosen), "一致"))
                      + ("(取最早, 共 %d 处候选)" % len(solid) if len(solid) > 1 else "")),
        "suspect": False,
        "suspect_reason": "",
        "debug": detail,
    }


def align_one_page(fp_replay, conns, hop, min_conf, min_match_sec,
                   anchor_ms=None, window_ms=None, *, min_conn_sec=10.0,
                   vote_segments=8, tolerance_sec=30.0):
    """把一个分P 的回放指纹对齐到直播 afp 上, 返回它的起始位置与证据。

    参数由调用方传入(含全部阈值), 不读全局配置: 播放端传 config 里的值,
    无头 CLI 传同一组值, 两边结果才可能一致。
    """
    # 惰性导入而不是放在模块顶层: align_vote -> audiofp -> numpy 这条链只在
    # 真正做对齐时才需要, 而轻量版包里**不带 numpy**(省 51MB, 校准结果由作者
    # 下发)。放顶层会让 `import server` 直接抛 ModuleNotFoundError, 用户看到的
    # 是黑窗口一闪 —— 缺库只该关掉校准这一项能力, 不该让整个播放端起不来。
    import align_vote

    ref_frames = int(fp_replay.shape[0])
    min_conn_frames = max(1, int(round(min_conn_sec / hop)))
    if ref_frames < min_conn_frames:
        raise ApiError("该分P 指纹过短, 无法对齐")

    starts, seg_frames = align_vote.segment_layout(
        ref_frames, hop, min_conn_frames, vote_segments)
    n_seg = len(starts)
    need = max(2, (n_seg + 2) // 3)      # ceil(n/3), 至少 2 票
    win_lo_s, win_hi_s = _window_bounds(window_ms)

    votes, detail = align_vote.collect_votes(
        fp_replay, starts, seg_frames, hop, conns, win_lo_s, win_hi_s)
    if not votes:
        raise ApiError("该分P 的音频在直播里找不到可判定的位置"
                       "(各片段都只有单峰, 可能这场直播没有音频或指纹损坏)")

    clusters = align_vote.cluster_votes(votes, tolerance_sec)
    solid, why = align_vote.solid_clusters(
        clusters, n_seg, need, min_conf, hop, tolerance_sec)
    solid.sort(key=lambda c: c[0])       # 按时间升序 -> 取最早
    solid = _apply_window(solid, window_ms, anchor_ms)
    chosen = solid[0]

    matched_sec = len(chosen[1]) * seg_frames * hop
    if matched_sec < min_match_sec:
        raise ApiError(
            "该分P 找到的位置只有 %.0f 秒音频吻合(低于下限 %.0f 秒), 证据不足, 未采纳"
            % (matched_sec, min_match_sec))

    spread = _spread(votes, chosen, tolerance_sec)
    return _build_result(chosen, n_seg, seg_frames, hop, solid, why, spread, detail)
