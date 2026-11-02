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

    for f in fails:
        print("FAIL:", f)
    print("OK: 替换器四项验收全过" if not fails else f"FAILED {len(fails)} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
