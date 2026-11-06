# -*- coding: utf-8 -*-
"""夜间流水线回归: 采集日期识别与发布文件选择。

不碰真实数据, 全部在临时目录里跑; 不跑网络与 git 提交。
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "danmaku-server"))

import nightly                                            # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def write_text(path: Path, text: str = "x") -> None:
    path.write_text(text, encoding="utf-8")


def test_running_days_and_publish_paths():
    with tempfile.TemporaryDirectory(prefix="dmk_nl_") as td:
        base = Path(td)
        sess = base / "2026-10-31" / "7734200"
        sess.mkdir(parents=True)
        write_text(sess / "7734200.afp.jsonl")
        write_text(sess / "7734200_s2300.jsonl.gz", "")
        write_text(sess / "7734200_s0000.jsonl")
        write_text(sess / "7734200.meta.json", "{}")
        write_text(sess / "7734200_s9999.jsonl.gz", "")     # manifest 未登记
        (base / "2026-10-31" / "align.json").write_text("{}", encoding="utf-8")
        mf = {"schema_version": 1, "ended_by": "running", "files": ["7734200_s2300.jsonl.gz"]}
        write_text(sess / "manifest.json", json.dumps(mf))

        nightly.DATA = base
        check("识别正在采集的日期", nightly.running_days() == ["2026-10-31"],
              str(nightly.running_days()))

        paths = [p.name for p in nightly.publish_paths("2026-10-31", base)]
        check("采集中只发布 manifest + 已封口片 + align",
              set(paths) == {"manifest.json", "7734200_s2300.jsonl.gz", "align.json"},
              str(paths))
        check("manifest 未登记的切片不发布", "7734200_s9999.jsonl.gz" not in paths,
              str(paths))

        mf["ended_by"] = "live_end"
        write_text(sess / "manifest.json", json.dumps(mf))
        paths = [p.name for p in nightly.publish_paths("2026-10-31", base)]
        check("收尾后补 afp/meta/raw",
              {"7734200.afp.jsonl", "7734200.meta.json", "7734200_s0000.jsonl"} <= set(paths),
              str(paths))


def test_discover_uses_fallback():
    """夜间流水线必须走带兜底的 discover_draft, 否则 Riot 漏场次的日子永远空列表。

    历史缺陷: 播放端 2026-11-04 加了 B 站兜底, 这里仍调 discover_draft_for_date,
    于是 2026-11-05 一整轮巡检都报"赛程里没有可识别的场次"。
    """
    called = {"fn": None}

    def fake_draft(date):
        called["fn"] = "discover_draft"
        return [{"title": "GEN vs LYON", "teams": ["GEN", "LYON"], "bv": "BV_X",
                 "start": __import__("datetime").datetime(2026, 11, 5, 6, 0)}]

    def fake_update(mut):
        m = {"lists": {}, "align": {}}
        mut(m)
        return True

    old = (nightly.server.discover_draft, nightly.server.discover_draft_for_date,
           nightly.server._session_match_date, nightly.server.update_matches)
    try:
        nightly.server.discover_draft = fake_draft
        nightly.server.discover_draft_for_date = lambda d: (_ for _ in ()).throw(
            AssertionError("不该再调只查 Riot 的 discover_draft_for_date"))
        nightly.server._session_match_date = lambda d: d
        nightly.server.update_matches = fake_update
        asyncio.run(nightly.discover("2026-11-05"))
        check("夜间发现走带兜底的 discover_draft", called["fn"] == "discover_draft",
              str(called))
    finally:
        (nightly.server.discover_draft, nightly.server.discover_draft_for_date,
         nightly.server._session_match_date, nightly.server.update_matches) = old


def test_loop_mode_single_instance():
    """常驻模式必须拒绝第二个实例(实测事故: 两个 nightly 同时 commit)。"""
    print("\n[单实例] 常驻模式的锁")
    import argparse
    import subprocess
    import sys as _sys
    tmp = Path(tempfile.mkdtemp(prefix="dmk_nl2_"))
    try:
        nightly.pipelinelock.RUN_DIR = tmp
        nightly.pipelinelock.LOOP_LOCK = tmp / "pipeline.loop.lock"
        nightly.pipelinelock.PID_PATH = tmp / "pipeline.pid"
        nightly.pipelinelock.STOP_PATH = tmp / "pipeline.stop"
        holder = nightly.pipelinelock.FileLock(nightly.pipelinelock.LOOP_LOCK)
        holder.acquire()
        args = argparse.Namespace(date="", loop=True, interval=3600,
                                  dry_run=True, discover_only=False)
        old_parse = nightly.argparse.ArgumentParser.parse_args
        nightly.argparse.ArgumentParser.parse_args = lambda self: args
        try:
            rc = nightly.main()
        finally:
            nightly.argparse.ArgumentParser.parse_args = old_parse
            holder.release()
        check("已有实例时第二个退出", rc == 3, f"退出码 {rc}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_lock_conflict():
    """单轮锁冲突时跳过本轮, 而不是两边一起 commit。"""
    print("\n[单轮] 冲突时跳过")
    import argparse
    tmp = Path(tempfile.mkdtemp(prefix="dmk_nl3_"))
    try:
        nightly.pipelinelock.TICK_LOCK = tmp / "pipeline.tick.lock"
        holder = nightly.pipelinelock.FileLock(nightly.pipelinelock.TICK_LOCK)
        holder.acquire()
        args = argparse.Namespace(date="", loop=False, interval=3600,
                                  dry_run=True, discover_only=False)
        old_parse = nightly.argparse.ArgumentParser.parse_args
        old_wait = nightly.pipelinelock.FileLock.acquire
        # 把 90 秒等待改成 0, 免得测试真等
        nightly.pipelinelock.FileLock.acquire = lambda self, wait_sec=0: old_wait(self, 0)
        nightly.argparse.ArgumentParser.parse_args = lambda self: args
        try:
            rc = nightly.main()
        finally:
            nightly.argparse.ArgumentParser.parse_args = old_parse
            nightly.pipelinelock.FileLock.acquire = old_wait
            holder.release()
        check("锁被占用时跳过", rc == 3, f"退出码 {rc}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_keeps_fallback_bv():
    """兜底草稿自带 BV 时, 不能再被搜不到的 bili_search_replay 覆盖成空。

    2026-11-06: Riot 赛程漏了 DK vs BLG, 日期兜底从官方投稿找到了 BV;
    随后发现流程逐场重搜, 综合排序又搜不到, 把 BV 清空 —— 场次建出来了却
    永远无法校准。已有 BV 必须直接跳过。
    """
    print("\n[兜底] 已有 BV 不被空搜索结果覆盖")
    seen = {}

    def fake_draft(date):
        return [{"title": "DK vs BLG", "teams": ["DK", "BLG"], "bv": "BV_FALLBACK",
                 "start": datetime(2026, 11, 6, 6, 0)}]

    def fake_update(mut):
        m = {"lists": {}, "align": {}}
        mut(m)
        seen["lists"] = m["lists"]
        return True

    old = (nightly.server.discover_draft, nightly.server._session_match_date,
           nightly.server.bili_search_replay, nightly.server.update_matches)
    try:
        nightly.server.discover_draft = fake_draft
        nightly.server._session_match_date = lambda d: d
        nightly.server.bili_search_replay = lambda a, b, c: ""
        nightly.server.update_matches = fake_update
        asyncio.run(nightly.discover("2026-11-06"))
    finally:
        (nightly.server.discover_draft, nightly.server._session_match_date,
         nightly.server.bili_search_replay, nightly.server.update_matches) = old
    lst = seen.get("lists", {}).get("2026-11-06") or []
    check("场次建出来了", len(lst) == 1, str(lst))
    check("BV 没被清空", bool(lst) and lst[0]["bv"] == "BV_FALLBACK", str(lst))


def test_discover_fills_bv_from_date_search():
    """Riot 已列出场次、但队名搜索找不到 BV 时, 用日期搜索兜底。

    2026-11-06 实测: 「全球总决赛 DK BLG」综合与最新排序前 20 条都没有官方
    回放, 但按日期搜官方投稿第 3 条就是它。若只信队名搜索, 这一天永远没有 BV,
    推送闸门会把数据永久卡在本地。
    """
    print("\n[兜底] 队名搜索失败后按日期补齐 BV")
    seen = {}

    def fake_draft(date):
        return [{"title": "DK vs BLG", "teams": ["DK", "BLG"], "bv": "",
                 "start": datetime(2026, 11, 6, 6, 0)}]

    def fake_date_search(date):
        return [{"title": "DK vs BLG", "teams": ["DK", "BLG"], "bv": "BV_DATE",
                 "start": datetime(2026, 11, 6, 6, 0)}]

    def fake_update(mut):
        m = {"lists": {}, "align": {}}
        mut(m)
        seen["lists"] = m["lists"]
        return True

    old = (nightly.server.discover_draft, nightly.server._session_match_date,
           nightly.server.bili_search_replay, nightly.server.bili_discover_date,
           nightly.server.update_matches)
    try:
        nightly.server.discover_draft = fake_draft
        nightly.server._session_match_date = lambda d: d
        nightly.server.bili_search_replay = lambda a, b, c: ""
        nightly.server.bili_discover_date = fake_date_search
        nightly.server.update_matches = fake_update
        asyncio.run(nightly.discover("2026-11-06"))
    finally:
        (nightly.server.discover_draft, nightly.server._session_match_date,
         nightly.server.bili_search_replay, nightly.server.bili_discover_date,
         nightly.server.update_matches) = old
    lst = seen.get("lists", {}).get("2026-11-06") or []
    check("场次建出来了", len(lst) == 1, str(lst))
    check("日期搜索补上了 BV", bool(lst) and lst[0]["bv"] == "BV_DATE", str(lst))


def test_discover_merges_missing_match():
    """Riot 只列了部分场次时, 日期兜底要把另一场补进草稿。

    否则用户拿到的列表永远少一场, 且因为没有"缺失"信号, 后续也不会再补。
    """
    print("\n[兜底] Riot 漏场时用日期搜索补齐")
    seen = {}

    def fake_draft(date):
        return [{"title": "DK vs BLG", "teams": ["DK", "BLG"],
                 "start": datetime(2026, 11, 6, 6, 0), "bv": ""}]

    def fake_date_search(date):
        return [
            {"title": "DK vs BLG", "teams": ["DK", "BLG"], "bv": "BV_DK",
             "start": datetime(2026, 11, 6, 6, 57), "start_is_publish": True},
            {"title": "GEN vs LYON", "teams": ["GEN", "LYON"], "bv": "BV_GEN",
             "start": datetime(2026, 11, 6, 8, 0), "start_is_publish": True},
        ]

    def fake_update(mut):
        m = {"lists": {}, "align": {}}
        mut(m)
        seen["lists"] = m["lists"]
        return True

    old = (nightly.server.discover_draft, nightly.server._session_match_date,
           nightly.server.bili_search_replay, nightly.server.bili_discover_date,
           nightly.server.update_matches)
    try:
        nightly.server.discover_draft = fake_draft
        nightly.server._session_match_date = lambda d: d
        nightly.server.bili_search_replay = lambda a, b, c: ""
        nightly.server.bili_discover_date = fake_date_search
        nightly.server.update_matches = fake_update
        asyncio.run(nightly.discover("2026-11-06"))
    finally:
        (nightly.server.discover_draft, nightly.server._session_match_date,
         nightly.server.bili_search_replay, nightly.server.bili_discover_date,
         nightly.server.update_matches) = old
    lst = seen.get("lists", {}).get("2026-11-06") or []
    got = {e["title"]: e.get("bv") for e in lst}
    check("两场都在", got == {"DK vs BLG": "BV_DK", "GEN vs LYON": "BV_GEN"},
          str(got))
    check("补进来的场次不把投稿时间当开赛时间",
          "start_ms" not in next(e for e in lst if e["title"] == "GEN vs LYON"),
          str(lst))


def test_discover_without_bv():
    """两个搜索都没有结果时, 仍要建出场次列表(只是 BV 为空)。

    覆盖真实路径: nightly.discover -> _apply_discover 的完整调用链。
    旧实现草稿缺 bv 键, 这里会直接 KeyError, 整天赛程落不了盘。
    """
    print("\n[兜底] 搜不到 BV 也要建出场次")
    seen = {}

    def fake_draft(date):
        return [{"title": "DK vs BLG", "teams": ["DK", "BLG"],
                 "start": datetime(2026, 11, 6, 6, 0)}]

    def fake_update(mut):
        m = {"lists": {}, "align": {}}
        mut(m)
        seen["lists"] = m["lists"]
        return True

    old = (nightly.server.discover_draft, nightly.server._session_match_date,
           nightly.server.bili_search_replay, nightly.server.bili_discover_date,
           nightly.server.update_matches)
    try:
        nightly.server.discover_draft = fake_draft
        nightly.server._session_match_date = lambda d: d
        nightly.server.bili_search_replay = lambda a, b, c: ""
        nightly.server.bili_discover_date = lambda d: []
        nightly.server.update_matches = fake_update
        asyncio.run(nightly.discover("2026-11-06"))
    finally:
        (nightly.server.discover_draft, nightly.server._session_match_date,
         nightly.server.bili_search_replay, nightly.server.bili_discover_date,
         nightly.server.update_matches) = old
    lst = seen.get("lists", {}).get("2026-11-06") or []
    check("场次建出来了", len(lst) == 1, str(lst))
    check("BV 为空而不是崩掉", bool(lst) and lst[0].get("bv") == "", str(lst))


def test_retry_window():
    print("\n[重试窗口] 太老的脏日期不再每小时重搜")
    today = datetime.now().strftime("%Y-%m-%d")
    check("今天不算过期", not nightly._too_old(today))
    check("很久以前算过期", nightly._too_old("2000-01-01"))
    check("非法日期不过期", not nightly._too_old("_test"))


def test_tick_retry_window():
    print("\n[重试窗口] 超期日期只发布, 不再发现+校准")
    discovered, calls = [], []

    async def fake_discover(day):
        discovered.append(day)

    async def fake_cal(day, session):
        return 0

    old = (nightly.running_days, nightly.dirty_days, nightly._too_old,
           nightly.discover, nightly.calibrate_day, nightly.calibrated,
           nightly.publish, nightly._auto_push)
    nightly.running_days = lambda: ["2026-11-06"]
    nightly.dirty_days = lambda: ["2026-11-04", "2026-11-05"]
    nightly._too_old = lambda d: d == "2026-11-04"
    nightly.discover = fake_discover
    nightly.calibrate_day = fake_cal
    nightly.calibrated = lambda d: True
    nightly.publish = lambda day, dry_run=False: calls.append(day)
    nightly._auto_push = lambda dry_run: None
    try:
        asyncio.run(nightly.tick(False))
    finally:
        (nightly.running_days, nightly.dirty_days, nightly._too_old,
         nightly.discover, nightly.calibrate_day, nightly.calibrated,
         nightly.publish, nightly._auto_push) = old
    check("过期的日期不再重搜", discovered == ["2026-11-05", "2026-11-06"],
          str(discovered))
    check("已校准的过期日期仍发布",
          calls == ["2026-11-04", "2026-11-05", "2026-11-06"], str(calls))


def test_publish_collects_align_after_split():
    """split 会写出 align.json, 文件列表必须在它之后收集。

    历史缺陷: 先收集再 split, 校准当天第一次提交永远漏掉 align.json ——
    本地看着已对齐, 远端却没有场次列表, 用户拉完数据仍然"没赛程"。
    """
    print("\n[发布] 文件列表在 split 之后收集")
    order = []
    seen = []
    ready = {"v": False}

    def fake_run_py(script, *args):
        order.append(script)
        if script.endswith("split_matches.py"):
            ready["v"] = True           # 模拟 split 写出 data/{day}/align.json
        return ""

    def fake_git(*args):
        return ""

    def fake_paths(day, data_dir=None):
        order.append("paths")
        seen.append(ready["v"])
        return []

    old_run, old_git, old_paths = nightly._run_py, nightly._git, nightly.publish_paths
    try:
        nightly._run_py, nightly._git, nightly.publish_paths = fake_run_py, fake_git, fake_paths
        nightly.publish("2026-11-06")
    finally:
        nightly._run_py, nightly._git, nightly.publish_paths = old_run, old_git, old_paths
    split_name = "tools/split_matches.py"
    check("先 split 再收集", order.index("paths") > order.index(split_name),
          str(order))
    check("收集时 align.json 已存在", seen == [True], str(seen))


def test_publish_skips_empty_index():
    print("\n[发布] 没有数据变化时不生成只改时间戳的提交")
    calls = []

    def fake_run(script, *args):
        calls.append(script)
        return ""

    def fake_git(*args):
        return ""

    old_run, old_git, old_paths = nightly._run_py, nightly._git, nightly.publish_paths
    try:
        nightly._run_py, nightly._git = fake_run, fake_git
        nightly.publish_paths = lambda day, data_dir=None: []
        got = nightly.publish("2026-11-06")
    finally:
        nightly._run_py, nightly._git, nightly.publish_paths = old_run, old_git, old_paths
    check("返回 False", got is False, str(got))
    check("没有跑 pack_data", not any("pack_data" in s for s in calls), str(calls))


def test_publish_refuses_non_data_staged():
    print("\n[发布] 暂存区混入非数据文件时拒绝提交")
    def fake_run(script, *args):
        return ""

    def fake_git(*args):
        if tuple(args) == ("diff", "--cached", "--name-only", "--", "data"):
            return "data/2026-11-06/align.json"
        if tuple(args) == ("diff", "--cached", "--name-only"):
            return "data/2026-11-06/align.json\nREADME.md"
        return ""

    old_run, old_git, old_paths = nightly._run_py, nightly._git, nightly.publish_paths
    try:
        nightly._run_py, nightly._git = fake_run, fake_git
        nightly.publish_paths = lambda day, data_dir=None: []
        try:
            nightly.publish("2026-11-06")
            raised = None
        except RuntimeError as e:
            raised = str(e)
    finally:
        nightly._run_py, nightly._git, nightly.publish_paths = old_run, old_git, old_paths
    check("拒绝提交", raised is not None and "README.md" in raised, str(raised))


def test_calibrated_gate():
    print("\n[推送闸门] 只有带分P对齐的日期才允许发布")
    old_md, old_load = nightly.server._session_match_date, nightly.server.load_matches
    nightly.server._session_match_date = lambda d: d
    cases = [
        ({}, False, "没有列表"),
        ({"2026-11-06": [{"title": "DK vs BLG", "bv": "BV_X"}]}, False, "没分P"),
        ({"2026-11-06": [{"title": "DK vs BLG", "bv": "", "pages": {"1": {}}}]},
         False, "没BV"),
        ({"2026-11-06": [{"title": "DK vs BLG", "bv": "BV_X", "pages": {}}]},
         False, "分P为空"),
        ({"2026-11-06": [{"title": "DK vs BLG", "bv": "BV_X", "pages": {"1": {}}}]},
         True, "有BV有分P"),
    ]
    try:
        for lists, want, why in cases:
            nightly.server.load_matches = lambda: {"lists": lists}
            got = nightly.calibrated("2026-11-06")
            check(f"{why} -> {'推送' if want else '不推送'}", got == want, str(lists))
    finally:
        nightly.server._session_match_date = old_md
        nightly.server.load_matches = old_load


def test_tick_skips_uncalibrated():
    print("\n[推送闸门] 未校准的日期不发布, 但要继续发现+校准")
    calls, discovered = [], []

    async def fake_discover(day):
        discovered.append(day)

    async def fake_cal(day, session):
        return 0

    old = (nightly.running_days, nightly.dirty_days, nightly._too_old,
           nightly.discover, nightly.calibrate_day, nightly.calibrated,
           nightly.publish, nightly._auto_push)
    # 11-06 还在采集; 11-05 已收尾但数据没提交(脏), 也必须继续重试发现
    nightly.running_days = lambda: ["2026-11-06"]
    nightly.dirty_days = lambda: ["2026-11-05"]
    nightly._too_old = lambda day: False       # 这些是回归样本日期, 不随运行日期过期
    nightly.discover = fake_discover
    nightly.calibrate_day = fake_cal
    nightly.publish = lambda day, dry_run=False: calls.append(day)
    nightly._auto_push = lambda dry_run: None
    try:
        nightly.calibrated = lambda day: False
        asyncio.run(nightly.tick(False))
        check("未校准 -> 不发布", calls == [], str(calls))
        check("已收尾但未发布的日期也继续发现",
              discovered == ["2026-11-05", "2026-11-06"], str(discovered))
        nightly.calibrated = lambda day: True
        asyncio.run(nightly.tick(False))
        check("已校准 -> 发布", calls == ["2026-11-05", "2026-11-06"], str(calls))
        # --date 是"只处理这一天", 不能顺手把别的脏日期也校准/发布
        discovered.clear()
        calls.clear()
        nightly.calibrated = lambda day: False
        asyncio.run(nightly.tick(False, only_date="2026-11-06"))
        check("--date 只发现指定日期",
              discovered == ["2026-11-06"], str(discovered))
        check("--date 不发布别的日期", calls == [], str(calls))
    finally:
        (nightly.running_days, nightly.dirty_days, nightly._too_old,
         nightly.discover, nightly.calibrate_day, nightly.calibrated,
         nightly.publish, nightly._auto_push) = old


def test_date_run_reports_uncalibrated():
    print("\n[单轮] --date 未校准时用退出码告诉控制台")
    import argparse
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="dmk_nl4_"))
    old_lock = nightly.pipelinelock.TICK_LOCK
    old_tick, old_cal = nightly.tick, nightly.calibrated

    async def fake_tick(dry_run, only_date=""):
        return None

    nightly.pipelinelock.TICK_LOCK = tmp / "pipeline.tick.lock"
    nightly.tick = fake_tick
    args = argparse.Namespace(date="2026-11-06", dry_run=False)
    try:
        nightly.calibrated = lambda day: False
        rc = nightly._run_once(args)
        check("未校准返回 4", rc == 4, str(rc))
        nightly.calibrated = lambda day: True
        check("已校准返回 0", nightly._run_once(args) == 0)

        async def failing_tick(dry_run, only_date=""):
            return 1

        nightly.tick = failing_tick
        rc = nightly._run_once(args)
        check("发布失败返回 5", rc == 5, str(rc))
        nightly.tick = fake_tick

        nightly.calibrated = lambda day: False
        args.dry_run = True
        check("dry-run 不报失败", nightly._run_once(args) == 0)
    finally:
        nightly.pipelinelock.TICK_LOCK = old_lock
        nightly.tick, nightly.calibrated = old_tick, old_cal
        shutil.rmtree(tmp, ignore_errors=True)


def test_valid_day():
    check("合法日期", nightly._valid_day("2026-10-31"))
    check("拒绝 _test", not nightly._valid_day("_test"))


def main() -> int:
    print("夜间流水线回归")
    test_running_days_and_publish_paths()
    test_discover_uses_fallback()
    test_discover_keeps_fallback_bv()
    test_discover_fills_bv_from_date_search()
    test_discover_merges_missing_match()
    test_discover_without_bv()
    test_retry_window()
    test_tick_retry_window()
    test_publish_collects_align_after_split()
    test_publish_skips_empty_index()
    test_publish_refuses_non_data_staged()
    test_calibrated_gate()
    test_tick_skips_uncalibrated()
    test_date_run_reports_uncalibrated()
    test_loop_mode_single_instance()
    test_tick_lock_conflict()
    test_valid_day()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
