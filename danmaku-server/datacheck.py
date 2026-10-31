"""数据完整性自检: 下载完成后按 manifest 校验文件齐全且 gzip 可解。

从 dataupdate.py 拆出: 这里只读不写, 与"下到哪"的更新编排分开, 各自可测。
"""
import gzip
import json
from pathlib import Path


def manifests_for_day(day_dir: Path) -> list:
    """一天里全部 manifest 的路径(按路径排序, 保证结果稳定)。

    为什么不是"找到第一个就用": 一天可能有多个会话目录 —— 同一天硬杀重启、
    或多次 finalize, 都会在 data/{day}/{room}/ 下各留一份 manifest。只验第一个
    等于其余会话**完全没自检**, 而用户看到的仍是"校验通过", 比不校验更误导。
    """
    if not day_dir.is_dir():
        return []
    root = day_dir / "manifest.json"
    if root.is_file():                 # 旧平铺布局: 日期目录根上一份
        return [root]
    return sorted(p for p in day_dir.glob("*/manifest.json") if p.is_file())


def verify_day(day_dir: Path) -> list:
    """校验一天的数据完整性(每个会话的 manifest 声明的文件都在且能解开)。

    只读检查, 不修改任何东西。给"更新后自检"用 —— 下载成功不等于数据能用,
    gzip 截断只有真去解才知道。

    **多会话日逐个验**: 见 manifests_for_day 的说明。
    """
    mfs = manifests_for_day(day_dir)
    if not mfs:
        return [f"{day_dir.name}: 没有 manifest.json"]
    problems = []
    for mf in mfs:
        problems.extend(_verify_manifest_files(day_dir, mf))
    return problems


def _verify_manifest_files(day_dir: Path, mf: Path) -> list:
    """校验单份 manifest 声明的文件齐全且 gzip 可解。"""
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 带上会话目录名, 多会话日才分得清是哪一份坏了
        return [f"{day_dir.name}/{mf.parent.name}: manifest 解析失败 {e}"]
    base = mf.parent
    tag = day_dir.name if base == day_dir else f"{day_dir.name}/{base.name}"
    problems = []
    for name in man.get("files") or []:
        p = base / name
        if not p.is_file():
            problems.append(f"{tag}: 缺文件 {name}")
            continue
        if name.endswith(".gz"):
            try:
                with gzip.open(p, "rb") as f:
                    while f.read(1 << 20):
                        pass
            except (OSError, EOFError) as e:
                problems.append(f"{tag}: {name} 损坏 {e}")
    return problems
