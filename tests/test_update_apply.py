"""替换器的端到端验收: 造一个"旧包"和一个"新包", 真的跑一次 swap。

验四件事(任何一条失败都说明用户会丢数据或程序起不来):
  1. data/ 与 config.json 被带到新目录, 内容逐字节一致
  2. 程序文件换成了新版的(server.py 里能读到新标记)
  3. 旧目录被清掉, 不留 .old-*
  4. 端口占用时拒绝替换(不能在半截状态动目录)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "danmaku-server" / "update_apply.py"

# 与别的 test_*.py 一样直接 import 被测模块: run_all.py 已在 sys.path 里放了 danmaku-server/
sys.path.insert(0, str(ROOT / "danmaku-server"))   # noqa: E402


def make_pkg(root: Path, version: str, marker: str) -> None:
    """造一个符合真实包结构的最小包(顶层一层同名目录)。"""
    inner = root / f"pkg-{version}"
    (inner / "danmaku-server" / "web").mkdir(parents=True)
    (inner / "python").mkdir(parents=True)
    (inner / "VERSION").write_text(version, encoding="utf-8")
    (inner / "danmaku-server" / "server.py").write_text(f"# {marker}", encoding="utf-8")
    (inner / "danmaku-server" / "web" / "app.js").write_text("// app", encoding="utf-8")
    (inner / "python" / "python.exe").write_bytes(b"MZ fake")


def zip_pkg(src_root: Path, version: str, out: Path) -> Path:
    inner = src_root / f"pkg-{version}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in inner.rglob("*"):
            if p.is_file():
                z.write(p, f"danmaku-timewarp-{version}-slim/{p.relative_to(inner).as_posix()}")
    return out


def occupy(port: int):
    """占住端口, 模拟"旧程序还没退"。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_download_path(payload: bytes) -> list:
    """验 appupdate.download 的整条路径(地址拼装 -> 取通道 -> 落盘 -> 校验)。

    为什么要单独测这条: 1.5.0 开发时 Release 解析从 srcfetch 搬到了 appupdate,
    `download()` 里漏改了两处调用(`srcfetch.release_download_url` / `srcfetch.mirrors_release`),
    而当时的测试只覆盖了"查版本" —— 真机点「立即更新」才崩。这条测试就是补那个洞:
    起一个本地 HTTP 服务当"发布页", 让 download() 真的走一遍下载+校验。
    """
    import http.server
    import threading

    import appupdate

    problems = []
    saved_reader = appupdate.mirrors_release
    httpd = None
    try:
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        port = free_port()
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        # 把"官方地址"换成本地服务, 只改通道来源而不改 download 的任何逻辑
        appupdate.mirrors_release = lambda url: [f"http://127.0.0.1:{port}/pkg.zip"]
        with tempfile.TemporaryDirectory() as td2:
            dest = appupdate.download("1.5.0", Path(td2))
            if dest.name != "danmaku-timewarp-1.5.0-slim.zip":
                problems.append(f"落地文件名不对: {dest.name}")
            if dest.read_bytes() != payload:
                problems.append("下载内容与源文件不一致")
            if not (dest.parent.name == appupdate.STAGE_DIRNAME):
                problems.append(f"没落在 {appupdate.STAGE_DIRNAME}/ 下: {dest.parent}")
            if list(dest.parent.glob("*.part")):
                problems.append("留下了 .part 半截文件")
            if not hits:
                problems.append("根本没发出请求(通道链是空的?)")
        # 版本对不上时必须拒绝
        with tempfile.TemporaryDirectory() as td3:
            try:
                appupdate.download("9.9.9", Path(td3))
                problems.append("版本对不上却下载成功")
            except appupdate.UpdateError:
                pass
    finally:
        appupdate.mirrors_release = saved_reader
        if httpd:
            httpd.shutdown()
    return problems


def main() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # --- 旧包 ---
        old = base / "danmaku-timewarp-1.4.0-slim"
        make_pkg(base, "1.4.0", "OLD")
        (base / "pkg-1.4.0").rename(old)
        (old / "Start.bat").write_text("@echo off\n", encoding="utf-8")
        # 用户数据
        (old / "data" / "2026-11-01").mkdir(parents=True)
        (old / "data" / "2026-11-01" / "afp.bin").write_bytes(b"\x00\x01" * 500)
        (old / "config.json").write_text(json.dumps({"port": 8789}), encoding="utf-8")
        (old / "matches.json").write_text('{"align":{}}', encoding="utf-8")
        data_before = (old / "data" / "2026-11-01" / "afp.bin").read_bytes()

        # --- 新包 ---
        make_pkg(base, "1.5.0", "NEW")
        z = zip_pkg(base, "1.5.0", base / "new.zip")

        # --- 1) 端口被占时必须拒绝 ---
        port = free_port()
        sock = occupy(port)
        try:
            r = subprocess.run([sys.executable, str(UPDATER), "--zip", str(z), "--root", str(old),
                                "--port", str(port), "--version", "1.5.0"],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            if r.returncode == 0:
                fails.append("端口被占用时居然替换成功了")
            if "OLD" not in (old / "danmaku-server" / "server.py").read_text(encoding="utf-8"):
                fails.append("端口被占时旧目录被改动了")
            if "占用" not in (r.stdout or ""):
                fails.append(f"拒绝原因没写清楚: {r.stdout!r}")
        finally:
            sock.close()

        # --- 2) 端口空出来, 真正替换 ---
        # 先把 zip 读进内存: 替换成功后整个 tmp 目录会被清掉, 后面那条下载测试还要用。
        zip_bytes = z.read_bytes()
        port2 = free_port()
        r = subprocess.run([sys.executable, str(UPDATER), "--zip", str(z), "--root", str(old),
                            "--port", str(port2), "--version", "1.5.0"],
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            fails.append(f"替换失败: {out[-500:]}")

        if not old.is_dir():
            fails.append("替换后包根目录不见了")
        else:
            if "NEW" not in (old / "danmaku-server" / "server.py").read_text(encoding="utf-8"):
                fails.append("程序文件没换成新版")
            got = (old / "data" / "2026-11-01" / "afp.bin").read_bytes() if (old / "data" / "2026-11-01" / "afp.bin").is_file() else b""
            if got != data_before:
                fails.append("data/ 没被保留(音频指纹丢失)")
            if not (old / "config.json").is_file():
                fails.append("config.json 丢失")
            if not (old / "matches.json").is_file():
                fails.append("matches.json 丢失")
            if (old / "VERSION").read_text(encoding="utf-8").strip() != "1.5.0":
                fails.append("VERSION 没更新")

        leftovers = [p.name for p in base.glob("*.old-*")]
        if leftovers:
            fails.append(f"留下旧目录: {leftovers}")
        if (old / "_update" / "stage").exists():
            fails.append("解压暂存目录没清掉")

        # --- 3) 版本不符必须拒绝 ---
        base3 = base / "case3"
        base3.mkdir()
        make_pkg(base3, "1.5.0", "NEW")
        z3 = zip_pkg(base3, "1.5.0", base3 / "new.zip")
        r3 = subprocess.run([sys.executable, str(UPDATER), "--zip", str(z3), "--root", str(old),
                             "--port", str(free_port()), "--version", "9.9.9"],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
        if r3.returncode == 0:
            fails.append("版本对不上却替换了")
        if "NEW" not in (old / "danmaku-server" / "server.py").read_text(encoding="utf-8"):
            fails.append("版本不符时把已装好的新目录改坏了")

        # --- 4) 下载路径(1.5.0 在这儿漏改过调用) ---
        fails.extend(check_download_path(zip_bytes))

        # --- 5) 目录被别的进程当工作目录时, 必须能靠"就地覆盖"完成更新 ---
        # 真实场景: Start.bat 第一行是 cd /d "%~dp0", 那个控制台窗口的工作目录就是
        # 包根, 一直锁到用户关窗口为止。这时改名必然失败, 所以要求退化成覆盖方式,
        # 并且**不能**碰用户数据。
        b5 = base / "case5"
        b5.mkdir()
        make_pkg(b5, "1.4.0", "OLD5")
        (b5 / "pkg-1.4.0").rename(b5 / "target")
        tgt = b5 / "target"
        (tgt / "Start.bat").write_text("@echo off\n", encoding="utf-8")
        (tgt / "data").mkdir()
        (tgt / "data" / "keep.bin").write_bytes(b"USERDATA")
        make_pkg(b5, "1.5.0", "NEW5")
        z5 = zip_pkg(b5, "1.5.0", b5 / "new5.zip")

        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(25)"], cwd=str(tgt))
        try:
            env_upd = dict(os.environ)
            env_upd["UPDATE_DIR_LOCK_WAIT"] = "2"     # 别让回归等 8 秒
            r5 = subprocess.run([sys.executable, str(UPDATER), "--zip", str(z5),
                                 "--root", str(tgt), "--port", str(free_port()),
                                 "--version", "1.5.0"],
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=180, env=env_upd)
            out5 = (r5.stdout or "") + (r5.stderr or "")
            if r5.returncode != 0:
                fails.append(f"目录锁着时没能覆盖安装: {out5[-300:]}")
            server5 = tgt / "danmaku-server" / "server.py"
            if not server5.is_file() or "NEW5" not in server5.read_text(encoding="utf-8"):
                fails.append("目录锁着时程序文件没更新")
            if not (tgt / "data" / "keep.bin").is_file():
                fails.append("覆盖方式把 data/ 弄丢了")
            if (tgt / "VERSION").read_text(encoding="utf-8").strip() != "1.5.0":
                fails.append("覆盖方式没更新 VERSION")
            if (tgt / "_update" / "stage").exists():
                fails.append("覆盖方式没清理解压暂存")
            if "覆盖" not in out5:
                fails.append("没说明用的是覆盖方式")
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    for f in fails:
        print("FAIL:", f)
    print("OK: 替换器与下载路径全部验收通过" if not fails else f"FAILED {len(fails)} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
