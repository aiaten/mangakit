"""自定义站点探测：贴一个漫画详情页网址，自动判断能不能搜、能不能下。

用户在「设置 → 站点管理 → 添加网站」里贴进来的往往只是一个网址，要让程序
自己回答三个问题：

1. **这是哪个站？**（域名 → 站名）
2. **章节能不能认出来？**（详情页里的章节链接 + 章节页里的图片）
3. **能不能真的下下来？**（抽 1～2 张图实际 GET 一次，看是不是 403/防盗链）

三条都过了才允许添加 —— 只认域名、不做实测的话，加进来的站十有八九是
「能搜到、点进去没有图」的半残状态，比不加更糟。

探测结果分三档：
- ``ok``：能拿到章节且能下到图 → 可添加
- ``partial``：能拿到章节但图片下不动（防盗链/需登录）→ 默认不建议添加
- ``fail``：连章节都拿不到 → 拒绝添加

探测出来的规则直接序列化成 MangaKit 的规则 JSON（css 选择器版本），存进
``config["user_rules"]``，重启后由 :meth:`SiteRegistry._load_rules` 自动装载。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from .http_client import HttpClient
from .logsetup import get as _get_log
from .sites import _as_html, normalize_url

log = _get_log("probe")

# 章节链接最常见的三种形态。命中任一即视为「这条 a 标签指向一个章节」。
_CHAPTER_HINTS = (
    re.compile(r"/(?:chapter|chap|read|view|comic|mh|go|episode|ep)/", re.I),
    re.compile(r"/\d+[-_]\d+\.html?$", re.I),
    re.compile(r"(?:chapter|chap|episode|id|cid)=\d+", re.I),
)
# 反过来，这些明显不是章节，先排掉再说。
_NOT_CHAPTER = re.compile(
    r"(?:javascript:|mailto:|#|\.(?:jpg|jpeg|png|webp|gif|svg|css|js|ico)(?:\?|$))", re.I
)
# 图片链接：有些站拿 data-src / data-original 懒加载，得连属性一起看。
_IMG_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-echo", "src")
_IMG_URL = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp|gif|bmp)(?:\?[^\s\"'<>]*)?", re.I)


def _err(e: BaseException) -> str:
    """把 httpx 异常说成人话。

    httpx 的 ConnectError/ConnectTimeout 在自身没有 message 时 ``str()`` 是空串，
    直接拿去拼提示会得到「打不开这个页面：」这种半截话。这里退回到类名，
    并把底层 cause 一并带出来（SSL 失败、DNS 失败都藏在那儿）。
    """
    msg = str(e).strip()
    if not msg:
        cause = e.__cause__ or e.__context__
        while cause is not None and not str(cause).strip():
            cause = cause.__cause__ or cause.__context__
        msg = str(cause).strip() if cause is not None else ""
    name = type(e).__name__
    if "Timeout" in name:
        return "连接超时，站点太慢或被墙"
    if "SSL" in name or "CERTIFICATE" in msg.upper():
        return "HTTPS 证书校验失败"
    if "ConnectError" in name and not msg:
        return "连不上（域名解析失败或站点已关停）"
    return f"{name}: {msg}" if msg else name


@dataclass
class ProbeResult:
    """一次探测的结论。"""

    url: str = ""
    host: str = ""
    site_name: str = ""
    status: str = "fail"          # ok | partial | fail
    can_search: bool = False
    search_url: str = ""
    chapter_count: int = 0
    image_count: int = 0
    title: str = ""
    cover: str = ""
    rule: dict[str, Any] = field(default_factory=dict)
    ok_urls: list[str] = field(default_factory=list)
    tried_urls: list[str] = field(default_factory=list)
    reason: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def addable(self) -> bool:
        """能不能加进站点列表。只有真正下得动图的站才让加。"""
        return self.status == "ok"

    def summary(self) -> str:
        lines = [
            f"站点：{self.site_name or self.host}",
            f"域名：{self.host}",
            f"漫画：{self.title or '（未识别）'}",
            f"章节：{self.chapter_count} 话",
            f"图片：{self.image_count} 张（实测 {len(self.ok_urls)}/{len(self.tried_urls)} 张可下载）",
            f"搜索：{'支持' if self.can_search else '不支持'}",
        ]
        return "\n".join(lines)


class SiteProber:
    """探测一个漫画详情页，尽量推导出可用的站点规则。"""

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http
        self._own = http is None

    async def __aenter__(self) -> "SiteProber":
        if self._http is None:
            self._http = await HttpClient().__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._own and self._http is not None:
            await self._http.__aexit__(*exc)
        self._http = None

    # ---- 主流程 ----
    async def probe(self, url: str, *, deep: bool = True) -> ProbeResult:
        """探测入口。deep=True 时会真的去拉一张图验证能不能下。"""
        r = ProbeResult(url=url)
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url.lstrip("/")
        host = (urlparse(url).hostname or "").lower()
        r.url, r.host = url, host
        if not host:
            r.reason = "网址格式不对，请贴一个完整的漫画详情页地址"
            return r

        try:
            html = await self._fetch(url)
        except Exception as e:
            r.reason = f"打不开这个页面：{_err(e)}"
            r.log.append(r.reason)
            return r

        r.site_name = self._guess_site_name(html, host)
        r.title = self._guess_title(html)
        r.cover = self._guess_cover(html, url)
        r.log.append(f"页面已打开，识别站点「{r.site_name}」")

        chapters = self._guess_chapters(html, url)
        r.chapter_count = len(chapters)
        r.log.append(f"从详情页找到 {len(chapters)} 个候选章节链接")
        if not chapters:
            r.reason = (
                "这个页面上找不到章节链接。可能它不是一个漫画详情页"
                "（比如是首页/列表页），或者章节靠 JS 异步加载。"
            )
            return r

        # 找一话「真能解出正文图」的章节来验证。
        # 不能只看头几话：dm5 最新的若干话是 VIP 锁的，页面里一张真图都没有，
        # 而老话（越往后越靠前）是免费的、图全在。只探前 3 话会误判成「这站不行」。
        target = chapters[0]
        pages: list[str] = []
        tried_ch = 0
        # 从后往前探（老话通常免费），再补几话靠前的，覆盖两种排版。
        order = list(reversed(chapters[-6:])) + chapters[:4]
        for cand in order:
            tried_ch += 1
            try:
                pages = await self._guess_pages(cand)
            except Exception as e:
                r.log.append(f"章节页打开失败：{_err(e)}")
                continue
            if pages:
                target = cand
                break
            if tried_ch >= 8:
                break
        r.image_count = len(pages)
        r.log.append(f"试了 {tried_ch} 话，章节页「{target}」解析出 {len(pages)} 张图片")
        if not pages:
            if self._looks_js_loaded(html, pages):
                r.reason = (
                    "试了多话都拿不到正文图片：这个站的图由 JavaScript 在浏览器里"
                    "现拼（常带服务端签名），HTML 源码里只有广告和图标。"
                    "这类站需要写专门适配器，暂时无法通过「添加网站」自动支持。"
                )
            else:
                r.reason = (
                    "试着打开多话都拿不到正文图片。"
                    "可能是整站需要登录/付费，或图片地址做了加密。"
                )
            r.status = "fail"
            return r

        # 真的下一张试试 —— 这一步才叫「能下载」。
        ok: list[str] = []
        tried: list[str] = []
        if deep:
            for u in pages[:3]:
                tried.append(u)
                try:
                    data = await self._fetch_bytes(u, referer=target)
                except Exception as e:
                    r.log.append(f"图片失败 {_err(e)}")
                    continue
                if data and len(data) >= 1024:
                    ok.append(u)
            r.ok_urls, r.tried_urls = ok, tried
            r.log.append(f"实测 {len(ok)}/{len(tried)} 张图片可下载")
            if not ok:
                r.status = "partial"
                r.reason = (
                    "章节和图片都能解析出来，但图片实际下不动（403/防盗链，"
                    "或需要登录 Cookie）。这类站加进来也是「能搜不能下」，"
                    "所以默认不添加。"
                )
                return r
        else:
            r.ok_urls, r.tried_urls = pages[:1], pages[:1]

        # 搜索：试一次常见的搜索入口，能搜到东西才点亮「支持搜索」。
        r.search_url, r.can_search = await self._probe_search(html, url, host)
        r.log.append(f"搜索入口：{r.search_url or '未找到'}（{'可用' if r.can_search else '不可用'}）")

        # 生成规则，并**回头用规则验证一遍**。
        # 这一步不能省：探测阶段是我自己写的宽松逻辑在认章节，规则阶段要交给
        # 引擎的 Selector 去认。两者不等价 —— 曾出现探测说「62 话、图能下」，
        # 生成的 url_re 在真实页面上却匹配 0 条，装进引擎一章都解不出来。
        r.rule = self._build_rule(r, chapters, target, pages, html)
        verified, why = await self._verify_rule(r.rule, url, len(chapters), len(pages))
        r.log.append(f"规则自检：{why}")
        if not verified:
            r.status = "fail"
            r.reason = f"能解析页面，但生成的规则在引擎里跑不通（{why}）。为避免加进来是个死站，不予添加。"
            return r

        r.status = "ok"
        return r

    async def _verify_rule(
        self, rule: dict, url: str, want_chapters: int, want_pages: int
    ) -> tuple[bool, str]:
        """把刚生成的规则放进真实引擎跑一遍，确认它解得出章节和图片。

        宁可在这里判定失败（用户看到「解析不了，不添加」），也不要写一条
        看起来能用的规则进去 —— 用户加完站、兴冲冲点开漫画却是空的，
        比一开始就拒绝糟糕得多。
        """
        try:
            from .sites import RuleAdapter, Site

            site = Site(
                id=str(rule.get("id") or "probe"),
                name=str(rule.get("name") or ""),
                domains=list(rule.get("domains") or []),
                rule="<user>", adapter=None, enabled=True, lang="cn",
                note="", headers={}, cookies={},
                search_url=str(rule.get("search_url") or ""),
                payload=rule, hidden=False,
            )
            assert self._http is not None
            ad = RuleAdapter(site, self._http, rule)
            chs = await ad.parse_chapters(url, full=False)
            if not chs:
                return False, f"引擎按规则解析出 0 话（探测时找到 {want_chapters} 个候选）"
            # 章节数差太多说明 url_re 把推荐位也放进来、或漏了大半。
            if want_chapters and len(chs) < max(1, want_chapters * 0.5):
                return False, f"引擎只解析出 {len(chs)} 话，远少于探测到的 {want_chapters} 个"
            imgs = await ad.parse_pages(chs[0].url)
            if not imgs:
                return False, "引擎按规则解析章节页得到 0 张图"
            return True, f"引擎可解析 {len(chs)} 话、首话 {len(imgs)} 张图"
        except Exception as e:
            return False, f"规则自检异常：{type(e).__name__}: {e}"

    # ---- 抓取辅助 ----
    async def _fetch(self, url: str) -> str:
        assert self._http is not None
        resp = await self._http.request("GET", url, kind="html")
        return resp.text

    async def _fetch_bytes(self, url: str, *, referer: str = "") -> bytes:
        assert self._http is not None
        headers = {"Referer": referer} if referer else None
        resp = await self._http.request("GET", url, kind="image", headers=headers)
        return resp.content

    # ---- 识别 ----
    @staticmethod
    def _guess_site_name(html: str, host: str) -> str:
        for pat in (
            r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
            r"<title[^>]*>([^<]+)</title>",
        ):
            m = re.search(pat, html, re.I)
            if m:
                t = re.sub(r"\s+", " ", m.group(1)).strip()
                # 标题常见「漫画名 - 站名」或「漫画名_站名」，取最后一段当站名。
                parts = [x.strip() for x in re.split(r"[_\-|｜–—]", t) if x.strip()]
                if len(parts) >= 2 and len(parts[-1]) <= 20:
                    return parts[-1]
                if t and len(t) <= 20:
                    return t
        return host

    @staticmethod
    def _guess_title(html: str) -> str:
        """认漫画名。

        ``<h1>`` 不能无脑信：很多站的导航栏、登录框也用 h1，dm5 上第一个 h1
        就是「登录」，白拿一个错名字。所以这里先筛掉明显是界面文字的 h1。
        """
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
        if m:
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if t:
                return t.split("_")[0].split("-")[0].strip()[:80]
        ui_words = re.compile(r"^(登录|注册|首页|搜索|会员|排行|分类|全部|漫画大全|导航)$")
        for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S):
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if t and 2 <= len(t) <= 80 and not ui_words.match(t):
                return t[:80]
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            if t:
                return t.split("_")[0].split("-")[0].strip()[:80]
        return ""

    @staticmethod
    def _guess_cover(html: str, base: str) -> str:
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I
        )
        if m:
            return normalize_url(base, m.group(1))
        return ""

    def _guess_chapters(self, html: str, base: str) -> list[str]:
        """从详情页里挑出所有像章节的链接。"""
        doc = self._doc(html)
        out: list[str] = []
        seen: set[str] = set()
        for a in doc.iter("a"):
            href = (a.get("href") or "").strip()
            text = " ".join((a.text_content() or "").split())
            if not href or _NOT_CHAPTER.search(href):
                continue
            full = normalize_url(base, href)
            if full in seen:
                continue
            # 三重命中任一即收：URL 形态像章节、链接文字像章节、或它带话数。
            hit = any(p.search(full) for p in _CHAPTER_HINTS) or bool(
                re.search(r"第\s*\d+\s*[话話章回卷]|^\s*\d+\s*$", text)
            )
            if hit:
                seen.add(full)
                out.append(full)
        # 有「最新话在最前」的站，按页面顺序保留即可。
        return out

    def _guess_pages(self, chapter_url: str) -> Any:  # -> list[str]
        """打开章节页，把里面的大图地址全抓出来（异步）。"""
        return self._guess_pages_async(chapter_url)

    async def _guess_pages_async(self, chapter_url: str) -> list[str]:
        html = await self._fetch(chapter_url)
        doc = self._doc(html)
        urls: list[str] = []
        seen: set[str] = set()

        def push(raw: str) -> None:
            if not raw:
                return
            u = normalize_url(chapter_url, raw.strip())
            if not re.match(r"^https?://", u, re.I) or u in seen:
                return
            if _NOT_CHAPTER.search(u) and not _IMG_URL.search(u):
                return
            seen.add(u)
            urls.append(u)

        for img in doc.iter("img"):
            for attr in _IMG_ATTRS:
                v = img.get(attr)
                if v and not v.startswith("data:image"):
                    push(v)
                    break
        # 有些站把图塞在 <a> 或背景里，再兜一遍正则。
        for m in _IMG_URL.finditer(html):
            push(m.group(0))
        return self._filter_page_images(urls, html)

    @staticmethod
    def _filter_page_images(urls: list[str], html: str) -> list[str]:
        """把站徽、广告、二维码这类非正文图剔掉，只留章节正文图。

        这一步比看上去重要。dm5 那种站的真实章节图是 JS 现拼的，HTML 里的
        ``<img>`` 全是头像、二维码、广告和「相关漫画」缩略图；如果不过滤，
        会得到「解析出 9 张图」的假结论，用户加完站才发现下下来全是广告图。
        所以宁可判成「没有正文图」，也不要让杂图冒充成功。

        判定标准是「**扎堆**」而不是「有几张」：真章节页的正文图一定是一串
        同构的图（同一个图床、同样的路径形态），而装饰图是零散、各不相同的。
        """
        noise = re.compile(
            r"(?:logo|banner|advert|ads?/|avatar|icon|qrcode|qr_|weixin|weibo|"
            r"loading|placeholder|blank|spacer|bg\.|background|"
            r"mrtx|tz\.|tips?/|warning|notice|"
            r"down-|download|view_|view-|cross_|gaicp|"
            r"account-|facebook|twitter|instagram|"
            r"/images?/[a-z0-9_\-]+\.(?:gif|png|jpe?g|webp)$)",
            re.I,
        )
        kept = [u for u in urls if not noise.search(u)]
        # 小尺寸缩略图（_130x174_ 这种）是「相关漫画」推荐位，不是正文页；
        # ``_c.jpg`` 同源，是封面缩略图的另一种拼法。
        kept = [
            u
            for u in kept
            if not re.search(r"_\d{2,4}x\d{2,4}_", u)
            and not re.search(r"_c\.(?:jpe?g|png|webp)(?:\?|$)", u, re.I)
        ]
        # 正文页必须是「一串」。按 host 分组后，只有成串（≥3 张）的那组才可能是
        # 正文；零散的 1~2 张一律视为装饰图。这一条专门用来挡住「恰好凑了 3 张
        # 但来自 3 个不同图床」这种假阳性。
        from collections import Counter

        hosts = Counter(urlparse(u).hostname or "" for u in kept)
        if not hosts:
            return []
        top, n = hosts.most_common(1)[0]
        if n < 3:
            return []
        kept = [u for u in kept if (urlparse(u).hostname or "") == top]
        # 真章节页至少有若干张同构的图。
        if len(kept) < 3:
            return []
        return kept

    @staticmethod
    def _declared_page_count(html: str) -> int:
        """页面自己声明的本章页数（dm5 的 ``DM5_IMAGE_COUNT`` 等）。

        用来交叉验证：页面说本章有 68 页，而我们一张正文图都没抓到，
        那就不是「站点没有正文图」，而是「图在 JS 里」——必须拒绝添加。
        """
        pats = (
            r"var\s+DM5_IMAGE_COUNT\s*=\s*(\d+)",
            r"[\"']?(?:imageCount|imgCount|picCount|pageCount|totalPage)[\"']?\s*[:=]\s*(\d+)",
            r"共\s*(\d+)\s*[页Pp]",
        )
        for p in pats:
            m = re.search(p, html, re.I)
            if m:
                try:
                    v = int(m.group(1))
                except ValueError:
                    continue
                if v > 0:
                    return v
        return 0

    def _looks_js_loaded(self, html: str, pages: list[str]) -> bool:
        """页面里几乎没有正文图、却带着一段章节脚本 —— 判定为 JS 加载。

        这类站（dm5 是典型）的图片地址由 JS 在客户端拼出来，往往还带服务端
        签名，纯规则抓不到。识别出来才好给用户一句实话，而不是让他加完才发现。

        两种依据：① 一张正文图都没有但带了章节脚本；② 页面**自己声明**了
        本章页数，而 HTML 里一张都没有 —— 后者比脚本关键字可靠得多。
        """
        if pages:
            return False
        declared = self._declared_page_count(html)
        if declared >= 3:
            return True
        has_script = bool(
            re.search(r"(?:chapter|comic|read|view)[\w-]*\.js", html, re.I)
            or re.search(r"\$\.(?:ajax|get|post)\s*\(", html)
        )
        return has_script and len(html) < 200_000

    async def _probe_search(self, html: str, url: str, host: str) -> tuple[str, bool]:
        """找站内搜索入口并验证它真能返回结果。

        先看页面上的 <form>，再退回到几种常见路径。验证方式是拿站点自己的
        名字当关键词搜一次，能搜出链接就算可用 —— 只写个 URL 不验证的话，
        用户会得到一个「点了搜索没结果」的死入口。
        """
        cands: list[str] = []
        doc = self._doc(html)
        # 表单里藏着一堆 name（错误提示、跳转地址、防爬 token），肉眼挑不出哪个
        # 才是搜索框。这里只认「用户能打字进去的那个框」，并且排除掉明显是
        # 杂项的名字，否则会生成 dm5 上 ?txt_erroremail={q} 这种废入口。
        junk = re.compile(
            r"(?:error|errmsg|msg|token|csrf|verify|code|captcha|redirect|url|"
            r"return|back|submit|button|hidden|remember|password|pwd|email|phone)",
            re.I,
        )
        good_hint = re.compile(r"(?:^|_)(?:q|s|wd|key|keyword|query|search|name|title|word)$", re.I)
        for form in doc.iter("form"):
            action = (form.get("action") or "").strip()
            method = (form.get("method") or "get").lower()
            if method != "get":
                continue
            names = []
            for i in form.iter("input"):
                n = (i.get("name") or "").strip()
                if not n:
                    continue
                typ = (i.get("type") or "text").lower()
                if typ == "hidden" or junk.search(n):
                    continue
                if typ in ("text", "search") or good_hint.search(n):
                    names.append(n)
            if not names:
                continue
            # 有搜索语义的名字优先（q/keyword/wd…），其次才按出现顺序取。
            names.sort(key=lambda n: 0 if good_hint.search(n) else 1)
            q = names[0]
            base = normalize_url(url, action or url)
            sep = "&" if "?" in base else "?"
            # action 里若有占位符（如 /search/{q}），直接替换而不是再挂参数。
            if "{q}" in base or "%s" in base:
                cands.append(base.replace("{q}", "{q}").replace("%s", "{q}"))
            else:
                cands.append(f"{base}{sep}{q}={{q}}")
        # 常见兜底路径
        root = f"{urlparse(url).scheme}://{host}/"
        for p in ("search?keyword={q}", "search?q={q}", "search?key={q}",
                  "search?query={q}", "search?wd={q}", "search/{q}", "?s={q}"):
            cands.append(urljoin(root, p))

        keyword = self._guess_keyword(html)
        if not keyword:
            return (cands[0] if cands else "", False)
        for tpl in cands[:6]:
            try:
                got = await self._fetch(tpl.replace("{q}", _quote(keyword)))
            except Exception:
                continue
            if len(got) < 2000:
                continue
            # 必须真搜出「指向详情页的链接」，不能只是首页回显。
            hrefs = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', got)
            entity = re.compile(r"/(?:manhua|comic|book|manga|detail)[-_/]", re.I)
            if sum(1 for h in hrefs if entity.search(h)) >= 2:
                return (tpl, True)
        return (cands[0] if cands else "", False)

    def _guess_keyword(self, html: str) -> str:
        """拿站点自己的漫画名当搜索词 —— 它一定能搜出东西。"""
        t = self._guess_title(html)
        t = re.sub(r"[\s_\-|｜]+", "", t)
        # 标题常带后缀（漫画_连载中_在线漫画），截前段当关键词更准。
        t = re.split(r"(?:漫画|在线|连载|全集|下拉)", t)[0] or t
        return t[:12]

    @staticmethod
    def _doc(html: str):
        from lxml import html as LH

        return LH.fromstring(_as_html(html))

    # ---- 生成规则 ----
    @staticmethod
    def _build_rule(
        r: ProbeResult, chapters: list[str], target: str, pages: list[str], html: str = ""
    ) -> dict:
        """把探测结论固化成一条 MangaKit 规则。"""
        chapter_re = _common_suffix([_path_of(c) for c in chapters])
        image_re = _image_regex(pages)
        sid = re.sub(r"[^a-z0-9]+", "", r.host.replace("www.", ""))[:20] or "custom"
        rule: dict[str, Any] = {
            "id": sid,
            "name": r.site_name or r.host,
            "lang": "cn",
            "note": f"由「添加网站」自动探测生成（{r.host}），实测可下载 {len(r.ok_urls)} 张图。"
                    f"若站点改版导致失效，可编辑或删除本条规则。",
            "domains": [r.host] + ([r.host[4:]] if r.host.startswith("www.") else []),
            "comic": {},
            "chapters": {
                # 详情页上「像章节」的链接 —— 用 URL 正则限定，避免把推荐位收进来。
                "list": {"css": "a[href]"},
            },
            "pages": {
                "list": {"css": "img"},
            },
        }
        title_sel = _pick_title_selector(html, r.title)
        if title_sel:
            rule["comic"]["title"] = title_sel
        if r.cover:
            rule["comic"]["cover"] = {"css": "meta[property='og:image']", "attr": "content"}
        if chapter_re:
            # 锚定到结尾，把「/comic/12」这类推荐链接挡在外面。
            rule["chapters"]["url_re"] = chapter_re
        # 章节排序键：引擎默认按 URL 字典序排，dm5 的 /m1839988/ 会因此乱序
        # （最新的 id 最大却排最后）。抓取它们的数字段当序号。
        order_key = _chapter_order_hint(chapters)
        if order_key:
            rule["chapters"]["order"] = order_key
        if image_re:
            rule["pages"]["filter"] = image_re
        rule["pages"]["url"] = {"attr": "data-src"}
        if r.can_search and r.search_url:
            rule["search_url"] = r.search_url
            rule["search"] = {"url": r.search_url}
        return rule


# 详情页上放漫画名的候选选择器，按「越具体越优先」排。
_TITLE_CANDIDATES: list[dict[str, Any]] = [
    {"css": "meta[property='og:title']", "attr": "content", "url": False},
    {"css": "h1.book-title", "text": True},
    {"css": ".book-title h1", "text": True},
    {"css": ".comic-title", "text": True},
    {"css": ".detail-title h1", "text": True},
    {"css": "#book_name", "text": True},
    {"css": "h1", "text": True},
]
# 明显是界面文字的标题，不能当漫画名。
_UI_TITLE = re.compile(
    r"^(?:登录|注册|首页|搜索|会员|排行|分类|全部|漫画大全|导航|个人中心|用户中心|"
    r"sign\s*in|log\s*in|register|home|search)$",
    re.I,
)


def _pick_title_selector(html: str, want: str) -> dict[str, Any]:
    """选一个真能取到漫画名的选择器。

    不能无脑写 ``{"css": "h1"}``：dm5 详情页上第一个 h1 是导航栏里的「登录」，
    规则装进引擎后漫画名就成了「登录」。这里逐个候选试，取第一个既非空、
    又不像界面文字的结果；取不到就干脆不写 title（引擎会退回用 URL 末段命名）。
    """
    if not html:
        return {}
    try:
        from .sites import Selector

        doc = SiteProber._doc(html)
        best: dict[str, Any] = {}
        for sel in _TITLE_CANDIDATES:
            try:
                vals = Selector(sel).find_all(doc, "")
            except Exception:
                continue
            for v in vals:
                t = " ".join(str(v).split()).strip()
                if not t or _UI_TITLE.match(t):
                    continue
                # 命中 og:title 这类直接信；其余要跟探测出的标题对得上一点。
                if sel.get("attr") or not want or _similar(t, want):
                    return sel
                if not best:
                    best = sel
        return best
    except Exception:
        return {}


def _similar(a: str, b: str) -> bool:
    """两个标题是否「大致是同一个东西」（去符号后有一段明显重叠）。"""
    ca = re.sub(r"[\s_\-|｜–—:：]+", "", a)
    cb = re.sub(r"[\s_\-|｜–—:：]+", "", b)
    if not ca or not cb:
        return False
    if ca in cb or cb in ca:
        return True
    # 取较短串的前 4 个字看看在不在长串里，挡住「第1话」这类不同实体。
    head = ca[:4] if len(ca) >= 4 else ca
    return len(head) >= 2 and head in cb


def _path_of(u: str) -> str:
    p = urlparse(u)
    return p.path + (("?" + p.query) if p.query else "")


def _chapter_order_hint(chapters: list[str]) -> str:
    """给 chapters.order 生成一条「取出章节序号」的正则（捕获组 1 = 序号）。

    引擎默认按 URL 字典序排。章节 URL 里若是「越新 id 越大」的纯数字
    （dm5 的 ``/m1839988/``），字典序会把最新话排到最后，还会把
    ``/m999/`` 排到 ``/m1839988/`` 后面 —— 用户看到的目录是乱的。
    这里把 URL 里的数字段抓出来当排序键。

    只有当「数字段在样本里各不相同」时才给，否则（比如 ``/chapter/1`` 之外
    还混着 ``/comic/999``）会抓错，宁可让引擎按默认字典序排。
    """
    if len(chapters) < 3:
        return ""
    # 取路径里的数字段。优先「最后一段里的最长数字串」。
    cands: list[str] = []
    for u in chapters:
        path = urlparse(u).path.rstrip("/")
        nums = re.findall(r"\d+", path)
        cands.append(nums[-1] if nums else "")
    if not all(cands) or len(set(cands)) < max(2, len(cands) * 0.8):
        return ""
    # 数字还必须「顺序相关」——即按数字大小排序与页面出现顺序基本一致，
    # 或者完全相反；否则这个数字多半不是话号（比如是域名里的端口）。
    asc = sum(1 for a, b in zip(cands, cands[1:]) if int(a) <= int(b))
    if asc not in (0, len(cands) - 1):
        # 页面顺序既不递增也不递减，说明这数字不是话号，别乱排。
        if abs(asc - (len(cands) - 1) / 2) > len(cands) * 0.35:
            return ""
    # 生成正则：抓路径最后一段里的数字串。
    return r"(\d+)(?=[^\d]*/?$)"


def _common_suffix(paths: list[str]) -> str:
    """从**实际抓到的章节 URL**里反推一条能匹配它们的正则。

    这里不能靠枚举常见形态：dm5 的章节是 ``/m1839988/``，既不是 ``/chapter/123``
    也不是 ``/123.html``，枚举法一个都命中不了，最后退化成的兜底正则
    ``/\\d+(?:\\.html?|/)?$`` 在真实页面上匹配数为 **0** —— 规则看着齐全，
    装进引擎却一章都解不出来。

    正确做法是拿观察到的样本当依据：把这些路径里「变动的那一段」抽象成
    ``\\d+``，保留共同的前后缀，再回头验证这条正则能覆盖多少样本。
    """
    if not paths:
        return ""
    paths = [p for p in paths if p]
    if not paths:
        return ""

    # 先试枚举常见的几种写法，覆盖率高就用它（正则最干净、可读性最好）。
    pats: list[re.Pattern[str]] = [
        re.compile(r"/chapter/\d+\.html?$", re.I),
        re.compile(r"/\d+[-_]\d+\.html?$", re.I),
        re.compile(r"/read/\d+/\d+/?$", re.I),
        re.compile(r"/\d+/\d+\.html?$", re.I),
        re.compile(r"(?:chapter|chap|cid|id)=\d+", re.I),
    ]
    for p in pats:
        n = sum(1 for x in paths if p.search(x))
        if n >= max(1, len(paths) * 0.6):
            return p.pattern

    # 枚举都不行 —— 从样本本身反推。先剥掉查询串，再按 '/' 切段。
    bare = [p.split("?")[0] for p in paths]
    segs = [b.strip("/").split("/") for b in bare]
    segs = [s for s in segs if s]
    if not segs:
        return ""
    width = min(len(s) for s in segs)
    if width == 0:
        return ""
    # 从尾部往前找第一个「会变」的段。dm5 的章节是 /m1839988/ —— 变的是
    # `m` + 数字，段本身不是纯数字，所以不能只认 isdigit()。
    from_tail = 1
    while from_tail <= width:
        col = {s[len(s) - from_tail] for s in segs if len(s) >= from_tail}
        if len(col) > 1:
            break
        from_tail += 1
    if from_tail > width:
        # 所有样本完全一样，没有可抽象的变动段。
        return re.escape("/" + "/".join(segs[0])) + "$"
    idx = from_tail  # 从尾部数第 idx 段就是变动段
    var = [s[len(s) - idx] for s in segs if len(s) >= idx]
    core = _segment_pattern(var)
    if core:
        # 尾部不变的段原样保留（dm5 那种末尾斜杠在 strip 时已经没了，这里补回）。
        tail = segs[0][len(segs[0]) - (from_tail - 1):] if from_tail > 1 else []
        head = segs[0][: len(segs[0]) - idx]
        pat = ""
        if head:
            pat += "/" + "/".join(re.escape(h) for h in head)
        pat += "/" + core
        if tail:
            pat += "/" + "/".join(re.escape(t) for t in tail)
        pat += "/?$"
        rx = re.compile(pat)
        covered = sum(1 for x in bare if rx.search(x))
        if covered >= max(1, len(paths) * 0.6):
            return pat
    # 退一步：只要「末尾这段含数字」就算章节。
    loose = re.compile(r"/[a-z]*\d+/?$", re.I)
    if sum(1 for x in bare if loose.search(x)) >= max(1, len(paths) * 0.6):
        return loose.pattern
    # 再退一步：末尾是一段「字母数字混排的短标识」也算（漫画1234 的 /go/MTQx
    # 是 base64 短链，不含任何数字，但成组出现就说明它是章节 id）。
    b64 = re.compile(r"/[A-Za-z0-9_-]{2,24}/?$")
    if sum(1 for x in bare if b64.search(x)) >= max(1, len(paths) * 0.6):
        return b64.pattern
    return r"/\d+/?$"


def _segment_pattern(vals: list[str]) -> str:
    """把一组「同类但不同值」的路径段抽象成一小段正则。

    ``['m1839988','m1809311']`` → ``m\\d+``；纯数字 → ``\\d+``；
    形如 ``12-34`` 的 → ``\\d+-\\d+``。推不出来就返回空串。
    """
    vals = [v for v in vals if v]
    if not vals:
        return ""
    # 共同前缀 / 后缀（逐字符比）。注意别把数字吃掉：dm5 的 id 都以 1 开头，
    # 逐字符比会得出「共同前缀 m1」，于是正则变成 /m1\d+/ —— 今天能匹配，
    # 明天来个 /m2xxxxxx/ 就漏了。所以共同前缀遇到数字就停。
    pre = vals[0]
    for v in vals[1:]:
        i = 0
        while i < len(pre) and i < len(v) and pre[i] == v[i]:
            i += 1
        pre = pre[:i]
        if not pre:
            break
    while pre and pre[-1].isdigit():
        pre = pre[:-1]
    suf = vals[0][len(pre):]
    for v in vals[1:]:
        body = v[len(pre):]
        i = 0
        while i < len(suf) and i < len(body) and suf[len(suf) - 1 - i] == body[len(body) - 1 - i]:
            i += 1
        suf = suf[len(suf) - i:]
        if not suf:
            break
    # 后缀同理：以数字开头的共同后缀也容易过拟合，去掉。
    while suf and suf[0].isdigit():
        suf = suf[1:]
    # 中间的变动部分必须是数字才有意义
    mids = [v[len(pre): len(v) - len(suf)] if suf else v[len(pre):] for v in vals]
    if not mids or not all(m.isdigit() for m in mids):
        return ""
    return f"{re.escape(pre)}\\d+{re.escape(suf)}"


def _image_regex(pages: list[str]) -> str:
    """给 pages.filter 生成一条「能圈住正文图」的**白名单**正则。

    引擎里 filter 是白名单（只保留命中的），所以这条正则必须同时做到两件事：
    圈得住正文图，且挡得掉同域名下的站徽/二维码/广告。

    两条经验：
    - 不能把第一张图的 host 写死。dm5 的图床按话轮换
      （``manhua1041-61-174-50-98.cdndm5.com`` / ``mhfm2tel.cdndm5.com``），
      锁一个 host，换话就全落空 → 取共同可注册域。
    - 光有域不够，站徽也在同域下 → 再叠一层**路径形态**：正文图路径里
      含 ``/<数字id>/<数字id>/`` 这种漫画 Id 目录，站徽走的是 ``/images/``。
    """
    if not pages:
        return ""
    hosts = [urlparse(u).hostname or "" for u in pages]
    hosts = [h for h in hosts if h]
    if not hosts:
        return ""
    domain = _common_domain(hosts) or ""
    ext = r"\.(?:jpe?g|png|webp|gif)(?:\?|$)"
    shape = _image_path_shape(pages)
    host_pat = rf"[\w.-]*{re.escape(domain)}" if domain else r"[\w.-]+"
    if shape:
        # shape 已经自带结尾锚点（覆盖到扩展名），直接拼接即可，
        # 不能再加一遍 ext —— 两个 $ 叠在同一条正则会永远匹配不上。
        # 中间用 ``.*`` 吸收固定目录前缀（``/th/comics/chapters/`` 之类）。
        return rf"^https?://{host_pat}/.*{shape}"
    # 推不出路径形态时，至少把常见的界面图目录排掉。
    if domain:
        return rf"^https?://{host_pat}(?!/images?/)(?!/static/)(?!/assets/).*{ext}"
    return ext


def _image_path_shape(pages: list[str]) -> str:
    """从正文图路径里提炼一段共同形态（**含扩展名与结尾锚点**）。

    只认「数字目录 + 数字目录」这种漫画 id 结构；提炼不出来就返回空串，
    交给调用方走兜底分支 —— 硬凑一段不靠谱的正则比没有更糟。

    注意数字目录**不要求紧跟在域名后**：松子漫画的正文图是
    ``/th/comics/chapters/204/260504/1069_1.webp``，前面还有一层固定目录。
    所以用 ``.*`` 吸收前缀，只把「数字目录 + 文件名」这段当特征。
    """
    paths = [urlparse(u).path for u in pages]
    paths = [p for p in paths if p]
    if not paths:
        return ""
    ext = r"\.(?:jpe?g|png|webp|gif)(?:\?|$)"
    # 「/<数字>/<数字>/…/<文件>」：漫画 id + 话 id 的经典结构。
    if sum(1 for p in paths if re.search(r"/\d+/\d+/", p)) >= max(1, len(paths) * 0.6):
        return rf"/\d+/\d+/[^/]*\d[^/]*{ext}"
    # 退一步：末段是「数字_数字.扩展名」（dm5 的 1_2461.jpg）。
    if sum(1 for p in paths if re.search(r"/\d+_\d+\.[a-z]+$", p, re.I)) >= max(1, len(paths) * 0.6):
        return rf"/[^/]*\d+_\d+{ext}"
    return ""


# 「公共后缀」——只有这些当共同域是没意义的（会把两个互不相关的站圈在一起）。
_PUBLIC_SUFFIX2 = re.compile(
    r"^(?:co|com|net|org|gov|edu|ac|or|ne|go)\.[a-z]{2}$", re.I
)


def _common_domain(hosts: list[str]) -> str:
    """取一组主机名的共同可注册域（``a.b.example.com`` + ``c.example.com`` → ``example.com``）。

    逐级去看哪个后缀被所有 host 共享，并保证至少两段（``example.com``），
    避免退化成把整个 TLD 都圈进去的 ``com``；同时挡掉 ``co.uk`` 这类
    公共后缀 —— 拿它当共同域会把 ``a.co.uk`` 和 ``b.co.uk`` 混为一谈。
    """
    if not hosts:
        return ""
    parts = [h.split(".") for h in hosts]
    n = 1
    while True:
        n += 1
        tails = {".".join(p[-n:]) for p in parts if len(p) >= n}
        if len(tails) != 1 or n > min(len(p) for p in parts):
            n -= 1
            break
    best = ".".join(parts[0][-n:]) if n >= 2 else ""
    if "." not in best or _PUBLIC_SUFFIX2.match(best):
        return ""
    return best


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s)


# ---- 同步外壳（给 GUI 的后台线程用）----

def probe_sync(url: str, *, deep: bool = True, timeout: float = 45.0) -> ProbeResult:
    """阻塞版探测，供 GUI 放到线程池里跑。"""

    async def run() -> ProbeResult:
        async with SiteProber() as p:
            return await asyncio.wait_for(p.probe(url, deep=deep), timeout=timeout)

    try:
        return asyncio.run(run())
    except asyncio.TimeoutError:
        r = ProbeResult(url=url, status="fail")
        r.reason = f"探测超时（{timeout:.0f} 秒），站点可能很慢或不可达。"
        return r
    except Exception as e:
        r = ProbeResult(url=url, status="fail")
        r.reason = f"探测失败：{type(e).__name__}: {e}"
        return r


def add_user_rule(rule: dict[str, Any]) -> bool:
    """把探测出来的规则写进 config["user_rules"]。"""
    from .config import config

    c = config()
    rules = list(c.get("user_rules") or [])
    sid = str(rule.get("id") or "")
    # 同 id 视为「更新这条」，别在列表里堆一串同域名的规则。
    for i, raw in enumerate(rules):
        try:
            if json.loads(raw).get("id") == sid:
                rules[i] = json.dumps(rule, ensure_ascii=False)
                c.set("user_rules", rules)
                log.info("更新自定义站点规则 %s", sid)
                return True
        except Exception:
            continue
    rules.append(json.dumps(rule, ensure_ascii=False))
    c.set("user_rules", rules)
    log.info("新增自定义站点规则 %s", sid)
    return True


def remove_user_rule(site_id: str) -> bool:
    """删掉一条自定义规则。只动 user_rules，不碰内置 rules/*.json。"""
    from .config import config

    c = config()
    rules = list(c.get("user_rules") or [])
    kept = []
    hit = False
    for raw in rules:
        try:
            if json.loads(raw).get("id") == site_id:
                hit = True
                continue
        except Exception:
            pass
        kept.append(raw)
    if hit:
        c.set("user_rules", kept)
        log.info("删除自定义站点规则 %s", site_id)
    return hit


def is_user_rule(site_id: str) -> bool:
    """某站点是不是用户自己加的（决定能不能删）。"""
    from .config import config

    for raw in config().get("user_rules") or []:
        try:
            if json.loads(raw).get("id") == site_id:
                return True
        except Exception:
            pass
    return False
