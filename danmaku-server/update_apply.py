"""程序自更新: 下载新包、校验、换目录、重启。

**这个文件必须能被"正在运行的旧版"和"刚下载的新版"同时读懂**, 所以只用标准库,
且不 import 本仓任何模块 —— 它会在包被改名一半的时刻被执行, 那一刻周边文件是什么
状态都说不准。

整体流程(为什么必须两段, 见 docs 或 DISTRIBUTION.md):

    [旧程序]  下载 zip -> 校验 -> 写 update.json -> 起 updater 子进程 -> 退出
    [updater] 等端口空出来 -> 旧目录改名 .old -> 新目录改名到位 -> 打印结果
              -> 旧启动脚本用新目录重开(见 Start.bat 的 :relaunch)

单独运行(调试用):

    python update_apply.py --stage <zip> --root <包根> --port 8789

`--stage` 只做"解压到临时目录并校验", 不动正式目录, 用于人工确认包是好的。
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# 这些名字在包根目录里**绝不参与替换**: 用户数据、用户的本地改动、以及更新过程自身。
# 判据是"丢了能不能再生": data/ 里的音频指纹无法再生, config/cookies 是用户手填的,
# 这三样一旦被新包覆盖就永久丢失 —— 所以它们只保留旧版的, 新包里的同名目录直接丢弃。
KEEP_NAMES = {"data", "matches.json", "config.json", "cookies.txt"}


def log(msg: str) -> None:
    """输出给 updater 自己的日志文件。stdout 已经断开了(父进程没了), 不能依赖它。"""
    print(msg, flush=True)


def port_busy(port: int) -> bool:
    """端口上还有人监听吗。

    **不能用 connect_ex 判断**: 在 Windows 上, 子进程连一个本地 listening socket
    可能拿到 10035(WSAEWOULDBLOCK, 非阻塞)或被安全软件静默丢弃, 于是"服务还在跑"
    被误判成"已经退出" —— 实测就是这么翻车的, 结果是在端口仍被占着时开始改目录。

    可靠做法是**自己去 bind+listen 同一个端口**: 成功 = 真的没人占着。用 127.0.0.1
    而不是 0.0.0.0: 后者在 SO_REUSEADDR 存在时可能与别人共用端口而"绑定成功",
    反而漏判(实测两个方向的行为不对称)。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
        except OSError:
            return True
    return False


def wait_port_free(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_busy(port):
            return True
        time.sleep(0.4)
    return not port_busy(port)


def dir_renameable(path: Path, timeout: float = 30.0) -> bool:
    """这个目录现在能不能改名(即: 没有别的进程把它当工作目录/句柄)。

    为什么需要: 端口空出来**不等于**目录能改名。实测两种都会卡住改名(WinError 32):
      - 旧程序是用 `cd 包根 && python server.py` 起的, 它的工作目录就是包根;
      - 早期版本把替换器的 cwd 也设成了包根。
    这两种锁都只持续到对应进程真正退出。与其改名失败后回滚(用户看到"更新失败"),
    不如在这里等到能改名为止; 等不到再报错, 报错信息也直接告诉用户怎么办。

    等待时长可用环境变量 UPDATE_DIR_LOCK_WAIT 缩短 —— 回归测试要用(否则每次慢 30 秒)。
    """
    env = os.environ.get("UPDATE_DIR_LOCK_WAIT", "").strip()
    if env.isdigit():
        timeout = float(env)
    probe = path.with_name(path.name + ".probe-" + str(os.getpid()))
    deadline = time.time() + timeout
    while True:
        try:
            path.rename(probe)
            probe.rename(path)          # 立刻改回来, 只在毫秒级内动这一下
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.5)


def safe_extract(zip_path: Path, dest: Path) -> Path:
    """解压并返回**包内容所在目录**(zip 顶层通常有一层同名目录)。

    自己逐个 zip entry 解压而不是 extractall: extractall 对 `../` 形式的路径不做
    检查, 一个被篡改的包能把文件写到包外面去。这里显式拒绝绝对路径与上跳。
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"包里有非法路径: {info.filename}")
            target = dest / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    # 顶层如果只有一个目录, 就把它当作包根
    entries = [p for p in dest.iterdir()]
    dirs = [p for p in entries if p.is_dir()]
    if len(entries) == 1 and len(dirs) == 1:
        return dirs[0]
    return dest


def read_version(root: Path) -> str:
    """读包里的 VERSION。读不到返回空串 —— 调用方据此判断"这包是不是完整"。"""
    p = root / "VERSION"
    if not p.is_file():
        return ""
    try:
        return (p.read_text(encoding="utf-8").strip().splitlines() or [""])[0].strip()
    except OSError:
        return ""


def check_package(new_root: Path, want_version: str) -> None:
    """新包必须是"能跑起来的样子"。宁可拒绝更新, 也不要换上去一个起不来的目录。"""
    ver = read_version(new_root)
    if not ver:
        raise ValueError("新包里没有 VERSION 文件，包不完整")
    if want_version and ver != want_version:
        raise ValueError(f"包里的版本是 {ver}，与要更新的 {want_version} 不一致")
    if not (new_root / "danmaku-server" / "server.py").is_file():
        raise ValueError("新包里没有 danmaku-server/server.py，包不完整")
    if not (new_root / "python" / "python.exe").is_file():
        raise ValueError("新包里没有便携 Python，包不完整")


def swap(old_root: Path, new_root: Path, port: int) -> None:
    """把新版内容换到包根, 用户数据原样保留。

    两种方式, 先试快的那种, 不行再退化成慢的:

      改名: 旧目录整体挪开、新目录挪进来。原子、失败可整体回滚, 是首选。
      覆盖: 把新文件逐个盖进旧目录。改不了名时用它 —— 而"改不了名"恰恰是
            **标准启动方式下的常态**(见下面第 2 步的注释)。

    .old 备份目录里保留着旧版全部内容, 即使新版起不来, 用户的数据与旧程序都还在。
    """
    if not wait_port_free(port):
        raise RuntimeError(f"端口 {port} 一直被占用，旧程序没有退出，已中止替换")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 两种方式都要先把用户数据带进新目录: 覆盖方式同样会以新目录为源,
    # 若那里缺了 data/, 覆盖时旧目录的 data/ 虽然不会被动, 但"新包自带的 data/"
    # 这类意外会悄悄盖掉用户数据。统一在这里做, 两边行为一致。
    _carry_user_data(old_root, new_root)

    # 2. 换上去。首选"改名"(原子、失败能整体回滚); 改不了名就退化成"就地覆盖"。
    #
    #    为什么会改不了名: Start.bat 第一行是 cd /d "%~dp0", 于是那个控制台窗口的
    #    工作目录就是包根, 一直锁到用户关窗口为止。实测在 Windows 上这时
    #    rename() 必然 WinError 32 —— 也就是说"改名"这条路对**标准启动方式**根本
    #    走不通。等用户关窗口不可接受(他正是点了更新去泡咖啡的那个), 所以必须有退路。
    #
    #    退路是"把新文件逐个覆盖进旧目录": 不需要改名, 只在写单个文件时短暂占用。
    #    比改名少一层原子性 —— 但前面已经把新包解压并校验过, 只剩"写文件"这一步,
    #    真的写失败时会如实报错并提示再点一次。
    if dir_renameable(old_root, timeout=8.0):
        _swap_by_rename(old_root, new_root, stamp)
        log("替换完成（改名方式）")
    else:
        _swap_in_place(old_root, new_root, stamp)
        log("替换完成（就地覆盖方式：程序目录被控制台窗口占用，已直接覆盖文件）")


def _carry_user_data(old_root: Path, new_root: Path) -> None:
    """把旧目录里的用户数据复制进新目录, 覆盖新包里的同名条目。

    只复制顶层的那几个名字(KEEP_NAMES), 不递归挑拣 —— 目录内部结构属于用户,
    我们不该理解它。这既是"覆盖方式的数据来源", 也是"改名方式换目录后的数据来源"。
    """
    for name in KEEP_NAMES:
        src = old_root / name
        if not src.exists():
            continue
        dst = new_root / name
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _swap_by_rename(old_root: Path, new_root: Path, stamp: str) -> None:
    """改名方式: 旧目录整体挪开, 新目录挪进来。快且失败可整体回滚。"""
    backup = old_root.with_name(old_root.name + ".old-" + stamp)
    new_home = old_root.with_name(old_root.name + ".new-" + stamp)
    new_root.rename(new_home)              # 先搬离 _update(否则 _update 跟着进新目录)
    old_root.rename(backup)
    try:
        new_home.rename(old_root)
    except OSError:
        # 新目录挪不过去 = 换上去也会是一半, 立刻把旧的恢复
        backup.rename(old_root)
        raise

    # 新目录就位后才删旧的。失败也不能影响新目录 —— 旧目录留着用户可以自己删。
    try:
        shutil.rmtree(backup)
    except OSError:
        log(f"旧目录没能删掉，可手动删除: {backup}")


# 就地覆盖时**绝不能动**的顶层名字: 用户数据(KEEP_NAMES) + 更新过程自己的落脚点。
# _update 里装着正在被执行的 updater 脚本与刚下载的包, 删了它等于把脚下的地板抽掉。
_IN_PLACE_SKIP = KEEP_NAMES | {"_update"}


def _swap_in_place(old_root: Path, new_root: Path, stamp: str) -> None:
    """覆盖方式: 把 new_root 里的条目逐个盖进 old_root, 跳过用户数据与 _update。

    为什么逐个删再拷, 而不是先删整个旧目录: 旧目录本身就是当前工作目录(锁着),
    删不掉; 但删它**里面的文件**是允许的 —— Windows 锁的是目录句柄, 不是每个子文件。

    顺序: 先删掉旧目录里"新包没有"的顶层条目(例如某个已下线的模块), 再逐个拷新的。
    只删**顶层**: 子目录里残留的旧文件无害(同名会被覆盖), 而递归删只会增加失败点。
    """
    for name in sorted(os.listdir(old_root)):
        if name in _IN_PLACE_SKIP:
            continue
        p = old_root / name
        if p.name in os.listdir(new_root):
            continue                        # 新包里也有 → 下面整体替换
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError as e:
            log(f"旧文件没能删掉（不影响新程序）: {name}: {e}")

    for name in sorted(os.listdir(new_root)):
        if name in _IN_PLACE_SKIP:
            continue
        src = new_root / name
        dst = old_root / name
        if dst.is_dir() and not src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.is_file() and src.is_dir():
            dst.unlink(missing_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    log(f"旧目录里的用户数据未动；被替换的旧文件已覆盖（时间戳 {stamp}）")


def relaunch(root: Path) -> None:
    """用新目录里的启动脚本重开。

    用 start 而不是直接 exec: 用户看到的是一个正常的控制台窗口(和双击 Start.bat 一样),
    updater 自己则马上退出, 不留一个看不见的后台进程。
    """
    bat = root / "Start.bat"
    if not bat.is_file():
        log("新目录里没有 Start.bat，请手动双击启动")
        return
    subprocess.Popen(["cmd", "/c", "start", "", str(bat)],
                     cwd=str(root), creationflags=0x00000008)  # DETACHED_PROCESS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="程序自更新替换器")
    ap.add_argument("--zip", help="已经下载好的新包")
    ap.add_argument("--root", required=True, help="要替换的包根目录")
    ap.add_argument("--port", type=int, default=8789)
    ap.add_argument("--version", default="", help="期望的版本, 与包内 VERSION 对不上就拒绝")
    ap.add_argument("--stage", action="store_true",
                    help="只解压校验, 不动正式目录(调试用)")
    ap.add_argument("--log", default="", help="日志文件路径")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if args.log:
        # 日志写到文件: updater 是脱离父进程起的, 控制台可能已经跟着父进程一起没了
        sys.stdout = open(args.log, "a", encoding="utf-8", buffering=1)

    if not args.zip:
        log("缺少 --zip")
        return 2
    zip_path = Path(args.zip).resolve()
    if not zip_path.is_file():
        log(f"找不到新包: {zip_path}")
        return 2

    work = root / "_update"
    stage = work / "stage"
    try:
        if stage.exists():
            shutil.rmtree(stage)
        new_root = safe_extract(zip_path, stage)
        check_package(new_root, args.version)
        log(f"包校验通过: {read_version(new_root)}")
        if args.stage:
            log("仅校验模式，未替换任何文件")
            return 0
        swap(root, new_root, args.port)
    except Exception as e:                    # noqa: BLE001 —— 任何失败都必须留下可读原因
        log(f"更新失败: {e}")
        return 1

    # 解压出来的暂存目录含整个新包, 留着会白白多占一份磁盘。
    # 下载的 zip 也一起删 —— 它已经用完了, 留着只会让下次误以为"已下好"。
    # runner/ 是临时复制出去的解释器(约 30MB), 同样用完即删。
    shutil.rmtree(stage, ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    shutil.rmtree(work / "runner", ignore_errors=True)
    log("暂存与安装包已清理")

    relaunch(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
