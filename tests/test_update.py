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

        # 1) 大小不符: 清单说 1060 字节, 实际 61 -> 必须拒绝
        entry_bad_size = json.loads(json.dumps(entry))
        entry_bad_size["file_sizes"][0]["bytes"] += 999
        (tmp / "local" / entry_bad_size["files"][0]).unlink(missing_ok=True)
        try:
            dataupdate.download_day(day0, entry_bad_size, local)
            check("大小不符被拒绝", False, "竟然接受了")
        except dataupdate.UpdateError as e:
            check("大小不符被拒绝", "校验失败" in str(e) or "字节" in str(e),
                  str(e)[:70])

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
    finally:
        if httpd:
            httpd.shutdown()
        dataupdate.RAW_BASE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAIL: ' + str(len(FAILED)) + ' 项失败' if FAILED else 'OK: 全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
