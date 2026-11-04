# -*- coding: utf-8 -*-
"""test_manifest.py — 采集端 manifest 的权威性回归。

用法: python tests/test_manifest.py     (退出码 0 = 全过)

覆盖的是 2026-10-28 体检发现的一个静默数据问题:
  同一个会话目录(data/{赛事日}/{room}/)里, 同一天硬杀重启会产生**第二次** finalize。
  meta 和 afp 都做了 _runN 归档, 唯独 manifest.json 直接被 tmp.replace 顶掉 ——
  第一段会话录了多少条、哪几个文件, 从此查不到。数据文件本身没丢(服务端还能目录扫描),
  但 manifest 是**权威索引**, 它失真等于账本失真。

这里用真实 finalize 路径验证:
  1. 第二次会话不会让 manifest 丢掉第一次会话的文件与计数
  2. 旧 manifest 被归档而不是覆盖
  3. 整夜未开播(没有任何数据片)不写 manifest

不碰真实数据, 全部在临时目录里跑。
"""
import asyncio
import gzip
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-collector"))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def make_recorder(sess_dir: Path, slice_name: str, start_ms: int, n_dm: int):
    """构造一个"已经录完一段"的 Recorder 骨架, 只保留 finalize 需要的字段。

    不直接调 Recorder.__init__(它要房间号/config 等), 手工铺属性更稳:
    finalize 只读这几个字段, 行为与真实路径一致。
    """
    import collector

    r = collector.Recorder.__new__(collector.Recorder)
    r.room_id = 7734200
    r.title = "TEST"
    r.is_test = False
    r.meta_name = None
    r._parts = []
    r._afp_path = None
    r._afp_enabled = False
    r._afp_task = None
    r._snapshot_task = None
    r.afp = None
    r.file = None
    r._dir_day = sess_dir.parent.name        # 锁死赛事日, 免得依赖当前时间
    r.file_path = sess_dir / slice_name
    r.record_start = None
    r.record_end = None
    r.first_ts = start_ms
    r.last_ts = start_ms + n_dm
    r.dm_count = n_dm
    r.sc_count = 0
    r.reconnect_count = 0
    r.gaps = []
    r.stream_events = []
    r.watched = 0
    r.kw_events = []
    r.title_events = []
    r.write_err_count = 0
    r.reason = {"value": "latest_end"}

    async def _noop():
        return None

    r._close_ws = _noop
    return r


def write_slices(sess_dir: Path, spec):
    """按 spec 写真实 jsonl.gz 数据片。spec = [(文件名, 条数, 起始 ts)]"""
    sess_dir.mkdir(parents=True, exist_ok=True)
    for name, n, ts0 in spec:
        p = sess_dir / name
        opener = gzip.open if name.endswith(".gz") else open
        with opener(p, "wt", encoding="utf-8", newline="\n") as f:
            for i in range(n):
                f.write(json.dumps({"ts": ts0 + i, "ty": "dm", "t": "x"}, ensure_ascii=False) + "\n")


def run_finalize(r):
    asyncio.run(r.finalize())


def test_second_session_keeps_first() -> None:
    """同目录第二次 finalize 不得丢掉第一次会话的文件与计数。"""
    with tempfile.TemporaryDirectory(prefix="dmk_mf_") as td:
        day = Path(td) / "2026-10-26"
        sess = day / "7734200"

        # 第一次会话: 两个切片, 共 60 条
        write_slices(sess, [("7734200_s2300.jsonl.gz", 30, 1_792_942_000_000),
                            ("7734200_s0000.jsonl.gz", 30, 1_792_943_000_000)])
        r1 = make_recorder(sess, "7734200_s0000.jsonl", 1_792_942_000_000, 60)
        run_finalize(r1)

        mf1 = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
        check("第一次 manifest 记录 2 个切片", len(mf1["files"]) == 2, str(mf1["files"]))
        check("第一次 manifest 计数 60", mf1["dm_count"] == 60, str(mf1["dm_count"]))

        # 第二次会话(硬杀重启后): 只多了一个新切片, 20 条
        write_slices(sess, [("7734200_s0900.jsonl.gz", 20, 1_792_950_000_000)])
        r2 = make_recorder(sess, "7734200_s0900.jsonl", 1_792_950_000_000, 20)
        run_finalize(r2)

        mf2 = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
        check("第二次 manifest 保留第一次的切片(3 个)", len(mf2["files"]) == 3,
              str(mf2["files"]))
        check("第二次 manifest 计数是整目录 80(不是 20)", mf2["dm_count"] == 80,
              str(mf2["dm_count"]))
        check("第二次 manifest 起始时间取整目录最早",
              mf2["start_ms"] == 1_792_942_000_000, str(mf2["start_ms"]))
        check("第二次 manifest 结束时间取整目录最晚",
              mf2["end_ms"] == 1_792_950_000_019, str(mf2["end_ms"]))

        # 旧 manifest 必须被归档, 不是消失
        archived = sorted(p.name for p in sess.glob("manifest_run*.json"))
        check("旧 manifest 被归档而非覆盖", len(archived) >= 1, str(archived))
        if archived:
            old = json.loads((sess / archived[0]).read_text(encoding="utf-8"))
            check("归档里的 manifest 是第一次会话的账(dm_count=60)",
                  old.get("dm_count") == 60, str(old.get("dm_count")))


def test_no_data_no_manifest() -> None:
    """整夜未开播(没有任何数据片)不得写出 manifest.garbage。"""
    with tempfile.TemporaryDirectory(prefix="dmk_mf_") as td:
        day = Path(td) / "2026-10-27"
        sess = day / "7734200"
        sess.mkdir(parents=True)
        r = make_recorder(sess, "7734200_s0000.jsonl", 0, 0)
        r.file_path = None          # 从未开片
        run_finalize(r)
        got = sorted(p.name for p in sess.iterdir())
        check("无数据时不写 manifest", "manifest.json" not in got, str(got))
        check("无数据时不写 meta", not any(n.endswith(".meta.json") for n in got), str(got))


def test_afp_not_counted_as_danmaku() -> None:
    """afp / meta 文件不得混进 manifest.files, 也不得算进 dm_count。"""
    with tempfile.TemporaryDirectory(prefix="dmk_mf_") as td:
        sess = Path(td) / "2026-10-26" / "7734200"
        write_slices(sess, [("7734200_s2300.jsonl.gz", 10, 1_792_942_000_000)])
        # 放一份 afp: 里面有 ts 字段, 但 ty != dm
        afp_lines = [
            {"ty": "hdr", "v": 1, "sr": 16000, "hop": 0.4, "win": 0.4, "bands": 33},
            {"ty": "conn", "c": 0, "t0_ms": 1_792_942_000_000},
        ]
        (sess / "7734200.afp.jsonl").write_text(
            "\n".join(json.dumps(o) for o in afp_lines) + "\n", encoding="utf-8")

        r = make_recorder(sess, "7734200_s2300.jsonl", 1_792_942_000_000, 10)
        run_finalize(r)
        mf = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
        check("afp 不进 manifest.files",
              all(".afp." not in f for f in mf["files"]), str(mf["files"]))
        check("afp 的 ts 不算进 dm_count", mf["dm_count"] == 10, str(mf["dm_count"]))


def test_live_snapshot_manifest() -> None:
    """采集期间的 manifest 快照: 只含已封口片, afp/meta 置空, 收尾时不归档。"""
    with tempfile.TemporaryDirectory(prefix="dmk_mf_") as td:
        sess = Path(td) / "2026-10-31" / "7734200"
        write_slices(sess, [("7734200_s2300.jsonl.gz", 10, 1_792_942_000_000)])
        # 正在写的 raw 片: 还没封口, 不得进快照(否则用户端会校验缺文件)
        write_slices(sess, [("7734200_s0000.jsonl", 5, 1_792_943_000_000)])
        r = make_recorder(sess, "7734200_s0000.jsonl", 1_792_942_000_000, 15)
        r._write_live_manifest(sess)

        mf = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
        check("快照只列已封口 gz", mf["files"] == ["7734200_s2300.jsonl.gz"],
              str(mf["files"]))
        check("快照计数只算已封口片", mf["dm_count"] == 10, str(mf["dm_count"]))
        check("快照不指向 afp", mf["afp"] is None, str(mf["afp"]))
        check("快照不指向 meta", mf["meta"] is None, str(mf["meta"]))
        check("快照标记仍在采集", mf["ended_by"] == "running", str(mf["ended_by"]))

        run_finalize(r)             # 收尾: 覆盖快照, 不应把它归档成 manifest_runN
        mf2 = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
        check("收尾后的 manifest 不是快照", mf2["ended_by"] != "running", str(mf2["ended_by"]))
        archived = sorted(p.name for p in sess.glob("manifest_run*.json"))
        check("快照不产生归档文件", not archived, str(archived))


def test_initial_snapshot_without_slices() -> None:
    """会话开始、一片未滚时也要有 manifest: 否则夜间发布校验整轮被拒
    (实测 2026-11-04 06:39 轮, 06:00 开录后第一小时内无快照)。"""
    with tempfile.TemporaryDirectory(prefix="dmk_mf0_") as td:
        sess = Path(td) / "2026-11-05" / "7734200"
        r = make_recorder(sess, "7734200_s0600.jsonl", 1_793_000_000_000, 0)
        r.start_snapshot_now(sess)

        mf_path = sess / "manifest.json"
        check("开录即有 manifest", mf_path.is_file())
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
        check("初始快照 files 为空", mf["files"] == [], str(mf["files"]))
        check("初始快照标记 running", mf["ended_by"] == "running", str(mf["ended_by"]))
        check("初始快照计数 0", mf["dm_count"] == 0, str(mf["dm_count"]))
        check("初始快照不指向 afp", mf["afp"] is None, str(mf["afp"]))

        # 滚出第一片后, 快照能正常覆盖初始版
        write_slices(sess, [("7734200_s0600.jsonl.gz", 10, 1_793_000_000_000)])
        r._write_live_manifest(sess)
        mf2 = json.loads(mf_path.read_text(encoding="utf-8"))
        check("滚片后快照更新", mf2["files"] == ["7734200_s0600.jsonl.gz"],
              str(mf2["files"]))


def main() -> int:
    print("manifest 权威性回归")
    for fn in (test_second_session_keeps_first,
               test_no_data_no_manifest,
               test_afp_not_counted_as_danmaku,
               test_live_snapshot_manifest,
               test_initial_snapshot_without_slices):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
