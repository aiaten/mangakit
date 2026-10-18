# -*- coding: utf-8 -*-
"""验证修复：条目在「封面还没到」时就必须是可点的。

修复前：CoverLoader.get() 未命中返回**空 QPixmap**，QListWidget 在
setUniformItemSizes(True) 下把统一尺寸冻成文本大小（实测 80×25），
封面异步到达后能画出来、但命中区域还是 80×25 → 点封面没反应。

修复后：get() 返回**满尺寸占位图**，sizeHint 从一开始就是带图标的尺寸。

本脚本用真 windows 平台跑，按网格格子中心点测 indexAt 命中率。
重点看 A2（封面到达之前）必须是 100%。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QPoint, QSize
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
from PySide6.QtTest import QTest

from mangakit.models import Comic
from mangakit.gui.main_window import SearchPage

ICON = QSize(140, 190)
GRID = QSize(170, 226)
N = 24


class DummySvc:
    def search_all(self, kw): return 1
    def search_site(self, sid, kw): return 1
    def load_comic(self, u): return 2
    def load_chapters(self, u, full=False): return 3


def batch(n=N):
    return [Comic(title=f"龙珠 第{i}卷", url=f"https://e.com/{i}.html",
                  site="songzimanhua", site_name="松子漫画",
                  cover=f"https://img.e.com/{i}.jpg") for i in range(n)]


def cover_pm(i):
    pm = QPixmap(ICON)
    pm.fill(QColor(40 + (i * 7) % 200, 90, 160))
    return pm


def cells(page, n=N):
    vp = page.list.viewport().size()
    cols = max(1, vp.width() // GRID.width())
    sb = page.list.verticalScrollBar().value()
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x = int((c + 0.5) * (vp.width() / cols))
        y = r * GRID.height() + GRID.height() // 2 - sb
        if 0 <= y < vp.height():
            out.append((i, QPoint(x, y)))
    return out


def report(page, label):
    r0 = page.list.visualItemRect(page.list.item(0)).getRect()
    print(f"\n[{label}]  条目0 rect = {r0}")
    img = page.list.viewport().grab().toImage()
    ok = empty = painted = 0
    pts = cells(page)
    for i, p in pts:
        idx = page.list.indexAt(p)
        if idx.isValid() and idx.row() == i:
            ok += 1
        elif not idx.isValid():
            empty += 1
        col = img.pixelColor(p)
        if col.saturation() > 30 or col.value() < 235:
            painted += 1
    tot = len(pts) or 1
    print(f"   >>> 格子中心命中自己 = {ok}/{tot}   点到空白 = {empty}   "
          f"像素非背景 = {painted}/{tot}")
    return ok, tot


def main():
    app = QApplication.instance() or QApplication([])
    print(f"平台 = {app.platformName()}  iconSize={ICON.toTuple()} grid={GRID.toTuple()}")
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    page = SearchPage(DummySvc())
    lay.addWidget(page)
    host.resize(1120, 760)
    host.show()
    QTest.qWait(400)

    page._reset()
    page._rid = 55
    page._pending = 1
    page._add_items(batch())
    QTest.qWait(400)
    okA1, totA1 = report(page, "A1 封面未到（修复后必须是满分）")

    # 模拟 CoverLoader.loaded 逐个回填真封面
    for i in range(N):
        page._on_cover(f"https://img.e.com/{i}.jpg", cover_pm(i))
    QTest.qWait(600)
    okA2, totA2 = report(page, "A2 封面全部回填之后")

    # 真双击：按格子中心点，看能否打开
    got = []
    page.open_comic.connect(lambda c: got.append(c.url))
    pts = cells(page)
    for i, p in pts[:4]:
        QTest.mouseClick(page.list.viewport(), Qt.LeftButton, Qt.NoModifier, p)
        QTest.mouseDClick(page.list.viewport(), Qt.LeftButton, Qt.NoModifier, p)
        QTest.qWait(80)
    print(f"\n   真双击（前 4 个格子中心）: 触发 {len(got)}/4  -> {got}")

    print("\n" + "=" * 70)
    good = okA1 == totA1 and okA2 == totA2 and len(got) >= 4
    print(f"  A1 可点率 {okA1}/{totA1}   A2 可点率 {okA2}/{totA2}   双击 {len(got)}/4")
    print("  >>> 修复生效：封面未到时也可点" if okA1 == totA1
          else "  >>> 仍未修好：封面未到时点不到")
    print("VERIFY OK" if good else "VERIFY FAIL")
    print("DONE")


if __name__ == "__main__":
    main()
    # 退出前按序拆 Qt（本脚本建了 SearchPage，含 CoverLoader 的 QThreadPool），
    # 否则 Windows 会弹「python.exe - 应用程序错误 ... 该内存不能为 read」。
    # 成因与实测数据见 tools/qt_teardown.py 的模块注释。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from qt_teardown import teardown
    teardown()
