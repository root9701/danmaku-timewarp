"""采集任务表: 按日期排队的纯调度逻辑。

文件读写与启动采集由 dashboard 负责; 这里只算"下一步该做什么", 便于单测。
任务字段:
  date    开播日期(YYYY-MM-DD); end 早于 start 视为跨零点, 数据日按结束日算
  start   开始时刻(HH:MM)
  end     截止时刻(HH:MM), 留空 = 不自动停
  rooms   房间号列表
  enabled 是否启用(默认 true)
  status  pending / running / done / stopped / missed
"""
import re
from datetime import datetime, timedelta

HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_hhmm(text: str) -> tuple:
    """'HH:MM' -> (时, 分); 非法抛 ValueError。"""
    m = HHMM_RE.fullmatch(str(text or "").strip())
    if not m:
        raise ValueError(f"时刻格式不对: {text}(应为 HH:MM)")
    return int(m.group(1)), int(m.group(2))


def window(task: dict) -> tuple:
    """任务的 (开始, 截止) 本地时间。end <= start 时截止顺延到次日。"""
    if not DATE_RE.fullmatch(str(task.get("date") or "")):
        raise ValueError(f"任务日期格式不对: {task.get('date')}")
    day = datetime.strptime(task["date"], "%Y-%m-%d")
    h0, m0 = parse_hhmm(task.get("start"))
    start = day.replace(hour=h0, minute=m0)
    if not str(task.get("end") or "").strip():
        return start, None
    h1, m1 = parse_hhmm(task["end"])
    end = day.replace(hour=h1, minute=m1)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def data_date(task: dict) -> str:
    """该任务的数据目录日期(会话按结束时刻归日)。"""
    _, end = window(task)
    return (end or window(task)[0]).date().isoformat()


def next_task(tasks: list, now: datetime) -> dict:
    """下一步动作。

    返回任务副本 + action:
      start = 已在窗口内(含"错过开始但没到结束", 立即开录)
      wait  = 最早一场还没到开始
      missed = 已过结束
    有 running 任务时返回 {} —— 队列一次只跑一个任务。
    """
    if any(t.get("status") == "running" for t in tasks):
        return {}
    pending = [t for t in tasks if t.get("enabled", True)
               and t.get("status", "pending") == "pending"]
    if not pending:
        return {}
    best = None
    for t in pending:
        start, end = window(t)
        if end is not None and now >= end:
            return {**t, "action": "missed"}
        action = "start" if now >= start else "wait"
        if best is None or start < best[0]:
            best = (start, {**t, "action": action})
    return best[1]


def draft_from_lists(lists: dict, rooms: list, lead_min: int = 30,
                     tail_min: int = 60) -> list:
    """把赛程 lists 转成任务草稿(只返回, 不落盘; 由 UI 确认后保存)。

    开始 = 首场开赛前 lead_min; 截止 = 末场开赛 + 预计时长 + tail_min。
    Bo5(任一场分P >= 4)按 5.5 小时, 其余按 3.5 小时 —— 只是草稿, 可再改。
    """
    out = []
    for match_date, items in sorted((lists or {}).items()):
        valid = [it for it in items or []
                 if isinstance(it, dict) and it.get("start_ms")]
        if not valid:
            continue
        valid.sort(key=lambda it: it["start_ms"])
        first, last = valid[0], valid[-1]
        pages = max(int(it.get("total_pages") or 0) for it in valid)
        hours = 5.5 if pages >= 4 else 3.5
        start = (datetime.fromtimestamp(first["start_ms"] / 1000)
                 - timedelta(minutes=lead_min))
        end = (datetime.fromtimestamp(last["start_ms"] / 1000)
               + timedelta(hours=hours, minutes=tail_min))
        titles = " / ".join(str(it.get("title") or "") for it in valid)
        out.append({
            "date": start.strftime("%Y-%m-%d"),
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "rooms": list(rooms or []),
            "enabled": True,
            "status": "pending",
            "note": titles,
        })
    return out


def validate(tasks: list) -> list:
    """校验任务表。返回问题列表; 空 = 可以保存。"""
    problems = []
    windows = []
    for i, t in enumerate(tasks):
        tag = f"第 {i + 1} 条({t.get('date') or '无日期'})"
        try:
            start, end = window(t)
        except (ValueError, TypeError) as e:
            problems.append(f"{tag}: {e}")
            continue
        if end is not None and end <= start:
            problems.append(f"{tag}: 截止必须晚于开始")
        rooms = t.get("rooms")
        if not isinstance(rooms, list) or not rooms:
            problems.append(f"{tag}: 房间号为空")
        elif not all(isinstance(r, int) and r > 0 for r in rooms):
            problems.append(f"{tag}: 房间号必须是正整数")
        if str(t.get("status") or "pending") not in (
                "pending", "running", "done", "stopped", "missed"):
            problems.append(f"{tag}: 状态非法")
        windows.append((tag, start, end, set(rooms or [])))
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            tag_a, sa, ea, ra = windows[i]
            tag_b, sb, eb, rb = windows[j]
            if not (ra & rb):
                continue
            # 无截止视为一直持续: 与任何有交集的窗口都算重叠
            a_inf = ea is None
            b_inf = eb is None
            overlap = (a_inf or b_inf or ea > sb) and (b_inf or a_inf or eb > sa)
            if overlap:
                problems.append(f"{tag_a} 与 {tag_b} 时间重叠(同一房间)")
    return problems
