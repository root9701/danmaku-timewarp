# -*- coding: utf-8 -*-
"""手机采集节点管理(PC 侧) —— vivo X50 Pro+ / Termux / adb

用法:
    python phone_ctl.py status     # 电压/温度/屏幕/采集进程/数据文件
    python phone_ctl.py start      # 启动正式采集(config 定时 00:30-09:00), nohup 后台
    python phone_ctl.py stop       # 停止采集(finalize)
    python phone_ctl.py log [n]    # 拉取手机侧 run.log 尾部 n 行(默认 15)
    python phone_ctl.py pull       # 打包拉取手机 collector/data 全部数据到本机 data_phone/
    python phone_ctl.py test [秒]  # 手机上跑一次测试采集

原理:
    - 命令通过 adbkeyboard 输入法注入 Termux 终端(需要屏幕短暂点亮);
    - 注入后立即灭屏, 采集由 Termux 前台服务 + wakelock + Doze 白名单保活;
    - 结果回传: 手机 curl 经 adb reverse 隧道访问 PC 8899, 以 GET 查询串回读;
    - pull: reverse 8900 + 临时 HTTP 接收器收 tar.gz。
前置: scrcpy 不要开 -S 模式(会压制屏幕点亮); adb reverse 8899/8900 已建立(本脚本自动补建)。
"""
import argparse
import base64
import http.server
import itertools
import re
import socket
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# 手机采集节点是**本机专属**的调试工具: adb 路径/设备序列号因机器而异。
# 全部允许用环境变量覆盖, 避免把某台电脑的绝对路径写死在源码里(换机就跑不通)。
# 设备序列号**不给默认值**: 写死一个真实序列号等于把作者机器的硬件标识连同仓库
# 一起发出去, 而它对别人没有任何用处。没设时由 _pick_serial() 从 adb devices 里挑。
ADB = os.environ.get("ADB_BIN", "").strip() or "adb"
SERIAL = os.environ.get("ADB_SERIAL", "").strip()
IME = "com.android.adbkeyboard/.AdbIME"
HTTP_LOG = Path(os.environ.get("PHONE_CTL_LOG", "").strip()
                or (Path(tempfile.gettempdir()) / "phone_ctl_cb.log"))
PHONE_HOME = "/data/data/com.termux/files/home"
THIS_DIR = Path(__file__).resolve().parent
# 手机回传文件的落地目录(可用环境变量覆盖)
RECV_DIR = Path(os.environ.get("PHONE_RECV_DIR", "").strip()
                or (Path(tempfile.gettempdir()) / "phone_recv"))


def _pick_serial():
    """没设 ADB_SERIAL 时从 adb devices 里挑唯一在线设备。

    为什么不在源码里留默认序列号: 那是作者机器的硬件标识, 入库等于随仓库分发,
    而且对别人毫无用处(他们的序列号必然不同)。这里改成运行时发现, 且只接受
    "恰好一台设备在线"——多台时报错让人显式指定, 免得误操作到别的手机。
    """
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                             timeout=15, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"adb 不可用({e})。请确认 adb 在 PATH 里, 或用 ADB_BIN 指定路径。")
    # 跳过 "List of devices attached" 表头与空行; 只认状态为 device 的
    devs = [ln.split()[0] for ln in out.splitlines()
            if ln.strip() and "\t" in ln and ln.split("\t")[-1].strip() == "device"]
    if not devs:
        raise SystemExit("没有在线的 adb 设备。请插上手机并确认已授权 USB 调试, "
                         "或用 ADB_SERIAL 指定序列号。")
    if len(devs) > 1:
        raise SystemExit("检测到多台在线设备(" + ", ".join(devs) +
                         ")。请用 ADB_SERIAL 指定要操作哪一台。")
    return devs[0]


def sh(*a, timeout=60):
    r = subprocess.run([ADB, "-s", SERIAL, *a], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def shell(cmd, timeout=60):
    return sh("shell", cmd, timeout=timeout)


def ensure_reverse():
    out = sh("reverse", "--list")
    if "tcp:8899" not in out:
        sh("reverse", "tcp:8899", "tcp:8899")
    if "tcp:8900" not in out:
        sh("reverse", "tcp:8900", "tcp:8900")


class CbServer(threading.Thread):
    """极简回调收集: 记录手机 GET /CB?... 的查询串到文件, 供 PC 判断命令结果"""

    def __init__(self, port=8899):
        super().__init__(daemon=True)
        self.port = port
        HTTP_LOG.parent.mkdir(parents=True, exist_ok=True)
        HTTP_LOG.touch(exist_ok=True)

    def run(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with open(HTTP_LOG, "a", encoding="utf-8") as f:
                    f.write(self.path + "\n")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):  # 8900 数据上传
                n = int(self.headers.get("Content-Length") or 0)
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", self.path.strip("/") or "upload.bin")
                dst = RECV_DIR / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(self.rfile.read(n))
                with open(HTTP_LOG, "a", encoding="utf-8") as f:
                    f.write(f"UPLOAD {dst} {n}\n")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        try:
            http.server.HTTPServer(("127.0.0.1", self.port), H).serve_forever()
        except OSError:
            pass  # 已有实例


def start_cb_server():
    ensure_reverse()
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 8899))
        probe.close()
        CbServer(8899).start()
    except OSError:
        pass
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 8900))
        probe.close()
        CbServer(8900).start()
    except OSError:
        pass


_MARK_SEQ = itertools.count(1)  # 注入标记单调递增: wait_cb 只认"本次注入之后"新出现的回调行


def new_mark():
    """取一个新的注入标记; 调用方应把它拼进手机侧回调 URL(如 /CB{mark}),
    使回读只可能命中本次注入产生的那一行。"""
    return next(_MARK_SEQ)


def _log_lines():
    try:
        return HTTP_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def inject(cmd, keep_screen_on_during=True, mark=None):
    """把命令打进 Termux 终端并回车; 自动处理 亮屏/解锁/焦点/IME/灭屏 全流程。
    返回本次注入的标记(整数, 单调递增), 调用方应把该标记拼进回调 URL(如 /CB{mark}),
    这样 wait_cb 只认注入之后新出现的行, 不会命中历史旧回调。"""
    mark = mark or new_mark()
    # IME 必须在注入之前读取: 旧代码注入后才读, 拿到的已是 adbkeyboard 自身, 条件恒假 -> 永不恢复
    orig = shell("settings get secure default_input_method").strip()
    shell("svc power stayon usb")            # 保亮: 注入期间屏幕不灭
    shell("input keyevent 224")
    shell("wm dismiss-keyguard")
    shell("am start -n com.termux/.HomeActivity")
    time.sleep(2.5)
    shell("input tap 540 1100")
    time.sleep(0.5)
    shell(f"ime enable {IME}; ime set {IME}")
    time.sleep(1)
    shell("am broadcast -a ADB_INPUT_B64 --es msg " +
          base64.b64encode(cmd.encode()).decode())
    time.sleep(1.8)
    shell("input keyevent 66")
    time.sleep(2)
    if orig and "null" not in orig and "adbkeyboard" not in orig:
        shell(f"ime set {orig}")  # 恢复用户原输入法(注入完不恢复, 手机键盘就废了)
    return mark


def wait_cb(marker, timeout_s, mark=0):
    """等待手机 GET 回调里出现 marker, 返回整行(从**最新**行往前找)。

    为什么: 旧实现用"行长度 > mark(恒传 0, 实为文件字节数)"判新行, 且取首个匹配行 ——
    cmd_pull 用 marker="UPLOAD" 时必然命中上一次拉取的旧行, 解包旧 tar 却打印"已解包"。
    mark 为 inject() 返回的注入标记, 命中"本行含该标记"才算本次回调。"""
    deadline = time.time() + timeout_s
    size0 = HTTP_LOG.stat().st_size if HTTP_LOG.exists() else 0
    while time.time() < deadline:
        lines = _log_lines()
        size_now = HTTP_LOG.stat().st_size if HTTP_LOG.exists() else 0
        for line in reversed(lines):  # 最新优先, 避免命中历史同名回调
            if marker not in line:
                continue
            if mark and str(mark) in line:
                return line
            if size_now > size0:  # 回调没带标记时的兜底: 只接受本次注入后追加的行
                return line
        time.sleep(3)
    return ""


def cmd_status():
    bat = shell("dumpsys battery | grep -E 'voltage|temperature|status|AC powered'")
    print("[电池]", *["  " + l.strip() for l in bat.splitlines() if l.strip()], sep="\n")
    print("[屏幕]", shell("dumpsys window policy | grep screenState | head -1").strip())
    # 采集进程与数据: 经终端查询并回调
    start_cb_server()
    mark = new_mark()  # 本次注入标记, 拼进回调路径 -> wait_cb 只认这一行
    inject(f"P=$(pgrep -f collector.py | tr '\\n' ','); L=$(ls {PHONE_HOME}/collector/data/*/ 2>/dev/null | tail -8 | tr '\\n' '|'); "
           f'curl -sg "http://[::1]:8899/CB{mark}?proc=$P&files=$L"', mark=mark)
    line = wait_cb(f"CB{mark}", 40, mark)
    import urllib.parse as up
    if line:
        q = up.unquote(line.split("?", 1)[1].split(" ")[0]) if "?" in line else ""
        print("[手机]", q.replace("&", "\n      "))
    else:
        print("[手机] (40s 无回调, 可能屏幕/注入异常)")


def cmd_start():
    start_cb_server()
    inject(f"cd {PHONE_HOME}/collector && nohup python collector.py > run.log 2>&1 & echo STARTED")
    time.sleep(4)
    print("采集进程已提交(config 定时自动开录, run.log 在手机 ~/collector/)")
    print("用 `python phone_ctl.py log` 查看; `status` 确认进程")


def cmd_stop():
    start_cb_server()
    inject("pkill -f 'python collector.py'; echo STOPPED")
    time.sleep(3)
    print("已发送停止信号(采集器收到后 finalize 收尾)")


def cmd_log(n=15):
    start_cb_server()
    mark = new_mark()
    inject(f"cd {PHONE_HOME}/collector; B=$(tail -n {n} run.log 2>/dev/null | tr '\\n' ';'); "
           f"curl -sg -G --data-urlencode \"x=$B\" 'http://[::1]:8899/LOG{mark}'", mark=mark)
    import urllib.parse as up
    line = wait_cb(f"LOG{mark}", 40, mark)
    if line and "?" in line:
        print(up.unquote(line.split("x=", 1)[1].split(" HTTP")[0]).replace("+", " ").replace(";", "\n"))
    else:
        print("(无回调)")


def cmd_pull():
    start_cb_server()
    mark = new_mark()
    # 上传文件名带本次标记 -> 既能唯一定位新 tar, 也让 wait_cb 不会命中历史 UPLOAD 行
    inject(f"cd {PHONE_HOME}/collector && tar czf /data/data/com.termux/files/home/pull{mark}.tar.gz data && "
           f"curl -sg -X POST --data-binary @/data/data/com.termux/files/home/pull{mark}.tar.gz "
           f"'http://[::1]:8900/pull{mark}.tar.gz'", mark=mark)
    line = wait_cb(f"pull{mark}.tar.gz", 240, mark)
    if "UPLOAD" in line:
        recv = line.split()[1]
        dst = THIS_DIR / "data_phone"
        dst.mkdir(exist_ok=True)
        subprocess.run(["tar", "xzf", recv, "-C", str(dst)], check=True)
        print("已解包到", dst / "data")
        # 列产物: 兼容两种布局 —— 新布局每个会话是一个子目录(data/{日}/{会话}/),
        # 旧布局是散文件(data/{日}/{文件})。这里把会话目录展开成里面的文件来展示。
        for p in sorted((dst / "data").glob("*/*")):
            if p.is_dir():
                for f in sorted(p.glob("*")):
                    print("  ", p.name + "/" + f.name, f"{f.stat().st_size/1024:.0f}KB")
            else:
                print("  ", p.name, f"{p.stat().st_size/1024:.0f}KB")
    else:
        print("拉取超时")


def cmd_test(sec=60):
    start_cb_server()
    mark = new_mark()
    inject(f"cd {PHONE_HOME}/collector && timeout {sec + 90} python collector.py --test {sec} > t.log 2>&1; "
           f"L=$(ls data/$(date +%F)/ 2>/dev/null | tr '\\n' '|'); "
           f'curl -sg "http://[::1]:8899/TEST{mark}?files=$L"', mark=mark)
    import urllib.parse as up
    line = wait_cb(f"TEST{mark}", sec + 150, mark)
    print("[测试产物]", up.unquote(line.split("?", 1)[1]) if line and "?" in line else "(无回调/无文件)")


def main():
    global SERIAL
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="手机采集节点管理")
    p.add_argument("action", choices=["status", "start", "stop", "log", "pull", "test"])
    p.add_argument("n", nargs="?", type=int, default=None)
    a = p.parse_args()
    # 序列号可能来自 ADB_SERIAL, 也可能要现场发现。**不在这里写死任何值**。
    if not SERIAL:
        SERIAL = _pick_serial()
    out = sh("devices")
    if "device\n" not in out and "\tdevice" not in out:
        print("手机未连接:", out.strip()); return 1
    {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
     "log": lambda: cmd_log(a.n or 15), "pull": cmd_pull,
     "test": lambda: cmd_test(a.n or 60)}[a.action]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
