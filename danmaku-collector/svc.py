#!/usr/bin/env python
"""弹幕采集器 · 后台服务管理。

用法（在本目录下执行，或双击同名 bat）:

    python svc.py start     后台启动采集器
    python svc.py stop      停止采集器
    python svc.py status    查看状态
    python svc.py restart   重启

为什么提供后台模式: 原来的 start.bat 是前台运行, 那个黑窗口一关采集就断了。
这里把采集器作为脱离终端的子进程启动, 关窗口不影响, 想停的时候跑 stop。

关于 PID 校验: 停止前会确认该 PID 的命令行里确实包含 collector.py。
Windows 的 PID 会被复用, 不校验就可能杀掉别的程序。
"""
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
SCRIPT = "collector.py"
PORT = 8787            # 控制台端口(dashboard.py)。以下用 python svc.py start --console 启动它
LOG_DIR = HERE / "run"
PID_FILE = LOG_DIR / "collector.pid"
LOG_FILE = LOG_DIR / "collector.log"

# 两种启动目标:
#   collector.py  —— 纯采集(前台日志), 不占端口
#   dashboard.py  —— 带网页控制台(8787), 可以在浏览器里改配置/看进度
TARGETS = {
    "collector": {"script": "collector.py", "port": None, "desc": "采集器(命令行)"},
    "console": {"script": "dashboard.py", "port": PORT, "desc": "采集控制台(网页)"},
}


def _no_window():
    return 0x08000000 if os.name == "nt" else 0


def _detached():
    return (0x00000008 | 0x00000200) if os.name == "nt" else 0


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=_no_window()).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline(pid: int):
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "ProcessId=%d" % pid, "get", "CommandLine", "/value"],
            capture_output=True, text=True, timeout=10,
            creationflags=_no_window()).stdout
        for line in out.splitlines():
            if line.startswith("CommandLine="):
                return line[len("CommandLine="):]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _port_open(port) -> bool:
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def read_pid():
    if not PID_FILE.is_file():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not _is_alive(pid):
        return None
    cmd = _cmdline(pid)
    if cmd is None:
        return pid
    # 校验确实是我们的脚本, 防止 PID 复用后杀错
    if ("collector.py" not in cmd) and ("dashboard.py" not in cmd):
        return None
    return pid


def _tail(n=12):
    try:
        return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def start(kind: str = "collector") -> int:
    tgt = TARGETS[kind]
    pid = read_pid()
    if pid:
        print("  采集器已在运行 (PID %d)" % pid)
        print("    控制台: http://127.0.0.1:%d" % PORT if _port_open(PORT)
              else "    (当前是命令行模式, 无网页控制台)")
        return 0
    if not PYTHON.is_file():
        print("  [错误] 找不到 Python 环境: %s" % PYTHON)
        print("  请先按 README 准备 .venv 虚拟环境")
        return 1
    if tgt["port"] and _port_open(tgt["port"]):
        print("  端口 %d 已被占用, 无法启动控制台" % tgt["port"])
        print("  查占用: netstat -ano | findstr :%d" % tgt["port"])
        return 1
    LOG_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["NO_BROWSER"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    fh = open(LOG_FILE, "ab")
    try:
        fh.write(("\n===== 启动 %s @ %s =====\n" % (
            tgt["desc"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
        fh.flush()
        proc = subprocess.Popen(
            [str(PYTHON), tgt["script"]],
            cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env,
            creationflags=_detached(), close_fds=True,
        )
    finally:
        fh.close()
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print("  正在启动 %s ..." % tgt["desc"], end="", flush=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if tgt["port"]:
            if _port_open(tgt["port"]):
                print(" 就绪")
                break
        elif read_pid():
            print(" 就绪")
            break
        if proc.poll() is not None:
            print(" 启动失败")
            print("    进程已退出(退出码 %s), 日志末尾:" % proc.returncode)
            for line in _tail():
                print("      " + line)
            PID_FILE.unlink(missing_ok=True)
            return 1
        time.sleep(0.4)
        print(".", end="", flush=True)
    else:
        print(" 超时")
    print("    PID=%d" % proc.pid)
    print("    日志: %s" % LOG_FILE)
    if _port_open(PORT):
        print("    控制台: http://127.0.0.1:%d" % PORT)
    return 0


def stop() -> int:
    pid = read_pid()
    if not pid:
        print("  采集器没在运行")
        PID_FILE.unlink(missing_ok=True)
        return 0
    print("  正在停止采集器 (PID %d) ..." % pid, end="", flush=True)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=_no_window())
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError) as e:
        print(" 失败: %s" % e)
        return 1
    for _ in range(25):
        if not _is_alive(pid):
            break
        time.sleep(0.3)
    print(" 已停止")
    PID_FILE.unlink(missing_ok=True)
    return 0


def status() -> int:
    pid = read_pid()
    print("=" * 56)
    if pid:
        print("  弹幕采集器: 运行中  PID=%d" % pid)
        print("  控制台:     %s" % ("http://127.0.0.1:%d" % PORT if _port_open(PORT)
                                    else "(命令行模式, 无网页控制台)"))
        try:
            import json
            import urllib.request
            j = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/api/status" % PORT, timeout=5).read())
            print("  采集状态:   %s" % ("录制中" if j.get("running") else "待机"))
            tail = (j.get("log_tail") or "").strip().splitlines()
            if tail:
                print("  最新日志:   %s" % tail[-1])
        except Exception:
            pass
    else:
        print("  弹幕采集器: 未运行")
    print("=" * 56)
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    kind = "console" if "--console" in sys.argv else "collector"
    if action == "start":
        return start(kind)
    if action == "stop":
        return stop()
    if action == "restart":
        stop()
        print()
        return start(kind)
    return status()


if __name__ == "__main__":
    sys.exit(main())
