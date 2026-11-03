# -*- coding: utf-8 -*-
"""夜间流水线回归: 采集日期识别与发布文件选择。

不碰真实数据, 全部在临时目录里跑; 不跑网络与 git 提交。
"""
import json
import sys
import tempfile
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

        mf["ended_by"] = "live_end"
        write_text(sess / "manifest.json", json.dumps(mf))
        paths = [p.name for p in nightly.publish_paths("2026-10-31", base)]
        check("收尾后补 afp/meta/raw",
              {"7734200.afp.jsonl", "7734200.meta.json", "7734200_s0000.jsonl"} <= set(paths),
              str(paths))


def test_valid_day():
    check("合法日期", nightly._valid_day("2026-10-31"))
    check("拒绝 _test", not nightly._valid_day("_test"))


def main() -> int:
    print("夜间流水线回归")
    test_running_days_and_publish_paths()
    test_valid_day()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
