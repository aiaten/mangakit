"""Qt 与异步下载器之间的线程桥。

后台事件循环里的回调必须通过 Qt signal 回到主线程，否则操作 widget 会崩。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    """把后台线程的通知转成主线程 signal。"""

    task_changed = Signal(object)      # Task
    task_added = Signal(object)        # Task
    log_line = Signal(str, str)        # (level, message)

    comic_loaded = Signal(object, object)     # (request_id, Comic)
    chapters_loaded = Signal(object, object)  # (request_id, list[Chapter])
    pages_loaded = Signal(object, object)     # (request_id, list[str])
    search_done = Signal(object, object)      # (request_id, list[Comic])
    search_partial = Signal(object, object)   # (request_id, list[Comic]) 单站先到先发
    failed = Signal(object, str)              # (request_id, message)

    # 收藏更新检查：一条一条推，中途就能看到结果，不用等全部站点跑完
    update_partial = Signal(object, object)   # (request_id, FavoriteUpdate)
    update_done = Signal(object, object)      # (request_id, dict 汇总)

    progress = Signal(object, int, int)       # (request_id, done, total)


_bridge: Bridge | None = None


def bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge