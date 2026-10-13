"""闭环验证：探测生成规则 → 写进 config.user_rules → 走真实注册表 → 解析并下载。

这才是用户实际会走的路：加完站点后重启，注册表从 user_rules 装载规则，
`registry().for_url(url)` 找站点、拿适配器、解析章节、下图片。
"""
import sys, asyncio, json
from pathlib import Path
# 仓库根 = 本文件(tools/)的上一级。以前这里写死了一个绝对路径，
# 项目换盘/换目录后就会 import 不到 mangakit —— 改成自动定位。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mangakit.config as cfgmod
from mangakit.probe import SiteProber, add_user_rule, remove_user_rule

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.dm5.com/manhua-aishudexiakeshang-disibu/"


def fresh_registry():
    """甩掉单例，让注册表重新读一遍 user_rules（等价于重启后的第一次加载）。"""
    import mangakit.sites as sm
    sm._registry = None
    return sm.registry()


async def main():
    # 1) 探测
    async with SiteProber() as p:
        r = await p.probe(URL, deep=True)
    print(f"① 探测: status={r.status} 章节={r.chapter_count} 图片={r.image_count}")
    if r.status != "ok":
        print("  探测未通过 => 按需求不加这个站。原因:", r.reason)
        return

    # 2) 写入 user_rules（真实落地路径）
    add_user_rule(r.rule)
    print(f"② 已写入 user_rules，id={r.rule['id']}")

    # 3) 重新装载注册表，走真实 code path
    reg = fresh_registry()
    hit = reg.site_for_url(URL)
    print(f"③ 注册表能否认出该网址: {hit.name if hit else '❌ 认不出'} (id={hit.id if hit else '-'})")
    if not hit:
        print("  规则里的 domains 没能匹配上，添加等于白加"); return

    site, ad = reg.for_url(URL)
    print(f"   适配器: {type(ad).__name__}")

    # 4) 解析详情页 + 章节
    comic = await ad.parse_comic(URL)
    print(f"④ 漫画标题: {comic.title}")
    chs = await ad.parse_chapters(URL, full=False)
    print(f"   章节数: {len(chs)}")
    for c in chs[:3]:
        print("     ", c.title, "->", c.url)
    if not chs:
        print("  ❌ 章节解析为空，规则不可用"); return

    # 5) 解析章节页图片
    imgs = await ad.parse_pages(chs[0].url)
    print(f"⑤ 首话图片数: {len(imgs)}")
    for u in imgs[:3]:
        print("     ", u)
    if not imgs:
        print("  ❌ 图片解析为空，规则不可用"); return

    # 6) 真下第一张
    from mangakit.http_client import HttpClient
    async with HttpClient() as h:
        try:
            resp = await h.request("GET", imgs[0], kind="image",
                                   headers={"Referer": chs[0].url})
            ok = resp.status_code == 200 and len(resp.content) > 1024
            print(f"⑥ 首图下载: HTTP {resp.status_code}, {len(resp.content)} 字节 -> {'✅ 成功' if ok else '❌ 失败'}")
        except Exception as e:
            print(f"⑥ 首图下载异常: {type(e).__name__} {e}")

    # 7) 搜索
    if site.search_url:
        try:
            got = await ad.search("爱书")
            print(f"⑦ 搜索「爱书」: {len(got)} 条结果")
            for g in got[:3]:
                print("     ", g.title, "->", g.url)
        except Exception as e:
            print(f"⑦ 搜索失败: {type(e).__name__} {e}")

    # 收尾：这次是测试，别把测试站留在用户配置里
    remove_user_rule(r.rule["id"])
    print("⑧ 已清理测试规则")


asyncio.run(main())
