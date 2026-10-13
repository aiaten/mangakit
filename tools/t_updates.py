"""真机验证：收藏更新检查。

用松子漫画（songzimanhua，1068 话）当样本：
  ① 收藏一本 -> 清掉 last_index -> 第一次检查应报「首次检查」
  ② 把 last_index 人为调小 5 -> 再检查应报「有 5 话更新」
  ③ last_index 调到当前值 -> 再检查应报「无更新」
  ④ 不存在的站点 -> 应报「检查失败/不支持」
用法： python tools\t_updates.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 跨线程 signal 需要一个存在的 QApplication 才能走队列投递
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mangakit.downloader import manager          # noqa: E402
from mangakit.service import ComicService        # noqa: E402
from mangakit.store import Store                 # noqa: E402

BOOK = "https://songzimanhua.com/book/15293/"
SITE = "songzimanhua"


def main() -> int:
    # 用临时库，别污染用户真实收藏
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="mk_upd_")) / "mangakit.db"

    import mangakit.store as st
    test_store = Store(tmp)
    st._store = test_store          # 让 store() 返回我们的临时库

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    mgr = manager()
    mgr.start()
    svc = ComicService(mgr)

    events: list = []
    from mangakit.gui.bridge import bridge
    br = bridge()
    br.update_partial.connect(lambda rid, up: events.append(up))
    br.update_done.connect(lambda rid, s: events.append(("DONE", s)))

    test_store.add_favorite({"title": "海贼王", "url": BOOK, "site": SITE})
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", (SITE, BOOK))
    print(f"① 收藏已建：title={row['title']} last_index={row['last_index']} "
          f"last_check={row['last_check']:.0f}")

    # ---------- 第一次检查 ----------
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up = _first_update(events)
    print(f"② 首次检查 -> status={up.status} new_count={up.new_count} "
          f"latest_index={up.latest_index} total={up.total}")
    print(f"   最新章节 = {up.latest_title!r}")
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", (SITE, BOOK))
    baseline = row["last_index"]
    print(f"   库里的基线已记录 = {baseline}")
    assert up.status in ("first", "ok"), f"首次检查状态异常: {up.status}"
    assert baseline >= 0, "首次检查没把基线写进库"
    assert up.new_count == 0, "首次检查不该谎报更新"

    # ---------- 人为把基线调小 5 ----------
    test_store.mark_favorite_checked(SITE, BOOK, baseline - 5)
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up = _first_update(events)
    print(f"③ 基线调小 5 后 -> status={up.status} new_count={up.new_count} "
          f"display={up.display!r}")
    assert up.status == "new" and up.new_count == 5, f"应报 5 话更新，实为 {up.status}/{up.new_count}"
    assert up.has_update is True

    # ---------- 基线调回当前值 ----------
    test_store.mark_favorite_checked(SITE, BOOK, up.latest_index)
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up2 = _first_update(events)
    print(f"④ 基线为最新时 -> status={up2.status} new_count={up2.new_count} "
          f"display={up2.display!r}")
    assert up2.status == "ok" and up2.new_count == 0, "不该报更新"
    assert up2.has_update is False

    # ---------- 坏站点 ----------
    test_store.add_favorite({"title": "坏站书", "url": "https://nope.invalid/book/1/",
                             "site": "nope_site"})
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=60)
    ups = [e for e in events if not isinstance(e, tuple)]
    bad = [u for u in ups if u.site == "nope_site"]
    print(f"⑤ 未知站点 -> status={bad[0].status} message={bad[0].message!r}")
    assert bad and bad[0].status == "unsupported", "未知站点应报不支持"
    # 坏站点不该把基线写成 0 以外的值
    row = test_store.one("SELECT * FROM favorites WHERE site=?", ("nope_site",))
    print(f"   坏站基线 = {row['last_index']}（应保持 -1）")

    # ---------- 只检查选中的 ----------
    events.clear()
    rid = svc.check_updates([(SITE, BOOK)])
    _wait(events, expect_done=1, timeout=120)
    ups = [e for e in events if not isinstance(e, tuple)]
    print(f"⑥ 只检查选中 1 本 -> 收到 {len(ups)} 条结果（应为 1）")
    assert len(ups) == 1, "筛选没生效"

    mgr.stop()
    print("\nALL UPDATE CHECKS PASSED")
    return 0


def _first_update(events):
    ups = [e for e in events if not isinstance(e, tuple)]
    assert ups, f"没收到任何 update_partial: {events}"
    return ups[0]


def _wait(events, *, expect_done: int, timeout: float) -> None:
    """等 update_done。

    signal 是从后台线程发出来的，跨线程投递要靠 Qt 的队列连接，
    **必须由主线程泵事件循环**才能送达；只 time.sleep 的话槽永远不会被调用，
    表现为「一个信号都收不到」——这正是本脚本早先超时的原因。
    """
    import time
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    t0 = time.time()
    while time.time() - t0 < timeout:
        if app is not None:
            app.processEvents()
        if sum(1 for e in events if isinstance(e, tuple)) >= expect_done:
            app and app.processEvents()
            return
        time.sleep(0.01)
    raise TimeoutError(f"等 update_done 超时，已收到: {events}")


if __name__ == "__main__":
    raise SystemExit(main())
