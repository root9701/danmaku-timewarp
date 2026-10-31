# -*- coding: utf-8 -*-
"""无头校准 CLI 回归: 选择逻辑、会话目录识别、dry-run 不写盘。"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "danmaku-server"))

import calibrate                                            # noqa: E402
import server                                               # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def test_targets_from_lists():
    lists = {
        "2026-10-31": [
            {"title": "LOS vs TLAW", "bv": "BV1W2x66WEE9"},
            {"title": "G2 vs HLE", "bv": "BV11Fx66REUr"},
            {"title": "未找到回放", "bv": ""},
        ],
        "2026-11-01": [{"title": "别的日子", "bv": "BV1xx"}],
    }
    all_t = calibrate.targets_from_lists(lists, "2026-10-31")
    check("当天两条有 BV 的场次被选中", len(all_t) == 2, f"得到 {len(all_t)}")
    check("空 BV 条目被跳过", all(t["bv"] for t in all_t))
    one = calibrate.targets_from_lists(lists, "2026-10-31", "BV11Fx66REUr")
    check("--bv 只留一场", len(one) == 1 and one[0]["bv"] == "BV11Fx66REUr")
    check("日期不存在返回空", calibrate.targets_from_lists(lists, "2026-10-30") == [])


def test_session_files():
    with tempfile.TemporaryDirectory() as td:
        day = Path(td) / "2026-10-31"
        (day / "7734200").mkdir(parents=True)
        (day / "7734200" / "7734200.afp.jsonl").write_text("", encoding="utf-8")
        (day / "noafp").mkdir()
        (day / "_test").mkdir()
        (day / "_test" / "x.afp.jsonl").write_text("", encoding="utf-8")
        got = calibrate.session_files("2026-10-31", Path(td))
        check("只认带 afp 的正式会话", got == ["7734200"], f"得到 {got}")


def test_dry_run_writes_nothing():
    before = (server.MATCHES_PATH.exists(),
              server.MATCHES_PATH.stat().st_mtime_ns if server.MATCHES_PATH.exists() else None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = __import__("asyncio").run(calibrate.run(
            "2026-10-31", [{"bv": "BV1W2x66WEE9", "title": "LOS vs TLAW"}],
            ["7734200"], 0, True))
    after = (server.MATCHES_PATH.exists(),
             server.MATCHES_PATH.stat().st_mtime_ns if server.MATCHES_PATH.exists() else None)
    check("dry-run 退出码 0", rc == 0, f"得到 {rc}")
    check("dry-run 不写本地层", before == after, f"{before} -> {after}")
    check("dry-run 打出计划", "[dry-run]" in buf.getvalue())


def main() -> int:
    print("无头校准 CLI 回归")
    test_targets_from_lists()
    test_session_files()
    test_dry_run_writes_nothing()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
