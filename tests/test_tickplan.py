# -*- coding: utf-8 -*-
"""tickplan.py 回归: 校准与发布必须是两条独立路径。

用法: python tests/test_tickplan.py     (退出码 0 = 全过)

背景(2026-11-07 用户反馈): 控制台原来只有一个「校准并提交」按钮, 点完不知道
"算完了没有"和"提交了没有"; 而校准失败和提交失败报的是同一句话。根因是
`nightly.tick` 把两件事串在一条路径里, 没有中间状态可表达。

这里用注入的假动作验证三条边界:
  1. calibrate 模式绝不调用发布(不 split/不 commit/不 push)
  2. publish 模式绝不调用校准(校准已就绪时才走这条)
  3. all 模式两者都做; dry-run 不做任何真动作
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import tickplan                                            # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def _run(mode, days, publish_days, dry_run, calibrated=True):
    """跑一次 run_tick, 记录实际调用了哪些动作。"""
    calls = []

    async def fake_cal(ds):
        calls.append(("calibrate", list(ds)))

    def fake_pub(ds):
        calls.append(("publish", list(ds)))
        return 0

    def fake_push(dry):
        calls.append(("push", dry))

    asyncio.run(tickplan.run_tick(
        mode=mode, days=days, publish_days=publish_days, dry_run=dry_run,
        calibrate_days=fake_cal, publish_days_fn=fake_pub,
        auto_push=fake_push, is_calibrated=lambda d: calibrated,
        print_fn=lambda *a, **k: None))
    return calls


def test_calibrate_only():
    print("\n[calibrate] 只校准, 绝不发布")
    calls = _run("calibrate", ["2026-11-07"], ["2026-11-07"], dry_run=False)
    kinds = [c[0] for c in calls]
    check("调用了校准", kinds == ["calibrate"], str(kinds))
    check("没有发布", "publish" not in kinds, str(kinds))
    check("没有推送", "push" not in kinds, str(kinds))


def test_publish_only():
    print("\n[publish] 只发布, 不重跑校准")
    calls = _run("publish", ["2026-11-07"], ["2026-11-07"], dry_run=False)
    kinds = [c[0] for c in calls]
    check("调用了发布", kinds == ["publish", "push"], str(kinds))
    check("没有校准", "calibrate" not in kinds, str(kinds))


def test_all_mode():
    print("\n[all] 两者都做(夜间常驻)")
    calls = _run("all", ["2026-11-07"], ["2026-11-07"], dry_run=False)
    kinds = [c[0] for c in calls]
    check("先校准后发布", kinds == ["calibrate", "publish", "push"], str(kinds))


def test_dry_run():
    print("\n[dry-run] 不做任何真动作")
    calls = _run("all", ["2026-11-07"], ["2026-11-07"], dry_run=True)
    check("没有真校准", "calibrate" not in [c[0] for c in calls], str(calls))
    check("推送也是 dry", all(c[1] is True for c in calls if c[0] == "push"),
          str(calls))


def test_manual_exit_code():
    print("\n[退出码] 手动按钮要能区分'校准成了没'与'提交成了没'")
    ok = tickplan.manual_exit_code("2026-11-07", "calibrate", 0,
                                   lambda d: True, print_fn=lambda *a: None)
    check("calibrate 成功返回 0", ok == 0, str(ok))
    bad = tickplan.manual_exit_code("2026-11-07", "calibrate", 0,
                                    lambda d: False, print_fn=lambda *a: None)
    check("calibrate 未校准返回 4", bad == 4, str(bad))
    failed = tickplan.manual_exit_code("2026-11-07", "publish", 1,
                                       lambda d: True, print_fn=lambda *a: None)
    check("publish 失败返回 5", failed == 5, str(failed))


def test_pick_days():
    print("\n[日期] 只处理 --date 指定那天, 不捎带其它脏日期")
    days, pub = tickplan.pick_days(
        "2026-11-07", running_days=lambda: [], dirty_days=lambda: [],
        too_old=lambda d: False, retry_days=3, print_fn=lambda *a: None)
    check("只一天", days == ["2026-11-07"] and pub == ["2026-11-07"], str(days))

    days, pub = tickplan.pick_days(
        "", running_days=lambda: ["2026-11-07"],
        dirty_days=lambda: ["2026-11-06", "2000-01-01"],
        too_old=lambda d: d == "2000-01-01", retry_days=3,
        print_fn=lambda *a: None)
    check("过期日期不再重试", "2000-01-01" not in days, str(days))
    check("发布仍覆盖脏日期", "2000-01-01" in pub, str(pub))


def main() -> int:
    print("tickplan 回归")
    test_calibrate_only()
    test_publish_only()
    test_all_mode()
    test_dry_run()
    test_manual_exit_code()
    test_pick_days()
    print()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项未过")
        for n in FAILED:
            print(f"  {n}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
