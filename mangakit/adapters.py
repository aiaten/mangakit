"""内置站点适配器。

只在这里写「规则 JSON 搞不定的站点」，其余站点都用 rules/*.json 声明。
注册方式：由 SiteRegistry._load_builtin() 调用本模块的 register_all(reg)，
不要在模块导入时自己调 registry()——那会在注册表构造途中递归建出第二个实例。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote, urlparse

from .http_client import normalize_url
from .images import is_image
from .logsetup import get
from .models import Chapter, Comic
from .sites import ParseError, Site, SiteAdapter

log = get("adapter")


def _n(text: str) -> int:
    """从 '第12话' / 'No.12' / '12' 里抽数字。"""
    m = re.search(r"(\d+(?:\.\d+)?)", text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)))
    except ValueError:
        return 0


# ------------------------------------------------------------------ M 站

class MStationAdapter(SiteAdapter):
    """m.dm5w.com / m.6htxt.com 等 M 站模板。

    漫画页 https://m.dm5w.com/manga/<id>/  : 章节在 #chapterlist
    章节页 https://m.dm5w.com/manga/<id>/<n>.html : 图片在 .showimg img 的 data-src
    """

    site_id = "mstation"

    def _pick(self, doc: Any, expr: str, base: str) -> list[str]:
        from .sites import Selector
        return Selector({"css": expr, "attr": "data-src"}).find_all(doc, base)

    async def parse_comic(self, url: str) -> Comic:
        html = await self.fetch(url)
        from lxml import html as LH
        doc = LH.fromstring(html)
        from .sites import Selector

        def one(sel: str, attr: str = "") -> str:
            r = Selector({"css": sel, **({"attr": attr} if attr else {"text": True})}).find_all(doc, url)
            return r[0] if r else ""

        c = Comic(title="", url=url, site=self.site.id, site_name=self.site.name)
        c.title = one("h1") or one(".bookName") or one("title")
        c.title = re.sub(r"\s*[-_|].*$", "", c.title).strip()
        c.author = one(".booktitle a + a, .info a") or one(".bookName")
        c.cover = one(".bookimg img", "src")
        c.tags = Selector({"css": ".nav a", "text": True, "all": True}).find_all(doc, url)
        c.description = one(".intro, .bookintro")
        c.cid = url.rstrip("/").split("/")[-1]
        return c

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        out: list[Chapter] = []
        seen: set[str] = set()
        for n in Selector({"css": "#chapterlist a"}).find_nodes(doc):
            href = n.get("href") if hasattr(n, "get") else ""
            if not href:
                continue
            u = normalize_url(url, href)
            if u in seen:
                continue
            seen.add(u)
            title = (n.text_content() or "").strip()
            out.append(Chapter(cid=u, title=title, url=u))
        out.sort(key=lambda c: _n(c.title))
        for i, c in enumerate(out):
            c.index = i
        return out

    async def parse_pages(self, url: str) -> list[str]:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        urls: list[str] = []
        for sel in (".showimg img", ".page_contain img", ".comic_pic img"):
            r = Selector({"css": sel, "attr": "data-src", "all": True}).find_all(doc, url)
            if r:
                urls = r
                break
        if not urls:
            urls = Selector({"css": ".showimg img", "attr": "src", "all": True}).find_all(doc, url)
        seen: set[str] = set()
        out = [u for u in urls if u and not (u in seen or seen.add(u))]
        if not out:
            raise ParseError("章节内没有图片（可能需要登录或站点改版）")
        return out


# ------------------------------------------------------------ 快看漫画

class KuaikanAdapter(SiteAdapter):
    """kuaikanmanzhen.com / kuaikank.com（快看漫画）。

    漫画页 /comic/<id>  章节在 .chapter-list a
    章节页 /comic/<id>/<cid>/1  图片在 .reader-img img 的 data-src
    """

    site_id = "kuaikan"

    async def parse_comic(self, url: str) -> Comic:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        c = Comic(title="", url=url, site=self.site.id, site_name=self.site.name)
        c.title = (Selector({"css": "h1.title, h1", "text": True}).find_all(doc, url) or [""])[0].strip()
        c.author = (Selector({"css": ".info a, .author", "text": True}).find_all(doc, url) or [""])[0]
        c.cover = (Selector({"css": ".cover img, .comic-cover img", "attr": "src"}).find_all(doc, url) or [""])[0]
        c.cid = url.rstrip("/").split("/")[-1]
        return c

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        out: list[Chapter] = []
        for n in Selector({"css": ".chapter-list a, .chapter-item"}).find_nodes(doc):
            href = n.get("href") if hasattr(n, "get") else ""
            if not href:
                continue
            u = normalize_url(url, href)
            out.append(Chapter(cid=u, title=(n.text_content() or "").strip(), url=u))
        out.sort(key=lambda c: _n(c.title))
        for i, c in enumerate(out):
            c.index = i
        return out

    async def parse_pages(self, url: str) -> list[str]:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        urls = Selector({"css": ".reader-img img", "attr": "data-src", "all": True}).find_all(doc, url)
        if not urls:
            urls = Selector({"css": ".reader-img img", "attr": "src", "all": True}).find_all(doc, url)
        if not urls:
            raise ParseError("章节页没有读到图片")
        return urls


# ---------------------------------------------------- 拷贝漫画（爬虫式）

class KZAdapter(SiteAdapter):
    """拷贝漫画（kzdm.work / m.kzdm.com）。

    M 站模板 + App API 兜底：
      https://app.kzdm.com/manhua/book/<id>       书籍信息（JSON）
      https://app.kzdm.com/manhua/book/<id>/<cid> 章节
      .../image_list/<cid>                      图片列表
    """

    site_id = "kzdm"

    API = "https://app.kzdm.com/manhua"

    async def _api(self, path: str) -> Any:
        url = self.API + path
        r = await self.http.get(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/120 Mobile",
        }, kind="html")
        r.raise_for_status()
        return r.json()

    async def parse_comic(self, url: str) -> Comic:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        c = Comic(title="", url=url, site=self.site.id, site_name=self.site.name)
        t = Selector({"css": "h1, .bookName", "text": True}).find_all(doc, url)
        c.title = (t[0] if t else "").strip()
        c.author = (Selector({"css": ".info a, .bookName", "text": True}).find_all(doc, url) or [""])[0]
        cov = Selector({"css": ".bookimg img, .cover img", "attr": "src"}).find_all(doc, url)
        c.cover = cov[0] if cov else ""
        c.cid = re.search(r"book/(\d+)", url).group(1) if re.search(r"book/(\d+)", url) else url.rstrip("/").split("/")[-1]
        return c

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        out: list[Chapter] = []
        try:
            cid = re.search(r"book/(\d+)", url).group(1)
            data = await self._api(f"/book/{cid}")
            items = (data.get("data") or {}).get("chapters") or []
            for i, it in enumerate(items):
                u = f"{self.API}/book/{cid}/{it['chapter_id']}"
                out.append(Chapter(cid=u, title=it.get("chapter_name", ""), url=u, index=i))
            if out:
                return out
        except Exception as e:
            log.debug("kzdm API 章节失败，退回 HTML: %s", e)
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        for n in Selector({"css": "#chapterlist a"}).find_nodes(doc):
            h = n.get("href") if hasattr(n, "get") else ""
            if h:
                u = normalize_url(url, h)
                out.append(Chapter(cid=u, title=(n.text_content() or "").strip(), url=u))
        out.sort(key=lambda c: _n(c.title))
        for i, c in enumerate(out):
            c.index = i
        return out

    async def parse_pages(self, url: str) -> list[str]:
        html = await self.fetch(url)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)
        urls = Selector({"css": ".showimg img", "attr": "data-src", "all": True}).find_all(doc, url)
        if urls:
            return urls
        # 长图：直接把 <img> 里的长图地址取出来
        m = re.search(r'var\s+chapterInfo\s*=\s*(\{.*?\});', html, re.S)
        if m:
            try:
                d = json.loads(m.group(1))
                imgs = d.get("images") or d.get("imgs") or []
                if imgs:
                    return [normalize_url(url, i) for i in imgs]
            except Exception:
                pass
        raise ParseError("拷贝漫画章节页未读到图片")


# ---------------------------------------------------------- 通用 JSON API

class GenericApiAdapter(SiteAdapter):
    """按站点 payload 里声明的 JSON API 解析。

    site.payload = {
      "api": "https://site/api",
      "book": {"path": "/book/{cid}", "map": {"title": "data.name",
               "cover": "data.cover", "chapters": "data.chapters"}},
      "pages": {"path": "/pages/{cid}", "map": {"urls": "data.images"}}
    }
    """

    site_id = "generic_api"

    def _dig(self, obj: Any, path: str) -> Any:
        from .sites import _json_path
        r = _json_path(obj, path)
        return r[0] if r else None

    async def parse_comic(self, url: str) -> Comic:
        p = self.site.payload
        api = p.get("api", "")
        cid = p.get("cid_from_url")
        key = re.search(cid, url).group(1) if cid and re.search(cid, url) else url.rstrip("/").split("/")[-1]
        url_api = api + p.get("book", {}).get("path", "").format(cid=key)
        data = await self.http.get_json(url_api, headers=self.site.headers)
        m = p.get("book", {}).get("map", {})
        c = Comic(title=str(self._dig(data, m.get("title", "title")) or ""),
                  url=url, site=self.site.id, site_name=self.site.name, cid=key)
        c.cover = str(self._dig(data, m.get("cover", "cover")) or "")
        c.author = str(self._dig(data, m.get("author", "author")) or "")
        c.description = str(self._dig(data, m.get("description", "description")) or "")
        chs = self._dig(data, m.get("chapters", "chapters")) or []
        c.chapters = [
            Chapter(cid=str(ch.get("id", "")), title=str(ch.get("name") or ch.get("title") or ""),
                    url=str(ch.get("url") or ""))
            for ch in chs if isinstance(ch, dict)
        ]
        for i, ch in enumerate(c.chapters):
            ch.index = i
        return c

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        c = await self.parse_comic(url)
        return c.chapters

    async def parse_pages(self, url: str) -> list[str]:
        p = self.site.payload
        key = self._cid(url)
        url_api = p.get("api", "") + p.get("pages", {}).get("path", "").format(cid=key)
        data = await self.http.get_json(url_api, headers=self.site.headers)
        arr = self._dig(data, p.get("pages", {}).get("map", {}).get("urls", "images")) or []
        return [normalize_url(url, str(x)) for x in arr if str(x).startswith(("http", "//"))]

    @staticmethod
    def _cid(url: str) -> str:
        segs = [s for s in urlparse(url).path.split("/") if s]
        return segs[-1] if segs else ""


def register_all(reg: "SiteRegistry") -> None:
    """把本模块的内置适配器注册进给定的注册表实例。

    参数是注册表本身而不是隐式取全局单例：SiteRegistry.__init__ 期间
    全局单例尚未赋值，只能由调用方把「正在构造的那个」传进来。
    """
    reg.sites.setdefault("mstation", Site(
        id="mstation", name="M站模板（dm5w/6htxt 等）",
        domains=["dm5w.com", "dm5w.net", "dm5w.cc", "dm5w.vip", "dm5w1.com",
                 "dm5m.com", "6htxt.com", "m.6htxt.com", "dmzyw.com", "dmzyw.net"],
    ))
    reg.sites.setdefault("kuaikan", Site(
        id="kuaikan", name="快看漫画",
        domains=["kuaikanmanzhen.com", "kuaikank.com", "kuaikanmanzhen.cn"],
    ))
    reg.sites.setdefault("kzdm", Site(
        id="kzdm", name="拷贝漫画",
        domains=["kzdm.work", "kzdm.com", "m.kzdm.com"],
    ))
    for cls in (MStationAdapter, KuaikanAdapter, KZAdapter):
        reg.register_adapter(cls)
