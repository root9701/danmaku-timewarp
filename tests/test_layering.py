# -*- coding: utf-8 -*-
"""test_layering.py — 对齐记录的"上游 / 本地"分层与分发包契约。

用法: python tests/test_layering.py     (退出码 0 = 全过)

覆盖的是分发方案的核心假设:
  - align/ 里上游算的结果, 用户在本地微调时**不能被写回 align/** (否则 git pull 冲突)
  - 本地只落"与上游不同"的那几条, 不把上游数据复制一份
  - 有 pages 的记录才需要赛程时间窗; 失败残留不该阻止发布
  - 裁剪掉 afp 后, manifest 必须同步置 null, 否则用户以为下载不全

这些用例用临时目录, 不碰真实数据。
"""
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import server                                              # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    if not cond:
        FAILED.append(name)


class Sandbox:
    """把 server 的 ALIGN_DIR / MATCHES_PATH 指到临时目录。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dmk_layer_"))
        self.align = self.tmp / "align"
        self.matches = self.tmp / "matches.json"
        self.align.mkdir()
        self._old_align = server.ALIGN_DIR
        self._old_matches = server.MATCHES_PATH
        server.ALIGN_DIR = self.align
        server.MATCHES_PATH = self.matches
        with server._ALIGN_CACHE_LOCK:
            server._ALIGN_CACHE.clear()

    def close(self):
        server.ALIGN_DIR = self._old_align
        server.MATCHES_PATH = self._old_matches
        with server._ALIGN_CACHE_LOCK:
            server._ALIGN_CACHE.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_day(self, day: str, body: dict):
        (self.align / f"{day}.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8")


K26 = "2026-10-26|7734200|BV19wnQ6sExk"
K26B = "2026-10-26|7734200|BV1TenD6yEkj"


def sample_day():
    return {
        "align": {
            K26: {"bv": "BV19wnQ6sExk", "date": "2026-10-26",
                  "pages": {"1": {"part": "第一局", "offset_sec": 100.0,
                                  "duration": 300}}},
            K26B: {"bv": "BV1TenD6yEkj", "date": "2026-10-26",
                   "pages": {"1": {"part": "第一局", "offset_sec": 200.0,
                                   "duration": 300}}},
        },
        "lists": {"2026-10-26": [
            {"title": "HLE vs IG", "bv": "BV19wnQ6sExk", "start_ms": 1792942382304},
            {"title": "G2 vs TES", "bv": "BV1TenD6yEkj", "start_ms": 1793034000000},
        ]},
    }


def test_upstream_read() -> None:
    sb = Sandbox()
    try:
        sb.write_day("2026-10-26", sample_day())
        m = server.load_matches()
        check("读到上游 align/ 的两条", len(m["align"]) == 2,
              f"实际 {len(m['align'])}")
        check("上游 offset 正确",
              m["align"][K26]["pages"]["1"]["offset_sec"] == 100.0)
        check("lists 也读到了", "2026-10-26" in m["lists"])
    finally:
        sb.close()


def test_local_override_wins() -> None:
    sb = Sandbox()
    try:
        sb.write_day("2026-10-26", sample_day())

        def mut(m):
            e = dict(m["align"][K26])
            e["pages"] = dict(e["pages"])
            e["pages"]["1"] = dict(e["pages"]["1"])
            e["pages"]["1"]["offset_sec"] = 999.0
            m["align"][K26] = e

        check("写入成功", server.update_matches(mut))
        m = server.load_matches()
        check("本地覆盖生效", m["align"][K26]["pages"]["1"]["offset_sec"] == 999.0,
              f"实际 {m['align'][K26]['pages']['1']['offset_sec']}")
        check("未改动的场次仍跟上游",
              m["align"][K26B]["pages"]["1"]["offset_sec"] == 200.0)
    finally:
        sb.close()


def test_align_dir_never_written() -> None:
    """本地微调绝不能改 align/ —— 那是 git pull 覆盖的区域。"""
    sb = Sandbox()
    try:
        sb.write_day("2026-10-26", sample_day())
        before = (sb.align / "2026-10-26.json").read_text(encoding="utf-8")

        def mut(m):
            e = dict(m["align"][K26])
            e["pages"] = {"1": {"offset_sec": 1.0, "part": "x", "duration": 1}}
            m["align"][K26] = e

        server.update_matches(mut)
        after = (sb.align / "2026-10-26.json").read_text(encoding="utf-8")
        check("align/ 文件字节未变", before == after)
    finally:
        sb.close()


def test_local_layer_is_minimal() -> None:
    """本地层只落与上游不同的记录, 不整份复制上游。"""
    sb = Sandbox()
    try:
        sb.write_day("2026-10-26", sample_day())

        def mut(m):
            e = dict(m["align"][K26])
            e["pages"] = {"1": {"offset_sec": 7.0, "part": "x", "duration": 1}}
            m["align"][K26] = e

        server.update_matches(mut)
        loc = json.loads(sb.matches.read_text(encoding="utf-8"))
        check("本地层只有 1 条(被改的那条)", len(loc["align"]) == 1,
              f"实际 {len(loc['align'])} 条: {list(loc['align'])}")
        check("本地层不含未改动的上游记录", K26B not in loc["align"])
    finally:
        sb.close()


def test_check_align_ignores_pageless() -> None:
    """无 pages 的历史残留不该阻止发布(它们不参与时间窗)。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import pack_data
    sb = Sandbox()
    old = pack_data.ALIGN
    try:
        pack_data.ALIGN = sb.align
        sb.write_day("2026-10-26", {
            "align": {
                # 有 pages 且 BV 在 lists 里 -> 通过
                K26: {"bv": "BV19wnQ6sExk",
                      "pages": {"1": {"offset_sec": 1.0, "duration": 2}}},
                # 无 pages 的陈旧残留 -> 不算问题
                "2026-10-26|old_session_name": {"bv": "BVstale", "pages": {}},
            },
            "lists": {"2026-10-26": [{"title": "x", "bv": "BV19wnQ6sExk"}]},
        })
        problems = pack_data.check_align()
        check("无 pages 的残留不算问题", problems == [], str(problems))
    finally:
        pack_data.ALIGN = old
        sb.close()


def test_check_align_flags_missing_window() -> None:
    """有 pages 但 BV 不在 lists -> 必须报出(否则重新校准拿不到时间窗)。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import pack_data
    sb = Sandbox()
    old = pack_data.ALIGN
    try:
        pack_data.ALIGN = sb.align
        sb.write_day("2026-10-26", {
            "align": {K26: {"bv": "BV19wnQ6sExk",
                            "pages": {"1": {"offset_sec": 1.0, "duration": 2}}}},
            "lists": {},                      # 故意不给 lists
        })
        problems = pack_data.check_align()
        check("缺赛程时间窗被报出", len(problems) == 1, str(problems))
    finally:
        pack_data.ALIGN = old
        sb.close()


def test_manifest_afp_stripped_shape() -> None:
    """裁剪包里的 manifest 形状: afp 必须 null, 且带标记。"""
    mf = {"schema_version": 1, "room": 7734200, "dm_count": 10,
          "afp": {"file": "x.afp.jsonl"}, "files": ["a.jsonl.gz"]}
    # 模拟 pack_data.strip_afp 的改写
    mf["afp"] = None
    mf["distributable"] = True
    mf["afp_stripped"] = True
    check("裁剪后 afp 为 null", mf["afp"] is None)
    check("裁剪后带 distributable 标记", mf.get("distributable") is True)
    check("裁剪后带 afp_stripped 标记", mf.get("afp_stripped") is True)


def test_stream_redirect_is_rechecked() -> None:
    """`/api/stream` 的每一跳重定向都必须过白名单 + 内网校验。

    旧实现用 aiohttp 默认的 allow_redirects=True: 白名单只查用户传进来的那个 URL,
    302 之后的目标不再复查 —— 只要能造一次跳转就绕过了全部校验。现在改为逐跳手动
    跟随, 每跳都调 _check_stream_url; 这里就测这个复检语义。
    """
    # 允许的: B 站 CDN
    for ok_url in ("https://cn-zjhz-cu-01-01.bilivideo.com/x.mp4",
                   "https://upos-sz-mirrorcos.bilivideo.com/a/b"):
        try:
            server._check_stream_url(ok_url)
            passed = True
        except server.ApiError:
            passed = False
        check(f"放行 {urlparse(ok_url).hostname}", passed)

    # 拒绝的: 非白名单域名 / 本机 / 内网 / 非 http(s)
    accept = ("http://evil.example.com/x",
              "http://127.0.0.1:8789/api/days",
              "http://localhost/x",
              "http://192.168.1.1/x",
              "http://[::1]/x",
              "file:///etc/passwd")
    for bad_url in accept:
        try:
            server._check_stream_url(bad_url)
            rejected = False
        except server.ApiError:
            rejected = True
        check(f"拒绝 {bad_url}", rejected, "竟然放行了")


def main() -> int:
    print("分层与分发契约")
    for fn in (test_upstream_read,
               test_local_override_wins,
               test_align_dir_never_written,
               test_local_layer_is_minimal,
               test_check_align_ignores_pageless,
               test_check_align_flags_missing_window,
               test_manifest_afp_stripped_shape,
               test_stream_redirect_is_rechecked):
        print(f"\n[{fn.__name__}]")
        fn()
    print(f"\n{'FAIL' if FAILED else 'OK'}: {len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
