"""诊断收藏「检查更新」卡住：逐站计时，看每站真实耗时。

用户截图里卡住的是 manhuagui_cc（龙珠），该站 verify.py 长期记录有 WAF。
用法：python tools/t_upd_slow.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 用户收藏里那一条
CASES = [
    ("manhuagui_cc", "https://manhuagui.cc/comic/longzhu"),
    ("songzimanhua", "https://songzimanhua.com/book/15293/"),
    ("comicwang", "https://www.comicwang.com/title/9286.html"),
]


async def main() -> int:
    from mangakit.sites import registry
    from mangakit.config import config

    reg = registry()
    print(f"config: timeout={config().get('timeout')} retries={config().get('retries')} "
          f"rate_limit={config().get('rate_limit')}\n")

    for site_id, url in CASES:
        site = reg.sites.get(site_id)
        if site is None:
            print(f"{site_id:16s} 站点不存在")
            continue
        adapter = reg.adapter_for(site)
        t0 = time.time()
        try:
            chs = await asyncio.wait_for(adapter.parse_chapters(url), timeout=120)
            dt = time.time() - t0
            print(f"{site_id:16s} {dt:6.1f}s  章节={len(chs):5d}  "
                  f"最新index={max((c.index for c in chs), default=-1)}")
        except asyncio.TimeoutError:
            print(f"{site_id:16s} {time.time()-t0:6.1f}s  **超过 120s 仍未返回**")
        except Exception as e:
            print(f"{site_id:16s} {time.time()-t0:6.1f}s  {type(e).__name__}: {str(e)[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
