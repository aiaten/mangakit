"""程序入口。

用法：
  python -m mangakit            启动 GUI
  python -m mangakit --url URL  直接把某个漫画页丢进解析并列出章节
  python -m mangakit --get URL  抓取章节并下载所有页到下载目录
  python -m mangakit --sites    列出内置站点
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import sys
import time
import traceback
from pathlib import Path


def _trace(msg: str) -> None:
    """诊断用的极简落盘日志。

    打包后的 windowed exe 没有控制台，出错也看不见。设了 MANGAKIT_TRACE=1
    就会把每一步写到 exe 同目录的 mangakit_selftest.log，用来定位卡死位置。
    """
    if not os.environ.get("MANGAKIT_TRACE"):
        return
    try:
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
        path = base if base.name == "mangakit" else Path(sys.executable).parent
        with (path / "mangakit_selftest.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _bootstrap() -> None:
    """让 PyInstaller onefile 与源码运行都能 import 到包。"""
    if getattr(sys, "frozen", False):
        os.environ.setdefault("MANGAKIT_FROZEN", "1")
        # onefile 被解包到 _MEIPASS；包以 .pyc 形式在 PYZ 里，
        # 站点规则是 datas，必须显式把 _MEIPASS 指给 rules_dir()。
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        rules = os.path.join(base, "mangakit", "rules")
        if os.path.isdir(rules):
            os.environ["MANGAKIT_RULES_DIR"] = rules
    # windowed exe 的 stdout 接的是空设备，Python 会退回 GBK 编码，
    # 一遇中文/符号就 UnicodeEncodeError 把进程带崩。强制 UTF-8 并容错。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    # 无论 frozen 与否，都把包所在目录放进 sys.path：
    # 源码从任意 cwd 运行、frozen 从 __main__.py 绝对导入时都靠它。
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)


_splash_state: bool | None = None
"""进程级缓存：None = 还没判定过（见 _splash_available）。

必须缓存，不能每次现查环境变量：`pyi_splash` 在 import 成功后会自己
`del os.environ['_PYI_SPLASH_IPC']`，第二次查就查不到了 —— 那样
`_splash_close()` 会被误判成「没有启动画面」而漏掉关闭。
"""


def _splash_available() -> bool:
    """当前进程里是否真的挂了启动画面（决定能不能 import pyi_splash）。

    ★ 必须先看环境变量，不能直接 `import pyi_splash`。
    PyInstaller 的 pyi_splash.py 在**模块级**就执行
    `_ipc_port = int(os.environ['_PYI_SPLASH_IPC'])`，读不到就走进
    `except (KeyError, ValueError)` 分支，往日志里写一条 warning **外加
    traceback**（日志里表现为 `KeyError: '_PYI_SPLASH_IPC'`）：
    "The environment does not allow connecting to the splash screen."

    这个副作用发生在 import 那一刻，函数内的 try/except 拦不住。
    而 `_PYI_SPLASH_IPC` 是 bootloader 在真的有 splash 时才设的：
      - spec 里写了 Splash() → 变量存在，值是该 TCP 端口；
      - splash 被优雅抑制（如子进程）→ 变量设为 "0"；
      - spec 里没有 Splash()（v0.3.1 起）→ 变量**根本不存在**，
        于是每次启动都白刷一条 warning + traceback 进用户日志。
    所以这里用「变量在不在」当开关：没有 splash 时压根不 import，
    `pyi_splash` 从未被加载，那条警告也就不会再出现。
    """
    global _splash_state
    if _splash_state is None:
        if os.environ.get("_PYI_SPLASH_IPC") is None:
            _splash_state = False
        else:
            try:
                import pyi_splash  # type: ignore[import-not-found]  # 仅打包态存在

                _splash_state = bool(getattr(pyi_splash, "_initialized", False))
            except Exception:  # noqa: BLE001
                _splash_state = False
    return _splash_state


def _splash_update(text: str) -> None:
    """更新启动画面上的进度文字（没有启动画面时静默无操作）。

    背景：v0.3.1 起 spec 里已移除 `Splash()`，所以打包态下本函数同样是空操作
    —— 留着是为了将来恢复启动画面时不用再改这 5 个调用点。

    两层兜：先 `_splash_available()` 判断该不该碰这个模块（见那里的说明，
    直接 import 会往日志吐警告），再逐个调用都包 try/except
    —— 启动画面永远不该把程序搞崩。
    """
    if not _splash_available():
        return
    try:
        import pyi_splash  # type: ignore[import-not-found]

        if pyi_splash.is_alive():
            pyi_splash.update_text(text)
    except Exception:  # noqa: BLE001
        pass


def _splash_close() -> None:
    """关掉启动画面（主窗口已经显示出来之后调用）。"""
    if not _splash_available():
        return
    try:
        import pyi_splash  # type: ignore[import-not-found]

        pyi_splash.close()
    except Exception:  # noqa: BLE001
        pass


def _configure_webengine_sandbox() -> None:
    """打包态启用 Chromium 沙箱豁免 —— 必须在 import QtWebEngine* 之前调用。

    根因（实测）：onefile 打包后 QWebEngineView 能建出来、也能 load()，但
    渲染进程起不来，页面永远白屏，loadFinished 收到 ok=False。原因是
    QtWebEngineProcess.exe 从 _MEIPASS 里启动时被 Chromium 自己的沙箱挡住。
    源码运行没有这个问题，所以只在 frozen 态设。

    必须在「任何 import PySide6.QtWebEngine* 之前」设好环境变量：QtWebEngine
    在第一次 import 时就把这些旗标读进原生库并初始化，之后再设无效。

    这里同时给两个变量（任一即可生效）：CHROMIUM_FLAGS 走 Chromium 命令行，
    DISABLE_SANDBOX 是 Qt 自己的开关。用 setdefault 是为了不覆盖用户/
    排障时自己设的值（例如 MANGAKIT_TRACE 排查时想换成别的旗标组合）。
    """
    if not getattr(sys, "frozen", False):
        return
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")


def _runtime_tmpdir_note() -> None:
    """把一个「解包目录到底在哪」记进日志，方便支持用户。

    runtime_tmpdir 在 mangakit.spec 里写的是 %LOCALAPPDATA%\\MangaKit\\run，
    bootloader 在运行期展开它并建出缺失的父目录，所以正常情况下
    _MEIPASS 一定落在用户目录下，不会因为 cwd 不同而漂移。
    （曾经用过相对路径 .mkrun，它按 cwd 解析 —— 从其它目录的快捷方式启动
    就会解包到那个目录，甚至 C:\\Windows\\System32，已废弃。）
    """
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        _trace(f"runtime tmpdir = {meipass}")


def _gui_error_box(title: str, text: str) -> None:
    """GUI 还没起来时也要能让人看见错误：优先 Qt 弹窗，退路是 Win32 消息框。

    绝不能只 print —— windowed exe 的 stdout 接空设备，用户双击后什么也看不到，
    只会以为「点了没反应」。这正是「启动自检」必须嵌进 exe 的理由。
    """
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        own = QApplication.instance() is None
        app = QApplication(sys.argv) if own else QApplication.instance()
        QMessageBox.critical(None, title, text)
        if own:
            app.quit()
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
        except Exception:  # noqa: BLE001
            pass


def _startup_preflight() -> int:
    """启动自检：下载目录/数据目录真能写吗？不能写就弹窗说清楚再退出。

    这段逻辑以前在「启动漫卷.bat」里用 echo/dir 探测，用户必须从 bat 启动才
    受保护；现在嵌进 exe，双击 exe 也走同一条路（用户要求）。

    为什么必须「建目录 + 真正写一个文件」两步：受限环境（沙箱、隔离工作区、
    同步盘占位）常常允许 mkdir 却拒绝建文件，只试 mkdir 会判成可写，错误一路
    漏到下载时才炸，那时用户已经等了很久。probe_writable() 已经做了两步，
    这里复用，不重造。

    返回 0 = 通过，非 0 = 已弹窗、调用方应直接退出。
    """
    from .config import WriteDenied, config, ensure_writable

    _trace("preflight start")
    cfg = None
    try:
        cfg = config()
        dl = Path(cfg.download_dir)
    except Exception as e:  # noqa: BLE001 —— 配置层自己坏了也得给人话，不能白屏
        _trace(f"preflight config fail: {e!r}")
        _gui_error_box(
            "漫卷 MangaKit 无法启动",
            "读取配置失败，程序无法确定下载目录。\n\n"
            f"原因：{type(e).__name__}: {e}\n\n"
            f"配置文件：{getattr(cfg, 'path', '(未知)')}\n\n"
            "怎么办：\n"
            "  1) 删掉上面这个 config.json 再启动（会重建为默认设置）；\n"
            "  2) 或设环境变量 MANGAKIT_HOME 指向一个可写目录后重启。",
        )
        return 1

    try:
        ensure_writable(dl)
    except WriteDenied as e:
        _trace(f"preflight denied: {e}")
        _gui_error_box(
            "漫卷 MangaKit 无法写入下载目录",
            f"{e}\n\n"
            f"当前下载目录：{dl}\n"
            f"程序也可以放到桌面或「文档」目录下运行（那里一定可写）。",
        )
        return 2
    except Exception as e:  # noqa: BLE001
        _trace(f"preflight unexpected: {e!r}")
        _gui_error_box(
            "漫卷 MangaKit 启动自检失败",
            f"检查下载目录时发生意外错误。\n\n"
            f"目录：{dl}\n原因：{type(e).__name__}: {e}",
        )
        return 3

    _trace("preflight ok")
    return 0


def _run_gui() -> int:
    from . import logsetup
    logsetup.setup("INFO")
    _trace("_run_gui entered")

    # 必须在 import 任何 PySide6.QtWebEngine* 之前。gui.main_window 是懒加载
    # WebEngine 的，但这里提前设好最保险：一旦别处先 import 了就来不及了。
    _configure_webengine_sandbox()
    _runtime_tmpdir_note()

    _splash_update("正在检查运行环境…")
    rc = _startup_preflight()
    if rc:
        _splash_close()
        return rc

    _splash_update("正在加载界面…")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .gui.main_window import MainWindow

    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("MangaKit")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    _splash_update("正在打开主窗口…")
    w = MainWindow()
    w.show()
    # 主窗口已经画出来了，启动画面的使命结束。必须在这里关：再晚（比如等
    # event loop 里某个定时器）用户会看到两个窗口叠着，再早则主窗口还没出现、
    # 屏幕一片空白。
    _splash_close()
    _trace("main window shown")
    return app.exec()



def _run_cli(args: argparse.Namespace) -> int:
    from . import logsetup
    logsetup.setup("INFO")
    from .config import WriteDenied, _why_unwritable, config, ensure_writable
    from .downloader import manager
    from .http_client import client
    from .sites import ParseError, registry

    async def go() -> int:
        await client().start()
        reg = registry()
        found = reg.for_url(args.url)
        if not found:
            print(f"× 没有匹配的站点规则：{args.url}")
            print("支持这些域名：")
            for h in reg.supported_hosts():
                print("   ", h)
            return 2
        site, adapter = found
        print(f"站点：{site.name}（{site.id}）\n网址：{args.url}\n")
        comic = await adapter.parse_comic(args.url)
        print(f"漫画：{comic.title}")
        if comic.author:
            print(f"作者：{comic.author}")
        if comic.tags:
            print("标签：" + "、".join(comic.tags[:10]))
        if comic.description:
            print("简介：" + comic.description[:160].replace("\n", " "))
        print()
        chs = await adapter.parse_chapters(args.url)
        print(f"章节数：{len(chs)}")
        for ch in chs[:12]:
            print(f"   [{ch.index:>3}] {ch.title}")
        if len(chs) > 12:
            print(f"   …… 另有 {len(chs) - 12} 章")
        print()

        target = chs[0] if chs else None
        if args.get:
            if not target:
                print("没有章节可下载")
                return 3
            target = chs[args.get - 1] if 0 < args.get <= len(chs) else target
            print(f"下载第 {args.get} 章：{target.title}")
            pages = await adapter.parse_pages(target.url)
            print(f"图片数：{len(pages)}")
            from pathlib import Path
            from .images import finalize, is_image, page_filename, safe_filename
            outdir = Path(config().download_dir) / safe_filename(comic.title) / safe_filename(target.title)
            try:
                ensure_writable(outdir)
            except WriteDenied as e:
                print(f"\n✗ {e}")
                await client().close()
                return 4
            ok = 0
            for i, u in enumerate(pages[: args.limit]):
                try:
                    data, ct = await client().download(
                        u, headers=site.headers, cookies=site.cookies or None)
                    if not is_image(data):
                        print(f"  [{i+1}] 非图片 {ct} {len(data)}B")
                        continue
                    data, ext = finalize(data, convert_webp=bool(config().get("convert_webp")))
                    try:
                        (outdir / page_filename(i, len(pages), ext)).write_bytes(data)
                    except PermissionError as e:
                        print(f"  [{i+1}] ✗ 写入被系统拒绝：{e.filename}")
                        print(f"       {_why_unwritable(e)}")
                        print("       请把 MangaKit 移到桌面等可写目录，或改「设置 → 下载目录」。")
                        break
                    ok += 1
                    print(f"  [{i+1}/{len(pages)}] ✓ {len(data)//1024}KB -> {page_filename(i, len(pages), ext)}")
                except Exception as e:
                    print(f"  [{i+1}] ✗ {e}")
            print(f"\n完成 {ok} 张，输出目录：{outdir}")
            if ok:
                try:
                    from .exporter import to_cbz
                    print("CBZ：", to_cbz(outdir))
                except Exception as e:  # noqa: BLE001 —— 打包失败不该盖掉已下载的结果
                    print(f"CBZ 打包失败：{e}")
        await client().close()
        return 0

    try:
        return asyncio.run(go())
    except ParseError as e:
        print(f"× 解析失败：{e}")
        return 4
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 —— CLI 不能把 traceback 甩给用户
        # httpx 的 str(e) 会带三行 "For more information see ..."，只取第一行
        print(f"\n× 失败：{type(e).__name__}: {str(e).splitlines()[0]}")
        hint = {
            404: "这个网址可能已经失效，或不是漫画详情页。",
            403: "被站点拒绝，可能需要 Cookie（设置页给该站点填 cookies）或访问频率过快。",
        }.get(getattr(getattr(e, "response", None), "status_code", None))
        if hint:
            print(f"  {hint}")
        _log_exc()
        return 5


def _log_exc() -> None:
    """把完整 traceback 写进日志文件（CLI 只给用户一行人话）。"""
    import logging
    import traceback
    logging.getLogger(__name__).error(
        "CLI 失败：%s", traceback.format_exc())


def _run_selftest(out_file: str, show_ui: bool = False) -> int:
    """离线自检。

    打包后的 exe 是 windowed（没有控制台），没法用 --sites/--url 观察输出，
    所以专门提供一个「结果写文件」的通道，方便自动冒烟：MangaKit.exe --selftest r.txt
    """
    import json
    import tempfile

    from . import __app_name__, __version__

    _trace(f"selftest begin, frozen={getattr(sys, 'frozen', False)}, argv={sys.argv!r}")
    lines: list[str] = [f"{__app_name__} {__version__} selftest"]
    bad = 0

    def flush() -> None:
        """每步跑完立刻落盘，这样即使后面卡死/崩溃也知道停在哪一步。

        依次尝试：指定路径 → 同目录 selftest.txt → 系统临时目录 → 应用数据目录。
        """
        text = "\n".join(lines) + "\n"
        cands = [Path(out_file), Path(out_file).with_name("selftest.txt"),
                 Path(tempfile.gettempdir()) / "mangakit_selftest.txt"]
        try:
            cands.append(Path(os.environ.get("APPDATA", tempfile.gettempdir())) / "mangakit" / "selftest.txt")
        except Exception:  # noqa: BLE001
            pass
        for path in cands:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                return
            except OSError:
                continue

    flush()

    def step(name: str, fn) -> None:
        nonlocal bad
        lines.append(f".... {name} 开始")
        flush()
        _trace(f"step {name} start")
        try:
            detail = fn()
            lines.append(f"OK   {name}{(' - ' + detail) if detail else ''}")
        except BaseException as e:  # noqa: BLE001 —— 这里要连 SystemExit/KeyboardInterrupt 都接住
            bad += 1
            lines.append(f"FAIL {name} - {type(e).__name__}: {e}")
        _trace(f"step {name} end")
        flush()

    from .config import app_dir, config, rules_dir

    def _cfg():
        cfg = config()
        assert isinstance(cfg.get("download_dir"), str)
        return f"home={app_dir()}"

    def _rules():
        from .sites import registry

        reg = registry()
        n = len(reg.all_sites())
        assert n > 0, "没有加载到任何站点规则"
        return f"{n} sites, {len(reg.supported_hosts())} hosts"

    def _db():
        from .models import Chapter, Task
        from .store import store

        st = store()
        t = Task(tid="selftest", comic="自检漫画", chapter="第1话", url="https://example.invalid/1",
                 site="selftest", chapter_id="selftest")
        st.upsert_task(t)
        t.status, t.total, t.finished = "done", 1, 1
        st.update_task(t)
        rows = [dict(r) for r in st.all_tasks()]
        assert any(r["tid"] == "selftest" for r in rows), f"写入后读不到: {rows}"
        st.delete_task("selftest")
        st.save_chapters("selftest", [{"cid": "s1", "url": "https://example.invalid/1",
                                       "title": "第1话", "index": 0}])
        assert len(st.get_chapters("selftest", "s1")) == 1, "章节写入后读不到"
        return "sqlite ok"

    def _exporter():
        import tempfile

        from .exporter import to_cbz

        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "ch"
            d.mkdir()
            from PIL import Image

            for i in range(3):
                Image.new("RGB", (8, 12), (40 * i % 255, 80, 160)).save(d / f"{i + 1:03d}.png")
            cbz = to_cbz(d)
            assert Path(cbz).is_file() and Path(cbz).stat().st_size > 0
        return "cbz ok"

    def _webengine():
        """真起一个 QWebEngineView 加载页面，确认渲染进程能起来。

        这一步专门抓「打包后浏览器白屏」：Chromium 沙箱在 frozen 态会挡住
        QtWebEngineProcess.exe，表现为视图建得出来、loadFinished 却收到
        ok=False，页面永远空白（源码运行看不出来）。只有真加载一次才算过。
        """
        _configure_webengine_sandbox()  # 必须在 import QtWebEngine* 之前
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineWidgets import QWebEngineView

        app = QApplication.instance() or QApplication([])
        view = QWebEngineView()
        view.resize(400, 300)
        got: dict[str, object] = {"ok": None, "text": ""}
        loop = QEventLoop()

        def done(ok: bool) -> None:
            got["ok"] = ok
            view.page().toPlainText(lambda t: (got.__setitem__("text", t), loop.quit()))

        view.loadFinished.connect(done)
        view.setHtml("<html><body>漫卷浏览器<script>document.write('JS-OK')</script></body></html>",
                     QUrl("about:blank"))
        QTimer.singleShot(15000, loop.quit)  # 兜底：绝不把自检挂死
        loop.exec()
        view.deleteLater()
        app.processEvents()
        assert got["ok"] is True, (
            f"页面加载失败（loadFinished ok={got['ok']!r}）—— "
            "打包后 Chromium 沙箱可能挡住了渲染进程，检查 _configure_webengine_sandbox()")
        assert "JS-OK" in str(got["text"]), f"页面加载了但 JS 没跑：{got['text']!r}"
        return "页面可加载、JS 可执行"

    step("config", _cfg)
    step("rules", _rules)
    step("store", _db)
    step("exporter", _exporter)
    step("webengine", _webengine)

    lines.append("SELFTEST PASS" if not bad else f"SELFTEST FAIL ({bad})")
    text = "\n".join(lines) + "\n"
    flush()
    try:  # windowed exe 没有控制台，靠 --selftest-ui 才弹窗给人看（自动冒烟时不要加）
        if show_ui:
            ctypes.windll.user32.MessageBoxW(None, text, __app_name__, 0)
    except Exception:  # noqa: BLE001
        pass
    print(text)
    return 0 if not bad else 1


def main(argv: list[str] | None = None) -> int:
    _trace(f"main() entered, argv={sys.argv!r}")
    _bootstrap()
    _trace(f"bootstrap done, frozen={getattr(sys, 'frozen', False)}")
    p = argparse.ArgumentParser(prog="mangakit", description="漫卷 MangaKit —— 通用漫画批量下载器")
    p.add_argument("--url", help="漫画详情页网址")
    p.add_argument("--get", type=int, default=0, metavar="N", help="解析后下载第 N 章")
    p.add_argument("--limit", type=int, default=8, metavar="N", help="每章最多下载 N 张（默认 8）")
    p.add_argument("--sites", action="store_true", help="列出内置站点")
    p.add_argument("--selftest", metavar="FILE", help="离线自检（配置/数据库/打包器/规则），结果写入 FILE")
    p.add_argument("--selftest-ui", metavar="FILE", help="同 --selftest，额外弹出结果窗口（仅打包后 exe 可用）")
    p.add_argument("--version", action="version", version=_version())
    args = p.parse_args(argv)

    if args.selftest or args.selftest_ui:
        return _run_selftest(args.selftest or args.selftest_ui, bool(args.selftest_ui))

    if args.sites:
        from . import logsetup
        logsetup.setup("WARNING")
        from .sites import registry
        reg = registry()
        for s in reg.all_sites():
            mark = "√" if s.enabled else "×"
            print(f"{mark} {s.id:<16} {s.name:<24} {'、'.join(s.domains)}")
            if s.note:
                print(f"    {s.note}")
        return 0
    if args.url:
        return _run_cli(args)
    return _run_gui()


def _version() -> str:
    from . import __app_name__, __version__
    return f"{__app_name__} {__version__}"


if __name__ == "__main__":
    sys.exit(main())