# 分P级对齐 + 数据结构约定 — 执行计划(已全部完成)

> 本文档是本轮改造的**唯一事实来源**。动手前先看这里，避免边做边改主意。

## 背景（已实测确认的问题）

### 问题 1：后两个分P 从未被正确匹配（真实 bug）

`bili_dash_audio_url(session, bv)` **不接受 page 参数**，内部固定用 `info["cid"]`（恒为第一分P）：

```python
async def bili_dash_audio_url(session, bv: str):        # ← 没有 page
    info = await bili_view(session, bv)
    params={"bvid": bv, "cid": info["cid"]}             # ← 永远第一分P
```

实测 G1（BV19wnQ6sExk）三个分P：

| 分P | 真实时长 | 实际取到的指纹 |
|---|---|---|
| P1 第一局 | 3031 秒 | 15148 帧 = 50.5 分钟 ✅ |
| P2 赛后采访 | 477 秒 | **15148 帧 = 50.5 分钟** ❌ |
| P3 赛前评论 | 916 秒 | **15148 帧 = 50.5 分钟** ❌ |

且缓存键 `{bv}_{page}` 让错误结果被缓存复用。**这就是"正赛匹配上了、后两个没匹配"的直接原因。**

### 问题 2：分P 上传顺序 ≠ 时间顺序

```
P1 第一局    3031 秒
P2 赛后采访   477 秒
P3 赛前评论   916 秒   ← 实际发生在 P1 之前
```

所以**不能按 P 序号推算时间**，必须每个分P 独立对齐后按 offset 排序。

### 问题 3：目录结构日期自相矛盾

| 目录 | 条数 | 实际录制的比赛 |
|---|---|---|
| `2026-10-25/2026-10-24_0755_7734200` | 182 | 25日 07:55（测试） |
| `2026-10-25/2026-10-24_0819_7734200` | 71609 | 25日 08:19–11:20 |
| `2026-10-25/2026-10-25_1327_7734200` | 16 | 25日 13:27（测试） |
| `2026-10-25/2026-10-25_2332_7734200` | 201840 | **26日 00:00–08:59** ← 但叫 25 号 |
| `2026-10-26/2026-10-25_0845_7734200` | 0 | 26日 08:45（事故残留，无弹幕） |

父目录日期与子目录名中的日期打架；正赛明明是 26 号却被标成 25 号。

### 问题 4：全屏控制条永不隐藏

```css
.player-wrap:fullscreen #ctrl-bar { opacity: 1; }   /* 强制常显 */
```

### 问题 5：场次标签硬编码 G1/G2

```python
draft.append({"title": f"G{len(draft) + 1} {c1} vs {c2}", ...})
```

`teams` 字段其实已经有了，标签应该直接用战队名。

---

## 目标结构

### 目录

```
data/
  2026-10-26/                        # 赛事日 = 会话【结束时刻】所在日期
    7734200/                         # 房间号
      manifest.json                  # 权威索引（新增）
      7734200_s2332.jsonl.gz         # 弹幕切片
      7734200_s0000.jsonl.gz
      ...
      7734200.afp.jsonl.gz           # 音频指纹（必须保留，无它无法校准）
  _test/                             # 测试数据，与正赛物理隔离
    2026-10-25T0755_7734200/
    2026-10-25T1327_7734200/
```

**赛事日规则**：取会话**结束时刻**的日期。跨零点大夜场归到"打完那天"，无魔法阈值。

| 会话 | 结束时间 | 赛事日 |
|---|---|---|
| 20 万条正赛 | 26日 08:59 | 2026-10-26 ✅ |
| 白天那场 | 25日 11:20 | 2026-10-25 |
| 两次测试 | 25日 | 进 `_test/` |

### manifest.json（schema_version = 1）

```json
{
  "schema_version": 1,
  "room": 7734200,
  "match_date": "2026-10-26",
  "start_ms": 1792966375000,
  "end_ms": 1793000340000,
  "dm_count": 201840,
  "sc_count": 0,
  "files": ["7734200_s2332.jsonl.gz", "7734200_s0000.jsonl.gz", "..."],
  "afp": {"file": "7734200.afp.jsonl.gz", "hop": 0.2, "win": 0.4, "conns": 1},
  "collector_version": "0.2.0"
}
```

**约定**：
- 由**采集器 finalize 时写出**，不是事后补算
- `start_ms` / `end_ms` 从弹幕数据实际 ts 计算
- 服务端**优先读 manifest**，读不到才回退扫描（兼容旧数据）
- 前端只依赖 API 返回字段，**永不解析路径**
- `schema_version` 供将来结构演进时识别

### matches.json 结构扩展

从「一场一个 offset」改为「一场一记录，内含各分P offset」：

```json
{
  "align": {
    "2026-10-26|7734200": {
      "bv": "BV19wnQ6sExk",
      "title": "HLE vs IG",
      "teams": ["HLE", "IG"],
      "pages": {
        "3": {"part": "赛前评论", "offset_sec": 4740.2, "confidence": 8.1,
              "matched_seconds": 900, "duration": 916},
        "1": {"part": "第一局",   "offset_sec": 5272.5, "confidence": 5.3,
              "matched_seconds": 1800, "duration": 3031},
        "2": {"part": "赛后采访", "offset_sec": 8303.1, "confidence": 4.9,
              "matched_seconds": 470, "duration": 477}
      },
      "updated": "2026-10-26T10:00:00"
    }
  }
}
```

兼容：旧格式（顶层 `offset_sec`）读取时视为只有 P1。

---

## 执行顺序

### P0-1 修音轨取流支持 cid

- `bili_dash_audio_url(session, bv, cid)` 增加 cid 参数
- 缓存键 `{bv}_{page}.fp.npz` → `{bv}_{cid}.fp.npz`
- `get_replay_fingerprint` 内部先 `bili_view` 取 pages，按 page 找到 cid 再取音轨

### P0-2 逐分P 独立对齐

`run_autocalign` 重构：
1. 取 `pages`（全部分P）
2. 对每个分P：取指纹 → 与直播 afp 各连接段互相关 → 得 `offset_sec`
3. 每个分P 独立过三重闸门（conf ≥ 2.0、isfinite、匹配时长）
4. 按 offset 排序后写回

### P0-3 matches.json 按分P 存

见上面结构。同步 `lists` 时按 `(bv, page)` 精确匹配，不再按 bv 全局扫。

### P0-4 采集器生成 manifest + 新目录

- `collector.py`：`_session_dir_path` 改为 `data/{赛事日}/{room}/`；测试会话落 `data/_test/`
- finalize 时算 `start_ms`/`end_ms` 写 `manifest.json`
- afp 落盘改为 gzip
- `migrate_sessions.py` 重写：按新规则搬迁现有 5 个目录

### P0-5 服务端读取层适配

- 新增 `read_manifest(date, room)`；`scan_days` / `danmaku_paths` / `find_afp_paths` 优先用它
- 保留旧布局扫描作为 fallback

### P0-6 迁移现有数据

只保留 20 万条正赛 → `data/2026-10-26/7734200/`；两次测试 → `_test/`；事故残留（无弹幕）删除。

### P1-1 总览：一场一轨道，内部分P 按名称

```
HLE vs IG  [赛前评论 15.3分][第一局 50.5分][赛后采访 8.0分]
            ↑offset=4740    ↑offset=5272    ↑offset=8303
```

每段独立 offset，段间留白即真实局间休息。悬停显示分P 名 + 起止时刻。

### P1-2 场次标签用战队名

`title` 去掉 `G{n}` 前缀，直接用 `teams`：

```python
draft.append({"title": f"{c1} vs {c2}", "teams": [c1, c2], ...})
```

旧数据里的 `G1 HLE vs IG` 在读取时剥掉前缀（正则，不硬编码）。

### P1-3 全屏控制条自动隐藏

- 删除 `.player-wrap:fullscreen #ctrl-bar { opacity: 1; }`
- 改为全屏时 3 秒无操作自动隐藏，鼠标移动/触摸时浮现
- 未全屏保持现有 hover 行为

### P1-4 多窗口投票 + 一致性校验

```python
offsets = [r["offset_sec"] for r in results if r["confidence"] >= 2.0]
if len(offsets) >= 2:
    spread = max(offsets) - min(offsets)
    if spread > 0.5:                    # 正常应近乎 0（实测 G3 三窗口偏差全 0.0）
        reject("多窗口对齐结果不一致")
```

### P1-5 SCHEMA.md

固化文件系统契约 + API 契约 + 版本演进规则。

---

## 验收标准

| 项 | 标准 |
|---|---|
| 分P 对齐 | G1 三个分P 都能匹配上，offset 互不相同且与真实时间顺序一致 |
| 目录 | 只有一个日期层级；正赛在 `2026-10-26/7734200/` |
| manifest | 采集器 --test 跑一次，产出的目录里直接有 manifest.json |
| 总览 | 一场一轨道，内部按分P 名称分段 |
| 标签 | 显示 `HLE vs IG` 而非 `G1 HLE vs IG` |
| 全屏 | 3 秒无操作控制条消失 |
| 回归 | 所有既有测试通过（采集端 69 + afp 10 + 布局 18 + 前端 9/8/8/8/6/10） |

---

## 执行结果(已完成)

| 项 | 结果 |
|---|---|
| P0-1 音轨取流支持 cid | 完成。三个分P 指纹长度 3029.6 / 475.8 / 914.8 秒, 与真实时长吻合 |
| P0-2 逐分P 对齐 | 完成。HLE vs IG 三分P 全部成功(见下) |
| P0-3 matches.json 按分P 存 | 完成。pages 字段 + 兼容字段 offset_sec |
| P0-4 采集器 manifest | 完成。finalize 时写出, 含 schema_version |
| P0-5 服务端读 manifest | 完成。优先 manifest, 退化目录扫描, v1/v2 仍可读 |
| P0-6 数据迁移 | 完成。正赛迁到 2026-10-26/7734200/, 测试入 _test/ |
| P1-1 总览分P 名称 | 完成。一场一轨道, 内部按分P 名分段 |
| P1-2 战队名标签 | 完成。并避开 G2 这个真实队名 |
| P1-3 全屏自动隐藏 | 完成。3 秒无操作隐藏, 鼠标指针一起隐藏 |
| P1-4 多窗口投票 | 完成。聚类 + 弹幕密度 + 时序紧邻三道判据 |
| P1-5 SCHEMA.md | 完成 |

### 逐分P 实际对齐结果

`
HLE vs IG:
  赛前评论  offset= 4353.513  conf=15.68  dur= 916s
  第一局    offset= 5272.508  conf=10.19  dur=3031s
  赛后采访  offset= 8302.498  conf= 4.88  dur= 477s
`

物理校验: 赛前评论 00:45:35 → 第一局 01:00:54(差 15.3 分 = 赛前评论时长)
→ 赛后采访 01:51:25(差 50.5 分 = 第一局时长)。时间顺序与时长完全自洽。

### 测试

| 组 | 结果 |
|---|---|
| 采集端自证 | 68/68 |
| afp 归档 | 10/10 |
| 布局兼容工具 | 18/18 |
| v3 结构契约 | 35/35 |
| 原有播放 | 9/9 |
| 总览 | 8/8 |
| 重构界面 | 8/8 |
| 防剧透 | 8/8 |
| 连播边界 | 6/6 |
| 会话选择器 | 10/10 |
| **合计** | **180 项全通过** |
