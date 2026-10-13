"""站点规则引擎。

一个站点 = 一个 JSON 规则文件（rules/<id>.json）或一个 Python 适配器类。
JSON 规则用声明式 selector 描述「怎么找到漫画/章节/图片」，
站点改版时只改 JSON，不动代码。
"""

from __future__ import annotations

import abc
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

from .http_client import HttpClient, normalize_url
from .images import is_image
from .logsetup import get
from .models import Chapter, Comic

log = get("site")


# ---------------------------------------------------------------- 选择器

class Selector:
    """从一段 HTML/JSON 中提取内容。

    规则里的 selector 写法（简写即可）：
      {"css": "div.item"}            CSS 选择器
      {"xpath": "//div[@id='x']"}    XPath
      {"json": "data.images"}        JSON 路径（点号 + 数字下标）
      {"json": "$.data[*].url"}      JSONPath 子集
      {"regex": "<img[^>]+src=['\\"](.+?)['\\"]"}
      {"attr": "src"}                配合 css/xpath，取属性
      {"text": true}                 取文本内容
      {"text": "direct"}             只取节点自身直接文本（不含子元素，如 h1 里的标签分类）
      {"all": true}                  取全部而非第一个
      {"replace": ["a", "b"]}        结果字符串替换
      {"re": "url\\((.+)\\)", "group": 1}  再用正则从结果里挖一次（style 里的封面等）
    """

    def __init__(self, spec: dict[str, Any] | str):
        self.spec = spec if isinstance(spec, dict) else {"css": spec}

    @property
    def is_all(self) -> bool:
        return bool(self.spec.get("all"))

    def _transform(self, values: list[Any], base_url: str = "") -> list[str]:
        # replace 支持三种写法（见 _as_replace_pairs）：单串删空、二元替换、多条并列
        for pat, rep in _as_replace_pairs(self.spec.get("replace")):
            values = [v.replace(pat, rep) for v in values]
        # base_url 只对「URL 型取值」有意义。取 title/alt 这类纯文本属性时
        # 必须写 "url": false 关掉，否则 "海贼王" 会被 urljoin 拼成
        # https://站点/海贼王 这种假地址。
        if base_url and self.spec.get("url", True):
            values = [normalize_url(base_url, v) for v in values]
        return values

    def find_nodes(self, root: Any) -> list[Any]:
        """返回原始节点（不做 attr/text 提取），供需要自己取属性的地方使用。"""
        s = self.spec
        css = s.get("css")
        if css:
            return _css(root, css)
        if (xp := s.get("xpath")) is not None:
            return _xpath(root, xp)
        if (jp := s.get("json")) is not None:
            return _json_path(root, jp)
        if (rx := s.get("regex")) is not None:
            return re.findall(str(rx), _as_html(root))
        return []

    def find_all(self, root: Any, base_url: str = "") -> list[Any]:
        s = self.spec
        out: list[Any] = []
        attr = s.get("attr")

        css = s.get("css")
        if css:
            nodes = _css(root, css)
            out = nodes
        elif (xp := s.get("xpath")) is not None:
            out = _xpath(root, xp)
        elif (jp := s.get("json")) is not None:
            out = _json_path(root, jp)
        elif (rx := s.get("regex")) is not None:
            html = _as_html(root)
            out = [m if isinstance(m, str) else m[0] for m in re.findall(str(rx), html)]
        elif attr or s.get("text"):
            # 只写 {"attr": "href"} 或 {"text": true}、没给选择器时，
            # 作用于传进来的节点本身（search 里的 link/title 就靠这条）。
            out = list(root) if isinstance(root, (list, tuple)) else [root]
        else:
            return []

        if attr:
            # attr 可以写成列表：同一份规则要同时吃桌面站与手机站的页面时很常用，
            # 比如 og:image 放在 <meta content> 里，而手机版是 <img src>。
            # 逐个属性试，取第一个非空的。
            attrs = list(attr) if isinstance(attr, (list, tuple)) else [attr]
            vals: list[Any] = []
            for n in out:
                v = ""
                for a in attrs:
                    v = _get_attr(n, str(a))
                    if v:
                        break
                vals.append(v)
            out = vals
        elif s.get("text"):
            if str(s.get("text")).lower() == "direct":
                out = [_get_own_text(n) for n in out]
            else:
                out = [_get_text(n) for n in out]

        strs = [str(x).strip() for x in out if x is not None]
        if s.get("re"):
            strs = [v for v in (_apply_re(x, s) for x in strs) if v]
        strs = [x for x in strs if x]
        # replace 是纯字符串替换，text 型取值也要生效（"作者：尾田" 想只留作者名）；
        # urljoin 只对 URL 型取值有意义，所以只在这一支里做。
        strs = self._transform(strs, base_url if (attr or s.get("regex")) else "")
        return strs


def _as_html(root: Any) -> str:
    """把节点转成 HTML 文本（正则选择器需要带标记的源码）。"""
    if isinstance(root, str):
        return root
    if isinstance(root, (bytes, bytearray)):
        return root.decode("utf-8", errors="replace")
    try:
        from lxml import etree
        return etree.tostring(root, encoding="unicode", method="html")
    except Exception:
        pass
    try:
        return root.text_content()
    except Exception:
        return str(root)


def _css(root: Any, expr: str) -> list[Any]:
    from lxml import html as LH

    if isinstance(root, (list, tuple)):
        # 规则里 link/title/cover 这类子选择器是作用在「上一步选出的节点列表」上的，
        # 必须逐个节点查询再拼起来，否则整条搜索结果都是空的。
        out: list[Any] = []
        for n in root:
            out += _css(n, expr)
        return out
    if isinstance(root, str):
        root = LH.fromstring(root)
    elif isinstance(root, (bytes, bytearray)):
        root = LH.fromstring(root)
    try:
        return list(root.cssselect(expr))
    except Exception:
        # cssselect 语法子集失败时退回自己实现的 CSS→XPath
        try:
            return list(root.xpath(_css_to_xpath(expr)))
        except Exception as e:
            log.debug("CSS %r 失败: %s", expr, e)
            return []


def _css_to_xpath(expr: str) -> str:
    """把简单 CSS 转 XPath（只处理 tag/.class/#id/[attr] 组合）。

    注意必须用相对路径 `.//` 从当前节点往下找，用 `//` 会脱离上下文、
    直接从整篇文档根开始匹配，子选择器就全乱了。
    """
    parts = re.split(r"\s+", expr.strip())
    xp = []
    for i, p in enumerate(parts):
        m = re.match(r"^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[^\]]+\])*)$", p)
        if not m:
            raise ValueError(f"unsupported css: {p}")
        tag = m.group(1) or "*"
        seg = (".//" if i == 0 else "//") + tag
        for t in re.finditer(r"([.#][\w-]+)|\[([^\]]+)\]", m.group(2) or ""):
            if t.group(1):
                kind, name = t.group(1)[0], t.group(1)[1:]
                if kind == ".":
                    seg += f'[contains(concat(" ", normalize-space(@class), " "), " {name}")]'
                else:
                    seg += f'[@id="{name}"]'
            else:
                attr = t.group(2)
                if "=" in attr:
                    k, v = attr.split("=", 1)
                    seg += f'[@{k.strip()}="{v.strip().strip(chr(39)).strip(chr(34))}"]'
                else:
                    seg += f"[@{attr.strip()}]"
        xp.append(seg)
    return "".join(xp)


def _xpath(root: Any, expr: str) -> list[Any]:
    from lxml import html as LH

    if isinstance(root, (list, tuple)):
        out: list[Any] = []
        for n in root:
            out += _xpath(n, expr)
        return out
    if isinstance(root, str):
        root = LH.fromstring(root)
    try:
        nodes = root.xpath(expr)
    except Exception as e:
        log.debug("XPath %r 失败: %s", expr, e)
        return []
    return nodes if isinstance(nodes, list) else [nodes]


def _json_path(root: Any, path: str) -> list[Any]:
    """支持 `a.b[0].c`、`a[*].b`、`$.a.b` 三种写法。`[*]` 表示展平一层。"""
    cur: Any = root
    p = path.lstrip("$").lstrip(".")
    segs = [s for s in p.split(".") if s]
    for i, seg in enumerate(segs):
        # 允许 `*`、`0`、`[0]`、`[*]` 四种下标写法（`$.*` 顶层展开成 `[*]`）
        s = seg.strip()
        if s in ("*", "[*]"):
            s = "[*]"
        elif re.fullmatch(r"\d+", s) or re.fullmatch(r"\[\d+\]", s):
            s = f"[{s.strip('[]')}]"
        m = re.match(r"^([\w-]*)(?:\[(\*|\d+)\])?$", s)
        if not m:
            return []
        name, idx = m.group(1), m.group(2)
        if name:
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur)
                except Exception:
                    return []
            if not isinstance(cur, dict) or name not in cur:
                return []
            cur = cur[name]
        if idx == "*":
            if not isinstance(cur, list):
                return []
            if i == len(segs) - 1:
                return cur
            # 展平：对列表每个元素递归解析剩余路径
            out: list[Any] = []
            for item in cur:
                out.extend(_json_path(item, ".".join(segs[i + 1:])))
            return out
        elif idx is not None:
            if not isinstance(cur, list):
                return []
            k = int(idx)
            if k >= len(cur):
                return []
            cur = cur[k]
    if isinstance(cur, list):
        return cur
    return [cur]


def _get_attr(node: Any, attr: str) -> str:
    if isinstance(node, str):
        return node
    if hasattr(node, "get"):
        return node.get(attr) or ""
    return ""


def _get_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if hasattr(node, "text_content"):
        try:
            return re.sub(r"\s+", " ", node.text_content()).strip()
        except Exception:
            return ""
    return ""


def _merge_chapter_label(out: list[Chapter], cu: str, title: str) -> None:
    """同一章节 URL 被多次链接时，挑一个更像「话数」的文案当标题。

    详情页顶部常有「开始阅读 / 继续阅读」按钮指向最新话，与目录里的
    「第N话」是同一个链接；谁先被解析到谁就定了章节名，于是整章显示成
    「开始阅读」。这里以「含数字（第12话 / 12话 / 12）」为更像话数的标准，
    按钮文案（无数字）让位给带数字的目录文案。
    """
    clean = " ".join(title.split())[:160]
    if not clean:
        return
    for c in out:
        if c.url != cu:
            continue
        old = c.title or ""
        if bool(re.search(r"\d", clean)) and not re.search(r"\d", old):
            c.title = clean
        return


def _as_replace_pairs(fix: Any) -> list[tuple[str, str]]:
    """把规则里的 replace / url_replace 字段归一成 [(正则, 替换), ...]。

    三种写法都支持：
      "模式"                    → 把命中的部分删掉
      ["模式", "替换"]           → 一条替换
      ["模式1", "替换1", ["模式2", "替换2"]] → 多条
    """
    if not fix:
        return []
    if isinstance(fix, str):
        return [(fix, "")]
    if isinstance(fix, (list, tuple)) and len(fix) == 2 and all(isinstance(x, str) for x in fix):
        return [(fix[0], fix[1])]
    return [(p, "") if isinstance(p, str) else (p[0], p[1]) for p in fix]


def _get_own_text(node: Any) -> str:
    """只取节点**自身**的直接文本，不含子元素里的文字。

    很多站的 h1 里塞了标签分类，例如
    `<h1 class="title"> 比邻而交 <span class="labelBox"><a>都市</a></span> </h1>`，
    text_content() 会把分类一起读进来当标题，这时用 "text": "direct"。
    """
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node).strip()
    if not hasattr(node, "text"):
        return _get_text(node)
    parts = [node.text or ""]
    for child in node:
        if getattr(child, "tail", None):
            parts.append(child.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _apply_re(text: str, spec: dict[str, Any]) -> str:
    """规则的 re/group：在取到的文本/属性值里再挖一次。

    很多站把字段包在 JS 字面量或 style 里，例如
    `<p class="subtitle">作者：AI漫画</p>`、
    `style="background-image: url(https://.../cover.webp)"`，
    直接当结果返回就是脏数据。不匹配时返回空串（由调用方过滤）。
    """
    rx = spec.get("re")
    if not rx:
        return text
    m = re.search(str(rx), text)
    if not m:
        return ""
    try:
        gi = int(spec.get("group", 1))
    except (TypeError, ValueError):
        return m.group(0)
    return m.group(gi) if 0 <= gi <= (m.re.groups or 0) else m.group(0)


# ---------------------------------------------------------------- 站点定义

@dataclass
class Site:
    """站点元信息。"""

    id: str
    name: str
    domains: list[str]
    rule: str | None = None            # rules/<id>.json
    adapter: str | None = None         # 内置适配器类名
    enabled: bool = True
    lang: str = "cn"
    note: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    search_url: str = ""               # 支持 {q} 占位
    payload: dict[str, Any] = field(default_factory=dict)
    # 规则里写 "hidden": true 时，该站默认不出现在界面下拉/站点管理里。
    # 用于「下载能用但没有站内搜索」的站：功能保留，只是不占用户的选择列表。
    hidden: bool = False

    @property
    def can_search(self) -> bool:
        """该站是否支持站内搜索（rule 里有 search 块或填了 search_url）。"""
        spec = self.payload.get("search") or {}
        return bool(spec.get("url") or self.search_url)

    @property
    def can_expand(self) -> bool:
        """该站是否能补齐「详情页折叠隐藏」的章节。

        只有规则里配了 chapters.api（去调站点自己的接口）或 chapters.embed
        （从页面内嵌 JSON 里挖）才补得动；内置适配器自己实现 parse_chapters 的
        一律算支持（它们本来就可以拿到多少返回多少）。
        这样界面就能区分「点展开真能变多」和「这站本来就全了」，避免点了没反应。
        """
        chapters = self.payload.get("chapters") or {}
        return bool(chapters.get("api") or chapters.get("embed"))

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.domains)


class ParseError(Exception):
    """规则不匹配或页面结构变化。"""


class SiteAdapter(abc.ABC):
    """适配器基类。内置适配器实现这些方法。"""

    site: Site

    def __init__(self, site: Site, http: HttpClient):
        self.site = site
        self.http = http

    # 子类按需覆盖
    async def parse_comic(self, url: str) -> Comic:
        raise NotImplementedError

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        """列出章节。

        full=True 表示「把站点折叠隐藏的章节也全部展开」。不少站的详情页只渲染
        最近 10 话，剩下的藏在「加载更多章节」按钮后面，需要另外调一次接口
        （见 RuleAdapter 里 chapters.api 的实现）；不开启 full 就只返回 HTML
        里直接可见的那些，界面上据此提示用户还有多少话没显示。
        """
        raise NotImplementedError

    async def parse_pages(self, url: str) -> list[str]:
        raise NotImplementedError

    async def search(self, keyword: str) -> list[Comic]:
        raise NotImplementedError

    async def cover(self, url: str) -> bytes:
        raise NotImplementedError

    # 便捷方法
    async def fetch(self, url: str, **kw: Any) -> str:
        kw.setdefault("headers", self.site.headers)
        kw.setdefault("cookies", self.site.cookies or None)
        return await self.http.get_text(url, **kw)


class RuleAdapter(SiteAdapter):
    """由 JSON 规则驱动的通用适配器。

    规则结构：
      {
        "id": "example",
        "comic": {
          "title": {"css": "h1.title", "text": true},
          "author": {"css": ".author a", "text": true, "all": true},
          "cover":  {"css": ".cover img", "attr": "src"},
          "tags":   {"css": ".tag", "text": true, "all": true},
          "status": {"css": ".status", "text": true},
          "description": {"css": ".desc", "text": true}
        },
        "chapters": {
          "list": {"css": ".chapter-list a"},
          "url":   {"attr": "href"},
          "title": {"text": true},
          "next":  {"css": "a.next", "attr": "href"},
          "paginated": true
        },
        "pages": {
          "list": {"css": ".page img"},
          "url":   {"attr": "data-src", "replace": ["-1x", ""]}
        },
        "search": {
          "url": "https://x.com/search?q={q}",
          "list": {"css": ".result"},
          "title": {"text": true},
          "link": {"attr": "href"},
          "cover": {"css": "img", "attr": "src"}
        }
      }
    """

    def __init__(self, site: Site, http: HttpClient, rule: dict[str, Any]):
        super().__init__(site, http)
        self.rule = rule
        self.sel = {k: Selector(v) for k, v in rule.items() if isinstance(v, dict)}

    def _pick(self, section: str, field: str, nodes: list[Any], base: str) -> list[str]:
        spec = self.rule.get(section, {}).get(field)
        if not spec:
            return []
        return Selector(spec).find_all(nodes, base)

    async def _load(self, url: str) -> Any:
        return self._parse_html(await self.fetch(url))

    @staticmethod
    def _parse_html(html: str) -> Any:
        from lxml import html as LH
        try:
            return LH.fromstring(html)
        except Exception as e:
            raise ParseError(f"HTML 解析失败: {e}") from e

    async def _follow_jump(self, url: str, spec: dict, max_hops: int = 3) -> tuple[str, str]:
        """跟随「跳转中」中转页，返回（最终 URL，已经抓到的 HTML）。

        有些站的章节链接是 base64 短链（如漫画1234 的 /go/MTQ1...），页面里
        只有一句 `<script>location.replace("真正的阅读页")</script>`，直接抓
        只能拿到 800 字节的空壳。规则里用 pages.jump 写识别用的正则（第 1 组
        是目标地址），这里就顺着它一跳一跳跟过去。抓到正文页就不再跟了，
        已经下载到的 HTML 一并返回，省一次请求。
        """
        jump = spec.get("jump")
        if not jump:
            return url, ""
        rx = re.compile(str(jump))
        html = ""
        for _ in range(max_hops):
            try:
                html = await self.fetch(url)
            except Exception as e:
                log.warning("%s: 中转页 %s 抓取失败: %s", self.site.id, url, e)
                return url, html
            m = rx.search(html)
            if not m:
                break
            nxt = normalize_url(url, m.group(1).strip())
            if not nxt or nxt == url:
                break
            log.debug("%s: 跟随跳转 %s -> %s", self.site.id, url, nxt)
            url = nxt
        return url, html

    async def parse_comic(self, url: str) -> Comic:
        doc = await self._load(url)
        c = Comic(title="", url=url, site=self.site.id, site_name=self.site.name)
        spec = self.rule.get("comic", {})

        def one(field: str) -> str:
            s = spec.get(field)
            if not s:
                return ""
            r = Selector(s).find_all(doc, url)
            return r[0] if r else ""

        def many(field: str) -> list[str]:
            s = spec.get(field)
            if not s:
                return []
            return Selector(s).find_all(doc, url)

        c.title = one("title").strip() or self._title_from_url(url)
        c.author = "、".join(many("author"))[:200]
        c.cover = one("cover")
        c.tags = many("tags")[:30]
        c.description = one("description")[:4000]
        c.cid = self._cid(url)
        st = one("status").lower()
        c.status = "finished" if any(k in st for k in ("完结", "結束", "完", "completed", "finished")) else (
            "ongoing" if any(k in st for k in ("连载", "連載", "ongoing", "serializing")) else "unknown"
        )
        return c

    @staticmethod
    def _title_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/").split("/")[-1]
        return re.sub(r"[_\-]+", " ", path)[:80]

    @staticmethod
    def _cid(url: str) -> str:
        p = urlparse(url)
        segs = [s for s in p.path.split("/") if s]
        return segs[-1] if segs else (p.hostname or "")

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        """列出章节。

        full=True 表示「把站点折叠隐藏的章节也全部展开」。不少站的详情页只渲染
        最近 10 话，剩下的藏在「加载更多章节」按钮后面，需要另外调一次接口
        （见 RuleAdapter 里 chapters.api 的实现）；不开启 full 就只返回 HTML
        里直接可见的那些，界面上据此提示用户还有多少话没显示。
        """
        spec = self.rule.get("chapters", {})
        list_sel = spec.get("list")
        if not list_sel:
            raise ParseError("规则缺少 chapters.list")
        seen: set[str] = set()
        out: list[Chapter] = []
        cur = url
        pages = 0
        first_html = ""
        max_pages = int(spec.get("max_pages") or 20)
        while cur and pages < max_pages:
            html = await self.fetch(cur)
            doc = self._parse_html(html)
            if not first_html:
                first_html = html
            for n in Selector(list_sel).find_nodes(doc):
                if isinstance(n, str):
                    cu = normalize_url(cur, n)
                    title = n
                else:
                    cu = normalize_url(cur, _get_attr(n, "href") or "")
                    title = _get_text(n)
                if not cu:
                    continue
                rx = spec.get("url_re")
                if rx and not re.search(rx, cu):
                    continue
                # 同一个 URL 出现多次是常态：详情页顶部常有一个「开始阅读 / 继续阅读」
                # 的按钮，指向最新话，和下面目录里的「第N话」是同一个链接。
                # 谁先出现谁就定了章节名，而按钮文案会把整章标成「开始阅读」——
                # 所以重复时保留「更像话数」的那个文案，而不是简单丢弃后来的。
                if cu in seen:
                    _merge_chapter_label(out, cu, _get_text(n) if not isinstance(n, str) else n)
                    continue
                # 可选：把章节 URL 改写成另一种形态。部分站目录页里的链接指向
                # "单页展示版"，另有 all.html 之类的"一屏放全部大图"版本更好用。
                for pat, rep in _as_replace_pairs(spec.get("url_replace")):
                    cu = re.sub(pat, rep, cu)
                if not cu or cu in seen:
                    continue
                seen.add(cu)
                # 章节标题会直接当目录名用，必须先把换行/tab/控制字符压平，
                # 否则 Windows 上会报 [WinError 123] 非法路径。
                clean = " ".join(_get_text(n).split()) if not isinstance(n, str) else " ".join(n.split())
                out.append(Chapter(cid=cu, title=clean[:160] or cu, url=cu))
            pages += 1
            nxt = spec.get("next")
            if not nxt:
                break
            r = Selector(nxt).find_all(doc, cur)
            cur = normalize_url(cur, r[0]) if r and r[0] else ""
        if full:
            extra = await self._api_chapters(spec, url, seen)
            if extra:
                log.info("%s: 通过接口补齐 %d 个折叠章节", self.site.id, len(extra))
                out.extend(extra)
            extra2 = self._embed_chapters(spec, url, first_html, seen)
            if extra2:
                log.info("%s: 从页面内嵌数据补齐 %d 个折叠章节", self.site.id, len(extra2))
                out.extend(extra2)
        # 排序：默认按 URL 字典序（对 /chapter/1、/chapter/2 这类够用）。
        # 但有些站的章节 URL 里是「越新 id 越大」的纯数字（dm5 的 /m1839988/），
        # 字典序会把最新话排到最后、还打乱数字顺序（/m999/ 排在 /m1839988/ 后面）。
        # 规则里可以给 chapters.order 写一条正则，捕获组 1 取出真正的序号。
        order = spec.get("order")
        if order:
            rx = re.compile(str(order))

            def _ok(c: Chapter) -> tuple[int, str]:
                m = rx.search(c.url)
                try:
                    return (int(m.group(1)), c.url) if m else (1 << 30, c.url)
                except (ValueError, IndexError):
                    return (1 << 30, c.url)

            out.sort(key=_ok)
        else:
            out.sort(key=lambda c: c.url)
        for i, c in enumerate(out):
            c.index = i
        return out

    async def _api_chapters(self, spec: dict, url: str, seen: set[str]) -> list[Chapter]:
        """调站点自己的接口，把折叠隐藏的章节补回来。

        规则写法（chapters.api）：
          {
            "url":    "https://x.com/comic/{id}",   # {id} 会被替换成漫画 id
            "method": "POST",                       # 默认 GET
            "data":   {},                           # POST 的 JSON body
            "list":   "data.chapters",              # _json_path 写法，指向章节数组
            "title":  "chapterName",
            "url":    "/chapter/{contentId}-{id}.html"   # {contentId}/{id} 用数组元素字段插值
          }
        抓不到就返回空列表，调用方会继续用 HTML 里那部分（不报错）。
        """
        api = spec.get("api")
        if not api:
            return []
        try:
            raw = self._cid(url)
            # 详情页 URL 常带后缀（/comic/2579.html），而接口地址通常要的是裸 id
            # （/comic/2579）。所以 {id} 给去后缀的，{raw} 给原始最后一段，
            # 规则里按站点实际情况挑一个用。
            cid = re.sub(r"\.(?:html?|php|aspx?|jsp)$", "", raw, flags=re.I) or raw
            api_url = str(api.get("url", "")).format(id=cid, cid=cid, slug=cid, raw=raw)
            if not api_url:
                return []
            method = str(api.get("method", "GET")).upper()
            payload = api.get("data")
            resp = await self.http.request(
                method, api_url,
                headers={**self.site.headers, **(api.get("headers") or {})},
                json=payload if method in ("POST", "PUT") else None,
                data=payload if method in ("POST", "PUT") and not isinstance(payload, (dict, list)) else None,
                kind="json",
            )
            if resp.status_code >= 400:
                log.warning("%s: 章节接口 %s 返回 %s", self.site.id, api_url, resp.status_code)
                return []
            data = resp.json()
            items = _json_path(data, str(api.get("list", "$.*")))
            items = [x for x in items if isinstance(x, dict)]
            tkey = str(api.get("title", "chapterName"))
            # 章节地址模板：既支持 /chapter/{contentId}-{id}.html 这种插值，
            # 也支持只写一个字段名（{"chapter_url": "url"}）直接取字段。
            # 注意键名不能用 "url"——那个已经被接口地址占用了。
            tpl = str(api.get("chapter_url") or api.get("url_tpl") or api.get("path") or "url")
            out: list[Chapter] = []
            for it in items:
                title = str(it.get(tkey) or "").strip()
                try:
                    cu = tpl.format(**it) if "{" in tpl else str(it.get(tpl) or "")
                except (KeyError, IndexError):
                    continue
                if not cu:
                    continue
                cu = normalize_url(api_url, cu)
                if cu in seen:
                    continue
                seen.add(cu)
                # 注意用 [:160] 取前缀；写成 [160:] 会把短标题整段切成空串
                out.append(Chapter(cid=cu, title=(title or cu)[:160], url=cu))
            return out
        except Exception as e:
            log.warning("%s: 章节接口补齐失败: %s", self.site.id, e)
            return []

    @staticmethod
    def _json_unescape(text: str) -> str:
        """把 JSON 里的转义序列还原成原字符（章节数组常被嵌在字符串里）。"""
        try:
            return json.loads(f'"{text}"')
        except Exception:
            return text

    @staticmethod
    def _balanced(text: str, start: int) -> str:
        """从 start 处（应是 '[' 或 '{'）开始做括号配平，取出完整片段。"""
        pairs = {"[": "]", "{": "}"}
        first = text[start]
        close = pairs.get(first)
        if not close:
            return ""
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == first:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return ""

    def _embed_chapters(self, spec: dict, url: str, html: str, seen: set[str]) -> list[Chapter]:
        """从详情页内嵌的数据里抠出折叠隐藏的章节。

        有些站是前端框架（Next.js 等）渲染的，页面上只画了最近 20 个章节，
        完整的章节数组却原样塞在 HTML 的内联脚本里。这里用规则里的正则找到
        那段数据的**开头**，再由引擎做括号配平取出完整片段，逐条取字段拼成
        章节地址。

        规则写法（chapters.embed）：
          {
            "start":  "\\\"chapters\\\":(\\[)",        # 第 1 组 = 数组/对象的起点
            "item":   "\\\"id\\\":\\\"([^\\\"]+)\\\",\\\"title\\\":\\\"([^\\\"]*)\\\"",
            "fields": ["id", "title"],
            "url":    "/read/{comic_id}/{id}"
          }
        """
        em = spec.get("embed")
        if not em or not html:
            return []
        try:
            m = re.search(str(em["start"]), html, re.S)
            if not m:
                return []
            frag = self._json_unescape(self._balanced(html, m.start(1)))
            item_rx = re.compile(str(em["item"]))
            fields = [str(x) for x in (em.get("fields") or [])]
            tpl = str(em.get("url") or "")
            # 章节地址模板里除了数组字段，还可能要用漫画自身的 id 拼路径
            # （神奇漫画是 /read/<comicId>/<chapterId>），一并放进插值上下文。
            base = {"comic_id": self._cid(url), "cid": self._cid(url)}
            out: list[Chapter] = []
            seen_local: set[str] = set()
            for mm in item_rx.finditer(frag):
                if fields:
                    kv = dict(zip(fields, mm.groups()))
                else:
                    kv = {"1": mm.group(1), "2": mm.group(2)}
                kv.update(base)
                try:
                    cu = tpl.format(**kv)
                except (KeyError, IndexError):
                    continue
                cu = normalize_url(url, cu)
                if not cu or cu in seen or cu in seen_local:
                    continue
                seen_local.add(cu)
                title = str(kv.get("title") or kv.get("2") or "").strip()
                out.append(Chapter(cid=cu, title=(title or cu)[:160], url=cu))
            return out
        except Exception as e:
            log.warning("%s: 内嵌章节数据解析失败: %s", self.site.id, e)
            return []

    async def parse_pages(self, url: str) -> list[str]:
        spec = self.rule.get("pages", {})
        list_sel = spec.get("list")
        if not list_sel:
            raise ParseError("规则缺少 pages.list")
        url, html = await self._follow_jump(url, spec)
        # 跳转页被跟随时正文已经抓在手里，直接拿来解析，不用再请求一次。
        doc = self._parse_html(html) if html else await self._load(url)
        urls: list[str] = []
        # pages.url 可以是字符串（属性名）或 {"attr": "..."} 形式；
        # 没写就按内置的常见懒加载属性顺序猜。
        url_spec = spec.get("url")
        if isinstance(url_spec, dict):
            custom = url_spec.get("attr")
        elif isinstance(url_spec, str):
            custom = url_spec
        else:
            custom = spec.get("attr")
        for n in Selector(list_sel).find_nodes(doc):
            if isinstance(n, str):
                src = n
            else:
                src = (_get_attr(n, custom) if custom else None) or (_get_attr(n, "src") or _get_attr(n, "data-src")
                       or _get_attr(n, "data-original") or _get_attr(n, "data-echo")
                       or _get_attr(n, "data-lazy-src") or "")
            if src:
                u = normalize_url(url, src)
                if u and not u.startswith("data:"):
                    urls.append(u)
        if spec.get("lazy_js"):
            urls += await self._lazy_pages(doc, url, spec["lazy_js"])
        if spec.get("crypto"):
            from .crypto import pages_from_html
            urls += pages_from_html(_as_html(doc))
        # 可选白名单正则：只保留命中者（用于章节页混着封面/广告图的站点）
        keep = spec.get("filter")
        if keep:
            rx = re.compile(keep)
            urls = [u for u in urls if rx.search(u)]
        # 可选 URL 修正：修正站点自己写错的路径（重复段、缺后缀等）。
        # 三种写法都支持：
        #   "模式"                 → 把命中的部分删掉
        #   ["模式", "替换"]        → 一条替换
        #   ["模式1", "替换1", ["模式2", "替换2"]] → 多条
        fix = spec.get("replace")
        if fix:
            for pat, rep in _as_replace_pairs(fix):
                urls = [re.sub(pat, rep, u) for u in urls]
        # 去重保序
        seen: set[str] = set()
        uniq = [u for u in urls if not (u in seen or seen.add(u))]
        if not uniq:
            raise ParseError("未找到图片，可能是站点改版或需要登录")
        return uniq

    async def _lazy_pages(self, doc: Any, url: str, js_expr: Any) -> list[str]:
        """处理把图片列表塞在 JS 变量里的站点（常见于国内站）。

        js_expr 可以是：
        - 字符串/字符串列表：正则第 1 组，命中后按 pages.split 分隔展开
          （也支持第 1 组直接是 JSON 数组/对象，用 $.* 取全部字符串）
        - 字典（"前缀 + 序号范围"模式）：{"prefix": <re>, "count": <re>,
          "start": 1, "suffix": ".webp"}，生成 prefix+start .. prefix+count+suffix。
          用于章节页只下发一个目录前缀和一个总页数的站。
        """
        html = _as_html(doc)
        if isinstance(js_expr, dict):
            return self._expand_range(html, url, js_expr, self.rule.get("pages", {}))
        exprs = js_expr if isinstance(js_expr, list) else [js_expr]
        out: list[str] = []
        for expr in exprs:
            # 用 finditer 而不是 search：很多站同一份 JS 里有多处 "url" 字段
            # （第一处往往是站点 base url，不是图片），只取首个会全军覆没。
            for m in re.finditer(str(expr), html):
                raw = m.group(1)
                sep = self.rule.get("pages", {}).get("split") or "|||"
                if raw.lstrip().startswith(("[", "{")):
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = None
                    if data is not None:
                        out += [x for x in _json_path(data, "$.*") if isinstance(x, str)]
                        continue
                out += [x.strip() for x in raw.split(sep) if x.strip()]
        return [normalize_url(url, x) for x in out if x]

    @staticmethod
    def _expand_range(html: str, url: str, spec: dict, outer: dict | None = None) -> list[str]:
        """展开「前缀 + 序号范围」式的图片列表（dmanhua 等站）。

        start / suffix / width 默认写在 pages 层（和 lazy_js 平级），
        也允许直接塞在 lazy_js 字典里。
        """
        outer = outer or {}
        pfx = re.search(str(spec.get("prefix", "")), html)
        cnt = re.search(str(spec.get("count", "")), html)
        if not (pfx and cnt):
            return []
        try:
            total = int(cnt.group(1))
        except (ValueError, IndexError):
            return []
        if total <= 0:
            return []
        start = int(spec.get("start", outer.get("start", 1)))
        suffix = str(spec.get("suffix", outer.get("suffix", "")))
        width = int(spec.get("width", outer.get("width", 0)))
        base = pfx.group(1).strip()
        out = []
        for i in range(start, start + total):
            n = str(i).zfill(width) if width > 0 else str(i)
            out.append(base + n + suffix)
        return [normalize_url(url, x) for x in out if x]

    async def search(self, keyword: str) -> list[Comic]:
        spec = self.rule.get("search") or {}
        tpl = spec.get("url") or self.site.search_url
        if not tpl:
            return []
        url = tpl.format(q=keyword)
        html = await self.fetch(url)
        from lxml import html as LH
        try:
            doc = LH.fromstring(html)
        except Exception:
            return []
        res: list[Comic] = []
        for n in Selector(spec.get("list", {})).find_nodes(doc):
            link = Selector(spec.get("link", {"attr": "href"})).find_all([n], url)
            title = Selector(spec.get("title", {"text": True})).find_all([n], url)
            cov = Selector(spec.get("cover", {"css": "img", "attr": "src"})).find_all([n], url)
            if not link:
                continue
            res.append(Comic(
                title=(title[0] if title else self._title_from_url(link[0]))[:120],
                url=link[0], site=self.site.id, site_name=self.site.name,
                cover=cov[0] if cov else "", cid=self._cid(link[0]),
            ))
        return res

    async def cover(self, url: str) -> bytes:
        data, _ = await self.http.download(
            url, headers=self.site.headers, cookies=self.site.cookies or None
        )
        return data


# ---------------------------------------------------------------- 注册表

class SiteRegistry:
    """加载 rules/*.json 与内置适配器，按域名匹配。"""

    def __init__(self) -> None:
        self.sites: dict[str, Site] = {}
        self._rules: dict[str, dict[str, Any]] = {}
        self._adapters: dict[str, type[SiteAdapter]] = {}
        self.rules_dir = Path(__file__).parent / "rules"
        self._load_builtin()
        self._load_rules()

    # ---- 加载 ----
    def _load_builtin(self) -> None:
        from . import adapters  # noqa: F401  （触发注册）

    def register_adapter(self, cls: type[SiteAdapter]) -> None:
        self._adapters[cls.site_id] = cls  # type: ignore[attr-defined]

    def _load_rules(self) -> None:
        from .config import rules_dir
        d = rules_dir()
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                self._add_rule_file(f)
        # 用户自定义规则（存放在配置里，方便不改程序就加站点）
        from .config import config
        for i, raw in enumerate(config().get("user_rules") or []):
            try:
                data = json.loads(raw)
            except Exception as e:
                log.warning("用户规则 #%d 解析失败: %s", i + 1, e)
                continue
            sid = data.get("id") or f"user{i + 1}"
            data["id"] = sid
            data.setdefault("name", sid)
            self._rules[sid] = data
            self.sites.setdefault(sid, Site(
                id=sid, name=data.get("name", sid), domains=data.get("domains", []),
                rule="<user>", lang=data.get("lang", "cn"), note=data.get("note", ""),
                headers=data.get("headers", {}), search_url=data.get("search_url", ""),
                hidden=bool(data.get("hidden")),
                payload=data,
            ))

    def _add_rule_file(self, f: Path) -> None:
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception as e:
            log.warning("规则 %s 解析失败: %s", f.name, e)
            return
        sid = data.get("id") or f.stem
        self._rules[sid] = data
        self.sites.setdefault(sid, Site(
            id=sid,
            name=data.get("name", sid),
            domains=data.get("domains", []),
            rule=f.name,
            lang=data.get("lang", "cn"),
            note=data.get("note", ""),
            headers=data.get("headers", {}),
            search_url=data.get("search_url", ""),
            hidden=bool(data.get("hidden")),
            payload=data,
        ))

    # ---- 查找 ----
    def for_url(self, url: str) -> tuple[Site, SiteAdapter] | None:
        site = self.site_for_url(url)
        if not site:
            return None
        return site, self.adapter_for(site)

    def site_for_url(self, url: str) -> Site | None:
        for s in self.sites.values():
            if s.enabled and s.matches(url):
                return s
        return None

    def adapter_for(self, site: Site) -> SiteAdapter:
        http = _http()
        cls = self._adapters.get(site.id)
        if cls is not None:
            return cls(site, http)
        if site.id in self._rules:
            return RuleAdapter(site, http, self._rules[site.id])
        raise ParseError(f"站点 {site.id} 没有可用适配器")

    def all_sites(self) -> list[Site]:
        return sorted(self.sites.values(), key=lambda s: (not s.enabled, s.lang, s.name))

    def visible_sites(self) -> list[Site]:
        """界面上应当列出的站点：已启用、且规则没有标 hidden 的。

        hidden 的站（比如有下载能力但没有站内搜索的）依然能被 for_url 匹配，
        只是不占用户的下拉列表——见 Site.hidden。
        """
        return [s for s in self.all_sites() if s.enabled and not s.hidden]

    def set_enabled(self, site_id: str, enabled: bool) -> None:
        if site_id in self.sites:
            self.sites[site_id].enabled = enabled

    def supported_hosts(self) -> list[str]:
        return sorted({d for s in self.sites.values() for d in s.domains})


_http: HttpClient | None = None


_HTTP: HttpClient | None = None


def _http() -> HttpClient:
    # 注意：模块级缓存变量不能也叫 _http，否则 global _http 拿到的是本函数
    # 自己，`return _http` 会把函数当 HttpClient 返回，报
    # "AttributeError: 'function' object has no attribute 'get_text'"。
    global _HTTP
    if _HTTP is None:
        from .http_client import client as get_client
        _HTTP = get_client()
    return _HTTP


_registry: SiteRegistry | None = None


def registry() -> SiteRegistry:
    global _registry
    if _registry is None:
        _registry = SiteRegistry()
    return _registry


def reload_registry() -> SiteRegistry:
    """丢弃缓存的注册表，重新读一遍规则文件与用户自定义规则。

    用户增删自定义站点后必须调一次，否则界面拿到的还是旧的站点列表。
    """
    global _registry
    _registry = None
    return registry()