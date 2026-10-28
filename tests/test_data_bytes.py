# -*- coding: utf-8 -*-
"""换行与清单哈希的回归: 为什么数据必须按字节原样存取。

这个 bug 真实的代价:
  `core.autocrlf=true`(Windows 上很常见)会在 checkout 时把 LF 换成 CRLF。
  data/ 下的 .jsonl / .afp.jsonl 被换过之后, 工作区字节 ≠ 仓库字节 ——
  作者按工作区算 sha256 写进清单, 用户从 GitHub/镜像下到的是仓库里的 LF 版本,
  哈希永远对不上, **所有用户都下不了数据**。

  实测数据(修复前): afp 本地 7316313 字节, 仓库 7315759 字节, 差 554 = CRLF 行数。
  平时完全看不出来, 因为读 JSON 行时尾部的 \\r 会被忽略。

这里守住三件事:
  1. .gitattributes 对 data/** 声明了 -text(禁止任何换行转换)
  2. 工作区 data/ 里每个文本文件的字节 == git 里的字节
  3. 清单里的 sha256 == 从磁盘算出来的 sha256(作者视角的正确性)
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + str(detail)}")
    if not cond:
        FAILED.append(name)


def git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT),
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def main() -> int:
    print("换行与清单哈希回归")

    print("\n[.gitattributes 规则]")
    ga = ROOT / ".gitattributes"
    check(".gitattributes 存在", ga.is_file())
    text = ga.read_text(encoding="utf-8") if ga.is_file() else ""
    check("data/** 声明为 -text", "data/** -text" in text,
          "缺这行会让 core.autocrlf 改坏数据")
    check("没有给 data/** 声明 text",
          "data/** text" not in text.replace("data/** -text", ""),
          "-text 与 text 不能同时用于 data/**")
    # git 的实际判定要问 git 自己, 不能只信文件里写了什么
    attr = git("check-attr", "text", "data/2026-10-26/7734200/7734200.afp.jsonl")
    if attr:
        check("git 认定 data 文件不做换行转换", attr.endswith(": unset"), attr)
    else:
        check("git 认定 data 文件不做换行转换", False, "git check-attr 无输出")

    print("\n[工作区字节 == git 字节]")
    skips = {".gitkeep"}
    files = [p for p in sorted((ROOT / "data").rglob("*"))
             if p.is_file() and p.name not in skips]
    check("data/ 下有文件可查", bool(files), "没有任何会话数据")
    diff = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        size_git = git("cat-file", "-s", "HEAD:" + rel)
        if not size_git:
            continue                     # 未入库的新数据, 不在这里判
        if int(size_git) != p.stat().st_size:
            diff.append(f"{rel}: 本地 {p.stat().st_size} / git {size_git}")
    check(f"{len(files)} 个数据文件字节一致", not diff, diff[:3])

    print("\n[清单哈希与磁盘一致]")
    idx_p = ROOT / "data-index.json"
    check("data-index.json 存在", idx_p.is_file())
    if idx_p.is_file():
        idx = json.loads(idx_p.read_text(encoding="utf-8"))
        bad = []
        n = 0
        for day, entry in (idx.get("days") or {}).items():
            for f in entry.get("file_sizes") or []:
                name = f.get("name") or ""
                sha = (f.get("sha256") or "").strip().lower()
                if not sha:
                    bad.append(f"{name}: 缺 sha256")
                    continue
                p = ROOT / name
                if not p.is_file():
                    bad.append(f"{name}: 文件不存在")
                    continue
                n += 1
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                if h != sha:
                    bad.append(f"{name}: 清单 {sha[:12]}… / 实际 {h[:12]}…")
        check("每个文件都带 sha256", n > 0, n)
        check("清单哈希全部与磁盘一致", not bad, bad[:3])

    print(f"\n{'FAIL: ' + str(len(FAILED)) + ' 项失败' if FAILED else 'OK: 全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
