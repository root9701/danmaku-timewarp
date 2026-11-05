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
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import srcfetch
import datalist
from datacheck import verify_day as verify_day

# 数据源: 只允许从这里取。改这里等于改分发渠道。
RAW_BASE = srcfetch.RAW_BASE
INDEX_NAME = "data-index.json"

# 并发下载路数。实际并发取 min(_WORKERS, 本批文件数), 见 _run_batch。
# 上限之所以不是越大越好: 每个文件有建连 + TLS + 镜像重定向的固定开销, 与文件大小
# 无关, 并发把这些开销重叠起来; 到这个量级后提升趋于平缓, 再往上只是增加对公共
# 镜像的请求压力。
_WORKERS = 4

# 「选哪些文件」在 datalist(纯函数, 不碰网络与磁盘); 本模块只管怎么下、下到哪。
# 这里转出几个最常用的名字, 免得调用方同时 import 两个模块。
is_afp = datalist.is_afp
AFP_SUFFIXES = datalist.AFP_SUFFIXES


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
    """算出要整日下载的日期。返回 [(日期, 条目)] 升序。逻辑见 datalist.plan。"""
    return datalist.plan(index, have)


def afp_fix_days(index: dict, have: dict, data_dir: Path, want_afp: bool) -> list:
    """已有日期里缺 afp 的那些。见 datalist.afp_fix_days。"""
    return datalist.afp_fix_days(index, have, data_dir.parent, want_afp)


def align_fix_days(index: dict, have: dict, data_dir: Path) -> list:
    """已有日期里 align.json 内容过期的那些。见 datalist.stale_align_days。

    发布版不带校准能力, 作者重算的对齐结果必须能自动到老用户手里; plan()
    只看 manifest 在不在, 已下过的整天会被永久跳过, 故单独按 sha256 比对。
    """
    root = data_dir.parent
    local = {rel: _sha_of(root / rel) for rel in datalist.only_aligns(index)}
    return datalist.stale_align_days(index, have, local)


def day_fix_days(index: dict, have: dict, data_dir: Path) -> list:
    """已有日期里 manifest 变了的那些(采集中新增切片 / 收尾后补 afp)。

    plan() 只看 manifest 在不在, 于是作者夜间每小时发布的增量对老用户永远
    不可见 —— 他们下过一次这天就一直停在那一版。这里按 manifest 哈希比对,
    命中后交给 download_day 只补缺的文件(已校验通过的不重下)。
    """
    root = data_dir.parent
    local = {rel: _sha_of(root / rel) for rel in datalist.manifest_paths(index)}
    return datalist.stale_manifest_days(index, have, local)


def _safe_rel(rel: str) -> str:
    """校验清单里的相对路径, 防路径穿越(逻辑在 datalist.safe_rel)。"""
    try:
        return datalist.safe_rel(rel)
    except ValueError as e:
        raise UpdateError(str(e)) from e


def _file_meta(entry: dict, rel: str) -> tuple:
    """从清单里取该文件的 (字节数, sha256)。见 datalist.file_meta。"""
    return datalist.file_meta(entry, rel)


def _sha256_file(p: Path) -> str:
    """流式算文件哈希, 不把整个文件读进内存(afp 单个可到 10MB)。"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_of(p: Path) -> str:
    """文件存在则返回 sha256, 否则空串(缺文件即"内容对不上")。"""
    try:
        return _sha256_file(p) if p.is_file() else ""
    except OSError:
        return ""


def _missing_bytes(entry: dict, root: Path) -> int:
    """这一天真正要下的字节数(只算缺失或内容对不上的文件)。

    为什么不能直接把条目总字节加起来: 增量发布时一天只多一两个文件, 报一个
    "还要下整晚 10MB"会让用户以为程序在重下; 实际已有文件全部校验后跳过。
    """
    total = 0
    for f in entry.get("file_sizes") or []:
        rel = str(f.get("name") or "")
        want = int(f.get("bytes") or 0)
        sha = str(f.get("sha256") or "").strip().lower()
        p = root / rel
        if not p.is_file() or p.stat().st_size != want or (sha and _sha_of(p) != sha):
            total += want
    return total


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


def download_day(day: str, entry: dict, data_dir: Path, on_file=None,
                 want_afp: bool = True) -> int:
    """下载一天的会话文件。返回写入字节数。

    幂等: 已存在且**大小与哈希都对得上**的文件跳过 —— 同一天重复跑不会重复下。

    已有文件也要读一遍算哈希: 上次中断会留下截断文件, 或上次下的是被镜像污染的
    版本。数据不可再生, 所以只认哈希, 不认大小。

    并发安全: 每个文件写自己的 .part 再原子改名, 不同文件之间不共享状态。

    want_afp=False 时跳过音频指纹文件(见模块顶部 AFP_SUFFIXES 的说明)。
    """
    files = [r for r in (entry.get("files") or [])
             if want_afp or not is_afp(r)]
    if not files:
        if not (entry.get("files") or []):
            raise UpdateError(f"{day} 的清单条目没有 files")
        return 0                          # 本条目只剩 afp 且不下 afp
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


def check(data_dir: Path, want_afp: bool = True) -> dict:
    """比对远端清单与本地, 返回可展示给用户的摘要(不下载)。

    `remote_app_version` 是**作者发布时的程序版本**(来自清单的 app_version 字段)。
    老清单没有这个字段 -> 返回 None, 调用方必须容忍(当作"未知"), 不能报错。

    want_afp=False 时 new_bytes 扣掉指纹部分, 并单列 afp_fix_days ——
    否则界面会报一个"下不到的体积", 且用户永远不知道自己缺指纹。
    """
    idx = fetch_index()
    have = local_days(data_dir)
    todo = plan(idx, have)
    fix = afp_fix_days(idx, have, data_dir, want_afp)
    redo = align_fix_days(idx, have, data_dir)
    redo_day = day_fix_days(idx, have, data_dir)
    new_bytes = 0
    for _, entry in todo:
        total, afp = datalist.day_bytes(entry)
        new_bytes += total if want_afp else total - afp
    for _day, entry in fix:
        new_bytes += datalist.day_bytes(entry)[1]
    for _day, entry in redo:
        new_bytes += sum(int(f.get("bytes") or 0)
                         for f in (entry.get("file_sizes") or []))
    for _day, entry in redo_day:
        new_bytes += _missing_bytes(entry, data_dir.parent)
    return {
        "remote_latest": idx.get("latest"),
        "remote_app_version": idx.get("app_version") or None,
        "local_days": sorted(have),
        "new_days": [d for d, _ in todo],
        "afp_fix_days": [d for d, _ in fix],
        "align_fix_days": [d for d, _ in redo],
        "day_update_days": [d for d, _ in redo_day],
        "new_bytes": new_bytes,
        "need": len(todo),
        "want_afp": want_afp,
    }


def _download_all(todo, fix, redo_day, redo, data_dir, want_afp, on_progress) -> tuple:
    """按四类补充依次下载, 返回 (新日期列表, 动过的日期列表, 总字节)。

    "动过的日期"(touched) 是前端刷新的唯一依据。历史缺陷: 旧实现只把新日期
    记进 updated, 于是"只补对齐/只补指纹"时 updated 为空 —— 界面提示"已刷新"
    却什么都没变, 用户必须按 F5。
    """
    groups = (todo, fix, redo_day, redo)       # 顺序即下载顺序
    total = sum(len(g) for g in groups)
    nbytes, touched = 0, []
    for i, (day, entry) in enumerate(todo + fix + redo_day + redo, 1):
        if on_progress:
            on_progress(i, total, day)

        def _cb(rel, n, _d=day, _i=i):
            if on_progress:
                on_progress(_i, total, f"{_d}  {Path(rel).name}")
        try:
            nbytes += download_day(day, entry, data_dir, _cb, want_afp=want_afp)
        except UpdateError:
            raise
        except OSError as e:
            raise UpdateError(f"{day} 写入失败: {e}") from e
        # 去重保序: 同一天可能既补了指纹又补了对齐, 界面只需刷新一次
        if day not in touched:
            touched.append(day)
    new_days = [d for d, _ in todo]
    return new_days, touched, nbytes


def update(data_dir: Path, on_progress=None, want_afp: bool = True) -> dict:
    """执行增量更新。返回结果摘要。

    四类补充共用同一套幂等与原子落盘, 各自只碰自己的文件: 整日下载(plan)、
    补指纹(afp_fix_days)、补当天增量(day_fix_days)、补对齐(align_fix_days)。
    补当天增量只下缺失的切片; 补对齐只有几 KB, 不重下弹幕。
    """
    idx = fetch_index()
    have = local_days(data_dir)
    todo = plan(idx, have)
    fix = afp_fix_days(idx, have, data_dir, want_afp)
    redo = align_fix_days(idx, have, data_dir)
    redo_day = day_fix_days(idx, have, data_dir)
    if not todo and not fix and not redo and not redo_day:
        return {"updated": [], "touched": [], "bytes": 0, "message": "已是最新"}

    done, touched, nbytes = _download_all(
        todo, fix, redo_day, redo, data_dir, want_afp, on_progress)
    parts = []
    if done:
        parts.append(f"已新增 {len(done)} 天")
    if fix:
        parts.append(f"补齐 {len(fix)} 天的音频指纹")
    if redo_day:
        parts.append(f"更新 {len(redo_day)} 天的采集数据（比赛可能仍在进行）")
    if redo:
        parts.append(f"更新 {len(redo)} 天的校准结果")
    return {"updated": done, "touched": touched, "bytes": nbytes,
            "message": "，并".join(parts) or "已是最新"}
