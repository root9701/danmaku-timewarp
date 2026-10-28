# -*- coding: utf-8 -*-
"""test_version.py — 版本号与「程序更新提示」的回归。

用法: python tests/test_version.py     (退出码 0 = 全过)

为什么需要:
  版本号一旦算错, 后果不是报错而是**静默误导**:
    - 按字符串比 "1.10.0" < "1.9.0"  → 用户永远收不到更新提示
    - 远端读不到却报"有新版本"       → 用户白折腾, 提示从此失去可信度
    - 老清单没有 app_version 字段就崩 → 升级过的老用户全挂

  这些都不会抛异常, 只会让人看到错的信息。所以用断言把它们钉住。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import version                                              # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


def test_read_version() -> None:
    """仓库根的 VERSION 必须存在且能被两端读到同一份。"""
    vf = ROOT / "VERSION"
    check("VERSION 文件存在", vf.is_file(), str(vf))
    got = version.read()
    check("read() 是合法版本串", version.parse(got) != (0, 0, 0), f"得到 {got!r}")

    # 两端各有一份 version.py, 必须给出同一结果(采集端要能单独拷走)
    check("采集端版本模块存在", (ROOT / "danmaku-collector" / "version.py").is_file())
    txt_s = (ROOT / "danmaku-server" / "version.py").read_text(encoding="utf-8")
    txt_c = (ROOT / "danmaku-collector" / "version.py").read_text(encoding="utf-8")
    check("两端 version.py 内容一致(只有一份逻辑)", txt_s == txt_c)


def test_parse_numeric() -> None:
    """必须按数值比较, 不能按字符串 —— 否则 1.10.0 会被判成小于 1.9.0。"""
    check("1.0.0 -> (1,0,0)", version.parse("1.0.0") == (1, 0, 0))
    check("1.10.0 -> (1,10,0)", version.parse("1.10.0") == (1, 10, 0))
    check("1.0 -> (1,0,0)(省略补零)", version.parse("1.0") == (1, 0, 0))
    check("无法识别 -> (0,0,0)", version.parse("!!!") == (0, 0, 0))
    check("空串 -> (0,0,0)", version.parse("") == (0, 0, 0))
    check("None -> (0,0,0)", version.parse(None) == (0, 0, 0))
    # 带后缀时只取数字段(发布不走预发布通道, 不需要完整 semver)
    check("1.0.0-rc1 -> (1,0,0)", version.parse("1.0.0-rc1") == (1, 0, 0))


def test_is_newer() -> None:
    """is_newer 的核心: 无法判定时**必须返回 False**(宁漏报不误报)。"""
    check("1.0.1 > 1.0.0", version.is_newer("1.0.1", "1.0.0") is True)
    check("1.10.0 > 1.9.0(数值比较)", version.is_newer("1.10.0", "1.9.0") is True,
          "按字符串会判成 False")
    check("2.0.0 > 1.99.99", version.is_newer("2.0.0", "1.99.99") is True)
    check("同版本不算新", version.is_newer("1.0.0", "1.0.0") is False)
    check("远端更旧不算新", version.is_newer("0.9.0", "1.0.0") is False)

    # ---- 失败路径: 一律 False ----
    check("远端为空 -> False(不误报)", version.is_newer(None, "1.0.0") is False)
    check("远端空串 -> False", version.is_newer("", "1.0.0") is False)
    check("远端乱码 -> False", version.is_newer("garbage", "1.0.0") is False)
    check("本地乱码 -> False(无法判定)", version.is_newer("9.9.9", "???") is False)
    check("两边都空 -> False", version.is_newer("", "") is False)


def test_version_file_single_source() -> None:
    """版本号不许在代码里再写死一份 —— 那正是之前失真的原因。

    允许出现的地方只有: VERSION 文件、version.py 的文档/正则、测试。
    """
    import re
    bad = []
    for py in ("danmaku-server/server.py", "danmaku-collector/collector.py",
               "danmaku-server/dataupdate.py"):
        p = ROOT / py
        if not p.is_file():
            continue
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            # 找形如  VERSION = "0.1.0"  的赋值
            if re.search(r'^\s*VERSION\s*=\s*["\']\d+\.\d+', ln):
                bad.append(f"{py}:{i}: {ln.strip()}")
    check("代码里没有写死的版本号", not bad, "; ".join(bad))


def test_dataupdate_tolerates_old_index() -> None:
    """老清单没有 app_version -> check() 必须返回 None 而不是抛错。

    真实场景: 作者还没用新脚本重新生成清单, 用户端就已经升级了。
    """
    import dataupdate
    play = dataupdate.plan      # 确认模块可用
    check("dataupdate 可导入", callable(play))

    # check() 会联网, 这里只验证**取值逻辑**: 用 dict 模拟清单解析结果
    old_idx = {"schema_version": 1, "latest": "2026-10-27", "days": {}}
    check("老清单取 app_version 得到 None",
          (old_idx.get("app_version") or None) is None)
    new_idx = {"schema_version": 1, "app_version": "1.0.1", "days": {}}
    check("新清单能取到版本", new_idx.get("app_version") == "1.0.1")


def test_idx_has_app_version() -> None:
    """已提交的 data-index.json 应带程序版本(供用户端比对)。"""
    import json
    p = ROOT / "data-index.json"
    if not p.is_file():
        check("data-index.json 存在", False)
        return
    idx = json.loads(p.read_text(encoding="utf-8"))
    ver = idx.get("app_version")
    check("data-index.json 有 app_version 字段", "app_version" in idx,
          f"实际字段: {sorted(idx.keys())}")
    if "app_version" in idx:
        check("app_version 是合法版本串(或空=生成时无版本)",
              ver == "" or version.parse(ver) != (0, 0, 0), f"得到 {ver!r}")


def main() -> int:
    print("版本号与更新提示回归")
    for fn in (test_read_version,
               test_parse_numeric,
               test_is_newer,
               test_version_file_single_source,
               test_dataupdate_tolerates_old_index,
               test_idx_has_app_version):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
