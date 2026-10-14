"""真机验证：收藏更新检查。

用松子漫画（songzimanhua，1068 话）当样本：
  ① 收藏一本 -> 清掉基线 -> 第一次检查应报「首次检查」，并把三个基线字段写进库
  ② 把基线人为调小 5 -> 再检查应报「有 5 话更新」
  ③ 基线调到当前值 -> 再检查应报「无更新」
  ④ 总数不变、话号涨 -> 应报「有更新」（靠话号基准发现）
  ⑤ 不存在的站点 -> 应报「检查失败/不支持」
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
          f"latest_index={up.latest_index} latest_no={up.latest_no} total={up.total}")
    print(f"   最新章节 = {up.latest_title!r}")
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", (SITE, BOOK))
    baseline = row["last_index"]
    print(f"   库里的基线已记录 = index:{baseline} "
          f"total:{row['last_total']} no:{row['last_no']}")
    assert up.status in ("first", "ok"), f"首次检查状态异常: {up.status}"
    assert baseline >= 0, "首次检查没把基线写进库"
    assert row["last_total"] == up.total, "首次检查没把章节数基线写进库"
    assert row["last_no"] == up.latest_no, "首次检查没把话号基线写进库"
    assert up.new_count == 0, "首次检查不该谎报更新"
    # 「最新章节」必须是话号最大的那一话，不能是列表末尾
    assert up.latest_no >= 0, f"没解析出话号: {up.latest_title!r}"

    # ---------- 人为把基线调小 5 ----------
    # 三个基准一起调：只调 last_index 是没用的（它只是列表位置，判断更新不看它）
    test_store.mark_favorite_checked(SITE, BOOK, baseline,
                                     up.total - 5, up.latest_no - 5)
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up = _first_update(events)
    print(f"③ 基线调小 5 后 -> status={up.status} new_count={up.new_count} "
          f"by={up.by} display={up.display!r}")
    assert up.status == "new" and up.new_count == 5, f"应报 5 话更新，实为 {up.status}/{up.new_count}"
    assert up.has_update is True

    # ---------- 总数不变、话号涨 3（模拟站方删了两话又加了三话）----------
    # 注意：这里**不能断言 new_count == 3**。真实站点的话号是**有跳号**的
    # （实测 songzimanhua 1068 话里话号只到 1162，中间缺号、还有重复号），
    # 所以「话号涨了 3」不等于「多了 3 话」。代码数的是「旧最高话号之上
    # 实际存在几话」，这才是对用户有意义的口径。这里只断言「靠话号发现了
    # 更新」；精确计数在离线套件 tools\t_updates2.py 里用可控数据验证。
    test_store.mark_favorite_checked(SITE, BOOK, baseline,
                                     up.total, up.latest_no - 3)
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up = _first_update(events)
    print(f"④ 总数不变、话号涨 3 -> status={up.status} new_count={up.new_count} "
          f"by={up.by}")
    assert up.status == "new" and up.new_count >= 1, \
        f"应靠话号报出更新，实为 {up.status}/{up.new_count}"
    assert up.by == "no", f"应是由话号发现的更新，实为 {up.by}"

    # ---------- 基线调回当前值 ----------
    test_store.mark_favorite_checked(SITE, BOOK, up.latest_index,
                                     up.total, up.latest_no)
    events.clear()
    rid = svc.check_updates()
    _wait(events, expect_done=1, timeout=120)
    up2 = _first_update(events)
    print(f"⑤ 基线为最新时 -> status={up2.status} new_count={up2.new_count} "
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
    print(f"⑥ 未知站点 -> status={bad[0].status} message={bad[0].message!r}")
    assert bad and bad[0].status == "unsupported", "未知站点应报不支持"
    # 坏站点不该把基线写成 0 以外的值
    row = test_store.one("SELECT * FROM favorites WHERE site=?", ("nope_site",))
    print(f"   坏站基线 = {row['last_index']}（应保持 -1）")

    # ---------- 只检查选中的 ----------
    events.clear()
    rid = svc.check_updates([(SITE, BOOK)])
    _wait(events, expect_done=1, timeout=120)
    ups = [e for e in events if not isinstance(e, tuple)]
    print(f"⑦ 只检查选中 1 本 -> 收到 {len(ups)} 条结果（应为 1）")
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
