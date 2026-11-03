# -*- coding: utf-8 -*-
"""gitpush.py — git 远端推送的网络环境与重试。

为什么单独成模块:
  `release.py` 与 `nightly.py` 都要推远端, 也都要读 Windows 系统代理 —— git 不读
  "设置 -> 网络 -> 代理", 只认 HTTPS_PROXY/HTTP_PROXY。两处各写一份必然漂移:
  一处修了代理、另一处还直连, 症状是"有时能推、有时超时", 且报错完全不提代理。

边界:
  - 只推调用方指定的分支; 不 force、不推 tag、不建 Release。
  - 只重试瞬时网络错误; 认证失败/非快进直接返回, 让调用方停下等人。
"""
import os
import subprocess
import time

# 不重试的失败: 重试只会重复失败, 还会把"需要人处理"拖成几分钟的等待。
_PERMANENT = (
    "non-fast-forward", "rejected", "fetch first",
    "Authentication failed", "Permission denied", "could not read Username",
    "Repository not found", "protected branch", "GH013",
)

# 瞬时失败: 网络/代理抖动, 下一轮或下一次重试可能成功。
_TRANSIENT = (
    "Could not resolve host", "Failed to connect", "Connection reset",
    "Connection timed out", "timed out", "timeout", "超时", "TLS", "SSL",
    "unable to access", "early EOF", "RPC failed", "remote end hung up",
    "The requested URL returned error: 5",
)


def system_proxy() -> str:
    """Windows 系统代理(如 http://127.0.0.1:7897), 没配返回空串。

    `git` 与 `gh` 都**不读** Windows 的"设置 -> 网络 -> 代理"，只认
    HTTPS_PROXY/HTTP_PROXY 环境变量。实测本机 github.com 直连 15 秒超时、
    设了代理 2.7 秒通 —— 于是"代理开着、浏览器正常、git push 却 Connection reset"
    这种状态很常见, 且报错完全不提代理。这里系统代理开着就自动沿用。
    """
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        return ""                     # 用户已自己设了, 不覆盖
    if os.name != "nt":
        return ""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            on = winreg.QueryValueEx(k, "ProxyEnable")[0]
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
    except (OSError, ImportError):
        return ""
    if not on or not server:
        return ""
    server = str(server).split(";")[0].strip()      # "http=x;https=y" 取第一段
    if "=" in server:
        server = server.split("=", 1)[1].strip()
    if not server.startswith("http"):
        server = "http://" + server
    return server


def tool_env() -> dict:
    """给 git/gh 用的环境: 补上系统代理。

    只影响调用方派生的子进程, 不改用户的环境变量、也不写任何配置 ——
    用你的机器可以随便折腾, 但别把设备状态改坏。
    """
    env = dict(os.environ)
    px = system_proxy()
    if px:
        env["HTTPS_PROXY"] = px
        env["HTTP_PROXY"] = px
    return env


def _run(cmd, env, timeout, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=env, timeout=timeout)
    except FileNotFoundError:
        return False, f"找不到可执行文件: {cmd[0]}（不在 PATH 里?）"
    except subprocess.TimeoutExpired:
        return False, f"命令超时({timeout}s): {' '.join(map(str, cmd[:3]))}"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()


def is_transient(output: str) -> bool:
    """这段 git 输出是否值得重试。先判永久失败, 避免被其中的 "timed out" 误伤。"""
    low = output.lower()
    if any(k.lower() in low for k in _PERMANENT):
        return False
    return any(k.lower() in low for k in _TRANSIENT)


def push(remote: str, branch: str, timeout: int = 180, attempts: int = 3,
         delays=(10, 30), runner=None, cwd=None, ref: str = "HEAD") -> tuple:
    """把 `ref` 推到 `remote/branch`。返回 `(ok, 输出)`。

    为什么用 `<ref>:refs/heads/<branch>` 而不是 `git push origin <branch>`:
    夜间流水线可能由控制台以任意工作目录拉起, 显式写全 ref 才能保证推的是
    指定提交。`pushctl` 会传入检查过数据范围的 HEAD SHA, 避免"检查完又冒出一个
    提交"时把它一起推走。

    `runner` 可注入, 测试用它替掉真实 git; 默认跑子进程。
    """
    env = tool_env()
    env["GIT_TERMINAL_PROMPT"] = "0"   # 无人值守: 不能停在等账号密码
    env["GCM_INTERACTIVE"] = "never"   # 凭据过期时也别弹 GUI 把夜间进程挂住
    cmd = ["git", "push", remote, f"{ref}:refs/heads/{branch}"]
    run = runner or (lambda c, e, t: _run(c, e, t, cwd))
    last = ""
    for i in range(max(1, attempts)):
        ok, out = run(cmd, env, timeout)
        if ok:
            return True, out
        last = out
        if not is_transient(out) or i == attempts - 1:
            break
        time.sleep(delays[min(i, len(delays) - 1)])
    return False, last
