"""真网验证：逐站走完 详情 -> 章节 -> 图片 -> 下载。

用法：
    python tools/verify.py                 # 跑全部 cases.json
    python tools/verify.py manhuagui_cc    # 只跑指定 case
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mangakit.config import config  # noqa: E402
from mangakit.http_client import BlockedError, client  # noqa: E402
from mangakit.sites import ParseError, registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).parent / "cases.json"


async def verify(case: dict, out_root: Path) -> dict:
    sid = case["id"]
    url = case["url"]
    r: dict = {"id": sid, "url": url, "site_name": case.get("site_name", "")}
    t0 = time.time()
    reg = registry()
    site = reg.site_for_url(url)
    if not site:
        r["stage"] = "match"
        r["ok"] = False
        r["error"] = "没有匹配到站点规则"
        return r
    r["site_id"] = site.id
    ad = reg.adapter_for(site)
    try:
        comic = await ad.parse_comic(url)
        r["title"] = comic.title
        r["cover"] = bool(comic.cover)
        r["author"] = comic.author[:60]
        if not comic.title:
            raise ParseError("标题为空")

        # full=true 的用例顺带验证「展开折叠隐藏章节」这条链路
        # （chapters.api / chapters.embed 两条补齐路径）。
        full = bool(case.get("full"))
        chs = await ad.parse_chapters(url, full=full)
        r["chapters"] = len(chs)
        if not chs:
            raise ParseError("没有章节")
        if full:
            short_n = len(await ad.parse_chapters(url))
            r["chapters_short"] = short_n
            r["chapters_extra"] = len(chs) - short_n
            if len(chs) <= short_n:
                raise ParseError("full=True 没有补齐任何折叠章节")
        r["chapter_sample"] = [c.title[:24] for c in chs[:3]]

        idx = case.get("chapter", -1)
        ch = chs[idx] if idx >= 0 else chs[-1]
        from mangakit.images import finalize, is_image, page_filename, safe_filename
        r["chapter"] = safe_filename(ch.title, 60)
        pages = await ad.parse_pages(ch.url)
        r["pages"] = len(pages)
        if not pages:
            raise ParseError("没有图片")
        r["page_sample"] = pages[0][:110]

        n = int(case.get("download", 3))
        outdir = out_root / sid / (safe_filename(ch.title, 60) or "ch")
        outdir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for i, u in enumerate(pages[:n]):
            try:
                data, ct = await client().download(u, headers=site.headers)
                if not is_image(data):
                    r.setdefault("skipped", []).append(f"p{i+1} 非图片({ct},{len(data)}B)")
                    continue
                data, ext = finalize(data, convert_webp=bool(config().get("convert_webp")))
                (outdir / page_filename(i, len(pages), ext)).write_bytes(data)
                saved += 1
            except Exception as e:
                r.setdefault("dl_err", []).append(f"p{i+1}: {e}")
        r["saved"] = saved
        r["bytes"] = sum(f.stat().st_size for f in outdir.iterdir() if f.is_file())
        r["outdir"] = str(outdir)
        if saved == 0:
            raise ParseError("图片全部下载失败")

        # 搜索：站点声明支持就必测。以前 verify 只走详情→章节→图，
        # 一批站的 search 是坏的却照样 PASS（这条是踩过的坑）。
        r["can_search"] = site.can_search
        kw = case.get("search")
        if kw:
            res_s = await ad.search(kw)
            r["search"] = len(res_s)
            r["search_sample"] = [x.title[:20] for x in res_s[:3]]
            if not res_s:
                raise ParseError(f"站内搜索「{kw}」0 条结果")
        r["ok"] = True
    except Exception as e:
        r["ok"] = False
        # BlockedError 是「站方拦了/站方挂了」，不是我们的规则坏了——
        # 两类分开标记，否则站方一次抖动就会把回归报告染红，久了没人当真。
        r["transient"] = isinstance(e, BlockedError)
        r["error"] = f"{type(e).__name__}: {e}"
    r["secs"] = round(time.time() - t0, 1)
    return r


async def main() -> None:
    cases = json.loads(CASES.read_text("utf-8"))
    only = sys.argv[1:]
    case_full = {c["id"]: bool(c.get("full")) for c in cases}
    if only:
        cases = [c for c in cases if c["id"] in only or c["id"].startswith(tuple(only))]
    # 样本图落在下载目录下的 _verify\，每次跑前清空，避免旧站点结果
    # 被误当成这次的结论。
    out_root = Path(config().download_dir) / "_verify"
    shutil.rmtree(out_root, ignore_errors=True)
    out_root.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)

    async def guarded(c):
        async with sem:
            try:
                return await verify(c, out_root)
            except Exception as e:
                return {"id": c["id"], "url": c["url"], "ok": False,
                        "error": f"fatal {type(e).__name__}: {e}"}

    async with client() as http:
        res = await asyncio.gather(*(guarded(c) for c in cases))
    await client().close()

    ok = [r for r in res if r.get("ok")]
    bad = [r for r in res if not r.get("ok")]
    # 站方故障（WAF/后端挂）不算我们的回归，单列出来，别每次都当成红色警报。
    flaky = [r for r in bad if r.get("transient")]
    hard = [r for r in bad if not r.get("transient")]
    print(f"\n{'='*66}")
    for r in res:
        mark = "PASS" if r.get("ok") else ("SITE" if r.get("transient") else "FAIL")
        print(f"[{mark}] {r['id']:<16} {r.get('site_name') or r.get('site_id') or '':<18} "
              f"{r.get('secs', 0):>5}s")
        if r.get("ok"):
            extra = ""
            if r.get("search"):
                extra += f" 搜索 {r['search']} 条"
            if case_full.get(r.get("id")):
                extra += f"（展开 +{r.get('chapters_extra')} 话）"
            print(f"       漫画《{r.get('title')}》 章节 {r.get('chapters')} 图片 {r.get('pages')} "
                  f"下载 {r.get('saved')} 张 {r.get('bytes', 0)//1024}KB{extra}")
        else:
            print(f"       × {r.get('error')}")
            if r.get("transient"):
                print("       （站方自己的问题，重跑或过一会儿再试；不算规则回归）")
    print(f"\n通过 {len(ok)}/{len(res)}"
          + (f"：{', '.join(r['id'] for r in ok)}" if ok else ""))
    if flaky:
        print(f"站方故障 {len(flaky)}：{', '.join(r['id'] for r in flaky)}")
    if hard:
        print(f"规则回归 {len(hard)}：{', '.join(r['id'] for r in hard)}")
    (Path(__file__).parent / "verify.out.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=str), "utf-8")


if __name__ == "__main__":
    asyncio.run(main())