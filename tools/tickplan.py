# -*- coding: utf-8 -*-
"""tickplan.py — 一轮巡检的编排: 校准与发布**拆成两条独立路径**。

为什么单独成文件:
  1. 校准与发布是两件事, 各自有独立的失败含义 ——
       校准回答"回放对到直播哪一刻", 结果写本地层, 可反复重算;
       发布回答"这些结果该不该给用户", 要过推送闸门、要提交、要推远端。
     混在一个按钮里, 用户点完不知道"算完了没有"和"提交了没有"。
  2. `nightly.py` 已经接近 400 行上限, 而这段编排逻辑自成一体
     (输入是日期与 mode, 输出是失败天数), 抽出来边界清晰。

边界: 只编排"先做什么再做什么", 具体动作全部由调用方注入 ——
  这样本模块不依赖 server/aiohttp, 能单独测(见 tests/test_tickplan.py)。
"""


def pick_days(only_date: str, *, running_days, dirty_days, too_old,
              retry_days, print_fn=print) -> tuple:
    """算出本轮要处理的日期。返回 (要校准的日期, 要发布的日期)。

    only_date 给了就只处理那一天(不捎带其它脏日期), 否则按"正在采集 + 最近
    未发布"来挑。抽到本模块是因为它和 mode 是同一层判断: 都在回答
    "这一轮该动哪些天"。
    """
    if only_date:
        days = [only_date]
        return days, days
    active = running_days()
    dirty = dirty_days()
    if not active and not dirty:
        print_fn("没有正在采集的会话")
    # 只重试最近 RETRY_DAYS 天: 更早的日期再搜也不会变, 只会累积 B站请求。
    retry = [d for d in dirty if d in active or not too_old(d)]
    stale = [d for d in dirty if d not in retry]
    if stale:
        print_fn(f"超过 {retry_days} 天仍未校准, 不再自动重试: {', '.join(stale)}")
    days = sorted(set(active) | set(retry))
    # 发布仍覆盖所有脏日期: 旧日期只要已校准, 新数据就该照常提交。
    return days, sorted(set(active) | set(dirty))


async def run_tick(*, mode: str, days: list, publish_days: list, dry_run: bool,
                   calibrate_days, publish_days_fn, auto_push,
                   is_calibrated, print_fn=print) -> int:
    """按 mode 跑一轮。返回发布失败天数(0 = 没有失败)。

    参数(全部由调用方注入, 本模块不 import nightly 的任何东西):
      mode          all | calibrate | publish
      days          要发现+校准的日期
      publish_days  要分发+提交+推送的日期
      dry_run       只打印动作
      calibrate_days     async (days) -> None
      publish_days_fn    (days) -> 失败天数
      auto_push          (dry_run) -> None
      is_calibrated      (day) -> bool
    """
    want_cal = mode in ("all", "calibrate")
    want_pub = mode in ("all", "publish")

    if dry_run:
        # 真 dry-run: 不联网、不校准、不写本地层, 只打印将要发布的日期
        print_fn("[dry-run] 处理日期: " + (", ".join(days) if days else "无"))
        if want_cal:
            print_fn("[dry-run] 会做: 发现赛程 + 逐分P 校准(只写本地层)")
        if not want_pub:
            print_fn("[dry-run] 不做分发/提交/推送(--mode=calibrate)")
            return 0
        for day in days:
            if not is_calibrated(day):
                print_fn(f"[dry-run] {day}: 尚未校准成功, 不会推送")
                continue
            publish_days_fn([day])          # dry-run 由调用方内部处理
        auto_push(True)
        return 0

    if want_cal:
        await calibrate_days(days)
    if not want_pub:
        return 0
    failed = publish_days_fn(publish_days)
    # 即使今天没有新日期, 也要试一次: 上一轮可能提交成功但推送失败。
    auto_push(False)
    return failed


def manual_exit_code(day: str, mode: str, failed: int, is_calibrated,
                     print_fn=print) -> int:
    """手动跑(--date)的退出码: 让控制台能如实报"校准成了没/提交成了没"。

    4 = 未校准(闸门不放行), 5 = 发布失败; 0 = 按 mode 该做的都成了。
    """
    if mode == "calibrate":
        if not is_calibrated(day):
            print_fn(f"{day}: 本轮尚未校准成功")
            return 4
        print_fn(f"{day}: 校准完成(未提交, 用「发布」按钮再提交)")
        return 0
    if failed:
        print_fn(f"{day}: 发布失败, 本轮未提交")
        return 5
    if not is_calibrated(day):
        print_fn(f"{day}: 本轮尚未校准成功, 未发布")
        return 4
    return 0
