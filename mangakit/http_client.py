"""HTTP 客户端层。

统一处理：UA/Referer/Cookie 注入、按域名限速、失败重试、
自动解压（含 brotli/zstd）、二进制流式下载。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse, urljoin

import httpx

from .config import config
from .logsetup import get

log = get("net")

def _supported_encodings() -> str:
    """只声明本进程真的能解码的编码。

    httpx 只在装了 brotli / zstandard 解码器时才会自动解码；宣告了 br/zstd
    却没装，服务端就会回压缩字节，而 httpx 不会报错——于是 r.text 变成一堆
    乱码，解析器只能看到空 DOM。此处按实际可用的模块收敛请求头。
    """
    import importlib.util as _u
    enc = ["gzip", "deflate"]
    if any(_u.find_spec(m) for m in ("brotli", "brotlicffi")):
        enc.append("br")
    if _u.find_spec("zstandard"):
        enc.append("zstd")
    return ", ".join(enc)


_NON_ASCII_RE = re.compile(r"[^\x20-\x7e]")


def _ascii_header_value(value: str) -> str:
    """把请求头里的非 ASCII 字符做百分号编码。

    httpx 的 `httpx/_models.py:_normalize_header_value()` 对 str 直接
    `value.encode(encoding or "ascii")`，头值里出现中文就抛
    `UnicodeEncodeError: 'ascii' codec can't encode characters ...`。
    而 httpx 自己只会对 URL 的 path/query 做 quote，头值不编码，所以
    像 Referer: https://mhquan.com/search?q=海贼王 这种（搜索时按
    referer_policy=page 自动注入）就会炸。
    """
    if not _NON_ASCII_RE.search(value):
        return value
    return _NON_ASCII_RE.sub(
        lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode("utf-8")), value
    )


# 浏览器常见的 H2 头顺序，最大化减少站方风控命中
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Accept-Encoding": _supported_encodings(),
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

IMAGE_HEADERS: dict[str, str] = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
}

class RateLimiter:
    """按 host 的最小请求间隔。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        lk = self._locks.get(host)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[host] = lk
        return lk

    async def acquire(self, host: str) -> None:
        gap = float(config().get("rate_limit") or 0)
        if gap <= 0:
            return
        async with self._lock(host):
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            wait = last + gap - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


def normalize_url(base: str, url: str) -> str:
    """把可能是相对路径的 URL 拼成绝对 URL，并去掉 fragment。"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        p = urlparse(base)
        url = f"{p.scheme}:{url}"
    elif not urlparse(url).scheme:
        url = urljoin(base, url)
    parts = urlparse(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, parts.query, ""))


def decode_html_sync(r: httpx.Response) -> str:
    """decode_html 的同步版本（内部用；公开 API 保持 await decode_html(r)）。"""
    cands: list[str] = []
    ctype = r.headers.get("content-type") or ""
    m = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype, re.I)
    if m:
        cands.append(m.group(1))
    head = r.content[:4096]
    m = re.search(rb"""charset\s*=\s*["']?([\w.:-]+)""", head, re.I)
    if m:
        try:
            cands.append(m.group(1).decode("ascii"))
        except Exception:
            pass
    cands += ["utf-8", "gb18030", "big5"]
    seen: set[str] = set()
    for enc in cands:
        e = enc.lower().strip()
        if e in seen:
            continue
        seen.add(e)
        try:
            return r.content.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return r.text


async def decode_html(r: httpx.Response) -> str:
    """按 HTTP 头 / <meta charset> / 内容猜三种方式解码。

    国内漫画站里 gb2312/gbk 仍有不少，而 httpx 默认按 utf-8 解码且**不报错**，
    结果中文全是替换字符——解析器照样跑，只是标题和章节名全乱。因此这里
    显式按顺序尝试，成功即返回。
    """
    return decode_html_sync(r)


class BlockedError(Exception):
    """站方防火墙 / WAF 拦截了请求（不是网络问题，也不是规则错了）。

    典型信号是返回非标准状态码 + 一张写明「触发防火墙规则」的拦截页，
    例如 mhquan.com 对普通浏览器 UA 回 `702` 并附带访问编号。
    """


# 拦截页里用来自证身份的强特征。命中即判定为被拦，不看状态码——
# 有的站用 200 返回挑战页（Cloudflare "Just a moment..."）。
_WAF_STRONG = (
    "触发防火墙规则",
    "访问编号",
    "访问代码",
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "enable javascript and cookies",
    "请开启javascript",
)

# 弱特征：正常页面里也可能出现（例如 Cloudflare 的统计脚本、
# 泛化的「安全防护」文案），只有状态码异常时才作为佐证。
_WAF_WEAK = (
    "cloudflare",
    "安全防护",
    "访问验证",
    "ray id",
    "captcha",
    "人机验证",
)


def detect_block(r: httpx.Response) -> str | None:
    """如果响应是被 WAF 拦下的，返回人话原因；正常响应返回 None。

    判定顺序很重要：**先按状态码分档，再看特征词**。只按特征词判会
    误杀正常页面——大量站点挂着 Cloudflare 统计脚本，正文里就有
    "cloudflare" 字样（实测 5 个正常站因此被误判）。
    """
    code = r.status_code
    try:
        text = decode_html_sync(r)
    except Exception:  # noqa: BLE001 —— 判定失败不该盖掉原始响应
        text = ""
    low = text.lower()

    if any(mk.lower() in low for mk in _WAF_STRONG):
        rule = ""
        m = re.search(r"触发防火墙规则\((\d+)\)", text)
        if m:
            rule = f"（规则 {m.group(1)}）"
        return f"被站点防火墙拦截{rule}，状态码 {code}，请稍后重试或换用同站其他入口"

    # 状态码异常时才看弱特征：WAF 自定义码（70x/40x）多是 3 位非标准值
    bad = code // 100 not in (2, 3)
    if bad and any(mk.lower() in low for mk in _WAF_WEAK):
        return f"站点返回异常状态码 {code}，疑似风控拦截，请稍后重试"

    # 非标状态码（WAF 常见 702 这类三位自定义码）直接报可读错误，
    # 否则 httpx 会抛 `Invalid status code '702 '` 这种天书。
    if bad and not (100 <= code < 600):
        return f"站点返回非标准状态码 {code}，请稍后重试或换用同站其他入口"

    # 站点后端自己出问题时，会用 200 + 一句人话代替整个页面（实测 manhuawang
    # 数据库连接池打满时回 51 字节的「数据库链接失败：… Too many connections」）。
    # 不拦住的话，解析层只看到「一个没有章节的空页面」，报「没有章节」——
    # 会把人引去改规则，而真正的原因是站方数据库挂了，晚点重试就好。
    if _server_error_in_text(text):
        return (f"站点服务端异常：{text.strip()[:120]}。"
                f"这是站方自己的问题（不是规则失效），请过一会儿再试。")
    return None


# 站方后端故障在响应正文里的特征。只认「短文本 + 中文故障措辞」这类强信号，
# 免得正常漫画页里恰好出现这些词就被误判。
_SERVER_ERROR_MARKS = (
    "sqlstate",
    "too many connections",
    "数据库连接失败",
    "数据库链接失败",
    "database error",
    "mysql_connect",
    "internal server error",
    "服务器内部错误",
    "服务暂时不可用",
)


def _server_error_in_text(text: str) -> bool:
    t = (text or "").strip()
    # 真页面都是几 KB 起的整页 HTML；几十~几百字的纯文本才可能是故障回执。
    if len(t) > 400 or "<html" in t.lower() or "<!doctype" in t.lower():
        return False
    low = t.lower()
    return any(m.lower() in low for m in _SERVER_ERROR_MARKS)


class HttpClient:
    """带重试与限速的异步 HTTP 客户端。"""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.rate = RateLimiter()
        self._client = client
        self._owns = client is None
        self._extra_headers: dict[str, str] = {}
        # 首次遇到 TLS 握手失败时立刻降级为不校验证书（不少小站证书链不全）
        self._insecure = False

    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(float(config().get("timeout") or 20), connect=15),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            http2=True,
            verify=not self._insecure,
        )

    def _downgrade(self) -> bool:
        """证书/握手类错误时切到不校验模式，只降级一次。"""
        if self._insecure or self._owns is False:
            return False
        old = self._client
        self._insecure = True
        self._client = None
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._start_new(loop, old))
                return True
            self._client = self._build_client()
            return True
        except Exception:
            self._client = self._build_client()
            return True

    async def _start_new(self, loop: asyncio.AbstractEventLoop, old: Any) -> None:
        self._client = self._build_client()
        try:
            if old is not None:
                await old.aclose()
        except Exception:
            pass
        log.warning("已切换为不校验 TLS 证书模式（站点证书链不完整）")

    async def start(self) -> None:
        if self._client is None:
            self._client = self._build_client()

    async def close(self) -> None:
        if self._client is not None and self._owns:
            await self._client.aclose()
            self._client = None

    def set_headers(self, headers: Mapping[str, str]) -> None:
        self._extra_headers = {str(k): str(v) for k, v in headers.items()}

    # ---- 内部 ----
    def _build_headers(
        self, url: str, extra: Mapping[str, str] | None, kind: str = "html"
    ) -> dict[str, str]:
        cfg = config()
        h: dict[str, str] = dict(DEFAULT_HEADERS if kind == "html" else IMAGE_HEADERS)
        h["User-Agent"] = str(cfg.get("user_agent"))
        h.update(self._extra_headers)
        if extra:
            h.update({str(k): str(v) for k, v in extra.items() if v is not None})
        policy = str(cfg.get("referer_policy") or "page")
        if policy == "page":
            h.setdefault("Referer", url)
        elif policy == "site":
            p = urlparse(url)
            h.setdefault("Referer", f"{p.scheme}://{p.netloc}/")
        else:
            h.pop("Referer", None)
        # httpx 头值只吃 ascii，统一兜底编码（Referer/Referer 之外的
        # 站点自定义头也可能带中文，如部分站的 "Accept-Charset"）
        return {_ascii_header_value(str(k)): _ascii_header_value(str(v)) for k, v in h.items()}

    # ---- 公开 API ----
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        cookies: Mapping[str, str] | None = None,
        kind: str = "html",
        retries: int | None = None,
    ) -> httpx.Response:
        await self.start()
        assert self._client is not None
        n = int(retries if retries is not None else config().get("retries") or 3)
        # retries=0 表示「不重试」，也就是只尝试一次；n=0 会让下面的循环
        # 一次都不执行，最后在 `assert last_exc is not None` 上炸掉。
        n = max(1, n)
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(1, n + 1):
            await self.rate.acquire(host)
            try:
                resp = await self._client.request(
                    method, url,
                    headers=self._build_headers(url, headers, kind),
                    params=params, data=data, json=json, cookies=cookies,
                )
                if resp.status_code in (429, 502, 503, 504) and attempt < n:
                    ra = resp.headers.get("Retry-After")
                    delay = float(ra) if (ra or "").isdigit() else min(2 ** attempt * 0.6, 10)
                    log.warning("%s %s -> %s，%.1fs 后重试 (%d/%d)", resp.status_code, url, ra or "", delay, attempt, n)
                    await asyncio.sleep(delay + random.uniform(0, 0.4))
                    continue
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt >= n:
                    break
                delay = min(2 ** attempt * 0.5, 8)
                log.warning("请求失败 %s: %s，%.1fs 后重试 (%d/%d)", url, e, delay, attempt, n)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
        assert last_exc is not None
        raise last_exc

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def get_text(self, url: str, *, encoding: str | None = None, **kw: Any) -> str:
        r = await self.get(url, **kw)
        blocked = detect_block(r)
        if blocked:
            raise BlockedError(blocked)
        r.raise_for_status()
        if encoding:
            return r.content.decode(encoding, errors="replace")
        return await decode_html(r)

    async def get_json(self, url: str, **kw: Any) -> Any:
        r = await self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    async def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        on_progress: Any = None,
    ) -> tuple[bytes, str]:
        """流式下载二进制。返回 (数据, content-type)。"""
        await self.start()
        assert self._client is not None
        cap = max_bytes if max_bytes is not None else int(config().get("max_image_mb") or 40) * 1024 * 1024
        host = urlparse(url).netloc
        await self.rate.acquire(host)
        buf = bytearray()
        ctype = ""
        async with self._client.stream(
            "GET", url,
            headers=self._build_headers(url, headers, kind="image"),
            cookies=cookies,
        ) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            clen = int(resp.headers.get("content-length") or 0)
            if clen > cap:
                raise ValueError(f"文件过大 ({clen/1048576:.1f}MB > {cap/1048576:.0f}MB)")
            async for chunk in resp.aiter_bytes(65536):
                buf.extend(chunk)
                if len(buf) > cap:
                    raise ValueError(f"文件超过上限 {cap/1048576:.0f}MB")
                if on_progress and clen:
                    on_progress(len(buf), clen)
        return bytes(buf), ctype


_shared: HttpClient | None = None


def client() -> HttpClient:
    global _shared
    if _shared is None:
        _shared = HttpClient()
    return _shared