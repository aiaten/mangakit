# -*- coding: utf-8 -*-
"""离线回归：搜索结果列表右键菜单收藏 / 取消收藏。

背景（用户需求）：
    「在搜索出来的漫画页面增加一个收藏按键，只有我点击了才收藏」
    用户补充选择「两个都要」：详情页按钮 + 搜索结果列表右键菜单。

本脚本只测**搜索页**那半边（详情页按钮见 tools/t_fav_button.py）。
核心不变量：
  1. 搜索出结果后**不自动收藏**（右键菜单只是菜单，不弹就等于没收藏）
  2. 菜单文案随收藏状态切换：「收藏」↔「取消收藏」
  3. 只有真的点了菜单项才写库，且标题/作者/封面写对
  4. 收藏变化后发 fav_changed，收藏页能跟着重载
  5. 空白处右键不弹菜单（itemAt 返回 None）

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'; $env:PYTHONIOENCODING='utf-8'
    <py> tools\\t_search_fav.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 必须在 import mangakit 之前隔离数据目录（本脚本会真的写收藏表）
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_sfav_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def menu_texts(m):
    """菜单项文案列表（跳过分隔符）。"""
    return [a.text() for a in m.actions() if not a.isSeparator()]


class DummySvc:
    """不起后台线程的桩：本脚本只测列表与菜单，不测网络。"""
    def search_all(self, kw): return 1
    def search_site(self, sid, kw): return 1
    def load_comic(self, u): return 2
    def load_chapters(self, u, full=False): return 3


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)

    from mangakit.gui.main_window import SearchPage
    from mangakit.models import Comic
    from mangakit.store import store

    s = store()
    s.clear_favorites()
    page = SearchPage(DummySvc())

    c = Comic(title="右键收藏样本", url="https://example.invalid/search/1/",
              site="fakesite", site_name="假站", author="作者B", cover="",
              tags=["t1", "t2"])
    page._add_items([c])
    app.processEvents()

    # ---------------------------------------------------------------- 1
    print("== 1. 搜索出结果后不自动收藏 ==")
    check("收藏表仍是空的", len(s.favorites()), 0)
    check("库里查不到这本", s.favorite_exists("fakesite", c.url), False)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 未收藏时的菜单 ==")
    it = page.list.item(0)
    m = page._build_item_menu(it)
    check("菜单造出来了", m is not None, True)
    check("菜单项", menu_texts(m), ["打开详情", "收藏", "复制网址"])

    # ---------------------------------------------------------------- 3
    print("\n== 3. 点「收藏」才写库 ==")
    action = next(a for a in m.actions() if a.text() == "收藏")
    action.trigger()
    app.processEvents()
    check("收藏表有 1 条", len(s.favorites()), 1)
    check("库里能查到", s.favorite_exists("fakesite", c.url), True)
    row = s.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fakesite", c.url))
    check("标题写对了", row["title"], "右键收藏样本")
    check("作者写对了", row["author"], "作者B")
    check("状态栏有回执", "已收藏" in page.status.text(), True)

    # ---------------------------------------------------------------- 4
    print("\n== 4. 收藏后菜单变成「取消收藏」==")
    m2 = page._build_item_menu(page.list.item(0))
    check("菜单项", menu_texts(m2), ["打开详情", "取消收藏", "复制网址"])

    # ---------------------------------------------------------------- 5
    print("\n== 5. 点「取消收藏」移出 ==")
    next(a for a in m2.actions() if a.text() == "取消收藏").trigger()
    app.processEvents()
    check("收藏表回到 0 条", len(s.favorites()), 0)
    check("库里查不到", s.favorite_exists("fakesite", c.url), False)
    check("状态栏提示取消", "已取消收藏" in page.status.text(), True)

    # ---------------------------------------------------------------- 6
    print("\n== 6. 反复点不产生重复行（ON CONFLICT 幂等）==")
    for _ in range(3):
        mm = page._build_item_menu(page.list.item(0))
        next(a for a in mm.actions() if a.text() == "收藏").trigger()
        app.processEvents()
        mm2 = page._build_item_menu(page.list.item(0))
        next(a for a in mm2.actions() if a.text() == "取消收藏").trigger()
        app.processEvents()
    check("切了 3 轮后收藏表为空", len(s.favorites()), 0)
    page._set_item_favorite(c, True)
    page._set_item_favorite(c, True)
    check("连点两次收藏仍只有 1 条", len(s.favorites()), 1)

    # ---------------------------------------------------------------- 7
    print("\n== 7. fav_changed 信号（收藏页要跟着重载）==")
    fired: list = []
    page.fav_changed.connect(lambda: fired.append(1))
    page._set_item_favorite(c, False)
    check("取消后发一次", len(fired), 1)
    page._set_item_favorite(c, True)
    check("再加后发一次", len(fired), 2)

    # ---------------------------------------------------------------- 8
    print("\n== 8. 空白处右键不弹菜单 ==")
    check("无条目时返回 None", page._build_item_menu(None), None)

    class FakeItem:
        def data(self, role):
            return None

    check("条目数据不是 Comic 时返回 None", page._build_item_menu(FakeItem()), None)

    # ---------------------------------------------------------------- 9
    print("\n== 9. 接线上真的通：搜索页收藏 -> 收藏页出现 ==")
    from mangakit.gui.main_window import MainWindow
    win = MainWindow()
    win.search._add_items([Comic(title="搜索结果里的书", url="https://example.invalid/s/9/",
                                 site="fakesite2", site_name="假站2")])
    app.processEvents()
    wm = win.search._build_item_menu(win.search.list.item(0))
    next(a for a in wm.actions() if a.text() == "收藏").trigger()
    app.processEvents()
    titles = [win.favs.table.item(i, win.favs.COL_TITLE).text()
              for i in range(win.favs.table.rowCount())]
    check("收藏页出现了这本", "搜索结果里的书" in titles, True)
    win.close()

    # ---------------------------------------------------------------- 10
    print("\n== 10. 双击仍然是打开详情（右键菜单没抢双击）==")
    got: list = []
    page.open_comic.connect(lambda cc: got.append(cc))
    page._open_item(page.list.item(0))
    check("双击链路仍触发 open_comic", len(got), 1)
    check("带出来的是这本", got[0].url, c.url)
    # 右键菜单里的「打开详情」也走同一条链路
    m3 = page._build_item_menu(page.list.item(0))
    next(a for a in m3.actions() if a.text() == "打开详情").trigger()
    check("菜单「打开详情」也触发 open_comic", len(got), 2)

    # ---------------------------------------------------------------- 11
    print("\n== 11. 「收藏选中」按钮（列表下方那个）==")
    s.clear_favorites()
    page._reset()
    page._add_items([Comic(title="按钮样本", url="https://example.invalid/btn/1/",
                           site="fakesite3", site_name="假站3")])
    app.processEvents()
    check("没选中时按钮置灰", page.btn_fav.isEnabled(), False)
    check("没选中时文案", page.btn_fav.text(), "收藏选中")
    page.list.setCurrentRow(0)
    app.processEvents()
    check("选中后按钮可点", page.btn_fav.isEnabled(), True)
    check("未收藏时文案", page.btn_fav.text(), "收藏选中")
    check("此时还没写库", len(s.favorites()), 0)
    page._toggle_sel_fav()
    app.processEvents()
    check("点了才收藏", len(s.favorites()), 1)
    check("库里能查到", s.favorite_exists("fakesite3", "https://example.invalid/btn/1/"), True)
    check("文案变「已收藏 ✓」", page.btn_fav.text(), "已收藏 ✓")
    page._toggle_sel_fav()
    app.processEvents()
    check("再点取消", len(s.favorites()), 0)
    check("文案变回「收藏选中」", page.btn_fav.text(), "收藏选中")
    # 清空结果后按钮必须跟着置灰，否则会对着空气点
    page._reset()
    app.processEvents()
    check("清空结果后置灰", page.btn_fav.isEnabled(), False)

    s.clear_favorites()

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    code = main()
    # 必须在 sys.exit 之前按顺序拆 Qt，否则 Windows 会弹
    # 「python.exe - 应用程序错误 ... 该内存不能为 read」。
    # 原因与实测数据见 tools/qt_teardown.py 的模块注释。
    from qt_teardown import teardown
    teardown()
    sys.exit(code)
