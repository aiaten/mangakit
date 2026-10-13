"""收藏「检查更新」提速基准（QApplication + processEvents 泵信号）。

跨线程 signal 靠 Qt 队列投递，纯 asyncio 等待收不到 —— 必须像 gui_smoke
那样用 QApplication 泵事件。

用法：python tools/t_upd_bench.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BOOKS = [
    ("manhuagui_cc", "https://manhuagui.cc/comic/longzhu"),
    ("songzimanhua", "https://songzimanhua.com/book/15293/"),
    ("songzimanhua", "https://songzimanhua.com/book/19818/"),
    ("comicwang", "https://www.comicwang.com/title/9286.html"),
    ("dmanhua", "https://www.dmanhua.com/comic/5758"),
    ("dingmanhua", "https://www.dingmanhua.com/manhua/6614/"),
    ("manhuahome", "https://www.manhuahome.com/manhua/1/"),
    ("mh250", "https://www.mh250.com/manhua/1/"),
]


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication([])

    import tempfile
    from pathlib import Path
    import mangakit.store as st
    from mangakit.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="mk_bench_")) / "mangakit.db"
    test_store = Store(tmp)
    st._store = test_store
    for i, (site, url) in enumerate(BOOKS):
        test_store.add_favorite({"title": f"书{i}", "url": url, "site": site})

    from mangakit.downloader import manager
    from mangakit.service import ComicService
    from mangakit.gui.bridge import bridge
    mgr = manager(); mgr.start()
    svc = ComicService(mgr)

    for conc in (1, 2, 4, 6):
        box: dict = {}
        state = {"done": False}

        def on_done(rid, stat):
            box.update(stat)
            state["done"] = True

        bridge().update_done.connect(on_done)
        t0 = time.time()
        svc.check_updates(concurrency=conc, per_book=20.0)
        while time.time() - t0 < 90 and not state["done"]:
            app.processEvents()
            time.sleep(0.01)
        dt = time.time() - t0
        if state["done"]:
            print(f"并发={conc}  总耗时={dt:5.2f}s  "
                  f"检查={box.get('checked')}/{box.get('total')}  失败={box.get('error')}  "
                  f"有更新={box.get('new')}", flush=True)
        else:
            print(f"并发={conc}  **90s 超时**", flush=True)
        bridge().update_done.disconnect(on_done)

    mgr.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
