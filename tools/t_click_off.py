# -*- coding: utf-8 -*-
"""离线可测的回归断言：条目在封面未到时也必须可点。

真机（windows 平台）上修复前后差异极大：
  修复前 条目 rect = 80×25   格子中心命中 0/18
  修复后 条目 rect = 154×218 格子中心命中 18/18

但 gui_smoke 跑在 offscreen 平台（无字体），几何数值不可比。所以回归断言
要挑**跨平台都成立**的不变量：条目图标必须是「满尺寸」的，不能是空图标。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QSize, QPoint
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
from mangakit.models import Comic
from mangakit.gui.main_window import SearchPage

ICON = QSize(140, 190)


class DummySvc:
    def search_all(self, kw): return 1
    def search_site(self, sid, kw): return 1
    def load_comic(self, u): return 2
    def load_chapters(self, u, full=False): return 3


def main():
    app = QApplication.instance() or QApplication([])
    print(f"平台 = {app.platformName()}")
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    page = SearchPage(DummySvc())
    lay.addWidget(page)
    host.resize(1120, 760)
    host.show()
    app.processEvents()

    page._reset()
    page._rid = 3
    page._pending = 1
    # cover 为空字符串 => 走 get("") 分支，也必须拿到满尺寸占位图
    page._add_items([
        Comic(title="无封面甲", url="https://e.com/1", site="s", site_name="S"),
        Comic(title="有封面乙", url="https://e.com/2", site="s", site_name="S",
              cover="http://10.255.255.1/x.jpg"),   # 不可达：必然走后台上取
    ])
    app.processEvents()

    it0, it1 = page.list.item(0), page.list.item(1)
    print(f"  条目0 rect = {page.list.visualItemRect(it0).getRect()}")
    print(f"  条目1 rect = {page.list.visualItemRect(it1).getRect()}")

    for name, it in (("无封面", it0), ("有封面未到", it1)):
        ic = it.icon()
        null = ic.isNull()
        sz = ic.pixmap(ICON).size()
        full = (not null) and sz == ICON
        print(f"[{'PASS' if full else 'FAIL'}] {name}条目图标是满尺寸占位图"
              f" -> null={null} size={sz.toTuple()}")

    # 命中测试：拿格子中心点问 indexAt（off 平台也要能命中）
    vp = page.list.viewport().size()
    print(f"  viewport = {vp.width()}x{vp.height()}")
    for i, it in ((0, it0), (1, it1)):
        r = page.list.visualItemRect(it)
        ctr = r.center()
        hit = page.list.indexAt(ctr)
        ok = hit.isValid() and hit.row() == i
        print(f"[{'PASS' if ok else 'FAIL'}] 条目{i} 自身矩形中心可命中"
              f" -> indexAt={hit.row() if hit.isValid() else None} 点=({ctr.x()},{ctr.y()})")

    # rid 守卫：旧搜索的残留信号必须被丢弃
    print("\n  --- rid 守卫 ---")
    page._reset()
    page._rid = 0
    page._pending = 1
    page._add_items([Comic(title="本次甲", url="u1", site="s", site_name="S")])
    app.processEvents()
    n_before = page.list.count()
    from mangakit.gui.bridge import bridge
    bridge().search_partial.emit(999, [Comic(title="旧站残留", url="u9", site="s",
                                            site_name="S")])
    bridge().search_done.emit(999, 20)
    app.processEvents()
    n_after = page.list.count()
    print(f"[{'PASS' if n_after == n_before else 'FAIL'}] 旧 rid 的残留结果不进列表"
          f" -> {n_before} -> {n_after}（应为 {n_before}）")
    print(f"[{'PASS' if page._pending else 'FAIL'}] 旧 rid 的 search_done 不提前收尾"
          f" -> _pending={page._pending}（应为 1）")

    # 正确 rid 必须正常放行
    bridge().search_partial.emit(page._rid, [Comic(title="本次乙", url="u2",
                                                  site="s", site_name="S")])
    app.processEvents()
    print(f"[{'PASS' if page.list.count() == n_after + 1 else 'FAIL'}] "
          f"正确 rid 的结果正常显示 -> {page.list.count()}（应为 {n_after + 1}）")
    print("DONE")


if __name__ == "__main__":
    main()
