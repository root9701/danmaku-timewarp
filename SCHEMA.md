# 数据契约（SCHEMA）

> **这是采集端 ↔ 服务端 ↔ 前端 三者之间的唯一契约。**
> 改动数据结构前先改这里；任何一方只能依赖本文写明的字段，不得依赖目录名/文件名推断语义。

当前版本：`schema_version = 1`

---

## 1. 文件系统布局

```
data/
  {赛事日}/                        # 例 2026-10-26
    {room}/                        # 例 7734200（一个房间一晚一个目录）
      manifest.json                # ★ 权威索引（由采集器 finalize 时写出）
      {room}_s{HHMM}.jsonl.gz      # 弹幕切片（按整点滚动）
      {room}_s{HHMM}.jsonl         # 活动切片（未压缩，采集进行中）
      {room}.afp.jsonl.gz          # 音频指纹（必须保留，否则无法自动校准）
      {room}.meta.json             # 人类可读的统计摘要
  _test/                           # 测试会话，与正赛物理隔离
    {日期}T{HHMM}_{room}/
      manifest.json
      ...
```

### 1.1 赛事日（`{赛事日}`）

= **会话结束时刻所在日期**。

- 25 日 23:33 开录、26 日 08:59 结束 → 赛事日 **2026-10-26**
- 25 日 08:19 开录、25 日 11:20 结束 → 赛事日 **2026-10-25**

不用"凌晨减一天"之类的阈值规则：跨零点的大夜场自然归到"打完那天"，可解释、无魔法。

### 1.2 为什么目录名里不含时间

时间信息由 `manifest.json` 的 `start_ms`/`end_ms` 表达。目录名越简单，
跨零点、重启合并、同房间多会话时越不容易出错。

### 1.3 测试数据隔离

测试会话一律放 `data/_test/`，不进 `data/{赛事日}/`，也不出现在 `/api/days` 的正式列表里。

---

## 2. manifest.json

由**采集器在 finalize 时写出**，是服务端读取数据的首选来源。

```json
{
  "schema_version": 1,
  "room": 7734200,
  "match_date": "2026-10-26",
  "is_test": false,
  "start_ms": 1792942382304,
  "end_ms": 1792976369630,
  "dm_count": 201840,
  "sc_count": 0,
  "files": ["7734200_s2332.jsonl.gz", "7734200_s0000.jsonl.gz"],
  "afp": {"file": "7734200.afp.jsonl.gz", "hop": 0.2, "win": 0.4},
  "meta": "7734200.meta.json",
  "collector_version": "0.2.0",
  "ended_by": "latest_end"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 契约版本。**结构变更必须 +1**，读取方据此判断能否解析 |
| `room` | int | 房间号 |
| `match_date` | str | 赛事日（YYYY-MM-DD），见 1.1 |
| `is_test` | bool | 是否测试会话 |
| `start_ms` / `end_ms` | int | 会话真实起止（毫秒墙钟），**从弹幕数据 ts 算**，不是 record_start |
| `dm_count` | int | 弹幕条数 |
| `files` | [str] | 本会话的弹幕数据片文件名 |
| `afp` | obj\|null | 音频指纹文件与参数；`null` 表示未采集 |
| `meta` | str\|null | 统计摘要文件名 |
| `collector_version` | str | 采集器版本（`migrated` 表示迁移脚本补写） |
| `ended_by` | str | 结束原因 |

### 2.1 读不到 manifest 怎么办

服务端**必须**退化为目录扫描（兼容历史数据）。规则：
按文件名前缀匹配 `{room}` / `test_HHMMSS_{room}`，剥掉 `_sHHMM`/`_runN` 后缀归并。

### 2.2 `meta.json` 的地位

**仅供人类阅读与旧工具兼容，不得作为权威数据源。**

已知陷阱：采集端多次重启时，`meta.json` 只覆盖**最后一次**收尾的片段。
实测主会话的 meta 记的是 08:45–09:00 的 281 条，而目录里有 20 万条、9.4 小时的数据。
所以**时间与条数一律以 manifest / 实际数据为准**。

---

## 3. afp（音频指纹）

**不可重新生成**：直播流已消失，只有回放侧可以重下。丢了就永远无法自动校准。

格式（UTF-8 JSONL，每行一个对象）：

```
{"ty":"hdr","v":1,"sr":16000,"hop":0.2,"win":0.4,"bands":33}
{"ty":"conn","c":0,"t0_ms":1792942375056}
{"ty":"fp","c":0,"ts":1792942375200,"n":300,"fp":"<base64>"}
{"ty":"end","c":0,"reason":"reconnect"}
```

- 采样 16000 Hz 单声道，窗 0.4s，步长 0.2s（= 5 帧/秒）
- 33 个对数频带（250–5000 Hz），每帧量化为 uint8（33 字节）
- 每 60 秒（300 帧）写一行 base64
- 体积 ≈ 0.59 MB/小时

第 i 帧的墙钟 = `ts + i * 200` 毫秒。反量化：`v = q / 127.5 - 1`。

---

## 4. 对齐记录（matches.json）

由服务端写入，**不是采集端产物**。

```json
{
  "align": {
    "2026-10-27|7734200":            // 会话级汇总(记录该会话对齐过哪些 BV)
      {"date":"2026-10-27","file":"7734200","bvs":["BV1uUn..."],"offset_sec":47.4},

    "2026-10-27|7734200|BV1uUnf6TEch":   // 每场比赛一条
      {
        "date": "2026-10-27",
        "file": "7734200",
        "bv": "BV1uUnf6TEch",
        "title": "【2026全球总决赛】10月27日 CFO vs T1",
        "pages": {
          "1": {"part":"第一局","duration":2905,"offset_sec":47.389,
                "confidence":8.28,"votes":2,"total_segments":8,
                "candidates":1,"picked_by":"多段投票 2/8 段一致",
                "method":"audiofp","ts":"2026-10-27T09:30:00"},
          "2": {"part":"赛前评论","duration":916,"offset_sec":24297.945, ...}
        },
        "offset_sec": 47.389,              // 兼容字段, 取第一分P
        "auto": {"confidence":8.28,"method":"audiofp","ts":"..."}
      }
  },
  "lists": {"2026-10-27":[{"title":"T1 vs CFO","bv":"...","offset_sec":47.4}]}
}
```

### 4.1 键的约定

| 键 | 含义 |
|---|---|
| `{赛事日}\|{file}` | 会话级汇总，记录该会话对齐过哪些 `bv` |
| `{赛事日}\|{file}\|{bv}` | 每场比赛的详细分P 记录 |

**必须带 BV 维度**。一个会话里有多场比赛（一晚 4 场），只按 `{赛事日}|{file}` 存会互相覆盖——
实测对齐完第 4 场后前 3 场的记录全没了。

`lists` 的键是**赛事日**（不是数据目录日）。

### 4.2 offset 语义

```
回放内时间 t 对应的墙钟 = anchor + offset_sec + t
```

`anchor` = 该会话弹幕首条 `ts`。

**每个分P 有独立的 `offset_sec`**。分P 是各自独立的直播切片，上传顺序 ≠ 时间顺序，
且各分P 之间的局间休息在直播侧与回放侧长度不同，按序号累加会漂移。

顶层 `offset_sec` 只是兼容字段（取第一分P），新代码应读 `pages`。

### 4.3 对齐可信度判据

逐分P 独立判定。把回放切成 8 段（每段约 3 分钟），分别去整晚直播指纹里定位，
按 30 秒容差聚类投票。一个位置被采信需满足**其一**：

1. 票数 ≥ 1/3（≥3 票）
2. 强票（`confidence >= 6`）≥ 2 张
3. 有 1 张强票（`confidence >= 7`）且总票数 ≥ 2

第 3 条兼容"直播只播了这一局开头"的情况——实测 T1 vs CFO 第一局 8 段全部指向同一位置
（离散 0.0 秒），但只有 1 段 conf=8.28，其余是 1.0x 的噪声水平（那段音频直播里没有）。

多个自洽位置时**取最早的那个**：同一段内容可能首播一次、之后又回顾一次，要的是当时正在直播的那次。

`picked_by` 记录最终采用的理由，便于事后排查。

---

## 5. HTTP API

### 5.1 `GET /api/days`

```json
{"ok": true, "days": [
  {"date": "2026-10-26", "match_date": "2026-10-26", "data_date": "2026-10-26",
   "rooms": [{"room": 7734200, "files": [
     {"name": "7734200", "is_test": false, "dm_count": 201840,
      "layout": "session", "schema_version": 1,
      "start_ms": 1792942382304, "end_ms": 1792976369630}
   ]}]}
]}
```

`name` 直接作为 `/api/danmaku?file=` 与 `/api/match?file=` 的入参。

### 5.2 `GET /api/danmaku?date=&file=`

返回合并并按 ts 排序后的 JSONL 文本。支持边采边读（活动 raw）。

### 5.3 `GET /api/overview?date=`

```json
{"ok": true, "date": "2026-10-26", "match_date": "2026-10-26",
 "file": "7734200", "anchor_ts": 1792942382304, "span_sec": 33987.3,
 "density": {"bins": [...], "bin_sec": 30, "peak": 1148, "t0": ...},
 "tracks": [
   {"idx": 0, "title": "HLE vs IG", "bv": "BV19wnQ6sExk", "offset_sec": 5272.508,
    "parts": [
      {"page": 3, "part": "赛前评论", "offset_sec": 4353.513, "duration": 916, "confidence": 15.68},
      {"page": 1, "part": "第一局", "offset_sec": 5272.508, "duration": 3031, "confidence": 10.19},
      {"page": 2, "part": "赛后采访", "offset_sec": 8302.498, "duration": 477, "confidence": 4.88}
    ]}
 ], "dm_count": 201840, "afp_duration_sec": 33120}
```

`tracks[].parts` 已按 `offset_sec` 升序（= 真实时间顺序）。前端应优先用它绘制，
而不是按分P 序号累加 `page_marks`。

### 5.4 `POST /api/autocalign`

```json
{"date": "2026-10-26", "file": "7734200", "bv": "BV19wnQ6sExk", "page": 0}
```

`page=0`（默认）= 所有分P 各对齐一遍；`page>=1` 只对齐该分P。

---

## 6. 版本演进规则

1. **只增字段不删字段**：新增字段必须有默认值语义，老客户端忽略即可
2. **语义变更必须 +`schema_version`**：例如改变 `offset_sec` 的参考零点
3. **目录布局变更**：服务端必须先支持新旧两代，再迁移数据
4. 前端**永不解析路径**：只依赖 API 返回的字段

---

## 7. 运行配置

两端的配置都**不进版本库**，都属于"你的本机设置"。
删除 config.json 后仍可启动（用内置默认值），这是刻意的：复制走代码就能跑。

### 7.1 采集端 `danmaku-collector/config.json`

模板见 `config.example.json`。

| 字段 | 默认 | 说明 |
|---|---|---|
| `rooms` | `[]` | 要采集的房间号，可多个。**必填**；为空时启动即报错并给出示例 |
| `earliest_start` | `"now"` | `now` / `HH:MM`（下一次出现）/ `+30m` |
| `latest_end` | `"09:00"` | 自动停止时刻，空串表示不停 |
| `poll_interval_sec` | `60` | 轮询间隔，最小 5 |
| `data_dir` / `log_dir` | `data` / `logs` | 相对脚本目录 |
| `audio_fp` | `true` | 是否采音频指纹。关掉就没有自动校准能力，且事后无法补 |
| `slice_minutes` | `60` | 弹幕切片间隔，`0` = 不切片 |

### 7.2 播放端 `danmaku-server/config.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` | `8789` | 网页端口 |
| `discover.enabled` | `true` | 是否自动发现赛程 |
| `discover.league_id` | Worlds | Riot 赛事 ID，见下表 |
| `discover.timezone` | `Asia/Shanghai` | IANA 时区名 |
| `discover.window_start` / `window_end` | `01:00` / `09:00` | 赛程过滤窗口；结束早于开始表示跨零点 |
| `bili.official_mid` | `50329118` | B站官方回放账号 |
| `align.min_confidence` | `2.0` | 对齐置信度下限 |
| `align.vote_segments` | `8` | 多段投票切成几段 |
| `align.vote_tolerance_sec` | `30` | 投票容差（秒） |

常用 `league_id`：

| 赛事 | ID |
|---|---|
| Worlds | `98767975604431411` |
| LPL | `98767991299243165` |
| LCK | `98767991310872058` |
| LEC | `98767991302996019` |

环境变量（优先级高于 config.json）：

| 变量 | 作用 |
|---|---|
| `DANMAKU_CONFIG` | 指定配置文件路径 |
| `DANMAKU_DATA` | 覆盖数据目录 |
| `RIOT_API_KEY` | 覆盖内置社区 key（失效时用） |
| `AFP_CACHE` | 回放音频指纹缓存目录 |

### 7.3 时区的坑

`discover.timezone` 用 IANA 名称。**Windows 上 `zoneinfo` 依赖 `tzdata` 包**，
没装会抛 `ZoneInfoNotFoundError`——代码会打警告并退回 UTC+8，而不是静默忽略，
因为时区错了会让赛程窗口整体偏移几小时，表现为"明明有比赛却一场都没发现"。

```bash
pip install tzdata
```
