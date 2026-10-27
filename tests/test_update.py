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
  - 路径穿越   -> 清单被篡改时写到 data/ 之外
"""
import gzip
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
            sizes.append({"name": rel, "bytes": len(body)})
            total += len(body)
        mf_rel = f"data/{day}/123/manifest.json"
        mf = remote / mf_rel
        man = {"schema_version": 1, "files": [f"x{i}.jsonl.gz" for i in range(n)]}
        mf.write_bytes(json.dumps(man).encode())
        files.append(mf_rel)
        sizes.append({"name": mf_rel, "bytes": mf.stat().st_size})
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

        print("\n[大小校验]")
        day0 = sorted(idx["days"])[0]
        entry = json.loads(json.dumps(idx["days"][day0]))   # 深拷贝
        entry["file_sizes"][0]["bytes"] += 999
        # 删掉该文件迫使重下, 然后大小对不上必须报错
        (tmp / "local" / entry["files"][0]).unlink(missing_ok=True)
        try:
            dataupdate.download_day(day0, entry, local)
            check("大小不符被拒绝", False, "竟然接受了")
        except dataupdate.UpdateError as e:
            check("大小不符被拒绝", "大小不符" in str(e), str(e)[:50])
    finally:
        if httpd:
            httpd.shutdown()
        dataupdate.RAW_BASE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAIL: ' + str(len(FAILED)) + ' 项失败' if FAILED else 'OK: 全部通过'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
