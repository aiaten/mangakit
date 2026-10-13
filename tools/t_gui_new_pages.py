"""task-3 验收：图源仓库页（SourcePage）与内置浏览器页（BrowserPage）。

四条硬要求，全部离线可跑（不碰真网络）：
  1. 图源页用桩数据能列出行（且构造期不联网）；
  2. 浏览器页懒加载：进入本页之前 QtWebEngineWidgets 不在 sys.modules；
  3. ycksource import 失败时页面显示「图源模块不可用」而不崩；
  4. 校验失败（ok=False）绝不写入 config。

跑法（从 mangakit\\ 目录）：
    $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
    $env:QT_QPA_PLATFORM='offscreen'
    $env:PYTHONPATH=(Get-Location).Path
    python tools\\t_gui_new_pages.py
"""
import gc
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 必须在 import mangakit 之前打好：config 落盘路径由 MANGAKIT_HOME 决定，
# 否则这个脚本会往用户真实配置里写东西（第 4 条断言也会被历史数据污染）。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_t3_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 压掉 offscreen 下 Chromium 的沙箱/GL 噪音（不影响懒加载断言）
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

sys.path.insert(0, str(ROOT))

RESULTS: list[bool] = []


def ck(cond: bool, label: str, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append(ok)
    tail = f" -> {detail}" if (detail and not ok) else (f"（{detail}）" if detail else "")
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{tail}", flush=True)
    return ok


def pump(app, seconds: float = 6.0, until=None) -> bool:
    """等后台线程的结果经队列信号回到主线程；不 pump 事件循环永远等不到。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return bool(until is not None and until())


def refresh_and_wait(app, page, seconds: float = 15.0) -> bool:
    """刷新并等到 _on_list 真的跑完（_busy 落回 False）。

    只等「池子里的函数被调用」是不够的：那只说明后台线程开工了，结果还没经
    队列回到主线程；此时表格还是上一批数据，选中行之后 `_fill()` 才执行，
    会把刚选的清掉 —— 表现成「选了行按钮却没亮」的假失败。
    """
    page.refresh()
    return pump(app, seconds, lambda: not page._busy)


class StubEntry:
    """结构上等价于 ycksource.SourceEntry（只用属性，GUI 侧不做 isinstance）。"""

    def __init__(self, sid, name, site="", updated="", uploader="",
                 downloads=0, can_search=False, can_find=False, json_url=""):
        self.sid = sid
        self.name = name
        self.site = site
        self.updated = updated
        self.uploader = uploader
        self.downloads = downloads
        self.can_search = can_search
        self.can_find = can_find
        self.json_url = json_url


class StubResult:
    """等价于 ycksource.CheckResult（GUI 侧只读这几个字段）。"""

    def __init__(self, ok, status="fail", message="", chapters=0, images=0,
                 site_name="", rule=None, log=None):
        self.ok = ok
        self.status = status
        self.message = message
        self.chapters = chapters
        self.images = images
        self.site_name = site_name
        self.rule = rule
        self.log = log or []


class StubModule:
    """桩 ycksource：记录调用、返回预置数据，不发任何网络请求。"""

    def __init__(self, entries=None, result=None):
        self.entries = entries or []
        self.result = result
        self.calls: list[tuple] = []

    def list_sources(self, *, page=1, keys="", order="time", **kw):
        self.calls.append(("list", page, keys, order))
        return list(self.entries)

    def check_source(self, sid, *, deep=True, **kw):
        self.calls.append(("check", int(sid), deep))
        return self.result

    def add_checked_source(self, sid, *, deep=True, **kw):
        self.calls.append(("add", int(sid), deep))
        return self.result

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def install_stub(mw, stub) -> None:
    """把 ycksource 换成桩（绕过真实 import，也不改 ycksource.py）。"""
    mw._yck_mod = stub
    mw._yck_err = ""


def reset_yck(mw) -> None:
    mw._yck_mod = None
    mw._yck_err = ""


# ------------------------------------------------------------------ 1. 懒加载

def check_lazy_webengine(app, mw) -> bool:
    """进入浏览器页之前，QtWebEngineWidgets 绝不能出现在 sys.modules 里。

    注意：**不要**先 import 一次再 pop 掉 sys.modules —— PySide6 的扩展模块
    被重复 PyInit 会让进程直接崩（无 traceback，退出码 1）。这里全程不预先
    import WebEngine，只记录「import 入口有没有被调用」，真类由原入口自己去取。
    """
    ok0 = ck("PySide6.QtWebEngineWidgets" not in sys.modules
             and "PySide6.QtWebEngineCore" not in sys.modules,
             "前置条件：本脚本此刻还没 import 过 WebEngine",
             f"leaked={[m for m in sys.modules if 'WebEngine' in m]}")

    mw._web_mod = None
    mw._web_err = ""
    orig_import = mw._import_webengine
    calls: list[int] = []

    def recorder():
        calls.append(1)
        return orig_import()      # 真 import 只在这里发生一次

    mw._import_webengine = recorder

    win = mw.MainWindow()
    win.show()
    app.processEvents()
    ok1 = ck("PySide6.QtWebEngineWidgets" not in sys.modules
             and "PySide6.QtWebEngineCore" not in sys.modules,
             "构造并显示 MainWindow 后仍未 import WebEngine")
    ok2 = ck(calls == [], "构造 MainWindow 时未调用 WebEngine 的 import 入口",
             f"calls={calls}")

    page = win.browser
    ok3 = ck(page.view is None, "BrowserPage 构造后尚未创建 QWebEngineView")

    # 切到浏览器页：_nav(6) -> showEvent -> ensure_view()
    win._goto(6)
    app.processEvents()
    pump(app, 8.0, lambda: page.view is not None)
    ok5 = ck("PySide6.QtWebEngineWidgets" in sys.modules,
             "首次进入浏览器页后才 import WebEngine")
    ok6 = ck(calls == [1], "WebEngine 的 import 入口只被调用一次", f"calls={calls}")
    ok7 = ck(page.view is not None, "首次进入后才创建 QWebEngineView")

    # 再切走再切回来，不该重复创建
    win._goto(0)
    app.processEvents()
    win._goto(6)
    app.processEvents()
    ok8 = ck(calls == [1] and page._host.count() == 2,
             "再次进入浏览器页复用同一个视图", f"calls={calls} host={page._host.count()}")

    # 地址栏 + 导航按钮 + 首页地址
    ok9 = ck(page.ed_url.text().startswith("https://")
             and "yck2026" in mw.DEFAULT_HOME_URL,
             "默认首页指向 yck2026 图源索引页", page.ed_url.text())
    # 地址栏回车要真的触发导航：直接 emit returnPressed 走完整的信号链路，
    # 比看 receivers() 更硬（PySide6 的 receivers() 只收 signal 签名字符串）。
    nav_seen: list[str] = []
    orig_nav = page._navigate
    page._navigate = lambda u: nav_seen.append(u)
    page.ed_url.setText("example.com/zz")
    page.ed_url.returnPressed.emit()
    page._navigate = orig_nav
    ok10 = ck(nav_seen == ["example.com/zz"] and callable(page.open_url),
              "地址栏回车触发导航，且提供公开 open_url()", f"nav_seen={nav_seen}")
    ok11 = ck(mw.normalize_url("example.com/x") == "https://example.com/x"
              and mw.normalize_url("http://a.b") == "http://a.b"
              and mw.normalize_url("about:blank") == "about:blank"
              and mw.normalize_url("file:///C:/x.html") == "file:///C:/x.html"
              and mw.normalize_url("localhost:8080/a") == "https://localhost:8080/a"
              and mw.normalize_url("") == "",
              "normalize_url 补 https:// 且不弄坏 about:/file:/主机:端口")
    # 空 urlChanged 不能把用户输的地址清掉。
    page.ed_url.setText("https://keep.me/typed")
    page._on_url_changed(mw.QUrl(""))
    ok12 = ck(page.ed_url.text() == "https://keep.me/typed",
              "空 urlChanged 不清空地址栏（用户还能复制）", page.ed_url.text())
    win.close()
    mw._import_webengine = orig_import
    return all([ok0, ok1, ok2, ok3, ok5, ok6, ok7, ok8, ok9, ok10, ok11, ok12])


# --------------------------------------------------- 2. 页面索引与「在浏览器打开」

def check_page_wiring(app, mw) -> bool:
    """新页必须挂在栈末尾，不能打乱既有索引（_nav 里有硬编码）。"""
    win = mw.MainWindow()
    app.processEvents()

    ok1 = ck(win.stack.count() == 7 and win.nav.count() == 7,
             "栈与导航都是 7 项", f"stack={win.stack.count()} nav={win.nav.count()}")
    ok2 = ck(win.stack.widget(3) is win.favs and win.nav.item(3).text() == "收藏",
             "既有索引没被挤动：3 仍是收藏页")
    ok3 = ck(win.stack.widget(4) is win.settings, "4 仍是设置页")
    ok4 = ck(win.stack.widget(5) is win.sources
             and win.nav.item(5).text() == "图源仓库",
             "图源仓库页加在索引 5")
    ok5 = ck(win.stack.widget(6) is win.browser
             and win.nav.item(6).text() == "内置浏览器",
             "内置浏览器页加在索引 6")

    # _nav(3) 的收藏刷新硬编码仍然有效
    fired: list[int] = []
    orig_reload = win.favs.reload

    def spy():
        fired.append(1)
        return orig_reload()

    win.favs.reload = spy
    win.nav.setCurrentRow(3)
    app.processEvents()
    win.favs.reload = orig_reload
    ok6 = ck(fired == [1] and win.stack.currentIndex() == 3,
             "_nav(3) 仍然触发收藏页刷新")

    # 详情页「在浏览器中打开」-> MainWindow.open_in_browser -> 切到 6
    ok7 = ck(hasattr(win.manga, "btn_browser") and hasattr(win.sources, "btn_browser"),
             "详情页与图源页都有「在浏览器中打开」按钮")
    url = "https://example.invalid/book/7/"
    win.manga.comic = None
    win.manga._url = url
    got: list[str] = []
    win.browser.open_url = lambda u: got.append(u)   # 不真起 WebEngine
    win.manga._open_in_browser()
    app.processEvents()
    ok8 = ck(got == [url] and win.stack.currentIndex() == 6,
             "详情页按钮把网址交给浏览器页并切到 6", f"got={got} idx={win.stack.currentIndex()}")
    win.close()
    return all([ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8])


# ------------------------------------------------------- 3. 图源页：桩数据列行

def check_source_list(app, mw) -> bool:
    entries = [
        StubEntry(11, "看漫画", "https://m.manhuagui.com", "2026-10-01", "阿甲",
                  12345, True),
        StubEntry(22, "拷贝漫画", "https://copymanga.site", "2026-09-20", "阿乙",
                  678, False),
        StubEntry(33, "某加密源", "https://enc.example", "2026-08-11", "阿丙",
                  1, True),
    ]
    stub = StubModule(entries=entries)
    install_stub(mw, stub)

    page = mw.SourcePage()
    ok1 = ck(stub.calls == [], "构造 SourcePage 不发任何网络请求（不联网）",
             f"calls={stub.calls}")
    ok2 = ck(page.tbl.rowCount() == 0, "未刷新前表格为空")

    page.refresh()
    pump(app, 6.0, lambda: page.tbl.rowCount() > 0)
    pump(app, 6.0, lambda: not page._busy)   # 等 _on_list 真正收尾，别抢在 _fill 之前动表格
    ok3 = ck(page.tbl.rowCount() == 3, "桩数据列出了 3 行",
             f"rowCount={page.tbl.rowCount()}")
    ok4 = ck(page.tbl.columnCount() == 6
             and [page.tbl.horizontalHeaderItem(i).text() for i in range(6)]
             == ["名称", "网址", "更新时间", "分享者", "下载量", "支持搜索"],
             "表头就是要求的 6 列",
             str([page.tbl.horizontalHeaderItem(i).text() for i in range(6)]))
    cells = [page.tbl.item(0, c).text() for c in range(6)]
    ok5 = ck(cells == ["看漫画", "https://m.manhuagui.com", "2026-10-01", "阿甲",
                       "12345", "是"],
             "第一行各列内容正确", str(cells))
    ok6 = ck(page.tbl.item(1, 5).text() == "否", "不支持搜索的行标「否」")
    ok7 = ck(stub.calls and stub.calls[0][0] == "list", "刷新确实调了 list_sources")

    # 关键词 + 排序下拉要透传
    stub.calls.clear()
    page.ed.setText("漫画")
    page.cb_order.setCurrentIndex(1)          # 切排序 -> 自动刷新
    pump(app, 6.0, lambda: any(c[0] == "list" for c in stub.calls))
    pump(app, 6.0, lambda: not page._busy)
    last = [c for c in stub.calls if c[0] == "list"][-1]
    ok8 = ck(last[2] == "漫画" and last[3] == "down",
             "关键词与「按下载量」排序透传给 list_sources", str(last))
    ok9 = ck(page.cb_order.count() == 2
             and page.cb_order.itemText(0) == "按更新时间"
             and page.cb_order.itemText(1) == "按下载量",
             "排序下拉是「更新时间 / 下载量」两项")

    # 选中一行才允许「验证并添加」
    page.tbl.setCurrentCell(0, 0)
    app.processEvents()
    ok10 = ck(page.btn_add.isEnabled(), "选中一行后「验证并添加」可用",
              f"sel={page.tbl.selectionModel().hasSelection()} row={page.tbl.currentRow()}")
    # 清空选中：QTableWidget 清空后 currentRow 仍停在原处（实测），只看 currentRow
    # 会让按钮继续亮着 —— 所以 _selected() 必须同时查 hasSelection()。
    page.tbl.clearSelection()
    page.tbl.setCurrentCell(-1, -1)
    app.processEvents()
    ok11 = ck(not page.btn_add.isEnabled(), "没选中时「验证并添加」置灰")

    page.deleteLater()
    return all([ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10, ok11])


# --------------------------------------------- 4. 校验失败 / 成功 与 config 写入

def check_verify_paths(app, mw) -> bool:
    from mangakit.config import config

    def rule_count() -> int:
        return len(config().get("user_rules") or [])

    before = rule_count()

    # --- 失败路径：ok=False 绝不能调 add_checked_source、绝不能写库 ---
    fail = StubResult(False, status="fail",
                      message="加密源：关键字段被 App 内部解密，客户端不支持")
    stub = StubModule(entries=[StubEntry(33, "某加密源", "https://enc.example")],
                      result=fail)
    install_stub(mw, stub)
    page = mw.SourcePage()
    refresh_and_wait(app, page)
    page.tbl.setCurrentCell(0, 0)
    app.processEvents()
    page._verify()
    pump(app, 8.0, lambda: not page._busy)
    ok1 = ck(stub.kinds().count("check") == 1, "点了「验证并添加」会调 check_source",
             str(stub.calls))
    ok2 = ck("add" not in stub.kinds(), "ok=False 时绝不调 add_checked_source",
             str(stub.calls))
    ok3 = ck(rule_count() == before, "ok=False 时 config 的 user_rules 没变",
             f"{before} -> {rule_count()}")
    ok4 = ck("加密源" in page.status_msg and "语法" not in page.status_msg,
             "失败原因原样透传（加密源）", page.status_msg)
    ok5 = ck("不可添加" in page.status_msg, "状态标签给出「不可添加」回执",
             page.status_msg)
    ok6 = ck(not page._busy and page.btn_refresh.isEnabled(),
             "失败后按钮解锁，不会卡在忙态")

    # --- 成功路径：ok=True 才写库，并回执「已添加，可在设置页查看」 ---
    good = StubResult(True, status="ok", message="可转换：1、2",
                      chapters=312, images=8921, site_name="看漫画",
                      rule={"id": "yck11"})
    stub2 = StubModule(entries=[StubEntry(11, "看漫画", "https://m.manhuagui.com")],
                       result=good)

    def add_checked_source(sid, *, deep=True, **kw):
        stub2.calls.append(("add", int(sid), deep))
        # 复刻真实现的副作用：通过校验才写 config.user_rules
        from mangakit.probe import add_user_rule
        add_user_rule({"id": f"yck{int(sid)}", "name": "看漫画",
                       "url": "https://m.manhuagui.com"})
        return good

    stub2.add_checked_source = add_checked_source  # type: ignore[method-assign]
    install_stub(mw, stub2)
    page2 = mw.SourcePage()
    seen: list[int] = []
    page2.added.connect(lambda sid: seen.append(sid))
    refresh_and_wait(app, page2)
    page2.tbl.setCurrentCell(0, 0)
    app.processEvents()
    page2._verify()
    pump(app, 8.0, lambda: stub2.kinds().count("add") == 1 and not page2._busy)
    ok7 = ck(stub2.kinds().count("add") == 1, "ok=True 时才调 add_checked_source",
             str(stub2.calls))
    ok8 = ck(rule_count() == before + 1, "ok=True 后 user_rules 多了一条",
             f"{before} -> {rule_count()}")
    ok9 = ck("已添加" in page2.status_msg and "设置页" in page2.status_msg,
             "回执是「已添加…可在设置页查看」", page2.status_msg)
    ok10 = ck(seen == [11], "写库成功后发出 added 信号（供设置页刷新）", str(seen))

    # 设置页真的能看见这条新站点（MainWindow 接线）
    win = mw.MainWindow()
    app.processEvents()
    n0 = win.settings.tbl_sites.rowCount()
    win._on_source_added(11)
    app.processEvents()
    ok11 = ck(win.settings.tbl_sites.rowCount() >= n0 and n0 > 0,
             "设置页 reload 后站点表可用", f"{n0} -> {win.settings.tbl_sites.rowCount()}")
    win.close()

    # 清理这条测试规则，别留在临时 MANGAKIT_HOME 之后的复用里
    try:
        from mangakit.probe import remove_user_rule
        remove_user_rule("yck11")
    except Exception:
        pass
    page.deleteLater()
    page2.deleteLater()
    return all([ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10, ok11])


# ----------------------------------------- 5. ycksource import 失败要降级不崩

def check_yck_import_failure(app, mw) -> bool:
    reset_yck(mw)

    def boom():
        raise ImportError("模拟：httpx 被裁掉")

    orig = mw._import_ycksource
    mw._import_ycksource = boom
    try:
        crashed = None
        try:
            page = mw.SourcePage()
        except Exception as exc:  # noqa: BLE001
            crashed = exc
            page = None
        ok1 = ck(crashed is None, "ycksource import 失败时构造 SourcePage 不抛异常",
                 repr(crashed))
        ok2 = ck(page is not None and "图源模块不可用" in page.status_msg,
                 "页面显示「图源模块不可用」",
                 page.status_msg if page else "(无页面)")
        ok3 = ck(page is not None and not page.ed.isEnabled()
                 and not page.btn_refresh.isEnabled() and not page.btn_add.isEnabled(),
                 "模块不可用时本页控件禁用（不装作能用）")
        ok4 = ck(page is not None and "模拟：httpx 被裁掉" in page.status_msg,
                 "提示里带上真实原因", page.status_msg if page else "")
        # 不可用时点刷新也不能炸
        bad = None
        try:
            page.refresh()
            page.ensure_loaded()
            page._verify()
            app.processEvents()
        except Exception as exc:  # noqa: BLE001
            bad = exc
        ok5 = ck(bad is None, "模块不可用时点刷新/验证都不抛异常", repr(bad))

        # 主窗口也必须能构造出来（不能让一个模块拖垮整个程序）
        win_err = None
        try:
            win = mw.MainWindow()
            app.processEvents()
            win._goto(5)
            app.processEvents()
            win.close()
        except Exception as exc:  # noqa: BLE001
            win_err = exc
        ok6 = ck(win_err is None, "ycksource 不可用时 MainWindow 仍能构造并切到图源页",
                 repr(win_err))

        # 打桩成第一次失败、第二次成功：确认失败结果被缓存（_yck_err 生效）而后可恢复
        mw._import_ycksource = orig
        reset_yck(mw)
        mod = mw.get_ycksource()
        ok7 = ck(mod is not None and not mw.ycksource_error(),
                 "恢复真实 import 后 get_ycksource() 可用",
                 mw.ycksource_error())
    finally:
        mw._import_ycksource = orig
        reset_yck(mw)
    return all([ok1, ok2, ok3, ok4, ok5, ok6, ok7])


# --------------------------------------------------------- 6. WebEngine 不可用降级

def check_webengine_unavailable(app, mw) -> bool:
    mw._web_mod = None
    mw._web_err = ""
    orig = mw._import_webengine

    def boom():
        raise ImportError("模拟：QtWebEngineWidgets 被 PyInstaller 裁掉")

    mw._import_webengine = boom
    try:
        err = None
        try:
            page = mw.BrowserPage()
            page.ensure_view()
            page.open_url("https://example.invalid/a")
            page.back()
            page.reload()
            app.processEvents()
        except Exception as exc:  # noqa: BLE001
            err = exc
            page = None
        ok1 = ck(err is None, "WebEngine 不可用时 BrowserPage 不抛异常", repr(err))
        ok2 = ck(page is not None and "内置浏览器不可用" in page.placeholder.text(),
                 "页面显示「内置浏览器不可用」人话提示",
                 page.placeholder.text()[:60] if page else "")
        ok3 = ck(page is not None and "PyInstaller" in page.placeholder.text(),
                 "提示里点明打包可能漏了 WebEngine")
        ok4 = ck(page is not None and page.view is None
                 and page.ed_url.text() == "https://example.invalid/a",
                 "open_url 失败后网址仍留在地址栏可复制",
                 page.ed_url.text() if page else "")
        ok5 = ck(page is not None and not page.btn_reload.isEnabled()
                 and not page.btn_home.isEnabled(),
                 "不可用时导航按钮置灰")
        win_err = None
        try:
            win = mw.MainWindow()
            app.processEvents()
            win._goto(6)
            app.processEvents()
            win.open_in_browser("https://example.invalid/b")
            app.processEvents()
            win.close()
        except Exception as exc:  # noqa: BLE001
            win_err = exc
        ok6 = ck(win_err is None, "WebEngine 不可用时主窗口与切页仍正常", repr(win_err))
    finally:
        mw._import_webengine = orig
        mw._web_mod = None
        mw._web_err = ""
    return all([ok1, ok2, ok3, ok4, ok5, ok6])


def teardown(app) -> None:
    """退出前把后台线程收干净（收尾失败不影响断言结果）。

    背景（已实测，别再重复排查）：本脚本在**进程关闭阶段**会以
    -1073740791（0xC0000409）或 -1073741819（0xC0000005）收场。它是
    **脚本自身的收尾竞态，不是产品缺陷**，三条证据：

      1. 真程序路径干净退出：MainWindow -> show -> 切到图源页/浏览器页 ->
         close，exit=0（两种页面各测过）；
      2. `tools\\gui_smoke.py` 连跑三次都是 exit=0；
      3. 脚本里装上 faulthandler 或 qInstallMessageHandler 之后 exit=0
         ——改成 heisenbug 了，说明是时序相关的析构竞态。

    成因：脚本里造过好几个 MainWindow，每个都 `manager().start()` 起了一条
    守护线程 mangakit-loop（asyncio run_forever）并留下若干 QRunnable；
    解释器关闭时 Qt 先拆 C++ 侧、Python 侧线程还在跑，析构顺序就成竞态。
    真程序走 QApplication 事件循环正常退出，不会踩到。

    这里尽力停掉下载管理器循环并排空线程池，减小竞态窗口；真正保证退出码
    可信的是 `__main__` 里的 os._exit（见下）。
    """
    try:
        from mangakit.downloader import manager
        manager().stop()
    except Exception:  # noqa: BLE001 - 收尾失败不该改变断言结果
        pass
    try:
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)
    except Exception:  # noqa: BLE001
        pass
    for w in list(app.topLevelWidgets()):
        try:
            w.close()
            w.deleteLater()
        except Exception:  # noqa: BLE001
            pass
    app.processEvents()
    app.sendPostedEvents(None, 0)   # 0 = QEvent.DeferredDelete
    app.processEvents()
    gc.collect()


def check_app_exit_clean(app, mw) -> bool:
    """真程序路径必须能干净退出：建 MainWindow -> 进两个新页 -> close -> exit 0。

    为什么单起子进程：本脚本进程自己在关闭阶段有 Qt/WebEngine 析构竞态（见
    teardown 注释），所以**不能用本进程的退出码**证明「产品退出干净」。子进程
    里只建一个 MainWindow、不做别的，跑完自然结束，退出码可信。
    """
    child = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication(sys.argv)
for f in ("information", "warning", "question"):
    setattr(QMessageBox, f, staticmethod(lambda *a, **k: None))
import mangakit.gui.main_window as mw
win = mw.MainWindow()
win.show()
app.processEvents()
win._goto(5)          # 图源仓库页
app.processEvents()
win._goto(6)          # 内置浏览器页（真起 WebEngine）
app.processEvents()
print("view_built", win.browser.view is not None, flush=True)
win.close()
app.processEvents()
print("clean_exit", flush=True)
'''
    import subprocess
    env = dict(os.environ)
    env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run([sys.executable, "-c", child, str(ROOT)], env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except Exception as exc:  # noqa: BLE001
        return ck(False, "真程序能干净退出（子进程）", f"{type(exc).__name__}: {exc}")
    out = (r.stdout or "") + (r.stderr or "")
    built = "view_built True" in out
    clean = "clean_exit" in out
    return ck(r.returncode == 0 and built and clean,
              "真程序建两页后 close 能干净退出（exit=0）",
              f"rc={r.returncode} view_built={built} clean_exit={clean} "
              f"tail={(r.stdout or '')[-120:]!r} {(r.stderr or '')[-160:]!r}")


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox
    import mangakit.gui.main_window as mw

    app = QApplication.instance() or QApplication(sys.argv)
    # offscreen 下模态框会挂住，全部桩掉
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(lambda *a, **k: None)

    print(f"=== 新页面验收（MANGAKIT_HOME={os.environ['MANGAKIT_HOME']}）===", flush=True)
    ok_lazy = check_lazy_webengine(app, mw)
    ok_wire = check_page_wiring(app, mw)
    ok_list = check_source_list(app, mw)
    ok_verify = check_verify_paths(app, mw)
    ok_yck = check_yck_import_failure(app, mw)
    ok_web = check_webengine_unavailable(app, mw)
    ok_exit = check_app_exit_clean(app, mw)

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"=== 断言 {passed}/{total} 通过 ===", flush=True)
    all_ok = all([ok_lazy, ok_wire, ok_list, ok_verify, ok_yck, ok_web, ok_exit])
    teardown(app)
    return 0 if all_ok and passed == total else 1


if __name__ == "__main__":
    # 本脚本自己会在解释器关闭阶段踩到 Qt/WebEngine 的析构竞态（见 teardown 注释），
    # 那与断言结果无关，却会让退出码变成 -1073740791 之类的垃圾值，脚本就没法当
    # 回归门用了。所有断言与 teardown 都已在 main() 里跑完，这里直接按结果
    # os._exit，绕开解释器关闭阶段；「真程序能否干净退出」由 check_app_exit_clean
    # 另起子进程单独断言，不靠本进程的退出码背书。
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
