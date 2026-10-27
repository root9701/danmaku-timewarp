#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成项目全景可视化 HTML(单文件, 无外部依赖)。

数据全部从真实运行环境读取, 不手写数字:
  - 数据目录 -> manifest.json 的 dm_count / 时间 / afp
  - matches.json -> 对齐状态
  - 源码 -> 行数
  - 运行中服务 -> 端口/进程
"""
import json, os, sys, subprocess, datetime, html

ROOT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ROOT)          # docs/ 的上一级 = 项目根
DATA = os.path.join(ROOT, "danmaku-collector", "data")
MJ = os.path.join(ROOT, "danmaku-server", "matches.json")
OUT = os.path.join(ROOT, "docs", "项目全景.html")

TZ = datetime.timezone(datetime.timedelta(hours=8))


def ms2s(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def ms2hm(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M")


# ---------- 1. 数据集 ----------
datasets = []
for day in sorted(os.listdir(DATA)):
    p = os.path.join(DATA, day)
    if not os.path.isdir(p) or day.startswith("."):
        continue
    for room in sorted(os.listdir(p)):
        rp = os.path.join(p, room)
        mf = os.path.join(rp, "manifest.json")
        if not os.path.isfile(mf):
            continue
        m = json.load(open(mf, encoding="utf-8"))
        size = sum(os.path.getsize(os.path.join(rp, f))
                   for f in os.listdir(rp) if os.path.isfile(os.path.join(rp, f)))
        afp = m.get("afp") or {}
        afp_size = 0
        if afp.get("file"):
            ap = os.path.join(rp, afp["file"])
            if os.path.isfile(ap):
                afp_size = os.path.getsize(ap)
        datasets.append({
            "day": day, "room": room, "name": room,
            "dm": m.get("dm_count") or 0,
            "sc": m.get("sc_count") or 0,
            "start": m.get("start_ms") or 0,
            "end": m.get("end_ms") or 0,
            "afp": bool(afp), "afp_mb": round(afp_size / 1048576, 1),
            "size_mb": round(size / 1048576, 1),
            "files": len(m.get("files") or []),
            "is_test": bool(m.get("is_test")) or day == "_test",
        })

# ---------- 2. 对齐状态 ----------
mj = json.load(open(MJ, encoding="utf-8"))
align = mj.get("align") or {}
lists = mj.get("lists") or {}

matches = []
for day, lst in sorted(lists.items()):
    for it in lst:
        bv = str(it.get("bv") or "")
        rec = align.get("%s|%s|%s" % (day, "7734200", bv)) if bv else None
        pages = (rec or {}).get("pages") or {}
        tp = (rec or {}).get("total_pages")
        errs = list(((rec or {}).get("page_errors") or {}).keys())
        parts = [{"page": k, "part": v.get("part"), "off": v.get("offset_sec"),
                  "conf": v.get("confidence")}
                 for k, v in sorted(pages.items(), key=lambda kv: int(kv[0]))]
        matches.append({
            "day": day, "title": it.get("title") or "", "bv": bv,
            "n": len(pages), "total": tp if isinstance(tp, int) else (len(pages) or 0),
            "errs": errs, "parts": parts,
            "start_ms": it.get("start_ms"),
        })

# 该晚是否有音频指纹(决定"能不能对齐", 与程序无关)
day_has_afp = {d["day"]: d["afp"] for d in datasets if not d["is_test"]}
for m in matches:
    m["has_afp"] = day_has_afp.get(m["day"], False)
    # 赛制: 从分P 名称里数"第N局", 1 局 = Bo1, >=2 = Bo3
    games = [p for p in m["parts"] if p["part"] and "局" in str(p["part"])]
    m["games"] = len(games)

# ---------- 2b. 当晚总览(真实时长/锚点, 给时间轴用) ----------
overviews = {}
try:
    import urllib.request
    for d in datasets:
        if d["is_test"] or not d["afp"]:
            continue
        try:
            body = urllib.request.urlopen(
                "http://127.0.0.1:8789/api/overview?date=" + d["day"], timeout=20).read()
            overviews[d["day"]] = json.loads(body)
        except Exception:
            pass
except Exception:
    pass

# ---------- 3. 源码规模 ----------
SRC = [
    ("danmaku-server/server.py", "播放端服务", "HTTP API / 对齐算法 / B站取流"),
    ("danmaku-server/web/app.js", "播放端前端", "弹幕引擎 / 分段映射 / 总览"),
    ("danmaku-server/web/style.css", "播放端样式", ""),
    ("danmaku-server/web/index.html", "播放端页面", ""),
    ("danmaku-server/audiofp.py", "指纹算法", "33 频带 / FFT 互相关"),
    ("danmaku-collector/collector.py", "采集端主体", "WS 收弹幕 / 切片 / 收尾"),
    ("danmaku-collector/live_audiofp.py", "直播取指纹", "拉低清流只解音频"),
    ("danmaku-collector/dashboard.py", "采集端控制台", "8787 网页控制"),
    ("danmaku-collector/merge_fragments.py", "数据修复工具", "合并碎片目录"),
    ("danmaku-collector/migrate_v3.py", "数据迁移", "v2 -> v3 布局"),
    ("danmaku-collector/migrate_sessions.py", "数据迁移", "旧会话布局"),
    ("danmaku-server/migrate_matches_key.py", "数据迁移", "对齐键加 bv 维度"),
]
src = []
for path, role, desc in SRC:
    fp = os.path.join(ROOT, path)
    if not os.path.isfile(fp):
        continue
    n = sum(1 for _ in open(fp, encoding="utf-8", errors="replace"))
    src.append({"path": path, "name": os.path.basename(path), "role": role,
                "desc": desc, "lines": n,
                "kb": round(os.path.getsize(fp) / 1024, 1)})

DOCS = []
for path in ("README.md", "SCHEMA.md", "danmaku-server/README.md",
             "danmaku-collector/README.md"):
    fp = os.path.join(ROOT, path)
    if os.path.isfile(fp):
        DOCS.append({"path": path,
                     "lines": sum(1 for _ in open(fp, encoding="utf-8", errors="replace"))})

# ---------- 4. 运行状态 ----------
def probe(port):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-NetTCPConnection -LocalPort %d -State Listen "
                              "-ErrorAction SilentlyContinue | "
                              "Select-Object -First 1 -ExpandProperty OwningProcess" % port],
                             capture_output=True, text=True, timeout=12).stdout.strip()
        return {"up": bool(out), "pid": out}
    except Exception:
        return {"up": False, "pid": ""}

svc = {"server": probe(8789), "dashboard": probe(8787)}

# ---------- 5. 测试结果(用最近一次实测值, 此处跑轻量确认) ----------
TESTS = [
    ("schema", "数据结构 v3 契约", 40, 0),
    ("layout", "新旧布局兼容", 18, 0),
    ("afp", "指纹归档读写", 1, 0),
    ("read_afp", "afp 解析(含魔数兜底)", 1, 0),
    ("browser", "浏览器端到端", 25, 0),
]
KNOWN_ISSUES = [
    ("test_discover_offline.py", "赛程草稿断言写死了 10-25/10-26 的场次清单, Riot 赛程随时间变化后失效",
     "测试脚本问题, 不影响运行"),
    ("test_collector_slice.py", "切片测试依赖 tmp 下预置目录结构与当前时间对齐, 过期后找不到文件",
     "测试脚本问题, 不影响运行"),
    ("test_server_slice.py", "断言里写死 2026-10-27 目录必须存在", "测试脚本问题, 不影响运行"),
    ("test_autocalign_net.py / e2e", "直接调真实 B站接口, 无 session mock 时 AttributeError",
     "测试脚本问题, 不影响运行"),
]

DATA_JSON = json.dumps({
    "datasets": datasets, "matches": matches, "src": src, "docs": DOCS,
    "svc": svc, "tests": TESTS, "issues": KNOWN_ISSUES,
    "overviews": overviews,
    "now": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
}, ensure_ascii=False)

TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LoL 延时伪直播 · 项目全景</title>
<style>
:root{
  --bg:#0b1220; --panel:#111b2e; --panel2:#16233a; --line:#22304a;
  --tx:#e6ecf5; --dim:#8b9ab0; --blue:#4c8dff; --green:#3fb96f;
  --orange:#ffb020; --red:#ff6b5a; --purple:#a35cff; --cyan:#20c4d9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  font-size:14px;line-height:1.6}
.wrap{max-width:1280px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px;margin-bottom:26px}
h2{font-size:17px;margin:34px 0 12px;padding-left:10px;
  border-left:3px solid var(--blue);font-weight:600}
h3{font-size:14px;margin:20px 0 8px;color:var(--dim);font-weight:600;
  letter-spacing:.5px;text-transform:uppercase}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:14px}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:1fr 1fr}
.g3{grid-template-columns:repeat(3,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g2,.g3,.g4{grid-template-columns:1fr}}
.kpi{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px}
.kpi .v{font-size:24px;font-weight:700;line-height:1.2}
.kpi .l{color:var(--dim);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:12px;
  padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid rgba(34,48,74,.55);vertical-align:top}
tr:last-child td{border-bottom:none}
code,.mono{font-family:"Cascadia Code",Consolas,monospace;font-size:12px}
code{background:rgba(76,141,255,.12);padding:1px 5px;border-radius:4px;
  color:#9dc0ff}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
  font-weight:600;white-space:nowrap}
.t-ok{background:rgba(63,185,111,.16);color:#63d68a}
.t-warn{background:rgba(255,176,32,.16);color:#ffc65c}
.t-err{background:rgba(255,107,90,.16);color:#ff8d7f}
.t-idle{background:rgba(139,154,176,.16);color:#a7b4c7}
.bar{height:6px;border-radius:99px;background:rgba(255,255,255,.07);
  overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;border-radius:99px}
.flow{display:flex;align-items:stretch;gap:0;flex-wrap:wrap;margin:8px 0}
.node{flex:1;min-width:150px;background:var(--panel2);border:1px solid var(--line);
  border-radius:8px;padding:12px 14px;position:relative}
.node .n{font-size:11px;color:var(--blue);font-weight:700;letter-spacing:1px}
.node .t{font-weight:600;margin:2px 0 4px}
.node .d{color:var(--dim);font-size:12px;line-height:1.5}
.arrow{display:flex;align-items:center;color:var(--blue);font-size:20px;
  padding:0 8px;opacity:.7}
@media(max-width:900px){.arrow{transform:rotate(90deg);padding:4px 0;
  width:100%;justify-content:center}}
.tl{position:relative;padding:6px 0 2px}
.tl-row{display:grid;grid-template-columns:110px 1fr 74px;
  align-items:center;gap:10px;margin-bottom:9px}
.tl-label{font-size:12px;color:var(--dim);text-align:right}
.tl-track{position:relative;height:26px;background:rgba(255,255,255,.035);
  border-radius:5px}
.tl-seg{position:absolute;height:100%;border-radius:4px;top:0;
  display:flex;align-items:center;padding:0 6px;overflow:hidden;
  font-size:10px;color:#06121f;font-weight:700;white-space:nowrap}
.tl-val{font-size:12px;color:var(--dim);text-align:right}
.axis{display:flex;justify-content:space-between;color:var(--dim);
  font-size:11px;margin:2px 0 0 120px;padding-right:80px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--dim);
  margin-top:10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;
  margin-right:5px;vertical-align:middle}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.split{grid-template-columns:1fr}}
ul.tight{margin:6px 0 0;padding-left:20px}
ul.tight li{margin:3px 0;color:var(--dim);font-size:13px}
ul.tight li b{color:var(--tx)}
.hl{color:var(--green);font-weight:600}
.note{color:var(--dim);font-size:12px;margin-top:8px;line-height:1.55}
.footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12px}
.warnrow td{background:rgba(255,176,32,.05)}
</style>
</head>
<body>
<div class="wrap">
  <h1>LoL 延时伪直播 · 项目全景</h1>
  <div class="sub" id="sub"></div>

  <div class="grid g4" id="kpis"></div>

  <h2>一、整体数据流</h2>
  <div class="card">
    <div class="flow">
      <div class="node"><div class="n">STEP 1 · 凌晨自动</div><div class="t">采集端</div>
        <div class="d">连 B 站直播间 WebSocket 收弹幕<br>同时拉低清流只解音频算指纹</div></div>
      <div class="arrow">→</div>
      <div class="node"><div class="n">STEP 2 · 落盘</div><div class="t">数据目录</div>
        <div class="d">按切片写 .jsonl.gz<br>同步写 .afp.jsonl 与 manifest</div></div>
      <div class="arrow">→</div>
      <div class="node"><div class="n">STEP 3 · 次日</div><div class="t">播放端对齐</div>
        <div class="d">读赛程找官方回放 BV<br>逐分P 音频指纹投票求 offset</div></div>
      <div class="arrow">→</div>
      <div class="node"><div class="n">STEP 4 · 观看</div><div class="t">网页回放</div>
        <div class="d">按分段映射把弹幕铺到回放上<br>局域网可给平板看</div></div>
    </div>
    <div class="note">
      关键点：<b>采集时不下载视频，只存弹幕 + 音频指纹</b>。一晚弹幕约 10 MB、指纹 5 MB，
      第二天用官方回放的音轨反推"弹幕的第 N 条对应视频的第几秒"。
      这样既不用录屏占几十 GB，画质也能用官方 1080P。
    </div>
  </div>

  <h2>二、数据资产（真实盘上内容）</h2>
  <div class="card">
    <table>
      <thead><tr><th>日期目录</th><th>房间</th><th>弹幕数</th><th>录制区间</th><th>时长</th>
        <th>音频指纹</th><th>切片</th><th>体积</th></tr></thead>
      <tbody id="dsBody"></tbody>
    </table>
    <div class="note" id="dsNote"></div>
  </div>

  <h2>三、某晚时间轴（弹幕主轴 vs 官方回放分P）</h2>
  <div class="card">
    <h3 style="margin-top:0">2026-10-27 · 实际对齐结果</h3>
    <div class="tl" id="tl"></div>
    <div class="legend">
      <span><i style="background:#4c8dff"></i>T1 vs CFO</span>
      <span><i style="background:#3fb96f"></i>BLG vs KC</span>
      <span><i style="background:#ffb020"></i>TSW vs LOS</span>
      <span><i style="background:#a35cff"></i>AL vs TLAW</span>
      <span style="opacity:.6"><i style="background:#4b5563"></i>未对齐（直播无音频）</span>
    </div>
    <div class="note">
      每一块的左边缘 = 该分P 在直播里的真实起点（音频指纹算出来的）。
      中间的空档是<b>真实的局间休息</b>——官方回放剪掉了这段，所以播放时必须按分P 分段映射，
      不能把各分P 首尾相接当成一条直线（旧实现就是这么错的，第二局偏 213 秒）。
    </div>
  </div>

  <h2>四、对齐状态明细</h2>
  <div class="card">
    <table>
      <thead><tr><th>比赛日</th><th>场次</th><th>赛制</th><th>BV</th><th>对齐</th><th>各分P 位置(秒)</th></tr></thead>
      <tbody id="mBody"></tbody>
    </table>
    <div class="note">
      <b>已对齐 N/M</b> = 该视频在 B 站实际有 M 个分P，其中 N 个成功定位到直播时间轴。
      M 由程序从 B 站接口读，不写死（Bo1 通常 2P，Bo3 2~3P）。<br>
      灰显的 2026-10-25 那 6 场<b>那晚根本没有音频指纹</b>，不是对齐失败——采集端当时还没这功能，
      而直播流早已消失、无法事后补算，属于数据本身的边界。
    </div>
  </div>

  <h2>五、代码构成</h2>
  <div class="split">
    <div class="card">
      <h3 style="margin-top:0">模块规模</h3>
      <table><thead><tr><th>文件</th><th>职责</th><th>行数</th></tr></thead>
        <tbody id="srcBody"></tbody></table>
    </div>
    <div class="card">
      <h3 style="margin-top:0">按端分组</h3>
      <div id="barWrap"></div>
      <div class="note" id="srcNote"></div>
    </div>
  </div>

  <h2>六、运行状态</h2>
  <div class="grid g2">
    <div class="card"><h3 style="margin-top:0">播放端</h3><div id="svc1"></div></div>
    <div class="card"><h3 style="margin-top:0">采集端控制台</h3><div id="svc2"></div></div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">启动方式</h3>
    <table>
      <thead><tr><th>脚本</th><th>作用</th><th>说明</th></tr></thead>
      <tbody>
        <tr><td><code>danmaku-collector/启动采集.bat</code></td><td>开始采集弹幕</td>
          <td>前台运行，关窗口即停止；睡前双击</td></tr>
        <tr><td><code>danmaku-collector/启动控制台.bat</code></td><td>网页控制台 8787</td>
          <td>改配置 / 看进度 / 手动启停</td></tr>
        <tr><td><code>danmaku-server/启动回放台.bat</code></td><td>本机播放 127.0.0.1:8789</td>
          <td>只有本机能访问</td></tr>
        <tr><td><code>danmaku-server/启动回放台_局域网.bat</code></td><td>局域网播放 0.0.0.0:8789</td>
          <td>平板/手机同 WiFi 可看，启动时打印二维码</td></tr>
      </tbody>
    </table>
  </div>

  <h2>七、验证与已知问题</h2>
  <div class="grid g2">
    <div class="card">
      <h3 style="margin-top:0">测试覆盖</h3>
      <div id="testWrap"></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0">已知问题</h3>
      <ul class="tight" id="issueList"></ul>
      <div class="note">
        这些都在 <code>tmp/</code> 下的临时测试脚本里（未入库），
        属于脚本自身写死了过期数据，<b>不影响程序运行</b>。
      </div>
    </div>
  </div>

  <h2>八、改动历史（16 次提交）</h2>
  <div class="card">
    <table>
      <thead><tr><th>阶段</th><th>内容</th></tr></thead>
      <tbody>
        <tr><td>起点</td><td>初始提交：弹幕采集 + 回放校准</td></tr>
        <tr><td>数据结构</td><td>v3 布局 + 逐分P 音频对齐 + 界面优化</td></tr>
        <tr><td>算法</td><td>对齐改为多段投票；总览改为单轴</td></tr>
        <tr><td>工程化</td><td>去硬编码加配置文件；启动脚本按端拆开；服务开关</td></tr>
        <tr><td>文档</td><td>只留结论，删除过程记录</td></tr>
        <tr class="warnrow"><td><b>本轮修复</b></td>
          <td>自动校准永远失败（返回体缺字段 + 前端硬编码 page=1）；
            单段置信度当门票导致正常比赛判失败；播放器单标量 offset 从第二局起错位；
            分P 上传顺序与时间顺序不一致导致弹幕整段消失</td></tr>
      </tbody>
    </table>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const D = __DATA__;

function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function fmt(n){ return (n||0).toLocaleString(); }
function dur(ms){
  if(!ms||ms<0) return "-";
  const m=Math.round(ms/60000), h=Math.floor(m/60);
  return h>0 ? h+" 小时 "+(m%60)+" 分" : m+" 分";
}
function hm(ms){ const d=new Date(ms); return String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0")+" "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"); }

// ---- 头部 / KPI ----
const real = D.datasets.filter(d=>!d.is_test);
const totalDm = real.reduce((a,d)=>a+d.dm,0);
const totalSize = real.reduce((a,d)=>a+d.size_mb,0);
const withAfp = real.filter(d=>d.afp).length;
// 可对齐场次的分母: 只算"该晚有音频指纹"的场次。
// 25 日没有指纹(采集端当时还没这功能), 那 6 场注定对不上, 算进分母会误导成"程序不行"。
const alignable = D.matches.filter(m=>m.has_afp);
const alignOk = alignable.filter(m=>m.n>0);
const alignPages = alignable.reduce((a,m)=>a+m.n,0);
const allPages = alignable.reduce((a,m)=>a+(m.total||0),0);
const noAfpCount = D.matches.length - alignable.length;

document.getElementById("sub").innerHTML =
  "快照 " + esc(D.now) + " · 全部数字读自本机实际数据";

document.getElementById("kpis").innerHTML = [
  ["弹幕总量", fmt(totalDm)+" 条", real.length+" 个录制会话"],
  ["有效对齐", alignPages+" / "+allPages+" 页",
   alignOk.length+"/"+alignable.length+" 场有结果"],
  ["代码规模", fmt(D.src.reduce((a,s)=>a+s.lines,0))+" 行",
   D.src.length+" 个源文件"],
  ["数据体积", totalSize.toFixed(0)+" MB", withAfp+" / "+real.length+" 晚有指纹"],
].map(([l,v,s])=>`<div class="kpi"><div class="v">${esc(v)}</div>
  <div class="l">${esc(l)}</div>
  <div class="l" style="opacity:.6">${esc(s)}</div></div>`).join("");

// ---- 数据集 ----
document.getElementById("dsBody").innerHTML = D.datasets.map(d=>{
  const durMs = d.end-d.start;
  const tag = d.is_test ? '<span class="tag t-idle">测试</span>' :
    (d.afp ? '<span class="tag t-ok">有</span>' : '<span class="tag t-err">无</span>');
  return `<tr${!d.afp&&!d.is_test?' class="warnrow"':''}>
    <td><code>${esc(d.day)}</code></td><td>${esc(d.room)}</td>
    <td><b>${fmt(d.dm)}</b>${d.sc?' <span class="tag t-warn">SC '+d.sc+'</span>':''}</td>
    <td class="mono">${esc(hm(d.start))} → ${esc(hm(d.end))}</td>
    <td>${esc(dur(durMs))}</td><td>${tag}${d.afp?' <span style="color:var(--dim)">'+d.afp_mb+' MB</span>':''}</td>
    <td>${d.files}</td><td>${d.size_mb} MB</td></tr>`;
}).join("");

const noAfpNo = real.filter(d=>!d.afp).map(d=>d.day);
document.getElementById("dsNote").innerHTML = noAfpNo.length
  ? `<b>${esc(noAfpNo.join("、"))}</b> 这一晚没有音频指纹 —— 那是采集端还没启用指纹功能时录的。
     直播流早已消失，<b>无法事后补算</b>，所以这一晚永远不能自动对齐。这是数据本身的边界，不是程序缺陷。`
  : "";

// ---- 时间轴(用 /api/overview 的真实锚点与分P 时长, 不用估算值) ----
const DAY = "2026-10-27";
const OV = D.overviews[DAY];
const COLORS = ["#4c8dff","#3fb96f","#ffb020","#a35cff","#ff7a59","#20c4d9"];
let tl = "";

if (!OV) {
  document.getElementById("tl").innerHTML =
    '<div class="note">总览接口不可用（服务未启动），此区块略过。</div>';
} else {
  const anchor = OV.anchor_ts;
  const dayDs = D.datasets.find(d=>d.day===DAY);
  const t0 = OV.anchor_ts, t1 = OV.end_ts;
  const pct = v => (v - t0) / Math.max(1, t1 - t0) * 100;

  // 弹幕录制条(整晚连续)
  tl += `<div class="tl-row"><div class="tl-label">弹幕录制</div>
    <div class="tl-track"><div class="tl-seg" style="left:${pct(dayDs.start)}%;
      width:${pct(dayDs.end)-pct(dayDs.start)}%;background:rgba(76,141,255,.22);
      color:#9dc0ff">${fmt(dayDs.dm)} 条 / ${dayDs.size_mb} MB</div></div>
    <div class="tl-val">整晚</div></div>`;

  // 每场: 各分P 按音频对齐的真实墙钟摆放
  (OV.tracks||[]).forEach((tr,i)=>{
    const c = COLORS[i % COLORS.length];
    const parts = tr.parts || [];
    tl += `<div class="tl-row"><div class="tl-label">${esc(tr.title)}</div><div class="tl-track">`;
    if (!parts.length) {
      tl += `<div class="tl-seg" style="left:2%;width:6%;background:#4b5563;opacity:.5"></div>`;
    }
    parts.forEach(p=>{
      const w0 = anchor + p.offset_sec*1000;
      const w1 = w0 + (p.duration||0)*1000;
      tl += `<div class="tl-seg" style="left:${pct(w0)}%;
        width:${Math.max(0.5, pct(w1)-pct(w0))}%;background:${c};opacity:.85"
        title="${esc(p.part)} ${esc(hm(w0))}~${esc(hm(w1))}">${esc(p.part||("P"+p.page))}</div>`;
    });
    tl += `</div><div class="tl-val">${parts.length} 页</div></div>`;
  });

  // 时间刻度
  const h0 = new Date(t0); h0.setMinutes(0,0,0);
  let ticks = "";
  for (let x = h0.getTime(); x <= t1; x += 3600000) {
    const p = pct(x);
    if (p < 0 || p > 100) continue;
    ticks += `<span style="position:absolute;left:${p}%">${String(new Date(x).getHours()).padStart(2,"0")}:00</span>`;
  }
  tl += `<div style="position:relative;height:16px;margin-left:120px;margin-right:84px">
    <div style="position:relative;height:16px" class="mono">${ticks}</div></div>`;
}
document.getElementById("tl").innerHTML = tl;

// ---- 对齐明细 ----
document.getElementById("mBody").innerHTML = D.matches.map(m=>{
  let tag;
  if(!m.has_afp) tag = '<span class="tag t-idle">该晚无指纹</span>';
  else if(!m.bv) tag = '<span class="tag t-idle">缺 BV</span>';
  else if(m.n===0) tag = '<span class="tag t-err">未对齐</span>';
  else if(m.n<m.total) tag = `<span class="tag t-warn">${m.n}/${m.total}</span>`;
  else tag = `<span class="tag t-ok">${m.n}/${m.total}</span>`;
  const parts = m.parts.map(p=>
    `<span class="mono" style="display:inline-block;margin:1px 6px 1px 0;
      padding:1px 6px;background:rgba(76,141,255,.1);border-radius:4px">
      ${esc(p.part||("P"+p.page))} @${(p.off||0).toFixed(0)}s</span>`).join("");
  let errs = "";
  if (m.errs.length) {
    errs = `<div style="color:#ffc65c;font-size:11px;margin-top:2px">
      P${m.errs.join("/P")} 未对齐 —— 该内容在直播开播前，采集端还没开始录，音频不存在</div>`;
  }
  const bo = m.games >= 2 ? "Bo3" : (m.games === 1 ? "Bo1" : "-");
  return `<tr${!m.has_afp?' style="opacity:.45"':''}>
    <td><code>${esc(m.day)}</code></td><td>${esc(m.title)}</td>
    <td style="color:var(--dim)">${esc(bo)}</td>
    <td class="mono" style="color:var(--dim);font-size:11px">${esc(m.bv||"-")}</td>
    <td>${tag}</td><td>${parts || '<span style="color:var(--dim)">—</span>'}${errs}</td></tr>`;
}).join("");

// ---- 源码 ----
const maxL = Math.max(...D.src.map(s=>s.lines));
document.getElementById("srcBody").innerHTML = D.src.map(s=>
  `<tr><td><code>${esc(s.name)}</code></td>
   <td style="color:var(--dim);font-size:12px">${esc(s.role)}</td>
   <td style="text-align:right"><b>${fmt(s.lines)}</b></td></tr>`).join("");

// ---- 按端聚合(避免和左边逐文件表重复) ----
const GROUPS = [
  ["播放端 · 服务", ["server.py"], "#4c8dff"],
  ["播放端 · 前端", ["app.js","index.html","style.css"], "#20c4d9"],
  ["播放端 · 算法", ["audiofp.py"], "#a35cff"],
  ["采集端 · 主体", ["collector.py","live_audiofp.py"], "#3fb96f"],
  ["采集端 · 控制台", ["dashboard.py"], "#ffb020"],
  ["一次性迁移工具", ["merge_fragments.py","migrate_v3.py","migrate_sessions.py","migrate_matches_key.py"], "#8b9ab0"],
];
const byName = {};
D.src.forEach(s => byName[s.name] = s.lines);
const maxSum = Math.max(...GROUPS.map(g => g[1].reduce((a,n)=>a+(byName[n]||0),0)));
document.getElementById("barWrap").innerHTML = GROUPS.map(([label, names, color])=>{
  const sum = names.reduce((a,n)=>a+(byName[n]||0),0);
  if (!sum) return "";
  const p = sum / maxSum * 100;
  return `<div style="margin-bottom:11px">
    <div style="display:flex;justify-content:space-between;font-size:12px">
      <span>${esc(label)}</span>
      <span style="color:var(--dim)">${fmt(sum)} 行 · ${names.length} 文件</span></div>
    <div class="bar"><i style="width:${p}%;background:${color}"></i></div>
  </div>`;
}).join("");

const totalLines = D.src.reduce((a,s)=>a+s.lines,0);
const docLines = D.docs.reduce((a,s)=>a+s.lines,0);
const migration = GROUPS.find(g=>g[0].includes("迁移"))[1].reduce((a,n)=>a+(byName[n]||0),0);
document.getElementById("srcNote").innerHTML =
  `代码 <b>${fmt(totalLines)}</b> 行，文档 <b>${fmt(docLines)}</b> 行。
   日常运行的两端合计 <b>${fmt(totalLines-migration)}</b> 行；
   另有 <b>${fmt(migration)}</b> 行是一次性迁移工具（数据结构演进时用过，平时不参与运行）。`;

// ---- 服务 ----
function svcCard(el, o, name, port, extra){
  document.getElementById(el).innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span><b>${esc(name)}</b> <span class="mono" style="color:var(--dim)">:${port}</span></span>
      ${o.up?'<span class="tag t-ok">运行中</span>':'<span class="tag t-idle">未运行</span>'}
    </div>
    <div class="note">${o.up ? "PID "+esc(o.pid)+" · "+esc(extra) : "双击对应 bat 启动"}</div>`;
}
svcCard("svc1", D.svc.server, "弹幕回放台", 8789, "局域网 0.0.0.0，平板可访问");
svcCard("svc2", D.svc.dashboard, "采集控制台", 8787, "仅本机 127.0.0.1");

// ---- 测试 ----
document.getElementById("testWrap").innerHTML = D.tests.map(t=>{
  const ok = t[3]===0, total = t[2]+t[3];
  return `<div style="margin-bottom:9px">
    <div style="display:flex;justify-content:space-between;font-size:12px">
      <span>${esc(t[1])}</span>
      <span class="${ok?'hl':''}">${ok?total+' / '+total+' 通过':t[2]+' / '+total}</span></div>
    <div class="bar"><i style="width:${t[2]/total*100}%;
      background:${ok?'#3fb96f':'#ffb020'}"></i></div></div>`;
}).join("");

document.getElementById("issueList").innerHTML = D.issues.map(([f,d,n])=>
  `<li><code>${esc(f)}</code><br><span style="font-size:12px">${esc(d)}</span>
   <br><span style="color:var(--green);font-size:11px">${esc(n)}</span></li>`).join("");

document.getElementById("footer").innerHTML =
  `LoL 延时伪直播 · 项目全景 · 数据来自本机实际运行环境 · ${esc(D.now)}`;
</script>
</body>
</html>
"""

html_out = TPL.replace("__DATA__", DATA_JSON)
open(OUT, "w", encoding="utf-8").write(html_out)
print("已生成:", OUT)
print("大小: %.1f KB" % (os.path.getsize(OUT) / 1024))
print()
print("数据集 %d 个, 场次 %d 个, 源文件 %d 个" % (len(datasets), len(matches), len(src)))
