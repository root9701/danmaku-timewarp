#!/usr/bin/env python
"""服务管理器: 后台启动 / 停止 / 查看两个服务。

被 start-all.bat / stop-all.bat 调用, 也可以单独在命令行用:

    python ctl.py status          看两个服务的状态
    python ctl.py start server    启动回放台(后台)
    python ctl.py start collector 启动弹幕采集(后台)
    python ctl.py start all       两个都启动
    python ctl.py stop server     停止回放台
    python ctl.py stop all        两个都停
    python ctl.py lan             启动回放台 + 局域网访问 + 打印二维码网址

为什么不用 bat 直接起: bat 前台窗口一关服务就没了。这里用 Python 以脱离
父终端的子进程方式启动, 并把 PID 写进 run/ 目录, 之后按 PID 精确停止,
避免"杀错进程"或"杀了 venv 父进程留下真解释器子进程"。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"

SERVICES = {
    "server": {
        "name": "弹幕回放台",
        "dir": ROOT / "danmaku-server",
        "script": "server.py",
        "port": 8789,
        "venv": ROOT / "danmaku-server" / ".venv" / "Scripts" / "python.exe",
    },
    "collector": {
        "name": "弹幕采集器",
        "dir": ROOT / "danmaku-collector",
        "script": "dashboard.py",     # 用控制台版本: 带网页 UI, 可在浏览器里改配置/看进度
        "port": 8787,
        "venv": ROOT / "danmaku-collector" / ".venv" / "Scripts" / "python.exe",
    },
}


def _pid_file(key: str) -> Path:
    return RUN_DIR / ("%s.pid" % key)


def _log_file(key: str) -> Path:
    return RUN_DIR / ("%s.log" % key)


def read_pid(key: str):
    """读 PID 并校验该进程确实还在跑且确实是我们的进程。"""
    pf = _pid_file(key)
    if not pf.is_file():
        return None
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not _is_alive(pid):
        return None
    # 校验命令行里确实包含目标脚本, 防止 PID 被系统复用后用错进程
    cmd = _cmdline(pid)
    if cmd is None:
        return pid            # 拿不到命令行就不深究(权限限制), 按活着算
    want = SERVICES[key]["script"]
    return pid if want in cmd else None


def _is_alive(pid: int) -> bool:
    """Windows 上检查进程存活。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=8,
                creationflags=_no_window(),
            ).stdout
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
            creationflags=_no_window(),
        ).stdout
        for line in out.splitlines():
            if line.startswith("CommandLine="):
                return line[len("CommandLine="):]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _no_window():
    """子进程不弹黑窗。"""
    if os.name == "nt":
        return 0x08000000          # CREATE_NO_WINDOW
    return 0


def _detached():
    """让子进程脱离当前终端, 关闭窗口后仍继续运行。"""
    if os.name == "nt":
        return 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return 0


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def start(key: str, wait_sec: float = 25.0, env_extra=None) -> int:
    svc = SERVICES[key]
    pid = read_pid(key)
    if pid:
        print("  %s 已在运行 (PID %d)" % (svc["name"], pid))
        return 0
    if _port_in_use(svc["port"]):
        print("  %s 的端口 %d 已被别的程序占用, 无法启动" % (svc["name"], svc["port"]))
        print("    查占用: netstat -ano | findstr :%d" % svc["port"])
        return 1
    venv = svc["venv"]
    if not venv.is_file():
        print("  找不到 Python 环境: %s" % venv)
        print("    请先按 README 准备虚拟环境(.venv)")
        return 1
    RUN_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["NO_BROWSER"] = "1"          # 别自动弹浏览器, 由我们自己打印地址
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    logf = _log_file(key)
    fh = open(logf, "ab")
    try:
        fh.write(("\n===== 启动 %s @ %s =====\n" % (
            svc["name"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
        fh.flush()
        proc = subprocess.Popen(
            [str(venv), svc["script"]],
            cwd=str(svc["dir"]),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=_detached(),
            close_fds=True,
        )
    finally:
        fh.close()
    _pid_file(key).write_text(str(proc.pid), encoding="utf-8")
    print("  正在启动 %s ..." % svc["name"], end="", flush=True)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if _port_in_use(svc["port"]):
            print(" 就绪")
            print("    PID=%d  端口=%d" % (proc.pid, svc["port"]))
            print("    日志: %s" % logf)
            return 0
        if proc.poll() is not None:
            print(" 启动失败")
            print("    进程已退出(退出码 %s), 日志末尾:" % proc.returncode)
            for line in _tail(logf, 12):
                print("      " + line)
            _pid_file(key).unlink(missing_ok=True)
            return 1
        time.sleep(0.4)
        print(".", end="", flush=True)
    print(" 超时")
    print("    端口 %d 在 %d 秒内没监听, 看日志: %s" % (svc["port"], int(wait_sec), logf))
    return 1


def stop(key: str) -> int:
    svc = SERVICES[key]
    pid = read_pid(key)
    if not pid:
        print("  %s 没在运行" % svc["name"])
        _pid_file(key).unlink(missing_ok=True)
        return 0
    print("  正在停止 %s (PID %d) ..." % (svc["name"], pid), end="", flush=True)
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
    _pid_file(key).unlink(missing_ok=True)
    return 0


def _tail(path: Path, n: int = 10):
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return data[-n:]
    except OSError:
        return []


def status() -> int:
    print("=" * 60)
    for key, svc in SERVICES.items():
        pid = read_pid(key)
        if pid:
            print("  %-8s 运行中  PID=%-7d  http://127.0.0.1:%d" % (
                svc["name"], pid, svc["port"]))
        elif _port_in_use(svc["port"]):
            print("  %-8s 端口被占用但无 PID 记录(可能是手动启动的)" % svc["name"])
        else:
            print("  %-8s 未运行" % svc["name"])
    print("=" * 60)
    return 0


def lan_ips():
    """列出局域网 IPv4(优先默认路由那个, 其余追加)。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("169.254.")]


def print_qr(url: str):
    """在终端打印二维码。用块字符画, 兼容 Windows 控制台。"""
    try:
        import qrcode
    except ImportError:
        print("  (未安装 qrcode, 无法显示二维码; 可用手机相机直接输入上面的网址)")
        print("  安装: .venv\\Scripts\\python.exe -m pip install qrcode")
        return
    qr = qrcode.QRCode(border=1, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    # 用两个字符宽表示一个模块, 更接近正方形, 扫码更容易
    for row in m:
        line = "".join("██" if cell else "  " for cell in row)
        print("    " + line)


def cmd_lan(restart: bool, show_qr: bool) -> int:
    """启动回放台并开局域网访问, 打印二维码与网址。"""
    key = "server"
    svc = SERVICES[key]
    cfg_path = svc["dir"] / "config.json"
    # 临时用环境变量覆盖监听地址, 不改用户的 config.json
    if restart:
        stop(key)
    print()
    rc = start(key, env_extra={"DANMAKU_HOST": "0.0.0.0"})
    if rc != 0:
        return rc
    ips = lan_ips()
    print()
    print("=" * 60)
    print("  弹幕回放台已启动")
    print()
    print("  本机访问:  http://127.0.0.1:%d" % svc["port"])
    if ips:
        for ip in ips:
            print("  局域网:    http://%s:%d" % (ip, svc["port"]))
        print()
        print("  手机/iPad 扫码（和电脑连同一个 WiFi）:")
        print()
        print_qr("http://%s:%d" % (ips[0], svc["port"]))
    else:
        print("  没检测到局域网地址, 检查网络连接")
    print()
    print("  停止服务:  stop-server.bat   或   python ctl.py stop server")
    print("=" * 60)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="弹幕伪直播 · 服务管理")
    ap.add_argument("action", choices=["start", "stop", "restart", "status", "lan"],
                    help="start/stop/restart/status/lan")
    ap.add_argument("target", nargs="?", default="all",
                    choices=["server", "collector", "all"], help="目标服务")
    ap.add_argument("--no-qr", action="store_true", help="lan 模式不显示二维码")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.action == "status":
        return status()
    if args.action == "lan":
        return cmd_lan(restart=True, show_qr=not args.no_qr)

    keys = ["server", "collector"] if args.target == "all" else [args.target]
    rc = 0
    if args.action in ("start", "restart"):
        if args.action == "restart":
            for k in keys:
                stop(k)
            print()
        for k in keys:
            rc |= start(k)
    elif args.action == "stop":
        for k in keys:
            rc |= stop(k)
    print()
    status()
    return rc


if __name__ == "__main__":
    sys.exit(main())
