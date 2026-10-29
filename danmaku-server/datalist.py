# -*- coding: utf-8 -*-
"""datalist.py — 解析数据清单, 决定"该下哪些文件"。

为什么单独一层:
  「选哪些文件」是纯函数: 输入清单 + 本地现状, 输出待下载列表, 不碰网络也不写盘。
  「怎么下、下到哪」在 dataupdate(镜像降级、原子落盘、并发)。混在一起时,
  选文件逻辑淹没在 IO 代码里, 想单独验证"关掉 afp 时少选了哪些"就得起 HTTP 服务。

本模块**不做任何 IO**, 也不 import dataupdate —— 依赖方向单向。
"""
from pathlib import Path

# 音频指纹文件的后缀(主文件与 _runN 归档都是这两种)。
# 它只服务于 /api/autocalign, 那条链路要 numpy 与 ffmpeg; 便携包两者都不带,
# 下下来只会被当成"不是弹幕"过滤掉 —— 而它占了数据体积的一半以上。
# 要不要下由调用方决定(见 dataupdate.download_day 的 want_afp)。
AFP_SUFFIXES = (".afp.jsonl", ".afp.jsonl.gz")


def is_afp(rel: str) -> bool:
    """清单路径是否为音频指纹文件。"""
    return str(rel).endswith(AFP_SUFFIXES)


def day_bytes(entry: dict) -> tuple:
    """该日清单的 (全部字节数, 其中 afp 的字节数)。

    按 file_sizes 求和而不是用 entry["bytes"]: 后者把 afp 也算进去了,
    而"不下 afp"时给用户的预估必须减掉它, 否则会报一个下不到的大小。
    """
    sizes = entry.get("file_sizes") or []
    if not sizes:
        return int(entry.get("bytes") or 0), 0
    total = afp = 0
    for f in sizes:
        n = int(f.get("bytes") or 0)
        total += n
        if is_afp(f.get("name") or ""):
            afp += n
    return total, afp


def only_afp(entry: dict) -> dict:
    """只保留 afp 条目的清单子集(用于给已有日期补齐指纹)。"""
    return {
        "files": [r for r in (entry.get("files") or []) if is_afp(r)],
        "file_sizes": [f for f in (entry.get("file_sizes") or [])
                       if is_afp(f.get("name") or "")],
    }


def plan(index: dict, have: dict) -> list:
    """算出要整日下载的日期。返回 [(日期, 条目)] 升序。"""
    todo = []
    for day in sorted(index.get("days") or {}):
        if day in have:
            continue
        todo.append((day, index["days"][day]))
    return todo


def afp_fix_days(index: dict, have: dict, root: Path, want_afp: bool) -> list:
    """已有日期里**缺 afp** 的那些。返回 [(日期, 仅含 afp 的条目)] 升序。

    存在的意义: 用户先关着 afp 下载过几天, 之后改成开启。plan() 只看
    "manifest 在不在", 这些日期会被永久跳过 —— 指纹再也补不回来, 除非删掉
    整天重下。这里单独找出来, 让开关是**可逆**的。

    root 是清单路径的基准目录(清单里写的是 data/... 相对仓库根)。
    """
    if not want_afp:
        return []
    out = []
    for day in sorted(index.get("days") or {}):
        if day not in have:
            continue                       # 整日还没下, plan() 会处理
        sub = only_afp(index["days"][day])
        if any(not (root / r).is_file() for r in (sub.get("files") or [])):
            out.append((day, sub))
    return out


def file_meta(entry: dict, rel: str) -> tuple:
    """从清单里取该文件的 (字节数, sha256)。缺失的字段返回 None。

    为什么要容忍缺失: 老清单(没有 sha256 的那版)在用户机器上可能还留着。
    读老清单不能报错, 退化到"只校验大小"——比不校验好, 也比直接拒绝更新好。
    """
    want = sha = None
    for f in entry.get("file_sizes") or []:
        if f.get("name") == rel:
            want = int(f.get("bytes") or 0)
            sha = (f.get("sha256") or "").strip().lower() or None
            break
    return want, sha


def safe_rel(rel: str) -> str:
    """校验清单里的相对路径, 防路径穿越。非法时抛 ValueError。

    不在这里抛 UpdateError: 那是 dataupdate 的异常类型, 本模块不该知道它。
    """
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"清单里有非法路径: {rel}")
    if p.parts[0] != "data":
        raise ValueError(f"清单路径必须以 data/ 开头: {rel}")
    return rel
