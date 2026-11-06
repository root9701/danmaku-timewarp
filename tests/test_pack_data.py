# -*- coding: utf-8 -*-
"""pack_data.py 回归: 发布前校验对"采集中空快照"的放行。

用法: python tests/test_pack_data.py     (退出码 0 = 全过)

背景(实测缺陷): 会话刚开始、第一片还没滚完时, 采集端会写一份 files=[] 的
manifest 快照。旧校验把它当错误, 于是当天**所有**发布整轮被拒 ——
2026-11-04 06:39、2026-11-05 06:36 都空转了一轮才等到整点滚片。
放行必须只针对"采集中"的空快照, 收尾后的空会话仍然要拦。

不碰真实数据: 全部在临时目录里造会话。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pack_data                                           # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def _sess(base: Path, mf: dict, files: tuple = ()) -> Path:
    sess = base / "2026-11-05" / "7734200"
    sess.mkdir(parents=True, exist_ok=True)
    for name in files:
        (sess / name).write_text("x", encoding="utf-8")
    (sess / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
    return sess


def test_live_empty_snapshot():
    print("\n[放行] 采集中的空快照")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        _sess(base, {"schema_version": 1, "ended_by": "running",
                     "files": [], "dm_count": 0})
        got = pack_data.check_session("2026-11-05", base / "2026-11-05" / "7734200")
        check("采集中 + 空 files + dm_count=0 -> 放行", got == [], str(got))


def test_still_reject():
    print("\n[拦截] 收尾后的空会话仍然要拦")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        _sess(base, {"schema_version": 1, "ended_by": "live_end",
                     "files": [], "dm_count": 0})
        got = pack_data.check_session("2026-11-05", base / "2026-11-05" / "7734200")
        check("收尾后 + 空 files -> 拦", any("files 为空" in p for p in got), str(got))
        check("收尾后 + dm_count=0 -> 拦", any("dm_count" in p for p in got), str(got))


def test_normal_session():
    print("\n[正常] 有片的会话照旧通过")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        _sess(base, {"schema_version": 1, "ended_by": "running",
                     "files": ["7734200_s0600.jsonl.gz"], "dm_count": 24701},
              files=("7734200_s0600.jsonl.gz",))
        got = pack_data.check_session("2026-11-05", base / "2026-11-05" / "7734200")
        check("采集中有片 -> 放行", got == [], str(got))


def test_missing_slice():
    print("\n[拦截] manifest 指向的切片必须存在")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        _sess(base, {"schema_version": 1, "ended_by": "running",
                     "files": ["7734200_s0600.jsonl.gz"], "dm_count": 100})
        got = pack_data.check_session("2026-11-05", base / "2026-11-05" / "7734200")
        check("切片缺失 -> 拦", any("切片缺失" in p for p in got), str(got))


def test_missing_manifest():
    print("\n[拦截] 缺 manifest 仍要拦")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        sess = base / "2026-11-05" / "7734200"
        sess.mkdir(parents=True)
        got = pack_data.check_session("2026-11-05", sess)
        check("缺 manifest -> 拦", any("缺 manifest" in p for p in got), str(got))


def test_index_mode_also_checks_align():
    print("\n[发布] --index 也必须校验 align(不能只查会话)")
    with tempfile.TemporaryDirectory(prefix="dmk_pd_") as td:
        base = Path(td)
        data = base / "data"
        (data / "2026-11-06").mkdir(parents=True)
        # 有分P 对齐但 BV 不在 lists: 重算时拿不到赛程时间窗
        (data / "2026-11-06" / "align.json").write_text(json.dumps({
            "align": {"2026-11-06|7734200|BV1BAD": {
                "bv": "BV1BAD", "pages": {"1": {"offset_sec": 1.0, "duration": 2}}}},
            "lists": {},
        }), encoding="utf-8")
        old_data, old_argv, old_write = pack_data.DATA, sys.argv, pack_data.write_index
        wrote = []
        pack_data.DATA = data
        pack_data.write_index = lambda staged=False: wrote.append(staged) or 0
        sys.argv = ["pack_data.py", "--index"]
        try:
            rc = pack_data.main()
        finally:
            pack_data.DATA, sys.argv, pack_data.write_index = old_data, old_argv, old_write
        check("拒绝生成清单", rc == 1, str(rc))
        check("没有写 data-index.json", wrote == [], str(wrote))


def main() -> int:
    print("pack_data 校验回归")
    test_live_empty_snapshot()
    test_still_reject()
    test_normal_session()
    test_missing_slice()
    test_missing_manifest()
    test_index_mode_also_checks_align()
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
