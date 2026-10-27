# align/ — 对齐结果（随仓库分发）

> 这里是**作者算好的对齐结果**，一天一个文件，入库、只读。
> 用户本地微调写 `../matches.json`（不入库），优先级高于这里。

## 为什么拆出来

一个文件同时装「作者算好的」和「用户手动改的」，既发不出去也合不回来：

| 做法 | 后果 |
|---|---|
| 全塞 `matches.json`（已 gitignore） | 用户 clone 下来没有 offset，**弹幕挂不到视频上，播不了** |
| 全塞一个入库的 `matches.json` | 用户的微调与上游更新天天空冲突 |
| **拆成本目录 + 本地覆盖** | 上游按天更新，用户只覆盖自己改的那几场 |

## 文件结构

```json
// align/2026-10-26.json
{
  "align": {
    "2026-10-26|7734200|BV19wnQ6sExk": {
      "bv": "BV19wnQ6sExk", "date": "2026-10-26", "file": "7734200",
      "title": "HLE vs IG",
      "total_pages": 3,
      "pages": {
        "1": {"part": "第一局", "offset_sec": 5272.508, "duration": 3031,
              "confidence": 7.16, "votes": 8, "total_segments": 8,
              "picked_by": "多段投票 8/8 段一致", "method": "audiofp"}
      }
    }
  },
  "lists": {
    "2026-10-26": [
      {"title": "HLE vs IG", "bv": "BV19wnQ6sExk",
       "offset_sec": 5272.508, "start_ms": 1792942382304}
    ]
  }
}
```

字段语义见根目录 `SCHEMA.md` §4。

## 合并规则

```
align/{日期}.json   ← 上游，按**单场比赛**为粒度覆盖
matches.json        ← 本地，优先级更高；按**天**整体覆盖 lists
```

- 实际读取顺序：先读全部 `align/*.json`，再用 `matches.json` 覆盖同名 key
- 写回**只写** `matches.json`，且只写「与上游不同」的记录（见 `server.py::_split_local_layer`）
- 因此 `align/` 永远保持上游状态，`git pull` 不会冲突

## 每天怎么更新

```powershell
# 1. 采集 + 对齐完之后, 把当天结果从本地层挪进 align/
python tools\split_matches.py --apply

# 2. 校验（缺 manifest / 切片 / 赛程时间窗都会拦住）
python tools\pack_data.py

# 3. 提交
git add danmaku-server/align && git commit -m "data(align): 2026-10-26 对齐结果"
```

## 注意

- **不要手工编辑**这里的文件，重新跑对齐会让它被覆盖
- 一个日期一个文件，**不要**把多天合进同一个文件（冲突面会变大）
- 无 `pages` 的条目（失败标记、已合并会话的残留）保留无害，打包校验会忽略它们
