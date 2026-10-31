# -*- coding: utf-8 -*-
"""弹幕采集器本地控制台：网页上自行配置、测试、启停采集。
用法: 双击 控制台.bat，或 .venv 的 python 运行本文件，浏览器打开 http://127.0.0.1:8787
"""
import asyncio
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path

from aiohttp import web

import collector
import tasks

PORT = 8787
STATE = {"task": None, "stop_event": None, "reason": None, "mode": None,
         "started_at": None, "queue_task": None, "queue_stop": None,
         "deadline": None, "current_task": None, "manual_stop": False}
TASKS_PATH = collector.SCRIPT_DIR / "run" / "tasks.json"

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>弹幕采集控制台</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --txt:#e6e9ef; --dim:#8b93a3;
          --acc:#4c8dff; --ok:#3fb96f; --warn:#e0a23c; --err:#e05c5c; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif; padding:24px; }
  .wrap { max-width:980px; margin:0 auto; }
  h1 { font-size:20px; display:flex; align-items:center; gap:12px; margin-bottom:18px; }
  .pill { font-size:12px; padding:3px 12px; border-radius:999px; background:#2a2f3a; color:var(--dim); }
  .pill.run { background:#12351f; color:var(--ok); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin-bottom:12px; color:var(--dim); font-weight:600; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px 20px; }
  label { display:block; font-size:12px; color:var(--dim); margin-bottom:4px; }
  input { width:100%; background:#10131a; color:var(--txt); border:1px solid var(--line);
          border-radius:6px; padding:8px 10px; font-size:14px; }
  input:focus { outline:none; border-color:var(--acc); }
  .hint { font-size:12px; color:var(--dim); margin-top:4px; }
  .btns { display:flex; gap:10px; margin-top:14px; flex-wrap:wrap; }
  button { border:none; border-radius:6px; padding:10px 20px; font-size:14px; cursor:pointer;
           color:#fff; background:#3a4150; }
  button:hover { filter:brightness(1.15); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  #btn-night { background:var(--acc); }
  #btn-test { background:var(--ok); }
  #btn-stop { background:var(--err); }
  .row { display:flex; gap:10px; align-items:center; }
  #log { background:#0a0c10; border:1px solid var(--line); border-radius:6px; padding:10px;
         font:12px/1.5 Consolas,monospace; height:260px; overflow-y:auto; white-space:pre-wrap;
         word-break:break-all; color:#9fb0c8; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td { padding:4px 8px; border-bottom:1px solid var(--line); }
  td:first-child { color:var(--dim); width:130px; }
  .warn { color:var(--warn); font-size:13px; }
  #toast { position:fixed; top:16px; right:16px; background:#2a2f3a; border-left:3px solid var(--acc);
           padding:10px 16px; border-radius:6px; display:none; font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>弹幕采集控制台 <span id="status" class="pill">加载中</span></h1>

  <div class="card">
    <h2>采集配置（保存后对下一次启动生效）</h2>
    <div class="grid">
      <div>
        <label>直播间房间号（多个用英文逗号分隔）</label>
        <input id="cfg-rooms" placeholder="例如 7734200">
        <div class="hint">填你要采集的直播间房间号；B站直播间地址里的数字即房间号</div>
      </div>
      <div>
        <label>最早开始时刻</label>
        <div class="row">
          <input id="cfg-start" placeholder="00:30">
          <button style="padding:8px 10px" onclick="setStart('00:30')">今晚00:30</button>
          <button style="padding:8px 10px" onclick="setStart('now')">立即</button>
        </div>
        <div class="hint">格式 HH:MM（到点才开始轮询）/ now（立即）/ +5m（5分钟后）</div>
      </div>
      <div>
        <label>最晚结束时刻（留空 = 不自动停）</label>
        <input id="cfg-end" placeholder="09:00">
        <div class="hint">到点自动收尾；也可随时点"停止"</div>
      </div>
      <div>
        <label>轮询间隔（秒，未开播时多久查一次）</label>
        <input id="cfg-poll" type="number" min="5" placeholder="30">
      </div>
    </div>
    <div class="btns">
      <button onclick="saveConfig()">保存配置</button>
      <span class="hint" id="cfg-saved" style="align-self:center"></span>
    </div>
  </div>

  <div class="card">
    <h2>运行控制</h2>
    <div class="btns" style="margin-top:0">
      <button id="btn-night" onclick="start('night')">开始今晚挂机</button>
      <div class="row">
        <button id="btn-test" onclick="start('test')">测试采集</button>
        <input id="test-sec" type="number" min="10" max="600" value="60" style="width:80px">
        <span class="hint">秒（房间用上面配置里的第一个）</span>
      </div>
      <button id="btn-stop" onclick="stopRun()" disabled>停止</button>
    </div>
    <div class="warn" style="margin-top:10px" id="run-warn" hidden>
      采集运行期间请勿关闭黑色控制台窗口（可最小化）。
    </div>
  </div>

  <div class="card">
    <h2>实时日志</h2>
    <div id="log">等待中…</div>
  </div>

  <div class="card">
    <h2>最近一次结果</h2>
    <table id="result"><tr><td>暂无</td><td></td></tr></table>
    <div class="btns" style="margin-top:10px">
      <button onclick="showDanmaku()">查看弹幕内容（最近一次采集）</button>
    </div>
    <div id="dm-list" style="margin-top:10px; background:#0a0c10; border:1px solid var(--line);
         border-radius:6px; padding:10px; font:12px/1.8 Consolas,monospace; max-height:240px;
         overflow-y:auto" hidden></div>
  </div>
</div>
<div id="toast"></div>
<script>
let timer = null;
const $ = id => document.getElementById(id);

function toast(msg, ok) {
  const t = $("toast");
  t.textContent = msg;
  t.style.borderLeftColor = ok ? "var(--ok)" : "var(--err)";
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 3000);
}
function setStart(v) { $("cfg-start").value = v; }

async function loadConfig() {
  const r = await (await fetch("/api/config")).json();
  if (r.ok) {
    $("cfg-rooms").value = (r.config.rooms || []).join(",");
    $("cfg-start").value = r.config.earliest_start ?? "";
    $("cfg-end").value = r.config.latest_end ?? "";
    $("cfg-poll").value = r.config.poll_interval_sec ?? 30;
  }
}
async function saveConfig() {
  const body = {
    rooms: $("cfg-rooms").value.split(/[,，\s]+/).filter(Boolean),
    earliest_start: $("cfg-start").value.trim() || "now",
    latest_end: $("cfg-end").value.trim() || "",
    poll_interval_sec: parseInt($("cfg-poll").value || "30", 10),
  };
  const r = await (await fetch("/api/config", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})).json();
  toast(r.ok ? "配置已保存" : "保存失败: " + r.error, r.ok);
  if (r.ok) $("cfg-saved").textContent = "已保存 " + new Date().toLocaleTimeString();
}

async function start(mode) {
  const body = mode === "test" ? {mode: "test", seconds: parseInt($("test-sec").value || "60", 10)}
                               : {mode: "night"};
  const r = await (await fetch("/api/start", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})).json();
  toast(r.ok ? (mode === "test" ? "测试已开始" : "挂机已开始") : "启动失败: " + r.error, r.ok);
  refresh();
}
async function stopRun() {
  if (!confirm("确定停止采集？已录数据会正常收尾。")) return;
  const r = await (await fetch("/api/stop", {method: "POST"})).json();
  toast(r.ok ? "已发出停止指令，正在收尾…" : "失败: " + r.error, r.ok);
}

async function refresh() {
  try {
    const s = await (await fetch("/api/status")).json();
    const pill = $("status");
    if (s.running) {
      pill.className = "pill run";
      pill.textContent = "运行中 · " + (s.mode === "test" ? "测试采集" : "今晚挂机") +
        (s.started_at ? " · 自 " + s.started_at : "");
    } else {
      pill.className = "pill";
      pill.textContent = s.finished_reason ? "已结束 · " + s.finished_reason : "空闲";
    }
    $("btn-stop").disabled = !s.running;
    $("btn-night").disabled = s.running;
    $("btn-test").disabled = s.running;
    $("run-warn").hidden = !s.running || s.mode !== "night";
    const logBox = $("log");
    logBox.textContent = s.log_tail || "（暂无日志）";
    logBox.scrollTop = logBox.scrollHeight;
    const tb = $("result");
    if (s.last_result) {
      const m = s.last_result;
      tb.innerHTML = [
        ["弹幕条数", m.dm_count], ["SC 条数", m.sc_count], ["断线重连", m.reconnect_count],
        ["录制起", m.record_start], ["录制止", m.record_end],
        ["结束原因", m.ended_by], ["房间", m.room], ["数据文件", m.gz_path || "-"],
      ].map(x => "<tr><td>" + x[0] + "</td><td>" + x[1] + "</td></tr>").join("");
    }
  } catch (e) { /* 忽略单次轮询失败 */ }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
}
async function showDanmaku() {
  const box = $("dm-list");
  box.hidden = false;
  box.textContent = "加载中…";
  const r = await (await fetch("/api/danmaku?n=50")).json();
  if (!r.ok) { box.textContent = r.error; return; }
  box.innerHTML = "<div style='color:var(--dim)'>文件: " + escapeHtml(r.file) +
    "（显示最近 " + r.count + " 条，新的在前）</div>" +
    r.rows.map(x => "<div>" + (x.ty === "sc" ? "[SC] " : "") + escapeHtml(x.time) + " " +
      escapeHtml(x.un || "") + ": " + escapeHtml(x.t || "") + "</div>").join("");
}

loadConfig();
refresh();
timer = setInterval(refresh, 2000);
</script>
</body>
</html>"""


def _read_json_config():
    path = collector.SCRIPT_DIR / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return dict(collector.DEFAULT_CONFIG)


def _latest_file(pattern_dir, pattern):
    files = sorted(Path(pattern_dir).glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


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


async def api_start(request):
    if STATE.get("queue_task") and not STATE["queue_task"].done():
        return web.json_response({"ok": False, "error": "队列正在运行，请先停止队列"})
    if STATE["task"] and not STATE["task"].done():
        return web.json_response({"ok": False, "error": "已有任务在运行，请先停止"})
    data = await request.json()
    mode = data.get("mode")
    if mode not in ("night", "test"):
        return web.json_response({"ok": False, "error": "mode 必须是 night 或 test"})
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


async def _queue_loop() -> None:
    """队列守护: 取下一场 -> 等开始 -> 跑完 -> 再取。关窗或停止队列时退出。"""
    qstop = STATE["queue_stop"]
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
        STATE["queue_task"] = None


async def api_queue_start(request):
    if STATE.get("queue_task") and not STATE["queue_task"].done():
        return web.json_response({"ok": False, "error": "队列已在运行"})
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
    deadline = STATE.get("deadline")
    return web.json_response({"running": running, "mode": STATE["mode"],
                              "started_at": STATE["started_at"],
                              "queue_running": queue_running,
                              "current_task": STATE.get("current_task"),
                              "deadline": deadline.at.strftime("%H:%M") if deadline else None,
                              "finished_reason": STATE["reason"]["value"] if (not running and STATE["reason"]) else None,
                              "log_tail": log_tail, "last_result": last_result})


async def on_shutdown(app):
    qstop = STATE.get("queue_stop")
    if qstop is not None:
        qstop.set()
    if STATE["task"] and not STATE["task"].done():
        STATE["stop_event"].set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(STATE["task"], timeout=20)


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
    app.router.add_get("/", lambda r: web.Response(text=INDEX_HTML, content_type="text/html"))
    app.router.add_route("*", "/api/config", api_config)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_route("*", "/api/tasks", api_tasks)
    app.router.add_post("/api/extend", api_extend)
    app.router.add_post("/api/queue/start", api_queue_start)
    app.router.add_post("/api/queue/stop", api_queue_stop)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/danmaku", api_danmaku)
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
