# -*- coding: utf-8 -*-
"""pushctl 回归: 设置收敛、只推数据、手动/自动开关。

不碰网络: 推送目标是一个临时裸仓。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pushctl                                            # noqa: E402

FAILED = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({why})" if why and not ok else ""))
    if not ok:
        FAILED.append(name)


def _commit(work: Path, msg: str, paths) -> None:
    subprocess.run(["git", "add", *paths], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=work,
                   check=True, capture_output=True)


def _repo(td: str):
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
    _commit(work, "init", ["."])
    subprocess.run(["git", "remote", "add", "origin", str(remote)],
                   cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"],
                   cwd=work, check=True, capture_output=True)
    return work, remote


def _data_commit(work: Path, day: str = "2026-11-01") -> None:
    d = work / "data" / day
    d.mkdir(parents=True)
    (d / "align.json").write_text("{}", encoding="utf-8")
    _commit(work, f"data({day}): 夜间增量发布", ["data"])


def test_settings():
    with tempfile.TemporaryDirectory(prefix="dmk_pc_") as td:
        root = Path(td)
        d = pushctl.load_settings(root)
        check("默认自动推送开", d["auto"] is True, str(d))
        check("默认重试 3 次", d["retries"] == 3, str(d))

        saved = pushctl.save_settings(
            {"auto": "off", "retries": 99, "delay_sec": 0}, root)
        check("字符串 off 识别为关", saved["auto"] is False, str(saved))
        check("重试夹到 5", saved["retries"] == 5, str(saved))
        check("间隔夹到 5", saved["delay_sec"] == 5, str(saved))
        check("设置落盘", pushctl.settings_path(root).is_file())
        check("读回一致", pushctl.load_settings(root) == saved)

        pushctl.settings_path(root).write_text("{bad", encoding="utf-8")
        check("坏 JSON 回默认", pushctl.load_settings(root)["auto"] is True)


def test_log():
    with tempfile.TemporaryDirectory(prefix="dmk_pc_") as td:
        root = Path(td)
        check("无日志返回空", pushctl.log_line(root) == "")
        pushctl.log_event("开始推送 2 个提交（自动）", root)
        pushctl.log_event("已推送 2 个数据提交", root)
        last = pushctl.log_line(root)
        check("最近一条可读", "已推送 2 个数据提交" in last, last)
        check("日志带时间戳", last[:4].isdigit(), last)

        pushctl.log_event("推送失败（手动）", root, detail="fatal: unable to access\nConnection reset")
        text = pushctl.log_path(root).read_text(encoding="utf-8")
        check("细节缩进落盘", "    Connection reset" in text, text)

        for i in range(pushctl.LOG_MAX + 20):
            pushctl.log_event(f"filler {i}", root)
        n = len(pushctl.log_path(root).read_text(encoding="utf-8").splitlines())
        check("日志不超过环形上限", n <= pushctl.LOG_MAX + 5, n)


def test_push_boundary():
    with tempfile.TemporaryDirectory(prefix="dmk_pc_") as td:
        work, remote = _repo(td)
        _data_commit(work)

        s = pushctl.status(work)
        check("数据提交可推", s["pending"] == 1 and s["data_only"] and s["can_push"], str(s))
        r = pushctl.push(work, manual=False)
        check("自动推送数据提交", r["ok"] and r["reason"] == "pushed", str(r))
        check("推完没有待推", pushctl.status(work)["pending"] == 0)

        (work / "README.md").write_text("y", encoding="utf-8")
        _commit(work, "docs", ["README.md"])
        s = pushctl.status(work)
        check("文档提交标记不可推",
              s["pending"] == 1 and not s["data_only"] and not s["can_push"], str(s))
        r = pushctl.push(work, manual=True)
        check("手动也不推非数据", not r["ok"] and r["reason"] == "non-data", str(r))

        out = subprocess.run(["git", "log", "--oneline", "main"], cwd=remote,
                             capture_output=True, text=True,
                             encoding="utf-8").stdout
        check("远端没有文档提交", "docs" not in out, out)


def test_pending_details():
    with tempfile.TemporaryDirectory(prefix="dmk_pc_") as td:
        work, _remote = _repo(td)
        _data_commit(work)
        (work / "README.md").write_text("y", encoding="utf-8")
        _commit(work, "docs", ["README.md"])
        s = pushctl.status(work)
        details = s.get("pending_commits") or []
        check("待推明细 2 条", len(details) == 2, str(details))
        check("明细区分 data/code",
              {d["kind"] for d in details} == {"data", "code"}, str(details))
        check("data/code 计数",
              s.get("data_pending") == 1 and s.get("code_pending") == 1, str(s))
        check("remote_head 有值", bool(s.get("remote_head")), str(s))


def test_auto_switch():
    with tempfile.TemporaryDirectory(prefix="dmk_pc_") as td:
        work, _remote = _repo(td)
        pushctl.save_settings({"auto": False}, work)
        _data_commit(work)

        r = pushctl.push(work, manual=False)
        check("关闭自动推送时不推", not r["ok"] and r["reason"] == "auto-off", str(r))
        check("自动关闭时仍待推", pushctl.status(work)["pending"] == 1)

        r = pushctl.push(work, manual=True)
        check("手动按钮忽略自动开关", r["ok"] and r["reason"] == "pushed", str(r))


def main() -> int:
    print("pushctl 回归")
    test_settings()
    test_log()
    test_push_boundary()
    test_pending_details()
    test_auto_switch()
    if FAILED:
        print(f"FAIL: {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("OK: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
