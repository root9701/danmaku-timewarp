# -*- coding: utf-8 -*-
"""srcfetch.py — 多通道取文件(含完整性校验)。

为什么单独一层:
  "从哪下"和"下到哪"是两个独立的问题。dataupdate 管后者(比对清单、原子落盘、
  防路径穿越), 本模块管前者(多个镜像之间降级、代理策略、sha256 校验)。
  混在一起会让 dataupdate 既超长又难测 —— 实测把下载通道的逻辑单独测,
  才能覆盖"镜像返回坏内容"这种线上很难复现的情形。

为什么需要多通道:
  实测(中国大陆, 无代理): raw.githubusercontent.com 拉 7MB 要 30 秒以上甚至超时,
  而 github.com 主站 0.7 秒就通 —— 被单独限速的是 raw 这个子域。目标用户不会开
  代理, 只给官方地址等于"下载数据"这个功能对多数人是坏的。

为什么必须校验 sha256:
  镜像是不受我们控制的第三方: 可能缓存了旧文件, 也可能被篡改。数据**不可再生**
  (直播流消失后 afp 永远造不出来), 所以不能只比字节数 —— 等长篡改发现不了。
  实测这个校验真的抓到过问题: .gitattributes 缺 data/** -text 时, 工作区被
  CRLF 污染, 作者算出的哈希与仓库里的字节不一致, 4 个通道全部校验失败。

**已知残余风险(接受, 不修)**: 清单(data-index.json)和各文件走的是同一批通道,
镜像若**同时**篡改清单与文件, 可以伪造出一份"内容 + sha256 自洽"的假数据 —— 这层
校验挡不住。彻底封死需要第二通道(如用作者私钥签名清单, 客户端内置公钥验签),
代价是引入密钥管理、轮换与丢失恢复流程, 对一个"辅助看回放"的本地工具不划算。
当前定位: 防**单个镜像**缓存过期/损坏/顺手改内容, 不防"镜像与清单一起被换"。

依赖: 只用标准库。本模块**不碰 data/, 不写任何文件**。
"""
import gzip
import hashlib
import io
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib

# 数据源: 只允许从这里取。改这里等于改分发渠道。
GH_OWNER = "root9701"
GH_REPO = "danmaku-timewarp"
GH_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/{GH_BRANCH}"

# 只有这个官方前缀才值得套镜像; 测试与自建源(127.0.0.1)直接用原地址,
# 否则会拼出 gh-proxy.com/http://127.0.0.1... 这种无效串。
_OFFICIAL_PREFIX = "https://raw.githubusercontent.com/"

UA = "danmaku-timewarp-updater/1"
# 连接超时与读取超时分开: 挂死的现场通常是"连上了但不返回", 读取超时才有用。
# 30 秒整体超时在直连被阻断时会等满, 前端表现为长时间无响应 —— 用户以为程序卡了。
TIMEOUT = 20
# 镜像给更短的超时: 通道有 4 个, 每个都等 20 秒的话全失败要一分多钟,
# 用户会以为程序死了。镜像不通就该快速放弃去试下一个。
MIRROR_TIMEOUT = 10
_MAX_ATTEMPTS = 2


class FetchError(Exception):
    """取文件失败, 消息可直接展示给用户。"""


def _proxies() -> dict:
    """当前生效的代理配置。

    为什么需要这个:
      Python 的 urllib 默认用 getproxies(), 它**只读环境变量**。而 Windows 上
      用户常见的做法是在系统设置里配代理(Clash/V2Ray 之类), 那是注册表 +
      WinINet 的配置, urllib 读不到 —— 于是程序直连 GitHub, 在需要代理的网络
      环境下会 RemoteDisconnected 或长时间挂起。

    为什么不能写死走代理:
      用户的代理是**按需开启**的。代理没开时硬走它, 反而全部失败。
      所以这里读"当前是否有系统代理", 有就用, 没有就直连。

    为什么必须显式带 no 列表:
      ProxyHandler 只认字典里的 no 项。丢了它, localhost / 127.0.0.1 的请求
      也会被送去代理 —— 而本机服务(回归测试的假数据源、局域网回放)不该走代理,
      且多数代理对本地地址的处理并不一致, 表现为时通时不通。
    """
    try:
        reg = urllib.request.getproxies_registry() or {}
    except Exception:
        reg = {}
    src = reg or {k: v for k, v in urllib.request.getproxies().items() if k != "no"}
    if not src:
        return {}
    no = reg.get("no") or ",".join(
        ["127.0.0.1", "localhost", "::1"])
    return {**src, "no": no}


def mirrors(url: str) -> list:
    """给定官方 raw 地址, 返回按速度排序的候选地址(含原地址兜底)。

    速度来自实测(无代理, 中国大陆, 7MB 文件):
      gh-proxy.com      1.75 MB/s
      ghfast.top        1.38 MB/s
      fastly.jsdelivr   0.33 MB/s
      cdn.jsdelivr      0.20 MB/s   (同 CDN 不同节点, 只留一个免得重复等)
      官方 raw          超时

    官方地址放**最后**而不是去掉: 有代理的用户它最快最可靠, 且它是唯一权威源。
    非官方地址(回归测试的 127.0.0.1)原样返回 —— 套镜像只会拼出无效 URL。
    """
    if not str(url).startswith(_OFFICIAL_PREFIX):
        return [url]
    rest = url[len(_OFFICIAL_PREFIX):]          # owner/repo/branch/路径
    parts = rest.split("/", 3)                  # [owner, repo, branch, 路径]
    chain = [
        f"https://gh-proxy.com/{url}",
        f"https://ghfast.top/{url}",
    ]
    if len(parts) == 4:
        # jsDelivr 格式: gh/{owner}/{repo}@{branch}/{path}
        chain.append(
            f"https://fastly.jsdelivr.net/gh/"
            f"{parts[0]}/{parts[1]}@{parts[2]}/{parts[3]}")
    chain.append(url)                # 官方源兜底
    return chain


def host(url: str) -> str:
    """取域名用于报错信息 —— 完整 URL 太长, 用户看不懂也不需要看。"""
    try:
        return str(url).split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return str(url)


def get_once(url: str, timeout: int = None) -> bytes:
    """下载单个 URL(不再换通道)。

    代理策略: 先按系统代理配置试, 失败再直连试一次。
    这样"代理开着"和"代理关着"两种状态都能工作, 不需要用户改配置。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    secs = TIMEOUT if timeout is None else timeout
    proxies = _proxies()
    # 依次尝试: 走代理 -> 直连。代理没配时第一个就是直连, 不会重复。
    plans = [proxies, {}] if proxies else [{}]
    last = None
    for plan in plans[: (_MAX_ATTEMPTS if proxies else 1)]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(plan))
        try:
            with opener.open(req, timeout=secs) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # HTTP 错误是服务器明确答复, 换路径也没用, 直接抛
            if e.code == 404:
                raise FetchError(
                    f"远端还没有这个文件(404)。作者可能尚未发布，或仓库名有变：{url}"
                ) from e
            raise FetchError(f"下载失败 HTTP {e.code}: {url}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            continue                     # 这条路径不通, 试下一条
    hint = ("；若你平时需要开代理才能访问 GitHub，请确认代理已启动"
            if proxies else "")
    raise FetchError(
        f"连不上数据源：{last.__class__.__name__}{hint}") from last


def content_ok(name: str, blob: bytes):
    """不看清单, 直接判断内容本身是否完整。返回 True/False/None(无法判断)。

    为什么需要这条路径:
      清单里的 bytes 是**元数据**, 它可能本身就是错的 —— 实测就发生过:
      .gitattributes 缺 data/** -text 时工作区被 CRLF 污染, 清单按被污染的本地
      文件算出 7316313, 而仓库里实际是 7315759。此时"按清单字节数校验"会让
      **每个通道都失败**, 下载功能彻底不可用 —— 而内容其实是好的。
      教训: 元数据可疑时, 要靠内容自身的校验能力来判断, 别把数字当真理。

    gzip 自带 CRC32: 截断、等长篡改、尾部追加都能发现(test_update 有断言)。
    .jsonl 没有自校验, 但它是逐行 JSON —— 截断必然断在最后一行, 解析就会失败。
    manifest.json 是整份 JSON: 截断/损坏会让解析直接失败, 也是可靠判据。
    """
    if name.endswith(".gz"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:
                while f.read(1 << 20):
                    pass
        except (OSError, EOFError, zlib.error):
            return False
        return True
    if name.endswith(".json"):
        # manifest.json / meta.json: 整份必须是合法 JSON
        try:
            json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        return True
    if name.endswith(".jsonl"):
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            return False
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return True                     # 空切片是合法的
        try:
            json.loads(lines[0])
            json.loads(lines[-1])           # 截断一定断在末尾, 验最后一行最有效
        except ValueError:
            return False
        return True
    return None                             # 二进制等无法自证, 交给哈希


def _check(blob: bytes, sha: str, want_bytes, hostname: str, name: str = ""):
    """校验内容。通过返回 None, 不通过返回失败原因(字符串)。

    优先级: sha256 > 内容自校验 > 字节数。
      sha256 是作者对"这份内容"的承诺, 最权威。
      没有 sha256 时(老清单), 若字节数对不上, 用内容自校验兜底 —— 见 content_ok
      的注释: 清单的字节数本身可能是错的, 但它不该让好文件下不来。
      两者都判不了才拒绝(unable to verify 不应该放行不可再生的数据)。

    清单自己(data-index.json)拉取时不带 sha/字节数 —— 那时还没有清单可依据,
    属于"先有鸡还是先有蛋", 只能不校验。这是可接受的: 清单解析失败会被上层拦下,
    而它不含不可再生的数据(重跑 pack_data --index 就能重建)。
    """
    if sha:
        if hashlib.sha256(blob).hexdigest() != sha:
            return f"{hostname} 内容哈希不符"
        return None                         # 哈希对 = 内容就是作者那份, 长度无关
    if want_bytes is None or len(blob) == want_bytes:
        return None
    ok = content_ok(name, blob)
    if ok is True:
        return None                         # 清单长度过时, 但内容完整
    if ok is False:
        return f"{hostname} 内容损坏(长度 {len(blob)}, 清单 {want_bytes})"
    return f"{hostname} 返回 {len(blob)} 字节, 期望 {want_bytes}"


def _attempt(cand: str, url: str, sha: str, want_bytes, timeout, name: str = ""):
    """试一个通道。

    返回 (内容, 失败原因, 是否内容不对)。成功时后两项是 ("", False)。
    404 会直接抛出去 —— 那是"远端根本没有", 换通道无意义。

    校验失败时**仍然返回内容**: 调用方要用它的哈希判断"多个通道是否给出同样的
    坏内容"(那说明清单错了, 不是镜像的问题)。返回 None 会让这个判断失效。
    """
    # 镜像给更短的超时: 4 个通道各等 20 秒, 全失败要一分多钟, 用户以为死机了。
    secs = timeout if timeout is not None else (
        TIMEOUT if cand == url else MIRROR_TIMEOUT)
    try:
        blob = get_once(cand, timeout=secs)
    except FetchError as e:
        if "404" in str(e):
            raise
        return None, str(e)[:40], False
    problem = _check(blob, sha, want_bytes, host(cand), name)
    if problem:
        return blob, problem, True      # 连上了但内容不对
    return blob, "", False


def fetch(url: str, sha256: str = "", want_bytes=None,
          timeout: int = None, name: str = "") -> bytes:
    """按通道优先级下载, 每个通道都要过完整性校验。返回第一个通过的内容。

    为什么"校验失败"也当作通道失败去换下一个:
      第三方镜像可能缓存了旧版本文件(长度很可能不同, 也可能巧合相同)。
      对**不可再生**的数据来说, 拿到一个坏文件比下载失败严重得多 —— 下载失败
      用户会重试, 坏文件会静默变成"某天数据打不开"。

    什么情况下直接报错(而不是继续换):
      404。那是"远端根本没有", 换十个镜像也还是没有, 且说明作者没发这一天的数据。
      其余(连不上 / 校验不过)继续试下一通道, 全部失败才报错。

    报错要区分两种失败, 否则用户没法判断该等还是该找人:
      全部是网络失败 -> "连不上" (过会儿再试 / 检查代理)
      有通道连上了但内容不对 -> "校验失败" (数据源有问题, 重试没用)

    为什么"两个通道给出相同但不对的内容"就立刻停:
      镜像之间互相独立。若它们返回**逐字节相同**的内容却都过不了哈希, 说明问题
      不在某个镜像的缓存, 而在**清单本身**(作者改了数据却忘了重跑 pack_index)。
      这时把 21MB 逐个通道再下一遍毫无意义 —— 实测 7MB 文件四个通道全试要 57 秒,
      用户只会以为程序卡死。早停并说清"是清单问题", 用户才知道该找作者。
    """
    sha = (sha256 or "").strip().lower()
    last = None
    net_fail, bad_data = [], []
    seen_bad = set()                 # 已见过的"内容不对"的哈希
    for cand in mirrors(url):
        blob, problem, is_data = _attempt(cand, url, sha, want_bytes,
                                          timeout, name)
        if not problem:
            return blob
        last = FetchError(problem)
        if not is_data:
            net_fail.append(f"{host(cand)}: {problem}")
            continue
        bad_data.append(problem)
        got = hashlib.sha256(blob).hexdigest() if blob else ""
        if sha and got in seen_bad:
            # 两个独立通道给出相同内容都不对 -> 清单错了, 再试没有意义
            raise FetchError(
                f"数据与清单不一致(两个通道内容相同且都不符合 sha256)。"
                f"这是发布方的问题, 请稍后再试或联系作者: "
                f"{'; '.join(bad_data)}") from last
        seen_bad.add(got)
    if bad_data:
        # 有通道答了但内容不对: 这是数据源的问题, 重试不会变好, 必须说清楚
        raise FetchError(
            f"数据校验失败(文件可能已损坏或被改动, 重试无益)："
            f"{'; '.join(bad_data)}") from last
    detail = "; ".join(net_fail) if net_fail else str(last)
    raise FetchError(f"连不上任何下载通道：{detail}") from last


# ---- 程序版本: 读 GitHub Releases, 不读 data-index.json ----
#
# 为什么程序版本要读 Release 而不是清单里的 app_version:
#   清单是**数据发布**的产物, 只在作者跑 pack_data.py --index 时更新。
#   只改代码、不重新生成清单的提交, 用户永远收不到"有新版本"的提示 ——
#   于是"程序版本号"实际上被数据发布的节奏绑架了。
#   Release 的 tag 是**专门为程序发布**打的一次标记, 意图明确。
#
# 为什么读 releases.atom 而不是 api.github.com:
#   API 未认证时限 **60 次/小时, 按 IP 计**, 而且**不是**共享给所有用户的频次 ——
#   国内大量用户处在运营商 NAT 后面, 出口 IP 是几百人共用的, 60 次很容易被同 IP
#   的别人吃光(实测本机就吃到 403 rate limit exceeded, X-RateLimit-Remaining: 0)。
#   此时"拉取弹幕"会一直报"没查到程序版本" —— 功能形同废弃。
#   releases.atom 是 github.com 主站上的一个普通静态 feed: 无频次限制、无鉴权、
#   走的是与仓库页面同一条通路(本项目实测 raw 被限速而主站正常)。
#
# 为什么不用 /releases/latest 的 302:
#   它也能用, 但要自己解析 Location 头; 没有 Release 时会跳回 /releases,
#   两种页面得靠正则分辨 —— 比解析 XML 更脆。
RELEASES_ATOM = f"https://github.com/{GH_OWNER}/{GH_REPO}/releases.atom"


def latest_release_tag() -> str:
    """远端最新 Release 的 tag(去掉可能的 v 前缀), 如 "1.2.0"。

    失败一律抛 FetchError, **不返回 None** —— 调用方必须能区分"确实没有新版"
    与"没查到", 后者不该显示成"已是最新"。
    """
    raw = get_once(RELEASES_ATOM, timeout=TIMEOUT)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise FetchError(f"发布信息格式不对: {e}") from e
    ns = "{http://www.w3.org/2005/Atom}"
    entry = root.find(f"{ns}entry")
    if entry is None:
        # feed 能拉到但没有 entry = 作者还没发过 Release。这是"正常但无数据",
        # 与网络失败要分开报, 否则用户会一直以为是自己网络问题。
        raise FetchError("远端还没有发布过版本（Releases 为空）")
    # **从 link 取 tag, 不从 title 取**: 同一页面里 title 是 Release 的标题
    # (作者可能写成"弹幕时光机 1.2.0"), 只有 link 末尾那一段必定是 tag 名。
    tag = ""
    for ln in entry.findall(f"{ns}link"):
        href = ln.get("href") or ""
        if "/releases/tag/" in href:
            tag = href.split("/releases/tag/", 1)[1].strip("/")
            break
    if not tag:
        tag = (entry.findtext(f"{ns}title") or "").strip()
    if not tag:
        raise FetchError("发布信息里没有 tag 名")
    return tag[1:] if tag[:1] in ("v", "V") else tag


def release_page_url() -> str:
    """给用户去下载的页面地址。便携版用户没有 git, 只能走这里。"""
    return f"https://github.com/{GH_OWNER}/{GH_REPO}/releases/latest"
