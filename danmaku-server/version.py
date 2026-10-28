# -*- coding: utf-8 -*-
"""version.py — 程序版本的唯一来源。

为什么要有这个模块:
  之前版本号散落各处(collector.py 里写死 "0.1.0", 实际数据里却是 "migrated"/"merged"),
  用户也完全看不到自己装的是哪一版。没有版本号就没法回答"我是不是旧版" ——
  而"不知道自己旧, 所以永远不更新"正是这个项目最主要的可用性问题。

唯一源:
  仓库根的 `VERSION` 文件(单行, 形如 `1.0.0`)。**不要在代码里再写死版本号**,
  一律从这里读。改版本只改那一个文件。

为什么读文件而不是 import 一个常量:
  采集端要能单独拷到手机 Termux 跑, 播放端只读采集端的数据目录 —— 两端不许互相
  import。本模块是**两端共用的一份拷贝**(各目录一份, 内容相同), 而版本号属于整个
  仓库, 所以放在根目录由两边各自向上查找。这样只有一处需要改。

读不到怎么办:
  返回 "0.0.0" 而不是抛错 —— 版本号缺失不该让服务起不来。但要留日志, 否则
  用户以为自己是 0.0.0 版、点了更新也永远是"有新版本"。
"""
import re
from pathlib import Path

UNKNOWN = "0.0.0"

# 只认 `数字.数字.数字`, 允许 `1.0` 这种省略(补成 1.0.0)。带后缀(如 1.0.0-rc1)
# 时保留原串用于显示, 但比较只用数字段 —— 发布版不走预发布通道, 不需要完整 semver。
_VER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _version_file() -> Path:
    """从本文件所在目录向上找仓库根的 VERSION。"""
    here = Path(__file__).resolve().parent
    for d in (here, here.parent, here.parent.parent):
        p = d / "VERSION"
        if p.is_file():
            return p
    return Path()


def read() -> str:
    """当前程序版本, 形如 "1.0.0"。读不到返回 UNKNOWN。"""
    p = _version_file()
    if not p.is_file():
        return UNKNOWN
    try:
        txt = p.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    if not txt:
        return UNKNOWN
    return txt.splitlines()[0].strip() or UNKNOWN


def parse(v) -> tuple:
    """版本串 -> (major, minor, patch)。无法识别返回 (0, 0, 0)。

    必须是**数值**比较: 按字符串比 "1.10.0" < "1.9.0", 会让用户跳过 10 个小版本。
    """
    m = _VER_RE.match(str(v or "").strip())
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())  # type: ignore[return-value]


def is_newer(remote, local) -> bool:
    """远端版本是否严格新于本地。**无法判定时一律返回 False**。

    为什么失败要返回 False(而不是 True): 拉不到远端版本、或远端格式不认识时,
    报"有新版本"会让用户白折腾一遍更新流程, 而且下次还会再报。宁可漏报一次
    (用户下次点还能发现), 不要误报(会让"有更新"这个提示失去可信度)。
    """
    r, l = parse(remote), parse(local)
    if r == (0, 0, 0) or l == (0, 0, 0):
        return False
    return r > l
