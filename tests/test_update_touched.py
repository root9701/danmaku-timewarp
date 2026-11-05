# -*- coding: utf-8 -*-
"""「拉取弹幕」后前端自动刷新的回归: update() 必须报出"动过哪些日期"。

用法: python tests/test_update_touched.py     (退出码 0 = 全过)

背景(用户实测): 点「拉取弹幕」只补对齐或只补指纹时, 界面提示"日期列表已刷新",
但场次列表还是旧的, 必须自己按 F5。根因在 dataupdate.update(): 旧实现只在
`i <= len(todo)` 时把日期记进 updated, 于是"没新增整天"的补丁下载全部被漏报,
前端 `if (j.updated.length) reloadDays()` 判据为假。

这里用纯内存的假下载器把四类补充都跑一遍, 只断言"报出的日期集合",
不碰网络、不碰真实 data/。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import dataupdate                                          # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


class Fake:
    """替掉 update() 里的四类判定与下载, 只验证"记账"逻辑。"""

    def __init__(self, todo=(), fix=(), redo_day=(), redo=()):
        self.plan_ = [(d, {"files": [], "sessions": []}) for d in todo]
        self.fix_ = [(d, {"files": []}) for d in fix]
        self.day_ = [(d, {"files": []}) for d in redo_day]
        self.redo_ = [(d, {"files": []}) for d in redo]
        self.downloaded = []

    def __enter__(self):
        self._saved = (dataupdate.fetch_index, dataupdate.local_days,
                       dataupdate.plan, dataupdate.afp_fix_days,
                       dataupdate.align_fix_days, dataupdate.day_fix_days,
                       dataupdate.download_day)
        dataupdate.fetch_index = lambda: {"days": {}, "latest": "2026-11-05"}
        dataupdate.local_days = lambda d: {}
        dataupdate.plan = lambda idx, have: self.plan_
        dataupdate.afp_fix_days = lambda idx, have, dd, want: self.fix_
        dataupdate.align_fix_days = lambda idx, have, dd: self.redo_
        dataupdate.day_fix_days = lambda idx, have, dd: self.day_
        dataupdate.download_day = self._download
        return self

    def _download(self, day, entry, data_dir, cb, want_afp=True):
        self.downloaded.append(day)
        return 1024

    def __exit__(self, *a):
        (dataupdate.fetch_index, dataupdate.local_days,
         dataupdate.plan, dataupdate.afp_fix_days,
         dataupdate.align_fix_days, dataupdate.day_fix_days,
         dataupdate.download_day) = self._saved


def run(**kw):
    with Fake(**kw) as f:
        res = dataupdate.update(Path("."))
    return res, f.downloaded


def test_only_align():
    print("\n[只补对齐] 必须报出 touched")
    res, dl = run(redo=["2026-11-04"])
    check("确实下载了那一天", dl == ["2026-11-04"], str(dl))
    check("updated 为空(不是新日期)", res["updated"] == [], str(res["updated"]))
    check("touched 列出该日期", res.get("touched") == ["2026-11-04"], str(res.get("touched")))
    check("消息说明是校准结果", "校准" in res["message"], res["message"])


def test_only_afp():
    print("\n[只补指纹] 必须报出 touched")
    res, dl = run(fix=["2026-11-01", "2026-10-31"])
    check("下载两天", len(dl) == 2, str(dl))
    check("updated 为空", res["updated"] == [], str(res["updated"]))
    check("touched 两天", res.get("touched") == ["2026-11-01", "2026-10-31"],
          str(res.get("touched")))


def test_day_fix():
    print("\n[补当天增量] 必须报出 touched")
    res, dl = run(redo_day=["2026-11-05"])
    check("updated 为空(这天本来就在)", res["updated"] == [], str(res["updated"]))
    check("touched 列出该日期", res.get("touched") == ["2026-11-05"],
          str(res.get("touched")))


def test_mixed_dedupe():
    print("\n[混合] 同一天既补指纹又补对齐, 只报一次")
    res, dl = run(todo=["2026-11-05"], fix=["2026-11-05", "2026-11-01"],
                  redo=["2026-11-05"])
    check("updated 只有新日期", res["updated"] == ["2026-11-05"], str(res["updated"]))
    check("touched 去重保序",
          res.get("touched") == ["2026-11-05", "2026-11-01"], str(res.get("touched")))


def test_nothing():
    print("\n[无变化] touched 为空, 前端不该刷新")
    res, dl = run()
    check("什么都没下", dl == [], str(dl))
    check("message 是已是最新", res["message"] == "已是最新", res["message"])
    check("touched 为空", res.get("touched") == [], str(res.get("touched")))


def main() -> int:
    print("拉取后自动刷新回归")
    test_only_align()
    test_only_afp()
    test_day_fix()
    test_mixed_dedupe()
    test_nothing()
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
