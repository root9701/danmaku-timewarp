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
    "2026-10-26|7734200": {
      "date": "2026-10-26",
      "file": "7734200",
      "bv": "BV19wnQ6sExk",
      "title": "【2026全球总决赛】10月26日 IG vs HLE",
      "pages": {
        "3": {"part": "赛前评论", "duration": 916, "offset_sec": 4353.513,
              "confidence": 15.68, "matched_seconds": 686,
              "windows_ok": 1, "windows_spread": 0.0,
              "candidates": 1, "picked_by": "唯一候选",
              "method": "audiofp", "ts": "2026-10-26T20:30:00"},
        "1": {"part": "第一局", "offset_sec": 5272.508, ...},
        "2": {"part": "赛后采访", "offset_sec": 8302.498, ...}
      },
      "offset_sec": 5272.508,
      "auto": {"confidence": 10.19, "method": "audiofp", "ts": "..."}
    }
  },
  "lists": {"2026-10-26": [ ... ]}
}
```

### 4.1 关键约定

- **每个分P 有独立的 `offset_sec`**。原因：分P 是各自独立的直播切片，
  上传顺序 ≠ 时间顺序（实测 G1 的 P3 赛前评论实际在 P1 之前），
  且各分P 之间的局间休息在直播侧与回放侧长度不同，累加会漂移。
- 顶层 `offset_sec` 只是**兼容字段**（取第一分P），新代码应读 `pages`。
- `offset_sec` 语义：`回放内时间 t 对应的墙钟 = anchor + offset_sec + t`，
  其中 `anchor` = 该会话弹幕首条 ts。

### 4.2 对齐可信度判据（服务端实现）

一道分P 的对齐结果只有在同时满足以下条件时才写入：

1. 单窗互相关置信度 `confidence >= 2.0`
2. `confidence` 有限（`inf` 表示只有单一峰，不可判定）
3. 匹配音频时长 `>= 20s`
4. 多时间窗各自推算的 offset 一致（离散 ≤ 0.5s）
5. 多候选时能通过**弹幕密度**或**时序紧邻**区分出唯一位置

`picked_by` 字段记录最终采用了哪条判据，便于事后排查。

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
