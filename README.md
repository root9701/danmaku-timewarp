# 弹幕时光机 · Danmaku Timewarp

把 B 站赛事直播间的弹幕整夜录下来，第二天对齐到官方回放视频上，在本地网页里把「录播」还原成一场「正在直播」——弹幕跟着画面飞，像回到比赛当晚。

为英雄联盟全球总决赛（Worlds）瑞士轮/淘汰赛设计：比赛在北京时间凌晨 1~8 点，睡醒后打开网页即可「伪直播」回看。

> **不想自己采集？** 仓库里已有采集好的数据，直接看：[`DISTRIBUTION.md`](DISTRIBUTION.md)
> —— `git clone` 后双击启动脚本即可，不需要 B 站账号、不需要挂机。

```
┌─────────── 夜间 ───────────┐      ┌─────────── 次日 ───────────┐
│  danmaku-collector         │      │  danmaku-server            │
│  定时开录 → 录弹幕 → 切片   │ ──►  │  选场次 → 自动校准 → 播放   │
│  同时算音频指纹            │      │  弹幕跟画面飞               │
│  到点自动收尾              │      │  视频流直连B站CDN, 不落地    │
└────────────────────────────┘      └────────────────────────────┘
```

## 原理

直播弹幕的时间 = 真实墙钟；官方回放视频是另起一条时间轴。两者只差一个**固定的 offset**。

夜里录弹幕时，顺便拉最低清晰度的直播流、只解音频算「音频指纹」（33 个对数频带，不落任何音频文件）。白天把官方回放的音频也抽出指纹，两张指纹做互相关 → 直接解出 offset。

用指纹而不是用「赛程时间猜」的原因：直播会提前开播、局间有休息、有时还插广告和重播，猜出来的 offset 误差可达几分钟。指纹对齐的实测偏差在 **0.2 秒内**。

## 目录

```
lol延时直播/
├── README.md / SCHEMA.md           ← 用法配置 / 数据契约
├── DISTRIBUTION.md                 ← 只用作者的数据(不采集)
├── AGENTS.md / 业务约束.md          ← 纪律红线 / 禁区与质检
├── 架构总览.md / 术语表.md / 模块设计/
├── LICENSE                         ← MIT(代码与文档)
├── tools/quality_gate.py           ← 质量闸门(退出码说话)
├── tests/test_align.py             ← 对齐算法回归
├── data/                           ← 采集产物(两端共用, 见 SCHEMA.md)
│   {赛事日}/{房间号}/ manifest + 切片 + afp
├── danmaku-collector/              ← 采集端(可单独部署到手机)
│   collector.py / live_audiofp.py / dashboard.py / phone_ctl.py
│   启动采集.bat / 启动控制台.bat / 退出控制台.bat
└── danmaku-server/                 ← 播放端(Python aiohttp + 原生JS)
    server.py / audiofp.py / matches.json(不入库) / web/
    启动回放台.bat / 启动回放台_局域网.bat / 退出回放台.bat
```

模块职责与依赖方向见 `架构总览.md`。

## 平台与已知限制

- **仅 Windows**（启动脚本是 `.bat`）。采集端可拷到手机 Termux 跑，播放端未做跨平台适配。
- 需要 Python 3.12+。依赖版本已在 `requirements.txt` 精确锁定。
- 视频不落地，播放时直连 B 站 CDN，因此**需要联网**，且受 B 站地区限制。
- 只对「B 站官方账号上传的回放」自动搜索 BV；其他来源需手动填。
- 数据里的弹幕保留**原始用户昵称**，未做匿名化（见 `LICENSE` 末尾说明）。

## 快速开始

**每晚睡前**：双击 `danmaku-collector/启动采集.bat`，窗口可最小化，别关。

**次日早上**：双击 `danmaku-server/启动回放台.bat`（想在 iPad 上看用 `启动回放台_局域网.bat`）
→ 浏览器开 http://127.0.0.1:8789 → 选日期和场次 → 点「开始播放」→ 点「自动校准」。

**官方回放通常中午前后才上传完整**，早上若有场次缺 BV，中午点一次「刷新赛程」即可补齐。

> 只想看别人采好的数据、不想自己挂机？见 **`DISTRIBUTION.md`**——`git clone` 后直接回放，不需要采集环境。

## 配置（换赛事 / 换房间 / 换时区）

两端各有自己的 `config.json`，**都不进版本库**，改完重启生效。删掉也能用内置默认值启动，均有 `config.example.json` 可抄。

### 采集端 `danmaku-collector/config.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `rooms` | `[]` | 要采集的直播间房间号，可多个。**必填**，留空启动即报错 |
| `earliest_start` | `"now"` | `now` / `HH:MM`（下一次出现）/ `+30m` |
| `latest_end` | `"09:00"` | 自动停止时刻，空串表示不停 |
| `poll_interval_sec` | `60` | 轮询间隔，最小 5 |
| `audio_fp` | `true` | 是否采音频指纹。**强烈建议开**——直播流过后就没了，事后无法补 |
| `slice_minutes` | `60` | 弹幕切片间隔，`0` = 整晚一个文件 |

### 播放端 `danmaku-server/config.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | `8789` | 网页端口 |
| `host` | `127.0.0.1` | 监听地址。`0.0.0.0` = 局域网可访问（无鉴权，只在可信网络用） |
| `discover.enabled` | `true` | 是否允许自动发现赛程；设 `false` 后 `/api/discover` 直接报错 |
| `discover.league_id` | Worlds | 抓哪个赛事，见下表 |
| `discover.timezone` | `Asia/Shanghai` | IANA 时区名。**Windows 需先 `pip install tzdata`** |
| `discover.window_start` / `window_end` | `01:00` / `09:00` | 赛程过滤窗口；结束早于开始表示跨零点 |
| `bili.official_mid` | `50329118` | B站官方回放账号 |
| `align.min_confidence` | `2.0` | 对齐置信度下限 |
| `align.vote_segments` | `8` | 多段投票切成几段 |
| `align.vote_tolerance_sec` | `30` | 投票容差（秒） |

常用 `league_id` 与环境变量见下表。

| 赛事 | `league_id` |  | 环境变量 | 作用 |
|---|---|---|---|---|
| Worlds | `98767975604431411` |  | `DANMAKU_CONFIG` | 配置路径 |
| LPL | `98767991299243165` |  | `DANMAKU_DATA` | 数据目录 |
| LCK | `98767991310872058` |  | `DANMAKU_HOST` | 监听地址 |
| LEC | `98767991302996019` |  | `RIOT_API_KEY` / `AFP_CACHE` | 覆盖社区 key / 指纹缓存目录 |

**换赛区示例**——录 LPL（北京时间晚上开赛）：

```json
{"discover": {"league_id": "98767991299243165", "window_start": "16:00", "window_end": "23:00"}}
```
采集端相应改 `earliest_start: "16:00"`、`latest_end: "23:30"`。**换直播间**只改采集端 `rooms`，播放端无需改。

`discover.timezone` 用 IANA 名称。**Windows 上 `zoneinfo` 依赖 `tzdata`**，没装会抛
`ZoneInfoNotFoundError`（代码打警告并退回 UTC+8）——时区错了会让赛程窗口整体偏移几小时，
表现为「明明有比赛却一场都没发现」。`pip install tzdata`。

## 核心机制

### 1. 采集

- **状态机**：等到定时 → 轮询开播 → 录制 → 下播等待重开 → 到点收尾。多场比赛共用一次运行。
- **弹幕协议**：B 站 WS，APP 签名 token + buvid3 预热（规避风控）；断线指数退避重连。
- **整点切片**：`{room}_s{HHMM}.jsonl`，每片完成即 gzip。
- **防覆盖**：同日重复运行自动归档为 `_runN` 后缀，弹幕/指纹/meta 共用序号。
- **收尾**：gzip + 写 `meta.json` + `manifest.json`（契约见 `SCHEMA.md`）。

### 2. 音频指纹自动校准

**原理**：直播弹幕墙钟与回放视频时间只差一个固定 offset。夜里录弹幕时顺便拉最低清晰度直播流，只解音频算指纹（33 频带，0.4s/帧，整晚约 3.5MB，CPU<3%，不落音频）；白天与官方回放音频做互相关，解出 offset。

**逐分P 独立对齐**：官方回放按「第一局/第二局/赛前评论/赛后采访」拆成多个分P，每个都是独立切片、各有 offset。分P 个数由程序从 B 站接口读，不写死。

**判定看投票一致性，不看单段置信度**：把每个分P 切成 8 段（每段约 3 分钟）分别定位，按 30 秒容差聚类投票。单段 conf 只反映「这 3 分钟音频有多独特」，BP/解说段天然低到 1.1，但不代表位置错——采信条件与守卫见 `模块设计/对齐.md`。

**赛程窗口**：直播间早上会重播前几场，音频与回放完全一致，投票也会命中。故按赛程开始时间算允许区间（提前 90 分钟 ~ 整场结束 +30 分钟），窗口外拒绝并提示「疑似重播」。多个自洽位置取**最早**的那个。

### 2b. 播放时的分段映射

播放器把各分P 首尾相接拼成虚拟轴，但官方回放剪掉了局间休息，所以每个分P 用**自己的 offset** 映射，不能用一个标量（实测偏差 200 秒以上）。公式与取舍见 `SCHEMA.md` §4.4。

### 3. 赛事日与赛程发现

**赛事日 = 会话结束时刻所在日期。** 跨零点的场次（23:33 开录、次日 08:59 结束）归到「打完那天」。目录名一旦确定即锁定。

Riot esports API 取当日比赛块 → 生成场次草稿 → B站搜索官方账号的单场回放标题匹配两队名 → 回填 BV。回放未上传时留空，之后一键刷新补齐。写回遵循「只回填空位，绝不覆盖已有 BV/offset」。

### 4. 手动微调（兜底）

自动校准不准时，展开「手动微调」用 ±30/10/5 秒逐档校正，即时生效并持久化。

## 日常运维

两端各有自己的启停脚本，都在各自目录里，互不影响。

| 采集端 `danmaku-collector/` | 作用 |
|---|---|
| `启动采集.bat` | 前台运行，窗口里能看实时进度。**关窗 = 停止采集** |
| `启动控制台.bat` | 网页控制台(8787)，可改配置、看进度、手动启停 |
| `退出控制台.bat` | 停止控制台(连带采集任务)，不用去翻窗口 |

| 播放端 `danmaku-server/` | 作用 |
|---|---|
| `启动回放台.bat` | 前台运行，**仅本机**可访问。**关窗 = 停止服务** |
| `启动回放台_局域网.bat` | 前台运行，手机/iPad 可访问，窗口里显示二维码 |
| `退出回放台.bat` | 停止回放台(按端口 8789 精确找进程，不误伤别的 python) |

启动脚本都是前台运行 —— 关窗即停，不留后台进程。万一窗口找不到了，直接双击对应的 `退出*.bat`。

**两种模式的监听地址互斥**：`启动回放台.bat` 显式设 `DANMAKU_HOST=127.0.0.1`，即使 `config.json` 存了 `0.0.0.0` 也只绑本机、不显示二维码；局域网脚本才绑 `0.0.0.0`。启动窗口会打印「监听地址: xxx (来自环境变量/config.json/默认值)」。

**端口被占用**时给出可执行提示（而不是一大段 traceback）。**局域网访问需防火墙放行**，管理员 PowerShell 执行一次：

```powershell
New-NetFirewallRule -DisplayName "弹幕回放台" -Direction Inbound -Protocol TCP -LocalPort 8789 -Action Allow -Profile Private
```

并检查有无针对 python.exe 的入站拦截规则（有则删除）：

```powershell
Get-NetFirewallRule -Direction Inbound -Action Block | Where-Object { $_.DisplayName -eq "python.exe" }
```

## 开发

```powershell
# 语法底线
python -c "import ast;ast.parse(open('danmaku-server/server.py',encoding='utf-8').read())"
node --check danmaku-server\web\app.js

# 质量闸门(函数60行/文件400行/复杂度15/死代码0), 退出码 0 = 全过
python tools\quality_gate.py

# 对齐算法回归
danmaku-server\.venv\Scripts\python.exe tests\test_align.py
```

改动前读 `AGENTS.md`（纪律与红线）、`业务约束.md`（禁区与质检）、`架构总览.md`（模块与架构债）。改数据字段前必读 `SCHEMA.md`。

## 手机备份节点（可选）

PC 与手机同时采同一房间，数据独立落盘，任一节点故障不丢当晚弹幕。

- 环境：Termux + python/numpy/ffmpeg，代码经 `adb reverse` 隧道拉取。
- 保活：`termux-wake-lock` + Doze 白名单；vivo 的 ABE 后台管理需手工豁免。
- 管理：`phone_ctl.py status|start|stop|log|pull|test`。
- 已知限制：Termux 的 Python 与 numpy ABI 兼容问题可能令指纹在手机上降级（弹幕不受影响）。

## 已知边界

| 情况 | 表现 |
|---|---|
| 直播只播了部分内容 | 该分P 只有部分片段能对齐，其余回放内容播不了 |
| 某分P 在直播开播前（如赛前评论） | 该分P 报「匹配不到」，其它分P 正常 |
| 早上重播了前几场 | 按赛程窗口拒绝并提示「疑似重播」，不会错位采纳 |
| 官方回放重导音轨 | 自动校准拒绝，需手动微调 |
| 采集时关了 `audio_fp` | 该晚永远无法自动对齐（指纹无法事后补） |
| 同一天多次运行 | 自动归档为 `_runN`，数据不丢但会分成多个会话 |
