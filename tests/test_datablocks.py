# -*- coding: utf-8 -*-
"""datablocks.py 回归: 小时块的 采集中/待提交/待推送/推送失败/已推送。

用临时 git 仓库 + 裸远端模拟, 不碰真实 data/。
"""
import gzip
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-collector"))

import datablocks  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def git(cwd: Path, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def write_slice(path: Path, n: int) -> None:
    lines = [json.dumps({"ts": i, "t": f"dm{i}", "ty": "dm"})
             for i in range(n)]
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    else:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_repo(td: Path) -> Path:
    remote = td / "origin.git"
    repo = td / "repo"
    remote.mkdir()
    git(remote, "init", "--bare", "-b", "main")
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    day = repo / "data" / "2026-11-04" / "7734200"
    day.mkdir(parents=True)
    write_slice(day / "7734200_s0600.jsonl.gz", 10)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "data 0600")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    # 第二片只提交本地, 不推远端
    write_slice(day / "7734200_s0700.jsonl.gz", 20)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "data 0700")
    # 正在写的 raw 片: 未提交
    write_slice(day / "7734200_s0800.jsonl", 3)
    return repo


def test_states() -> None:
    print("\n[状态] 采集中 / 待推送 / 已推送 / 推送失败 / 待提交")
    with tempfile.TemporaryDirectory(prefix="dmk_blocks_") as td:
        repo = make_repo(Path(td))
        blocks = datablocks.scan(repo, "2026-11-04", [7734200],
                                 running=True)
        check("三块", len(blocks) == 3, str([b["name"] for b in blocks]))
        if len(blocks) == 3:
            check("已推送", blocks[0]["state"] == "已推送", str(blocks[0]))
            check("已推送计数", blocks[0]["count"] == 10, str(blocks[0]))
            check("已推送有提交号", bool(blocks[0]["commit"]), str(blocks[0]))
            check("本地已提交未推", blocks[1]["state"] == "待推送", str(blocks[1]))
            check("待推送计数", blocks[1]["count"] == 20, str(blocks[1]))
            check("正在写 raw", blocks[2]["state"] == "采集中", str(blocks[2]))
        failed = datablocks.scan(repo, "2026-11-04", [7734200],
                                 running=True, push_failed=True)
        check("推送失败标记", failed[1]["state"] == "推送失败", str(failed[1]))
        stopped = datablocks.scan(repo, "2026-11-04", [7734200],
                                  running=False)
        check("停止后 raw 变待提交",
              stopped[2]["state"] == "待提交", str(stopped[2]))


def test_parse() -> None:
    print("\n[解析] 切片名与时间")
    check("gz 切片", datablocks.parse_slice("7734200_s0600.jsonl.gz")
          == {"start_min": 360, "sealed": True})
    check("raw 切片", datablocks.parse_slice("7734200_s2330.jsonl")
          == {"start_min": 1410, "sealed": False})
    check("非切片", datablocks.parse_slice("manifest.json") is None)
    check("跨零点格式", datablocks.fmt_min(1410 + 60) == "00:30")


def main() -> int:
    print("datablocks 回归")
    test_parse()
    test_states()
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
