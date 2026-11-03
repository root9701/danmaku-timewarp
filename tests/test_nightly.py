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


def test_push_boundary():
    """自动推送只覆盖纯数据提交。全部用假 git, 不碰网络与真实仓库。"""
    real_git, real_push = nightly._git, nightly.gitpush.push
    pushed = []

    def fake_git(branch="main", commits=("aaa",), paths=("data/2026-10-31/align.json",)):
        def _f(*args):
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return branch + "\n"
            if args[0] == "rev-list" and args[1].endswith("..HEAD"):
                return "".join(c + "\n" for c in commits)
            if args[:2] == ("rev-list", "--merges"):
                return ""
            if args[0] == "show":
                return "\n".join(paths) + "\n"
            raise AssertionError(f"未预期的 git 调用: {args}")
        return _f

    def fake_push(remote, branch, **kw):
        pushed.append((remote, branch))
        return True, ""

    nightly.gitpush.push = fake_push
    try:
        nightly._git = fake_git()
        check("纯数据提交自动推送",
              nightly.push_pending() is True and pushed == [("origin", "main")],
              str(pushed))

        pushed.clear()
        nightly._git = fake_git(commits=())
        check("无待推提交不推", nightly.push_pending() is True and not pushed, str(pushed))

        pushed.clear()
        nightly._git = fake_git(branch="feature")
        check("非 main 分支不推", nightly.push_pending() is False and not pushed, str(pushed))

        pushed.clear()
        nightly._git = fake_git(paths=("README.md",))
        check("混入非数据改动不推", nightly.push_pending() is False and not pushed, str(pushed))

        pushed.clear()
        nightly._git = fake_git()
        nightly.gitpush.push = lambda *a, **k: (False, "Connection reset")
        check("推送失败返回 False", nightly.push_pending() is False)
    finally:
        nightly._git = real_git
        nightly.gitpush.push = real_push


def test_push_integration():
    """用临时裸仓走一遍真实 git push, 验证"数据推、文档不推"。不碰网络。"""
    import subprocess
    with tempfile.TemporaryDirectory(prefix="dmk_push_") as td:
        base = Path(td)
        remote, work = base / "remote.git", base / "work"
        subprocess.run(["git", "init", "--bare", str(remote)],
                       check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(work)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(["git", "config", k, v], cwd=work,
                           check=True, capture_output=True)
        (work / "README.md").write_text("x", encoding="utf-8")
        for args in (["git", "add", "."], ["git", "commit", "-m", "init"]):
            subprocess.run(args, cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)],
                       cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"],
                       cwd=work, check=True, capture_output=True)

        real_root = nightly.ROOT
        nightly.ROOT = work
        try:
            data = work / "data" / "2026-11-01"
            data.mkdir(parents=True)
            (data / "align.json").write_text("{}", encoding="utf-8")
            for args in (["git", "add", "data"],
                         ["git", "commit", "-m", "data(2026-11-01): 夜间增量发布"]):
                subprocess.run(args, cwd=work, check=True, capture_output=True)
            check("纯数据提交推到本地裸仓", nightly.push_pending() is True)

            (work / "README.md").write_text("y", encoding="utf-8")
            for args in (["git", "add", "README.md"],
                         ["git", "commit", "-m", "docs"]):
                subprocess.run(args, cwd=work, check=True, capture_output=True)
            check("混入文档的提交不推", nightly.push_pending() is False)

            out = subprocess.run(["git", "log", "--oneline", "main"], cwd=remote,
                                 capture_output=True, text=True,
                                 encoding="utf-8").stdout
            check("远端只收到数据提交", "docs" not in out, out)
        finally:
            nightly.ROOT = real_root


def main() -> int:
    print("夜间流水线回归")
    test_running_days_and_publish_paths()
    test_valid_day()
    test_push_boundary()
    test_push_integration()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
