"""验证单本超时闸门：故意给一个不可达地址，必须限时收尾而不是永远卡住。

跨线程 signal 靠 Qt 队列投递，纯 asyncio 等待收不到 —— 必须
QApplication + processEvents 泵事件（与 tools/t_upd_bench.py 同理）。

用法：python tools/t_upd_timeout.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PER_BOOK = 8.0
BUDGET = 45.0


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication([])

    import tempfile
    from pathlib import Path
    import mangakit.store as st
    from mangakit.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="mk_to_")) / "mangakit.db"
    test_store = Store(tmp)
    st._store = test_store
    # 黑洞地址：TCP 连不上，http 层会一路重试到超时
    test_store.add_favorite({"title": "黑洞", "url": "http://10.255.255.1/book/1/",
                             "site": "songzimanhua"})
    # 陪跑的好书：证明闸门只掐住坏书，不拖累正常书
    test_store.add_favorite({"title": "正常", "url": "https://songzimanhua.com/book/15293/",
                             "site": "songzimanhua"})

    from mangakit.downloader import manager
    from mangakit.service import ComicService
    from mangakit.gui.bridge import bridge
    mgr = manager(); mgr.start()
    svc = ComicService(mgr)

    box: dict = {}
    seen: list = []
    state = {"done": False}

    def on_partial(rid, up):
        seen.append((up.status, up.message[:40]))

    def on_done(rid, stat):
        box.update(stat)
        state["done"] = True

    bridge().update_partial.connect(on_partial)
    bridge().update_done.connect(on_done)

    t0 = time.time()
    svc.check_updates(concurrency=4, per_book=PER_BOOK)
    while time.time() - t0 < BUDGET and not state["done"]:
        app.processEvents()
        time.sleep(0.01)
    dt = time.time() - t0

    print(f"per_book={PER_BOOK:.0f}s  实测耗时={dt:.1f}s（预算 {BUDGET:.0f}s）")
    print(f"逐本事件: {seen}")
    print(f"汇总: {box}")

    ok = state["done"] and dt < BUDGET
    if not state["done"]:
        print(f"[FAIL] {BUDGET:.0f}s 仍未收尾 —— 闸门没生效")
    elif box.get("checked", 0) != 2:
        print(f"[FAIL] 应检查 2 本，实际 {box.get('checked')}")
        ok = False
    elif box.get("error", 0) != 1:
        print(f"[FAIL] 黑洞书应记 1 笔失败，实际 {box.get('error')}")
        ok = False
    else:
        print(f"[PASS] 黑洞书限时记失败（{dt:.1f}s 收尾），好书照常检查，未永久卡住")

    mgr.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
