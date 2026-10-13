"""异步业务层：把站点解析、搜索包成「提交后台循环 + 返回 request_id」的形式。

GUI 只管发请求收 signal，不碰 asyncio。
"""

from __future__ import annotations

import itertools
import traceback
from typing import Any, Callable, Coroutine

from .http_client import BlockedError, client
from .logsetup import get
from .models import Chapter, Comic
from .sites import ParseError, registry

log = get("service")

_counter = itertools.count(1)


def _next_id() -> int:
    return next(_counter)


class ComicService:
    """站点解析 + 搜索的统一入口。"""

    def __init__(self, manager: Any):
        self.mgr = manager

    # ---- 内部 ----
    def _run(self, coro: Coroutine, on_ok: Callable[[int, Any], None],
             on_err: Callable[[int, str], None]) -> int:
        rid = _next_id()
        br = self.bridge
        fut = self.mgr.submit(self._guard(rid, coro, on_ok, on_err))
        return rid

    @staticmethod
    async def _guard(rid: int, coro: Coroutine, on_ok, on_err) -> None:
        try:
            on_ok(rid, await coro)
        except ParseError as e:
            on_err(rid, f"解析失败：{e}")
        except BlockedError as e:
            # WAF 拦截不是程序 bug，给用户看人话，别甩类型名
            on_err(rid, str(e))
        except Exception as e:  # noqa: BLE001
            log.debug(traceback.format_exc())
            on_err(rid, f"{type(e).__name__}: {e}")

    @property
    def bridge(self):
        from .gui.bridge import bridge
        return bridge()

    # ---- 对外 ----
    def load_comic(self, url: str) -> int:
        return self._run(
            self._comic(url),
            lambda rid, c: self.bridge.comic_loaded.emit(rid, c),
            lambda rid, m: self.bridge.failed.emit(rid, m),
        )

    def load_chapters(self, url: str, full: bool = False) -> int:
        return self._run(
            self._chapters(url, full),
            lambda rid, c: self.bridge.chapters_loaded.emit(rid, c),
            lambda rid, m: self.bridge.failed.emit(rid, m),
        )

    def reload_chapters_full(self, url: str) -> int:
        """展开站点折叠隐藏的章节：重新走一遍加载，但 full=True。"""
        return self.load_chapters(url, full=True)

    def preview_pages(self, url: str) -> int:
        return self._run(
            self._pages(url),
            lambda rid, u: self.bridge.pages_loaded.emit(rid, u),
            lambda rid, m: self.bridge.failed.emit(rid, m),
        )

    def search_site(self, site_id: str, keyword: str) -> int:
        return self._run(
            self._search(site_id, keyword),
            lambda rid, r: self.bridge.search_done.emit(rid, r),
            lambda rid, m: self.bridge.failed.emit(rid, m),
        )

    def search_all(self, keyword: str) -> int:
        """全站搜索：每站出结果就立刻推一发给 GUI（先到先显示）。"""
        rid = _next_id()
        self.mgr.submit(self._search_all_stream(rid, keyword))
        return rid

    async def _search_all_stream(self, rid: int, keyword: str) -> None:
        import asyncio

        lock = asyncio.Lock()
        total = 0

        async def one(site, reg) -> None:
            nonlocal total
            try:
                adapter = reg.adapter_for(site)
                res = await adapter.search(keyword)
                for c in res:
                    c.site, c.site_name = site.id, site.name
            except Exception as e:
                log.info("搜索 %s 失败: %s", site.id, e)
                return
            if not res:
                return
            async with lock:
                total += len(res)
                # 每站单独推一发：最慢的站不再拖住整屏结果
                self.bridge.search_partial.emit(rid, res)

        try:
            reg = registry()
            enabled = [s for s in reg.visible_sites() if s.can_search]
            if enabled:
                await asyncio.gather(*(one(s, reg) for s in enabled),
                                     return_exceptions=True)
        except Exception:
            # 这里不该有异常：每个站自己的异常都在 one() 里吃掉了。
            # 但如果真有（例如规则文件损坏让 registry() 炸掉），也必须记下来 ——
            # 否则下面是唯一一次 search_done，用户只会看到按钮永远灰着。
            log.warning("全站搜索异常：\n%s", traceback.format_exc())
        finally:
            # search_done 是 GUI 收尾的唯一依据（复位按钮、解除「搜索中…」）。
            # 无论成功、失败还是半路出错，这一发必须发出去。
            self.bridge.search_done.emit(rid, total)

    # ---- 协程实现 ----
    async def _comic(self, url: str) -> Comic:
        found = registry().for_url(url)
        if not found:
            raise ParseError(f"暂不支持这个网址：{url}\n可在「设置 → 站点」里添加规则。")
        site, adapter = found
        c = await adapter.parse_comic(url)
        c.site, c.site_name = site.id, site.name
        return c

    async def _chapters(self, url: str, full: bool = False) -> list[Chapter]:
        found = registry().for_url(url)
        if not found:
            raise ParseError(f"暂不支持这个网址：{url}")
        site, adapter = found
        chs = await adapter.parse_chapters(url, full=full)
        for i, ch in enumerate(chs):
            ch.index = i
            if not ch.volume:
                ch.volume = ch.title
        return chs

    async def _pages(self, url: str) -> list[str]:
        found = registry().for_url(url)
        if not found:
            raise ParseError(f"暂不支持这个网址：{url}")
        _, adapter = found
        return await adapter.parse_pages(url)

    async def _search(self, site_id: str, keyword: str) -> list[Comic]:
        reg = registry()
        site = reg.sites.get(site_id)
        if not site or not site.can_search:
            return []
        adapter = reg.adapter_for(site)
        res = await adapter.search(keyword)
        for c in res:
            c.site, c.site_name = site.id, site.name
        return res

    # ---- 收藏更新检查 ----
    def check_updates(self, items: list[tuple[str, str]] | None = None,
                      *, concurrency: int = 6, per_book: float = 45.0) -> int:
        """检查收藏里这些漫画有没有新章节。items 为 None 表示检查全部收藏。

        每本书查完立刻推一发 update_partial，全部结束再推 update_done。
        concurrency 默认 6：单本书就是一次详情页请求（实测每本 0.3~0.7s），
        串行查几十本会明显发木；站点自身有 RateLimiter 兜着，不用担心打爆。
        per_book 是单本上限：卡住不回的站最多拖这么久，之后记一笔失败放行，
        绝不能让它把整次检查无限期挂住。
        """
        rid = _next_id()
        # 必须和其它入口一样走 _guard：不加守卫时，_check_updates 里任何异常
        # 都会被线程池静默吞掉，一个信号都不发，界面上「检查中…」永远不结束，
        # 用户既看不到结果也看不到错误。
        self.mgr.submit(self._guard(
            rid,
            self._check_updates(rid, items, concurrency, per_book),
            lambda _rid, _res: None,
            lambda _rid, m: self.bridge.update_done.emit(
                rid, {"total": 0, "new": 0, "error": 1, "checked": 0, "message": m}),
        ))
        return rid

    async def _check_updates(self, rid: int, items: list[tuple[str, str]] | None,
                             concurrency: int, per_book: float = 45.0) -> None:
        import asyncio

        from .models import FavoriteUpdate
        from .store import store

        br = self.bridge
        rows = store().favorites()
        if items is not None:
            want = set(items)
            rows = [r for r in rows if (r["site"], r["url"]) in want]

        if not rows:
            br.update_done.emit(rid, {"total": 0, "new": 0, "error": 0, "checked": 0})
            return

        reg = registry()
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        lock = asyncio.Lock()
        stat = {"total": len(rows), "checked": 0, "new": 0, "error": 0}

        async def one(row) -> None:
            site_id, url, title = row["site"], row["url"], row["title"]
            prev = int(row["last_index"]) if row["last_index"] is not None else -1
            async with sem:
                # 单本最多等 per_book 秒。没有这层闸门时，一个卡住不回的站
                # 会把整次「检查更新」无限期拖住（按钮永远停在「检查中…」），
                # 而且它排在前面的书已经查完也看不到汇总。
                try:
                    up = await asyncio.wait_for(
                        self._check_one(reg, site_id, url, title, prev),
                        timeout=per_book)
                except asyncio.TimeoutError:
                    up = FavoriteUpdate(site_id, url, title, status="error",
                                        message=f"检查超时（超过 {per_book:.0f} 秒未返回）")
                except Exception as e:  # noqa: BLE001
                    up = FavoriteUpdate(site_id, url, title, status="error",
                                        message=f"{type(e).__name__}: {e}")
            async with lock:
                stat["checked"] += 1
                if up.status == "new":
                    stat["new"] += 1
                elif up.status == "error":
                    stat["error"] += 1
                # 成功见到章节就记住这次的位置，下次才有对比基准。
                # error/unsupported 只更新时间，不动基线，免得把没查成当成了"没更新"。
                if up.status in ("ok", "new"):
                    store().mark_favorite_checked(site_id, url, up.latest_index)
                elif up.status == "first":
                    store().mark_favorite_checked(site_id, url, up.latest_index)
                else:
                    store().mark_favorite_checked(site_id, url, None)
                br.update_partial.emit(rid, up)

        await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
        br.update_done.emit(rid, stat)

    @staticmethod
    async def _check_one(reg, site_id: str, url: str, title: str,
                         prev_index: int) -> "FavoriteUpdate":
        from .models import FavoriteUpdate

        site = reg.sites.get(site_id)
        if site is None or not site.enabled:
            return FavoriteUpdate(site_id, url, title, status="unsupported",
                                  message="站点已停用或规则不存在")
        try:
            adapter = reg.adapter_for(site)
            chs = await adapter.parse_chapters(url)
        except ParseError as e:
            return FavoriteUpdate(site_id, url, title, status="error",
                                  message=f"解析失败：{e}")
        except BlockedError as e:
            return FavoriteUpdate(site_id, url, title, status="error", message=str(e))
        except Exception as e:  # noqa: BLE001
            log.debug(traceback.format_exc())
            return FavoriteUpdate(site_id, url, title, status="error",
                                  message=f"{type(e).__name__}: {e}")

        if not chs:
            return FavoriteUpdate(site_id, url, title, status="error",
                                  message="这个站没解析出任何章节")

        latest = max(ch.index for ch in chs)
        newest = max(chs, key=lambda c: c.index)
        total = len(chs)

        if prev_index < 0:
            # 第一次检查：只记基线，不谎报"有更新"
            return FavoriteUpdate(site_id, url, title, latest_index=latest,
                                  latest_title=newest.title, total=total,
                                  new_count=0, status="first",
                                  message="首次检查，已记录当前进度")
        new_count = latest - prev_index
        if new_count <= 0:
            return FavoriteUpdate(site_id, url, title, latest_index=latest,
                                  latest_title=newest.title, total=total,
                                  new_count=0, status="ok")
        return FavoriteUpdate(site_id, url, title, latest_index=latest,
                              latest_title=newest.title, total=total,
                              new_count=new_count, status="new")

