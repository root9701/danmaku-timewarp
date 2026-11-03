# -*- coding: utf-8 -*-
"""pushctl.py — 数据提交的推送策略、设置与手动入口。

职责:
  - 判断哪些提交可以推: 只允许 `data/` 与 `data-index.json`;
  - 读写采集控制台的推送设置(`danmaku-collector/run/push.json`, 本机运行状态);
  - 给 `nightly.py` 提供自动推送, 给控制台提供 status / push / settings 三个子命令。

边界:
  - 只推 `origin/main`; 不 force、不推 tag、不推其它分支、不建 Release。
  - 手动推送也要过"只推数据"的检查; 非数据提交一律停下等人工。
  - 本模块不 import 播放端任何代码, 采集端可以把它当独立 CLI 调用。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitpush  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "origin"
BRANCH = "main"
DEFAULTS = {"auto": True, "retries": 3, "delay_sec": 10}
LIMITS = {"retries": (1, 5), "delay_sec": (5, 120)}


def settings_path(root=None) -> Path:
    """设置文件位置。放 run/ 下, 已被仓库根的忽略规则覆盖。"""
    return Path(root or ROOT) / "danmaku-collector" / "run" / "push.json"


def _clamp(value, lo, hi, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize(raw) -> dict:
    """把任意输入收敛成合法设置。越界值夹到边界, 不是报错 —— 界面输入不该让保存失败。"""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "auto": _as_bool(raw.get("auto"), DEFAULTS["auto"]),
        "retries": _clamp(raw.get("retries"), *LIMITS["retries"], DEFAULTS["retries"]),
        "delay_sec": _clamp(raw.get("delay_sec"), *LIMITS["delay_sec"],
                            DEFAULTS["delay_sec"]),
    }


def load_settings(root=None) -> dict:
    p = settings_path(root)
    try:
        return normalize(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return normalize({})


def save_settings(raw, root=None) -> dict:
    """原子写设置。半写的 JSON 会让下一轮巡检退回默认值, 所以先写临时文件再替换。"""
    cfg = normalize(raw)
    p = settings_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    return cfg


def _git(root, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def pending_commits(root=None) -> list:
    """本地领先 origin/main 的提交。远端引用不存在时抛 RuntimeError。"""
    root = Path(root or ROOT)
    out = _git(root, "rev-list", f"{REMOTE}/{BRANCH}..HEAD")
    return [x for x in out.split() if x]


def _commit_paths(root, sha: str) -> list:
    # --no-renames: 重命名会把"data/x -> README.md"折叠成一行, 只看新路径会漏判。
    out = _git(root, "show", "--pretty=format:", "--name-only", "--no-renames", sha)
    return [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]


def is_data_only(root=None, commits=None) -> bool:
    """待推提交是否只动数据。混入代码/文档/配置或夹带合并提交时返回 False。"""
    root = Path(root or ROOT)
    if commits is None:
        commits = pending_commits(root)
    if not commits:
        return True
    if _git(root, "rev-list", "--merges", f"{REMOTE}/{BRANCH}..HEAD").split():
        return False
    for sha in commits:
        for path in _commit_paths(root, sha):
            if path != "data-index.json" and not path.startswith("data/"):
                return False
    return True


def status(root=None) -> dict:
    """给控制台看的只读状态: 设置、待推数量、是否只含数据。"""
    root = Path(root or ROOT)
    cfg = load_settings(root)
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        commits = pending_commits(root) if branch == BRANCH else []
        data_only = is_data_only(root, commits) if commits else True
        error = ""
    except RuntimeError as e:
        branch, commits, data_only, error = "", [], False, str(e)
    return {
        "ok": True,
        "settings": cfg,
        "branch": branch,
        "pending": len(commits),
        "data_only": data_only,
        "can_push": bool(branch == BRANCH and commits and data_only),
        "error": error,
    }


def push(root=None, manual: bool = False) -> dict:
    """推待推的数据提交。manual=True 时忽略"自动推送"开关, 但仍只推数据。"""
    root = Path(root or ROOT)
    cfg = load_settings(root)
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch != BRANCH:
            return {"ok": False, "reason": "not-main", "count": 0,
                    "message": f"当前分支 {branch}，只推 {BRANCH}"}
        commits = pending_commits(root)
        if not commits:
            return {"ok": True, "reason": "nothing", "count": 0,
                    "message": "没有待推送的数据提交"}
        if not is_data_only(root, commits):
            return {"ok": False, "reason": "non-data", "count": len(commits),
                    "message": f"有 {len(commits)} 个待推提交含非数据改动，需人工推送"}
        if not manual and not cfg["auto"]:
            return {"ok": False, "reason": "auto-off", "count": len(commits),
                    "message": f"有 {len(commits)} 个数据提交待推，自动推送已关闭"}
    except RuntimeError as e:
        return {"ok": False, "reason": "error", "count": 0,
                "message": f"推送检查失败: {e}"}
    delays = (cfg["delay_sec"], min(120, cfg["delay_sec"] * 3))
    ok, out = gitpush.push(REMOTE, BRANCH, timeout=90 if manual else 180,
                           attempts=cfg["retries"], delays=delays, cwd=root)
    if ok:
        return {"ok": True, "reason": "pushed", "count": len(commits),
                "message": f"已推送 {len(commits)} 个数据提交"}
    return {"ok": False, "reason": "push-failed", "count": len(commits),
            "message": "推送失败，稍后重试", "detail": out[-500:]}


def auto_push(root=None) -> dict:
    """夜间流水线入口: 受"自动推送"开关控制。"""
    return push(root, manual=False)


def _print_human(cmd: str, data: dict) -> None:
    if cmd == "settings":
        s = data["settings"]
        print(f"自动推送 {'开' if s['auto'] else '关'}，"
              f"重试 {s['retries']} 次，间隔 {s['delay_sec']} 秒")
        return
    if cmd == "status":
        s = data["settings"]
        print(f"自动推送: {'开' if s['auto'] else '关'}")
        print(f"待推提交: {data['pending']}（{'可推' if data['can_push'] else '不可推'}）")
        if data.get("error"):
            print(f"检查失败: {data['error']}")
        return
    print(data.get("message", ""))
    if data.get("detail"):
        print(data["detail"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据提交推送: 状态 / 手动推送 / 设置")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="查看设置与待推提交")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("push", help="推送待推的数据提交")
    p.add_argument("--manual", action="store_true",
                   help="忽略自动推送开关（控制台按钮用）")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("settings", help="修改并保存推送设置")
    p.add_argument("--auto", choices=("on", "off"))
    p.add_argument("--retries", type=int)
    p.add_argument("--delay", type=int)
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "status":
        data = status()
    elif args.cmd == "push":
        data = push(manual=args.manual)
    else:
        cur = load_settings()
        if args.auto is not None:
            cur["auto"] = args.auto == "on"
        if args.retries is not None:
            cur["retries"] = args.retries
        if args.delay is not None:
            cur["delay_sec"] = args.delay
        data = {"ok": True, "settings": save_settings(cur)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        _print_human(args.cmd, data)
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
