"""程序自更新: 检查新版、下载新包、交给 update_apply.py 替换。

与 dataupdate 的分工: 那个只管 data/(每晚都有新数据, 从不碰代码); 这个只管程序本体
(只在新版本时动)。两者共用 srcfetch 的通道与代理策略。

**不在启动路径上**: 检查放在 /api/app/latest, 更新放在 /api/app/update, 都由用户看到
提示后自己触发。自动更新一旦在启动时自己跑, 断网/半截下载就会拖慢甚至卡住用户打开
页面 —— 那是拿"能不能看"去换"是不是最新"。

Release 的解析也放在这里(而不是 srcfetch): srcfetch 是通用抓取层, 每次改发布格式
都往里加会把它撑成第二个 server.py; 而且这些逻辑只有这一个消费方。
"""

import asyncio
import html
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import srcfetch
from srcfetch import GH_OWNER, GH_REPO

RELEASES_ATOM = f"https://github.com/{GH_OWNER}/{GH_REPO}/releases.atom"


def mirrors_release(url: str) -> list:
    """Release 相关地址(releases.atom 与附件的下载地址)的候选通道: 镜像在前, 官方兜底。

    为什么不复用 srcfetch.mirrors(): 那个是为 raw.githubusercontent.com 写的, 里面带
    jsDelivr 分支, 而 jsDelivr 不代理 Release 页面与附件, 硬套会拼出无效地址。
    实测: 直连 github.com 会超时/被重置, 两个镜像都能通(附件还支持 Range)。

    非 GitHub 地址(回归测试的本地服务)原样返回, 否则会拼出无效 URL。
    """
    if not str(url).startswith("https://github.com/"):
        return [url]
    return [
        f"https://gh-proxy.com/{url}",
        f"https://ghfast.top/{url}",
        url,
    ]


def _atom_entry(raw: bytes):
    """解析 atom, 返回 (tag, 正文 HTML)。失败抛 srcfetch.FetchError。

    只取第一个 entry(最新的那个)。**从 link 取 tag, 不从 title 取**: title 是
    Release 标题(作者可能写成"弹幕时光机 1.2.0"), 只有 link 末尾那一段必定是 tag 名。
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise srcfetch.FetchError(f"发布信息格式不对: {e}") from e
    ns = "{http://www.w3.org/2005/Atom}"
    entry = root.find(f"{ns}entry")
    if entry is None:
        # feed 能拉到但没有 entry = 作者还没发过 Release。这是"正常但无数据",
        # 与网络失败要分开报, 否则用户会一直以为是自己网络问题。
        raise srcfetch.FetchError("远端还没有发布过版本（Releases 为空）")
    tag = ""
    for ln in entry.findall(f"{ns}link"):
        href = ln.get("href") or ""
        if "/releases/tag/" in href:
            tag = href.split("/releases/tag/", 1)[1].strip("/")
            break
    if not tag:
        tag = (entry.findtext(f"{ns}title") or "").strip()
    if not tag:
        raise srcfetch.FetchError("发布信息里没有 tag 名")
    tag = tag[1:] if tag[:1] in ("v", "V") else tag
    body = entry.findtext(f"{ns}content") or entry.findtext(f"{ns}summary") or ""
    return tag, body


def release_download_url(version: str, kind: str = "slim") -> str:
    """Release 附件的下载地址。

    **不解析正文里的链接**: releases.atom 的正文只有 `<code>xxx.zip</code>` 这种文件名
    文本, 没有真正的 href(实测 1.3.0 的 entry 全文只有一个 issues 链接)。附件名与路径
    都是发布脚本(tools/release.py)定死的, 按规则拼比解析正文可靠得多。

    返回的是**官方地址**, 实际下载要走 mirrors_release() 换通道。
    """
    name = f"danmaku-timewarp-{version}-{kind}.zip"
    return f"https://github.com/{GH_OWNER}/{GH_REPO}/releases/download/v{version}/{name}"


def latest_release() -> dict:
    """最新 Release 的 {version, notes}。

    notes 是正文 HTML 原文, 由调用方用 notes_brief() 清洗后给界面。

    **atom 也要走镜像**: 实测 github.com 会整段时间连不上(超时/连接被重置)。
    只让"下载"走镜像是不够的 —— 检查这一步不通, 用户连"有新版本"都看不到。
    """
    last = None
    for url in mirrors_release(RELEASES_ATOM):
        try:
            raw = srcfetch.get_once(url, timeout=srcfetch.TIMEOUT)
            break
        except Exception as e:              # noqa: BLE001 —— 换下一个通道
            last = e
    else:
        raise srcfetch.FetchError(f"连不上发布信息：{last}")
    tag, body = _atom_entry(raw)
    return {"version": tag, "notes": body}


def latest_release_tag() -> str:
    """远端最新 Release 的版本号(去掉 v 前缀), 如 "1.2.0"。

    保留这个薄封装是因为 /api/overview 与「拉取弹幕」只关心版本号。
    """
    return latest_release()["version"]


class UpdateError(Exception):
    """更新失败, 消息可直接展示给用户。"""


# 下载中的包放这里, 与正式目录分开: 半截文件绝不能出现在包根。
STAGE_DIRNAME = "_update"

# 正文里"更新内容"这一节的标题(由 tools/release.py 的 build_notes 生成)
_SECTION_RE = re.compile(r"<h2[^>]*>\s*更新内容\s*</h2>(.*?)(?=<h2|\Z)", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def notes_brief(body: str, max_lines: int = 12, max_chars: int = 900) -> str:
    """从 Release 正文里抽出"本版改了什么", 变成纯文本给界面显示。

    **不做 Markdown/HTML 渲染**: 正文是作者写的 HTML, 里面有标题、链接、图片、
    甚至内嵌的下载说明。用户要看的只有"这次改了什么", 渲染整篇会让弹窗塞满
    与本版无关的内容(固定说明每版都一样)。所以只取「## 更新内容」之后的一段,
    去标签、压空白, 超长就截断。

    取不到那一节时**退回全文前几行**, 而不是返回空串: 空着会让人以为"没写更新
    内容", 而实际可能是作者改了标题写法。宁可多显示一点。
    """
    text = body or ""
    m = _SECTION_RE.search(text)
    seg = m.group(1) if m else text
    # <li> 与段落边界换成换行, 其余标签直接去掉 —— 先换行再去标签,
    # 否则 <li>a</li><li>b</li> 会粘成 "ab"。
    seg = re.sub(r"</(?:p|li|h\d|div|tr)>", "\n", seg, flags=re.I)
    seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
    seg = _TAG_RE.sub("", seg)
    seg = html.unescape(seg)
    lines = [ln.strip() for ln in seg.splitlines()]
    lines = [ln for ln in lines if ln]
    out, used = [], 0
    for ln in lines[:max_lines]:
        if used + len(ln) > max_chars:
            break
        out.append(ln)
        used += len(ln)
    return "\n".join(out)


def package_name(version: str, kind: str = "slim") -> str:
    return f"danmaku-timewarp-{version}-{kind}.zip"


def check_zip(path: Path, want_version: str) -> None:
    """确认下下来的确实是个完整、对版本的新包。

    三道检查缺一不可:
      1. 能当 zip 打开(zipfile 会校验中央目录, 截断必然失败)
      2. 里面有 VERSION 且等于期望版本
      3. 有 server.py —— 有 VERSION 但没代码的包一样跑不起来

    **不校验 sha256**: Release 正文里没有哈希清单, 作者也没发布 .sha256。
    与其编一个校验步骤让人以为"验过了", 不如老实做"能不能装"的检查。
    """
    if not path.is_file():
        raise UpdateError("下载的文件不存在")
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            bad = z.testzip()
            if bad:
                raise UpdateError(f"包已损坏（{bad} 解压失败）")
            ver_name = next((n for n in names if n.count("/") == 1 and n.endswith("/VERSION")), "")
            if not ver_name:
                raise UpdateError("包里没有 VERSION，不是程序包")
            ver = z.read(ver_name).decode("utf-8", "replace").strip().splitlines()
            got = ver[0].strip() if ver else ""
            if want_version and got != want_version:
                raise UpdateError(f"包里的版本是 {got}，与要更新的 {want_version} 不一致")
            if not any(n.endswith("/danmaku-server/server.py") for n in names):
                raise UpdateError("包里没有 server.py，不是完整的程序包")
    except zipfile.BadZipFile as e:
        raise UpdateError(f"下载的包不完整或不是 zip：{e}") from e


def download(version: str, root: Path, kind: str = "slim") -> Path:
    """下载指定版本的程序包到 <root>/_update/ 下, 返回文件路径。

    走 srcfetch.download_mirrors 的通道链(镜像优先)。**下完立刻校验**, 坏包当场
    丢掉 —— 留着会让下一次"更新"误以为已经下好了。
    """
    url = srcfetch.release_download_url(version, kind)
    stage = root / STAGE_DIRNAME
    stage.mkdir(parents=True, exist_ok=True)
    dest = stage / package_name(version, kind)
    tmp = dest.with_suffix(".part")

    last = None
    for u in srcfetch.mirrors_release(url):
        try:
            data = srcfetch.get_once(u, timeout=60)
        except Exception as e:              # noqa: BLE001 —— 换下一个通道
            last = e
            continue
        tmp.write_bytes(data)
        try:
            check_zip(tmp, version)
        except UpdateError as e:
            tmp.unlink(missing_ok=True)
            last = e
            continue
        tmp.replace(dest)
        return dest
    raise UpdateError(f"下载新版本失败：{last}")


async def run_update(version: str, root: Path, port: int,
                     kind: str = "slim") -> dict:
    """下载 -> 校验 -> 起替换器。**不负责退出主进程**, 由调用方在响应发出后再退。

    用一个独立进程做替换, 是因为 Windows 不允许删除正在运行的程序文件。
    替换器会等端口释放(旧进程真的退出了)再动目录, 所以主进程退出得越晚,
    替换器等得越久但不会出错 —— 顺序是安全的。
    """
    pkg = await asyncio.to_thread(download, version, root, kind)
    script = Path(__file__).resolve().parent / "update_apply.py"
    if not script.is_file():
        raise UpdateError("缺少 update_apply.py，无法替换程序文件")
    log = root / STAGE_DIRNAME / "update.log"
    # 脱离父进程启动: 父进程马上要退出, 子进程不能跟着一起死。
    flags = 0x00000008 if sys.platform == "win32" else 0   # DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, str(script), "--zip", str(pkg), "--root", str(root),
         "--port", str(port), "--version", version, "--log", str(log)],
        cwd=str(root), creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    return {"package": str(pkg), "log": str(log)}
