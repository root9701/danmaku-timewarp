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
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "danmaku-collector" / "data"
ALIGN = ROOT / "danmaku-server" / "align"
DIST = ROOT / "dist"


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


def check_align() -> list:
    """align/ 的一致性检查。

    规则: **只有带 pages(真的算出 offset)的记录**才要求 BV 能在 lists 里找到 ——
    时间窗只在"算 offset"时用得上。无 pages 的条目是失败标记或历史残留(比如会话
    目录后来被合并改名、或那场其实属于另一天), 它们不影响播放, 不该阻止发布。
    """
    problems = []
    if not ALIGN.is_dir():
        return problems
    for p in sorted(ALIGN.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"align/{p.name}: 解析失败 {e}")
            continue
        if not isinstance(d, dict):
            problems.append(f"align/{p.name}: 顶层不是对象")
            continue
        lists = d.get("lists") or {}
        pairs = set()
        for day, lst in lists.items():
            for it in (lst or []):
                if isinstance(it, dict) and it.get("bv"):
                    pairs.add((day, str(it["bv"])))
        for key, rec in (d.get("align") or {}).items():
            if not isinstance(rec, dict):
                continue
            pages = rec.get("pages")
            if not isinstance(pages, dict) or not pages:
                continue                      # 无 offset 的记录, 不参与判定
            bv = str(rec.get("bv") or "").strip()
            if not bv:
                continue
            day = str(key).split("|")[0]
            if (day, bv) not in pairs:
                problems.append(
                    f"align/{p.name}: {key} 有 {len(pages)} 页对齐结果, "
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
    # align/ 一并带上
    if ALIGN.is_dir():
        shutil.copytree(ALIGN, DIST / "align")
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"生成 {DIST.relative_to(ROOT)}/")
    print(f"  会话 {n_sess} 个, 剔除 afp {n_afp} 个文件")
    print(f"  体积 {size / 1024 / 1024:.1f} MB")
    print("  注意: 分发版**不能重新校准**(没有指纹), 但可以直接回放。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip-afp", action="store_true",
                    help="生成不含 afp 的发行副本到 dist/")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

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
