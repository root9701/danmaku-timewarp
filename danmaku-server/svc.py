#!/usr/bin/env python
"""弹幕回放台 · 后台服务管理。

用法（在本目录下执行，或双击同名 bat）:

    python svc.py start      后台启动（仅本机可访问）
    python svc.py lan        后台启动 + 局域网可访问 + 打印二维码
    python svc.py stop       停止
    python svc.py status     查看状态
    python svc.py restart    重启

关于局域网: start 只绑 127.0.0.1(最安全); lan 用环境变量把监听地址临时改成
0.0.0.0, 不改动你的 config.json。注意本服务没有登录鉴权, 只在可信网络里用 lan。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
SCRIPT = "server.py"
PORT = 8789
LOG_DIR = HERE / "run"
PID_FILE = LOG_DIR / "server.pid"
LOG_FILE = LOG_DIR / "server.log"


def _no_window():
    return 0x08000000 if os.name == "nt" else 0


def _detached():
    """子进程的创建标志: 不弹窗 + 脱离父终端独立运行。

    **不要用 DETACHED_PROCESS(0x8)**: 它的语义是"不继承父控制台", 结果 Windows
    会给子进程新开一个控制台窗口 —— 用户点一次启动就看到两个黑窗(一个 bat 的,
    一个新弹出的)。踩过这个坑。

    改用 CREATE_NO_WINDOW(0x08000000) | CREATE_NEW_PROCESS_GROUP(0x200):
      - 完全不创建控制台窗口
      - 进程独立于父终端, 关掉 bat 窗口不影响
      - 独立进程组, taskkill /T 能整组结束
    """
    if os.name != "nt":
        return 0
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


_PS_PROC_CACHE = {"ts": 0.0, "data": None}


def _ps_processes():
    """用 PowerShell CIM 一次性取全部进程的 (PID, 父PID, 路径, 命令行)。

    为什么不用 wmic: 新版 Windows 已移除 wmic(实测 Get-Command wmic 找不到),
    以前依赖它的函数全部静默返回 None, 导致 PID 校验和端口诊断都失效。
    结果缓存 2 秒, 避免启动/停止流程里反复调用 powershell 拖慢速度。
    """
    import time as _t
    now = _t.time()
    if _PS_PROC_CACHE["data"] is not None and now - _PS_PROC_CACHE["ts"] < 2.0:
        return _PS_PROC_CACHE["data"]
    data = {}
    try:
        ps = ("Get-CimInstance Win32_Process | "
              "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
              "ConvertTo-Csv -NoTypeInformation")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=25,
            creationflags=_no_window()).stdout
        import csv as _csv
        import io as _io
        for row in _csv.DictReader(_io.StringIO(out)):
            try:
                pid = int(row["ProcessId"])
            except (TypeError, ValueError, KeyError):
                continue
            data[pid] = {
                "ppid": int(row.get("ParentProcessId") or 0),
                "exe": (row.get("ExecutablePath") or "").strip(),
                "cmd": (row.get("CommandLine") or "").strip(),
            }
    except (OSError, subprocess.SubprocessError):
        pass
    _PS_PROC_CACHE["data"] = data
    _PS_PROC_CACHE["ts"] = now
    return data


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
    """取进程命令行(用于校验该 PID 确实是我们的脚本)。取不到返回 None。"""
    info = _ps_processes().get(pid)
    return info["cmd"] if info and info["cmd"] else None


def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()



def port_owner(port: int):
    """查出是谁在监听指定端口。返回 (pid, 可执行文件路径), 查不到返回 (None, None)。

    用途: 端口被占时告诉用户"是哪个程序占着", 而不是只报一句"已被占用"。
    """
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        return (None, None)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        m = re.search(r":(\d+)$", parts[1])
        if not m or int(m.group(1)) != int(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid <= 0:
            continue
        return (pid, _exe_of(pid))
    return (None, None)


def _exe_of(pid: int):
    """取进程可执行文件路径(用于告诉用户"端口被哪个程序占着")。"""
    info = _ps_processes().get(pid)
    return info["exe"] if info and info["exe"] else None


def _is_child_of(child: int, parent: int) -> bool:
    """判断 child 是否是 parent 的（直接或间接）子进程。

    Windows 上 .venv\\Scripts\\python.exe 只是个启动器, 会立刻派生一个真正的
    解释器子进程来跑脚本; 监听端口的实际是那个子进程。所以校验端口占用者时
    不能只比 PID, 还要认子进程。
    """
    procs = _ps_processes()
    if not procs:
        return True          # 查不到就不阻断(宁可放过也不错杀)
    cur, depth = child, 0
    while depth < 8:
        info = procs.get(cur)
        if not info:
            return False
        ppid = info["ppid"]
        if ppid == parent:
            return True
        if ppid <= 0 or ppid == cur:
            return False
        cur = ppid
        depth += 1
    return False


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
    return pid if "server.py" in cmd else None


def lan_ips():
    """本机局域网地址。第一个是默认路由用的那个, 通常就是手机该连的。"""
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
    try:
        import qrcode
    except ImportError:
        print("  (未安装 qrcode 库, 无法显示二维码; 手动输入上面的网址即可)")
        print("   安装: .venv\\Scripts\\python.exe -m pip install qrcode")
        return
    qr = qrcode.QRCode(border=1, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    for row in qr.get_matrix():
        # 每格画两个字符宽, 接近正方形, 扫码更容易
        print("    " + "".join("██" if cell else "  " for cell in row))


def _tail(n=12):
    try:
        return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def start(lan: bool = False) -> int:
    pid = read_pid()
    if pid:
        print("  回放台已在运行 (PID %d)" % pid)
        return 0
    if not PYTHON.is_file():
        print("  [错误] 找不到 Python 环境: %s" % PYTHON)
        print("  请先按 README 准备 .venv 虚拟环境")
        return 1
    if _port_open(PORT):
        owner_pid, owner_exe = port_owner(PORT)
        print("  端口 %d 已被占用, 无法启动。" % PORT)
        if owner_pid:
            print("    占用者: PID %d" % owner_pid)
            print("    程序:   %s" % (owner_exe or "(查不到路径)"))
            print("    处理:   先关掉它, 或在任务管理器里按 PID 结束")
        else:
            print("    查不到占用进程（可能需要管理员权限）")
            print("    自查: netstat -ano | findstr :%d" % PORT)
        return 1
    LOG_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["NO_BROWSER"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 显式指定监听地址, 不依赖 config.json 里的 host。
    # 否则用户改过 config 之后, "本机模式"会意外变成对局域网开放 —— 那是安全问题。
    env["DANMAKU_HOST"] = "0.0.0.0" if lan else "127.0.0.1"
    fh = open(LOG_FILE, "ab")
    try:
        fh.write(("\n===== 启动回放台%s @ %s =====\n" % (
            "(局域网)" if lan else "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
        fh.flush()
        proc = subprocess.Popen(
            [str(PYTHON), SCRIPT],
            cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env,
            creationflags=_detached(), close_fds=True,
        )
    finally:
        fh.close()
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print("  正在启动回放台 ...", end="", flush=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if _port_open(PORT):
            # 确认端口确实是**我们刚起的**进程占的, 而不是别的程序抢了端口
            owner_pid, _ = port_owner(PORT)
            if owner_pid and owner_pid not in (proc.pid,) and not _is_child_of(owner_pid, proc.pid):
                print(" 异常")
                print("    端口被 PID %d 占用, 不是本次启动的进程(PID %d)" % (owner_pid, proc.pid))
                print("    可能上一个实例还没退干净, 请先运行 stop")
                return 1
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
        for line in _tail(6):
            print("      " + line)
        return 1
    print("    PID=%d" % proc.pid)
    print("    日志: %s" % LOG_FILE)
    return 0


def stop() -> int:
    pid = read_pid()
    if not pid:
        print("  回放台没在运行")
        PID_FILE.unlink(missing_ok=True)
        return 0
    print("  正在停止回放台 (PID %d) ..." % pid, end="", flush=True)
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
        print("  弹幕回放台: 运行中  PID=%d" % pid)
        print("  本机访问:   http://127.0.0.1:%d" % PORT)
        ips = lan_ips()
        # 判断是否真的对外监听: 用外部地址试探
        exposed = False
        for ip in ips:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.6)
            try:
                exposed = s.connect_ex((ip, PORT)) == 0
            finally:
                s.close()
            if exposed:
                print("  局域网:     http://%s:%d  (可手机访问)" % (ip, PORT))
                break
        if not exposed:
            print("  局域网:     未开启（需要手机访问请用 svc.py lan 启动）")
    else:
        # PID 文件可能丢了(手工启动/上次没清干净), 但端口上可能真有服务在跑。
        # 只说"未运行"会让用户以为没开, 实际页面却打得开 —— 必须说清楚。
        owner_pid, owner_exe = port_owner(PORT) if _port_open(PORT) else (None, None)
        if owner_pid:
            print("  弹幕回放台: 运行中(非本脚本启动)")
            print("  占用进程:   PID %d" % owner_pid)
            if owner_exe:
                print("  程序:       %s" % owner_exe)
            print("  本机访问:   http://127.0.0.1:%d" % PORT)
            print("  提示:       本脚本没记录它的 PID, 无法用 svc.py stop 停止;")
            print("              需要停请到任务管理器结束 PID %d" % owner_pid)
        else:
            print("  弹幕回放台: 未运行")
    print("=" * 56)
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action == "start":
        return start(lan=False)
    if action == "lan":
        stop()
        print()
        rc = start(lan=True)
        if rc != 0:
            return rc
        ips = lan_ips()
        print()
        print("=" * 56)
        print("  手机/iPad 访问（和电脑连同一个 WiFi）")
        print()
        print("    本机:   http://127.0.0.1:%d" % PORT)
        if ips:
            for ip in ips:
                print("    局域网: http://%s:%d" % (ip, PORT))
            print()
            print("  扫码直接打开:")
            print()
            print_qr("http://%s:%d" % (ips[0], PORT))
        else:
            print("    没检测到局域网地址, 检查网络连接")
        print()
        print("  停止: svc.py stop   或双击 停止.bat")
        print("=" * 56)
        return 0
    if action == "stop":
        return stop()
    if action == "restart":
        stop()
        print()
        return start(lan=False)
    return status()


if __name__ == "__main__":
    sys.exit(main())
