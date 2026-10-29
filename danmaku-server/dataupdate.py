# -*- coding: utf-8 -*-
"""dataupdate.py — 从 GitHub 增量拉取采集数据。

为什么要有这个模块:
  用户装了播放台后, 每晚新数据要能一键到位, 而不是让他装 git 敲 `git pull`。
  绝大多数用户只想要"能看", 不想碰命令行。

设计约束:
  1. **只用标准库**(urllib)。用户已经要装 Python, 不该为更新功能多装依赖。
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
  - **第三方镜像下载必须校验 sha256**: 见 srcfetch.py(通道选择与校验都在那)。

分工:
  本模块管"下到哪"(比对清单、原子落盘、防穿越), srcfetch 管"从哪下"
  (多镜像降级、代理策略、sha256 校验)。拆开是为了两块各自可测:
  "镜像返回坏内容"这种情形在线上极难复现, 单独成层才写得进回归。
"""
import gzip
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import srcfetch

# 数据源: 只允许从这里取。改这里等于改分发渠道。
RAW_BASE = srcfetch.RAW_BASE
INDEX_NAME = "data-index.json"

# 并发下载路数。实际并发取 min(_WORKERS, 本批文件数), 见 _run_batch。
# 上限之所以不是越大越好: 每个文件有建连 + TLS + 镜像重定向的固定开销, 与文件大小
# 无关, 并发把这些开销重叠起来; 到这个量级后提升趋于平缓, 再往上只是增加对公共
# 镜像的请求压力。
_WORKERS = 4


class UpdateError(Exception):
    """更新失败, 消息可直接展示给用户。"""


def _fetch(url: str, sha256: str = "", want_bytes=None,
           name: str = "") -> bytes:
    """下载并校验, 把 srcfetch 的异常翻译成本模块的 UpdateError。

    保留这个薄封装而不是让调用方直接用 srcfetch:
    调用方(server.py)只捕获 dataupdate.UpdateError, 不该知道底下有几个模块。
    `name` 用于内容自校验(gzip 解开 / jsonl 逐行解析), 见 srcfetch.content_ok。
    """
    try:
        return srcfetch.fetch(url, sha256=sha256, want_bytes=want_bytes, name=name)
    except srcfetch.FetchError as e:
        raise UpdateError(str(e)) from e


def fetch_index() -> dict:
    """拉取远端数据清单。清单由 tools/pack_data.py --index 生成。"""
    raw = _fetch(f"{RAW_BASE}/{INDEX_NAME}")
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


def _file_meta(entry: dict, rel: str) -> tuple:
    """从清单里取该文件的 (字节数, sha256)。缺失的字段返回 None。

    为什么要容忍缺失: 老清单(没有 sha256 的那版)在用户机器上可能还留着。
    读老清单不能报错, 退化到"只校验大小"——比不校验好, 也比直接拒绝更新好。
    """
    want = sha = None
    for f in entry.get("file_sizes") or []:
        if f.get("name") == rel:
            want = int(f.get("bytes") or 0)
            sha = (f.get("sha256") or "").strip().lower() or None
            break
    return want, sha


def _sha256_file(p: Path) -> str:
    """流式算文件哈希, 不把整个文件读进内存(afp 单个可到 10MB)。"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_ok(p: Path, want, sha, rel: str) -> bool:
    """本地已有文件是否可直接用(不必重下)。

    判据必须与下载后的校验一致(srcfetch._check), 否则会出现
    "下完判通过, 下次又判不通过" —— 每次点更新都重下 21MB:
      有 sha256  -> 只认 sha256(最权威)
      字节数对得上 -> 直接用
      字节数不符或没给 -> 问内容自校验(gzip CRC / JSON 可解析)
    """
    if sha:
        return _sha256_file(p) == sha
    if want is not None and p.stat().st_size == want:
        return True
    try:
        ok = srcfetch.content_ok(rel, p.read_bytes())
    except OSError:
        return False
    return ok is True


def _download_one(rel: str, want, sha, root: Path) -> int:
    """下载并落盘单个文件。返回写入字节数。失败抛 UpdateError。

    只做一件事, 便于被线程池并发调用。每个文件写自己的 .part,
    并发写不同文件互不干扰; 同一文件不会同时被下两次(调用方先去重)。
    """
    rel = _safe_rel(rel)
    dst = root / rel
    blob = _fetch(f"{RAW_BASE}/{rel}", sha256=sha or "", want_bytes=want,
                  name=rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, dst)        # 原子替换: 断电也不会留半个文件
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return len(blob)


def _run_batch(batch: list, root: Path, on_file, counter: list) -> None:
    """并发下载一批文件。成功后把字节数累加进 counter[0]。

    抽出来是为了让 download_day 只表达"顺序"(先切片后 manifest)这件事。
    counter 用单元素列表而不是返回值: 需要在 with 块内累加, 而异常时要原样抛出去,
    调用方拿不到正常返回值 —— 用列表可以避免为此引入可变对象属性。
    """
    if not batch:
        return
    n = max(1, min(_WORKERS, len(batch)))
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = {
            ex.submit(_download_one, rel, want, sha, root): rel
            for rel, want, sha in batch
        }
        for fut in as_completed(futures):
            rel = futures[fut]
            size = fut.result()          # UpdateError/OSError 原样抛给调用方
            counter[0] += size
            if on_file:
                on_file(rel, size)


def download_day(day: str, entry: dict, data_dir: Path, on_file=None) -> int:
    """下载一天的会话文件。返回写入字节数。

    幂等: 已存在且**大小与哈希都对得上**的文件跳过 —— 同一天重复跑不会重复下。

    已有文件也要读一遍算哈希: 上次中断会留下截断文件, 或上次下的是被镜像污染的
    版本。数据不可再生, 所以只认哈希, 不认大小。

    并发安全: 每个文件写自己的 .part 再原子改名, 不同文件之间不共享状态。
    """
    files = entry.get("files") or []
    if not files:
        raise UpdateError(f"{day} 的清单条目没有 files")
    root = data_dir.parent          # 清单路径形如 data/{day}/{room}/x, 相对仓库根

    # 先筛出真正要下的(已有且校验通过的直接跳过), 并发只用在下载上
    todo = []
    for rel in files:
        rel = _safe_rel(rel)
        want, sha = _file_meta(entry, rel)
        dst = root / rel
        if dst.is_file() and _local_ok(dst, want, sha, rel):
            continue
        todo.append((rel, want, sha))
    if not todo:
        return 0

    # ★ manifest.json 必须**最后**落盘。
    #   本地判断"这一天有没有"看的是 manifest 在不在(local_days)。若它先落盘而某个
    #   切片失败了, 用户就会看到"这天有数据"却播不完整 —— 而且下次更新会认为已有的
    #   跳过, 坏状态被固化。反过来(切片先、manifest 后)失败时只是"这天还没下好",
    #   下次重跑会补齐。这就是把它单独拎出来的全部理由。
    #   注意不能只靠"排到最后提交": 多线程提交顺序 ≠ 完成顺序, 必须等前面全完再下它。
    head = [t for t in todo if not t[0].endswith("manifest.json")]
    tail = [t for t in todo if t[0].endswith("manifest.json")]

    counter = [0]
    _run_batch(head, root, on_file, counter)
    _run_batch(tail, root, on_file, counter)   # 前面失败会抛出 -> manifest 不落盘
    return counter[0]


def check(data_dir: Path) -> dict:
    """比对远端清单与本地, 返回可展示给用户的摘要(不下载)。

    `remote_app_version` 是**作者发布时的程序版本**(来自清单的 app_version 字段)。
    老清单没有这个字段 -> 返回 None, 调用方必须容忍(当作"未知"), 不能报错。
    """
    idx = fetch_index()
    have = local_days(data_dir)
    todo = plan(idx, have)
    return {
        "remote_latest": idx.get("latest"),
        "remote_app_version": idx.get("app_version") or None,
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


def _manifests_for_day(day_dir: Path) -> list:
    """一天里全部 manifest 的路径(按路径排序, 保证结果稳定)。

    为什么不是"找到第一个就用": 一天可能有多个会话目录 —— 同一天硬杀重启、
    或多次 finalize, 都会在 data/{day}/{room}/ 下各留一份 manifest。只验第一个
    等于其余会话**完全没自检**, 而用户看到的仍是"校验通过", 比不校验更误导。
    """
    if not day_dir.is_dir():
        return []
    root = day_dir / "manifest.json"
    if root.is_file():                 # 旧平铺布局: 日期目录根上一份
        return [root]
    return sorted(p for p in day_dir.glob("*/manifest.json") if p.is_file())


def verify_day(day_dir: Path) -> list:
    """校验一天的数据完整性(每个会话的 manifest 声明的文件都在且能解开)。

    只读检查, 不修改任何东西。给"更新后自检"用 —— 下载成功不等于数据能用,
    gzip 截断只有真去解才知道。

    **多会话日逐个验**: 见 _manifests_for_day 的说明。
    """
    mfs = _manifests_for_day(day_dir)
    if not mfs:
        return [f"{day_dir.name}: 没有 manifest.json"]
    problems = []
    for mf in mfs:
        problems.extend(_verify_manifest_files(day_dir, mf))
    return problems


def _verify_manifest_files(day_dir: Path, mf: Path) -> list:
    """校验单份 manifest 声明的文件齐全且 gzip 可解。"""
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 带上会话目录名, 多会话日才分得清是哪一份坏了
        return [f"{day_dir.name}/{mf.parent.name}: manifest 解析失败 {e}"]
    base = mf.parent
    tag = day_dir.name if base == day_dir else f"{day_dir.name}/{base.name}"
    problems = []
    for name in man.get("files") or []:
        p = base / name
        if not p.is_file():
            problems.append(f"{tag}: 缺文件 {name}")
            continue
        if name.endswith(".gz"):
            try:
                with gzip.open(p, "rb") as f:
                    while f.read(1 << 20):
                        pass
            except (OSError, EOFError) as e:
                problems.append(f"{tag}: {name} 损坏 {e}")
    return problems
