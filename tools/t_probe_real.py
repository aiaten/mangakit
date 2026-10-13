"""用站点自己的搜索接口拿到真实详情页 URL，再交给探测器判断。

先搜「海贼王」拿到每个站的真实详情页，然后逐个 probe，最后打印
「能添加 / 不能添加」的名单。这是检验探测器放行能力的正面用例。
"""
from __future__ import annotations

import concurrent.futures
import re
import sys
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from lxml import html as LH  # noqa: E402

from mangakit.probe import probe_sync  # noqa: E402
from mangakit.sites import registry  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
KEYWORD = "海贼王"


def first_detail(site) -> tuple[str, str]:
    """搜索一次，返回第一个看起来像详情页的 URL。"""
    tpl = (site.payload.get("search") or {}).get("url") or site.search_url
    if not tpl:
        return site.id, ""
    url = tpl.replace("{q}", quote(KEYWORD)).replace("%s", quote(KEYWORD))
    try:
        with httpx.Client(
            headers={"User-Agent": UA, "Referer": f"https://{site.domains[0]}/"},
            timeout=15,
            follow_redirects=True,
        ) as c:
            r = c.get(url)
            if r.status_code != 200:
                return site.id, ""
            doc = LH.fromstring(r.text)
            host = site.domains[0]
            for a in doc.xpath("//a[@href]"):
                h = urljoin(url, a.get("href") or "")
                p = urlparse(h)
                if host.split(":")[0] not in (p.hostname or ""):
                    continue
                if re.search(
                    r"/(?:comic|manhua|book|manga|detail|mhw)/[^/]*(?:\d|/)$", p.path
                ) and p.path.count("/") >= 2:
                    return site.id, h
    except Exception:  # noqa: BLE001
        pass
    return site.id, ""


def main() -> int:
    sites = [s for s in registry().all_sites() if s.can_search and s.lang == "cn"]
    print(f"用 {KEYWORD} 搜出详情页，共 {len(sites)} 个可搜索站…\n")
    found: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for sid, u in ex.map(first_detail, sites):
            if u:
                found.append((sid, u))
                print(f"  {sid:18s} {u}")
    print()
    if not found:
        print("一个详情页都没搜到")
        return 1
    print("=" * 70)
    print("逐个探测：\n")
    acc, rej = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda t: probe_sync(t[1], deep=True, timeout=60.0), found))
    for (sid, u), r in zip(found, results):
        tag = "✅可添加" if r.addable else "❌拒绝"
        print(f"{tag}  {sid:18s} 章节={r.chapter_count:4d} 图片={r.image_count:3d} 搜索={'是' if r.can_search else '否'}")
        if r.addable:
            acc.append(sid)
        else:
            rej.append((sid, r.reason[:70]))
    print()
    for sid, why in rej:
        print(f"  拒绝 {sid}: {why}")
    print(f"\n可添加 {len(acc)}：{acc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
