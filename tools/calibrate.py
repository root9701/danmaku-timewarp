"""无头校准 CLI: 不启动播放端, 直接对某天的官方回放算 offset。

用法:
    python tools/calibrate.py --date 2026-10-31
    python tools/calibrate.py --date 2026-10-31 --bv BV1W2x66WEE9
    python tools/calibrate.py --date 2026-10-31 --dry-run

只写本地层 `danmaku-server/matches.json`; 不碰 `data/{日期}/align.json`
(分发由 `tools/split_matches.py --apply` 负责, 夜间流水线再提交)。

设计约束:
- 不依赖 HTTP 服务, 但复用 server 里的取流与对齐编排(同一份实现, 避免两套算法)。
- 目标是"能反复跑": 取不到音频的分P 保留上次 offset, 由 run_autocalign 区分处理。
"""
import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "danmaku-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import server  # noqa: E402
import pipelinelock  # noqa: E402


def targets_from_lists(lists, match_date: str, only_bv: str = "") -> list:
    """从 matches 的 lists 里选出当天带 BV 的场次。纯函数, 便于单测。"""
    out = []
    for item in (lists or {}).get(match_date) or []:
        if not isinstance(item, dict):
            continue
        bv = str(item.get("bv") or "").strip()
        if not bv or (only_bv and bv != only_bv):
            continue
        out.append({"bv": bv, "title": str(item.get("title") or "")})
    return out


def session_files(date: str, data_dir=None) -> list:
    """某日下带音频指纹的会话目录名(run_autocalign 的 file 参数)。

    只认含 `*.afp.jsonl` / `*.afp.jsonl.gz` 的目录: 没有指纹的会话对齐不了。
    `_test` 目录不参与正式发布, 直接跳过。
    """
    day = (data_dir or server.DATA_DIR) / date
    if not day.is_dir():
        return []
    out = []
    for d in sorted(day.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if any(p.is_file() and ".afp.jsonl" in p.name for p in d.iterdir()):
            out.append(d.name)
    return out


async def run(date: str, targets: list, files: list, page: int, dry_run: bool) -> int:
    """逐个 (会话, 场次) 跑校准。返回进程退出码: 0 = 至少一场成功。"""
    if not server.HAS_AUDIOFP:
        print(server._no_audiofp_msg())
        return 2
    if dry_run:
        for f in files:
            for t in targets:
                print(f"[dry-run] {date}/{f} {t['bv']} {t['title']}")
        return 0
    ok = fail = 0
    async with aiohttp.ClientSession() as session:
        for f in files:
            for t in targets:
                try:
                    r = await server.run_autocalign(date, f, t["bv"], page, session)
                except server.ApiError as e:
                    fail += 1
                    print(f"[失败] {date}/{f} {t['bv']}: {e}")
                    continue
                ok += 1
                print(f"[完成] {date}/{f} {t['bv']} {t['title']}: "
                      f"{r['aligned_pages']}/{r['total_pages']} 分P, "
                      f"首个 offset={r['offset_sec']}s")
    print(f"结束: 成功 {ok} 场, 失败 {fail} 场; 结果已写入本地层 matches.json")
    return 0 if ok else 1


def _calibrate(args) -> int:
    match_date = args.match_date or server._session_match_date(args.date)
    lists = (server.load_matches() or {}).get("lists") or {}
    targets = targets_from_lists(lists, match_date, args.bv)
    if not targets:
        print(f"{match_date} 的赛程里没有可校准的 BV; 先跑 nightly --discover-only 补齐(或等下一轮巡检)")
        return 2
    files = [args.file] if args.file else session_files(args.date)
    if not files:
        print(f"{args.date} 下没有带音频指纹的会话目录")
        return 2
    if len(files) > 1:
        print(f"注意: {args.date} 有 {len(files)} 个会话目录, 将逐个校准: {files}")
    return asyncio.run(run(args.date, targets, files, args.page, args.dry_run))


def main() -> int:
    ap = argparse.ArgumentParser(description="无头校准: 官方回放 -> 直播 offset")
    ap.add_argument("--date", required=True, help="数据目录日期(YYYY-MM-DD)")
    ap.add_argument("--match-date", default="",
                    help="比赛日(默认按数据目录推断); 用于定位 lists 里的场次")
    ap.add_argument("--bv", default="", help="只校准这一场(默认当天全部有 BV 的场次)")
    ap.add_argument("--file", default="", help="只校准这个会话目录(默认全部带 afp 的)")
    ap.add_argument("--page", type=int, default=0, help="分P(0 = 全部分P)")
    ap.add_argument("--dry-run", action="store_true", help="只打印要做什么, 不写任何文件")
    args = ap.parse_args()

    if not server.DATE_RE.fullmatch(args.date):
        print(f"日期格式不对: {args.date}(应为 YYYY-MM-DD)")
        return 2
    # 与常驻巡检共用单轮锁: 两边同时写 matches.json / split 会丢记录
    lock = pipelinelock.FileLock(pipelinelock.TICK_LOCK)
    if not lock.acquire(wait_sec=90):
        print("另一轮巡检正在执行(单轮锁被占用), 本次跳过")
        return 3
    try:
        return _calibrate(args)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
