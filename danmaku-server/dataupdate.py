# -*- coding: utf-8 -*-
"""dataupdate.py — 从 GitHub 增量拉取采集数据。

为什么要有这个模块:
  用户装了播放台后, 每晚新数据要能一键到位, 而不是让他装 git 敲 `git pull`。
  绝大多数用户只想要"能看", 不想碰命令行。

设计约束:
  1. **只用标准库**(urllib)。用户已经要装 Python, 不该再为更新功能多装依赖。
  2. **只下载缺失的日期**。全量 21MB 现在还能接受, 但一届 Worlds 约 300MB ——
     每晚都下全量是不可持续的。比对清单后只拉新的那几天。
  3. **原子落盘**: 先写 .part 再 rename。中途断网不会留下半个文件, 否则
     下次启动会读到损坏的 gzip, 表现为"某天数据忽然打不开"。
  4. **只写 data/ 下的新目录, 绝不改已有数据**。数据不可再生(直播流已消失),
     更新逻辑越权写入 = 永久损坏。
  5. **不进 align/**。那是上游分发物, 由 git 管理; 这里只管 data/。

安全:
  - 下载地址拼自固定的 owner/repo/branch, 不接受用户输入任意 URL。
  - 同时校验清单里的相对路径(拒绝 .. 与绝对路径), 防路径穿越。
"""
import gzip
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# 数据源: 只允许从这里取。改这里等于改分发渠道。
GH_OWNER = "root9701"
GH_REPO = "danmaku-timewarp"
GH_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/{GH_BRANCH}"
INDEX_NAME = "data-index.json"

UA = "danmaku-timewarp-updater/1"
# 连接超时与读取超时分开: 挂死的现场通常是"连上了但不返回", 读取超时才有用。
# 30 秒整体超时在直连被阻断时会等满, 前端表现为长时间无响应 —— 用户以为程序卡了。
TIMEOUT = 20
_MAX_ATTEMPTS = 2


class UpdateError(Exception):
    """更新失败, 消息可直接展示给用户。"""


def _proxies() -> dict:
    """当前生效的代理配置。

    为什么需要这个:
      Python 的 urllib 默认用 getproxies(), 它**只读环境变量**。而 Windows 上
      用户常见的做法是在系统设置里配代理(Clash/V2Ray 之类), 那是注册表 +
      WinINet 的配置, urllib 读不到 —— 于是程序直连 GitHub, 在需要代理的网络
      环境下会 RemoteDisconnected 或长时间挂起。

    为什么不能写死走代理:
      用户的代理是**按需开启**的。代理没开时硬走它, 反而全部失败。
      所以这里读"当前是否有系统代理", 有就用, 没有就直连。

    为什么必须显式带 no 列表:
      ProxyHandler 只认字典里的 no 项。丢了它, localhost / 127.0.0.1 的请求
      也会被送去代理 —— 而本机服务(回归测试的假数据源、局域网回放)不该走代理,
      且多数代理对本地地址的处理并不一致, 表现为时通时不通。
    """
    try:
        reg = urllib.request.getproxies_registry() or {}
    except Exception:
        reg = {}
    src = reg or {k: v for k, v in urllib.request.getproxies().items() if k != "no"}
    if not src:
        return {}
    no = reg.get("no") or ",".join(
        ["127.0.0.1", "localhost", "::1"])
    return {**src, "no": no}


def _get(url: str) -> bytes:
    """下载一个 URL。

    代理策略: 先按系统代理配置试, 失败再直连试一次。
    这样"代理开着"和"代理关着"两种状态都能工作, 不需要用户改配置。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    proxies = _proxies()
    # 依次尝试: 走代理 -> 直连。代理没配时第一个就是直连, 不会重复。
    plans = [proxies, {}] if proxies else [{}]
    last = None
    for plan in plans[: (_MAX_ATTEMPTS if proxies else 1)]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(plan))
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # HTTP 错误是服务器明确答复, 换路径也没用, 直接抛
            if e.code == 404:
                raise UpdateError(
                    f"远端还没有数据清单(404)。作者可能尚未发布，或仓库名有变：{url}"
                ) from e
            raise UpdateError(f"下载失败 HTTP {e.code}: {url}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            continue                     # 这条路径不通, 试下一条
    hint = ("；若你平时需要开代理才能访问 GitHub，请确认代理已启动"
            if proxies else "")
    raise UpdateError(
        f"连不上数据源：{last.__class__.__name__}{hint}") from last


def fetch_index() -> dict:
    """拉取远端数据清单。清单由 tools/pack_data.py --index 生成。"""
    raw = _get(f"{RAW_BASE}/{INDEX_NAME}")
    try:
        idx = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError(f"清单格式错误: {e}") from e
    if not isinstance(idx.get("days"), dict):
        raise UpdateError("清单缺少 days 字段")
    return idx


def local_days(data_dir: Path) -> dict:
    """扫本地已有的会话: {日期: 该日 manifest 的字节数}。

    用 manifest 是否存在判断"这一天有没有", 而不是看目录 —— 空的或不完整的
    目录(下载中断的残留)不算已有。
    """
    out = {}
    if not data_dir.is_dir():
        return out
    for day in data_dir.iterdir():
        if not day.is_dir() or day.name.startswith((".", "_")):
            continue
        if not (len(day.name) == 10 and day.name[4] == "-"):
            continue
        for sess in day.iterdir():
            mf = sess / "manifest.json"
            if sess.is_dir() and mf.is_file():
                try:
                    out[day.name] = out.get(day.name, 0) + mf.stat().st_size
                except OSError:
                    out.setdefault(day.name, 0)
    return out


def plan(index: dict, have: dict) -> list:
    """算出要下哪些日期。返回 [(日期, 条目)] 升序。"""
    todo = []
    for day in sorted(index.get("days") or {}):
        if day in have:
            continue
        todo.append((day, index["days"][day]))
    return todo


def _safe_rel(rel: str) -> str:
    """校验清单里的相对路径, 防路径穿越。"""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise UpdateError(f"清单里有非法路径: {rel}")
    if p.parts[0] != "data":
        raise UpdateError(f"清单路径必须以 data/ 开头: {rel}")
    return rel


def download_day(day: str, entry: dict, data_dir: Path, on_file=None) -> int:
    """下载一天的会话文件。返回写入字节数。

    幂等: 已存在且大小一致的文件跳过 —— 同一天重复跑不会重复下。
    """
    files = entry.get("files") or []
    if not files:
        raise UpdateError(f"{day} 的清单条目没有 files")
    total = 0
    root = data_dir.parent          # 清单路径形如 data/{day}/{room}/x, 相对仓库根
    for rel in files:
        rel = _safe_rel(rel)
        dst = root / rel
        want = None
        for f in entry.get("file_sizes") or []:
            if f.get("name") == rel:
                want = int(f.get("bytes") or 0)
                break
        if dst.is_file() and (want is None or dst.stat().st_size == want):
            continue                # 已有且大小对得上, 不重下
        url = f"{RAW_BASE}/{rel}"
        blob = _get(url)
        if want is not None and len(blob) != want:
            raise UpdateError(f"{rel} 大小不符(期望 {want}, 实际 {len(blob)})")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, dst)    # 原子替换: 断电也不会留半个文件
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        total += len(blob)
        if on_file:
            on_file(rel, len(blob))
    return total


def check(data_dir: Path) -> dict:
    """比对远端清单与本地, 返回可展示给用户的摘要(不下载)。"""
    idx = fetch_index()
    have = local_days(data_dir)
    todo = plan(idx, have)
    return {
        "remote_latest": idx.get("latest"),
        "local_days": sorted(have),
        "new_days": [d for d, _ in todo],
        "new_bytes": sum(int(e.get("bytes") or 0) for _, e in todo),
        "need": len(todo),
    }


def update(data_dir: Path, on_progress=None) -> dict:
    """执行增量更新。返回结果摘要。"""
    idx = fetch_index()
    have = local_days(data_dir)
    todo = plan(idx, have)
    if not todo:
        return {"updated": [], "bytes": 0, "message": "已是最新"}

    done, nbytes = [], 0
    for i, (day, entry) in enumerate(todo, 1):
        if on_progress:
            on_progress(i, len(todo), day)
        def _cb(rel, n, _d=day):
            if on_progress:
                on_progress(i, len(todo), f"{_d}  {Path(rel).name}")
        try:
            nbytes += download_day(day, entry, data_dir, _cb)
            done.append(day)
        except UpdateError:
            raise
        except OSError as e:
            raise UpdateError(f"{day} 写入失败: {e}") from e
    return {"updated": done, "bytes": nbytes,
            "message": f"已新增 {len(done)} 天"}


def verify_day(day_dir: Path) -> list:
    """校验一天的数据完整性(manifest 声明的文件都在且能解开)。

    只读检查, 不修改任何东西。给"更新后自检"用 —— 下载成功不等于数据能用,
    gzip 截断只有真去解才知道。
    """
    problems = []
    mf = day_dir / "manifest.json"
    if not mf.is_file():
        for sess in day_dir.iterdir() if day_dir.is_dir() else []:
            if sess.is_dir() and (sess / "manifest.json").is_file():
                mf = sess / "manifest.json"
                break
        else:
            return [f"{day_dir.name}: 没有 manifest.json"]
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{day_dir.name}: manifest 解析失败 {e}"]
    base = mf.parent
    for name in man.get("files") or []:
        p = base / name
        if not p.is_file():
            problems.append(f"{day_dir.name}: 缺文件 {name}")
            continue
        if name.endswith(".gz"):
            try:
                with gzip.open(p, "rb") as f:
                    while f.read(1 << 20):
                        pass
            except (OSError, EOFError) as e:
                problems.append(f"{day_dir.name}: {name} 损坏 {e}")
    return problems
