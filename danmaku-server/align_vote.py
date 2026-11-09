"""多段投票: 切段、逐段定位、聚类与采纳判定。

只做纯计算, 不做 IO、不读配置: 阈值全部由调用方传入, 因此播放端与无头 CLI
可以共用同一份实现。异常类型来自 errors.py, 与 server 是同一个类。
"""
import math

import audiofp
from errors import ApiError


def segment_layout(ref_frames: int, hop: float, min_conn_frames: int,
                   vote_segments: int):
    """把回放指纹均匀切成若干段, 返回 (各段起始帧, 每段帧数)。

    目标片长 3 分钟, 但不短于 min_conn_frames, 也不超过回放的 1/3。
    回放本身不到 3 分钟时整段当一个样本。
    """
    seg_secs = min(180.0, ref_frames * hop / 3.0)
    seg_frames = max(int(seg_secs / hop), min_conn_frames)
    if seg_frames >= ref_frames:
        return [0], ref_frames
    spans = ref_frames - seg_frames
    starts = [int(round(spans * i / (vote_segments - 1))) for i in range(vote_segments)]
    seen = set()
    starts = [s for s in starts if not (s in seen or seen.add(s))]
    return starts, seg_frames


def _best_for_piece(piece, conns, seg_frames, hop, st, win_lo_s, win_hi_s):
    """一个回放片段在全部连接段里找最强位置。

    窗口给了就优先在窗口内找, 窗口内没有才退回全局最强(留给后面报"疑似重播")。
    必须在 align 内部限制搜索范围: 它只返回全局最大峰, 而重播峰可能更高,
    若先取全局最优再按窗口过滤, 窗口内的正确峰根本不会出现。
    """
    best = best_in = None
    for seg in conns:
        if seg["n"] < seg_frames:
            continue
        # base = seg_t0 + off - st*hop  =>  off = base - seg_t0 + st*hop
        rng = None
        if win_lo_s is not None:
            seg_t0_s = seg["t0_ms"] / 1000.0
            rng = (win_lo_s - seg_t0_s + st * hop,
                   win_hi_s - seg_t0_s + st * hop)
        try:
            # 方向固定: 直播整段是 ref, 回放片段是 query; align 返回 query 在 ref 里的起点。
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
    return best, best_in


def collect_votes(fp_replay, starts, seg_frames, hop, conns, win_lo_s, win_hi_s):
    """逐段定位, 返回 (投票 [(base秒, 段号, 置信度)], 排查明细)。"""
    votes, detail = [], []
    for i, st in enumerate(starts):
        piece = fp_replay[st:st + seg_frames]
        best, best_in = _best_for_piece(piece, conns, seg_frames, hop, st,
                                        win_lo_s, win_hi_s)
        pick = best_in if best_in is not None else best
        if pick is None:
            detail.append(f"段{i}无可判定位置")
            continue
        base, conf = pick
        votes.append((base, i, conf))
        detail.append(f"段{i}(回放{st*hop/60:.0f}分)->{base/3600:.2f}h conf={conf:.2f}"
                      + ("(窗口内)" if best_in is not None else "(窗口外)"))
    return votes, detail


def cluster_votes(votes, tolerance):
    """锚定式聚类: 每票只与簇首比, 不做链式传递。

    链式(与簇内最新一票比)会让 100/130/160/190 这种每票只差 30 秒的散票一路
    串成同一簇, 簇内离散累积到 90 秒仍算"一致" —— 随机噪声就是这样凑够票数的。
    """
    votes.sort()
    clusters = []           # [[锚点秒, [置信度...], [段号...], [各票位置...]]]
    for base, i, conf in votes:
        if clusters and base - clusters[-1][0] <= tolerance:
            c = clusters[-1]
            c[1].append(conf)
            c[2].append(i)
            c[3].append(base)
        else:
            clusters.append([base, [conf], [i], [base]])
    return clusters


def cluster_score(c):
    """票数与票质的综合分: 各票置信度之和(低分票几乎不加分)。"""
    return sum(c[1])


def _reasons(n_vote, n_strong, n_seg, need, max_conf, min_conf, very_strong_conf):
    """采纳理由。任一条成立即采信, 理由写进 picked_by 供排查。

    「多段一致」这一条**不能只看票数**: need = ceil(n_seg/3) 在 8 段时只有 3 票,
    而 TOP/TOP5 这类"精彩集锦"分P 的音频是从别的时段剪出来的, 在直播里本来
    就没有连续对应位置 —— 它靠零星巧合也能凑到 3~4 票(实测 2026-11-04 P7、
    11-05 P6、11-06 P6、11-08 P5、11-09 P7 全是 3~4/8 票、conf 2.2~3.1)。
    这些假位置一旦落进某一局的区间里, 会把那一局的弹幕整段抢走。

    真分P 的实测分布是 7~8/8 票、conf ≥ 3.8, 与假位置没有重叠。所以这里要求
    「票数够」**且**「至少有一张强票」, 把纯靠票数凑上来的弱匹配挡在外面。
    """
    reasons = []
    if n_vote >= need and n_strong >= 1:
        reasons.append("多段一致")
    if n_strong >= 2:
        reasons.append("%d 张强票" % n_strong)
    if max_conf >= very_strong_conf and n_vote >= 2:
        reasons.append("强票+同向")
    if n_vote == n_seg and max_conf >= min_conf:
        reasons.append("全票一致")
    return reasons


def _raise_no_solid(clusters, n_seg, min_conf):
    """没有自洽位置时, 区分"全票随机水平"与"票数不足"两种原因。"""
    best_c = max(clusters, key=cluster_score)
    best_n = len(best_c[1])
    best_conf = max(best_c[1])
    if best_n >= n_seg and best_conf < min_conf:
        raise ApiError(
            "该分P 的音频在直播里匹配不到(全部 %d 段都落在同一处, 但最强票 conf=%.2f "
            "仅达随机水平, 低于下限 %.2f); 该时段直播可能没有这段音频"
            % (best_n, best_conf, min_conf))
    raise ApiError(
        "该分P 的音频在直播里找不到自洽位置(最多 %d/%d 段指向同一处, 最强票 conf=%.2f); "
        "可能是这场直播没有转播, 或采集期间音频断流"
        % (best_n, n_seg, best_conf))


def solid_clusters(clusters, n_seg, need, min_conf, hop, tolerance):
    """筛出自洽位置, 返回 (solid, 每个簇的采纳理由)。

    判据是**投票一致性**, 不是单段置信度: BP/解说段 conf 天然低(1.1~2.1),
    但位置是对的。强票门槛随 hop 缩放 —— hop 越大相邻帧越不相似、conf 系统性
    下降, 写死 6.0/7.0 会让"部分段落能对上"的救援分支失效(hop=0.4 时约 -45%)。
    """
    hop_scale = max(0.4, min(1.0, 0.2 / max(hop, 1e-6)))
    strong_conf = max(6.0, min_conf * 3) * hop_scale
    very_strong_conf = 7.0 * hop_scale
    solid, why = [], {}
    for c in clusters:
        n_vote = len(c[1])
        confs = sorted(c[1])
        max_conf = confs[-1]
        n_strong = len([x for x in confs if x >= strong_conf])
        # 锚定聚类已保证簇内离散 <= 容差, 这里再卡一道硬上限, 把"票数够但各自
        # 偏得远"的伪一致挡掉(实测随机噪声 spread 达 47.58 秒)。
        c_spread = max(c[3]) - min(c[3])
        if c_spread > tolerance:
            why[id(c)] = "簇内离散 %.1fs 超容差" % c_spread
            continue
        if max_conf < min_conf:
            why[id(c)] = "最强票 conf=%.2f 低于下限 %.2f" % (max_conf, min_conf)
            continue
        reasons = _reasons(n_vote, n_strong, n_seg, need, max_conf, min_conf,
                           very_strong_conf)
        if reasons:
            solid.append(c)
            why[id(c)] = "、".join(reasons)
    if not solid:
        _raise_no_solid(clusters, n_seg, min_conf)
    return solid, why
