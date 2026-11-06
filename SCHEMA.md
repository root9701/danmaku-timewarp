# 数据契约（SCHEMA）

> 当前版本：`schema_version = 1`
> 本文件管**数据与接口的形状**。配置项见 `README.md` §配置；业务边界见 `业务约束.md`。

## 1. 文件系统布局

根目录 = **仓库根**（采集端代码在 `danmaku-collector/`，数据不在它里面）：

```
data/
  {赛事日}/                        # 例 2026-10-26
    align.json                     # ★ 该日对齐结果（随数据分发，见 §4）
    {room}/                        # 例 7734200（一房间一晚一目录）
      manifest.json                # ★ 唯一权威索引
      {room}_s{HHMM}.jsonl.gz      # 弹幕切片（整点滚动）
      {room}_s{HHMM}.jsonl         # 活动切片（采集中未压缩）
      {room}.afp.jsonl[.gz]        # 音频指纹（不可再生，必须保留）
      {room}.meta.json             # 人类可读摘要
  _test/                           # 测试会话，与正赛物理隔离
    {日期}T{HHMM}_{room}/
```

**`align.json` 放在数据目录里**，不是程序目录：它只有几 KB，却决定「弹幕贴到回放第几秒」。
与弹幕同处一个日期目录，用户一次「拉取弹幕」即可拿到，**不必为新增一天的校准而更新程序**。
轻量版没有 numpy/ffmpeg、不能自行校准，完全依赖这份文件。

**赛事日 = 会话结束时刻所在日期。** 25 日 23:33 开录、26 日 08:59 结束 → 归属 2026-10-26。
不用「凌晨减一天」的阈值规则：跨零点大夜场自然归到打完那天，可解释、无魔法。
目录名不含时间——时间信息由 manifest 的 `start_ms`/`end_ms` 表达。

三代布局共存，读取层全部兼容：v1 平铺 / v2 `{日期}_{HHMM}_{room}` / v3 纯房间号。

## 2. manifest.json

由**采集器写出**，是读取数据的首选来源。收尾时写最终版；采集期间每小时滚完一片后
还会写一份**运行中快照**（`ended_by: "running"`）：`files` 只列已封口的 `.jsonl.gz`，
`afp` / `meta` 为 `null`，收尾时覆盖为最终版。读端应把它当作「可用但仍在增长」，
**不要据此判断这一天已经结束**。

```json
{
  "schema_version": 1, "room": 7734200, "match_date": "2026-10-26",
  "is_test": false, "start_ms": 1792942382304, "end_ms": 1792976369630,
  "dm_count": 201840, "sc_count": 0,
  "files": ["7734200_s2332.jsonl.gz", "7734200_s0000.jsonl.gz"],
  "afp": {"file": "7734200.afp.jsonl.gz", "hop": 0.4, "win": 0.4},
  "meta": "7734200.meta.json", "collector_version": "0.2.0",
  "ended_by": "latest_end"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 契约版本。**结构变更必须 +1** |
| `room` | int | 房间号，用于校验与展示；不要从目录名反推 |
| `match_date` | str | 赛事日 `YYYY-MM-DD` |
| `is_test` | bool | 测试会话标记；正式日期列表会把它分到 `_test` 组 |
| `start_ms` / `end_ms` | int | 真实起止（毫秒墙钟），**从弹幕 ts 算**，不是 record_start |
| `dm_count` | int | 整目录弹幕条数（跨多次重启累计；运行中快照只算已封口片） |
| `sc_count` | int | 醒目留言条数 |
| `files` | [str] | 已可发布的弹幕切片文件名；运行中快照只含已封口 `.jsonl.gz` |
| `afp` | obj\|null | 指纹文件与参数；`null` 表示未采集或仍在写（快照） |
| `meta` | str\|null | 人类可读摘要文件名；**不得作为权威源**；快照为 `null` |
| `collector_version` | str | 写这份 manifest 时的采集端版本（历史快照，不是当前版本） |
| `ended_by` | str | 结束原因；`"running"` = 采集中的快照（见上） |

示例里的 `collector_version` 是 2026-10-26 当时的值，不代表当前版本；版本号以根目录 `VERSION` 为准。

**读不到 manifest** 时服务端必须退化为目录扫描：按 `{room}` 前缀匹配，剥掉 `_sHHMM`/`_runN` 归并。

**`meta.json` 仅供人类阅读，不得作为权威源。** 采集端多次重启时它只覆盖最后一次收尾的片段
（实测记的是 281 条，而目录里有 20 万条、9.4 小时）——时间与条数一律以 manifest 为准。

## 3. afp（音频指纹）

**不可重新生成**：直播流已消失，只有回放侧能重下。丢了那晚就永远不能自动对齐。

UTF-8 JSONL，四种行：

```
{"ty":"hdr","v":1,"sr":16000,"hop":0.4,"win":0.4,"bands":33}
{"ty":"conn","c":0,"t0_ms":1792942375056}
{"ty":"fp","c":0,"ts":1792942375200,"n":300,"fp":"<base64>"}
{"ty":"end","c":0,"reason":"reconnect"}
```

- 16000 Hz 单声道，窗 0.4s，步长 0.4s（2.5 帧/秒）
- **hop 以 `hdr` 行为准，不是代码默认值**：`2026-10-26` 及更早的指纹是 `hop=0.2`，
  之后是 `0.4`。帧的物理时长 = `hop`，读错会让整条时间轴拉伸/压缩（且不报错）。
  参数不同的 afp 文件**不得合并**（`server.read_afp_merged` 会丢弃异参归档）。
- `win` 固定 0.4 且**不随 hop 放大**：窗中心由 hop 决定，放大 win 会让两端算出的
  指纹不再等价。hop=win 时相邻帧首尾相接、不漏任何音频，这是降频的上限。
- 33 个对数频带（250–5000 Hz），每帧量化为 uint8（33 字节）
- 每批 `BATCH_FRAMES`（300）帧写一行 base64，**物理时长 = 帧数 × hop**（hop=0.4 时
  为 120 秒）。不要把这个秒数写死进文档：hop 变了它跟着变。
- 第 i 帧墙钟 = `ts + i*hop*1000` 毫秒；反量化 `v = q/127.5 - 1`
- `ts` 是**冗余字段**：服务端 `audiofp.read_afp` 只用 `conn.t0_ms` + 帧序号重建
  时间轴，不读 `fp.ts`。但它是对外契约（第三方按本文档实现会读），写错会让外部读者
  算出错时间轴且不报错，所以 hop 变化时步长必须同步。

`fp` 行挂在同 `c` 的**最近一个** `conn` 段上。读到半行（采集中被强杀）只丢该行。

## 4. 对齐记录（align.json / matches.json）

由服务端写入，**不是采集端产物**。分两层：

| 文件 | 角色 | 是否入库 |
|---|---|---|
| `data/{赛事日}/align.json` | **分发层**：作者算好的对齐结果，随数据下发 | ✅ 入库 |
| `danmaku-server/matches.json` | **本地层**：用户自己的微调，覆盖同名 key | ❌ 不入库 |

读取时以分发层为底，本地层覆盖；写回只写本地层，绝不改分发层（否则被下次拉取覆盖）。
`tools/split_matches.py` 把本地层里可归日期的记录搬进 `data/{日期}/align.json`。

```json
{
  "align": {
    "2026-10-27|7734200": {"date":"...","file":"...","bvs":["BV1uUn..."],"offset_sec":47.4},
    "2026-10-27|7734200|BV1uUnf6TEch": {
      "bv": "BV1uUnf6TEch", "title": "【2026全球总决赛】10月27日 CFO vs T1",
      "pages": {
        "1": {"part":"第一局","duration":2905,"offset_sec":47.389,
              "confidence":8.28,"votes":2,"total_segments":8,
              "candidates":1,"picked_by":"多段投票 2/8 段一致",
              "method":"audiofp","ts":"2026-10-27T09:30:00"}
      },
      "total_pages": 2, "offset_sec": 47.389,
      "auto": {"confidence":8.28,"method":"audiofp","ts":"..."}
    }
  },
  "lists": {"2026-10-27":[
    {"title":"T1 vs CFO","bv":"...","offset_sec":47.4,"start_ms":1793034000000}
  ]}
}
```

### 4.1 键的约定

| 键 | 含义 |
|---|---|
| `{赛事日}\|{file}` | 会话级汇总，记录该会话对齐过哪些 `bv` |
| `{赛事日}\|{file}\|{bv}` | 每场比赛的详细分P 记录 |

**必须带 BV 维度**：一晚 4 场共用一个 `{赛事日}|{file}` 会互相覆盖（实测对齐完第 4 场，前 3 场记录全没了）。

### 4.1.1 换源（同一场比赛的其他解说版）

用户可以把某场次的弹幕挂到**另一条同场比赛的视频**上（不同解说，画面同源、音轨不同）。
做法与官方源**完全相同**：给新源 BV 写一份带 BV 的详细记录，`pages` 每局一个 `offset_sec`。
播放新源时走的就是原有分段映射，前端不需要任何新状态。

```json
"2026-11-01|7734200|BV1Xxx解说版": {
  "bv": "BV1Xxx解说版", "offset_sec": 17380.563, "source_swap": true,
  "official_bv": "BV1Xxx官方源", "title": "…",
  "pages": {
    "1": {"part":"第一局","duration":2953,"offset_sec":17380.563,
          "source_swap":true,"from_page":1,"align_sec":100,"anchor_sec":60},
    "2": {"part":"第二局","duration":2424,"offset_sec":21445.492,
          "source_swap":true,"from_page":2,"align_sec":140,"anchor_sec":0}
  }
}
```

| 字段 | 含义 |
|---|---|
| `source_swap` | `true` 表示这条记录来自换源人工标定，**不是**音频自动对齐的结果。分P 级是给前端读的判据（`setup` 用它决定「别把这份 offset 当成官方基准」），记录级标的是整条记录的来历 |
| `from_page` | 对应官方源的第几个分P（两源分P 数量/顺序不一定一致，`0` 表示这一局不挂弹幕） |
| `align_sec` | 用户标的 **N**：官方那一局内的参照秒数（播官方源、拖到位置点「对齐」得到） |
| `anchor_sec` | 用户标的 **A**：用户在换源视频里**挑的那一帧**是第几秒，默认 `0`（= 第一帧）。任意一帧都行 —— A 只是两源对号的参照物，不必是这一局的开头。老记录没这个字段，读出来按 0 处理 |
| `official_bv` | 这份对齐**是按哪条官方源算的**。同一个弹幕文件一晚可以有好几场比赛，用户可能拿同一条解说视频给两场都换源；没有它，第二场会覆盖第一场的参数，读回时也无法判断记录属于哪一场，于是拿 A 场的 N 去配 B 场的官方 offset。老记录没这个字段，读作"不分场次"，按原样回填 |

两者合起来定义映射：**官方该局第 N 秒 = 换源视频第 A 秒**（同一帧在两源里的两个时间点）。

**记录归属**：`/api/sources` 一并返回 `saved_official_bv`（记录里的 `official_bv`）与
`saved_pages`（有没有换源记录）。前端只在 `saved_official_bv` 等于本场官方 BV 时才回填；
不等就保留空白并提示"上次是对另一场对齐的"，绝不把别场的 N 套到本场。`official_bv` 为空
（老记录）按"属于本场"处理，行为与加这个字段之前一致。

**换源值怎么来的**：两源剪辑不同（官方 P1 3771s vs 解说 P1 2953s），实测音频交叉对齐
置信度约 1.0（随机水平）、画面滑窗反推的 delta 极差 2340 秒，**没有任何可靠的自动对齐手段**。
所以由用户先在换源视频里挑一帧、记下它的秒数 A 填进「锚点」，
再回官方源拖到同一画面点一次「对齐」取 N，
最后按 `换源视频offset = 官方那局offset + N − A` 换算成 `offset_sec` 落盘（A 默认 0）。
每局单独一对 N/A —— 每局两边剪掉的长度不一样，整场共用一个参数对不上。
切到换源视频时播放点落在**第 A 秒**，与减掉的 A 是同一个值，否则画面和弹幕还是错开。

**写入约束**：换源记录**只写**带 BV 的详细 key，**完全不碰**会话级 `{赛事日}|{file}`。
会话级是一晚所有场次的汇总，覆盖它会把整晚汇总变成单个 bv，其它场次读取跟着错位。
连会话级的 `bvs` 也**不登记**新源 BV：换源记录是挂在官方源那场比赛上的**派生**数据，
不是"这一晚又对齐了一场"；登记进去会让 `/api/sources` 的官方源兜底（`bvs[0]`）可能取到
新源，把派生记录当成基准去算 offset。读取走精确 key `{date}|{file}|{bv}`，不需要 `bvs`。

读取时**按精确 key 取**，不要遍历取第一条 bv 命中的记录——同一 BV 可能有残留多条（目录合并改名后），
字典顺序先撞上残留就会显示错的分P 数。

`lists` 的键是**赛事日**（不是数据目录日）。`lists[]` 字段：`title` / `bv` / `offset_sec`（兼容，取第一分P）/
`pages`（对齐后回写）/ `start_ms`（**官方赛程开始时间**，由采集端 `nightly.discover` 落盘，是时间窗约束的依据）。

`/api/matchlist` 另返回三个派生字段（不落盘）：`total_pages`（B站实际分P 数）、
`page_errors`（`{分P号: 原因}`）、`align_failed`（**仅完全没有分P 成功**时为 true）。
前端用 `pages` 与 `total_pages` 对比显示「已对齐 1/2」，缺页原因挂悬停——只说「已对齐」会让人以为程序漏了。

### 4.2 offset 语义

```
回放内时间 t 对应的墙钟 = anchor + offset_sec + t
```

`anchor` = 该会话弹幕首条 `ts`。

**每个分P 有独立 `offset_sec`**。分P 是各自独立的直播切片，上传顺序 ≠ 时间顺序，且局间休息在
直播侧与回放侧长度不同，按序号累加会漂移。顶层 `offset_sec` 只是兼容字段（取第一分P），新代码读 `pages`。

### 4.3 对齐判据与时间窗

判据与时间窗的完整规则见 `模块设计/对齐.md`。契约层面只需知道：

- 每个分P 的 `pages[k]` 可以存在，也可以缺失（`page_errors` 记录原因）
- 采信的位置带 `votes` / `total_segments` / `picked_by`，供事后判断可信度
- `suspect=true` 表示只在赛程窗口外找到，该页**不得**用于映射
- 多候选时取**最早**的自洽位置

### 4.4 前端时间映射（分段映射）

播放器把各分P 首尾相接拼成虚拟轴，但官方回放剪掉了局间休息，所以虚拟位置与真实墙钟是**分段直线**：

```
分P k 占虚拟轴 [S_k, S_k + Dur_k)
虚拟位置 v 的墙钟 = anchor + offset_k*1000 + (v - S_k)*1000
弹幕挂到虚拟轴的 rel = ts - (anchor + offset_k*1000 - S_k*1000)
```

用单标量等价于假设「各分P 无缝相连且与直播等长」，实测偏差达 200 秒以上。
**落在局间休息里的弹幕直接丢弃**，不压缩到相邻分P——压缩会把几百条堆到下一局开头，比丢掉更难看出问题。

## 5. HTTP API

统一响应：成功 `{"ok": true, ...}`；失败 `{"ok": false, "error": "中文原因"}`（不抛 500）。

| 接口 | 用途 |
|---|---|
| `GET /api/days` | 列出全部赛事日与会话。`files[].name` 即 `file` 入参 |
| `GET /api/danmaku?date=&file=` | 合并并按 ts 排序的 JSONL 文本，支持边采边读 |
| `GET /api/match?date=&file=[&bv=]` | 会话对齐信息；带 `bv` 时额外返回该场 `pages` |
| `POST /api/match` | 保存手动 offset（`manual=true` 会清掉旧的 auto 记录） |
| `GET /api/matchlist?date=` | 场次列表 + 各自对齐状态 |
| `GET /api/overview?date=` | 一晚总览：密度直方图 + 各场分P 轨道 |
| `GET /api/playurl?bv=&page=&qn=` | mp4 直链（多段时由前端拼接）；`qn` 是画质码，缺省 720P |
| `GET /api/markers?bv=` | B站 看点（一血/大龙/推塔…）换算到整条虚拟时间轴 |
| `GET /api/stream?url=` | 反代 B站 CDN（白名单域名 + 解析后 IP 校验） |
| `GET /api/version` | 当前程序版本（只读本地 `VERSION` 文件，**不联网**） |
| `GET /api/update/check` | 拉远端 `data-index.json`，比对本地，返回待下载清单（不落盘）。含 `want_afp`、`afp_fix_days`（补指纹）与 `align_fix_days`（补作者重算的校准） |
| `GET /api/update/progress` | 当前下载进度（只读，不触发任何动作） |
| `POST /api/update/pull` | 按清单增量下载数据文件，原子落盘到 `data/` |

**`/api/update/*`** 是唯一会**访问互联网并写入 `data/`** 的接口（其余接口只读本地）。
它读系统代理、失败回退直连，所以「代理按需开关」和「没有代理」两种状态都能用；
下载走多通道镜像降级链，每个文件校验 sha256（见 `DISTRIBUTION.md`）。
远端清单里没有的日期不会被删（不存在"同步删除"语义）。
`pull` 进程内互斥：同时发两个请求，第二个返回
`{"ok":false,"error":"已经有一个更新在进行中，请等它结束。"}`。
`pull` 成功后需要重新拉 `/api/days` 才能看到新会话。
`progress` 返回 `{running, done, total, current, bytes, error}`：`done`/`total` 以**天**为单位，
`current` 是当前文件名（含日期前缀）。它只用于显示，前端轮询失败不应影响下载本身。

**`/api/playurl`** 的 `qn` 只影响 `durl`(整段 mp4) 这条路径，**可取值上限是 720P**：
1080P 及以上只存在于 DASH，需自行拼接音视频裸轨，`<video>` 无法直出，故不提供。
响应里 `quality` 是**实际下发**的画质（B站 可能降档），`qualities` 是可选档位
`[{qn,label}]`，前端画质菜单只能列这两个字段的内容，不得自造档位。

**`/api/markers`** 返回 `{bv, count, markers:[{at,page,part,label,team,side,kind}]}`。
`at` 是**整条虚拟时间轴**上的秒数（= `page_marks[].start_sec + 该分P 内秒数`），
`kind` 只有 `kill`/`obj` 两种，仅用于上色。**没有看点时返回空数组而不是错误** ——
大量非官方/非赛事视频本来就没有看点，调用方不能据此报错。

**`/api/days`** 返回 `days[].{date, match_date, data_date, rooms[].files[]}`；
`file` 字段含 `name`/`is_test`/`dm_count`/`layout`/`schema_version`/`start_ms`/`end_ms`。

**`/api/overview`** 的 `tracks[].parts` 已按 `offset_sec` 升序（= 真实时间顺序）。
前端应优先用它绘制，而不是按分P 序号累加 `page_marks`。

**对齐结果**由采集端写入 `lists[].pages`（逐分P）：`{分P号:{part, offset_sec,
confidence, votes, total_segments, picked_by, suspect}}`。顶层 `offset_sec`/`confidence`
取**第一分P**，仅供兼容；读多分P 结果**必须用 `pages`**。校准过程内部的 `errors`
（哪个分P 没对上及原因）不落盘，只写日志。

## 6. 版本演进规则

1. **只增字段不删字段**：新增字段必须有默认值语义，老客户端忽略即可
2. **语义变更必须 +`schema_version`**：例如改变 `offset_sec` 的参考零点
3. **目录布局变更**：服务端先支持新旧两代，再迁数据
4. **前端永不解析路径**：只依赖 API 返回的字段
