# -*- coding: utf-8 -*-
"""datapaths.py — 某一天要入库的文件清单。

只读目录、只判断"哪些文件已经封口可以提交", 不写任何文件。
采集中的会话只收 manifest 与 manifest.files 登记的切片; 收尾后再补 afp/meta/raw。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SERVER_DIR = str(ROOT / "danmaku-server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import server  # noqa: E402

DATA = server.DATA_DIR


def publish_paths(day: str, data_dir=None) -> list:
    """该日要 `git add` 的文件。

    采集中的会话只加 manifest 与已封口 gz; afp/meta 还在写或还没生成, 不收。
    收尾后的会话再补 afp/meta/raw(此时文件已关闭, 字节不再变)。
    """
    base = data_dir or DATA
    day_dir = base / day
    paths = []
    align = day_dir / "align.json"
    if align.is_file():
        paths.append(align)
    if not day_dir.is_dir():
        return paths
    for sess in sorted(day_dir.iterdir()):
        if not sess.is_dir() or sess.name.startswith("_"):
            continue
        mf = sess / "manifest.json"
        if not mf.is_file():
            continue
        try:
            mf_data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mf_data = {}
        paths.append(mf)
        # 以 manifest 的 files 为准: 只发布登记过的切片, 避免把上一场遗留的
        # *_runN.afp.jsonl.gz 或半截临时文件带出去。老 manifest 没有 files 时退回 glob。
        sealed = mf_data.get("files")
        if not (isinstance(sealed, list) and sealed):
            sealed = sorted(p.name for p in sess.glob("*.jsonl.gz"))
        for name in sealed:
            if isinstance(name, str) and "/" not in name and "\\" not in name:
                p = sess / name
                if p.is_file():
                    paths.append(p)
        if mf_data.get("ended_by") == "running":
            continue
        paths.extend(sorted(p for p in sess.glob("*.jsonl") if ".afp." not in p.name))
        paths.extend(sorted(sess.glob("*.meta.json")))
        paths.extend(sorted(p for p in sess.glob("*.afp.jsonl*") if p.is_file()))
    return paths
