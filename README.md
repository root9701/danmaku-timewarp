# LOL 延时伪直播（Danmaku Replayer）

把 B 站赛事直播间的弹幕整夜录下来，第二天对齐到官方回放视频上，在本地网页里把"录播"还原成一场"正在直播"——弹幕跟着画面飞，像回到比赛当晚。

为英雄联盟全球总决赛（Worlds）瑞士轮/淘汰赛设计：比赛在北京时间凌晨 1~8 点，睡醒后打开网页即可"伪直播"回看。

```
┌─────────────── 夜间 ───────────────┐      ┌─────────────── 次日 ───────────────┐
│  danmaku-collector (PC + 手机备份)  │      │  danmaku-server (localhost:8789)   │
│  00:30 轮询开播 → WS 录弹幕 → 整点切片 │      │  网页点选场次 → 自动校准 offset      │
│  同时拉直播流算音频指纹(每0.2s一帧)   │  ──► │  → 播放器 + 实时弹幕 + 密度校准中心   │
│  09:00 自动收尾 gzip+meta           │      │  (视频流直连B站CDN, 不落地)          │
└────────────────────────────────────┘      └────────────────────────────────────┘
```

## 目录

```
lol延时直播/
├── README.md                 ← 本文档(总览/架构/数据格式/运维手册)
├── SCHEMA.md                 ← 数据契约(manifest/afp/matches.json/API 字段)
├── danmaku-collector/        ← 采集端(Python, 见其 README)
│   ├── collector.py          主程序: 状态机/WS协议/切片/防覆盖
│   ├── live_audiofp.py       音频指纹 sidecar(拉流+增量指纹)
│   ├── dashboard.py          控制台网页(8787)
│   ├── phone_ctl.py          手机节点管理(status/start/stop/pull/test)
│   ├── config.example.json   配置模板(复制成 config.json 后填)
│   ├── config.json           你的本机配置(不入版本库)
│   └── data/                 弹幕与指纹落盘(按赛事日分目录)
└── danmaku-server/           ← 播放端(Python aiohttp + 原生JS, 见其 README)
    ├── server.py             8789: 静态页/弹幕聚合/对齐注册表/自动校准/赛程发现
    ├── audiofp.py            指纹库(读afp/互相关对齐)
    ├── config.json           播放端配置(端口/赛事/时区/窗口), 可删
    ├── matches.json          对齐注册表+场次列表(持久化)
    └── web/                  前端(index.html + app.js + style.css, 无框架)
```

## 配置（换赛事 / 换房间 / 换时区）

两端各有自己的 `config.json`，**都不进版本库**，改完重启对应进程生效。
两份都提供了 `*.example.json` 模板，删掉 config.json 也能用内置默认值启动。

### 采集端 `danmaku-collector/config.json`

```json
{
  "rooms": [7734200],
  "earliest_start": "01:00",
  "latest_end": "08:00",
  "poll_interval_sec": 30,
  "data_dir": "data",
  "log_dir": "logs",
  "audio_fp": true,
  "slice_minutes": 60
}
```

| 字段 | 说明 |
|---|---|
| `rooms` | 要采集的直播间房间号，可多个。**必填**，留空会直接报错并提示 |
| `earliest_start` | 开始录的时间。`"now"` 立即 / `"01:00"` 下一个凌晨 1 点 / `"+30m"` 30 分钟后 |
| `latest_end` | 自动停止时间，`""` 表示不停 |
| `audio_fp` | 是否采音频指纹。**强烈建议开**——直播流过后就没了，不开就没有自动对齐能力 |
| `slice_minutes` | 弹幕切片间隔，`0` = 整晚一个文件 |

### 播放端 `danmaku-server/config.json`

```json
{
  "port": 8789,
  "discover": {
    "enabled": true,
    "league_id": "98767975604431411",
    "timezone": "Asia/Shanghai",
    "window_start": "01:00",
    "window_end": "09:00"
  },
  "bili": { "official_mid": 50329118 },
  "align": { "min_confidence": 2.0 }
}
```

| 字段 | 说明 |
|---|---|
| `port` | 网页端口 |
| `discover.league_id` | 抓哪个赛事。Worlds=`98767975604431411`，LPL=`98767991299243165`，LCK=`98767991310872058`，LEC=`98767991302996019` |
| `discover.timezone` | IANA 时区名。**Windows 需先 `pip install tzdata`**，否则会退回 UTC+8 并打警告 |
| `discover.window_start` / `window_end` | 只在此时间窗口内的比赛才算"当晚场次"。结束早于开始表示跨零点（如 `23:00`→`06:00`） |
| `bili.official_mid` | B站官方回放账号 ID（哔哩哔哩英雄联盟赛事 = 50329118） |

**换赛区示例**：想录 LPL（北京时间晚上 17:00 开赛），改成

```json
{
  "discover": {
    "league_id": "98767991299243165",
    "timezone": "Asia/Shanghai",
    "window_start": "16:00",
    "window_end": "23:00"
  }
}
```

采集端 `earliest_start` 相应改成 `"16:00"`、`latest_end` 改成 `"23:30"`。

**换直播间**：只改采集端 `rooms` 为对应直播间数字即可，播放端无需改。

## 核心机制

### 1. 采集（collector.py）

- **状态机**：等定时(00:30) → 轮询开播 → 录制 → 下播等待重开 → 09:00 收尾。多场比赛共用一次运行。
- **弹幕协议**：B 站 WS（`broadcastlv.chat.bilibili.com`），APP 签名取 token + buvid3 预热过 2026 风控；DANMU_MSG + SC；断线指数退避重连，全程计入 meta。
- **整点切片**：数据文件按整点滚动 `{room}_s{HHMM}.jsonl`，每片完成即 gzip。方便"边采边看"和分段管理。
- **防覆盖**：finalize 遇同日旧产物自动归档为 `_runN` 后缀（弹幕/指纹/meta 三件套共用序号），一天多次运行绝不丢数据。
- **收尾**：gzip 原始文件 + 写 `{room}.meta.json`（起止时间/条数/重连/关键词事件/parts 清单）。

### 2. 音频指纹自动校准（自动对齐 offset，核心创新）

**原理**：直播弹幕的墙钟时间与回放视频时间之间只差一个固定 offset。夜里录弹幕时顺便拉最低清晰度直播流，只解音频算指纹（33 频带能量，0.2s/帧，整晚约 7MB、CPU<3%，不落任何音频）；白天把指纹与官方回放的音频互相关，直接解出 offset。

- 实测精度：**0.000~0.039 秒**（对 AAC 转码、加噪鲁棒；整晚 8 小时长段用滑窗对齐）。
- 全自动：回放音频优先走 dash 纯音轨，指纹带缓存；置信度过低（<2.0）自动拒绝写入，退回手动校准——宁缺毋滥。
- 实战：D3 四场中 3 场自动对齐成功（conf 7.7~10.2），1 场官方重导音轨被正确拒绝。

### 3. 会话归属（日期语义）

比赛跨零点（23:33 开录 → 次日 08:41 结束），约定：**凌晨 00:00~11:00 的内容属"前一晚比赛"**。

- 采集端 `_session_dirname()`：凌晨录制直接写进**昨天日期的目录**（如 26 日凌晨的切片落在 `data/2026-10-25/`），跨零点切片跟随会话日。
- 服务端 `/api/matchlist`：读该日数据首条时间戳判定会话归属，自动路由到正确比赛日的场次列表（旧数据兼容回落）。
- 弹幕文件聚合：`{room}.jsonl.gz` 主文件 + `_runN` 归档 + `_sHHMM` 切片 + 活动 raw（未压缩，边采边读）自动合并按 ts 排序；历史数据若散在次日目录也能归并（次日无主 meta 才视为非独立会话）。

### 4. 赛程与 BV 自动发现（/api/discover）

Riot esports API（社区 key）取当日比赛块 → 生成 G1..Gn 草稿 → B 站搜索官方账号（mid=50329118）单场回放标题匹配两队名 → 回填 BV。回放未上传时留空，之后一键刷新回填。写回遵循"只回填空位，绝不覆盖已有 bv/offset"。

### 5. 手动校准（自动的兜底）

校准中心提供：弹幕密度曲线（10s 粒度）+ 自动识别事件标注（一血/大龙/水晶/结束等，直播侧关键词聚类）+ 白色播放位置线。用法：点事件标记跳转 → 画面上该事件发生瞬间点「对齐到此刻」；或 ±30/10/5 微调。全部即时生效并持久化。

## 数据格式

### 弹幕文件 `{room}_s{HHMM}.jsonl(.gz)`，每行一个 JSON：

```json
{"ts": 1792942382304, "un": "用户名", "t": "弹幕文本", "ty": "dm", "price": 0}
```
`ts` 为毫秒墙钟（对齐的锚点），`ty`: `dm`|`sc`。测试录制文件名前缀 `test_`。

### 音频指纹 `{room}.afp.jsonl(.gz)`，四种行：

```
{"ty":"hdr","v":1,"sr":16000,"hop":0.2,"win":0.4,"bands":33}
{"ty":"conn","c":0,"t0_ms":1792942382000}          // 每个拉流连接段
{"ty":"fp","c":0,"ts":...,"n":300,"fp":"<base64>"} // 60s 一批, uint8量化帧×33带
{"ty":"end","c":0,"reason":"reconnect"}
```

### matches.json（对齐注册表 + 场次列表）：

```json
{
  "align":  { "2026-10-26|7734200": {"bv":"...","offset_sec":5272.5,"auto":{"confidence":10.2,"method":"audiofp"}} },
  "lists":  { "2026-10-26": [ {"title":"G1 HLE vs IG","bv":"...","offset_sec":5272.5} ] }
}
```
align 键 = `会话日|文件名`；lists 键 = 比赛日。写入全部原子替换（tmp+rename）。

## 日常运维

### 每晚（睡前）
1. PC 双击 `danmaku-collector/start.bat`（或控制台 bat 里点"开始今晚挂机"）。
2. 手机节点：`python phone_ctl.py start`（可选备份；详见下节）。

### 次日早上
1. 双击 `danmaku-server/伪直播.bat` → 浏览器自动开 http://127.0.0.1:8789
2. 日期选昨晚（数据目录日期）→ 场次下拉自动出现当晚比赛 → 点「开始播放」。
3. 每场点一次「自动校准」（首次约半分钟）；官方重导音轨的场次自动校准会拒绝，用校准中心手动对齐。
4. 「刷新赛程」可在回放陆续上传后补齐 BV。

### 回放上传时间
官方单场回放通常**中午前后**陆续上传。早上起来若"缺 BV"，中午再点一次「刷新赛程」即可。

## 手机备份节点（可选，vivo X50 Pro+ / Termux）

PC 与手机**同时采同一个房间**，数据独立落盘，任一节点故障不丢当晚弹幕。

- 环境：Termux（F-Droid 版）+ python/numpy/ffmpeg（清华源），代码经 `adb reverse` 隧道拉取。
- 保活：`termux-wake-lock` + Doze 白名单；**vivo 的 ABE 后台管理需手工豁免**（设置 → 电池 → 后台高耗电 → 允许 Termux），未豁免会被冻结（实测 D3 夜该节点因此丢主体时段）。
- 管理：`phone_ctl.py status|start|stop|log|pull|test`；命令注入依赖 adbkeyboard（屏幕会短暂点亮），数据回传走 `adb reverse` HTTP 隧道。
- 已知限制：Termux 的 Python 3.14 与 numpy ABI 兼容性问题可能令音频指纹在手机上降级（弹幕不受影响）。

## 风险与对策备忘（踩坑实录）

| 风险 | 对策 |
|---|---|
| B 站 WS 风控（-352/拒绝认证） | 主页 buvid3 预热 + APP 签名 token（collector 内置） |
| 同日重复运行覆盖数据 | finalize `_runN` 归档 + 切片文件名自带时刻 |
| 官方回放重导音轨 → 音频对齐失败 | 置信度门槛自动拒绝 + 手动校准兜底（密度图+事件标记） |
| 弹幕会话跨零点目录错乱 | 会话归属规则（见"核心机制 3"），服务端读取兼容新旧两种归属 |
| vivo 后台冻结手机采集 | ABE 豁免（一次性 UI 操作）+ Doze 白名单 |
| mp4 直链 403 | 服务端 /api/stream 反代兜底（带 Referer/UA） |
| Riot key 失效/限流 | discover 内置退避重试 + 官网页面赛程兜底解析 |

## 后续路线（M4）

- GitHub 打包发布：采集端 PyInstaller 单 exe + 每日弹幕包 Release；合规命名与免责声明。
- 场次列表全自动：回放上传监控 + 自动 discover 定时任务。
- 手机节点 ABE 豁免脚本化。
