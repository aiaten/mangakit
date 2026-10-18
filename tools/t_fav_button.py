# -*- coding: utf-8 -*-
"""离线回归：打开漫画**不再自动收藏**，只有点详情页的「收藏」按钮才收。

背景（用户需求 m02713 原文）：
    「现在搜索出来之后双击就自动收藏这个不好，以后不要自动收藏了，在搜索出来的
      漫画页面增加一个收藏按键，只有我点击了才收藏」

以前 `MangaPage._on_comic` 里直接 `store().add_favorite(...)` —— 用户只是搜出来
看看详情，收藏列表就被塞满，删起来还麻烦（这个自动写入也正是 2026-10-14
清空收藏事故的传播路径，见 tools/check_isolation.py 的说明）。

现在：
  - `_on_comic` 只把按钮状态同步成「这本是否已收藏」，绝不写库；
  - 详情页多一个「收藏」按钮，点一次加入、再点一次取消（文案「已收藏 ✓」）；
  - 换书时先清掉上一本的 comic，避免解析期间把上一本收藏了；
  - 收藏变化后发 `fav_changed` 信号，收藏页跟着重载。

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'; $env:PYTHONIOENCODING='utf-8'
    <py> tools\\t_fav_button.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 必须在 import mangakit 之前隔离数据目录（本脚本会真的写收藏表）
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_favbtn_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)

    from mangakit.downloader import manager
    from mangakit.gui.main_window import MangaPage
    from mangakit.models import Comic
    from mangakit.service import ComicService
    from mangakit.store import store

    mgr = manager()
    mgr.start()
    svc = ComicService(mgr)
    s = store()
    s.clear_favorites()

    mp = MangaPage(svc)

    # ---------------------------------------------------------------- 1
    print("== 1. 打开漫画不得自动收藏（核心需求）==")
    c = Comic(title="不该被自动收藏", url="https://example.invalid/auto/1/",
              site="fake", site_name="假站", author="作者A", cover="",
              tags=["标签1"])
    mp._rid_c = 11
    mp._on_comic(11, c)
    app.processEvents()
    check("打开后收藏表仍是空的", len(s.favorites()), 0)
    check("库里查不到这本", s.favorite_exists("fake", c.url), False)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 按钮在未收藏时的初始状态 ==")
    check("按钮可用", mp.btn_fav.isEnabled(), True)
    check("文案是「收藏」", mp.btn_fav.text(), "收藏")

    # ---------------------------------------------------------------- 3
    print("\n== 3. 点一下才收藏 ==")
    mp._toggle_fav()
    # 状态栏必须**同步**检查：_on_comic 里发的章节解析会在 processEvents 时
    # 失败（example.invalid 不存在），失败回执会盖掉收藏回执 —— 那是测试桩
    # 造成的时序，不是产品缺陷。
    status_after_add = mp.lb_status.text()
    app.processEvents()
    check("收藏表有 1 条", len(s.favorites()), 1)
    check("库里能查到", s.favorite_exists("fake", c.url), True)
    row = s.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", c.url))
    check("标题写对了", row["title"], "不该被自动收藏")
    check("作者写对了", row["author"], "作者A")
    check("按钮文案变「已收藏 ✓」", mp.btn_fav.text(), "已收藏 ✓")
    check("状态栏有回执", "已收藏" in status_after_add, True)

    # ---------------------------------------------------------------- 4
    print("\n== 4. 再点一下取消收藏 ==")
    mp._toggle_fav()
    status_after_del = mp.lb_status.text()
    app.processEvents()
    check("收藏表回到 0 条", len(s.favorites()), 0)
    check("库里查不到", s.favorite_exists("fake", c.url), False)
    check("按钮文案变回「收藏」", mp.btn_fav.text(), "收藏")
    check("状态栏提示取消", "已取消收藏" in status_after_del, True)

    # ---------------------------------------------------------------- 5
    print("\n== 5. 重复收藏不会产生重复行（ON CONFLICT 幂等）==")
    mp._toggle_fav()      # 加
    mp._toggle_fav()      # 取消
    mp._toggle_fav()      # 再加
    app.processEvents()
    check("反复切换后仍只有 1 条", len(s.favorites()), 1)

    # ---------------------------------------------------------------- 6
    print("\n== 6. 已收藏的书再打开，按钮应直接显示「已收藏 ✓」==")
    mp2 = MangaPage(svc)
    mp2._rid_c = 12
    mp2._on_comic(12, c)
    app.processEvents()
    check("重新打开后按钮显示已收藏", mp2.btn_fav.text(), "已收藏 ✓")
    check("重新打开没有新增行", len(s.favorites()), 1)

    # ---------------------------------------------------------------- 7
    print("\n== 7. 换书时先清掉上一本（防止收错对象）==")
    # 模拟：详情页正显示已收藏的 c，用户去搜另一本并双击
    mp2.load("https://example.invalid/other/9/")
    app.processEvents()
    check("load 后 comic 已清空", mp2.comic, None)
    check("load 后按钮置灰", mp2.btn_fav.isEnabled(), False)
    check("load 后文案回「收藏」", mp2.btn_fav.text(), "收藏")
    # 此时点按钮不该把上一本收进去
    before = len(s.favorites())
    mp2._toggle_fav()
    check("无 comic 时点收藏不写库", len(s.favorites()), before)

    # ---------------------------------------------------------------- 8
    print("\n== 8. fav_changed 信号：收藏变化要通知收藏页 ==")
    mp3 = MangaPage(svc)
    fired: list = []
    mp3.fav_changed.connect(lambda: fired.append(1))
    mp3._rid_c = 13
    other = Comic(title="另一本", url="https://example.invalid/other2/1/",
                  site="fake2", site_name="假站2")
    mp3._on_comic(13, other)
    app.processEvents()
    check("打开时不发 fav_changed", len(fired), 0)
    mp3._toggle_fav()
    check("收藏后发一次 fav_changed", len(fired), 1)
    mp3._toggle_fav()
    check("取消后再发一次", len(fired), 2)

    # ---------------------------------------------------------------- 9
    print("\n== 9. 收藏页 reload 能被信号驱动（接线上真的通）==")
    from mangakit.gui.main_window import MainWindow
    win = MainWindow()
    win.manga._rid_c = 14
    third = Comic(title="第三本", url="https://example.invalid/third/1/",
                  site="fake3", site_name="假站3")
    win.manga._on_comic(14, third)
    app.processEvents()
    win.manga._toggle_fav()
    app.processEvents()
    # 收藏页的表格里应该能看见这本（说明 fav_changed -> favs.reload 通了）
    titles = [win.favs.table.item(i, win.favs.COL_TITLE).text()
              for i in range(win.favs.table.rowCount())]
    check("收藏页出现了这本", "第三本" in titles, True)
    win.close()

    # ---------------------------------------------------------------- 10
    print("\n== 10. 源码里不得再有自动收藏 ==")
    import inspect
    src = inspect.getsource(MangaPage._on_comic)
    check("_on_comic 里没有 add_favorite", "add_favorite" in src, False)

    s.clear_favorites()
    mgr.stop()

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
