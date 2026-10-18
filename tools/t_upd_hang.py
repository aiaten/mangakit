"""复现 GUI「检查更新」卡住：走完整链路，看信号到底卡在哪。

用法：python tools/t_upd_hang.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication([])

    import tempfile
    from pathlib import Path
    import mangakit.store as st
    from mangakit.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="mk_hang_")) / "mangakit.db"
    test_store = Store(tmp)
    st._store = test_store

    from mangakit.downloader import manager
    from mangakit.gui.main_window import MainWindow
    from mangakit.gui.bridge import bridge

    mgr = manager(); mgr.start()
    win = MainWindow(); win.show()

    # 造一条与用户截图完全一致的收藏
    test_store.add_favorite({"title": "龙珠", "url": "https://manhuagui.cc/comic/longzhu",
                             "site": "manhuaguitt".replace("tt", "_cc"), "author": ""})
    fp = win.favs
    fp.reload()
    app.processEvents()
    print(f"收藏行数 = {fp.table.rowCount()}")
    print(f"按钮文字 = {fp.btn_check.text()!r}  enabled={fp.btn_check.isEnabled()}")

    events: list = []
    bridge().update_partial.connect(lambda rid, up: events.append(("partial", rid, up.status)))
    bridge().update_done.connect(lambda rid, s: events.append(("DONE", rid, s)))
    bridge().failed.connect(lambda rid, m: events.append(("FAILED", rid, m[:60])))

    print("\n--- 点「检查更新」 ---")
    t0 = time.time()
    fp._check_updates()
    print(f"点后立刻: btn={fp.btn_check.text()!r} count={fp.lb_count.text()!r}")

    # 泵事件循环最多 180s，看信号何时到
    last = ""
    while time.time() - t0 < 180:
        app.processEvents()
        cur = f"btn={fp.btn_check.text()!r} count={fp.lb_count.text()!r}"
        if cur != last:
            print(f"  [{time.time()-t0:6.1f}s] {cur}")
            last = cur
        if any(e[0] == "DONE" for e in events):
            break
        time.sleep(0.02)

    print(f"\n耗时 = {time.time()-t0:.1f}s")
    print(f"事件 = {events}")
    print(f"最终 btn={fp.btn_check.text()!r} enabled={fp.btn_check.isEnabled()}")
    print(f"最终 count={fp.lb_count.text()!r}")

    if not any(e[0] == "DONE" for e in events):
        print("\n>>> 卡住了：update_done 从未到达")

    mgr.stop()
    return 0


if __name__ == "__main__":
    code = main()
    # 退出前按序拆 Qt（本脚本建了 MainWindow），否则 Windows 会弹
    # 「python.exe - 应用程序错误 ... 该内存不能为 read」。
    # 成因与实测数据见 tools/qt_teardown.py 的模块注释。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from qt_teardown import teardown
    teardown()
    raise SystemExit(code)
