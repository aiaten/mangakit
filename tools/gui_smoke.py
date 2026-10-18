"""exe GUI 启动验证：确认 .bat 启动器环境下 exe 能起窗口，并抓两种只在打包后
才暴露的问题：onefile 解包失败（弹 "Could not create temporary directory!"）、
stdout 编码把中文打成 ascii 编码错误。

注意：TEMP 目录必须先 mkdir —— PyInstaller onefile 在 TEMP 不存在时会
静默挂起或报上面那个弹窗（PYI-3216）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "MangaKit.exe"
BASE = ROOT / "tools" / "_smoke" / "gui"

# ── 隔离护栏：必须在 import mangakit 之前打好 ──────────────────────────────
# config._resolve_home() 优先读 MANGAKIT_HOME，没设就落在 %APPDATA%\mangakit，
# 也就是用户的真库。本文件里的检查函数会直接调 store() 增删收藏，2026-10-14
# 就是因为缺这一行，把用户真实收藏清空了（tools\_incident\ 保留了取证）。
# 任何在本进程内 import mangakit 的测试都必须先设好这个变量。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_smoke_"))
_HOME_IS_TEMP = Path(os.environ["MANGAKIT_HOME"]).name.startswith("mk_")


def check_site_picker() -> bool:
    """offscreen 打开搜索页，断言站点下拉列出了注册表里全部站点，
    并且不支持搜索的站都带上了「（不支持搜索）」标注。"""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from mangakit.gui.main_window import MainWindow, SearchPage
        from mangakit.sites import registry

        app = QApplication.instance() or QApplication(sys.argv)
        # offscreen 下 QMessageBox 是模态的，会挂住等点击，桩掉。
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)

        reg = registry()
        sites = reg.all_sites()
        visible = reg.visible_sites()
        win = MainWindow()
        win.show()
        page = win.findChild(SearchPage)
        items = [page.cb_site.itemText(i) for i in range(page.cb_site.count())]
        ids = [page.cb_site.itemData(i) for i in range(page.cb_site.count())]
        # 规则里标了 hidden 的站（没有站内搜索的那些）不该占用户的下拉列表，
        # 但仍要能被 for_url 匹配——两件事分别断言。
        missing = [s.id for s in visible if s.id not in ids]
        leaked = [s.id for s in sites if s.hidden and s.id in ids]
        unmarked = [s.name for s in visible
                    if not s.can_search and not any(
                        s.name in t and "不支持搜索" in t for t in items)]
        hidden = [s for s in sites if s.hidden]
        print(f"[{'PASS' if not missing else 'FAIL'}] 下拉列出全部 {len(visible)} 个可见站点"
              + (f"，缺 {missing}" if missing else f"（另有 {len(hidden)} 个 hidden 站不出现在列表里）"))
        print(f"[{'PASS' if not leaked else 'FAIL'}] hidden 站没有混进下拉列表"
              + (f"，混进 {leaked}" if leaked else ""))
        print(f"[{'PASS' if not unmarked else 'FAIL'}] 不支持搜索的站都有标注"
              + (f"，漏 {unmarked}" if unmarked else ""))
        win.close()
        return not missing and not leaked and not unmarked
    except Exception as exc:
        print(f"[FAIL] 下拉检查异常：{exc}")
        return False


def check_chapter_picker() -> bool:
    """断言章节列表的勾选框真能点、且点了能进下载队列。

    这条是回归测试：勾选列曾经被设成 28px 宽而缩进是 30px，导致 visualRect
    宽度为负、勾选框画到章节标题文字上，点它改的是章节列的勾选态，
    用户表现为「勾不上 + 点下载无反应」。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtTest import QTest
        from mangakit.gui.main_window import MainWindow
        from mangakit.models import Chapter, Comic

        app = QApplication.instance() or QApplication(sys.argv)
        msgs: list[str] = []
        QMessageBox.information = staticmethod(
            lambda *a, **k: msgs.append(a[2] if len(a) > 2 else ""))
        QMessageBox.warning = staticmethod(
            lambda *a, **k: msgs.append(a[2] if len(a) > 2 else ""))

        win = MainWindow()
        page = win.manga
        page.comic = Comic(title="测试漫画", url="https://example.com/c/1", site="x")
        page.chapters = [Chapter(cid=f"c{i}", title=f"第{i}话", url=f"u{i}", index=i)
                         for i in range(1, 9)]
        page._render()
        app.processEvents()

        tree = page.tree
        vr = tree.visualRect(tree.model().index(0, 0))
        geo_ok = vr.width() > 0 and vr.x() >= 0
        print(f"[{'PASS' if geo_ok else 'FAIL'}] 勾选列可视矩形 {vr}"
              + ("" if geo_ok else " 宽度非正 → 勾选框会画到别的列上"))

        hits = []
        for x in range(vr.x(), vr.x() + vr.width()):
            tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
            QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                             vr.topLeft() + QPoint(x - vr.x(), vr.height() // 2))
            app.processEvents()
            if tree.topLevelItem(0).checkState(0) == Qt.Checked:
                hits.append(x)
        print(f"[{'PASS' if hits else 'FAIL'}] 点勾选框能切换状态，可点 x={hits[:1]}..{hits[-1:]}"
              + ("" if hits else " → 勾不上"))

        # 点章节列只应选中行，不该改勾选态
        tree.topLevelItem(0).setCheckState(0, Qt.Unchecked)
        vr1 = tree.visualRect(tree.model().index(0, 1))
        QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier, vr1.center())
        app.processEvents()
        col_ok = tree.topLevelItem(0).checkState(0) == Qt.Unchecked
        print(f"[{'PASS' if col_ok else 'FAIL'}] 点章节列不误改勾选态")

        # 表头第一列 = 全选开关
        page._on_header_clicked(0)
        app.processEvents()
        head_ok = len(page._checked()) == len(page.chapters)
        print(f"[{'PASS' if head_ok else 'FAIL'}] 表头第一列点击=全选"
              + (f"，选中 {len(page._checked())}/{len(page.chapters)}" if not head_ok else ""))

        # 端到端：勾选 → 点「下载选中章节」→ 进队列 → 切到下载任务页
        win.add_tasks(page.comic, page._checked())
        app.processEvents()
        enq_ok = len(win.mgr.tasks()) == len(page.chapters) and win.stack.currentIndex() == 2
        print(f"[{'PASS' if enq_ok else 'FAIL'}] 勾选后点下载能入队并跳转任务页"
              + (f"，任务 {len(win.mgr.tasks())} 个" if enq_ok else
                 f"，任务 {len(win.mgr.tasks())} 个、当前页 {win.stack.currentIndex()}"))
        for t in win.mgr.tasks():
            win.mgr.cancel(t.tid)
        win.close()
        return all((geo_ok, bool(hits), col_ok, head_ok, enq_ok))
    except Exception as exc:
        print(f"[FAIL] 章节勾选检查异常：{exc}")
        return False


def check_expand_button() -> bool:
    """断言「展开全部章节」按钮能补回折叠章节，且展开后不丢原来的勾选。

    这条是回归测试：站点详情页只渲染最近 N 话时按钮才有意义，而补齐走的是
    chapters.api / chapters.embed 两条引擎分支（full=True）。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from mangakit.gui.main_window import MainWindow
        from mangakit.gui.bridge import bridge
        from mangakit.models import Chapter, Comic

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)

        win = MainWindow()
        page = win.manga
        page.comic = Comic(title="测试漫画", url="https://x.test/c/1", site="x")
        page._url = page.comic.url
        short = [Chapter(cid=f"c{i}", title=f"第{i}话", url=f"u{i}", index=i)
                 for i in range(1, 4)]
        page.chapters = short
        # 站点必须真的支持展开，按钮才会亮 —— 这是 2026 修复的核心：
        # 之前只看「列表非空」就点亮，21 个站里只有 2 个配了 chapters.api，
        # 用户在其余站点上点展开等于点了个寂寞，还完全没有提示。
        page._can_expand_site = True
        page._render()
        app.processEvents()
        btn_ok = page.btn_expand.isEnabled()
        print(f"[{'PASS' if btn_ok else 'FAIL'}] 支持展开的站点：章节加载后「展开全部章节」可用")

        # 反向断言：不支持展开的站点必须置灰（否则就是「点了没反应」的老毛病）。
        # 这两条放在勾选之前做 —— 它们各自会 _render() 一次，而 _render 会把
        # 勾选态整个重画，先勾后测的话勾选会被冲掉。
        page._can_expand_site = False
        page._apply_expand_state()
        app.processEvents()
        grey_ok = not page.btn_expand.isEnabled()
        print(f"[{'PASS' if grey_ok else 'FAIL'}] 不支持展开的站点：「展开全部章节」置灰"
              + ("" if grey_ok else f"，enabled={page.btn_expand.isEnabled()}"))
        tip = page.btn_expand.toolTip()
        tip_ok = bool(tip) and ("无需展开" in tip or "不支持" in tip)
        print(f"[{'PASS' if tip_ok else 'FAIL'}] 置灰时给出原因提示"
              + ("" if tip_ok else f" → 「{tip}」"))
        # 恢复：注意这里**只**改标志位并刷新按钮，不调 _render()，避免冲掉下面设的勾选。
        page._can_expand_site = True
        page._apply_expand_state()
        app.processEvents()

        # 现在勾第 2 话，展开后应当仍然是勾上的
        page.tree.topLevelItem(1).setCheckState(0, Qt.Checked)
        before = len(page._checked())

        # 用假的 full 加载顶掉真实网络请求：返回 20 话。
        # 必须等 _expand_all 把 self._rid_ch 赋值完再 emit——真实链路是后台线程
        # 回调、Qt 队列到主线程才进槽，先 emit 会被 rid 对不上直接 return 掉。
        # 展开是「补齐」而不是「换一批」：原有 3 话（url 保持 u1..u3）必须仍在结果里，
        # 否则按 url 复原勾选当然复原不到——那是假数据造出来的假失败。
        full = short + [Chapter(cid=f"f{i}", title=f"第{i}话", url=f"v{i}", index=i)
                        for i in range(4, 21)]
        got: dict = {}

        def fake_reload(url: str) -> int:
            rid = 999
            got["rid"] = rid
            QTimer.singleShot(0, lambda: bridge().chapters_loaded.emit(rid, full))
            return rid

        page.svc.reload_chapters_full = fake_reload  # type: ignore[method-assign]
        page._expand_all()
        app.processEvents()
        print(f"[{'PASS' if got.get('rid') == page._rid_ch else 'FAIL'}] 展开走的是 full=True 重载请求")

        n_ok = len(page.chapters) == len(full)
        print(f"[{'PASS' if n_ok else 'FAIL'}] 点展开后章节从 {len(short)} 变 {len(page.chapters)}")
        keep_ok = len(page._checked()) == before == 1 and page._checked()[0].url == "u2"
        print(f"[{'PASS' if keep_ok else 'FAIL'}] 展开后保留展开前的勾选"
              + ("" if keep_ok else f"，展开前勾了 {before} 个、现在 {len(page._checked())} 个"))
        btn_txt_ok = page.btn_expand.text() == "展开全部章节" and page.btn_expand.isEnabled()
        print(f"[{'PASS' if btn_txt_ok else 'FAIL'}] 展开完成后按钮文案/状态复原"
              + ("" if btn_txt_ok else f" → 「{page.btn_expand.text()}」enabled={page.btn_expand.isEnabled()}"))
        # 展开后必须给回执 —— 之前静默复原，用户分不清「已全部展开」和「不支持展开」。
        msg = page.status_msg or ""
        msg_ok = "展开完成" in msg and "新增" in msg
        print(f"[{'PASS' if msg_ok else 'FAIL'}] 展开后给出「新增 N 话」回执"
              + ("" if msg_ok else f" → 「{msg}」"))
        win.close()
        return bool(btn_ok and grey_ok and tip_ok and got.get("rid") == page._rid_ch
                    and n_ok and keep_ok and btn_txt_ok and msg_ok)
    except Exception as exc:
        print(f"[FAIL] 展开按钮检查异常：{exc}")
        return False


def check_favorites_delete() -> bool:
    """断言收藏页能删：多选删、清空、以及删除前必须确认。

    回归点：早先「删除选中」点了就**直接删**，没有任何确认，误点一下收藏就没了；
    而且只支持单选、没有右键菜单、删除后计数不刷新。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from mangakit.gui.main_window import FavoritesPage
        from mangakit.store import store

        app = QApplication.instance() or QApplication(sys.argv)
        # offscreen 下 QMessageBox.question 会阻塞等点击：桩掉，默认返回 Yes。
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        QMessageBox.information = staticmethod(lambda *a, **k: None)

        s = store()
        s.clear_favorites()
        for i in range(4):
            s.add_favorite({"site": f"s{i}", "url": f"u{i}", "title": f"漫{i}",
                            "author": "", "cover": "", "tags": []})

        # 必须按真实签名构造（要传 svc）。之前这里写成无参 FavoritesPage()，
        # 正好和当时「__init__ 收不到 svc」的 bug 互相掩护 —— 测试跟着 bug 写，
        # 于是 self.svc 不存在这种致命错误一路全绿放行。
        from mangakit.service import ComicService
        from mangakit.downloader import manager
        svc = ComicService(manager())
        page = FavoritesPage(svc)
        page.show()
        app.processEvents()
        n_all = page.table.rowCount()
        print(f"[{'PASS' if n_all == 4 else 'FAIL'}] 收藏页列出全部收藏"
              + ("" if n_all == 4 else f"，列了 {n_all} 行"))

        # 防回归：检查更新按钮点下去不能抛异常，且必须真的走到 service。
        ok_svc = getattr(page, "svc", None) is not None
        print(f"[{'PASS' if ok_svc else 'FAIL'}] 收藏页拿到了 service（check_updates 依赖它）")
        try:
            page._check_updates()
            started = page.btn_check.text() != "检查更新" or not page.btn_check.isEnabled()
            ok_click = True
        except Exception as exc:
            ok_click = False
            started = False
            print(f"       点检查更新抛异常：{type(exc).__name__}: {exc}")
        print(f"[{'PASS' if ok_click and started else 'FAIL'}] "
              f"点「检查更新」能进入检查态（不抛异常）")
        page._checking = False
        page.btn_check.setEnabled(True)
        page.btn_check.setText("检查更新")

        # 防回归：检查进行中时，reload() 不能把进度文案盖回「共 N 条」。
        # 以前 _check_updates() 先写「正在检查 N 本…」再调 reload()，而 reload()
        # 无条件写「共 N 条」，用户盯着一动不动的文字，观感就是「点了卡住」。
        page._checking = True
        page._check_total = 4
        page._updates.clear()
        page.reload()
        prog = page.lb_count.text()
        ok_prog = "正在检查" in prog
        print(f"[{'PASS' if ok_prog else 'FAIL'}] 检查中底部显示进度而非条数"
              + ("" if ok_prog else f" → 「{prog}」"))
        ok_btn = not page.btn_check.isEnabled() and "检查中" in page.btn_check.text()
        print(f"[{'PASS' if ok_btn else 'FAIL'}] 检查中按钮置灰并改文案")
        page._checking = False
        page._updates.clear()
        page.reload()

        # 计数标签
        cnt_ok = "4" in page.lb_count.text()
        print(f"[{'PASS' if cnt_ok else 'FAIL'}] 底部显示收藏条数"
              + ("" if cnt_ok else f" → 「{page.lb_count.text()}」"))

        # 多选删除：选第 0、1 行
        from PySide6.QtCore import QItemSelectionModel
        page.table.selectRow(0)
        page.table.selectionModel().select(
            page.table.model().index(1, 0),
            QItemSelectionModel.Select | QItemSelectionModel.Rows)
        sel_n = len(page._selected())
        print(f"[{'PASS' if sel_n == 2 else 'FAIL'}] 支持 Ctrl/Shift 多选"
              + (f"（选到 {sel_n} 条）" if sel_n == 2 else f"，只选到 {sel_n} 条"))

        page._del()
        app.processEvents()
        left = page.table.rowCount()
        in_db = len(s.favorites())
        multi_ok = left == 2 and in_db == 2
        print(f"[{'PASS' if multi_ok else 'FAIL'}] 多选删除生效"
              + ("" if multi_ok else f"，界面剩 {left} 行、库里剩 {in_db} 条"))

        # 确认框必须真的弹（桩里记账）
        asked = {"n": 0}
        orig = QMessageBox.question

        def spy(*a, **k):
            asked["n"] += 1
            return QMessageBox.Yes

        QMessageBox.question = staticmethod(spy)
        page.table.selectRow(0)
        page._del()
        app.processEvents()
        confirm_ok = asked["n"] == 1
        print(f"[{'PASS' if confirm_ok else 'FAIL'}] 删除前有二次确认"
              + ("" if confirm_ok else f"，确认框弹了 {asked['n']} 次"))
        QMessageBox.question = orig

        # 清空
        page._clear()
        app.processEvents()
        clear_ok = page.table.rowCount() == 0 and len(s.favorites()) == 0
        print(f"[{'PASS' if clear_ok else 'FAIL'}] 清空收藏生效"
              + ("" if clear_ok else
                 f"，界面 {page.table.rowCount()} 行、库里 {len(s.favorites())} 条"))

        # 空库时按钮应禁用
        page.reload()
        app.processEvents()
        dis_ok = (not page.btn_del.isEnabled() and not page.btn_clear.isEnabled())
        print(f"[{'PASS' if dis_ok else 'FAIL'}] 空收藏时删除/清空按钮禁用"
              + ("" if dis_ok else
                 f" del={page.btn_del.isEnabled()} clear={page.btn_clear.isEnabled()}"))

        s.clear_favorites()
        page.close()
        return bool(n_all == 4 and cnt_ok and sel_n == 2 and multi_ok
                    and confirm_ok and clear_ok and dis_ok)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 收藏删除检查异常：{exc}")
        return False


def check_search_streaming() -> bool:
    """断言搜索页不是「等所有站凑齐才显示」，且封面不会阻塞 GUI 线程。

    两条回归：
    1) 以前 search_all 用 gather 等全部站，最慢的站（mhua5 约 3s）拖住整屏，
       用户看到的是长时间空列表。现在每站结果单独走 search_partial 推过来。
    2) 以前 _cover_pixmap 在 GUI 线程里同步 httpx.get（每张 8s 超时），
       bzmanga 一次 83 条 = 最多上百秒界面冻结。现在必须毫秒级返回占位图。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        import time as _t

        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QApplication, QMessageBox
        from mangakit.gui.bridge import bridge
        from mangakit.gui.main_window import CoverLoader, MainWindow, SearchPage
        from mangakit.models import Comic

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)

        win = MainWindow()
        win.show()
        page = win.findChild(SearchPage)

        # --- 1) search_partial 先到先显示 ---
        seen: list[int] = []
        bridge().search_partial.connect(lambda rid, r: seen.append(len(r)))
        c1 = Comic(site="a", title="甲", url="u1", cover="")
        c2 = Comic(site="b", title="乙", url="u2", cover="")
        page._reset()
        page._pending = 1
        rid = 7
        # 必须把本次 rid 写进去：SearchPage 现在会丢弃「不是本次搜索」的信号
        # （否则旧搜索的残留分片会混进新列表、残留 done 会提前收尾）。
        page._rid = rid
        bridge().search_partial.emit(rid, [c1])
        app.processEvents()
        first_visible = page.list.count()
        bridge().search_partial.emit(rid, [c2])
        app.processEvents()
        both_visible = page.list.count()
        bridge().search_done.emit(rid, 2)
        app.processEvents()
        stream_ok = first_visible == 1 and both_visible == 2 and page._pending == 0
        print(f"[{'PASS' if stream_ok else 'FAIL'}] 单站结果先到先显示"
              + ("" if stream_ok else
                 f"，首批 {first_visible} 条第 2 批 {both_visible} 条"))
        status_ok = "2" in page.status.text()
        print(f"[{'PASS' if status_ok else 'FAIL'}] 完成后状态栏报总数"
              + ("" if status_ok else f" → 「{page.status.text()}」"))

        # --- 1b) 旧搜索的残留信号必须被丢弃 ---
        page._reset()
        page._pending = 1
        page._add_items([Comic(site="a", title="本次甲", url="u1", cover="")])
        app.processEvents()
        n_before = page.list.count()
        bridge().search_partial.emit(999, [Comic(site="a", title="旧站残留",
                                               url="u9", cover="")])
        bridge().search_done.emit(999, 20)
        app.processEvents()
        stale_ok = page.list.count() == n_before and page._pending == 1
        print(f"[{'PASS' if stale_ok else 'FAIL'}] 旧 rid 的残留结果不进列表、不提前收尾"
              + ("" if stale_ok else
                 f" -> {n_before}→{page.list.count()} 条，_pending={page._pending}"))

        # --- 1c) 条目图标必须满尺寸（封面未到时也点得到） ---
        # 修复前 get() 未命中返回**空 QPixmap**，QListWidget 在
        # setUniformItemSizes(True) 下把统一尺寸冻成文本大小（真机实测 80×25），
        # 封面稍后画得出来却点不到 —— 用户报的「有时点不了搜索出来的漫画」。
        icon_sz = page.list.iconSize()
        it_empty = page.list.item(0)
        ic = it_empty.icon().pixmap(icon_sz).size()
        icon_ok = (not it_empty.icon().isNull()) and ic == icon_sz
        print(f"[{'PASS' if icon_ok else 'FAIL'}] 无封面条目也带满尺寸图标"
              f"（{ic.toTuple()} vs iconSize {icon_sz.toTuple()}）"
              + ("" if icon_ok else " -> 空图标会让条目点不到"))
        # 命中区必须装得下图标：以前只有文本高度（真机 25px vs 图标 190px），
        # 用户瞄着封面点就落在条目外。
        rect_h = page.list.visualItemRect(it_empty).height()
        rect_ok = rect_h >= icon_sz.height()
        print(f"[{'PASS' if rect_ok else 'FAIL'}] 条目命中区高度不小于封面高度"
              f"（{rect_h}px >= {icon_sz.height()}px）")
        hit_ok = True
        for i in range(page.list.count()):
            it = page.list.item(i)
            ctr = page.list.visualItemRect(it).center()
            idx = page.list.indexAt(ctr)
            if not idx.isValid() or idx.row() != i:
                hit_ok = False
                break
        print(f"[{'PASS' if hit_ok else 'FAIL'}] 每条自身矩形中心都能 indexAt 命中")

        # --- 2) 封面 get() 不许阻塞 ---
        loader = CoverLoader()
        # 故意用不可达地址：真要同步去连，这里会卡满超时。
        urls = [f"http://10.255.255.1/{i}.jpg" for i in range(20)]
        t0 = _t.monotonic()
        for u in urls:
            loader.get(u, QSize(140, 190))
        dt = _t.monotonic() - t0
        # 20 张图，同步实现至少要几秒；放宽到 1s 仍能可靠区分两种实现。
        fast_ok = dt < 1.0
        print(f"[{'PASS' if fast_ok else 'FAIL'}] 封面取图不阻塞 GUI 线程"
              + (f"（20 张耗时 {dt*1000:.0f}ms）" if fast_ok
                 else f" -> 耗时 {dt:.2f}s，说明又同步下载了"))
        pool_ok = loader._pool.maxThreadCount() >= 2
        print(f"[{'PASS' if pool_ok else 'FAIL'}] 封面用线程池并发取"
              + (f"（{loader._pool.maxThreadCount()} 线程）" if pool_ok else ""))
        win.close()
        return bool(stream_ok and status_ok and stale_ok and icon_ok and rect_ok
                    and hit_ok and fast_ok and pool_ok)
    except Exception as exc:
        print(f"[FAIL] 搜索流式/封面异步检查异常：{exc}")
        return False


def check_add_site() -> bool:
    """断言设置页的「添加网站 / 删除站点」这一套能用，且严格按「不能下载的不加」。

    回归点（用户明确要求）：
      · 站点管理里有「添加网站」按钮
      · 贴入一个**下不了图**的网址，探测必须判 fail，且「添加到站点列表」是灰的
      · 自建站点写进 user_rules 后，注册表重载能认出它、列表里带 [自建] 标记
      · 删除自建站点后，列表里就没有它了
      · 内置站点不允许删除（只能停用）

    全程不打真网：探测结果用手造的 ProbeResult 替换，保证断言稳定。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from mangakit.config import config
        from mangakit.gui.main_window import AddSiteDialog, SettingsPage
        from mangakit.probe import ProbeResult, add_user_rule, is_user_rule, remove_user_rule
        from mangakit.sites import reload_registry

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)

        ok = True
        page = SettingsPage()
        page.show()
        app.processEvents()

        has_add = hasattr(page, "btn_add_site") and page.btn_add_site.isEnabled()
        print(f"[{'PASS' if has_add else 'FAIL'}] 站点管理里有「添加网站」按钮")
        ok &= has_add

        # --- 探测不通过的站：不能添加 ---
        dlg = AddSiteDialog()
        r_fail = ProbeResult(url="https://x.test/comic/1/", host="x.test")
        r_fail.status = "fail"
        r_fail.site_name = "X 站"
        r_fail.reason = "试了多话都拿不到正文图片：这个站的图由 JavaScript 在浏览器里现拼。"
        dlg._on_done({"result": r_fail})
        app.processEvents()
        blocked = not dlg.btn_add.isEnabled() and dlg.rule is None
        shown = "不予添加" in dlg.txt.toPlainText() and "JavaScript" in dlg.txt.toPlainText()
        print(f"[{'PASS' if blocked else 'FAIL'}] 不能下载的站：添加按钮置灰、不产生规则")
        print(f"[{'PASS' if shown else 'FAIL'}] 不能下载的站：把原因写清给用户看")
        ok &= blocked and shown
        dlg.close()

        # --- 探测通过的站：可以添加 ---
        dlg2 = AddSiteDialog()
        r_ok = ProbeResult(url="https://y.test/comic/9/", host="y.test")
        r_ok.status = "ok"
        r_ok.site_name = "Y 站"
        r_ok.chapter_count = 120
        r_ok.image_count = 40
        r_ok.title = "测试漫画"
        r_ok.rule = {"id": "smoketesty", "name": "Y 站", "domains": ["y.test"],
                     "chapters": {"list": {"css": "a[href]"}, "url_re": "/read/\\d+/"},
                     "pages": {"list": {"css": "img"}}}
        dlg2._on_done({"result": r_ok})
        app.processEvents()
        allowed = dlg2.btn_add.isEnabled() and dlg2.rule is not None
        print(f"[{'PASS' if allowed else 'FAIL'}] 能下载的站：允许添加并生成规则")
        ok &= allowed
        sid = dlg2.rule["id"]
        dlg2.close()

        # --- 写进 user_rules → 重载 → 列表里出现 [自建] ---
        # 先数一下本来就有的自建站：测试跑在用户真实 config 上，机器上可能
        # 早就有自建站（比如上一轮测试留下的），断言不能写死「自建 1」。
        from mangakit.config import config as _cfg
        n_before = len(_cfg().get("user_rules") or [])
        add_user_rule(r_ok.rule)
        reload_registry()
        page.reload()
        app.processEvents()
        names = [page.tbl_sites.item(r, 1).text() for r in range(page.tbl_sites.rowCount())]
        marked = any("[自建]" in n and "Y 站" in n for n in names)
        want = f"自建 {n_before + 1}"
        cnt_shown = want in page.lb_sites.text()
        print(f"[{'PASS' if marked else 'FAIL'}] 自建站点在列表里带 [自建] 标记"
              + ("" if marked else f"，当前列表：{names[-3:]}"))
        print(f"[{'PASS' if cnt_shown else 'FAIL'}] 底部统计自建站点数量"
              + f"（原有 {n_before} 个，加 1 后应显示「{want}」）"
              + ("" if cnt_shown else f" → 「{page.lb_sites.text()}」"))
        ok &= marked and cnt_shown

        # --- 内置站点不可删 ---
        builtin_row = None
        for r in range(page.tbl_sites.rowCount()):
            it = page.tbl_sites.item(r, 0)
            if it and not is_user_rule(it.data(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.UserRole)):
                builtin_row = r
                break
        if builtin_row is not None:
            page.tbl_sites.selectRow(builtin_row)
            page._del_site()
            still = page.tbl_sites.rowCount()
            protected = still == len(names)
            print(f"[{'PASS' if protected else 'FAIL'}] 内置站点删除被拒绝（行数不变）")
            ok &= protected

        # --- 删除自建站点 ---
        dlg3_yes = True
        QMessageBox.exec = lambda self: QMessageBox.Yes  # 确认框一律点「是」
        target = None
        for r in range(page.tbl_sites.rowCount()):
            it = page.tbl_sites.item(r, 0)
            if it and it.data(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.UserRole) == sid:
                target = r
                break
        if target is not None:
            page.tbl_sites.selectRow(target)
            page._del_site()
            app.processEvents()
            gone = not is_user_rule(sid) and all(
                "Y 站" not in (page.tbl_sites.item(r, 1).text() if page.tbl_sites.item(r, 1) else "")
                for r in range(page.tbl_sites.rowCount())
            )
            print(f"[{'PASS' if gone else 'FAIL'}] 删除自建站点生效")
            ok &= gone
        else:
            print("[FAIL] 自建站点没有出现在列表里，无法测试删除")
            ok = False

        remove_user_rule(sid)
        reload_registry()
        page.close()
        return ok
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 添加网站检查异常：{exc}")
        return False


def check_double_click_open() -> bool:
    """搜索出结果后双击条目要真的打开详情页。

    这是用户报过的「双击结果完全没有反应」。此前整份 gui_smoke 对双击路径
    零覆盖（_open_item / itemDoubleClicked / open_comic 都没被测到），所以
    这条链路断了也全绿。这里用真 QTest 双击走完整链路，并断言详情页拿到
    了标题与章节。
    """
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mangakit.gui.main_window import MainWindow
        from mangakit.gui.bridge import bridge
        from mangakit.models import Comic

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        QMessageBox.information = staticmethod(lambda *a, **k: None)

        win = MainWindow()
        win.show()
        page = win.search
        mp = win.manga

        # ① 用假数据只验「双击 -> 切换页面 + 发起解析」这一跳，
        #    不依赖真网，保证离线可跑。
        page._reset()
        page._add_items([
            Comic(title="双击样本", url="https://example.invalid/book/1/",
                  site="songzimanhua", site_name="松子漫画"),
        ])
        app.processEvents()

        fired: list = []
        page.open_comic.connect(lambda c: fired.append(c))
        it = page.list.item(0)
        ctr = page.list.visualItemRect(it).center()
        QTest.mouseClick(page.list.viewport(), Qt.LeftButton, Qt.NoModifier, ctr)
        QTest.mouseDClick(page.list.viewport(), Qt.LeftButton, Qt.NoModifier, ctr)
        app.processEvents()

        ok1 = len(fired) == 1
        print(f"[{'PASS' if ok1 else 'FAIL'}] 双击结果触发 open_comic（{len(fired)} 次）")

        # ② 真的把详情页喂上数据，断言标题/章节/状态都到位
        c = Comic(title="双击样本", url="https://example.invalid/book/1/",
                  site="songzimanhua", site_name="松子漫画")
        mp._rid_c = 91
        mp._on_comic(91, c)
        ok2 = mp.lb_title.text() == "双击样本"
        print(f"[{'PASS' if ok2 else 'FAIL'}] 详情页标题已更新 -> {mp.lb_title.text()!r}")

        # _on_comic 内部会重新发一次 load_chapters 并覆盖 _rid_ch，
        # 所以这里必须用它真实返回的 rid，不能自己编一个（编了会被守卫拦掉，
        # 表现成「章节一行都没有」的假失败）。
        from mangakit.models import Chapter
        mp._on_chapters(mp._rid_ch,
                        [Chapter("第1话", "https://example.invalid/1", "s", "c1")])
        ok3 = len(mp.chapters) == 1 and mp.tree.topLevelItemCount() == 1
        print(f"[{'PASS' if ok3 else 'FAIL'}] 章节已渲染（{mp.tree.topLevelItemCount()} 行）")

        # ③ 状态栏必须有可见文字：以前 status_msg 是没人读的裸属性，
        #    解析全程界面无反馈，用户就以为「没反应」。
        ok4 = bool(mp.lb_status.text())
        print(f"[{'PASS' if ok4 else 'FAIL'}] 详情页状态可见 -> {mp.lb_status.text()!r}")

        # ④ 双击空条目不炸
        try:
            page._open_item(None or page.list.item(0))
            ok5 = True
        except Exception as exc:
            ok5 = False
            print(f"       空条目异常：{exc}")
        print(f"[{'PASS' if ok5 else 'FAIL'}] 双击异常条目不抛异常")

        # ⑤ 「打开网址…」必须走与双击相同的链路：
        #    以前它自己调 svc.load_comic，MangaPage._rid_c 从没被赋值，
        #    comic_loaded 到达时被详情页守卫拦掉 —— 输完网址「什么都没发生」。
        from mangakit.gui import main_window as _mw
        _URL = "https://example.invalid/book/9/"
        _orig = _mw.QInputDialog_getText
        _mw.QInputDialog_getText = lambda *a, **k: (_URL, True)
        try:
            page.open_by_url()
            app.processEvents()
            ok6 = (mp._url == _URL and mp._rid_c > 0)
            print(f"[{'PASS' if ok6 else 'FAIL'}] 「打开网址…」走详情页链路"
                  f" -> _url={mp._url!r} _rid_c={mp._rid_c}")
        finally:
            _mw.QInputDialog_getText = _orig

        win.close()
        return ok1 and ok2 and ok3 and ok4 and ok5 and ok6
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 双击检查异常：{exc}")
        return False


def _assert_isolated() -> bool:
    """开跑前确认数据目录确实隔离，绝不碰用户真库。

    这是 2026-10-14 收藏清空事故的直接护栏：当时本文件没设 MANGAKIT_HOME，
    检查函数里的 store() 直接落在 %APPDATA%\\mangakit\\mangakit.db 上。
    """
    from mangakit.config import app_dir
    d = app_dir().resolve()
    home = Path(os.environ["MANGAKIT_HOME"]).resolve()
    ok = d == home
    appdata = Path(os.environ.get("APPDATA") or Path.home()) / "mangakit"
    if not ok or d == appdata.resolve():
        print(f"[FAIL] 数据目录未隔离：app_dir()={d}")
        print("       测试会打到用户真库上，已中止。")
        return False
    print(f"[PASS] 数据目录已隔离：{d}")
    return True


def main() -> int:
    if not _assert_isolated():
        return 3
    picker_ok = check_site_picker()
    chapters_ok = check_chapter_picker()
    expand_ok = check_expand_button()
    stream_ok = check_search_streaming()
    fav_ok = check_favorites_delete()
    addsite_ok = check_add_site()
    dbl_ok = check_double_click_open()
    if not (picker_ok and chapters_ok and expand_ok and stream_ok and fav_ok
            and addsite_ok and dbl_ok):
        print("=== GUI 离线检查未全通过，仍继续跑 exe 启动验证 ===")
    if not EXE.is_file():
        print(f"找不到 exe：{EXE}")
        return 2
    shutil.rmtree(BASE, ignore_errors=True)
    tmp = BASE / "tmp"
    home = BASE / "home"
    tmp.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"first_run_done": True}, ensure_ascii=False), "utf-8")

    env = dict(os.environ)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    env["MANGAKIT_HOME"] = str(home)

    proc = subprocess.Popen([str(EXE)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            start_new_session=True)
    log = BASE / "gui.log"
    lines: list[str] = []

    def pump() -> None:
        """把子进程输出落到文件：管道不读会填满而把 GUI 进程卡死。"""
        try:
            for line in proc.stdout:
                line = line.rstrip()
                lines.append(line)
                log.write_text("\n".join(lines), "utf-8")
        except Exception:
            pass

    import threading
    t = threading.Thread(target=pump, daemon=True)
    t.start()

    time.sleep(30)
    alive = proc.poll() is None
    print(f"[{'PASS' if alive else 'FAIL'}] 存活 30s={alive}")
    text = "\n".join(lines)
    bad = [ln for ln in lines
           if "UnicodeEncodeError" in ln or "couldn't encode" in ln]
    print(f"[{'PASS' if not bad else 'FAIL'}] 无 ascii 编码错误"
          + (f" -> {bad[:3]}" if bad else ""))
    if "下载管理器已启动" in text:
        print("[PASS] 下载管理器已启动")
    else:
        print("[WARN] 日志里没有「下载管理器已启动」，可能启动卡在别处")

    if alive:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"=== GUI 启动验证完成（日志：{log}）===")
    all_ok = (alive and not bad and picker_ok and chapters_ok and expand_ok
              and stream_ok and fav_ok and addsite_ok and dbl_ok)
    if not all_ok:
        return 1
    # offscreen 平台没字体，PySide6 会往 stderr 打一堆 Populate font family from
    # fontconfig ... 之类、与本软件无关的警告；它们不该让冒烟测试背锅。
    # 真出问题时进程本身会非 0 退出，所以只把 stdout/stderr 里的异常行透出来看。
    noise = ("populating font family", "fontconfig", "unable to load default",
             "qt.qpa.fonts")
    for ln in lines:
        low = ln.lower()
        if any(n in low for n in noise):
            continue
        print("  |", ln)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    code = main()
    # 本脚本建过 MainWindow/SearchPage（含 CoverLoader 的 QThreadPool），退出前
    # 必须按序拆 Qt，否则 Windows 会弹「python.exe - 应用程序错误 ... 该内存不能
    # 为 read」。成因与实测数据见 tools/qt_teardown.py 的模块注释。
    from qt_teardown import teardown
    teardown()
    raise SystemExit(code)