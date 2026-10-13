"""闭环验证：探测 → 写 user_rules → 重载注册表 → 用引擎真跑一遍 → 清理。

用真实路径走一遍用户点「添加网站」后会发生什么，确认：
  ① 探测判定为可添加
  ② 规则写进 config 的 user_rules
  ③ 注册表能认出该详情页网址
  ④ 引擎解析出章节 / 图片
  ⑤ 首图真能下载
  ⑥ 站内搜索可用
  ⑦ 清理掉测试规则，不留痕
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mangakit.sites as sm  # noqa: E402
from mangakit.probe import (  # noqa: E402
    add_user_rule,
    is_user_rule,
    probe_sync,
    remove_user_rule,
)
from mangakit.sites import registry  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://songzimanhua.com/book/15293/"


def reload_registry() -> None:
    sm._registry = None
    registry()


def main() -> int:
    print("=" * 70)
    print(f"目标：{TARGET}")

    # 探测本身是同步阻塞的（GUI 里跑在线程池），必须在事件循环外面调 ——
    # 在 async 函数里调会撞上「asyncio.run() cannot be called from a
    # running event loop」。
    r = probe_sync(TARGET, deep=True, timeout=90.0)
    print(f"① 探测: status={r.status} 章节={r.chapter_count} 图片={r.image_count} 搜索={r.can_search}")
    if not r.addable:
        print(f"   不予添加：{r.reason}")
        return 0

    sid = r.rule["id"]
    add_user_rule(r.rule)
    print(f"② 已写入 user_rules，id={sid}  is_user_rule={is_user_rule(sid)}")

    reload_registry()
    site = registry().site_for_url(TARGET)
    print(f"③ 注册表认出：{site.name} (id={site.id})" if site else "③ 注册表认不出该网址 ❌")
    if not site:
        remove_user_rule(sid)
        reload_registry()
        return 1

    try:
        return asyncio.run(_engine_check(site, sid))
    finally:
        remove_user_rule(sid)
        reload_registry()
        print(f"⑧ 已清理测试规则（is_user_rule={is_user_rule(sid)}）")


async def _engine_check(site, sid: str) -> int:
    ad = registry().adapter_for(site)
    async with ad.http:
        comic = await ad.parse_comic(TARGET)
        print(f"④ 漫画标题：{comic.title}  封面：{(comic.cover or '')[:70]}")
        chapters = await ad.parse_chapters(TARGET)
        print(f"   章节数：{len(chapters)}")
        for c in chapters[:3]:
            print(f"      {c.title} -> {c.url}")
        if not chapters:
            print("   没有解析到章节 ❌")
            return 1

        pages = await ad.parse_pages(chapters[0].url)
        print(f"⑤ 首话图片数：{len(pages)}")
        for u in pages[:3]:
            print(f"      {u[:100]}")
        if not pages:
            print("   没有解析到图片 ❌")
            return 1

        data, ctype = await ad.http.download(pages[0])
        print(f"⑥ 首图下载：{len(data)} 字节 ct={ctype} -> {'✅ 成功' if len(data) > 1024 else '⚠ 太小，可能是占位图'}")

        if site.can_search:
            try:
                hits = await ad.search("海贼王")
                print(f"⑦ 搜索「海贼王」：{len(hits)} 条")
                for h in hits[:3]:
                    print(f"      {h.title} -> {h.url[:80]}")
            except Exception as e:  # noqa: BLE001
                print(f"⑦ 搜索失败：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
