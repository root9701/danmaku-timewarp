# -*- coding: utf-8 -*-
"""数据增量更新的回归: 清单比对、下载落盘、幂等、防穿越、截断检测。

为什么不测真实 GitHub:
  那是外部依赖, 断网/限流都会让回归变红, 而红的原因与代码无关。
  这里用本地 HTTP 服务冒充 raw 源, 造小体量假数据 —— 逻辑完全一样,
  但跑得快且确定。

覆盖的失败模式(每条都对应一个真实会踩的坑):
  - 路径拼接错 -> 文件落到错误位置, 用户看到"下好了但打不开"
  - 非原子写入 -> 断网留下半个 gzip, 下次启动读到损坏数据
  - 不幂等     -> 每次点更新都重下 21MB
  - 不校验大小 -> 截断的响应被当成成功, gzip 解到一半才炸
  - 只校验大小 -> **等长篡改**(第三方镜像的典型风险)被放过去, 坏数据静默落盘
  - 路径穿越   -> 清单被篡改时写到 data/ 之外
  - 单通道写死 -> 官方 raw 在国内被限速时, 下载功能对多数用户等于坏的
"""
import gzip
import hashlib
import http.server
import json
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "danmaku-server"))

import dataupdate  # noqa: E402
import srcfetch  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + str(detail)}")
    if not cond:
        FAILED.append(name)


def make_fake_remote(tmp: Path) -> tuple:
    """造两天假数据 + 清单。文件内容是真 gzip, 才能验"下载后能解开"。"""
    remote = tmp / "remote"
    days = {}
    for day, n in (("2026-01-01", 3), ("2026-01-02", 2)):
        files, sizes, total = [], [], 0
        for i in range(n):
            rel = f"data/{day}/123/x{i}.jsonl.gz"
            p = remote / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            # 可压缩的重复内容, 让 gzip 真的有意义
            body = gzip.compress((f"{day}-{i}\n" * 500).encode())
            p.write_bytes(body)
            files.append(rel)
            sizes.append({"name": rel, "bytes": len(body),
                          "sha256": hashlib.sha256(body).hexdigest()})
            total += len(body)
        mf_rel = f"data/{day}/123/manifest.json"
        mf = remote / mf_rel
        man = {"schema_version": 1, "files": [f"x{i}.jsonl.gz" for i in range(n)]}
        mf.write_bytes(json.dumps(man).encode())
        files.append(mf_rel)
        sizes.append({"name": mf_rel, "bytes": mf.stat().st_size,
                      "sha256": hashlib.sha256(mf.read_bytes()).hexdigest()})
        total += mf.stat().st_size
        days[day] = {"files": files, "file_sizes": sizes, "bytes": total}
    idx = {"schema_version": 1, "latest": max(days), "days": days}
    (remote / "data-index.json").write_text(
        json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return remote, idx


def serve(directory: Path):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    print("数据增量更新回归")
    tmp = Path(tempfile.mkdtemp(prefix="upd_"))
    saved = dataupdate.RAW_BASE
    httpd = None
    try:
        remote, idx = make_fake_remote(tmp)
        real_files = sum(len(v["files"]) for v in idx["days"].values())
        real_bytes = sum(v["bytes"] for v in idx["days"].values())

        httpd, port = serve(remote)
        dataupdate.RAW_BASE = f"http://127.0.0.1:{port}"

        local = tmp / "local" / "data"
        local.mkdir(parents=True)

        print("\n[清单比对]")
        info = dataupdate.check(local)
        check("空目录识别出全部天数", info["need"] == len(idx["days"]), info["need"])
        check("字节数等于远端合计", info["new_bytes"] == real_bytes,
              f"{info['new_bytes']} vs {real_bytes}")

        print("\n[下载落盘]")
        res = dataupdate.update(local)
        check("天数正确", sorted(res["updated"]) == sorted(idx["days"]),
              res["updated"])
        got = [p for p in (tmp / "local").rglob("*") if p.is_file()]
        check("文件数一致", len(got) == real_files, f"{len(got)} vs {real_files}")
        check("字节数一致", sum(p.stat().st_size for p in got) == real_bytes)

        print("\n[幂等]")
        check("再 check 已最新", dataupdate.check(local)["need"] == 0)
        check("再 update 不重下", dataupdate.update(local).get("message") == "已是最新")

        print("\n[完整性]")
        problems = []
        for day in idx["days"]:
            problems.extend(dataupdate.verify_day(local / day))
        check("下载的 gzip 全部可解", not problems, problems[:3])
        # 反向验证: 故意截断一个 gzip, verify_day 必须报出来
        day0_dir = local / sorted(idx["days"])[0] / "123"
        victim = next(iter(sorted(day0_dir.glob("*.gz"))), None)
        check("找到用于截断的样本", victim is not None, day0_dir)
        if victim is not None:
            raw = victim.read_bytes()
            victim.write_bytes(raw[: len(raw) // 2])
            bad = dataupdate.verify_day(local / sorted(idx["days"])[0])
            check("截断的 gzip 被检出", bool(bad), "verify_day 没报损坏")

        print("\n[防路径穿越]")
        for bad_rel in ("../x", "/etc/passwd", "align/x.json", "data/../../x",
                        "src/x.py"):
            try:
                dataupdate._safe_rel(bad_rel)
                ok = False
            except dataupdate.UpdateError:
                ok = True
            check(f"拒绝 {bad_rel}", ok)

        print("\n[完整性校验]")
        day0 = sorted(idx["days"])[0]
        entry = json.loads(json.dumps(idx["days"][day0]))   # 深拷贝

        # 1) 清单字节数**过时**但内容完整 -> 应当放过。
        #    这是线上真实发生过的情况: .gitattributes 缺 data/** -text 时,
        #    清单按被 CRLF 污染的工作区算出 7316313, 而仓库里实际 7315759。
        #    若按字节数硬拒, 每个通道都失败 -> 下载功能彻底坏掉。
        entry_stale = json.loads(json.dumps(entry))
        entry_stale["file_sizes"][0]["bytes"] += 999
        stale_rel = entry_stale["files"][0]
        (tmp / "local" / stale_rel).unlink(missing_ok=True)
        try:
            dataupdate.download_day(day0, entry_stale, local)
            check("清单字节数过时但内容完整 -> 放过", True)
        except dataupdate.UpdateError as e:
            check("清单字节数过时但内容完整 -> 放过", False, str(e)[:70])

        # 2) 内容真损坏(截断)且没有 sha256 -> 必须拒绝。
        #    此时只有长度和内容自校验两道防线, 不能把半个文件写进 data/。
        entry_nosha = json.loads(json.dumps(entry))
        for f in entry_nosha["file_sizes"]:
            f.pop("sha256", None)
        entry_nosha["file_sizes"][0]["bytes"] = 999999      # 故意给个不可能的期望
        (tmp / "local" / stale_rel).unlink(missing_ok=True)
        # 让远端这个文件变成截断的 gzip, 模拟"下载到一半断了"
        broken = remote / stale_rel
        broken_good = broken.read_bytes()
        broken.write_bytes(broken_good[: len(broken_good) // 2])
        try:
            dataupdate.download_day(day0, entry_nosha, local)
            check("无 sha256 时截断内容被拒绝", False, "竟然接受了坏数据")
        except dataupdate.UpdateError as e:
            check("无 sha256 时截断内容被拒绝",
                  "损坏" in str(e) or "校验" in str(e), str(e)[:70])
        finally:
            broken.write_bytes(broken_good)                 # 还原远端样本

        # 2) **等长篡改**: 改一个字节, 大小完全不变 -> 只有 sha256 能抓到。
        #    这是第三方镜像最典型的风险(缓存了被污染的副本), 而字节数校验对它无效。
        dataupdate.download_day(day0, entry, local)   # 先把上一步删掉的文件补回
        victim_rel = entry["files"][0]
        victim = tmp / "local" / victim_rel
        want_bytes = entry["file_sizes"][0]["bytes"]
        victim.write_bytes(b"X" * want_bytes)         # 等长但内容错
        check("等长篡改样本已构造", victim.stat().st_size == want_bytes)
        n = dataupdate.download_day(day0, entry, local)       # 应重下正确版本
        check("等长篡改被检出并重下", dataupdate._sha256_file(victim)
              == entry["file_sizes"][0]["sha256"],
              "文件哈希仍不符")
        check("重下确实写了数据", n > 0, n)

        # 3) 老清单没有 sha256 时不能报错(向后兼容: 1.0.0 之前生成的清单)
        legacy = json.loads(json.dumps(entry))
        for f in legacy["file_sizes"]:
            f.pop("sha256", None)
        (tmp / "local" / legacy["files"][0]).unlink(missing_ok=True)
        try:
            dataupdate.download_day(day0, legacy, local)
            check("无 sha256 的老清单仍可下载", True)
        except dataupdate.UpdateError as e:
            check("无 sha256 的老清单仍可下载", False, str(e)[:60])

        # 4) **manifest 必须最后落盘**。local_days() 用 manifest 存在与否判断
        #    "这天有没有", 所以若 manifest 先落盘而切片失败, 用户会看到"有数据"
        #    却播不完整, 且下次更新会跳过这天, 坏状态被固化。
        #    验证: 让某个切片必定失败, 断言失败后 manifest 不在本地。
        tmp2 = tmp / "failcase"
        (tmp2 / "data").mkdir(parents=True)
        fail_entry = json.loads(json.dumps(entry))
        fail_day_dir = tmp2 / "data" / day0 / "123"
        fail_day_dir.mkdir(parents=True, exist_ok=True)
        # 删掉本地已有的, 迫使全部重下; 并把一个切片的远端内容改坏
        victim2 = remote / fail_entry["files"][0]
        victim2_good = victim2.read_bytes()
        victim2.write_bytes(victim2_good[: len(victim2_good) // 2])
        try:
            dataupdate.download_day(day0, fail_entry, tmp2 / "data")
            check("切片失败时整体失败", False, "竟然成功了")
        except dataupdate.UpdateError:
            check("切片失败时整体失败", True)
        finally:
            victim2.write_bytes(victim2_good)
        check("切片失败时 manifest 未落盘",
              not (fail_day_dir / "manifest.json").is_file(),
              "manifest 落盘了 -> 会被误判为'这天已有数据'")
        check("切片失败时 local_days 不认为这天存在",
              day0 not in dataupdate.local_days(tmp2 / "data"))

        print("\n[镜像降级链]")
        # 官方地址应排在最后(有代理时它最可靠), 镜像在前(国内直连最快)
        official = ("https://raw.githubusercontent.com/root9701/"
                    "danmaku-timewarp/main/data-index.json")
        chain = srcfetch.mirrors(official)
        check("官方地址在最后", chain[-1] == official, chain)
        check("有多个镜像通道", len(chain) >= 3, chain)
        check("含 gh-proxy", any("gh-proxy.com" in c for c in chain))
        check("含 ghfast", any("ghfast.top" in c for c in chain))
        check("jsDelivr 用 @branch 格式",
              any("@main/" in c and "gh/" in c for c in chain), chain)
        # 本地/非官方源不套镜像, 否则会拼出 gh-proxy.com/http://127.0.0.1 这种废链
        local_url = f"http://127.0.0.1:{port}/data-index.json"
        check("非官方源不套镜像", srcfetch.mirrors(local_url) == [local_url])

        # 降级验证: 头两个通道指向死端口, 必须自动落到可用的那个
        saved_mirrors = srcfetch.mirrors
        real_url = f"{dataupdate.RAW_BASE}/data-index.json"
        dead = "http://127.0.0.1:1/x"       # 必定连不上
        try:
            srcfetch.mirrors = lambda u: [dead, dead, u]
            got = srcfetch.fetch(real_url)
            check("镜像失败自动降级到可用通道",
                  got == (remote / "data-index.json").read_bytes())
        except srcfetch.FetchError as e:
            check("镜像失败自动降级到可用通道", False, str(e)[:60])
        finally:
            srcfetch.mirrors = saved_mirrors

        # 网络全失败时, 报错必须是"连不上"而不是"校验失败"
        # (用户看到"数据损坏"会以为要重下大文件, 实际只是网络问题)
        try:
            srcfetch.mirrors = lambda u: [dead, dead]
            try:
                srcfetch.fetch(real_url)
                check("全网络失败报'连不上'", False, "竟然没报错")
            except srcfetch.FetchError as e:
                check("全网络失败报'连不上'", "连不上" in str(e), str(e)[:60])
        finally:
            srcfetch.mirrors = saved_mirrors

        # 清单哈希错 -> 多个通道给出相同内容都不对, 应早停并说"发布方的问题",
        # 而不是傻乎乎把 21MB 每个通道各下一遍(实测要 57 秒, 用户以为卡死)。
        calls = {"n": 0}
        saved_once = srcfetch.get_once
        saved_m = srcfetch.mirrors

        def _counting(url, timeout=None):
            calls["n"] += 1
            return saved_once(url, timeout=timeout)

        try:
            srcfetch.get_once = _counting
            # 两个通道指向同一个本地源: 内容必然相同 -> 第 2 次后应早停
            srcfetch.mirrors = lambda u: [u, u, u, u]
            try:
                srcfetch.fetch(real_url, sha256="0" * 64)
                check("清单哈希错时早停", False, "竟然没报错")
            except srcfetch.FetchError as e:
                msg = str(e)
                check("清单哈希错时早停", calls["n"] <= 2, f"调用了 {calls['n']} 次")
                check("哈希错时说清是发布方问题", "不一致" in msg and "连不上" not in msg,
                      msg[:80])
        finally:
            srcfetch.get_once = saved_once
            srcfetch.mirrors = saved_m

        # ---- 程序版本: 从 Releases 的 atom feed 解析 tag ----
        # 为什么不测真实 GitHub: 断网/限流会让回归变红, 而红的原因与代码无关
        # (实测 api.github.com 未认证 60 次/小时按 IP 计, 本机就被吃到 403,
        #  这正是改用 atom 的理由)。这里给固定 XML, 只验解析规则。
        def _atom(inner: str) -> bytes:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom">'
                '<title>Release notes from repo</title>'
                f"{inner}</feed>"
            ).encode("utf-8")

        def _tag_of(blob: bytes) -> str:
            saved_get = srcfetch.get_once
            try:
                srcfetch.get_once = lambda url, timeout=None: blob
                return srcfetch.latest_release_tag()
            finally:
                srcfetch.get_once = saved_get

        entry = ('<entry><title>弹幕时光机 1.2.0</title>'
                 '<link href="https://github.com/o/r/releases/tag/v1.2.0"/></entry>')
        check("atom: 取到 tag 并去掉 v 前缀", _tag_of(_atom(entry)) == "1.2.0")

        entry2 = ('<entry><title>x</title>'
                  '<link href="https://github.com/o/r/releases/tag/2.0.1"/></entry>')
        check("atom: 无 v 前缀原样返回", _tag_of(_atom(entry2)) == "2.0.1")

        # 标题里的版本可能被作者写成任何样子, 只有 link 末段必定是 tag。
        # 这条用"标题与 tag 故意不一致"来证明取的是 link 而不是标题。
        entry3 = ('<entry><title>弹幕时光机 9.9.9</title>'
                  '<link href="https://github.com/o/r/releases/tag/v1.3.0"/></entry>')
        check("atom: 以 link 里的 tag 为准(不是标题)", _tag_of(_atom(entry3)) == "1.3.0")

        # 没有 entry = 还没发过 Release。必须报错而不是返回空串 ——
        # 返回空串会被上层当成"版本未知", 进而显示成"已是最新"。
        try:
            _tag_of(_atom(""))
            check("atom: 空 feed 要报错", False, "竟然没报错")
        except srcfetch.FetchError as e:
            check("atom: 空 feed 报「还没发布过」", "还没有发布" in str(e), str(e)[:60])

        check("Release 页面地址可给用户", 
              srcfetch.release_page_url().endswith("/releases/latest"))
    finally:
        if httpd:
            httpd.shutdown()
        dataupdate.RAW_BASE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAIL: ' + str(len(FAILED)) + ' 项失败' if FAILED else 'OK: 全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
