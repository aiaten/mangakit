"""逐本计时，找出到底哪本书把检查拖住。

用法：python tools/t_upd_which.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BOOKS = [
    ("manhuagui_cc", "https://manhuagui.cc/comic/longzhu"),
    ("songzimanhua", "https://songzimanhua.com/book/15293/"),
    ("songzimanhua", "https://songzimanhua.com/book/19818/"),
    ("comicwang", "https://www.comicwang.com/title/9286.html"),
    ("dmanhua", "https://www.dmanhua.com/comic/5758"),
    ("dingmanhua", "https://www.dingmanhua.com/manhua/6614/"),
    ("manhuahome", "https://www.manhuahome.com/manhua/1/"),
    ("mh250", "https://www.mh250.com/manhua/1/"),
]


async def main() -> int:
    from mangakit.sites import registry
    reg = registry()
    for site_id, url in BOOKS:
        site = reg.sites.get(site_id)
        if site is None:
            print(f"{site_id:14s} 站点不存在", flush=True)
            continue
        adapter = reg.adapter_for(site)
        t0 = time.time()
        try:
            chs = await asyncio.wait_for(adapter.parse_chapters(url), timeout=25)
            print(f"{site_id:14s} {time.time()-t0:6.2f}s  章节={len(chs):5d}", flush=True)
        except asyncio.TimeoutError:
            print(f"{site_id:14s} {time.time()-t0:6.2f}s  **25s 超时**", flush=True)
        except Exception as e:
            print(f"{site_id:14s} {time.time()-t0:6.2f}s  {type(e).__name__}: {str(e)[:70]}",
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
