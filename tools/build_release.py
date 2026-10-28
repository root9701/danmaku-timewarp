# -*- coding: utf-8 -*-
"""build_release.py — 打便携包(轻量版 / 完整版)。

用法:
    python tools/build_release.py --slim          # 不含 numpy + ffmpeg(约 63MB)
    python tools/build_release.py --full          # 含自动校准(约 150MB+)
    python tools/build_release.py --slim --zip    # 额外打 zip

为什么是"两个包一份代码":
  自动校准要 numpy(51MB) + imageio-ffmpeg(84MB), 而播放、弹幕、手动微调 offset
  **一个都不需要**。代码里本来就有 try/except ImportError 降级(HAS_AUDIOFP),
  所以"轻量版"只是打包时少放两个目录 —— 不是两套代码, 维护成本接近零。

  实测(拔掉 numpy/ffmpeg 起服务): /api/version、/api/days、/api/matchlist 全 ok,
  选场次能读到 align/ 里的 offset(3122.393), 点播放 currentTime 在推进、
  duration=3051、readyState=4、98 条弹幕在滚 —— 轻量版**真的能播**。

为什么打包便携 Python 而不是要求用户自己装:
  用户是小红书上的普通玩家。让 TA 先装 Python、勾 PATH、跑 pip 装 150MB 依赖,
  绝大多数人会在第二步放弃。整包自带解释器, 解压双击就能用。
  代价只是 31MB —— 比"装不上"便宜得多。

为什么**不含 data/**:
  数据靠面板里的「检查更新」下载(镜像降级链, 见 srcfetch.py)。这样:
    - 包体小(数据 21MB 且会一直涨, 一届 Worlds 约 300MB)
    - 新数据不必重新发版
    - 作者不用为"用户手里是旧数据"负责
  首次启动时 data/ 是空的, 程序必须能正常起来(已验: 空 data/ 下 /api/days 返回空列表)。
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "danmaku-server"
DIST = ROOT / "dist"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

# 轻量版**不要**的依赖。名字与 site-packages 里的顶层项一致。
FULL_ONLY = ("numpy", "numpy.libs", "imageio_ffmpeg")

# 便携 Python 需要的最小集合(实测 31MB):
#   DLLs(15MB) = 运行时; Lib(20MB, 含 stdlib); python.exe + python312.dll 等
# 排除: test/idlelib/tkinter/venv/ensurepip/pydoc_data(用不到)、site-packages(自己装)
STDLIB_SKIP = ("test", "idlelib", "tkinter", "venv", "ensurepip", "pydoc_data",
               "site-packages", "__pycache__", "lib2to3", "turtledemo")

# 启动脚本(UTF-8 无 BOM + CRLF, 由 .gitattributes 固定 *.bat 为 CRLF)
LAUNCH_BAT = """@echo off
title 弹幕时光机 - 本机访问（关闭本窗口即停止）
cd /d "%~dp0"
echo ==============================================
echo   弹幕时光机 {ver}
echo   关闭本窗口 = 停止服务
echo ==============================================
echo.
rem 用包内自带的解释器, 不依赖系统装了 Python。
rem 显式覆盖 host: 用户可能从局域网模式切回来, 不自造 0.0.0.0。
set DANMAKU_HOST=127.0.0.1
set PYTHONIOENCODING=utf-8
"%~dp0python\\python.exe" "%~dp0danmaku-server\\server.py"
echo.
echo 服务已停止。
pause
"""

# 局域网版。为什么便携包必须有它:
#   server.py 在仅本机模式下会打印"要让手机/iPad 看, 请用「启动回放台_局域网.bat」",
#   而打包时**删掉了仓库里的那几个 .bat**。若不补这一份, 包里就只有本机模式,
#   提示指向一个不存在的文件 —— 用户按提示找不到东西, 局域网能力等于被砍掉。
# 与仓库版行为一致: 只差 DANMAKU_HOST=0.0.0.0(绑定全部网卡 -> 打印二维码)。
LAN_BAT = """@echo off
title 弹幕时光机 - 局域网访问（关闭本窗口即停止）
cd /d "%~dp0"
echo ==============================================
echo   弹幕时光机 {ver} · 局域网访问
echo   手机/iPad 和电脑连同一个 WiFi，扫下方二维码
echo   关闭本窗口 = 停止服务
echo ==============================================
echo.
set DANMAKU_HOST=0.0.0.0
set PYTHONIOENCODING=utf-8
"%~dp0python\\python.exe" "%~dp0danmaku-server\\server.py"
echo.
echo 服务已停止。
pause
"""


def _size_mb(p: Path) -> float:
    if p.is_file():
        return p.stat().st_size / 1024 / 1024
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total / 1024 / 1024


def _py_base() -> Path:
    """当前 Python 的安装根目录(venv 里跑也能拿到真实的 base)。"""
    import sysconfig
    if sys.base_prefix:
        return Path(sys.base_prefix)
    return Path(sysconfig.get_paths()["stdlib"]).parent


def copy_python(dst: Path) -> None:
    """复制一份能独立运行的 Python 到 dst/python/。"""
    base = _py_base()
    out = dst / "python"
    out.mkdir(parents=True, exist_ok=True)
    # 根目录的 exe / dll(vcruntime 也在根, 少了它启动直接报错)
    for f in base.glob("*.exe"):
        shutil.copy2(f, out / f.name)
    for f in base.glob("*.dll"):
        shutil.copy2(f, out / f.name)
    for sub in ("DLLs", "Lib", "libs"):
        src = base / sub
        if not src.is_dir():
            continue
        shutil.copytree(
            src, out / sub,
            ignore=shutil.ignore_patterns(*STDLIB_SKIP),
            dirs_exist_ok=True)


def copy_deps(dst: Path, full: bool) -> list:
    """把 venv 里的第三方包复制进便携 Python。

    为什么不复制整个 site-packages: 里面还有 pip(10MB)、pyzbar(1.1MB, 本机另一个
    项目的遗留)、brotli(采集端才用) —— 播放端一个都不需要。
    显式列举而不是"排除法": 新增依赖时忘了放进去会立刻 ImportError, 比悄悄多带
    50MB 更容易发现。
    """
    venv_sp = SERVER / ".venv" / "Lib" / "site-packages"
    if not venv_sp.is_dir():
        raise SystemExit(
            f"找不到 {venv_sp}\n"
            f"先在 danmaku-server/ 里建好 .venv 并装依赖(点一次启动脚本即可)")
    dst_sp = dst / "python" / "Lib" / "site-packages"
    dst_sp.mkdir(parents=True, exist_ok=True)

    wanted = ["aiohttp", "aiohappyeyeballs", "aiosignal", "attr", "attrs",
              "frozenlist", "idna", "multidict", "propcache", "yarl",
              "tzdata", "qrcode", "colorama"]
    if full:
        wanted += list(FULL_ONLY)

    copied = []
    for name in wanted:
        hits = [p for p in venv_sp.glob(name + "*")
                if p.name.endswith((".dist-info", ".data")) is False]
        if not hits:
            if full and name in FULL_ONLY:
                raise SystemExit(f"完整版缺少依赖 {name}, 先 pip install -r requirements.txt")
            continue                      # 可选依赖(如 qrcode)没有也能跑
        for h in hits:
            target = dst_sp / h.name
            if h.is_dir():
                shutil.copytree(h, target, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(h, target)
            copied.append(h.name)
    # dist-info 要带上(有些包靠它读版本号), 但不再是复制主体
    for pattern in ("*.dist-info", "*.data"):
        for d in venv_sp.glob(pattern):
            stem = d.name.split("-")[0].lower()
            if stem in {c.lower() for c in copied} or stem in {
                    "aiohttp", "attrs", "yarl", "multidict", "frozenlist",
                    "propcache", "idna", "aiosignal", "aiohappyeyeballs",
                    "tzdata", "qrcode", "colorama", "numpy", "imageio_ffmpeg"}:
                if d.is_dir():
                    shutil.copytree(d, dst_sp / d.name, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("__pycache__"))
    return copied


def _check_deps(dst: Path, full: bool) -> list:
    """用**包里那份解释器**核对依赖, 而不是靠"文件看起来复制了"。

    为什么必须实测: 手工挑目录极容易漏 —— 实测漏了 aiohappyeyeballs 时 aiohttp
    直接 ImportError, 而源码文件全都在。症状是用户双击后窗口一闪而过,
    作者机器上却不复现(因为作者有系统 Python 兜底)。

    轻量版还要反查 numpy 是否混入: 混进来包会白大 51MB, 而功能上完全看不出来。
    """
    py = dst / "python" / "python.exe"
    if not full:
        for junk in FULL_ONLY:
            if (dst / "python" / "Lib" / "site-packages" / junk).exists():
                return [f"轻量版混入了 {junk}, 包体白白变大"]
    mods = ["aiohttp"]
    if full:
        mods += ["numpy", "imageio_ffmpeg"]
    code = "import " + ", ".join(mods) + "\nprint('deps-ok')\n"
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return [f"导入失败({','.join(mods)}): {r.stderr.strip()[:200]}"]
    return []


def copy_code(dst: Path) -> None:
    """复制播放端代码 + align/。**不含 data/、不含 .venv、不含 matches.json**。"""
    shutil.copytree(
        SERVER, dst / "danmaku-server",
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "matches.json", "config.json",
            "*.pyc", "*.log"),
        dirs_exist_ok=True)
    # 启动脚本在包根, 替掉原来那套"建 venv 装依赖"的脚本(便携版不需要)
    for old in ("_ensure_venv.bat", "启动回放台.bat", "启动回放台_局域网.bat",
                "退出回放台.bat"):
        (dst / "danmaku-server" / old).unlink(missing_ok=True)
    for name, tpl in (("启动弹幕时光机.bat", LAUNCH_BAT),
                      ("启动弹幕时光机_局域网.bat", LAN_BAT)):
        (dst / name).write_text(
            tpl.replace("{ver}", VERSION), encoding="utf-8", newline="\r\n")
    for name in ("VERSION", "README.md"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, dst / name)


def write_notice(dst: Path, full: bool) -> None:
    """包内说明。用户解压后第一眼看到的就是它。"""
    calib = ("可以（含 numpy + ffmpeg）" if full
             else "**不可用**（轻量版未包含校准依赖；数据已由作者校准好，直接看即可）")
    text = f"""弹幕时光机 v{VERSION} — {'完整版' if full else '轻量版'}
==============================================

怎么用
------
1. 解压到**任意目录**（路径别太深，中文可以）
2. 双击「启动弹幕时光机.bat」
3. 浏览器会自动打开；没打开就手动访问 http://127.0.0.1:8789
4. 第一次要先下载数据：点底栏右侧的「检查更新」

关闭：关掉那个黑窗口就行。

用手机 / iPad 看
----------------
双击「启动弹幕时光机_局域网.bat」，窗口里会显示二维码，手机和电脑
连同一个 WiFi，扫码即可。

手机打不开的话，多半是 Windows 防火墙没放行。用【管理员】PowerShell
执行一次（只需一次）：

  New-NetFirewallRule -DisplayName "弹幕时光机" -Direction Inbound -Protocol TCP -LocalPort 8789 -Action Allow -Profile Private

（只在本机看就用「启动弹幕时光机.bat」，它不开放局域网。）

数据要自己下载
--------------
包里**不带比赛数据**（否则一个包就几百 MB，而且新数据一出来就得重下）。
首次使用点底栏的「检查更新」，会从公共镜像下载，速度约 1-2 MB/s。

自动校准：{calib}

出问题
------
- 下不到数据：多半是网络。稍等再试，或换个时间。
- 提示"数据校验失败"：说明数据源有问题，不是你的电脑坏了，过会儿再试。
- 端口 8789 被占用（提示"已有一个回放台在运行"）：可能上次的窗口没关，
  先关掉旧窗口。要换端口就复制一份 danmaku-server\\config.example.json
  改名为 config.json，改里面的 port。

详见 README.md。
"""
    (dst / "使用说明.txt").write_text(text, encoding="utf-8", newline="\r\n")


def make_zip(src: Path, out: Path) -> None:
    """打 zip。用 deflate —— 代码与 py/pyd 都能压, 实测总压缩率约 30%。"""
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src.parent))
    print(f"  zip: {out.name}  {_size_mb(out):.0f} MB")


def verify(dst: Path, full: bool) -> bool:
    """冒烟测试: 便携解释器能否 import 服务端模块, 且能力标志符合预期。

    必须实测而不是"看起来对": 便携 Python 少一个 dll、site-packages 少一个包,
    症状都是用户双击后窗口一闪而过 —— 在作者机器上不易复现, 因为作者有系统 Python。

    还要断言 **HAS_AUDIOFP 与包类型一致**: 完整版若漏了 numpy, 用户点了自动校准
    才报错(而那时已经在看比赛了); 轻量版若多带了 numpy, 包白白大 51MB。
    这两种"打包漏了/多了"都不会让服务起不来, 只能靠这里拦。
    """
    problems = _check_deps(dst, full)
    if problems:
        print(f"  ✗ {problems[0]}")
        return False
    py = dst / "python" / "python.exe"
    if not py.is_file():
        print("  ✗ 便携 Python 不存在")
        return False
    # 两个启动脚本必须都在。这个检查不是形式主义: 曾经打包时删掉了仓库里的
    # 局域网 .bat 却没补新的, 包里只剩本机模式, 而服务端提示还指向那个不存在的
    # 文件 —— 用户按提示找不到东西, 局域网能力等于被砍掉, 且构建全绿。
    for bat in ("启动弹幕时光机.bat", "启动弹幕时光机_局域网.bat"):
        if not (dst / bat).is_file():
            print(f"  ✗ 缺启动脚本: {bat}")
            return False
    expect = "True" if full else "False"
    code = (
        "import sys, json, asyncio, gzip, urllib.request, sqlite3, ssl\n"
        "import aiohttp\n"
        f"sys.path.insert(0, {str(dst / 'danmaku-server')!r})\n"
        "import server\n"
        "print('  version =', server.APP_VERSION)\n"
        "print('  HAS_AUDIOFP =', server.HAS_AUDIOFP)\n"
        "print('  HAS_DATAUPDATE =', server.HAS_DATAUPDATE)\n"
        "print('  DATA_DIR =', server.DATA_DIR.name)\n"
        f"assert server.HAS_AUDIOFP is {expect}, "
        f"'HAS_AUDIOFP 应为 {expect}, 实际 %r' % (server.HAS_AUDIOFP,)\n"
        "assert server.HAS_DATAUPDATE is True, 'dataupdate 不可用'\n"
        "print('  asserts-ok')\n"
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    print(r.stdout.rstrip() or f"  ✗ 无输出, stderr={r.stderr[:300]}")
    return r.returncode == 0


def build(full: bool, do_zip: bool) -> int:
    tag = "full" if full else "slim"
    name = f"danmaku-timewarp-{VERSION}-{tag}"
    dst = DIST / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    print(f"打{'完整版' if full else '轻量版'} v{VERSION}")
    print("  1/4 复制便携 Python ...")
    copy_python(dst)
    print("  2/4 复制依赖 ...")
    pkgs = copy_deps(dst, full)
    print(f"      {len(pkgs)} 个包")
    print("  3/4 复制代码 ...")
    copy_code(dst)
    write_notice(dst, full)
    print("  4/4 冒烟测试 ...")
    ok = verify(dst, full)

    print(f"\n  {name}/  {_size_mb(dst):.0f} MB")
    if do_zip:
        make_zip(dst, DIST / f"{name}.zip")
    if not ok:
        print("\nFAIL: 冒烟测试未通过, 包不可用")
        return 1
    print("\nOK: 包已生成")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--slim", action="store_true", help="轻量版(不含自动校准)")
    g.add_argument("--full", action="store_true", help="完整版(含自动校准)")
    ap.add_argument("--zip", action="store_true", help="额外打 zip")
    args = ap.parse_args()
    DIST.mkdir(exist_ok=True)
    return build(args.full, args.zip)


if __name__ == "__main__":
    sys.exit(main())
