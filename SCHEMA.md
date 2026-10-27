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

读取时**按精确 key 取**，不要遍历字典取第一条 bv 命中的记录：同一 BV 可能残留多条
（数据目录被合并改名后，旧目录名的记录还在），字典顺序一旦先撞上残留下拉框就会显示错的分P 数。

`lists` 的键是**赛事日**（不是数据目录日）。

### 4.1b `lists[]` 字段

| 字段 | 含义 |
|---|---|
| `title` | `A vs B` |
| `bv` | 官方回放 BV，未搜到为空 |
| `offset_sec` | 兼容字段，取第一分P |
| `pages` | 逐分P 对齐结果（对齐后回写） |
| `start_ms` | **官方赛程开始时间**（毫秒墙钟），由 `/api/discover` 落盘 |

`start_ms` 是对齐时加时间窗约束的依据（见 4.4），也是"这场比赛应该发生在哪个时段"的
唯一权威来源。旧数据没有该字段时不做窗口约束，行为与旧版一致。

`/api/matchlist` 还会额外返回三个**派生**字段（不落盘在 lists 里，只在对齐记录中）：

| 字段 | 含义 |
|---|---|
| `total_pages` | B 站当时实际有几个分P |
| `page_errors` | `{分P号: 原因}`，哪几页没对齐及原因 |
| `align_failed` | 仅当**完全没有**任何分P 成功时才为 true |

前端用 `pages` 与 `total_pages` 对比显示「已对齐 1/2 个分P」，把缺的那页原因挂到悬停提示。
只显示「已对齐」而不提缺页，会让用户以为程序漏了分P。

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
按 30 秒容差聚类投票。

**判据是投票一致性，不是单段置信度**。单段 `confidence` 反映的是"这 3 分钟音频本身有多独特"
（BP/解说段的音频特征本来就不独特，值会低到 1.1），不是"这个位置对不对"。
实测 T1 vs CFO 第一局：8 段全部指向同一位置（离散 0.0 秒），但只有 1 段 conf=8.28，
其余 7 段是 1.08~2.12。若按单段置信度先过滤，只剩 1 票，正常比赛会被判失败。

一个位置被采信需满足**其一**：

1. 票数 ≥ 1/3（≥3 票）
2. 强票（`confidence >= 6`）≥ 2 张
3. 有 1 张强票（`confidence >= 7`）且总票数 ≥ 2
4. 全票一致（票数 = 总段数）且最强票 ≥ `min_confidence`

第 4 条是实践中最常用的一条：8 段无一例外指向同一处、且离散 0.0 秒，
不可能是随机巧合（对照组：拿别天的回放去对齐，8 段会散落在 8 个毫无关系的位置）。

### 4.4 赛程时间窗约束

`lists[].start_ms` 是该场比赛的**官方赛程开始时间**（由 `/api/discover` 从 Riot 赛程落盘）。

直播间常把前几场在早上重播一遍，音频与官方回放完全一致，投票也会一致命中。
因此对齐时按赛程时间算出一个允许区间：

```
窗口 = [start_ms - 90分钟, start_ms + 该场全部分P总时长 + 30分钟]
```

搜索在 `audiofp.align(valid_range=...)` 内部就限制在这个区间——**不能**先取全局最大峰
再过滤，因为重播那段码率更高、峰可能比首播还高，窗口内的正确峰根本不会出现在结果里。

窗口内找不到位置时该分P 报"疑似重播，未采纳"，而不是错位采纳。

多个自洽位置时**取最早的那个**：同一段内容可能首播一次、之后又回顾一次。

`picked_by` 记录最终采用的理由，便于事后排查。

### 4.5 前端时间映射（分段映射）

播放器把各分P **首尾相接**拼成一条虚拟时间轴，但直播侧两局之间有局间休息、
官方回放剪掉了，所以"虚拟轴位置"与"真实墙钟"是**分段直线**关系：

```
分P k 占虚拟轴 [S_k, S_k + Dur_k)
该段内局部时间 t 的墙钟 = anchor + offset_k + t
虚拟位置 v 的墙钟 = anchor + offset_k*1000 + (v - S_k)*1000
弹幕挂到虚拟轴的 rel = ts - (anchor + offset_k*1000 - S_k*1000)
```

用单个标量 offset（取第一分P）等价于假设"各分P 无缝相连且与直播等长"。
实测偏差：AL vs TLAW 第二局 −213.9 秒、第三局 −207.8 秒；TSW vs LOS 第二局 −224.9 秒。

**落在局间休息里的弹幕直接丢弃**，不压缩到相邻分P——压缩会把休息期间的几百条弹幕
堆到下一局开头，比丢掉更难看出问题。

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

### 5.2b `GET /api/match?date=&file=[&bv=]`

返回该会话的对齐信息。带 `bv` 时**额外返回该场比赛的逐分P 对齐结果**，
前端播放时用它做分段映射（见 4.5）。

```json
{"ok": true, "anchor_ms": 1793033999962, "offset_sec": 47.389,
 "bv": "BV1wNHw6PEdQ", "pages_bv": "BV1wNHw6PEdQ",
 "pages": {"1": {"part": "第一局", "duration": 3142, "offset_sec": 13802.393,
                 "confidence": 15.78},
           "2": {"part": "第二局", "duration": 4137, "offset_sec": 17157.396},
           "3": {"part": "第四轮抽签仪式", "duration": 511, "offset_sec": 21287.389}},
 "title": "T1 vs CFO", "room_title": "...", "match_date": "2026-10-27",
 "meta_missing": false}
```

顶层 `offset_sec` 是**会话级**标量（历史字段）。读多分P 必须用 `pages`——
只有标量时从第二局起弹幕会整体错位（实测 −213.9 秒）。

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

响应：

```json
{"ok": true, "bv": "BV19wnQ6sExk", "page": 3, "total_pages": 3, "aligned_pages": 3,
 "all_ok": true,
 "offset_sec": 4353.513, "confidence": 10.48,
 "pages": {"3": {"part": "赛前评论", "offset_sec": 4353.513, "confidence": 10.48,
                 "votes": 8, "total_segments": 8, "picked_by": "多段投票 8/8 段一致(...)",
                 "suspect": false, "suspect_reason": ""}},
 "errors": {}, "elapsed_sec": 4.8, "message": "..."}
```

顶层 `offset_sec` / `confidence` 取**第一分P**，仅为兼容单分P 场景与旧前端。
读多分P 结果**必须用 `pages`**。

部分分P 失败时 `ok` 仍为 `true`（有分P 成功即算成功），失败原因在 `errors` 里：

```json
{"ok": true, "aligned_pages": 1, "total_pages": 2,
 "errors": {"2": "该分P 只在窗口外找到位置(10-27 07:44, 距赛程窗口 +405 分钟), 判为早上重播/回顾, 未采纳"}}
```

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
