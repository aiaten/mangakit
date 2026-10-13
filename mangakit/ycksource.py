"""异次元（yiciyuan）图源仓库 yck2026 → MangaKit 站点规则。

这个模块只做一件事：把「异次元漫画 App」格式的图源对象（yck2026.fun 上
`/yiciyuan/tuyuan/json/id/<sid>.json` 返回的那种 JSON）翻译成 MangaKit 自己的
站点规则 dict（就是 `mangakit/rules/*.json` 的格式），并判断这个源到底能不能用。

设计原则（很重要）：

1. `convert()` 是**纯函数**，不联网、不读文件，可以拿本地样本反复跑。
2. 只有三种结局，任何一种都会给出中文说明，绝不静默产出坏规则：
   - ``可转换：…``  —— 转出了规则（说明里会列出被降级/忽略的字段）
   - ``加密源：…``  —— 关键字段是 ``:_R002_…`` 这类不透明块，是阅读器 App 内部
     解密的，客户端没有实现，直接不支持
   - ``语法不支持：…`` / ``无法转换：…`` —— 选择器用了 MangaKit 表达不了的语法，
     或缺少关键字段
3. 转换出来的规则**必须**再进真实引擎跑一遍才算数，见 `check_source()`：
   复用 `probe.SiteProber._verify_rule()`（`mangakit/probe.py`）那套
   `parse_chapters` + `parse_pages` 真跑流程。
4. 拿不准的语法宁可报「不支持」，也不猜一个看起来能用的翻译 —— 猜错的结果是
   用户加了个站、点开漫画却是空的，比一开始就拒绝糟糕得多。

线程/事件循环约定（GUI 侧请注意）：

* `list_sources()` / `fetch_*()` / `check_source()` / `add_checked_source()` 都是
  **同步阻塞**函数，内部用 httpx 同步客户端联网、用 `asyncio.run` 驱动引擎自检。
  请放进后台线程（例如 QThreadPool）调用，不要在 GUI 线程里直接调。
* `check_source()` / `add_checked_source()` **不能在已有运行中的事件循环里调**。
  为了稳妥，模块内部用 `_run_sync()` 兜底：当前线程没有事件循环时走
  `asyncio.run()`，已经有事件循环时改丢到一个临时线程里跑，但同步调用方
  仍应假定它们是阻塞的。
* `check_source()` 与 `add_checked_source()` **不会把异常抛给调用方**，
  失败一律写进返回的 `CheckResult.message`（中文）。
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx

__all__ = [
    "YCK_INDEX_URL",
    "YCK_DETAIL_URL",
    "YCK_JSON_URL",
    "YckSource",
    "SourceEntry",
    "CheckResult",
    "UnsupportedSelector",
    "index_url",
    "parse_index",
    "parse_detail",
    "fetch_index",
    "fetch_detail",
    "fetch_source_json",
    "list_sources",
    "convert_selector",
    "convert",
    "check_source",
    "add_checked_source",
]

YCK_INDEX_URL = "https://www.yck2026.fun/yiciyuan/tuyuan/index.html"
YCK_DETAIL_URL = "https://www.yck2026.fun/yiciyuan/tuyuan/content/id/{sid}.html"
YCK_JSON_URL = "https://www.yck2026.fun/yiciyuan/tuyuan/json/id/{sid}.json"

# 索引页支持的服务端筛选参数（POST 与 GET 都认，这里走 GET）。
INDEX_FILTERS = ("keys", "uid", "order1", "order2", "faxian", "sousuo")
# order1：time（按时间）/ down（按下载量）；order2：1/2 升降序
# faxian / sousuo：0/1，是否只显示支持「发现」/「搜索」的源

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 30.0
_RETRIES = 4

# `:_R002_<hex>` / `:_H006_<base64>` —— 阅读器 App 内部加密的规则块。
# 站点客户端（content 页 / static/js/*.js）里没有任何解密逻辑，
# 所以只能判「不支持」，不要尝试猜密钥。
_ENC_RE = re.compile(r"^\s*:_([A-Za-z0-9]{1,8})_")

# 脚本规则：需要 App 里的 Java/Rhino 运行时（org.jsoup、java.put 之类），
# MangaKit 是纯 Python + lxml，表达不了。
# 注意：`@css:` **不是**脚本 —— 普查里它 28 次全部是「后面这段就是原样 CSS」的
# 显式标记（`@css: table[...] > tr@text`），所以单独处理、不能当脚本拒绝。
# 注意：`@xpath:` **也不在这里** —— 它是「后面这段就是 XPath」的显式标记，
# 而 MangaKit 的选择器原生支持 xpath，所以能直接映射（见 `_parse_single`）。
_SCRIPT_RE = re.compile(r"@js:|@put:|@get:|<js>|java\.|org\.jsoup")
# `@JSon:$.data[*].image` —— 取值来自 JSON 接口，MangaKit 只解析 HTML
_JSON_RE = re.compile(r"@json\s*:|@JSon\s*:", re.I)
# 显式 CSS 标记
_CSS_MARK_RE = re.compile(r"@css\s*:|^css\s*:", re.I)
# 方言里的取值标记（不是 HTML 属性名），MangaKit 没有对应能力
_EXTRACT_TOKENS = {"owntext", "alltext", "outerhtml", "innerhtml", "textnodes", "htmls"}

# HTML 标签名。异次元的 `@tag.a@href` / `@ul@li` 写法里，裸标识符既可能是
# 标签（`ul`）也可能是属性名（`href`），靠这张表区分：
#   - 非最后一段的裸标识符 → 一定是标签（下探）
#   - 最后一段的裸标识符 → 是标签就当节点、否则当取值属性
_HTML_TAGS = {
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base", "bdi",
    "bdo", "blockquote", "body", "br", "button", "canvas", "caption", "center",
    "cite", "code", "col", "colgroup", "data", "datalist", "dd", "del", "details",
    "dfn", "dialog", "div", "dl", "dt", "em", "embed", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head",
    "header", "hgroup", "hr", "html", "i", "iframe", "img", "input", "ins", "kbd",
    "label", "legend", "li", "link", "main", "map", "mark", "menu", "meta",
    "meter", "nav", "noscript", "object", "ol", "optgroup", "option", "output",
    "p", "param", "picture", "pre", "progress", "q", "rp", "rt", "ruby", "s",
    "samp", "script", "section", "select", "slot", "small", "source", "span",
    "strong", "style", "sub", "summary", "sup", "table", "tbody", "td", "template",
    "textarea", "tfoot", "th", "thead", "time", "title", "tr", "track", "u", "ul",
    "var", "video", "wbr",
}

# 常见的 HTML 属性名（不是标签）。裸标识符命中这里就当取值，否则查 _HTML_TAGS。
_ATTR_WORDS = {
    "href", "src", "text", "title", "alt", "content", "value", "style", "id",
    "class", "rel", "target", "width", "height", "name", "type", "datetime",
    "action", "method", "placeholder", "role", "lang", "dir", "media", "sizes",
    "srcset", "poster", "download", "aria-label", "data-url", "data-href",
}

# 选择器字段 → 语义名。键统一转小写比较（有的源写成小写开头）。
_CANON = {
    "rulesearchurl": "search.url",
    "rulesearchlist": "search.list",
    "rulesearchname": "search.name",
    "rulesearchcoverurl": "search.cover",
    "rulesearchnoteurl": "search.link",
    "rulesearchauthor": "search.author",
    "rulesearchlastchapter": "search.last_chapter",
    "rulesearchkind": "search.kind",
    "rulesearchurlnext": "search.next",
    "rulebookname": "comic.title",
    "rulecoverurl": "comic.cover",
    "rulebookauthor": "comic.author",
    "rulebookkind": "comic.tags",
    "ruleintroduce": "comic.description",
    "rulebooklastchapter": "comic.last_chapter",
    "rulebookurlpattern": "comic.url_pattern",
    "rulechapterlist": "chapters.list",
    "rulechaptername": "chapters.name",
    "rulechapterurl": "chapters.url",
    "rulecontenturl": "chapters.content_url",
    "rulechapterurlnext": "chapters.next",
    "rulebookcontent": "pages.list",
    "rulebookcontentdecoder": "pages.decoder",
    "rulecontenturlnext": "pages.next",
    "rulefindurl": "find.url",
}

# 缺了就没法用的字段
_CRITICAL = {"chapters.list", "pages.list", "chapters.content_url"}

_MULTI_TLD = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "co.jp", "ne.jp", "or.jp",
    "com.tw", "org.tw", "com.hk", "com.kr", "co.kr", "com.au", "co.uk", "org.uk",
    "com.br", "com.sg", "com.my", "com.vn", "co.th",
}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class YckSource:
    """索引页上的一条图源条目。"""

    sid: int
    title: str          # 索引里的标题（可能含网址）
    site_name: str      # 站点名（如「看漫画」）
    site_url: str       # 站点网址（如 https://m.manhuagui.com）
    author: str = ""
    updated: str = ""
    downloads: int = 0
    can_find: bool = False     # 「发」= 支持发现
    can_search: bool = False   # 「搜」= 支持搜索
    detail_url: str = ""
    json_url: str = ""
    raw: dict | None = None    # 异次元源对象（抓过才有）
    reason: str = ""           # 不可用原因


@dataclass
class SourceEntry:
    """给 GUI 列表用的一行（比 `YckSource` 再瘦一点，字段名对齐界面）。"""

    sid: int
    name: str          # 站点名（如「看漫画」）
    site: str          # 站点网址
    updated: str = ""
    uploader: str = ""
    downloads: int = 0
    can_search: bool = False
    can_find: bool = False
    json_url: str = ""


@dataclass
class CheckResult:
    """`check_source()` 的结果。

    按新版契约，`check_source` 返回的就是它；为了兼容任务书里的
    ``(规则, 说明, ProbeResult|None)`` 三元组写法，这个对象**可以直接解包**
    （``rule, msg, probe = check_source(sid)``），`probe` 就是下面的 `res`。
    """

    ok: bool = False
    status: str = "fail"          # ok | partial | fail
    message: str = ""
    rule: dict | None = None
    site_name: str = ""
    chapters: int = 0
    images: int = 0
    log: list[str] = None         # type: ignore[assignment]
    res: object = None            # probe.ProbeResult | None

    def __post_init__(self) -> None:
        if self.log is None:
            self.log = []

    def __iter__(self):
        """允许 ``rule, message, probe = check_source(...)``。"""
        return iter((self.rule, self.message, self.res))

    def __getitem__(self, i: int):
        return (self.rule, self.message, self.res)[i]

    def __len__(self) -> int:
        return 3


class UnsupportedSelector(ValueError):
    """选择器用了 MangaKit 表达不了的语法。`str(e)` 就是中文原因。"""


# --------------------------------------------------------------------------
# 索引页 / 详情页
# --------------------------------------------------------------------------


def index_url(page: int = 0, **filters: Any) -> str:
    """拼索引页 URL（分页 + 筛选）。

    page 从 0 开始（0 与「不带 page」等价，都是最新的一页）。筛选参数见
    `INDEX_FILTERS`，例如 `index_url(0, order1="down")` 按下载量排序、
    `index_url(2, sousuo=1)` 只看支持搜索的源。
    """
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 0
    qs: list[tuple[str, str]] = []
    if p > 0:
        qs.append(("page", str(p)))
    for k in INDEX_FILTERS:
        v = filters.get(k)
        if v not in (None, ""):
            qs.append((k, str(v)))
    for k, v in filters.items():
        if k not in INDEX_FILTERS and v not in (None, ""):
            qs.append((k, str(v)))
    return YCK_INDEX_URL + (("?" + urlencode(qs)) if qs else "")


def _split_title(title: str) -> tuple[str, str]:
    """「看漫画 https://m.manhuagui.com」→ («看漫画», «https://m.manhuagui.com»)。"""
    title = _clean_text(title)
    m = re.search(r"(https?://[^\s，,、]+)", title)
    if not m:
        return title, ""
    url = m.group(1).rstrip("/")
    name = (title[: m.start()] + " " + title[m.end():]).strip(" -–—|｜:：")
    return _clean_text(name) or title, url


def _clean_text(s: Any) -> str:
    if s is None:
        return ""
    s = _html.unescape(str(s))
    s = "".join((" " if ch < " " or ch == "\x7f" else ch) for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def _node_text(node: Any) -> str:
    try:
        return _clean_text(node.text_content())
    except Exception:
        return ""


def _lxml(html: str) -> Any:
    from lxml import html as LH

    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")
    return LH.fromstring(html)


def parse_index(html: str) -> list[YckSource]:
    """解析索引页，返回条目列表（第 0 页 100 条）。

    只认「含 `input[name="ids[]"]` 的条目块」，避免把页面上的其它格子当数据；
    徽章文本是拼接的单字（形如 ``发 搜 ``），所以判定用「字在不在文本里」，
    不能用等号比较。
    """
    if not html:
        return []
    try:
        doc = _lxml(html)
    except Exception:
        return []

    out: list[YckSource] = []
    seen: set[int] = set()
    for div in doc.xpath("//div[contains(@class,'layui-col-md4')]"):
        ids = div.xpath(".//input[@name='ids[]']/@value")
        if not ids:
            continue
        try:
            sid = int(str(ids[0]).strip())
        except (TypeError, ValueError):
            continue
        if sid <= 0 or sid in seen:
            continue

        a_nodes = div.xpath(".//h2/a")
        if not a_nodes:
            a_nodes = div.xpath(".//a[@href]")
        if not a_nodes:
            continue
        href = (a_nodes[0].get("href") or "").strip()
        title = _node_text(a_nodes[0])
        if not title and not href:
            continue
        site_name, site_url = _split_title(title)

        spans = [n for n in div.xpath(".//span")]
        badge = ""
        author = ""
        downloads = 0
        for sp in spans:
            t = _node_text(sp)
            if not t:
                continue
            cls = sp.get("class") or ""
            if "layui-font-orange" in cls or (("发" in t or "搜" in t) and len(t) <= 8):
                badge += t
            if t.startswith("用户") or t.startswith("分享者"):
                author = t.split(":", 1)[-1].split("：", 1)[-1].strip()
            m = re.search(r"下载[:：]?\s*(\d+)", t)
            if m and "下载" in t:
                try:
                    downloads = int(m.group(1))
                except ValueError:
                    pass

        updated = ""
        for p in div.xpath(".//p[contains(@class,'m-right')]"):
            updated = _node_text(p)
            if updated:
                break

        seen.add(sid)
        out.append(
            YckSource(
                sid=sid,
                title=title,
                site_name=site_name,
                site_url=site_url,
                author=author,
                updated=updated,
                downloads=downloads,
                can_find="发" in badge,
                can_search="搜" in badge,
                detail_url=urljoin(YCK_INDEX_URL, href) if href else "",
                json_url=YCK_JSON_URL.format(sid=sid),
            )
        )
    return out


def parse_detail(html: str) -> dict:
    """解析详情页，返回 ``{site_name, site_url, author, updated, json_url}``。

    详情页里混着混淆过的广告脚本（``_0xad8d(...)`` 变量表），这里只读
    ``<p>字段: 值</p>`` 与 ``input#jsonurl``，不去碰任何 ``<script>``，
    所以广告不会被当成数据。
    """
    out = {"site_name": "", "site_url": "", "author": "", "updated": "", "json_url": ""}
    if not html:
        return out
    labels = {
        "网站名称": "site_name",
        "网站地址": "site_url",
        "网站网址": "site_url",
        "源分享者": "author",
        "分享者": "author",
        "更新时间": "updated",
        "更新时间:": "updated",
    }
    try:
        doc = _lxml(html)
    except Exception:
        doc = None

    if doc is not None:
        for p in doc.xpath("//p"):
            t = _node_text(p)
            if not t:
                continue
            m = re.match(r"^([^:：]{2,10})[:：]\s*(.+)$", t)
            if not m:
                continue
            key = labels.get(m.group(1).strip())
            if key:
                out[key] = m.group(2).strip()
        for v in doc.xpath("//input[@id='jsonurl']/@value"):
            if v and "/json/" in v:
                out["json_url"] = str(v).strip()
                break
        if not out["json_url"]:
            for a in doc.xpath("//a[@href]"):
                href = a.get("href") or ""
                if "/tuyuan/json/id/" in href:
                    out["json_url"] = href.strip()
                    break

    if not out["json_url"]:
        m = re.search(r"/yiciyuan/tuyuan/json/id/(\d+)\.json", html)
        if m:
            out["json_url"] = YCK_JSON_URL.format(sid=int(m.group(1)))
    if out["site_url"] and not out["site_url"].startswith("http"):
        out["site_url"] = "https://" + out["site_url"].lstrip("/")
    return out


# --------------------------------------------------------------------------
# 联网抓取（同步，内部 httpx）
# --------------------------------------------------------------------------


def _http_get(url: str, *, timeout: float = _TIMEOUT, retries: int = _RETRIES,
              headers: dict[str, str] | None = None) -> httpx.Response:
    """带重试的 GET。

    站点会偶发 ``RemoteProtocolError: Server disconnected without sending a
    response.`` 与 ``ConnectError``（同一脚本连续请求时更明显），重试一次
    基本就过，所以这里必须带退避重试。
    """
    h = {"User-Agent": _USER_AGENT, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=h) as c:
                resp = c.get(url)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001 —— 全部转成最后一次异常抛给调用者
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
    assert last is not None
    raise last


def _http_post(url: str, data: dict[str, str], *, timeout: float = _TIMEOUT,
               retries: int = _RETRIES) -> httpx.Response:
    """带重试的 POST（索引页的筛选表单就是 POST）。"""
    h = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
    }
    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=h) as c:
                resp = c.post(url, data=data)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
    assert last is not None
    raise last


def _resp_text(resp: httpx.Response) -> str:
    if not resp.encoding:
        resp.encoding = "utf-8"
    text = resp.text
    if "\ufffd" in text[:2000]:
        return resp.content.decode("utf-8", errors="replace")
    return text


def list_sources(
    *,
    page: int = 1,
    keys: str = "",
    order: str = "time",
    faxian: str = "",
    sousuo: str = "",
    uid: str = "",
) -> list[SourceEntry]:
    """列出图源（POST 索引页，一次拿一页，不逐条抓详情）。

    ``page`` 是界面上的页码，从 1 开始。站点自己的页码有点怪：URL 的
    ``page=0`` 与 ``page=1`` 返回同一页，``page=2`` 才是第二页 —— 所以这里
    page<=1 时不带 page 参数，page>=2 时原样透传。

    ``order`` 取 ``time``（按时间）或 ``down``（按下载量），映射到站点的
    ``order1``；``faxian`` / ``sousuo`` 传 ``"0"``/``"1"`` 或空串表示不筛。
    """
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    url_page = 0 if p <= 1 else p
    data: dict[str, str] = {"keys": keys or "", "uid": uid or ""}
    data["order1"] = "down" if str(order).lower() in ("down", "download", "下载") else "time"
    data["order2"] = "2" if data["order1"] == "down" else "1"
    for k, v in (("faxian", faxian), ("sousuo", sousuo)):
        if v not in (None, ""):
            data[k] = str(v)
    if url_page:
        data["page"] = str(url_page)

    resp = _http_post(YCK_INDEX_URL, data)
    entries = parse_index(_resp_text(resp))
    return [_entry_of(s) for s in entries]


def _entry_of(s: YckSource) -> SourceEntry:
    return SourceEntry(
        sid=s.sid,
        name=s.site_name or s.title,
        site=s.site_url,
        updated=s.updated,
        uploader=s.author,
        downloads=s.downloads,
        can_search=s.can_search,
        can_find=s.can_find,
        json_url=s.json_url or YCK_JSON_URL.format(sid=s.sid),
    )


def fetch_index(page: int = 0, **filters: Any) -> list[YckSource]:
    """抓取并解析索引页（同步）。网络异常直接抛 httpx 异常。"""
    resp = _http_get(index_url(page, **filters))
    return parse_index(_resp_text(resp))


def fetch_detail(sid: int) -> dict:
    """抓取并解析详情页（同步）。"""
    resp = _http_get(YCK_DETAIL_URL.format(sid=int(sid)))
    return parse_detail(_resp_text(resp))


def fetch_source_json(sid: int) -> dict | None:
    """抓图源 JSON，返回异次元源对象（顶层 list 的第 0 个元素）。

    返回的对象里会补一个 ``_yck_id``（源在 yck 站上的 id），`convert()` 用它
    生成 ``yck<sid>`` 形式的规则 id。抓不到就返回 None。
    """
    try:
        resp = _http_get(YCK_JSON_URL.format(sid=int(sid)))
        data = json.loads(_resp_text(resp))
    except Exception:  # noqa: BLE001 —— 拿不到就是 None，说明交给调用方
        return None
    obj: Any = None
    if isinstance(data, list) and data:
        obj = data[0]
    elif isinstance(data, dict):
        obj = data
    if not isinstance(obj, dict):
        return None
    obj.setdefault("_yck_id", int(sid))
    return obj


# --------------------------------------------------------------------------
# 选择器翻译（纯函数）
# --------------------------------------------------------------------------


def _split_at(expr: str) -> list[str]:
    """按 ``@`` 切段，但跳过 ``[...]`` 里的内容（CSS 属性选择器、XPath 谓词）。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in expr:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "@" and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def _strip_header(expr: str) -> tuple[str, dict[str, str], str]:
    """剥掉尾部的 ``@Header:{...}``，返回（剩余选择器, headers, 说明）。"""
    m = re.search(r"@header\s*:", expr, re.I)
    if not m:
        return expr, {}, ""
    core = expr[: m.start()]
    tail = expr[m.end():].strip()
    if not tail.startswith("{"):
        return core, {}, "已忽略无法解析的 @Header 后缀"
    depth = 0
    end = -1
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return core, {}, "已忽略无法解析的 @Header 后缀"
    body = tail[1:end]
    headers: dict[str, str] = {}
    ref = re.search(r"referer\s*:\s*['\"]?([^'\",}]+)", body, re.I)
    if ref:
        headers["Referer"] = ref.group(1).strip()
    ua = re.search(r"user-agent\s*:\s*['\"]?([^'\",}]+)", body, re.I)
    if ua:
        headers["User-Agent"] = ua.group(1).strip()
    return core, headers, "已忽略源里指定的部分请求头（只保留 Referer/User-Agent）"


def _regex_replace_spec(pat: str, rep: str) -> dict | None:
    """把 ``#正则#捕获组`` 映射成引擎的 ``{"re": ..., "group": N}``。

    引擎的 ``Selector`` 支持 ``re`` + ``group``：「先取文本/属性，再用正则从结果里
    挖一次」。异次元里 ``#(^.*?【.*?】).*$#$1`` 这种「正则 + ``$1``」正好是同一语义，
    可以精确映射；只有替换串不是单纯的捕获组引用（``$1``/``\\1``/``$0``）时才拒绝。
    """
    m = re.fullmatch(r"\s*[$\\](\d+)\s*", str(rep or ""))
    if not m:
        return None
    return {"__re__": str(pat), "group": int(m.group(1))}


def _split_replace(expr: str) -> tuple[str, Any, str, bool]:
    """切出异次元的后缀替换语法。

    支持三种写法：``##模式##替换``、``#^#模式#^#替换``、``#模式#替换``
    （最后一种省略替换表示删掉）。返回（核心选择器, replace 规格, 说明,
    是否含正则）。
    """
    for sep in ("#^#", "##"):
        parts = expr.split(sep)
        if len(parts) >= 3:
            core, pat, rep = parts[0], parts[1], parts[2]
            if not core.strip():
                return expr, None, "", False
            ok, lit = _literal_pat(pat)
            if not ok:
                rx = _regex_replace_spec(pat, rep)
                if rx is not None:
                    return core, rx, f"已映射正则替换 {pat!r}→{rep!r}（捕获组）", False
                return core, None, f"后缀替换 {pat!r} 是正则写法", True
            return core, ([lit, rep] if rep else [lit]), f"已映射替换 {pat!r}→{rep!r}", False

    # 单个 `#`：后面不能再带 `@`（那样是 CSS 里的 #id，例如 class.color.#ffffff@text）
    depth = 0
    for i, ch in enumerate(expr):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "#" and depth == 0:
            core, tail = expr[:i], expr[i + 1:]
            # 单 `#` 只有在「选择器@取值#模式」这个形态下才是替换分隔符。
            # 纯 CSS 的 #id 不能切（例如 730 的 `-@css:#mh-chapter-list-ol-0 li a`）。
            if not core.strip() or "@" not in core or "@" in tail or not tail:
                break
            if "#" in tail:
                pat, rep = tail.split("#", 1)
            else:
                pat, rep = tail, ""
            ok, lit = _literal_pat(pat)
            if ok:
                return core, ([lit, rep] if rep else [lit]), f"已映射替换 {pat!r}→{rep!r}", False
            rx = _regex_replace_spec(pat, rep)
            if rx is not None:
                return core, rx, f"已映射正则替换 {pat!r}→{rep!r}（捕获组）", False
            return core, None, f"后缀替换 {pat!r} 是正则写法", True
    return expr, None, "", False


_REGEX_META = re.compile(r"[\^$*+?\[\]{}]|\\(?:d|w|s|D|W|S|b)")


def _literal_pat(pat: str) -> tuple[bool, str]:
    """判断替换模式能不能当「字面量」处理（Selector.replace 是 str.replace）。

    异次元里 ``\\|`` 这种转义只是为了保护分隔符，去掉反斜杠仍是字面量，
    所以先反转义再判断有没有正则元字符。
    """
    raw = str(pat)
    plain = re.sub(r"\\(.)", r"\1", raw)
    if _REGEX_META.search(plain):
        return False, plain
    return True, plain


def _split_index(rest: str) -> tuple[str, int | None, bool]:
    """拆出尾部的下标：``.2`` = 第 3 个（0 基）、``.-1`` = 倒数第 1 个。

    ``!N``（负索引）语义在异次元里没有权威文档，**不猜**，直接拒绝。
    """
    m = re.match(r"^(.*?)([.!])(-?\d+)$", rest)
    if not m:
        return rest, None, False
    name, kind, num = m.group(1), m.group(2), int(m.group(3))
    if kind == "!":
        raise UnsupportedSelector(
            f"片段 {rest!r} 用了 `!{num}` 负索引写法，异次元没有公开文档说明它的确切含义，"
            "MangaKit 也没有对应能力，不猜语义、直接放弃这个选择器"
        )
    if num < 0:
        return name, -num - 1, True
    return name, num, False


def _with_index(css: str, idx: int | None, last: bool) -> str:
    if idx is None:
        return css
    if last:
        return f"{css}:nth-last-of-type({idx + 1})"
    return f"{css}:nth-of-type({idx + 1})"


def _has_type_selector(css: str) -> bool:
    """CSS 片段是否以「标签名」开头。

    cssselect 的 ``:nth-of-type()`` 只对带标签的选择器成立（``p.x:nth-of-type(3)``
    可以，``.x:nth-of-type(3)`` 直接抛 ``ExpressionError: not implemented``），
    所以没有标签名又带下标时必须改走 XPath。
    """
    return bool(re.match(r"^[A-Za-z][\w-]*", css.strip()))


def _looks_like_css(s: str) -> bool:
    return bool(re.search(r"[.#\[\]>*:~ ]", s))


def _css_ok(expr: str) -> bool:
    """粗略校验一段 CSS 在引擎里能不能用（cssselect 或引擎自己的兜底）。"""
    try:
        from cssselect import GenericTranslator

        GenericTranslator().css_to_xpath(expr)
        return True
    except Exception:
        pass
    try:
        from .sites import _css_to_xpath

        _css_to_xpath(expr)
        return True
    except Exception:
        return False


def _xpath_step(css: str) -> str:
    """把一段 CSS 片段翻成一条 XPath 定位步。"""
    from cssselect import GenericTranslator

    return GenericTranslator().css_to_xpath(css.strip())


_XPATH_MARK = "__xpath__"


def _steps_to_selector(
    steps: list[tuple[str, int | None, bool]], value: tuple[str, Any] | None
) -> dict:
    """把「CSS 片段 + 下标」串成引擎能吃的选择器（能用 CSS 就用 CSS，否则 XPath）。

    下标是异次元的「取匹配到的第 N 个」语义，不是 nth-child。带标签的选择器
    可以精确写成 ``:nth-of-type(N+1)``；不带标签的选择器只能整条翻成 XPath
    ``(...)[N+1]``，这才是同一语义。
    """
    if len(steps) == 1 and steps[0][0].startswith(_XPATH_MARK):
        spec: dict[str, Any] = {"xpath": steps[0][0][len(_XPATH_MARK):]}
    elif not steps:
        # 只有取值、没有节点选择器（如 `href`、`text`）：引擎会作用在传入的节点上。
        # **绝不能写 `"css": ""`** —— 空串是 falsy，会让引擎走错分支。
        spec = {}
    else:
        if any(css.startswith(_XPATH_MARK) for css, _i, _l in steps):
            raise UnsupportedSelector("XPath 片段不能和 CSS 片段拼在一条选择器里")
        need_xpath = any(
            idx is not None and not _has_type_selector(css) for css, idx, _last in steps
        )
        spec = {}
        if need_xpath:
            pieces: list[str] = []
            for css, idx, last in steps:
                try:
                    piece = _xpath_step(css)
                except Exception as e:  # noqa: BLE001
                    raise UnsupportedSelector(f"片段 {css!r} 无法翻成 XPath：{e}") from e
                if idx is not None:
                    piece = f"({piece})[last()-{idx}]" if last else f"({piece})[{idx + 1}]"
                pieces.append(piece)
            spec["xpath"] = "/".join(pieces)
        else:
            spec["css"] = " ".join(_with_index(css, idx, last) for css, idx, last in steps)
    if value is None:
        value = ("text", None)
    if value[0] == "text":
        spec["text"] = True
    else:
        spec["attr"] = value[1]
    return spec


def _split_cands(core: str) -> list[str]:
    """按 ``||`` / ``&&`` / ``|`` / ``&`` 切候选，跳过 ``[]`` 和引号里的内容。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(core):
        ch = core[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in "|&":
            j = i
            while j < len(core) and core[j] in "|&":
                j += 1
            out.append("".join(buf))
            buf = []
            i = j
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def _strip_marks(s: str) -> tuple[str, list[str]]:
    """剥掉选择器开头的方言标记（``-``、``@css:``、``@xpath:``）。

    `-` 只是「这是个列表」的标记；`@css:` 表示后面整段就是原样 CSS；
    `@xpath:` 表示后面整段就是 XPath。三者都可能在 `@` 分段里独立成段
    （如 730 的 ``-@css:#mh-chapter-list-ol-0 li a``），所以要在切段**之前**
    先剥一遍，切段之后再逐段剥。
    """
    notes: list[str] = []
    while True:
        t = s.strip()
        if t.startswith("-") and not t.startswith("--"):
            t = t[1:].strip()
        m = re.match(r"^@?(css|xpath)\s*:\s*", t, re.I)
        if m:
            t = t[m.end():].strip()
        elif t.lower().startswith("css:") or t.lower().startswith("xpath:"):
            t = t.split(":", 1)[1].strip()
        if t == s.strip():
            return t, notes
        s = t


def _parse_single(
    cand: str, field: str
) -> tuple[list[tuple[str, int | None, bool]], tuple[str, Any] | None, list[str]]:
    """解析单个候选选择器，返回（片段列表, 取值方式, 说明列表）。

    XPath 候选用 ``("__xpath__" + 表达式, None, False)`` 这一个哨兵片段表示。
    """
    notes: list[str] = []
    s, _sn = _strip_marks(cand)
    if not s:
        raise UnsupportedSelector(f"{field} 的选择器为空")

    if s.startswith("$.") or s.startswith("$["):
        raise UnsupportedSelector(
            f"{field} 要从 JSON 接口取值（JSONPath {s[:24]}）。MangaKit 的章节/图片"
            "流程只解析 HTML 页面，不会去调页面里的 JSON 接口，无法转换"
        )
    if s.startswith("//") or s.startswith("~//"):
        xsteps, xval = _parse_xpath(s, field)
        return xsteps, xval, notes

    segs = _split_at(s)
    steps: list[tuple[str, int | None, bool]] = []
    value: tuple[str, Any] | None = None
    # 只有多段式（`节点@取值`）里的最后一段才可能是「取值」。
    # 单段式（如 `href`、`img`）要单独判断，否则 `a@href` 的 `a` 会被当成属性名。
    for pos, seg in enumerate(segs):
        seg = seg.strip()
        if not seg:
            continue
        last_seg = pos == len(segs) - 1
        low = seg.lower()
        # 段内的方言标记（`@css:...` 已由 _split_at 切开成独立段时是 `@css:`）
        seg, _n2 = _strip_marks(seg)
        if not seg:
            continue
        low = seg.lower()

        if low in ("text", "@text", "owntext", "alltext", "textnodes"):
            if low in ("owntext", "alltext", "textnodes"):
                notes.append(f"{field} 用了 {low} 取值，MangaKit 只能取节点文本，已按文本处理")
            value = ("text", None)
            continue
        if low in ("html", "@html", "innerhtml", "outerhtml", "htmls"):
            value = ("text", None)
            notes.append(f"{field} 取的是 HTML（{seg}），MangaKit 只能取文本，已降级")
            continue
        if low in ("css", "@css", "xpath", "@xpath"):
            continue
        if low in ("null", "-", ""):
            continue

        # `@css:` / `@xpath:` 后面整段是裸 CSS / XPath（603、730）
        raw_mark = re.match(r"^@?(css|xpath)\s*:\s*(.+)$", seg, re.I)
        if raw_mark:
            kind, body = raw_mark.group(1).lower(), raw_mark.group(2).strip()
            if kind == "xpath":
                if not body.startswith("//"):
                    body = "//" + body.lstrip("/")
                xsteps, xval = _parse_xpath(body, field)
                if xval:
                    value = xval
                return xsteps, value, notes
            name, idx, last = _split_index(body)
            if not _css_ok(name):
                raise UnsupportedSelector(f"{field} 的片段无法当 CSS 解析：{seg!r}")
            steps.append((name, idx, last))
            continue

        m = re.match(r"^(class|tag|id)\.(.*)$", seg, re.I)
        if m:
            kind = m.group(1).lower()
            rest = m.group(2).strip()
            if not rest:
                raise UnsupportedSelector(f"{field} 里出现了空片段：{seg!r}")
            name, idx, last = _split_index(rest)
            names = [x for x in re.split(r"\s+", name.strip()) if x]
            if not names:
                raise UnsupportedSelector(f"{field} 里出现了空片段：{seg!r}")
            if kind == "class":
                # `class.a b` 是「同一个元素上挂着多个类名」，拼成 `.a.b`。
                # 异次元里多类名写法远多于「.a 下的 b」，且后者另有一层 `@tag.b`。
                css = "".join("." + n for n in names)
            elif kind == "id":
                css = "#" + names[0]
            else:
                # `tag.div.2`：第一个名字是标签，其余是类名
                css = names[0] + "".join("." + n for n in names[1:])
            if not _css_ok(css):
                raise UnsupportedSelector(f"{field} 的片段无法当 CSS 解析：{seg!r}")
            steps.append((css, idx, last))
            continue

        if seg.startswith("color.") or seg.startswith("text."):
            raise UnsupportedSelector(
                f"{field} 用了 {seg.split('.')[0]}. 开头的方言写法（{seg[:24]}），"
                "MangaKit 的选择器没有这种「按颜色/按文本找节点」的能力"
            )

        # 裸标识符：要先分清「节点（标签）」还是「取值（属性）」。
        # 1) 任何带 CSS 特征的片段都是节点（`img.wp-post-image`、`a[itemprop=url]`）
        if _looks_like_css(seg):
            name, idx, last = _split_index(seg)
            if not _css_ok(name):
                raise UnsupportedSelector(f"{field} 的片段无法当 CSS 解析：{seg!r}")
            steps.append((name, idx, last))
            continue

        if re.match(r"^[A-Za-z_][\w:.-]*$", seg):
            bare = seg.lower()
            # 2) 已知属性名 / data-* 一定是取值
            if bare in _ATTR_WORDS or "data-" in bare:
                value = ("attr", seg)
                continue
            # 3) 裸 `tag` 是「任意标签」的方言标记
            if bare in ("tag", "*"):
                steps.append(("*", None, False))
                continue
            # 4) 其余裸标识符：是 HTML 标签名就当节点，否则（如 chapter-data-url）当属性
            if bare in _HTML_TAGS:
                steps.append((bare, None, False))
                continue
            value = ("attr", seg)
            continue

        raise UnsupportedSelector(f"{field} 里出现了无法识别的片段：{seg!r}")

    # `@css:` / `@xpath:` 出现在整条选择器开头时（如 `@css:.chapter-img@src`），
    # 直接按整段 CSS 处理。
    if not steps and value is None:
        raise UnsupportedSelector(f"{field} 的选择器为空")
    return steps, value, notes


def _parse_xpath(
    s: str, field: str
) -> tuple[list[tuple[str, int | None, bool]], tuple[str, Any] | None]:
    """把一条 XPath 拆成（哨兵片段列表, 取值方式）。

    ``//img[@class="x"]/@src`` → 节点 ``//img[@class="x"]`` + 取属性 ``src``；
    ``//h3/a/text()`` → 节点 ``//h3/a`` + 取文本。
    """
    core = s[1:] if s.startswith("~") else s
    attr = ""
    m = re.search(r"/@([A-Za-z_][\w:.-]*)\s*$", core)
    if m:
        attr = m.group(1)
        core = core[: m.start()]
    else:
        m = re.search(r"/text\(\)\s*$", core)
        if m:
            core = core[: m.start()]
    if not _xpath_ok(core):
        raise UnsupportedSelector(f"{field} 的 XPath 无法解析：{core[:60]}")
    value = ("attr", attr) if attr else ("text", None)
    return [(_XPATH_MARK + core, None, False)], value


def _xpath_ok(expr: str) -> bool:
    try:
        from lxml import etree

        etree.XPath(expr)
        return True
    except Exception:
        return False


def _cand_to_xpath(steps: list[tuple[str, int | None, bool]]) -> str | None:
    """把一条候选的片段串成单条 XPath（失败返回 None）。"""
    pieces: list[str] = []
    for css, idx, last in steps:
        if css.startswith(_XPATH_MARK):
            expr = css[len(_XPATH_MARK):]
        else:
            try:
                expr = _xpath_step(css)
            except Exception:  # noqa: BLE001
                return None
        if idx is not None:
            expr = f"({expr})[last()-{idx}]" if last else f"({expr})[{idx + 1}]"
        pieces.append(expr)
    return "/".join(pieces) if pieces else None


def _all_to_xpath(
    parsed: list[tuple[list[tuple[str, int | None, bool]], tuple[str, Any] | None]]
) -> list[str] | None:
    """把全部候选统一翻成 XPath（cssselect 没有 XPath→CSS 的反查）。"""
    out: list[str] = []
    for steps, _val in parsed:
        expr = _cand_to_xpath(steps)
        if not expr:
            return None
        out.append(expr)
    return out or None


def convert_selector(
    expr: Any,
    *,
    field: str = "选择器",
    default_value: tuple[str, Any] | None = None,
    attr_url: bool = False,
) -> dict:
    """把一个异次元选择器字符串转成 MangaKit 的 Selector spec。

    ``attr_url`` 表示取值是不是 URL：取 title/alt 这类非 URL 属性时必须写
    ``"url": false``，否则 MangaKit 会用 base_url 把它拼成假地址。

    表达不了就抛 `UnsupportedSelector`，`str(e)` 是中文原因。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise UnsupportedSelector(f"{field} 为空")
    raw = expr.strip()
    if _ENC_RE.match(raw):
        raise UnsupportedSelector(f"{field} 是阅读器 App 内部加密块（{raw[:12]}…），客户端无法解密")
    m = _SCRIPT_RE.search(raw)
    if m:
        raise UnsupportedSelector(
            f"{field} 用了脚本规则（{m.group(0)}），需要 App 内置的 Java/Rhino 运行时"
            "（org.jsoup / java.put 之类），MangaKit 无法执行"
        )

    core, headers, hnote = _strip_header(raw)
    # `-` 与 `@css:` / `@xpath:` 标记必须在切替换**之前**剥掉，
    # 否则 `-@css:#mh-chapter-list-ol-0 li a` 里的 `#` 会被当成替换分隔符。
    core, _mk = _strip_marks(core)
    core, replace_spec, rnote, need_regex = _split_replace(core)
    if need_regex:
        raise UnsupportedSelector(
            f"{field} 的后缀替换是正则写法，MangaKit 的选择器替换只支持字面量替换，无法可靠映射"
        )
    notes = [n for n in (hnote, rnote) if n]
    if headers:
        notes.append("源指定了请求头：" + ",".join(sorted(headers)))

    cands = _split_cands(core)
    if not cands:
        raise UnsupportedSelector(f"{field} 的选择器为空")

    parsed: list[tuple[list[tuple[str, int | None, bool]], tuple[str, Any] | None]] = []
    for c in cands:
        steps, val, ns = _parse_single(c, field)
        notes += ns
        parsed.append((steps, val))

    # 候选合并：节点部分用 CSS 逗号并集，属性部分用 MangaKit 的 attr 列表
    # （引擎会逐个试、取第一个非空的）。异次元里 `||`/`&&`/`|` 的原始语义是
    # 「取第一个命中」，这里只能近似成并集，说明里会讲清楚。
    with_nodes = [p for p in parsed if p[0]]
    if with_nodes and len(with_nodes) != len(parsed):
        notes.append(
            f"{field} 的多候选里有的没有节点选择器，已按「取子节点」的那一支处理"
        )
    if with_nodes:
        parsed = with_nodes

    specs: list[dict] = [ _steps_to_selector(steps, val or default_value) for steps, val in parsed ]

    if len(specs) == 1:
        out = specs[0]
    else:
        css_all: list[str] = []
        attr_all: list[str] = []
        texts: list[str] = []
        xp_all: list[str] = []
        for sp in specs:
            if sp.get("xpath"):
                xp_all.append(str(sp["xpath"]))
            if sp.get("css"):
                css_all += [x.strip() for x in str(sp["css"]).split(",") if x.strip()]
            if sp.get("attr"):
                a = sp["attr"]
                attr_all += list(a) if isinstance(a, (list, tuple)) else [a]
            elif sp.get("text"):
                texts.append("text")
        if not attr_url and attr_all and texts:
            # 非 URL 字段：一个候选取属性、另一个取文本，只能服从唯一取值方式
            attr_all = []
        if attr_url and attr_all and texts:
            texts = []
            notes.append(f"{field} 的多候选里既有属性又有文本，已按 URL 属性取值")
        out = {}
        if xp_all:
            if len(xp_all) != len(specs):
                # 有一支必须走 XPath（带下标的无标签选择器），另一支本来能当 CSS。
                # cssselect 没有「XPath → CSS」的反查，所以只能把**所有**候选
                # 都翻成 XPath，再用 `|` 拼成一条并集（22 号源就是这个情况）。
                xp_all = _all_to_xpath(parsed)
                if xp_all is None:
                    raise UnsupportedSelector(
                        f"{field} 把 XPath 候选和 CSS 候选混在一起，且无法统一成 XPath，无法合并"
                    )
                notes.append(
                    f"{field} 的多候选里有一支必须用 XPath 表达，已把所有候选统一成 XPath 并集"
                )
            out["xpath"] = " | ".join(xp_all)
        elif css_all:
            seen: list[str] = []
            for c in css_all:
                if c not in seen:
                    seen.append(c)
            out["css"] = ", ".join(seen)
        if attr_all:
            uniq: list[Any] = []
            for a in attr_all:
                if a not in uniq:
                    uniq.append(a)
            out["attr"] = uniq[0] if len(uniq) == 1 else uniq
            if not attr_url:
                out["url"] = False
        elif texts:
            out["text"] = True
        notes.append(
            f"{field} 的多候选已合并为 CSS 并集 + 属性逐个试"
            "（原语义是「取第一个命中的候选」，这里是近似）"
        )

    if replace_spec:
        if isinstance(replace_spec, dict) and "__re__" in replace_spec:
            # 引擎 Selector 的 re+group 语义正是「取完值再用正则挖第 N 组」，
            # 和异次元的 `#正则#$1` 完全一致，可以直接落成 re/group。
            out["re"] = replace_spec["__re__"]
            out["group"] = replace_spec["group"]
        else:
            out["replace"] = replace_spec
    if out.get("attr") and not attr_url:
        out["url"] = False
    if headers.get("Referer"):
        notes.append("Referer=" + headers["Referer"])
    if notes:
        out["_notes"] = notes  # 只给 convert() 收集说明用，落规则前会删掉
    return out


def _strip_notes(spec: dict) -> tuple[dict, list[str]]:
    out = {k: v for k, v in spec.items() if k != "_notes"}
    return out, list(spec.get("_notes") or [])


# --------------------------------------------------------------------------
# 主体：convert
# --------------------------------------------------------------------------


def _field_of(key: str) -> str:
    return _CANON.get(key.lower().replace("_", ""), "")


def _pick(raw: dict[str, Any], key: str) -> str:
    """按字段名取值（大小写不敏感）。"""
    want = key.lower()
    for k, v in raw.items():
        if str(k).lower() == want:
            return v if isinstance(v, str) else ("" if v is None else str(v))
    return ""


def _registrable(host: str) -> str:
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _MULTI_TLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _convert_search_url(raw_url: str) -> tuple[str | None, list[str]]:
    """把 ruleSearchUrl 转成 MangaKit 的 ``{q}`` 模板；转不了返回 (None, 说明)。"""
    notes: list[str] = []
    u = (raw_url or "").strip()
    if not u:
        return None, notes
    u, _headers, hnote = _strip_header(u)
    if hnote:
        notes.append(hnote)
    u = u.strip()
    if "{{" in u:
        return None, ["搜索地址用的是 legado 的 {{key}}/{{page}} 占位符，MangaKit 只认 {q}"]
    # `路径@key=value&…` 这种把 host/query 分开写的写法
    m = re.match(r"^(?P<base>[^@\s]+)@(?P<q>[A-Za-z_][\w-]*=.*)$", u)
    if m:
        u = m.group("base") + "?" + m.group("q")
    # `|char=gbk` / `|big5` 之类的编码后缀：URL 本身还能用，但 MangaKit
    # 不做 gbk 编码，只能保留 URL 并提示。
    m = re.search(r"\|(char=\w+|\w+)\s*$", u, re.I)
    if m:
        notes.append(f"原源要求按 {m.group(1)} 编码搜索关键词，MangaKit 未实现，中文关键词可能搜不到")
        u = u[: m.start()]
    if "#" in u:
        return None, ["搜索地址里带 # 占位（原源把搜索关掉了），不生成搜索入口"]
    if "searchPage" in u:
        # 页码写在路径里（`/page/searchPage/`）就没法剥，只能放弃；
        # 只写在 query 里（`&page=searchPage`）可以直接去掉那个参数，
        # 代价是只能搜第一页——对 MangaKit 来说这已经够用了。
        base, _sep, query = u.partition("?")
        if "searchPage" in base:
            return None, [
                "搜索地址把页码写在路径里（含 searchPage），MangaKit 的搜索 URL 不支持页码"
            ]
        kept = [p for p in query.split("&") if p and "searchPage" not in p]
        u = base + ("?" + "&".join(kept) if kept else "")
        notes.append(
            "原源搜索地址带页码占位符 searchPage，MangaKit 的搜索不支持翻页，已去掉页码参数（只搜第一页）"
        )
    if "searchKey" not in u:
        return None, ["搜索地址里没有 searchKey 占位符，无法映射成 MangaKit 的 {q}"]
    u = u.replace("searchKey", "{q}")
    if re.search(r"\{[^}]*\}", u.replace("{q}", "")):
        return None, ["搜索地址里还有 MangaKit 不认识的占位符"]
    return u, notes


def _compose_list_with_child(list_css: str, child_css: str) -> str:
    """把「列表项里再找子元素」的语义展开成 CSS（逗号并集要逐支追加）。"""
    parts = [p.strip() for p in re.split(r",", list_css) if p.strip()]
    return ", ".join(f"{p} {child_css}".strip() for p in parts)


def _chapters_from(raw: dict[str, Any], notes: list[str]) -> tuple[dict | None, str]:
    """构造 chapters 段。返回（段, 失败原因），失败原因非空表示用不了。"""
    list_expr = _pick(raw, "ruleChapterList")
    if not list_expr.strip():
        return None, "缺少 ruleChapterList（章节列表选择器），转出来就是空目录"
    if _ENC_RE.match(list_expr.strip()):
        tag = _ENC_RE.match(list_expr.strip()).group(1)
        return None, f"ruleChapterList 是阅读器 App 内部加密块（:_{tag}_…），客户端无法解密"
    try:
        spec = convert_selector(list_expr, field="ruleChapterList")
    except UnsupportedSelector as e:
        return None, str(e)
    spec, ns = _strip_notes(spec)
    notes += ns
    if "css" not in spec and "xpath" not in spec:
        return None, "ruleChapterList 没解析出节点选择器"
    # 引擎读的是 chapters["list"]，这里必须包一层，否则 RuleAdapter.parse_chapters
    # 会抛 ParseError("规则缺少 chapters.list")，整个源就废了。
    spec = {"list": spec}

    # 章节链接：MangaKit 的 parse_chapters 固定读节点的 href 属性、标题读节点文本，
    # 所以能表达只有两种情况——列表项本身就是 <a href>，或者链接在子元素 a 上
    # （这时把子选择器并进 CSS）。
    cu = _pick(raw, "ruleContentUrl").strip()
    if cu and _SCRIPT_RE.search(cu):
        return None, (
            "ruleContentUrl 是脚本规则（@js:/@css:），章节地址由 App 现场算出来，"
            "MangaKit 无法执行"
        )
    if cu and cu != "-":
        try:
            cspec = convert_selector(cu, field="ruleContentUrl", attr_url=True,
                                     default_value=("attr", "href"))
            cspec, cns = _strip_notes(cspec)
            notes += cns
        except UnsupportedSelector as e:
            return None, f"ruleContentUrl 无法映射：{e}"
        attr = cspec.get("attr")
        attrs = list(attr) if isinstance(attr, (list, tuple)) else ([attr] if attr else [])
        child = str(cspec.get("css") or "").strip()
        if attrs and all(a == "href" for a in attrs):
            if child and "css" in spec["list"]:
                spec["list"]["css"] = _compose_list_with_child(
                    str(spec["list"]["css"]), child
                )
                notes.append("章节链接取自列表项内的子元素，已并进 chapters.list 的 CSS")
            elif child and "xpath" in spec["list"]:
                return None, "章节列表用 XPath、链接在子元素上，两段无法拼成一条规则"
        elif attrs:
            notes.append(
                f"章节链接取自 {attrs[0]!r} 属性，MangaKit 的章节解析只读 href，"
                "已改读 href（能不能用由真实页面校验决定）"
            )
    else:
        notes.append("源没写 ruleContentUrl，按「列表项自身就是链接」处理（引擎会读 href）")

    name_expr = _pick(raw, "ruleChapterName").strip()
    if name_expr:
        if _SCRIPT_RE.search(name_expr):
            notes.append("已忽略 ruleChapterName（脚本规则），章节名改用链接文字，可能有前后缀")
        else:
            try:
                convert_selector(name_expr, field="ruleChapterName")
            except UnsupportedSelector as e:
                notes.append(f"已忽略 ruleChapterName：{e}")

    nxt = _pick(raw, "ruleChapterUrlNext").strip()
    if nxt:
        notes.append(
            "源用 ruleChapterUrlNext 翻目录页，MangaKit 的 chapters.next 只能跟链接、"
            "不能算页码，已忽略：只解析第一页目录"
        )
    return spec, ""


def _pages_from(raw: dict[str, Any], notes: list[str]) -> tuple[dict | None, str]:
    """构造 pages 段。返回（段, 失败原因）。"""
    bc = _pick(raw, "ruleBookContent")
    if not bc.strip():
        return None, "缺少 ruleBookContent（正文图片选择器），转出来一章图都没有"
    if _ENC_RE.match(bc.strip()):
        tag = _ENC_RE.match(bc.strip()).group(1)
        return None, f"ruleBookContent 是阅读器 App 内部加密块（:_{tag}_…），客户端无法解密"
    try:
        spec = convert_selector(bc, field="ruleBookContent")
    except UnsupportedSelector as e:
        return None, str(e)
    spec, ns = _strip_notes(spec)
    notes += ns
    if "css" not in spec and "xpath" not in spec:
        return None, "ruleBookContent 没解析出节点选择器"
    if spec.get("text"):
        return None, "ruleBookContent 取的是文本而不是图片地址属性，无法当图片列表用"

    out: dict[str, Any] = {"list": spec}
    # pages.list 里不需要 url 语义（原选择器是给 HTML 取值用的），清掉免得误导
    spec.pop("url", None)
    attr = spec.pop("attr", None)
    if isinstance(attr, (list, tuple)):
        # 引擎的 parse_pages 把 url.attr 直接交给 _get_attr(node, attr)，
        # 而 _get_attr 没有列表分支（传 list 会 TypeError），所以只能取第一个。
        # 懒加载站点的 src 常是占位图，多候选时优先取第一个（一般就是 data-src）。
        chain = [str(a) for a in attr if a]
        if len(chain) > 1:
            notes.append(
                f"图片属性有多个候选（{'、'.join(chain)}），MangaKit 的 pages.url 只能填一个，"
                f"已取 {chain[0]!r}；若该站实际用另一个属性，取图会为空"
            )
        attr = chain[0] if chain else None
    if attr:
        out["url"] = {"attr": attr}
    else:
        notes.append(
            "ruleBookContent 没写属性，交给引擎按 src/data-src/data-original 顺序猜"
        )
    if spec.get("replace"):
        out["replace"] = [list(p) for p in spec.pop("replace")]
        notes.append("图片地址的字符串替换已挪到 pages.replace（引擎在那里按正则处理）")

    dec = _pick(raw, "ruleBookContentDecoder").strip()
    if dec:
        notes.append(
            "源写了 ruleBookContentDecoder（图片地址在 App 里还要再解一次码），"
            "MangaKit 没有这一步，可能取不到图"
        )
    if _pick(raw, "ruleContentUrlNext").strip():
        notes.append("源支持「下一话」翻页，MangaKit 只按当前章节页取图，已忽略")
    return out, ""


def _comic_from(raw: dict[str, Any], notes: list[str]) -> dict:
    """构造 comic 段（全部是可选字段，转不了就丢并记说明）。"""
    comic: dict[str, Any] = {}
    text_fields = [
        ("ruleBookName", "title"),
        ("ruleBookAuthor", "author"),
        ("ruleBookKind", "tags"),
        ("ruleIntroduce", "description"),
        ("ruleBookLastChapter", "last_chapter"),
    ]
    for expr_key, out_key in text_fields:
        expr = _pick(raw, expr_key).strip()
        if not expr:
            continue
        try:
            spec = convert_selector(expr, field=expr_key)
        except UnsupportedSelector as e:
            notes.append(f"已忽略 {expr_key}：{e}")
            continue
        spec, ns = _strip_notes(spec)
        notes += ns
        comic[out_key] = spec

    cover = _pick(raw, "ruleCoverUrl").strip()
    if cover:
        try:
            spec = convert_selector(cover, field="ruleCoverUrl", attr_url=True,
                                    default_value=("attr", "src"))
        except UnsupportedSelector as e:
            notes.append(f"已忽略 ruleCoverUrl：{e}")
        else:
            spec, ns = _strip_notes(spec)
            notes += ns
            comic["cover"] = spec
    return comic


def _search_from(raw: dict[str, Any], tpl: str | None, notes: list[str]) -> dict | None:
    """构造 search 段；有任何一项转不了就整块不写（宁可没搜索，也不要搜出一堆脏结果）。"""
    if not tpl:
        return None
    need = [
        ("ruleSearchList", "list", False),
        ("ruleSearchName", "title", False),
        ("ruleSearchNoteUrl", "link", True),
        ("ruleSearchCoverUrl", "cover", True),
    ]
    out: dict[str, Any] = {"url": tpl}
    for expr_key, out_key, is_url in need:
        expr = _pick(raw, expr_key).strip()
        if out_key == "list":
            if not expr:
                notes.append(f"已关闭搜索：源里没有 {expr_key}")
                return None
            try:
                spec = convert_selector(expr, field=expr_key)
            except UnsupportedSelector as e:
                notes.append(f"已关闭搜索：{expr_key} 无法映射（{e}）")
                return None
            spec, ns = _strip_notes(spec)
            notes += ns
            out["list"] = spec
            continue
        if not expr:
            if out_key == "title":
                continue
            if out_key == "link":
                notes.append("已关闭搜索：源里没有 ruleSearchNoteUrl，拿不到结果链接")
                return None
            continue
        default = ("attr", "src") if out_key == "cover" else (
            ("attr", "href") if out_key == "link" else ("text", None)
        )
        try:
            spec = convert_selector(expr, field=expr_key, attr_url=is_url, default_value=default)
        except UnsupportedSelector as e:
            # 只有 list / link 转不了才算致命（引擎没有对应默认值）；
            # title / cover 引擎有内置兜底，丢一个字段比关掉整个搜索更划算。
            if out_key in ("title", "cover"):
                notes.append(f"搜索的 {expr_key} 无法映射（{e}），已丢弃该字段，改用引擎兜底")
                continue
            what = "搜索列表" if out_key == "list" else expr_key
            notes.append(f"已关闭搜索：{what} 无法映射（{e}）")
            return None
        spec, ns = _strip_notes(spec)
        notes += ns
        out[out_key] = spec
    if "link" not in out:
        notes.append("已关闭搜索：没有可用的结果链接选择器")
        return None
    return out


def _find_entries(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """解析 ruleFindUrl（``名称::URL``，分隔符换行或 ``&&`` 都见过）。"""
    txt = _pick(raw, "ruleFindUrl")
    if not txt.strip():
        return []
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\n+|&&", txt):
        chunk = chunk.strip()
        if not chunk or "::" not in chunk:
            continue
        name, url = chunk.split("::", 1)
        name, url = name.strip(), url.strip()
        if url:
            out.append((name, url))
    return out


def convert(raw: dict, *, site_id: str | None = None) -> tuple[dict | None, str]:
    """异次元源对象 → MangaKit 规则 dict。纯函数，不联网。

    返回 ``(规则, 中文说明)``：规则为 None 时说明以「加密源：」「语法不支持：」
    或「无法转换：」开头；成功时以「可转换：」开头，说明里列出所有降级项。
    """
    if not isinstance(raw, dict) or not raw:
        return None, "无法转换：源对象不是有效的 JSON 对象"

    # 1) 加密块：关键字面就藏成 :_R002_ / :_H006_，客户端没有解密能力
    enc = [
        (k, _ENC_RE.match(v).group(1))
        for k, v in raw.items()
        if isinstance(v, str) and _ENC_RE.match(v.strip())
    ]
    if enc:
        crit = [(k, t) for k, t in enc if _field_of(str(k)) in _CRITICAL]
        if crit:
            detail = "、".join(f"{k}(:_ {t}_)" for k, t in crit)
            return None, (
                "加密源：关键字段整段是不透明加密块（" + detail + "），"
                "内容是异次元阅读器 App 内部解密的，MangaKit 客户端没有对应解密逻辑，不支持。"
                "这类源只能等作者放出明文规则。"
            )

    name = _clean_text(_pick(raw, "bookSourceName")) or "异次元源"
    base = _pick(raw, "bookSourceUrl").strip()
    if not base:
        return None, "无法转换：源里没有 bookSourceUrl（站点地址），不知道要抓哪个站"
    if not base.lower().startswith(("http://", "https://")):
        base = "https://" + base.lstrip("/")
    host = (urlparse(base).hostname or "").lower()
    if not host:
        return None, f"无法转换：bookSourceUrl 不是合法网址（{base}）"

    notes: list[str] = []
    disabled = str(raw.get("enable", "")).strip().lower() in ("false", "0", "否")
    if disabled:
        notes.append("注意：该源在原站标着「已禁用」，但规则文本是完整的，仍然尝试转换")

    # 2) 关键段
    chapters, ch_err = _chapters_from(raw, notes)
    if chapters is None:
        return None, f"语法不支持：{ch_err}"
    pages, pg_err = _pages_from(raw, notes)
    if pages is None:
        return None, f"语法不支持：{pg_err}"

    # 3) 可选段
    comic = _comic_from(raw, notes)
    search_tpl, search_notes = _convert_search_url(_pick(raw, "ruleSearchUrl"))
    notes += search_notes
    search = _search_from(raw, search_tpl, notes)
    if search_tpl and not search:
        search_tpl = None

    sid = raw.get("_yck_id")
    try:
        sid_int = int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        sid_int = 0
    if site_id:
        rid = str(site_id)
    elif sid_int:
        rid = f"yck{sid_int}"
    else:
        rid = "yck_" + re.sub(r"[^\w]+", "_", host).strip("_")

    domains: list[str] = []
    for d in (_registrable(host), host):
        if d and d not in domains:
            domains.append(d)

    headers: dict[str, str] = {}
    ua = _pick(raw, "httpUserAgent").strip()
    if ua and ua.lower() not in ("", "default"):
        headers["User-Agent"] = ua
    for expr_key in ("ruleBookContent", "ruleCoverUrl", "ruleSearchUrl", "ruleIntroduce"):
        expr = _pick(raw, expr_key)
        if "@header" in expr.lower() and "referer" in expr.lower():
            _core, hdrs, _n = _strip_header(expr)
            ref = hdrs.get("Referer", "")
            if ref:
                low = ref.lower()
                if low in ("host", "thispage", "baseurl"):
                    ref = base.rstrip("/") + "/"
                elif not ref.startswith("http"):
                    ref = urljoin(base, ref)
                headers["Referer"] = ref
            break
    if _pick(raw, "bookCookieAuto").strip() in ("是", "true", "True"):
        notes.append("原源开了「自动获取 Cookie」，MangaKit 没有这一步，需要登录的章节会拿不到图")
    if _pick(raw, "ruleBookContentDecoder").strip():
        pass  # 已在 _pages_from 里记过

    rule: dict[str, Any] = {
        "id": rid,
        "name": name,
        "lang": "cn",
        "domains": domains,
    }
    if headers:
        rule["headers"] = headers
    if comic:
        rule["comic"] = comic
    rule["chapters"] = chapters
    rule["pages"] = pages
    if search_tpl:
        rule["search_url"] = search_tpl
        rule["search"] = {k: v for k, v in (search or {}).items() if k != "url"}
        rule["search"]["url"] = search_tpl

    note_lines = [
        f"由异次元源 #{sid_int or '?'} 转换（yck2026.fun）",
        f"原始标题：{_clean_text(_pick(raw, 'bookSourceName'))}｜站点：{base}",
    ]
    find = _find_entries(raw)
    if find:
        note_lines.append(
            "原源的「发现」入口（MangaKit 暂不支持发现页，仅存档）："
            + "；".join(f"{n}→{u}" for n, u in find[:6])
        )
    if notes:
        note_lines.append("转换说明：")
        note_lines += [f"- {n}" for n in notes]
    rule["note"] = "\n".join(note_lines)

    summary = [
        f"站点 {name}（{host}）",
        f"章节列表 {'已转' if 'list' in chapters else '缺'}",
        f"图片列表 {'已转' if 'list' in pages else '缺'}",
        f"搜索 {'可用' if search_tpl else '未开启'}",
    ]
    head = f"可转换：{('、'.join(summary))}。"
    if notes:
        head += f"有 {len(notes)} 处降级/忽略（见规则的 note 字段）：" + "；".join(notes[:4])
        if len(notes) > 4:
            head += " 等"
    return rule, head


# --------------------------------------------------------------------------
# 联网校验：抓 JSON → convert → 真跑引擎
# --------------------------------------------------------------------------


_DETAIL_BAD = re.compile(
    r"/(?:chapter|read|search|login|logout|user|uc|tag|tags|genre|list|sort|page|"
    r"author|rank|ranking|top|booklist|update|category|classify|about|contact|"
    r"hanman|riman|tongrenzhi|completed|latest|history|novel\d*/\d)\b",
    re.I,
)


def _seed_pages(raw: dict, base: str) -> list[str]:
    """把 ruleFindUrl 的「发现」入口变成可抓的分类页地址。"""
    out: list[str] = []
    for _name, u in _find_entries(raw):
        u = re.sub(r"\{\{.*?\}\}", "", u)  # legado 的 {{...}} 占位整段丢掉
        u = re.sub(r"searchPage\s*[-+]\s*\d+", "1", u)
        u = u.replace("searchPage", "1")
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin(base, u)
        if u not in out:
            out.append(u)
    return out


def _score_link(path: str) -> int:
    """给同域链接打分：越像「漫画详情页」分越高。"""
    score = 0
    if re.search(r"\d{2,}", path):
        score += 3  # 详情页一般带 id / 数字
    depth = path.strip("/").count("/")
    score += min(depth, 3)
    if path.endswith(".html") or path.endswith("/"):
        score += 1
    if path.strip("/").count("/") == 0:
        score -= 4  # 一级栏目页，基本不是详情
    return score


async def _candidate_detail_urls(
    prober: Any, rule: dict, raw: dict, limit: int = 8
) -> tuple[list[str], list[str]]:
    """给这个源找几个「看起来是漫画详情页」的地址，用来跑规则自检。

    返回（候选地址, 抓取失败说明）。两条路子并用：抓「发现」入口页里的链接 +
    用转换出来的 search 段真搜一次。一条都找不到就没法验收 —— 宁可报
    「找不到详情页」也不假装通过，同时把「抓页面就失败了」和「抓到了但没链接」
    分开报，用户才知道到底是站点关了还是规则挑不出详情页。
    """
    base = _pick(raw, "bookSourceUrl").strip()
    if not base.startswith("http"):
        base = "https://" + (rule.get("domains") or ["example.com"])[0]
    base = base.rstrip("/") + "/"
    pattern = _pick(raw, "ruleBookUrlPattern").strip()
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None

    host = (urlparse(base).hostname or "").lower()
    reg = _registrable(host)
    out: list[str] = []
    seen: set[str] = set()

    def usable(href: str) -> bool:
        if not href.startswith("http") or href in seen:
            return False
        h = (urlparse(href).hostname or "").lower()
        if not (h == host or h.endswith("." + reg) or h == reg):
            return False
        if re.search(r"\.(?:jpg|jpeg|png|webp|gif|css|js|ico|xml|zip|json)$", href, re.I):
            return False
        if rx is not None and not rx.search(href):
            return False
        path = urlparse(href).path
        if path in ("", "/") or _DETAIL_BAD.search(path):
            return False
        if href.rstrip("/") == base.rstrip("/"):
            return False
        return True

    seeds = _seed_pages(raw, base)[:3]
    if base not in seeds:
        seeds.append(base)

    scored: list[tuple[int, str]] = []
    fetch_errors: list[str] = []
    for seed in seeds:
        try:
            page_html = await prober._fetch(seed)
        except Exception as e:  # noqa: BLE001
            try:
                from .probe import _err as _probe_err

                fetch_errors.append(f"{seed} → {_probe_err(e)}")
            except Exception:  # noqa: BLE001
                fetch_errors.append(f"{seed} → {type(e).__name__}")
            continue
        if not isinstance(page_html, str):
            page_html = str(page_html)
        try:
            doc = prober._doc(page_html)
        except Exception:  # noqa: BLE001
            continue
        for a in doc.xpath("//a[@href]"):
            href = urljoin(seed, (a.get("href") or "").strip())
            if not usable(href):
                continue
            seen.add(href)
            scored.append((_score_link(urlparse(href).path), href))

    # 再用转换出来的搜索规则真搜一次 —— 搜索结果的链接一定是漫画详情页
    if rule.get("search_url") or (rule.get("search") or {}).get("url"):
        try:
            from .sites import RuleAdapter, Site

            site = Site(
                id=str(rule.get("id") or "probe"), name=str(rule.get("name") or ""),
                domains=list(rule.get("domains") or []), rule="<user>", adapter=None,
                enabled=True, lang="cn", note="", headers={}, cookies={},
                search_url=str(rule.get("search_url") or ""), payload=rule, hidden=False,
            )
            ad = RuleAdapter(site, prober._http, rule)
            for kw in ("漫画", "a", "1"):
                try:
                    found = await ad.search(kw)
                except Exception:  # noqa: BLE001
                    continue
                for c in found:
                    href = str(getattr(c, "url", "") or "")
                    if usable(href):
                        seen.add(href)
                        scored.append((9, href))
                if scored and any(s >= 9 for s, _ in scored):
                    break
        except Exception:  # noqa: BLE001
            pass

    scored.sort(key=lambda kv: kv[0], reverse=True)
    for _s, href in scored:
        if href not in out:
            out.append(href)
        if len(out) >= limit:
            break
    return out, fetch_errors


async def _run_check(rule: dict, raw: dict, sid: int, deep: bool) -> tuple[str, Any]:
    """真跑一遍：找详情页 → SiteProber._verify_rule → 深度校验图片能不能下。"""
    from .http_client import HttpClient
    from .probe import ProbeResult, SiteProber, _err

    res = ProbeResult(
        site_name=str(rule.get("name") or ""),
        host=(rule.get("domains") or [""])[0],
        url="",
        search_url=str(rule.get("search_url") or ""),
        can_search=bool(rule.get("search_url")),
        rule=rule,
    )
    async with HttpClient() as http:
        prober = SiteProber(http)
        fetch_errors: list[str] = []
        try:
            urls, fetch_errors = await _candidate_detail_urls(prober, rule, raw)
        except Exception as e:  # noqa: BLE001
            urls = []
            res.log.append(f"找详情页失败：{_err(e)}")
        if not urls:
            res.status = "fail"
            base = (raw.get("bookSourceUrl") or "").strip() or (
                (rule.get("domains") or [""])[0]
            )
            if fetch_errors:
                # 一个页都抓不到 —— 说明站点本身连不上，不是规则的问题。
                res.reason = (
                    f"站点打不开，无法校验规则：{fetch_errors[0]}"
                    f"（{base}；可能是站点已关停、被墙，或需要代理）"
                )
                for line in fetch_errors[:3]:
                    res.log.append(f"抓取失败 {line}")
            else:
                res.reason = (
                    "站点能打开，但页面上找不到任何漫画详情页链接，无法校验规则。"
                    "（源里的「发现」入口可能失效，或站点改版了）"
                )
            return res.reason, res

        verified = False
        why = ""
        ok_url = ""
        for u in urls:
            res.tried_urls.append(u)
            try:
                ok, why = await prober._verify_rule(rule, u, 0, 0)
            except Exception as e:  # noqa: BLE001
                ok, why = False, f"规则自检异常：{_err(e)}"
            res.log.append(f"规则自检 {u} → {why}")
            if ok:
                verified, ok_url = True, u
                break

        if not verified:
            res.status = "fail"
            res.reason = (
                f"转换成功，但规则在真实页面上跑不通（试了 {len(res.tried_urls)} 个详情页）：{why}。"
                "为避免加进来是个死站，不予添加。"
            )
            return res.reason, res

        # 校验通过：再用同一套引擎拿到话数/图数，并（deep）真下一张图。
        try:
            from .sites import RuleAdapter, Site

            site = Site(
                id=str(rule.get("id") or f"yck{sid}"), name=str(rule.get("name") or ""),
                domains=list(rule.get("domains") or []), rule="<user>", adapter=None,
                enabled=True, lang="cn", note="", headers={}, cookies={},
                search_url=str(rule.get("search_url") or ""), payload=rule, hidden=False,
            )
            ad = RuleAdapter(site, http, rule)
            res.url = ok_url
            try:
                comic = await ad.parse_comic(ok_url)
                res.title, res.cover = comic.title, comic.cover
            except Exception as e:  # noqa: BLE001
                res.log.append(f"详情页字段解析失败（不影响可用性）：{_err(e)}")
            chs = await ad.parse_chapters(ok_url, full=False)
            res.chapter_count = len(chs)
            imgs: list[str] = []
            for ch in chs[:3]:
                try:
                    imgs = await ad.parse_pages(ch.url)
                except Exception as e:  # noqa: BLE001
                    res.log.append(f"章节页解析失败 {ch.url}：{_err(e)}")
                    continue
                if imgs:
                    break
            res.image_count = len(imgs)
        except Exception as e:  # noqa: BLE001
            res.status = "fail"
            res.reason = f"规则自检通过，但二次解析出错：{_err(e)}"
            return res.reason, res

        if deep and imgs:
            ok_imgs: list[str] = []
            tried: list[str] = []
            for u in imgs[:2]:
                tried.append(u)
                try:
                    resp = await http.request(
                        "GET", u, kind="image", headers={"Referer": ok_url}
                    )
                    if resp.status_code < 400 and len(resp.content) >= 1024:
                        ok_imgs.append(u)
                except Exception as e:  # noqa: BLE001
                    res.log.append(f"图片下载失败 {_err(e)}")
            res.ok_urls, res.tried_urls = ok_imgs, tried
            res.log.append(f"实测 {len(ok_imgs)}/{len(tried)} 张图片可下载")
            if not ok_imgs:
                res.status = "partial"
                res.reason = (
                    f"能解析出 {res.chapter_count} 话、{res.image_count} 张图，但图片实际下不动"
                    "（403/防盗链或需要登录 Cookie），加进来也是「能搜不能下」，不予添加。"
                )
                return res.reason, res

        res.status = "ok"
        res.reason = (
            f"校验通过：{res.title or rule.get('name') or ''} —— "
            f"{res.chapter_count} 话、首话 {res.image_count} 张图"
            + (f"，实测可下载 {len(res.ok_urls)} 张" if res.ok_urls else "")
            + f"（详情页 {ok_url}）"
        )
        return res.reason, res


def _run_sync(coro_factory, timeout: float):
    """同步跑一个协程，即使当前已经有事件循环也不会炸（丢到临时线程里跑）。"""
    def runner():
        return asyncio.run(asyncio.wait_for(coro_factory(), timeout=timeout))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return runner()
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(runner).result(timeout=timeout + 15)


def check_source(
    sid: int, *, deep: bool = True, timeout: float = 60.0
) -> "CheckResult":
    """抓源 JSON → convert → 用 MangaKit 引擎真跑一遍。

    返回 `CheckResult`：

    - ``ok=True``（``status == "ok"``）：规则真的能用，可以直接入库。
    - ``status == "partial"``：页面能解析但图片下不动，不建议添加。
    - ``status == "fail"``：转不了或跑不通，`message` 里是中文原因。

    为了兼容 ``(规则, 说明, ProbeResult)`` 的旧写法，`CheckResult` 可以直接
    解包三元组。

    这里是本模块唯一的联网入口，**任何异常都被吞进 `message`，不会抛给调用方**
    （GUI 的线程池不该因为网络抖动崩）。解析校验直接复用 `probe.SiteProber`
    的那套调用（`_verify_rule` → `RuleAdapter.parse_chapters/parse_pages`），
    不另写一套，避免「探测能过、装进引擎跑不通」的偏差。
    """
    out = CheckResult()

    def fail(msg: str, rule: dict | None = None) -> "CheckResult":
        out.ok, out.status, out.message, out.rule = False, "fail", msg, rule
        return out

    try:
        raw = fetch_source_json(sid)
    except Exception as e:  # noqa: BLE001
        from .probe import _err

        return fail(f"抓取失败：{_err(e)}")
    if not raw:
        return fail(f"抓取失败：yck2026 没有返回 #{sid} 的图源 JSON（可能已下架）")

    try:
        rule, why = convert(raw, site_id=f"yck{int(sid)}")
    except Exception as e:  # noqa: BLE001
        return fail(f"转换出错：{type(e).__name__}: {e}")
    if rule is None:
        return fail(why)

    out.rule = rule
    out.site_name = str(rule.get("name") or "")
    try:
        msg, res = _run_sync(lambda: _run_check(rule, raw, int(sid), deep), timeout)
    except asyncio.TimeoutError:
        return fail(f"校验超时（{timeout:.0f} 秒），站点太慢或不可达", rule)
    except Exception as e:  # noqa: BLE001
        from .probe import _err

        return fail(f"校验异常：{_err(e)}", rule)

    out.res = res
    out.message = msg
    if res is not None:
        out.status = str(getattr(res, "status", "fail"))
        out.chapters = int(getattr(res, "chapter_count", 0) or 0)
        out.images = int(getattr(res, "image_count", 0) or 0)
        out.log = list(getattr(res, "log", []) or [])
        if getattr(res, "site_name", ""):
            out.site_name = res.site_name
    out.ok = out.status == "ok"
    if not out.ok and not out.message:
        out.message = "校验未通过"
    return out


def add_checked_source(
    sid: int, *, deep: bool = True, timeout: float = 60.0
) -> "CheckResult":
    """校验并（仅当真的可用时）写入 ``config["user_rules"]``。

    失败一律不写 config —— 免得用户点一下就在站点列表里多出一个打不开的站。
    写库复用既有的 `probe.add_user_rule()`（同 id 视为更新，不会堆重复项）。
    """
    res = check_source(sid, deep=deep, timeout=timeout)
    if not res.ok or not res.rule:
        res.log.append("未通过校验，未写入配置")
        return res
    try:
        from .probe import add_user_rule

        if add_user_rule(res.rule):
            res.log.append(f"已写入自定义站点规则 {res.rule.get('id')}")
        else:
            res.ok = False
            res.status = "fail"
            res.message = "规则校验通过，但写入配置失败"
            res.log.append("add_user_rule 返回 False")
    except Exception as e:  # noqa: BLE001
        res.ok = False
        res.status = "fail"
        res.message = f"规则校验通过，但写入配置出错：{type(e).__name__}: {e}"
    return res
