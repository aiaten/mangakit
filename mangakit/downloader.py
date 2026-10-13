"""下载队列：并发控制、失败重试、断点续传、SQLite 持久化、暂停/恢复。

事件循环跑在独立线程里，GUI 通过 `submit()` 投递协程，
结果通过回调（线程安全地转发到 Qt）通知。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import WriteDenied, _why_unwritable, config, ensure_writable
from .http_client import HttpClient, client
from .images import (
    convert_webp_to_png, finalize, is_image, page_filename,
    safe_filename, split_long, verify,
)
from .logsetup import get
from .models import Chapter, Comic, Task
from .sites import ParseError, registry
from .store import store

log = get("downloader")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_PAUSED = "paused"
STATUS_SKIPPED = "skipped"


def _now() -> float:
    return time.time()


@dataclass
class TaskContext:
    """一次下载所需的全部输入。"""

    task: Task
    comic_title: str
    chapter_title: str
    comic_url: str
    chapter_url: str
    folder: Path


class DownloadManager:
    """线程安全的下载管理器。

    - `start()`  启动后台事件循环线程
    - `enqueue()`  把漫画/章节加入队列（立即落库，重启后可见）
    - `pause(tid)` / `resume(tid)`
    - 事件回调：on_task_changed / on_log
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sem: asyncio.Semaphore | None = None
        self._tasks: dict[str, Task] = {}
        self._paused: set[str] = set()
        self._controls: dict[str, asyncio.Event] = {}
        self._lock = threading.Lock()
        self.on_task_changed: Callable[[Task], None] | None = None
        self.on_log: Callable[[str, str], None] | None = None

    # ---------------------------------------------------- 事件循环
    def start(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._sem = asyncio.Semaphore(int(config().get("concurrency") or 6))
            loop.call_soon(ready.set)
            loop.run_forever()

        t = threading.Thread(target=runner, name="mangakit-loop", daemon=True)
        t.start()
        ready.wait(5)
        self._thread = t
        log.info("下载管理器已启动")

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.http_client().close(), self._loop)
            fut.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None

    def submit(self, coro: Any) -> Future:
        """把协程投递到后台循环，返回 concurrent.futures.Future。"""
        if self._loop is None:
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def http_client(self) -> HttpClient:
        return client()

    # ---------------------------------------------------- 对外操作
    def enqueue_comic(self, comic: Comic, chapters: Iterable[Chapter], folder: Path | None = None) -> list[str]:
        ids: list[str] = []
        for ch in chapters:
            ids.append(self.enqueue(comic, ch, folder))
        return ids

    def enqueue(self, comic: Comic, chapter: Chapter, folder: Path | None = None) -> str:
        cfg = config()
        tid = uuid.uuid4().hex[:12]
        base = folder or Path(str(cfg.get("download_dir")))
        vol = safe_filename(chapter.volume or chapter.title or f"第{chapter.index + 1}话")
        comic_dir = safe_filename(comic.title or "未命名")
        try:
            path = Path(base) / comic_dir / vol
        except Exception:
            path = Path(base) / "downloads"
        t = Task(
            tid=tid,
            comic=comic.title,
            chapter=chapter.title,
            folder=str(path),
            status=STATUS_QUEUED,
            created=_now(),
        )
        ctx = TaskContext(
            task=t, comic_title=comic.title or "未命名", chapter_title=chapter.title or vol,
            comic_url=comic.url, chapter_url=chapter.url, folder=path,
        )
        with self._lock:
            self._tasks[tid] = t
        store().upsert_task(t)
        store().kv_set(f"ctx:{tid}", {
            "comic_url": ctx.comic_url, "chapter_url": ctx.chapter_url,
            # folder 是 Path，存进 SQLite 前必须转 str —— 直接塞给 json.dumps
            # 会抛 TypeError: Object of type WindowsPath is not JSON serializable，
            # 而且异常发生在 Qt 槽里会被吞掉，表现为「点下载毫无反应」。
            "folder": str(ctx.folder),
            "title": ctx.comic_title,
        })
        self.submit(self._run(ctx))
        return tid

    def pause(self, tid: str) -> None:
        with self._lock:
            t = self._tasks.get(tid)
            if t and t.status == STATUS_RUNNING:
                self._paused.add(tid)
                ev = self._controls.get(tid)
                if ev:
                    ev.set()
                t.status = STATUS_PAUSED
                self._emit(t)

    def resume(self, tid: str) -> None:
        with self._lock:
            self._paused.discard(tid)
            t = self._tasks.get(tid)
            ctx = store().kv_get(f"ctx:{tid}")
        if t and ctx:
            t.status = STATUS_QUEUED
            t.error = ""
            self.submit(self._run(TaskContext(
                task=t, comic_title=ctx.get("title", ""),
                chapter_title=t.chapter, comic_url=ctx.get("comic_url", ""),
                chapter_url=ctx.get("chapter_url", ""), folder=Path(ctx.get("folder", ".")),
            )))

    def retry(self, tid: str) -> None:
        self.resume(tid)

    def cancel(self, tid: str) -> None:
        with self._lock:
            t = self._tasks.pop(tid, None)
            self._paused.add(tid)
            ev = self._controls.get(tid)
            if ev:
                ev.set()
        if t:
            t.status = STATUS_SKIPPED
            store().update_task(t)
            self._emit(t)

    def clear_finished(self) -> None:
        store().clear_finished()
        with self._lock:
            for tid in [k for k, v in self._tasks.items() if v.status in (STATUS_DONE, STATUS_SKIPPED)]:
                self._tasks.pop(tid, None)

    def tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    # ---------------------------------------------------- 内部执行
    def _emit(self, t: Task) -> None:
        store().update_task(t)
        if self.on_task_changed:
            try:
                self.on_task_changed(t)
            except Exception:
                log.debug("on_task_changed 异常", exc_info=True)

    def _log(self, msg: str, level: str = "info") -> None:
        log.info(msg)
        if self.on_log:
            try:
                self.on_log(msg, level)
            except Exception:
                pass

    async def _run(self, ctx: TaskContext) -> None:
        t = ctx.task
        assert self._sem is not None
        async with self._sem:
            if t.status == STATUS_SKIPPED:
                return
            t.status = STATUS_RUNNING
            t.started = t.started or _now()
            t.error = ""
            self._emit(t)
            try:
                await self._download_chapter(ctx)
                t.status = STATUS_DONE
                t.finished = t.total
                self._log(f"✅ {t.comic} / {t.chapter} 完成（{t.total} 页）")
            except asyncio.CancelledError:
                t.status = STATUS_PAUSED
                self._emit(t)
                raise
            except ParseError as e:
                t.status = STATUS_ERROR
                t.error = f"解析失败: {e}"
                self._log(f"⚠ {t.comic} / {t.chapter}: {t.error}", "error")
            except WriteDenied as e:
                # 目录不可写：说人话 + 给修复步骤，别甩裸 PermissionError
                t.status = STATUS_ERROR
                t.error = str(e)
                self._log(f"✖ {t.comic} / {t.chapter}: 目录不可写", "error")
                log.warning("目录不可写：%s", e)
            except PermissionError as e:
                # 兜底：还有别处能冒出来的裸权限错误（如最终合并 part 时）
                t.status = STATUS_ERROR
                t.error = f"写入被系统拒绝（{e.filename}）：{_why_unwritable(e)}"
                self._log(f"✖ {t.comic} / {t.chapter}: {t.error}", "error")
                log.debug(traceback.format_exc())
            except Exception as e:
                t.status = STATUS_ERROR
                t.error = f"{type(e).__name__}: {e}"
                self._log(f"✖ {t.comic} / {t.chapter}: {t.error}", "error")
                log.debug(traceback.format_exc())
            finally:
                self._emit(t)
                with self._lock:
                    self._controls.pop(t.tid, None)

    async def _download_chapter(self, ctx: TaskContext) -> None:
        cfg = config()
        t = ctx.task
        found = registry().for_url(ctx.chapter_url)
        if not found:
            raise ParseError(f"没有匹配的站点规则：{ctx.chapter_url}")
        site, adapter = found

        # 可写性闸门放在最前面：解析和抓图都要花时间，先确认落盘没问题，
        # 否则用户等半天才看到一句「拒绝访问」。
        #
        # 这一步必须排在 mkdir 之前 —— 目录建不出来时裸 mkdir 抛的正是
        # PermissionError: [WinError 5]，那正是用户看到的报错。
        # 只试 mkdir 也不够：受限环境（沙箱/隔离目录/被组策略接管的用户目录）下
        # 目录往往建得出来、空文件却建不出来，硬扛到下面 tmp.write_bytes 才炸。
        ensure_writable(ctx.folder)
        part = ctx.folder / f"{safe_filename(ctx.chapter_title)}.part"
        ensure_writable(part)

        urls = await adapter.parse_pages(ctx.chapter_url)
        maxp = int(cfg.get("max_chapter_pages") or 3000)
        if len(urls) > maxp:
            urls = urls[:maxp]
        t.total = len(urls)
        t.finished = 0
        self._emit(t)

        ok = 0
        sem_img = asyncio.Semaphore(max(2, int(cfg.get("concurrency") or 6)))
        results: dict[int, bytes] = {}
        errors: list[str] = []
        # 目录不可写是「整体性」的故障：不是某一张图失败，重试也没用。
        # 用一个独立容器把它带出并发循环（gather(return_exceptions=True) 会把
        # 协程里的异常变成返回值，直接 raise 等于丢进垃圾桶）。
        denied: list[str] = []

        async def one(i: int, url: str) -> None:
            nonlocal ok
            async with sem_img:
                if denied:
                    return
                dest = self._page_path(part, i, len(urls))
                if dest.exists() and dest.stat().st_size > 0:      # 断点续传：跳过已下载
                    ok += 1
                    results[i] = b""
                    self._progress(t, ctx.folder)
                    return
                for attempt in range(1, int(cfg.get("retries") or 3) + 1):
                    try:
                        data, ctype = await self.http_client().download(
                            url,
                            headers=adapter.site.headers,
                            cookies=adapter.site.cookies or None,
                            max_bytes=int(cfg.get("max_image_mb") or 40) * 1024 * 1024,
                        )
                        if not is_image(data):
                            raise ValueError(f"返回的不是图片 (ctype={ctype}, {len(data)}B)")
                        if cfg.get("verify_image") and not verify(data):
                            raise ValueError("图片校验失败")
                        data, ext = finalize(data, convert_webp=bool(cfg.get("convert_webp")))
                        dest = self._page_path(part, i, len(urls), ext)
                        tmp = dest.with_suffix(dest.suffix + ".tmp")
                        try:
                            tmp.write_bytes(data)
                            os.replace(tmp, dest)
                        except PermissionError as pe:
                            # 目录中途变得不可写（同步盘锁定、磁盘弹出、被安全软件
                            # 隔离）：记下来让整章立刻中止，别让每一页都重试 3 次
                            # 再报同一个错、把日志刷成几百行。
                            denied.append(
                                f"写入被系统拒绝：{tmp}\n原因：{_why_unwritable(pe)}\n"
                                f"请把 MangaKit 移到桌面等可写目录，或在「设置」里换一个下载目录。")
                            return
                        # 长图切割
                        if cfg.get("auto_split_long"):
                            pieces = split_long(data, int(cfg.get("split_min_height") or 1600))
                            if len(pieces) > 1:
                                for j, pd in enumerate(pieces, 1):
                                    p2 = dest.with_name(f"{dest.stem}_s{j:02d}{dest.suffix}")
                                    p2.write_bytes(pd)
                        ok += 1
                        results[i] = b""
                        self._progress(t, ctx.folder, i)
                        return
                    except Exception as e:
                        if attempt >= int(cfg.get("retries") or 3):
                            errors.append(f"第{i + 1}页 {url} — {e}")
                            log.warning("页 %d 下载失败: %s", i + 1, e)
                            return
                        await asyncio.sleep(min(2 ** attempt * 0.4, 5))

        await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)), return_exceptions=True)
        if denied:
            raise WriteDenied(denied[0])
        t.finished = ok

        if ok == 0:
            raise RuntimeError("全部页面下载失败：" + ("；".join(errors[:3]) or "未知原因"))
        if errors:
            t.error = f"{len(errors)} 页失败：" + "；".join(errors[:2])

        # 合并 part 目录 → 正式目录
        files = sorted(p for p in part.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
        if ctx.folder.exists():
            for old in ctx.folder.iterdir():
                if old.is_file():
                    old.unlink()
        ctx.folder.mkdir(parents=True, exist_ok=True)
        for f in files:
            os.replace(f, ctx.folder / f.name)
        try:
            part.rmdir()
        except OSError:
            pass

        # 自动打包
        if str(cfg.get("pack_after") or "off") != "off":
            try:
                from .exporter import pack_cbz
                mode = str(cfg.get("pack_after"))
                target = pack_cbz(ctx.folder, mode)
                self._log(f"📦 已打包 {target.name}")
            except Exception as e:
                self._log(f"打包失败：{e}", "error")

    def _page_path(self, part: Path, i: int, total: int, ext: str = ".jpg") -> Path:
        return part / page_filename(i, total, ext)

    def _progress(self, t: Task, folder: Path, idx: int = -1) -> None:
        t.finished += 0 if idx < 0 else 0
        el = max(1.0, _now() - (t.started or _now()))
        t.speed = t.finished / el
        t.eta = (t.total - t.finished) / t.speed if t.speed > 0 else 0
        self._emit(t)


_shared: DownloadManager | None = None


def manager() -> DownloadManager:
    global _shared
    if _shared is None:
        _shared = DownloadManager()
    return _shared