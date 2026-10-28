# -*- coding: utf-8 -*-
"""release.py — 一条命令做完整发布: 校验 -> 出包 -> 端到端实测 -> (可选)推送上架。

用法:
    python tools/release.py                    # 只在本机出包并验证, 不碰网络
    python tools/release.py --publish          # 再多做: 打 tag + 推送 + 建 Release 传包
    python tools/release.py --publish --dry-run  # 把 --publish 要做的事打印出来, 什么都不动

为什么需要它:
  发布原本是七八条命令的顺序劳动(清 dist / 跑闸门 / 跑回归 / 重生成清单 /
  打两个包 / 拿空 APPDATA 试一遍 / 打 tag / 传包)。漏掉任何一步的后果不对称:

    - 漏跑 pack_data --index  -> 用户端「检查更新」拉到的还是旧清单, 永远收不到新数据
    - 漏掉空 APPDATA 实测     -> 包在作者机器上是好的, 用户双击窗口一闪而过
                                 (实测就发生过: 漏了 typing_extensions, 作者机器上
                                  靠用户级 site-packages 侥幸兜住, 一直没发现)
    - 漏推 tag / 漏传包       -> 界面提示"有新版本", 用户点过去 404

  所以这些步骤不该靠人记。本脚本把它们串成一条链, **任何一步失败就停在那里**,
  不继续往下做 —— 发布到一半的产物比没有产物更难收拾。

发布顺序为什么是"先本地全绿, 再碰网络":
  打 tag 与推送是**不可撤销**的动作(历史会留在 GitHub 上)。先在本机把两个包都
  出好、实测能跑, 再推 —— 推的时候手上已经握着验证过的产物, 而不是推完才发现
  包是坏的。
"""
import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
TAG = "v" + VERSION
GH_REMOTE = "origin"
GH_REPO = "root9701/danmaku-timewarp"

# 出包的种类。顺序固定: 轻量版先出 —— 体积小、出错早暴露。
KINDS = ("slim", "full")


class StepFailed(Exception):
    """某一步失败。消息直接给用户看, 带该做什么。"""


def say(msg=""):
    print(msg, flush=True)


def _system_proxy() -> str:
    """Windows 系统代理(如 http://127.0.0.1:7897), 没配返回空串。

    为什么要读注册表: `git` 与 `gh` 都**不读** Windows 的"设置 -> 网络 -> 代理"，
    只认 HTTPS_PROXY/HTTP_PROXY 环境变量。而实测本机 github.com 直连 15 秒超时、
    设了代理 2.7 秒通 —— 于是"代理开着、浏览器正常、git push 却 Connection reset"
    这种状态很常见, 且报错完全不提代理。这里系统代理开着就自动沿用,
    与浏览器/git 的既有行为保持一致, 不让用户为发布脚本单独记一套配置。
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


def _tool_env() -> dict:
    """给 git/gh 用的环境: 补上系统代理。

    只影响本脚本派生的子进程, 不改用户的环境变量、也不写任何配置 ——
    用你的机器可以随便折腾, 但别把设备状态改坏。
    """
    env = dict(os.environ)
    px = _system_proxy()
    if px:
        env["HTTPS_PROXY"] = px
        env["HTTP_PROXY"] = px
    return env


def _run(cmd, cwd=None, env=None, timeout=1800):
    """跑一条命令, 返回 (ok, 合并输出)。**不抛异常** —— 由调用方决定怎么报。

    找不到可执行文件时返回 (False, 说明) 而不是让 FileNotFoundError 冒出去:
    发布脚本的每处失败都该给出"下一步做什么", 而不是一段 traceback。
    """
    try:
        r = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env if env is not None else _tool_env(),
                           timeout=timeout)
    except FileNotFoundError:
        return False, f"找不到可执行文件: {cmd[0]}（不在 PATH 里?）"
    except subprocess.TimeoutExpired:
        return False, f"命令超时({timeout}s): {' '.join(map(str, cmd[:3]))}"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()


def _git(*args, timeout=120):
    return _run(["git", *args], timeout=timeout)


def _git_token() -> str:
    """从 git 的凭据存储里取出 GitHub token, 给 gh 用。

    为什么这么做: 用户已经用 git 推送过, 凭据(repo scope)就躺在
    credential manager 里。而 `gh auth login` 要**交互式**走一遍设备码流程,
    在国内还要额外给 gh 配代理 —— 明明手上已经有一个能用的 token, 却让人
    重做一遍授权, 是纯粹的重复劳动。
    取不到就返回空串(那种情况才需要 gh auth login)。
    """
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, env=_tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def _gh_env() -> dict:
    """gh 的运行环境: 补代理 + 尽量补 token。

    显式传 GH_TOKEN 的好处: 不必要求用户 `gh auth login`。git 里那份凭据
    与 gh 用的是同一个 GitHub 账号, 权限也够(需要 repo 与 workflow)。
    """
    env = _tool_env()
    if not (env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")):
        tok = _git_token()
        if tok:
            env["GH_TOKEN"] = tok
    return env


def _py(*args, timeout=1800):
    return _run([sys.executable, *args], timeout=timeout)


# ---------------- 1. 前置检查 ----------------

def check_clean_tree():
    """工作区必须干净。带着未提交改动打的包, 无法从任何 tag 复现。"""
    ok, out = _git("status", "--porcelain")
    if not ok:
        raise StepFailed(f"git status 失败:\n{out}")
    if out.strip():
        raise StepFailed(
            "工作区有未提交的改动, 不能发布(打出来的包无法从 tag 复现):\n"
            + "\n".join("  " + ln for ln in out.splitlines()[:12]))
    say("  ✓ 工作区干净")


def check_version_shape():
    """版本号必须形如 1.2.3。tag 与 VERSION 不一致是最常见的发布事故。"""
    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        raise StepFailed(
            f"VERSION 内容不像版本号: {VERSION!r}（应为 主.次.修，如 1.2.0）")
    say(f"  ✓ 版本 {VERSION}")


def check_tag_available(allow_existing=False):
    """本地与远端都不能已有同名 tag —— 覆盖 tag 会让已下载的用户对不上号。

    唯一的例外: 本地有 tag 但**远端没有**。那说明上次发布卡在"打完 tag、
    还没推"这一步(断网/代理), 此时 tag 指向的正是当前提交, 重试应当允许 ——
    否则用户只能手动 `git tag -d` 才能继续, 而删 tag 正是最容易被误做成
    "覆盖已发布版本"的动作。
    """
    ok, out = _git("tag", "--list", TAG)
    if not ok:
        raise StepFailed(f"查本地 tag 失败:\n{out}")
    local_has = bool(out.strip())
    ok, out = _git("ls-remote", "--tags", GH_REMOTE, f"refs/tags/{TAG}")
    if not ok:
        raise StepFailed(
            f"查远端 tag 失败(网络?):\n{out}\n"
            "  无法确认远端是否已有同名 tag 时不能继续 —— 覆盖已发布的 tag 会"
            "让已下载的用户对不上号。")
    remote_has = TAG in out

    if remote_has and not allow_existing:
        raise StepFailed(
            f"远端已有 tag {TAG}, 不能覆盖。先升 VERSION 再发。\n"
            f"  （发布过的版本不能重发: 用户手里那版会与 tag 指向的内容对不上）")
    if local_has and not remote_has:
        # 上次推到一半。确认它确实指向当前 HEAD, 才能放心复用。
        ok, head = _git("rev-parse", "HEAD")
        ok2, tag_commit = _git("rev-list", "-n", "1", TAG)
        if not (ok and ok2 and head.strip() == tag_commit.strip()):
            raise StepFailed(
                f"本地 tag {TAG} 指向的不是当前提交, 且远端还没有这个 tag。\n"
                f"  这通常意味着上次发布在不同的提交上中断了。先确认:\n"
                f"    git log --oneline -3\n"
                f"    git rev-parse {TAG}^0\n"
                f"  确认后可删掉重打: git tag -d {TAG}")
        say(f"  ! 本地已有 tag {TAG} 但远端没有(上次未推成功), 将复用")
        return
    say(f"  ✓ tag {TAG} 未被占用")


def _find_gh():
    """定位 gh 可执行文件。

    为什么要自己找: winget 安装后写的是**机器级 PATH**, 而已经打开的终端/进程
    （包括刚装完就在这个会话里跑脚本）不会自动刷新环境变量。此时 `gh` 明明装了,
    subprocess 却报 WinError 2 —— 报错信息会把人引向"没装", 而不是"重启一下终端"。
    """
    exe = shutil.which("gh")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                 r"C:\Program Files", r"C:\Program Files (x86)"):
        if not base:
            continue
        p = Path(base) / "GitHub CLI" / "gh.exe"
        if p.is_file():
            return str(p)
    return ""


def _proxy_hint() -> str:
    """没登录时, 说清"为什么 gh auth login 会卡"以及该怎么办。

    `gh auth login` 是**用户在自己终端里跑**的, 那里的环境与本脚本无关 ——
    本脚本会自动补代理, 但补不到用户手敲的那一条命令。所以这里要给出可以直接
    粘贴的两行。

    实测本机: github.com 直连 15 秒超时, 而 api.github.com 直连 0.55 秒正常。
    两个域名解析到同一段的 IP 却表现不同, 所以"能上网"不代表 gh 能登录。
    """
    px = _system_proxy() or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"
    return (
        "\n  gh 不读 Windows 系统代理, 只认 HTTPS_PROXY/HTTP_PROXY 环境变量。\n"
        f"  你那台机器上 github.com 直连会超时, 请先设好再登录"
        f"(本脚本会自动补 {px}, 但你手敲的那条命令不在本脚本里):\n"
        f'    $env:HTTPS_PROXY="{px}"\n'
        f'    $env:HTTP_PROXY="{px}"\n'
        "    gh auth login")


def check_gh():
    """--publish 需要 gh 且已登录。失败要早报, 而不是打完包才发现传不了。"""
    gh = _find_gh()
    if not gh:
        raise StepFailed(
            "--publish 需要 GitHub CLI(gh), 当前找不到。\n"
            "  安装: winget install --id GitHub.cli -e\n"
            "  装完**新开一个终端**再跑（PATH 是安装时写入的，旧终端不会刷新）\n"
            "  登录: gh auth login\n"
            "  只想在本机出包就别加 --publish")
    ok, out = _run([gh, "--version"], timeout=60)
    if not ok:
        raise StepFailed(f"gh 存在但跑不起来: {gh}\n{out[:300]}")
    env = _gh_env()
    ok, out = _run([gh, "auth", "status"], env=env, timeout=60)
    if not ok:
        raise StepFailed(
            "gh 没有可用凭据, 传不了包。两种解法(任选):\n"
            "  A. 直接用 git 里那份: 本脚本已尝试过, 但没取到。先确认\n"
            "     `git credential fill` 能返回 password(说明推送凭据还在)\n"
            "  B. 走 gh 自己的登录(交互式, 需要你本人在终端里点完):"
            + _proxy_hint())
    if env.get("GH_TOKEN") and "GH_TOKEN" in out:
        say("  ✓ gh 可用（凭据复用自 git, 无需单独登录）")
    else:
        say(f"  ✓ gh 已登录 ({gh})")
    return gh


# ---------------- 2. 质量与数据 ----------------

def run_quality():
    ok, out = _py("tools/quality_gate.py")
    tail = "\n".join(out.splitlines()[-4:])
    if not ok:
        raise StepFailed(f"质量闸门未通过:\n{tail}")
    say(f"  ✓ 闸门\n" + "\n".join("      " + ln for ln in tail.splitlines()))


def run_tests():
    ok, out = _py("tests/run_all.py")
    tail = "\n".join(out.splitlines()[-2:])
    if not ok:
        raise StepFailed(f"回归未通过:\n{out[-2000:]}")
    say(f"  ✓ 回归 {tail.strip()}")


def refresh_index():
    """重生成 data-index.json。**必须在打包之前** —— 包内不带清单,
    但用户端「检查更新」拉的是仓库里这份, 忘了更新就等于没发布新数据。"""
    ok, out = _py("tools/pack_data.py", "--index")
    if not ok:
        raise StepFailed(f"生成清单失败:\n{out[-1500:]}")
    say("  ✓ " + " / ".join(out.splitlines()[:2]))


# ---------------- 3. 出包 ----------------

def build(kind):
    tag = "" if kind == "slim" else ""      # 参数用 --slim / --full
    ok, out = _py("tools/build_release.py", f"--{kind}", "--zip")
    if not ok:
        raise StepFailed(f"打 {kind} 包失败:\n{out[-2500:]}")
    z = DIST / f"danmaku-timewarp-{VERSION}-{kind}.zip"
    if not z.is_file():
        raise StepFailed(f"打完却没找到 {z.name}")
    say(f"  ✓ {z.name}  {z.stat().st_size / 1024 / 1024:.1f} MB")
    return z


def commit_index(dry_run):
    """把重生成的清单提交掉。**漏这一步就等于没发布新数据。**

    为什么必须单独提交: publish() 只做 `git push`, 而**未提交的工作区改动不会被
    推送**。若清单只在工作区被改写, tag 指向的提交里仍是旧清单 —— 用户端
    「检查更新」拉到的旧清单会认为"没有新数据", 而作者以为发过了。
    这正是本脚本要防的那类"看起来发成功了、其实没有"的事故。
    """
    ok, dirty = _git("status", "--porcelain", "data-index.json")
    if not ok:
        raise StepFailed(f"查清单状态失败:\n{dirty}")
    if not dirty.strip():
        say("  ✓ 清单无变化")
        return
    if dry_run:
        say("  [dry-run] 将要提交 data-index.json")
        return
    ok, out = _git("add", "data-index.json")
    if not ok:
        raise StepFailed(f"git add 清单失败:\n{out}")
    ok, out = _git("commit", "-m",
                   f"chore(数据): 重生成 data-index.json（{VERSION}）",
                   "-m", "发布前由 tools/release.py --index 重生成。\n"
                         "提交它, 否则 tag 指向的提交里仍是旧清单, 用户端拉不到新数据。")
    if not ok:
        raise StepFailed(f"提交清单失败:\n{out}")
    say("  ✓ 已提交 data-index.json")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _probe(url, timeout=8, tries=30):
    """等服务起来。aiohttp 启动 + 读目录需要一点时间, 不能只探一次。"""
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, r.read()
        except Exception as e:               # noqa: BLE001 —— 起来前必然连不上
            last = e
            time.sleep(1)
    raise StepFailed(f"端到端探测超时: {url}（{last}）")


def e2e(kind, zip_path):
    """把 zip 解到临时目录, 用**空 APPDATA** 启动, 探测接口后关掉。

    这是唯一能拦住"作者机器上好好的、用户一开就崩"的检查。两条实测教训:
      1. 便携解释器默认会读 %APPDATA%\\Python\\Python312\\site-packages, 且优先级
         高于包内目录 —— 作者机器上装着同名包, 就测不出"包里漏了依赖"。
      2. 依赖清单漏 copy typing_extensions 时, 上面的污染正好把它兜住, 冒烟测试全绿。
    所以这里把 APPDATA / LOCALAPPDATA 指向空目录, 让包只能用自己那份。
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"e2e-{kind}-"))
    proc = None
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        # zip 顶层有一层同名目录
        root = next(p for p in tmp.iterdir() if p.is_dir())
        port = _free_port()
        (root / "danmaku-server" / "config.json").write_text(
            '{"port": %d}' % port, encoding="utf-8")
        blank = tmp / "blank-appdata"
        blank.mkdir()
        env = dict(os.environ)
        env.update({
            "APPDATA": str(blank), "LOCALAPPDATA": str(blank),
            "PYTHONNOUSERSITE": "1", "NO_BROWSER": "1",
            "PYTHONIOENCODING": "utf-8", "DANMAKU_HOST": "127.0.0.1",
        })
        for k in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(k, None)
        proc = subprocess.Popen(
            [str(root / "python" / "python.exe"), "danmaku-server/server.py"],
            cwd=str(root), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace")
        status, body = _probe(f"http://127.0.0.1:{port}/api/version")
        if status != 200:
            raise StepFailed(f"{kind}: /api/version 返回 {status}")
        if VERSION.encode() not in body:
            raise StepFailed(f"{kind}: 版本号不对, 期望 {VERSION}, 实际 {body[:120]}")
        # 首页必须能打开 —— 服务起来但静态资源 404 也是坏的
        _probe(f"http://127.0.0.1:{port}/")
        say(f"  ✓ {kind}: 空 APPDATA 下启动正常, 版本与首页均通")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 4. 发布(不可撤销动作) ----------------

def publish(zips, dry_run):
    """打 tag + 推 main + 建 Release 传包。**这一步之后内容就公开了。**"""
    if dry_run:
        say("\n[dry-run] 将要执行:")
        say(f"  git tag -a {TAG} -m '...'")
        say(f"  git push {GH_REMOTE} main --follow-tags")
        say(f"  gh release create {TAG} <两个 zip> --title ... --notes ...")
        for z in zips:
            say(f"    - {z.name}")
        return
    ok, out = _git("tag", "--list", TAG)
    if ok and out.strip():
        say(f"  ✓ 复用已存在的本地 tag {TAG}")
    else:
        ok, out = _git("tag", "-a", TAG, "-m", f"弹幕时光机 {VERSION}")
        if not ok:
            raise StepFailed(f"打 tag 失败:\n{out}")
        say(f"  ✓ 已打 tag {TAG}")
    ok, out = _git("push", GH_REMOTE, "main", "--follow-tags", timeout=600)
    if not ok:
        raise StepFailed(
            f"推送失败:\n{out}\n"
            "  网络/代理问题不是授权问题 —— 换个时间再试, 不要把 tag 删了重来。\n"
            f"  当前状态: 本地已有 tag {TAG}, 仍是「已提交未推送」。")
    say("  ✓ 已推送 main 与 tag")
    notes = (
        f"## 弹幕时光机 {VERSION}\n\n"
        "配套 B 站官方回放的弹幕播放器。数据与程序分离：本包不含比赛数据，"
        "首次使用在界面内下载。\n\n"
        "### 下载\n\n"
        "| 版本 | 体积（zip） | 自动校准 | 适用场景 |\n"
        "|---|---|---|---|\n"
        f"| 轻量版 | {zips[0].stat().st_size / 1024 / 1024:.0f} MB | 不支持 | "
        "直接观看。校准结果随数据发布，无需本地计算 |\n"
        f"| 完整版 | {zips[1].stat().st_size / 1024 / 1024:.0f} MB | 支持 | "
        "需要自行重算 offset，或校对已有结果 |\n\n"
        "两者代码相同，差异仅在是否打包 numpy 与 ffmpeg。\n\n"
        "### 使用\n\n"
        "解压至任意目录（路径可含中文），双击 `启动弹幕时光机.bat`，"
        "浏览器自动打开 `http://127.0.0.1:8789`。\n\n"
        "需要在手机或平板观看时，改用 `启动弹幕时光机_局域网.bat`，"
        "终端会显示二维码。若手机无法访问，需先放行 Windows 防火墙的 8789 端口，"
        "命令见包内 `使用说明.txt`。\n\n"
        "首次运行请点击底栏「检查更新」下载比赛数据，单晚约 17 MB。\n\n"
        "### 本版内容\n\n"
        "**播放**\n\n"
        "- 单击画面播放／暂停，双击全屏；双击不会改变播放状态\n"
        "- 音量改为悬停展开的竖向滑块\n"
        "- 「自动校准」并入「更多」，与手动微调相邻\n"
        "- 视频直链失效时自动重新获取签名链接\n\n"
        "**弹幕**\n\n"
        "- 默认显示区域改为半屏，字号改为小，默认不描边；新增 1/4 屏档位\n"
        "- 连播默认开启\n"
        "- 修复弹幕列表底部出现横向滚动条（连续全角标点撑宽容器）\n\n"
        "**总览**\n\n"
        "- 折叠状态显示时间刻度，总高度不变\n"
        "- 修复展开分P 时上方图表被拉伸\n\n"
        "**防剧透**\n\n"
        "- 修复局数在底栏与页面标题中原样泄露\n\n"
        "**更新与打包**\n\n"
        "- 程序版本改从 Releases 读取，不再依赖数据清单的生成时机\n"
        "- 修复便携版在未安装相关 Python 包的机器上无法启动\n"
        "- 便携包补回局域网启动脚本\n\n"
        "### 已知限制\n\n"
        "- 「检查更新」只下载数据，不替换程序文件。程序更新需重新下载本页附件。\n"
        "- 轻量版不包含自动校准；界面会给出提示，不会静默失败。\n\n"
        "数据与对齐结果为作者采集产物，版权归原发布者及哔哩哔哩所有，"
        "本项目不主张任何权利。"
    )
    gh = _find_gh() or "gh"
    ok, out = _run([gh, "release", "create", TAG, *[str(z) for z in zips],
                    "--repo", GH_REPO, "--title", f"弹幕时光机 {VERSION}",
                    "--notes", notes], timeout=3600, env=_gh_env())
    if not ok:
        raise StepFailed(
            f"建 Release 失败(代码与 tag 已推上去):\n{out}\n"
            f"  可手动补: gh release create {TAG} <zip 路径> --repo {GH_REPO}")
    say(f"  ✓ Release {TAG} 已发布, 附件 {len(zips)} 个")


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true",
                    help="打完包后打 tag / 推送 / 建 Release（对外可见，不可撤销）")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --publish: 只打印将要执行的动作")
    ap.add_argument("--skip-tests", action="store_true",
                    help="跳过回归（不建议；仅用于验证发布脚本本身）")
    args = ap.parse_args()

    steps = ["前置检查", "质量与数据", "出包", "端到端实测"]
    if args.publish:
        steps.append("发布")
    say(f"== 发布 {VERSION}（{'会推送到远端' if args.publish else '仅本机'}）==\n")

    try:
        say("[1/{}] {}".format(len(steps), steps[0]))
        check_clean_tree()
        check_version_shape()
        check_tag_available()
        if args.publish:
            check_gh()

        say("\n[2/{}] {}".format(len(steps), steps[1]))
        run_quality()
        if not args.skip_tests:
            run_tests()
        refresh_index()
        # 清单重生成后**必须提交**, 而且要放在端到端实测通过之后 ——
        # 实测失败时不该在历史里留下一个"为坏版本准备的清单"提交。
        # 所以提交动作挪到 [4] 之后, 见下方。

        say("\n[3/{}] {}".format(len(steps), steps[2]))
        zips = [build(k) for k in KINDS]

        say("\n[4/{}] {}".format(len(steps), steps[3]))
        for k, z in zip(KINDS, zips):
            e2e(k, z)

        # 两个包都实测通过, 才把清单落进历史。此时工作区必须干净 ——
        # 发布要推的是一个完整、可复现的状态。
        say("")
        commit_index(args.dry_run)
        ok, dirty = _git("status", "--porcelain")
        if dirty.strip() and not args.dry_run:
            raise StepFailed(
                "发布前工作区仍有未提交改动:\n"
                + "\n".join("  " + ln for ln in dirty.strip().splitlines()[:10]))

        if args.publish:
            say("\n[5/{}] {}".format(len(steps), steps[4]))
            publish(zips, args.dry_run)
    except StepFailed as e:
        say(f"\nFAIL: {e}")
        return 1

    say(f"\nOK: {VERSION} 已出包{' 并发布' if args.publish and not args.dry_run else ''}")
    for z in zips:
        say(f"  {z.relative_to(ROOT)}  {z.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
