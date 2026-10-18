# -*- coding: utf-8 -*-
"""测试脚本的 Qt 收尾工具：保证进程干净退出，不再弹「应用程序错误」对话框。

问题现象
--------
只要脚本里建过 MangaPage / MainWindow 这类带 QThreadPool 的控件，跑完退出时
Windows 会随机弹出：

    python.exe - 应用程序错误
    0x00007FFC8E0AC91E 指令引用了 0x0000000000000208 内存。
    该内存不能为 read。

进程退出码 0xC0000005（访问冲突），stderr 上还会看到
`QThreadStorage: entry 1 destroyed before end of thread 0x...`。
如果脚本里起过内置浏览器（QWebEngineView），还会多一行：

    Release of profile requested but WebEnginePage still not deleted.
    Expect troubles !

根因（两条，互不相同，都得治）
------------------------------
1. **线程没收工**：解释器收尾时 Python 对象和 Qt 对象的析构顺序是随机的。
   `CoverLoader` 的 `QThreadPool` 是 `MangaPage` 的子对象，而 `QApplication`
   可能先被销毁 —— 池里的取封面线程随后再碰 Qt 全局状态，就是一次访问冲突。
   所以要 `waitForDone()` 先等线程收工。

2. **deleteLater 根本没跑**：`deleteLater()` 只是投递一个
   `QEvent.Type.DeferredDelete` 事件，而 `processEvents()` **不会**处理
   DeferredDelete（Qt 只在事件循环里、且 loop level 匹配时才发）。于是窗口
   和 QWebEngineView 一直活到解释器退出，Qt 的 profile 先被释放，随后
   WebEnginePage 析构时去碰已释放的 profile = 访问冲突。所以必须显式
   `sendPostedEvents(None, QEvent.Type.DeferredDelete)`。

   ★ 坑：`sendPostedEvents(None, 0)` 的 0 **不是** DeferredDelete，而是
   「所有事件」；这么写会把别的已投递事件（含内部删除链上的）一起放出来，
   实测照样崩。必须传 `int(QEvent.Type.DeferredDelete)`（= 52）。

实测数据（每项各跑 10~15 次，`QT_QPA_PLATFORM=windows` / offscreen）
------------------------------------------------------------------
| 退出方式                                              | 崩溃次数 |
|-------------------------------------------------------|----------|
| 裸 `sys.exit(0)`（什么都不做）                        | 0/15，但场景一复杂就偶发 |
| `os._exit(0)`（跳过析构）                             | 8/10 —— 更糟，别用 |
| `waitForDone` + close + `processEvents`（无 Deferred） | 4/4（起过 WebEngine 时必崩） |
| `waitForDone` + close + `sendPostedEvents(None, 0)`    | 4/4（同上，0 = 所有事件，没用） |
| 本模块 `teardown()`（waitForDone + close + 显式 DeferredDelete） | 0/12（t_gui_new_pages 含 WebEngine） |

`os._exit()` 反而更容易崩，这点很反直觉：它跳过了 Python 侧的清理，但 Qt 的
静态对象和线程仍在收尾，等于把「先等线程收工」这一步彻底省掉了。**别再用
`os._exit()` 来绕这个问题。**

用法
----
    from qt_teardown import teardown

    if __name__ == "__main__":
        code = main()
        teardown()          # 传 app 也行：teardown(app)
        sys.exit(code)

也可以让模块自己挂到 atexit 上（适合不想改 `__main__` 的脚本）：

    import qt_teardown
    qt_teardown.install()      # 之后正常 sys.exit() 即可

注意：必须在**建过 Qt 对象之后**调用 install()/teardown() 才有意义；模块本身
在 import 时不碰 Qt，所以放在文件顶部也安全。
"""
from __future__ import annotations

import atexit

# 收尾时等线程池的秒数。取封面线程是网络请求，5 秒足够；超时也继续往下走。
_WAIT_MS = 5000


def teardown(app=None) -> None:
    """按正确顺序拆掉 Qt 对象。重复调用安全，没建过 Qt 对象时是空操作。

    顺序有讲究，别调换：

    1. 停下载管理器的 asyncio 守护线程（mangakit-loop）；
    2. `waitForDone` —— 让线程池里的取封面线程全部结束。否则后面销毁窗口时
       线程还在跑，等于把 QPixmap 从它脚下抽走；
    3. `close` —— 关顶层窗口，触发各页自己的清理；
    4. `deleteLater` + **显式发 DeferredDelete** —— 让窗口、QWebEngineView、
       QWebEnginePage 真的在这一刻被销毁，而不是拖到解释器退出；
    5. `processEvents` / `quit` —— 把剩下的投递事件跑完，退出事件循环。
    """
    try:
        from PySide6.QtCore import QEvent, QThreadPool
        from PySide6.QtWidgets import QApplication
    except Exception:          # 没装 PySide6 / 不是 Qt 脚本：无事可做
        return

    inst = app if app is not None else QApplication.instance()
    if inst is None:           # 压根没建过 QApplication，Qt 没起来，别乱动
        return

    # 1. 下载管理器的 asyncio 线程
    try:
        from mangakit.downloader import manager
        manager().stop()
    except Exception:          # noqa: BLE001 - 收尾失败不该改变断言结果
        pass

    # 2. 等线程池收工
    try:
        QThreadPool.globalInstance().waitForDone(_WAIT_MS)
    except Exception:          # noqa: BLE001
        pass

    # 3. 关窗口
    tops = []
    try:
        tops = list(QApplication.topLevelWidgets())
    except Exception:          # noqa: BLE001
        pass
    for w in tops:
        try:
            w.close()
        except Exception:      # noqa: BLE001
            pass

    # 4. 投递删除并立刻跑掉 DeferredDelete
    for w in tops:
        try:
            w.deleteLater()
        except Exception:      # noqa: BLE001
            pass
    _flush_deletes(inst, QEvent)

    # 5. 退出事件循环
    try:
        inst.quit()
    except Exception:          # noqa: BLE001
        pass
    try:
        inst.processEvents()
    except Exception:          # noqa: BLE001
        pass


def _flush_deletes(inst, QEvent) -> None:
    """把已投递的 DeferredDelete 事件真正发出去（processEvents 不会发它们）。

    连跑两轮：第一轮删顶层窗口，窗口的析构会再投递子对象（QWebEngineView
    等）的删除事件，第二轮把它们也跑掉。中间插 processEvents 让析构里排队的
    普通事件先落地。
    """
    deferred = int(QEvent.Type.DeferredDelete)
    for _ in range(2):
        try:
            inst.processEvents()
            inst.sendPostedEvents(None, deferred)
        except Exception:      # noqa: BLE001
            return
    try:
        inst.processEvents()
    except Exception:          # noqa: BLE001
        pass


def install() -> None:
    """把 teardown() 挂到 atexit 上，之后正常 sys.exit() 就能干净退出。"""
    atexit.register(teardown)
