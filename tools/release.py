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


def _run(cmd, cwd=None, env=None, timeout=1800):
    """跑一条命令, 返回 (ok, 合并输出)。**不抛异常** —— 由调用方决定怎么报。"""
    r = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()


def _git(*args, timeout=120):
    return _run(["git", *args], timeout=timeout)


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
    """本地与远端都不能已有同名 tag —— 覆盖 tag 会让已下载的用户对不上号。"""
    ok, out = _git("tag", "--list", TAG)
    if ok and out.strip() and not allow_existing:
        raise StepFailed(
            f"本地已有 tag {TAG}。发布过的版本不能重发:\n"
            f"  要么升 VERSION(改行为/接口), 要么删掉误打的 tag: git tag -d {TAG}")
    ok, out = _git("ls-remote", "--tags", GH_REMOTE, f"refs/tags/{TAG}")
    if ok and TAG in out:
        raise StepFailed(f"远端已有 tag {TAG}, 不能覆盖。先升 VERSION 再发。")
    say(f"  ✓ tag {TAG} 未被占用")


def check_gh():
    """--publish 需要 gh 且已登录。失败要早报, 而不是打完包才发现传不了。"""
    ok, out = _run(["gh", "--version"], timeout=60)
    if not ok:
        raise StepFailed(
            "--publish 需要 GitHub CLI(gh), 当前不可用。\n"
            "  安装: winget install --id GitHub.cli -e\n"
            "  登录: gh auth login\n"
            "  只想在本机出包就别加 --publish")
    ok, out = _run(["gh", "auth", "status"], timeout=60)
    if not ok:
        raise StepFailed(
            "gh 没登录, 传不了包。先执行:\n  gh auth login")
    say("  ✓ gh 已登录")


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
        f"- 轻量版(`-slim`)：不含自动校准，约 "
        f"{zips[0].stat().st_size / 1024 / 1024:.0f} MB\n"
        f"- 完整版(`-full`)：含 numpy + ffmpeg，可自己重算 offset\n\n"
        "两个包都不含比赛数据。解压后双击「启动弹幕时光机.bat」，"
        "首次点底栏「检查更新」下载数据。"
    )
    ok, out = _run(["gh", "release", "create", TAG, *[str(z) for z in zips],
                    "--repo", GH_REPO, "--title", f"弹幕时光机 {VERSION}",
                    "--notes", notes], timeout=3600)
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
        # 重生成清单后工作区可能变脏(数据没变就不会)。这不算错误, 但要让用户知道
        # 它会不会跟着这次提交出去。
        ok, dirty = _git("status", "--porcelain")
        if dirty.strip():
            say("  ! 清单有变化, 记得随本次发布一起提交: "
                + ", ".join(x.split()[-1] for x in dirty.strip().splitlines()[:5]))

        say("\n[3/{}] {}".format(len(steps), steps[2]))
        zips = [build(k) for k in KINDS]

        say("\n[4/{}] {}".format(len(steps), steps[3]))
        for k, z in zip(KINDS, zips):
            e2e(k, z)

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
