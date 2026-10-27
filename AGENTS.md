# AGENTS.md — LOL 延时伪直播

本仓 = 业务文档 + 代码。业务事实见 `业务约束.md`，结构见 `架构总览.md`，数据契约见 `SCHEMA.md`。
分层：本文件（纪律）→ `业务约束.md`（项目事实）→ `架构总览.md` + `模块设计/`（结构）。

## 干活顺序

1. **先读后写**：改代码前读该模块与相关契约（改字段必读 `SCHEMA.md`）。说不出改动落在哪句上，就别动笔。
2. **小步验**：一处一改，改完立刻验，不攒批。
   ```powershell
   python -c "import ast;ast.parse(open('danmaku-server/server.py',encoding='utf-8').read())"
   node --check danmaku-server\web\app.js
   python tools\quality_gate.py
   danmaku-server\.venv\Scripts\python.exe tests\test_align.py
   ```
3. **修根因**：改前 grep 该函数的全部调用方，修在共享处，不只修报告点名的那条路径。
4. **走前清现场**：不留下占端口的进程、不留下 `.bak`、不留下未入库的临时脚本。`git status` 要干净。

## 红线

- **两端不许互相 import**。采集端要能单独拷到手机 Termux 跑；播放端只读采集端的数据目录。跨端只通过文件系统。
- **指纹数学定义改一处必改两处**（`danmaku-server/audiofp.py` 与 `danmaku-collector/live_audiofp.py`），改完必须跑 `tests/test_align.py` 的逐 bit 等价断言。
- **`data/` 下的产物不删不改**。音频指纹无法再生——直播流消失后，那晚就永远不能自动对齐。
- **写对齐记录只走 `update_matches()`**，它带锁 + 唯一临时名 + 原子替换，且只落「与 `align/` 不同」的记录。
- **绝不写 `danmaku-server/align/`**。那是上游分发的只读产物，写了会让用户 `git pull` 冲突。
- **`.bat` 保持 UTF-8 无 BOM + CRLF**（`.gitattributes` 已固定）。改成 LF 会让 cmd 把多行当一条命令、中文乱码。
- **启动脚本保持前台运行**。后台化会改变进程生命周期，牵扯退出脚本的定位逻辑。
- **判据里的数字不许写死在文档**：行数、超标项数由 `python tools\quality_gate.py` 现场产出。

## 架构纪律

- 新增超标（文件 400 行 / 函数 60 行 / 复杂度 15）**不登记**，先拆；存量债已在 `架构总览.md` 登记，只减不增。
- 新模块先在 `架构总览.md` §模块 表占一行（职责 + 禁止依赖），再写代码。
- 依赖方向以 `架构总览.md` 的 Mermaid 图为准；图与代码不符时改图，不靠记忆。
- 死代码 0：不留「以后可能用到」的开关和参数。发现死常量先判断它是不是上一代算法的残留——留着比删掉更危险。

## 文档纪律

- 一个事实一个家：同一件事只写一处，其余给引用。
- 说话：第一句给判断，依据往后放。
- 讲结构 / 流程 / 状态变化用 Mermaid，一图一主题；单点问答不画。
- 要人决定时给四点：必要性、改后预期、工作量、推荐。

## 本仓布局

| 内容 | 位置 |
|---|---|
| 采集端 / 播放端 | `danmaku-collector/` / `danmaku-server/` |
| 数据契约 | `SCHEMA.md`（根） |
| 结构 / 业务约束 / 术语 | `架构总览.md` / `业务约束.md` / `术语表.md` |
| 模块设计 | `模块设计/` |
| 质量闸门 / 回归 | `tools/` / `tests/` |
| 一次性迁移脚本 | 4 个，已挪进 `tools/`（运行期不参与） |
| 临时产出 | `tmp/`（gitignore 不入库） |
