# -*- coding: utf-8 -*-
"""pack_data.py — 发布前校验 + 打包。

用法:
    python tools/pack_data.py                 # 校验(默认), 退出码 0 = 可发布
    python tools/pack_data.py --strip-afp     # 生成不含 afp 的发行副本到 dist/
    python tools/pack_data.py --json          # 机器可读报告

为什么需要:
  1. 分发给别人时, 任何一个会话目录缺 manifest, /api/days 会退化成目录扫描 ——
     实测 365 天规模下从 352ms 变成 9046ms(慢 26 倍), 用户以为程序卡死。
  2. 若裁掉 afp 却不改 manifest, 用户看到 "afp": {"file": ...} 却找不到文件,
     会以为下载不完整。
  3. 弹幕切片缺失/为空会让该晚无法回放, 发布前必须拦住。

校验项(任一不过即退出码 1):
  - 每个正式会话目录必须有 manifest.json, 且能被解析、有 schema_version
  - manifest 里 files[] 列出的每个切片文件都必须真实存在
  - dm_count 必须 > 0
  - start_ms < end_ms
  - align/ 里引用的每一场比赛, 若带 BV, 必须在 lists 里能查到对应条目
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ALIGN_NAME = "align.json"          # 与 server.py 的 ALIGN_NAME 一致
DIST = ROOT / "dist"


def _app_version() -> str:
    """程序版本, 来自仓库根的 VERSION(唯一源)。读不到返回空串。

    这里**不 import 各端的 version.py**: 本脚本是发布工具, 只该依赖根目录的
    那一个文件。读不到时给空串而不是 "0.0.0", 让清单里能看出"生成时没有版本信息",
    而不是伪造一个版本号骗用户。
    """
    p = ROOT / "VERSION"
    try:
        txt = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return (txt.splitlines()[0].strip() if txt else "")


def _sha256(p: Path) -> str:
    """流式算文件哈希。不整个读进内存: 单个 afp 可到 10MB, 将来还会更大。"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_sessions():
    """遍历正式会话目录; 跳过 _test 与隐藏目录。"""
    if not DATA.is_dir():
        return
    for day in sorted(DATA.iterdir()):
        if not day.is_dir() or day.name.startswith((".", "_")):
            continue
        if not (len(day.name) == 10 and day.name[4] == "-"):
            continue
        for sess in sorted(day.iterdir()):
            if sess.is_dir() and not sess.name.startswith("."):
                yield day.name, sess


def check_session(day: str, sess: Path) -> list:
    """返回问题列表; 空 = 通过。"""
    problems = []
    mf_path = sess / "manifest.json"
    if not mf_path.is_file():
        problems.append(f"{day}/{sess.name}: 缺 manifest.json "
                        f"(会导致目录扫描, 慢 26 倍)")
        return problems
    try:
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problems.append(f"{day}/{sess.name}: manifest 解析失败: {e}")
        return problems
    if not isinstance(mf, dict):
        problems.append(f"{day}/{sess.name}: manifest 顶层不是对象")
        return problems
    if not isinstance(mf.get("schema_version"), int):
        problems.append(f"{day}/{sess.name}: manifest 缺 schema_version")

    files = mf.get("files") or []
    if not files:
        problems.append(f"{day}/{sess.name}: manifest.files 为空")
    for fn in files:
        # 切片可能是 .jsonl 或 .jsonl.gz
        if not ((sess / fn).is_file() or (sess / (fn + ".gz")).is_file()):
            problems.append(f"{day}/{sess.name}: 切片缺失 {fn}")

    if int(mf.get("dm_count") or 0) <= 0:
        problems.append(f"{day}/{sess.name}: dm_count 为 0")

    s, e = mf.get("start_ms"), mf.get("end_ms")
    if isinstance(s, (int, float)) and isinstance(e, (int, float)) and s >= e:
        problems.append(f"{day}/{sess.name}: start_ms >= end_ms")

    afp = mf.get("afp")
    if isinstance(afp, dict) and afp.get("file"):
        if not (sess / afp["file"]).is_file():
            # 允许 .gz 变体
            if not (sess / (afp["file"] + ".gz")).is_file():
                problems.append(
                    f"{day}/{sess.name}: manifest 指向的 afp 不存在 {afp['file']} "
                    f"(裁掉 afp 时必须同时把 manifest.afp 置 null)")
    return problems


def iter_align():
    """遍历全部 data/{日期}/align.json, 产出 (日期, 路径)。"""
    if not DATA.is_dir():
        return
    for day_dir in sorted(DATA.iterdir()):
        if not day_dir.is_dir():
            continue
        p = day_dir / ALIGN_NAME
        if p.is_file():
            yield day_dir.name, p


def check_align() -> list:
    """对齐表的一致性检查(一天一份, 住在 data/{日期}/align.json)。

    规则: **只有带 pages(真的算出 offset)的记录**才要求 BV 能在 lists 里找到 ——
    时间窗只在"算 offset"时用得上。无 pages 的条目是失败标记或历史残留(比如会话
    目录后来被合并改名、或那场其实属于另一天), 它们不影响播放, 不该阻止发布。
    """
    problems = []
    for day, p in iter_align():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"data/{day}/{ALIGN_NAME}: 解析失败 {e}")
            continue
        if not isinstance(d, dict):
            problems.append(f"data/{day}/{ALIGN_NAME}: 顶层不是对象")
            continue
        lists = d.get("lists") or {}
        pairs = set()
        for d2, lst in lists.items():
            for it in (lst or []):
                if isinstance(it, dict) and it.get("bv"):
                    pairs.add((d2, str(it["bv"])))
        for key, rec in (d.get("align") or {}).items():
            if not isinstance(rec, dict):
                continue
            pages = rec.get("pages")
            if not isinstance(pages, dict) or not pages:
                continue                      # 无 offset 的记录, 不参与判定
            bv = str(rec.get("bv") or "").strip()
            if not bv:
                continue
            dkey = str(key).split("|")[0]
            if (dkey, bv) not in pairs:
                problems.append(
                    f"data/{day}/{ALIGN_NAME}: {key} 有 {len(pages)} 页对齐结果, "
                    f"但 BV 在 lists 里查不到(重新校准时拿不到赛程时间窗)")
    return problems


def strip_afp() -> int:
    """生成 dist/data —— 不含 afp, 且把 manifest.afp 置 null。"""
    import shutil

    if DIST.exists():
        shutil.rmtree(DIST)
    out = DIST / "data"
    n_sess = n_afp = 0
    for day, sess in iter_sessions():
        dst = out / day / sess.name
        dst.mkdir(parents=True, exist_ok=True)
        for f in sess.iterdir():
            if not f.is_file():
                continue
            if ".afp.jsonl" in f.name:
                n_afp += 1
                continue                      # 不复制指纹
            shutil.copy2(f, dst / f.name)
        # 改 manifest: afp 置 null, 并声明这是裁剪版
        mfp = dst / "manifest.json"
        if mfp.is_file():
            mf = json.loads(mfp.read_text(encoding="utf-8"))
            if isinstance(mf, dict):
                mf["afp"] = None
                mf["distributable"] = True
                mf["afp_stripped"] = True
                mfp.write_text(json.dumps(mf, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        n_sess += 1
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"生成 {DIST.relative_to(ROOT)}/")
    print(f"  会话 {n_sess} 个, 剔除 afp {n_afp} 个文件")
    print(f"  体积 {size / 1024 / 1024:.1f} MB")
    print("  注意: 分发版**不能重新校准**(没有指纹), 但可以直接回放。")
    return 0


def _collect_files(day: str, files, days: dict) -> None:
    """把一批文件登记进 `days[day]`, 逐文件算字节数与 sha256。"""
    d = days.setdefault(day, {"files": [], "file_sizes": [],
                              "sessions": [], "bytes": 0})
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if rel in d["files"]:
            continue
        d["files"].append(rel)
        d["file_sizes"].append({"name": rel, "bytes": f.stat().st_size,
                                "sha256": _sha256(f)})
        d["bytes"] += f.stat().st_size


def _build_days() -> dict:
    """扫盘构造 {日期: {files, file_sizes, sessions, bytes}}。

    两类文件来源不同, 必须都收:
      1. 房间子目录里的会话产物(弹幕/指纹/meta/manifest) —— iter_sessions
      2. 日期目录下的对齐表 data/{日期}/align.json —— iter_align
    对齐表不在房间子目录里, 只看 (1) 会漏 —— 而它正是轻量版播放所必需的
    "弹幕贴第几秒"参数, 漏了用户拉完数据仍然看不了。
    """
    days: dict = {}
    for day, sess in iter_sessions():
        _collect_files(day, [f for f in sorted(sess.iterdir()) if f.is_file()], days)
        days[day]["sessions"].append(sess.relative_to(ROOT).as_posix())
    for day, p in iter_align():
        _collect_files(day, [p], days)
    return days


def write_index() -> int:
    """生成 data-index.json —— 用户端「拉取弹幕」就靠它判断该拉哪几天。

    每个日期记下完整文件列表与逐文件**字节数 + sha256**:

      字节数 —— 用于显示"要下多少", 也用于抓截断的响应。
      sha256 —— 用户端可能从**第三方公共镜像**下载(官方 raw 在国内被限速,
                实测 7MB 要 30 秒以上), 那些镜像不受我们控制。只比字节数发现不了
                等长篡改, 而数据不可再生 —— 等长的坏文件比下载失败严重得多。

    哈希是流式算的, 21MB 约 0.1 秒, 不构成发布负担。
    """
    days = _build_days()
    idx = {
        "schema_version": 1,
        # 程序版本: 用户端用它判断"我的代码是不是旧的"。
        # 老清单没有这个字段 —— 读端必须容忍缺失(当作未知), 不能报错。
        "app_version": _app_version(),
        "generated_at": None,       # 由调用方填, 便于人眼判断新鲜度
        "latest": max(days) if days else None,
        "days": {k: days[k] for k in sorted(days)},
    }
    from datetime import datetime, timezone
    idx["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ROOT / "data-index.json"
    out.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    total_mb = sum(v["bytes"] for v in days.values()) / 1024 / 1024
    print(f"生成 {out.relative_to(ROOT)}: {len(days)} 天, "
          f"{sum(len(v['files']) for v in days.values())} 文件, {total_mb:.1f} MB")
    print(f"  最新: {idx['latest']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip-afp", action="store_true",
                    help="生成不含 afp 的发行副本到 dist/")
    ap.add_argument("--index", action="store_true",
                    help="生成 data-index.json(用户端增量更新用)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.index:
        problems = []
        for day, sess in iter_sessions():
            problems.extend(check_session(day, sess))
        if problems:
            print("校验未通过, 拒绝生成清单:")
            for p in problems:
                print("  - " + p)
            return 1
        return write_index()

    if args.strip_afp:
        problems = []
        for day, sess in iter_sessions():
            problems.extend(check_session(day, sess))
        problems.extend(check_align())
        if problems:
            print("校验未通过, 拒绝打包:")
            for p in problems:
                print("  - " + p)
            return 1
        return strip_afp()

    problems = []
    n = 0
    for day, sess in iter_sessions():
        n += 1
        problems.extend(check_session(day, sess))
    problems.extend(check_align())

    if args.json:
        print(json.dumps({"sessions": n, "problems": problems},
                         ensure_ascii=False, indent=2))
        return 1 if problems else 0

    print(f"检查 {n} 个会话 + align/ 记录")
    if problems:
        print(f"\n发现 {len(problems)} 个问题:")
        for p in problems:
            print("  - " + p)
        print("\nFAIL: 修复后才能发布")
        return 1
    print("\nOK: 可以发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())
