"""夜间流水线: 每小时巡检 -> 发现赛程 -> 校准 -> 分发 -> 提交。

用法:
    python tools/nightly.py                # 跑一轮(适合交给计划任务)
    python tools/nightly.py --loop          # 常驻, 每 --interval 秒一轮
    python tools/nightly.py --date 2026-10-31 --dry-run

边界:
- 只写本地层与 `data/`; 只自动推送**仅含数据**的提交, 且受控制台「自动推送」
  开关控制(边界见 AGENTS.md「夜间流水线的自动推送」)。混入代码/文档的提交
  停下等人工推送。
- **校准成功才发布**: 当天场次里至少一个分P 有对齐结果, 才 split/commit/push。
  未校准的数据留在本地, 下一轮继续发现+校准; 超过 RETRY_DAYS 天仍校准不了则
  不再自动重搜(避免每小时对同一天无休止请求 B站), 需要时手动跑 --date。
  这样用户拉到的每一天都自带场次(align.json) —— 播放端不做赛程发现, 只负责播放。
- 有新增内容才提交, 没有就静默退出, 不产生空提交。
- 回放未上传/分P 取不到音频时跳过, 下一轮再试; 不写失败标记。
- 采集端负责写 manifest 快照; 本脚本不重新解释"哪些片已封口"。
"""
import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
for sub in ("danmaku-server", "tools"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import server  # noqa: E402
import calibrate  # noqa: E402
import pipelinelock  # noqa: E402
import pushctl  # noqa: E402
from daygate import RETRY_DAYS, calibrated, too_old as _too_old  # noqa: E402
from datapaths import publish_paths  # noqa: E402
from split_matches import pending_days  # noqa: E402
import tickplan  # noqa: E402

DATA = server.DATA_DIR


def _valid_day(name: str) -> bool:
    return len(name) == 10 and name[4] == "-" and not name.startswith("_")


def running_days() -> list:
    """正在采集的日期: 会话 manifest 里 ended_by == "running"。"""
    out = []
    if not DATA.is_dir():
        return out
    for day in sorted(DATA.iterdir()):
        if not day.is_dir() or not _valid_day(day.name):
            continue
        for sess in day.iterdir():
            mf = sess / "manifest.json"
            if not (sess.is_dir() and mf.is_file()):
                continue
            try:
                live = json.loads(mf.read_text(encoding="utf-8")).get("ended_by") == "running"
            except (OSError, ValueError):
                live = False
            if live:
                out.append(day.name)
                break
    return out


def _git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:300]}")
    return r.stdout


def _run_py(script: str, *args) -> str:
    r = subprocess.run([sys.executable, str(ROOT / script), *args], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"{script} 失败: {(r.stdout + r.stderr).strip()[-300:]}")
    return r.stdout


def publish(day: str, dry_run: bool = False) -> bool:
    """把一天的新增内容分发并提交。返回 True = 产生了一笔提交。"""
    if dry_run:
        paths = publish_paths(day)
        print(f"[dry-run] {day}: split --apply, git add {len(paths)} 个文件, "
              f"pack_data --index --staged, git commit")
        return False
    _run_py("tools/split_matches.py", "--apply")
    # split 成功后本地层已清空, 它留下的 .bak 不再需要(现场纪律: 不留 .bak)
    with contextlib.suppress(OSError):
        (ROOT / "danmaku-server" / "matches.json.bak").unlink()
    # 必须在 split 之后收集: data/{day}/align.json 正是 split 写出来的。
    paths = publish_paths(day)
    rels = [p.relative_to(ROOT).as_posix() for p in paths]
    if rels:
        _git("add", "--", *rels)
    # 没有任何数据变化时不要跑 pack_data: 它每次都会更新 generated_at,
    # 否则会产出"只改时间戳"的空数据提交, 还会被自动推送出去。
    if not _git("diff", "--cached", "--name-only", "--", "data").strip():
        print(f"{day}: 没有新增数据, 跳过提交")
        return False
    _run_py("tools/pack_data.py", "--index", "--staged")
    _git("add", "--", "data-index.json")
    staged = _git("diff", "--cached", "--name-only").splitlines()
    if not staged:
        print(f"{day}: 没有新增内容, 跳过提交")
        return False
    # 只允许提交数据与清单: 暂存区里若有用户/别的流程放的代码或文档, 宁可停下,
    # 也不能让夜间进程替人提交出去(pushctl 也会因此拒推整条链)。
    bad = [p for p in staged
           if p != "data-index.json" and not p.startswith("data/")]
    if bad:
        raise RuntimeError("暂存区含非数据文件, 未提交: " + ", ".join(bad[:5]))
    _git("commit", "-m", f"data({day}): 夜间增量发布")
    print(f"{day}: 已提交本地增量")
    return True


async def discover(day: str) -> None:
    """把当天赛程与 B站回放 BV 刷进本地层。失败不阻断校准。"""
    match_date = await asyncio.to_thread(server._session_match_date, day)
    # 必须用带兜底的 discover_draft: Riot 经常漏掉当天场次(表演赛/淘汰赛首日),
    # 只查 Riot 会让这一晚永远没有场次列表, 用户拉完数据仍然看不了。
    # 历史缺陷: 播放端 2026-11-04 加了兜底, 这里仍调 discover_draft_for_date,
    # 于是 2026-11-05 GEN vs LYON 一整轮巡检都报"赛程里没有可识别的场次"。
    draft = await asyncio.to_thread(server.discover_draft, match_date)
    if not draft:
        print(f"{day}: 赛程里没有可识别的场次")
        return
    for m in draft:
        m.setdefault("bv", "")           # 草稿默认没有 bv, 后面统一按"可能为空"处理
        if m.get("bv"):
            continue                     # 日期兜底已经带 BV, 不要用搜不到的结果覆盖它
        bv = await asyncio.to_thread(
            server.bili_search_replay, m["teams"][0], m["teams"][1], match_date)
        if bv:
            m["bv"] = bv
    missing = [m for m in draft if not m.get("bv")]
    if missing:
        # 队名搜索可能被排序埋掉; 再用"按日期搜官方投稿"兜底, 一次覆盖所有缺 BV 的场次。
        hits = await asyncio.to_thread(server.bili_discover_date, match_date)
        by_teams = {frozenset(t.lower() for t in (h.get("teams") or [])): h.get("bv")
                    for h in hits if h.get("bv")}
        for m in missing:
            bv = by_teams.get(frozenset(t.lower() for t in m["teams"]))
            if bv:
                m["bv"] = bv
        # Riot 有时只列部分场次: 日期搜索里另有对阵的, 补进草稿, 否则永远少一场。
        have = {frozenset(t.lower() for t in m["teams"]) for m in draft}
        for h in hits:
            key = frozenset(t.lower() for t in (h.get("teams") or []))
            if key and key not in have:
                draft.append(h)
                have.add(key)
    result = {"created": 0, "added": 0, "backfilled": 0, "time_filled": 0}

    def _mut(matches):
        server._apply_discover(matches, match_date, draft, result)

    if not await asyncio.to_thread(server.update_matches, _mut):
        print(f"{day}: 赛程写入本地层失败")
        return
    print(f"{day}: 赛程 {len(draft)} 场, 新回放 {sum(1 for m in draft if m.get('bv'))} 个")


def dirty_days() -> list:
    """有未发布改动的日期。

    两个来源缺一不可:
      - `data/` 下未提交的文件(收尾后 afp/meta/manifest 会出现在这里);
      - 本地层 `danmaku-server/matches.json` 里待分发的对齐记录。手动跑
        `tools/calibrate.py` 只写本地层; 若那天 data/ 已经提交干净, 只看
        git status 会永远认为"没有新数据", 重算的校准结果就永远发不出去。
    """
    days = set()
    try:
        text = _git("status", "--porcelain", "--", "data")
    except RuntimeError:
        text = ""
    for line in text.splitlines():
        rel = line[3:].strip().strip('"')
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 2 and _valid_day(parts[1]):
            days.add(parts[1])
    days.update(pending_days())
    return sorted(days)


async def calibrate_day(day: str, session: aiohttp.ClientSession) -> int:
    """对当天有 BV 的场次逐一分P 校准。返回成功场数。"""
    match_date = await asyncio.to_thread(server._session_match_date, day)
    lists = (await asyncio.to_thread(server.load_matches) or {}).get("lists") or {}
    targets = calibrate.targets_from_lists(lists, match_date)
    files = calibrate.session_files(day)
    if not targets or not files:
        print(f"{day}: 没有可校准的场次或音频指纹")
        return 0
    ok = 0
    for f in files:
        for t in targets:
            try:
                await server.run_autocalign(day, f, t["bv"], 0, session,
                                           record_failures=False)
                ok += 1
            except server.ApiError as e:
                print(f"{day}/{f} {t['bv']}: 本轮跳过({e})")
    print(f"{day}: 校准完成 {ok} 场")
    return ok


def _auto_push(dry_run: bool) -> None:
    """夜间自动推送。失败只打印, 不抛 —— 一次网络抖动不该让常驻进程退出。"""
    if dry_run:
        s = pushctl.status(ROOT)
        if s["pending"]:
            print(f"[dry-run] 待推 {s['pending']} 个提交，自动推送"
                  f"{'开' if s['settings']['auto'] else '关'}")
        return
    r = pushctl.auto_push(ROOT)
    if r.get("reason") == "nothing":
        return
    if r.get("reason") == "auto-off":
        print(r.get("message", "自动推送已关闭"))
        return
    if r.get("ok"):
        print(r.get("message", "已推送"))
        return
    print(f"推送未完成: {r.get('message', '')}")
    if r.get("detail"):
        print(r["detail"])


async def _calibrate_days(days: list) -> None:
    """发现赛程 + 逐分P 校准(只写本地层, 不碰 git)。"""
    async with aiohttp.ClientSession() as session:
        # 发现与校准都覆盖 days 而不只是 active: 回放常在采集结束后才上传,
        # 只给正在采集的日期重试, 已封口但还没校准的夜晚就永远等不到 BV,
        # 而推送闸门又不允许未校准的数据出去 —— 数据会永久卡在本地。
        for day in days:
            try:
                await discover(day)
            except Exception as e:          # 网络/赛程失败不阻断校准
                print(f"{day}: 刷新赛程失败({e})")
            try:
                await calibrate_day(day, session)
            except Exception as e:          # 某一天坏数据不能拖住其它日期的发布
                print(f"{day}: 校准失败({e})")


def _publish_days(publish_days: list, dry_run: bool = False) -> int:
    """分发 + 提交 + 推送。返回失败天数。"""
    if dry_run:
        for day in publish_days:
            if calibrated(day):
                publish(day, dry_run=True)
        return 0
    failed = 0
    for day in publish_days:
        if not calibrated(day):
            print(f"{day}: 尚未校准成功, 暂不推送(数据保留在本地, 下一轮重试)")
            continue
        try:
            publish(day, dry_run=False)
        except Exception as e:
            print(f"{day}: 发布失败({e})")
            failed += 1
    return failed


async def tick(dry_run: bool, only_date: str = "", mode: str = "all") -> int:
    """跑一轮巡检。返回值 = 发布失败的日期数(0 = 没有失败)。

    mode 把**校准**与**发布**拆成两条独立路径(见 tools/tickplan.py 的说明):
      all       发现 + 校准 + 分发提交推送(夜间常驻用)
      calibrate 只发现 + 校准, 结果写本地层; 不 split、不 commit、不 push
      publish   只分发 + 提交 + 推送(校准结果已就绪时用)
    """
    days, publish_days = tickplan.pick_days(
        only_date, running_days=running_days, dirty_days=dirty_days,
        too_old=_too_old, retry_days=RETRY_DAYS)
    return await tickplan.run_tick(
        mode=mode, days=days, publish_days=publish_days, dry_run=dry_run,
        calibrate_days=_calibrate_days,
        publish_days_fn=lambda ds: _publish_days(ds, dry_run),
        auto_push=_auto_push, is_calibrated=calibrated)


async def discover_days(days: list) -> None:
    """只刷新赛程(采集控制台的「刷新赛程」按钮用)。"""
    for day in days:
        try:
            await discover(day)
        except Exception as e:
            print(f"{day}: 刷新赛程失败({e})")


def _run_once(args) -> int:
    """跑一轮(或指定日期), 全程持有单轮锁。

    单轮锁的意义: 常驻进程正在 commit 时, 手动点「校准」或「发布」不能同时进来 ——
    两边各跑一次 split_matches/pack_data, 轻则重复提交, 重则撞 index.lock。
    手动那条会等最多 90 秒(一轮通常几十秒), 等不到就明确报"正在跑, 稍后再试"。
    """
    lock = pipelinelock.FileLock(pipelinelock.TICK_LOCK)
    if not lock.acquire(wait_sec=90):
        print("另一轮巡检正在执行(单轮锁被占用), 本次跳过")
        return 3
    mode = getattr(args, "mode", "all") or "all"
    try:
        failed = asyncio.run(tick(args.dry_run, args.date, mode))
    finally:
        lock.release()
    # --date 是控制台手动按钮的入口: 没校准或没提交都算失败, 不能退出 0。
    if args.date and not args.dry_run:
        return tickplan.manual_exit_code(args.date, mode, failed, calibrated)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """命令行参数。抽出来只为让 main 只剩"选哪条路径"这一件事。"""
    ap = argparse.ArgumentParser(description="夜间巡检: 校准 + 分发 + 提交/推送")
    ap.add_argument("--date", default="", help="只处理这一天(默认扫描正在采集的日期)")
    ap.add_argument("--loop", action="store_true", help="常驻, 按 --interval 循环")
    ap.add_argument("--interval", type=int, default=3600, help="巡检间隔(秒)")
    ap.add_argument("--dry-run", action="store_true", help="只打印动作, 不写不改不提交")
    ap.add_argument("--discover-only", action="store_true", help="只刷新赛程, 不校准/发布")
    ap.add_argument("--mode", choices=("all", "calibrate", "publish"), default="all",
                    help="all=发现+校准+发布(默认); calibrate=只校准; publish=只发布")
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    if args.date and not _valid_day(args.date):
        print(f"日期格式不对: {args.date}")
        return 2
    if args.discover_only:
        # 与常驻巡检共用单轮锁: 两边同时 read-modify-write matches.json 会丢记录
        lock = pipelinelock.FileLock(pipelinelock.TICK_LOCK)
        if not lock.acquire(wait_sec=90):
            print("另一轮巡检正在执行(单轮锁被占用), 本次跳过")
            return 3
        try:
            asyncio.run(discover_days([args.date] if args.date else running_days()))
        finally:
            lock.release()
        return 0
    if not args.loop:
        return _run_once(args)

    loop_lock = pipelinelock.FileLock(pipelinelock.LOOP_LOCK)
    if not loop_lock.acquire():
        pid = pipelinelock.is_alive().get("pid", "?")
        print(f"已有发布进程在运行(PID {pid}), 本进程退出")
        return 3
    interval = max(60, args.interval)
    pipelinelock.clear_stop()
    try:
        while True:
            pipelinelock.beat(interval)
            try:
                rc = _run_once(args)
            except Exception as e:       # 单轮异常不能让常驻进程直接退出
                print(f"本轮巡检异常: {e}")
                rc = 1
            if rc == 3:
                print("跳过本轮(另一轮正在执行)")
            # 停止请求由控制台写文件, 这里自己退 —— 不强杀, 免得停在 commit 中间
            if pipelinelock.stop_requested():
                print("收到停止请求, 发布进程退出")
                break
            # 分段睡眠: 一觉睡一小时会让"停止"要等一小时才生效
            slept = 0
            while slept < interval:
                if pipelinelock.stop_requested():
                    print("收到停止请求, 发布进程退出")
                    return 0
                time.sleep(min(5, interval - slept))
                slept += 5
                pipelinelock.beat(interval)
    finally:
        pipelinelock.clear_pid()
        pipelinelock.clear_stop()
        loop_lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
