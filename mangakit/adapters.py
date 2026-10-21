"""内置站点适配器。

只在这里写「规则 JSON 搞不定的站点」，其余站点都用 rules/*.json 声明。
注册方式：由 SiteRegistry._load_builtin() 调用本模块的 register_all(reg)，
不要在模块导入时自己调 registry()——那会在注册表构造途中递归建出第二个实例。
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from .aes import decrypt_page_blob
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


# ------------------------------------------------ 拷貝漫畫（copy4000 系）

# 详情页密钥缓存：host -> (取到的时间, ccz, dnt)。
# parse_comic 与 parse_chapters 都要用 ccz/dnt，缓存一下省掉重复抓详情页。
# 过期、或者拿缓存解不开时会重新抓一次（密钥偶有轮换）。
_COPY_KEYS: dict[str, tuple[float, str, str]] = {}


class Copy4000Adapter(SiteAdapter):
    """拷貝漫畫（www.copy4000.com / www.copy5000.com）。

    这个站的详情页和章节页都是「服务端渲染壳 + JS 填充」，但 JS 要的数据
    我们自己算得出来，不需要跑浏览器：

    1. 详情页 https://www.copy4000.com/comic/<path_word>
       内联 `var ccz = '<16 字符>'`（章节列表的 AES 密钥）和
       `<span id="dnt" value="9">`（要作为 dnts 请求头回传）。
    2. 章节列表 GET /comicdetail/<path_word>/chapters，头 dnts: <dnt>
       响应 results 是「16 字符 iv + hex 密文」，用 ccz 做 AES-128-CBC 解密，
       得到 {"groups": {"default": {"chapters": [{"name","id","type"}]}}}。
    3. 章节页 /comic/<path_word>/chapter/<uuid>
       内联 `var cct = '<16 字符>'` 和 `var contentKey = '<iv+hex>'`，
       解密后是 [{"url": ...}]，就是图片列表。
    4. 站内搜索走 /api/kb/web/searchcl/comics（普通 JSON，不用解密）。

    注意两件事：
    - 站点对内容接口有 IP 级限流。被限时网页版不会报错，而是**安静地**
      返回空章节列表，所以「详情页明明有开始阅读链接、却解不出章节」按
      限流报错处理，比静默返回空列表好排查。
    - 站点自己还提供 v3 App API，但那条线对匿名请求只给 5 张预览图，而且
      更容易触发「请下载正版 APP」的风控。这里一律走网页版接口。
    """

    site_id = "copy4000"

    BASE = "https://www.copy4000.com"
    SEARCH_API = BASE + "/api/kb/web/searchcl/comics"
    PAGE_SIZE = 30
    MAX_RESULTS = 60
    KEY_TTL = 600.0

    # ---- 小工具
    @staticmethod
    def _path_word(url: str) -> str:
        segs = [s for s in urlparse(url).path.split("/") if s]
        if len(segs) >= 2 and segs[0] == "comic":
            return segs[1]
        return segs[-1] if segs else ""

    @staticmethod
    def _var(html: str, name: str) -> str:
        """抠页面里的 `var <name> = '...'`（单双引号都认）。"""
        m = re.search(r"var\s+" + name + r"\s*=\s*'([^']*)'", html)
        if m:
            return m.group(1)
        m = re.search(r'var\s+' + name + r'\s*=\s*"([^"]*)"', html)
        return m.group(1) if m else ""

    @staticmethod
    def _dnt(html: str) -> str:
        m = re.search(r'id="dnt"[^>]*value="([^"]*)"', html)
        return m.group(1) if m else ""

    @staticmethod
    def _status(text: str) -> str:
        st = (text or "").lower()
        if any(k in st for k in ("完结", "完結", "已完", "completed", "finished")):
            return "finished"
        if any(k in st for k in ("连载", "連載", "ongoing", "serializing")):
            return "ongoing"
        return "unknown"

    @staticmethod
    def _decrypt(blob: str, key: str) -> str | None:
        try:
            return decrypt_page_blob(blob, key)
        except Exception as exc:                    # noqa: BLE001 - 解不开就是改版/限流
            log.warning("拷貝漫畫密文解密失败：%s", exc)
            return None

    def _remember(self, detail_url: str, html: str) -> None:
        ccz, dnt = self._var(html, "ccz"), self._dnt(html)
        if ccz and dnt:
            _COPY_KEYS[urlparse(detail_url).hostname or ""] = (time.time(), ccz, dnt)

    async def _key(self, detail_url: str, force: bool = False) -> tuple[str, str]:
        """取 (ccz, dnt)。优先用缓存，缓存没有或强制刷新时才抓详情页。"""
        host = urlparse(detail_url).hostname or ""
        hit = _COPY_KEYS.get(host)
        if not force and hit and time.time() - hit[0] < self.KEY_TTL:
            return hit[1], hit[2]
        html = await self.fetch(detail_url)
        self._remember(detail_url, html)
        hit = _COPY_KEYS.get(host)
        if not hit:
            raise ParseError("拷貝漫畫详情页里没有找到解密参数（站点可能改版了）")
        return hit[1], hit[2]

    # ---- 解析
    async def parse_comic(self, url: str) -> Comic:
        html = await self.fetch(url)
        self._remember(url, html)
        from lxml import html as LH
        from .sites import Selector
        doc = LH.fromstring(html)

        def one(sel: str, attr: str = "", *, is_url: bool = False) -> str:
            # is_url=False 时必须显式写 "url": false，否则「海贼王」会被
            # urljoin 拼成 https://站点/海贼王（见 Selector._transform 的注释）。
            spec: dict[str, Any] = {"css": sel}
            if attr:
                spec["attr"] = attr
                if not is_url:
                    spec["url"] = False
            else:
                spec["text"] = True
            r = Selector(spec).find_all(doc, url)
            return r[0].strip() if r else ""

        c = Comic(title="", url=url, site=self.site.id, site_name=self.site.name)
        c.title = one(".comicParticulars-title-right h6", "title") \
            or one(".comicParticulars-title-right h6") or one("title")
        c.cover = one(".comicParticulars-left-img img", "data-src", is_url=True)
        c.author = one('.comicParticulars-title-right a[href^="/author/"]')
        c.description = one("p.intro")
        tags = Selector({"css": "span.comicParticulars-left-theme-all a",
                         "text": True, "all": True}).find_all(doc, url)
        c.tags = [t.lstrip("#").strip() for t in tags if t.strip()]
        m = re.search(r"狀態：</span>\s*<span[^>]*>([^<]*)<", html)
        c.status = self._status(m.group(1) if m else "")
        c.cid = self._path_word(url)
        return c

    async def _chapters_blob(self, pw: str, dnt: str, detail_url: str) -> str:
        api = f"{self.BASE}/comicdetail/{pw}/chapters"
        r = await self.http.get(api, headers={
            "dnts": dnt,
            "Referer": detail_url,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }, kind="html")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as exc:
            raise ParseError(f"拷貝漫畫章节接口返回的不是 JSON：{exc}") from exc
        code = data.get("code")
        if code != 200:
            raise ParseError(f"拷貝漫畫章节接口返回 {code}：{data.get('message') or ''}")
        blob = data.get("results")
        if not isinstance(blob, str) or len(blob) < 32:
            raise ParseError("拷貝漫畫章节接口没有返回密文")
        return blob

    async def parse_chapters(self, url: str, full: bool = False) -> list[Chapter]:
        pw = self._path_word(url)
        if not pw:
            raise ParseError("拷貝漫畫链接里没有漫画编号")
        detail_url = f"{self.BASE}/comic/{pw}"
        ccz, dnt = await self._key(detail_url)
        plain = self._decrypt(await self._chapters_blob(pw, dnt, detail_url), ccz)
        if plain is None:                            # 密钥轮换了，强制重抓一次
            ccz, dnt = await self._key(detail_url, force=True)
            plain = self._decrypt(await self._chapters_blob(pw, dnt, detail_url), ccz)
        if plain is None:
            raise ParseError("拷貝漫畫章节列表解密失败（站点可能改版，或正在限流）")
        try:
            data = json.loads(plain)
        except ValueError as exc:
            raise ParseError(f"拷貝漫畫章节列表不是合法 JSON：{exc}") from exc
        items: list[Any] = []
        for g in (data.get("groups") or {}).values():
            if isinstance(g, dict):
                items.extend(g.get("chapters") or [])
        out: list[Chapter] = []
        seen: set[str] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("id") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            title = str(it.get("name") or "").strip() or cid
            out.append(Chapter(cid=cid, title=title,
                               url=f"{self.BASE}/comic/{pw}/chapter/{cid}"))
        if not out:
            # 空列表有两种可能：这本确实没章节，或者 IP 被限流（站点不报错，
            # 只是安静返回 count=0）。详情页有开始阅读链接就说明是后者。
            html = await self.fetch(detail_url)
            self._remember(detail_url, html)
            if "/chapter/" in html:
                raise ParseError("拷貝漫畫没有返回任何章节：站点在限流（换个网络或等一小时再试）")
            return []
        for i, ch in enumerate(out):
            ch.index = i
        return out

    async def parse_pages(self, url: str) -> list[str]:
        html = await self.fetch(url)
        cct, ck = self._var(html, "cct"), self._var(html, "contentKey")
        if not cct:
            raise ParseError("拷貝漫畫章节页里没有找到解密参数（站点可能改版了）")
        if not ck:
            raise ParseError("拷貝漫畫这一话没有给出图片（可能需要登录，或站点在限流）")
        plain = self._decrypt(ck, cct)
        if plain is None:
            raise ParseError("拷貝漫畫章节图片解密失败（站点可能改版，或正在限流）")
        try:
            items = json.loads(plain)
        except ValueError as exc:
            raise ParseError(f"拷貝漫畫章节图片不是合法 JSON：{exc}") from exc
        urls: list[str] = []
        seen: set[str] = set()
        for it in items if isinstance(items, list) else []:
            raw = it.get("url") if isinstance(it, dict) else (it if isinstance(it, str) else "")
            u = normalize_url(url, str(raw or "").strip())
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
        if not urls:
            raise ParseError("拷貝漫畫这一话没有解析到图片")
        return urls

    async def search(self, keyword: str) -> list[Comic]:
        q = (keyword or "").strip()
        if not q:
            return []
        out: list[Comic] = []
        offset = 0
        while len(out) < self.MAX_RESULTS:
            data = await self.http.get_json(self.SEARCH_API, params={
                "offset": offset, "platform": 2, "limit": self.PAGE_SIZE,
                "q": q, "q_type": "",
            }, headers={"Accept": "application/json"})
            res = (data or {}).get("results") or {}
            batch = res.get("list") or []
            total = int(res.get("total") or 0)
            for it in batch:
                if not isinstance(it, dict):
                    continue
                pw = str(it.get("path_word") or "").strip()
                if not pw:
                    continue
                authors = [str(a.get("name") or "").strip()
                           for a in (it.get("author") or []) if isinstance(a, dict)]
                out.append(Comic(
                    title=str(it.get("name") or "").strip() or pw,
                    url=f"{self.BASE}/comic/{pw}",
                    site=self.site.id, site_name=self.site.name,
                    cid=pw,
                    author=", ".join([n for n in authors if n]),
                    cover=str(it.get("cover") or ""),
                ))
            offset += self.PAGE_SIZE
            if not batch or offset >= total:
                break
        return out[:self.MAX_RESULTS]

    async def cover(self, url: str) -> bytes:
        data, _ = await self.http.download(
            url, headers=self.site.headers, cookies=self.site.cookies or None
        )
        return data


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
    reg.sites.setdefault("copy4000", Site(
        id="copy4000", name="拷貝漫畫",
        domains=["copy4000.com", "copy5000.com"],
        search_url=Copy4000Adapter.SEARCH_API,
    ))
    for cls in (MStationAdapter, KuaikanAdapter, KZAdapter, Copy4000Adapter):
        reg.register_adapter(cls)
