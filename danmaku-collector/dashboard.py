# -*- coding: utf-8 -*-
"""弹幕采集器本地控制台：网页上自行配置、测试、启停采集。
用法: 双击 控制台.bat，或 .venv 的 python 运行本文件，浏览器打开 http://127.0.0.1:8787
"""
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

import collector
import datablocks
import tasks

PORT = int(os.environ.get("DANMAKU_CONSOLE_PORT", "8787"))
BLOCKS_TTL = 10.0
_BLOCKS_CACHE = {"key": None, "ts": 0.0, "data": []}
STATE = {"task": None, "stop_event": None, "reason": None, "mode": None,
         "started_at": None, "queue_task": None, "queue_stop": None,
         "deadline": None, "current_task": None, "manual_stop": False,
         "pipeline": None, "pipeline_log": None, "push_running": False}
TASKS_PATH = collector.SCRIPT_DIR / "run" / "tasks.json"
WEB_DIR = collector.SCRIPT_DIR / "web"
ROOT = collector.SCRIPT_DIR.parent
NIGHTLY = ROOT / "tools" / "nightly.py"
PUSHCTL = ROOT / "tools" / "pushctl.py"
PIPELINE_INTERVAL = 3600     # 每小时巡检一次


def _read_json_config():
    path = collector.SCRIPT_DIR / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return dict(collector.DEFAULT_CONFIG)


def _latest_file(pattern_dir, pattern):
    files = sorted(Path(pattern_dir).glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _run_pushctl(*args, timeout=360):
    """跑 tools/pushctl.py 并取最后一行 JSON。

    为什么走子进程而不是 import: 采集端要能独立部署, 不能 import 播放端/工具链;
    pushctl 只依赖标准库, 用控制台自己的解释器就能跑。失败也返回 JSON, 让前端
    统一按 ok=false 处理。
    """
    if not PUSHCTL.is_file():
        return {"ok": False, "error": "当前不是仓库布局，无法推送（缺 tools/pushctl.py）"}
    try:
        r = subprocess.run([sys.executable, str(PUSHCTL), *args, "--json"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"推送工具运行失败: {e}"}
    out = (r.stdout or "").strip()
    if out:
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError:
            pass
    tail = ((r.stderr or "") + (r.stdout or "")).strip()[-500:]
    return {"ok": False, "error": tail or "推送工具无输出"}


async def api_config(request):
    if request.method == "GET":
        return web.json_response({"ok": True, "config": _read_json_config()})
    data = await request.json()
    try:
        rooms = [int(x) for x in data.get("rooms", []) if str(x).strip()]
        if not rooms:
            raise ValueError("房间号不能为空")
        earliest = str(data.get("earliest_start") or "now").strip()
        collector.parse_earliest(earliest, datetime.now())
        latest = str(data.get("latest_end") or "").strip() or None
        if latest:
            collector.next_occurrence(latest, datetime.now())
        poll = max(5, int(data.get("poll_interval_sec") or 30))
    except (ValueError, TypeError) as e:
        return web.json_response({"ok": False, "error": str(e)})
    old = _read_json_config()   # 控制台未展示的字段(audio_fp/slice_minutes 等)原样保留
    _sm = old.get("slice_minutes")
    # data_dir / log_dir 必须沿用原值: 控制台不编辑它们, 写死默认值会把用户
    # 配好的 "data_dir": "../data" 悄悄改回 "data", 表现为"保存配置后数据跑到了
    # 采集端目录, 播放端突然读不到"。缺省时才回落默认。
    cfg = {"rooms": rooms, "earliest_start": earliest, "latest_end": latest,
           "poll_interval_sec": poll,
           "data_dir": str(old.get("data_dir") or "data"),
           "log_dir": str(old.get("log_dir") or "logs"),
           "audio_fp": bool(old.get("audio_fp", True)),
           "slice_minutes": max(0, int(_sm if _sm is not None else 60))}
    (collector.SCRIPT_DIR / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return web.json_response({"ok": True})


def _acquire_collector_lock():
    """拿采集单实例锁; 拿不到返回 None(已有采集进程在写同一目录)。"""
    cfg = collector.load_config(collector.parse_args([]))
    lock = collector.SingleInstanceLock(Path(cfg["log_dir"]) / "collector.lock")
    return lock if lock.acquire() else None


async def api_start(request):
    if STATE.get("queue_task") and not STATE["queue_task"].done():
        return web.json_response({"ok": False, "error": "队列正在运行，请先停止队列"})
    if STATE["task"] and not STATE["task"].done():
        return web.json_response({"ok": False, "error": "已有任务在运行，请先停止"})
    data = await request.json()
    mode = data.get("mode")
    if mode not in ("night", "test"):
        return web.json_response({"ok": False, "error": "mode 必须是 night 或 test"})
    lock = _acquire_collector_lock()
    if lock is None:
        return web.json_response(
            {"ok": False, "error": "另一个采集进程正在运行（锁被占用），请先确认没有双开"})
    args = collector.parse_args([])
    if mode == "test":
        args.test = min(600, max(10, int(data.get("seconds") or 60)))
    cfg = collector.load_config(args)

    stop_event = asyncio.Event()
    reason = {"value": None}

    async def runner():
        keep = mode == "night"
        if keep:
            collector.set_keep_awake(True)
        try:
            await collector.amain(cfg, args, stop_event, reason)
        except Exception:
            collector.log.exception("控制台任务异常")
        finally:
            if keep:
                collector.set_keep_awake(False)
            lock.release()

    STATE.update(task=asyncio.create_task(runner()), stop_event=stop_event, reason=reason,
                 mode=mode, started_at=datetime.now().strftime("%H:%M:%S"))
    return web.json_response({"ok": True})


async def api_stop(request):
    """结束本次(task, 默认) 或 停止整个队列(queue)。两者都会触发收尾。"""
    scope = (request.query.get("scope") or "task").strip()
    running = bool(STATE["task"] and not STATE["task"].done())
    if scope == "queue":
        qstop = STATE.get("queue_stop")
        if qstop is None:
            return web.json_response({"ok": False, "error": "当前没有运行中的队列"})
        qstop.set()
        if running:
            STATE["manual_stop"] = True
            STATE["stop_event"].set()
        return web.json_response({"ok": True})
    if not running:
        return web.json_response({"ok": False, "error": "当前没有运行中的任务"})
    STATE["manual_stop"] = True
    STATE["stop_event"].set()
    return web.json_response({"ok": True})


def _data_dir():
    """采集端实际使用的数据目录, 取自 config.json 的 data_dir(相对脚本目录解析)。

    不写死 SCRIPT_DIR/"data": 本机把数据放在仓库根的 data/, 靠 config.json 的
    "../data" 指过去。写死会让控制台的"最近结果/弹幕预览"永远读不到东西,
    而采集进程明明在正常写盘 —— 这种只在一处失效的错位最难查。
    """
    return collector.load_config(collector.parse_args([]))["data_dir"]


def _latest_meta():
    """最新的会话 meta。

    目录布局有几代, 都要能找到(取最新那个):
      * 新:  data/{数据日}/{会话ID}/{base}.meta.json      (会话目录化, 当前写入格式)
      * 旧:  data/{数据日}/{base}.meta.json                (平铺, 历史数据)
      * 更旧: data/{base}.meta.json                        (无日期层)
    """
    data_dir = _data_dir()
    for pattern in ("*/*/*meta.json", "*/*meta.json", "*meta.json"):
        found = _latest_file(data_dir, pattern)
        if found:
            return found
    return None


def _gz_for_meta(meta_file):
    """meta 对应的数据 gzip: 优先同名 {stem}.jsonl.gz(旧格式/测试), 切片模式下退而取
    最新一片 {stem}_s????.jsonl.gz(展示最近弹幕够用; 完整数据由 danmaku-server 聚合)。
    meta 与切片同在会话目录里, 所以只需在 meta_file.parent 里找。"""
    gz = Path(str(meta_file).replace(".meta.json", ".jsonl.gz"))
    if gz.exists():
        return gz
    base = gz.name[: -len(".jsonl.gz")]
    cands = sorted(gz.parent.glob(base + "_s????.jsonl.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else gz


async def api_danmaku(request):
    import gzip as gzmod
    from collections import deque
    meta_file = _latest_meta()
    if not meta_file:
        return web.json_response({"ok": False, "error": "还没有数据文件，先跑一次测试采集"})
    gz = _gz_for_meta(meta_file)
    if not gz.exists():
        return web.json_response({"ok": False, "error": "数据压缩包不存在: %s" % gz.name})
    n = min(200, max(1, int(request.query.get("n", 50))))
    try:
        with gzmod.open(gz, "rt", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=n)  # 流式读, 大文件也不吃内存
    except OSError as e:
        return web.json_response({"ok": False, "error": "读取失败: %s" % e})
    rows = []
    for ln in tail:
        try:
            o = json.loads(ln)
            rows.append({"time": datetime.fromtimestamp(o["ts"] / 1000).strftime("%H:%M:%S"),
                         "un": o.get("un"), "t": o.get("t"), "ty": o.get("ty")})
        except (ValueError, KeyError, TypeError):
            pass
    rows.reverse()  # 新的在前
    return web.json_response({"ok": True,
                              "file": str(gz.relative_to(collector.SCRIPT_DIR)),
                              "count": len(rows), "rows": rows})


def _load_tasks() -> dict:
    """读任务表; 文件不存在/损坏时返回空表(不阻断控制台启动)。"""
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tasks": []}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return {"tasks": []}
    return data


def _save_tasks(data: dict) -> None:
    """原子写任务表: 写到一半被杀不会留下半截 JSON。

    路径在 run/ 下(整目录不入库): 任务带房间号与状态, 属于本机运行状态,
    不进仓库, 也不需要为此改 .gitignore。
    """
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_PATH.with_name(TASKS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TASKS_PATH)


def _mark_task(task: dict, status: str) -> None:
    """按 (date,start,end) 定位并改状态。UI 编辑任务时请避开正在跑的那条。"""
    data = _load_tasks()
    for t in data["tasks"]:
        if (t.get("date"), t.get("start"), t.get("end")) == (
                task.get("date"), task.get("start"), task.get("end")):
            t["status"] = status
            break
    _save_tasks(data)


def _reset_stale_tasks() -> None:
    """控制台启动时把上一次异常退出留下的 running 改成 stopped。

    否则队列会永远认为"有任务在跑", 再也不会开下一场。
    """
    data = _load_tasks()
    changed = False
    for t in data["tasks"]:
        if t.get("status") == "running":
            t["status"] = "stopped"
            changed = True
    if changed:
        _save_tasks(data)


async def api_tasks(request):
    if request.method == "GET":
        data = _load_tasks()
        dates = []
        for t in data["tasks"]:
            try:
                dates.append(tasks.data_date(t))
            except (ValueError, TypeError):
                dates.append("")
        return web.json_response({"ok": True, "tasks": data["tasks"],
                                  "data_dates": dates})
    try:
        data = await request.json()
        items = data.get("tasks")
        if not isinstance(items, list):
            raise ValueError("tasks 必须是数组")
        problems = tasks.validate(items)
        if problems:
            return web.json_response({"ok": False, "error": "；".join(problems)})
    except (ValueError, TypeError) as e:
        return web.json_response({"ok": False, "error": str(e)})
    _save_tasks({"tasks": items})
    return web.json_response({"ok": True})


async def api_tasks_draft(request):
    """按赛程生成任务草稿(不落盘; UI 确认后随「保存任务」一起提交)。"""
    data = _load_schedule()
    if not data.get("lists"):
        return web.json_response({"ok": False, "error": "还没有赛程，先点「刷新赛程」"})
    rooms = collector.load_config(collector.parse_args([]))["rooms"]
    if not rooms:
        return web.json_response({"ok": False, "error": "先在默认配置里填房间号"})
    return web.json_response({"ok": True,
                              "tasks": tasks.draft_from_lists(data["lists"], rooms)})


async def api_extend(request):
    """延长本次采集。截止后不允许续: 会话已 finalize, 再延长只能开新会话。"""
    try:
        minutes = int((await request.json()).get("minutes") or 0)
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "minutes 必须是整数"})
    if minutes <= 0:
        return web.json_response({"ok": False, "error": "延长分钟数必须大于 0"})
    deadline = STATE.get("deadline")
    running = bool(STATE["task"] and not STATE["task"].done())
    if not running or deadline is None:
        return web.json_response({"ok": False, "error": "当前没有运行中的任务"})
    deadline.extend(minutes)
    return web.json_response({"ok": True, "end": deadline.at.strftime("%H:%M")})


async def _queue_task(task: dict) -> None:
    """跑一条任务: 到点开录 -> 到截止收尾 -> 标记完成。"""
    cfg = collector.load_config(collector.parse_args([]))
    cfg = dict(cfg, rooms=[int(r) for r in task["rooms"]],
               earliest_start="now", latest_end="")
    _, end = tasks.window(task)
    deadline = collector.Deadline(end) if end else None
    stop_event, reason = asyncio.Event(), {"value": None}
    STATE.update(task=None, stop_event=stop_event, reason=reason, mode="queue",
                 started_at=datetime.now().strftime("%H:%M:%S"),
                 deadline=deadline, current_task=task, manual_stop=False)
    _mark_task(task, "running")
    try:
        STATE["task"] = asyncio.create_task(
            collector.amain(cfg, collector.parse_args([]), stop_event, reason,
                            deadline=deadline))
        await STATE["task"]
    finally:
        qstop = STATE.get("queue_stop")
        status = "stopped" if STATE.get("manual_stop") or (qstop and qstop.is_set()) else "done"
        _mark_task(task, status)
        STATE.update(deadline=None, current_task=None, task=None)


async def _pause(stop_event, seconds: float) -> None:
    """等待指定秒数, 期间被要求停止就提前返回。"""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


def _pipeline_start() -> bool:
    """随队列启动夜间发布(独立子进程)。发布失败不影响采集 —— 这是硬边界。"""
    proc = STATE.get("pipeline")
    if proc is not None and proc.poll() is None:
        return True
    if not NIGHTLY.is_file():
        collector.log.warning("夜间发布不可用: 缺少 %s", NIGHTLY)
        return False
    log_path = collector.SCRIPT_DIR / "logs" / "nightly.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        STATE["pipeline"] = subprocess.Popen(
            [sys.executable, str(NIGHTLY), "--loop", "--interval", str(PIPELINE_INTERVAL)],
            cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=flags,
            env=dict(os.environ, PYTHONUNBUFFERED="1"))
    except OSError as e:
        collector.log.warning("夜间发布启动失败(采集继续): %s", e)
        log_file.close()
        return False
    STATE["pipeline_log"] = log_file
    collector.log.info("夜间发布已启动(PID %s)", STATE["pipeline"].pid)
    return True


def _pipeline_stop() -> None:
    """停止夜间发布子进程; 只影响发布, 不碰采集。"""
    proc, STATE["pipeline"] = STATE.get("pipeline"), None
    if proc is not None and proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
    log_file = STATE.pop("pipeline_log", None)
    if log_file is not None:
        with contextlib.suppress(Exception):
            log_file.close()


async def _queue_loop() -> None:
    """队列守护: 取下一场 -> 等开始 -> 跑完 -> 再取。关窗或停止队列时退出。"""
    qstop = STATE["queue_stop"]
    collector.set_keep_awake(True)      # 等待下一场时也不让电脑睡
    lock = STATE.get("lock")
    _pipeline_start()
    try:
        while not qstop.is_set():
            if STATE["task"] and not STATE["task"].done():
                await _pause(qstop, 10)
                continue
            nxt = tasks.next_task(_load_tasks()["tasks"], datetime.now())
            if not nxt:
                await _pause(qstop, 20)
                continue
            if nxt["action"] == "missed":
                _mark_task(nxt, "missed")
                continue
            if nxt["action"] == "wait":
                start, _end = tasks.window(nxt)
                await collector.wait_until(start, "等待任务开始", qstop)
                continue
            await _queue_task(nxt)
    finally:
        _pipeline_stop()
        STATE["queue_task"] = None
        collector.set_keep_awake(False)
        if lock is not None:
            lock.release()
            STATE["lock"] = None


async def api_queue_start(request):
    if STATE.get("queue_task") and not STATE["queue_task"].done():
        return web.json_response({"ok": False, "error": "队列已在运行"})
    if STATE["task"] and not STATE["task"].done():
        return web.json_response({"ok": False, "error": "已有任务在运行，请先停止"})
    lock = _acquire_collector_lock()
    if lock is None:
        return web.json_response(
            {"ok": False, "error": "另一个采集进程正在运行（锁被占用），请先确认没有双开"})
    STATE["lock"] = lock
    STATE["queue_stop"] = asyncio.Event()
    STATE["queue_task"] = asyncio.create_task(_queue_loop())
    return web.json_response({"ok": True})


async def api_queue_stop(request):
    qstop = STATE.get("queue_stop")
    if qstop is None:
        return web.json_response({"ok": False, "error": "当前没有运行中的队列"})
    qstop.set()
    if STATE["task"] and not STATE["task"].done():
        STATE["manual_stop"] = True
        STATE["stop_event"].set()
    return web.json_response({"ok": True})


def _latest_data_date() -> str:
    """最近一个带 align.json 的数据日。"""
    data_dir = _data_dir()
    if not data_dir.is_dir():
        return ""
    for day in sorted(data_dir.iterdir(), reverse=True):
        if (day.is_dir() and len(day.name) == 10
                and (day / "align.json").is_file()):
            return day.name
    return ""


def _load_schedule() -> dict:
    """读分发层的赛程(align.json 的 lists)。读不到就是空, 不报错。"""
    day = _latest_data_date()
    if not day:
        return {"ok": True, "date": "", "lists": {}}
    try:
        d = json.loads((_data_dir() / day / "align.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "date": day, "lists": {}}
    return {"ok": True, "date": day, "lists": d.get("lists") or {}}


async def api_schedule(request):
    return web.json_response(_load_schedule())


async def api_schedule_refresh(request):
    """调夜间流水线的赛程发现(子进程, 不 import 播放端)。"""
    root = collector.SCRIPT_DIR.parent
    script = root / "tools" / "nightly.py"
    if not script.is_file():
        return web.json_response(
            {"ok": False, "error": "当前不是仓库布局, 无法刷新赛程(缺 tools/nightly.py)"})
    day = _latest_data_date() or datetime.now().strftime("%Y-%m-%d")

    def _run():
        return subprocess.run(
            [sys.executable, str(script), "--discover-only", "--date", day],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            timeout=300)

    try:
        r = await asyncio.to_thread(_run)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"刷新赛程失败: {e}"})
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-3:])
        return web.json_response({"ok": False, "error": f"刷新赛程失败: {tail}"})
    data = _load_schedule()
    data["output"] = (r.stdout or "").strip()[-500:]
    return web.json_response(data)


def _room_list(value) -> list:
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    out = []
    for r in value or []:
        try:
            out.append(int(r))
        except (TypeError, ValueError):
            pass
    return out


def _default_rooms() -> list:
    try:
        return _room_list(_read_json_config().get("rooms"))
    except (OSError, ValueError):
        return []


def _push_scope() -> dict:
    """数据块展示范围: 当前任务优先, 否则取最新有数据的一天。"""
    task = STATE.get("current_task")
    if task:
        try:
            rooms = _room_list(task.get("rooms")) or _default_rooms()
            return {"date": tasks.data_date(task), "rooms": rooms,
                    "source": "current"}
        except (ValueError, TypeError):
            pass
    data_dir = ROOT / "data"
    days = []
    if data_dir.is_dir():
        days = sorted(p.name for p in data_dir.iterdir()
                      if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    date = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    day = data_dir / date
    rooms = sorted(int(p.name) for p in day.iterdir()
                   if p.is_dir() and p.name.isdigit()) if day.is_dir() else []
    return {"date": date, "rooms": rooms, "source": "latest"}


def _calibration_info(date: str) -> dict:
    path = ROOT / "data" / date / "align.json"
    if not path.is_file():
        return {"ok": False, "message": "还没有对齐记录"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "message": "align.json 读取失败"}
    items = []
    for entry in (data.get("lists") or {}).get(date) or []:
        if not isinstance(entry, dict):
            continue
        pages = entry.get("pages") or {}
        total = entry.get("total_pages")
        if not isinstance(total, int):
            total = len(pages)
        items.append({"title": str(entry.get("title") or ""),
                      "aligned": len(pages), "total": total})
    if not items:
        return {"ok": False, "message": "还没有对齐记录"}
    return {"ok": True, "items": items}


def _plan_info(now=None) -> dict:
    """给前端的采集时钟: 当前/下一任务、计划起止、服务器当前时间。"""
    now = now or datetime.now()
    task = STATE.get("current_task")
    kind = "current" if task else "none"
    if task is None:
        try:
            task = tasks.next_task(_load_tasks()["tasks"], now)
        except Exception:
            task = None
        kind = "next" if task else "none"
    if not task or task.get("action") == "missed":
        return {"now_ts": now.timestamp() * 1000, "task_kind": "none"}
    try:
        start, end = tasks.window(task)
    except (ValueError, TypeError):
        return {"now_ts": now.timestamp() * 1000, "task_kind": "none"}
    return {"now_ts": now.timestamp() * 1000, "task_kind": kind,
            "plan_start_ts": start.timestamp() * 1000,
            "plan_end_ts": end.timestamp() * 1000 if end else None,
            "plan_task": task}


def _blocks_for(scope: dict, failed: bool, remote_head: str) -> list:
    key = (scope["date"], tuple(scope["rooms"]), bool(failed), remote_head)
    now = time.monotonic()
    if _BLOCKS_CACHE["key"] == key and now - _BLOCKS_CACHE["ts"] < BLOCKS_TTL:
        return _BLOCKS_CACHE["data"]
    running = bool(STATE["task"] and not STATE["task"].done())
    blocks = datablocks.scan(ROOT, scope["date"], scope["rooms"],
                             datetime.now(), push_failed=failed,
                             running=running)
    _BLOCKS_CACHE.update(key=key, ts=now, data=blocks)
    return blocks


async def api_push_status(request):
    data = await asyncio.to_thread(_run_pushctl, "status", timeout=60)
    data["running"] = bool(STATE.get("push_running"))
    scope = _push_scope()
    data["scope"] = {"date": scope["date"], "rooms": scope["rooms"],
                     "source": scope["source"]}
    data["calibration"] = _calibration_info(scope["date"])
    last = data.get("last_push") or {}
    failed = "失败" in str(last.get("message") or "")
    data["blocks"] = await asyncio.to_thread(
        _blocks_for, scope, failed, data.get("remote_head", ""))
    return web.json_response(data)


async def api_push_log(request):
    return web.json_response(await asyncio.to_thread(_run_pushctl, "log", timeout=60))


async def api_push(request):
    if STATE.get("push_running"):
        return web.json_response({"ok": False, "error": "正在推送，请稍候"})
    STATE["push_running"] = True
    try:
        data = await asyncio.to_thread(_run_pushctl, "push", "--manual", timeout=360)
    finally:
        STATE["push_running"] = False
    if not data.get("ok") and data.get("message") and not data.get("error"):
        data["error"] = data["message"]
    return web.json_response(data)


async def api_push_settings(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "设置格式不对"})
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "设置格式不对"})
    args = ["settings",
            "--auto", "on" if body.get("auto", True) else "off",
            "--retries", str(body.get("retries", 3)),
            "--delay", str(body.get("delay_sec", 10))]
    data = await asyncio.to_thread(_run_pushctl, *args, timeout=60)
    if not data.get("ok") and data.get("message") and not data.get("error"):
        data["error"] = data["message"]
    return web.json_response(data)


async def api_status(request):
    running = bool(STATE["task"] and not STATE["task"].done())
    log_tail = ""
    log_file = _latest_file(collector.SCRIPT_DIR / "logs", "collector_*.log")
    if log_file:
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-40:])
        except OSError:
            pass
    last_result = None
    meta_file = _latest_meta()
    if meta_file:
        try:
            m = json.loads(meta_file.read_text(encoding="utf-8"))
            gz = _gz_for_meta(meta_file)
            last_result = {"dm_count": m.get("dm_count"), "sc_count": m.get("sc_count"),
                           "reconnect_count": m.get("reconnect_count"),
                           "record_start": m.get("record_start"), "record_end": m.get("record_end"),
                           "ended_by": m.get("ended_by"), "room": m.get("room"),
                           "gz_path": str(gz) if gz.exists() else None}
        except (OSError, ValueError):
            pass
    queue_running = bool(STATE.get("queue_task") and not STATE["queue_task"].done())
    proc = STATE.get("pipeline")
    pipeline_running = bool(proc is not None and proc.poll() is None)
    deadline = STATE.get("deadline")
    data = {"running": running, "mode": STATE["mode"],
            "started_at": STATE["started_at"],
            "queue_running": queue_running,
            "pipeline_running": pipeline_running,
            "current_task": STATE.get("current_task"),
            "deadline": deadline.at.strftime("%H:%M") if deadline else None,
            "finished_reason": STATE["reason"]["value"] if (not running and STATE["reason"]) else None,
            "log_tail": log_tail, "last_result": last_result}
    data.update(_plan_info())
    return web.json_response(data)


async def on_shutdown(app):
    _pipeline_stop()
    qstop = STATE.get("queue_stop")
    if qstop is not None:
        qstop.set()
    if STATE["task"] and not STATE["task"].done():
        STATE["stop_event"].set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(STATE["task"], timeout=20)
    qt = STATE.get("queue_task")
    if qt is not None and not qt.done():
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(qt, timeout=5)
    lock = STATE.get("lock")
    if lock is not None:                 # 队列没来得及走 finally 时的兜底
        lock.release()
        STATE["lock"] = None


def main():
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")
    cfg = collector.load_config(collector.parse_args([]))
    collector.setup_logging(cfg["log_dir"])
    _reset_stale_tasks()
    import os
    import threading
    import webbrowser

    def _open_browser():
        with contextlib.suppress(Exception):
            webbrowser.open("http://127.0.0.1:%d" % PORT)

    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.5, _open_browser).start()
    app = web.Application()
    app.router.add_get("/", lambda r: web.FileResponse(WEB_DIR / "index.html"))
    app.router.add_static("/web/", WEB_DIR)
    app.router.add_route("*", "/api/config", api_config)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_route("*", "/api/tasks", api_tasks)
    app.router.add_post("/api/tasks/draft", api_tasks_draft)
    app.router.add_post("/api/extend", api_extend)
    app.router.add_post("/api/queue/start", api_queue_start)
    app.router.add_post("/api/queue/stop", api_queue_stop)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/danmaku", api_danmaku)
    app.router.add_get("/api/schedule", api_schedule)
    app.router.add_post("/api/schedule/refresh", api_schedule_refresh)
    app.router.add_get("/api/push", api_push_status)
    app.router.add_get("/api/push/log", api_push_log)
    app.router.add_post("/api/push", api_push)
    app.router.add_post("/api/push/settings", api_push_settings)
    app.on_shutdown.append(on_shutdown)
    print("=" * 46)
    print("  弹幕采集控制台已启动")
    print("  浏览器打开: http://127.0.0.1:%d" % PORT)
    print("  采集运行期间请勿关闭本窗口（可最小化）")
    print("=" * 46)
    try:
        web.run_app(app, host="127.0.0.1", port=PORT, print=None)
    except OSError as e:
        print("[错误] 端口 %d 启动失败(%s)。可能已有一个控制台在运行，" 
              "关掉旧的黑窗口再试。" % (PORT, e))
        input("按回车键退出…")


if __name__ == "__main__":
    main()
